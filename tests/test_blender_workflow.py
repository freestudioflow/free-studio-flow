"""Real trusted Blender scenes, exact control routing and rejected conditions."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.asset_library import canonical_library_record_digest, validate_asset_record, AssetValidationError
from fsf.core.asset_store import register_asset, lookup_asset
from fsf.core.consistency import canonical_asset_digest
from fsf.runtime.blender import (BlenderError, compile_blender_plan, validate_request, execute_blender_plan,
    verify_bundle, condition_request, validate_condition_ack, validate_glb, probe_blender)


def seal(record):
    record["content_hash"] = canonical_asset_digest(record["asset_id"], record["asset_version"],
        record["source_prompt"], record["kind"], record["constraints"], record["ref_images"])
    record["record_digest"] = canonical_library_record_digest(record)
    return record


def record(folder, name="scene.blend", data=b"test"):
    path = folder / name
    if not path.exists():
        path.write_bytes(data)
    return seal({"schema_version": "fsf_library_asset_record_v1", "digest_version": "fsf_library_asset_record_digest_v1",
        "asset_id": "prop_A", "asset_version": "1", "source_prompt": "A fixed cube.", "kind": "prop",
        "constraints": {"preserve": [], "forbid": [], "unresolved": []}, "ref_images": [],
        "primary_artifact_id": "scene", "artifacts": [{"artifact_id": "scene", "role": "scene",
        "media_type": "application/x-blender", "logical_path": name, "locator": name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}]})


def request(rec, **changes):
    return {"schema_version": "fsf_blender_request_v1", "control_id": "control_A", "revision": 1,
        "source_record_digest": rec["record_digest"], "source_file": "scene.blend", "source_type": "blend",
        "profile": "clay_v1", "clock": {"num": 24, "den": 1}, "start_frame": 0, "end_frame": 4,
        "width": 64, "height": 64, "views": [{"view_id": "front", "camera_id": "Camera"}],
        "motions": [], **changes}


def test_request_plan_pdx_and_nonmutation(tmp_path):
    req = request(record(tmp_path))
    before = copy.deepcopy(req)
    from pdx_artifact_core import validate_execution_plan
    assert validate_execution_plan(compile_blender_plan(req)) == []
    assert before == req


@pytest.mark.parametrize("mutation", ["profile", "clock", "range", "path", "view", "keys", "code", "odd"])
def test_request_invalid_rejected(tmp_path, mutation):
    req = request(record(tmp_path))
    if mutation == "profile": req["profile"] = "missing_texture_fallback"
    if mutation == "clock": req["clock"]["num"] = 23
    if mutation == "range": req["end_frame"] = 0
    if mutation == "path": req["source_file"] = "../scene.blend"
    if mutation == "view": req["views"] *= 2
    if mutation == "keys": req["motions"] = [{"object_id": "Cube", "keys": []}]
    if mutation == "code": req["python_expr"] = "forbidden"
    if mutation == "odd": req["width"] = 65
    with pytest.raises((BlenderError, ValueError)):
        validate_request(req)


def test_immutable_register_get_conflict_and_revalidation(tmp_path):
    src = tmp_path / "source"; src.mkdir()
    lib = tmp_path / "library"; lib.mkdir()
    rec = record(src)
    got = register_asset(lib, rec, resource_root=src)
    assert register_asset(lib, rec, resource_root=src) == got
    assert lookup_asset(lib, "prop_A", "1") == got
    new = copy.deepcopy(rec); new["source_prompt"] = "Changed"
    seal(new)
    with pytest.raises(AssetValidationError, match="CONFLICT"):
        register_asset(lib, new, resource_root=src)
    (lib / "prop_A@1" / "scene.blend").unlink()
    with pytest.raises(AssetValidationError): lookup_asset(lib, "prop_A", "1")


def test_trust_required_and_modified_plan_rejected(tmp_path):
    req = request(record(tmp_path)); plan = compile_blender_plan(req)
    with pytest.raises(BlenderError, match="TRUST"):
        execute_blender_plan(plan, source_root=tmp_path, output_root=tmp_path, executable="unused")
    plan["steps"][0]["transform"] = "run_script"
    with pytest.raises(BlenderError, match="PLAN"):
        execute_blender_plan(plan, source_root=tmp_path, output_root=tmp_path, executable="unused", trusted_scene=True)


@pytest.fixture(scope="module")
def real_scene(tmp_path_factory):
    executable = os.environ.get("FSF_BLENDER_EXECUTABLE") or shutil.which("blender")
    if not executable:
        pytest.skip("Blender executable not configured")
    folder = tmp_path_factory.mktemp("blender-source")
    script = folder / "fixture.py"
    script.write_text('''import bpy,sys
from pathlib import Path
bpy.ops.wm.read_factory_settings(use_empty=False)
cube=bpy.data.objects['Cube'];cube.location=(0,0,0)
cam=bpy.data.objects['Camera'];cam.location=(0,0,8);cam.rotation_euler=(0,0,0);cam.data.lens=50;cam.data.sensor_width=36
cam2=cam.copy();cam2.data=cam.data.copy();bpy.context.collection.objects.link(cam2);cam2.name='Camera2';cam2.location=(1,0,8)
bpy.ops.wm.save_as_mainfile(filepath=str(Path(sys.argv[-1])))
bpy.context.scene.use_nodes=True
bpy.ops.wm.save_as_mainfile(filepath=str(Path(sys.argv[-1]).with_name('compositor.blend')))
''', "utf-8")
    result = subprocess.run([executable, "--background", "--factory-startup", "--disable-autoexec",
        "--python-exit-code", "17", "--python", str(script), "--", str(folder / "scene.blend")],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert result.returncode == 0, "Trusted fixture creation failed"
    rec = record(folder)
    (folder / "record.json").write_text(json.dumps(rec), "utf-8")
    return executable, folder, rec


@pytest.fixture(scope="module")
def rendered(real_scene, tmp_path_factory):
    executable, source, rec = real_scene
    out = tmp_path_factory.mktemp("blender-output")
    motion = {"object_id": "Camera", "keys": [{"frame": 0, "location": [0,0,8], "rotation_euler": [0,0,0]},
        {"frame": 3, "location": [1,0,8], "rotation_euler": [0,0,0]}]}
    req = request(rec, motions=[motion])
    plan = compile_blender_plan(req)
    result = execute_blender_plan(plan, source_root=source, output_root=out, executable=executable, trusted_scene=True)
    return executable, source, out, req, result


def test_real_blender_motion_projection_video_and_resume(rendered):
    exe, source, out, req, result = rendered
    bundle = out / result["bundle"]
    manifest = verify_bundle(bundle)
    frames = manifest["frame_mapping"]
    assert len(frames) == 4
    # Independent perspective geometry: x_screen = .5 - camera_x * focal/(sensor*depth).
    assert frames[0]["projection"]["Cube"][0] == pytest.approx(.5, abs=.002)
    assert frames[-1]["projection"]["Cube"][0] == pytest.approx(.5-50/(36*8), abs=.002)
    assert manifest["generation_dispatches"] == 0
    assert manifest["motion_review"] == "not_reviewed"
    assert any(a["role"] == "control_video" for a in manifest["artifacts"])
    second = execute_blender_plan(compile_blender_plan(req), source_root=source, output_root=out,
                                 executable=exe, trusted_scene=True)
    assert second["status"] == "reused"
    data = validate_glb(bundle / "model.glb")
    assert data["meshes"]


def test_real_condition_capability_and_ack(rendered):
    _, _, out, _, result = rendered
    req = condition_request(out / result["bundle"], "front")
    cap = {"video_conditioning": True, "max_frames": 96, "max_width": 512, "max_height": 512, "clocks": [{"num":24,"den":1},{"num":24000,"den":1001}]}
    ack = {"condition_digest": req["condition_digest"], "artifact_sha256": req["artifact"]["sha256"],
           "frame_mapping_digest": req["frame_mapping_digest"], "preprocess": "none", "capability_gap": None}
    assert validate_condition_ack(req, cap, ack)["motion_review"] == "not_reviewed"
    for key in ack:
        bad = copy.deepcopy(ack); bad[key] = "wrong"
        with pytest.raises(BlenderError): validate_condition_ack(req, cap, bad)
    with pytest.raises(BlenderError, match="UNSUPPORTED"): validate_condition_ack(req, {}, ack)


def test_bundle_disk_tamper_rejected(rendered, tmp_path):
    _, _, out, _, result = rendered
    folder = tmp_path / "bundle"
    shutil.copytree(out / result["bundle"], folder)
    (folder / "control_front.mp4").write_bytes(b"changed")
    with pytest.raises(BlenderError): verify_bundle(folder)


def test_glb_nonfinite_binary_position_is_rejected(rendered,tmp_path):
    import struct
    _,_,out,_,result=rendered
    original=out/result["bundle"]/'model.glb'
    document=validate_glb(original)
    accessor=document['accessors'][document['meshes'][0]['primitives'][0]['attributes']['POSITION']]
    view=document['bufferViews'][accessor['bufferView']]
    data=bytearray(original.read_bytes())
    json_length=struct.unpack_from('<I',data,12)[0]
    offset=12+8+json_length+8+view.get('byteOffset',0)+accessor.get('byteOffset',0)
    struct.pack_into('<f',data,offset,float('nan'))
    target=tmp_path/'invalid.glb';target.write_bytes(data)
    with pytest.raises(BlenderError,match='GLB'):validate_glb(target)


def test_real_rational_object_motion(real_scene, tmp_path):
    exe, source, rec = real_scene
    move = {"object_id": "Cube", "keys": [{"frame":0,"location":[0,0,0],"rotation_euler":[0,0,0]},
        {"frame":2,"location":[1,0,0],"rotation_euler":[0,0,.5]}]}
    req = request(rec, end_frame=3, clock={"num":24000,"den":1001}, motions=[move])
    result = execute_blender_plan(compile_blender_plan(req), source_root=source, output_root=tmp_path, executable=exe, trusted_scene=True)
    manifest = verify_bundle(tmp_path/result["bundle"])
    assert manifest["frame_count"] == 3
    assert manifest["frame_mapping"][-1]["projection"]["Cube"][0] > .6


def test_real_static_views_and_cli(real_scene, tmp_path):
    exe, source, rec = real_scene
    req = request(rec, profile="reference", end_frame=1, views=[{"view_id":"front","camera_id":"Camera"}, {"view_id":"side","camera_id":"Camera2"}])
    path = tmp_path / "request.json";path.write_text(json.dumps(req), "utf-8")
    def cli(*args):
        p = subprocess.run([sys.executable, "-m", "fsf.cli", "blender", *map(str,args)], capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stdout
        return json.loads(p.stdout)
    plan = tmp_path / "plan.json"
    cli("plan", path, "--out", plan)
    out = tmp_path / "out";out.mkdir()
    result = cli("execute", plan, "--source-root",source,"--out",out,"--executable",exe,"--trusted-scene")["result"]
    manifest=cli("verify",out/result["bundle"])["result"]
    assert sum(a["role"]=="reference_image" for a in manifest["artifacts"]) == 2
    from fsf.runtime.blender import bundle_asset_record
    from fsf.core.asset_store import asset_context_ref
    from fsf.core.consistency import build_consistency_context, resolve_execution_context
    bundle=out/result["bundle"]
    asset=bundle_asset_record(bundle,asset_id="prop_A",version="2",source_prompt="A fixed cube.")
    ctx=build_consistency_context(asset_refs=[asset_context_ref(asset)],review_required=True,
        shot_id="shot_A",shot_spec={"generation_prompt":"A fixed cube."},known_asset_ids={"prop_A"},
        known_shot_ids={"shot_A"},shot_order=["shot_A"])
    image_paths={a["sha256"]:bundle/a["path"] for a in manifest["artifacts"] if a["role"]=="reference_image"}
    resolved=resolve_execution_context(ctx,shot_id="shot_A",resolved_artifact_shas={},ref_image_files=image_paths)
    assert len(resolved["asset_refs"][0]["ref_images"])==2


def test_cli_errors_do_not_echo_arbitrary_inputs(tmp_path):
    p = subprocess.run([sys.executable,"-m","fsf.cli","blender","--secret-fake","SYNTHETIC_PRIVATE_TEXT"],capture_output=True,text=True)
    assert p.returncode != 0
    assert "SYNTHETIC_PRIVATE_TEXT" not in p.stdout+p.stderr
    assert "Traceback" not in p.stderr


def _register_competitor(root, source, rec, barrier, queue):
    barrier.wait(timeout=20)
    try:
        register_asset(root, rec, resource_root=source)
        queue.put("accepted")
    except AssetValidationError as exc:
        queue.put(exc.code)


def test_independent_process_registration_conflict(tmp_path):
    import multiprocessing as mp
    source = tmp_path/"source"; source.mkdir()
    library = tmp_path/"library"; library.mkdir()
    first = record(source)
    second = copy.deepcopy(first);second["source_prompt"]="Different version content";seal(second)
    ctx=mp.get_context("spawn");barrier=ctx.Barrier(3);queue=ctx.Queue()
    workers=[ctx.Process(target=_register_competitor,args=(str(library),str(source),rec,barrier,queue)) for rec in [first,second]]
    try:
        for worker in workers: worker.start()
        barrier.wait(timeout=20)
        outcomes=[queue.get(timeout=30) for _ in workers]
        assert sorted(outcomes)==["ASSET_VERSION_CONFLICT","accepted"]
        assert lookup_asset(library,"prop_A","1")["record_digest"] in {first["record_digest"],second["record_digest"]}
    finally:
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():worker.terminate();worker.join(timeout=5)


@pytest.mark.parametrize("failure", ["timeout", "cancel", "nonzero"])
def test_failure_does_not_publish_partial_bundle(real_scene, tmp_path, monkeypatch, failure):
    import fsf.runtime.blender as runtime
    from types import SimpleNamespace
    exe,source,rec=real_scene
    monkeypatch.setattr(runtime,"probe_blender",lambda _: {"version":"3.2.2","engine":"CYCLES","device":"CPU"})
    def fail(*args,**kwargs):
        if failure=="cancel":raise KeyboardInterrupt()
        return SimpleNamespace(timed_out=failure=="timeout",returncode=1)
    monkeypatch.setattr(runtime,"run_safe_subprocess",fail)
    with pytest.raises(BlenderError,match={"timeout":"DEADLINE","cancel":"CANCELLED","nonzero":"TOOL_FAILED"}[failure]):
        execute_blender_plan(compile_blender_plan(request(rec)),source_root=source,output_root=tmp_path,executable=exe,trusted_scene=True)
    assert not [p for p in tmp_path.iterdir() if p.is_dir()]


def test_real_missing_camera_fails_without_bundle(real_scene,tmp_path):
    exe,source,rec=real_scene
    req=request(rec,views=[{"view_id":"front","camera_id":"Missing"}])
    with pytest.raises(BlenderError,match="TOOL_FAILED"):
        execute_blender_plan(compile_blender_plan(req),source_root=source,output_root=tmp_path,executable=exe,trusted_scene=True)
    assert not [p for p in tmp_path.iterdir() if p.is_dir()]


def test_compositor_cannot_replace_declared_camera_render(real_scene,tmp_path):
    exe,source,_=real_scene
    rec=record(source,name="compositor.blend")
    inputs=tmp_path/"source";inputs.mkdir()
    shutil.copyfile(source/"compositor.blend",inputs/"compositor.blend")
    (inputs/"record.json").write_text(json.dumps(rec),encoding='utf-8')
    out=tmp_path/"out";out.mkdir()
    req=request(rec,source_file="compositor.blend")
    with pytest.raises(BlenderError,match="TOOL_FAILED"):
        execute_blender_plan(compile_blender_plan(req),source_root=inputs,output_root=out,executable=exe,trusted_scene=True)
    assert not [p for p in out.iterdir() if p.is_dir()]


def test_dynamic_bundle_registers_without_static_reference(rendered,tmp_path):
    from fsf.runtime.blender import bundle_asset_record
    _,_,out,_,result=rendered
    bundle=out/result["bundle"]
    rec=bundle_asset_record(bundle,asset_id="prop_motion",version="1",source_prompt="A controlled camera move.")
    assert rec["schema_version"]=="fsf_library_asset_record_v2"
    assert rec["ref_images"]==[]
    library=tmp_path/"library";library.mkdir()
    register_asset(library,rec,resource_root=bundle)
    assert lookup_asset(library,"prop_motion","1")["record_digest"]==rec["record_digest"]


def test_condition_handoff_ingest_rejects_missing_ack(rendered,tmp_path):
    from fsf.runtime.handoff import build_workorder,ingest_workorder_result,HandoffIntegrityError
    _,_,out,_,result=rendered
    condition=condition_request(out/result["bundle"],"front")
    cap={"video_conditioning":True,"max_frames":96,"max_width":512,"max_height":512, "clocks":[{"num":24,"den":1},{"num":24000,"den":1001}]}
    work=build_workorder(job_id="j",run_id="r",step_id="s",attempt_id=1,tool="studio.video.generate",
        plan_digest="a"*64,inputs_digest="b"*64,target_artifact="movie.mp4",task_payload={},
        video_condition=condition,condition_capability=cap)
    path=tmp_path/"movie.mp4";path.write_bytes(b"Result bytes; media QC remains a separate gate.")
    data={k:work[k] for k in ["job_id","run_id","step_id","attempt_id","workorder_id","plan_digest","inputs_digest"]}
    data.update(schema_version="fsf_workorder_result_v1",status="completed",artifact_path="movie.mp4",artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    result_path=tmp_path/"result.json";result_path.write_text(json.dumps(data))
    with pytest.raises(HandoffIntegrityError,match="CONDITION_ACK"):
        ingest_workorder_result(result_path,work,tmp_path)
    data["condition_acknowledgement"]={"condition_digest":condition["condition_digest"],"artifact_sha256":condition["artifact"]["sha256"],
        "frame_mapping_digest":condition["frame_mapping_digest"],"preprocess":"none","capability_gap":None}
    result_path.write_text(json.dumps(data))
    assert ingest_workorder_result(result_path,work,tmp_path)["condition_acknowledgement"]==data["condition_acknowledgement"]


def test_inherit_env_false_does_not_forward_secret(monkeypatch):
    from fsf.executors.subprocess_safety import run_safe_subprocess
    monkeypatch.setenv("SYNTHETIC_API_KEY","not_forwarded_value")
    res=run_safe_subprocess([sys.executable,"-c","import os;print('present' if 'SYNTHETIC_API_KEY' in os.environ else 'absent')"],
        inherit_env=False,env={k:os.environ[k] for k in ["SystemRoot","WINDIR"] if k in os.environ})
    assert res.returncode==0 and res.stdout.strip()=="absent"


def test_bounded_capture_rejects_output_without_secret_fragment():
    from fsf.executors.subprocess_safety import run_safe_subprocess
    with pytest.raises(RuntimeError,match="PROCESS_OUTPUT_LIMIT") as error:
        run_safe_subprocess([sys.executable,"-c","print('PRIVATE_PREFIX'+('x'*100000))"],output_limit_bytes=4096)
    assert "PRIVATE_PREFIX" not in str(error.value)


def test_bounded_timeout_discards_partial_secret(monkeypatch):
    from fsf.executors.subprocess_safety import run_safe_subprocess
    secret="SYNTHETIC_PRIVATE_PREFIX"+"x"*10000
    monkeypatch.setenv("SYNTHETIC_API_KEY",secret)
    result=run_safe_subprocess([sys.executable,"-c","import os,time;print(os.environ['SYNTHETIC_API_KEY'][:30],flush=True);time.sleep(10)"],
        timeout_seconds=.5,output_limit_bytes=4096)
    assert result.timed_out
    assert "SYNTHETIC_PRIVATE_PREFIX" not in result.stdout+result.stderr+result.raw_stdout+result.raw_stderr


def _registration_fault_worker(library, source, rec, when, reached, release):
    import fsf.core.asset_store as store
    rename=store.os.rename
    def paused(a,b):
        if when=="after":rename(a,b)
        reached.set()
        # A killed worker must not own a synchronization lock later used by parent.
        import threading
        threading.Event().wait(timeout=30)
        if when=="before":rename(a,b)
    store.os.rename=paused
    store.register_asset(library,rec,resource_root=source)


@pytest.mark.parametrize("when",["before","after"])
def test_killed_registration_never_publishes_partial_bundle(tmp_path,when):
    import multiprocessing as mp
    source=tmp_path/"source";source.mkdir();library=tmp_path/"library";library.mkdir()
    rec=record(source)
    ctx=mp.get_context("spawn");reached=ctx.Event();release=ctx.Event()
    worker=ctx.Process(target=_registration_fault_worker,args=(str(library),str(source),rec,when,reached,release))
    worker.start()
    try:
        assert reached.wait(timeout=20)
        worker.terminate();worker.join(timeout=10)
        assert not worker.is_alive()
        if when=="before":assert not (library/"prop_A@1").exists()
        else:assert lookup_asset(library,"prop_A","1")["record_digest"]==rec["record_digest"]
        assert register_asset(library,rec,resource_root=source)["record_digest"]==rec["record_digest"]
    finally:
        if worker.is_alive():worker.terminate();worker.join(timeout=5)
