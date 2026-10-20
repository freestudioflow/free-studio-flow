"""Read-only library record validation, without registration or media decoding."""
from __future__ import annotations

import copy
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from jsonschema import Draft202012Validator

from fsf.conformance.digest import canonical_contract_digest
from fsf.core.consistency import canonical_asset_digest
from fsf.core.edit_store import _open_reader as _windows_reader

KINDS = frozenset({"unspecified", "character", "scene", "prop", "style", "audio", "animation"})
ROLE_MEDIA_TYPES = {
    "model": frozenset({"model/gltf-binary", "model/gltf+json"}),
    "scene": frozenset({"application/x-blender", "model/gltf-binary", "model/gltf+json"}),
    "texture": frozenset({"image/png", "image/jpeg", "image/webp"}),
    "reference_image": frozenset({"image/png", "image/jpeg", "image/webp"}),
    "audio": frozenset({"audio/wav", "audio/mpeg", "audio/flac", "audio/ogg"}),
    "animation": frozenset({"model/gltf-binary", "model/gltf+json"}),
    "other": frozenset({"application/json", "text/plain", "application/octet-stream"}),
}
HASH_CHUNK_BYTES = 1024 * 1024


def _open_reader(path):
    if os.name == "nt":
        return _windows_reader(path)
    # Do not block on a FIFO substituted between path inspection and open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class AssetValidationError(ValueError):
    """A fixed diagnostic code, never raw operator data or filesystem paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_library_record_digest(record):
    """Bind all record fields except the digest itself and location hints."""
    try:
        payload = copy.deepcopy(record)
        payload.pop("record_digest", None)
        for artifact in payload["artifacts"]:
            artifact.pop("locator", None)
        return canonical_contract_digest(payload)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AssetValidationError("ASSET_INVALID") from None


def _path_parts(value):
    # Portable relative POSIX names; never normalize an unsafe name into safety.
    if not value or "\\" in value or re.search(r'[\x00-\x1f\x7f:<>"|?*]', value):
        raise AssetValidationError("ASSET_PATH")
    parts = value.split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if any(part in ("", ".", "..") or part.endswith((".", " "))
           or part.split(".")[0].casefold() in reserved for part in parts):
        raise AssetValidationError("ASSET_PATH")
    return parts


def _no_links(path):
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AssetValidationError("ASSET_PATH")


def _root(resource_root):
    try:
        path = Path(resource_root).absolute()
        _no_links(path)
        if not path.is_dir():
            raise AssetValidationError("ASSET_PATH")
        return path.resolve(strict=True)
    except (OSError, ValueError, TypeError):
        raise AssetValidationError("ASSET_PATH") from None


def _hash_artifact(root, artifact):
    try:
        path = root.joinpath(*_path_parts(artifact["locator"]))
        _no_links(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise AssetValidationError("ASSET_FILE")
        path.resolve(strict=True).relative_to(root)
        with _open_reader(path) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetValidationError("ASSET_FILE")
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(HASH_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AssetValidationError("ASSET_FILE_CHANGED")
        if size != artifact["size_bytes"]:
            raise AssetValidationError("ASSET_SIZE")
        if digest.hexdigest() != artifact["sha256"]:
            raise AssetValidationError("ASSET_SHA256")
    except AssetValidationError:
        raise
    except (OSError, ValueError):
        raise AssetValidationError("ASSET_FILE") from None


def validate_asset_record(record, *, resource_root):
    """Verify declarations and local bytes, return an isolated record copy.

    No record, index or version is written. MIME/role checks do not decode files
    or certify rendering, dependency completeness, Gaming or VR suitability.
    """
    value = copy.deepcopy(record)
    schema_name = value.get("schema_version") if isinstance(value, dict) else None
    if schema_name not in {"fsf_library_asset_record_v1", "fsf_library_asset_record_v2"}:
        raise AssetValidationError("ASSET_SCHEMA")
    schema = json.loads(resources.files("fsf.schemas").joinpath(schema_name + ".schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(value):
        raise AssetValidationError("ASSET_SCHEMA")
    if value["kind"] not in KINDS:
        raise AssetValidationError("ASSET_KIND")
    identifiers, paths = set(), set()
    for artifact in value["artifacts"]:
        if artifact["artifact_id"] in identifiers:
            raise AssetValidationError("ASSET_ARTIFACT_ID")
        identifiers.add(artifact["artifact_id"])
        parts = _path_parts(artifact["logical_path"])
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if any(key == known or key.startswith(known + "/") or known.startswith(key + "/") for known in paths):
            raise AssetValidationError("ASSET_PATH_COLLISION")
        paths.add(key)
        _path_parts(artifact["locator"])
        pairs = ROLE_MEDIA_TYPES if schema_name.endswith("_v1") else {
            **ROLE_MEDIA_TYPES, "control_video": frozenset({"video/mp4"}), "control_frame": frozenset({"image/png"})}
        if artifact["media_type"] not in pairs[artifact["role"]]:
            raise AssetValidationError("ASSET_MEDIA_TYPE")
    if value["primary_artifact_id"] not in identifiers:
        raise AssetValidationError("ASSET_PRIMARY")
    reference_shas = {item["sha256"] for item in value["artifacts"] if item["role"] == "reference_image"}
    if any(item["sha256"] not in reference_shas for item in value["ref_images"]):
        raise AssetValidationError("ASSET_REFERENCE")
    try:
        expected = canonical_asset_digest(value["asset_id"], value["asset_version"], value["source_prompt"],
                                          value["kind"], value["constraints"], value["ref_images"])
    except (ValueError, TypeError):
        raise AssetValidationError("ASSET_INVALID") from None
    if expected != value["content_hash"]:
        raise AssetValidationError("ASSET_CONTENT_HASH")
    if canonical_library_record_digest(value) != value["record_digest"]:
        raise AssetValidationError("ASSET_RECORD_DIGEST")
    root = _root(resource_root)
    for artifact in value["artifacts"]:
        _hash_artifact(root, artifact)
    return value
