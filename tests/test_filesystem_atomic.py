"""Atomic document replacement survives deeply nested worker directories."""
import json
import os
from pathlib import Path
from fsf.core.filesystem import native_path
from fsf.remote.contracts import write_json
from fsf.production.contracts import publish


def test_atomic_json_writes_with_long_absolute_destination(tmp_path):
    folder=tmp_path/('a'*max(1,220-len(str(tmp_path))-1))
    folder.mkdir(parents=True)
    for name,writer in (('remote',write_json),('production',publish)):
        target=folder/(name+'_'+'d'*64+'.json')
        for revision in (1,2):
            writer(target,{'revision':revision})
            assert json.loads(Path(native_path(target)).read_text())=={'revision':revision}
    assert not list(folder.glob('.remote-json-*')) and not list(folder.glob('.production-*'))


def test_native_path_is_absolute_on_windows_and_unchanged_elsewhere(tmp_path):
    value=native_path(tmp_path/'file.json')
    if os.name=='nt':
        assert value.startswith('\\\\?\\')
        assert native_path(value)==value
    else:assert value==str(tmp_path/'file.json')
