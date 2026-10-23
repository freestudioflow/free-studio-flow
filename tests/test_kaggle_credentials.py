"""Offline credential discovery tests. Never use a real API key."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from fsf.live.credentials import apply_kaggle_config_env, inspect_kaggle_credentials


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN", "KAGGLE_CONFIG_DIR", "FSF_KAGGLE_CREDENTIAL_BINDING_FILE"):
        monkeypatch.delenv(name, raising=False)


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
    assert creds["username"] is None
    assert creds["has_key"] is True
    assert "key" not in creds
    assert creds["config_dir"] is None
    assert creds["source"] == "legacy_file"
    assert "demo-user" not in json.dumps(creds)
    assert str(config_dir) not in json.dumps(creds)

    env = apply_kaggle_config_env({})
    assert env["KAGGLE_CONFIG_DIR"] == str(config_dir)


@pytest.mark.parametrize("source", ["environment", "file", "text_file", "environment_file"])
def test_token_without_legacy_json(source, tmp_path, monkeypatch):
    secret = "SYNTHETIC_KAGGLE_TOKEN_4500"
    if source == "environment":
        monkeypatch.setenv("KAGGLE_API_TOKEN", secret)
    else:
        folder = tmp_path / ".kaggle"
        folder.mkdir()
        path = folder / ("access_token.txt" if source == "text_file" else "access_token")
        path.write_text(secret + "\n", encoding="utf-8")
        if source == "environment_file":
            monkeypatch.setenv("KAGGLE_API_TOKEN", str(path))
    status = inspect_kaggle_credentials()
    assert status["configured"] and status["has_token"] and status["authenticated"] is None
    assert not status["has_key"]
    assert secret not in json.dumps(status) and str(tmp_path) not in json.dumps(status)
    from fsf.executors.kaggle import KaggleLtx23Executor
    assert KaggleLtx23Executor().is_available()


def test_no_credentials_does_not_claim_auth_or_legacy_retirement():
    value = inspect_kaggle_credentials()
    assert not value["ok"] and value["authenticated"] is None
    assert "kaggle.json" not in value["error"]


def test_bad_binding_does_not_fall_back_to_valid_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "synthetic-account")
    monkeypatch.setenv("KAGGLE_KEY", "synthetic-legacy-key")
    path = tmp_path / "binding.json"
    path.write_text("{}")
    monkeypatch.setenv("FSF_KAGGLE_CREDENTIAL_BINDING_FILE", str(path))
    status = inspect_kaggle_credentials()
    assert not status["ok"] and status["source"] == "binding"


def test_host_secret_is_only_resolved_for_scoped_child_environment(tmp_path, monkeypatch):
    import os
    import sys
    from fsf.live.credentials import kaggle_credential_environment
    helper = tmp_path / "host.py"
    helper.write_text("import json,time\nprint(json.dumps({'value':'HOST_SYNTHETIC_TOKEN_5000','revision':'r1','expires_at':time.time()+300}))\n")
    binding = {"schema_version": "fsf_credential_binding_v1", "profile_id": "kaggle",
               "source": "host_secret_manager", "reference": "kaggle-token", "revision": "r1",
               "authorized": True, "resolver": [sys.executable, str(helper)]}
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    monkeypatch.setenv("FSF_KAGGLE_CREDENTIAL_BINDING_FILE", str(path))
    monkeypatch.setenv("KAGGLE_USERNAME", "other-synthetic-account")
    monkeypatch.setenv("KAGGLE_KEY", "OTHER_SYNTHETIC_LEGACY_4000")
    assert inspect_kaggle_credentials()["configured"]
    with kaggle_credential_environment() as env:
        assert env["KAGGLE_API_TOKEN"] == "HOST_SYNTHETIC_TOKEN_5000"
        assert "KAGGLE_API_TOKEN" not in os.environ
        assert "KAGGLE_USERNAME" not in env and "KAGGLE_KEY" not in env
        assert env["KAGGLE_CONFIG_DIR"] != str(tmp_path)
        assert env["HOME"] == env["USERPROFILE"] == env["KAGGLE_CONFIG_DIR"]
    assert "KAGGLE_API_TOKEN" not in env
    assert not Path(env["KAGGLE_CONFIG_DIR"]).exists()
    assert os.environ["KAGGLE_USERNAME"] == "other-synthetic-account"
    assert "HOST_SYNTHETIC_TOKEN_5000" not in json.dumps(inspect_kaggle_credentials())


def test_explicit_environment_not_mutated():
    source = {"KAGGLE_CONFIG_DIR": "configured-directory"}
    assert apply_kaggle_config_env(source) == source
    assert apply_kaggle_config_env(source) is not source


def test_live_kaggle_report_never_contains_account_or_directory(sample_docx, tmp_path, monkeypatch):
    from fsf.live import kaggle_check
    monkeypatch.setenv("KAGGLE_USERNAME", "REPORT_SYNTHETIC_ACCOUNT_8000")
    monkeypatch.setenv("KAGGLE_KEY", "REPORT_SYNTHETIC_SECRET_8000")
    monkeypatch.setattr(kaggle_check, "ping_kaggle_api", lambda: {"ok": True})
    report = kaggle_check.verify_kaggle_work(sample_docx, tmp_path/"live")
    assert report["ok"] and report["credentials"]["authenticated"] is True
    text = json.dumps(report)
    for forbidden in ("REPORT_SYNTHETIC_ACCOUNT_8000", "REPORT_SYNTHETIC_SECRET_8000", str(tmp_path)):
        assert forbidden not in text


@pytest.mark.parametrize("message,expected", [
    ("401 synthetic-private-account", "KAGGLE_AUTH_REJECTED"),
    ("403 synthetic-private-account", "KAGGLE_FORBIDDEN"),
    ("getaddrinfo synthetic-private-account", "KAGGLE_UNREACHABLE"),
])
def test_probe_errors_classified_once_without_account_echo(message, expected, monkeypatch):
    from types import SimpleNamespace
    from fsf.live import kaggle_check
    calls = []
    monkeypatch.setattr(kaggle_check, "kaggle_bin", lambda: "synthetic-kaggle")
    def run(*a, **kw):
        calls.append(1)
        return SimpleNamespace(returncode=1, timed_out=False, stdout="", stderr=message)
    monkeypatch.setattr(kaggle_check, "run_safe_subprocess", run)
    result = kaggle_check.ping_kaggle_api()
    assert result["code"] == expected and len(calls) == 1
    assert "synthetic-private-account" not in json.dumps(result)


def test_expired_host_binding_fails_without_using_legacy(tmp_path, monkeypatch):
    import sys
    from fsf.live.credentials import kaggle_credential_environment
    from fsf.remote.contracts import RemoteError
    helper = tmp_path/"expired.py"
    helper.write_text("import json\nprint(json.dumps({'value':'SYNTHETIC_EXPIRED_9000','revision':'r1','expires_at':1}))\n")
    path = tmp_path/"binding.json"
    path.write_text(json.dumps({"schema_version":"fsf_credential_binding_v1", "profile_id":"kaggle",
        "source":"host_secret_manager", "reference":"kaggle-token", "revision":"r1", "authorized":True,
        "resolver":[sys.executable,str(helper)]}))
    monkeypatch.setenv("FSF_KAGGLE_CREDENTIAL_BINDING_FILE", str(path))
    monkeypatch.setenv("KAGGLE_USERNAME", "valid-other-account")
    monkeypatch.setenv("KAGGLE_KEY", "VALID_SYNTHETIC_LEGACY_9000")
    with pytest.raises(RemoteError, match="API_SECRET_EXPIRED"):
        with kaggle_credential_environment():
            pytest.fail("expired credential accepted")
