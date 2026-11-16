"""START01/02 Handoff continuity input and review resume regression."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from fsf.cli.main import main
from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.runtime.consistency_review import build_review_record, check_assembly_gate, load_reviews
from fsf.runtime.handoff import list_pending_workorders, resolve_workorder_input, HandoffSecurityError
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor
from test_d_consistency_review_integration import _build_review_required_job
from test_handoff import _write_test_mp4


def accept(root, order):
    target = root / order["target_artifact"]
    _write_test_mp4(target, duration_sec=2.0, width=1024, height=576)
    keys = ("job_id","run_id","workorder_id","step_id","attempt_id","plan_digest",
            "inputs_digest","expected_contract_digest")
    result = {key: order.get(key) for key in keys}
    result.update(schema_version="fsf_workorder_result_v1",status="completed",
                  artifact_path=order["target_artifact"],artifact_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    if order.get("requirements_digest"):
        result["consistency_acknowledgement"] = {
            "acknowledged_digest":order["requirements_digest"],"continuity_method":"first_frame_init"}
    folder = root / "handoff/results"
    folder.mkdir(exist_ok=True)
    (folder / (order["workorder_id"] + ".json")).write_text(json.dumps(result),encoding="utf-8")


@pytest.mark.parametrize("linked_parent", [False, True])
def test_open_portable_handoff_input_and_resume_review(tmp_path, capsys, linked_parent):
    original = tmp_path / "home" / "fixture-user" / "film folder 片"
    original.mkdir(parents=True)
    job = _build_review_required_job("start01")
    shot = job["tasks"][0]
    shot["duration_seconds"] = 2.0
    shot["shot_spec"]["duration_seconds"] = 2.0
    second = copy.deepcopy(shot)
    second["id"] = "S02"
    second["consistency_context"]["continues_from"] = {
        "shot_id":"S01","selection_rule":"latest_completed","aspects":["appearance"]}
    job["tasks"].append(second)
    plan = build_execution_plan(job,executor_tool="studio.video.generate",shape=False,
                                enable_assembly=True,enable_media_conformance=True)

    def execute(folder, run_id="fixture"):
        engine = PlanExecutor(folder,job_id="start01",run_id=run_id,handoff=True)
        outcome = engine.execute_plan(plan)
        return engine, outcome

    execute(original)
    first, = list_pending_workorders(original)
    first_path = original / "handoff/workorders" / (first["workorder_id"]+".workorder.json")
    first_bytes = first_path.read_bytes()
    execute(original)
    assert first_path.read_bytes() == first_bytes
    accept(original, first)
    execute(original)
    assert main(["review","approve",str(original),first["step_id"]]) == 0
    second, = [w for w in list_pending_workorders(original) if w["step_id"] != first["step_id"]]
    relative = second["task_payload"]["input_artifact"]
    assert relative == first["target_artifact"]
    assert second["task_payload"]["input_artifact_sha256"] == second["consistency_requirements"]["resolved_sha256"]

    moved = tmp_path / "moved 影片"
    shutil.move(original,moved)
    if linked_parent:
        alias = tmp_path / "linked parent"
        directory_link(tmp_path / "real parent", alias)
        relocated = tmp_path / "real parent" / moved.name
        shutil.move(moved, relocated)
        moved = alias / relocated.name
    capsys.readouterr()
    args = ["workorder","input",str(moved),second["workorder_id"]]
    assert main(args) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["path"] == relative and (moved / info["path"]).is_file()
    second_path = moved / "handoff/workorders" / (second["workorder_id"]+".workorder.json")
    original_order = second_path.read_bytes()
    resealed = {**second, "created_at":"different"}
    resealed["workorder_digest"] = digest({k:v for k,v in resealed.items() if k!="workorder_digest"})
    second_path.write_text(json.dumps(resealed),encoding="utf-8")
    assert main(args) == 1
    second_path.write_bytes(original_order)
    other = tmp_path/"other"
    shutil.copytree(moved,other)
    journal = other/"fsf_execution_journal.json"
    contents = json.loads(journal.read_text())
    contents["job_id"] = "foreign"
    journal.write_text(json.dumps(contents))
    assert main(["workorder","input",str(other),second["workorder_id"]]) == 1
    (other / relative).write_bytes(b"tampered")
    with pytest.raises(ValueError,match="SHA mismatch"):
        resolve_workorder_input(second,other)
    altered = copy.deepcopy(second)
    altered["task_payload"]["input_artifact"] = "../foreign.mp4"
    altered["workorder_digest"] = digest({k:v for k,v in altered.items() if k!="workorder_digest"})
    with pytest.raises(HandoffSecurityError):
        resolve_workorder_input(altered,moved)
    if linked_parent:
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.copyfile(moved / relative, outside / "source.mp4")
        directory_link(outside, moved / "internal-link")
        altered["task_payload"]["input_artifact"] = "internal-link/source.mp4"
        altered["workorder_digest"] = digest({k:v for k,v in altered.items() if k!="workorder_digest"})
        with pytest.raises(HandoffSecurityError):
            resolve_workorder_input(altered,moved)
    accept(moved,second)
    _, pending = execute(moved)
    assert pending["steps"][second["step_id"]]["status"] == "completed"
    assert main(["review","approve",str(moved),second["step_id"]]) == 0
    ids = {w["step_id"]:(w["workorder_id"],w["attempt_id"]) for w in (first,second)}
    journal = moved/"fsf_execution_journal.json"
    legacy = json.loads(journal.read_text())
    for step in ids:
        legacy["steps"][step].pop("workorder_run_id")
    journal.write_text(json.dumps(legacy))
    for run in ("fixture-next", "fixture-third"):
        engine, outcome = execute(moved, run)
        assert outcome["success"], outcome["steps"]
        assert engine.generation_dispatches["handoff"] == 0
        assert {step:(outcome["steps"][step]["workorder_id"],outcome["steps"][step]["attempt_id"])
                for step in ids} == ids
        assert all(outcome["steps"][step]["workorder_run_id"] == "fixture" for step in ids)
    data = json.loads(journal.read_text())
    reviews = load_reviews(moved)
    assert check_assembly_gate(data,reviews,moved)[0]
    foreign = {**data, "job_id":"foreign"}
    assert not check_assembly_gate(foreign,reviews,moved)[0]
    wrong_attempt = copy.deepcopy(data)
    wrong_attempt["steps"][second["step_id"]]["attempt_id"] += 1
    assert not check_assembly_gate(wrong_attempt,reviews,moved)[0]
    data["steps"][second["step_id"]]["workorder_id"] = ""
    journal.write_text(json.dumps(data))
    assert main(["review","approve",str(moved),second["step_id"]]) == 1
    artifact = data["steps"][second["step_id"]]["output_files"][0]["sha256"]
    empty = build_review_record(step_id=second["step_id"],job_id=data["job_id"],run_id=data["run_id"],
        workorder_id="",artifact_sha256=artifact,
        requirements_digest=data["steps"][second["step_id"]]["requirements_digest"],
        acknowledged_digest=data["steps"][second["step_id"]]["requirements_digest"],
        reviewer="fixture",verdict="approved")
    reviews[second["step_id"]] = empty
    gate, reasons = check_assembly_gate(data,reviews,moved)
    assert not gate and any("identity is missing or invalid" in reason for reason in reasons)


def directory_link(target, link):
    target.mkdir(exist_ok=True)
    try:
        if os.name == "nt":
            from _winapi import CreateJunction
            CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except PermissionError:
        pytest.skip("directory link creation is not permitted in this environment")


@pytest.mark.parametrize("separator, parts", [
    (chr(92), ("C:", "Users", "fixture-user", "source.mp4")),
    ("/", ("", "home", "fixture-user", "source.mp4")),
])
def test_portable_workorder_binding_keeps_prompt_redaction(tmp_path, separator, parts):
    from fsf.runtime.handoff import build_workorder
    path = tmp_path / "source 影片.mp4"
    path.write_bytes(b"fixture")
    order = build_workorder(job_id="job",run_id="run",step_id="step",attempt_id=1,
        tool="fixture",plan_digest="a"*64,inputs_digest="b"*64,target_artifact="target.mp4",
        task_payload={"prompt":"read "+separator.join(parts)},
        input_binding=(path.name,hashlib.sha256(path.read_bytes()).hexdigest()))
    assert "[REDACTED_USER_PATH]" in order["task_payload"]["prompt"]
    assert resolve_workorder_input(order,tmp_path) == path
    link = tmp_path / "link.mp4"
    try:
        link.symlink_to(path)
    except OSError:
        return
    order["task_payload"]["input_artifact"] = link.name
    order["workorder_digest"] = digest({k:v for k,v in order.items() if k!="workorder_digest"})
    with pytest.raises(HandoffSecurityError):
        resolve_workorder_input(order,tmp_path)
