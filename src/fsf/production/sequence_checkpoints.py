"""Portable generated-frame checkpoints, independent of a provider's local disk.

Pages and content-addressed 8 MiB chunks avoid the native inline archive limit.
The caller transports the directory/chunks; no cloud store is selected here.
Integrity and continuation binding do not grant semantic or human approval.
"""
from pathlib import Path
import json
import hashlib
import re
import shutil
import tempfile

from fsf.core.asset_library import _root, _no_links, _path_parts, _open_reader
from fsf.core.edit_store import edit_lock, read_json, decode_json, MAX_JSON_BYTES
from fsf.production.artifacts import split_artifact, assemble_artifact, validate_manifest
from fsf.production.contracts import ProductionError, seal, publish, integer
from fsf.production.control import file_identity
from fsf.production.segments import validate_segments
from fsf.remote.contracts import safe_path

PAGE_BYTES = 4 * 1024**2
MAX_BYTES = 64 * 1024**3
MAX_FILES = 300000


def _bad():
    raise ProductionError('PRODUCTION_CHECKPOINT_INVALID')


def _receipt(value, segment, prior, plan):
    fields = {'schema_version', 'plan_digest', 'segment', 'initialization',
              'control_frame_hashes', 'frame_hashes', 'core_identity',
              'motion_review', 'checkpoint_digest'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['schema_version'] != 'fsf_video_segment_checkpoint_v1'
            or seal(value, 'checkpoint_digest') != value
            or value['plan_digest'] != plan['plan_digest'] or value['segment'] != segment
            or value['motion_review'] != 'not_reviewed'):
        _bad()
    initialization = []
    if segment['depends_on']:
        if prior is None or segment['depends_on'] != [prior['segment']['segment_id']]:
            _bad()
        for frame in range(segment['context'][0], segment['core'][0]):
            offset = frame - prior['segment']['context'][0]
            if not 0 <= offset < len(prior['frame_hashes']):_bad()
            initialization.append({'global_frame': frame, 'sha256': prior['frame_hashes'][offset]})
    hashes = value['frame_hashes']
    if (value['initialization'] != initialization or not isinstance(hashes, list)
            or len(hashes) != segment['model_frames']
            or not isinstance(value['control_frame_hashes'], list)
            or len(value['control_frame_hashes']) != segment['context'][1] - segment['context'][0]
            or not isinstance(value['core_identity'], dict)
            or set(value['core_identity']) != {'sha256','size_bytes'}):
        _bad()
    integer(value['core_identity']['size_bytes'], 1, MAX_BYTES)
    for item in [*hashes, *value['control_frame_hashes'], value['core_identity']['sha256']]:
        if not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item):_bad()
    return value


def _prefix(folder, plan):
    """Validate the accepted prefix, including predecessor pixel identities."""
    result = []
    gap = False
    for segment in plan['segments']:
        target = folder / segment['segment_id']
        if not target.exists():
            gap = True
            continue
        _root(target)
        if gap or not (target / 'segment.json').is_file():
            _bad()
        value = _receipt(read_json(target / 'segment.json'), segment, result[-1] if result else None, plan)
        hashes = value['frame_hashes']
        expected = {'segment.json', 'core.mp4', *(f'frames/{i:06d}.png' for i in range(len(hashes)))}
        actual = set()
        for item in target.rglob('*'):
            _no_links(item)
            if not item.is_file():
                if not item.is_dir():
                    _bad()
                continue
            relative = item.relative_to(target).as_posix()
            # Assembly creates stable locks; they carry no checkpoint payload.
            if relative.endswith('.lock') and relative[:-5] in expected and item.stat().st_size == 0:
                continue
            actual.add(relative)
        if actual != expected:
            _bad()
        if file_identity(target / 'core.mp4') != value['core_identity']:
            _bad()
        for i, expected_hash in enumerate(hashes):
            if file_identity(target / 'frames' / f'{i:06d}.png')['sha256'] != expected_hash:
                _bad()
        result.append(value)
    return result


def _files(prefix):
    for receipt in prefix:
        sid = receipt['segment']['segment_id']
        yield sid + '/segment.json'
        yield sid + '/core.mp4'
        for i in range(len(receipt['frame_hashes'])):
            yield sid + f'/frames/{i:06d}.png'


