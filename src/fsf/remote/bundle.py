"""Bounded portable bundles; archive paths are data, never extraction targets."""
import copy
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from fsf import __version__
from fsf.core.asset_library import _root, _open_reader, _no_links
from fsf.core.edit_store import edit_lock, read_json
from fsf.remote.contracts import (RemoteError, MAX_BUNDLE_BYTES, MAX_ENTRIES, digest, sha,
    seal, shape, safe_path, write_json, file_item, validate_recipe, validate_profile,
    validate_request, compile_child_plan, check_authorization)


def copy_bounded(source, target):
    source = Path(source)
    _no_links(source)
    total = 0
    with _open_reader(source) as incoming, Path(target).open("xb") as outgoing:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            raise RemoteError("REMOTE_PATH")
        while data := incoming.read(1024*1024):
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            outgoing.write(data)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def pack(folder, paths, target):
    paths = sorted(paths)
    if len(paths) > MAX_ENTRIES or len(paths) != len(set(paths)):
        raise RemoteError("REMOTE_ARCHIVE")
    total = 0
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in paths:
            path = safe_path(folder, name)
            total += path.stat().st_size
            if total > MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, path.read_bytes())
    if Path(target).stat().st_size > MAX_BUNDLE_BYTES:
        raise RemoteError("REMOTE_SIZE_LIMIT")


def unpack(archive_path, folder):
    _no_links(Path(archive_path).absolute())
    if not 0 < Path(archive_path).stat().st_size <= MAX_BUNDLE_BYTES:
        raise RemoteError("REMOTE_SIZE_LIMIT")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        if len(names) > MAX_ENTRIES or len(names) != len(set(names)) or archive.comment:
            raise RemoteError("REMOTE_ARCHIVE")
        total = 0
        for entry in entries:
            # ZipInfo normalizes backslashes on Windows. Its original name
            # retains the archive spelling and must be checked before use.
            if entry.orig_filename!=entry.filename or "\\" in entry.orig_filename:
                raise RemoteError("REMOTE_ARCHIVE")
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise RemoteError("REMOTE_ARCHIVE")
            if entry.flag_bits & 1:
                raise RemoteError("REMOTE_ARCHIVE")
            total += entry.file_size
            if total > MAX_BUNDLE_BYTES or entry.file_size > MAX_BUNDLE_BYTES:
                raise RemoteError("REMOTE_SIZE_LIMIT")
            path = safe_path(folder, entry.filename, exists=False)
            if path.exists():
                raise RemoteError("REMOTE_ARCHIVE")
            with archive.open(entry) as source, path.open("xb") as target:
                count = 0
                while data := source.read(1024*1024):
                    count += len(data)
                    if count > entry.file_size:
                        raise RemoteError("REMOTE_SIZE_LIMIT")
                    target.write(data)
                if count != entry.file_size:
                    raise RemoteError("REMOTE_ARCHIVE")
    return set(names)


def bundle_identity(request):
    return digest({"request_digest": request["request_digest"], "inputs": request["inputs"], "worker_wheel": request["worker_wheel"]})


def read_bundle(folder, names=None):
    request = validate_request(read_json(Path(folder)/"request.json"))
    expected = {"request.json", request["worker_wheel"]["path"], *(a["path"] for a in request["inputs"])}
    if names is not None and names != expected:
        raise RemoteError("REMOTE_UNDECLARED_FILE")
    for item in [request["worker_wheel"], *request["inputs"]]:
        path = safe_path(folder, item["path"])
        if sha(path) != item["sha256"] or path.stat().st_size != item["size_bytes"]:
            raise RemoteError("REMOTE_INPUT_INTEGRITY")
    return request


