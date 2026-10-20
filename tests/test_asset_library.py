"""Library record integrity, portable paths and declared role compatibility."""
import copy
import hashlib
import os
import multiprocessing as mp
from pathlib import Path

import pytest

from fsf.core.asset_library import (
    AssetValidationError, KINDS, ROLE_MEDIA_TYPES, canonical_library_record_digest,
    validate_asset_record,
)
from fsf.core.consistency import canonical_asset_digest


def seal(value):
    value["content_hash"] = canonical_asset_digest(value["asset_id"], value["asset_version"],
        value["source_prompt"], value["kind"], value["constraints"], value["ref_images"])
    value["record_digest"] = canonical_library_record_digest(value)
    return value


def record(root, role="reference_image", media="image/png", kind="prop"):
    data = b"Synthetic bytes; not evidence of decodable media."
    (root / "reference.bin").write_bytes(data)
    return seal({"schema_version": "fsf_library_asset_record_v1", "asset_id": "prop_A",
        "asset_version": "1", "digest_version": "fsf_library_asset_record_digest_v1",
        "source_prompt": "A brass device.", "kind": kind,
        "constraints": {"preserve": [], "forbid": [], "unresolved": []},
        "ref_images": [], "artifacts": [{"artifact_id": "reference", "role": role,
        "logical_path": "reference.bin", "media_type": media, "locator": "reference.bin",
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}],
        "primary_artifact_id": "reference"})


@pytest.mark.parametrize("role,media", [(role, media) for role, types in ROLE_MEDIA_TYPES.items() for media in sorted(types)])
def test_all_declared_role_pairs_accept_primary_without_decoding(tmp_path, role, media):
    value = record(tmp_path, role, media)
    assert validate_asset_record(value, resource_root=tmp_path) == value


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_all_seven_kinds(tmp_path, kind):
    assert validate_asset_record(record(tmp_path, kind=kind), resource_root=tmp_path)["kind"] == kind


@pytest.mark.parametrize("kind", ["unknown", "Prop", " prop", "prop ", ""])
def test_unknown_or_noncanonical_kind_rejected(tmp_path, kind):
    with pytest.raises(AssetValidationError):
        validate_asset_record(record(tmp_path, kind=kind), resource_root=tmp_path)


@pytest.mark.parametrize("role,media", [("reference_image", "audio/wav"), ("audio", "image/png"),
    ("model", "image/png"), ("audio", "audio/x-wav"), ("audio", "Audio/wav"),
    ("audio", "audio/wav "), ("audio", "audio/wav; codecs=pcm"), ("other", "unknown/type"), ("unknown", "image/png")])
def test_role_media_rejections(tmp_path, role, media):
    with pytest.raises(AssetValidationError):
        validate_asset_record(record(tmp_path, role, media), resource_root=tmp_path)


@pytest.mark.parametrize("field,path", [(field, path) for field in ("logical_path", "locator") for path in
    ("/absolute", "../escape", "a/../escape", "https://example.test/file", "C:/private/file",
     "a\\b", "a//b", "./file", "NUL", "file.", "file ", "file:stream", "file\0")])
def test_unsafe_paths_rejected(tmp_path, field, path):
    value = record(tmp_path)
    value["artifacts"][0][field] = path
    seal(value)
    with pytest.raises(AssetValidationError):
        validate_asset_record(value, resource_root=tmp_path)


