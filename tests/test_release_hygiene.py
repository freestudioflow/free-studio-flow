"""Public package metadata and release-boundary checks."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_public_package_metadata_is_complete() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "free-studio-flow"
    assert project["version"] == "0.1.0rc1"
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "FSF Team", "email": "info@prodocux.com"}]
    assert project["urls"]["Homepage"] == "https://github.com/freestudioflow/free-studio-flow"
    assert project["urls"]["Repository"] == "https://github.com/freestudioflow/free-studio-flow"
    assert project["urls"]["Issues"] == "https://github.com/freestudioflow/free-studio-flow/issues"
    assert project["urls"]["Releases"] == "https://github.com/freestudioflow/free-studio-flow/releases"


def test_package_version_matches_runtime() -> None:
    from fsf import __version__

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __version__ == project["version"]


def test_readme_release_status_matches_source_version() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "update_readme_after_publish.py"),
            "--check-source-version",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_public_markdown_has_no_windows_checkout_paths() -> None:
    windows_path = re.compile(r"(?i)\b[A-Z]:\\")
    found = []
    for path in ROOT.rglob("*.md"):
        relative = path.relative_to(ROOT)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if relative.parts[0] in {"build", "dist", "output", "internal"}:
            continue
        if windows_path.search(path.read_text(encoding="utf-8")):
            found.append(relative.as_posix())
    assert found == []


def test_public_markdown_has_no_cjk() -> None:
    hygiene = _load("check_public_hygiene", "scripts/check_public_hygiene.py")
    found = []
    for path in ROOT.rglob("*.md"):
        relative = path.relative_to(ROOT)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if relative.parts[0] in {"build", "dist", "output", "internal"}:
            continue
        if hygiene.CJK.search(path.read_text(encoding="utf-8")):
            found.append(relative.as_posix())
    assert found == []


def test_verify_release_assets_accepts_matching_github_digests(tmp_path: Path) -> None:
    assets = _load("verify_release_assets", "scripts/verify_release_assets.py")
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "free-studio-flow"\nversion = "0.1.0rc1"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "free_studio_flow-0.1.0rc1-py3-none-any.whl"
    sdist = dist / "free_studio_flow-0.1.0rc1.tar.gz"
    metadata = b"Metadata-Version: 2.4\nName: free-studio-flow\nVersion: 0.1.0rc1\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("free_studio_flow-0.1.0rc1.dist-info/METADATA", metadata)
    sdist.write_bytes(b"sdist-bytes")
    release_json = tmp_path / "release.json"
    release_json.write_text(
        json.dumps(
            {
                "tag_name": "v0.1.0rc1",
                "assets": [
                    {"name": wheel.name, "digest": f"sha256:{assets._sha256(wheel)}"},
                    {"name": sdist.name, "digest": f"sha256:{assets._sha256(sdist)}"},
                ],
            }
        ),
        encoding="utf-8",
    )
    argv = sys.argv
    sys.argv = [
        "verify_release_assets.py",
        "--tag",
        "v0.1.0rc1",
        "--dist",
        str(dist),
        "--release-json",
        str(release_json),
        "--source",
        str(source),
    ]
    try:
        assert assets.main() == 0
    finally:
        sys.argv = argv
