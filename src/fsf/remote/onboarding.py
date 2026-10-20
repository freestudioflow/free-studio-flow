"""Offline setup guidance and persistent, explicitly re-opened API checks."""
from contextlib import contextmanager
import copy
from importlib import resources
import json
import os
import time

from fsf.core.edit_store import edit_lock, read_json
from fsf.core.asset_library import _root
from fsf.remote.contracts import RemoteError, digest, seal, check_digest, write_json
from fsf.remote.credentials import validate_binding

PAUSED = {"needs_configuration", "paused_quota", "paused_unknown"}


def error_state(code):
    if code in {"API_RATE_LIMITED", "REMOTE_QUOTA_EXCEEDED"}:
        return "paused_quota"
    if code in {"API_AUTH_FAILED", "API_AUTH_MISSING", "API_NOT_AUTHORIZED", "API_BINDING_NOT_AUTHORIZED",
                "API_SECRET_UNAVAILABLE", "API_SECRET_EXPIRED", "API_SECRET_REVISION", "API_REQUEST_REJECTED",
                "API_BEDROCK_ACCESS_DENIED", "API_BEDROCK_EXPIRED", "API_BEDROCK_MODEL_UNAVAILABLE"}:
        return "needs_configuration"
    return "paused_unknown"


def setup(provider, capability, model, *, source="environment", reference=None, region=None, resolver=None):
    if provider not in {"nebius", "google", "bedrock", "custom"} or capability not in {"text", "image", "video", "audio", "upscale"}:
        raise RemoteError("API_SETUP_UNSUPPORTED")
    if provider != "custom" and capability != "text":
        raise RemoteError("API_SETUP_REQUIRES_MEDIA_PROFILE")
    filename = provider + "-text" if provider != "custom" else capability if capability != "text" else "image"
    profile = json.loads(resources.files("fsf.remote.templates").joinpath(filename + ".api-profile.json").read_text("utf-8"))
    if provider == "custom" and capability == "text":
        profile.update(capability="text", profile_id="custom_text")
        profile["output"].update(media_type="text/plain", suffix=".txt")
    profile["model"] = model
    if region:
        profile["region"] = region
    if provider == "bedrock":
        source = "aws_default_chain"
    binding = {"schema_version": "fsf_credential_binding_v1", "profile_id": profile["profile_id"],
        "source": source, "reference": reference or ("default" if provider == "bedrock" else profile["key_env"]),
        "revision": "1", "authorized": False, "resolver": resolver}
    validate_binding(binding, profile)
    guidance = ["Keep credentials in the host credential facility; never paste secret values into chat.",
        "Select an accessible model, region and billing/quota limits before enabling authorization.",
        "Set profile.authorized and binding.authorized only after operator approval.",
        "Run one bounded smoke request; validate every required media capability separately."]
    if source == "host_secret_manager":
        guidance += ["Configure the host-owned resolver: --reference NAME returns value, revision and expires_at as JSON on stdout.",
            "No universal Muse secret command is assumed. Use the actual host integration."]
    if provider == "bedrock":
        guidance += ["Configure an AWS profile/role or SSO and Bedrock model access in the selected region.",
            "A model may require an inference profile. Read-only model probing is not implemented for Converse."]
    return profile, binding, {"provider": provider, "capability": capability, "guidance": guidance,
        "configured": False, "verified_inference": False, "manual_review": "not_reviewed"}


@contextmanager
def connection_attempt(profile, binding, root, *, recheck=False):
    # No credential bytes or hash of credential bytes participates in identity.
    identity = digest({"profile": profile, "binding": binding})
    directory = root / ".cx"
    directory.mkdir(exist_ok=True)
    _root(directory)
    path = directory / (identity[:24] + ".json")
    with edit_lock(directory / identity[:24]):
        state = {"identity": identity, "status": "configured", "last_error": None,
            "verified_inference": False, "checks": 0}
        if path.exists():
            state = read_json(path)
            check_digest(state, "state_digest")
            if state.get("identity") != identity:
                raise RemoteError("API_CONNECTION_STATE")
            if state["status"] in PAUSED | {"checking"} and not recheck:
                raise RemoteError("API_CONNECTION_PAUSED")
        state.update(status="checking", checks=state["checks"] + 1)
        write_json(path, seal(state, "state_digest"))
        try:
            yield state
        except BaseException as exc:
            code = getattr(exc, "code", "API_CANCELLED" if isinstance(exc, KeyboardInterrupt) else "API_CONNECTION_UNKNOWN")
            state.update(status=error_state(code), last_error=code)
            write_json(path, seal(state, "state_digest"))
            raise
        else:
            if state["status"]=="checking":
                state.update(status="ready", last_error=None)
            write_json(path, seal(state, "state_digest"))
