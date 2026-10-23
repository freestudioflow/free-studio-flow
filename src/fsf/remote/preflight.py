"""Read-only preflight: declarations, authorization and observed readiness differ."""
import json
import os
from pathlib import Path
import tempfile
from importlib import resources

from jsonschema import Draft202012Validator

from fsf.remote.contracts import (
    RemoteError, digest, validate_recipe, validate_profile, reject_inline_credentials,
    safe_path, sha, MAX_BUNDLE_BYTES,
)

_DEFINITION_KEYS = {"parent", "parameters", "inputs", "video_condition",
                    "consistency_requirements", "expected_media_contract"}
_SAFE_CODES = frozenset({
    "REMOTE_DEFINITION", "REMOTE_SCHEMA", "REMOTE_CONTRACT_DIGEST", "REMOTE_QC_CONTRACT_REQUIRED",
    "REMOTE_QC_CONTRACT_INVALID", "REMOTE_REQUIREMENTS_DIGEST", "REMOTE_PARAMETERS", "REMOTE_INPUTS",
    "REMOTE_NOT_AUTHORIZED", "REMOTE_RECIPE_NOT_APPROVED", "REMOTE_GPU_NOT_AUTHORIZED",
    "REMOTE_KAGGLE_CPU_ONLY", "REMOTE_NETWORK_NOT_AUTHORIZED", "REMOTE_CREDENTIAL_NOT_AUTHORIZED",
    "REMOTE_CREDENTIAL_MISSING", "REMOTE_STALE_WHEEL", "REMOTE_WHEEL", "REMOTE_SIZE_LIMIT",
    "REMOTE_CONDITION_INVALID", "REMOTE_CONDITION_REQUIRED",
    "REMOTE_FILE_MISSING", "REMOTE_PATH", "REMOTE_PROFILE", "REMOTE_INTEGRITY", "REMOTE_VERSION",
    "REMOTE_RUNTIME_UNAVAILABLE", "REMOTE_PYTHON_VERSION", "REMOTE_OS_UNSUPPORTED",
    "REMOTE_HTTP_FAILED", "REMOTE_AUTH_MISSING", "REMOTE_SSH_FAILED", "REMOTE_SSH_CONFIG",
    "REMOTE_KAGGLE_API_FAILED", "REMOTE_KAGGLE_SDK_VERSION", "REMOTE_PROBE_FAILED",
    "REMOTE_RATE_LIMITED", "REMOTE_SECRET_ARGUMENT", "REMOTE_UNPINNED_MODEL", "REMOTE_UNPINNED_SETUP",
})


def _code(exc, fallback):
    return exc.code if isinstance(exc, RemoteError) and exc.code in _SAFE_CODES else fallback


