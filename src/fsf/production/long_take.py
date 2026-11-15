"""Local analytic whitebox measurements and explicit long-take evidence gates."""
from collections import Counter
from fractions import Fraction
import math
from pathlib import Path
import time

from fsf.production.contracts import ProductionError, digest, integer, key, seal
from fsf.production.control import file_identity
from fsf.production.timeline import position, interval

CLOCKS = ({'num': 24, 'den': 1}, {'num': 24000, 'den': 1001})
SEEDS = (17, 23, 31)


def _require(condition, code='LT_CONTRACT'):
    if not condition:
        raise ProductionError(code)


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value))
    return value


def _sub(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _mix(left, right, t):
    return tuple(a + t * (b - a) for a, b in zip(left, right))


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _swept(left, right):
    delta = _sub(right, left)
    squared = _dot(delta, delta)
    t = min(1, max(0, -_dot(left, delta) / squared)) if squared else 0
    return math.dist(_mix(left, right, t), (0, 0, 0))


def _angle(left, right):
    return (right - left + 180) % 360 - 180


def _coverage(ranges, count):
    end = 0
    for bounds in ranges:
        start, stop = interval(bounds, count)
        _require(start == end, 'LT_COVERAGE')
        end = stop
    _require(end == count, 'LT_COVERAGE')


def _sealed(value, version, field):
    _require(isinstance(value, dict) and value.get('schema_version') == version and seal(value, field) == value)


def _fields(value, required, optional=()):
    fields = set(required.split())
    _require(isinstance(value, dict) and fields <= value.keys() <= fields.union(optional))


def _visibility(camera, yaw, subject, poses, radii):
    """Projected sphere bounds and five camera rays for this analytic proxy profile."""
    x, y, z = _sub(poses[subject], camera)
    angle = math.radians(yaw)
    forward = (math.cos(angle), math.sin(angle), 0)
    right = (-forward[1], forward[0], 0)
    depth = _dot((x, y, z), forward)
    radius = radii[subject]
    if depth <= radius:
        return 0, 0, True
    # Fixed vertical FOV 60 degrees and 16:9 in this local profile.
    half = math.tan(math.radians(30)) * depth
    side = _dot((x, y, z), right)
    outside = abs(z) + radius > half or abs(side) + radius > half * 16 / 9
    visible = 0
    for dx, dz in ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius)):
        point = tuple(a + dx * b for a, b in zip(poses[subject], right))
        point = (point[0], point[1], point[2] + dz)
        ray = _sub(point, camera)
        occluded = False
        for name, center in poses.items():
            if name == subject:
                continue
            relative = _sub(center, camera)
            fraction = _dot(relative, ray) / _dot(ray, ray)
            if 0 < fraction < 1 and math.dist(relative, _mix((0, 0, 0), ray, fraction)) < radii[name]:
                occluded = True
                break
        visible += not occluded
    return radius / half, visible / 5, outside


