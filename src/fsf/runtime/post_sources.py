"""Read controlled production evidence; never execute or rewrite production jobs."""
import copy
import hashlib
import os
from pathlib import Path

from pdx_artifact_core import validate_execution_plan

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.conformance.media import SafeFfprobeRunner, receipt_authorizes_production
from fsf.core.asset_library import _path_parts, _no_links, _root, _open_reader
from fsf.core.edit_store import read_json
from fsf.core.editing import _shape
from fsf.core.consistency import resolve_execution_context
from fsf.runtime.handoff import build_consistency_requirements
from fsf.runtime.consistency_review import is_review_valid, REVIEW_STORE_FILENAME


class PostError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def file_sha(path):
    h = hashlib.sha256()
    with _open_reader(path) as stream:
        before = os.fstat(stream.fileno())
        import stat
        if not stat.S_ISREG(before.st_mode):
            raise PostError("POST_SOURCE_FILE")
        remaining = before.st_size
        while chunk := stream.read(min(1024 * 1024, remaining + 1)):
            h.update(chunk)
            remaining -= len(chunk)
            if remaining < 0:
                raise PostError("POST_SOURCE_CHANGED")
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PostError("POST_SOURCE_CHANGED")
    return h.hexdigest()


def safe_file(root, relative):
    try:
        root = _root(root)
        path = root.joinpath(*_path_parts(relative))
        _no_links(path)
        path.resolve(strict=True).relative_to(root)
        if not path.is_file():
            raise PostError("POST_SOURCE_FILE")
        return path
    except (ValueError, OSError, TypeError):
        raise PostError("POST_SOURCE_FILE") from None


def verify_entry(root, rows, relative):
    matches = [row for row in rows if row.get("path") == relative]
    if len(matches) != 1 or not matches[0].get("sha256"):
        raise PostError("POST_SOURCE_ENTRY")
    path = safe_file(root, relative)
    if file_sha(path) != matches[0]["sha256"]:
        raise PostError("POST_SOURCE_SHA")
    return path


