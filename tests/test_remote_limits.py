"""Bounded Kaggle dataset verification without a provider account."""
from email.message import Message
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from fsf.remote.contracts import RemoteError
from fsf.remote.kaggle_native import publish_private_dataset
from test_streaming_api import limits
from test_kaggle_native import native, case, remote_wheel


@pytest.mark.parametrize("phase,expected", [
    ("create","REMOTE_KAGGLE_DATASET_CREATE"),
    ("ready","REMOTE_KAGGLE_DATASET_READY"),
    ("verify","REMOTE_KAGGLE_DATASET_VERIFY"),
])
def test_dataset_phase_reports_only_safe_code(tmp_path,phase,expected):
    class API:
        def dataset_status(self,_):
            if phase=="create":
                return None
            if phase=="ready":
                raise RuntimeError("untrusted provider detail") if self.polled else self.mark()
            return "ready"
        polled=False
        def mark(self):
            self.polled=True
            return RuntimeError("initial status unavailable")
        def dataset_create_new(self,*args,**kwargs):
            if phase=="create":
                raise RuntimeError("untrusted provider detail")
        def dataset_metadata(self,*args):
            raise RuntimeError("untrusted provider detail")
    archive=tmp_path/"input.zip"
    archive.write_bytes(b"payload")
    with pytest.raises(RemoteError,match=f"^{expected}$"):
        publish_private_dataset(API(),"owner/dataset",archive,tmp_path,limits=limits())


@pytest.mark.parametrize("code,timed_out,expected", [
    ("REMOTE_KAGGLE_DATASET_CREATE",False,"REMOTE_KAGGLE_DATASET_CREATE"),
    ("REMOTE_KAGGLE_DATASET_READY",False,"REMOTE_KAGGLE_DATASET_READY"),
    ("STREAM_SHA_MISMATCH",False,"STREAM_SHA_MISMATCH"),
    ("untrusted provider detail",False,"REMOTE_KAGGLE_API_FAILED"),
    ("REMOTE_KAGGLE_DATASET_VERIFY",True,"REMOTE_KAGGLE_API_FAILED"),
])
def test_parent_preserves_safe_child_failure(tmp_path,monkeypatch,code,timed_out,expected):
    from fsf.remote.v2_transports import V2Adapter
    adapter=object.__new__(V2Adapter)
    adapter.profile={}
    adapter.store_config="unused"
    monkeypatch.setattr(adapter,"_native",lambda:True)
    def child(*args,**kwargs):
        (tmp_path/"provider-v2-response.json").write_text(json.dumps({"ok":False,"code":code}))
        return SimpleNamespace(returncode=2,timed_out=timed_out,stdout="",stderr="")
    monkeypatch.setattr("fsf.executors.subprocess_safety.run_safe_subprocess",child)
    with pytest.raises(RemoteError,match=f"^{expected}$"):
        adapter._kaggle("prepare",{"request_digest":"a"*64},None,tmp_path)
    report=json.loads(next((tmp_path/"operation-reports").glob("*.json")).read_text())
    assert report["safe_code"]==expected


