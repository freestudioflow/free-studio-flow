"""Synthetic transcript contracts and real PCM; never a neural ASR claim."""
import copy
from fractions import Fraction
import hashlib
import math
import struct
import wave
import pytest
from fsf.production.contracts import ProductionError,seal
from fsf.production.speech import (build_expected_dialogue,build_coverage_policy,evaluate_dialogue_coverage,
    frame_sample_position,frame_to_sample,sample_to_frame,normalize_words,recognize_dialogue,
    build_ducking_envelope,ducking_gain_at)


def expected(clock=None):
    return build_expected_dialogue(clock=clock or {'num':24,'den':1},timeline_frames=48,events=[
        {'event_id':'EV01','track_id':'VO','clip_id':'C01','start_frame':12,'end_frame':24,'text':'One, TWO words.'}])


def policy(start=0,end=0,padding=0):
    return build_coverage_policy(max_start_drift_samples=start,max_end_drift_samples=end,max_padding_samples=padding)


def transcript(value=None):
    value=value or expected();event=value['events'][0]
    return seal({'schema_version':'fsf_dialogue_transcript_v1','artifact_sha256':'a'*64,'recognizer_digest':'b'*64,
        'sample_rate':48000,'sample_count':frame_to_sample(value['timeline_frames'],value['clock'],rounding='ceil'),
        'status':'completed','events':[{'event_id':event['event_id'],'track_id':event['track_id'],'clip_id':event['clip_id'],
            'start_sample':frame_to_sample(event['start_frame'],value['clock'],rounding='floor'),
            'end_sample':frame_to_sample(event['end_frame'],value['clock'],rounding='ceil'),'text':'one two words'}]},'transcript_digest')


def report(intervals=None,total=100):
    return seal({'schema_version':'fsf_dialogue_coverage_report_v1','status':'accepted','artifact_sha256':'a'*64,
        'expected_digest':'b'*64,'transcript_digest':'c'*64,'policy_digest':'d'*64,'sample_rate':48000,
        'timeline_samples':total,'normalization':'unicode_nfkc_casefold_punctuation_separator_v1',
        'listening_review':'not_reviewed','intervals':intervals if intervals is not None else [
            {'event_id':'E1','track_id':'VO','clip_id':'C1','start_sample':20,'end_sample':40}]},'report_digest')


def envelope(value=None,**kwargs):
    return build_ducking_envelope(value or report(),target_track_id='BGM',attenuation_millidb=6000,
        attack_samples=kwargs.get('attack',10),release_samples=kwargs.get('release',20),
        max_operations=kwargs.get('budget',1000000))


def test_rational_mapping_never_truncates_fps_or_guesses_rounding():
    clock={'num':24000,'den':1001}
    assert frame_sample_position(4320,clock)==8648640
    assert sample_to_frame(8648640,clock)==4320
    fractional={'num':30000,'den':1001}
    assert frame_sample_position(1,fractional)==Fraction(8008,5)
    assert frame_to_sample(1,fractional,rounding='floor')==1601
    assert frame_to_sample(1,fractional,rounding='ceil')==1602
    assert frame_to_sample(1,fractional,rounding='nearest_half_up')==1602
    assert sample_to_frame(1602,fractional)==Fraction(4005,4004)
    with pytest.raises(ProductionError):frame_sample_position(True,clock)


def test_normalization_keeps_marks_and_symbols_and_does_not_translate():
    assert normalize_words('ＯＮＥ, Two!')==['one','two']
    assert normalize_words('C++')==['c','+','+']
    assert normalize_words('café')!=normalize_words('cafe')
    assert normalize_words('Hello')!=normalize_words('您好')
    assert normalize_words('कि')!=normalize_words('क')


@pytest.mark.parametrize('clock',[{'num':24,'den':1},{'num':24000,'den':1001}])
def test_explicit_coverage_and_policy_drifts(clock):
    value=expected(clock);rows=transcript(value)
    rows['events'][0]['start_sample']+=2;rows['events'][0]['end_sample']-=3
    rows=seal(rows,'transcript_digest')
    result=evaluate_dialogue_coverage(value,rows,policy(2,3),artifact_sha256='a'*64)
    assert result['status']=='accepted' and result['listening_review']=='not_reviewed'
    with pytest.raises(ProductionError,match='PRODUCTION_SPEECH_DRIFT'):
        evaluate_dialogue_coverage(value,rows,policy(1,3),artifact_sha256='a'*64)