def prepare_bundle(definition, recipe, *, inputs_root, wheel, profile, output_root):
    """Create an immutable dispatch request from approved local declarations."""
    recipe = validate_recipe(recipe)
    profile = validate_profile(profile)
    from fsf.remote.preflight import validate_definition
    validate_definition(definition, recipe)
    if not isinstance(definition, dict) or set(definition) != {"parent", "parameters", "inputs", "video_condition", "consistency_requirements", "expected_media_contract"}:
        raise RemoteError("REMOTE_DEFINITION")
    root = _root(output_root)
    source = _root(inputs_root)
    wheel = Path(wheel).absolute()
    _no_links(wheel)
    if not wheel.name.startswith("free_studio_flow-") or not wheel.name.endswith("-py3-none-any.whl"):
        raise RemoteError("REMOTE_WHEEL")
    if not isinstance(definition["inputs"], dict) or set(definition["inputs"]) != {a["artifact_id"] for a in recipe["inputs"]}:
        raise RemoteError("REMOTE_INPUTS")
    with edit_lock(root/"prepare"):
        scratch = Path(tempfile.mkdtemp(prefix=".remote-prepare-", dir=root))
        archive_path = None
        try:
            wheel_rel = "runtime/"+wheel.name
            copy_bounded(wheel, safe_path(scratch, wheel_rel, exists=False))
            inputs = []
            for spec in recipe["inputs"]:
                path = safe_path(source, definition["inputs"][spec["artifact_id"]])
                extension = path.suffix.lower()
                import re
                if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
                    extension = ".bin"
                rel = "inputs/"+spec["artifact_id"]+extension
                dest = safe_path(scratch, rel, exists=False)
                copy_bounded(path, dest)
                inputs.append(file_item(dest, spec["artifact_id"], rel, spec["role"], spec["media_type"]))
                if sha(path) != inputs[-1]["sha256"]:
                    raise RemoteError("REMOTE_SOURCE_CHANGED")
            from fsf.remote import worker
            identity = digest({"parent": definition["parent"], "recipe": recipe["recipe_digest"], "inputs": inputs,
                               "parameters": definition["parameters"], "profile": digest(profile)})
            request = {"schema_version": "fsf_remote_request_v1", "protocol_version": "1", "fsf_version": __version__,
                "parent": definition["parent"], "profile_id": profile["profile_id"], "adapter_digest": digest(profile),
                "child": {"job_id": "child_"+identity[:24], "run_id": "run_"+identity[24:48], "attempt_id": definition["parent"]["attempt_id"],
                          "plan": compile_child_plan(recipe)}, "recipe": recipe, "parameters": definition["parameters"],
                "inputs": inputs, "policy": copy.deepcopy(profile["policy"]), "worker_core_sha256": sha(Path(worker.__file__)),
                "worker_wheel": file_item(scratch/wheel_rel, "worker_wheel", wheel_rel, "data", "application/zip"),
                "video_condition": definition["video_condition"], "consistency_requirements": definition["consistency_requirements"],
                "expected_media_contract": definition["expected_media_contract"]}
            request = seal(request, "request_digest")
            check_authorization(request, profile)
            # The wheel must contain precisely this worker implementation.
            with zipfile.ZipFile(wheel) as installed:
                import hashlib
                if hashlib.sha256(installed.read("fsf/remote/worker.py")).hexdigest() != request["worker_core_sha256"]:
                    raise RemoteError("REMOTE_STALE_WHEEL")
            write_json(scratch/"request.json", request)
            paths = {"request.json", wheel_rel, *(a["path"] for a in inputs)}
            archive_path = root/(".pending-"+request["request_digest"]+".zip")
            archive_path.unlink(missing_ok=True)
            pack(scratch, paths, archive_path)
            target = root/(request["request_digest"]+".zip")
            if target.exists():
                if sha(archive_path) != sha(target):
                    raise RemoteError("REMOTE_BUNDLE_CONFLICT")
                archive_path.unlink()
            else:
                os.replace(archive_path, target)
            return {"dispatch_id": "dispatch_"+request["request_digest"][:40], "bundle": target.name,
                    "request_digest": request["request_digest"], "bundle_digest": bundle_identity(request)}
        finally:
            shutil.rmtree(scratch)
            if archive_path:
                archive_path.unlink(missing_ok=True)
