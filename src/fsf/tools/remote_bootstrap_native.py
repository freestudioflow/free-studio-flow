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

started=time.monotonic();root=Path(WORK_ROOT) if 'WORK_ROOT' in globals() else Path(tempfile.mkdtemp(prefix='fsf-native-'))
root.mkdir(parents=True,exist_ok=True)
phase='input_verification'
print(json.dumps({'native_python':platform.python_version()}))
def status(state):
    path=root/'native-status.json';tmp=root/'native-status.tmp'
    tmp.write_text(json.dumps({'status':state,'request_digest':globals().get('REQUEST_DIGEST',request.get('request_digest') if 'request' in globals() else None)}))
    os.replace(tmp,path)
try:
    bundle=Path(BUNDLE)
    if not 0<bundle.stat().st_size<=64*1024*1024 or hashlib.sha256(bundle.read_bytes()).hexdigest()!=BUNDLE_SHA:raise ValueError()
    with zipfile.ZipFile(bundle) as archive:
        infos=archive.infolist();names=[v.filename for v in infos]
        if len(names)>128 or len(names)!=len(set(names)) or sum(v.file_size for v in infos)>64*1024*1024:raise ValueError()
        for entry in infos:
            parts=entry.filename.split('/')
            if any(v in ('','..','.') for v in parts) or '\\' in entry.filename or ':' in entry.filename or entry.flag_bits&1 or stat.S_ISLNK(entry.external_attr>>16):raise ValueError()
            target=root.joinpath(*parts);target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(entry) as source,target.open('xb') as out:
                count=0
                while block:=source.read(1024*1024):
                    count+=len(block)
                    if count>entry.file_size:raise ValueError()
                    out.write(block)
                if count!=entry.file_size:raise ValueError()
    request=json.loads((root/'request.json').read_text());ref=request['worker_wheel']
    wheel=root/'blobs'/ref['sha256']
    if hashlib.sha256(wheel.read_bytes()).hexdigest()!=ref['sha256']:raise ValueError()
    package=root/Path(ref['object_key']).name;package.write_bytes(wheel.read_bytes())
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
    def run(args):
        remaining=DEADLINE-(time.monotonic()-started)
        if remaining<=0:raise TimeoutError()
        value=safety_module.run_safe_subprocess(args,cwd=root,env=env,inherit_env=False,timeout_seconds=remaining,output_limit_bytes=1024*1024)
        if value.timed_out or value.returncode:raise RuntimeError('REMOTE_NATIVE_BOOTSTRAP_FAILED')
    phase='worker_environment';venv=root/'venv';run([sys.executable,'-m','venv','--without-pip',str(venv)])
    python=venv/'bin/python'
    phase='worker_installation'
    run([sys.executable,'-m','pip','--python',str(python),'install','-q','--disable-pip-version-check','--index-url','https://pypi.org/simple',str(package)+'[remote]'])
    phase='worker_execution'
    run([str(python),'-m','fsf.remote.kaggle_native_worker',str(root),'--out',globals().get('RESULT_OUT','/kaggle/working/fsf-native-result.zip')])
    status('completed')
    print(json.dumps({'native_worker':'completed','artifacts_in_log':False}))
except BaseException as exc:
    status('failed')
    code='REMOTE_INSTALL_OUTPUT_LIMIT' if str(exc)=='PROCESS_OUTPUT_LIMIT' else 'REMOTE_NATIVE_BOOTSTRAP_FAILED'
    print(json.dumps({'native_worker':'failed','code':code,'phase':phase}))
    raise RuntimeError('REMOTE_NATIVE_BOOTSTRAP_FAILED') from None
