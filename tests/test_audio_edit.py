"""Audio metadata, exact sample clocks and measured output regression tests."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.audio_edit import AudioError, frame_sample, check_ab, event_layout
from fsf.core.editing import _digest, validate_edit, apply_edit_patch, timing_anchor_digest
from fsf.runtime.audio_mix import Deadline, ACTIVE_DEADLINE, mix_pcm, measure, encode_verified, pcm_input
from fsf.runtime.post_render import run_ffmpeg
from fsf.runtime.post_plan import compile_post_plan
from test_edit_identity import edit_fixture, patch


def settings():
    return {"normalization":{"mode":"none","target_lufs":-16,"tolerance_lu":1,"max_gain_db":12,"minimum_seconds":10,"groups":[],"method":"measured_linear_gain_v1"},
        "master_gain_db":0,"master_mute":False,"boundary_ramp_samples":240,"peak_ceiling_dbtp":-1,"encoding_headroom_db":0.5,"peak_extra_attempts":2,"processing_budget_seconds":300}


def audio_edit():
    edit = edit_fixture(tracks=[{"track_id":"VO01","role":"vo","label":"Narration"}])
    mix = {"schema_version":"fsf_audio_mix_v1","enabled":True,"sample_rate":48000,"channel_layout":"stereo",
        "track_settings":[{"track_id":"VO01","timebase":"clip_relative","gain_db":0,"mute":False}],
        "sources":[{"audio_source_id":"AU01","binding":{"kind":"operator_audio_import","resource_id":"AUDIO","relative_path":"tone.wav","sha256":"d"*64,"size_bytes":100,"stream_index":0,"canonical_samples":480000,"decoder_policy":"pcm48_stereo_v1"}}],
        "events":[{"event_id":"EV01","track_id":"VO01","audio_source_id":"AU01","source_in_sample":0,"source_out_sample":48000,
            "placement":{"timebase":"clip_relative","clip_id":"C01","start_frame":0},"tail_policy":"natural","gain_db":0,"mute":False,"regions":[]}],
        "settings":settings(),"retired_ids":{"track":[],"source":[],"event":[],"region":[]}}
    return apply_edit_patch(edit,patch(edit,{"op":"initialize_audio_mix","audio_mix":mix}))


def seal(edit):
    edit["edit_digest"] = _digest(edit)
    return edit


def test_legacy_digest_and_missing_null_distinction():
    old = edit_fixture()
    assert "audio_mix" not in old
    before = _digest(old)
    validate_edit(old)
    assert _digest(old) == before
    for value in (None,{}):
        invalid = copy.deepcopy(old);invalid["audio_mix"] = value;seal(invalid)
        with pytest.raises(ValueError):validate_edit(invalid)


def test_audio_gain_does_not_move_video_anchor():
    edit = audio_edit();anchor=timing_anchor_digest(edit)
    setting = copy.deepcopy(edit["audio_mix"]["settings"]);setting["master_gain_db"]=-3
    changed = apply_edit_patch(edit,patch(edit,{"op":"set_audio_settings","settings":setting}))
    assert changed["edit_digest"] != edit["edit_digest"]
    assert timing_anchor_digest(changed)==anchor
    check_ab(edit,changed)


@pytest.mark.parametrize("kind",["track","source","event","region"])
def test_retired_id_reuse_rejected(kind):
    edit=audio_edit()
    mix=edit["audio_mix"]
    if kind=="region":mix["events"][0]["regions"]=[{"region_id":"R1","start_sample":0,"end_sample":1000,"gain_db":-3,"mute":False}]
    identifier={"track":"VO01","source":"AU01","event":"EV01","region":"R1"}[kind]
    mix["retired_ids"][kind].append(identifier);seal(edit)
    with pytest.raises(AudioError,match="AUDIO_ID_RETIRED"):validate_edit(edit)


def test_track_cannot_be_repurposed_as_subtitle():
    edit=audio_edit();edit["audio_mix"]["retired_ids"]["track"]=["SUB"]
    edit["subtitle_tracks"]=[{"schema_version":"fsf_edit_subtitle_track_v1","track_id":"SUB","clock_rate":edit["clock_rate"],"timebase":"clip_relative","cues":[]}]
    seal(edit)
    with pytest.raises(AudioError):validate_edit(edit)


@pytest.mark.parametrize("mode",["master","none"])
def test_non_dialogue_mode_rejects_groups(mode):
    edit=audio_edit();norm=edit["audio_mix"]["settings"]["normalization"]
    norm.update(mode=mode,groups=[{"group_id":"G1","event_ids":["EV01"]}]);seal(edit)
    with pytest.raises(AudioError):validate_edit(edit)


@pytest.mark.parametrize("role",["bgm","sfx"])
def test_bgm_sfx_not_dialogue(role):
    edit=audio_edit();edit["audio_tracks"][0]["role"]=role
    edit["audio_mix"]["settings"]["normalization"].update(mode="dialogue_groups",groups=[{"group_id":"G1","event_ids":["EV01"]}]);seal(edit)
    with pytest.raises(AudioError):validate_edit(edit)


@pytest.mark.parametrize("mutation",["placement","trim","sha","video"])
def test_ab_rejects_source_or_position_change(mutation):
    before=audio_edit();after=copy.deepcopy(before)
    if mutation=="placement":after["audio_mix"]["events"][0]["placement"]["start_frame"]=1
    if mutation=="trim":after["audio_mix"]["events"][0]["source_in_sample"]=1
    if mutation=="sha":after["audio_mix"]["sources"][0]["binding"]["sha256"]="e"*64
    if mutation=="video":after["clips"].reverse()
    seal(after)
    with pytest.raises(AudioError,match="AUDIO_AB_MISMATCH"):check_ab(before,after)


def test_disabled_mix_still_hashes_settings_and_retirement():
    edit=audio_edit();edit["audio_mix"]["enabled"]=False;seal(edit)
    changed=copy.deepcopy(edit);changed["audio_mix"]["retired_ids"]["source"].append("OLD");seal(changed)
    validate_edit(changed)
    assert changed["edit_digest"]!=edit["edit_digest"]


def test_deleting_event_retires_regions_and_atomic_invalid_patch():
    edit=audio_edit();event=copy.deepcopy(edit["audio_mix"]["events"][0]);event["regions"]=[{"region_id":"R1","start_sample":0,"end_sample":1000,"gain_db":-3,"mute":False}]
    edit=apply_edit_patch(edit,patch(edit,{"op":"upsert_audio_event","event":event}))
    changed=apply_edit_patch(edit,patch(edit,{"op":"remove_audio_event","event_id":"EV01"}))
    assert changed["audio_mix"]["retired_ids"]["region"]==["R1"]
    with pytest.raises(AudioError):apply_edit_patch(changed,patch(changed,{"op":"upsert_audio_event","event":event}))
    assert edit["audio_mix"]["events"][0]==event


def test_audio_timeline_anchor_requires_explicit_confirmation():
    edit=audio_edit();p=edit["audio_mix"]["events"][0]["placement"]
    p.update(timebase="timeline_absolute",timing_anchor_digest=timing_anchor_digest(edit));p.pop("clip_id")
    edit["audio_mix"]["track_settings"][0]["timebase"]="timeline_absolute";seal(edit)
    op={"op":"reorder","clip_ids":["C02","C01"]}
    with pytest.raises(AudioError,match="AUDIO_STALE"):apply_edit_patch(edit,patch(edit,op))
    result=apply_edit_patch(edit,patch(edit,op,{"op":"confirm_audio_timeline_anchor","event_id":"EV01"}))
    validate_edit(result)


def test_rational_frame_sample_boundaries_telescope():
    clock={"num":30000,"den":1001}
    total=sum(frame_sample(i+1,clock)-frame_sample(i,clock) for i in range(30000))
    assert total==48048000


def test_audio_plan_closed_four_steps_and_strip_rejected():
    from pdx_artifact_core import validate_execution_plan
    edit=audio_edit();plan=compile_post_plan(edit)
    assert validate_execution_plan(plan)==[] and len(plan["steps"])==4
    assert "report:staging/audio_report.json" in plan["steps"][1]["outputs"]
    with pytest.raises(AudioError):compile_post_plan(edit,audio="strip")


@pytest.fixture
def tone(tmp_path):
    path=tmp_path/"tone.wav"
    run_ffmpeg(["-v","error","-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=12","-ac","2",path],tmp_path)
    return path


def real_edit(tone, seconds=12):
    edit=audio_edit();edit["sources"][0]["duration_frames"]=seconds*24
    edit["clips"]=[{**edit["clips"][0],"in_frame":0,"out_frame":seconds*24}]
    source=edit["audio_mix"]["sources"][0]
    from fsf.runtime.post_sources import file_sha
    source["binding"].update(sha256=file_sha(tone),size_bytes=tone.stat().st_size,canonical_samples=576000)
    edit["audio_mix"]["events"][0]["source_out_sample"]=seconds*48000;seal(edit)
    return edit,{"AU01":{"path":tone,"sha256":source["binding"]["sha256"],"binding":source["binding"]}}


def test_real_group_order_and_manual_gain_after_normalization(tone,tmp_path):
    edit,sources=real_edit(tone)
    event=edit["audio_mix"]["events"][0];event["source_out_sample"]=288000
    second=copy.deepcopy(event);second["event_id"]="EV02";second["placement"]["start_frame"]=144
    edit["audio_mix"]["events"].append(second)
    norm=edit["audio_mix"]["settings"]["normalization"]
    norm.update(mode="dialogue_groups",groups=[{"group_id":"G1","event_ids":["EV02","EV01"]}]);seal(edit)
    validate_edit(edit)
    first,report=mix_pcm(edit,sources,tmp_path/"first")
    assert report["groups"][0]["ordered_event_ids"]==["EV01","EV02"]
    assert report["groups"][0]["is_timeline_loudness"] is False
    assert abs(report["groups"][0]["after"]["integrated_lufs"]+16)<1
    edit["audio_mix"]["track_settings"][0]["gain_db"]=-3;seal(edit)
    second,other=mix_pcm(edit,sources,tmp_path/"second")
    assert abs(measure(second,tmp_path,pcm=True)["integrated_lufs"]-measure(first,tmp_path,pcm=True)["integrated_lufs"]+3)<0.2
    assert report["groups"][0]["gain_db"]==other["groups"][0]["gain_db"]


def test_real_required_normalization_short_silent_and_low_fail(tone,tmp_path):
    for scenario,code in (("short","AUDIO_TOO_SHORT"),("silent","AUDIO_SILENT"),("quiet","AUDIO_TARGET_UNREACHABLE")):
        edit,sources=real_edit(tone,1 if scenario=="short" else 12)
        if scenario!="short":
            path=tmp_path/f"{scenario}.wav"
            run_ffmpeg(["-i",tone,"-af",f"volume={0 if scenario=='silent' else 0.01}",path],tmp_path)
            edit,sources=real_edit(path)
        edit["audio_mix"]["settings"]["normalization"]["mode"]="master";seal(edit)
        with pytest.raises(AudioError,match=code):mix_pcm(edit,sources,tmp_path/scenario)


def test_peak_retry_measures_final_encoded_mp4(tone,tmp_path,monkeypatch):
    import fsf.runtime.audio_mix as audio
    edit,sources=real_edit(tone)
    pcm,report=mix_pcm(edit,sources,tmp_path/"pcm")
    video=tmp_path/"video.mp4"
    run_ffmpeg(["-f","lavfi","-i","testsrc2=size=128x72:rate=24:duration=12","-an","-c:v","libx264","-preset","ultrafast",video],tmp_path)
    actual=audio.measure;seen=[]
    def fail_first_two(path,folder,**kwargs):
        result=actual(path,folder,**kwargs)
        seen.append(result)
        if len(seen)<=2:result={**result,"true_peak_dbtp":0}
        return result
    monkeypatch.setattr(audio,"measure",fail_first_two)
    out,result=encode_verified(edit,pcm,video,tmp_path,report)
    assert len(result["attempts"])==3 and result["final"]["true_peak_dbtp"]<=-1
    assert result["listening_review"]=="not_reviewed" and out.is_file()


def test_shared_deadline_expires_between_stages(tmp_path):
    token=ACTIVE_DEADLINE.set(Deadline(0))
    try:
        with pytest.raises(AudioError,match="AUDIO_DEADLINE_EXCEEDED"):run_ffmpeg(["-version"],tmp_path)
    finally:ACTIVE_DEADLINE.reset(token)


def test_analysis_cache_reuses_only_unchanged_raw_content(tone,tmp_path):
    edit,sources=real_edit(tone)
    edit['audio_mix']['settings']['normalization'].update(mode='dialogue_groups',groups=[{'group_id':'G','event_ids':['EV01']}]);seal(edit)
    _,first=mix_pcm(edit,sources,tmp_path/'one',cache=tmp_path/'cache')
    edit['audio_mix']['track_settings'][0]['gain_db']=-3;seal(edit)
    _,second=mix_pcm(edit,sources,tmp_path/'two',cache=tmp_path/'cache')
    assert not first['groups'][0]['analysis_cache_hit'] and second['groups'][0]['analysis_cache_hit']
    assert first['groups'][0]['input_digest']==second['groups'][0]['input_digest']
    assert first['processing_digest']!=second['processing_digest']
    for cache in (tmp_path/'cache').glob('*.json'):cache.write_text('{"broken":true}')
    _,third=mix_pcm(edit,sources,tmp_path/'three',cache=tmp_path/'cache')
    assert not third['groups'][0]['analysis_cache_hit']


@pytest.mark.parametrize('clock,frames', [({'num':24,'den':1},144),({'num':24000,'den':1001},141)])
def test_encoded_mp4_sample_alignment_and_padding(clock,frames,tmp_path):
    import array
    from fsf.runtime.audio_mix import decode
    from fsf.runtime.post_sources import file_sha
    count=frame_sample(frames,clock)
    samples=array.array('f',[0])*(count*2)
    # Two bounded impulses, avoiding fades at the exact film edge.
    samples[2000:2002]=array.array('f',[0.25,0.25])
    samples[24000:24002]=array.array('f',[0.25,0.25])
    path=tmp_path/'impulses.f32';samples.tofile(path.open('wb'))
    edit=audio_edit();edit['clock_rate']=clock
    edit['sources'][0]['duration_frames']=frames
    edit['clips']=[{**edit['clips'][0],'in_frame':0,'out_frame':frames}]
    edit['audio_mix']['events'][0]['source_out_sample']=count;seal(edit)
    video=tmp_path/'video.mp4'
    run_ffmpeg(['-f','lavfi','-i',f"testsrc2=size=128x72:rate={clock['num']}/{clock['den']}",'-frames:v',str(frames),'-an','-c:v','libx264','-preset','ultrafast',video],tmp_path)
    report={'listen_scope':'mix','listening_review':'not_reviewed'}
    output,_=encode_verified(edit,path,video,tmp_path,report)
    decoded=tmp_path/'decoded.f32';length=decode(output,1,decoded)
    assert 0<=length-count<=1024
    data=array.array('f');data.frombytes(decoded.read_bytes())
    peak=max(range(500,1500),key=lambda i:abs(data[i*2]))
    assert abs(peak-1000)<=48


def test_master_normalization_preserves_post_gain_in_final_mp4(tone,tmp_path):
    edit,sources=real_edit(tone)
    edit['audio_mix']['settings']['normalization']['mode']='master'
    edit['audio_mix']['settings']['master_gain_db']=-3;seal(edit)
    pcm,report=mix_pcm(edit,sources,tmp_path/'mix')
    video=tmp_path/'video.mp4'
    run_ffmpeg(['-f','lavfi','-i','testsrc2=size=128x72:rate=24:duration=12','-an','-c:v','libx264','-preset','ultrafast',video],tmp_path)
    _,report=encode_verified(edit,pcm,video,tmp_path,report)
    assert abs(report['final']['integrated_lufs']+19)<1


def test_two_real_stages_share_remaining_budget(tmp_path):
    from fsf.executors.subprocess_safety import run_safe_subprocess
    import time
    budget=Deadline(0.6)
    first=run_safe_subprocess([sys.executable,'-c','import time;time.sleep(0.15)'],timeout_seconds=budget.remaining())
    assert first.returncode==0
    left=budget.remaining()
    assert left<0.6
    result=run_safe_subprocess([sys.executable,'-c','import time;time.sleep(3)'],timeout_seconds=left)
    assert result.timed_out
    with pytest.raises(AudioError,match='AUDIO_DEADLINE_EXCEEDED'):budget.remaining()


def test_audio_source_is_immutable_and_delete_id_never_reused():
    edit=audio_edit();original=copy.deepcopy(edit)
    source=edit['audio_mix']['sources'][0]
    with pytest.raises(AudioError):apply_edit_patch(edit,patch(edit,{'op':'add_audio_source','source':source}))
    assert edit==original
    deleted=apply_edit_patch(edit,patch(edit,{'op':'remove_audio_event','event_id':'EV01'},{'op':'remove_audio_source','audio_source_id':'AU01'}))
    with pytest.raises(AudioError,match='AUDIO_ID_RETIRED'):apply_edit_patch(deleted,patch(deleted,{'op':'add_audio_source','source':source}))


def test_overlapping_events_and_regions_rejected():
    edit=audio_edit()
    second=copy.deepcopy(edit['audio_mix']['events'][0]);second['event_id']='EV02'
    with pytest.raises(AudioError):apply_edit_patch(edit,patch(edit,{'op':'upsert_audio_event','event':second}))
    event=copy.deepcopy(edit['audio_mix']['events'][0])
    event['regions']=[{'region_id':'R1','start_sample':0,'end_sample':1000,'gain_db':0,'mute':False},{'region_id':'R2','start_sample':999,'end_sample':2000,'gain_db':0,'mute':False}]
    with pytest.raises(AudioError):apply_edit_patch(edit,patch(edit,{'op':'upsert_audio_event','event':event}))


@pytest.mark.parametrize('argv',[['audio','inspect'],['audio','inspect','x','--unknown','synthetic_private_marker'],['audio','import','x','--out','a','--out','b']])
def test_audio_cli_errors_are_stable_and_sanitized(argv,capsys):
    from fsf.cli.edit import main
    assert main(argv)==2
    body=json.loads(capsys.readouterr().out)
    assert body['error']['code']=='AUDIO_USAGE'
    assert 'synthetic_private_marker' not in json.dumps(body)


def test_missing_filter_and_bad_measurement_have_distinct_codes(tmp_path,monkeypatch):
    import types
    import fsf.runtime.post_render as renderer
    import fsf.runtime.audio_mix as audio
    token=ACTIVE_DEADLINE.set(Deadline())
    try:
        monkeypatch.setattr(renderer,'run_safe_subprocess',lambda *a,**k:types.SimpleNamespace(returncode=1,stderr="No such filter: synthetic",stdout="",timed_out=False))
        with pytest.raises(AudioError,match='AUDIO_FILTER_UNAVAILABLE'):renderer.run_ffmpeg([],tmp_path)
        monkeypatch.setattr(audio,'_run',lambda *a:types.SimpleNamespace(stderr='{"input_i":"nan","input_tp":"0","input_lra":"0"}'))
        with pytest.raises(AudioError,match='AUDIO_ANALYSIS_FAILED'):audio.measure(tmp_path/'x',tmp_path)
    finally:ACTIVE_DEADLINE.reset(token)


@pytest.mark.parametrize('rate,channels',[(44100,1),(48000,2),(96000,2)])
def test_canonical_resample_and_short_sfx(rate,channels,tmp_path):
    from fsf.runtime.audio_mix import decode
    path=tmp_path/'tone.wav'
    run_ffmpeg(['-f','lavfi','-i',f'sine=frequency=500:sample_rate={rate}:duration=1','-ac',str(channels),path],tmp_path)
    assert decode(path,0,tmp_path/'canonical.f32')==48000
    edit,sources=real_edit(path,1)
    edit['audio_mix']['sources'][0]['binding']['canonical_samples']=48000
    edit['audio_mix']['events'][0]['source_out_sample']=100
    seal(edit);validate_edit(edit)
    pcm,report=mix_pcm(edit,sources,tmp_path/'sfx')
    assert pcm.stat().st_size==48000*8 and report['samples']==48000


def test_region_gain_and_mute_preserve_unselected_samples(tone,tmp_path):
    import array
    edit,sources=real_edit(tone)
    first,_=mix_pcm(edit,sources,tmp_path/'first')
    event=edit['audio_mix']['events'][0]
    event['regions']=[{'region_id':'QUIET','start_sample':48000,'end_sample':96000,'gain_db':-3,'mute':False},
        {'region_id':'MUTE','start_sample':144000,'end_sample':192000,'gain_db':0,'mute':True}]
    seal(edit)
    second,_=mix_pcm(edit,sources,tmp_path/'second')
    a=array.array('f');a.frombytes(first.read_bytes());b=array.array('f');b.frombytes(second.read_bytes())
    def rms(data,start,end):return math.sqrt(sum(x*x for x in data[start*2:end*2])/(2*(end-start)))
    assert abs(20*math.log10(rms(b,60000,80000)/rms(a,60000,80000))+3)<0.01
    assert a[20000:40000]==b[20000:40000]
    assert rms(b,150000,180000)==0


def test_final_digital_silence_is_unmeasurable_not_passed(tone,tmp_path):
    from fsf.runtime.audio_mix import numeric_report_valid
    edit,sources=real_edit(tone,1)
    edit['audio_mix']['settings']['master_mute']=True;seal(edit)
    pcm,report=mix_pcm(edit,sources,tmp_path/'mix')
    video=tmp_path/'video.mp4'
    run_ffmpeg(['-f','lavfi','-i','testsrc2=size=128x72:rate=24:duration=1','-an','-c:v','libx264','-preset','ultrafast',video],tmp_path)
    _,report=encode_verified(edit,pcm,video,tmp_path,report)
    assert report['numeric_quality']=='unmeasurable'
    assert report['final']['true_peak_dbtp'] is None
    assert report['final']['peak_status']=='verified_digital_silence'
    assert report['digital_silence_verified'] and numeric_report_valid(report)
    report['numeric_quality']='passed'
    assert not numeric_report_valid(report)


def test_missing_both_measurements_does_not_prove_silence(tone,tmp_path,monkeypatch):
    import fsf.runtime.audio_mix as audio
    edit,sources=real_edit(tone,1)
    pcm,report=mix_pcm(edit,sources,tmp_path/'mix')
    video=tmp_path/'video.mp4'
    run_ffmpeg(['-f','lavfi','-i','testsrc2=size=128x72:rate=24:duration=1','-an','-c:v','libx264','-preset','ultrafast',video],tmp_path)
    monkeypatch.setattr(audio,'measure',lambda *a,**k:{'integrated_lufs':None,'true_peak_dbtp':None,'lra_lu':None})
    with pytest.raises(AudioError,match='AUDIO_ANALYSIS_FAILED'):encode_verified(edit,pcm,video,tmp_path,report)
    assert 'numeric_quality' not in report


def test_old_passed_report_without_peak_is_rejected():
    from fsf.runtime.audio_mix import numeric_report_valid
    assert not numeric_report_valid({'numeric_quality':'passed','final':{'integrated_lufs':None,'true_peak_dbtp':None}})


def test_final_silence_cannot_satisfy_master_target(tone,tmp_path):
    edit,sources=real_edit(tone)
    edit['audio_mix']['settings']['normalization']['mode']='master'
    edit['audio_mix']['settings']['master_mute']=True;seal(edit)
    pcm,report=mix_pcm(edit,sources,tmp_path/'mix')
    video=tmp_path/'video.mp4'
    run_ffmpeg(['-f','lavfi','-i','testsrc2=size=128x72:rate=24:duration=12','-an','-c:v','libx264','-preset','ultrafast',video],tmp_path)
    with pytest.raises(AudioError,match='AUDIO_TARGET_UNREACHABLE'):encode_verified(edit,pcm,video,tmp_path,report)
