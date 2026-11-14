"""Loopback data plane tests; the endpoint is a fixture, not a model provider."""
import hashlib
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import subprocess
import sys
import stat
import threading
import time
import zipfile

import pytest

from fsf.remote import streaming
from fsf.remote.contracts import RemoteError
from fsf.remote.model_api import _http, run_api, validate_profile, verify_receipt
from test_model_api_capabilities import png, profile_for, request_for

MIB = 1024**2
GIB = 1024*MIB
LARGE = pytest.mark.skipif(os.environ.get("FSF_STREAM_BOUNDARY_TESTS") != "1",
                           reason="opt in to GiB-scale loopback transfers")


@pytest.fixture(autouse=True)
def controlled_resource_sample(monkeypatch):
    # Keep protocol assertions independent of unrelated desktop memory pressure.
    # The data plane still uses real sockets, files, hashing, and disk admission.
    from fsf.executors import resource_sample
    native = resource_sample.sample

    def sample():
        row = native()
        row["host_available_bytes"] = row["host_total_bytes"]
        row["host_commit_available_bytes"] = row["host_commit_limit_bytes"]
        row["cgroup"] = None
        return row

    monkeypatch.setattr(resource_sample,"sample",sample)


def limits(**changes):
    return {"schema_version":"fsf_resource_limits_v1",
            **dict.fromkeys(streaming.FIELDS, 4*MIB),
            "manifest_line_bytes":65536, "seconds":60, "disk_reserve_bytes":1,
            "process_tree_rss_bytes":512*MIB, "process_tree_commit_bytes":768*MIB, **changes}


def test_unpack_cli_publishes_only_verified_bytes(tmp_path):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("nested/payload.bin", b"verified bytes")
    policy = tmp_path / "limits.json"
    policy.write_text(json.dumps(limits(seconds=15)))
    destination = tmp_path / "output"
    args = [sys.executable, "-m", "fsf.cli", "api", "unpack", str(archive),
            "--destination", str(destination), "--limits", str(policy), "--sha256"]
    bad = subprocess.run(args + ["0"*64], capture_output=True, timeout=30)
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["error"]["code"] == "STREAM_SHA_MISMATCH"
    assert not destination.exists()
    good = subprocess.run(args + [hashlib.sha256(archive.read_bytes()).hexdigest()],
                          capture_output=True, timeout=30)
    assert good.returncode == 0, good.stdout
    assert json.loads(good.stdout)["result"]["unpacked_bytes"] == 14
    assert (destination / "nested/payload.bin").read_bytes() == b"verified bytes"


