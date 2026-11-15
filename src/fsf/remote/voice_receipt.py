"""Re-decode a locked PCM voice receipt before invoking the operator's verifier."""
import wave

from fsf.remote.artifacts import shape
from fsf.remote.contracts import RemoteError, digest, safe_path, seal, sha


def verify_voice(outputs, request, receipt):
    shape(receipt, 'fsf_voice_receipt_v1')
    recipe = request['recipe']
    requirements = recipe['execution_requirements']
    model = requirements['model']
    lock = model['lock']
    if (requirements['render']['device'] != 'cpu'
            or seal(receipt, 'receipt_digest') != receipt or receipt['request_digest'] != request['request_digest']
            or digest(lock) != model['lock_digest'] or receipt['lock_digest'] != model['lock_digest']
            or receipt['weights_sha256'] != lock['source']['content_sha256']
            or receipt['dtype'] != {c['id']: c['compute_dtype'] for c in lock['components']}):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    from fsf.remote.extensions import verify_capability
    verify_capability(recipe, receipt['capability_digest'])
    wav = receipt['wav']
    sidecars = receipt['sidecars']
    declared = {item['path'] for step in recipe['steps'] for item in step['outputs']} - {model['receipt_path']}
    if (set(sidecars) | {wav['path']} != declared or wav['path'] in sidecars
            or receipt['config_path'] not in sidecars or receipt['tool_lock_path'] not in sidecars
            or receipt['config_path'] == receipt['tool_lock_path']
            or sidecars[receipt['tool_lock_path']] != lock['runtime']['environment_digest']
            or receipt['outputs'] != {**sidecars, wav['path']: wav['sha256']}):
        raise RemoteError('REMOTE_OUTPUTS')
    for name, expected in sidecars.items():
        if sha(safe_path(outputs, name)) != expected:
            raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    path = safe_path(outputs, wav['path'])
    if path.stat().st_size != wav['size_bytes'] or sha(path) != wav['sha256']:
        raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    try:
        with wave.open(str(path), 'rb') as stream:
            channels, width = stream.getnchannels(), stream.getsampwidth()
            if (stream.getframerate(), channels, stream.getnframes()) != (
                    wav['sample_rate'], wav['channels'], wav['sample_count']):
                raise RemoteError('REMOTE_VOICE_CLOCK')
            decoded = 0
            while block := stream.readframes(65536):
                decoded += len(block)
            if decoded != wav['sample_count'] * channels * width:
                raise RemoteError('REMOTE_VOICE_CLOCK')
    except (EOFError, OSError, wave.Error):
        raise RemoteError('REMOTE_VOICE_CLOCK') from None