class SourceVerifier:
    def __init__(self, edit, configurations):
        try:
            _shape(configurations, "fsf_source_executions_v1")
            self.configs = {e["execution_id"]: copy.deepcopy(e) for e in configurations["executions"]}
            if len(self.configs) != len(configurations["executions"]):
                raise PostError("POST_SOURCE_CONFIG")
        except ValueError:
            raise PostError("POST_SOURCE_CONFIG") from None
        self.edit = edit
        self._probe_cache = {}
        self._ancestors = frozenset()

    def verify(self):
        try:
            return self._verify()
        except PostError:
            raise
        except Exception:
            raise PostError("POST_SOURCE_INVALID") from None

    def _verify(self):
        result = {}
        paths_seen = {}
        used = {c["source_id"] for c in self.edit["clips"]}
        for source in self.edit["sources"]:
            if source["source_id"] not in used:
                continue
            ref = source["source_ref"]
            config = self.configs.get(ref["source_execution_id"])
            if not config:
                raise PostError("POST_SOURCE_CONFIG")
            root = _root(config["root"])
            plan = read_json(safe_file(root, config["plan"]))
            journal = read_json(safe_file(root, config["journal"]))
            job = read_json(safe_file(root, config["job"]))
            if validate_execution_plan(plan) or digest(plan) != ref["source_plan_digest"] or journal.get("plan_digest") != ref["source_plan_digest"]:
                raise PostError("POST_SOURCE_PLAN")
            if (journal.get("schema_version") != "fsf_plan_execution_journal_v1" or journal.get("job_id") != ref["source_job_id"]
                    or journal.get("run_id") != ref["source_run_id"] or digest(journal) != ref["source_journal_digest"]
                    or journal.get("plan_request_id") != plan.get("request_id")):
                raise PostError("POST_SOURCE_JOURNAL")
            source_sha = job.get("source_sha256")
            plan_shas = {s["inputs"]["source_sha256"] for s in plan["steps"] if s.get("inputs", {}).get("source_sha256")}
            if job.get("schema_version") != "fsf_video_job_v1" or not source_sha or plan_shas != {source_sha} or journal.get("source_sha256") != source_sha:
                raise PostError("POST_SOURCE_JOB")
            producers = [s for s in plan["steps"] if s["id"] == ref["producer_step_id"]]
            if len(producers) != 1:
                raise PostError("POST_SOURCE_PRODUCER")
            producer = producers[0]
            identity = (ref["source_execution_id"], ref["producer_step_id"], ref["artifact_ref"])
            if identity in self._ancestors:
                raise PostError("POST_SOURCE_CYCLE")
            tool = producer.get("tool", producer.get("transform"))
            allowed = (producer.get("kind") == "tool" and tool in {"studio.video.generate", "studio.video.remaster", "studio.video.vfx"}
                       or producer.get("kind") == "transform" and tool == "fsf.assembly.fast_concat")
            if not allowed or ref["purpose"] != "video":
                raise PostError("POST_SOURCE_PURPOSE")
            if producer.get("kind") == "tool" and producer.get("inputs", {}).get("shot_id") != source["shot_id"]:
                raise PostError("POST_SOURCE_SHOT")
            declared = ref["artifact_ref"]
            if producer.get("outputs", []).count(declared) != 1 or not declared.startswith("artifact:"):
                raise PostError("POST_SOURCE_DECLARATION")
            relative = declared[len("artifact:"):]
            state = journal.get("steps", {}).get(producer["id"], {})
            if state.get("status") != "completed" or state.get("execution_kind") in ("mock", "shape"):
                raise PostError("POST_SOURCE_STATUS")
            if Path(relative).suffix.casefold() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m2ts"}:
                raise PostError("POST_SOURCE_PURPOSE")
            path = verify_entry(root, state.get("output_files", []), relative)
            path_key = os.path.normcase(str(path.resolve()))
            if path_key in paths_seen and paths_seen[path_key] != digest(ref):
                raise PostError("POST_SOURCE_AMBIGUOUS")
            paths_seen[path_key] = digest(ref)
            if file_sha(path) != ref["artifact_sha256"]:
                raise PostError("POST_SOURCE_SHA")
            qc_steps = [s for s in plan["steps"] if s.get("tool") == "pdx.media.technical_conform" and s.get("inputs", {}).get("target_artifact") == declared and producer["id"] in s.get("depends_on", [])]
            if len(qc_steps) != 1:
                raise PostError("POST_SOURCE_QC")
            qc = qc_steps[0]
            qc_state = journal["steps"].get(qc["id"], {})
            if qc_state.get("status") != "completed" or len(qc.get("outputs", [])) != 1:
                raise PostError("POST_SOURCE_QC")
            report_relative = qc["outputs"][0].removeprefix("report:")
            report = read_json(verify_entry(root, qc_state.get("output_files", []), report_relative))
            evidence = read_json(verify_entry(root, qc_state.get("output_files", []), report_relative + ".evidence.json"))
            expected = qc.get("inputs", {}).get("expected_media_contract")
            if not expected or evidence.get("report") != report or not receipt_authorizes_production(evidence,
                    artifact_sha256=ref["artifact_sha256"], artifact_name=path.name, required_expected_digest=digest(expected)):
                raise PostError("POST_SOURCE_QC")
            if tool == "fsf.assembly.fast_concat":
                # An assembled source does not erase the review obligations of
                # its explicitly selected upstream video inputs.
                self._verify_assembly_ancestors(producer, ref, journal, plan, root, identity)
            # Rebuild requirements, including exact upstream and local reference bytes.
            ctx = producer.get("inputs", {}).get("consistency_context")
            required = bool(ctx and ctx.get("review_required"))
            requirements = None
            if ctx is not None:
                ctx = copy.deepcopy(ctx)
                refs = list(ctx.get("ref_images", [])) + [ri for a in ctx.get("asset_refs", []) for ri in a.get("ref_images", [])]
                for ri in refs:
                    if not ri.get("path"):
                        raise PostError("POST_REFERENCE_MISSING")
                    ri["path"] = str(safe_file(root, ri["path"]))
                upstream = {}
                if ctx.get("continues_from"):
                    from fsf.runtime.plan_executor import PlanExecutor
                    resolver = PlanExecutor(root)
                    resolver._steps_by_id = {s["id"]: s for s in plan["steps"]}
                    # Compiler declaration order is the stable order for legacy plans.
                    resolver._topo_order = [s["id"] for s in plan["steps"]]
                    earlier = {s["id"] for s in plan["steps"][:plan["steps"].index(producer)]}
                    earlier_results = {k: v for k, v in journal["steps"].items() if k in earlier}
                    upstream = resolver._resolve_continues_from_sha(ctx["continues_from"], earlier_results)
                resolved = resolve_execution_context(ctx, shot_id=producer.get("inputs", {}).get("shot_id", source["shot_id"]), resolved_artifact_shas=upstream)
                requirements = build_consistency_requirements(resolved)
            req_digest = requirements["requirements_digest"] if requirements else None
            if (required and (state.get("review_required") is not True or state.get("requirements_digest") != req_digest)) or (state.get("requirements_digest") is not None and state["requirements_digest"] != req_digest):
                raise PostError("POST_REQUIREMENTS")
            review_digest = None
            if required:
                reviews = read_json(safe_file(root, REVIEW_STORE_FILENAME)).get("reviews", {})
                review = reviews.get(producer["id"], {})
                if not is_review_valid(review, current_artifact_sha=ref["artifact_sha256"], current_requirements_digest=req_digest,
                        current_workorder_id=state.get("workorder_id", ""), expected_step_id=producer["id"], expected_job_id=journal["job_id"])[0]:
                    raise PostError("POST_REVIEW")
                review_digest = review["review_digest"]
            cache_key = (str(path), ref["artifact_sha256"])
            if cache_key not in self._probe_cache:
                raw = SafeFfprobeRunner().probe(path)
                if not any(s.get("codec_type") == "video" for s in raw.get("streams", [])):
                    raise PostError("POST_SOURCE_MEDIA")
                self._probe_cache[cache_key] = raw
            raw = self._probe_cache[cache_key]
            from fractions import Fraction
            video = next(s for s in raw["streams"] if s.get("codec_type") == "video")
            duration = Fraction(str(video.get("duration") or raw.get("format", {}).get("duration", "0")))
            frames = duration * Fraction(self.edit["clock_rate"]["num"], self.edit["clock_rate"]["den"])
            # Container rounding can fall a tiny fraction below an exact frame.
            actual_frames = (frames + Fraction(1, 1000)).numerator // (frames + Fraction(1, 1000)).denominator
            if source["duration_frames"] != actual_frames:
                raise PostError("POST_SOURCE_DURATION")
            result[source["source_id"]] = {"path": path, "raw": raw, "binding": {"source_id": source["source_id"], "source_ref": copy.deepcopy(ref),
                "artifact_ref": f"artifact:sources/{source['source_id']}.mp4", "sha256": ref["artifact_sha256"],
                "size_bytes": path.stat().st_size, "review_digest": review_digest}}
        return result

    def _verify_assembly_ancestors(self, producer, ref, journal, plan, root, identity):
        from fractions import Fraction
        entries = producer.get("inputs", {}).get("artifact_inputs")
        if not entries or len(self._ancestors) >= 64:
            raise PostError("POST_SOURCE_ASSEMBLY")
        sources, clips = [], []
        for index, entry in enumerate(entries):
            artifact = entry.get("artifact")
            candidates = [s for s in plan["steps"] if artifact in s.get("outputs", [])
                          and (not entry.get("producer_step_id") or s["id"] == entry["producer_step_id"])]
            if not artifact or len(candidates) != 1:
                raise PostError("POST_SOURCE_ASSEMBLY")
            upstream = candidates[0]
            state = journal["steps"].get(upstream["id"], {})
            path = verify_entry(root, state.get("output_files", []), artifact.removeprefix("artifact:"))
            raw = SafeFfprobeRunner().probe(path)
            video = next(s for s in raw["streams"] if s.get("codec_type") == "video")
            value = Fraction(str(video.get("duration") or raw["format"]["duration"])) * Fraction(self.edit["clock_rate"]["num"], self.edit["clock_rate"]["den"]) + Fraction(1, 1000)
            frames = value.numerator // value.denominator
            source_id = f"VERIFY_{index}"
            source_ref = {**copy.deepcopy(ref), "producer_step_id": upstream["id"], "artifact_ref": artifact, "artifact_sha256": file_sha(path)}
            sources.append({"source_id": source_id, "shot_id": entry["shot_id"], "duration_frames": frames, "source_ref": source_ref})
            clips.append({"source_id": source_id})
        verifier = SourceVerifier({"clock_rate": self.edit["clock_rate"], "sources": sources, "clips": clips},
                                  {"schema_version": "fsf_source_executions_v1", "executions": list(self.configs.values())})
        verifier._ancestors = self._ancestors | {identity}
        verifier._probe_cache = self._probe_cache
        verifier.verify()


