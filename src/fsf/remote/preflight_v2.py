"""V2 read-only readiness: static authority, store versions, host observations."""
import json
from pathlib import Path
import shutil
import tempfile
from fsf.remote.contracts import RemoteError
from fsf.remote.v2 import check_authorization,wheel_identity


_CODES={'REMOTE_NOT_AUTHORIZED','REMOTE_GPU_NOT_AUTHORIZED','REMOTE_GPU_UNAVAILABLE','REMOTE_GPU_DEVICE_MISMATCH',
        'REMOTE_STORE_FORBIDDEN','REMOTE_STORE_AUTH_REQUIRED','REMOTE_STORE_UNAVAILABLE','REMOTE_ARTIFACT_MISSING',
        'REMOTE_ARTIFACT_VERSION_REQUIRED','REMOTE_ARTIFACT_INTEGRITY','REMOTE_STALE_WHEEL','REMOTE_WORKER_VERSION',
        'REMOTE_QC_CONTRACT_REQUIRED','REMOTE_QC_CONTRACT_INVALID','REMOTE_CONTRACT_DIGEST','REMOTE_SCHEMA',
        'REMOTE_PYTHON_VERSION','REMOTE_ENVIRONMENT_MISMATCH','REMOTE_RUNTIME_UNAVAILABLE','REMOTE_PARAMETERS',
        'REMOTE_INTEGRITY','REMOTE_ARTIFACT_SCHEMA','REMOTE_WHEEL'}


def preflight(request,*,profile,store,store_config,probe=False):
    checks=[]
    def add(phase,name,status,code):checks.append({'phase':phase,'check':name,'status':status,'code':code})
    phase,name='static','declarations'
    try:
        check_authorization(request,profile,store)
        add(phase,name,'passed','REMOTE_STATIC_VALID')
        if not probe:
            add('static','files','unverified','REMOTE_FILES_UNVERIFIED')
            add('provider','connection','unverified','REMOTE_PROVIDER_UNVERIFIED')
            add('runtime','environment','unverified','REMOTE_CAPABILITY_UNVERIFIED')
        else:
            phase,name='provider','connection'
            store.probe()
            from fsf.remote.v2_transports import V2Adapter
            with tempfile.TemporaryDirectory(prefix='fsf-preflight-v2-') as temporary:
                root=Path(temporary);state=root/'transfer';state.mkdir()
                wheel=root/'worker.whl'
                phase,name='static','files'
                store.get(request['worker_wheel'],output=wheel,state_root=state)
                if wheel_identity(wheel)!=request['worker_core_sha256']:raise RemoteError('REMOTE_STALE_WHEEL')
                for item in request['inputs']:
                    ref=item['reference'];head=store._head(ref['object_key'],__import__('time').monotonic()+30,ref['version_id'])
                    if head.get('VersionId')!=ref['version_id'] or head.get('ContentLength')!=ref['size_bytes'] or head.get('Metadata',{}).get('sha256')!=ref['sha256']:
                        raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                add(phase,name,'passed','REMOTE_FILES_VALID')
                phase,name='provider','connection'
                # prepare creates only a local provider control document; never
                # submit. The locator below is not used to retrieve a job.
                locator={'artifact_id':'dispatch_request','store_id':store.profile['store_id'],'object_key':request['store_binding']['input_prefix']+'preflight.json',
                         'version_id':'preflight','sha256':'0'*64,'size_bytes':1,'role':'data','media_type':'application/json'}
                V2Adapter(profile,store_config=store_config).prepare(request,locator,root)
                add(phase,name,'passed','REMOTE_CONNECTION_VERIFIED')
            phase,name='runtime','environment'
            ready=False
            if profile['transport']=='local':
                from fsf.remote.preflight import _local_runtime
                ready=_local_runtime(request['recipe'],profile)
                if request['policy']['gpu']:
                    from fsf.remote.gpu import cuda_inventory
                    cuda_inventory(min_vram_mb=request['recipe']['execution_requirements']['render']['min_vram_mb'])
                    # Device availability does not verify CUDA inside Blender.
                    ready=False
            add(phase,name,'passed' if ready else 'unverified','REMOTE_RUNTIME_VERIFIED' if ready else 'REMOTE_CAPABILITY_UNVERIFIED')
    except Exception as exc:
        code=exc.code if isinstance(exc,RemoteError) and exc.code in _CODES else 'REMOTE_PREFLIGHT_FAILED'
        add(phase,name,'failed',code)
    static=[c for c in checks if c['phase']=='static']
    status='failed' if any(c['status']=='failed' for c in checks) else ('passed' if checks and all(c['status']=='passed' for c in checks) else 'unverified')
    return {'schema_version':'fsf_remote_preflight_v1','status':status,'static_ok':all(c['status']=='passed' for c in static),
            'ready_for_dispatch':status=='passed','verified_execution':False,'checks':checks}