def _index(bundle, plan, *, max_bytes, max_files):
    value = read_json(bundle / 'checkpoint.json')
    fields = {'schema_version', 'plan', 'completed', 'pages', 'file_count', 'size_bytes',
              'motion_review', 'semantic_quality', 'checkpoint_digest'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['schema_version'] != 'fsf_portable_sequence_checkpoint_v1'
            or seal(value, 'checkpoint_digest') != value or value['plan'] != plan
            or value['motion_review'] != 'not_reviewed' or value['semantic_quality'] != 'not_evaluated'):
        _bad()
    integer(value['file_count'], 1, max_files)
    integer(value['size_bytes'], 1, max_bytes)
    complete = value['completed']
    if not isinstance(complete, list) or not 1 <= len(complete) <= len(plan['segments']):
        _bad()
    if any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in complete):
        _bad()
    if not isinstance(value['pages'], list) or not 1 <= len(value['pages']) <= max_files:
        _bad()
    count = total = 0
    for i, page in enumerate(value['pages']):
        if (not isinstance(page, dict) or set(page) != {'path', 'sha256', 'size_bytes'}
                or page['path'] != f'pages/{i:06d}.json'):
            _bad()
        target = safe_path(bundle, page['path'])
        if file_identity(target) != {k: page[k] for k in ('sha256', 'size_bytes')}:
            _bad()
        rows = read_json(target)
        if not isinstance(rows, list) or not rows:
            _bad()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'path', 'chunks'}:
                _bad()
            if not isinstance(row['path'], str):
                _bad()
            # Resolve through the same portable path validator used by remote IO.
            _path_parts(row['path'])
            chunk = validate_manifest(row['chunks'])
            count += 1; total += chunk['size_bytes']
            if count > max_files or total > max_bytes:
                _bad()
    if (count, total) != (value['file_count'], value['size_bytes']):
        _bad()
    return value


def _rows(bundle, index):
    for page in index['pages']:
        yield from read_json(safe_path(bundle, page['path']))


def _verify_row(root, row, expected_files, *, capture=False):
    value = row['chunks']
    if capture and value['size_bytes'] > MAX_JSON_BYTES:_bad()
    manifest_name = 'chunks/' + value['manifest_digest'] + '.json'
    expected_files.add(manifest_name)
    if read_json(safe_path(root, manifest_name)) != value:_bad()
    whole = hashlib.sha256(); total = 0; payload = bytearray()
    for chunk in value['chunks']:
        name = 'chunks/' + chunk['sha256']; expected_files.add(name)
        observed = hashlib.sha256(); size = 0
        with _open_reader(safe_path(root, name)) as source:
            while block := source.read(1024**2):
                size += len(block)
                if size > chunk['size_bytes']:_bad()
                observed.update(block); whole.update(block)
                if capture:payload.extend(block)
        if size != chunk['size_bytes'] or observed.hexdigest() != chunk['sha256']:_bad()
        total += size
    if total != value['size_bytes'] or whole.hexdigest() != value['sha256']:_bad()
    return bytes(payload) if capture else None


