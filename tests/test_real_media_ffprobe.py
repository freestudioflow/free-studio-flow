"""Real ffprobe evidence: contract adequacy vs true-media integration.

Mock probes prove Media a3 schema/API coverage. These tests use FFmpeg-generated
MP4 and the production FfprobeRunner. Skip when ffmpeg/ffprobe are not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fsf.conformance.media import build_expected_contract, evaluate_media_file


def _require_ffprobe() -> None:
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
