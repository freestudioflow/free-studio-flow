"""Fixed post-production operations dispatched by the existing PlanExecutor."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.conformance.media import receipt_authorizes_production
from fsf.core.asset_library import _no_links, _open_reader
from fsf.core.edit_store import edit_lock, read_json, EditStoreError
from fsf.core.audio_edit import AudioError, enabled
from fsf.runtime.audio_mix import ACTIVE_DEADLINE, Deadline, resolve_sources, render_audio, numeric_report_valid
from fsf.core.editing import _shape, inspect_edit, validate_edit
from fsf.runtime.post_plan import compile_post_plan, validate_post_plan
from fsf.runtime.post_render import FFMPEG_CALLS, render, tool_version
from fsf.runtime.post_sources import PostError, SourceVerifier, file_sha, safe_file


def directory(path):
    path = Path(path).absolute()
    existing = path
    while not existing.exists():
        existing = existing.parent
    _no_links(existing)
    path.mkdir(parents=True, exist_ok=True)
    _no_links(path)
    return path.resolve()


def write_json(path, value):
    path = Path(path)
    directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".post-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_post_receipt(folder):
    try:
        folder = Path(folder)
        receipt = read_json(safe_file(folder, "receipt.json"))
        _shape(receipt, "fsf_post_receipt_v1")
        if digest({k: v for k, v in receipt.items() if k != "receipt_digest"}) != receipt["receipt_digest"]:
            return False
        names = [f["path"] for f in receipt["files"]]
        if len(names) != len(set(names)) or not {"movie.mp4", "qc.json", "source_manifest.json"}.issubset(names):
            return False
        for file in receipt["files"]:
            if file_sha(safe_file(folder, file["path"])) != file["sha256"]:
                return False
        report = read_json(safe_file(folder, "qc.json"))
        manifest = read_json(safe_file(folder, "source_manifest.json"))
        _shape(manifest, "fsf_post_source_manifest_v1")
        clips = receipt["clips"]
        bindings = [item["source_id"] for item in manifest["bindings"]]
        if (len(bindings) != len(set(bindings)) or len({c["clip_id"] for c in clips}) != len(clips)
                or any(c["source_id"] not in bindings or c["out_frame"] <= c["in_frame"] for c in clips)
                or receipt["total_frames"] != sum(c["out_frame"] - c["in_frame"] for c in clips)
                or set(names) - {"movie.mp4", "qc.json", "source_manifest.json", "captions.srt", "audio_report.json", "audio_analysis.json"}):
            return False
        if "audio_report.json" in names:
            audio_report = read_json(safe_file(folder, "audio_report.json"))
            if (audio_report.get("report_digest") != digest({k:v for k,v in audio_report.items() if k != "report_digest"})
                    or audio_report.get("artifact_sha256") != file_sha(safe_file(folder, "movie.mp4"))
                    or not numeric_report_valid(audio_report)):
                return False
        return (report.get("evaluation_status") == "conforms" and digest(report) == receipt["qc_digest"]
                and digest(manifest) == receipt["source_manifest_digest"] and manifest.get("edit_digest") == receipt["edit_digest"])
    except Exception:
        return False


class PostContext:
    def __init__(self, plan, configurations, audio_resources=None):
        self.context = validate_post_plan(plan)
        self.edit = self.context["edit"]
        self.verifier = SourceVerifier(self.edit, configurations)
        self.plan = plan
        self.plan_digest = digest(plan)
        self.version = tool_version()
        self.audio_resources = audio_resources
        self.audio_bindings = None
        self.audio_sources = {}
        self.bindings = None

    def check(self):
        sources = self.verifier.verify()
        if enabled(self.edit):
            self.audio_sources = resolve_sources(self.edit, sources, self.audio_resources)
            bindings = [item["binding"] for item in self.audio_sources.values()]
            if self.audio_bindings is not None and bindings != self.audio_bindings:
                raise AudioError("AUDIO_SOURCE_INVALID")
            self.audio_bindings = bindings
        manifest = {"schema_version": "fsf_post_source_manifest_v1", "edit_digest": self.edit["edit_digest"],
                    "bindings": [sources[s["source_id"]]["binding"] for s in self.edit["sources"] if s["source_id"] in sources]}
        _shape(manifest, "fsf_post_source_manifest_v1")
        if self.bindings is not None and manifest != self.bindings:
            from fsf.runtime.private_diagnostics import source_changed
            source_changed('binding_recheck')
            raise PostError("POST_SOURCE_CHANGED")
        self.bindings = manifest
        return sources

    def execute(self, step, output):
        sources = self.check()
        operation = step["transform"]
        if operation == "fsf.edit.resolve_sources":
            folder = directory(output / "sources")
            for source_id, item in sources.items():
                path = folder / f"{source_id}.mp4"
                # Copy exclusively into the attempt; validate the bytes actually used.
                temporary = folder / f".src-{uuid.uuid4().hex[:8]}.tmp"
                with _open_reader(item["path"]) as src, temporary.open("xb") as dst:
                    import stat
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        raise PostError("POST_SOURCE_COPY")
                    remaining = item["binding"]["size_bytes"]
                    while chunk := src.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise PostError("POST_SOURCE_COPY")
                        dst.write(chunk)
                    dst.flush(); os.fsync(dst.fileno())
                if file_sha(temporary) != item["binding"]["sha256"] or temporary.stat().st_size != item["binding"]["size_bytes"]:
                    raise PostError("POST_SOURCE_COPY")
                os.replace(temporary, path)
            if enabled(self.edit):
                from fsf.runtime.audio_mix import decode
                from fsf.runtime.post_render import run_ffmpeg
                audio_folder = directory(folder / "audio")
                rows = []
                token = ACTIVE_DEADLINE.set(Deadline())
                try:
                    for index,(identifier,item) in enumerate(self.audio_sources.items()):
                        pcm = audio_folder / f"decode_{index}.f32"
                        samples = decode(item["path"],item["binding"]["stream_index"],pcm)
                        if samples != item["binding"].get("canonical_samples",samples) or file_sha(item["path"]) != item["sha256"]:
                            raise AudioError("AUDIO_SOURCE_INVALID")
                        wav = audio_folder / f"{identifier}.wav"
                        from fsf.runtime.audio_mix import pcm_input
                        run_ffmpeg([*pcm_input(pcm),"-c:a","pcm_f32le",wav],audio_folder)
                        rows.append({"audio_source_id":identifier,"binding":item["binding"],"path":f"sources/audio/{identifier}.wav","sha256":file_sha(wav),"canonical_samples":samples})
                        pcm.unlink()
                    audio_manifest = {"edit_digest":self.edit["edit_digest"],"sources":rows}
                    audio_manifest["manifest_digest"] = digest(audio_manifest)
                    write_json(folder / "audio_manifest.json",audio_manifest)
                finally:
                    ACTIVE_DEADLINE.reset(token)
            self.check()
            write_json(folder / "manifest.json", self.bindings)
        elif operation == "fsf.edit.assemble":
            for source_id, item in sources.items():
                path = safe_file(output, f"sources/{source_id}.mp4")
                if file_sha(path) != item["binding"]["sha256"]:
                    raise PostError("POST_MATERIALIZED_SHA")
                item["path"] = path
            folder = directory(output / "staging")
            if enabled(self.edit):
                token = ACTIVE_DEADLINE.set(Deadline())
                try:
                    settings = {**self.context, "audio":"strip"}
                    video, _ = render(self.edit, settings, sources, folder)
                    materialized = read_json(safe_file(output,"sources/audio_manifest.json"))
                    if (materialized.get("manifest_digest") != digest({k:v for k,v in materialized.items() if k != "manifest_digest"})
                            or materialized.get("edit_digest") != self.edit["edit_digest"]):
                        raise AudioError("AUDIO_SOURCE_INVALID")
                    audio_sources = {}
                    for item in materialized["sources"]:
                        original = self.audio_sources.get(item["audio_source_id"])
                        if original is None or original["binding"] != item["binding"]:
                            raise AudioError("AUDIO_SOURCE_INVALID")
                        path = safe_file(output,item["path"])
                        if file_sha(path) != item["sha256"]:
                            raise AudioError("AUDIO_SOURCE_INVALID")
                        audio_sources[item["audio_source_id"]] = {"path":path,"sha256":item["sha256"],"binding":item["binding"],"decode_stream_index":0}
                    if set(audio_sources) != set(self.audio_sources):
                        raise AudioError("AUDIO_SOURCE_INVALID")
                    processed, audio_report = render_audio(self.edit, audio_sources, video, directory(folder / "audio"),cache=output.parents[1]/"audio_cache")
                    os.replace(processed, video)
                    write_json(folder / "audio_report.json", audio_report)
                    analysis = {"schema_version":"fsf_audio_analysis_v1","edit_digest":self.edit["edit_digest"],"groups":audio_report["groups"],
                        "source_digest":audio_report["source_digest"],"listening_review":"not_reviewed"}
                    analysis["analysis_digest"] = digest(analysis)
                    write_json(folder / "audio_analysis.json",analysis)
                finally:
                    ACTIVE_DEADLINE.reset(token)
            else:
                render(self.edit, self.context, sources, folder)
            self.check()
        elif operation == "fsf.edit.publish":
            folder = output / "delivery"
            if folder.exists():
                raise PostError("POST_BUNDLE_EXISTS")
            stage = directory(output / (".bundle-" + uuid.uuid4().hex))
            evidence = read_json(safe_file(output, "staging/qc.json.evidence.json"))
            expected = self.plan["steps"][2]["inputs"]["expected_media_contract"]
            movie = safe_file(output, "staging/movie.mp4")
            report = read_json(safe_file(output, "staging/qc.json"))
            if evidence.get("report") != report or not receipt_authorizes_production(evidence, artifact_sha256=file_sha(movie), artifact_name="movie.mp4", required_expected_digest=digest(expected)):
                raise PostError("POST_FINAL_QC")
            names = ["movie.mp4", "qc.json"] + (["captions.srt"] if self.context["subtitles"] != "none" else [])
            if enabled(self.edit):
                names.extend(["audio_report.json", "audio_analysis.json"])
            for name in names:
                source = safe_file(output, "staging/" + name)
                shutil.copyfile(source, stage / name)
                if file_sha(stage / name) != file_sha(source):
                    raise PostError("POST_BUNDLE_COPY")
            write_json(stage / "source_manifest.json", self.bindings)
            names.append("source_manifest.json")
            receipt = {"schema_version": "fsf_post_receipt_v1", "edit_id": self.edit["edit_id"], "revision": self.edit["revision"],
                "edit_digest": self.edit["edit_digest"], "post_plan_digest": self.plan_digest, "source_manifest_digest": digest(self.bindings),
                "clips": copy.deepcopy(self.edit["clips"]), "clock_rate": copy.deepcopy(self.edit["clock_rate"]),
                "total_frames": sum(c["out_frame"] - c["in_frame"] for c in self.edit["clips"]),
                "files": [{"path": name, "sha256": file_sha(stage / name)} for name in names],
                "tool_version": self.version, "qc_digest": digest(report), "visual_review": "not_evaluated"}
            receipt["receipt_digest"] = digest(receipt)
            _shape(receipt, "fsf_post_receipt_v1")
            write_json(stage / "receipt.json", receipt)
            if not verify_post_receipt(stage):
                raise PostError("POST_RECEIPT")
            self.check()
            os.rename(stage, folder)
        else:
            raise PostError("POST_OPERATION")
        outputs = [str(safe_file(output, ref.split(":", 1)[1])) for ref in step["outputs"]]
        return {"id": step["id"], "kind": "transform", "status": "completed", "execution_kind": "post", "output_artifacts": outputs}


def _valid_attempt(folder):
    try:
        journal = read_json(safe_file(folder, "fsf_execution_journal.json"))
        for state in journal["steps"].values():
            if state.get("status") == "completed":
                for row in state.get("output_files", []):
                    if file_sha(safe_file(folder, row["path"])) != row["sha256"]:
                        return False
        if (folder / "delivery").exists() and not verify_post_receipt(folder / "delivery"):
            return False
        return True
    except Exception:
        return False


def execute_post(executor, plan):
    root = executor.output_dir
    executor.generation_dispatches = {"process": 0, "api": 0, "handoff": 0}
    counts = {"ffmpeg_calls": 0}
    token = FFMPEG_CALLS.set(counts)
    try:
        if executor.executor_override is not None or executor.handoff or executor.job_id or executor.run_id:
            raise PostError("POST_EXECUTOR_OVERRIDE")
        context = PostContext(plan, executor.source_executions, executor.audio_resources)
        context.check()
        key_payload = {"plan": plan, "source_bindings": context.bindings, "engine": "fsf_edit_post_v1", "tool": context.version}
        if enabled(context.edit):
            key_payload["audio_quality_policy"] = "measured_or_verified_silence_v1"
        key = digest(key_payload)
        revision_root = directory(root / ("edit_" + digest(context.edit["edit_id"])[:12]) / ("rev_" + context.edit["edit_digest"][:16]))
        with edit_lock(revision_root / "state.json"):
            pointer = read_json(revision_root / "state.json") if (revision_root / "state.json").exists() else {}
            prior = pointer.get("attempt", "")
            import re
            valid_name = bool(re.fullmatch(r"attempt_[0-9a-f]{16}", prior))
            reusable = not executor.no_resume and pointer.get("key") == key and valid_name and _valid_attempt(revision_root / prior)
            attempt = prior if reusable else "attempt_" + uuid.uuid4().hex[:16]
            output = directory(revision_root / attempt)
            write_json(revision_root / "state.json", {"key": key, "attempt": attempt})
            executor.output_dir = output
            executor._post_context = context
            try:
                result = executor.execute_plan(plan)
            finally:
                executor._post_context = None
                executor.output_dir = root
            result["attempt_id"] = attempt
            for state in result.get("steps", {}).values():
                if state.get("error") and not re.fullmatch(r"(?:POST|AUDIO)_[A-Z_]+", state["error"]):
                    state["error"] = "POST_STEP_FAILED"
                if state.get("output_artifacts"):
                    state["output_artifacts"] = [Path(p).relative_to(output).as_posix() for p in state["output_artifacts"]]
                if state.get("report_path"):
                    state["report_path"] = Path(state["report_path"]).relative_to(output).as_posix()
            result["revision"] = context.edit["revision"]
            result["edit_digest"] = context.edit["edit_digest"]
            result["generation_dispatches"] = dict(executor.generation_dispatches)
            result["post_transform_dispatches"] = sum(s.get("kind") == "transform" and s.get("status") == "completed" for s in result["steps"].values())
            result["media_process_counts"] = dict(counts)
            result["artifact_directory"] = (output / "delivery").relative_to(root).as_posix() if result.get("success") else None
            return result
    except Exception as exc:
        code = exc.code if isinstance(exc, (PostError, AudioError)) else "AUDIO_LOCK_TIMEOUT" if isinstance(exc, EditStoreError) and exc.code == "EDIT_LOCK_TIMEOUT" else "POST_INVALID"
        return {"schema_version": "fsf_plan_execution_result_v1", "status": "blocked", "success": False,
                "error": code, "completed_count": 0, "reused_count": 0, "blocked_count": 4, "failed_count": 0,
                "steps": {}, "generation_dispatches": dict(executor.generation_dispatches), "media_process_counts": dict(counts)}
    finally:
        FFMPEG_CALLS.reset(token)


def preview_edit(edit, configurations, out, *, revision, edit_digest, clip_id, start_frame=0, end_frame=None,
                 width=512, height=288, subtitles="none", subtitle_track=None, audio_resources=None):
    validate_edit(edit)
    if type(revision) is not int or (revision, edit_digest) != (edit["revision"], edit["edit_digest"]):
        raise PostError("POST_PREVIEW_STALE")
    clip = next((c for c in edit["clips"] if c["clip_id"] == clip_id), None)
    if not clip:
        raise PostError("POST_PREVIEW_CLIP")
    end_frame = clip["out_frame"] - clip["in_frame"] if end_frame is None else end_frame
    plan = compile_post_plan(edit, width=width, height=height, subtitles=subtitles, subtitle_track=subtitle_track)
    context = PostContext(plan, configurations, audio_resources)
    sources = context.check()
    folder = directory(Path(out) / (".preview-" + uuid.uuid4().hex))
    if enabled(edit):
        from fsf.core.audio_edit import frame_sample
        from fsf.runtime.post_render import run_ffmpeg
        location = next(c for c in inspect_edit(edit)["clips"] if c["clip_id"] == clip_id)
        if not 0 <= start_frame < end_frame <= clip["out_frame"] - clip["in_frame"]:
            raise PostError("POST_PREVIEW_RANGE")
        start = location["timeline_start_frame"] + start_frame
        end = location["timeline_start_frame"] + end_frame
        full_folder = directory(folder / "full")
        token = ACTIVE_DEADLINE.set(Deadline())
        try:
            full_video,_ = render(edit,{**context.context,"audio":"strip"},sources,full_folder)
            full_audio_sources = resolve_sources(edit,sources,audio_resources)
            processed,audio_report = render_audio(edit,full_audio_sources,full_video,directory(full_folder/"audio"),cache=Path(out)/"audio_cache")
            video = folder/"movie.mp4"
            filters = f"[0:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v];[0:a]atrim=start_sample={frame_sample(start,edit['clock_rate'])}:end_sample={frame_sample(end,edit['clock_rate'])},asetpts=PTS-STARTPTS[a]"
            run_ffmpeg(["-i",processed,"-filter_complex",filters,"-map","[v]","-map","[a]","-c:v","libx264","-preset","ultrafast","-c:a","aac","-b:a","192k",video],folder)
            write_json(folder/"audio_report.json",audio_report)
        finally:
            ACTIVE_DEADLINE.reset(token)
    else:
        video, _ = render(edit, context.context, sources, folder, preview_range=(clip_id, start_frame, end_frame))
    context.check()
    located = next(c for c in inspect_edit(edit)["clips"] if c["clip_id"] == clip_id)
    manifest = {"schema_version": "fsf_edit_preview_v1", "edit_id": edit["edit_id"], "revision": revision,
                "edit_digest": edit_digest, "clip_id": clip_id, "source_id": clip["source_id"],
                "source_sha256": sources[clip["source_id"]]["binding"]["sha256"], "start_frame": start_frame,
                "end_frame": end_frame, "source_in_frame": clip["in_frame"] + start_frame, "source_out_frame": clip["in_frame"] + end_frame,
                "timeline_start_frame": located["timeline_start_frame"] + start_frame, "timeline_end_frame": located["timeline_start_frame"] + end_frame,
                "preview_sha256": file_sha(video), "settings_digest": digest(context.context), "kind": "preview"}
    manifest["manifest_digest"] = digest(manifest)
    _shape(manifest, "fsf_edit_preview_v1")
    write_json(folder / "preview.json", manifest)
    destination = folder.with_name("preview_" + uuid.uuid4().hex)
    os.rename(folder, destination)
    return {"manifest": manifest, "preview_path": (destination / "movie.mp4").relative_to(Path(out).resolve()).as_posix()}
