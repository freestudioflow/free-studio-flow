"""Real HTTP contract tests; generated signals are not model inference."""
import base64
import io
import json
import subprocess
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

import pytest
from PIL import Image

from fsf.remote.contracts import RemoteError
from fsf.remote.model_api import run_api, verify_receipt
from fsf.conformance.media import build_expected_contract, ensure_media_tools_on_path


@pytest.fixture
def api_server():
    state = {"payload": b"", "calls": 0, "body": None, "authorization": None, "response": None, "http_status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state["calls"] += 1
            state["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["authorization"] = self.headers.get("Authorization")
            data = json.dumps(state["response"] or {"output": base64.b64encode(state["payload"]).decode()}).encode()
            self.send_response(state["http_status"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            data=json.dumps(state.get("poll_response",{})).encode()
            self.send_response(200)
            self.send_header("Content-Length",str(len(data)))
            self.end_headers();self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def profile_for(capability, endpoint):
    template = "image" if capability == "text" else capability
    value = json.loads(resources.files("fsf.remote.templates").joinpath(template + ".api-profile.json").read_text())
    value.update(authorized=True, allow_loopback=True, base_url=endpoint, capability=capability)
    value["http"].update(response_mode="base64", job_id_pointer=None)
    value["http"]["routes"]["status"] = None
    if capability == "text":
        value["output"].update(media_type="text/plain", suffix=".txt")
    return value


def png(size):
    buffer = io.BytesIO()
    Image.new("RGB", size, "orange").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("capability", ["text", "image", "audio", "upscale"])
def test_typed_api_http_roundtrip_and_verified_resume(capability, api_server, tmp_path, monkeypatch):
    state, endpoint = api_server
    monkeypatch.setenv("FSF_MODEL_API_KEY", "synthetic-contract-credential")
    profile = profile_for(capability, endpoint)
    request = {"schema_version": "fsf_model_api_request_v1", "request_id": "contract",
               "prompt": "contract test", "parameters": {}, "inputs": {}, "expected_media_contract": None}
    if capability == "text":
        state["payload"] = b"A real UTF-8 response."
    elif capability == "audio":
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as sound:
            sound.setnchannels(1)
            sound.setsampwidth(2)
            sound.setframerate(48000)
            sound.writeframes(b"\x10\x00" * 48000)
        state["payload"] = buffer.getvalue()
    else:
        state["payload"] = png((64, 64))
        if capability == "upscale":
            (tmp_path / "input.png").write_bytes(png((32, 32)))
            request.update(inputs={"image": "input.png"}, parameters={"scale": 2})
    out = tmp_path / "state"
    out.mkdir()
    first = run_api(request, profile, inputs_root=tmp_path, state_root=out)
    assert first["status"] == "completed"
    receipt = verify_receipt(out / first["bundle"])
    assert receipt["capability"] == capability
    assert receipt["review"] == "not_reviewed"
    assert state["authorization"] == "Bearer synthetic-contract-credential"
    second = run_api(request, profile, inputs_root=tmp_path, state_root=out)
    assert first == second
    assert state["calls"] == 1
    assert state["body"]["prompt"] == request["prompt"]


def test_image_api_rejects_nonimage_bytes(api_server, tmp_path, monkeypatch):
    state, endpoint = api_server
    state["payload"] = b"not an image"
    monkeypatch.setenv("FSF_MODEL_API_KEY", "synthetic-contract-credential")
    request = {"schema_version": "fsf_model_api_request_v1", "request_id": "invalid",
               "prompt": "contract", "parameters": {}, "inputs": {}, "expected_media_contract": None}
    with pytest.raises(RemoteError):
        run_api(request, profile_for("image", endpoint), inputs_root=tmp_path, state_root=tmp_path)
    assert not list(tmp_path.glob("api_*/accepted"))


@pytest.mark.parametrize("capability", ["video", "upscale"])
def test_video_api_decoded_qc_and_upscale(api_server, tmp_path, monkeypatch, capability):
    state, endpoint = api_server
    monkeypatch.setenv("FSF_MODEL_API_KEY", "synthetic-contract-credential")
    ffmpeg, ffprobe = ensure_media_tools_on_path()
    assert ffmpeg and ffprobe, "Real media tools are required for this contract test"

    def video(path, size):
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}x{size}:rate=24", "-frames:v", "24", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)

    output = tmp_path / "response.mp4"
    video(output, 128)
    state["payload"] = output.read_bytes()
    profile = profile_for(capability, endpoint)
    profile["output"].update(media_type="video/mp4", suffix=".mp4")
    profile["http"]["body"] = {"model": "{model}", "prompt": "{prompt}"}
    request = {"schema_version": "fsf_model_api_request_v1", "request_id": "video",
               "prompt": "contract", "parameters": {}, "inputs": {},
               "expected_media_contract": build_expected_contract({"duration_seconds": 1}, width=128, height=128)}
    if capability == "upscale":
        video(tmp_path / "input.mp4", 64)
        request.update(inputs={"video": "input.mp4"}, parameters={"scale": 2})
    out = tmp_path / "state"
    out.mkdir()
    result = run_api(request, profile, inputs_root=tmp_path, state_root=out)
    assert result["status"] == "completed"
    assert verify_receipt(out / result["bundle"])["quality"] == "conforms"
    assert state["calls"] == 1


def request_for(name="request"):
    return {"schema_version":"fsf_model_api_request_v1","request_id":name,"prompt":"contract",
            "parameters":{},"inputs":{},"expected_media_contract":None}


@pytest.mark.parametrize("status,code",[(401,"API_AUTH_FAILED"),(403,"API_AUTH_FAILED"),(429,"API_RATE_LIMITED"),(500,"API_SUBMISSION_UNKNOWN")])
def test_post_failure_never_blindly_retries(api_server,tmp_path,monkeypatch,status,code):
    state,endpoint=api_server;state["http_status"]=status
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-secret")
    profile=profile_for("text",endpoint);request=request_for()
    with pytest.raises(RemoteError,match=code):run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path)
    with pytest.raises(RemoteError,match="API_CONNECTION_PAUSED"):
        run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path)
    changed=dict(request,prompt="changed")
    with pytest.raises(RemoteError,match="REQUEST_ID_CONFLICT"):
        run_api(changed,profile,inputs_root=tmp_path,state_root=tmp_path,recheck=True)
    assert state["calls"]==1


@pytest.mark.parametrize("protocol",["openai_chat_v1","gemini_content_v1"])
def test_native_text_wire_contract(api_server,tmp_path,monkeypatch,protocol):
    state,endpoint=api_server
    template="nebius-text" if protocol=="openai_chat_v1" else "google-text"
    profile=json.loads(resources.files("fsf.remote.templates").joinpath(template+".api-profile.json").read_text())
    profile.update(authorized=True,base_url=endpoint,allow_loopback=True)
    monkeypatch.setenv(profile["key_env"],"synthetic-native-secret")
    state["response"]={"choices":[{"message":{"content":"response"},"finish_reason":"stop"}],"model":"test"} if protocol=="openai_chat_v1" else {"candidates":[{"content":{"parts":[{"text":"response"}]},"finishReason":"STOP"}]}
    request=request_for();request["parameters"]={"max_tokens":16,"temperature":0}
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path)["status"]=="completed"
    if protocol=="openai_chat_v1":assert state["body"]["max_tokens"]==16
    else:assert state["body"]["generationConfig"]["maxOutputTokens"]==16


