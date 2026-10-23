"""Small standalone data-plane bootstrap. Requires host boto3 and AWS chain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import zipfile


def failure_report(exc, phase):
    """Report host access failures without reflecting provider error bodies.

    Some host SDKs wrap HTTPError in ConnectionError. Preserve a bounded HTTP
    classification, without guessing whether a missing grant caused it.
    """
    code = 'REMOTE_CANCELLED' if isinstance(exc, KeyboardInterrupt) else 'REMOTE_BOOTSTRAP_FAILED'
    report = {'ok': False, 'code': code, 'phase': phase}
    if not phase.startswith('host_secret_') or isinstance(exc, KeyboardInterrupt):
        return report
    report['code'] = 'REMOTE_HOST_SECRET_ACCESS_FAILED'
    current = exc
    seen = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        status = getattr(current, 'code', None)
        if type(status) is int and 100 <= status <= 599:
            report['http_status'] = status
            # Informative only; this bootstrap never retries a Secret request.
            report['retryable'] = status == 429 or 500 <= status <= 599
            break
        current = current.__cause__ or current.__context__
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--locator',required=True)
    parser.add_argument('--store-id',required=True)
    parser.add_argument('--state-root')
    args=parser.parse_args(argv)
    phase='host_binding'
    try:
        secret_client=None
        config_path=os.environ.get('FSF_ARTIFACT_STORE_CONFIG')
        if not config_path:
            phase='host_secret_sdk'
            # Saved-run bindings must first pass an explicitly authorized CPU
            # smoke. Labels are fixed; secret values never enter the notebook.
            from kaggle_secrets import UserSecretsClient
            secrets=UserSecretsClient()
            secret_client=secrets
            phase='host_secret_config'
            config_text=secrets.get_secret('FSF_ARTIFACT_STORE_CONFIG')
            config_root=Path(tempfile.mkdtemp(prefix='fsf-store-config-'))
            config_file=config_root/'store.json';config_file.write_text(config_text,'utf-8');config_file.chmod(0o600)
            config_path=str(config_file)
            phase='host_secret_access_key'
            os.environ['AWS_ACCESS_KEY_ID']=secrets.get_secret('FSF_STORE_ACCESS_KEY_ID')
            phase='host_secret_secret_key'
            os.environ['AWS_SECRET_ACCESS_KEY']=secrets.get_secret('FSF_STORE_SECRET_ACCESS_KEY')
            try:os.environ['AWS_SESSION_TOKEN']=secrets.get_secret('FSF_STORE_SESSION_TOKEN')
            except Exception:pass
        phase='store_configuration'
        config=json.loads(Path(config_path).read_text('utf-8-sig'))
        if config['store_id']!=args.store_id or not config['authorized'] or config['credential_mode']!='aws_default_chain':
            raise ValueError('REMOTE_STORE_AUTH_REQUIRED')
        endpoint=urlsplit(config['endpoint'])
        if endpoint.scheme!='https' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('REMOTE_STORE_ENDPOINT')
        phase='store_client'
        import boto3
        from botocore.config import Config
        client=boto3.client('s3',endpoint_url=config['endpoint'],region_name=config['region'],
            config=Config(signature_version='s3v4',connect_timeout=10,read_timeout=30,retries={'max_attempts':0},s3={'addressing_style':'path'}))
        def restrict(request,**kwargs):
            parsed=urlsplit(request.url)
            if parsed.scheme!='https' or parsed.netloc!=endpoint.netloc:raise ValueError('REMOTE_STORE_ENDPOINT')
        client.meta.events.register('before-send.s3',restrict)
        root=Path(args.state_root) if args.state_root else Path(tempfile.mkdtemp(prefix='fsf-v2-'))
        root.mkdir(parents=True,exist_ok=True)
        def status(value):
            target=root/'bootstrap-status.json';temp=root/'.bootstrap-status.tmp'
            temp.write_text(json.dumps({'status':value}),encoding='utf-8');os.replace(temp,target)
        status('running')
        deadline=time.monotonic()+config['deadline_seconds']
        locator=json.loads(args.locator)
        def download(ref,target,limit):
            if (ref['store_id']!=config['store_id'] or not ref['version_id'] or ref['version_id'].lower() in {'latest','null','none'}
                    or not 0<ref['size_bytes']<=min(limit,config['max_artifact_bytes'])
                    or not any(ref['object_key'].startswith(p) for p in config['read_prefixes'])):
                raise ValueError('REMOTE_ARTIFACT_INTEGRITY')
            reply=client.get_object(Bucket=config['bucket'],Key=ref['object_key'],VersionId=ref['version_id'])
            if reply.get('VersionId')!=ref['version_id'] or reply['ContentLength']!=ref['size_bytes']:raise ValueError('REMOTE_ARTIFACT_INTEGRITY')
            h=hashlib.sha256();size=0
            try:
                with target.open('xb') as stream:
                    while data:=reply['Body'].read(1024*1024):
                        if time.monotonic()>=deadline:raise ValueError('REMOTE_TRANSFER_DEADLINE')
                        size+=len(data)
                        if size>ref['size_bytes']:raise ValueError('REMOTE_ARTIFACT_INTEGRITY')
                        h.update(data);stream.write(data)
            finally:reply['Body'].close()
            if size!=ref['size_bytes'] or h.hexdigest()!=ref['sha256']:raise ValueError('REMOTE_ARTIFACT_INTEGRITY')
        phase='request_download'
        request_path=root/'request.json';download(locator,request_path,512*1024)
        request=json.loads(request_path.read_text('utf-8'))
        deadline=min(deadline,time.monotonic()+request['policy']['deadline_seconds'])
        wheel=request['worker_wheel'];filename=Path(wheel['object_key']).name
        if not filename.startswith('free_studio_flow-') or not filename.endswith('-py3-none-any.whl'):raise ValueError('REMOTE_WHEEL')
        phase='wheel_download'
        package=root/filename;download(wheel,package,64*1024*1024)
        if 'https://pypi.org/simple' not in request['policy']['network_sources']:raise ValueError('REMOTE_NETWORK_NOT_AUTHORIZED')
        # Process safety is authenticated as part of the immutable worker wheel.
        with zipfile.ZipFile(package) as archive:
            safety=root/'safety.py';safety.write_bytes(archive.read('fsf/executors/subprocess_safety.py'))
        import importlib.util
        spec=importlib.util.spec_from_file_location('fsf_v2_safety',safety)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        def run(command,*,worker=False,installation=False):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('REMOTE_TRANSFER_DEADLINE')
            env={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP','HOME','USERPROFILE') if k in os.environ}
            if worker:
                for name in ('AWS_PROFILE','AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION','AWS_REGION','AWS_CA_BUNDLE'):
                    if name in os.environ:env[name]=os.environ[name]
                env['FSF_ARTIFACT_STORE_CONFIG']=config_path
                for name in request['recipe']['environment']['credential_env_names']:
                    if name not in request['policy']['credential_env_names']:raise ValueError('REMOTE_CREDENTIAL_NOT_AUTHORIZED')
                    if name in os.environ:env[name]=os.environ[name]
                    elif secret_client is not None:env[name]=secret_client.get_secret(name)
            if installation:
                for name,target in {'FSF_REMOTE_INSTALL_HTTP_PROXY':'HTTP_PROXY','FSF_REMOTE_INSTALL_HTTPS_PROXY':'HTTPS_PROXY',
                                    'FSF_REMOTE_INSTALL_NO_PROXY':'NO_PROXY','FSF_REMOTE_INSTALL_CA':'SSL_CERT_FILE'}.items():
                    if name in os.environ:
                        if name not in request['policy']['credential_env_names']:raise ValueError('REMOTE_INSTALL_NETWORK_NOT_AUTHORIZED')
                        env[target]=os.environ[name]
            res=module.run_safe_subprocess(command,cwd=root,timeout_seconds=remaining,output_limit_bytes=1024*1024,env=env,inherit_env=False,
                cancel_check=lambda:(root/'cancel').exists())
            if res.returncode==130:raise KeyboardInterrupt()
            if res.timed_out or res.returncode:raise ValueError('REMOTE_BOOTSTRAP_FAILED')
        phase='worker_environment'
        venv=root/'venv';run([sys.executable,'-m','venv','--without-pip',str(venv)])
        python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        phase='worker_installation'
        run([sys.executable,'-m','pip','--python',str(python),'install','--disable-pip-version-check',
             '--index-url','https://pypi.org/simple',str(package)+'[remote]'],installation=True)
        output=root/'work';output.mkdir()
        phase='worker_execution'
        run([str(python),'-m','fsf.cli','remote','worker-v2',str(request_path),'--out',str(output),
             '--store',config_path,'--recipe-digest',request['recipe']['recipe_digest']],worker=True)
        status('completed')
        return 0
    except BaseException as exc:
        code='REMOTE_CANCELLED' if isinstance(exc,KeyboardInterrupt) else 'REMOTE_BOOTSTRAP_FAILED'
        if 'status' in locals():status('cancelled' if code=='REMOTE_CANCELLED' else 'failed')
        # Provider logs are not a result transport and contain no arbitrary error.
        print(json.dumps(failure_report(exc, phase)))
        return 130 if code=='REMOTE_CANCELLED' else 2


if __name__=='__main__':raise SystemExit(main())
