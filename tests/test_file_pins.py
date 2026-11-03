"""Actual Win32 sharing-mode proof, not simulated filesystem metadata caching."""
import os
from pathlib import Path

import pytest

from fsf.core.file_pins import WindowsReadSet


@pytest.mark.skipif(os.name!='nt',reason='Win32 lease profile; POSIX retains full per-call hashing')
def test_locked_bytes_and_parent_name_cannot_be_replaced_until_release(tmp_path):
    folder=tmp_path/'code'
    folder.mkdir()
    file=folder/'worker.py'
    file.write_bytes(b'original bytes')
    pins=WindowsReadSet()
    try:
        original=pins.hash(file)
        with pytest.raises(PermissionError):file.write_bytes(b'changed')
        with pytest.raises(PermissionError):file.rename(folder/'other.py')
        with pytest.raises(PermissionError):folder.rename(tmp_path/'moved')
        assert pins.hash(file)==original
    finally:
        pins.close()
    file.write_bytes(b'changed')
    folder.rename(tmp_path/'moved')
    assert (tmp_path/'moved/worker.py').read_bytes()==b'changed'


@pytest.mark.skipif(os.name!='nt',reason='Win32 read sharing')
def test_json_lease_observes_actual_bytes_and_releases_all_handles(tmp_path):
    import json
    file=tmp_path/'provider.json'
    file.write_text(json.dumps({'request':'actual_request'}))
    pins=WindowsReadSet()
    try:
        assert pins.json(file)=={'request':'actual_request'}
        with pytest.raises(PermissionError):file.unlink()
    finally:
        pins.close()
    file.unlink()
    assert not file.exists()


@pytest.mark.skipif(os.name!='nt',reason='Win32 transient reply lease')
def test_reply_leaf_is_reread_while_ancestor_names_remain_locked(tmp_path):
    folder=tmp_path/'replies'
    folder.mkdir()
    file=folder/'reply.json'
    file.write_text('{"revision":1}')
    pins=WindowsReadSet()
    try:
        assert pins.read_json_once(file)=={'revision':1}
        with pytest.raises(PermissionError):folder.rename(tmp_path/'moved')
        file.write_text('{"revision":2}')
        assert pins.read_json_once(file)=={'revision':2}
        file.unlink()
        with pytest.raises(OSError):pins.read_json_once(file)
    finally:pins.close()
    folder.rename(tmp_path/'moved')
