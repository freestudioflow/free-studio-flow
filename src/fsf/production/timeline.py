"""Local generator handoff on the global control clock."""

from fractions import Fraction
from copy import deepcopy
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PIL import Image

from fsf.core.asset_library import _root
from fsf.core.edit_store import decode_json, read_json
from fsf.conformance.media import ensure_media_tools_on_path
from fsf.production.contracts import ProductionError, digest, integer, key, seal
from fsf.production.control import _run, inspect_control
from fsf.production.control_bundle import Budget, compare_rgb, definition, identity, path_at, verify
from fsf.production.segments import map_windows


ROLES = {"opening", "character", "costume", "color", "revisit"}
TRACKS = {"camera", "action", "prop", "world", "prompt"}


def require(condition, code):
    if not condition:
        raise ProductionError("TIMELINE_" + code)


def interval(value, limit):
    require(isinstance(value, list) and len(value) == 2, "RANGE")
    start, end = value
    integer(start, 0, limit)
    integer(end, start + 1, limit)
    return start, end


def intersects(a, b):
    return max(a[0], b[0]) < min(a[1], b[1])


def position(value):
    require(isinstance(value, list) and len(value) == 3, "POSITION")
    require(all(type(n) in (int, float) and math.isfinite(n) for n in value), "POSITION")
    return value


def compile_schedule(clock, frame_count, tracks):
    """Seal explicit half-open spans. Missing tracks remain unspecified."""
    integer(frame_count, 1, 172800)
    require(clock in ({"num": 24, "den": 1}, {"num": 24000, "den": 1001}), "CLOCK")
    require(all(type(n) is int for n in clock.values()), "CLOCK")
    require(isinstance(tracks, list) and len(tracks) <= 100000, "LIMIT")
    decode_json(json.dumps(tracks, allow_nan=False).encode())
    ends = {}
    endpoints = {}
    for event in tracks:
        require(isinstance(event, dict) and set(event) == {
            "track", "subject", "range", "value", "interpolation"
        }, "EVENT")
        track, subject = event["track"], key(event["subject"])
        require(track in TRACKS, "TRACK")
        start, end = interval(event["range"], frame_count)
        identity_key = (track, subject)
        require(start >= ends.get(identity_key, 0), "OVERLAP")
        ends[identity_key] = end
        mode = event["interpolation"]
        require(mode in {"hold", "linear", "embedding"}, "INTERPOLATION")
        if mode == "linear" or track == "camera":
            require(track in {"camera", "action"}, "INTERPOLATION")
            value = event["value"]
            require(isinstance(value, dict) and set(value) == {"from_m", "to_m"}, "POSITION")
            a, b = position(value["from_m"]), position(value["to_m"])
            require(mode == "linear" or a == b, "POSITION")
            if track == "camera" and identity_key in endpoints:
                previous_end, previous_position = endpoints[identity_key]
                require(previous_end == start and previous_position == a, "CAMERA_DISCONTINUITY")
            if track == "camera":
                endpoints[identity_key] = (end, b)
        if mode == "embedding":
            require(track == "prompt", "INTERPOLATION")
    return seal({"schema_version": "rc11_schedule_v1", "clock": clock,
                 "frame_count": frame_count, "tracks": tracks}, "schedule_digest")


def sample_motion(event, frame, clock):
    """Evaluate a metric trajectory at a global frame and report its speed."""
    start, end = event["range"]
    require(start <= frame < end, "RANGE")
    a, b = event["value"]["from_m"], event["value"]["to_m"]
    elapsed = Fraction((frame - start) * clock["den"], clock["num"])
    duration = Fraction((end - start) * clock["den"], clock["num"])
    return {"position_m": [x + (y - x) * float(elapsed / duration) for x, y in zip(a, b)],
            "speed_m_s": math.dist(a, b) / float(duration),
            "elapsed_seconds": [elapsed.numerator, elapsed.denominator]}


def _reference(root, ref, budget):
    require(isinstance(ref, dict) and set(ref) == {"path", "sha256", "size_bytes"}, "REFERENCE")
    integer(ref["size_bytes"], 1, 256 * 1024**2)
    return identity(root, ref, budget)


