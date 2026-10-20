"""Host-owned secrets, bounded onboarding and no repeated rejected API calls."""
import copy
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fsf.remote.contracts import RemoteError
from fsf.remote.credentials import credential_scope, credential_value, validate_binding
from fsf.remote.model_api import run_api
from fsf.remote.onboarding import setup
from test_model_api_capabilities import api_server, profile_for, request_for


def binding(profile, source="environment", reference="TEST_HOST_CREDENTIAL", resolver=None):
    return {"schema_version":"fsf_credential_binding_v1", "profile_id":profile["profile_id"],
        "source":source, "reference":reference, "revision":"1", "authorized":True,"resolver":resolver}


@pytest.mark.parametrize("provider", ["nebius","google","bedrock"])
def test_offline_setup_starts_unauthorized(provider):
    profile, host, guide = setup(provider,"text","operator-model")
    assert not profile["authorized"] and not host["authorized"]
    assert not guide["verified_inference"]
    assert guide["guidance"]
    validate_binding(host,profile)


def test_native_text_setup_does_not_claim_provider_media():
    with pytest.raises(RemoteError,match="REQUIRES_MEDIA_PROFILE"):
        setup("google","video","operator-model")


def test_host_binding_scoped_without_environment_mutation(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;state["payload"]=b"Host-bound output."
    profile=profile_for("text",endpoint)
    secret="test-host-only-credential"
    monkeypatch.setenv("TEST_HOST_CREDENTIAL",secret)
    monkeypatch.delenv(profile["key_env"],raising=False)
    result=run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path,binding=binding(profile))
    assert result["status"]=="completed"
    assert state["authorization"]=="Bearer "+secret
    assert profile["key_env"] not in os.environ
    for path in tmp_path.rglob("*.json"):
        assert secret not in path.read_text()


def test_real_host_resolver_process_and_expiry(tmp_path):
    profile,_,_=setup("nebius","text","operator-model")
    resolver=tmp_path/"resolver.py"
    resolver.write_text("import json,time;print(json.dumps(dict(value='host-test-credential',revision='1',expires_at=time.time()+60)))")
    host=binding(profile,"host_secret_manager","project/model",[sys.executable,str(resolver)])
    with credential_scope(profile,host):
        assert credential_value(profile)=="host-test-credential"
    resolver.write_text("import json;print(json.dumps(dict(value='host-test-credential',revision='1',expires_at=1)))")
    with credential_scope(profile,host),pytest.raises(RemoteError,match="SECRET_EXPIRED"):
        credential_value(profile)


@pytest.mark.parametrize("change", ["revision","scope","authorization","inline_secret"])
def test_binding_rejects_wrong_scope_and_inline_values(change):
    profile,_,_=setup("nebius","text","operator-model")
    host=binding(profile)
    if change=="revision":host["revision"]=""
    if change=="scope":host["profile_id"]="different"
    if change=="authorization":host["authorized"]="true"
    if change=="inline_secret":host["token"]="synthetic"
    with pytest.raises(RemoteError):validate_binding(host,profile)


def test_host_secret_nonzero_diagnostics_never_echo_value(tmp_path):
    profile,_,_=setup("nebius","text","operator-model")
    resolver=tmp_path/"resolver.py"
    resolver.write_text("import sys;print('host-test-secret-should-not-escape',file=sys.stderr);sys.exit(2)")
    with credential_scope(profile,binding(profile,"host_secret_manager",resolver=[sys.executable,str(resolver)])):
        with pytest.raises(RemoteError) as exc:credential_value(profile)
    assert "host-test-secret" not in str(exc.value)


def test_explicit_host_proxy_cannot_masquerade_as_native():
    profile,_,_=setup("google","text","operator-model")
    with pytest.raises(RemoteError,match="HOST_PROXY_REQUIRED"):
        validate_binding(binding(profile,"host_proxy"),profile)


def test_403_stops_new_requests_until_explicit_recheck(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;state["http_status"]=403
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-test-key")
    profile=profile_for("text",endpoint)
    with pytest.raises(RemoteError,match="AUTH_FAILED"):
        run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path)
    for n in range(3):
        request=dict(request_for(),request_id="different_"+str(n))
        with pytest.raises(RemoteError,match="CONNECTION_PAUSED"):
            run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path)
    assert state["calls"]==1
    state.update(http_status=200,payload=b"Authorized after configuration update.")
    result=run_api(dict(request_for(),request_id="new_authorized_intent"),profile,
        inputs_root=tmp_path,state_root=tmp_path,recheck=True)
    assert result["status"]=="completed" and state["calls"]==2


def test_returned_secret_is_rejected_not_written_to_receipt(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;secret="synthetic-host-response-secret"
    state["payload"]=secret.encode()
    monkeypatch.setenv("TEST_HOST_CREDENTIAL",secret)
    profile=profile_for("text",endpoint)
    with pytest.raises(RemoteError,match="SECRET"):
        run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path,binding=binding(profile))
    assert not list(tmp_path.rglob("receipt.json"))


