"""One-level v2 scheduling; object data never travels through provider logs."""
import base64
from importlib import resources
import json
import os
from pathlib import Path
import subprocess
import sys

from fsf.remote.contracts import RemoteError,digest,write_json
from fsf.core.edit_store import read_json
from fsf.remote.v2 import validate_profile,validate_request


def secret_setup_notebook():
    """Operator-run private CPU seed; it neither submits nor invokes a model."""
    code='''import json
from urllib.parse import urlsplit
try:
    import boto3
    from botocore.config import Config
    from kaggle_secrets import UserSecretsClient
    secrets = UserSecretsClient()
    store = json.loads(secrets.get_secret("FSF_ARTIFACT_STORE_CONFIG"))
    endpoint = urlsplit(store["endpoint"])
    assert endpoint.scheme == "https" and not endpoint.username and not endpoint.password
    assert not endpoint.query and not endpoint.fragment and endpoint.path in ("", "/")
    assert store["authorized"] is True
    credentials = {"aws_access_key_id": secrets.get_secret("FSF_STORE_ACCESS_KEY_ID"),
                   "aws_secret_access_key": secrets.get_secret("FSF_STORE_SECRET_ACCESS_KEY")}
    try:
        token = secrets.get_secret("FSF_STORE_SESSION_TOKEN")
        if token: credentials["aws_session_token"] = token
    except Exception:
        pass
    client = boto3.client("s3", endpoint_url=store["endpoint"], region_name=store["region"],
        config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=20,
                      retries={"max_attempts": 0}, s3={"addressing_style": "path"}), **credentials)
    response = client.get_bucket_versioning(Bucket=store["bucket"])
    assert response.get("Status") == "Enabled"
    print(json.dumps({"secret_probe": "passed", "versioning": True,
                      "verified_transfer": False, "gpu_render": False, "model_inference": False}))
except Exception:
    print(json.dumps({"secret_probe": "failed", "code": "FSF_SECRET_PROBE_FAILED"}))
    raise SystemExit(2)
'''
    return {'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},
        'cells':[{'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':code.splitlines(keepends=True)}]}


def notebook(request,locator):
    validate_request(request)
    bootstrap=resources.files('fsf.tools').joinpath('remote_bootstrap_v2.py').read_bytes()
    code='import base64,os,pathlib,subprocess,sys\n'
    code+="p=pathlib.Path('/tmp/fsf-v2-bootstrap.py');p.write_bytes(base64.b64decode("+repr(base64.b64encode(bootstrap).decode())+"))\n"
    # Notebook hosts may inject their Secret Manager SDK into sys.path rather
    # than the interpreter's site-packages. Preserve that host module context
    # for bootstrap only; recipe commands still use the worker's clean env.
    code+='bootstrap_env=os.environ.copy();bootstrap_env["PYTHONPATH"]=os.pathsep.join(str(pathlib.Path(item).absolute()) for item in sys.path)\n'
    code+='subprocess.run([sys.executable,str(p),"--locator",'+repr(json.dumps(locator,separators=(',',':')))+',"--store-id",'+repr(request['store_binding']['store_id'])+'],check=True,timeout='+str(request['policy']['deadline_seconds'])+',env=bootstrap_env)\n'
    value={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},
           'cells':[{'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':code.splitlines(keepends=True)}]}
    # Importing into an existing notebook must not retain its former CPU flag.
    # This is a requested capability, never evidence of an available device.
    value['metadata']['kaggle']={'isGpuEnabled':request['policy'].get('gpu',False),
        'isInternetEnabled':True,'language':'python','sourceType':'notebook'}
    if len(json.dumps(value).encode())>512*1024:raise RemoteError('REMOTE_NOTEBOOK_LIMIT')
    return value


def metadata(request,profile,owner):
    gpu=request['policy']['gpu']
    slug=profile['settings'].get('worker_notebook','fsf-worker-'+request['request_digest'][:24])
    value={'id':owner+'/'+slug,'title':slug,
        'code_file':'worker.ipynb','language':'python','kernel_type':'notebook','is_private':True,
        'enable_gpu':gpu,'enable_tpu':False,'enable_internet':True,
        'machine_shape':profile['settings']['accelerator'] if gpu else '',
        'dataset_sources':[],'competition_sources':[],'kernel_sources':[]}
    if profile['settings'].get('docker_image'):value['docker_image']=profile['settings']['docker_image']
    return value


