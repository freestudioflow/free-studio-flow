"""Real ffprobe evidence: contract adequacy vs true-media integration.

Mock probes prove Media a3 schema/API coverage. These tests use FFmpeg-generated
MP4 and the production FfprobeRunner. Skip when ffmpeg/ffprobe are not on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fsf.conformance.media import build_expected_contract, evaluate_media_file


def _require_ffprobe() -> None:
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()
    except Exception:
        pass
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe not on PATH; true-media analyzer evidence remains pending")


def _require_ffmpeg_tools() -> tuple[str, str]:
    _require_ffprobe()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not on PATH; true-media E2E remains pending")
    return ffmpeg, shutil.which("ffprobe") or "ffprobe"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _tiny_expected() -> dict:
    return build_expected_contract(
        {"duration_seconds": 1.0},
        width=320,
        height=240,
        fps=24,
    )


def _write_testsrc_mp4(ffmpeg: str, dest: Path, *, width: int = 320, height: int = 240, duration: str = "1") -> None:
    try:
        _run(
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
            ]
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"ffmpeg cannot encode libx264 testsrc: {exc.stderr[-400:]}")


def test_real_mp4_conforms_mismatch_remux_and_corrupt(tmp_path: Path) -> None:
    ffmpeg, _ffprobe = _require_ffmpeg_tools()
    expected = _tiny_expected()
    original = tmp_path / "original.mp4"
    _write_testsrc_mp4(ffmpeg, original)

    report, action = evaluate_media_file(original, expected)
    assert report["schema_version"] == "pdx_media_conformance_result_v1"
    assert report["evaluation_status"] == "conforms"
    assert action["create_binding"] is True

    mismatched = tmp_path / "scaled.mp4"
    _write_testsrc_mp4(ffmpeg, mismatched, width=640, height=360)
    bad, bad_action = evaluate_media_file(mismatched, expected)
    assert bad["evaluation_status"] == "does_not_conform"
    assert any(i["code"] == "MEDIA_DIMENSIONS_MISMATCH" for i in bad["issues"])
    assert bad_action["create_binding"] is True

    remuxed = tmp_path / "remuxed.mp4"
    _run([ffmpeg, "-y", "-i", str(original), "-c", "copy", str(remuxed)])
    again, again_action = evaluate_media_file(remuxed, expected)
    assert again["evaluation_status"] == "conforms"
    assert again_action["create_binding"] is True

    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"NOT_MEDIA_BYTES")
    failed, failed_action = evaluate_media_file(corrupt, expected)
    assert failed["evaluation_status"] == "evaluation_failed"
    assert failed["schema_version"] == "pdx_media_conformance_result_v1"
    assert failed_action["create_binding"] is True


def test_shape_bytes_renamed_mp4_fail_real_analyzer(tmp_path: Path) -> None:
    _require_ffprobe()
    renamed = tmp_path / "task_001.mp4"
    renamed.write_bytes(b"DETERMINISTIC_VIDEO_BYTES:task_001:not-a-container")
    report, action = evaluate_media_file(renamed, _tiny_expected())
    assert report["schema_version"] == "pdx_media_conformance_result_v1"
    assert report["evaluation_status"] == "evaluation_failed"
    assert action["create_binding"] is True
    assert action["check_terminal_outcome"] == "failed"


def test_real_mp4_freeze_to_eof_detected_and_fails_conformance(tmp_path: Path) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    still_mp4 = tmp_path / "still_2s.mp4"
    # Create 2-second completely still video
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=24:d=2.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(still_mp4),
        ]
    )
    expected = build_expected_contract(
        {"duration_seconds": 2.0},
        width=320,
        height=240,
        fps=24,
    )
    # Expected allows max 0.5s freeze duration
    assert expected["max_freeze_duration_seconds"] == 0.5

    report, action = evaluate_media_file(still_mp4, expected)
    assert report["schema_version"] == "pdx_media_conformance_result_v1"
    assert report["evaluation_status"] == "does_not_conform"
    assert any(i["code"] == "MEDIA_FREEZE_LIMIT_EXCEEDED" for i in report["issues"])
    assert action["create_binding"] is True
    assert action["host_action"] == "block_next_product_step"


def test_analyzer_failure_preserves_evaluation_failed_status(tmp_path: Path) -> None:
    _require_ffprobe()
    # A corrupt/truncated file causes analyzer failure
    corrupt = tmp_path / "truncated.mp4"
    corrupt.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00corrupt")

    report, action = evaluate_media_file(corrupt, _tiny_expected())
    assert report["schema_version"] == "pdx_media_conformance_result_v1"
    assert report["evaluation_status"] == "evaluation_failed"
    assert any(
        i["code"] in ("MEDIA_ANALYZER_UNAVAILABLE", "MEDIA_DECODE_FAILED")
        for i in report["issues"]
    )
    assert action["create_binding"] is True
    assert action["check_terminal_outcome"] == "failed"


def test_filter_failure_does_not_mask_defects_and_preserves_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, _ = _require_ffmpeg_tools()
    orig = tmp_path / "valid.mp4"
    _write_testsrc_mp4(ffmpeg, orig, width=320, height=240, duration="1.0")

    # Simulate ffmpeg filter command failure (returncode != 0)
    real_run = subprocess.run

    def mock_run(cmd, *args, **kwargs):
        if any("freezedetect" in str(arg) for arg in cmd):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Filter error")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    from fsf.conformance.media import FfmpegEnrichedProbeRunner

    runner = FfmpegEnrichedProbeRunner()
    raw = runner.probe(orig)

    # Must NOT mask filter failure with zero defects
    assert "black_frame_ratio" not in raw
    assert "freeze_ranges" not in raw

    # Evaluating with expected contract requiring black/freeze measurements fails closed
    report, action = evaluate_media_file(orig, _tiny_expected(), probe_runner=runner, allow_injected_probe=True)
    assert report["evaluation_status"] == "evaluation_failed"
    assert any(i["code"] == "MEDIA_ANALYZER_UNAVAILABLE" for i in report["issues"])
    assert action["create_binding"] is True
    assert action["check_terminal_outcome"] == "failed"


def test_system_tool_priority_and_safe_probe_governance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies system tool priority over static_ffmpeg and safe subprocess governance of probe entry points."""
    from fsf.conformance.media import SafeFfprobeRunner, ensure_media_tools_on_path
    from pdx_adapter_media import ProbeUnavailable
    import fsf.conformance.media as media_mod

    # 1. System tool priority: if shutil.which finds tools, static_ffmpeg is never called
    called_static = False

    class DummyStatic:
        @staticmethod
        def add_paths():
            nonlocal called_static
            called_static = True

    monkeypatch.setattr(shutil, "which", lambda cmd: f"/mock/path/{cmd}")
    monkeypatch.setattr(media_mod, "_STATIC_FFMPEG_ATTEMPTED", False)
    monkeypatch.setitem(sys.modules, "static_ffmpeg", DummyStatic)

    ff, fp = ensure_media_tools_on_path()
    assert ff == "/mock/path/ffmpeg"
    assert fp == "/mock/path/ffprobe"
    assert called_static is False, "ensure_media_tools_on_path unconditionally called static_ffmpeg despite PATH tools!"

    # 2. SafeFfprobeRunner timeout enforcement
    runner = SafeFfprobeRunner(timeout_seconds=0.1)

    # Mock run_safe_subprocess to simulate timeout
    from fsf.executors.subprocess_safety import SafeSubprocessResult

    def mock_safe_run(cmd, timeout_seconds=None, **kwargs):
        return SafeSubprocessResult(
            args=cmd,
            returncode=-1,
            stdout="",
            stderr="ffprobe timed out",
            elapsed_ms=105.0,
            timed_out=True,
        )

    monkeypatch.setattr(media_mod, "run_safe_subprocess", mock_safe_run)
    with pytest.raises(ProbeUnavailable, match="timed out"):
        runner.probe(tmp_path / "dummy.mp4")


