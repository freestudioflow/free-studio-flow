"""Locate Kaggle credentials without printing secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PLACEHOLDER_USERNAMES = {"", "YOUR_KAGGLE_USERNAME", "your-username"}


def kaggle_json_candidates() -> list[Path]:
    paths: list[Path] = []
    config_dir = os.environ.get("KAGGLE_CONFIG_DIR", "").strip()
    if config_dir:
        paths.append(Path(config_dir) / "kaggle.json")
    paths.append(Path.home() / ".kaggle" / "kaggle.json")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def apply_kaggle_config_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure Kaggle CLI subprocesses see the same config dir we inspected."""
    resolved = env if env is not None else os.environ.copy()
    if resolved.get("KAGGLE_CONFIG_DIR", "").strip():
        return resolved
    creds = inspect_kaggle_credentials()
    config_dir = creds.get("config_dir")
    if creds.get("ok") and config_dir and not str(config_dir).startswith("env:"):
        resolved["KAGGLE_CONFIG_DIR"] = str(config_dir)
    return resolved


def inspect_kaggle_credentials() -> dict[str, Any]:
    """Return username presence only. Never include the API key."""
    env_user = os.environ.get("KAGGLE_USERNAME", "").strip()
    env_key = os.environ.get("KAGGLE_KEY", "").strip()
    if env_user and env_key and env_user.lower() not in {item.lower() for item in PLACEHOLDER_USERNAMES}:
        return {
            "ok": True,
            "username": env_user,
            "config_dir": "env:KAGGLE_USERNAME",
            "has_key": True,
        }
    for path in kaggle_json_candidates():
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        username = str(payload.get("username") or "").strip()
        key = str(payload.get("key") or "").strip()
        if username.lower() in {item.lower() for item in PLACEHOLDER_USERNAMES}:
            username = ""
        has_key = bool(key) and not key.startswith("*")
        if username and has_key:
            return {
                "ok": True,
                "username": username,
                "config_dir": str(path.parent),
                "has_key": True,
            }
    return {
        "ok": False,
        "username": None,
        "config_dir": None,
        "has_key": False,
        "error": "kaggle.json not found or username/key missing",
    }
