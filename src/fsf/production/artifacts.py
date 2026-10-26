"""Immutable chunk manifests and recoverable assembly on mounted worker disks."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from fsf.core.asset_library import _root, _no_links, _open_reader
from fsf.core.edit_store import edit_lock, read_json, _target
from fsf.production.contracts import ProductionError, digest, integer, publish, seal
from fsf.production.control import file_identity

CHUNK_BYTES = 8*1024*1024


def validate_manifest(value):
    if (not isinstance(value,dict) or set(value) != {"schema_version","sha256","size_bytes","chunk_bytes","chunks","manifest_digest"}
            or value["schema_version"] != "fsf_production_chunks_v1"
            or seal(value,"manifest_digest") != value or value["chunk_bytes"] != CHUNK_BYTES):
        raise ProductionError("PRODUCTION_CHUNK_INVALID")
    if not isinstance(value['chunks'],list) or not 1<=len(value['chunks'])<=8192:
        raise ProductionError('PRODUCTION_CHUNK_INVALID')
    integer(value['size_bytes'],1,64*1024**3)
    if not isinstance(value['sha256'],str) or len(value['sha256'])!=64 or any(c not in '0123456789abcdef' for c in value['sha256']):
        raise ProductionError('PRODUCTION_CHUNK_INVALID')
    offset = 0
    for item in value["chunks"]:
        if (set(item) != {"offset","size_bytes","sha256"} or type(item['offset']) is not int or item["offset"] != offset
                or type(item["size_bytes"]) is not int or not 1 <= item["size_bytes"] <= CHUNK_BYTES
                or not isinstance(item["sha256"],str) or len(item["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in item["sha256"])):
            raise ProductionError("PRODUCTION_CHUNK_INVALID")
        offset += item["size_bytes"]
    if not offset or offset != value["size_bytes"]:
        raise ProductionError("PRODUCTION_CHUNK_INVALID")
    return value


def split_artifact(source, *, chunk_root):
    root = _root(chunk_root)
    before = file_identity(source)
    chunks,offset,whole = [],0,hashlib.sha256()
    with edit_lock(root/".chunks"), _open_reader(Path(source).absolute()) as stream:
        while block := stream.read(CHUNK_BYTES):
            whole.update(block)
            sha = hashlib.sha256(block).hexdigest()
            item = {"offset":offset,"size_bytes":len(block),"sha256":sha}
            target = root/sha
            if target.exists():
                if file_identity(target) != {"sha256":sha,"size_bytes":len(block)}:
                    raise ProductionError("PRODUCTION_CHUNK_CHANGED")
            else:
                partial = root/(sha+".partial")
                if partial.exists():
                    _no_links(partial)
                with partial.open("wb") as outgoing:
                    outgoing.write(block);outgoing.flush();os.fsync(outgoing.fileno())
                os.replace(partial,target)
            chunks.append(item);offset += len(block)
            if offset > before["size_bytes"]:
                raise ProductionError("PRODUCTION_SOURCE_CHANGED")
        if whole.hexdigest() != before["sha256"] or offset != before["size_bytes"] or file_identity(source) != before:
            raise ProductionError("PRODUCTION_SOURCE_CHANGED")
        manifest = seal({"schema_version":"fsf_production_chunks_v1",**before,
                         "chunk_bytes":CHUNK_BYTES,"chunks":chunks},"manifest_digest")
        validate_manifest(manifest)
        publish(root/(manifest["manifest_digest"]+".json"),manifest)
        return manifest


def _append_chunk(stream,path,item):
    if file_identity(path) != {"sha256":item["sha256"],"size_bytes":item["size_bytes"]}:
        raise ProductionError("PRODUCTION_CHUNK_CHANGED")
    h,total = hashlib.sha256(),0
    with _open_reader(path) as incoming:
        while block := incoming.read(1024*1024):
            total += len(block)
            if total > item["size_bytes"]:
                raise ProductionError("PRODUCTION_CHUNK_CHANGED")
            h.update(block);stream.write(block)
    if total != item["size_bytes"] or h.hexdigest() != item["sha256"]:
        raise ProductionError("PRODUCTION_CHUNK_CHANGED")
    stream.flush();os.fsync(stream.fileno())


def assemble_artifact(manifest, *, chunk_root, output):
    value = validate_manifest(manifest)
    root,target = _root(chunk_root),_target(output)
    with edit_lock(target):
        expected = {k:value[k] for k in ("sha256","size_bytes")}
        if target.exists():
            if file_identity(target) != expected:
                raise ProductionError("PRODUCTION_TARGET_CONFLICT")
            return {**expected,"reused":True,"resumed_bytes":value["size_bytes"]}
        partial = target.with_name(".chunk-"+value["manifest_digest"]+".partial")
        checkpoint = target.with_name(".chunk-"+value["manifest_digest"]+".json")
        _target(partial);_target(checkpoint)
        count = 0
        if checkpoint.exists():
            state = read_json(checkpoint)
            if (set(state) != {"manifest_digest","complete_chunks"} or state["manifest_digest"] != value["manifest_digest"]):
                raise ProductionError("PRODUCTION_CHUNK_INVALID")
            count = integer(state["complete_chunks"],0,len(value["chunks"]))
        offset = sum(c["size_bytes"] for c in value["chunks"][:count])
        if count and not partial.exists():
            raise ProductionError("PRODUCTION_CHECKPOINT_LOST")
        if partial.exists():
            with _open_reader(partial) as incoming:
                for item in value["chunks"][:count]:
                    block = incoming.read(item["size_bytes"])
                    if hashlib.sha256(block).hexdigest() != item["sha256"]:
                        raise ProductionError("PRODUCTION_CHUNK_CHANGED")
        with partial.open("r+b" if partial.exists() else "w+b") as outgoing:
            outgoing.truncate(offset);outgoing.seek(offset)
            for index,item in enumerate(value["chunks"][count:],count):
                _append_chunk(outgoing,root/item["sha256"],item)
                publish(checkpoint,{"manifest_digest":value["manifest_digest"],"complete_chunks":index+1})
        if file_identity(partial) != expected:
            raise ProductionError("PRODUCTION_CHUNK_CHANGED")
        os.replace(partial,target)
        checkpoint.unlink(missing_ok=True)
        return {**expected,"reused":False,"resumed_bytes":offset}