@pytest.mark.parametrize("mutation,code", [
    ("primary", "ASSET_PRIMARY"), ("duplicate_id", "ASSET_ARTIFACT_ID"),
    ("case_collision", "ASSET_PATH_COLLISION"), ("reference", "ASSET_REFERENCE"),
    ("size", "ASSET_SIZE"), ("sha", "ASSET_SHA256"), ("content", "ASSET_CONTENT_HASH"),
    ("digest", "ASSET_RECORD_DIGEST"), ("schema", "ASSET_SCHEMA"), ("version", "ASSET_SCHEMA"),
    ("missing", "ASSET_FILE"),
])
def test_independent_integrity_rejections(tmp_path, mutation, code):
    value = record(tmp_path)
    if mutation == "primary": value["primary_artifact_id"] = "missing"
    elif mutation == "duplicate_id": value["artifacts"].append(copy.deepcopy(value["artifacts"][0]))
    elif mutation == "case_collision":
        other = copy.deepcopy(value["artifacts"][0]); other.update(artifact_id="other", logical_path="REFERENCE.BIN")
        value["artifacts"].append(other)
    elif mutation == "reference": value["ref_images"] = [{"role": "prop", "sha256": "0" * 64}]
    elif mutation == "size": value["artifacts"][0]["size_bytes"] += 1
    elif mutation == "sha": value["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "schema": value["schema_version"] = "future"
    elif mutation == "version": value["digest_version"] = "future"
    elif mutation == "missing": (tmp_path / "reference.bin").unlink()
    seal(value)
    if mutation == "content": value["content_hash"] = "0" * 64
    if mutation == "digest": value["record_digest"] = "0" * 64
    with pytest.raises(AssetValidationError) as error:
        validate_asset_record(value, resource_root=tmp_path)
    assert error.value.code == code


def test_reference_order_location_digest_and_copy_isolation(tmp_path):
    value = record(tmp_path)
    first_sha = value["artifacts"][0]["sha256"]
    other = copy.deepcopy(value["artifacts"][0])
    (tmp_path / "second.bin").write_bytes(b"second")
    other.update(artifact_id="second", logical_path="second.bin", locator="second.bin",
                 sha256=hashlib.sha256(b"second").hexdigest(), size_bytes=6)
    value["artifacts"].append(other)
    value["ref_images"] = [{"role": "prop", "sha256": first_sha}, {"role": "prop", "sha256": other["sha256"]}]
    seal(value)
    before = copy.deepcopy(value)
    result = validate_asset_record(value, resource_root=tmp_path)
    result["artifacts"].clear()
    assert value == before
    changed = copy.deepcopy(value)
    changed["ref_images"].reverse(); seal(changed)
    assert changed["content_hash"] != value["content_hash"]
    assert changed["record_digest"] != value["record_digest"]
    (tmp_path / "moved.bin").write_bytes((tmp_path / "reference.bin").read_bytes())
    changed = copy.deepcopy(value); changed["artifacts"][0]["locator"] = "moved.bin"
    assert canonical_library_record_digest(changed) == value["record_digest"]
    assert validate_asset_record(changed, resource_root=tmp_path)
    for key, replacement in [("logical_path", "renamed.bin"), ("sha256", "0" * 64), ("size_bytes", 1)]:
        changed = copy.deepcopy(value); changed["artifacts"][0][key] = replacement
        assert canonical_library_record_digest(changed) != value["record_digest"]
    changed = copy.deepcopy(value); changed["primary_artifact_id"] = "second"
    assert canonical_library_record_digest(changed) != value["record_digest"]
    changed = copy.deepcopy(value); changed["artifacts"].reverse()
    assert canonical_library_record_digest(changed) != value["record_digest"]


def test_resources_hashed_in_chunks_and_io_errors_sanitized(tmp_path, monkeypatch):
    import fsf.core.asset_library as library
    value = record(tmp_path)
    real = library._open_reader
    reads = []
    class Reader:
        def __enter__(self): self.inner = real(tmp_path / "reference.bin"); return self
        def __exit__(self, *args): self.inner.close()
        def fileno(self): return self.inner.fileno()
        def read(self, size): reads.append(size); return self.inner.read(size)
    monkeypatch.setattr(library, "_open_reader", lambda _: Reader())
    assert validate_asset_record(value, resource_root=tmp_path)
    assert reads and all(size == library.HASH_CHUNK_BYTES for size in reads)
    def fail(_): raise OSError("SYNTHETIC_SECRET private location")
    monkeypatch.setattr(library, "_open_reader", fail)
    with pytest.raises(AssetValidationError) as error:
        validate_asset_record(value, resource_root=tmp_path)
    assert str(error.value) == "ASSET_FILE" and error.value.__suppress_context__


def test_symlink_root_parent_and_file_rejected(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    value = record(real)
    link = tmp_path / "link"
    try: link.symlink_to(real, target_is_directory=True)
    except OSError: pytest.skip("Symlink creation unavailable")
    with pytest.raises(AssetValidationError): validate_asset_record(value, resource_root=link)
    value["artifacts"][0]["locator"] = "link/reference.bin"; seal(value)
    with pytest.raises(AssetValidationError): validate_asset_record(value, resource_root=tmp_path)
    file_link = real / "alias.bin"; file_link.symlink_to(real / "reference.bin")
    value["artifacts"][0]["locator"] = "alias.bin"; seal(value)
    with pytest.raises(AssetValidationError): validate_asset_record(value, resource_root=real)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_root_rejected(tmp_path):
    import _winapi
    real = tmp_path / "real"; real.mkdir()
    value = record(real); link = tmp_path / "junction"
    _winapi.CreateJunction(str(real), str(link))
    try:
        with pytest.raises(AssetValidationError): validate_asset_record(value, resource_root=link)
    finally: os.rmdir(link)


@pytest.mark.parametrize("logical", ["REFERENCE.BIN", "reference.bin/child"])
def test_normalized_and_file_directory_logical_collisions(tmp_path, logical):
    value = record(tmp_path)
    extra = copy.deepcopy(value["artifacts"][0])
    extra.update(artifact_id="extra", logical_path=logical)
    value["artifacts"].append(extra); seal(value)
    with pytest.raises(AssetValidationError, match="ASSET_PATH_COLLISION"):
        validate_asset_record(value, resource_root=tmp_path)


def test_unicode_normalization_collision(tmp_path):
    value = record(tmp_path)
    value["artifacts"][0]["logical_path"] = "caf\u00e9.bin"
    extra = copy.deepcopy(value["artifacts"][0])
    extra.update(artifact_id="extra", logical_path="cafe\u0301.bin")
    value["artifacts"].append(extra); seal(value)
    with pytest.raises(AssetValidationError, match="ASSET_PATH_COLLISION"):
        validate_asset_record(value, resource_root=tmp_path)


def test_same_bytes_distinct_artifacts_and_refs_are_not_collapsed(tmp_path):
    value = record(tmp_path)
    extra = copy.deepcopy(value["artifacts"][0])
    extra.update(artifact_id="extra", logical_path="extra.bin")
    value["artifacts"].append(extra)
    value["ref_images"] = [{"role": "prop", "sha256": extra["sha256"]}] * 2
    seal(value)
    before = sorted(path.name for path in tmp_path.iterdir())
    result = validate_asset_record(value, resource_root=tmp_path)
    assert len(result["artifacts"]) == len(result["ref_images"]) == 2
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_role_table_is_the_approved_contract():
    assert ROLE_MEDIA_TYPES == {
        "model": {"model/gltf-binary", "model/gltf+json"},
        "scene": {"application/x-blender", "model/gltf-binary", "model/gltf+json"},
        "texture": {"image/png", "image/jpeg", "image/webp"},
        "reference_image": {"image/png", "image/jpeg", "image/webp"},
        "audio": {"audio/wav", "audio/mpeg", "audio/flac", "audio/ogg"},
        "animation": {"model/gltf-binary", "model/gltf+json"},
        "other": {"application/json", "text/plain", "application/octet-stream"},
    }


def test_file_changed_during_hash_rejected(tmp_path, monkeypatch):
    import fsf.core.asset_library as library
    value = record(tmp_path)
    real = library._open_reader
    class Reader:
        def __enter__(self): self.inner = real(tmp_path / "reference.bin"); self.changed = False; return self
        def __exit__(self, *args): self.inner.close()
        def fileno(self): return self.inner.fileno()
        def read(self, size):
            chunk = self.inner.read(size)
            if not self.changed:
                with (tmp_path / "reference.bin").open("ab") as writer: writer.write(b"changed")
                self.changed = True
            return chunk
    monkeypatch.setattr(library, "_open_reader", lambda _: Reader())
    with pytest.raises(AssetValidationError, match="ASSET_FILE_CHANGED"):
        validate_asset_record(value, resource_root=tmp_path)


def _fifo_worker(root, value, queue):
    try:
        validate_asset_record(value, resource_root=root)
        queue.put("ACCEPTED")
    except AssetValidationError as exc:
        queue.put(exc.code)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
def test_fifo_is_rejected_without_blocking(tmp_path):
    value = record(tmp_path)
    path = tmp_path / "reference.bin"
    path.unlink(); os.mkfifo(path)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_fifo_worker, args=(str(tmp_path), value, queue))
    worker.start()
    try:
        assert queue.get(timeout=10) == "ASSET_FILE"
        worker.join(5)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive(): worker.kill(); worker.join(5)
        queue.close(); queue.join_thread()


def test_non_utf8_encodable_prompt_has_sanitized_error(tmp_path):
    value = record(tmp_path)
    value["source_prompt"] = "\ud800"
    with pytest.raises(AssetValidationError, match="ASSET_INVALID"):
        validate_asset_record(value, resource_root=tmp_path)


def test_large_artifact_streaming_and_reference_role_binding(tmp_path):
    value = record(tmp_path)
    data = b"x" * (2 * 1024 * 1024 + 17)
    (tmp_path / "reference.bin").write_bytes(data)
    value["artifacts"][0].update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    value["ref_images"] = [{"role": "prop", "sha256": value["artifacts"][0]["sha256"]}]
    seal(value)
    assert validate_asset_record(value, resource_root=tmp_path)
    value["artifacts"][0].update(role="model", media_type="model/gltf-binary"); seal(value)
    with pytest.raises(AssetValidationError, match="ASSET_REFERENCE"):
        validate_asset_record(value, resource_root=tmp_path)


def test_changed_spec_cannot_keep_old_content_hash(tmp_path):
    value = record(tmp_path)
    value["source_prompt"] = "A changed description."
    value["record_digest"] = canonical_library_record_digest(value)
    with pytest.raises(AssetValidationError, match="ASSET_CONTENT_HASH"):
        validate_asset_record(value, resource_root=tmp_path)
