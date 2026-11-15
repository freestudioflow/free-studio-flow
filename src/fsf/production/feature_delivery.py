"""Assemble and re-verify an accepted feature on the exact frame clock."""
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import time

from fsf.conformance.media import ensure_media_tools_on_path
from fsf.core.edit_store import edit_lock, read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.contracts import ProductionError, integer, publish, seal
from fsf.production.control import file_identity
from fsf.production.feature import FRAMES, require
from fsf.production.timeline import interval
from fsf.production.store import ProductionStore
from fsf.production.store_runner import _load


def _tool(argv, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise ProductionError('PRODUCTION_DEADLINE_EXCEEDED')
    result = run_safe_subprocess(argv, timeout_seconds=left, output_limit_bytes=8 * 1024 * 1024)
    if not result.ok:
        raise ProductionError('FEATURE_MEDIA_INVALID')
    return result.stdout


def video_timing(path, ffprobe, *, clock, frames, deadline):
    base = [ffprobe, '-v', 'error', '-protocol_whitelist', 'file']
    info = json.loads(_tool(base + ['-show_streams', '-of', 'json', str(path)], deadline))
    streams = info['streams']
    if len(streams) != 1 or streams[0]['codec_type'] != 'video':
        raise ProductionError('FEATURE_MEDIA_INVALID')
    stream = streams[0]
    rate = Fraction(clock['num'], clock['den'])
    timebase = Fraction(stream['time_base'])
    duration = Fraction(frames, 1) / rate
    if (Fraction(stream['avg_frame_rate']) != rate or int(stream.get('nb_frames', 0)) != frames
            or int(stream.get('duration_ts', -1)) * timebase != duration):
        raise ProductionError('FEATURE_MEDIA_CLOCK')
    raw = _tool(base + ['-select_streams', 'v:0', '-show_frames',
                         '-show_entries', 'frame=best_effort_timestamp', '-of', 'csv=p=0', str(path)], deadline)
    pts = [line.strip().split(',', 1)[0] for line in raw.splitlines() if line.strip()]
    if len(pts) != frames or any(int(value) * timebase != Fraction(index, 1) / rate
                                for index, value in enumerate(pts)):
        raise ProductionError('FEATURE_MEDIA_CLOCK')
    return {'frames': frames, 'clock': clock, 'seconds': [duration.numerator, duration.denominator],
            'resolution': [stream['width'], stream['height']]}


def _accepted(store, execution, feature):
    items = []
    for shot in feature['shots']:
        row = store.task(execution, shot['step_id'])
        if row['status'] != 'machine_accepted' or not row['accepted'] or not row['artifact']:
            raise ProductionError('PRODUCTION_NOT_READY')
        report = _load(row['accepted'])
        path = Path(row['artifact'])
        identity = file_identity(path)
        if (seal(report, 'report_digest') != report or identity['sha256'] != report['artifact_sha256']):
            raise ProductionError('POST_SOURCE_CHANGED')
        items.append((shot['step_id'], path, identity, report['report_digest']))
    return items


def assemble(root, output, *, seconds=300):
    """Publish a technical video-only delivery with an independently decoded clock."""
    from fsf.production.film import _document

    deadline = time.monotonic() + integer(seconds, 1, 5400)
    output = Path(output).absolute()
    if output.suffix.lower() != '.mp4':
        raise ProductionError('FEATURE_MEDIA_INVALID')
    ffmpeg, ffprobe = ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:
        raise ProductionError('PRODUCTION_TOOLS_UNAVAILABLE')
    receipt_path = output.with_suffix('.receipt.json')
    store = ProductionStore(root)
    try:
        execution, spec = _document(store)
        feature = spec.get('feature')
        if feature is None:
            raise ProductionError('FEATURE_PLAN_INVALID')
        with edit_lock(store.root / '.parent-run'), edit_lock(output):
            items = _accepted(store, execution, feature)
            payload = {'schema_version': 'fsf_feature_delivery_v1', 'execution_id': execution,
                       'feature_digest': feature['feature_digest'], 'scope': 'technical_video_only',
                       'quality_qualified': False, 'human_review': 'not_reviewed',
                       'sources': [{'step_id': step, **identity, 'report_digest': report}
                                   for step, _, identity, report in items]}

            def receipt(path):
                media = video_timing(path, ffprobe, clock=feature['clock'], frames=FRAMES, deadline=deadline)
                return seal({**payload, 'media': media, 'artifact': file_identity(path)}, 'receipt_digest')

            pending = output.with_suffix('.pending.json')
            if receipt_path.exists() or output.exists():
                if not output.exists() or not (receipt_path.exists() or pending.exists()):
                    raise ProductionError('PRODUCTION_TARGET_EXISTS')
                value = receipt(output)
                if value != read_json(receipt_path if receipt_path.exists() else pending):
                    raise ProductionError('POST_SOURCE_CHANGED')
                if not receipt_path.exists():
                    publish(receipt_path, value)
                return value
            with tempfile.TemporaryDirectory(prefix='feature-', dir=output.parent) as workspace:
                folder = Path(workspace)
                # Relative names inside the staging directory keep the concat demuxer in safe mode.
                import shutil
                names = []
                for index, (_, source, identity, _) in enumerate(items):
                    name = f'part-{index:05d}.mp4'
                    staged = folder / name
                    shutil.copyfile(source, staged)
                    if file_identity(staged) != identity:
                        raise ProductionError('POST_SOURCE_CHANGED')
                    shot = feature['shots'][index]
                    video_timing(staged, ffprobe, clock=feature['clock'],
                                 frames=shot['range'][1]-shot['range'][0], deadline=deadline)
                    names.append(name)
                playlist = folder / 'parts.txt'
                playlist.write_text(''.join(f"file '{name}'\n" for name in names), encoding='utf-8')
                staged = folder / 'delivery.mp4'
                _tool([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file',
                       '-f', 'concat', '-safe', '1', '-i', str(playlist), '-map', '0:v:0',
                       '-c', 'copy', str(staged)], deadline)
                value = receipt(staged)
                if _accepted(store, execution, feature) != items:
                    raise ProductionError('POST_SOURCE_CHANGED')
                if pending.exists() and read_json(pending) != value:
                    raise ProductionError('POST_SOURCE_CHANGED')
                if not pending.exists():
                    publish(pending, value)
                staged.replace(output)
                publish(receipt_path, value)
                return value
    finally:
        store.close()


def review_feature(root, artifact, review, policy, *, now):
    from fsf.production.film import _document

    code = 'FEATURE_REVIEW_REQUIRED'
    store = ProductionStore(root)
    try:
        execution, spec = _document(store)
        feature = spec.get('feature')
        path = Path(artifact).absolute()
        receipt = read_json(path.with_suffix('.receipt.json'))
        require(feature is not None and seal(receipt, 'receipt_digest') == receipt
                and receipt['execution_id'] == execution and receipt['feature_digest'] == feature['feature_digest']
                and receipt['artifact'] == file_identity(path), 'POST_SOURCE_CHANGED')
        require(seal(review, 'review_digest') == review and review.get('schema_version') == 'fsf_feature_review_v1'
                and seal(policy, 'policy_digest') == policy and policy.get('schema_version') == 'fsf_feature_review_policy_v1'
                and review['policy_digest'] == policy['policy_digest']
                and review['reviewer'] in policy['reviewers'] and review['version'] == policy['version']
                and review['plan_digest'] == spec['plan']['plan_digest']
                and review['feature_digest'] == feature['feature_digest']
                and review['delivery_digest'] == receipt['receipt_digest']
                and review['artifact_sha256'] == receipt['artifact']['sha256']
                and review['clock'] == feature['clock'], code)
        require(type(review['reviewed_at']) is int and type(policy['max_age_seconds']) is int
                and 0 <= now - review['reviewed_at'] <= policy['max_age_seconds']
                and review['decision'] == 'accepted' and review['coverage_kind'] == 'continuous_playback'
                and isinstance(review['reason'], str) and bool(review['reason'].strip())
                and isinstance(review['defects'], list) and not review['defects'], code)
        end = 0
        for bounds in review['coverage']:
            start, stop = interval(bounds, FRAMES)
            require(start == end, code)
            end = stop
        require(end == FRAMES, code)
        expected = {}
        for chapter in feature['chapters']:
            a, b = chapter['range']
            expected[chapter['chapter_id']] = {'head': a, 'midpoint': (a+b)//2, 'tail': b-1}
        require(review['chapter_markers'] == expected
                and review['transitions'] == [chapter['range'][0] for chapter in feature['chapters'][1:]], code)
        from fsf.production.feature_quality import av_alignment
        require(review['av_measurements']['clock'] == feature['clock']
                and review['av_measurements']['frame_count'] == FRAMES, code)
        audio = av_alignment(review['av_measurements'])
        return {'status': 'review_record_current', 'review_digest': review['review_digest'],
                'av_receipt_digest': audio['receipt_digest'], 'qualification': 'pending_actual_av_and_quality_evidence'}
    finally:
        store.close()
