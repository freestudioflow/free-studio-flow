"""Impact hints do not grant consent; source diagnostics never echo host paths."""
import copy
import json
import os
import subprocess
import sys

import pytest
from fsf.core.edit_diagnostics import patch_impact
from fsf.core.editing import EditValidationError,timing_anchor_digest,create_edit
from fsf.runtime.private_diagnostics import source_changed
from test_edit_identity import edit_fixture,patch


def absolute_edit():
    value=edit_fixture()
    track={'schema_version':'fsf_edit_subtitle_track_v1','track_id':'SUB','clock_rate':{'num':24,'den':1},
           'timebase':'timeline_absolute','timing_anchor_digest':timing_anchor_digest(value),
           'cues':[{'cue_id':'CUE','start_frame':0,'end_frame':24,'text':'Operator line'}]}
    return edit_fixture(subtitles=[track])


def test_impact_is_read_only_and_does_not_confirm():
    value=absolute_edit();before=copy.deepcopy(value)
    request=patch(value,{'op':'reorder','clip_ids':['C02','C01']})
    result=patch_impact(value,request)
    assert result['potential_absolute_cue_ids']==['CUE']
    assert result['potential_anchor_tracks']==['SUB'] and result['confirmation_tracks_present']==[]
    assert result['applied'] is False and result['patch_validated'] is False and value==before


def test_stale_patch_does_not_get_replacement_locator():
    value=edit_fixture();request=patch(value,{'op':'reorder','clip_ids':['C02','C01']});request['base_revision']+=1
    with pytest.raises(EditValidationError):patch_impact(value,request)


def test_cli_impact_does_not_modify_files(tmp_path):
    value=absolute_edit();request=patch(value,{'op':'reorder','clip_ids':['C02','C01']})
    edit=tmp_path/'edit.json';changes=tmp_path/'patch.json'
    edit.write_text(json.dumps(value));changes.write_text(json.dumps(request));before=edit.read_bytes()
    completed=subprocess.run([sys.executable,'-m','fsf.cli','edit','patch-impact',str(edit),'--patch',str(changes)],capture_output=True,text=True,timeout=10)
    assert completed.returncode==0,completed.stdout
    assert 'CUE' in completed.stdout and edit.read_bytes()==before


def test_source_diagnostics_are_opt_in_and_path_free(tmp_path,monkeypatch):
    data=tmp_path/'source.mp4';data.write_bytes(b'original')
    before=data.stat();data.write_bytes(b'changed bytes');after=data.stat()
    diagnostics=tmp_path/'private';diagnostics.mkdir()
    monkeypatch.delenv('FSF_PRIVATE_DIAGNOSTICS_DIR',raising=False)
    source_changed('file_stat_changed',data,before,after)
    assert not list(diagnostics.iterdir())
    monkeypatch.setenv('FSF_PRIVATE_DIAGNOSTICS_DIR',str(diagnostics))
    source_changed('file_stat_changed',data,before,after)
    path=next(diagnostics.iterdir());text=path.read_text()
    assert str(tmp_path) not in text and 'source.mp4' not in text
    value=json.loads(text)
    assert value['before']['st_size']==8 and value['after']['st_size']==13


def test_real_growth_mid_read_is_rejected_and_recorded(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from fsf.runtime import post_sources
    path=tmp_path/'source.bin';path.write_bytes(b'a'*1024)
    diagnostics=tmp_path/'private';diagnostics.mkdir();monkeypatch.setenv('FSF_PRIVATE_DIAGNOSTICS_DIR',str(diagnostics))
    original=post_sources._open_reader
    @contextmanager
    def changing(target):
        with original(target) as stream:
            class Reader:
                changed=False
                def fileno(self):return stream.fileno()
                def read(self,size):
                    if not self.changed:
                        self.changed=True
                        with path.open('ab') as writer:writer.write(b'modified')
                    return stream.read(size)
            yield Reader()
    monkeypatch.setattr(post_sources,'_open_reader',changing)
    with pytest.raises(post_sources.PostError,match='POST_SOURCE_CHANGED'):post_sources.file_sha(path)
    assert json.loads(next(diagnostics.iterdir()).read_text())['phase'] in {'file_grew','file_stat_changed'}
