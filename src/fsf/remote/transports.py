"""Lifecycle adapters. Recipes never implement transport-specific submission."""
import base64
from importlib import resources
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from fsf.core.asset_library import _root
from fsf.core.edit_store import read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError, MAX_BUNDLE_BYTES, digest, sha, write_json, validate_profile
from fsf.remote.bundle import copy_bounded


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RemoteError("REMOTE_REDIRECT_REFUSED")


class Adapter:
    def __init__(self, profile):
        self.profile = validate_profile(profile)
        self.settings = profile["settings"]

    def prepare(self, request, bundle, directory):
        return None

    def cancel(self, request, remote, directory):
        return "unsupported"


class LocalAdapter(Adapter):
    def cancel(self, request, remote, directory):
        root=_root(self.settings["worker_root"])
        current=self.status(request,remote,directory)
        if current=="cancelled":return "confirmed"
        if current in {"completed","failed"}:return "already_finished"
        (root/(request["request_digest"]+".cancel")).write_text("cancel requested",encoding="ascii")
        return "requested"
    def prepare(self, request, bundle, directory):
        _root(self.settings["worker_root"])
        if not Path(self.settings["python"]).is_file():
            raise RemoteError("REMOTE_PYTHON_UNAVAILABLE")

    def submit(self, request, bundle, directory):
        root = _root(self.settings["worker_root"])
        incoming = root/(request["request_digest"]+".zip")
        if not incoming.exists():
            copy_bounded(bundle, incoming)
        elif sha(incoming) != sha(bundle):
            raise RemoteError("REMOTE_BUNDLE_CONFLICT")
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        # Explicitly authorized names only, never the parent's full secret set.
        for name in request["recipe"]["environment"]["credential_env_names"]:
            if name in os.environ:
                env[name] = os.environ[name]
        env["FSF_REMOTE_DEPTH"] = "1"
        args = [self.settings["python"], "-m", "fsf.cli", "remote", "worker", str(incoming), "--out", str(root),
                "--recipe-digest", request["recipe"]["recipe_digest"]]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **options)
        return {"request_digest": request["request_digest"], "pid": proc.pid}

    def reconcile(self, request, directory):
        root = _root(self.settings["worker_root"])
        status = root/request["request_digest"]/"worker-status.json"
        if status.is_file() and read_json(status).get("request_digest") == request["request_digest"]:
            return {"request_digest": request["request_digest"]}
        return None

    def status(self, request, remote, directory):
        path = Path(self.settings["worker_root"])/request["request_digest"]/"worker-status.json"
        # Recover the narrow crash window after sealed result publication but
        # before the last progress update. Parent ingest still verifies it all.
        if (path.parent/"result.zip").is_file() and (path.parent/"result.json").is_file():
            result=read_json(path.parent/"result.json")
            if result.get("request_digest")!=request["request_digest"]:raise RemoteError("REMOTE_WORKER_IDENTITY")
            return result["status"]
        if not path.is_file():
            return "queued"
        value = read_json(path)
        if value.get("request_digest") != request["request_digest"]:
            raise RemoteError("REMOTE_WORKER_IDENTITY")
        return value["status"]

    def fetch(self, request, remote, target, directory):
        source = Path(self.settings["worker_root"])/request["request_digest"]/"result.zip"
        copy_bounded(source, target)


