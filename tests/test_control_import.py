"""External control import uses real media and never invents render evidence."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from fsf.assembly.fast_assembly import require_ffmpeg
from fsf.runtime.blender import (BlenderError, condition_request, verify_bundle,
    validate_condition_ack, bundle_asset_record, digest)
from fsf.runtime.control_import import import_control_video
from fsf.core.asset_store import register_asset, lookup_asset


def movie(path, *, rate="24", count=12, size="64x64", audio=False):
    args=[require_ffmpeg(),"-nostdin","-v","error","-y","-f","lavfi","-i",f"testsrc2=size={size}:rate={rate}"]
    if audio: args += ["-f","lavfi","-i","sine=frequency=440:sample_rate=48000"]
    args += ["-frames:v",str(count),"-c:v","libx264","-pix_fmt","yuv420p"]
    if audio: args += ["-c:a","aac","-shortest"]
    subprocess.run(args+[str(path)],check=True,timeout=30,stdin=subprocess.DEVNULL,capture_output=True)
    return path


def cli(*args, ok=True):
    proc=subprocess.run([sys.executable,"-m","fsf.cli","blender",*map(str,args)],capture_output=True,text=True,timeout=40)
    assert (proc.returncode==0)==ok,proc.stdout+proc.stderr
    value=json.loads(proc.stdout)
    assert "Traceback" not in proc.stderr
    return value


@pytest.mark.parametrize("rate,count",[("24",12),("24000/1001",11)])
def test_cli_import_condition_ack_and_registry(tmp_path,rate,count):
    src=movie(tmp_path/"user.mp4",rate=rate,count=count)
    root=tmp_path/"imports";root.mkdir()
    before=src.read_bytes()
    result=cli("import-control",src,"--out",root,"--control-id","user_A")["result"]
    bundle=root/result["bundle"]
    manifest=cli("verify",bundle)["result"]
    assert manifest["frame_count"]==count
    assert manifest["provenance"]=="operator_control_video_import"
    assert manifest["clay_profile"]=="unverified"
    assert manifest["motion_review"]=="not_reviewed"
    assert "render_profile" not in manifest and "source_file" not in manifest
    assert (bundle/"control.mp4").read_bytes()==before==src.read_bytes()
    assert cli("import-control",src,"--out",root,"--control-id","user_A")["result"]==result
    condition=cli("condition",bundle,"--view-id","imported","--out",tmp_path/"condition.json")["result"]
    capability={"video_conditioning":True,"max_frames":96,"max_width":512,"max_height":512,"clocks":[manifest["clock"]]}
    ack={"condition_digest":condition["condition_digest"],"artifact_sha256":condition["artifact"]["sha256"],
         "frame_mapping_digest":condition["frame_mapping_digest"],"preprocess":"none","capability_gap":None}
    assert validate_condition_ack(condition,capability,ack)["motion_review"]=="not_reviewed"
    capability["video_conditioning"]=False
    with pytest.raises(BlenderError):validate_condition_ack(condition,capability,ack)
    rec=bundle_asset_record(bundle,asset_id="white_model",version="1",source_prompt="User control")
    library=tmp_path/"library";library.mkdir()
    register_asset(library,rec,resource_root=bundle)
    assert lookup_asset(library,"white_model","1")
    assert not (bundle/"receipt.json").exists()


@pytest.mark.parametrize("changes,code",[
    ({"rate":"25"},"CONTROL_CLOCK_UNSUPPORTED"),
    ({"size":"514x64"},"CONTROL_DIMENSIONS_UNSUPPORTED"),
    ({"count":97},"CONTROL_FRAME_LIMIT"),
    ({"audio":True},"CONTROL_STREAMS_INVALID")])
def test_unsupported_inputs_refused_without_conversion(tmp_path,changes,code):
    src=movie(tmp_path/"user.mp4",**changes)
    root=tmp_path/"out";root.mkdir()
    value=cli("import-control",src,"--out",root,"--control-id","A",ok=False)
    assert value["error"]["code"]==code
    assert not list(root.glob("*/manifest.json"))
    assert not list(root.glob(".control-import-*"))


def test_corrupt_input_and_secret_diagnostics(tmp_path):
    src=tmp_path/"PRIVATE_SYNTHETIC_NAME.mp4";src.write_bytes(b"not video")
    result=cli("import-control",src,"--out",tmp_path,"--control-id","A",ok=False)
    assert "PRIVATE_SYNTHETIC_NAME" not in json.dumps(result)


def test_tamper_and_mapping_integrity(tmp_path):
    src=movie(tmp_path/"user.mp4")
    result=import_control_video(src,output_root=tmp_path,control_id="A")
    bundle=tmp_path/result["bundle"]
    original=json.loads((bundle/"manifest.json").read_text())
    changed=copy.deepcopy(original);changed["frame_mapping"][1]["input_frame"]=0
    changed["frame_mapping_digest"]=digest(changed["frame_mapping"])
    changed["manifest_digest"]=digest({k:v for k,v in changed.items() if k!="manifest_digest"})
    (bundle/"manifest.json").write_text(json.dumps(changed))
    with pytest.raises(BlenderError):verify_bundle(bundle)
    (bundle/"manifest.json").write_text(json.dumps(original))
    (bundle/"control.mp4").unlink()
    with pytest.raises((BlenderError,OSError)):verify_bundle(bundle)


def test_cancel_publish_nothing(tmp_path,monkeypatch):
    from fsf.runtime import control_import as module
    src=movie(tmp_path/"user.mp4")
    def cancelled(*args,**kwargs):raise KeyboardInterrupt()
    monkeypatch.setattr(module,"run_safe_subprocess",cancelled)
    with pytest.raises(BlenderError,match="BLENDER_CANCELLED"):
        import_control_video(src,output_root=tmp_path,control_id="A")
    assert not list(tmp_path.glob("*/manifest.json"))
    assert not list(tmp_path.glob(".control-import-*"))


def test_imported_condition_handoff_roundtrip(tmp_path):
    import hashlib
    from fsf.runtime.handoff import build_workorder, ingest_workorder_result, HandoffIntegrityError
    src=movie(tmp_path/"user.mp4")
    result=import_control_video(src,output_root=tmp_path,control_id="A")
    c=condition_request(tmp_path/result["bundle"],"imported")
    cap={"video_conditioning":True,"max_frames":96,"max_width":512,"max_height":512,"clocks":[c["clock"]]}
    work=build_workorder(job_id="j",run_id="r",step_id="s",attempt_id=1,tool="studio.video.generate",
        plan_digest="a"*64,inputs_digest="b"*64,target_artifact="movie.mp4",task_payload={},video_condition=c,condition_capability=cap)
    output=movie(tmp_path/"movie.mp4")
    data={k:work[k] for k in ("job_id","run_id","step_id","attempt_id","workorder_id","plan_digest","inputs_digest")}
    data.update(schema_version="fsf_workorder_result_v1",status="completed",artifact_path="movie.mp4",artifact_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    path=tmp_path/"result.json";path.write_text(json.dumps(data))
    with pytest.raises(HandoffIntegrityError):ingest_workorder_result(path,work,tmp_path)
    data["condition_acknowledgement"]={"condition_digest":c["condition_digest"],"artifact_sha256":c["artifact"]["sha256"],"frame_mapping_digest":c["frame_mapping_digest"],"preprocess":"none","capability_gap":None}
    path.write_text(json.dumps(data))
    assert ingest_workorder_result(path,work,tmp_path)["condition_acknowledgement"]==data["condition_acknowledgement"]


def test_import_timeout_does_not_publish(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from fsf.runtime import control_import as module
    src=movie(tmp_path/"user.mp4")
    monkeypatch.setattr(module,"run_safe_subprocess",lambda *a,**k:SimpleNamespace(timed_out=True))
    with pytest.raises(BlenderError,match="CONTROL_DEADLINE_EXCEEDED"):
        import_control_video(src,output_root=tmp_path,control_id="A")
    assert not list(tmp_path.glob("*/manifest.json"))


def test_input_change_during_import_refused(tmp_path,monkeypatch):
    from fsf.runtime import control_import as module
    src=movie(tmp_path/"user.mp4")
    calls=[]
    def changed(path):
        calls.append(1)
        return "a"*64 if len(calls)==1 else "b"*64
    monkeypatch.setattr(module,"_source_sha",changed)
    with pytest.raises(BlenderError,match="CONTROL_SOURCE_CHANGED"):
        import_control_video(src,output_root=tmp_path,control_id="A")
    assert not list(tmp_path.glob("*/manifest.json"))


def test_variable_timestamps_rejected(tmp_path):
    src=tmp_path/"vfr.mp4"
    subprocess.run([require_ffmpeg(),"-nostdin","-v","error","-y","-f","lavfi","-i","testsrc2=size=64x64:rate=24",
       "-vf","setpts=PTS+gte(N\\,6)*2", "-fps_mode","vfr","-frames:v","12","-c:v","libx264",str(src)],
       check=True,capture_output=True,timeout=30,stdin=subprocess.DEVNULL)
    with pytest.raises(BlenderError):import_control_video(src,output_root=tmp_path,control_id="A")


@pytest.mark.parametrize("data",[b"",b"{}",b"\\x00"*1024])
def test_fake_mp4_rejected(tmp_path,data):
    src=tmp_path/"fake.mp4";src.write_bytes(data)
    with pytest.raises(BlenderError):import_control_video(src,output_root=tmp_path,control_id="A")


def test_symlink_source_rejected(tmp_path):
    src=movie(tmp_path/"source.mp4");link=tmp_path/"link.mp4"
    try:link.symlink_to(src)
    except OSError:pytest.skip("Symlink privilege unavailable")
    with pytest.raises(BlenderError):import_control_video(link,output_root=tmp_path,control_id="A")