def test_async_model_api_status_fetch_cancel_and_reconcile(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-secret")
    profile=profile_for("text",endpoint)
    http=profile["http"]
    http.update(job_id_pointer="/job_id",status_pointer="/status",response_mode="text")
    http["routes"].update(status="/jobs/{job_id}",reconcile="/requests/{request_id}",cancel="/jobs/{job_id}/cancel")
    state["response"]={"job_id":"job1"};state["poll_response"]={"status":"running","job_id":"job1"}
    request=request_for()
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path,wait=False)["status"]=="running"
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path,action="status",wait=False)["status"]=="running"
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path,action="reconcile",wait=False)["status"]=="running"
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path,action="cancel")["status"]=="cancel_requested"
    state["poll_response"]={"status":"completed","output":"finished"}
    assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path,action="fetch")["status"]=="completed"
    assert state["calls"]==2  # One submit, one cancel; never a second submit.


def test_bedrock_converse_sdk_contract(tmp_path,monkeypatch):
    boto3=pytest.importorskip("boto3")
    from botocore.stub import Stubber
    client=boto3.client("bedrock-runtime",region_name="us-east-1",aws_access_key_id="synthetic",aws_secret_access_key="synthetic")
    profile=json.loads(resources.files("fsf.remote.templates").joinpath("bedrock-text.api-profile.json").read_text())
    profile.update(authorized=True,model="test-model")
    request=request_for();request["parameters"]={"max_tokens":16,"temperature":0}
    expected={"modelId":"test-model","messages":[{"role":"user","content":[{"text":"contract"}]}],"inferenceConfig":{"maxTokens":16,"temperature":0}}
    response={"output":{"message":{"role":"assistant","content":[{"text":"response"}]}},"stopReason":"end_turn",
              "usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}
    with Stubber(client) as stub:
        stub.add_response("converse",response,expected)
        monkeypatch.setattr(boto3,"client",lambda *a,**k:client)
        assert run_api(request,profile,inputs_root=tmp_path,state_root=tmp_path)["status"]=="completed"
        stub.assert_no_pending_responses()


def test_model_api_executor_real_plan_and_resume(api_server,tmp_path,monkeypatch):
    from fsf.remote.api_executor import ModelAPIExecutor
    from fsf.executors.registry import ExecutorRegistry
    from fsf.runtime.plan_executor import PlanExecutor
    from fsf.remote.contracts import write_json
    state,endpoint=api_server;state["payload"]=png((64,64))
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-secret")
    profile=profile_for("image",endpoint)
    write_json(tmp_path/"profile.json",profile)
    backend=ModelAPIExecutor(executor_id="api.image.test",tool_name="studio.storyboard.first_frame",
        profile=tmp_path/"profile.json",state_root=tmp_path)
    registry=ExecutorRegistry(load_extras=False);registry.register(backend)
    plan={"schema_version":"pdx_execution_plan_v1","request_id":"image_plan",
          "producer":{"type":"fsf_planner","name":"test"},"steps":[{"id":"image","name":"Image","kind":"tool",
          "tool":"studio.storyboard.first_frame","depends_on":[],"inputs":{"shot_id":"S1","shot_spec":{"source_prompt":"test image","duration_seconds":1}},
          "outputs":["artifact:image.png"]}]}
    executor=PlanExecutor(tmp_path/"production",registry=registry,executor_override=backend.executor_id)
    first=executor.execute_plan(plan)
    assert first["success"],first
    assert state["calls"]==1
    second=executor.execute_plan(plan)
    assert second["steps"]["image"]["status"]=="reused"
    assert state["calls"]==1 and sum(executor.generation_dispatches.values())==0


def test_api_secret_response_not_published(api_server,tmp_path,monkeypatch):
    state,endpoint=api_server;state["payload"]=b"synthetic-private-token"
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-private-token")
    with pytest.raises(RemoteError,match="SECRET_OUTPUT"):
        run_api(request_for(),profile_for("text",endpoint),inputs_root=tmp_path,state_root=tmp_path)
    assert not list(tmp_path.glob("api_*/accepted"))


def test_nested_inline_credential_is_rejected_before_network(api_server,tmp_path):
    state,endpoint=api_server
    profile=profile_for("text",endpoint)
    profile["http"]["body"]["options"]={"api_key":"synthetic-inline-secret"}
    with pytest.raises(RemoteError,match="INLINE_CREDENTIAL"):
        run_api(request_for(),profile,inputs_root=tmp_path,state_root=tmp_path)
    assert state["calls"]==0