@pytest.mark.parametrize('mutate,code',[
    (lambda r:r.update(events=[]),'PRODUCTION_SPEECH_MISSING'),
    (lambda r:r.update(status='unknown'),'PRODUCTION_SPEECH_UNKNOWN'),
    (lambda r:r.update(status='failed'),'PRODUCTION_SPEECH_UNKNOWN'),
    (lambda r:r['events'][0].update(text='one two'),'PRODUCTION_SPEECH_TRUNCATED'),
    (lambda r:r['events'][0].update(text='one different words'),'PRODUCTION_SPEECH_TEXT_MISMATCH'),
    (lambda r:r['events'][0].update(track_id='BGM'),'PRODUCTION_SPEECH_IDENTITY'),
    (lambda r:r['events'][0].update(clip_id='other'),'PRODUCTION_SPEECH_IDENTITY'),
    (lambda r:r['events'][0].update(start_sample=50000,end_sample=40000),'PRODUCTION_SPEECH_INTERVAL'),
    (lambda r:r['events'][0].update(end_sample=96001),'PRODUCTION_SPEECH_INTERVAL'),
    (lambda r:r.update(sample_count=95999),'PRODUCTION_SPEECH_SAMPLE_COUNT'),
    (lambda r:r.update(artifact_sha256='e'*64),'PRODUCTION_SPEECH_SOURCE_CHANGED'),
    (lambda r:r['events'][0].update(event_id='unknown'),'PRODUCTION_SPEECH_INVALID'),
    (lambda r:r['events'].append(copy.deepcopy(r['events'][0])),'PRODUCTION_SPEECH_INVALID'),
])
def test_missing_truncated_bad_intervals_unknown_and_identity_never_accept(mutate,code):
    rows=transcript();mutate(rows);rows=seal(rows,'transcript_digest')
    with pytest.raises(ProductionError,match=code):evaluate_dialogue_coverage(expected(),rows,policy(),artifact_sha256='a'*64)


def test_padding_bound_and_no_dialogue_are_explicit():
    rows=transcript();rows['sample_count']+=1024;rows=seal(rows,'transcript_digest')
    assert evaluate_dialogue_coverage(expected(),rows,policy(padding=1024),artifact_sha256='a'*64)['status']=='accepted'
    with pytest.raises(ProductionError,match='PRODUCTION_SPEECH_SAMPLE_COUNT'):
        evaluate_dialogue_coverage(expected(),rows,policy(padding=1023),artifact_sha256='a'*64)
    with pytest.raises(ProductionError):policy(padding=1025)
    wanted=build_expected_dialogue(clock={'num':24,'den':1},timeline_frames=48,events=[])
    rows=transcript();rows['events']=[];rows=seal(rows,'transcript_digest')
    accepted=evaluate_dialogue_coverage(wanted,rows,policy(),artifact_sha256='a'*64)
    duck=envelope(accepted)
    assert all(ducking_gain_at(duck,n)==0 for n in (0,48000,95999))


def test_ducking_boundaries_and_user_gain_are_not_rewritten():
    value=report();before=copy.deepcopy(value);duck=envelope(value)
    assert value==before and duck['user_gain_unchanged'] and duck['gain_mode']=='additive_millidb'
    assert ducking_gain_at(duck,9)==0
    assert ducking_gain_at(duck,15)==-3000
    assert ducking_gain_at(duck,20)==-6000
    assert ducking_gain_at(duck,39)==-6000
    assert ducking_gain_at(duck,50)==-3000
    assert ducking_gain_at(duck,60)==0
    assert duck['pieces'][0]['start_sample']==0 and duck['pieces'][-1]['end_sample']==100
    for a,b in zip(duck['pieces'],duck['pieces'][1:]):assert a['end_sample']==b['start_sample']


