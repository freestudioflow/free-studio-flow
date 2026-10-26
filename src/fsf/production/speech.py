"""Dialogue evidence and additive ducking, independent of any ASR provider.

Times are global timeline positions. Unicode NFKC/casefold normalization makes
punctuation/whitespace separators, retains letters/numbers/combining marks and
symbol tokens, and does not stem, translate, remove accents, or infer synonyms.
Text equality is a bounded coverage check, not a semantic or listening review.
"""
from __future__ import annotations

from fractions import Fraction
import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
import wave

from fsf.core.asset_library import _no_links, _open_reader
from fsf.production.contracts import ProductionError, digest, integer, key, seal

RATE=48000
NORMALIZATION='unicode_nfkc_casefold_punctuation_separator_v1'


def _invalid():raise ProductionError('PRODUCTION_SPEECH_INVALID')


def _hash(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):_invalid()
    return value


def _clock(value):
    if not isinstance(value,dict) or set(value)!={'num','den'}:_invalid()
    integer(value['num'],1);integer(value['den'],1)
    return Fraction(value['num'],value['den'])


def frame_sample_position(frame,clock):
    """Exact rational sample position; no implicit rounding or integer fps."""
    integer(frame)
    return Fraction(frame*RATE,1)/_clock(clock)


def frame_to_sample(frame,clock,*,rounding):
    position=frame_sample_position(frame,clock)
    if rounding=='floor':return position.numerator//position.denominator
    if rounding=='ceil':return -(-position.numerator//position.denominator)
    if rounding=='nearest_half_up':return (position.numerator*2+position.denominator)//(2*position.denominator)
    _invalid()


def sample_to_frame(sample,clock):
    integer(sample)
    return Fraction(sample, RATE)*_clock(clock)


def normalize_words(text):
    if not isinstance(text,str) or not 0<len(text)<=32768:_invalid()
    try:text.encode('utf-8')
    except UnicodeError:_invalid()
    result=[];word=[]
    def flush():
        if word:result.append(''.join(word));word.clear()
    for char in unicodedata.normalize('NFKC',text).casefold():
        category=unicodedata.category(char)
        if category[0] in {'L','M','N'}:word.append(char)
        elif category[0] in {'P','Z'} or char.isspace():flush()
        elif category[0]=='S':flush();result.append(char)
        else:_invalid()
    flush()
    if not result:_invalid()
    return result


def _sealed(value,version,field,fields):
    if (not isinstance(value,dict) or set(value)!={*fields,field}
            or value.get('schema_version')!=version):_invalid()
    try:
        if seal(value,field)!=value:_invalid()
    except (ValueError,TypeError,RecursionError):_invalid()
    return value


def build_expected_dialogue(*,clock,timeline_frames,events):
    _clock(clock);integer(timeline_frames,1)
    if not isinstance(events,list) or len(events)>10000:_invalid()
    seen=set()
    for event in events:
        if not isinstance(event,dict) or set(event)!={'event_id','track_id','clip_id','start_frame','end_frame','text'}:_invalid()
        for field in ('event_id','track_id','clip_id'):key(event[field])
        if event['event_id'] in seen:_invalid()
        seen.add(event['event_id'])
        integer(event['start_frame']);integer(event['end_frame'])
        if not 0<=event['start_frame']<event['end_frame']<=timeline_frames:_invalid()
        normalize_words(event['text'])
    return seal({'schema_version':'fsf_expected_dialogue_v1','clock':clock,'sample_rate':RATE,
                 'timeline_frames':timeline_frames,'events':events},'expected_digest')


def _expected(value):
    fields={'schema_version','clock','sample_rate','timeline_frames','events'}
    _sealed(value,'fsf_expected_dialogue_v1','expected_digest',fields)
    if value['sample_rate']!=RATE or build_expected_dialogue(clock=value['clock'],timeline_frames=value['timeline_frames'],events=value['events'])!=value:_invalid()
    return value


def build_coverage_policy(*,max_start_drift_samples,max_end_drift_samples,max_padding_samples):
    integer(max_start_drift_samples);integer(max_end_drift_samples);integer(max_padding_samples,0,1024)
    return seal({'schema_version':'fsf_dialogue_coverage_policy_v1','normalization':NORMALIZATION,
                 'sample_rounding':'outward','max_start_drift_samples':max_start_drift_samples,
                 'max_end_drift_samples':max_end_drift_samples,'max_padding_samples':max_padding_samples},'policy_digest')


def _policy(value):
    _sealed(value,'fsf_dialogue_coverage_policy_v1','policy_digest',
            {'schema_version','normalization','sample_rounding','max_start_drift_samples','max_end_drift_samples','max_padding_samples'})
    if build_coverage_policy(max_start_drift_samples=value['max_start_drift_samples'],
        max_end_drift_samples=value['max_end_drift_samples'],max_padding_samples=value['max_padding_samples'])!=value:_invalid()
    return value


def evaluate_dialogue_coverage(expected,transcript,policy,*,artifact_sha256):
    """Validate exact event coverage under explicit sample drift tolerances.

    Extra/unknown events or padding speech are not silently discarded. Event
    IDs map recognizer results to the fixed intent; track/clip are not labels.
    """
    expected=_expected(expected);policy=_policy(policy);_hash(artifact_sha256)
    _sealed(transcript,'fsf_dialogue_transcript_v1','transcript_digest',
            {'schema_version','artifact_sha256','recognizer_digest','sample_rate','sample_count','status','events'})
    _hash(transcript['recognizer_digest'])
    if transcript['artifact_sha256']!=artifact_sha256:raise ProductionError('PRODUCTION_SPEECH_SOURCE_CHANGED')
    if not isinstance(transcript['status'],str):_invalid()
    if transcript['status'] in {'unknown','failed'}:raise ProductionError('PRODUCTION_SPEECH_UNKNOWN')
    if transcript['status']!='completed' or transcript['sample_rate']!=RATE:_invalid()
    integer(transcript['sample_count'],1)
    total=frame_to_sample(expected['timeline_frames'],expected['clock'],rounding='ceil')
    if not 0<=transcript['sample_count']-total<=policy['max_padding_samples']:
        raise ProductionError('PRODUCTION_SPEECH_SAMPLE_COUNT')
    rows=transcript['events']
    if not isinstance(rows,list) or len(rows)>10000:_invalid()
    wanted={e['event_id']:e for e in expected['events']};actual={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'event_id','track_id','clip_id','start_sample','end_sample','text'}:_invalid()
        for field in ('event_id','track_id','clip_id'):key(row[field])
        if row['event_id'] in actual or row['event_id'] not in wanted:_invalid()
        item=wanted[row['event_id']]
        if any(row[k]!=item[k] for k in ('track_id','clip_id')):raise ProductionError('PRODUCTION_SPEECH_IDENTITY')
        integer(row['start_sample']);integer(row['end_sample'])
        if not 0<=row['start_sample']<row['end_sample']<=total:raise ProductionError('PRODUCTION_SPEECH_INTERVAL')
        actual[row['event_id']]=row
    if set(actual)!=set(wanted):raise ProductionError('PRODUCTION_SPEECH_MISSING')
    intervals=[]
    for event in expected['events']:
        row=actual[event['event_id']];words=normalize_words(event['text']);recognized=normalize_words(row['text'])
        if recognized!=words:
            truncated=len(recognized)<len(words) and (recognized==words[:len(recognized)] or recognized==words[-len(recognized):])
            raise ProductionError('PRODUCTION_SPEECH_TRUNCATED' if truncated else 'PRODUCTION_SPEECH_TEXT_MISMATCH')
        start=frame_to_sample(event['start_frame'],expected['clock'],rounding='floor')
        end=frame_to_sample(event['end_frame'],expected['clock'],rounding='ceil')
        if abs(row['start_sample']-start)>policy['max_start_drift_samples'] or abs(row['end_sample']-end)>policy['max_end_drift_samples']:
            raise ProductionError('PRODUCTION_SPEECH_DRIFT')
        intervals.append({k:row[k] for k in ('event_id','track_id','clip_id','start_sample','end_sample')})
    return seal({'schema_version':'fsf_dialogue_coverage_report_v1','status':'accepted',
                 'artifact_sha256':artifact_sha256,'expected_digest':expected['expected_digest'],
                 'transcript_digest':transcript['transcript_digest'],'policy_digest':policy['policy_digest'],
                 'sample_rate':RATE,'timeline_samples':total,'intervals':intervals,
                 'normalization':NORMALIZATION,'listening_review':'not_reviewed'},'report_digest')


def _file_hash(path,deadline):
    path=Path(path).absolute()
    _no_links(path);h=hashlib.sha256();size=0
    with _open_reader(path) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):_invalid()
        while block:=stream.read(1024*1024):
            if time.monotonic()>=deadline:raise ProductionError('PRODUCTION_SPEECH_DEADLINE')
            size+=len(block)
            if size>64*1024**3:raise ProductionError('PRODUCTION_SPEECH_SOURCE_LIMIT')
            h.update(block)
    return h.hexdigest()