def compile_bundle_plan(bundle, capability, *, shot_id):
    source = definition(read_json(path_at(_root(bundle), "bundle.json")))
    limit = integer(capability["max_frames"], 1, 172800)
    step = integer(capability["frame_step"], 1, 4096)
    overlap = integer(capability["overlap_frames"], 0, limit - 1)
    continuation = capability["continuation"]
    require(continuation in {"none", "previous_frame"}, "CAPABILITY_LOCK")
    require(source["clock"] in capability["clocks"]
            and [source["width"], source["height"]] in capability["dimensions"], "CAPABILITY_LOCK")
    segments = map_windows(source["frame_count"], limit, step, overlap,
                           continuation, capability["tail_padding"])
    return seal({"schema_version": "rc11_bundle_plan_v1", "logical_shot_id": key(shot_id),
                 "source_digest": source["bundle_digest"], "capability_digest": digest(capability),
                 "clock": source["clock"], "frame_count": source["frame_count"],
                 "qualification_scope": "probe_only", "segments": segments,
                 "generated_frames": sum(s["model_frames"] for s in segments)}, "plan_digest")


def build_request(*, plan, segment_id, schedule, bundle, resources, anchors,
                  capability, capability_digest, lock_digest, request_id, attempt_id,
                  accepted_frames=None, accepted_latent=None, seconds=300, allow_probe=False):
    """Verify input bytes and bind a single segment before a local callback.

    accepted_frames is supplied by the caller's trusted acceptance store.
    Each entry maps a global frame to the accepted PNG reference.
    """
    budget = Budget(seconds)
    root = _root(resources)
    require(plan == compile_bundle_plan(bundle, capability, shot_id=plan["logical_shot_id"]),
            "PLAN_SEAL")
    require(plan["qualification_scope"] == "qualified" or allow_probe is True, "PROBE_ONLY")
    require(digest(capability) == capability_digest == plan["capability_digest"]
            and capability["lock_digest"] == lock_digest, "CAPABILITY_LOCK")
    require(schedule == compile_schedule(schedule["clock"], schedule["frame_count"],
                                         schedule["tracks"]), "SCHEDULE_SEAL")
    require(schedule["clock"] == plan["clock"] and schedule["frame_count"] == plan["frame_count"],
            "CLOCK")
    segment = next((s for s in plan["segments"] if s["segment_id"] == segment_id), None)
    require(segment is not None, "SEGMENT")
    context = segment["context"]
    require(segment["model_frames"] <= capability["max_frames"]
            and (segment["model_frames"] - 1) % capability["frame_step"] == 0, "WINDOW")
    window = verify(bundle, interval=tuple(context), timeout=seconds,
                    capability=capability["control_channels"])
    require(window["bundle_digest"] == plan["source_digest"], "CONTROL_BINDING")
    source = definition(read_json(path_at(_root(bundle), "bundle.json")))
    require(source["bundle_digest"] == plan["source_digest"], "CONTROL_BINDING")
    supported = capability.get("timeline_inputs", [])
    active = []
    require(len(anchors) <= 256, "LIMIT")
    names = set()
    for anchor in anchors:
        require(set(anchor) == {"id", "role", "version", "range", "space", "required",
                                "artifact", "camera_subject", "position_m"}, "ANCHOR")
        name = key(anchor["id"])
        require(name not in names and anchor["role"] in ROLES and type(anchor["required"]) is bool,
                "ANCHOR")
        names.add(name)
        key(anchor["version"])
        require(anchor["space"] in {"image_normalized", "world_m", "color"}, "ANCHOR")
        interval(anchor["range"], plan["frame_count"])
        if not intersects(anchor["range"], context):
            continue
        if anchor["role"] not in supported:
            require(not anchor["required"], "REQUIRED_INPUT")
            continue
        if anchor["position_m"] is not None:
            require(anchor["space"] == "world_m", "POSITION")
            p = position(anchor["position_m"])
            frame = anchor["range"][0]
            camera = next((e for e in schedule["tracks"] if e["track"] == "camera"
                           and e["subject"] == anchor["camera_subject"]
                           and e["range"][0] <= frame < e["range"][1]), None)
            require(camera is not None and math.dist(p, sample_motion(camera, frame, plan["clock"])
                                                    ["position_m"]) <= 0.01, "ANCHOR_CAMERA")
        _reference(root, anchor["artifact"], budget)
        active.append(anchor)
    events = [e for e in schedule["tracks"] if intersects(e["range"], context)]
    require(all(e["track"] in supported for e in events), "REQUIRED_INPUT")
    require(all(e["interpolation"] != "embedding" or "prompt_embedding" in supported
                for e in events), "REQUIRED_INPUT")
    initialized = []
    if segment["depends_on"]:
        require("initialized_png" in supported and accepted_frames is not None, "REQUIRED_INPUT")
        for frame in range(context[0], segment["core"][0]):
            require(frame in accepted_frames, "ACCEPTED_PREFIX")
            ref = accepted_frames[frame]
            path = _reference(root, ref, budget)
            with Image.open(path) as image:
                require(image.format == "PNG" and image.mode == "RGB"
                        and image.size == (source["width"], source["height"])
                        and getattr(image, "n_frames", 1) == 1, "INITIALIZATION")
                image.load()
            _reference(root, ref, budget)
            initialized.append({"global_frame": frame, "artifact": ref})
    if accepted_latent is not None:
        require("latent" in supported and segment["depends_on"], "REQUIRED_INPUT")
        require(set(accepted_latent) == {"identity", "artifact", "segment_id"}
                and accepted_latent["segment_id"] == segment["depends_on"][0], "LATENT")
        latent_identity = accepted_latent["identity"]
        require(isinstance(latent_identity, dict)
                and set(latent_identity) == {"model", "vae", "shape", "dtype", "version"}
                and latent_identity == capability.get("latent_identity"), "LATENT")
        _reference(root, accepted_latent["artifact"], budget)
        initialized.append({"latent": accepted_latent})
    budget.check()
    binding = seal({"schema_version": "rc11_generator_v2", "request_id": key(request_id),
                    "attempt_id": key(attempt_id), "logical_shot_id": plan["logical_shot_id"],
                    "segment_id": segment_id, "core_range": segment["core"], "context_range": context,
                    "tail_padding": segment["padding_frames"], "clock": plan["clock"],
                    "lock_digest": lock_digest, "capability_digest": capability_digest,
                    "bundle_window": window, "inputs": initialized, "anchors": active,
                    "schedule_digest": schedule["schedule_digest"], "events": events,
                    "segment_input_digest": digest({"segment": segment, "window": window,
                                                    "inputs": initialized, "anchors": active,
                                                    "events": events, "clock": plan["clock"],
                                                    "lock": lock_digest, "capability": capability_digest}),
                    "deadline_seconds": seconds}, "request_digest")
    return binding