def test_pure_cli_setup_and_no_overwrite(tmp_path):
    target=tmp_path/"config"
    args=[sys.executable,"-m","fsf.cli","api","setup","--provider","nebius","--model","operator-model","--out",str(target)]
    first=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert first.returncode==0,first.stdout
    assert not json.loads((target/"binding.json").read_text())["authorized"]
    before=(target/"profile.json").read_bytes()
    second=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert second.returncode!=0 and (target/"profile.json").read_bytes()==before


def test_keyring_host_binding_uses_scoped_service(monkeypatch):
    profile,_,_=setup("nebius","text","operator-model")
    seen=[]
    monkeypatch.setitem(sys.modules,"keyring",SimpleNamespace(get_password=lambda service,account:
        (seen.append((service,account)) or "synthetic-keyring-value")))
    with credential_scope(profile,binding(profile,"os_credential_store","model-account")):
        assert credential_value(profile)=="synthetic-keyring-value"
    assert seen==[("fsf/"+profile["profile_id"],"model-account")]


@pytest.mark.parametrize("provider_code,code",[("AccessDeniedException","ACCESS_DENIED"),
    ("ExpiredTokenException","EXPIRED"),("ThrottlingException","RATE_LIMITED"),("ValidationException","MODEL_UNAVAILABLE")])
def test_bedrock_errors_are_classified_without_raw_diagnostics(monkeypatch,provider_code,code):
    from fsf.remote import model_api
    profile,_,_=setup("bedrock","text","operator-model",region="us-east-1")
    class Failure(Exception):
        response={"Error":{"Code":provider_code,"Message":"synthetic-secret-diagnostic"}}
    class Client:
        def converse(self,**kwargs):raise Failure("synthetic-secret-diagnostic")
    monkeypatch.setattr(model_api,"bedrock_client",lambda *a:Client())
    with pytest.raises(RemoteError,match=code) as exc:
        model_api._native(profile,dict(prompt="test",parameters={}),time.monotonic()+10)
    assert "synthetic-secret" not in str(exc.value)


def test_cooperative_api_worker_collects_same_job_without_second_post(api_server,tmp_path,monkeypatch):
    from fsf.remote.api_executor import ModelAPIExecutor
    from fsf.conformance.media import ensure_media_tools_on_path,build_expected_contract
    state,endpoint=api_server
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-api-key")
    ffmpeg,_=ensure_media_tools_on_path()
    video=tmp_path/"response.mp4"
    subprocess.run([ffmpeg,"-nostdin","-v","error","-y","-f","lavfi","-i","testsrc2=size=128x128:rate=24",
        "-frames:v","24","-an","-c:v","libx264","-pix_fmt","yuv420p",str(video)],capture_output=True,check=True,timeout=30)
    profile=profile_for("video",endpoint)
    profile["http"].update(job_id_pointer="/job_id",response_mode="base64")
    profile["http"]["routes"].update(status="/jobs/{job_id}",fetch=None)
    state["response"]={"job_id":"job-1"};state["poll_response"]={"status":"running"}
    p=tmp_path/"profile.json";p.write_text(json.dumps(profile))
    backend=ModelAPIExecutor(executor_id="api.cooperative",tool_name="studio.video.generate",profile=p,state_root=tmp_path)
    task=dict(id="S01",request_id="req:cooperative",prompt="real test signal",duration_seconds=1,artifact_filename="shot.mp4",
        cooperative_wait=True,remote_parent={"attempt_id":1},
        remote_expected_media_contract=build_expected_contract({"duration_seconds":1},width=128,height=128))
    first=backend.execute_shot(task,tmp_path)
    assert first["status"]=="pending_external" and state["calls"]==1
    state["poll_response"]={"status":"completed","output":base64.b64encode(video.read_bytes()).decode()}
    second=backend.execute_shot(task,tmp_path)
    assert second["status"]=="completed" and state["calls"]==1
    assert backend.last_submitted_delta==0 and (tmp_path/"shot.mp4").is_file()


def test_binding_revision_cannot_relabel_an_old_inference(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;state["payload"]=b"Bound response."
    monkeypatch.setenv("TEST_HOST_CREDENTIAL","synthetic-bound-value")
    profile=profile_for("text",endpoint)
    host=binding(profile)
    result=run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path,binding=host)
    assert result["status"]=="completed"
    changed=dict(host,revision="2")
    with pytest.raises(RemoteError,match="REQUEST_ID_CONFLICT"):
        run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path,binding=changed)
    assert state["calls"]==1


def test_explicit_unknown_intent_check_does_not_reopen_connection(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;state["http_status"]=500
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-test-key")
    profile=profile_for("text",endpoint)
    with pytest.raises(RemoteError,match="SUBMISSION_UNKNOWN"):
        run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path)
    result=run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path,recheck=True)
    assert result["status"]=="submission_unknown" and state["calls"]==1
    with pytest.raises(RemoteError,match="CONNECTION_PAUSED"):
        run_api(request_for("another_intent"),profile,inputs_root=tmp_path,state_root=tmp_path)
    assert state["calls"]==1
