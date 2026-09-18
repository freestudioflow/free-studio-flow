"""Verify that local release assets exactly match GitHub and package metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError(f"expected one METADATA file in {path.name}")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
    return metadata["Name"], metadata["Version"]


def _expected_names(name: str, version: str) -> tuple[str, set[str]]:
    distribution_stem = name.replace("-", "_")
    wheel_name = f"{distribution_stem}-{version}-py3-none-any.whl"
    sdists = {
        f"{distribution_stem}-{version}.tar.gz",
        f"{name}-{version}.tar.gz",
    }
    return wheel_name, sdists


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--release-json", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()

    project = tomllib.loads((args.source / "pyproject.toml").read_text("utf-8"))["project"]
    name = project["name"]
    version = project["version"]
    if args.tag != f"v{version}":
        raise ValueError(f"tag {args.tag!r} does not match package version {version!r}")

    wheel_name, expected_sdists = _expected_names(name, version)
    local_files = [path for path in args.dist.iterdir() if path.is_file()]
    local_names = {path.name for path in local_files}
    wheels = {path.name for path in local_files if path.suffix == ".whl"}
    sdists = {path.name for path in local_files if path.name.endswith(".tar.gz")}
    if wheels != {wheel_name}:
        raise ValueError(f"wheel mismatch: expected {wheel_name!r}, got {wheels}")
    if len(sdists) != 1 or next(iter(sdists)) not in expected_sdists:
        raise ValueError(f"sdist mismatch: expected one of {expected_sdists}, got {sdists}")

    release = json.loads(args.release_json.read_text("utf-8"))
    if release["tag_name"] != args.tag:
        raise ValueError("GitHub release tag does not match requested tag")
    remote_digests = {asset["name"]: asset.get("digest") for asset in release["assets"]}
    for asset_name in local_names:
        local_digest = f"sha256:{_sha256(args.dist / asset_name)}"
        if remote_digests.get(asset_name) != local_digest:
            raise ValueError(f"digest mismatch for {asset_name}")

    metadata_name, metadata_version = _wheel_metadata(args.dist / wheel_name)
    if (metadata_name, metadata_version) != (name, version):
        raise ValueError("wheel metadata does not match pyproject.toml")
    print(f"release assets PASS: {name} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
