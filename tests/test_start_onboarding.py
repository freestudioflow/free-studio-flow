"""START07-10 onboarding regressions: contract-in-workorder, fulfill, sheet, flows, diagnostics."""
import json
from pathlib import Path

from fsf.cli.main import main
from fsf.intake.compiler import compile_job
from fsf.runtime.handoff import list_pending_workorders
from test_handoff import _write_test_mp4


def test_workorder_embeds_contract_fulfill_and_diagnostics(tmp_path, capsys):
    sheet = tmp_path / "shot_list.docx"
    assert main(["sheet", "new", "--out", str(sheet), "--handoff"]) == 0
    capsys.readouterr()

    plan_path = tmp_path / "plan.json"
    assert main([
        "plan", str(sheet), "--pipeline", "generate", "--media", "--assembly", "--out", str(plan_path)
    ]) == 0
    capsys.readouterr()

    out = tmp_path / "job"
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 10
    capsys.readouterr()

    wo_t01, = list_pending_workorders(out)
    contract = wo_t01.get("expected_media_contract")
    assert isinstance(contract, dict)
    assert (contract["width"], contract["height"], contract["fps"]) == (1024, 576, 24)
    assert contract["duration_seconds"] == 2.0
    assert wo_t01.get("expected_contract_digest") is not None

    # Bad media duration rejected before writing a result file
    t01_target = out / wo_t01["target_artifact"]
    _write_test_mp4(t01_target, duration_sec=1.0, width=1024, height=576)
    assert main([
        "workorder", "fulfill", str(out), wo_t01["workorder_id"], "--artifact", wo_t01["target_artifact"]
    ]) == 1
    assert "MEDIA_DURATION_MISMATCH" in capsys.readouterr().err
    assert not list((out / "handoff" / "results").glob("*.json"))

    # Valid media succeeds and computes SHA; duplicate fulfill is rejected
    _write_test_mp4(t01_target, duration_sec=2.0, width=1024, height=576)
    assert main([
        "workorder", "fulfill", str(out), wo_t01["workorder_id"], "--artifact", wo_t01["target_artifact"]
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert main([
        "workorder", "fulfill", str(out), wo_t01["workorder_id"], "--artifact", wo_t01["target_artifact"]
    ]) == 1
    assert "already submitted" in capsys.readouterr().err

    # Stale/wrong attempt ID rejected
    assert main([
        "workorder", "fulfill", str(out), wo_t01["workorder_id"] + "_bad", "--artifact", wo_t01["target_artifact"]
    ]) == 1
    capsys.readouterr()

    # Ingest T01 and emit T02
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 10
    capsys.readouterr()
    assert main(["review", "approve", str(out), wo_t01["step_id"]]) == 0
    capsys.readouterr()

    # workorder list separates active vs historical
    assert main(["workorder", "list", str(out)]) == 0
    listed_text = capsys.readouterr().out
    assert "Active Handoff Workorders" in listed_text
    assert "Historical / Superseded Workorders" in listed_text

    assert main(["workorder", "list", str(out), "--active-only", "--json"]) == 0
    active_wos = json.loads(capsys.readouterr().out)
    assert len(active_wos) == 1
    wo_t02 = active_wos[0]
    assert wo_t02["step_id"] != wo_t01["step_id"]

    # Continuity workorder MUST NOT auto-claim continuity_method
    t02_target = out / wo_t02["target_artifact"]
    _write_test_mp4(t02_target, duration_sec=2.0, width=1024, height=576)
    assert main([
        "workorder", "fulfill", str(out), wo_t02["workorder_id"], "--artifact", wo_t02["target_artifact"]
    ]) == 1
    assert "continuity_method" in capsys.readouterr().err

    # Explicit continuity_method succeeds
    assert main([
        "workorder", "fulfill", str(out), wo_t02["workorder_id"],
        "--artifact", wo_t02["target_artifact"], "--continuity-method", "first_frame_init",
    ]) == 0
    capsys.readouterr()

    # Execute ingests T02 and pauses cleanly at review gate (not "Failed: 0 failed")
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 2
    captured = capsys.readouterr()
    assert "Waiting for consistency review: 0 failed" in captured.err
    assert "[FSF Execute] Failed: 0 failed" not in captured.err

    # Reject review -> status reports REVIEW_NOT_APPROVED and verdict needs_rework
    assert main(["review", "reject", str(out), wo_t02["step_id"], "--needs-rework", "--notes", "fix seam"]) == 0
    capsys.readouterr()
    assert main(["status", str(out), "--json"]) == 0
    rejected_status = json.loads(capsys.readouterr().out)
    assert rejected_status["state"] == "waiting_review"
    assert rejected_status["next_action"]["kind"] == "inspect_rejected_review"
    assert any(b.get("code") == "REVIEW_NOT_APPROVED" and b.get("verdict") == "needs_rework"
               for b in rejected_status["blockers"])

    # Approve T02 -> assemble -> status verify_with_plan keeps argv=None -> verify --job conforms
    assert main(["review", "approve", str(out), wo_t02["step_id"]]) == 0
    capsys.readouterr()
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 0
    capsys.readouterr()
    assert main(["status", str(out), "--json"]) == 0
    done_status = json.loads(capsys.readouterr().out)
    assert done_status["next_action"]["kind"] == "verify_with_plan"
    assert done_status["next_action"]["argv"] is None
    assert main(["verify", "--job", str(out), "--plan", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["evaluation_status"] == "conforms"


def test_workorder_fulfill_failed_capability_gap_advances_attempt(tmp_path, capsys):
    sheet = tmp_path / "sheet.docx"
    assert main(["sheet", "new", "--out", str(sheet), "--shots", "1", "--handoff"]) == 0
    capsys.readouterr()
    plan_path = tmp_path / "plan.json"
    assert main(["plan", str(sheet), "--pipeline", "generate", "--media", "--out", str(plan_path)]) == 0
    capsys.readouterr()
    out = tmp_path / "job"
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 10
    capsys.readouterr()

    wo1, = list_pending_workorders(out)
    assert main([
        "workorder", "fulfill", str(out), wo1["workorder_id"],
        "--status", "failed", "--capability-gap", "missing local i2v model",
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    res_json = json.loads((out / receipt["result_path"]).read_text(encoding="utf-8"))
    assert res_json["consistency_acknowledgement"]["capability_gap"] == "missing local i2v model"

    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 2
    capsys.readouterr()
    assert main(["execute", str(plan_path), "--out", str(out), "--handoff"]) == 10
    capsys.readouterr()
    wo2 = [w for w in list_pending_workorders(out) if w["attempt_id"] == 2][0]
    assert wo2["workorder_id"] != wo1["workorder_id"]


def test_sheet_new_and_flows_honest_shape_wording(tmp_path, capsys):
    sheet = tmp_path / "starter.docx"
    assert main(["sheet", "new", "--out", str(sheet), "--shots", "3"]) == 0
    capsys.readouterr()
    manifest, _, _ = compile_job(sheet)
    assert manifest["shot_count"] == 3
    assert main(["sheet", "new", "--out", str(sheet)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["sheet", "new", "--out", str(sheet), "--shots", "2", "--force"]) == 0
    capsys.readouterr()

    assert main(["flows"]) == 0
    text_out = capsys.readouterr().out
    assert "[implemented]" in text_out
    assert "shape-only" in text_out
    assert "shape stubs" in text_out

    assert main(["flows", "--json"]) == 0
    v2 = json.loads(capsys.readouterr().out)
    assert v2["schema_version"] == "fsf_flows_v2"
    assert v2["live_backend_wired"] is False
