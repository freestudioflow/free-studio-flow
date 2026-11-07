from fractions import Fraction

import pytest

from fsf.production.contracts import ProductionError, digest
from fsf.production.control_bundle import import_bundle
from fsf.production.timeline import build_request, compile_bundle_plan, compile_schedule, invoke_local, sample_motion
from fsf.production.segments import map_windows
from test_control_bundle import fixture, reference


@pytest.mark.parametrize("clock", [{"num": 24, "den": 1}, {"num": 24000, "den": 1001}])
def test_global_window_and_revisit(clock):
    segments = map_windows(720, 125, 4, 5, "previous_frame", "repeat_last_explicit")
    assert len(segments) == 6
    assert [f for s in segments for f in range(*s["core"])] == list(range(720))
    assert sum(s["model_frames"] for s in segments) == 746
    events = [
        {"track": "camera", "subject": "camera", "range": span,
         "value": {"from_m": a, "to_m": b}, "interpolation": "linear"}
        for span, a, b in [([0, 240], [0, 0, 0], [10, 0, 0]),
                           ([240, 480], [10, 0, 0], [0, 0, 0]),
                           ([480, 720], [0, 0, 0], [0, 0, 0])]
    ]
    schedule = compile_schedule(clock, 720, events)
    assert sample_motion(schedule["tracks"][2], 480, clock)["position_m"] == [0, 0, 0]
    point = sample_motion(events[0], 120, clock)
    assert point["position_m"] == [5, 0, 0]
    assert Fraction(*point["elapsed_seconds"]) == Fraction(120 * clock["den"], clock["num"])
    assert point["speed_m_s"] == pytest.approx(clock["num"] / (24 * clock["den"]))


def test_request_preflight_png_and_scoped_anchor_change(tmp_path):
    source, out, definition = fixture(tmp_path, 4)
    import_bundle(definition, resources_root=source, output_root=out)
    bundle = out / "scene_control"
    cap = {"lock_digest": "a" * 64, "max_frames": 3, "frame_step": 1,
           "overlap_frames": 1, "continuation": "previous_frame",
           "tail_padding": "repeat_last_explicit", "clocks": [definition["clock"]],
           "dimensions": [[32, 32]], "timeline_inputs": ["revisit", "initialized_png", "prompt"],
           "control_channels": {"schema_version": "rc11_control_capability_v1",
                                "channels": [{k: c[k] for k in ("kind", "encoding", "semantics_digest")}
                                             for c in definition["channels"]]}}
    plan = compile_bundle_plan(bundle, cap, shot_id="shot")
    ref = reference(source, "frames/rgb.png")
    anchor = {"id": "return", "role": "revisit", "version": "v1", "range": [3, 4],
              "space": "image_normalized", "required": True, "artifact": ref,
              "camera_subject": None, "position_m": None}
    schedule = compile_schedule(definition["clock"], 4, [
        {"track": "prompt", "subject": "phase", "range": [0, 4],
         "value": "continue", "interpolation": "hold"}])
    args = dict(plan=plan, schedule=schedule, bundle=bundle, resources=source,
                capability=cap, capability_digest=digest(cap), lock_digest=cap["lock_digest"],
                request_id="req", attempt_id="attempt", accepted_frames={2: ref}, allow_probe=True)
    first = build_request(segment_id="segment_000001", anchors=[anchor], **args)
    second = build_request(segment_id="segment_000002", anchors=[anchor], **args)
    assert second["inputs"] == [{"global_frame": 2, "artifact": ref}]
    changed = dict(anchor, version="v2")
    assert build_request(segment_id="segment_000001", anchors=[changed], **args) == first
    assert build_request(segment_id="segment_000002", anchors=[changed], **args)[
        "segment_input_digest"] != second["segment_input_digest"]
    with pytest.raises(ProductionError, match="REQUIRED_INPUT"):
        build_request(segment_id="segment_000002", anchors=[dict(anchor, role="color")], **args)
    with pytest.raises(ProductionError, match="ACCEPTED_PREFIX"):
        build_request(segment_id="segment_000002", anchors=[], **dict(args, accepted_frames={}))
    with pytest.raises(ProductionError, match="ACKNOWLEDGMENT"):
        invoke_local(lambda request, deadline: {"request_digest": request["request_digest"],
                                               "consumed_digests": []}, second)
    def callback(request, deadline):
        return {"request_digest": request["request_digest"], "consumed_digests": [
            digest(item) for item in request["inputs"] + request["anchors"]
            + request["bundle_window"]["channels"]]}
    assert invoke_local(callback, second)["request_digest"] == second["request_digest"]
    with pytest.raises(ProductionError, match="CAPABILITY_LOCK|PLAN_SEAL"):
        build_request(segment_id="segment_000002", anchors=[], **dict(
            args, capability=dict(cap, max_frames=4)))
    with pytest.raises(ProductionError, match="ANCHOR_CAMERA"):
        build_request(segment_id="segment_000002", anchors=[dict(
            anchor, space="world_m", position_m=[1, 0, 0], camera_subject="missing")], **args)
    with pytest.raises(ProductionError, match="LATENT"):
        latent_cap = dict(cap, timeline_inputs=cap["timeline_inputs"] + ["latent"],
                          latent_identity={"model": "a", "vae": "a", "shape": [1],
                                           "dtype": "float32", "version": "1"})
        latent_plan = compile_bundle_plan(bundle, latent_cap, shot_id="shot")
        build_request(segment_id="segment_000002", anchors=[], accepted_latent={
            "identity": dict(latent_cap["latent_identity"], vae="b"),
            "artifact": ref, "segment_id": "segment_000001"
        }, **dict(args, plan=latent_plan, capability=latent_cap,
                  capability_digest=digest(latent_cap)))


