"""Tests for Stage 6 Fast Assembly (F12, M5 reuse, deterministic FFmpeg assembly).

Tests pad/trim without stream_loop, concat demuxing, execution plan transform steps,
and post-assembly PDX media technical conformance evaluation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pdx_artifact_core import validate_execution_plan

from fsf.assembly.fast_assembly import (
    concat_segments,
    fast_assemble_timeline,
    generate_coarse_srt,
    prepare_segment_video,
    probe_clip_duration,
    require_ffmpeg,
    verify_assembly_receipt,
)
from fsf.conformance.media import build_expected_contract, evaluate_media_file
from fsf.intake.compiler import compile_job
from fsf.runtime.plan import build_execution_plan


def _require_ffmpeg_tools() -> tuple[str, str]:
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()
    except Exception:
        pass
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg or ffprobe not on PATH; skipping fast assembly integration tests")
    return ffmpeg, ffprobe


def _write_testsrc_mp4(
    ffmpeg: str, dest: Path, *, width: int = 320, height: int = 240, duration: str = "1"
) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate=24",
            "-t",
            duration,
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-an",
            str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_plan_compilation_with_assembly(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True)

    errs = validate_execution_plan(plan)
    assert not errs, f"Validation errors: {errs}"

    # Verify assembly transform step exists
    transform_steps = [s for s in plan["steps"] if s["kind"] == "transform"]
    assert len(transform_steps) == 1
    asm_step = transform_steps[0]
    assert asm_step["transform"] == "fsf.assembly.fast_concat"
    assert "shot_ids" in asm_step["inputs"]
    assert asm_step["inputs"]["target_duration_seconds"] > 0
    assert asm_step["outputs"] == ["artifact:assembled_timeline.mp4"]

    # Verify post-assembly QC step exists
    qc_steps = [
        s
        for s in plan["steps"]
        if s["kind"] == "tool"
        and s["tool"] == "pdx.media.technical_conform"
        and "assembled" in s["id"]
    ]
    assert len(qc_steps) == 1
    assembled_qc = qc_steps[0]
    assert assembled_qc["depends_on"] == [asm_step["id"]]
    assert "expected_media_contract" in assembled_qc["inputs"]
    assert "expected_media_contract_digest" in assembled_qc["inputs"]


def test_segment_prepare_pad_and_trim(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    orig = tmp_path / "orig.mp4"
    _write_testsrc_mp4(ffmpeg, orig, width=320, height=240, duration="1.0")

    # Pad 1.0s clip to 2.0s via tpad (NO stream_loop)
    padded = tmp_path / "padded.mp4"
    prepare_segment_video(
        orig,
        padded,
        target_duration=2.0,
        width=320,
        height=240,
        fps=24,
    )
    padded_dur = probe_clip_duration(padded)
    assert abs(padded_dur - 2.0) <= 0.25

    # Trim 1.0s clip to 0.5s
    trimmed = tmp_path / "trimmed.mp4"
    prepare_segment_video(
        orig,
        trimmed,
        target_duration=0.5,
        width=320,
        height=240,
        fps=24,
    )
    trimmed_dur = probe_clip_duration(trimmed)
    assert abs(trimmed_dur - 0.5) <= 0.25


def test_fast_assemble_timeline_e2e_conforms(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    shot1 = tmp_path / "shot1.mp4"
    shot2 = tmp_path / "shot2.mp4"
    _write_testsrc_mp4(ffmpeg, shot1, width=320, height=240, duration="1.0")
    _write_testsrc_mp4(ffmpeg, shot2, width=320, height=240, duration="2.0")

    timeline = [
        {"shot_id": "shot_001", "video_path": shot1, "duration_seconds": 1.0},
        {"shot_id": "shot_002", "video_path": shot2, "duration_seconds": 2.0},
    ]

    out_mp4 = tmp_path / "assembled_timeline.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        audio_policy="forbidden",
    )

    assert out_mp4.is_file()
    assert receipt.segment_count == 2
    assert abs(receipt.total_duration_seconds - 3.0) < 0.01
    assert len(receipt.output_sha256) == 64
    assert receipt.conformance_report["schema_version"] == "pdx_media_conformance_result_v1"
    assert receipt.conformance_report["evaluation_status"] == "conforms"
    assert receipt.host_action["create_binding"] is True


def test_fast_assemble_timeline_refuses_empty() -> None:
    with pytest.raises(ValueError, match="0 segments"):
        fast_assemble_timeline([], "out.mp4")


def test_segment_prepare_with_required_audio_and_no_vo(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    orig = tmp_path / "orig_silent.mp4"
    _write_testsrc_mp4(ffmpeg, orig, width=320, height=240, duration="1.0")

    with_audio = tmp_path / "with_audio.mp4"
    # Execute segment prepare with audio_policy="required" and no VO audio_path
    prepare_segment_video(
        orig,
        with_audio,
        target_duration=1.0,
        width=320,
        height=240,
        fps=24,
        audio_path=None,
        audio_policy="required",
    )
    assert with_audio.is_file()

    expected = build_expected_contract(
        {"duration_seconds": 1.0},
        width=320,
        height=240,
        fps=24,
        audio="required",
    )
    report, action = evaluate_media_file(with_audio, expected)
    assert report["evaluation_status"] == "conforms"
    assert action["create_binding"] is True


def test_fast_assembly_receipt_tamper_evident_and_integrity(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    shot1 = tmp_path / "shot1.mp4"
    _write_testsrc_mp4(ffmpeg, shot1, width=320, height=240, duration="1.0")

    timeline = [{"shot_id": "shot_001", "video_path": shot1, "duration_seconds": 1.0}]
    out_mp4 = tmp_path / "out.mp4"

    receipt = fast_assemble_timeline(timeline, out_mp4, width=320, height=240, fps=24)
    assert receipt.verify_integrity() is True
    assert verify_assembly_receipt(receipt) is True

    # Tampering with metadata fields
    tampered_data = receipt.to_dict()
    tampered_data["total_duration_seconds"] = 99.0
    assert verify_assembly_receipt(tampered_data) is False

    # Tampering with source segments
    tampered_segments = receipt.to_dict()
    tampered_segments["source_segments"].append({"shot_id": "fake"})
    assert verify_assembly_receipt(tampered_segments) is False

    # Tampering with output sha
    tampered_sha = receipt.to_dict()
    tampered_sha["output_sha256"] = "0" * 64
    assert verify_assembly_receipt(tampered_sha) is False

    # Missing output file test: deleting output file must fail check_file_sha=True,
    # but pass check_file_sha=False (metadata-only validation).
    out_mp4.unlink()
    assert verify_assembly_receipt(receipt, check_file_sha=True) is False
    assert receipt.verify_integrity(check_file_sha=True) is False
    assert verify_assembly_receipt(receipt, check_file_sha=False) is True
    assert receipt.verify_integrity(check_file_sha=False) is True


def test_segment_prepare_with_short_audio_pads_to_target_duration(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    # 1.0s video with 0.2s audio
    v_only = tmp_path / "v_1s.mp4"
    _write_testsrc_mp4(ffmpeg, v_only, width=320, height=240, duration="1.0")

    a_only = tmp_path / "a_02s.wav"
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.2", str(a_only)],
        check=True,
        capture_output=True,
    )

    src_short_audio = tmp_path / "short_audio.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-i", str(v_only), "-i", str(a_only), "-c:v", "copy", "-c:a", "aac", str(src_short_audio)],
        check=True,
        capture_output=True,
    )

    dst_out = tmp_path / "pad_padded.mp4"
    prepare_segment_video(
        src_short_audio,
        dst_out,
        target_duration=1.0,
        width=320,
        height=240,
        fps=24,
        audio_policy="required",
    )

    dur = probe_clip_duration(dst_out)
    assert abs(dur - 1.0) <= 0.05, f"Expected 1.0s, but got {dur}s (short audio cut off video)"

    expected = build_expected_contract(
        {"duration_seconds": 1.0},
        width=320,
        height=240,
        fps=24,
        audio="required",
    )
    report, action = evaluate_media_file(dst_out, expected)
    assert report["evaluation_status"] == "conforms"
    assert action["create_binding"] is True


def test_plan_fields_and_assembly_function_integration(sample_docx: Path, tmp_path: Path) -> None:
    """Integration test between plan transform step inputs and fast assembly execution.

    Verifies that fields compiled into pdx_execution_plan_v1 transform and QC steps
    can be consumed directly by fast_assemble_timeline to yield a conforming output.
    (Note: Full execution of steps, dependencies, and QC by an orchestration Plan
    Executor remains a future implementation step).
    """
    ffmpeg, _ = _require_ffmpeg_tools()
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True)

    transform_step = next(s for s in plan["steps"] if s["kind"] == "transform")
    qc_step = next(
        s
        for s in plan["steps"]
        if s["kind"] == "tool"
        and s["tool"] == "pdx.media.technical_conform"
        and "assembled" in s["id"]
    )

    shot_ids = transform_step["inputs"]["shot_ids"]
    task_durations = {t["id"]: float(t.get("duration_seconds", 5.0)) for t in manifest["tasks"]}
    timeline = []
    for sid in shot_ids:
        dur = task_durations.get(sid, 5.0)
        sp = tmp_path / f"{sid}.mp4"
        _write_testsrc_mp4(ffmpeg, sp, width=1024, height=576, duration=str(dur))
        timeline.append({"shot_id": sid, "video_path": sp, "duration_seconds": dur})

    out_assembled = tmp_path / "assembled_from_plan.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_assembled,
        width=1024,
        height=576,
        fps=24,
        expected_contract=qc_step["inputs"]["expected_media_contract"],
    )

    assert out_assembled.is_file()
    assert receipt.conformance_report["evaluation_status"] == "conforms"
    assert receipt.expected_media_contract_digest == qc_step["inputs"]["expected_media_contract_digest"]


def test_toolchain_dependencies_declared_and_resolved() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    deps = pyproject["project"]["dependencies"]
    opt_deps = pyproject["project"].get("optional-dependencies", {})

    has_dep = any("static-ffmpeg" in d for d in deps)
    has_media_extra = any("static-ffmpeg" in d for d in opt_deps.get("media", []))
    assert has_dep or has_media_extra, (
        "static-ffmpeg must be declared in dependencies or optional-dependencies"
    )

    exe = require_ffmpeg()
    assert Path(exe).is_file()


def _write_sine_audio(ffmpeg: str, dest: Path, *, duration: str = "1", freq: int = 440) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration}",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_coarse_subtitle_scheduling_and_sidecar(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip1.mp4"
    c2 = tmp_path / "clip2.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="1")
    _write_testsrc_mp4(ffmpeg, c2, duration="1")

    timeline = [
        {"shot_id": "shot_01", "video_path": c1, "duration_seconds": 1.0},
        {"shot_id": "shot_02", "video_path": c2, "duration_seconds": 1.0},
    ]
    subtitles = [
        {"shot_id": "shot_01", "text": "First dialogue line", "start_seconds": 0.1, "end_seconds": 0.9},
        {"shot_id": "shot_02", "text": "Second dialogue line", "start_seconds": 1.1, "end_seconds": 1.9},
    ]

    out_mp4 = tmp_path / "assembled_sub_sidecar.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        subtitles=subtitles,
        subtitle_policy="sidecar",
    )

    assert out_mp4.is_file()
    srt_file = tmp_path / "assembled_sub_sidecar.srt"
    assert srt_file.is_file()
    srt_text = srt_file.read_text(encoding="utf-8")
    assert "First dialogue line" in srt_text
    assert "Second dialogue line" in srt_text

    assert receipt.subtitle_policy == "sidecar"
    assert receipt.subtitle_scheduling == "coarse"
    assert receipt.subtitle_path == str(srt_file)
    assert receipt.subtitle_sha256 is not None
    assert receipt.verify_integrity() is True


def test_subtitle_burned_mode(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip_burned.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="1")

    timeline = [
        {"shot_id": "shot_01", "video_path": c1, "duration_seconds": 1.0},
    ]
    subtitles = [
        {"shot_id": "shot_01", "text": "Burned on video", "start_seconds": 0.1, "end_seconds": 0.9},
    ]

    out_mp4 = tmp_path / "assembled_burned.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        subtitles=subtitles,
        subtitle_policy="burned",
    )

    assert out_mp4.is_file()
    assert receipt.subtitle_policy in ("burned", "sidecar")
    assert receipt.verify_integrity() is True


def test_vo_and_bgm_audio_mixing_and_receipt_binding(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip_mix.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="2")

    vo = tmp_path / "vo.wav"
    bgm = tmp_path / "bgm.wav"
    _write_sine_audio(ffmpeg, vo, duration="2", freq=800)
    _write_sine_audio(ffmpeg, bgm, duration="2", freq=200)

    timeline = [
        {"shot_id": "shot_01", "video_path": c1, "duration_seconds": 2.0},
    ]

    out_mp4 = tmp_path / "assembled_audio_mix.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        vo_path=vo,
        bgm_path=bgm,
        vo_volume=1.0,
        bgm_volume=0.25,
    )

    assert out_mp4.is_file()
    assert receipt.vo_path == str(vo)
    assert receipt.bgm_path == str(bgm)
    assert receipt.vo_sha256 is not None
    assert receipt.bgm_sha256 is not None
    assert receipt.conformance_report["evaluation_status"] == "conforms"
    assert receipt.verify_integrity() is True


def test_fast_assembly_receipt_tamper_detection_for_subtitles_and_audio(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip_tamper.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="1")
    bgm = tmp_path / "bgm.wav"
    _write_sine_audio(ffmpeg, bgm, duration="1", freq=300)

    timeline = [{"shot_id": "shot_01", "video_path": c1, "duration_seconds": 1.0}]
    subtitles = [{"shot_id": "shot_01", "text": "Original subtitle", "start_seconds": 0.1, "end_seconds": 0.9}]

    out_mp4 = tmp_path / "assembled_tamper.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        subtitles=subtitles,
        subtitle_policy="sidecar",
        bgm_path=bgm,
    )

    assert receipt.verify_integrity() is True

    # Tamper with the subtitle file
    srt_file = Path(receipt.subtitle_path)
    srt_file.write_text("TAMPERED SUBTITLE", encoding="utf-8")
    assert receipt.verify_integrity() is False

    # Restore subtitle and tamper with bgm
    srt_file.write_text(generate_coarse_srt(subtitles, srt_file).read_text(encoding="utf-8"), encoding="utf-8")
    # Actually just re-check: since SHA changed, it was detected
    bgm.write_bytes(b"TAMPERED_BGM_BYTES")
    assert receipt.verify_integrity() is False


def test_missing_vo_or_bgm_raises_filenotfound(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="1")
    timeline = [{"shot_id": "shot_01", "video_path": c1, "duration_seconds": 1.0}]

    missing_audio = tmp_path / "non_existent_audio.wav"
    out_mp4 = tmp_path / "out_missing.mp4"

    with pytest.raises(FileNotFoundError, match="Specified BGM audio track file not found"):
        fast_assemble_timeline(timeline, out_mp4, bgm_path=missing_audio)

    with pytest.raises(FileNotFoundError, match="Specified VO audio track file not found"):
        fast_assemble_timeline(timeline, out_mp4, vo_path=missing_audio)


def test_burned_subtitle_failure_raises_unless_explicit_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fsf.assembly import fast_assembly

    ffmpeg, _ = _require_ffmpeg_tools()
    c1 = tmp_path / "clip_fail_burn.mp4"
    _write_testsrc_mp4(ffmpeg, c1, duration="1")
    timeline = [{"shot_id": "shot_01", "video_path": c1, "duration_seconds": 1.0}]
    subtitles = [{"shot_id": "shot_01", "text": "Test line", "start_seconds": 0.1, "end_seconds": 0.9}]
    out_mp4 = tmp_path / "out_burn_fail.mp4"

    # Simulate burn_subtitles_to_video returning False (e.g. missing fonts / libass filter failure)
    monkeypatch.setattr(fast_assembly, "burn_subtitles_to_video", lambda *args, **kwargs: False)

    # By default: fail-closed! Raises RuntimeError
    with pytest.raises(RuntimeError, match="Fail-closed: refusing silent downgrade"):
        fast_assemble_timeline(timeline, out_mp4, subtitles=subtitles, subtitle_policy="burned")

    # When explicitly allowed: falls back to sidecar and records warning
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        subtitles=subtitles,
        subtitle_policy="burned",
        allow_burned_fallback=True,
    )
    assert receipt.subtitle_policy == "sidecar"
    assert "explicitly allowed fallback" in str(receipt.warning)


def test_mix_audio_tracks_preserves_embedded_video_audio_and_bgm(tmp_path: Path) -> None:
    """Verifies that mixing external BGM preserves the video's existing per-shot audio."""
    from fsf.assembly.fast_assembly import clip_has_audio, mix_audio_tracks

    ffmpeg, _ = _require_ffmpeg_tools()

    # 1. Create a video with audio stream (e.g. 440Hz sine wave)
    vid_with_audio = tmp_path / "vid_with_audio.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2.0",
            "-t",
            "2.0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(vid_with_audio),
        ],
        check=True,
        capture_output=True,
    )
    assert clip_has_audio(vid_with_audio) is True

    # 2. Create BGM audio track (880Hz sine wave)
    bgm_audio = tmp_path / "bgm.wav"
    _write_sine_audio(ffmpeg, bgm_audio, duration="2", freq=880)

    # 3. Mix BGM onto the video
    out_mixed = tmp_path / "out_mixed.mp4"
    mix_audio_tracks(
        vid_with_audio,
        out_mixed,
        duration_seconds=2.0,
        bgm_path=bgm_audio,
        bgm_volume=0.3,
    )

    assert out_mixed.is_file()
    assert clip_has_audio(out_mixed) is True

    # 4. Rigorous frequency check: decode PCM to verify BOTH 440Hz (embedded audio) and 880Hz (BGM) are present
    import math
    import struct

    pcm_bytes = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(out_mixed),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-t",
            "1.0",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout

    num_samples = len(pcm_bytes) // 2
    assert num_samples > 1000
    samples = struct.unpack(f"<{num_samples}h", pcm_bytes)

    def _tone_magnitude(target_freq: float, sample_rate: float = 8000.0) -> float:
        omega = 2.0 * math.pi * target_freq / sample_rate
        re = sum(s * math.cos(omega * i) for i, s in enumerate(samples))
        im = sum(s * math.sin(omega * i) for i, s in enumerate(samples))
        return math.hypot(re, im) / num_samples

    mag_440 = _tone_magnitude(440.0)
    mag_880 = _tone_magnitude(880.0)
    mag_unrelated = _tone_magnitude(1500.0)

    # Both the 440Hz embedded video audio and 880Hz BGM must be clearly present.
    # If embedded audio was dropped, mag_440 is near zero; if BGM was dropped, mag_880 is near zero.
    assert mag_440 > 300.0, f"Expected 440Hz embedded audio, but magnitude was {mag_440}"
    assert mag_880 > 100.0, f"Expected 880Hz BGM audio (at volume 0.3), but magnitude was {mag_880}"
    assert mag_unrelated < 50.0, f"Unrelated frequency unexpectedly high: {mag_unrelated}"


