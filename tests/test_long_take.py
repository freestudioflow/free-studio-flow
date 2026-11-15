"""Frozen R13 long-take local acceptance vectors."""
import copy
import json
import subprocess
import sys

import pytest

from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.control import file_identity
from fsf.production.long_take import CLOCKS, SEEDS, measure, probe, review_take


def fixture(count=24, clock=CLOCKS[0]):
    scene = seal(dict(schema_version='scene_spec_v1', units='m', proxy='analytic_sphere_v1',
                      reference_m=1, measured_m=1, sphere_radii_m={'left': .3, 'right': .1}), 'scene_digest')
    action = seal(dict(schema_version='action_graph_v1', scene_digest=scene['scene_digest'],
                       logical_shot_id='one', initial_holder='left',
                       contacts=[{'range': [0, count], 'pair': ['left', 'right']}],
                       transfers=[{'frame': 1, 'from': 'left', 'to': 'right'}]), 'action_digest')
    camera = seal(dict(schema_version='camera_spec_v1', scene_digest=scene['scene_digest'],
                       action_digest=action['action_digest'], logical_shot_id='one', clock=clock,
                       range=[0, count], resolution=[1920, 1080], camera_id='one',
                       targets=['left'], seams={str(count//2): {'position': [0, 0, 0], 'yaw_deg': 0}}),
                  'camera_digest')
    frames = [dict(index=i, camera_id='one', camera=[0, 0, 0], yaw_deg=0,
                   positions={'left': [2, -.2, 0], 'right': [2, .2, 0]},
                   holder='left' if i == 0 else 'right') for i in range(count)]
    return scene, action, camera, frames


@pytest.mark.parametrize('clock,seconds', [(CLOCKS[0], [180, 1]), (CLOCKS[1], [9009, 50])])
def test_all_4320_frames_and_clock_coverage(clock, seconds):
    scene, action, camera, frames = fixture(4320, clock)
    action = seal(dict(action, speed=[{'subject': 'left', 'range': [0, 4320],
                                      'speed_m_s': .0004 * clock['num'] / clock['den']}]), 'action_digest')
    camera['action_digest'] = action['action_digest']
    camera['seams']['2160']['position'][0] = .0004 * 2160
    camera = seal(camera, 'camera_digest')
    docs = scene, action, camera
    for index, frame in enumerate(frames):
        offset = .0004 * index
        frame['camera'][0] = offset
        for pose in frame['positions'].values():
            pose[0] += offset
    result = measure(*docs, frames, core_ranges=[[0, 2160], [2160, 4320]])
    assert result['status'] == 'passed'
    assert result['seconds'] == seconds and result['frame_count'] == 4320
    assert result['visible_contact_frames'] == 4320
    assert result['swept_checks'] == 8638
    assert not result['cross_model_qualified'] and not result['neural_take_qualified']
    frames[2500]['holder'] = 'left'
    assert measure(*docs, frames, core_ranges=[[0, 2160], [2160, 4320]])['first_frame']['ownership'] == 2500


@pytest.mark.parametrize('clock', CLOCKS)
@pytest.mark.parametrize('delta,subframes', [(.01, 0), (.011, 4)])
def test_swept_trigger_uses_per_frame_displacement(clock, delta, subframes):
    *docs, frames = fixture(3, clock)
    frames[1]['positions']['right'][1] += delta
    frames[2]['positions']['right'][1] += delta
    receipt = measure(*docs, frames, core_ranges=[[0, 3]])
    assert receipt['subframes'] == subframes


@pytest.mark.parametrize('fault,code', [('cut', 'angular_speed'), ('missing', 'contact_unobserved'),
                                         ('crossing', 'swept_penetration'), ('seam', 'camera_seam')])
def test_local_faults_fail_or_remain_unknown(fault, code):
    *docs, frames = fixture(4)
    if fault == 'cut':
        frames[2]['yaw_deg'] = 50
    elif fault == 'missing':
        del frames[2]['positions']['right']
    elif fault == 'crossing':
        frames[2]['positions']['right'][1] = -.6
    elif fault == 'seam':
        docs[2]['seams']['2']['position'][0] = .002
        docs[2] = seal(docs[2], 'camera_digest')
    result = measure(*docs, frames, core_ranges=[[0, 2], [2, 4]])
    assert result['status'] == ('unknown' if fault == 'missing' else 'rejected')
    assert code in result['defects'] or code in result['unknown']


def test_invalid_proxy_never_qualifies_and_gaps_reject():
    scene, action, camera, frames = fixture(4)
    scene = seal(dict(scene, proxy='foreign_mesh'), 'scene_digest')
    action = seal(dict(action, scene_digest=scene['scene_digest']), 'action_digest')
    camera = seal(dict(camera, scene_digest=scene['scene_digest'], action_digest=action['action_digest']), 'camera_digest')
    assert measure(scene, action, camera, frames, core_ranges=[[0, 4]])['status'] == 'unknown'
    with pytest.raises(ProductionError, match='LT_COVERAGE'):
        measure(scene, action, camera, frames, core_ranges=[[0, 1], [2, 4]])


def observations():
    def arm(seed, segments, anchor):
        anchors, channels = [{'id': anchor}], [{'id': 'depth'}]
        observations = []
        for index in range(segments):
            request = seal(dict(schema_version='rc11_generator_v2', inputs=[], anchors=anchors,
                                bundle_window={'channels': channels}, logical_shot_id='shot',
                                segment_id=f'seg{index}', core_range=[index*24, (index+1)*24],
                                lock_digest='d'*64, tail_padding=0, clock=CLOCKS[0]), 'request_digest')
            observations.append(dict(request=request, request_digest=request['request_digest'],
                                     consumed_digests=[digest(item) for item in anchors + channels],
                                     first_frame_sha256='a'*64, last_frame_sha256='b'*64))
        return dict(seed=seed, parameters_digest='e'*64, model_lock_digest='d'*64,
                    anchor=digest([anchors]*segments), control=digest([channels]*segments), observations=observations,
                    seam=dict(previous_sha256='b'*64, next_sha256='a'*64, verdict='passed'))
    return [dict(segments=n, seed=seed, vary='anchor', baseline=arm(seed, n, 'a'), candidate=arm(seed, n, 'b'))
            for n in (1, 2) for seed in SEEDS]


@pytest.mark.parametrize('fault', [None, 'ignored', 'seam', 'gap', 'control', 'duplicate'])
def test_paired_probe_validates_scope_without_signing_model(fault):
    items = observations()
    if fault == 'ignored':
        items[0]['baseline']['observations'][0]['consumed_digests'].pop()
    if fault == 'control':
        items[0]['candidate']['control'] = 'a'*64
    if fault == 'seam':
        items[-1]['candidate']['seam']['verdict'] = 'rejected'
    if fault == 'gap':
        items[0]['capability_gap'] = {'reason': 'missing'}
    if fault == 'duplicate':
        samples = items[-1]['baseline']['observations']
        samples[1] = copy.deepcopy(samples[0])
    if fault:
        with pytest.raises(ProductionError):
            probe(items)
    else:
        assert probe(items)['status'] == 'unknown'


@pytest.mark.parametrize('fault', [None, 'sha', 'expired', 'rejected', 'short', 'gap'])
def test_operator_review_current_artifact_and_continuous_coverage(tmp_path, fault):
    artifact = tmp_path / 'fixture.bin'
    artifact.write_bytes(b'synthetic identity')
    policy = seal(dict(schema_version='fsf_long_take_review_policy_v1', reviewers=['operator'],
                       plan_digest='a'*64, profile_digest='b'*64, version='review-1', max_age_seconds=10),
                  'policy_digest')
    review = dict(schema_version='fsf_long_take_review_v1', reviewer='operator', policy_digest=policy['policy_digest'],
                  plan_digest=policy['plan_digest'], profile_digest=policy['profile_digest'], version='review-1',
                  coverage=[[0, 2000], [2000, 4320]], frame_count=4320, clock=CLOCKS[0],
                  artifact_sha256=file_identity(artifact)['sha256'], reviewed_at=95, decision='accepted',
                  coverage_kind='continuous_playback', reason='synthetic review record', defects=[])
    if fault == 'sha':
        artifact.write_bytes(b'changed')
    if fault == 'expired':
        review['reviewed_at'] = 1
    if fault == 'gap':
        review['coverage'][1][0] += 1
    if fault == 'short':
        review['frame_count'] = 720
    if fault == 'rejected':
        review['decision'] = 'rejected'
    review = seal(review, 'review_digest')
    if fault:
        with pytest.raises(ProductionError):
            review_take(artifact, review, policy, now=100)
    else:
        assert review_take(artifact, review, policy, now=100)['qualification'] == 'pending_neural_and_machine_evidence'


def test_cli_writes_bound_analytic_receipt(tmp_path):
    *docs, frames = fixture()
    files = []
    for index, document in enumerate(docs + [{'frames': frames, 'core_ranges': [[0, 24]]}]):
        path = tmp_path / f'{index}.json'
        path.write_text(json.dumps(document))
        files.append(str(path))
    output = tmp_path / 'receipt.json'
    result = subprocess.run([sys.executable, '-m', 'fsf.cli', 'production', 'long-take-measure',
                             *files, '--out', str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text())['status'] == 'passed'


@pytest.mark.parametrize('fault', ['short', 'freeze', 'unavailable', None])
def test_technical_master_fails_closed_on_short_or_frozen_evidence(tmp_path, monkeypatch, fault):
    from fsf.production import control
    from fsf.production import long_take
    from fsf.production.long_take import technical_take

    path = tmp_path / 'fixture.bin'
    path.write_bytes(b'controlled decoder fixture')
    monkeypatch.setattr(control, 'inspect_control', lambda *a, **kw: {
        'clock': CLOCKS[0], 'frame_count': 720 if fault == 'short' else 4320, 'width': 1920, 'height': 1080})
    monkeypatch.setattr(long_take, '_freeze_detected', lambda *_: None if fault == 'unavailable' else fault == 'freeze')
    if fault in {'short', 'freeze'}:
        with pytest.raises(ProductionError, match='LT_COVERAGE' if fault == 'short' else 'LT_FROZEN_HOLD'):
            technical_take(path, tmp_path)
    else:
        assert technical_take(path, tmp_path)['status'] == ('unknown' if fault else 'technical_scope_passed')


def test_matrix_does_not_promote_missing_locks_or_short_diagnostic():
    from fsf.production.long_take import whitebox_matrix

    result = whitebox_matrix([], {'development': ['a'*64], 'held_out': []}, [])
    assert result['status'] == 'unknown' and result['required_runs'] == 12
    *docs, frames = fixture(720)
    short = measure(*docs, frames, core_ranges=[[0, 720]])
    with pytest.raises(ProductionError, match='LT_COVERAGE'):
        whitebox_matrix(['b'*64], {'development': ['a'*64], 'held_out': []},
                        [{'model_lock_digest': 'b'*64, 'subject_digest': 'a'*64, 'receipt': short}])


def test_real_ffmpeg_detects_trailing_hold(tmp_path):
    import time
    from fsf.conformance.media import ensure_media_tools_on_path
    from fsf.production.long_take import _freeze_detected

    ffmpeg, _ = ensure_media_tools_on_path()
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    # The tail extends to EOF, where FFmpeg may omit the freeze-end marker.
    path = tmp_path / 'held.mp4'
    subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=blue:s=64x64:r=24', '-t', '1', '-c:v', 'mpeg4', str(path)],
                   check=True, timeout=20)
    assert _freeze_detected(path, time.monotonic() + 20) is True