def measure(scene, action, camera, frames, *, core_ranges):
    _sealed(scene, 'scene_spec_v1', 'scene_digest')
    _sealed(action, 'action_graph_v1', 'action_digest')
    _sealed(camera, 'camera_spec_v1', 'camera_digest')
    _fields(scene, 'schema_version units proxy reference_m measured_m sphere_radii_m scene_digest')
    _fields(action, 'schema_version scene_digest logical_shot_id initial_holder transfers contacts action_digest',
            ('support', 'speed'))
    _fields(camera, 'schema_version scene_digest action_digest logical_shot_id camera_id clock range resolution targets camera_digest',
            ('seams',))
    _require(action['scene_digest'] == scene['scene_digest'] == camera['scene_digest']
             and camera['action_digest'] == action['action_digest']
             and action['logical_shot_id'] == camera['logical_shot_id'], 'LT_BINDING')
    key(action['logical_shot_id'])
    clock = camera['clock']
    _require(clock in CLOCKS and all(type(value) is int for value in clock.values()))
    count = len(frames)
    _require(1 <= count <= 4320)
    _coverage(core_ranges, count)
    _require(camera['range'] == [0, count] and camera['resolution'] == [1920, 1080])
    expected_scale, measured_scale = map(_number, (scene['reference_m'], scene['measured_m']))
    _require(expected_scale > 0 and measured_scale > 0 and scene['units'] == 'm')
    radii = {key(name): _number(radius) for name, radius in scene['sphere_radii_m'].items()}
    unknown = Counter()
    defects = Counter()
    first = {}
    if not radii or any(radius <= 0 for radius in radii.values()) or scene['proxy'] != 'analytic_sphere_v1':
        unknown['unsupported_proxy'] += 1
    if abs(measured_scale / expected_scale - 1) > .01 + 1e-12:
        defects['scale'] += 1

    def flag(code, frame, conditions=True, *, missing=False):
        if conditions:
            (unknown if missing else defects)[code] += 1
            first.setdefault(code, frame)

    transfers = {}
    for event in action['transfers']:
        _fields(event, 'frame from to')
        index = event['frame']
        _require(type(index) is int and 0 <= index < count and index not in transfers)
        _require(event['from'] in radii and event['to'] in radii and event['from'] != event['to'])
        transfers[index] = event
    bindings = {}
    for event in action['contacts']:
        _fields(event, 'range pair')
        start, end = interval(event['range'], count)
        pair = tuple(event['pair'])
        _require(len(pair) == 2 and len(set(pair)) == 2 and set(pair) <= radii.keys())
        for index in range(start, end):
            bindings.setdefault(index, []).append(pair)
    for index in transfers:
        _require(any(set(pair) == {transfers[index]['from'], transfers[index]['to']}
                     for pair in bindings.get(index, ())), 'LT_CONTACT_UNBOUND')
    holder = action['initial_holder']
    _require(holder in radii or holder is None)
    targets = camera['targets']
    _require(isinstance(targets, list) and len(targets) == len(set(targets)) and targets and set(targets) <= radii.keys())
    previous = velocity = angular_velocity = None
    dt = clock['den'] / clock['num']
    subframes = swept_checks = 0
    visible_contacts = total_contacts = 0
    support = [(key(item['subject']), interval(item['range'], count)) for item in action.get('support', [])]
    speed = [(key(item['subject']), interval(item['range'], count), _number(item['speed_m_s']))
             for item in action.get('speed', [])]
    _require(all(subject in radii for subject, _ in support)
             and all(subject in radii and expected > 0 for subject, _, expected in speed))
    support_origins = {}
    boundaries = {start for start, _ in core_ranges if start}
    seams = camera.get('seams', {})
    for index, frame in enumerate(frames):
        _fields(frame, 'index camera_id camera yaw_deg positions holder')
        _require(type(frame['index']) is int and frame['index'] == index
                 and frame['camera_id'] == camera['camera_id'], 'LT_COVERAGE')
        poses = {name: position(value) for name, value in frame['positions'].items()}
        _require(poses.keys() <= radii.keys())
        location, yaw = position(frame['camera']), _number(frame['yaw_deg'])
        if index in boundaries:
            seam = seams.get(str(index))
            if seam is None:
                flag('camera_seam_unobserved', index, missing=True)
            else:
                flag('camera_seam', index, math.dist(position(seam['position']), location) > .001 + 1e-12
                     or abs(_angle(_number(seam['yaw_deg']), yaw)) > .1 + 1e-12)
        if index in transfers:
            event = transfers[index]
            flag('ownership', index, holder != event['from'])
            holder = event['to']
        flag('ownership', index, frame['holder'] != holder)
        flag('missing_proxy_frame', index, poses.keys() != radii.keys(), missing=True)
        for event, (subject, (start, stop)) in enumerate(support):
            if not start <= index < stop:
                continue
            if subject not in poses:
                flag('support_unobserved', index, missing=True)
                continue
            original = support_origins.setdefault(event, poses[subject])
            flag('foot_slip', index, math.dist(original, poses[subject]) > .03 + 1e-12)
        if not unknown['unsupported_proxy']:
            for name, center in poses.items():
                flag('camera_clearance', index, math.dist(center, location) - radii[name] < .05 - 1e-12)
            for name in targets:
                if name in poses:
                    height, visible, outside = _visibility(location, yaw, name, poses, radii)
                    flag('framing', index, outside or not .2 <= height <= .85 or visible < .75)
            for a, b in bindings.get(index, ()):
                total_contacts += 1
                if a not in poses or b not in poses:
                    flag('contact_unobserved', index, missing=True)
                    continue
                distance = math.dist(poses[a], poses[b]) - radii[a] - radii[b]
                flag('contact', index, distance > .02 + 1e-12)
                flag('penetration', index, distance < -.01 - 1e-12)
                visible_contacts += all(_visibility(location, yaw, name, poses, radii)[1] >= .75
                                        and not _visibility(location, yaw, name, poses, radii)[2] for name in (a, b))
        if previous is not None:
            old, prior_location, prior_yaw = previous
            current_velocity = tuple(x / dt for x in _sub(location, prior_location))
            current_angular = _angle(prior_yaw, yaw) / dt
            flag('angular_speed', index, abs(current_angular) > 90 + 1e-9)
            if velocity is not None:
                flag('translation_acceleration', index, math.dist(velocity, current_velocity) / dt > 3 + 1e-9)
                flag('angular_acceleration', index, abs(current_angular - angular_velocity) / dt > 360 + 1e-9)
            velocity, angular_velocity = current_velocity, current_angular
            for subject, (start, stop), expected in speed:
                if start < index < stop:
                    if subject in poses and subject in old:
                        measured = math.dist(old[subject], poses[subject]) / dt
                        flag('action_speed', index, abs(measured / expected - 1) > .15 + 1e-12)
                    else:
                        flag('speed_unobserved', index, missing=True)
            if not unknown['unsupported_proxy']:
                for name in poses.keys() & old.keys():
                    relative0 = _sub(old[name], prior_location)
                    relative1 = _sub(poses[name], location)
                    swept_checks += 1
                    flag('camera_swept', index, _swept(relative0, relative1) - radii[name] < .05 - 1e-12)
                pairs = set(bindings.get(index - 1, ())) | set(bindings.get(index, ()))
                for a, b in pairs:
                    if not {a, b} <= poses.keys() & old.keys():
                        flag('swept_unobserved', index, missing=True)
                        continue
                    left, right = _sub(old[a], old[b]), _sub(poses[a], poses[b])
                    if math.dist(left, right) > .01 + 1e-12:
                        radius = radii[a] + radii[b]
                        for step in range(1, 5):
                            point = _mix(left, right, step / 5)
                            flag('subframe_penetration', index, math.dist(point, (0, 0, 0)) - radius < -.01 - 1e-12)
                            subframes += 1
                        flag('swept_penetration', index, _swept(left, right) - radius < -.01 - 1e-12)
                        swept_checks += 1
        previous = poses, location, yaw
    flag('contact_visibility', 0, total_contacts > 0 and not unknown['contact_unobserved']
         and visible_contacts / total_contacts < .9)
    seconds = Fraction(count * clock['den'], clock['num'])
    return seal({'schema_version': 'compile_receipt_v1', 'logical_shot_id': action['logical_shot_id'],
                 'scene_digest': scene['scene_digest'], 'action_digest': action['action_digest'],
                 'camera_digest': camera['camera_digest'], 'observation_digest': digest(frames),
                 'core_ranges': core_ranges, 'clock': clock, 'resolution': camera['resolution'],
                 'frame_count': count, 'seconds': [seconds.numerator, seconds.denominator],
                 'method': 'analytic_sphere_v1', 'scope': 'local_analytic_fixture',
                 'status': 'rejected' if defects else 'unknown' if unknown else 'passed',
                 'defects': dict(defects), 'unknown': dict(unknown), 'first_frame': first,
                 'subframes': subframes, 'swept_checks': swept_checks,
                 'contact_frames': total_contacts, 'visible_contact_frames': visible_contacts,
                 'cross_model_qualified': False, 'neural_take_qualified': False}, 'compile_receipt_digest')