def recognize_dialogue(audio_path,expected,*,recognizer,recognizer_digest,seconds=300):
    """Optional caller-owned ASR callback(path, expected, deadline).

    Input is decoded 48kHz PCM WAV (mono/stereo), not arbitrary compressed audio.
    Callback returns only status/events. It must honor its deadline; an in-process
    callback is not a sandbox or forcibly interruptible. Use a bounded process
    adapter for untrusted or potentially blocking recognizers.
    """
    _expected(expected);_hash(recognizer_digest);integer(seconds,1,5400)
    if not callable(recognizer):raise ProductionError('PRODUCTION_SPEECH_RECOGNIZER_REQUIRED')
    deadline=time.monotonic()+seconds;before=_file_hash(audio_path,deadline)
    try:
        with _open_reader(audio_path) as stream,wave.open(stream,'rb') as sound:
            channels=sound.getnchannels();width=sound.getsampwidth();declared=sound.getnframes();count=0
            if sound.getframerate()!=RATE or channels not in (1,2) or width not in (1,2,3,4) or sound.getcomptype()!='NONE':_invalid()
            while block:=sound.readframes(8192):
                if time.monotonic()>=deadline:raise ProductionError('PRODUCTION_SPEECH_DEADLINE')
                if len(block)%(channels*width):_invalid()
                count+=len(block)//(channels*width)
            if count!=declared or count<1:raise ProductionError('PRODUCTION_SPEECH_DECODE_FAILED')
        try:result=recognizer(audio_path,copy.deepcopy(expected),deadline)
        except Exception:raise ProductionError('PRODUCTION_SPEECH_ANALYSIS_FAILED') from None
    except ProductionError:raise
    except Exception:raise ProductionError('PRODUCTION_SPEECH_ANALYSIS_FAILED') from None
    if time.monotonic()>=deadline:raise ProductionError('PRODUCTION_SPEECH_DEADLINE')
    if _file_hash(audio_path,deadline)!=before:raise ProductionError('PRODUCTION_SPEECH_SOURCE_CHANGED')
    if (not isinstance(result,dict) or set(result)!={'status','events'} or not isinstance(result['status'],str)
            or result['status'] not in {'completed','unknown','failed'}
            or not isinstance(result['events'],list) or len(result['events'])>10000):_invalid()
    return seal({'schema_version':'fsf_dialogue_transcript_v1','artifact_sha256':before,
                 'recognizer_digest':recognizer_digest,'sample_rate':RATE,'sample_count':count,
                 'status':result['status'],'events':result['events']},'transcript_digest')


