"""FSF Media Conformance Consumer (FSF-M3).

Compiles FSF shot specs and output contracts into PDX Media expected specifications,
and delegates evaluation directly to `pdx-adapter-media` without duplicate ffprobe logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pdx_adapter_media import (
    FfprobeRunner,
    ProbeUnavailable,
    build_technical_profile_v2,
    evaluate_media_conformance,
    not_evaluated_result,
)

# Standard B-roll / Studio Pad baseline expected contract
DEFAULT_BROLL_EXPECTED = {
    "containers": ["mp4"],
    "video_codecs": ["h264"],
    "width": 1024,
    "height": 576,
    "fps": 24,
    "fps_tolerance": 0.01,
    "duration_seconds": 5.0,
    "duration_tolerance_seconds": 0.25,
    "audio": "forbidden",
    "max_black_frame_ratio": 0.01,
    "max_freeze_duration_seconds": 0.5,
}

HOST_CONFORMANCE_MAPPINGS = {
    "not_evaluated": {
        "create_binding": False,
        "check_terminal_outcome": None,
        "host_action": "continue_without_technical_check",
    },
    "evaluation_failed": {
        "create_binding": True,
        "check_terminal_outcome": "failed",
        "host_action": "fail_block_or_retry",
    },
    "does_not_conform": {
        "create_binding": True,
        "check_terminal_outcome": "succeeded",
        "host_action": "block_next_product_step",
    },
    "conforms": {
        "create_binding": True,
        "check_terminal_outcome": "succeeded",
        "host_action": "permit_next_step",
    },
}


def build_expected_contract(
    task_or_spec: dict[str, Any],
    *,
    width: int = 1024,
    height: int = 576,
    fps: int = 24,
    audio: str = "forbidden",
    containers: list[str] | None = None,
    video_codecs: list[str] | None = None,
) -> dict[str, Any]:
    """Compile a task or shot spec into a pdx_media_conformance expected contract."""
    duration = float(task_or_spec.get("duration_seconds", 5.0))
    expected = {
        "containers": containers or ["mp4"],
        "video_codecs": video_codecs or ["h264"],
        "width": width,
        "height": height,
        "fps": fps,
        "fps_tolerance": 0.01,
        "duration_seconds": duration,
        "duration_tolerance_seconds": 0.25,
        "audio": audio,
        "max_black_frame_ratio": 0.01,
        "max_freeze_duration_seconds": 0.5,
    }
    return expected


def evaluate_media_file(
    media_path: str | Path,
    expected_contract: dict[str, Any] | None = None,
    *,
    probe_runner: Any | None = None,
    request_id: str = "request:fsf:media:eval",
    skip_evaluation: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate a media file against expected technical conformance using PDX Media Adapter.

    Returns:
        (conformance_report, host_action_mapping)
    """
    path = Path(media_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")

    expected = expected_contract or DEFAULT_BROLL_EXPECTED
    probe = probe_runner or FfprobeRunner()

    if skip_evaluation:
        profile = build_technical_profile_v2(path, probe)
        report = not_evaluated_result(profile)
    else:
        profile = build_technical_profile_v2(path, probe)
        req = {
            "schema_version": "pdx_media_conformance_request_v1",
            "request_id": request_id,
            "profile": profile,
            "expected": expected,
        }
        report = evaluate_media_conformance(req)

    status = report.get("evaluation_status", "evaluation_failed")
    host_action = map_conformance_status_to_host(status)

    return report, host_action


def map_conformance_status_to_host(status: str) -> dict[str, Any]:
    """Map a Media evaluation status to a host decision/action dict."""
    return HOST_CONFORMANCE_MAPPINGS.get(
        status,
        {
            "create_binding": True,
            "check_terminal_outcome": "failed",
            "host_action": "fail_block_or_retry",
        },
    )
