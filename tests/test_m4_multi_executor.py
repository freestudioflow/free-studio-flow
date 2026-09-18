"""Tests for FSF-M4: Multi-Executor Registry & Invariant Guarantees."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.cli.main import main
from fsf.executors.registry import DEFAULT_REGISTRY, get_executor
from fsf.intake.compiler import compile_job


def test_registry_executors_listed() -> None:
    executors = DEFAULT_REGISTRY.list_executors()
    ids = {exc["executor_id"] for exc in executors}
    assert "kaggle.ltx23.t2v" in ids
    assert "local.comfy.video" in ids
    assert "mock.studio.video" in ids


def test_executor_switching_preserves_invariants(sample_docx: Path, tmp_path: Path) -> None:
    """M4 Exit Criterion: Switching backend executor does NOT mutate source prompt or provenance."""
    manifest, request, profile = compile_job(sample_docx)

    # Run with Mock Executor
    mock_out = tmp_path / "mock_run"
    mock_exc = get_executor("mock.studio.video")
    mock_res = mock_exc.execute_shot(manifest["tasks"][0], mock_out)
    assert mock_res["schema_version"] == "pdx_tool_result_v1"
    assert mock_res["outputs"]["executor"] == "mock.studio.video"

    # Run with Kaggle Executor
    kaggle_out = tmp_path / "kaggle_run"
    kaggle_exc = get_executor("kaggle.ltx23.t2v")
    kaggle_res = kaggle_exc.execute_shot(manifest["tasks"][0], kaggle_out)
    assert kaggle_res["schema_version"] == "pdx_tool_result_v1"
    assert kaggle_res["outputs"]["executor"] == "kaggle.ltx23.t2v"

    # Verify Invariants
    # 1. Source prompt in task is strictly preserved
    assert manifest["tasks"][0]["prompt"].startswith("Locked-off wide shot")
    assert manifest["tasks"][0]["shot_spec"]["source_prompt"].startswith("Locked-off wide shot")
    # 2. Provenance SHA remains anchored to Kernel profile
    assert manifest["tasks"][0]["shot_spec"]["provenance"]["source_sha256"] == profile["source"]["sha256"]


def test_cli_plan_and_run(sample_docx: Path, tmp_path: Path, capsys) -> None:
    # 1. CLI Plan
    plan_out = tmp_path / "plan.json"
    exit_plan = main(["plan", str(sample_docx), "--out", str(plan_out)])
    assert exit_plan == 0
    assert plan_out.is_file()
    plan_data = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan_data["schema_version"] == "pdx_execution_plan_v1"

    # 2. CLI Run (M4 + M2 Resume)
    run_out = tmp_path / "cli_run"
    exit_run = main(["run", str(sample_docx), "--out", str(run_out), "--executor", "mock.studio.video"])
    assert exit_run == 0
    assert (run_out / "task_001.mock.bin").is_file()
    assert (run_out / "fsf_checkpoint.json").is_file()

    # 3. CLI Executors
    exit_execs = main(["executors"])
    captured = capsys.readouterr()
    assert exit_execs == 0
    assert "Registered Video Executors" in captured.out
    assert "kaggle.ltx23.t2v" in captured.out
