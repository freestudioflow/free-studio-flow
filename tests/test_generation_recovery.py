"""Real media retries and durable bounded asynchronous generation."""
import copy
import json
from pathlib import Path
import sys
import subprocess
import os
import time

import pytest

from fsf.executors.process_cmd import ProcessCommandExecutor
from fsf.executors.registry import ExecutorRegistry
from fsf.remote.contracts import RemoteError,write_json
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor
from fsf.runtime.recovery import validate_policy
from fsf.conformance.media import build_expected_contract
from test_plan_executor import _require_ffmpeg


def policy(**kwargs):
    value=dict(schema_version="fsf_generation_recovery_policy_v1",authorized=True,
        max_attempts=2,max_dispatches=8,max_new_dispatches_per_run=8,deadline_seconds=300)
    value.update(kwargs)
    return value


def plan():
    return build_execution_plan({"schema_version":"fsf_job_manifest_v1","job_id":"recovery_test",
        "tasks":[dict(id="S0"+str(i),unit_kind="shot",duration_seconds=1,
            expected_media_contract=build_expected_contract({"duration_seconds":1},audio="allowed"),shot_spec=dict(
            generation_prompt="A moving test signal.",source_prompt="A moving test signal.",
            duration_seconds=1,aspect_ratio="16:9")) for i in (1,2,3)]},
        shape=False,enable_assembly=True)


def generator(tmp_path,*,always_bad=False):
    ffmpeg=_require_ffmpeg()
    counter=tmp_path/"calls.json"
    script=tmp_path/"generator.py"
    script.write_text("import json,subprocess,sys\nfrom pathlib import Path\n"
        f"counter=Path({str(counter)!r})\n"
        "out,shot=sys.argv[1:3]\nc=json.loads(counter.read_text()) if counter.exists() else {}\n"
        "c[shot]=c.get(shot,0)+1\ncounter.write_text(json.dumps(c))\n"
        f"bad=shot=='S02' and ({always_bad!r} or c[shot]==1)\n"
        "signal='color=c=gray:size=1024x576:rate=24' if bad else 'testsrc2=size=1024x576:rate=24'\n"
        f"subprocess.run([{ffmpeg!r},'-nostdin','-y','-f','lavfi','-i',signal,'-t','1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-an',out],check=True)\n")
    registry=ExecutorRegistry(load_extras=False)
    registry.register(ProcessCommandExecutor(executor_id="local.recovery",tool_name="studio.video.generate",
        command_args=[sys.executable,str(script),"{output}","{shot_id}"],expected_suffix=".mp4"))
    return registry,counter


def execute(tmp_path,registry,**kwargs):
    return PlanExecutor(tmp_path/"out",registry=registry,executor_override="local.recovery",
        recovery_policy=policy(**kwargs)).execute_plan(plan())


def test_three_shots_only_failed_producer_regenerates_and_resume_reuses(tmp_path):
    registry,counter=generator(tmp_path)
    result=execute(tmp_path,registry)
    assert result["success"],result
    assert json.loads(counter.read_text())=={"S01":1,"S02":2,"S03":1}
    assert result["recovery"]["video_dispatches"]==4
    assert result["recovery"]["success"] is True
    assert list((tmp_path/"out/attempts").rglob("record.json"))
    again=execute(tmp_path,registry)
    assert again["success"] and again["completed_count"]==0
    assert json.loads(counter.read_text())=={"S01":1,"S02":2,"S03":1}


def test_exhaustion_blocks_delivery_without_third_attempt(tmp_path):
    registry,counter=generator(tmp_path,always_bad=True)
    result=execute(tmp_path,registry)
    assert result["status"]=="exhausted" and not result["success"]
    assert not (tmp_path/"out/assembled_timeline.mp4").exists()
    assert json.loads(counter.read_text())["S02"]==2
    assert execute(tmp_path,registry)["status"]=="exhausted"
    assert json.loads(counter.read_text())["S02"]==2


def test_batch_pause_survives_new_executor_without_repeating_good_shot(tmp_path):
    registry,counter=generator(tmp_path)
    first=execute(tmp_path,registry,max_new_dispatches_per_run=1)
    assert first["status"]=="paused_batch"
    assert json.loads(counter.read_text())=={"S01":1}
    for _ in range(4):
        result=execute(tmp_path,registry,max_new_dispatches_per_run=1)
        if result.get("success"):break
    assert result["success"],result
    assert json.loads(counter.read_text())=={"S01":1,"S02":2,"S03":1}


def test_pending_handoff_resumes_same_attempt_without_new_dispatch(tmp_path):
    p=plan()
    first=PlanExecutor(tmp_path,handoff=True,recovery_policy=policy()).execute_plan(p)
    assert first["status"]=="waiting_external"
    before=list((tmp_path/"handoff/workorders").glob("*"))
    second=PlanExecutor(tmp_path,handoff=True,recovery_policy=policy()).execute_plan(p)
    assert second["status"]=="waiting_external"
    assert second["recovery"]["video_dispatches"]==3
    assert list((tmp_path/"handoff/workorders").glob("*"))==before


@pytest.mark.parametrize("mutation", [{"max_attempts":True},{"max_dispatches":0},{"deadline_seconds":-1},{"authorized":False}])
def test_bad_policy_fails_before_generation(mutation):
    with pytest.raises(RemoteError):validate_policy(policy(**mutation))


def test_recovery_requires_freeze_gate_and_unique_producer(tmp_path):
    p=plan()
    qc=next(s for s in p["steps"] if s.get("tool")=="pdx.media.technical_conform")
    qc["inputs"]["expected_media_contract"].pop("max_freeze_duration_seconds")
    with pytest.raises(RemoteError,match="FREEZE_GATE_REQUIRED"):
        PlanExecutor(tmp_path,recovery_policy=policy()).execute_plan(p)


