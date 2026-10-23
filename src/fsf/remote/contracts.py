"""Closed remote contracts and shared policy validation."""
import copy
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from jsonschema import Draft202012Validator
from fsf import __version__
from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.asset_library import _path_parts, _no_links
from fsf.core.edit_store import read_json, decode_json
from fsf.runtime.blender import sha

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 128


def reject_inline_credentials(value):
    """Credential values belong in the environment, never nested config data."""
    forbidden={"api_key","apikey","access_key","secret_key","password","passwd","token","authorization","private_key"}
    if isinstance(value,dict):
        for key,item in value.items():
            if str(key).lower().replace("-","_") in forbidden:
                raise RemoteError("REMOTE_INLINE_CREDENTIAL")
            reject_inline_credentials(item)
    elif isinstance(value,list):
        for item in value:reject_inline_credentials(item)


def reject_credential_bytes(path, values):
    """Reject known credentials across chunk boundaries; never rewrite artifacts."""
    needles=[value.encode("utf-8") for value in values if isinstance(value,str) and len(value)>=4]
    if not needles:return
    overlap=max(map(len,needles))-1
    tail=b""
    with Path(path).open("rb") as stream:
        while block:=stream.read(1024*1024):
            data=tail+block
            if any(value in data for value in needles):raise RemoteError("REMOTE_SECRET_OUTPUT")
            tail=data[-overlap:] if overlap else b""


