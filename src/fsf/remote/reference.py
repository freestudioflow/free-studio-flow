"""Loopback-only authenticated async reference handler, not a cloud service."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import tempfile

from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock, read_json
from fsf.remote.contracts import RemoteError, MAX_BUNDLE_BYTES, check_authorization, sha
from fsf.remote.bundle import unpack, read_bundle, copy_bounded
from fsf.remote.transports import LocalAdapter


def make_server(profile, *, state_root, worker_root, python, port=0):
    state = _root(state_root)
    _root(worker_root)
    secret = os.environ.get(profile["settings"]["auth_env"])
    if not secret:
        raise RemoteError("REMOTE_AUTH_MISSING")
    local = dict(profile,transport="local",settings={"worker_root":str(worker_root),"python":str(python)})
    adapter = LocalAdapter(local)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,code,value):
            import json
            data=json.dumps(value).encode()
            self.send_response(code);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)

        def key(self):
            if not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+secret):
                self.send(403,{"code":"REMOTE_AUTH_FAILED"});return None
            match=re.fullmatch(r"/jobs/([0-9a-f]{64})(/(result|cancel))?",self.path)
            if not match:
                self.send(404,{"code":"REMOTE_NOT_FOUND"});return None
            return match

        def do_PUT(self):
            match=self.key()
            if not match:return
            import shutil
            scratch=None
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=MAX_BUNDLE_BYTES or match[2]:raise RemoteError("REMOTE_SIZE_LIMIT")
                scratch=Path(tempfile.mkdtemp(prefix=".http-",dir=state))
                bundle=scratch/"request.zip"
                with bundle.open("xb") as output:
                    remaining=size
                    while remaining:
                        block=self.rfile.read(min(1024*1024,remaining))
                        if not block:raise RemoteError("REMOTE_UPLOAD")
                        output.write(block);remaining-=len(block)
                folder=scratch/"contents";folder.mkdir()
                request=read_bundle(folder,unpack(bundle,folder))
                if request["request_digest"]!=match[1]:raise RemoteError("REMOTE_PROVIDER_IDENTITY")
                check_authorization(request,profile)
                directory=state/match[1]
                with edit_lock(state/(match[1]+".lock-target")):
                    if directory.exists():
                        if sha(directory/"request.zip")!=sha(bundle):raise RemoteError("REMOTE_INTENT_CONFLICT")
                    else:
                        directory.mkdir()
                        copy_bounded(bundle,directory/"request.zip")
                        from fsf.remote.contracts import write_json
                        write_json(directory/"request.json",request)
                        # A durable claim precedes spawn. A server crash here is
                        # unknown and does not authorize another spawn on PUT.
                        adapter.submit(request,directory/"request.zip",directory)
                self.send(202,{"request_digest":match[1]})
            except Exception:
                self.send(409,{"code":"REMOTE_UPLOAD_REJECTED"})
            finally:
                if scratch:shutil.rmtree(scratch)

        def do_GET(self):
            if self.path=="/capabilities":
                if not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+secret):
                    self.send(403,{"code":"REMOTE_AUTH_FAILED"})
                else:self.send(200,{"protocol_version":"1","async":True,"cancel":"cooperative"})
                return
            match=self.key()
            if not match:return
            if match[2] not in (None,"/result"):self.send(404,{"code":"REMOTE_NOT_FOUND"});return
            directory=state/match[1]
            if not directory.exists():self.send(404,{"code":"REMOTE_NOT_FOUND"});return
            request=read_json(directory/"request.json")
            if match[2]:
                path=Path(worker_root)/match[1]/"result.zip"
                if not path.is_file():self.send(409,{"code":"REMOTE_NOT_READY"});return
                size=path.stat().st_size
                if size>MAX_BUNDLE_BYTES:self.send(409,{"code":"REMOTE_SIZE_LIMIT"});return
                self.send_response(200);self.send_header("Content-Length",str(size));self.end_headers()
                with path.open("rb") as stream:
                    while block:=stream.read(1024*1024):self.wfile.write(block)
            else:self.send(200,{"request_digest":match[1],"status":adapter.status(request,{},directory)})

        def do_POST(self):
            match=self.key()
            if not match:return
            directory=state/match[1]
            if match[2]!="/cancel" or not directory.exists():self.send(404,{"code":"REMOTE_NOT_FOUND"});return
            request=read_json(directory/"request.json")
            self.send(202,{"status":adapter.cancel(request,{},directory)})

    server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
    server.daemon_threads=True
    return server