def test_actual_adapter_mp4_is_a_measured_derivative(tmp_path):
    from PIL import Image
    from fsf.conformance.media import ensure_media_tools_on_path
    from fsf.production.control import _run
    from fsf.production.timeline import verify_mp4_initialization

    ffmpeg, ffprobe = ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:
        pytest.skip("media tools unavailable")
    Image.new("RGB", (32, 32), (128, 128, 128)).save(tmp_path / "input.png")
    _run([ffmpeg, "-nostdin", "-v", "error", "-framerate", "24000/1001", "-i",
          str(tmp_path / "input.png"), "-frames:v", "1", "-threads", "1",
          "-c:v", "libx264rgb", "-crf", "0", "-video_track_timescale", "24000",
          str(tmp_path / "adapter.mp4")], 30)
    source, target = (reference(tmp_path, name) for name in ("input.png", "adapter.mp4"))
    result = verify_mp4_initialization(tmp_path, source, target,
                                       clock={"num": 24000, "den": 1001}, width=32, height=32)
    assert result["metrics"]["max_abs_error_8bit"] == 0
    assert result["source"]["sha256"] != result["derived"]["sha256"]


@pytest.mark.parametrize("subject,prop,phase", [
    ("buyer", "shopping_bag", "return_from_shop"),
    ("negotiator", "briefcase", "leave_restaurant"),
    ("guide", "catalog", "tour_with_guest"),
])
def test_revisit_keeps_camera_and_role_references(subject, prop, phase):
    def event(track, who, span, value, mode="hold"):
        return {"track": track, "subject": who, "range": span,
                "value": value, "interpolation": mode}
    events = [
        event("camera", "main", [0, 240], {"from_m": [0, 0, 0], "to_m": [10, 0, 0]}, "linear"),
        event("camera", "main", [240, 480], {"from_m": [10, 0, 0], "to_m": [0, 0, 0]}, "linear"),
        event("camera", "main", [480, 720], {"from_m": [0, 0, 0], "to_m": [0, 0, 0]}),
        event("action", subject, [240, 480], {"from_m": [10, 0, 0], "to_m": [0, 0, 0]}, "linear"),
        event("prop", prop, [240, 720], {"holder": subject}),
        event("world", "location", [0, 720], {"continuity": "same_location"}),
        event("prompt", subject, [240, 720], phase),
    ]
    clock = {"num": 24, "den": 1}
    assert compile_schedule(clock, 720, events)["tracks"] == events
    assert sample_motion(events[3], 240, clock)["speed_m_s"] == 1
    assert sample_motion(events[2], 480, clock)["position_m"] == [0, 0, 0]
    events[2]["value"] = {"from_m": [1, 0, 0], "to_m": [1, 0, 0]}
    with pytest.raises(ProductionError, match="CAMERA_DISCONTINUITY"):
        compile_schedule(clock, 720, events)
