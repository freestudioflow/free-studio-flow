"""Tests for M1 Determinism: Same document intaken twice produces exact matches."""

from __future__ import annotations

import json
from pathlib import Path
from fsf.intake.compiler import compile_job


def test_m1_intake_determinism(sample_docx: Path, tmp_path: Path) -> None:
    """M1 Exit Criterion: The same prompt sheet intaken twice yields stable profile SHA and task IDs."""
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"

    manifest1, req1, prof1 = compile_job(sample_docx, out_dir=out1)
    manifest2, req2, prof2 = compile_job(sample_docx, out_dir=out2)

    # 1. Profile SHA must match
    assert prof1["source"]["sha256"] == prof2["source"]["sha256"]
    assert manifest1["source_sha256"] == manifest2["source_sha256"]

    # 2. Tasks count and IDs must be bit-for-bit identical
    assert manifest1["task_count"] == manifest2["task_count"]
    for t1, t2 in zip(manifest1["tasks"], manifest2["tasks"]):
        assert t1["id"] == t2["id"]
        assert t1["prompt"] == t2["prompt"]
        assert t1["normalized_prompt"] == t2["normalized_prompt"]
        assert t1["duration_seconds"] == t2["duration_seconds"]
        assert t1["shot_spec"] == t2["shot_spec"]

    # 3. Tool requests must be identical
    assert req1 == req2

    # 4. JSON files written to disk must match bit-for-bit
    assert (out1 / "job_manifest.json").read_bytes() == (out2 / "job_manifest.json").read_bytes()
    assert (out1 / "pdx_tool_request.json").read_bytes() == (out2 / "pdx_tool_request.json").read_bytes()
    assert (out1 / "prodocux_docx_profile.json").read_bytes() == (out2 / "prodocux_docx_profile.json").read_bytes()