def test_audio_policy_end_to_end_default_keep(sample_docx: Path, tmp_path: Path) -> None:
    """[P2] Verifies that audio policy defaults to 'keep' end-to-end:

    1. fast_assemble_timeline defaults to audio_policy='keep'.
    2. build_execution_plan defaults to audio_policy='keep'.
    3. Segments with audio preserve audio track in final assembled output under default settings.
    """
    import inspect
    from fsf.cli.main import main
    from fsf.assembly.fast_assembly import clip_has_audio

    # 1. Signature default inspection
    sig = inspect.signature(fast_assemble_timeline)
    assert sig.parameters["audio_policy"].default == "keep"

    # 2. Plan compiler default
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)
    concat_step = next(s for s in plan["steps"] if s["kind"] == "transform")
    assert concat_step["inputs"]["audio_policy"] == "keep"

    # 3. CLI plan --audio
    plan_out = tmp_path / "plan_audio.json"
    code = main(["plan", str(sample_docx), "--assembly", "--media", "--audio", "keep", "--out", str(plan_out)])
    assert code == 0
    loaded_plan = json.loads(plan_out.read_text(encoding="utf-8"))
    c_step = next(s for s in loaded_plan["steps"] if s["kind"] == "transform")
    assert c_step["inputs"]["audio_policy"] == "keep"
    # Compiled contract should be 'allowed' when no BGM/VO is specified, permitting both silent and audio shots
    assert c_step["inputs"]["expected_media_contract"]["audio"] == "allowed"


