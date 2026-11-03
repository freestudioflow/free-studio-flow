"""Operator-selected ASR command adapter; expected text is never sent as a prompt."""
from pathlib import Path

from fsf.core.asset_library import _root
from fsf.production.contracts import ProductionError, digest, integer, seal
from fsf.production.speech import recognize_dialogue, frame_to_sample
from fsf.remote.command_adapter import CommandAdapter, validate_settings
from fsf.remote.contracts import reject_inline_credentials


def recognize_with_command(audio, expected, *, config, work_root, seconds=300):
    import time
    integer(seconds,1,5400)
    from fsf.core.editing import _shape
    try:
        _shape(config, 'fsf_asr_command_v1')
    except ValueError:
        raise ProductionError('PRODUCTION_SPEECH_RECOGNIZER_INVALID') from None
    if (not isinstance(config,dict) or set(config) != {'schema_version','authorized','settings','model_lock_digest','recognizer_digest'}
            or config['schema_version'] != 'fsf_asr_command_v1' or type(config['authorized']) is not bool
            or seal(config,'recognizer_digest') != config):
        raise ProductionError('PRODUCTION_SPEECH_RECOGNIZER_INVALID')
    if not config['authorized']:
        raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
    from fsf.production.speech import _hash
    _hash(config['model_lock_digest'])
    reject_inline_credentials(config)
    validate_settings(config['settings'])
    root = _root(work_root)
    adapter = CommandAdapter({'settings':config['settings']},root/'no-store-config.json')

    def recognize(path, declaration, deadline):
        from fsf.production.control import file_identity
        from fsf.production.contracts import publish
        from fsf.core.edit_store import edit_lock, read_json
        identity = file_identity(path)
        events = [{name:item[name] for name in ('event_id','track_id','clip_id')} | {
            'start_sample':frame_to_sample(item['start_frame'],declaration['clock'],rounding='floor'),
            'end_sample':frame_to_sample(item['end_frame'],declaration['clock'],rounding='ceil')}
            for item in declaration['events']]
        # No expected words, hints, or initial_prompt are exposed to the recognizer.
        request = {'audio_sha256':identity['sha256'],'sample_rate':48000,'events':events,
                   'model_lock_digest':config['model_lock_digest'],'recognizer_digest':config['recognizer_digest']}
        request['request_digest'] = digest(request)
        directory = root/request['request_digest']
        directory.mkdir(exist_ok=True)
        binding = directory/'recognizer-binding.json'
        with edit_lock(binding):
            if binding.exists() and read_json(binding) != request:
                raise ProductionError('PRODUCTION_BINDING_CHANGED')
            if not binding.exists():publish(binding,request)
        remaining = deadline-time.monotonic()
        if remaining<=0:
            raise ProductionError('PRODUCTION_SPEECH_DEADLINE')
        adapter.settings = {**adapter.settings,'timeout_seconds':max(1,min(adapter.settings['timeout_seconds'],int(remaining)))}
        return adapter.call('recognize',request,{'audio':str(Path(path).absolute())},directory)

    return recognize_dialogue(audio,expected,recognizer=recognize,
        recognizer_digest=config['recognizer_digest'],seconds=seconds)