def test_auth_failure_is_paused_across_sessions_and_never_becomes_image(tmp_path):
    registry,_=generator(tmp_path)
    backend=registry.get("local.recovery")
    calls=[]
    def fail(task,out):
        calls.append(task["id"])
        raise RemoteError("API_AUTH_FAILED")
    backend.execute_shot=fail
    result=execute(tmp_path,registry)
    assert result["status"]=="paused_auth"
    assert len(calls)==1
    before=list(calls)
    again=execute(tmp_path,registry)
    assert again["status"]=="paused_auth" and calls==before
    assert not list((tmp_path/"out").glob("*.mp4"))


def test_uncertain_side_effect_cannot_be_replayed_even_with_resume(tmp_path):
    from fsf.runtime.recovery import Recovery
    registry,_=generator(tmp_path)
    ex=PlanExecutor(tmp_path/"out",registry=registry,executor_override="local.recovery",recovery_policy=policy())
    recovery=Recovery(ex,plan())
    step=next(s for s in plan()["steps"] if s.get("tool")=="studio.video.generate")
    recovery.before(step)  # simulates death after durable intent, before completion
    before=recovery.state["dispatches"]
    ex2=PlanExecutor(tmp_path/"out",registry=registry,executor_override="local.recovery",recovery_policy=policy(),resume_paused=True)
    result=ex2.execute_plan(plan())
    assert result["status"]=="paused_unknown"
    assert result["recovery"]["video_dispatches"]==before
    assert not (tmp_path/"calls.json").exists()


def test_policy_change_cannot_reset_budget(tmp_path):
    registry,_=generator(tmp_path)
    execute(tmp_path,registry,max_new_dispatches_per_run=1)
    with pytest.raises(RemoteError,match="IDENTITY_CHANGED"):
        execute(tmp_path,registry,max_dispatches=100)


def test_pure_cli_checkpoint_recovers_after_process_exit(tmp_path):
    registry,counter=generator(tmp_path)
    script=tmp_path/"generator.py"
    configs=tmp_path/"executors.json"
    write_json(configs,{"executors":[dict(executor_id="local.recovery",transport="local",tool_name="studio.video.generate",
        kind="process",command_args=[sys.executable,str(script),"{output}","{shot_id}"],expected_suffix=".mp4")]})
    p=tmp_path/"plan.json";write_json(p,plan())
    pol=tmp_path/"policy.json";write_json(pol,policy())
    out=tmp_path/"out"
    env=os.environ.copy();env["FSF_EXECUTORS_FILE"]=str(configs)
    # A private harness fault, never a product CLI switch.
    fault=tmp_path/"fault.py"
    fault.write_text("import os,sys\nfrom fsf.runtime.recovery import Recovery\nfrom fsf.cli.main import main\n"
        "original=Recovery.checkpoint\n"
        "def stop(self,results):\n original(self,results)\n os._exit(23)\n"
        "Recovery.checkpoint=stop\nmain(sys.argv[1:])\n")
    args=["execute",str(p),"--out",str(out),"--executor","local.recovery","--recovery-policy",str(pol)]
    first=subprocess.run([sys.executable,str(fault),*args],env=env,capture_output=True,text=True,timeout=60)
    assert first.returncode==23,first.stderr+first.stdout
    assert json.loads(counter.read_text())=={"S01":1}
    second=subprocess.run([sys.executable,"-m","fsf.cli",*args],env=env,capture_output=True,text=True,timeout=90)
    assert second.returncode==0,second.stderr+second.stdout
    assert json.loads(counter.read_text())=={"S01":1,"S02":2,"S03":1}


def test_two_cli_processes_share_one_recovery_intent(tmp_path):
    registry,counter=generator(tmp_path)
    conf=tmp_path/"executors.json"
    write_json(conf,{"executors":[dict(executor_id="local.recovery",transport="local",tool_name="studio.video.generate",
        kind="process",command_args=[sys.executable,str(tmp_path/"generator.py"),"{output}","{shot_id}"],expected_suffix=".mp4")]})
    p=tmp_path/"plan.json";write_json(p,plan())
    pol=tmp_path/"policy.json";write_json(pol,policy())
    gate=tmp_path/"go"
    launcher=tmp_path/"launch.py"
    launcher.write_text("import pathlib,sys,time\nfrom fsf.cli.main import main\n"
        "ready,gate=sys.argv[1:3]\npathlib.Path(ready).touch()\nend=time.monotonic()+15\n"
        "while not pathlib.Path(gate).exists():\n assert time.monotonic()<end\n time.sleep(.01)\n"
        "raise SystemExit(main(sys.argv[3:]))\n")
    env=os.environ.copy();env["FSF_EXECUTORS_FILE"]=str(conf)
    args=["execute",str(p),"--out",str(tmp_path/"out"),"--executor","local.recovery","--recovery-policy",str(pol)]
    procs=[subprocess.Popen([sys.executable,str(launcher),str(tmp_path/("ready"+str(i))),str(gate),*args],
        env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in (1,2)]
    try:
        end=time.monotonic()+15
        while not all((tmp_path/("ready"+str(i))).exists() for i in (1,2)):
            assert time.monotonic()<end
            time.sleep(.01)
        gate.touch()
        for proc in procs:
            stdout,stderr=proc.communicate(timeout=90)
            assert proc.returncode==0,stdout+stderr
        assert json.loads(counter.read_text())=={"S01":1,"S02":2,"S03":1}
    finally:
        for proc in procs:
            if proc.poll() is None:proc.kill();proc.wait(timeout=10)