def probe(pairs):
    """Validate the local test contract. Model calibration remains separate."""
    _require(isinstance(pairs, list) and len(pairs) == 6)
    seen = set()
    gaps = []
    for pair in pairs:
        segments, seed = pair['segments'], pair['seed']
        _require(type(segments) is int and type(seed) is int and segments in (1, 2) and seed in SEEDS)
        _require((segments, seed) not in seen)
        seen.add((segments, seed))
        if pair.get('capability_gap') is not None:
            gap = pair['capability_gap']
            keys = {'model_lock_digest', 'subject_digest', 'phase', 'capability', 'affected_range',
                    'reason', 'evidence_digest', 'status'}
            _require(isinstance(gap, dict) and set(gap) == keys and gap['status'] == 'unknown')
            for field in ('model_lock_digest', 'subject_digest', 'evidence_digest'):
                _sha(gap[field])
            _require(all(isinstance(gap[k], str) and gap[k] for k in ('phase', 'capability', 'reason')))
            interval(gap['affected_range'], 4320)
            gaps.append(gap)
            continue
        baseline, candidate = pair['baseline'], pair['candidate']
        _require(baseline['seed'] == candidate['seed'] == seed
                 and baseline['parameters_digest'] == candidate['parameters_digest']
                 and baseline['model_lock_digest'] == candidate['model_lock_digest'], 'LT_AB_BINDING')
        _sha(baseline['parameters_digest'])
        _sha(baseline['model_lock_digest'])
        variant = pair['vary']
        _require(variant in {'anchor', 'control'})
        for field in ('anchor', 'control'):
            _sha(baseline[field])
            _sha(candidate[field])
            _require((baseline[field] != candidate[field]) == (field == variant), 'LT_AB_BINDING')
        for arm in (baseline, candidate):
            _require(len(arm['observations']) == segments, 'LT_COVERAGE')
            anchor_bindings, control_bindings, spans = [], [], []
            prior_segment_id = None
            for observation in arm['observations']:
                request = observation['request']
                _require(seal(request, 'request_digest') == request
                         and request['schema_version'] == 'rc11_generator_v2'
                         and observation['request_digest'] == request['request_digest']
                         and request['lock_digest'] == arm['model_lock_digest']
                         and type(request['tail_padding']) is int and request['tail_padding'] >= 0, 'LT_AB_BINDING')
                segment_id = key(request['segment_id'])
                _require(segment_id != prior_segment_id, 'LT_COVERAGE')
                prior_segment_id = segment_id
                spans.append(request['core_range'])
                if len(spans) > 1:
                    _require(request['logical_shot_id'] == arm['observations'][0]['request']['logical_shot_id']
                             and request['core_range'][0] == spans[-2][1], 'LT_COVERAGE')
                interval(request['core_range'], 4320)
                anchors = request['anchors']
                channels = request['bundle_window']['channels']
                _require(anchors and channels, 'LT_CONTROL_UNVERIFIED')
                anchor_bindings.append(anchors)
                control_bindings.append(channels)
                expected = {digest(item) for item in request['inputs'] + anchors + channels}
                _require(set(observation['consumed_digests']) == expected, 'LT_CONTROL_UNVERIFIED')
                for field in ('first_frame_sha256', 'last_frame_sha256'):
                    _sha(observation[field])
            _require(digest(anchor_bindings) == arm['anchor'] and digest(control_bindings) == arm['control'],
                     'LT_CONTROL_UNVERIFIED')
            if segments == 2:
                seam = arm['seam']
                _require(seam['previous_sha256'] == arm['observations'][0]['last_frame_sha256']
                         and seam['next_sha256'] == arm['observations'][1]['first_frame_sha256']
                         and seam['verdict'] in {'passed', 'unknown', 'rejected'}, 'LT_SEAM')
                _require(seam['verdict'] != 'rejected', 'LT_SEAM')
        def comparison(arm):
            return [(o['request']['logical_shot_id'], o['request']['segment_id'],
                     o['request']['core_range'], o['request']['clock']) for o in arm['observations']]
        _require(comparison(baseline) == comparison(candidate), 'LT_AB_BINDING')
    return {'status': 'unknown', 'paired_contracts_checked': len(seen),
            'capability_gaps': gaps, 'qualification': 'pending_model_calibration'}


