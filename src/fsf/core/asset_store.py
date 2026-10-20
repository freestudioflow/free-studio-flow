"""Immutable local asset bundles; cooperative locking, not hostile-filesystem isolation."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shutil
import tempfile

from fsf.core.asset_library import AssetValidationError, _root, validate_asset_record
from fsf.core.edit_store import edit_lock, read_json


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise AssetValidationError("ASSET_KEY")
    return value


def _folder(root, asset_id, version):
    return _root(root) / (_key(asset_id) + "@" + _key(version))


def lookup_asset(root, asset_id, version):
    folder = _folder(root, asset_id, version)
    value = validate_asset_record(read_json(folder / "record.json"), resource_root=folder)
    if value["asset_id"] != asset_id or value["asset_version"] != version:
        raise AssetValidationError("ASSET_IDENTITY")
    return value


def register_asset(root, record, *, resource_root):
    """Copy verified resources; publish a whole directory once, without overwrites."""
    base = _root(root)
    value = validate_asset_record(record, resource_root=resource_root)
    folder = _folder(base, value["asset_id"], value["asset_version"])
    # Stable lock names and case-folding prevent portable-key aliases.
    lock = base / (folder.name.casefold() + ".entry")
    with edit_lock(lock):
        aliases = [p for p in base.iterdir() if p.is_dir() and p.name.casefold() == folder.name.casefold()]
        if aliases:
            if len(aliases) != 1 or aliases[0].name != folder.name:
                raise AssetValidationError("ASSET_KEY_COLLISION")
            prior = lookup_asset(base, value["asset_id"], value["asset_version"])
            if prior["record_digest"] != value["record_digest"]:
                raise AssetValidationError("ASSET_VERSION_CONFLICT")
            return prior
        scratch = Path(tempfile.mkdtemp(prefix=".asset-", dir=base))
        try:
            source = _root(resource_root)
            for artifact in value["artifacts"]:
                if artifact["logical_path"].split("/")[0].casefold() == "record.json":
                    raise AssetValidationError("ASSET_RESERVED_PATH")
                target = scratch / artifact["logical_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / artifact["locator"], target)
                artifact["locator"] = artifact["logical_path"]
            validate_asset_record(value, resource_root=scratch)
            # Detect source mutation during the copy too.
            validate_asset_record(record, resource_root=resource_root)
            import json
            with (scratch / "record.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(scratch, folder)
            return copy.deepcopy(value)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)


def asset_context_ref(record):
    """Existing consistency asset projection; preserve reference order and hashes."""
    return {key: copy.deepcopy(record[key]) for key in ("asset_id", "content_hash", "ref_images")} | {
        "version": record["asset_version"]}
