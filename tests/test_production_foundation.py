"""Production contract and real-process concurrency tests, without inference."""
import copy
from fractions import Fraction
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from fsf.core.edit_store import read_json
from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.ledger import Ledger
from fsf.production.segments import compile_segments, affected_tasks, validate_segments
from fsf.production.workflow import Workflow


def source(frames=4320):
    return seal({"schema_version":"fsf_control_source_v2","control_asset_id":"control_A",
        "origin":"operator_control_video_import","sha256":"a"*64,"size_bytes":1024,
        "width":64,"height":64,"clock":{"num":24,"den":1},"frame_count":frames,
        "timestamps_digest":"b"*64,"decoded_frames_digest":"c"*64,
        "clay_profile":"unverified","motion_review":"not_reviewed"},"source_digest")


def capability(limit=81, step=4, overlap=8, continuation="independent"):
    return seal({"schema_version":"fsf_model_capability_v1","model_id":"fixture_model",
        "model_revision":"a"*64,"runner_digest":"b"*64,"environment_digest":"c"*64,
        "qualification":"verified","qualification_evidence_digest":"d"*64,
        "modalities":["control_video"],"clocks":[{"num":24,"den":1}],"dimensions":[[64,64]],
        "max_frames":limit,"frame_step":step,"overlap_frames":overlap,"continuation":continuation,
        "tail_padding":"repeat_last_explicit"},"capability_digest")


def auth(total=1000, cleanup=100):
    return seal({"schema_version":"fsf_production_budget_v1","production_id":"film_A",
        "currency":"USD","unit_scale":100,"authorized":True,"total_units":total,
        "cleanup_units":cleanup,"fixed_delivery_units":80,"route_limits":{"local":total},
        "max_generated_frames":10000,"logical_shot_ids":["shot_A"]},"authorization_digest")


@pytest.mark.parametrize("count",[1,96,97,4320,172800])
@pytest.mark.parametrize("limit",[81,121])
def test_complete_control_frame_coverage_and_tail(count,limit):
    plan = compile_segments(source(count),capability(limit),shot_id="shot_A",policy_digest="e"*64)
    assert plan["segments"][0]["core"][0] == 0
    assert plan["segments"][-1]["core"][1] == count
    assert sum(b-a for a,b in (s["core"] for s in plan["segments"])) == count
    assert all(s["model_frames"] <= limit and (s["model_frames"]-1)%4 == 0 for s in plan["segments"])
    assert validate_segments(plan) == plan


def test_dependent_segments_and_different_capability():
    a = compile_segments(source(),capability(81,continuation="previous_frame"),shot_id="shot_A",policy_digest="e"*64)
    b = compile_segments(source(),capability(121),shot_id="shot_A",policy_digest="e"*64)
    assert len(a["segments"]) > len(b["segments"])
    assert a["segments"][1]["depends_on"] == [a["segments"][0]["segment_id"]]
    assert not b["segments"][1]["depends_on"]


@pytest.mark.parametrize("mutation",[
    lambda c:c.update(qualification="unverified"),
    lambda c:c.update(modalities=["text"]),
    lambda c:c.update(dimensions=[[128,128]]),
    lambda c:c.update(overlap_frames=81),
    lambda c:c.update(tail_padding="refuse"),
    lambda c:c.update(frame_step=True),
    lambda c:c.update(extra=True),
])
def test_unsupported_or_unknown_capability_refused(mutation):
    cap = capability(); mutation(cap);cap = seal(cap,"capability_digest")
    with pytest.raises(ProductionError):
        compile_segments(source(99),cap,shot_id="shot_A",policy_digest="e"*64)


def test_malformed_segment_mapping_and_digest():
    plan = compile_segments(source(200),capability(),shot_id="shot_A",policy_digest="e"*64)
    for reseal in (False,True):
        bad = copy.deepcopy(plan);bad["segments"][1]["core"][0] += 1
        if reseal:bad = seal(bad,"plan_digest")
        with pytest.raises(ProductionError):validate_segments(bad)


def test_full_dependency_closure_includes_prompt():
    graph={"shot":[],"latent":["shot"],"prompt":["shot"],"assemble":["latent","prompt"],"unrelated":[]}
    assert affected_tasks(graph,["shot"]) == ["assemble","latent","prompt","shot"]
    for invalid in ({"a":["missing"]},{"a":["b"],"b":["a"]}):
        with pytest.raises(ProductionError):affected_tasks(invalid,[])


