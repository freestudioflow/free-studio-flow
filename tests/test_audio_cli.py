"""Pure CLI import, mix revisions, A/B playback and authoritative resume."""
import copy
import json
from pathlib import Path

import pytest

from fsf.core.editing import apply_edit_patch, _digest
from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.audio_edit import ab_projection
from fsf.runtime.post_sources import file_sha
from fsf.runtime.post_runtime import verify_post_receipt
from fsf.runtime.post_render import run_ffmpeg
from test_post_production import production, bundle, cli, write
from test_audio_edit import settings
from test_edit_identity import patch


def test_cli_audio_import_three_revisions_and_ab(bundle,tmp_path):
    root,edit,config=bundle
    source_hashes={p.relative_to(root).as_posix():file_sha(p) for p in root.rglob('*') if p.is_file()}
    tone=tmp_path/'narration.wav'
    run_ffmpeg(['-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=12',tone],tmp_path)
    config_file=write(tmp_path/'sources.json',config)
    audio_config=write(tmp_path/'audio.json',{'schema_version':'fsf_audio_resources_v1','resources':[{'resource_id':'AUDIO','root':str(tmp_path)}]})
    edit_file=write(tmp_path/'edit.json',edit)
    request=write(tmp_path/'request.json',{'schema_version':'fsf_audio_import_request_v1','edit_id':edit['edit_id'],'base_revision':edit['revision'],'base_digest':edit['edit_digest'],
        'selections':[{'audio_source_id':'AU1','resource_id':'AUDIO','relative_path':'narration.wav','stream_index':0}]})
    imported=tmp_path/'imported.json'
    response=json.loads(cli('edit','audio','import',edit_file,'--definition',request,'--audio-resources',audio_config,'--out',imported).stdout)['result']
    assert response['sources'][0]['binding']['canonical_samples']==576000
    assert json.loads(edit_file.read_text())==edit
    mix={'schema_version':'fsf_audio_mix_v1','enabled':True,'sample_rate':48000,'channel_layout':'stereo','track_settings':[],
        'sources':response['sources'],'events':[],'settings':settings(),'retired_ids':{'track':[],'source':[],'event':[],'region':[]}}
    identity={'track_id':'VO','role':'vo','label':'Narration'}
    track={'track_id':'VO','timebase':'clip_relative','gain_db':0,'mute':False}
    ops=[{'op':'initialize_audio_mix','audio_mix':mix},{'op':'upsert_audio_track','identity':identity,'track':track}]
    # Original video audio is routed explicitly, not silently replaced by VO.
    for i,clip in enumerate(edit['clips']):
        source=edit['sources'][i]
        ops.extend([{'op':'add_audio_source','source':{'audio_source_id':f'ORIG{i}','binding':{'kind':'clip_original','video_source_id':source['source_id'],
            'video_sha256':source['source_ref']['artifact_sha256'],'stream_index':1}}},
            {'op':'upsert_audio_track','identity':{'track_id':f'TR{i}','role':'source','label':'Original'},'track':{'track_id':f'TR{i}','timebase':'clip_relative','gain_db':0,'mute':False}},
            {'op':'upsert_audio_event','event':{'event_id':f'EV{i}','track_id':f'TR{i}','audio_source_id':f'ORIG{i}','source_in_sample':0,'source_out_sample':96000,
                'placement':{'timebase':'clip_relative','clip_id':clip['clip_id'],'start_frame':0},'tail_policy':'natural','gain_db':0,'mute':False,'regions':[]}}])
    ops.append({'op':'upsert_audio_event','event':{'event_id':'NARR','track_id':'VO','audio_source_id':'AU1','source_in_sample':0,'source_out_sample':48000,
        'placement':{'timebase':'clip_relative','clip_id':'C01','start_frame':0},'tail_policy':'natural','gain_db':0,'mute':False,'regions':[]}})
    patch_file=write(tmp_path/'initial.json',patch(edit,*ops))
    edit=json.loads(cli('edit','patch',edit_file,'--patch',patch_file).stdout)['result']
    snapshots=[]
    for cycle in range(3):
        if cycle:
            track={**track,'gain_db':-3*cycle}
            patch_file=write(tmp_path/f'patch{cycle}.json',patch(edit,{'op':'upsert_audio_track','identity':identity,'track':track}))
            edit=json.loads(cli('edit','patch',edit_file,'--patch',patch_file).stdout)['result']
        snapshot=write(tmp_path/f'edit{cycle}.json',edit)
        snapshots.append((snapshot,copy.deepcopy(edit)))
        plan=tmp_path/f'post{cycle}.json'
        cli('edit','plan',snapshot,'--out',plan,'--width','320','--height','180')
        result=json.loads(cli('execute',plan,'--out',tmp_path/'out','--source-executions',config_file,'--audio-resources',audio_config).stdout)
        assert result['success'],result
        assert result['generation_dispatches']=={'process':0,'api':0,'handoff':0}
        delivery=tmp_path/'out'/result['artifact_directory']
        assert verify_post_receipt(delivery)
        report=json.loads((delivery/'audio_report.json').read_text())
        assert report['numeric_quality']=='passed' and report['listening_review']=='not_reviewed'
        if cycle==0:
            measured=json.loads(cli('edit','audio','analyze',plan,'--source-executions',config_file,'--audio-resources',audio_config,'--out',tmp_path/'analysis').stdout)['result']
            assert all(r['status']=='unmeasurable' and r['reason']=='AUDIO_TOO_SHORT' and not r['normalization_verified'] for r in measured['report']['results'])
            preview=json.loads(cli('edit','preview',snapshot,'--source-executions',config_file,'--audio-resources',audio_config,'--out',tmp_path/'preview',
                '--revision',str(edit['revision']),'--digest',edit['edit_digest'],'--clip-id','C01','--end-frame','24','--width','320','--height','180').stdout)['result']
            assert preview['manifest']['kind']=='preview'
        repeated=json.loads(cli('execute' ,plan,'--out',tmp_path/'out','--source-executions',config_file,'--audio-resources',audio_config).stdout)
        assert repeated['reused_count']==4,repeated
    first,last=snapshots[0][1],snapshots[-1][1]
    request=write(tmp_path/'ab.json',{'schema_version':'fsf_audio_audition_request_v1','edit_id':edit['edit_id'],
        'before_revision':first['revision'],'before_digest':first['edit_digest'],'after_revision':last['revision'],'after_digest':last['edit_digest'],
        'clip_id':'C01','start_frame':0,'end_frame':24,'projection_digest':digest(ab_projection(first)),'listen_scope':'mix','track_id':None})
    result=json.loads(cli('edit','audio','audition',snapshots[0][0],snapshots[-1][0],'--request',request,'--source-executions',config_file,
        '--audio-resources',audio_config,'--out',tmp_path/'ab').stdout)['result']
    assert result['manifest']['kind']=='preview'
    assert all((tmp_path/'ab'/result['directory']/f'{side}.wav').is_file() for side in ('A','B'))
    import array, math, wave
    def magnitudes(path):
        with wave.open(str(path),'rb') as stream:
            values=array.array('h',stream.readframes(stream.getnframes()))[::2]
            rate=stream.getframerate()
        return [abs(sum(value*complex(math.cos(2*math.pi*f*i/rate),math.sin(2*math.pi*f*i/rate)) for i,value in enumerate(values)))/len(values) for f in (450,880)]
    left=magnitudes(tmp_path/'ab'/result['directory']/'A.wav');right=magnitudes(tmp_path/'ab'/result['directory']/'B.wav')
    assert abs(20*math.log10(right[0]/left[0]))<0.25
    assert abs(20*math.log10(right[1]/left[1])+6)<0.25
    assert source_hashes=={p.relative_to(root).as_posix():file_sha(p) for p in root.rglob('*') if p.is_file()}
    assert (root/'generation_count.txt').read_text()=='3'
    # Changed external bytes invalidate even an otherwise reusable delivery.
    tone.write_bytes(tone.read_bytes()+b'changed')
    denied=cli('execute',plan,'--out',tmp_path/'out','--source-executions',config_file,'--audio-resources',audio_config,check=False)
    assert denied.returncode!=0 and json.loads(denied.stdout)['error']=='AUDIO_SOURCE_INVALID'