def _sha(value):
    _require(isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value))


def review_take(artifact, review, policy, *, now):
    """Verify an operator-identified review against the currently selected file and policy."""
    _sealed(review, 'fsf_long_take_review_v1', 'review_digest')
    _sealed(policy, 'fsf_long_take_review_policy_v1', 'policy_digest')
    _require(review['reviewer'] in policy['reviewers']
             and review['policy_digest'] == policy['policy_digest']
             and review['plan_digest'] == policy['plan_digest']
             and review['profile_digest'] == policy['profile_digest']
             and review['version'] == policy['version'], 'LT_REVIEW_BINDING')
    _require(review['clock'] == CLOCKS[0] and all(type(v) is int for v in review['clock'].values())
             and type(review['frame_count']) is int and review['frame_count'] == 4320, 'LT_COVERAGE')
    _coverage(review['coverage'], 4320)
    _require(type(now) is int and type(review['reviewed_at']) is int and type(policy['max_age_seconds']) is int
             and 0 <= now - review['reviewed_at'] <= policy['max_age_seconds'], 'LT_REVIEW_EXPIRED')
    _require(file_identity(Path(artifact))['sha256'] == review['artifact_sha256'], 'LT_REVIEW_BINDING')
    _require(review['decision'] == 'accepted' and review['coverage_kind'] == 'continuous_playback'
             and isinstance(review['reason'], str) and review['reason'].strip(), 'LT_REVIEW_REQUIRED')
    _require(isinstance(review['defects'], list) and not review['defects'], 'LT_REVIEW_REQUIRED')
    return {'status': 'review_record_current', 'review_digest': review['review_digest'],
            'qualification': 'pending_neural_and_machine_evidence'}


