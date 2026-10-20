"""Operator-owned credential bindings; values never enter plans or receipts."""
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import resources
import json
import os
import time
from jsonschema import Draft202012Validator

from fsf.core.edit_store import decode_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError, reject_inline_credentials, digest

_scope = ContextVar("fsf_credentials", default=None)
SOURCES = {"environment", "aws_default_chain", "os_credential_store", "host_secret_manager", "host_proxy"}


def validate_binding(binding, profile):
    reject_inline_credentials(binding)
    schema=json.loads(resources.files("fsf.schemas").joinpath("fsf_credential_binding_v1.schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(binding):
        raise RemoteError("API_BINDING_INVALID")
    required = {"schema_version", "profile_id", "source", "reference", "revision", "authorized", "resolver"}
    if (not isinstance(binding, dict) or set(binding) != required
            or binding["schema_version"] != "fsf_credential_binding_v1"
            or binding["profile_id"] != profile["profile_id"] or binding["source"] not in SOURCES
            or type(binding["authorized"]) is not bool
            or any(not isinstance(binding[k], str) or not binding[k].strip() or len(binding[k]) > 512
                   for k in ("reference", "revision"))):
        raise RemoteError("API_BINDING_INVALID")
    resolver = binding["resolver"]
    if binding["source"] == "host_secret_manager":
        if (not isinstance(resolver, list) or not resolver or len(resolver) > 32
                or any(not isinstance(a, str) or not a or len(a) > 4096 for a in resolver)):
            raise RemoteError("API_BINDING_INVALID")
    elif resolver is not None:
        raise RemoteError("API_BINDING_INVALID")
    if binding["source"] == "aws_default_chain" and profile["protocol"] != "bedrock_converse_v1":
        raise RemoteError("API_BINDING_PROTOCOL")
    if binding["source"] == "host_proxy" and (profile["protocol"] != "json_http_v1"
            or profile["http"]["auth_header"] != "none"):
        raise RemoteError("API_HOST_PROXY_REQUIRED")
    return binding


@contextmanager
def credential_scope(profile, binding=None):
    if binding is not None:
        validate_binding(binding, profile)
        if not binding["authorized"]:
            raise RemoteError("API_BINDING_NOT_AUTHORIZED")
    token = _scope.set({"profile_id": profile["profile_id"], "binding": binding, "value": None})
    try:
        yield
    finally:
        _scope.reset(token)


def credential_value(profile):
    scope = _scope.get()
    binding = scope["binding"] if scope and scope["profile_id"] == profile["profile_id"] else None
    if binding is None:
        return os.environ.get(profile["key_env"]) if profile["key_env"] else None
    if scope["value"] is not None:
        return scope["value"]
    source = binding["source"]
    try:
        if source == "environment":
            value = os.environ.get(binding["reference"])
        elif source == "os_credential_store":
            try:
                import keyring
            except ImportError:
                raise RemoteError("API_KEYRING_UNAVAILABLE") from None
            value = keyring.get_password("fsf/" + profile["profile_id"], binding["reference"])
        elif source == "host_secret_manager":
            # Trusted operator executable, not a command from a workorder. The
            # reference is public metadata; only bounded stdout carries a value.
            env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
            result = run_safe_subprocess([*binding["resolver"], "--reference", binding["reference"]],
                env=env, inherit_env=False, timeout_seconds=10, output_limit_bytes=16384, scrub_stdout=False)
            if result.returncode or result.timed_out:
                raise RemoteError("API_SECRET_UNAVAILABLE")
            reply = decode_json(result.stdout.encode("utf-8"))
            if (not isinstance(reply, dict) or set(reply) != {"value", "revision", "expires_at"}
                    or reply["revision"] != binding["revision"]):
                raise RemoteError("API_SECRET_REVISION")
            expiry = reply["expires_at"]
            if type(expiry) not in (int, float) or expiry <= time.time():
                raise RemoteError("API_SECRET_EXPIRED")
            value = reply["value"]
        elif source == "host_proxy":
            value = "managed-by-host"
        else:
            raise RemoteError("API_BINDING_PROTOCOL")
        if not isinstance(value, str) or not value or len(value) > 8192 or any(c in value for c in "\r\n"):
            raise RemoteError("API_AUTH_MISSING")
        scope["value"] = value
        return value
    except RemoteError:
        raise
    except Exception:
        raise RemoteError("API_SECRET_UNAVAILABLE") from None


def credential_binding_identity(profile):
    scope=_scope.get()
    if scope and scope["profile_id"]==profile["profile_id"] and scope["binding"] is not None:
        return digest(scope["binding"])
    return None


def bedrock_client(profile, config):
    import boto3
    scope = _scope.get()
    binding = scope["binding"] if scope else None
    if binding and binding["source"] != "aws_default_chain":
        raise RemoteError("API_BINDING_PROTOCOL")
    if binding and binding["reference"] != "default":
        return boto3.Session(profile_name=binding["reference"]).client(
            "bedrock-runtime", region_name=profile["region"], config=config)
    return boto3.client("bedrock-runtime", region_name=profile["region"], config=config)