def test_overlap_uses_exact_lower_envelope_instead_of_double_attenuation():
    events=[{'event_id':'A','track_id':'VO1','clip_id':'C1','start_sample':20,'end_sample':30},
            {'event_id':'B','track_id':'VO2','clip_id':'C2','start_sample':37,'end_sample':50}]
    duck=envelope(report(events),attack=11,release=17)
    def reference(n,event):
        start,end=event['start_sample'],event['end_sample']
        if start-11<=n<start:return Fraction(-6000*(n-start+11),11)
        if start<=n<end:return Fraction(-6000)
        if end<=n<end+17:return Fraction(-6000)+Fraction(6000*(n-end),17)
        return Fraction(0)
    for n in range(100):assert ducking_gain_at(duck,n)==min(reference(n,e) for e in events)
    with pytest.raises(ProductionError,match='PRODUCTION_DUCKING_COMPLEXITY'):envelope(report(events),budget=1)


def test_zero_ramps_and_timeline_clipping_do_not_accelerate_release():
    duck=envelope(attack=0,release=0)
    assert ducking_gain_at(duck,19)==0 and ducking_gain_at(duck,20)==-6000
    assert ducking_gain_at(duck,40)==0
    clipped=envelope(report([{'event_id':'A','track_id':'VO','clip_id':'C','start_sample':0,'end_sample':90}]))
    assert ducking_gain_at(clipped,0)==-6000 and ducking_gain_at(clipped,99)==-3300
    with pytest.raises(ProductionError,match='PRODUCTION_DUCKING_SELF_TARGET'):
        build_ducking_envelope(report(),target_track_id='VO',attenuation_millidb=6000,attack_samples=1,release_samples=1)


def write_wave(path):
    with wave.open(str(path),'wb') as sound:
        sound.setnchannels(1);sound.setsampwidth(2);sound.setframerate(48000)
        sound.writeframes(b''.join(struct.pack('<h',round(12000*math.sin(2*math.pi*440*n/48000))) for n in range(96000)))


def test_real_pcm_recognizer_boundary_is_source_bound_and_does_not_claim_real_asr(tmp_path):
    path=tmp_path/'tone.wav';write_wave(path);value=expected()
    def synthetic_callback(source,expected_copy,deadline):
        assert source==path and expected_copy==value
        expected_copy['events'].clear()
        return {'status':'completed','events':transcript()['events']}
    rows=recognize_dialogue(path,value,recognizer=synthetic_callback,recognizer_digest='d'*64)
    assert value==expected() and rows['sample_count']==96000
    assert rows['artifact_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    result=evaluate_dialogue_coverage(value,rows,policy(),artifact_sha256=rows['artifact_sha256'])
    assert result['listening_review']=='not_reviewed'
    def mutating(source,*_):
        source.write_bytes(source.read_bytes()+b'x')
        return {'status':'completed','events':transcript()['events']}
    with pytest.raises(ProductionError,match='PRODUCTION_SPEECH_SOURCE_CHANGED'):
        recognize_dialogue(path,value,recognizer=mutating,recognizer_digest='d'*64)


def test_untrusted_callback_failure_is_sanitized(tmp_path):
    path=tmp_path/'tone.wav';write_wave(path)
    def broken(*_):raise RuntimeError('SYNTHETIC_SECRET_DO_NOT_RETURN')
    with pytest.raises(ProductionError,match='PRODUCTION_SPEECH_ANALYSIS_FAILED') as exc:
        recognize_dialogue(path,expected(),recognizer=broken,recognizer_digest='d'*64)
    assert 'SYNTHETIC_SECRET' not in str(exc.value)


@pytest.mark.parametrize('mutate',[
    lambda r:r['pieces'][0]['start_gain_millidb'].update(den=0),
    lambda r:r['pieces'][0].update(start_sample=1),
    lambda r:r.update(gain_mode='absolute_db'),lambda r:r.update(user_gain_unchanged=False),
])
def test_resealed_malformed_envelopes_are_not_accepted_as_valid(mutate):
    value=envelope();mutate(value);value=seal(value,'envelope_digest')
    with pytest.raises(ProductionError):ducking_gain_at(value,0)
