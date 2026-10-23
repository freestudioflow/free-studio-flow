"""Readiness checks must fail before a provider is allowed to submit."""
import copy
import json
from importlib import resources
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from fsf.remote.contracts import RemoteError, digest, seal, write_json
from fsf.remote.bundle import prepare_bundle
from fsf.remote.preflight import preflight, validate_definition
from test_remote_dispatch import remote_case, remote_wheel, template


def video_case(case):
    root, _, profile, definition, wheel = case
    recipe = template("ffmpeg.recipe.json")
    recipe["environment"]["python"] = ".".join(map(str, sys.version_info[:2]))
    recipe = seal(recipe, "recipe_digest")
    profile["approved_recipes"] = [recipe["recipe_digest"]]
    definition["parameters"] = {}
    return root, recipe, profile, definition, wheel


def report(case, **kwargs):
    root, recipe, profile, definition, wheel = case
    return preflight(definition, recipe, profile, inputs_root=root/"inputs", wheel=wheel, **kwargs)


def test_video_missing_contract_rejected_before_packaging_or_provider(remote_case, monkeypatch):
    case = video_case(remote_case)
    root, recipe, profile, definition, wheel = case
    from fsf.remote import bundle, transports
    calls = []
    monkeypatch.setattr(bundle, "pack", lambda *a: calls.append("pack"))
    monkeypatch.setattr(transports, "adapter_for", lambda *a: calls.append("adapter"))
    with pytest.raises(RemoteError, match="REMOTE_QC_CONTRACT_REQUIRED"):
        prepare_bundle(definition, recipe, inputs_root=root/"inputs", wheel=wheel, profile=profile, output_root=root/"state")
    result = report(case, probe=True)
    assert result["status"] == "failed" and not result["static_ok"]
    assert calls == [] and list((root/"state").iterdir()) == []


def test_contract_digest_check_remains_before_provider(remote_case):
    case = video_case(remote_case)
    case[3]["expected_media_contract"] = {"duration_seconds": 1}
    with pytest.raises(RemoteError, match="REMOTE_CONTRACT_DIGEST"):
        validate_definition(case[3], case[1])


def test_malformed_contract_with_matching_digest_rejected(remote_case):
    case = video_case(remote_case)
    contract = {"invented_property": True}
    case[3]["expected_media_contract"] = contract
    case[3]["parent"]["expected_contract_digest"] = digest(contract)
    with pytest.raises(RemoteError, match="REMOTE_QC_CONTRACT_INVALID"):
        validate_definition(case[3], case[1])


