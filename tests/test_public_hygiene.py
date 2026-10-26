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
    upgrade = tmp_path / "upgrade-film-vr-gaming-v1.md"
    upgrade.write_text("plan\n", encoding="utf-8")
    upgrade_hits = hygiene.inspect_entry("docs/upgrade-film-vr-gaming-v1.md", upgrade)
    assert upgrade_hits
    assert "denied filename" in upgrade_hits[0]


def test_hygiene_flags_user_paths(tmp_path: Path) -> None:
    """Windows and Unix user paths (username leak) must be rejected."""
    hygiene = _hygiene()

    win_user = tmp_path / "win_user.py"
    win_user.write_text(
        'path = r"C:' + '\\\\Users\\\\SomeUser\\\\AppData\\\\Local\\\\Temp\\\\foo.txt"\n',
        encoding="utf-8",
    )
    win_hits = hygiene.inspect_entry("src/win_user.py", win_user)
    assert any("username leak" in hit for hit in win_hits), win_hits

    unix_user = tmp_path / "unix_user.py"
    unix_user.write_text(
        'path = "/' + 'home/someuser/projects/fsf/run.py"\n',
        encoding="utf-8",
    )
    unix_hits = hygiene.inspect_entry("src/unix_user.py", unix_user)
    assert any("username leak" in hit for hit in unix_hits), unix_hits

    mac_user = tmp_path / "mac_user.py"
    mac_user.write_text(
        'path = "/' + 'Users/JohnDoe/dev/free-studio-flow"\n',
        encoding="utf-8",
    )
    mac_hits = hygiene.inspect_entry("src/mac_user.py", mac_user)
    assert any("username leak" in hit for hit in mac_hits), mac_hits

    # URLs with /Users/ org segment must NOT be flagged (e.g. GitHub URLs)
    url_safe = tmp_path / "url_safe.py"
    url_safe.write_text(
        'url = "https://github.com/freestudioflow/free-studio-flow"\n',
        encoding="utf-8",
    )
    url_hits = hygiene.inspect_entry("src/url_safe.py", url_safe)
    assert not any("username leak" in hit for hit in url_hits), url_hits


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


@pytest.mark.parametrize("username", ["a", "some user", "\u738b\u660e"])
@pytest.mark.parametrize("root", ["home", "Users"])
def test_hygiene_flags_short_and_unicode_home_paths(tmp_path: Path, username: str, root: str) -> None:
    hygiene = _hygiene()
    sample = tmp_path / "sample.py"
    sample.write_text('path = "/' + root + '/' + username + '/project/file"', encoding="utf-8")
    assert any("username leak" in hit for hit in hygiene.inspect_entry("src/sample.py", sample))


def test_hygiene_marker_never_suppresses_secrets(tmp_path: Path) -> None:
    hygiene = _hygiene()
    sample = tmp_path / "sample.py"
    sample.write_text('token = "ghp_' + 'a' * 30 + '" # hygiene: skip-line', encoding="utf-8")
    for relative in ("src/sample.py", "tests/test_public_hygiene.py", "scripts/check_public_hygiene.py"):
        assert any("secret-like" in hit for hit in hygiene.inspect_entry(relative, sample))


@pytest.mark.parametrize("defect", ["secret", "oversize", "link"])
def test_hygiene_scans_scanner_in_alternate_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str) -> None:
    hygiene = _hygiene()
    scanner = tmp_path / "scripts" / "check_public_hygiene.py"
    scanner.parent.mkdir()
    scanner.write_text("token ghp_" + "a" * 30 if defect == "secret" else "safe", encoding="utf-8")
    if defect == "oversize":
        scanner.write_bytes(b"x" * (hygiene.MAX_FILE_BYTES + 1))
    if defect == "link":
        monkeypatch.setattr(hygiene, "_is_link", lambda path: path == scanner)
    monkeypatch.setattr(hygiene, "PUBLIC_FILES", {"scripts/check_public_hygiene.py"})
    monkeypatch.setattr(hygiene, "tracked_files", lambda root: [scanner])
    hits = hygiene.scan_public_tree(tmp_path)
    expected = {"secret": "secret-like", "oversize": "file larger", "link": "symlink"}[defect]
    assert any(expected in hit for hit in hits), hits


def test_hygiene_does_not_flag_url_home_segments(tmp_path: Path) -> None:
    hygiene = _hygiene()
    sample = tmp_path / "sample.py"
    sample.write_text('url = "https://example.com/' + 'Users/alice/project"', encoding="utf-8")
    assert hygiene.inspect_entry("src/sample.py", sample) == []


def test_hygiene_includes_untracked_sources_and_excludes_operator_ignored_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hygiene = _hygiene()
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("Public instructions.\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("operator-private/\n", encoding="utf-8")
    monkeypatch.setattr(hygiene, "PUBLIC_FILES", {"README.md", ".gitignore"})
    assert hygiene.scan_public_tree(tmp_path) == []
    private = tmp_path / "operator-private"
    private.mkdir()
    (private / "token.txt").write_text("ghp_" + "a" * 30, encoding="utf-8")
    assert hygiene.scan_public_tree(tmp_path) == []
    source = tmp_path / "src"
    source.mkdir()
    (source / "unsafe.py").write_text('token = "ghp_' + "a" * 30 + '"', encoding="utf-8")
    assert any("src/unsafe.py" in hit and "secret-like" in hit for hit in hygiene.scan_public_tree(tmp_path))


@pytest.mark.parametrize("body", ["a" * 36, "123_" + "a-b_" * 80 + "." + "c" * 100 + "." + "d" * 100])
def test_installation_tokens_scanned_and_scrubbed(tmp_path: Path, body: str) -> None:
    from fsf.executors.subprocess_safety import sanitize_process_text, scrub_string_secrets
    from fsf.runtime.external_op import scrub_dict_secrets

    token = "ghs_" + body
    sample = tmp_path / "sample.txt"
    sample.write_text("output: " + token, encoding="utf-8")
    hits = _hygiene().inspect_entry("sample.txt", sample)
    assert any("secret-like token pattern" in hit for hit in hits)
    assert all(token not in hit for hit in hits)
    assert scrub_string_secrets("before " + token + " after") == "before [REDACTED_SECRET] after"
    assert sanitize_process_text({"output": [token]}) == {"output": ["[REDACTED_SECRET]"]}
    assert scrub_dict_secrets({"output": [token]}) == {"output": ["[REDACTED_SECRET]"]}


def test_installation_token_documentation_is_not_a_credential() -> None:
    from fsf.executors.subprocess_safety import scrub_string_secrets
    text = "ghs_ prefix and ghs_example are documentation"
    assert scrub_string_secrets(text) == text
    assert not any(p.search(text) for p in _hygiene().SECRET_PATTERNS)
