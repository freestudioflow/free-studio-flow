"""Tests for FSF-M1 compiler."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from fsf.intake.compiler import compile_job, compile_job_from_profile
from prodocux_kernel.intake.docx import profile_docx


def test_compile_from_kernel_profile(sample_docx: Path) -> None:
    profile = profile_docx(sample_docx)
    assert profile["schema_version"] == "prodocux_docx_profile_v1"
    assert profile["source"]["sha256"]

    manifest = compile_job_from_profile(profile)
    assert manifest["schema_version"] == "fsf_video_job_v1"
    assert manifest["task_count"] == 3
    assert len(manifest["tasks"]) == 3

    # Check first task
    t1 = manifest["tasks"][0]
    assert t1["id"] == "task_001"
    assert t1["mode"] == "text_to_video"
    assert t1["duration_seconds"] == 5.0
    assert t1["normalized_prompt"].endswith("no vintage film.")
    assert "seconds" not in t1["normalized_prompt"]
    assert t1["shot_spec"]["schema_version"] == "fsf_shot_spec_v1"
    assert t1["shot_spec"]["shot_id"] == "task_001"
    assert t1["shot_spec"]["provenance"]["source_sha256"] == profile["source"]["sha256"]

    # Check second task duration
    t2 = manifest["tasks"][1]
    assert t2["id"] == "task_002"
    assert t2["duration_seconds"] == 10.0

    # Check third task duration
    t3 = manifest["tasks"][2]
    assert t3["id"] == "task_003"
    assert t3["duration_seconds"] == 5.0


def test_compile_job_artifacts(sample_docx: Path, tmp_path: Path) -> None:
    out = tmp_path / "job_out"
    manifest, request, profile = compile_job(sample_docx, out_dir=out)

    assert (out / "job_manifest.json").is_file()
    assert (out / "pdx_tool_request.json").is_file()
    assert (out / "prodocux_docx_profile.json").is_file()

    saved_manifest = json.loads((out / "job_manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest["task_count"] == 3

    saved_request = json.loads((out / "pdx_tool_request.json").read_text(encoding="utf-8"))
    assert saved_request["schema_version"] == "pdx_tool_request_v1"
    assert saved_request["tool"] == "broll.library.ltx23.t2v"
    assert len(saved_request["inputs"]["shots"]) == 3
