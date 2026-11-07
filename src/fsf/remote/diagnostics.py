"""Bounded, append-only operation evidence; never a submission authority."""
from datetime import datetime, timezone
import re
import time

from fsf.executors.subprocess_safety import sanitize_process_text
from fsf.remote.contracts import RemoteError, seal, write_json
from fsf.core.edit_store import read_json


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, *, env=None, limit=8192):
    # Redact before cutting UTF-8 bytes, including credentials crossing the cut.
    text = sanitize_process_text(str(value), env=env)
    text = re.sub(r'https?://[^\s<>"\']+', '[REDACTED_URL]', text)
    text = re.sub(r'(?:[A-Za-z]:[/\\]|/)(?:[^\s<>"\']*[/\\])+[^\s<>"\']*', '[REDACTED_PATH]', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return text.encode('utf-8')[-limit:].decode('utf-8', errors='ignore')


def safe_code(exc, default='REMOTE_OPERATION_FAILED'):
    code = getattr(exc, 'code', '')
    if isinstance(exc, RemoteError) and re.fullmatch(r'[A-Z][A-Z0-9]{1,31}(?:_[A-Z0-9]{1,32}){1,6}', code):
        return safe_text(code, limit=128)
    return default


def record(directory, *, request_id, phase, started_at, started_clock,
           operation_status, submission_status='not_attempted', job_status='not_known',
           code=None, result=None, env=None, missing_reason='not_a_subprocess'):
    folder = directory / 'operation-reports'
    folder.mkdir(exist_ok=True)
    # Callers own the dispatch/worker lock. Exclusive creation also guards misuse.
    sequence = len(list(folder.glob('*.json'))) + 1
    path = folder / f'{sequence:08d}.json'
    if path.exists():
        raise RemoteError('REMOTE_INTENT_CONFLICT')
    value = {'schema_version': 'rc11_operation_report_v1', 'request_id': request_id,
             'attempt_id': sequence, 'phase': phase, 'sequence': sequence,
             'operation_status': operation_status, 'submission_status': submission_status,
             'job_status': job_status, 'safe_code': code,
             'timing': {'started_at': started_at, 'finished_at': now(),
                        'elapsed_seconds': max(0, time.monotonic() - started_clock),
                        'collection_time': now(), 'returncode': None,
                        'timed_out': False, 'cancelled': operation_status == 'cancelled',
                        'missing_reason': missing_reason},
             'stdout_tail': '', 'stderr_tail': ''}
    if result is not None:
        value['timing'].update(returncode=result.returncode, timed_out=result.timed_out,
                               missing_reason=None)
        value['stdout_tail'] = safe_text(result.stdout, env=env)
        value['stderr_tail'] = safe_text(result.stderr, env=env)
    value = seal(value, 'report_digest')
    write_json(path, value)
    return {'path': 'operation-reports/' + path.name, 'report_digest': value['report_digest']}


def inspect(directory):
    values = []
    for path in sorted((directory / 'operation-reports').glob('*.json')):
        value = read_json(path)
        if seal(value, 'report_digest') != value:
            raise RemoteError('REMOTE_INTEGRITY')
        values.append(value)
    return values
