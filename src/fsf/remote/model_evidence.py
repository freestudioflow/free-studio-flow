"""Typed neural GPU evidence, distinct from Cycles render or semantic approval."""
import math

from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,digest,seal,sha,safe_path


def begin_cuda_measurement(torch,device=0):
    """Initialize the allocator before resetting its counters on lazy/offloaded models.

    A warm-up allocation is not evidence of model execution. Positive model
    calls, CUDA events, immutable weights and output bytes remain mandatory.
    """
    if type(device) is not int or not 0<=device<16 or not torch.cuda.is_available():
        raise RemoteError('REMOTE_GPU_UNAVAILABLE')
    warmup=torch.empty(1,dtype=torch.float32,device=f'cuda:{device}')
    del warmup
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)


def validate_model_lock(requirements):
    model=requirements['model']
    if digest(model['lock']) != model['lock_digest']:
        raise RemoteError('REMOTE_MODEL_INTEGRITY')
    from fsf.core.asset_library import _path_parts
    for name in model['lock']['files']:
        _path_parts(name)
    _path_parts(model['receipt_path'])


def verify_model_receipt(outputs,request,visible):
    requirements=request['recipe']['execution_requirements']
    validate_model_lock(requirements)
    spec=requirements['model']
    value=read_json(safe_path(outputs,spec['receipt_path']))
    if not isinstance(value,dict):raise RemoteError('REMOTE_MODEL_EVIDENCE')
    observed=value.get('schema_version')=='fsf_model_execution_receipt_v2'
    fields={'schema_version','request_digest','lock_digest','observed_files','devices','precision',
            'model_calls','cuda_event_ms','started_ns','finished_ns','peak_allocated_bytes','outputs','receipt_digest'}
    if observed:
        fields=(fields-{'cuda_event_ms'})|{'runtime','runtime_metrics','verification_level'}
        if (request.get('execution_mode')!='experimental' or value.get('verification_level')!='observed'
                or not isinstance(value.get('runtime'),str) or not value['runtime']
                or not isinstance(value.get('runtime_metrics'),dict)):
            raise RemoteError('REMOTE_MODEL_EVIDENCE')
    if (not isinstance(value,dict) or set(value) != fields or value['schema_version'] not in {'fsf_model_execution_receipt_v1','fsf_model_execution_receipt_v2'}
            or seal(value,'receipt_digest')!=value or value['request_digest']!=request['request_digest']
            or value['lock_digest']!=spec['lock_digest'] or value['observed_files']!=spec['lock']['files']
            or value['precision']!=spec['lock']['precision']):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    devices=value['devices']
    if (not isinstance(devices,list) or len(devices)!=requirements['render']['device_count']
            or len(set(devices))!=len(devices) or not set(devices)<={d['uuid'] for d in visible}):
        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    if (any(type(value[k]) is not int for k in ('model_calls','started_ns','finished_ns','peak_allocated_bytes'))
            or value['model_calls']<1 or not 0<value['started_ns']<value['finished_ns']
            or value['peak_allocated_bytes']<1):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    if not observed and (isinstance(value['cuda_event_ms'],bool) or not isinstance(value['cuda_event_ms'],(int,float))
            or not math.isfinite(value['cuda_event_ms']) or value['cuda_event_ms']<=0):
        raise RemoteError('REMOTE_MODEL_EVIDENCE')
    declared={s['path'] for step in request['recipe']['steps'] for s in step['outputs']}
    if spec['receipt_path'] not in declared or set(value['outputs'])!=declared-{spec['receipt_path']}:
        raise RemoteError('REMOTE_OUTPUTS')
    for name,expected in value['outputs'].items():
        if sha(safe_path(outputs,name))!=expected:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    return value
