"""Receipt publication and active workorder validation through local entry points."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier

from fsf.cli.main import main
from fsf.runtime import handoff


def _pending(tmp_path, capsys):
    sheet = tmp_path / "sheet.docx"
    plan = tmp_path / "plan.json"
    job = tmp_path / "job"
    assert main(["sheet", "new", "--out", str(sheet), "--handoff"]) == 0
    assert main(["plan", str(sheet), "--pipeline", "generate", "--media",
                 "--assembly", "--out", str(plan)]) == 0
    assert main(["execute", str(plan), "--out", str(job), "--handoff"]) == 10
    capsys.readouterr()
    order, = handoff.list_pending_workorders(job)
    return job, order["workorder_id"]


def _failed(job, identifier):
    return handoff.fulfill_workorder(job, identifier, status="failed", capability_gap="synthetic fixture")


def test_interrupted_fulfill_leaves_no_visible_receipt_and_can_retry(tmp_path, capsys):
    job, identifier = _pending(tmp_path, capsys)
    directory = job / "handoff/results"
    directory.mkdir(exist_ok=True)
    legacy_stage = directory / f".stage_{identifier}.json"
    legacy_stage.write_bytes(b"preserve this file")
    crash = """import os, sys
from fsf.runtime import handoff
def stop(*args):
    os._exit(42)
handoff._publish_workorder_result = stop
handoff.fulfill_workorder(sys.argv[1], sys.argv[2], status='failed', capability_gap='fixture')
"""
    env = {**os.environ, "PYTHONPATH": str(Path(handoff.__file__).resolve().parents[2])}
    result = subprocess.run([sys.executable, "-c", crash, str(job), identifier], env=env, timeout=60)
    assert result.returncode == 42
    canonical = directory / f"{identifier}.result.json"
    assert not canonical.exists()
    abandoned, = directory.glob(".fulfill-*.tmp")
    orphan_bytes = abandoned.read_bytes()
    assert json.loads(orphan_bytes)["workorder_id"] == identifier
    assert _failed(job, identifier)["result_path"] == f"handoff/results/{canonical.name}"
    published = canonical.read_bytes()
    assert json.loads(published)["workorder_id"] == identifier
    assert abandoned.read_bytes() == orphan_bytes
    assert legacy_stage.read_bytes() == b"preserve this file"
    assert main(["workorder", "fulfill", str(job), identifier, "--status", "failed",
                 "--detail", "different outcome"]) == 1
    assert "already submitted" in capsys.readouterr().err
    assert canonical.read_bytes() == published


def test_concurrent_fulfill_publishes_one_complete_receipt(tmp_path, capsys, monkeypatch):
    job, identifier = _pending(tmp_path, capsys)
    directory = job / "handoff/results"
    directory.mkdir(exist_ok=True)
    barrier = Barrier(2)
    publish = handoff._publish_workorder_result

    def race(source, target):
        assert not target.exists()
        assert json.loads(source.read_bytes())["workorder_id"] == identifier
        barrier.wait(timeout=15)
        publish(source, target)

    monkeypatch.setattr(handoff, "_publish_workorder_result", race)

    def submit(detail):
        try:
            handoff.fulfill_workorder(job, identifier, status="failed", detail=detail)
        except handoff.HandoffIntegrityError as error:
            assert "already submitted" in str(error)
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(submit, ("first", "second")))
    assert sum(outcomes) == 1
    winner = ("first", "second")[outcomes.index(True)]
    canonical, = directory.glob("*.result.json")
    assert json.loads(canonical.read_bytes())["detail"] == winner
    assert not list(directory.glob(".fulfill-*.tmp"))


def test_active_list_and_status_use_same_canonical_workorder(tmp_path, capsys):
    job, identifier = _pending(tmp_path, capsys)
    order_path = job / "handoff/workorders" / f"{identifier}.workorder.json"
    original = json.loads(order_path.read_bytes())
    fake = {**original, "target_artifact": "wrong-target.mp4"}
    duplicate = order_path.with_name("duplicate.workorder.json")
    duplicate.write_text(json.dumps(fake), encoding="utf-8")
    active = ["workorder", "list", str(job), "--active-only", "--json"]
    status = ["status", str(job), "--json"]
    assert main(active) == 0
    assert json.loads(capsys.readouterr().out) == [original]
    assert main(status) == 0
    assert json.loads(capsys.readouterr().out)["workorders"][0]["target_artifact"] == original["target_artifact"]
    order_path.write_text(json.dumps(fake), encoding="utf-8")
    for args in (active, [*active, "--summary"]):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out) == []
    assert main(status) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "blocked" and report["workorders"] == []
    assert report["blockers"] == [{"step_id": original["step_id"], "code": "WORKORDER_UNVERIFIED"}]
