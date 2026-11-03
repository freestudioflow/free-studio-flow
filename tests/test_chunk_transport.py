"""Mounted-disk chunk recovery, distinct from network byte-offset qualification."""
import pytest

from fsf.production.artifacts import split_artifact, transfer_chunks, assemble_artifact, CHUNK_BYTES
from fsf.production.contracts import ProductionError
from fsf.production.control import file_identity


def test_128_mib_transfer_resumes_verified_chunks_after_interruption(tmp_path, monkeypatch):
    import fsf.production.artifacts as module
    source = tmp_path / 'source.bin'
    with source.open('wb') as stream:
        for value in range(128):
            stream.write(bytes([value]) * 1024 * 1024)
    chunks, received = tmp_path / 'chunks', tmp_path / 'received'
    chunks.mkdir()
    received.mkdir()
    manifest = split_artifact(source, chunk_root=chunks)
    replace = module.os.replace
    completed = []

    def interrupted(before, after):
        if str(before).endswith('.transfer-partial'):
            if len(completed) == 2:
                raise OSError('injected disk disconnect')
            completed.append(str(after))
        return replace(before, after)

    monkeypatch.setattr(module.os, 'replace', interrupted)
    from fsf.core.edit_store import EditStoreError
    with pytest.raises(EditStoreError, match='EDIT_IO'):
        transfer_chunks(manifest, source_root=chunks, destination_root=received)
    assert len([path for path in received.iterdir() if len(path.name) == 64]) == 2
    before = {path.name: path.stat().st_mtime_ns for path in received.iterdir() if len(path.name) == 64}
    monkeypatch.setattr(module.os, 'replace', replace)
    result = transfer_chunks(manifest, source_root=chunks, destination_root=received)
    assert result['reused_bytes'] == 2 * CHUNK_BYTES
    assert result['copied_bytes'] == 128 * 1024 * 1024 - 2 * CHUNK_BYTES
    assert result['byte_offset_resume'] is False
    assert all((received / name).stat().st_mtime_ns == stamp for name, stamp in before.items())
    output = tmp_path / 'output.bin'
    assemble_artifact(manifest, chunk_root=received, output=output)
    assert file_identity(output) == file_identity(source)
    assert transfer_chunks(manifest, source_root=chunks, destination_root=received)['copied_bytes'] == 0


def test_existing_corrupt_destination_chunk_never_replaced(tmp_path):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'bounded source')
    chunks, received = tmp_path / 'chunks', tmp_path / 'received'
    chunks.mkdir()
    received.mkdir()
    manifest = split_artifact(source, chunk_root=chunks)
    target = received / manifest['chunks'][0]['sha256']
    target.write_bytes(b'changed')
    with pytest.raises(ProductionError, match='CHUNK_CHANGED'):
        transfer_chunks(manifest, source_root=chunks, destination_root=received)
    assert target.read_bytes() == b'changed'
