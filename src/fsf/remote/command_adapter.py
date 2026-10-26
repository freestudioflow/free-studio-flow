"""Operator-owned adapter processes. This is a trust boundary, not a sandbox."""
import os
from pathlib import Path
import json
import re

from fsf.core.edit_store import read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError, reject_inline_credentials, reject_credential_bytes, sha, write_json


def validate_settings(settings):
    if set(settings) != {'command', 'files', 'cwd', 'timeout_seconds', 'credential_env_names'}:
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    command = settings['command']
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    if not Path(command[0]).is_absolute() or not Path(settings['cwd']).is_absolute():
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    if type(settings['timeout_seconds']) is not int or not 1 <= settings['timeout_seconds'] <= 5400:
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    names = settings['credential_env_names']
    if not isinstance(names, list) or len(names) != len(set(names)) or any(not isinstance(x,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',x) for x in names):
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    files = settings['files']
    if not isinstance(files, list) or not files or any(not isinstance(f,dict) or set(f)!={'path','sha256'} or not isinstance(f['path'],str) or not Path(f['path']).is_absolute() or not isinstance(f['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',f['sha256']) for f in files):
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    if command[0] not in {f['path'] for f in files}:
        raise RemoteError('REMOTE_ADAPTER_CONFIG')
    return settings


class CommandAdapter:
    def __init__(self, profile, store_config):
        self.profile = profile
        self.settings = validate_settings(profile['settings'])
        self.store_config = str(Path(store_config).absolute())

    def call(self, operation, request, locator, directory, remote=None):
        directory = Path(directory)
        if operation == 'submit':
            from fsf.remote.dispatch_hold import refuse_held_submit
            refuse_held_submit(directory)
        for item in self.settings['files']:
            path = Path(item['path'])
            # Virtualenv Python is commonly a symlink on POSIX. Pin the bytes
            # reached by the configured path, rather than banning that layout.
            if not path.is_file() or sha(path) != item['sha256']:
                raise RemoteError('REMOTE_ADAPTER_CHANGED')
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / ('adapter-' + operation + '-request.json')
        target = directory / ('adapter-' + operation + '-response.json')
        target.unlink(missing_ok=True)
        write_json(source, {'schema_version':'fsf_command_adapter_call_v1', 'operation':operation,
            'request':request, 'locator':locator, 'remote':remote, 'store_config':self.store_config})
        env = {k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
        for name in self.settings['credential_env_names']:
            if name not in os.environ:raise RemoteError('REMOTE_ADAPTER_AUTH_MISSING')
            env[name] = os.environ[name]
        result = run_safe_subprocess([*self.settings['command'],str(source),str(target)],
            cwd=self.settings['cwd'], env=env, inherit_env=False,
            timeout_seconds=self.settings['timeout_seconds'], output_limit_bytes=16384)
        if result.timed_out:raise RemoteError('REMOTE_ADAPTER_TIMEOUT')
        if result.returncode or not target.is_file() or target.is_symlink():raise RemoteError('REMOTE_ADAPTER_FAILED')
        secrets=[env[n] for n in self.settings['credential_env_names']]
        try:
            reject_credential_bytes(target,secrets)
        except RemoteError:
            target.unlink(missing_ok=True)
            raise
        try:
            value = read_json(target)
            decoded=json.dumps(value,ensure_ascii=False)
            if any(len(secret)>=4 and secret in decoded for secret in secrets):
                target.unlink(missing_ok=True)
                raise RemoteError('REMOTE_SECRET_OUTPUT')
            reject_inline_credentials(value)
            if set(value)!={'schema_version','request_digest','operation','result'} or value['schema_version']!='fsf_command_adapter_reply_v1' or value['request_digest']!=request['request_digest'] or value['operation']!=operation:
                raise ValueError()
            output = value['result']
            if operation=='status' and output not in ('queued','running','completed','failed','cancelled','unknown'):raise ValueError()
            if operation=='cancel' and output not in ('requested','cancelled','unsupported','unknown'):raise ValueError()
            if operation in ('prepare','submit','fetch') and not isinstance(output,dict):raise ValueError()
            if operation=='reconcile' and output is not None and not isinstance(output,dict):raise ValueError()
            return output
        except RemoteError:
            raise
        except (ValueError,TypeError,KeyError):
            raise RemoteError('REMOTE_ADAPTER_REPLY') from None