def test_static_only_never_claims_remote_or_execution_verified(remote_case):
    result = report(remote_case)
    assert result["static_ok"] and result["status"] == "unverified"
    assert not result["ready_for_dispatch"] and result["verified_execution"] is False
    schema = json.loads(resources.files("fsf.schemas").joinpath("fsf_remote_preflight_v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(result)


def test_local_interpreter_probe_is_not_generation(remote_case, monkeypatch):
    from fsf.remote.transports import LocalAdapter
    monkeypatch.setattr(LocalAdapter, "submit", lambda *a: pytest.fail("preflight submitted"))
    result = report(remote_case, probe=True)
    assert result["status"] == "passed" and result["ready_for_dispatch"]
    assert result["verified_execution"] is False


def test_tool_probe_not_misrepresented_as_complete_environment(remote_case):
    root, recipe, profile, definition, wheel = remote_case
    recipe["environment"]["probes"] = [{"program": "{blender}", "args": ["--version"], "pattern": "Blender"}]
    recipe = seal(recipe, "recipe_digest")
    profile["approved_recipes"] = [recipe["recipe_digest"]]
    result = report((root, recipe, profile, definition, wheel), probe=True)
    assert result["static_ok"] and result["status"] == "unverified"
    assert not result["ready_for_dispatch"]


@pytest.mark.parametrize("authorized,gpu,switch,transport,code", [
    (False, False, False, "local", "REMOTE_NOT_AUTHORIZED"),
    (True, True, False, "local", "REMOTE_GPU_NOT_AUTHORIZED"),
    (True, True, True, "kaggle", "REMOTE_KAGGLE_CPU_ONLY"),
])
def test_authorization_fails_before_any_probe(remote_case, monkeypatch, authorized, gpu, switch, transport, code):
    from fsf.remote import transports
    root, recipe, profile, definition, wheel = remote_case
    profile.update(authorized=authorized, transport=transport)
    if transport == "kaggle": profile["settings"] = {}
    profile["policy"]["gpu"] = gpu
    if switch: monkeypatch.setenv("FSF_REMOTE_GPU", "1")
    else: monkeypatch.delenv("FSF_REMOTE_GPU", raising=False)
    monkeypatch.setattr(transports, "adapter_for", lambda *a: pytest.fail("provider called"))
    result = report(remote_case, probe=True)
    assert result["status"] == "failed"
    assert code in [x["code"] for x in result["checks"]]


def test_foreign_exception_and_private_input_not_echoed(remote_case, monkeypatch):
    from fsf.remote import transports
    secret = "SYNTHETIC_HOST_AND_TOKEN_6000"
    def fail(*a): raise RuntimeError(secret)
    monkeypatch.setattr(transports, "adapter_for", fail)
    result = report(remote_case, probe=True)
    assert result["status"] == "failed" and secret not in json.dumps(result)
    assert str(remote_case[0]) not in json.dumps(result)


def test_remote_connection_is_not_runtime_proof(remote_case, monkeypatch):
    from fsf.remote import transports
    remote_case[2].update(transport="http", settings={"endpoint": "http://127.0.0.1:9", "auth_env": "SYNTHETIC_REMOTE_KEY"})
    class Connected:
        def _call(self, *a): return {"ok": True}
    monkeypatch.setattr(transports, "adapter_for", lambda *a: Connected())
    result = report(remote_case, probe=True)
    assert result["static_ok"] and result["status"] == "unverified"
    assert not result["ready_for_dispatch"]


@pytest.mark.parametrize("mutation,code", [
    ("parameter", "REMOTE_PARAMETERS"),
    ("approval", "REMOTE_RECIPE_NOT_APPROVED"),
    ("wheel", "REMOTE_STALE_WHEEL"),
])
def test_invalid_inputs_do_not_reach_provider(remote_case, monkeypatch, mutation, code):
    from fsf.remote import transports
    root, recipe, profile, definition, wheel = remote_case
    if mutation == "parameter": definition["parameters"] = {"value": "secret-input"}
    if mutation == "approval": profile["approved_recipes"] = []
    if mutation == "wheel":
        import zipfile
        bad = root/"free_studio_flow-bad-py3-none-any.whl"
        with zipfile.ZipFile(bad,"w") as z: z.writestr("fsf/remote/worker.py", "wrong worker")
        wheel = bad
    monkeypatch.setattr(transports,"adapter_for", lambda *a: pytest.fail("provider called"))
    result = report((root,recipe,profile,definition,wheel),probe=True)
    assert result["status"] == "failed"
    assert code in [x["code"] for x in result["checks"]]


def test_wrong_interpreter_version_blocks_local_readiness(remote_case):
    root, recipe, profile, definition, wheel = remote_case
    recipe["environment"]["python"] = "3.11" if sys.version_info.minor != 11 else "3.12"
    recipe = seal(recipe, "recipe_digest")
    profile["approved_recipes"] = [recipe["recipe_digest"]]
    result = report((root, recipe, profile, definition, wheel), probe=True)
    assert result["status"] == "failed"
    assert "REMOTE_PYTHON_VERSION" in [c["code"] for c in result["checks"]]
    assert result["checks"][-1]["phase"] == "runtime"


def test_invalid_condition_is_rejected_before_provider(remote_case, monkeypatch):
    from fsf.remote import transports
    remote_case[3]["video_condition"] = {"condition_digest": "0" * 64}
    monkeypatch.setattr(transports, "adapter_for", lambda *a: pytest.fail("provider called"))
    result = report(remote_case, probe=True)
    assert result["status"] == "failed"
    assert result["checks"][0]["code"] == "REMOTE_CONDITION_INVALID"


def test_cli_three_states_and_no_secret_echo(remote_case):
    root, recipe, profile, definition, wheel = remote_case
    for name, value in [("definition",definition),("recipe",recipe),("profile",profile)]:
        write_json(root/(name+".json"), value)
    args = [sys.executable, "-m", "fsf.cli", "remote", "preflight", str(root/"definition.json"),
            "--recipe", str(root/"recipe.json"), "--profile", str(root/"profile.json"),
            "--inputs", str(root/"inputs"), "--wheel", str(wheel)]
    first = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert first.returncode == 3 and json.loads(first.stdout)["result"]["status"] == "unverified"
    second = subprocess.run(args+["--probe"], capture_output=True, text=True, timeout=30)
    assert second.returncode == 0 and json.loads(second.stdout)["result"]["status"] == "passed"
    definition["parent"]["plan_digest"] = "SYNTHETIC_SECRET_7000"
    write_json(root/"definition.json",definition)
    third = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert third.returncode == 2 and "SYNTHETIC_SECRET_7000" not in third.stdout+third.stderr
    assert "Traceback" not in third.stderr