def validate_definition(definition, recipe):
    """Reject unusable declarations before packaging or any provider submit."""
    recipe = validate_recipe(recipe)
    try:
        reject_inline_credentials(definition)
        json.dumps(definition, allow_nan=False)
        if not isinstance(definition, dict) or set(definition) != _DEFINITION_KEYS:
            raise RemoteError("REMOTE_DEFINITION")
        schema = json.loads(resources.files("fsf.schemas").joinpath("fsf_remote_request_v1.schema.json").read_text("utf-8"))
        if not Draft202012Validator(schema["properties"]["parent"]).is_valid(definition["parent"]):
            raise RemoteError("REMOTE_DEFINITION")
        contract = definition["expected_media_contract"]
        if definition["parent"]["expected_contract_digest"] != (digest(contract) if contract is not None else None):
            raise RemoteError("REMOTE_CONTRACT_DIGEST")
        video = any(a["role"] == "video" for step in recipe["steps"] for a in step["outputs"])
        if video and not contract:
            raise RemoteError("REMOTE_QC_CONTRACT_REQUIRED")
        if contract is not None:
            from fsf.conformance.media import validate_media_expected
            try:
                validate_media_expected(contract)
            except (ValueError, TypeError):
                raise RemoteError("REMOTE_QC_CONTRACT_INVALID") from None
        requirements = definition["consistency_requirements"]
        if requirements is not None and not isinstance(requirements, dict):
            raise RemoteError("REMOTE_REQUIREMENTS_DIGEST")
        if definition["parent"]["requirements_digest"] != (requirements.get("requirements_digest") if requirements else None):
            raise RemoteError("REMOTE_REQUIREMENTS_DIGEST")
        condition = definition["video_condition"]
        if condition is not None:
            from fsf.runtime.blender import validate_condition_capability
            try:
                validate_condition_capability(condition, recipe["condition_capability"])
            except Exception:
                raise RemoteError("REMOTE_CONDITION_INVALID") from None
        elif any(a["role"] == "control_video" for a in recipe["inputs"]):
            raise RemoteError("REMOTE_CONDITION_REQUIRED")
        spec = {"type": "object", "properties": recipe["parameters"],
                "required": list(recipe["parameters"]), "additionalProperties": False}
        if not Draft202012Validator(spec).is_valid(definition["parameters"]):
            raise RemoteError("REMOTE_PARAMETERS")
        inputs = definition["inputs"]
        if not isinstance(inputs, dict) or set(inputs) != {a["artifact_id"] for a in recipe["inputs"]}:
            raise RemoteError("REMOTE_INPUTS")
        from fsf.core.asset_library import _path_parts
        for value in inputs.values():
            _path_parts(value)
    except RemoteError:
        raise
    except Exception:
        raise RemoteError("REMOTE_DEFINITION") from None


def _policy(recipe, profile):
    if os.environ.get("FSF_REMOTE_DEPTH") or not profile["authorized"]:
        raise RemoteError("REMOTE_NOT_AUTHORIZED")
    if recipe["recipe_digest"] not in profile["approved_recipes"]:
        raise RemoteError("REMOTE_RECIPE_NOT_APPROVED")
    if profile["policy"]["gpu"]:
        if os.environ.get("FSF_REMOTE_GPU") != "1":
            raise RemoteError("REMOTE_GPU_NOT_AUTHORIZED")
        if profile["transport"] == "kaggle":
            raise RemoteError("REMOTE_KAGGLE_CPU_ONLY")
    if any(a["source"] not in profile["policy"]["network_sources"] for a in recipe["environment"]["setup"]):
        raise RemoteError("REMOTE_NETWORK_NOT_AUTHORIZED")
    if not set(recipe["environment"]["credential_env_names"]) <= set(profile["policy"]["credential_env_names"]):
        raise RemoteError("REMOTE_CREDENTIAL_NOT_AUTHORIZED")


def _local_runtime(recipe, profile):
    """Observe the configured interpreter; never execute setup/model work."""
    from fsf.executors.subprocess_safety import run_safe_subprocess
    code = "import json,platform;print(json.dumps({'python':platform.python_version(),'os':platform.system().lower()}))"
    result = run_safe_subprocess([profile["settings"]["python"], "-c", code],
                                 timeout_seconds=10, output_limit_bytes=4096)
    if result.timed_out or result.returncode:
        raise RemoteError("REMOTE_RUNTIME_UNAVAILABLE")
    try:
        observed = json.loads(result.stdout)
        if not observed["python"].startswith(recipe["environment"]["python"]):
            raise RemoteError("REMOTE_PYTHON_VERSION")
        if recipe["environment"]["os"] not in {"any", observed["os"]}:
            raise RemoteError("REMOTE_OS_UNSUPPORTED")
    except RemoteError:
        raise
    except Exception:
        raise RemoteError("REMOTE_RUNTIME_UNAVAILABLE") from None
    # Preparation/installation and arbitrary probes need the environment phase;
    # an interpreter ping does not prove tools, models or GPU capability.
    env = recipe["environment"]
    if any(not os.environ.get(name) for name in env["credential_env_names"]):
        raise RemoteError("REMOTE_CREDENTIAL_MISSING")
    if profile["policy"]["gpu"] or any(env[k] for k in ("setup", "models", "probes")):
        return False
    return True


