"""Opt-in host diagnostics; never emit paths, inputs or raw exception text."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def source_changed(phase,path=None,before=None,after=None):
    target=os.environ.get('FSF_PRIVATE_DIAGNOSTICS_DIR')
    if not target:return
    if phase not in {'file_grew','file_stat_changed','binding_recheck'}:return
    try:
        from fsf.core.asset_library import _root
        root=_root(target)
        def snapshot(value):
            return {name:getattr(value,name) for name in ('st_size','st_mtime_ns','st_ctime_ns','st_ino','st_dev')} if value else None
        value={'event':'POST_SOURCE_CHANGED','phase':phase,'observed_at_ns':time.time_ns(),
               'source_token':hashlib.sha256(str(path).encode()).hexdigest() if path is not None else None,
               'before':snapshot(before),'after':snapshot(after)}
        fd,name=tempfile.mkstemp(prefix='post-source-',suffix='.json',dir=root)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(value,stream)
    except Exception:
        # Diagnostic I/O cannot change source verification or mask its failure.
        return
