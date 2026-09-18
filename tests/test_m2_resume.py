"""Tests for FSF-M2: Execution Plans, Checkpoints, Secret Scrubbing, and Resume."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.intake.compiler import compile_job
from fsf.runtime.external_op import (
    build_resumed_job,
    create_fsf_external_op,
    inspect_existing_artifacts,
    save_checkpoint,
    scrub_secrets,
)
from fsf.runtime.plan import build_execution_plan


def test_build_execution_plan(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, executor_tool="studio.video.generate")

    assert plan["schema_version"] == "pdx_execution_plan_v1"
    assert len(plan["steps"]) == 6  # 3 tasks * 2 steps (gen + qc)
    assert plan["steps"][0]["tool"] == "studio.video.generate"
    assert plan["steps"][1]["tool"] == "pdx.media.technical_conform"
    assert plan["steps"][1]["depends_on"] == [plan["steps"][0]["id"]]


def test_secret_scrubbing() -> None:
    win_user = "C:" + r"\Users\Administrator\secret.txt"
    dirty_text = json.dumps(
        {
            "api_key": "sk-" + "12345678901234567890abcdef",
            "token": "HF_" + "mysecrettoken",
            "kaggle_token": "ghp_" + "supersecret123",
            "path": win_user,
        }
    )
    clean = scrub_secrets(dirty_text)
    assert "12345678901234567890abcdef" not in clean
    assert "mysecrettoken" not in clean
    assert "supersecret123" not in clean
    assert "Administrator" not in clean
    assert "[REDACTED_SECRET]" in clean
    assert "[REDACTED_USER_PATH]" in clean


def test_create_external_operation() -> None:
    op = create_fsf_external_op(
        operation_id="op:test:001",
        provider="kaggle",
        inputs={"prompt": "Locked-off shot 5s", "api_key": "secret_12345"},
    )
    assert op["schema_version"] == "pdx_external_operation_v1"
    assert op["operation_id"] == "op:test:001"
    assert op["provider"] == "kaggle"
    assert "secret_12345" not in json.dumps(op)


def test_resume_only_runs_pending_tasks(sample_docx: Path, tmp_path: Path) -> None:
    """M2 Exit Criterion: Partial success allows resume without re-running completed shots."""
    out = tmp_path / "resume_run"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)

    # Simulate: task_001 succeeded and generated artifact
    t1_file = out / "task_001.mock.bin"
    t1_file.write_bytes(b"DETERMINISTIC_VIDEO_BYTES:task_001")

    # task_002 and task_003 did not finish yet
    status_map = inspect_existing_artifacts(manifest, out)
    assert status_map["task_001"]["completed"] is True
    assert status_map["task_002"]["completed"] is False
    assert status_map["task_003"]["completed"] is False

    # Build resumed job
    resumed_manifest, pending, completed = build_resumed_job(manifest, out)
    assert resumed_manifest["is_resumed"] is True
    assert len(completed) == 1
    assert completed[0]["id"] == "task_001"
    assert completed[0]["execution_status"] == "reused"

    assert len(pending) == 2
    assert pending[0]["id"] == "task_002"
    assert pending[1]["id"] == "task_003"

    # Save checkpoint
    save_checkpoint(out, resumed_manifest, {"task_001": status_map["task_001"]})
    assert (out / "fsf_checkpoint.json").is_file()
