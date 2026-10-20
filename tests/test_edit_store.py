"""Local edit store: independent writers, crash boundaries and strict JSON."""
import copy
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading

import pytest

from fsf.core import edit_store as store
from test_edit_identity import edit_fixture, patch


def _race(path, payload, create, barrier, results):
    try:
        barrier.wait(20)
        value = (store.create_edit_file if create else store.patch_edit_file)(path, payload)
        results.put(("OK", value["revision"]))
    except store.EditStoreError as exc:
        results.put((exc.code, None))


def _holder(path, ready, release):
    with store.edit_lock(path):
        ready.set()
        if not release.wait(30):
            raise RuntimeError("Test synchronization timed out")


def _crash(path, payload, after, ready, release):
    real_replace = store.os.replace

    def replace(source, destination):
        if after:
            real_replace(source, destination)
        ready.set()
        if not release.wait(30):
            raise RuntimeError("Test synchronization timed out")
        if not after:
            real_replace(source, destination)

    store.os.replace = replace
    store.patch_edit_file(path, payload)


def _join(process):
    process.join(20)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("Worker did not finish")
    assert process.exitcode == 0


@pytest.mark.parametrize("create", [False, True])
def test_independent_writers_compare_and_publish_ten_rounds(tmp_path, create):
    ctx = mp.get_context("spawn")
    for index in range(10):
        path = tmp_path / f"edit{index}.json"
        original = edit_fixture()
        if not create:
            store.create_edit_file(path, original)
        payload = original if create else patch(original, {"op": "rename", "clip_id": "C01", "label": "New"})
        barrier, queue = ctx.Barrier(3), ctx.Queue()
        workers = [ctx.Process(target=_race, args=(str(path), payload, create, barrier, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            barrier.wait(20)
            outcomes = [queue.get(timeout=20)[0] for _ in workers]
            for worker in workers:
                _join(worker)
            assert sorted(outcomes) == sorted(["OK", "EDIT_TARGET_EXISTS" if create else "EDIT_CONFLICT"])
            result = store.read_edit(path)
            assert result["revision"] == (1 if create else 2)
            assert result["parent_digest"] == (None if create else original["edit_digest"])
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.kill()
                worker.join(5)
            queue.close()
            queue.join_thread()


def test_lock_timeout_alias_and_process_exit_recovery(tmp_path, monkeypatch):
    ctx = mp.get_context("spawn")
    path = tmp_path / "edit.json"
    store.create_edit_file(path, edit_fixture())
    ready, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_holder, args=(str(path), ready, release))
    worker.start()
    try:
        assert ready.wait(20)
        monkeypatch.chdir(tmp_path)
        alias = os.path.relpath(path)
        with pytest.raises(store.EditStoreError, match="EDIT_LOCK_TIMEOUT"):
            with store.edit_lock(alias, timeout=0.15):
                pass
        worker.terminate()
        worker.join(10)
        assert not worker.is_alive()
        with store.edit_lock(path, timeout=1):
            pass
        assert path.with_name("edit.json.lock").is_file()
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(5)


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("termination", ["terminate", "kill"])
def test_crash_before_and_after_replace(tmp_path, after, termination):
    ctx = mp.get_context("spawn")
    path = tmp_path / "edit.json"
    original = edit_fixture()
    store.create_edit_file(path, original)
    payload = patch(original, {"op": "rename", "clip_id": "C01", "label": "New"})
    ready, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_crash, args=(str(path), payload, after, ready, release))
    worker.start()
    try:
        assert ready.wait(20)
        getattr(worker, termination)()
        worker.join(10)
        assert not worker.is_alive()
        current = store.read_edit(path)
        assert current["revision"] == (2 if after else 1)
        with store.edit_lock(path, timeout=1):
            pass
        # A leftover complete temporary file is never used instead of the target.
        if not after:
            assert list(tmp_path.glob(".fsf-edit-*.tmp"))
            assert current == original
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(5)


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "replace"])
def test_publish_failure_keeps_original_and_scrubs_error(tmp_path, monkeypatch, failure):
    path = tmp_path / "edit.json"
    original = edit_fixture()
    store.create_edit_file(path, original)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("SYNTHETIC_SECRET must not escape")

    if failure in ("write", "flush"):
        real_fdopen = store.os.fdopen

        class BrokenFile:
            def __init__(self, inner):
                self.inner = inner
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.inner.close()
            def write(self, data):
                if failure == "write":
                    self.inner.write(data[:20])
                    fail()
                return self.inner.write(data)
            def flush(self):
                fail()
            def fileno(self):
                return self.inner.fileno()

        monkeypatch.setattr(store.os, "fdopen", lambda fd, mode, **kw: BrokenFile(real_fdopen(fd, mode, **kw)) if mode == "wb" else real_fdopen(fd, mode, **kw))
    else:
        monkeypatch.setattr(store.os, failure, fail)
    with pytest.raises(store.EditStoreError) as caught:
        store.patch_edit_file(path, patch(original, {"op": "rename", "clip_id": "C01", "label": "New"}))
    assert str(caught.value) == "EDIT_IO"
    assert caught.value.__suppress_context__
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".fsf-edit-*.tmp"))