class RemoteError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def shape(value, name):
    schema = json.loads(resources.files("fsf.schemas").joinpath("fsf_remote_" + name + "_v1.schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(value):
        raise RemoteError("REMOTE_SCHEMA")
    decode_json(json.dumps(value, allow_nan=False).encode("utf-8"))
    return value


def seal(value, field):
    value = copy.deepcopy(value)
    value.pop(field, None)
    value[field] = digest(value)
    return value


def check_digest(value, field):
    if seal(value, field).get(field) != value.get(field):
        raise RemoteError("REMOTE_INTEGRITY")


def write_json(path, value):
    path = Path(path)
    data = json.dumps(value, allow_nan=False, indent=2).encode("utf-8")
    decode_json(data)
    fd, tmp = tempfile.mkstemp(prefix=".remote-json-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers can briefly hold a file without delete sharing.
        # Preserve atomic visibility and retry only that transient condition.
        deadline=time.monotonic()+2
        while True:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if time.monotonic()>=deadline:raise
                time.sleep(.02)
    finally:
        Path(tmp).unlink(missing_ok=True)


def safe_path(root, relative, *, exists=True):
    if not isinstance(relative,str) or "\\" in relative:
        raise RemoteError("REMOTE_PATH")
    path = Path(root).joinpath(*_path_parts(relative))
    if exists:
        _no_links(path)
        if not path.is_file():
            raise RemoteError("REMOTE_FILE_MISSING")
    else:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        _no_links(parent)
        if path.exists():
            _no_links(path)
    return path


def file_item(path, artifact_id, relative, role, media_type):
    _no_links(Path(path))
    size = Path(path).stat().st_size
    if not 0 < size <= MAX_BUNDLE_BYTES:
        raise RemoteError("REMOTE_SIZE_LIMIT")
    return {"artifact_id": artifact_id, "path": relative, "role": role, "media_type": media_type,
            "sha256": sha(path), "size_bytes": size}


def validate_recipe(recipe):
    recipe = copy.deepcopy(recipe)
    reject_inline_credentials(recipe)
    shape(recipe, "recipe")
    check_digest(recipe, "recipe_digest")
    ids, outputs = set(), set()
    for item in recipe["inputs"]:
        if item["artifact_id"] in ids:
            raise RemoteError("REMOTE_DUPLICATE_ID")
        ids.add(item["artifact_id"])
    seen = set()
    for step in recipe["steps"]:
        sid = step["step_id"]
        if sid in seen or any(dep not in seen for dep in step["depends_on"]):
            raise RemoteError("REMOTE_DAG")
        seen.add(sid)
        if step["tool"].startswith("fsf.remote"):
            raise RemoteError("REMOTE_NESTING_FORBIDDEN")
        for item in step["outputs"]:
            _path_parts(item["path"])
            if item["artifact_id"] in ids or item["path"] in outputs:
                raise RemoteError("REMOTE_DUPLICATE_ID")
            ids.add(item["artifact_id"])
            outputs.add(item["path"])
        if any("{env." in a for a in step["args"]):
            raise RemoteError("REMOTE_SECRET_ARGUMENT")
    env = recipe["environment"]
    if not re.fullmatch(r"3\.(11|12|13)(\.\d+)?", env["python"]):
        raise RemoteError("REMOTE_VERSION")
    for model in env["models"]:
        _path_parts(model["path"])
        if model["revision"].lower() in {"main", "master", "latest"}:
            raise RemoteError("REMOTE_UNPINNED_MODEL")
    for setup in env["setup"]:
        if any("latest" in a.lower() for a in setup["args"]):
            raise RemoteError("REMOTE_UNPINNED_SETUP")
    if recipe["acknowledgement_output"] is not None:
        _path_parts(recipe["acknowledgement_output"])
        if recipe["acknowledgement_output"] not in outputs:
            raise RemoteError("REMOTE_ACK_OUTPUT")
    return recipe


def compile_child_plan(recipe):
    recipe = validate_recipe(recipe)
    plan = {"schema_version": "pdx_execution_plan_v1", "request_id": "child_" + recipe["recipe_digest"][:24],
            "producer": {"type": "fsf_planner", "name": "fsf.remote.worker"}, "steps": []}
    for step in recipe["steps"]:
        plan["steps"].append({"id": step["step_id"], "name": "Run approved recipe step", "kind": "tool",
            "tool": step["tool"], "depends_on": step["depends_on"], "inputs": {"recipe_digest": recipe["recipe_digest"],
            "recipe_step_id": step["step_id"]}, "outputs": ["artifact:"+a["path"] for a in step["outputs"]]})
    from pdx_artifact_core import validate_execution_plan
    if validate_execution_plan(plan):
        raise RemoteError("REMOTE_CHILD_PLAN")
    return plan


SETTINGS = {
    "local": ({"worker_root", "python"}, {"worker_root", "python"}),
    "http": ({"endpoint", "auth_env"}, {"endpoint", "auth_env"}),
    "ssh": ({"host_alias", "known_hosts", "ssh_config", "remote_root", "python"},
            {"host_alias", "known_hosts", "ssh_config", "remote_root", "python"}),
    "kaggle": ({"owner_env"}, set()),
}


def validate_profile(profile):
    reject_inline_credentials(profile)
    shape(profile, "profile")
    keys, required = SETTINGS[profile["transport"]]
    settings = profile["settings"]
    if set(settings)-keys or required-set(settings) or any(not isinstance(v, str) or not v for v in settings.values()):
        raise RemoteError("REMOTE_PROFILE")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", v) for k, v in settings.items() if k.endswith("_env")):
        raise RemoteError("REMOTE_PROFILE")
    if profile["transport"] == "ssh":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", settings["host_alias"]):
            raise RemoteError("REMOTE_PROFILE")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", settings["remote_root"]) or ".." in settings["remote_root"].split("/"):
            raise RemoteError("REMOTE_PROFILE")
    return copy.deepcopy(profile)


def validate_request(request):
    shape(request, "request")
    check_digest(request, "request_digest")
    recipe = validate_recipe(request["recipe"])
    contract = request["expected_media_contract"]
    if request["parent"]["expected_contract_digest"] != (digest(contract) if contract is not None else None):
        raise RemoteError("REMOTE_CONTRACT_DIGEST")
    if any(a["role"] == "video" for step in recipe["steps"] for a in step["outputs"]) and not contract:
        raise RemoteError("REMOTE_QC_CONTRACT_REQUIRED")
    if contract is not None:
        from fsf.conformance.media import validate_media_expected
        try:
            validate_media_expected(contract)
        except (ValueError, TypeError):
            raise RemoteError("REMOTE_QC_CONTRACT_INVALID") from None
    requirements = request["consistency_requirements"]
    if request["parent"]["requirements_digest"] != (requirements.get("requirements_digest") if requirements else None):
        raise RemoteError("REMOTE_REQUIREMENTS_DIGEST")
    if request["fsf_version"] != __version__ or request["child"]["plan"] != compile_child_plan(recipe):
        raise RemoteError("REMOTE_VERSION_OR_PLAN")
    props = recipe["parameters"]
    parameters = request["parameters"]
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    if not Draft202012Validator(schema).is_valid(parameters):
        raise RemoteError("REMOTE_PARAMETERS")
    expected = {i["artifact_id"]: i for i in recipe["inputs"]}
    if len(request["inputs"]) != len(expected) or {i["artifact_id"] for i in request["inputs"]} != set(expected):
        raise RemoteError("REMOTE_INPUTS")
    paths = set()
    for item in request["inputs"]:
        _path_parts(item["path"])
        if not item["path"].startswith("inputs/") or item["path"] in paths:
            raise RemoteError("REMOTE_PATH")
        paths.add(item["path"])
        spec = expected[item["artifact_id"]]
        if (item["role"], item["media_type"]) != (spec["role"], spec["media_type"]):
            raise RemoteError("REMOTE_INPUTS")
    _path_parts(request["worker_wheel"]["path"])
    if not request["worker_wheel"]["path"].startswith("runtime/") or not request["worker_wheel"]["path"].endswith(".whl"):
        raise RemoteError("REMOTE_WHEEL")
    if request["video_condition"]:
        from fsf.runtime.blender import validate_condition_capability
        validate_condition_capability(request["video_condition"], recipe["condition_capability"])
        matches = [a for a in request["inputs"] if a["role"] == "control_video"]
        control = request["video_condition"]["artifact"]
        if len(matches) != 1 or (matches[0]["sha256"], matches[0]["size_bytes"]) != (control["sha256"], control["size_bytes"]):
            raise RemoteError("REMOTE_CONDITION_INPUT")
    elif any(a["role"] == "control_video" for a in request["inputs"]):
        raise RemoteError("REMOTE_CONDITION_REQUIRED")
    return copy.deepcopy(request)


def check_authorization(request, profile):
    validate_request(request)
    validate_profile(profile)
    if os.environ.get("FSF_REMOTE_DEPTH") or not profile["authorized"]:
        raise RemoteError("REMOTE_NOT_AUTHORIZED")
    if request["profile_id"] != profile["profile_id"] or request["adapter_digest"] != digest(profile):
        raise RemoteError("REMOTE_PROFILE_CHANGED")
    if request["recipe"]["recipe_digest"] not in profile["approved_recipes"]:
        raise RemoteError("REMOTE_RECIPE_NOT_APPROVED")
    for key in ("cpu", "ram_mb", "disk_mb", "deadline_seconds"):
        if request["policy"][key] > profile["policy"][key]:
            raise RemoteError("REMOTE_RESOURCE_LIMIT")
    if request["policy"]["gpu"] and (not profile["policy"]["gpu"] or os.environ.get("FSF_REMOTE_GPU") != "1"):
        raise RemoteError("REMOTE_GPU_NOT_AUTHORIZED")
    if not set(request["policy"]["network_sources"]) <= set(profile["policy"]["network_sources"]):
        raise RemoteError("REMOTE_NETWORK_NOT_AUTHORIZED")
    if any(s["source"] not in request["policy"]["network_sources"] for s in request["recipe"]["environment"]["setup"]):
        raise RemoteError("REMOTE_NETWORK_NOT_AUTHORIZED")
    if not set(request["recipe"]["environment"]["credential_env_names"]) <= set(request["policy"]["credential_env_names"]):
        raise RemoteError("REMOTE_CREDENTIAL_NOT_AUTHORIZED")
    if not set(request["policy"]["credential_env_names"]) <= set(profile["policy"]["credential_env_names"]):
        raise RemoteError("REMOTE_CREDENTIAL_NOT_AUTHORIZED")
