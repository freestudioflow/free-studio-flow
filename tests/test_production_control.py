"""Real long-media imports and bounded chunk recovery, with unchanged originals."""
from fractions import Fraction
import json
import subprocess
import sys

import pytest

from fsf.assembly.fast_assembly import require_ffmpeg
from fsf.production.control import import_control,verify_control,file_identity
from fsf.production.contracts import ProductionError
from fsf.production.artifacts import split_artifact,assemble_artifact,CHUNK_BYTES
from fsf.core.edit_store import EditStoreError


def movie(path,rate="24",count=4320):
    result=subprocess.run([require_ffmpeg(),"-nostdin","-v","error","-f","lavfi","-i",
        f"testsrc2=size=64x64:rate={rate}","-frames:v",str(count),"-threads","1","-c:v","libx264",
        "-preset","ultrafast","-pix_fmt","yuv420p","-y",str(path)],capture_output=True,timeout=60)
    assert result.returncode == 0,result.stderr
    return path


@pytest.mark.parametrize("rate",["24","24000/1001"])
def test_complete_three_minute_control_without_manual_segments(tmp_path,rate):
    source=movie(tmp_path/"source.mp4",rate)
    before=file_identity(source)
    out=tmp_path/"imports";out.mkdir()
    result=import_control(source,output_root=out,control_asset_id="control_A")
    value=verify_control(out/result["bundle"])
    assert value["frame_count"] == 4320
    assert Fraction(value["clock"]["num"],value["clock"]["den"]) == Fraction(rate)
    assert value["sha256"] == before["sha256"]
    assert file_identity(source) == before
    assert import_control(source,output_root=out,control_asset_id="control_A") == result
    assert value["motion_review"] == "not_reviewed" and value["clay_profile"] == "unverified"
    assert not list(out.glob(".long-control-*"))


def test_new_cli_import_and_old_path_keeps_short_limit(tmp_path):
    source=movie(tmp_path/"source.mp4",count=97)
    out=tmp_path/"imports";out.mkdir()
    for group,success in (("production",True),("blender",False)):
        result=subprocess.run([sys.executable,"-m","fsf.cli",group,"import-control",str(source),
            "--out",str(out),"--control-id","A"],capture_output=True,text=True,timeout=45)
        value=json.loads(result.stdout)
        assert value["ok"] is success
        assert (result.returncode == 0) is success
        assert "Traceback" not in result.stderr


def test_import_tamper_refused_and_no_private_path_in_error(tmp_path):
    source=movie(tmp_path/"source.mp4",count=100)
    result=import_control(source,output_root=tmp_path,control_asset_id="A")
    (tmp_path/result["bundle"]/"control.mp4").write_bytes(b"changed")
    with pytest.raises(ProductionError,match="SOURCE_CHANGED"):verify_control(tmp_path/result["bundle"])
    result=subprocess.run([sys.executable,"-m","fsf.cli","production","verify-control","SYNTHETIC_PRIVATE_VALUE"],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode == 2
    assert "SYNTHETIC_PRIVATE_VALUE" not in result.stdout+result.stderr


def test_large_chunk_artifact_recovers_without_recopied_complete_prefix(tmp_path,monkeypatch):
    import fsf.production.artifacts as module
    source=tmp_path/"large.bin"
    # More than the old archive limit; bounded generation, no large RAM allocation.
    with source.open("wb") as stream:
        for i in range(65):stream.write(bytes([i])*1024*1024)
    chunks=tmp_path/"chunks";chunks.mkdir()
    manifest=split_artifact(source,chunk_root=chunks)
    target=tmp_path/"recovered.bin"
    original=module._append_chunk
    calls=[]
    def fail_on_second(stream,path,item):
        calls.append(item["offset"])
        if len(calls) == 2:raise OSError("Injected transfer interruption")
        original(stream,path,item)
    monkeypatch.setattr(module,"_append_chunk",fail_on_second)
    with pytest.raises(EditStoreError,match="EDIT_IO"):assemble_artifact(manifest,chunk_root=chunks,output=target)
    assert not target.exists()
    monkeypatch.setattr(module,"_append_chunk",original)
    result=assemble_artifact(manifest,chunk_root=chunks,output=target)
    assert result["resumed_bytes"] == CHUNK_BYTES
    assert file_identity(source) == file_identity(target)
    assert assemble_artifact(manifest,chunk_root=chunks,output=target)["reused"]


def test_wrong_chunk_refused(tmp_path):
    source=tmp_path/"original.bin";source.write_bytes(b"test")
    chunks=tmp_path/"chunks";chunks.mkdir()
    value=split_artifact(source,chunk_root=chunks)
    (chunks/value["chunks"][0]["sha256"]).write_bytes(b"evil")
    with pytest.raises(ProductionError):assemble_artifact(value,chunk_root=chunks,output=tmp_path/"out.bin")
    assert not (tmp_path/"out.bin").exists()


def test_decode_checkpoint_survives_interruption(tmp_path,monkeypatch):
    import fsf.production.control as module
    source=movie(tmp_path/'long.mp4')
    original=module._run
    decoded=[]
    def interrupt(args,timeout):
        if 'framehash' in args:
            decoded.append(args)
            if len(decoded)==2:raise RuntimeError('Injected decoder loss')
        return original(args,timeout)
    monkeypatch.setattr(module,'_run',interrupt)
    with pytest.raises(RuntimeError):import_control(source,output_root=tmp_path,control_asset_id='A')
    checkpoint=next(tmp_path.glob('.long-control-*/decode-checkpoints/00000000.json'))
    before=checkpoint.read_bytes()
    decoded.clear()
    def observe(args,timeout):
        if 'framehash' in args:decoded.append(args)
        return original(args,timeout)
    monkeypatch.setattr(module,'_run',observe)
    result=import_control(source,output_root=tmp_path,control_asset_id='A')
    assert len(decoded)==2
    assert (tmp_path/result['bundle']/'decode-checkpoints/00000000.json').read_bytes()==before
    assert verify_control(tmp_path/result['bundle'])['frame_count']==4320