def invoke_local(callback, request, *, seconds=300):
    """Invoke a cooperative local callback and check its required input acknowledgment."""
    Budget(seconds)
    require(request == seal(request, "request_digest"), "REQUEST_SEAL")
    deadline = time.monotonic() + min(seconds, request["deadline_seconds"])
    expected = {digest(item) for item in request["inputs"] + request["anchors"]}
    expected.update(digest(item) for item in request["bundle_window"]["channels"])
    response = callback(deepcopy(request), deadline)
    require(time.monotonic() < deadline, "DEADLINE")
    require(response["request_digest"] == request["request_digest"]
            and set(response["consumed_digests"]) == expected, "ACKNOWLEDGMENT")
    return response


def verify_rgb_derivation(resources, source, target, *, width, height, profile, seconds=60):
    """Bind a PNG adapter derivative and measure its actual decoded samples."""
    budget = Budget(seconds)
    root = _root(resources)
    original = _reference(root, source, budget)
    derived = _reference(root, target, budget)
    metrics = compare_rgb(original, derived, width, height, profile, budget)
    _reference(root, source, budget)
    _reference(root, target, budget)
    return seal({"source": source, "derived": target, "profile": profile,
                 "metrics": metrics}, "derivation_digest")


def verify_mp4_initialization(resources, source, target, *, clock, width, height, seconds=60):
    """Measure a single-frame adapter MP4 against the original PNG."""
    budget = Budget(seconds)
    root = _root(resources)
    original = _reference(root, source, budget)
    derived = _reference(root, target, budget)
    ffmpeg, _ = ensure_media_tools_on_path()
    require(ffmpeg is not None, "MEDIA_TOOLS")
    with TemporaryDirectory(prefix="fsf-anchor-") as tmp:
        scratch = Path(tmp)
        budget.check()
        info = inspect_control(derived, scratch, timeout=max(1, int(budget.deadline - time.monotonic())))
        require(info["clock"] == clock and info["frame_count"] == 1
                and (info["width"], info["height"]) == (width, height), "DERIVATIVE_MAPPING")
        decoded = scratch / "decoded.png"
        budget.check()
        _run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file",
              "-i", str(derived), "-frames:v", "1", "-threads", "1", "-pix_fmt", "rgb24",
              str(decoded)], max(1, budget.deadline - time.monotonic()))
        profile = "lossy_rgb_reference_v1_1"
        metrics = compare_rgb(original, decoded, width, height, profile, budget)
    _reference(root, source, budget)
    _reference(root, target, budget)
    return seal({"source": source, "derived": target, "clock": clock,
                 "profile": profile, "metrics": metrics}, "derivation_digest")
