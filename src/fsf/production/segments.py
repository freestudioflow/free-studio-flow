"""Capability-driven global frame mapping, independent of old short profiles."""
from __future__ import annotations

from fractions import Fraction

from fsf.production.contracts import ProductionError, digest, integer, key, seal, validate


def compile_segments(source, capability, *, shot_id, policy_digest, qualification_probe=False):
    """Compile core/context intervals. Capability must be measured before live use."""
    key(shot_id)
    source = validate(source, "fsf_control_source_v2", "source_digest")
    cap = validate(capability, "fsf_model_capability_v1", "capability_digest")
    if type(qualification_probe) is not bool:
        raise ProductionError("PRODUCTION_INVALID")
    if (cap["qualification"] != "verified" and not qualification_probe) or "control_video" not in cap["modalities"]:
        raise ProductionError("PRODUCTION_CAPABILITY_UNVERIFIED")
    if source["clock"] not in cap["clocks"]:
        raise ProductionError("PRODUCTION_CAPABILITY_UNSUPPORTED")
    if [source["width"], source["height"]] not in cap["dimensions"]:
        raise ProductionError("PRODUCTION_PREPROCESS_REQUIRED")
    limit, step, overlap = cap["max_frames"], cap["frame_step"], cap["overlap_frames"]
    if overlap >= limit or (cap["continuation"] == "previous_frame" and overlap < 1):
        raise ProductionError("PRODUCTION_CAPABILITY_UNSUPPORTED")
    segments, start = [], 0
    while start < source["frame_count"]:
        left = max(0, start-overlap)
        # Legal model frame lengths are 1 + k*frame_step.
        length = 1 + ((limit-1)//step)*step
        end = min(source["frame_count"], left+length)
        if end <= start:
            raise ProductionError("PRODUCTION_CAPABILITY_UNSUPPORTED")
        real_length = end-left
        padding = (1-real_length) % step
        if padding and cap["tail_padding"] != "repeat_last_explicit":
            raise ProductionError("PRODUCTION_TAIL_UNSUPPORTED")
        sid = f"segment_{len(segments)+1:06d}"
        segments.append({"segment_id": sid, "core": [start,end], "context": [left,end],
                         "padding_frames": padding, "model_frames": real_length+padding,
                         "depends_on": [segments[-1]["segment_id"]]
                         if segments and cap["continuation"] == "previous_frame" else []})
        start = end
    result = seal({"schema_version": "fsf_production_segments_v1", "logical_shot_id": shot_id,
                   "source_digest": source["source_digest"], "source_sha256": source["sha256"],
                   "capability_digest": cap["capability_digest"], "policy_digest": policy_digest,
                   "clock": source["clock"], "frame_count": source["frame_count"],
                   "qualification_scope":"probe_only" if qualification_probe else "qualified",
                   "segments": segments, "generated_frames": sum(s["model_frames"] for s in segments)}, "plan_digest")
    return validate_segments(result)


def validate_segments(plan):
    value = validate(plan, "fsf_production_segments_v1", "plan_digest")
    position, total, seen = 0, 0, set()
    for segment in value["segments"]:
        start,end = segment["core"]
        left,right = segment["context"]
        if start != position or not 0 <= left <= start < end == right <= value["frame_count"]:
            raise ProductionError("PRODUCTION_FRAME_MAPPING")
        if segment["segment_id"] in seen or not set(segment["depends_on"]) <= seen:
            raise ProductionError("PRODUCTION_DEPENDENCY_INVALID")
        if segment["model_frames"] != right-left+segment["padding_frames"]:
            raise ProductionError("PRODUCTION_FRAME_MAPPING")
        seen.add(segment["segment_id"])
        position = end
        total += segment["model_frames"]
    if position != value["frame_count"] or total != value["generated_frames"]:
        raise ProductionError("PRODUCTION_FRAME_MAPPING")
    return value


def affected_tasks(dependencies, changed):
    """Full declared-input closure, including prompts and evaluator dependencies."""
    if not set(changed) <= dependencies.keys():
        raise ProductionError("PRODUCTION_DEPENDENCY_INVALID")
    pending, done = set(dependencies), set()
    while pending:
        ready = {k for k in pending if set(dependencies[k]) <= done}
        if not ready:
            raise ProductionError("PRODUCTION_DEPENDENCY_INVALID")
        done |= ready
        pending -= ready
    result = set(changed)
    while True:
        more = {k for k, deps in dependencies.items() if result.intersection(deps)}
        if more <= result:
            return sorted(result)
        result |= more