def import_edit_selection(definition, configurations):
    """Capture exact operator-selected sources without hand-authored digests."""
    from fractions import Fraction
    from fsf.core.editing import create_edit
    try:
        _shape(definition, "fsf_edit_import_v1")
        _shape(configurations, "fsf_source_executions_v1")
        configs = {item["execution_id"]: item for item in configurations["executions"]}
        if len(configs) != len(configurations["executions"]):
            raise PostError("POST_SOURCE_CONFIG")
        sources, clips, seen = [], [], set()
        for index, selected in enumerate(definition["selections"], 1):
            identity = (selected["source_execution_id"], selected["producer_step_id"], selected["artifact_ref"])
            if identity in seen:
                raise PostError("POST_IMPORT_DUPLICATE")
            seen.add(identity)
            config = configs[selected["source_execution_id"]]
            root = _root(config["root"])
            plan = read_json(safe_file(root, config["plan"]))
            journal = read_json(safe_file(root, config["journal"]))
            producers = [s for s in plan["steps"] if s["id"] == selected["producer_step_id"]]
            if len(producers) != 1 or selected["artifact_ref"] not in producers[0].get("outputs", []):
                raise PostError("POST_IMPORT_SELECTION")
            state = journal["steps"][selected["producer_step_id"]]
            relative = selected["artifact_ref"].removeprefix("artifact:")
            path = verify_entry(root, state.get("output_files", []), relative)
            raw = SafeFfprobeRunner().probe(path)
            video = next(s for s in raw["streams"] if s.get("codec_type") == "video")
            clock = definition["clock_rate"]
            frame_value = Fraction(str(video.get("duration") or raw["format"]["duration"])) * Fraction(clock["num"], clock["den"]) + Fraction(1, 1000)
            frames = frame_value.numerator // frame_value.denominator
            source_id, clip_id = f"S{index:02}", f"C{index:02}"
            ref = {"source_execution_id": selected["source_execution_id"], "source_job_id": journal["job_id"], "source_run_id": journal["run_id"],
                "source_plan_digest": digest(plan), "source_journal_digest": digest(journal), "producer_step_id": selected["producer_step_id"],
                "artifact_ref": selected["artifact_ref"], "artifact_sha256": file_sha(path), "purpose": "video"}
            shot = producers[0].get("inputs", {}).get("shot_id", "assembled_timeline")
            label = selected.get("label", shot)
            sources.append({"source_id": source_id, "shot_id": shot, "label": label, "duration_frames": frames, "source_ref": ref})
            clips.append({"clip_id": clip_id, "source_id": source_id, "label": label, "in_frame": 0, "out_frame": frames, "lineage": []})
        edit = create_edit(edit_id=definition["edit_id"], clock_rate=definition["clock_rate"], sources=sources, clips=clips)
        SourceVerifier(edit, configurations).verify()
        return edit
    except PostError:
        raise
    except Exception:
        raise PostError("POST_IMPORT") from None