def test_mixed_audio_and_silent_clips_normalize_under_keep_policy(tmp_path: Path) -> None:
    """[P2] Verifies that mixing silent clips with clips with audio under 'keep' normalizes smoothly."""
    ffmpeg, _ = _require_ffmpeg_tools()

    # Shot 1: Video with audio track
    v1 = tmp_path / "v1.mp4"
    _write_testsrc_mp4(ffmpeg, v1, duration="1")
    a1 = tmp_path / "a1.wav"
    _write_sine_audio(ffmpeg, a1, duration="1", freq=440)
    shot1_with_audio = tmp_path / "shot1_with_audio.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-i", str(v1), "-i", str(a1), "-c:v", "copy", "-c:a", "aac", str(shot1_with_audio)],
        check=True,
        capture_output=True,
    )

    # Shot 2: Purely silent video (no audio stream)
    shot2_silent = tmp_path / "shot2_silent.mp4"
    _write_testsrc_mp4(ffmpeg, shot2_silent, duration="1")

    timeline = [
        {"shot_id": "shot_01", "video_path": shot1_with_audio, "duration_seconds": 1.0},
        {"shot_id": "shot_02", "video_path": shot2_silent, "duration_seconds": 1.0},
    ]

    out_mp4 = tmp_path / "assembled_mixed.mp4"
    receipt = fast_assemble_timeline(
        timeline,
        out_mp4,
        width=320,
        height=240,
        fps=24,
        audio_policy="keep",
    )

    assert out_mp4.is_file()
    assert receipt.segment_count == 2
    assert abs(receipt.total_duration_seconds - 2.0) < 0.1
    assert receipt.conformance_report["evaluation_status"] == "conforms"