def test_concurrent_reader_sees_only_valid_revisions(tmp_path):
    path = tmp_path / "edit.json"
    store.create_edit_file(path, edit_fixture())
    finished, started = threading.Event(), threading.Event()
    errors, revisions = [], []

    def reader():
        try:
            while not finished.is_set():
                revisions.append(store.read_edit(path)["revision"])
                started.set()
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert started.wait(5)
        for index in range(20):
            current = store.read_edit(path)
            store.patch_edit_file(path, patch(current, {"op": "rename", "clip_id": "C01", "label": f"Label {index}"}))
    finally:
        finished.set()
        thread.join(10)
    assert not thread.is_alive() and not errors and revisions
    assert store.read_edit(path)["revision"] == 21


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b"NaN", b"Infinity", b"-Infinity", b"1e999", b"\xff", b"\xef\xbb\xbf\xef\xbb\xbf{}", b"[" * 65 + b"0" + b"]" * 65])
def test_strict_json_rejects(data):
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        store.decode_json(data)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_json_byte_boundary(delta):
    data = b"{}" + b" " * (store.MAX_JSON_BYTES + delta - 2)
    if delta > 0:
        with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
            store.decode_json(data)
    else:
        assert store.decode_json(data) == {}


def test_json_depth_bom_and_quoted_brackets():
    assert store.decode_json(b"[" * 64 + b"0" + b"]" * 64)
    assert store.decode_json(b'\xef\xbb\xbf{"text":"[[[\\\""}') == {"text": '[[["'}
    with pytest.raises(store.EditStoreError):
        store.decode_json(b"\xef\xbb\xbf{}" + b" " * (store.MAX_JSON_BYTES - 2))


def test_patch_invalid_stale_and_input_isolation(tmp_path):
    path = tmp_path / "edit.json"
    original = edit_fixture()
    untouched = copy.deepcopy(original)
    store.create_edit_file(path, original)
    operation = patch(original, {"op": "rename", "clip_id": "C01", "label": "New"})
    original_patch = copy.deepcopy(operation)
    store.patch_edit_file(path, operation)
    assert original == untouched and operation == original_patch
    with pytest.raises(store.EditStoreError, match="EDIT_CONFLICT"):
        store.patch_edit_file(path, operation)
    operation["edit_id"] = "another"
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        store.patch_edit_file(path, operation)


def test_oversized_patch_result_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "edit.json"
    original = edit_fixture()
    store.create_edit_file(path, original)
    before = path.read_bytes()
    monkeypatch.setattr(store, "MAX_JSON_BYTES", len(before) + 10)
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        store.patch_edit_file(path, patch(original, {"op": "rename", "clip_id": "C01", "label": "x" * 1000}))
    assert path.read_bytes() == before


def test_missing_file_and_invalid_disk_digest(tmp_path):
    path = tmp_path / "edit.json"
    with pytest.raises(store.EditStoreError, match="EDIT_IO"):
        store.read_edit(path)
    value = edit_fixture()
    value["edit_digest"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        store.read_edit(path)


def test_read_bound_uses_actual_bytes_not_stat(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b"{}" + b" " * store.MAX_JSON_BYTES)
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        store.read_json(path)


@pytest.mark.parametrize("timeout", [True, -1, float("inf"), float("nan")])
def test_invalid_lock_timeout_rejected(tmp_path, timeout):
    with pytest.raises(store.EditStoreError, match="EDIT_INVALID"):
        with store.edit_lock(tmp_path / "edit.json", timeout=timeout):
            pass


def test_symlink_target_parent_and_lock_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(store.EditStoreError, match="EDIT_IO"):
        store.create_edit_file(link / "edit.json", edit_fixture())
    path = real / "edit.json"
    store.create_edit_file(path, edit_fixture())
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(store.EditStoreError, match="EDIT_IO"):
        store.read_edit(alias)
    lock = path.with_name("edit.json.lock")
    lock.unlink()
    lock.symlink_to(path)
    with pytest.raises(store.EditStoreError, match="EDIT_IO"):
        with store.edit_lock(path):
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_parent_rejected(tmp_path):
    import _winapi
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "junction"
    _winapi.CreateJunction(str(real), str(link))
    try:
        with pytest.raises(store.EditStoreError, match="EDIT_IO"):
            store.create_edit_file(link / "edit.json", edit_fixture())
    finally:
        os.rmdir(link)