class HttpAdapter(Adapter):
    def cancel(self, request, remote, directory):
        return self._call("POST","/jobs/"+request["request_digest"]+"/cancel",b"")["status"]
    def _call(self, method, suffix, data=None, *, binary=False):
        endpoint = self.settings["endpoint"].rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise RemoteError("REMOTE_ENDPOINT")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise RemoteError("REMOTE_ENDPOINT")
        token = os.environ.get(self.settings["auth_env"])
        if not token:
            raise RemoteError("REMOTE_AUTH_MISSING")
        req = Request(endpoint+suffix, data=data, method=method,
            headers={"Authorization": "Bearer "+token, "Content-Type": "application/zip"})
        try:
            with build_opener(NoRedirect).open(req, timeout=30) as response:
                result = response.read(MAX_BUNDLE_BYTES+1)
                if len(result) > MAX_BUNDLE_BYTES:
                    raise RemoteError("REMOTE_SIZE_LIMIT")
        except Exception as exc:
            if getattr(exc, "code", None) == 429:
                raise RemoteError("REMOTE_RATE_LIMITED") from None
            if isinstance(exc, RemoteError):
                raise
            raise RemoteError("REMOTE_HTTP_FAILED") from None
        if binary:
            return result
        from fsf.core.edit_store import decode_json
        return decode_json(result)

    def submit(self, request, bundle, directory):
        value = self._call("PUT", "/jobs/"+request["request_digest"], Path(bundle).read_bytes())
        if value.get("request_digest") != request["request_digest"]:
            raise RemoteError("REMOTE_PROVIDER_IDENTITY")
        return {"request_digest": request["request_digest"]}

    def reconcile(self, request, directory):
        try:
            value = self._call("GET", "/jobs/"+request["request_digest"])
            return {"request_digest": request["request_digest"]} if value.get("request_digest") == request["request_digest"] else None
        except RemoteError:
            return None

    def status(self, request, remote, directory):
        value = self._call("GET", "/jobs/"+request["request_digest"])
        if value.get("request_digest") != request["request_digest"]:
            raise RemoteError("REMOTE_PROVIDER_IDENTITY")
        return value["status"]

    def fetch(self, request, remote, target, directory):
        Path(target).write_bytes(self._call("GET", "/jobs/"+request["request_digest"]+"/result", binary=True))


class SshAdapter(Adapter):
    def cancel(self, request, remote, directory):
        script="import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('cancel requested');print('requested')"
        self._ssh([self.settings["python"],"-c",script,self._dir(request)+"/worker/"+request["request_digest"]+".cancel"])
        return "requested"
    def _ssh(self, command, timeout=30):
        args = ["ssh", "-F", self.settings["ssh_config"], "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile="+self.settings["known_hosts"], "-o", "ConnectTimeout=10", self.settings["host_alias"],
                " ".join(shlex.quote(str(a)) for a in command)]
        result = run_safe_subprocess(args, timeout_seconds=timeout, output_limit_bytes=65536)
        if result.timed_out or result.returncode:
            raise RemoteError("REMOTE_SSH_FAILED")
        return result.stdout

    def _dir(self, request):
        return self.settings["remote_root"].rstrip("/")+"/"+request["request_digest"]

    def _scp(self, source, destination):
        result = run_safe_subprocess(["scp", "-F", self.settings["ssh_config"], "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile="+self.settings["known_hosts"], str(source), str(destination)], timeout_seconds=120, output_limit_bytes=65536)
        if result.timed_out or result.returncode:
            raise RemoteError("REMOTE_SSH_TRANSFER_FAILED")

    def prepare(self, request, bundle, directory):
        for name in ("known_hosts", "ssh_config"):
            if not Path(self.settings[name]).is_file():
                raise RemoteError("REMOTE_SSH_CONFIG")
        self._ssh([self.settings["python"], "-c", "import sys;print(sys.version_info.major)"])

    def submit(self, request, bundle, directory):
        from fsf.remote.dispatch_hold import refuse_held_submit
        refuse_held_submit(directory)
        remote = self._dir(request)
        # Atomic remote claim: reconnect never repeats the spawn operation.
        self._ssh([self.settings["python"], "-c", "import os,sys;os.makedirs(sys.argv[1],exist_ok=True);os.mkdir(sys.argv[2])",
                   self.settings["remote_root"], remote])
        self._scp(bundle, self.settings["host_alias"]+":"+remote+"/request.zip")
        bootstrap = directory/"bootstrap.py"
        bootstrap.write_bytes(resources.files("fsf.tools").joinpath("remote_bootstrap.py").read_bytes())
        self._scp(bootstrap, self.settings["host_alias"]+":"+remote+"/bootstrap.py")
        args = [self.settings["python"], remote+"/bootstrap.py", "--bundle", remote+"/request.zip", "--sha256", sha(bundle),
                "--out", remote+"/worker", "--recipe-digest", request["recipe"]["recipe_digest"]]
        # Fixed launch wrapper, not a shell command supplied by the workorder.
        launch = "import os,subprocess,sys;os.makedirs(sys.argv[1],exist_ok=True);subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);print('started')"
        self._ssh([self.settings["python"], "-c", launch, remote+"/worker", *args])
        return {"request_digest": request["request_digest"]}

    def reconcile(self, request, directory):
        script = "import os,sys;print('claimed' if os.path.isdir(sys.argv[1]) else 'missing')"
        # A claim proves at least submission was attempted. It does not prove
        # a worker started; status may remain queued/unknown until the deadline.
        return {"request_digest": request["request_digest"]} if "claimed" in self._ssh([self.settings["python"], "-c", script, self._dir(request)]) else None

    def status(self, request, remote, directory):
        path = self._dir(request)+"/worker/"+request["request_digest"]+"/worker-status.json"
        script = "import json,os,sys;p=sys.argv[1];d=os.path.dirname(p);r=os.path.join(d,'result.json');p=r if os.path.isfile(os.path.join(d,'result.zip')) and os.path.isfile(r) else p;print(json.dumps(json.load(open(p)) if os.path.isfile(p) else {'status':'queued','request_digest':sys.argv[2]}))"
        data = json.loads(self._ssh([self.settings["python"], "-c", script, path, request["request_digest"]]))
        if data.get("request_digest") != request["request_digest"]:
            raise RemoteError("REMOTE_WORKER_IDENTITY")
        return data["status"]

    def fetch(self, request, remote, target, directory):
        self._scp(self.settings["host_alias"]+":"+self._dir(request)+"/worker/"+request["request_digest"]+"/result.zip", target)