@pytest.mark.parametrize("reported_size,received,error", [
    (-1,b"good",None), (-1,b"evil","STREAM_SHA_MISMATCH"),
    (-1,b"go","STREAM_TRUNCATED"), (5,b"good","REMOTE_KAGGLE_DATASET"),
])
def test_private_dataset_unknown_size_is_verified(tmp_path,monkeypatch,reported_size,received,error):
    import requests
    from contextlib import nullcontext

    class Response:
        url="https://storage.googleapis.com/example"
        is_redirect=False
        def __init__(self):
            self.raw=io.BytesIO(received)
            self.raw.headers=Message()
        def __enter__(self):
            return self
        def __exit__(self,*args):
            self.raw.close()
        def raise_for_status(self):
            pass
    class API:
        def dataset_status(self,_):
            return "ready"
        def dataset_metadata(self,_,directory):
            from pathlib import Path
            (Path(directory)/"dataset-metadata.json").write_text('{"isPrivate":true}')
        def dataset_list_files(self,_):
            return SimpleNamespace(files=[SimpleNamespace(name="payload.bin",size=reported_size)])
        def build_kaggle_client(self):
            sdk=SimpleNamespace(download_dataset=lambda _:Response())
            return nullcontext(SimpleNamespace(datasets=SimpleNamespace(dataset_api_client=sdk)))
    monkeypatch.setattr(requests,"get",lambda *args,**kwargs:Response())
    bundle=tmp_path/"payload.bin"
    bundle.write_bytes(b"good")
    if error:
        with pytest.raises(RemoteError,match=error):
            publish_private_dataset(API(),"owner/dataset",bundle,tmp_path,limits=limits())
        assert not (tmp_path/"dataset-verification.json").exists()
    else:
        publish_private_dataset(API(),"owner/dataset",bundle,tmp_path,limits=limits())
        proof=json.loads((tmp_path/"dataset-verification.json").read_text())
        assert proof=={"reported_size":None,"reused":True,"private":True,
                       "identity":{"size_bytes":4,"sha256":hashlib.sha256(b"good").hexdigest()}}


def test_limits_handshake_rejects_drift_before_worker(native,monkeypatch):
    from fsf.remote.artifacts import store_public_identity
    from fsf.remote.contracts import digest, seal
    from fsf.remote.kaggle_native import external_notebook
    from fsf.remote.native_store import NativeStore
    from fsf.remote.v2 import prepare_request, validate_request, run_worker

    root,store,profile,old=native
    store=NativeStore({**store.profile,"schema_version":"fsf_kaggle_native_store_v2"})
    profile.update(schema_version="fsf_remote_profile_v5",transport="kaggle",
                   settings={"accelerator":"none"},limits=limits(seconds=120))
    profile["store_binding"]["config_digest"]=store_public_identity(store.profile)
    fields=("parent","parameters","video_condition","consistency_requirements","expected_media_contract")
    definition={key:old[key] for key in fields}
    definition["inputs"]={}
    request=prepare_request(definition,old["recipe"],profile=profile,store=store,inputs={},
                            wheel=old["worker_wheel"],segment=old["segment"],output=root/"prepared")
    assert request["limits_digest"]==digest(profile["limits"])
    assert request["protocol_version"]=="5"
    notebook=external_notebook(request,"a"*64,"fixture")
    assert request["limits_digest"] in "".join(notebook["cells"][0]["source"])
    bad=seal({**request,"limits":{**request["limits"],"output_bytes":1}},"request_digest")
    with pytest.raises(RemoteError,match="REMOTE_LIMITS_MISMATCH"):
        validate_request(bad,store)
    with pytest.raises(RemoteError,match="REMOTE_LIMITS_MISMATCH"):
        run_worker(bad,store=store,output_root=root/"worker",approved_recipe=old["recipe"]["recipe_digest"])


def test_bootstrap_rejects_mismatched_limits_before_extracting(tmp_path,capsys):
    from importlib import resources
    from fsf.remote.contracts import digest

    archive=tmp_path/"archive.zip"
    archive.write_bytes(b"should not be read")
    bootstrap=resources.files("fsf.tools").joinpath("remote_bootstrap_native.py").read_bytes()
    policy=limits()
    with pytest.raises(RuntimeError,match="REMOTE_NATIVE_BOOTSTRAP_FAILED"):
        exec(compile(bootstrap,"native_bootstrap","exec"),{
            "WORK_ROOT":str(tmp_path/"bootstrap"),"BUNDLE":str(archive),"BUNDLE_SHA":"0"*64,
            "DEADLINE":60,"limits":{**policy,"member_bytes":1},"limits_digest":digest(policy),
            "request_digest":"a"*64,
        })
    reports=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[-1]["code"]=="REMOTE_LIMITS_MISMATCH"
    assert not (tmp_path/"bootstrap"/"request.json").exists()