class V2Adapter:
    def __init__(self,profile,*,store_config):
        self.profile=validate_profile(profile);self.store_config=str(Path(store_config).absolute())

    def _native(self):
        return Path(self.store_config).is_file() and read_json(self.store_config).get('schema_version') in {'fsf_kaggle_native_store_v1','fsf_kaggle_native_store_v2'}

    def _command(self,operation,request,locator,directory,remote=None):
        from fsf.remote.command_adapter import CommandAdapter
        return CommandAdapter(self.profile,self.store_config).call(operation,request,locator,directory,remote)

    def _native_ssh(self):
        from fsf.remote.native_ssh import NativeSsh
        return NativeSsh(self._ssh(),self.store_config)

    def prepare(self,request,locator,directory):
        if self.profile['transport']=='command':return self._command('prepare',request,locator,directory)
        if self.profile['transport']=='ssh' and self._native():return self._native_ssh().prepare(request,directory)
        if self.profile['transport']=='local':
            from fsf.remote.transports import LocalAdapter
            base={k:v for k,v in self.profile.items() if k not in {'store_binding','gpu_limits','group_limits','allocation','allow_recipe_experiments'}}
            base['schema_version']='fsf_remote_profile_v1'
            LocalAdapter(base).prepare(None,None,None)
        elif self.profile['transport']=='ssh':self._ssh().prepare(None,None,None)
        else:self._kaggle('prepare',request,locator,directory)

    def _ssh(self):
        from fsf.remote.transports import SshAdapter
        base={k:v for k,v in self.profile.items() if k not in {'store_binding','gpu_limits','group_limits','allocation','allow_recipe_experiments'}}
        base['schema_version']='fsf_remote_profile_v1'
        return SshAdapter(base)

    def _kaggle(self,operation,request,locator,directory,remote=None):
        from fsf.executors.subprocess_safety import run_safe_subprocess
        from fsf.live.credentials import kaggle_credential_environment
        native=self._native()
        payload={'operation':operation,'request':request,'locator':locator,'profile':self.profile,'directory':str(directory),'remote':remote}
        if native:payload['store_config']=self.store_config
        write_json(directory/'provider-v2-call.json',payload)
        with kaggle_credential_environment() as env:
            result=run_safe_subprocess([sys.executable,'-m','fsf.remote.kaggle_native' if native else 'fsf.remote.kaggle_v2_api',str(directory/'provider-v2-call.json')],
                env=env,inherit_env=False,timeout_seconds=300 if native else 120,output_limit_bytes=16384)
        if result.returncode or result.timed_out:raise RemoteError('REMOTE_KAGGLE_API_FAILED')
        reply=read_json(directory/'provider-v2-response.json')
        if not reply['ok']:raise RemoteError(reply['code'])
        return reply['result']

    def fetch_into_store(self,request,remote,directory):
        if self.profile['transport']=='command':return self._command('fetch',request,None,directory,remote)
        if self.profile['transport']=='ssh' and self._native():
            self._native_ssh().fetch(request,directory)
        elif self.profile['transport']=='kaggle' and self._native():
            self._kaggle('fetch',request,None,directory,remote=remote)

    def submit(self,request,locator,directory):
        transport=self.profile['transport'];key=request['request_digest']
        if transport in {'kaggle','ssh','command'}:
            from fsf.remote.dispatch_hold import refuse_held_submit
            refuse_held_submit(directory)
        if transport=='command':return self._command('submit',request,locator,directory)
        if transport=='kaggle':return self._kaggle('submit',request,locator,directory)
        if transport=='ssh' and self._native():return self._native_ssh().submit(request,directory)
        if transport=='ssh':
            import shlex
            bootstrap=resources.files('fsf.tools').joinpath('remote_bootstrap_v2.py').read_bytes()
            # stdin carries only authenticated bootstrap + locator, never a key.
            remote_root=self.profile['settings']['remote_root'].rstrip('/')+'/v2-'+key
            script='import base64,pathlib,subprocess,sys,tempfile\nr=pathlib.Path('+repr(remote_root)+');r.mkdir(parents=True,exist_ok=True)\np=r/"bootstrap.py"\np.write_bytes(base64.b64decode('+repr(base64.b64encode(bootstrap).decode())+'))\n'
            script+='subprocess.Popen([sys.executable,str(p),"--locator",'+repr(json.dumps(locator))+',"--store-id",'+repr(request['store_binding']['store_id'])+',"--state-root",str(r)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\nprint("launched")\n'
            self._ssh()._ssh([self.profile['settings']['python'],'-c',script])
            return {'launched':True,'request_digest':key}
        env={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP','HOME','USERPROFILE',
            'AWS_PROFILE','AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION','AWS_REGION','AWS_CA_BUNDLE') if k in os.environ}
        env['FSF_ARTIFACT_STORE_CONFIG']=self.store_config
        for name in request['recipe']['environment']['credential_env_names']:
            if name in request['policy']['credential_env_names'] and name in os.environ:env[name]=os.environ[name]
        target=Path(self.profile['settings']['worker_root']);target.mkdir(exist_ok=True)
        process=subprocess.Popen([self.profile['settings']['python'],'-m','fsf.cli','remote','worker-v2',str(directory/'request.json'),
            '--store',self.store_config,'--out',str(target),'--recipe-digest',request['recipe']['recipe_digest']],
            env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0,start_new_session=os.name!='nt')
        return {'pid':process.pid,'request_digest':key}

    def status(self,request,remote,directory):
        if self.profile['transport']=='command':return self._command('status',request,None,directory,remote)
        if self.profile['transport']=='kaggle':return self._kaggle('status',request,None,directory,remote=remote)['status']
        if self.profile['transport']=='ssh' and self._native():return self._native_ssh().status(request)
        if self.profile['transport']=='ssh':
            path=self.profile['settings']['remote_root'].rstrip('/')+'/v2-'+request['request_digest']+'/bootstrap-status.json'
            script='import pathlib,json; p=pathlib.Path('+repr(path)+');print(p.read_text() if p.is_file() else json.dumps({"status":"unknown"}))'
            return json.loads(self._ssh()._ssh([self.profile['settings']['python'],'-c',script]))['status']
        if self.profile['transport']=='local':
            path=Path(self.profile['settings']['worker_root'])/request['request_digest']/'worker-status-v2.json'
            if path.is_file():
                value=read_json(path)
                if value['request_digest']!=request['request_digest']:raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
                return value['status']
        # FSF2's durable result is authoritative for completion; scheduling
        # acceptance alone is not provider completion evidence.
        return 'unknown'

    def reconcile(self,request,locator,directory):
        if self.profile['transport']=='command':return self._command('reconcile',request,locator,directory)
        if self.profile['transport']=='kaggle':return self._kaggle('reconcile',request,locator,directory)
        if self.profile['transport']=='ssh' and self._native():return self._native_ssh().reconcile(request)
        # Never respawn an unknown SSH/local submission automatically.
        return None

    def cancel(self,request,remote,directory):
        if self.profile['transport']=='command':return self._command('cancel',request,None,directory,remote)
        if self.profile['transport']=='kaggle':return 'unsupported'
        if self.profile['transport']=='ssh' and self._native():return self._native_ssh().cancel(request)
        if self.profile['transport']=='local':
            marker=Path(self.profile['settings']['worker_root'])/request['request_digest']/'cancel'
            marker.parent.mkdir(exist_ok=True)
            from fsf.core.asset_library import _no_links
            _no_links(marker.parent)
            if marker.exists():_no_links(marker)
            marker.touch(exist_ok=True)
        else:
            path=self.profile['settings']['remote_root'].rstrip('/')+'/v2-'+request['request_digest']+'/cancel'
            script='import pathlib;p=pathlib.Path('+repr(path)+');p.parent.mkdir(parents=True,exist_ok=True);p.touch(exist_ok=True);print("requested")'
            self._ssh()._ssh([self.profile['settings']['python'],'-c',script])
        return 'requested'