@pytest.fixture
def endpoint():
    payload = png((8, 8))
    state = {"puts":0, "posts":0, "gets":0, "payload":payload,
             "sha":hashlib.sha256(payload).hexdigest()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_PUT(self):
            state["puts"] += 1
            count = int(self.headers["Content-Length"])
            digest = hashlib.sha256()
            while count:
                block = self.rfile.read(min(count, MIB))
                if not block:
                    return
                digest.update(block)
                count -= len(block)
            state["upload_sha"] = digest.hexdigest()
            state["upload_header"] = self.headers["X-Content-SHA256"]
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            state["posts"] += 1
            state["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps({"output":url+"/output", "sha256":state["sha"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state["gets"] += 1
            if state.get("redirect"):
                self.send_response(302)
                self.send_header("Location", url+"/never")
                self.end_headers()
                return
            self.send_response(200)
            for length in state.get("lengths", []):
                self.send_header("Content-Length", length)
            self.end_headers()
            self.wfile.write(state["payload"])
            pad = state.get("pad", 0)
            block = bytes(MIB)
            while pad:
                size = min(MIB, pad)
                self.wfile.write(block[:size])
                pad -= size

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def profile(url, **changes):
    value = profile_for("image", url)
    value.pop("input_max_bytes", None)
    value.pop("request_max_bytes", None)
    value.update(schema_version="fsf_model_api_profile_v3", upload_prefix="/objects",
                 output_sha256_pointer="/sha256", limits=limits(**changes),
                 artifact_hosts=["127.0.0.1"], key_env=None)
    value["http"].update(auth_header="none", response_mode="url",
                         body={"id":"{request_id}", "ref":"{inputs.source.url}", "sha":"{inputs.source.sha256}"})
    return value


def test_streaming_api_validates_and_reuses_upload(endpoint, tmp_path):
    state, url = endpoint
    source = b"x" * (2*MIB+1)
    (tmp_path/"source").write_bytes(source)
    request = dict(request_for(), inputs={"source":"source"})
    cfg = profile(url, input_bytes=len(source), http_request_bytes=len(source))
    first = run_api(request, cfg, inputs_root=tmp_path, state_root=tmp_path)
    assert first == run_api(request, cfg, inputs_root=tmp_path, state_root=tmp_path)
    assert state["puts"] == state["posts"] == state["gets"] == 1
    assert state["body"]["ref"].startswith(url+"/objects/"+first["request_digest"]+"/")
    assert state["upload_header"] == state["upload_sha"] == state["body"]["sha"] == hashlib.sha256(source).hexdigest()
    assert verify_receipt(tmp_path/first["bundle"])["artifact"]["sha256"] == state["sha"]
    with pytest.raises(RemoteError, match="API_REQUEST_ID_CONFLICT"):
        run_api(request, profile(url), inputs_root=tmp_path, state_root=tmp_path)
    assert state["posts"] == 1


@pytest.mark.parametrize("field,code", [("input_bytes","API_INPUT_LIMIT"),
                                       ("http_request_bytes","API_REQUEST_LIMIT")])
def test_upload_limits_are_independent(endpoint, tmp_path, field, code):
    state, url = endpoint
    (tmp_path/"source").write_bytes(b"x"*129)
    request = dict(request_for(), inputs={"source":"source"})
    with pytest.raises(RemoteError, match=code):
        run_api(request, profile(url, **{field:128}), inputs_root=tmp_path, state_root=tmp_path)
    assert state["puts"] == state["posts"] == 0


@pytest.mark.parametrize("fault,code", [
    ({"lengths":["-1"]},"STREAM_LENGTH_INVALID"),
    ({"lengths":["200","201"]},"STREAM_LENGTH_INVALID"),
    ({"lengths":["10000"]},"STREAM_TRUNCATED"),
    ({"sha":"0"*64},"STREAM_SHA_MISMATCH"),
    ({"redirect":True},"REMOTE_REDIRECT_REFUSED"),
])
def test_stream_errors_never_publish(endpoint, tmp_path, fault, code):
    state, url = endpoint
    state.update(fault)
    (tmp_path/"source").write_bytes(b"x")
    with pytest.raises(RemoteError, match=code):
        run_api(dict(request_for(),inputs={"source":"source"}), profile(url),
                inputs_root=tmp_path, state_root=tmp_path)
    assert state["gets"] == 1
    assert not list(tmp_path.rglob("accepted"))
    assert not list(tmp_path.rglob(".stream-*"))


@pytest.mark.parametrize("field,maximum", [
    ("input_max_bytes",64*MIB), ("request_max_bytes",128*MIB), ("output",64*1024*MIB)])
@pytest.mark.parametrize("delta", [-1,0,1])
def test_legacy_profile_boundaries(field, maximum, delta):
    cfg = profile_for("image", "https://example.invalid")
    cfg.update(schema_version="fsf_model_api_profile_v2", input_max_bytes=64*MIB,
               request_max_bytes=128*MIB)
    cfg["http"]["response_mode"] = "url"
    if field == "output":
        cfg["output"]["max_bytes"] = maximum+delta
    else:
        cfg[field] = maximum+delta
    if delta > 0:
        with pytest.raises(RemoteError, match="API_SCHEMA"):
            validate_profile(cfg)
    else:
        assert validate_profile(cfg) == cfg


def test_stream_bound_and_cancel_cleanup(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        headers = Message()

    for delta in (-1,0,1):
        response = Response(b"x"*(16+delta))
        if delta > 0:
            with pytest.raises(RemoteError, match="STREAM_SIZE_LIMIT"):
                streaming.download(response,tmp_path,limits(),time.monotonic()+2,None,maximum=16)
        else:
            path = streaming.download(response,tmp_path,limits(),time.monotonic()+2,None,maximum=16)
            assert path.stat().st_size == 16+delta
            path.unlink()
    with pytest.raises(RemoteError, match="STREAM_DISK_LIMIT"):
        streaming.download(Response(b"xx"),tmp_path,limits(disk_bytes=1),time.monotonic()+2,None,maximum=16)
    def cancelled(_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(Response,"read1",lambda self,size:cancelled(size))
    with pytest.raises(KeyboardInterrupt):
        streaming.download(Response(b"x"),tmp_path,limits(),time.monotonic()+2,None,maximum=16)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("fault", ["none","traversal","symlink","duplicate","expand","sha"])
def test_archive_publish_is_atomic(tmp_path, fault):
    source = tmp_path/"artifact.zip"
    with zipfile.ZipFile(source,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        member = zipfile.ZipInfo("../escape" if fault=="traversal" else "member")
        member.compress_type = zipfile.ZIP_DEFLATED
        if fault=="symlink":
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, b"x"*MIB)
        if fault=="duplicate":
            archive.writestr("MEMBER",b"conflict")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path/"accepted"
    cfg = limits(archive_unpacked_bytes=MIB-1 if fault=="expand" else MIB)
    if fault=="none":
        receipt = streaming.extract_archive(source,destination,cfg,checksum)
        assert receipt["unpacked_bytes"] == (destination/"member").stat().st_size == MIB
    else:
        with pytest.raises(RemoteError,match="STREAM_"):
            streaming.extract_archive(source,destination,cfg,"0"*64 if fault=="sha" else checksum)
        assert not destination.exists()
        assert not list(tmp_path.glob(".stream-archive-*"))


@LARGE
@pytest.mark.parametrize("delta", [-1,0,1])
def test_gib_input_through_real_consumer(endpoint, tmp_path, delta):
    state, url = endpoint
    with (tmp_path/"source").open("wb") as file:
        file.truncate(GIB+delta)
    cfg = profile(url, input_bytes=GIB, http_request_bytes=GIB, seconds=180)
    cfg["timeout_seconds"] = 180
    request = dict(request_for(), inputs={"source":"source"})
    if delta > 0:
        with pytest.raises(RemoteError,match="API_INPUT_LIMIT"):
            run_api(request,cfg,inputs_root=tmp_path,state_root=tmp_path)
        assert state["puts"] == state["posts"] == 0
    else:
        receipt = run_api(request,cfg,inputs_root=tmp_path,state_root=tmp_path)
        assert receipt["status"] == "completed"
        assert state["puts"] == state["posts"] == 1
        assert state["upload_sha"] == state["upload_header"] == state["body"]["sha"]


@LARGE
@pytest.mark.parametrize("maximum,delta", [(16*MIB,-1),(16*MIB,0),(16*MIB,1),
                                           (64*MIB,0),(256*MIB,-1),(256*MIB,0),(256*MIB,1)])
def test_real_url_download_boundaries(endpoint,tmp_path,maximum,delta):
    state,url = endpoint
    total = maximum+delta
    state["pad"] = total-len(state["payload"])
    digest = hashlib.sha256(state["payload"])
    for start in range(0,state["pad"],MIB):
        digest.update(bytes(min(MIB,state["pad"]-start)))
    state["sha"] = digest.hexdigest()
    # The fixture's trailing bytes exercise byte transport. Image decoding
    # validates the PNG prefix and is separate from transport SHA verification.
    cfg = profile(url,output_bytes=maximum,disk_bytes=maximum,seconds=180)
    cfg["http"]["body"] = {"id":"{request_id}"}
    cfg["output"]["max_bytes"] = maximum
    cfg["timeout_seconds"] = 180
    if delta > 0:
        with pytest.raises(RemoteError,match="STREAM_SIZE_LIMIT"):
            run_api(request_for(),cfg,inputs_root=tmp_path,state_root=tmp_path)
        assert not list(tmp_path.rglob("accepted"))
        assert not list(tmp_path.rglob(".stream-*"))
    else:
        import tracemalloc
        tracemalloc.start()
        try:
            result = run_api(request_for(),cfg,inputs_root=tmp_path,state_root=tmp_path)
            receipt = verify_receipt(tmp_path/result["bundle"])
            assert receipt["artifact"]["sha256"] == state["sha"]
            assert receipt["artifact"]["size_bytes"] == total
            assert tracemalloc.get_traced_memory()[1] < 16*MIB
        finally:
            tracemalloc.stop()


def test_legacy_wire_body_utf8_and_base64_boundary(endpoint,monkeypatch):
    import base64
    from fsf.remote.model_api import _body
    state,url = endpoint
    cfg = profile_for("image",url)
    cfg.update(schema_version="fsf_model_api_profile_v2",input_max_bytes=4096)
    monkeypatch.setenv("FSF_MODEL_API_KEY","synthetic-contract-credential")
    template = {"source":"{inputs.source.base64}", "prompt":"例"}
    shell = len(json.dumps(_body(template,{"inputs.source.base64":""})).encode())
    cap = shell + 4*128
    for size in (383,384,385):
        data = _body(template,{"inputs.source.base64":base64.b64encode(bytes(size)).decode()})
        count = len(json.dumps(data).encode())
        assert count == shell + 4*((size+2)//3)
        cfg["request_max_bytes"] = cap
        if size == 385:
            with pytest.raises(RemoteError,match="API_INPUT_LIMIT"):
                _http(cfg,"POST","/submit",data,deadline=time.monotonic()+5)
        else:
            _http(cfg,"POST","/submit",data,deadline=time.monotonic()+5)
    for delta in (-1,0,1):
        cfg["request_max_bytes"] = cap+delta
        if delta < 0:
            with pytest.raises(RemoteError,match="API_INPUT_LIMIT"):
                _http(cfg,"POST","/submit",data := _body(template,{"inputs.source.base64":"A"*512}),
                      deadline=time.monotonic()+5)
        else:
            _http(cfg,"POST","/submit",data,deadline=time.monotonic()+5)
    assert state["posts"] == 4


def test_64_gib_counter_boundary_without_real_transfer(monkeypatch):
    cap = 64*GIB
    class Counter:
        def __init__(self,total):
            self.total = total
        def read(self,size):
            # Virtual blocks provide only the byte count to the stub digest.
            count = min(size,self.total)
            self.total -= count
            return range(count)
    class Digest:
        def update(self,_):
            pass
        def hexdigest(self):
            return "0"*64
    monkeypatch.setattr(streaming.hashlib,"sha256",Digest)
    for delta in (-1,0,1):
        args = {"maximum":cap, "deadline":time.monotonic()+10}
        if delta > 0:
            with pytest.raises(RemoteError,match="STREAM_SIZE_LIMIT"):
                streaming.copy_stream(Counter(cap+delta),None,**args)
        else:
            assert streaming.copy_stream(Counter(cap+delta),None,**args)["size_bytes"] == cap+delta