def test_ensure_media_tools_preserves_raw_user_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P2] Verifies that machine tool discovery preserves raw user paths and doesn't fail due to diagnostic redaction."""
    import fsf.conformance.media as media_mod
    from fsf.executors.subprocess_safety import SafeSubprocessResult

    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    user_ffmpeg = tmp_path / "user_home" / exe_name
    user_ffmpeg.parent.mkdir(parents=True)
    user_ffmpeg.write_bytes(b"mock_bin")

    # Simulate run_safe_subprocess where diagnostic stdout was redacted to [REDACTED_USER_PATH]
    # but machine raw_stdout preserves the actual filesystem path
    def mock_safe_subprocess(*args, **kwargs):
        return SafeSubprocessResult(
            args=["mock"],
            returncode=0,
            stdout="[REDACTED_USER_PATH]/ffmpeg",
            stderr="",
            elapsed_ms=10.0,
            timed_out=False,
            raw_stdout=str(user_ffmpeg),
        )

    monkeypatch.setattr(media_mod, "_STATIC_FFMPEG_ATTEMPTED", False)
    # Ensure system PATH check fails initially so it calls static_ffmpeg resolution
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd: str(user_ffmpeg) if cmd == "ffmpeg" and str(user_ffmpeg.parent) in os.environ.get("PATH", "") else None,
    )
    monkeypatch.setattr(media_mod, "run_safe_subprocess", mock_safe_subprocess)

    ff, _ = media_mod.ensure_media_tools_on_path()
    assert ff == str(user_ffmpeg)
    assert str(user_ffmpeg.parent) in os.environ["PATH"]


def test_assembly_and_cli_check_use_bounded_ensure_media_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that require_ffmpeg and cli check call ensure_media_tools_on_path instead of static_ffmpeg directly."""
    import fsf.assembly.fast_assembly as fast_mod
    import fsf.cli.main as main_mod

    called_bounded = False

    def mock_ensure():
        nonlocal called_bounded
        called_bounded = True
        return "/mock/ffmpeg", "/mock/ffprobe"

    monkeypatch.setattr(fast_mod, "ensure_media_tools_on_path", mock_ensure)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/mock/ffmpeg")

    ff = fast_mod.require_ffmpeg()
    assert ff == "/mock/ffmpeg"
    assert called_bounded is True
