"""Compile immutable edit revisions into a closed, generation-free PDX plan."""
import copy
from fractions import Fraction

from pdx_artifact_core import validate_execution_plan

from fsf.conformance.media import build_expected_contract, validate_media_expected
from fsf.core.editing import validate_edit


def compile_post_plan(edit, *, width=1024, height=576, audio="keep", subtitles="none", subtitle_track=None):
    validate_edit(edit)
    from fsf.core.audio_edit import enabled, AudioError
    has_mix = enabled(edit)
    if has_mix and audio == "strip":
        raise AudioError()
    if type(width) is not int or type(height) is not int or width < 2 or height < 2 or width % 2 or height % 2:
        raise ValueError("POST_SETTINGS")
    if audio not in ("keep", "strip", "require") or subtitles not in ("none", "sidecar", "burned"):
        raise ValueError("POST_SETTINGS")
    tracks = [t["track_id"] for t in edit["subtitle_tracks"]]
    if subtitles != "none":
        if subtitle_track is None and len(tracks) == 1:
            subtitle_track = tracks[0]
        if subtitle_track not in tracks:
            raise ValueError("POST_SUBTITLE_SELECTION")
    elif subtitle_track is not None:
        raise ValueError("POST_SUBTITLE_SELECTION")
    clock = edit["clock_rate"]
    fps = Fraction(clock["num"], clock["den"])
    frames = sum(c["out_frame"] - c["in_frame"] for c in edit["clips"])
    expected = build_expected_contract({"duration_seconds": float(frames / fps)}, width=width, height=height,
                                       fps=float(fps), audio={"keep": "allowed", "strip": "forbidden", "require": "required"}[audio])
    expected["duration_tolerance_seconds"] = float(1 / fps)
    validate_media_expected(expected)
    context = {"edit": copy.deepcopy(edit), "width": width, "height": height, "audio": audio,
               "subtitles": subtitles, "subtitle_track": subtitle_track}
    selected = {c["source_id"] for c in edit["clips"]}
    # Unused sources remain declarations but are not imported or approved by inference.
    outputs = [f"artifact:sources/{s['source_id']}.mp4" for s in edit["sources"] if s["source_id"] in selected]
    if has_mix:
        context["audio_engine"] = "fsf_audio_mix_v1"
        outputs += [f"artifact:sources/audio/{s['audio_source_id']}.wav" for s in edit["audio_mix"]["sources"]]
        outputs.append("artifact:sources/audio_manifest.json")
    audio_reports = ["report:staging/audio_report.json", "report:staging/audio_analysis.json"] if has_mix else []
    steps = []
    def add(identifier, kind, operation, inputs, outputs, dependencies):
        steps.append({"id": identifier, "kind": kind, "name": operation,
                      "tool" if kind == "tool" else "transform": operation,
                      "inputs": {"post_context": copy.deepcopy(context), **inputs}, "outputs": outputs, "depends_on": dependencies})
    add("post_resolve", "transform", "fsf.edit.resolve_sources", {}, ["artifact:sources/manifest.json", *outputs], [])
    add("post_assemble", "transform", "fsf.edit.assemble", {}, ["artifact:staging/movie.mp4"] + audio_reports + (["artifact:staging/captions.srt"] if subtitles != "none" else []), ["post_resolve"])
    add("post_qc", "tool", "pdx.media.technical_conform", {"target_artifact": "artifact:staging/movie.mp4", "expected_media_contract": expected}, ["report:staging/qc.json"], ["post_assemble"])
    add("post_publish", "transform", "fsf.edit.publish", {}, ["artifact:delivery/receipt.json", "artifact:delivery/movie.mp4", "artifact:delivery/qc.json", "artifact:delivery/source_manifest.json"] + (["artifact:delivery/captions.srt"] if subtitles != "none" else []), ["post_qc"])
    if has_mix:
        steps[-1]["outputs"].extend(["artifact:delivery/audio_report.json", "artifact:delivery/audio_analysis.json"])
    plan = {"schema_version": "pdx_execution_plan_v1", "request_id": f"post_{edit['edit_id']}_{edit['revision']}_{edit['edit_digest'][:12]}",
            "producer": {"type": "fsf_planner", "name": "fsf.runtime.post_plan"}, "steps": steps}
    if validate_execution_plan(plan):
        raise ValueError("POST_PLAN_SCHEMA")
    return plan


def validate_post_plan(plan):
    try:
        context = plan["steps"][0]["inputs"]["post_context"]
        settings = {k: context[k] for k in ("width", "height", "audio", "subtitles", "subtitle_track")}
        if compile_post_plan(context["edit"], **settings) != plan:
            raise ValueError("POST_PLAN_MISMATCH")
        return copy.deepcopy(context)
    except (KeyError, TypeError, IndexError):
        raise ValueError("POST_PLAN_MISMATCH") from None
