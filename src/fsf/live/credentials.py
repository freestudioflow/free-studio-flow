"""Bounded Kaggle credential discovery and non-reflective status."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any

PLACEHOLDER_USERNAMES = {"", "your_kaggle_username", "your-username"}
_MAX_CREDENTIAL_BYTES = 32768


def _read_file(path):
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > _MAX_CREDENTIAL_BYTES:
            return None
        return raw.decode("utf-8-sig").strip()
    except (OSError, UnicodeError, ValueError):
        return None


def _valid_token(value):
    return (isinstance(value, str) and 0 < len(value) <= 8192
            and not value.startswith("*") and not any(c.isspace() for c in value))


def kaggle_json_candidates(env=None):
    env = os.environ if env is None else env
    paths = []
    if env.get("KAGGLE_CONFIG_DIR", "").strip():
        paths.append(Path(env["KAGGLE_CONFIG_DIR"]) / "kaggle.json")
    paths.append(Path.home() / ".kaggle" / "kaggle.json")
    return list(dict.fromkeys(paths))


def _binding_profile():
    return {"profile_id": "kaggle", "protocol": "kaggle_token", "key_env": "KAGGLE_API_TOKEN"}


def _discover(env):
    # Explicit bindings never silently fall back on failure.
    if env.get("FSF_KAGGLE_CREDENTIAL_BINDING_FILE"):
        from fsf.core.edit_store import read_json
        from fsf.remote.credentials import validate_binding
        try:
            binding = validate_binding(read_json(env["FSF_KAGGLE_CREDENTIAL_BINDING_FILE"]), _binding_profile())
            allowed = binding["source"] in {"environment", "os_credential_store", "host_secret_manager"}
            return "binding", allowed and binding["authorized"], None
        except Exception:
            return "binding", False, None
    token = env.get("KAGGLE_API_TOKEN", "").strip()
    if token:
        try:
            if len(token) < 1024 and Path(token).is_file():
                token = _read_file(Path(token))
        except (OSError, ValueError):
            pass
        return "token_environment", _valid_token(token), None
    for name in ("access_token", "access_token.txt"):
        token = _read_file(Path.home() / ".kaggle" / name)
        if token:
            return "token_file", _valid_token(token), None
    user, key = env.get("KAGGLE_USERNAME", "").strip(), env.get("KAGGLE_KEY", "").strip()
    if user and user.lower() not in PLACEHOLDER_USERNAMES and _valid_token(key):
        return "legacy_environment", True, None
    for path in kaggle_json_candidates(env):
        try:
            payload = json.loads(_read_file(path) or "null")
            if not isinstance(payload, dict):
                continue
            user, key = payload.get("username"), payload.get("key")
            if (isinstance(user, str) and user.strip().lower() not in PLACEHOLDER_USERNAMES
                    and _valid_token(key)):
                return "legacy_file", True, path.parent
        except (TypeError, ValueError):
            continue
    return "none", False, None


def inspect_kaggle_credentials() -> dict[str, Any]:
    """Configuration only; authentication needs an explicit API probe."""
    source, configured, _directory = _discover(os.environ)
    return {"ok": configured, "source": source, "configured": configured,
            "authenticated": None, "has_key": configured and source.startswith("legacy"),
            "has_token": configured and source.startswith("token"),
            "username": None, "config_dir": None,
            "error": None if configured else "Kaggle credentials are not configured or the binding is invalid"}


def apply_kaggle_config_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve privately without changing the supplied environment."""
    resolved = os.environ.copy() if env is None else env.copy()
    search = os.environ.copy()
    search.update(resolved)
    source, configured, directory = _discover(search)
    if configured and source == "legacy_file" and directory and not resolved.get("KAGGLE_CONFIG_DIR"):
        resolved["KAGGLE_CONFIG_DIR"] = str(directory)
    return resolved


@contextmanager
def kaggle_credential_environment():
    """Resolve one operator secret for one child call, never into os.environ."""
    env = apply_kaggle_config_env()
    binding_file = env.get("FSF_KAGGLE_CREDENTIAL_BINDING_FILE")
    if not binding_file:
        yield env
        return
    from fsf.core.edit_store import read_json
    from fsf.remote.contracts import RemoteError
    from fsf.remote.credentials import credential_scope, credential_value, validate_binding
    profile = _binding_profile()
    binding = validate_binding(read_json(binding_file), profile)
    if binding["source"] not in {"environment", "os_credential_store", "host_secret_manager"}:
        raise RemoteError("REMOTE_KAGGLE_BINDING_PROTOCOL")
    with credential_scope(profile, binding):
        value = credential_value(profile)
        if not _valid_token(value):
            raise RemoteError("REMOTE_AUTH_MISSING")
        # Keep SDK fallback discovery away from the operator's legacy/OAuth
        # files. Resolving an explicit binding must not switch account on 401.
        with tempfile.TemporaryDirectory(prefix="fsf-kaggle-auth-") as home:
            for name in ("KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_V1_TOKEN"):
                env.pop(name, None)
            env.update(HOME=home, USERPROFILE=home, KAGGLE_CONFIG_DIR=home, KAGGLE_API_TOKEN=value)
            try:
                yield env
            finally:
                env.pop("KAGGLE_API_TOKEN", None)
