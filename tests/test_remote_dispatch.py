"""Parent/child lifecycle and hostile artifact tests using actual child jobs."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile
from importlib import resources

import pytest
from pdx_artifact_core import validate_execution_plan

from fsf.remote.bundle import prepare_bundle, unpack, read_bundle, pack
from fsf.remote.contracts import RemoteError, digest, seal, validate_recipe, compile_child_plan, write_json
from fsf.remote.dispatch import submit_bundle, update_dispatch, accepted_result, validate_result
from fsf.remote.transports import LocalAdapter
from fsf.remote.worker import run_worker
from fsf.cli.remote import wait_dispatch


def template(name):
    return json.loads(resources.files("fsf.remote.templates").joinpath(name).read_text())


@pytest.fixture(scope="session")
def remote_wheel(tmp_path_factory):
    root=tmp_path_factory.mktemp("rw")
    project=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,"-m","build","--wheel","--no-isolation","--outdir",str(root)],
                          cwd=project,capture_output=True,timeout=90)
    assert result.returncode==0, result.stderr.decode(errors="replace")[-1000:]
    return next(root.glob("*.whl"))


@pytest.fixture
def remote_case(tmp_path_factory,remote_wheel):
    root=tmp_path_factory.mktemp("r")
    for name in ("state","worker","inputs"):(root/name).mkdir()
    recipe=template("cpu.recipe.json")
    recipe["environment"]["python"]=".".join(map(str,sys.version_info[:2]))
    recipe=seal(recipe,"recipe_digest")
    profile=template("local.profile.json")
    profile.update(authorized=True,approved_recipes=[recipe["recipe_digest"]])
    profile["settings"]={"worker_root":str(root/"worker"),"python":sys.executable}
    profile["policy"]["deadline_seconds"]=90
    definition={"parent":{"job_id":"parent_job","run_id":"parent_run","step_id":"producer","attempt_id":1,
        "plan_digest":"a"*64,"inputs_digest":"b"*64,"expected_contract_digest":None,"requirements_digest":None},
        "parameters":{"value":7},"inputs":{},"video_condition":None,"consistency_requirements":None,"expected_media_contract":None}
    return root,recipe,profile,definition,remote_wheel


def prepare(case):
    root,recipe,profile,definition,wheel=case
    value=prepare_bundle(definition,recipe,inputs_root=root/"inputs",wheel=wheel,profile=profile,output_root=root/"state")
    return value,root/"state"/value["bundle"]


def test_actual_child_cli_roundtrip_and_idempotent_collection(remote_case):
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    write_json(root/"profile.json",profile)
    args=[sys.executable,"-m","fsf.cli","remote","dispatch",str(bundle),"--profile",str(root/"profile.json"),"--state",str(root/"state"),"--wait"]
    completed=subprocess.run(args,capture_output=True,text=True,timeout=90)
    assert completed.returncode==0,completed.stdout
    result=json.loads(completed.stdout)["result"]
    assert result["status"]=="verified" and result["submitted_count"]==1
    manifest,folder=accepted_result(prepared["dispatch_id"],profile=profile,state_root=root/"state")
    assert manifest["child"]["job_id"]!=definition["parent"]["job_id"]
    assert json.loads((folder/"outputs/result.json").read_text())["square"]==49
    assert submit_bundle(bundle,profile=profile,state_root=root/"state")["submitted_count"]==1
    assert update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state",action="fetch")["status"]=="verified"


def test_response_loss_reconciles_without_second_spawn(remote_case):
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    class Lost(LocalAdapter):
        def submit(self,*args):
            super().submit(*args)
            raise ConnectionError("synthetic lost response")
    result=submit_bundle(bundle,profile=profile,state_root=root/"state",adapter=Lost(profile))
    assert result["status"]=="submission_unknown"
    result=wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")
    assert result["status"]=="verified" and result["submitted_count"]==1


def _race(bundle,profile,state,barrier,queue):
    barrier.wait(timeout=20)
    try:queue.put(submit_bundle(bundle,profile=profile,state_root=state))
    except Exception as exc:queue.put({"error":type(exc).__name__})


def test_independent_parent_processes_submit_once(remote_case):
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    ctx=multiprocessing.get_context("spawn")
    barrier=ctx.Barrier(3);queue=ctx.Queue()
    workers=[ctx.Process(target=_race,args=(str(bundle),profile,str(root/"state"),barrier,queue)) for _ in range(2)]
    for worker in workers:worker.start()
    barrier.wait(timeout=20)
    values=[queue.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode==0
    assert all(v.get("submitted_count")==1 for v in values)
    assert wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")["status"]=="verified"


def test_http_reference_actual_async_roundtrip(remote_case,monkeypatch):
    from fsf.remote.reference import make_server
    root,recipe,profile,definition,wheel=remote_case
    monkeypatch.setenv("FSF_REMOTE_TEST_TOKEN","synthetic-reference-token")
    profile.update(transport="http",settings={"endpoint":"http://127.0.0.1:1","auth_env":"FSF_REMOTE_TEST_TOKEN"})
    server=make_server(profile,state_root=root/"inputs",worker_root=root/"worker",python=sys.executable)
    profile["settings"]["endpoint"]=f"http://127.0.0.1:{server.server_port}"
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        prepared,bundle=prepare(remote_case)
        submit_bundle(bundle,profile=profile,state_root=root/"state")
        assert wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")["status"]=="verified"
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.parametrize("mutation",["parent","child","artifact","journal","environment","extra"])
def test_result_identity_and_integrity_rejected(remote_case,mutation):
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    run_worker(bundle,output_root=root/"worker",approved_recipe=recipe["recipe_digest"])
    extracted=root/"extracted";extracted.mkdir()
    source=root/"worker"/prepared["request_digest"]/"result.zip"
    names=unpack(source,extracted)
    manifest=json.loads((extracted/"result.json").read_text())
    if mutation=="parent":manifest["parent"]["run_id"]="other"
    elif mutation=="child":manifest["child"]["job_id"]="other"
    elif mutation=="artifact":(extracted/"outputs/result.json").write_text("{}")
    elif mutation=="journal":(extracted/"child-journal.json").write_text("{}")
    elif mutation=="environment":(extracted/"environment.json").write_text("{}")
    else:(extracted/"extra.json").write_text("{}");names.add("extra.json")
    write_json(extracted/"result.json",seal(manifest,"result_digest"))
    malicious=root/"malicious.zip";pack(extracted,names,malicious)
    request=json.loads((root/"worker"/prepared["request_digest"]/"request.json").read_text())
    with pytest.raises((RemoteError,ValueError)):
        validate_result(malicious,request,root)


@pytest.mark.parametrize("name",["../escape","/absolute","a/../../escape","a\\b","C:escape"])
def test_archive_unsafe_paths_rejected(tmp_path,name):
    path=tmp_path/"bad.zip"
    entry=zipfile.ZipInfo("entry")
    entry.filename=name
    with zipfile.ZipFile(path,"w") as archive:archive.writestr(entry,b"data")
    out=tmp_path/"out";out.mkdir()
    with pytest.raises(ValueError):unpack(path,out)


@pytest.mark.parametrize("mutation",["cycle","nested","unpin","secret","duplicate"])
def test_recipe_fail_closed(mutation):
    recipe=template("cpu.recipe.json")
    if mutation=="cycle":recipe["steps"][0]["depends_on"]=["job"]
    elif mutation=="nested":recipe["steps"][0]["tool"]="fsf.remote.dispatch"
    elif mutation=="unpin":recipe["environment"]["setup"]=[{"source":"https://pypi.org/simple","args":["pip","install","latest"]}]
    elif mutation=="secret":recipe["steps"][0]["args"].append("{env.API_KEY}")
    else:recipe["steps"][0]["outputs"].append(copy.deepcopy(recipe["steps"][0]["outputs"][0]))
    with pytest.raises(RemoteError):validate_recipe(seal(recipe,"recipe_digest"))


def test_nested_parent_refused(remote_case,monkeypatch):
    prepared,bundle=prepare(remote_case)
    root,recipe,profile,definition,wheel=remote_case
    monkeypatch.setenv("FSF_REMOTE_DEPTH","1")
    with pytest.raises(RemoteError,match="NESTING"):
        submit_bundle(bundle,profile=profile,state_root=root/"state")


def test_templates_validate_fixed_pdx_plans():
    for item in resources.files("fsf.remote.templates").iterdir():
        if item.name.endswith(".recipe.json"):
            assert not validate_execution_plan(compile_child_plan(json.loads(item.read_text())))


def test_cancel_reclaims_child_job(remote_case):
    root,recipe,profile,definition,wheel=remote_case
    recipe["steps"][0]["args"]=["{python}","-c","import time; time.sleep(60)"]
    recipe=seal(recipe,"recipe_digest");profile["approved_recipes"]=[recipe["recipe_digest"]]
    prepared,bundle=prepare((root,recipe,profile,definition,wheel))
    submit_bundle(bundle,profile=profile,state_root=root/"state")
    end=time.monotonic()+20
    while time.monotonic()<end:
        value=update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")
        if value["status"]=="running":break
        time.sleep(.1)
    update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state",action="cancel")
    while time.monotonic()<end:
        value=update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")
        if value["status"]=="cancelled":break
        time.sleep(.1)
    assert value["status"]=="cancelled"


def video_plan(contract):
    return {"schema_version":"pdx_execution_plan_v1","request_id":"production_video",
        "producer":{"type":"fsf_planner","name":"test"},"steps":[
            {"id":"producer","name":"Generate","kind":"tool","tool":"studio.video.generate","depends_on":[],
             "inputs":{"shot_id":"S01","shot_spec":{"source_prompt":"test pattern","duration_seconds":1}},"outputs":["artifact:clip.mp4"]},
            {"id":"qc","name":"QC","kind":"tool","tool":"pdx.media.technical_conform","depends_on":["producer"],
             "inputs":{"target_artifact":"artifact:clip.mp4","expected_media_contract":contract},"outputs":["report:qc.json"]}]}


def test_remote_plan_executor_qc_and_three_resume_cycles(remote_case):
    from fsf.conformance.media import build_expected_contract,ensure_media_tools_on_path
    from fsf.remote.executor import RemoteExecutor
    from fsf.executors.registry import ExecutorRegistry
    from fsf.runtime.plan_executor import PlanExecutor
    ensure_media_tools_on_path()
    root,recipe,profile,definition,wheel=remote_case
    recipe=template("ffmpeg.recipe.json")
    recipe["environment"]["python"]=".".join(map(str,sys.version_info[:2]))
    recipe=seal(recipe,"recipe_digest")
    profile["approved_recipes"]=[recipe["recipe_digest"]]
    write_json(root/"profile.json",profile);write_json(root/"recipe.json",recipe)
    backend=RemoteExecutor(executor_id="local.remote.video",tool_name="studio.video.generate",profile=root/"profile.json",
        recipe=root/"recipe.json",worker_wheel=wheel,state_root=root/"state",output_id="video")
    registry=ExecutorRegistry(load_extras=False);registry.register(backend)
    executor=PlanExecutor(root/"production",registry=registry,executor_override=backend.executor_id)
    plan=video_plan(build_expected_contract({"duration_seconds":1},width=64,height=64))
    first=executor.execute_plan(plan)
    assert first["success"],first
    assert executor.generation_dispatches["local"]==1
    for _ in range(2):
        result=executor.execute_plan(plan)
        assert result["success"],result
        assert all(v["status"]=="reused" for v in result["steps"].values())
        assert sum(executor.generation_dispatches.values())==0
        assert result["steps"]["producer"]["remote_provenance"]["result_digest"]


def test_control_movie_transferred_and_backend_ack_verified(remote_case):
    from fsf.assembly.fast_assembly import require_ffmpeg
    from fsf.runtime.control_import import import_control_video
    from fsf.runtime.blender import condition_request
    from fsf.conformance.media import build_expected_contract
    root,recipe,profile,definition,wheel=remote_case
    control=root/"inputs/control.mp4"
    subprocess.run([require_ffmpeg(),"-nostdin","-v","error","-f","lavfi","-i","testsrc2=size=64x64:rate=24",
        "-frames:v","24","-an","-c:v","libx264","-pix_fmt","yuv420p",str(control)],
        stdin=subprocess.DEVNULL,capture_output=True,timeout=30,check=True)
    imported=import_control_video(control,output_root=root/"inputs",control_id="control")
    condition=condition_request(root/"inputs"/imported["bundle"],"imported")
    recipe["parameters"]={}
    recipe["inputs"]=[{"artifact_id":"control","role":"control_video","media_type":"video/mp4"}]
    recipe["condition_capability"]={"video_conditioning":True,"max_frames":96,"max_width":512,"max_height":512,"clocks":[condition["clock"]]}
    recipe["acknowledgement_output"]="ack.json"
    recipe["steps"][0].update(args=["{python}","-m","fsf.remote.jobs","condition-copy","--input","{input.control}",
        "--request","{request}","--out","{output.video}","--ack","{output.ack}"],outputs=[
            {"artifact_id":"video","path":"video.mp4","role":"video","media_type":"video/mp4"},
            {"artifact_id":"ack","path":"ack.json","role":"data","media_type":"application/json"}])
    recipe=seal(recipe,"recipe_digest");profile["approved_recipes"]=[recipe["recipe_digest"]]
    contract=build_expected_contract({"duration_seconds":1},width=64,height=64)
    definition.update(parameters={},inputs={"control":"control.mp4"},video_condition=condition,expected_media_contract=contract)
    definition["parent"]["expected_contract_digest"]=digest(contract)
    prepared,bundle=prepare((root,recipe,profile,definition,wheel))
    submit_bundle(bundle,profile=profile,state_root=root/"state")
    assert wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")["status"]=="verified"
    manifest,_=accepted_result(prepared["dispatch_id"],profile=profile,state_root=root/"state")
    assert manifest["condition_acknowledgement"]["condition_digest"]==condition["condition_digest"]


@pytest.mark.parametrize("change",["gpu","cpu","deadline","credential","unauthorized"])
def test_admission_rejects_policy_change_before_submit(remote_case,change):
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    if change=="gpu":profile["policy"]["gpu"]=True
    elif change=="cpu":profile["policy"]["cpu"]=1
    elif change=="deadline":profile["policy"]["deadline_seconds"]=1
    elif change=="credential":profile["policy"]["credential_env_names"]=["UNAPPROVED_TOKEN"]
    else:profile["authorized"]=False
    with pytest.raises(RemoteError):submit_bundle(bundle,profile=profile,state_root=root/"state")
    assert not list((root/"worker").glob("*/worker-status.json"))


def test_worker_rejects_known_secret_output(remote_case,monkeypatch):
    root,recipe,profile,definition,wheel=remote_case
    monkeypatch.setenv("SYNTHETIC_WORKER_TOKEN","synthetic-long-private-credential")
    recipe["environment"]["credential_env_names"]=["SYNTHETIC_WORKER_TOKEN"]
    recipe["steps"][0]["args"]=["{python}","-c",
        "import os,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(os.environ['SYNTHETIC_WORKER_TOKEN'])","{output.result}"]
    recipe=seal(recipe,"recipe_digest");profile["approved_recipes"]=[recipe["recipe_digest"]]
    profile["policy"]["credential_env_names"]=["SYNTHETIC_WORKER_TOKEN"]
    prepared,bundle=prepare((root,recipe,profile,definition,wheel))
    result=run_worker(bundle,output_root=root/"worker",approved_recipe=recipe["recipe_digest"])
    assert result["status"]=="failed" and result["error_code"]=="REMOTE_SECRET_OUTPUT"
    manifest=json.loads((root/"worker"/prepared["request_digest"]/"result.json").read_text())
    assert manifest["artifacts"]==[]


def test_interrupted_child_step_is_not_repeated(remote_case):
    import signal
    root,recipe,profile,definition,wheel=remote_case
    recipe["steps"][0]["args"]=["{python}","-c",
        "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)","{output.result}"]
    recipe=seal(recipe,"recipe_digest");profile["approved_recipes"]=[recipe["recipe_digest"]]
    prepared,bundle=prepare((root,recipe,profile,definition,wheel))
    process=subprocess.Popen([sys.executable,"-m","fsf.cli","remote","worker",str(bundle),"--out",str(root/"worker"),
        "--recipe-digest",recipe["recipe_digest"]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    child_file=root/"worker"/prepared["request_digest"]/"outputs/result.json"
    deadline=time.monotonic()+20
    child_pid=None
    try:
        while time.monotonic()<deadline:
            if child_file.exists():
                child_pid=int(child_file.read_text());break
            time.sleep(.1)
        assert child_pid
        process.kill();process.wait(timeout=10)
        result=run_worker(bundle,output_root=root/"worker",approved_recipe=recipe["recipe_digest"])
        assert result["error_code"]=="REMOTE_EXECUTION_UNKNOWN"
        assert int(child_file.read_text())==child_pid
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=10)
        if child_pid:
            try:os.kill(child_pid,signal.SIGTERM)
            except OSError:pass


def test_atomic_json_retries_transient_windows_reader(tmp_path,monkeypatch):
    from fsf.remote import contracts
    original=contracts.os.replace
    calls=[]
    def transient(source,target):
        calls.append(1)
        if len(calls)<4:raise PermissionError("synthetic reader sharing conflict")
        return original(source,target)
    monkeypatch.setattr(contracts.os,"replace",transient)
    contracts.write_json(tmp_path/"status.json",{"status":"completed"})
    assert len(calls)==4
    assert json.loads((tmp_path/"status.json").read_text())["status"]=="completed"


def test_published_result_conflict_rejected_after_parent_crash(remote_case):
    from fsf.remote.contracts import sha,file_item
    root,recipe,profile,definition,wheel=remote_case
    prepared,bundle=prepare(remote_case)
    submit_bundle(bundle,profile=profile,state_root=root/"state")
    wait_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state")
    directory=root/"state"/prepared["dispatch_id"]
    manifest,accepted=accepted_result(prepared["dispatch_id"],profile=profile,state_root=root/"state")
    first_digest=manifest["result_digest"]
    # Model the crash window: accepted bytes published, final ledger not saved.
    ledger=json.loads((directory/"ledger.json").read_text())
    ledger.update(accepted_result_digest=None,status="downloaded")
    write_json(directory/"ledger.json",seal(ledger,"ledger_digest"))
    child=root/"worker"/prepared["request_digest"]
    output=child/"outputs/result.json";output.write_text('{"different":true}')
    artifact=file_item(output,"result","result.json","data","application/json")
    journal=json.loads((child/"child-journal.json").read_text())
    journal["steps"]["job"]["artifacts"]=[artifact]
    write_json(child/"child-journal.json",journal)
    manifest.update(artifacts=[artifact],child_journal_sha256=sha(child/"child-journal.json"))
    write_json(child/"result.json",seal(manifest,"result_digest"))
    (child/"result.zip").unlink()
    pack(child,["result.json","child-journal.json","environment.json","outputs/result.json"],child/"result.zip")
    with pytest.raises(RemoteError,match="RESULT_CONFLICT"):
        update_dispatch(prepared["dispatch_id"],profile=profile,state_root=root/"state",action="fetch")
    assert json.loads((accepted/"result.json").read_text())["result_digest"]==first_digest