def export_checkpoint(sequence_root, plan, *, output_root, max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Publish an immutable directory containing pages, chunk manifests and bytes."""
    integer(max_bytes, 1, MAX_BYTES); integer(max_files, 1, MAX_FILES)
    plan = validate_segments(plan)
    source = _root(sequence_root) / plan['plan_digest']
    _root(source)
    root = _root(output_root)
    scratch = Path(tempfile.mkdtemp(prefix='.sequence-export-', dir=root))
    try:
        chunks = scratch / 'chunks'; chunks.mkdir()
        pages = scratch / 'pages'; pages.mkdir()
        page_rows = []; page_size = 2; page_refs = []; count = total = 0
        def flush():
            nonlocal page_rows, page_size
            if page_rows:
                name = f'pages/{len(page_refs):06d}.json'
                publish(scratch / name, page_rows)
                page_refs.append({'path': name, **file_identity(scratch / name)})
                page_rows = []; page_size = 2
        with edit_lock(source / '.sequence'):
            prefix = _prefix(source, plan)
            if not prefix:
                _bad()
            for name in _files(prefix):
                count += 1
                if count > max_files:
                    _bad()
                manifest = split_artifact(safe_path(source, name), chunk_root=chunks)
                total += manifest['size_bytes']
                if total > max_bytes:
                    _bad()
                row = {'path': name, 'chunks': manifest}
                size = len(json.dumps(row, ensure_ascii=True).encode()) + 2
                if page_rows and page_size + size > PAGE_BYTES:
                    flush()
                page_rows.append(row); page_size += size
            flush()
            if _prefix(source, plan) != prefix:
                raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        index = seal({'schema_version': 'fsf_portable_sequence_checkpoint_v1', 'plan': plan,
                      'completed': [p['checkpoint_digest'] for p in prefix], 'pages': page_refs,
                      'file_count': count, 'size_bytes': total, 'motion_review': 'not_reviewed',
                      'semantic_quality': 'not_evaluated'}, 'checkpoint_digest')
        publish(scratch / 'checkpoint.json', index)
        verify_checkpoint(scratch, plan, max_bytes=max_bytes, max_files=max_files)
        target = root / index['checkpoint_digest']
        with edit_lock(root / (index['checkpoint_digest'] + '.publish')):
            if target.exists():
                if verify_checkpoint(target, plan, max_bytes=max_bytes, max_files=max_files) != index:
                    raise ProductionError('PRODUCTION_TARGET_CONFLICT')
            else:
                scratch.rename(target)
        return {'bundle': index['checkpoint_digest'], 'checkpoint_digest': index['checkpoint_digest'],
                'completed_segments': len(prefix), 'file_count': count, 'size_bytes': total}
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def verify_checkpoint(bundle, plan, *, max_bytes=MAX_BYTES, max_files=MAX_FILES, expected_checkpoint_digest=None):
    """Validate portable metadata and every referenced byte, without restoring."""
    integer(max_bytes, 1, MAX_BYTES); integer(max_files, 1, MAX_FILES)
    root = _root(bundle); plan = validate_segments(plan)
    index = _index(root, plan, max_bytes=max_bytes, max_files=max_files)
    if expected_checkpoint_digest is not None and index['checkpoint_digest'] != expected_checkpoint_digest:
        _bad()
    expected = {'checkpoint.json', *(p['path'] for p in index['pages']), 'chunks/.chunks.lock'}
    rows = iter(_rows(root, index)); prior = None
    def take(name):
        row = next(rows, None)
        if row is None or row['path'] != name:_bad()
        return row
    for i, segment in enumerate(plan['segments'][:len(index['completed'])]):
        sid = segment['segment_id']
        row = take(sid + '/segment.json')
        receipt = _receipt(decode_json(_verify_row(root, row, expected, capture=True)), segment, prior, plan)
        if receipt['checkpoint_digest'] != index['completed'][i]:_bad()
        row = take(sid + '/core.mp4')
        if {k:row['chunks'][k] for k in ('sha256','size_bytes')} != receipt['core_identity']:_bad()
        _verify_row(root, row, expected)
        for j, identity in enumerate(receipt['frame_hashes']):
            row = take(sid + f'/frames/{j:06d}.png')
            if row['chunks']['sha256'] != identity:_bad()
            _verify_row(root, row, expected)
        prior = receipt
    if next(rows, None) is not None:_bad()
    actual = set()
    for p in root.rglob('*'):
        _no_links(p)
        if p.is_file():actual.add(p.relative_to(root).as_posix())
        elif not p.is_dir():_bad()
    if actual != expected or (root / 'chunks/.chunks.lock').stat().st_size:
        _bad()
    return index


def restore_checkpoint(bundle, plan, *, output_root, max_bytes=MAX_BYTES, max_files=MAX_FILES, expected_checkpoint_digest=None):
    """Restore to a fresh worker disk; accepted frames are never regenerated."""
    plan = validate_segments(plan); source = _root(bundle); root = _root(output_root)
    index = verify_checkpoint(source, plan, max_bytes=max_bytes, max_files=max_files,
                              expected_checkpoint_digest=expected_checkpoint_digest)
    scratch = Path(tempfile.mkdtemp(prefix='.sequence-restore-', dir=root))
    try:
        seen = set()
        for row in _rows(source, index):
            if row['path'] in seen:
                _bad()
            seen.add(row['path'])
            target = safe_path(scratch, row['path'], exists=False)
            assemble_artifact(row['chunks'], chunk_root=source / 'chunks', output=target)
        prefix = _prefix(scratch, plan)
        if ([r['checkpoint_digest'] for r in prefix] != index['completed']
                or seen != set(_files(prefix))):
            _bad()
        destination = root / plan['plan_digest']
        with edit_lock(root / (plan['plan_digest'] + '.restore')):
            if destination.exists():
                with edit_lock(destination / '.sequence'):
                    current = _prefix(destination, plan)
                    if [r['checkpoint_digest'] for r in current[:len(prefix)]] != index['completed']:
                        raise ProductionError('PRODUCTION_TARGET_CONFLICT')
                reused = True
            else:
                scratch.rename(destination); reused = False
        return {'bundle': plan['plan_digest'], 'checkpoint_digest': index['checkpoint_digest'],
                'completed_segments': len(prefix), 'reused': reused, 'motion_review': 'not_reviewed'}
    finally:
        if scratch.exists():shutil.rmtree(scratch)
