"""CLI status observations across separate local process invocations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import fsf
from fsf.conformance.digest import canonical_contract_digest
from fsf.runtime.plan import build_execution_plan
from test_d_consistency_review_integration import _build_review_required_job
from test_start_handoff import accept


def run_cli(*args, expected=0):
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(Path(fsf.__file__).resolve().parents[1]),
    }
    result = subprocess.run([sys.executable, "-m", "fsf.cli", *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60,
                            env=env)
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
    pending_first = status(root, "waiting_handoff")
    order, = pending_first["workorders"]
    assert order["attempt_id"] == 1
    assert order["write_artifact_to"] == str(root / order["target_artifact"])
    assert order["input_argv"] is None
    assert order["fulfill_argv"] == [
        "fsf", "workorder", "fulfill", str(root), order["workorder_id"],
        "--artifact", order["write_artifact_to"],
    ]
    assert pending_first["next_action"]["kind"] == "fulfill_workorder"
    assert pending_first["next_action"]["argv"] == order["fulfill_argv"]
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
    ready_ingest = status(root, "waiting_handoff")
    assert ready_ingest["next_action"]["kind"] == "resume"
    assert ready_ingest["next_action"]["argv"] == [
        "fsf", "execute", str(root / "plan.json"), "--out", str(root), "--handoff",
    ]
    run_cli(*execute, expected=10)
    waiting = status(root, "waiting_review")
    review = next(item for item in waiting["blockers"] if item["code"] == "REVIEW_REQUIRED")
    assert (review["workorder_id"], review["attempt_id"]) == (order["workorder_id"], 1)
    run_cli("review", "approve", root, review["step_id"])
    next_order = next(row for row in status(root, "waiting_handoff")["workorders"]
                      if row["step_id"] != order["step_id"])
    assert next_order["input_argv"] == [
        "fsf", "workorder", "input", str(root), next_order["workorder_id"],
    ]
    assert next_order["write_artifact_to"] == str(root / next_order["target_artifact"])
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
    ready_resume = status(root, "ready_to_resume")
    assert ready_resume["next_action"]["kind"] == "resume"
    assert ready_resume["next_action"]["argv"] == [
        "fsf", "execute", str(root / "plan.json"), "--out", str(root), "--handoff",
    ]
    run_cli(*execute, "--run-id", "restarted")
    done = status(root, "completed")
    assert done["run_id"] == "restarted" and done["workorders"] == []
    assert done["next_action"]["kind"] == "verify"
    assert done["next_action"]["argv"] == ["fsf", "verify", "--job", str(root)]
    assert json.loads(run_cli("verify", "--job", root))["evaluation_status"] == "conforms"
    (root / "plan.json").unlink()
    fallback = status(root, "completed")
    assert fallback["next_action"]["kind"] == "verify_with_plan"
    assert fallback["next_action"]["argv"] is None
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


def test_status_multiple_pending_workorders_keeps_next_action_argv_none(tmp_path):
    root = tmp_path / "parallel job"
    plan_path = tmp_path / "parallel-plan.json"
    job = _build_review_required_job("parallel")
    shot = job["tasks"][0]
    shot["duration_seconds"] = shot["shot_spec"]["duration_seconds"] = 2.0
    second = copy.deepcopy(shot)
    second["id"] = "S02"
    job["tasks"].append(second)
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False,
                                enable_assembly=True, enable_media_conformance=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    run_cli("execute", plan_path, "--out", root, "--handoff", expected=10)
    report = status(root, "waiting_handoff")
    assert len(report["workorders"]) == 2
    assert all(row["fulfill_argv"][:3] == ["fsf", "workorder", "fulfill"] for row in report["workorders"])
    assert report["next_action"]["kind"] == "fulfill_workorder"
    assert report["next_action"]["argv"] is None


def test_execute_preserves_existing_conflicting_plan_and_status_reports_conflict(tmp_path):
    root = tmp_path / "conflict job"
    root.mkdir()
    foreign_bytes = json.dumps({"schema_version": "foreign_plan"}).encode("utf-8")
    (root / "plan.json").write_bytes(foreign_bytes)

    plan_path = tmp_path / "compiled-plan.json"
    job = _build_review_required_job("conflict")
    job["tasks"][0]["duration_seconds"] = job["tasks"][0]["shot_spec"]["duration_seconds"] = 2.0
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False,
                                enable_assembly=True, enable_media_conformance=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    run_cli("execute", plan_path, "--out", root, "--handoff", expected=10)
    assert (root / "plan.json").read_bytes() == foreign_bytes

    blocked = status(root, "blocked")
    assert {"code": "PLAN_RECORD_CONFLICT"} in blocked["blockers"]
    assert blocked["next_action"]["argv"] is None

    (root / "plan.json").write_text("{", encoding="utf-8")
    corrupt = status(root, "blocked")
    assert {"code": "PLAN_RECORD_CONFLICT"} in corrupt["blockers"]
    assert corrupt["next_action"]["argv"] is None


def test_status_resume_argv_rejects_tampered_invocation_and_respects_whitelist(tmp_path):
    root = tmp_path / "invocation job"
    plan_path = tmp_path / "source-plan.json"
    job = _build_review_required_job("inv")
    job["tasks"][0]["duration_seconds"] = job["tasks"][0]["shot_spec"]["duration_seconds"] = 2.0
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False,
                                enable_assembly=True, enable_media_conformance=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    run_cli("execute", plan_path, "--out", root, "--handoff", expected=10)

    journal_path = root / "fsf_execution_journal.json"
    journal = json.loads(journal_path.read_bytes())
    assert journal["invocation"] == {"handoff": True, "executor_override": None, "plan_ref": "plan.json"}
    assert str(plan_path) not in json.dumps(journal["invocation"])
    assert journal["run_id"] not in json.dumps(journal["invocation"])

    order, = status(root, "waiting_handoff")["workorders"]
    accept(root, json.loads((root / order["path"]).read_bytes()))
    baseline = status(root, "waiting_handoff")
    assert baseline["next_action"]["kind"] == "resume"
    assert baseline["next_action"]["argv"] == [
        "fsf", "execute", str(root / "plan.json"), "--out", str(root), "--handoff",
    ]

    for bad_invocation in (
        {"handoff": "yes", "executor_override": None, "plan_ref": "plan.json"},
        {"handoff": 1, "executor_override": None, "plan_ref": "plan.json"},
        {"handoff": True, "executor_override": "--evil;rm", "plan_ref": "plan.json"},
        {"handoff": True, "executor_override": "bad executor", "plan_ref": "plan.json"},
        {"handoff": True, "executor_override": None, "plan_ref": "plan.json", "extra": "inject"},
        {"handoff": True, "executor_override": None, "plan_ref": "../other.json"},
    ):
        tampered = copy.deepcopy(journal)
        tampered["invocation"] = bad_invocation
        journal_path.write_text(json.dumps(tampered), encoding="utf-8")
        res = status(root, "waiting_handoff")
        assert res["next_action"]["kind"] == "resume"
        assert res["next_action"]["argv"] is None

    valid_exec = copy.deepcopy(journal)
    valid_exec["invocation"] = {"handoff": False, "executor_override": "handoff", "plan_ref": "plan.json"}
    journal_path.write_text(json.dumps(valid_exec), encoding="utf-8")
    res_exec = status(root, "waiting_handoff")
    assert res_exec["next_action"]["argv"] == [
        "fsf", "execute", str(root / "plan.json"), "--out", str(root), "--executor", "handoff",
    ]

    (root / "plan.json").unlink()
    assert status(root, "waiting_handoff")["next_action"]["argv"] is None
