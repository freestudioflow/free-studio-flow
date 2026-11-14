"""Bounded Kaggle native bootstrap. Embedded by an authenticated parent."""
import hashlib
import json
import platform
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
import re
import shutil
from datetime import datetime,timezone

started=time.monotonic();root=Path(WORK_ROOT) if 'WORK_ROOT' in globals() else Path(tempfile.mkdtemp(prefix='fsf-native-'))
root.mkdir(parents=True,exist_ok=True)
phase='input_verification'
print(json.dumps({'native_python':platform.python_version()}))
def status(state):
    path=root/'native-status.json';tmp=root/'native-status.tmp'
    tmp.write_text(json.dumps({'status':state,'request_digest':globals().get('REQUEST_DIGEST',request.get('request_digest') if 'request' in globals() else globals().get('request_digest'))}))
    os.replace(tmp,path)

def checksum(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):
            if time.monotonic()-started>=DEADLINE:raise TimeoutError()
            value.update(block)
    return value.hexdigest()

try:
    bundle=Path(BUNDLE)
    legacy_limit=64*1024*1024
    extended='limits' in globals()
    if extended:
        encoded=json.dumps(limits,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if hashlib.sha256(encoded).hexdigest()!=limits_digest:raise ValueError('REMOTE_LIMITS_MISMATCH')
    if not bundle.is_file():raise ValueError('REMOTE_KAGGLE_MOUNT_MISSING')
    if not 0<bundle.stat().st_size<=(limits['archive_compressed_bytes'] if extended else legacy_limit) or checksum(bundle)!=BUNDLE_SHA:raise ValueError()
    if extended:
        with bundle.open('rb') as source:
            end=zipfile._EndRecData(source)
            if not end or end[zipfile._ECD_SIZE]>min(limits['manifest_bytes'],8*1024*1024) or end[zipfile._ECD_ENTRIES_TOTAL]>limits['archive_members']:
                raise ValueError()
    with zipfile.ZipFile(bundle) as archive:
        infos=archive.infolist();names=[v.filename for v in infos]
        total=sum(v.file_size for v in infos)
        if (len(names)>(limits['archive_members'] if extended else 128)
                or len(names)!=len(set(name.casefold() if extended else name for name in names))
                or total>(limits['archive_unpacked_bytes'] if extended else legacy_limit)):raise ValueError()
        if extended:
            disk=shutil.disk_usage(root)
            reserve=max(limits['disk_reserve_bytes'],1024**3,disk.total//100)
            if total>limits['disk_bytes'] or disk.free-reserve<total:raise ValueError('STREAM_DISK_LIMIT')
        for entry in infos:
            parts=entry.filename.split('/')
            if any(v in ('','..','.') for v in parts) or '\\' in entry.filename or ':' in entry.filename or entry.flag_bits&1 or stat.S_ISLNK(entry.external_attr>>16):raise ValueError()
            if extended and (entry.file_size>limits['member_bytes']
                    or entry.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                    or stat.S_IFMT(entry.external_attr>>16) not in (0,stat.S_IFREG)):
                raise ValueError()
            target=root.joinpath(*parts);target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(entry) as source,target.open('xb') as out:
                count=0
                while block:=source.read(1024*1024):
                    if time.monotonic()-started>=DEADLINE:raise TimeoutError()
                    count+=len(block)
                    if count>entry.file_size:raise ValueError()
                    out.write(block)
                if count!=entry.file_size:raise ValueError()
    request=json.loads((root/'request.json').read_text());ref=request['worker_wheel']
    if extended and (request.get('schema_version')!='fsf_remote_request_v5'
            or request.get('limits')!=limits or request.get('limits_digest')!=limits_digest
            or request.get('request_digest')!=request_digest or ref['size_bytes']>limits['worker_package_bytes']):
        raise ValueError('REMOTE_LIMITS_MISMATCH')
    if not extended and request.get('schema_version')=='fsf_remote_request_v5':
        raise ValueError('REMOTE_LIMITS_MISMATCH')
    wheel=root/'blobs'/ref['sha256']
    if checksum(wheel)!=ref['sha256']:raise ValueError()
    package=root/Path(ref['object_key']).name
    shutil.copyfile(wheel,package)
    status('running')
    # Use the same bounded process-tree cleanup as every other worker route.
    with zipfile.ZipFile(package) as archive:
        safety=root/'safety.py';safety.write_bytes(archive.read('fsf/executors/subprocess_safety.py'))
    import importlib.util
    spec=importlib.util.spec_from_file_location('native_safety',safety)
    safety_module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=safety_module;spec.loader.exec_module(safety_module)
    env={k:os.environ[k] for k in ('PATH','HOME','TEMP','TMP','SystemRoot','WINDIR') if k in os.environ}
    for name in ('LD_LIBRARY_PATH','CUDA_HOME','CUDA_PATH','CUDA_VISIBLE_DEVICES','NVIDIA_VISIBLE_DEVICES','NVIDIA_DRIVER_CAPABILITIES'):
        if name in os.environ:env[name]=os.environ[name]
    def run(args,worker=False):
        remaining=DEADLINE-(time.monotonic()-started)
        if remaining<=0:raise TimeoutError()
        child_env=dict(env)
        if worker:
            for name in request['recipe']['environment']['credential_env_names']:
                if name not in request['policy']['credential_env_names']:raise ValueError('REMOTE_CREDENTIAL_NOT_AUTHORIZED')
                if not os.environ.get(name):raise ValueError('REMOTE_CREDENTIAL_MISSING')
                if name not in {'FSF_REMOTE_INSTALL_HTTP_PROXY','FSF_REMOTE_INSTALL_HTTPS_PROXY','FSF_REMOTE_INSTALL_NO_PROXY','FSF_REMOTE_INSTALL_CA'}:
                    child_env[name]=os.environ[name]
        command_started=datetime.now(timezone.utc).isoformat();clock=time.monotonic()
        value=safety_module.run_safe_subprocess(args,cwd=root,env=child_env,inherit_env=False,timeout_seconds=remaining,output_limit_bytes=16*1024*1024)
        if value.timed_out or value.returncode:
            clean=safety_module.sanitize_process_text(value.stderr+'\n'+value.stdout,env=child_env)
            codes=sorted(set(re.findall(r'\b[A-Z][A-Z0-9]{1,31}(?:_[A-Z0-9]{1,32}){1,6}\b',clean)))[:32]
            print(json.dumps({'native_phase':phase,'returncode':value.returncode,
                              'timed_out':value.timed_out,'error_codes':codes,
                              'started_at':command_started,'finished_at':datetime.now(timezone.utc).isoformat(),
                              'elapsed_seconds':time.monotonic()-clock}))
            raise RuntimeError('REMOTE_NATIVE_BOOTSTRAP_FAILED')
    phase='worker_environment';venv=root/'venv';run([sys.executable,'-m','venv','--without-pip',str(venv)])
    python=venv/'bin/python'
    phase='worker_installation'
    run([sys.executable,'-m','pip','--python',str(python),'install','-q','--disable-pip-version-check','--index-url','https://pypi.org/simple',str(package)+'[remote]'])
    phase='worker_execution'
    run([str(python),'-m','fsf.remote.kaggle_native_worker',str(root),'--out',globals().get('RESULT_OUT','/kaggle/working/fsf-native-result.zip')],worker=True)
    status('completed')
    print(json.dumps({'native_worker':'completed','artifacts_in_log':False}))
except BaseException as exc:
    status('failed')
    code='REMOTE_INSTALL_OUTPUT_LIMIT' if str(exc)=='PROCESS_OUTPUT_LIMIT' else 'REMOTE_NATIVE_BOOTSTRAP_FAILED'
    if isinstance(exc,ValueError) and str(exc) in {'REMOTE_CREDENTIAL_MISSING','REMOTE_CREDENTIAL_NOT_AUTHORIZED','REMOTE_HOST_SECRET_ACCESS_FAILED','REMOTE_LIMITS_MISMATCH','STREAM_DISK_LIMIT','REMOTE_KAGGLE_MOUNT_MISSING'}:
        code=str(exc)
    print(json.dumps({'native_worker':'failed','code':code,'phase':phase}))
    raise RuntimeError('REMOTE_NATIVE_BOOTSTRAP_FAILED') from None
