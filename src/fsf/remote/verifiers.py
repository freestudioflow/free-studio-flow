"""Host-authorized, digest-bound verifier commands, independent of receipt code."""
import math
import os
from pathlib import Path
import tempfile

from fsf.core.edit_store import read_json
from fsf.core.asset_library import _no_links
from fsf.remote.contracts import RemoteError, digest, seal, sha, safe_path, write_json
from fsf.executors.subprocess_safety import run_safe_subprocess


def registry_entry(identifier, expected_digest, *, registry=None):
    source = registry or os.environ.get('FSF_REMOTE_VERIFIERS_FILE')
    if not source:
        raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    path = Path(source)
    _no_links(path)
    value = read_json(path)
    if set(value) != {'schema_version', 'authorized', 'entries'} or value['schema_version'] != 'rc11_verifier_registry_v1' or value['authorized'] is not True:
        raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    entries = value['entries']
    if not isinstance(entries, list) or len(entries) > 64:
        raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    matches = [entry for entry in entries if entry.get('id') == identifier]
    if len(matches) != 1:
        raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    entry = matches[0]
    if (set(entry) != {'id', 'command', 'code_path', 'code_sha256', 'program_sha256', 'timeout_seconds', 'receipt_versions'}
            or digest(entry) != expected_digest or type(entry['timeout_seconds']) is not int
            or not 1 <= entry['timeout_seconds'] <= 120
            or not isinstance(entry['command'], list) or not entry['command']
            or any(not isinstance(a, str) or not a for a in entry['command'])
            or entry['code_path'] not in entry['command']
            or not isinstance(entry['receipt_versions'],list) or not entry['receipt_versions']
            or any(not isinstance(v,str) or not v for v in entry['receipt_versions'])):
        raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    for name, expected in ((entry['code_path'], entry['code_sha256']), (entry['command'][0], entry['program_sha256'])):
        target = Path(name)
        if not target.is_absolute():
            raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
        # A virtualenv interpreter may be a symlink. Pin its resolved bytes;
        # verifier source itself must be a regular, non-linked file.
        _no_links(target.resolve(strict=True) if name==entry['command'][0] else target)
        if sha(target) != expected:
            raise RemoteError('REMOTE_VERIFIER_UNTRUSTED')
    return entry


def verify(outputs, request, visible, *, registry=None):
    spec = request['recipe']['execution_requirements']['model']
    entry = registry_entry(spec['verifier_id'], spec['verifier_digest'], registry=registry)
    value = read_json(safe_path(outputs, spec['receipt_path']))
    fields = {'schema_version', 'request_digest', 'lock_digest', 'capability_digest', 'outputs',
              'devices', 'model_calls', 'started_ns', 'finished_ns', 'runtime_metrics', 'receipt_digest'}
    if (set(value) != fields or value['schema_version'] not in entry['receipt_versions']
            or seal(value, 'receipt_digest') != value or value['request_digest'] != request['request_digest']
            or value['lock_digest'] != spec['lock_digest']):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    from fsf.remote.extensions import verify_capability
    verify_capability(request['recipe'], value['capability_digest'])
    declared = {s['path'] for step in request['recipe']['steps'] for s in step['outputs']} - {spec['receipt_path']}
    if not isinstance(value['outputs'], dict) or set(value['outputs']) != declared:
        raise RemoteError('REMOTE_OUTPUTS')
    for name, expected in value['outputs'].items():
        if sha(safe_path(outputs, name)) != expected:
            raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    render = request['recipe']['execution_requirements']['render']
    devices = value['devices']
    if (not isinstance(devices, list) or any(not isinstance(d, str) for d in devices)
            or len(devices) != render['device_count'] or len(set(devices)) != len(devices)
            or not set(devices) <= {d['uuid'] for d in visible or []}):
        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    if (any(type(value[k]) is not int for k in ('model_calls', 'started_ns', 'finished_ns'))
            or value['model_calls'] < (1 if render['engine'] == 'MODEL' else 0)
            or not 0 < value['started_ns'] < value['finished_ns'] or not isinstance(value['runtime_metrics'], dict)):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    if render['device'] == 'gpu':
        event = value['runtime_metrics'].get('cuda_event_ms')
        peak = value['runtime_metrics'].get('peak_allocated_bytes')
        if (type(event) not in (int, float) or not math.isfinite(event) or event <= 0
                or type(peak) is not int or peak <= 0):
            raise RemoteError('REMOTE_MODEL_EVIDENCE')
    # The host command receives validated data, never an executable path from media.
    with tempfile.TemporaryDirectory(prefix='fsf-verifier-') as temporary:
        root = Path(temporary)
        context = root / 'context.json'
        write_json(context, {'request': request, 'receipt': value, 'outputs_root': str(Path(outputs).resolve())})
        with context.open('rb') as stream:
            result = run_safe_subprocess(entry['command'], stdin=stream, cwd=root,
                timeout_seconds=entry['timeout_seconds'], output_limit_bytes=65536,
                inherit_env=False, env={k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP') if k in os.environ})
        if result.timed_out or result.returncode:
            raise RemoteError('REMOTE_VERIFIER_FAILED')
        from fsf.core.edit_store import decode_json
        try:
            decision = decode_json(result.stdout.encode('utf-8'))
        except Exception:
            raise RemoteError('REMOTE_VERIFIER_FAILED') from None
        if decision != {'accepted': True, 'receipt_digest': value['receipt_digest']}:
            raise RemoteError('REMOTE_VERIFIER_FAILED')
    return value


def revalidate(outputs, request, visible, *, original_result, destination, registry=None):
    if (not isinstance(original_result,dict) or original_result.get('request_digest')!=request['request_digest']
            or seal(original_result,'result_digest')!=original_result):
        raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
    receipt = verify(outputs, request, visible, registry=registry)
    from fsf.remote.diagnostics import now
    value = seal({'schema_version': 'rc11_revalidation_v1', 'original_result_digest': original_result['result_digest'],
                  'request_digest': request['request_digest'], 'receipt_digest': receipt['receipt_digest'],
                  'verifier_digest': request['recipe']['execution_requirements']['model']['verifier_digest'],
                  'policy_digest': request['recipe']['execution_requirements']['model']['verifier_digest'],
                  'artifact_digests': receipt['outputs'], 'new_generation_dispatches': 0,
                  'validated_at':now(),
                  'verification_level': 'observed'}, 'revalidation_digest')
    if Path(destination).exists():
        raise RemoteError('REMOTE_TARGET_EXISTS')
    write_json(destination, value)
    return value
