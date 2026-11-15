"""Local feature preflight and R13 quality rejection vectors."""
import copy
import json
import subprocess
import sys

from PIL import Image
import pytest

from fsf.core.edit_store import read_json
from fsf.production.contracts import ProductionError, seal
from fsf.production.feature import validate_feature
from fsf.production.feature_quality import advisory_call, av_alignment, quality_observation


def film():
    state = {'prop': 'owner_a'}
    shots = []
    tasks = []
    chapters = []
    for index, (start, end) in enumerate(((0, 72000), (72000, 172800))):
        step, chapter = f'step{index}', f'chapter{index}'
        after = {'prop': f'owner_{index}'}
        shots.append(dict(step_id=step, chapter=chapter, range=[start, end], beat_id=f'beat{index}',
                          purpose=f'Advance beat {index}', location='room', asset_ids=['story'],
                          dialogue={'mode': 'no_dialogue', 'reason': 'silent beat'},
                          state_before=state, state_after=after))
        tasks.append(dict(task_id=step, generated_frames=end-start,
                          depends_on=[tasks[-1]['task_id']] if tasks else []))
        chapters.append(dict(chapter_id=chapter, range=[start, end], steps=[step]))
        state = after
    return dict(schema_version='fsf_feature_plan_v1', clock={'num': 24, 'den': 1}, frame_count=172800,
                initial_state={'prop': 'owner_a'}, shots=shots, chapters=chapters), {'tasks': tasks}


@pytest.mark.parametrize('fault', ['chapter', 'missing-dialogue', 'state', 'beat', 'asset', 'dependency', None])
def test_feature_plan_missing_or_stale_story_is_blocked(fault):
    value, plan = film()
    if fault == 'chapter':
        value['chapters'].pop()
    elif fault == 'missing-dialogue':
        value['shots'][1]['dialogue'] = {'mode': 'speech', 'lines': []}
    elif fault == 'state':
        value['shots'][1]['state_before'] = {'prop': 'other'}
    elif fault == 'beat':
        value['shots'][1]['beat_id'] = value['shots'][0]['beat_id']
    elif fault == 'asset':
        value['shots'][1]['asset_ids'] = []
    elif fault == 'dependency':
        plan['tasks'][1]['depends_on'] = []
    else:
        assert validate_feature(seal(value, 'feature_digest'), plan)['seconds'] == [7200, 1]
        value['clock'] = {'num': 24000, 'den': 1001}
        assert validate_feature(seal(value, 'feature_digest'), plan)['seconds'] == [36036, 5]
        return
    with pytest.raises(ProductionError):
        validate_feature(seal(value, 'feature_digest'), plan)


@pytest.mark.parametrize('fault,code', [
    ('offset', 'QC_AV_OFFSET'), ('splice', 'QC_AV_OFFSET'), ('silence', 'QC_SILENCE'),
    ('dialogue', 'QC_DIALOGUE_CLIPPED'), ('cumulative', 'QC_PCM_SURPLUS'), (None, None),
])
@pytest.mark.parametrize('num,den,step', [(24, 1, 2000), (24000, 1001, 2002)])
def test_av_measures_splices_and_non_accumulating_final_surplus(fault, code, num, den, step):
    value = dict(clock={'num': num, 'den': den}, frame_count=48,
                 cuts=[{'frames': [0, 24], 'pcm_start': 0, 'decoded_samples': 24*step+600},
                       {'frames': [24, 48], 'pcm_start': 24*step, 'decoded_samples': 24*step+600}],
                 final_pcm_start=0, final_decoded_samples=48*step+1024, final_splices=[0, 25*step],
                 dialogue=[[7, 19], [28, 35]], intentional_silence=[], silent_gaps=[])
    if fault == 'offset':
        value['final_pcm_start'] = step + 1
    elif fault == 'splice':
        value['final_splices'][1] += 1
    elif fault == 'silence':
        value['silent_gaps'] = [[0, step+1]]
    elif fault == 'dialogue':
        value['dialogue'].append([24, 26])
    elif fault == 'cumulative':
        value['final_decoded_samples'] = 48*step + 1200
    if code:
        with pytest.raises(ProductionError, match=code):
            av_alignment(value)
    else:
        assert av_alignment(value)['final_sample_surplus'] == 1024