def test_budget_unknown_reconciliation_and_idempotency(tmp_path):
    ledger=Ledger(tmp_path/"budget.json");ledger.create(auth())
    entry=ledger.reserve("first",route="local",worst_units=800,generated_frames=100,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":"first","repair_round":1})
    assert ledger.reserve("first",route="local",worst_units=800,generated_frames=100,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":"first","repair_round":1}) == entry
    assert ledger.inspect()["consumed_frames"] == 100
    ledger.reconcile("first")
    with pytest.raises(ProductionError,match="EXCEEDED"):
        ledger.reserve("second",route="local",worst_units=101,generated_frames=100,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":"second","repair_round":1})
    ledger.reconcile("first",settled_units=200)
    ledger.reserve("second",route="local",worst_units=700,generated_frames=100,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":"second","repair_round":1})
    with pytest.raises(ProductionError):ledger.reconcile("first",settled_units=199)


@pytest.mark.parametrize("total,cleanup",[(1000,99),(1001,100),(1000,0)])
def test_cleanup_floor_refused(tmp_path,total,cleanup):
    with pytest.raises(ProductionError):Ledger(tmp_path/"budget.json").create(auth(total,cleanup))
    assert not (tmp_path/"budget.json").exists()


@pytest.mark.parametrize("amount",[True,1.0,-1])
def test_money_rejects_bool_float_negative(tmp_path,amount):
    ledger=Ledger(tmp_path/"budget.json");ledger.create(auth())
    with pytest.raises(ProductionError):ledger.reserve("r",route="local",worst_units=amount,generated_frames=1,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":"first","repair_round":1})


def _compete(path,barrier,queue,name):
    try:
        barrier.wait(timeout=15)
        Ledger(path).reserve(name,route="local",worst_units=600,generated_frames=1,binding_digest="a"*64,generation_slot={"logical_shot_id":"shot_A","segment_id":name,"repair_round":1})
        queue.put("accepted")
    except ProductionError as exc:queue.put(exc.code)


def test_two_independent_processes_cannot_overspend(tmp_path):
    path=tmp_path/"budget.json";Ledger(path).create(auth())
    ctx=mp.get_context("spawn");barrier=ctx.Barrier(3);queue=ctx.Queue()
    processes=[ctx.Process(target=_compete,args=(path,barrier,queue,"r"+str(i))) for i in range(2)]
    try:
        for process in processes:process.start()
        barrier.wait(timeout=15)
        results=sorted(queue.get(timeout=25) for _ in processes)
        assert results == ["PRODUCTION_BUDGET_EXCEEDED","accepted"]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():process.terminate();process.join(timeout=5)
    assert all(p.exitcode == 0 for p in processes)
    assert len(Ledger(path).inspect()["reservations"]) == 1


def test_generation_slots_cannot_be_renamed_or_exceed_three_rounds(tmp_path):
    ledger=Ledger(tmp_path/'budget.json');ledger.create(auth())
    slot={'logical_shot_id':'shot_A','segment_id':'segment_A','repair_round':1}
    ledger.reserve('first',route='local',worst_units=1,generated_frames=24,binding_digest='a'*64,generation_slot=slot)
    with pytest.raises(ProductionError,match='SLOT_USED'):
        ledger.reserve('renamed',route='local',worst_units=1,generated_frames=24,binding_digest='a'*64,generation_slot=slot)
    slot['repair_round']=4
    with pytest.raises(ProductionError):
        ledger.reserve('fourth',route='local',worst_units=1,generated_frames=24,binding_digest='a'*64,generation_slot=slot)
    slot.update(repair_round=1,logical_shot_id='unapproved_shot')
    with pytest.raises(ProductionError,match='NOT_AUTHORIZED'):
        ledger.reserve('unknown_shot',route='local',worst_units=1,generated_frames=24,binding_digest='a'*64,generation_slot=slot)


def test_workflow_restart_pause_and_unknown_lease(tmp_path):
    path=tmp_path/"state.json";flow=Workflow(path)
    flow.create(production_id="film",binding_digest="a"*64,dependencies={"shot":[],"post":["shot"]})
    flow.pause()
    with pytest.raises(ProductionError,match="NOT_READY"):flow.claim("shot",owner="worker",now=0)
    flow.pause(False)
    first=flow.claim("shot",owner="worker",now=0)
    reloaded=Workflow(path)
    with pytest.raises(ProductionError,match="RECONCILE_REQUIRED"):reloaded.claim("shot",owner="worker",now=301)
    assert reloaded.inspect()["tasks"]["shot"]["attempts"][0] == first
    reloaded.record("shot",epoch=first["epoch"],request_id=first["request_id"],status="unknown")
    reloaded.record("shot",epoch=first["epoch"],request_id=first["request_id"],status="failed")
    second=reloaded.claim("shot",owner="worker",now=400)
    with pytest.raises(ProductionError,match="FENCED"):
        reloaded.record("shot",epoch=first["epoch"],request_id=first["request_id"],status="submitted")
    assert second["epoch"] > first["epoch"]


def test_workflow_missing_evidence_cannot_accept(tmp_path):
    flow=Workflow(tmp_path/"state.json");flow.create(production_id="film",binding_digest="a"*64,dependencies={"shot":[]})
    claim=flow.claim("shot",owner="worker")
    args={"epoch":claim["epoch"],"request_id":claim["request_id"]}
    flow.record("shot",status="submitted",**args);flow.record("shot",status="produced",**args)
    with pytest.raises(ProductionError,match="EVIDENCE_REQUIRED"):flow.record("shot",status="verified",**args)
    assert flow.inspect()["tasks"]["shot"]["accepted"] is None
