"""CLI status observations across separate local process invocations."""
import copy
import hashlib
import json
import os
import socket
import subprocess
import sys

from fsf.conformance.digest import canonical_contract_digest
from fsf.runtime.plan import build_execution_plan
from test_d_consistency_review_integration import _build_review_required_job
from test_start_handoff import accept


def run_cli(*args, expected=0):
    result = subprocess.run([sys.executable, "-m", "fsf.cli", *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout


def fingerprint(root):
    return {path.relative_to(root).as_posix(): (
        path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
    ) for path in (root, *root.rglob("*"))} if root.exists() else {}


def status(root, state, *, expected=0):
    before = fingerprint(root)
    result = json.loads(run_cli("status", root, "--json", expected=expected))
    assert result["state"] == state
    assert fingerprint(root) == before
    return result


def test_status_before_start_and_after_demo(tmp_path):
    root = tmp_path / "first job"
    assert status(root, "not_started")["next_action"]["kind"] == "start"
    assert not root.exists()
    run_cli("demo", "--out", root)
    done = status(root, "completed")
    assert done["next_action"]["argv"] == ["fsf", "verify", "--job", str(root)]
    assert done["blockers"] == done["workorders"] == []
    assert "completed" in run_cli("status", root)
    journal = root / "fsf_execution_journal.json"
    value = json.loads(journal.read_bytes())
    del value["execution_status"]
    journal.write_text(json.dumps(value), encoding="utf-8")
    status(root, "completed")
    (root / "plan.json").unlink()
    status(root, "unknown")
    journal.write_text("{", encoding="utf-8")
    assert status(root, "unknown", expected=1)["blockers"] == [{"code": "STATUS_RECORD_INVALID"}]


def test_status_tracks_handoff_review_and_resume(tmp_path):
    root = tmp_path / "handoff job"
    plan_path = tmp_path / "original-plan.json"
    job = _build_review_required_job("status")
    shot = job["tasks"][0]
    shot["duration_seconds"] = shot["shot_spec"]["duration_seconds"] = 2.0
    second = copy.deepcopy(shot)
    second["id"] = "S02"
    second["consistency_context"]["continues_from"] = {
        "shot_id": "S01", "selection_rule": "latest_completed", "aspects": ["appearance"],
    }
    job["tasks"].append(second)
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False,
                                enable_assembly=True, enable_media_conformance=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    execute = ["execute", plan_path, "--out", root, "--handoff"]
    run_cli(*execute, expected=10)
    order, = status(root, "waiting_handoff")["workorders"]
    assert order["attempt_id"] == 1
    order_path = root / order["path"]
    original = order_path.read_bytes()
    workorder = json.loads(original)
    changed = {**workorder, "attempt_id": 2}
    changed["workorder_digest"] = canonical_contract_digest(
        {key: value for key, value in changed.items() if key != "workorder_digest"})
    (order_path.parent / "stale.workorder.json").write_text(json.dumps(changed), encoding="utf-8")
    restarted = status(root, "waiting_handoff")
    assert restarted["workorders"] == [order]
    order_path.write_text(json.dumps(changed), encoding="utf-8")
    blocked = status(root, "blocked")
    assert "WORKORDER_UNVERIFIED" in {item["code"] for item in blocked["blockers"]}
    order_path.write_bytes(original)
    accept(root, workorder)
    assert status(root, "waiting_handoff")["next_action"]["kind"] == "resume"
    run_cli(*execute, expected=10)
    waiting = status(root, "waiting_review")
    review = next(item for item in waiting["blockers"] if item["code"] == "REVIEW_REQUIRED")
    assert (review["workorder_id"], review["attempt_id"]) == (order["workorder_id"], 1)
    run_cli("review", "approve", root, review["step_id"])
    next_order = next(row for row in status(root, "waiting_handoff")["workorders"]
                      if row["step_id"] != order["step_id"])
    accept(root, json.loads((root / next_order["path"]).read_bytes()))
    run_cli(*execute, expected=2)
    waiting = status(root, "waiting_review")
    review, = [item for item in waiting["blockers"] if item["code"] == "REVIEW_REQUIRED"]
    artifact = root / next_order["target_artifact"]
    original = artifact.read_bytes()
    artifact.write_bytes(b"changed")
    assert "REVIEW_ARTIFACT_CHANGED" in {item["code"] for item in status(root, "blocked")["blockers"]}
    artifact.write_bytes(original)
    run_cli("review", "approve", root, review["step_id"])
    assert status(root, "ready_to_resume")["next_action"]["kind"] == "resume"
    run_cli(*execute, "--run-id", "restarted")
    done = status(root, "completed")
    assert done["run_id"] == "restarted" and done["workorders"] == []
    assert done["next_action"]["kind"] == "verify_with_plan"
    assert json.loads(run_cli("verify", "--job", root, "--plan", plan_path))["evaluation_status"] == "conforms"


def test_status_failed_step_is_observation_not_retry(tmp_path):
    journal = {"schema_version": "fsf_plan_execution_journal_v1", "job_id": "job", "run_id": "run",
               "plan_digest": "digest", "execution_status": "failed",
               "steps": {"S01": {"status": "failed", "attempt_id": 2, "error": "private details"}}}
    (tmp_path / "fsf_execution_journal.json").write_text(json.dumps(journal), encoding="utf-8")
    failed = status(tmp_path, "failed")
    assert failed["blockers"] == [{"code": "STEP_FAILED", "step_id": "S01", "attempt_id": 2, "workorder_id": None}]
    assert "private details" not in json.dumps(failed)


def test_status_does_not_enter_execution_or_connect(tmp_path, monkeypatch, capsys):
    from fsf.cli.main import main
    from fsf.runtime import handoff
    from fsf.runtime.plan_executor import PlanExecutor

    def forbidden(*args, **kwargs):
        raise AssertionError("Status must only read local records")

    monkeypatch.setattr(PlanExecutor, "execute_plan", forbidden)
    monkeypatch.setattr(handoff, "write_workorder", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    journal = {"schema_version": "fsf_plan_execution_journal_v1", "job_id": "job", "run_id": "run",
               "plan_digest": "digest", "execution_status": "waiting_external",
               "steps": {"S01": {"status": "pending_external", "attempt_id": 1}}}
    (tmp_path / "fsf_execution_journal.json").write_text(json.dumps(journal), encoding="utf-8")
    before = fingerprint(tmp_path)
    assert main(["status", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "waiting_external"
    assert fingerprint(tmp_path) == before
