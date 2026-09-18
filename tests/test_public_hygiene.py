"""Public hygiene gate: current tree plus injected negative cases."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _hygiene():
    spec = importlib.util.spec_from_file_location(
        "check_public_hygiene",
        ROOT / "scripts" / "check_public_hygiene.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_public_hygiene_clean() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_hygiene.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "clean" in completed.stdout


def test_hygiene_flags_secret_denied_large_and_missing(tmp_path: Path) -> None:
    hygiene = _hygiene()
    secret = tmp_path / "leak.txt"
    secret.write_text("token ghp_" + ("a" * 24) + "\n", encoding="utf-8")
    google = tmp_path / "google.txt"
    google.write_text("key AIza" + ("B" * 35) + "\n", encoding="utf-8")
    denied = tmp_path / "kaggle.json"
    denied.write_text("{}", encoding="utf-8")
    db = tmp_path / "cache.sqlite"
    db.write_bytes(b"sqlite")
    log = tmp_path / "debug.log"
    log.write_text("log\n", encoding="utf-8")
    pyd = tmp_path / "native.pyd"
    pyd.write_bytes(b"x")
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"x" * 32)

    hits = []
    hits.extend(hygiene.inspect_entry("leak.txt", secret))
    hits.extend(hygiene.inspect_entry("google.txt", google))
    hits.extend(hygiene.inspect_entry("kaggle.json", denied))
    hits.extend(hygiene.inspect_entry("cache.sqlite", db))
    hits.extend(hygiene.inspect_entry("debug.log", log))
    hits.extend(hygiene.inspect_entry("native.pyd", pyd))
    hits.extend(hygiene.inspect_entry("huge.bin", huge, max_bytes=8))
    hits.extend(hygiene.inspect_entry("missing.txt", tmp_path / "no-such-file"))

    joined = "\n".join(hits)
    assert "secret-like token pattern" in joined
    assert "kaggle.json: denied filename" in joined
    assert "denied suffix .sqlite" in joined
    assert "denied suffix .log" in joined
    assert "denied suffix .pyd" in joined
    assert "file larger than 8 bytes" in joined
    assert "tracked path is missing" in joined


def test_hygiene_flags_symlink_before_missing(tmp_path: Path) -> None:
    hygiene = _hygiene()
    original = hygiene._is_link
    hygiene._is_link = lambda path: True
    try:
        hits = hygiene.inspect_entry("broken.txt", tmp_path / "does-not-exist")
    finally:
        hygiene._is_link = original
    assert hits
    assert "symlink" in hits[0]
    assert "missing" not in hits[0]


def test_hygiene_flags_symlink(tmp_path: Path) -> None:
    hygiene = _hygiene()
    target = tmp_path / "target.txt"
    target.write_text("ok\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not permitted on this Windows host")
    hits = hygiene.inspect_entry("link.txt", link)
    assert hits
    assert "symlink" in hits[0]

    broken = tmp_path / "broken.txt"
    try:
        broken.symlink_to(tmp_path / "does-not-exist")
    except OSError:
        pytest.skip("symlink creation is not permitted on this Windows host")
    broken_hits = hygiene.inspect_entry("broken.txt", broken)
    assert broken_hits
    assert "symlink" in broken_hits[0]
    assert "missing" not in broken_hits[0]


def test_hygiene_rejects_internal_docs_and_cjk(tmp_path: Path) -> None:
    hygiene = _hygiene()
    assert hygiene.is_allowlisted("docs/RELEASE.md")
    assert hygiene.is_allowlisted("src/fsf/cli/main.py")
    assert not hygiene.is_allowlisted("docs/FSF_M1_IMPLEMENTATION.md")
    assert not hygiene.is_allowlisted("internal/docs/plan.md")
    chinese = tmp_path / "note.md"
    chinese.write_text("# 內部說明\n", encoding="utf-8")
    hits = hygiene.inspect_entry("docs/note.md", chinese)
    assert any("CJK" in hit for hit in hits)
    plan = tmp_path / "FREE_STUDIO_FLOW_INTEGRATION_PLAN_V3.md"
    plan.write_text("plan\n", encoding="utf-8")
    denied = hygiene.inspect_entry("docs/FREE_STUDIO_FLOW_INTEGRATION_PLAN_V3.md", plan)
    assert denied
    assert "denied filename" in denied[0]


def test_hygiene_flags_env_openai_key_and_full_cjk(tmp_path: Path) -> None:
    hygiene = _hygiene()

    env_prod = tmp_path / ".env.production"
    env_prod.write_text("FSF_VENDOR_API_KEY=placeholder\n", encoding="utf-8")
    env_hits = hygiene.inspect_entry("src/.env.production", env_prod)
    assert env_hits
    assert "denied filename" in env_hits[0]

    env_local = tmp_path / ".env.local"
    env_local.write_text("x=1\n", encoding="utf-8")
    assert "denied filename" in hygiene.inspect_entry("src/.env.local", env_local)[0]

    env_plain = tmp_path / ".env"
    env_plain.write_text("x=1\n", encoding="utf-8")
    assert "denied filename" in hygiene.inspect_entry("src/.env", env_plain)[0]

    example = tmp_path / ".env.example"
    example.write_text("FSF_VENDOR_API_KEY=\n", encoding="utf-8")
    assert hygiene.inspect_entry("src/.env.example", example) == []
    assert not hygiene.is_denied_filename(".env.example")
    assert hygiene.is_denied_filename(".env.production")

    proj = tmp_path / "token.txt"
    proj.write_text("sk-proj-" + ("a" * 40) + "\n", encoding="utf-8")
    proj_hits = hygiene.inspect_entry("src/token.txt", proj)
    assert any("secret-like token pattern" in hit for hit in proj_hits)

    svc = tmp_path / "svc.txt"
    svc.write_text("sk-svcacct-" + ("b" * 32) + "\n", encoding="utf-8")
    svc_hits = hygiene.inspect_entry("src/svc.txt", svc)
    assert any("secret-like token pattern" in hit for hit in svc_hits)

    ext_a = tmp_path / "ext-a.md"
    ext_a.write_text("\u3400\n", encoding="utf-8")
    ext_hits = hygiene.inspect_entry("docs/ext-a.md", ext_a)
    assert any("CJK" in hit for hit in ext_hits)

    kana = tmp_path / "japanese.md"
    kana.write_text("カタカナ\n", encoding="utf-8")
    kana_hits = hygiene.inspect_entry("docs/japanese.md", kana)
    assert any("CJK" in hit for hit in kana_hits)

    hangul = tmp_path / "korean.md"
    hangul.write_text("한글\n", encoding="utf-8")
    hangul_hits = hygiene.inspect_entry("docs/korean.md", hangul)
    assert any("CJK" in hit for hit in hangul_hits)

    compat = tmp_path / "compat.md"
    compat.write_text("\uf900\n", encoding="utf-8")
    compat_hits = hygiene.inspect_entry("docs/compat.md", compat)
    assert any("CJK" in hit for hit in compat_hits)
