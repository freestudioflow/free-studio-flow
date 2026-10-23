"""Approved recipe runner. This is a trusted worker, not a hostile-code sandbox."""
import copy
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import time

from fsf import __version__
from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock, read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import (RemoteError, digest, sha, seal, shape, write_json, safe_path, file_item, reject_credential_bytes)
from fsf.remote.bundle import unpack, pack, read_bundle, bundle_identity


def child_identity(request):
    child = request["child"]
    return {"job_id": child["job_id"], "run_id": child["run_id"], "attempt_id": child["attempt_id"],
            "plan_digest": digest(child["plan"])}


def run_worker(bundle, *, output_root, approved_recipe, remaining_seconds=None):
    root = _root(output_root)
    scratch = Path(tempfile.mkdtemp(prefix=".worker-open-", dir=root))
    try:
        names = unpack(bundle, scratch)
        request = read_bundle(scratch, names)
        if request["recipe"]["recipe_digest"] != approved_recipe or sha(Path(__file__)) != request["worker_core_sha256"]:
            raise RemoteError("REMOTE_WORKER_VERSION")
        key = request["request_digest"]
        directory = root/key
        with edit_lock(root/(key+".lock-target")):
            if not directory.exists():
                os.replace(scratch, directory)
            else:
                read_bundle(directory)
            result_path = directory/"result.zip"
            if result_path.exists():
                # Parent performs full independent verification on every fetch.
                old=read_json(directory/"result.json")
                return {"status": old["status"], "request_digest": key, "error_code":old.get("error_code"), "result_sha256": sha(result_path)}
            status_path = directory/"worker-status.json"
            journal_path = directory/"child-journal.json"
            started = time.time()
            journal = read_json(journal_path) if journal_path.exists() else {
                "schema_version": "fsf_remote_child_journal_v1", "request_digest": key,
                "child": child_identity(request), "plan_digest": digest(request["child"]["plan"]), "steps": {}}
            if journal.get("request_digest") != key or journal.get("child") != child_identity(request):
                raise RemoteError("REMOTE_CHILD_JOURNAL")
            write_json(status_path, {"status": "running", "request_digest": key, "pid": os.getpid()})
            budget = request["policy"]["deadline_seconds"]
            if remaining_seconds is not None:
                if type(remaining_seconds) is not int or remaining_seconds < 1:
                    raise RemoteError("REMOTE_DEADLINE_EXCEEDED")
                budget = min(budget,remaining_seconds)
            deadline = time.monotonic()+budget
            recipe = request["recipe"]
            environment = {}
            env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
            missing_credentials=[]
            for name in recipe["environment"]["credential_env_names"]:
                if name not in request["policy"]["credential_env_names"] or name not in os.environ:
                    missing_credentials.append(name)
                else:env[name] = os.environ[name]
            env.update(FSF_REMOTE_DEPTH="1", OMP_NUM_THREADS=str(request["policy"]["cpu"]),
                       OPENBLAS_NUM_THREADS=str(request["policy"]["cpu"]))
            from fsf.remote.environment import INSTALL_NETWORK, install_environment
            # Host-managed installation settings must not become job credentials.
            for name in INSTALL_NETWORK:
                env.pop(name, None)
            replacements = {"python": sys.executable}
            for tool in ("ffmpeg", "blender"):
                value = shutil.which(tool)
                if value:
                    replacements[tool] = value
            replacements.update({"param."+k: str(v) for k,v in request["parameters"].items()})
            for item in request["inputs"]:
                replacements["input."+item["artifact_id"]] = str(safe_path(directory, item["path"]))
            for step in recipe["steps"]:
                for item in step["outputs"]:
                    replacements["output."+item["artifact_id"]] = str(safe_path(directory/"outputs", item["path"], exists=False))
            write_json(directory/"job-request.json", request)
            replacements["request"] = str(directory/"job-request.json")

            def resolve(args):
                result = []
                for arg in args:
                    def replace(match):
                        if match[1] not in replacements:
                            raise RemoteError("REMOTE_PLACEHOLDER")
                        return replacements[match[1]]
                    value = re.sub(r"\{([A-Za-z0-9_.-]+)\}", replace, arg)
                    if "{" in value or "}" in value:
                        raise RemoteError("REMOTE_PLACEHOLDER")
                    result.append(value)
                return result

            def run(args, *, installation=False):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise RemoteError("REMOTE_DEADLINE_EXCEEDED")
                effective_env = install_environment(env, request["policy"]["credential_env_names"]) if installation else env
                res = run_safe_subprocess(resolve(args), cwd=directory, env=effective_env, inherit_env=False,
                    timeout_seconds=remaining, output_limit_bytes=1024*1024,
                    cancel_check=lambda:(root/(key+".cancel")).exists())
                if res.timed_out:
                    raise RemoteError("REMOTE_DEADLINE_EXCEEDED")
                if res.returncode:
                    if installation and any(name in request['policy']['credential_env_names'] for name in INSTALL_NETWORK):
                        from fsf.remote.environment import installation_error
                        raise RemoteError(installation_error(res.stderr))
                    raise RemoteError("REMOTE_COMMAND_FAILED")
                size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                if size > request["policy"]["disk_mb"]*1024*1024:
                    raise RemoteError("REMOTE_DISK_LIMIT")
                return res

            error = None
            artifacts = []
            try:
                if missing_credentials:
                    raise RemoteError("REMOTE_CREDENTIAL_MISSING")
                if recipe["environment"]["os"] not in {"any", platform.system().lower()}:
                    raise RemoteError("REMOTE_OS_UNSUPPORTED")
                if not platform.python_version().startswith(recipe["environment"]["python"]):
                    raise RemoteError("REMOTE_PYTHON_VERSION")
                phase = time.monotonic()
                for setup in recipe["environment"]["setup"]:
                    if setup["source"] not in request["policy"]["network_sources"]:
                        raise RemoteError("REMOTE_NETWORK_NOT_AUTHORIZED")
                    run(setup["args"], installation=True)
                    if 'fsf.remote.environment' in setup['args']:
                        from fsf.remote.contracts import check_digest
                        args = setup['args']
                        pinned = args[args.index('--digest')+1]
                        if not re.fullmatch('[0-9a-f]{64}',pinned):
                            raise RemoteError('REMOTE_ENVIRONMENT_DIGEST')
                        receipt = read_json(safe_path(directory,'.prepared-tools/'+pinned+'/receipt.json'))
                        check_digest(receipt,'receipt_digest')
                        if receipt.get('environment_digest') != pinned:
                            raise RemoteError('REMOTE_ENVIRONMENT_DIGEST')
                        environment.setdefault('prepared_tools',[]).append({
                            'environment_digest':pinned,'receipt_digest':receipt['receipt_digest'],'tools':receipt['tools']})
                environment["setup_seconds"] = time.monotonic()-phase
                environment["python"] = platform.python_version()
                import importlib.metadata
                environment["packages"] = {d.metadata["Name"]:d.version for d in importlib.metadata.distributions() if d.metadata.get("Name")}
                environment["models"] = []
                for model in recipe["environment"]["models"]:
                    actual=sha(safe_path(directory,model["path"]))
                    if actual!=model["sha256"]:
                        raise RemoteError("REMOTE_MODEL_INTEGRITY")
                    environment["models"].append({"model_id":model["model_id"],"revision":model["revision"],"sha256":actual})
                environment["probes"] = []
                for probe in recipe["environment"]["probes"]:
                    res = run([probe["program"], *probe["args"]])
                    match = re.search(probe["pattern"], res.stdout)
                    if not match:
                        raise RemoteError("REMOTE_ENVIRONMENT_MISMATCH")
                    environment["probes"].append({"version": match[0]})
                for step in recipe["steps"]:
                    sid = step["step_id"]
                    prior = journal["steps"].get(sid)
                    if prior:
                        if prior.get("status") != "completed":
                            raise RemoteError("REMOTE_EXECUTION_UNKNOWN")
                        for item in prior["artifacts"]:
                            p = safe_path(directory/"outputs", item["path"])
                            if file_item(p, item["artifact_id"], item["path"], item["role"], item["media_type"]) != item:
                                raise RemoteError("REMOTE_OUTPUT_CHANGED")
                        artifacts.extend(prior["artifacts"])
                        continue
                    journal["steps"][sid] = {"status": "running", "recipe_step_digest": digest(step)}
                    write_json(journal_path, journal)
                    phase = time.monotonic()
                    run(step["args"])
                    actual = [file_item(safe_path(directory/"outputs", a["path"]), a["artifact_id"], a["path"], a["role"], a["media_type"]) for a in step["outputs"]]
                    for item in actual:
                        reject_credential_bytes(directory/"outputs"/item["path"],
                            [env.get(name) for name in recipe["environment"]["credential_env_names"]])
                    journal["steps"][sid] = {"status": "completed", "recipe_step_digest": digest(step),
                        "artifacts": actual, "elapsed_seconds": time.monotonic()-phase}
                    artifacts.extend(actual)
                    write_json(journal_path, journal)
                # Verify that the job did not change its copied inputs or request.
                read_bundle(directory)
            except KeyboardInterrupt:
                error = "REMOTE_CANCELLED"
            except Exception as exc:
                error = exc.code if isinstance(exc, RemoteError) else "REMOTE_WORKER_FAILED"
            ack = None
            consistency_ack = None
            if not error and (request["video_condition"] or request["consistency_requirements"]):
                try:
                    report = read_json(safe_path(directory/"outputs", recipe["acknowledgement_output"]))
                    ack = report.get("condition_acknowledgement")
                    consistency_ack = report.get("consistency_acknowledgement")
                    if request["video_condition"]:
                        from fsf.runtime.blender import validate_condition_ack
                        validate_condition_ack(request["video_condition"], recipe["condition_capability"], ack)
                except Exception:
                    error = "REMOTE_ACK_MISSING_OR_INVALID"
            write_json(directory/"environment.json", environment)
            final_status = "cancelled" if error == "REMOTE_CANCELLED" else "failed" if error else "completed"
            journal["status"] = final_status
            journal["elapsed_seconds"] = time.time()-started
            write_json(journal_path, journal)
            manifest = seal({"schema_version": "fsf_remote_result_v1", "request_digest": key,
                "bundle_digest": bundle_identity(request), "parent": request["parent"], "child": child_identity(request),
                "fsf_version": __version__, "status": final_status, "error_code": error,
                "artifacts": [] if error else artifacts, "child_journal_sha256": sha(journal_path),
                "environment_digest": digest(environment), "condition_acknowledgement": ack,
                "consistency_acknowledgement": consistency_ack}, "result_digest")
            shape(manifest, "result")
            write_json(directory/"result.json", manifest)
            files = ["result.json", "child-journal.json", "environment.json"]
            files += ["outputs/"+a["path"] for a in manifest["artifacts"]]
            temp_result = directory/".result-pending.zip"
            temp_result.unlink(missing_ok=True)
            pack(directory, files, temp_result)
            os.replace(temp_result, result_path)
            write_json(status_path, {"status": final_status, "request_digest": key,
                                     "result_sha256": sha(result_path)})
            return {"status": final_status, "request_digest": key, "error_code": error}
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