def preflight(definition, recipe, profile, *, inputs_root=None, wheel=None, probe=False):
    """Return only fixed codes and booleans; no arbitrary input or provider text."""
    checks = []
    check_name = "declarations"

    def item(phase, name, status, code):
        checks.append({"phase": phase, "check": name, "status": status, "code": code})

    try:
        recipe = validate_recipe(recipe)
        profile = validate_profile(profile)
        validate_definition(definition, recipe)
        _policy(recipe, profile)
        item("static", "declarations", "passed", "REMOTE_STATIC_VALID")
        check_name = "files"
        if inputs_root is None or wheel is None:
            item("static", "files", "unverified", "REMOTE_FILES_UNVERIFIED")
        else:
            from fsf.core.asset_library import _root, _open_reader, _no_links
            import hashlib
            import stat
            import zipfile
            source = _root(inputs_root)
            total = 0
            for relative in definition["inputs"].values():
                path = safe_path(source, relative)
                with _open_reader(path) as stream:
                    size = os.fstat(stream.fileno()).st_size
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or not 0 < size <= MAX_BUNDLE_BYTES:
                        raise RemoteError("REMOTE_SIZE_LIMIT")
                    total += size
            path = Path(wheel).absolute()
            _no_links(path)
            if not path.name.startswith("free_studio_flow-") or not path.name.endswith("-py3-none-any.whl"):
                raise RemoteError("REMOTE_WHEEL")
            if not 0 < path.stat().st_size <= MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            if total + path.stat().st_size > MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            from fsf.remote import worker
            with zipfile.ZipFile(path) as archive:
                if hashlib.sha256(archive.read("fsf/remote/worker.py")).hexdigest() != sha(Path(worker.__file__)):
                    raise RemoteError("REMOTE_STALE_WHEEL")
            item("static", "files", "passed", "REMOTE_FILES_VALID")
    except Exception as exc:
        item("static", check_name, "failed", _code(exc, "REMOTE_PREFLIGHT_FAILED"))

    static_ok = bool(checks) and all(c["status"] == "passed" for c in checks)
    if static_ok and probe:
        phase, name = "provider", "connection"
        try:
            from fsf.remote.transports import adapter_for
            adapter = adapter_for(profile)
            with tempfile.TemporaryDirectory() as temp:
                if profile["transport"] == "kaggle":
                    adapter._call("diagnose", {"request_digest": "0" * 64, "policy": profile["policy"]}, Path(temp))
                elif profile["transport"] == "http":
                    adapter._call("GET", "/capabilities")
                else:
                    adapter.prepare(None, None, None)
            item("provider", "connection", "passed", "REMOTE_CONNECTION_VERIFIED")
            phase, name = "runtime", "environment"
            ready = profile["transport"] == "local" and _local_runtime(recipe, profile)
            item("runtime", "environment", "passed" if ready else "unverified",
                 "REMOTE_RUNTIME_VERIFIED" if ready else "REMOTE_CAPABILITY_UNVERIFIED")
        except Exception as exc:
            item(phase, name, "failed", _code(exc, "REMOTE_PROBE_FAILED"))
    else:
        item("provider", "connection", "unverified", "REMOTE_PROVIDER_UNVERIFIED")
        item("runtime", "environment", "unverified", "REMOTE_CAPABILITY_UNVERIFIED")
    status = "failed" if any(c["status"] == "failed" for c in checks) else (
        "passed" if all(c["status"] == "passed" for c in checks) else "unverified")
    return {"schema_version": "fsf_remote_preflight_v1", "status": status,
            "static_ok": static_ok, "ready_for_dispatch": status == "passed",
            "verified_execution": False, "checks": checks}
