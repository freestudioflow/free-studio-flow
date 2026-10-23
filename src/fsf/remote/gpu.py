"""Explicit CUDA inventory and authorization; capability is not render evidence."""
import csv
import io
import os
import re
import shutil
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError


MAX_DECLARED_DEVICES = 16
GPU_RUNTIME_NAMES = ('LD_LIBRARY_PATH', 'CUDA_HOME', 'CUDA_PATH',
                     'CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'NVIDIA_DRIVER_CAPABILITIES')


def gpu_runtime_environment():
    """Driver paths/device visibility only; no provider or model credentials."""
    return {key: os.environ[key] for key in GPU_RUNTIME_NAMES if key in os.environ}


def memory_inventory(timeout=5):
    """Sample physical memory and resident compute contexts, not a hard quota."""
    executable=shutil.which('nvidia-smi')
    if not executable:raise RemoteError('REMOTE_GPU_UNAVAILABLE')
    env={**{k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ},**gpu_runtime_environment()}
    def query(fields,kind):
        result=run_safe_subprocess([executable,'--query-'+kind+'='+fields,'--format=csv,noheader,nounits'],
            timeout_seconds=timeout,output_limit_bytes=32768,inherit_env=False,env=env)
        if result.returncode or result.timed_out:raise RemoteError('REMOTE_GPU_MEMORY_UNAVAILABLE')
        return list(csv.reader(io.StringIO(result.stdout)))
    try:
        memory={}
        for row in query('uuid,memory.used,memory.total','gpu'):
            uuid,used,total=[v.strip() for v in row]
            if not used.isdecimal() or not total.isdecimal():raise ValueError()
            memory[uuid]={'used_mb':int(used),'total_mb':int(total),'compute_processes':0}
        for row in query('gpu_uuid,pid','compute-apps'):
            if not row:continue
            uuid,pid=[v.strip() for v in row]
            if not pid.isdecimal() or uuid not in memory:raise ValueError()
            memory[uuid]['compute_processes']+=1
        return memory
    except (ValueError,TypeError):raise RemoteError('REMOTE_GPU_MEMORY_UNAVAILABLE') from None


def select_devices(visible,*,authorized,requested):
    """Bind a job to devices. N is the authorized count, not a hardcoded pair."""
    if type(authorized) is not int or type(requested) is not int or authorized<0 or requested<0:
        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    if requested>MAX_DECLARED_DEVICES or authorized>MAX_DECLARED_DEVICES:
        raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    if requested==0:return []
    if authorized<1 or authorized>len(visible):raise RemoteError('REMOTE_GPU_UNAVAILABLE')
    pool=sorted(visible,key=lambda item:item['index'])[:authorized]
    if requested>len(pool):raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    return pool[:requested]


def assign_segments(visible,*,authorized,segments):
    """One segment pins its declared devices. Concurrency cannot exceed N."""
    select_devices(visible, authorized=authorized, requested=authorized)
    seen=set();assigned=[];waiting=[];pool=sorted(visible,key=lambda item:item['index'])[:authorized]
    for segment in segments:
        identifier=segment['segment_id']
        if identifier in seen:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
        seen.add(identifier)
        need=segment.get('device_count',1)
        if type(need) is not int or need<1:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
        if need>authorized:raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
        if len(pool)<need:
            waiting.append(segment);continue
        chosen,pool=pool[:need],pool[need:]
        assigned.append({**segment,'devices':chosen})
    occupied=[device for item in assigned for device in item['devices']]
    return {'visible':len(visible),'authorized':authorized,'occupied':occupied,
            'assigned':assigned,'waiting':waiting}


def authorize_gpu(requirements,limits,*,new_submission=True):
    render=requirements['render']
    if render['device']=='cpu':
        if render['backend']!='none' or render['device_count']!=0 or render['min_vram_mb']!=0:
            raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
        return False
    if (not limits['allowed'] or render['backend'] not in limits['backends']
            or render['device_count']>limits['max_devices'] or render['min_vram_mb']>limits['max_vram_mb']):
        raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    if new_submission and os.environ.get('FSF_REMOTE_GPU')!='1':raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    return True


def cuda_inventory(*,min_vram_mb=0,timeout=10):
    executable=shutil.which('nvidia-smi')
    if not executable:raise RemoteError('REMOTE_GPU_UNAVAILABLE')
    result=run_safe_subprocess([executable,'--query-gpu=index,uuid,name,memory.total','--format=csv,noheader,nounits'],
        timeout_seconds=timeout,output_limit_bytes=16384,inherit_env=False,
        env={**{k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}, **gpu_runtime_environment()})
    if result.timed_out or result.returncode:raise RemoteError('REMOTE_GPU_UNAVAILABLE')
    try:
        devices=[]
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row)!=4:raise ValueError()
            index,uuid,name,memory=[v.strip() for v in row]
            if not index.isdecimal() or not re.fullmatch(r'GPU-[0-9a-fA-F-]{16,64}',uuid) or not memory.isdecimal():raise ValueError()
            if int(memory)>=min_vram_mb:
                devices.append({'index':int(index),'uuid':uuid,'name':name,'memory_mb':int(memory)})
        restriction=os.environ.get('CUDA_VISIBLE_DEVICES')
        if restriction is not None:
            tokens=restriction.split(',') if restriction else []
            if not tokens or len(tokens)!=len(set(tokens)):raise ValueError()
            chosen=[]
            for token in tokens:
                matches=[d for d in devices if token==d['uuid'] or token==str(d['index'])]
                if len(matches)!=1:raise ValueError()
                chosen.extend(matches)
            devices=chosen
        if not devices:raise ValueError()
        return devices
    except Exception:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH') from None
