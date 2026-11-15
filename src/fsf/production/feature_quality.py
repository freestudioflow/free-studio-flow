"""R13 quality evidence, exact PCM alignment and serialization limits."""
import base64
from fractions import Fraction
import io
import json
from pathlib import Path
import time

from PIL import Image

from fsf.core.edit_store import decode_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.contracts import ProductionError, digest, integer, seal
from fsf.production.control import file_identity
from fsf.production.timeline import interval

CLOCKS = ({'num': 24, 'den': 1}, {'num': 24000, 'den': 1001})
PCM = 48000
MAX_PAYLOAD = 1024 * 1024


def _check(condition, code='QC_CONTRACT'):
    if not condition:
        raise ProductionError(code)


def _sha(value):
    _check(isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value))


def quality_observation(policy, evidence):
    _check(seal(policy, 'policy_digest') == policy
           and seal(evidence, 'evidence_digest') == evidence
           and evidence['policy_digest'] == policy['policy_digest'], 'QC_BINDING')
    for name in ('request_digest', 'artifact_sha256', 'reference_digest', 'route_digest', 'capability_digest'):
        _sha(evidence[name])
    _check(all(evidence[name] == policy[name] for name in ('reference_digest', 'route_digest', 'capability_digest')),
           'QC_BINDING')
    count = integer(evidence['frame_count'], 1, 172800)
    _check(count == policy['frame_count'], 'QC_FRAME_COUNT')
    expected = {'head': 0, 'midpoint': count // 2, 'tail': count - 1}
    if policy.get('seam_frame') is not None:
        seam = integer(policy['seam_frame'], 1, count - 1)
        expected.update(seam_left=seam - 1, seam_right=seam)
    samples = evidence['samples']
    _check(isinstance(samples, dict) and samples.keys() <= expected.keys())
    for name, sample in samples.items():
        _check(sample['frame'] == expected[name] and sample['verdict'] in {'accept', 'unknown', 'reject'})
        _sha(sample['pixels_sha256'])
    checks = evidence['checks']
    required = {'freeze', 'reference_leak', 'proportion', 'continuity'}
    _check(isinstance(checks, dict) and checks.keys() <= required
           and all(value in {'accept', 'reject', 'unknown'} for value in checks.values()))
    values = [samples.get(name, {}).get('verdict', 'unknown') for name in expected]
    values.extend(checks.get(name, 'unknown') for name in required)
    if evidence.get('source') != 'locked_local_evaluator':
        values.append('unknown')
    verdict = 'reject' if 'reject' in values else 'unknown' if 'unknown' in values else 'accept'
    return seal({'schema_version': 'fsf_qc_observation_v1', 'verdict': verdict, 'policy_digest': policy['policy_digest'],
                 'artifact_sha256': evidence['artifact_sha256'], 'evidence_digest': evidence['evidence_digest'],
                 'scope': 'bound_observations', 'advisory_can_authorize': False}, 'receipt_digest')


def av_alignment(value):
    """Map half-open frame cuts to PCM once, with a separate final decode check."""
    clock = value['clock']
    _check(clock in CLOCKS and all(type(n) is int for n in clock.values()))
    frame_count = integer(value['frame_count'], 1, 172800)
    per_frame = Fraction(PCM * clock['den'], clock['num'])
    _check(per_frame.denominator == 1)
    samples_per_frame = per_frame.numerator
    end = 0
    splices = []
    for cut in value['cuts']:
        start, stop = interval(cut['frames'], frame_count)
        _check(start == end, 'QC_AV_COVERAGE')
        audio_start = integer(cut['pcm_start'])
        decoded = integer(cut['decoded_samples'])
        surplus = decoded - (stop - start) * samples_per_frame
        _check(abs(audio_start - start * samples_per_frame) <= samples_per_frame, 'QC_AV_OFFSET')
        _check(0 <= surplus <= 1024, 'QC_PCM_SURPLUS')
        splices.append(start)
        end = stop
    _check(end == frame_count, 'QC_AV_COVERAGE')
    timeline_end = frame_count * samples_per_frame
    final_start = integer(value['final_pcm_start'])
    final_samples = integer(value['final_decoded_samples'])
    surplus = final_samples - timeline_end
    _check(final_start <= samples_per_frame, 'QC_AV_OFFSET')
    _check(0 <= surplus <= 1024, 'QC_PCM_SURPLUS')
    _check(len(value['final_splices']) == len(splices), 'QC_AV_COVERAGE')
    actual_splices = [integer(actual) for actual in value['final_splices']]
    _check(actual_splices == sorted(set(actual_splices)) and
           all(abs(actual - frame * samples_per_frame) <= samples_per_frame
               for actual, frame in zip(actual_splices, splices)), 'QC_AV_OFFSET')
    for speech in value['dialogue']:
        start, end = interval(speech, frame_count)
        _check(not any(start < frame < end or start * samples_per_frame < actual < end * samples_per_frame
                       for frame, actual in zip(splices[1:], actual_splices[1:])), 'QC_DIALOGUE_CLIPPED')
    declared = [interval(gap, frame_count) for gap in value['intentional_silence']]
    for gap in value['silent_gaps']:
        start, end = map(integer, gap)
        _check(0 <= start < end <= timeline_end, 'QC_AV_COVERAGE')
        _check(end - start <= samples_per_frame or any(
            a * samples_per_frame <= start and end <= b * samples_per_frame for a, b in declared), 'QC_SILENCE')
    return seal({'schema_version': 'fsf_av_alignment_v1', 'status': 'passed', 'scope': 'supplied_measurements',
                 'clock': clock, 'frame_count': frame_count, 'pcm_rate': PCM, 'samples_per_frame': samples_per_frame,
                 'final_sample_surplus': surplus, 'input_digest': digest(value)}, 'receipt_digest')


def verify_pcm_alignment(value, cut_paths, final_path):
    """Re-decode measured PCM sidecars. Their splice coordinates come from the edit."""
    import wave

    def observe(path, expected):
        identity = file_identity(path)
        try:
            with wave.open(str(path), 'rb') as stream:
                _check(stream.getframerate() == PCM and stream.getnframes() == expected, 'QC_PCM_DECODE')
                stride = stream.getsampwidth() * stream.getnchannels()
                observed = 0
                while chunk := stream.readframes(65536):
                    observed += len(chunk)
                _check(observed == stride * expected, 'QC_PCM_DECODE')
        except (OSError, EOFError, wave.Error):
            raise ProductionError('QC_PCM_DECODE') from None
        _check(file_identity(path) == identity, 'POST_SOURCE_CHANGED')
        return identity

    receipt = av_alignment(value)
    _check(len(cut_paths) == len(value['cuts']), 'QC_AV_COVERAGE')
    cuts = [observe(path, cut['decoded_samples']) for path, cut in zip(cut_paths, value['cuts'])]
    final = observe(final_path, value['final_decoded_samples'])
    return seal({**receipt, 'scope': 'decoded_pcm_with_supplied_splice_coordinates',
                 'cut_pcm': cuts, 'final_pcm': final}, 'receipt_digest')


def advisory_payload(paths):
    _check(isinstance(paths, list) and 1 <= len(paths) <= 6, 'QC_PAYLOAD_LIMIT')
    images = []
    for path in paths:
        _check(file_identity(path, limit=16 * 1024 * 1024)['size_bytes'] > 0, 'QC_PAYLOAD_LIMIT')
        with Image.open(path) as source:
            _check(source.width * source.height <= 3840 * 2160 and getattr(source, 'n_frames', 1) == 1,
                   'QC_PAYLOAD_LIMIT')
            source.thumbnail((960, 960))
            with io.BytesIO() as output:
                source.convert('RGB').save(output, format='JPEG', quality=70)
                images.append(base64.b64encode(output.getvalue()).decode('ascii'))
        _check(sum(map(len, images)) <= MAX_PAYLOAD, 'QC_PAYLOAD_LIMIT')
    return {'schema_version': 'fsf_qc_advisory_request_v1', 'images': images}


def advisory_call(paths, argv, *, seconds=120, retries=3):
    """An operator command transports advisory data. Retry only proven non-start."""
    import tempfile
    data = advisory_payload(paths)
    identity = digest(data)
    payload = json.dumps({**data, 'request_digest': identity}, separators=(',', ':')).encode()
    _check(len(payload) <= MAX_PAYLOAD, 'QC_PAYLOAD_LIMIT')
    _check(isinstance(argv, list) and bool(argv) and all(isinstance(arg, str) and arg for arg in argv))
    deadline = time.monotonic() + integer(seconds, 1, 120)
    tries = 0
    for _ in range(integer(retries, 0, 3) + 1):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        tries += 1
        try:
            with tempfile.TemporaryFile() as stream:
                stream.write(payload)
                stream.seek(0)
                response = run_safe_subprocess(argv, stdin=stream, timeout_seconds=left,
                                               output_limit_bytes=65536, inherit_env=False)
            if response.returncode or response.timed_out:
                break
            reply = decode_json(response.stdout.encode('utf-8'))
            if reply.get('request_digest') != identity:
                break
            if reply.get('status') == 'not_started':
                continue
            if reply.get('status') == 'observed':
                return {'status': 'advisory', 'request_digest': identity, 'attempts': tries,
                        'advisory_can_authorize': False}
        except (OSError, RuntimeError, ValueError):
            break
        break
    return {'status': 'unknown', 'request_digest': identity, 'attempts': tries,
            'advisory_can_authorize': False}


def validate_gate(config, plan):
    _check(isinstance(config, dict) and set(config) == {'command', 'files', 'policies'})
    argv = config['command']
    _check(isinstance(argv, list) and bool(argv) and all(isinstance(arg, str) and arg for arg in argv))
    pins = config['files']
    _check(isinstance(pins, dict) and len(argv) >= 2 and argv[0] in pins and argv[1] in pins)
    for path, expected in pins.items():
        _sha(expected)
        target = Path(path).resolve(strict=True) if path == argv[0] else Path(path)
        _check(Path(path).is_absolute() and path in argv and file_identity(target)['sha256'] == expected,
               'QC_EVALUATOR_CHANGED')
    policies = config['policies']
    _check(isinstance(policies, dict) and set(policies) == {task['task_id'] for task in plan['tasks']})
    for task in plan['tasks']:
        policy = policies[task['task_id']]
        _check(seal(policy, 'policy_digest') == policy and policy['frame_count'] == task['generated_frames'])
        for name in ('reference_digest', 'route_digest', 'capability_digest'):
            _sha(policy[name])


def make_gate(config, plan):
    """Adapt a pinned evaluator's technical observations to the existing store gate."""
    import tempfile
    from fsf.production.segment_quality import LocalSegmentGate

    def evaluate(observation, artifact, deadline):
        for path, expected in config['files'].items():
            target = Path(path).resolve(strict=True) if path == config['command'][0] else Path(path)
            _check(file_identity(target)['sha256'] == expected, 'QC_EVALUATOR_CHANGED')
        policy = config['policies'][observation['task_id']]
        with tempfile.TemporaryFile() as stream:
            stream.write(json.dumps({'observation': observation, 'policy': policy, 'artifact': str(artifact)}).encode())
            stream.seek(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            response = run_safe_subprocess(config['command'], stdin=stream, timeout_seconds=min(remaining, 60),
                                           output_limit_bytes=65536, inherit_env=False)
        if not response.ok:
            return {}
        evidence = decode_json(response.stdout.encode('utf-8'))
        _check(evidence['request_digest'] == observation['request_id']
               and evidence['artifact_sha256'] == observation['artifact_sha256'], 'QC_BINDING')
        return {'intra_segment': quality_observation(policy, evidence)['verdict']}

    return LocalSegmentGate(policy_digest=plan['policy_digest'], evaluator_digest=digest(config),
                            required=['intra_segment'], evaluate=evaluate)