def technical_take(artifact, work, *, seconds=300):
    """Check actual video-only master bytes, clock and long frozen holds."""
    from tempfile import TemporaryDirectory
    from fsf.core.asset_library import _root
    from fsf.production.control import inspect_control

    path = Path(artifact).absolute()
    deadline = time.monotonic() + integer(seconds, 1, 5400)
    identity = file_identity(path)
    with TemporaryDirectory(prefix='long-take-', dir=_root(work)) as temporary:
        remaining = math.floor(deadline - time.monotonic())
        _require(remaining >= 1, 'PRODUCTION_DEADLINE_EXCEEDED')
        metadata = inspect_control(path, Path(temporary), timeout=remaining)
    _require(metadata['clock'] == CLOCKS[0] and metadata['frame_count'] == 4320
             and [metadata['width'], metadata['height']] == [1920, 1080], 'LT_COVERAGE')
    frozen = _freeze_detected(path, deadline)
    _require(file_identity(path) == identity, 'LT_ARTIFACT_CHANGED')
    if frozen is None:
        return {'status': 'unknown', 'reason': 'freeze_measurement_unavailable', 'identity': identity}
    _require(not frozen, 'LT_FROZEN_HOLD')
    return {'status': 'technical_scope_passed', 'identity': identity, 'media': metadata,
            'qualification': 'pending_neural_evidence'}


def _freeze_detected(path, deadline):
    from fsf.conformance.media import ensure_media_tools_on_path
    from fsf.executors.subprocess_safety import run_safe_subprocess

    ffmpeg, _ = ensure_media_tools_on_path()
    remaining = deadline - time.monotonic()
    if not ffmpeg or remaining <= 0:
        return None
    result = run_safe_subprocess([ffmpeg, '-nostdin', '-nostats', '-protocol_whitelist', 'file', '-i', str(path),
                                  '-vf', 'freezedetect=n=0.003:d=0.5', '-an', '-f', 'null', '-'],
                                 timeout_seconds=remaining, output_limit_bytes=512 * 1024)
    if result.returncode or result.timed_out:
        return None
    return 'freeze_start:' in result.stderr

def whitebox_matrix(model_locks, subjects, runs):
    """Check a predeclared 2-by-6 matrix without elevating synthetic records."""
    _require(isinstance(model_locks, list) and len(model_locks) <= 2)
    for lock in model_locks:
        _sha(lock)
    _require(len(set(model_locks)) == len(model_locks))
    _require(isinstance(subjects, dict) and set(subjects) == {'development', 'held_out'})
    all_subjects = []
    for group in subjects.values():
        _require(isinstance(group, list) and len(group) <= 3)
        for subject in group:
            _sha(subject)
        all_subjects.extend(group)
    _require(len(all_subjects) == len(set(all_subjects)))
    expected = {(lock, subject) for lock in model_locks for subject in all_subjects}
    seen = set()
    for run in runs:
        _fields(run, 'model_lock_digest subject_digest receipt')
        pair = run['model_lock_digest'], run['subject_digest']
        _require(pair in expected and pair not in seen, 'LT_COVERAGE')
        seen.add(pair)
        receipt = run['receipt']
        _sealed(receipt, 'compile_receipt_v1', 'compile_receipt_digest')
        _require(receipt['frame_count'] == 4320 and receipt['clock'] == CLOCKS[0]
                 and receipt['resolution'] == [1920, 1080], 'LT_COVERAGE')
        _coverage(receipt['core_ranges'], 4320)
    return {'status': 'unknown', 'qualification': 'pending_cross_model_evidence',
            'locks_declared': len(model_locks), 'held_out_declared': len(subjects['held_out']),
            'required_runs': 12, 'matrix_cells_present': len(seen)}