def _ratio(value):return {'num':value.numerator,'den':value.denominator}


def build_ducking_envelope(coverage,*,target_track_id,attenuation_millidb,attack_samples,release_samples,max_operations=1000000):
    """Lower envelope of additive linear-dB ramps on integer sample positions.

    Ramps use their declared slopes, clipped to the timeline without speeding
    them up. Zero attack/release permits a step. User gain is neither read nor
    rewritten: consumers add this envelope to that gain. No LUFS target exists.
    An operation budget bounds pathological overlapping dialogue, not duration.
    """
    _sealed(coverage,'fsf_dialogue_coverage_report_v1','report_digest',
        {'schema_version','status','artifact_sha256','expected_digest','transcript_digest','policy_digest',
         'sample_rate','timeline_samples','intervals','normalization','listening_review'})
    if (coverage['status']!='accepted' or coverage['sample_rate']!=RATE or coverage['listening_review']!='not_reviewed'
            or coverage['normalization']!=NORMALIZATION):_invalid()
    for name in ('artifact_sha256','expected_digest','transcript_digest','policy_digest'):_hash(coverage[name])
    key(target_track_id);integer(attenuation_millidb,0,120000);integer(attack_samples);integer(release_samples);integer(max_operations,1)
    total=integer(coverage['timeline_samples'],1);events=coverage['intervals']
    if not isinstance(events,list) or len(events)>10000:_invalid()
    edges={0:{'add':[],'remove':[]},total:{'add':[],'remove':[]}};lines={};ordinal=0
    def add(left,right,slope,intercept):
        nonlocal ordinal
        left=max(0,left);right=min(total,right)
        if left>=right:return
        ordinal+=1;lines[ordinal]=(slope,intercept)
        edges.setdefault(left,{'add':[],'remove':[]})['add'].append(ordinal)
        edges.setdefault(right,{'add':[],'remove':[]})['remove'].append(ordinal)
    seen=set()
    for event in events:
        if not isinstance(event,dict) or set(event)!={'event_id','track_id','clip_id','start_sample','end_sample'}:_invalid()
        for field in ('event_id','track_id','clip_id'):key(event[field])
        if event['event_id'] in seen:_invalid()
        seen.add(event['event_id'])
        if event['track_id']==target_track_id:raise ProductionError('PRODUCTION_DUCKING_SELF_TARGET')
        start=integer(event['start_sample']);end=integer(event['end_sample'])
        if not 0<=start<end<=total:_invalid()
        amount=Fraction(attenuation_millidb)
        if attack_samples:
            slope=-amount/attack_samples;add(start-attack_samples,start,slope,-slope*(start-attack_samples))
        add(start,end,Fraction(0),-amount)
        if release_samples:
            slope=amount/release_samples;add(end,end+release_samples,slope,-amount-slope*end)
    active={0:(Fraction(0),Fraction(0))};positions=sorted(edges);pieces=[];operations=0
    for index,left in enumerate(positions[:-1]):
        for ident in edges[left]['remove']:active.pop(ident,None)
        for ident in edges[left]['add']:active[ident]=lines[ident]
        right=positions[index+1];values=list(active.values());operations+=len(values)**2
        if operations>max_operations:raise ProductionError('PRODUCTION_DUCKING_COMPLEXITY')
        cuts={left,right}
        for i,(a,b) in enumerate(values):
            for c,d in values[i+1:]:
                if a==c:continue
                cross=(d-b)/(a-c)
                if left<cross<right:
                    cut=-(-cross.numerator//cross.denominator)
                    if left<cut<right:cuts.add(cut)
        ordered=sorted(cuts)
        for start,end in zip(ordered,ordered[1:]):
            slope,intercept=min(values,key=lambda pair:(pair[0]*start+pair[1],pair[0]))
            gain0=slope*start+intercept;gain1=slope*end+intercept
            if pieces and pieces[-1]['end_sample']==start and pieces[-1]['_line']==(slope,intercept):
                pieces[-1].update(end_sample=end,end_gain_millidb=_ratio(gain1))
            else:pieces.append({'start_sample':start,'end_sample':end,'start_gain_millidb':_ratio(gain0),
                                'end_gain_millidb':_ratio(gain1),'_line':(slope,intercept)})
    for piece in pieces:piece.pop('_line')
    return seal({'schema_version':'fsf_ducking_envelope_v1','coverage_digest':coverage['report_digest'],
                 'target_track_id':target_track_id,'sample_rate':RATE,'timeline_samples':total,
                 'attenuation_millidb':attenuation_millidb,'attack_samples':attack_samples,'release_samples':release_samples,
                 'gain_mode':'additive_millidb','interpolation':'linear_db_integer_samples','pieces':pieces,
                 'user_gain_unchanged':True,'listening_review':'not_reviewed'},'envelope_digest')


def validate_ducking_envelope(envelope):
    _sealed(envelope,'fsf_ducking_envelope_v1','envelope_digest',
        {'schema_version','coverage_digest','target_track_id','sample_rate','timeline_samples','attenuation_millidb',
         'attack_samples','release_samples','gain_mode','interpolation','pieces','user_gain_unchanged','listening_review'})
    _hash(envelope['coverage_digest']);key(envelope['target_track_id'])
    total=integer(envelope['timeline_samples'],1);amount=integer(envelope['attenuation_millidb'],0,120000)
    integer(envelope['attack_samples']);integer(envelope['release_samples'])
    if (envelope['sample_rate']!=RATE or envelope['gain_mode']!='additive_millidb'
            or envelope['interpolation']!='linear_db_integer_samples' or envelope['user_gain_unchanged'] is not True
            or envelope['listening_review']!='not_reviewed' or not isinstance(envelope['pieces'],list) or not envelope['pieces']):_invalid()
    end=0
    for piece in envelope['pieces']:
        if not isinstance(piece,dict) or set(piece)!={'start_sample','end_sample','start_gain_millidb','end_gain_millidb'}:_invalid()
        left=integer(piece['start_sample']);right=integer(piece['end_sample'])
        if left!=end or not left<right<=total:_invalid()
        end=right
        for field in ('start_gain_millidb','end_gain_millidb'):
            value=piece[field]
            if not isinstance(value,dict) or set(value)!={'num','den'} or type(value['num']) is not int:_invalid()
            integer(value['den'],1)
            if not -amount<=Fraction(value['num'],value['den'])<=0:_invalid()
    if end!=total:_invalid()
    return envelope


def ducking_gain_at(envelope,sample):
    """Exact additive millidB at one sample; does not multiply or alter audio."""
    envelope=validate_ducking_envelope(envelope)
    integer(sample)
    if sample>=envelope['timeline_samples']:_invalid()
    for piece in envelope['pieces']:
        left,right=piece['start_sample'],piece['end_sample']
        if left<=sample<right:
            a=Fraction(piece['start_gain_millidb']['num'],piece['start_gain_millidb']['den'])
            b=Fraction(piece['end_gain_millidb']['num'],piece['end_gain_millidb']['den'])
            return a+(b-a)*Fraction(sample-left,right-left)
    _invalid()
