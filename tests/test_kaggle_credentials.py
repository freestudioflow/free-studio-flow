"""Offline credential discovery tests. Never use a real API key."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.live.credentials import apply_kaggle_config_env, inspect_kaggle_credentials


def test_inspect_kaggle_credentials_from_config_dir(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "kaggle-config"
    config_dir.mkdir()
    (config_dir / "kaggle.json").write_text(
        json.dumps({"username": "demo-user", "key": "demo-not-a-real-key"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KAGGLE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)

    creds = inspect_kaggle_credentials()
    assert creds["ok"] is True
    assert creds["username"] == "demo-user"
    assert creds["has_key"] is True
    assert "key" not in creds
    assert creds["config_dir"] == str(config_dir)

    env = apply_kaggle_config_env({})
    assert env["KAGGLE_CONFIG_DIR"] == str(config_dir)
