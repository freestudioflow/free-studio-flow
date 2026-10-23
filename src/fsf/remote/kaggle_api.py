"""Pinned Kaggle API worker. Provider identifiers remain in private state only."""
import base64
import contextlib
from importlib import resources
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError, MAX_BUNDLE_BYTES, sha, write_json


def _download(url, target):
    import requests
    # Signed storage URLs must still be HTTPS on an explicit platform allowlist.
    for _ in range(4):
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password or not any(host == d or host.endswith("."+d) for d in (
                "kaggle.com", "kaggleusercontent.com", "googleapis.com", "googleusercontent.com")):
            raise RemoteError("REMOTE_DOWNLOAD_HOST")
        response = requests.get(url, stream=True, timeout=(10, 30), allow_redirects=False)
        if response.is_redirect:
            url = response.headers.get("Location", "")
            response.close()
            continue
        response.raise_for_status()
        try:
            if int(response.headers.get("Content-Length", 0)) > MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            with Path(target).open("xb") as stream:
                total = 0
                for block in response.iter_content(1024*1024):
                    total += len(block)
                    if total > MAX_BUNDLE_BYTES:
                        raise RemoteError("REMOTE_SIZE_LIMIT")
                    stream.write(block)
        finally:
            response.close()
        return
    raise RemoteError("REMOTE_REDIRECT_LIMIT")


def call(value):
    directory = Path(value["directory"])
    if value.get("operation") == "submit":
        from fsf.remote.dispatch_hold import refuse_held_submit
        refuse_held_submit(directory)
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    settings = value["settings"]
    owner = os.environ.get(settings.get("owner_env", "KAGGLE_USERNAME")) or api.config_values.get("username")
    if not owner or not re.fullmatch(r"[A-Za-z0-9_-]+", owner):
        raise RemoteError("REMOTE_KAGGLE_OWNER")
    key = value["request_digest"]
    slug = "fsf-worker-"+key[:24]
    ref = owner+"/"+slug
    operation = value["operation"]
    if operation == "diagnose":
        api.kernels_list(mine=True,page_size=1)
        return {"authenticated":True,"reachable":True}
    if operation == "prepare":
        bundle = directory/"request.zip"
        code = "import base64, pathlib, subprocess, sys\n"
        # Only the sealed result belongs in notebook outputs. An isolated venv
        # contains thousands of files and must not become an output dataset.
        code += "root=pathlib.Path('/tmp/fsf-worker');root.mkdir(exist_ok=True)\n"
        code += "bundle=root/'request.zip';bundle.write_bytes(base64.b64decode("+repr(base64.b64encode(bundle.read_bytes()).decode())+"))\n"
        bootstrap = resources.files("fsf.tools").joinpath("remote_bootstrap.py").read_bytes()
        code += "bootstrap=root/'bootstrap.py';bootstrap.write_bytes(base64.b64decode("+repr(base64.b64encode(bootstrap).decode())+"))\n"
        request = read_json(directory/"request.json")
        code += "subprocess.run([sys.executable,str(bootstrap),'--bundle',str(bundle),'--sha256',"+repr(sha(bundle))+",'--out',str(root/'worker'),'--recipe-digest',"+repr(request["recipe"]["recipe_digest"])+",'--result-out','/kaggle/working/result.zip'],check=True,timeout="+str(value["deadline_seconds"])+")\n"
        notebook = {"nbformat":4,"nbformat_minor":5,"metadata":{"kernelspec":{"display_name":"Python 3","language":"python","name":"python3"}},
            "cells":[{"cell_type":"code","metadata":{},"execution_count":None,"outputs":[],"source":code.splitlines(keepends=True)}]}
        write_json(directory/"worker.ipynb", notebook)
        metadata = {"id":ref,"title":slug,"code_file":"worker.ipynb","language":"python","kernel_type":"notebook",
                    "is_private":True,"enable_gpu":False,"enable_tpu":False,"enable_internet":True,
                    "dataset_sources":[],"competition_sources":[],"kernel_sources":[]}
        write_json(directory/"kernel-metadata.json", metadata)
        return {"prepared":True,"is_private":True,"gpu":False}
    if operation == "submit":
        metadata = read_json(directory/"kernel-metadata.json")
        if metadata["is_private"] is not True or metadata["enable_gpu"] is not False:
            raise RemoteError("REMOTE_KAGGLE_POLICY")
        response = api.kernels_push(str(directory), timeout=str(value["deadline_seconds"]))
        if response.error or response.version_number != 1:
            raise RemoteError("REMOTE_KAGGLE_VERSION_AMBIGUOUS")
        return {"kernel":ref,"version":response.version_number,"request_digest":key}
    if operation in {"status", "reconcile"}:
        try:
            response = api.kernels_status(ref)
        except Exception:
            if operation == "reconcile":
                return None
            raise RemoteError("REMOTE_KAGGLE_STATUS") from None
        if operation == "reconcile":
            return {"kernel":ref,"request_digest":key,"version_proof":"unique_job_and_result_binding"}
        raw = str(getattr(response.status,"name",response.status)).lower().rsplit(".",1)[-1]
        status = {"complete":"completed","completed":"completed","running":"running","queued":"queued",
                  "error":"failed","failed":"failed","cancelacknowledged":"failed"}.get(raw, "unknown")
        return {"status":status}
    if operation == "fetch":
        # The pinned SDK's kernels_output ignores the parsed version and reads
        # latest session output. Use a unique notebook per request and require
        # full request/bundle/attempt proof at parent ingest, never just basename.
        from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
        token = None
        matches = []
        for _ in range(4):
            with api.build_kaggle_client() as client:
                request = ApiListKernelSessionOutputRequest()
                request.user_name = owner
                request.kernel_slug = slug
                api._set_paging(request,20,token)
                response = client.kernels.kernels_api_client.list_kernel_session_output(request)
                matches.extend(item for item in response.files or [] if item.file_name == "result.zip")
                token = response.next_page_token
            if not token:
                break
        if token or len(matches) != 1:
            raise RemoteError("REMOTE_KAGGLE_OUTPUT_NOT_READY")
        target = directory/"provider-result.zip"
        target.unlink(missing_ok=True)
        _download(matches[0].url,target)
        return {"downloaded":True}
    raise RemoteError("REMOTE_ACTION")


def main():
    directory = None
    try:
        value = read_json(sys.argv[1])
        directory = Path(value["directory"])
        # SDK chatter may include private references. It is not a public CLI log.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = call(value)
        response = {"ok":True,"result":result}
    except Exception as exc:
        response = {"ok":False,"code":exc.code if isinstance(exc,RemoteError) else "REMOTE_KAGGLE_API_FAILED"}
    if directory is not None:
        write_json(directory/"provider-response.json",response)
    return 0 if response["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
