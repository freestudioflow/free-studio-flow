"""Two-hour state/coverage arithmetic, not a generated or qualified film."""
import copy
import json
import subprocess
import sys

import pytest

from fsf.production.contracts import ProductionError,seal
from fsf.production.memory import state_at,interval_requirements
from fsf.production.segments import compile_segments
from fsf.production.speech import build_expected_dialogue,build_coverage_policy,evaluate_dialogue_coverage,frame_to_sample
from test_production_foundation import source,capability
from test_production_memory import world
from test_quality_rollup import fixture


def test_two_hour_global_memory_segments_and_dialogue_preserve_chapter_identity():
    w=world(frames=172800)
    plan=compile_segments(source(172800),capability(81,overlap=1,continuation='previous_frame'),shot_id='long_shot',policy_digest='e'*64)
    assert plan['segments'][-1]['core'][1]==172800
    assert all(s['model_frames']<=81 for s in plan['segments'])
    events=[]
    for i,c in enumerate(w['chapters']):
        start=c['start_frame']
        events.append(dict(event_id=f'line_{i}',track_id='hero',clip_id=c['chapter_id'],start_frame=start,end_frame=start+24,text=f'Chapter {i}'))
        assert state_at(w,start)['facts']['hero_clothes']['value']=='blue_jacket'
        assert interval_requirements(w,start,start+24)['world_digest']==w['world_digest']
    expected=build_expected_dialogue(clock=w['clock'],timeline_frames=172800,events=events)
    transcript=seal(dict(schema_version='fsf_dialogue_transcript_v1',artifact_sha256='a'*64,recognizer_digest='b'*64,
        sample_rate=48000,sample_count=345600000,status='completed',events=[dict(event_id=e['event_id'],track_id=e['track_id'],clip_id=e['clip_id'],
            start_sample=frame_to_sample(e['start_frame'],w['clock'],rounding='floor'),end_sample=frame_to_sample(e['end_frame'],w['clock'],rounding='ceil'),text=e['text']) for e in events]),'transcript_digest')
    policy=build_coverage_policy(max_start_drift_samples=0,max_end_drift_samples=0,max_padding_samples=1024)
    report=evaluate_dialogue_coverage(expected,transcript,policy,artifact_sha256='a'*64)
    assert report['status']=='accepted' and report['listening_review']=='not_reviewed'
    wrong=copy.deepcopy(transcript);wrong['events'][20]['clip_id']='chapter_0'
    with pytest.raises(ProductionError,match='IDENTITY'):
        evaluate_dialogue_coverage(expected,seal(wrong,'transcript_digest'),policy,artifact_sha256='a'*64)


def test_quality_cli_retains_unresolved_chapter_failure(tmp_path):
    manifest,reports=fixture();reports[40]=seal({**reports[40],'verdict':'unknown'},'report_digest')
    for name,value in [('manifest',manifest),('reports',reports)]:
        (tmp_path/(name+'.json')).write_text(json.dumps(value))
    output=tmp_path/'rollup.json'
    child=subprocess.run([sys.executable,'-m','fsf.cli','production','quality-rollup',str(tmp_path/'manifest.json'),
        '--reports',str(tmp_path/'reports.json'),'--out',str(output)],capture_output=True,text=True,timeout=60)
    assert child.returncode==0,child.stdout
    value=json.loads(output.read_text());assert not value['machine_accepted']
    assert value['unknown']==['chapter_0']
    assert value['calibration_status']=='not_attested'