class KaggleAdapter(Adapter):
    def _call(self, operation, request, directory):
        data = {"operation": operation, "request_digest": request["request_digest"], "directory": str(directory),
                "settings": self.settings, "deadline_seconds": request["policy"]["deadline_seconds"]}
        write_json(directory/"provider-call.json", data)
        from fsf.live.credentials import kaggle_credential_environment
        with kaggle_credential_environment() as env:
            result = run_safe_subprocess([sys.executable, "-m", "fsf.remote.kaggle_api", str(directory/"provider-call.json")],
                                        env=env, inherit_env=False, timeout_seconds=120, output_limit_bytes=65536)
        if result.timed_out or result.returncode:
            raise RemoteError("REMOTE_KAGGLE_API_FAILED")
        value = read_json(directory/"provider-response.json")
        if not value.get("ok"):
            raise RemoteError(value.get("code", "REMOTE_KAGGLE_API_FAILED"))
        return value["result"]

    def prepare(self, request, bundle, directory):
        if request["policy"]["gpu"]:
            raise RemoteError("REMOTE_KAGGLE_CPU_ONLY")
        import importlib.metadata
        if importlib.metadata.version("kaggle") != "2.2.4":
            raise RemoteError("REMOTE_KAGGLE_SDK_VERSION")
        self._call("prepare", request, directory)

    def submit(self, request, bundle, directory):
        return self._call("submit", request, directory)

    def reconcile(self, request, directory):
        return self._call("reconcile", request, directory)

    def status(self, request, remote, directory):
        return self._call("status", request, directory)["status"]

    def fetch(self, request, remote, target, directory):
        self._call("fetch", request, directory)
        copy_bounded(directory/"provider-result.zip", target)


def adapter_for(profile):
    return {"local": LocalAdapter, "http": HttpAdapter, "ssh": SshAdapter, "kaggle": KaggleAdapter}[profile["transport"]](profile)