def test_advisory_or_missing_samples_cannot_accept_a_shot():
    policy = seal(dict(reference_digest='a'*64, route_digest='b'*64, capability_digest='c'*64,
                       frame_count=24, seam_frame=12), 'policy_digest')
    evidence = {**policy, 'artifact_sha256': 'd'*64, 'request_digest': 'e'*64,
                'samples': {name: {'frame': index, 'pixels_sha256': 'f'*64, 'verdict': 'accept'}
                            for name, index in zip(('head', 'midpoint', 'tail', 'seam_left', 'seam_right'), (0, 12, 23, 11, 12))},
                'checks': dict.fromkeys(('freeze', 'reference_leak', 'proportion', 'continuity'), 'accept'),
                'source': 'locked_local_evaluator'}
    assert quality_observation(policy, seal(evidence, 'evidence_digest'))['verdict'] == 'accept'
    advisory = {**evidence, 'source': 'advisory'}
    assert quality_observation(policy, seal(advisory, 'evidence_digest'))['verdict'] == 'unknown'
    missing = copy.deepcopy(evidence)
    missing['samples'].pop('seam_left')
    assert quality_observation(policy, seal(missing, 'evidence_digest'))['verdict'] == 'unknown'
    evidence['checks']['reference_leak'] = 'reject'
    assert quality_observation(policy, seal(evidence, 'evidence_digest'))['verdict'] == 'reject'
    evidence['route_digest'] = '0'*64
    with pytest.raises(ProductionError, match='QC_BINDING'):
        quality_observation(policy, seal(evidence, 'evidence_digest'))


@pytest.mark.parametrize('status,tries', [('unknown', 1), ('not_started', 4), ('observed', 1)])
def test_advisory_bounded_subprocess_and_explicit_bom_cli_import(tmp_path, status, tries):
    image = tmp_path/'frame.png'
    Image.new('RGB', (1400, 900)).save(image)
    script = tmp_path/'fixture.py'
    script.write_text('import json,sys,base64\nr=json.load(sys.stdin)\n'
                      'assert base64.b64decode(r["images"][0])[:2] == bytes([255,216])\n'
                      'print(json.dumps(dict(request_digest=r["request_digest"],status=sys.argv[1])))\n')
    result = advisory_call([str(image)], [sys.executable, str(script), status], seconds=15)
    assert result['attempts'] == tries and result['advisory_can_authorize'] is False
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'images': [str(image)], 'argv': [sys.executable, str(script), status]}),
                      encoding='utf-8-sig')
    assert read_json(config)['images'] == [str(image)]
    cli = subprocess.run([sys.executable, '-m', 'fsf.cli', 'production', 'feature-advisory', str(config)],
                         capture_output=True, text=True, timeout=30)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert json.loads(cli.stdout)['result']['attempts'] == tries
    with pytest.raises(ProductionError, match='QC_PAYLOAD_LIMIT'):
        advisory_call([str(image)]*7, [sys.executable, str(script), status])


def test_pcm_is_redecoded_instead_of_trusting_claimed_samples(tmp_path):
    import wave
    from fsf.production.feature_quality import verify_pcm_alignment

    value = dict(clock={'num': 24, 'den': 1}, frame_count=2,
                 cuts=[{'frames': [0, 1], 'pcm_start': 0, 'decoded_samples': 2000},
                       {'frames': [1, 2], 'pcm_start': 2000, 'decoded_samples': 2000}],
                 final_pcm_start=0, final_decoded_samples=4000, final_splices=[0, 2000],
                 dialogue=[], intentional_silence=[], silent_gaps=[])
    paths = [tmp_path / name for name in ('first.wav', 'second.wav', 'final.wav')]
    for path, count in zip(paths, (2000, 2000, 4000)):
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x10\x00' * count)
    result = verify_pcm_alignment(value, paths[:2], paths[2])
    assert result['scope'] == 'decoded_pcm_with_supplied_splice_coordinates'
    paths[-1].write_bytes(paths[-1].read_bytes()[:-2])
    with pytest.raises(ProductionError, match='QC_PCM_DECODE'):
        verify_pcm_alignment(value, paths[:2], paths[2])
