"""Sealed Kaggle boolean/machine-shape policy without provider requests."""
import copy
import importlib.metadata
import json
from types import SimpleNamespace
import sys

import pytest
from fsf.remote.contracts import seal,RemoteError
from fsf.remote.v2 import prepare_request
from fsf.remote.v2_dispatch import submit
from test_remote_v2 import case,remote_wheel


@pytest.fixture
def gateway_sdk(monkeypatch):
    """Model the pinned gateway SDK independently of the host installation."""
    installed_version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, 'version',
        lambda name: '2.2.4' if name == 'kaggle' else installed_version(name))


@pytest.mark.parametrize('version', ['1.6.17', '2.2.5'])
def test_gateway_rejects_unverified_sdk_before_authentication(monkeypatch, version):
    from fsf.remote.kaggle_v2_api import call
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: version)
    class Never:
        def __init__(self):pytest.fail('unverified SDK must not authenticate')
    monkeypatch.setitem(sys.modules, 'kaggle.api.kaggle_api_extended', SimpleNamespace(KaggleApi=Never))
    with pytest.raises(RemoteError, match='^REMOTE_KAGGLE_SDK_VERSION$'):
        call({'operation': 'prepare'})


def kaggle_request(case,monkeypatch,gpu):
    root,_,store,profile,request=case
    profile=copy.deepcopy(profile);profile['transport']='kaggle'
    profile['settings']={'accelerator':'SYNTHETIC_AUTHORIZED_SHAPE','owner_env':'SYNTHETIC_OWNER'}
    profile['policy']['gpu']=True;profile['gpu_limits']={'allowed':True,'backends':['cuda'],'max_devices':1,'max_vram_mb':24576}
    recipe=copy.deepcopy(request['recipe'])
    if gpu:recipe['execution_requirements']['render'].update(device='gpu',backend='cuda',device_count=1,min_vram_mb=16000)
    recipe=seal(recipe,'recipe_digest');profile['approved_recipes']=[recipe['recipe_digest']]
    monkeypatch.setenv('FSF_REMOTE_GPU','1')
    definition={k:request[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')};definition['inputs']={}
    value=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=request['worker_wheel'],segment=request['segment'],output=root/'prepared')
    return profile,value


@pytest.mark.parametrize('gpu',[False,True])
def test_actual_gateway_validates_boolean_and_machine_shape_before_sdk(case,monkeypatch,gpu,gateway_sdk):
    root,_,_,_,_=case
    profile,request=kaggle_request(case,monkeypatch,gpu)
    calls=[]
    class API:
        config_values={'username':'synthetic_owner'}
        def authenticate(self):pass
        def kernels_push(self,path,**kwargs):
            calls.append(kwargs);return SimpleNamespace(error=False,version_number=1)
    monkeypatch.setitem(sys.modules,'kaggle.api.kaggle_api_extended',SimpleNamespace(KaggleApi=API))
    from fsf.remote.kaggle_v2_api import call
    locator={'artifact_id':'request','store_id':'store','object_key':'jobs/request.json','version_id':'fixed','sha256':'a'*64,'size_bytes':500,'role':'data','media_type':'application/json'}
    value={'operation':'prepare','request':request,'profile':profile,'locator':locator,'directory':str(root/'prepared')}
    call(value)
    original=json.loads((root/'prepared/kernel-metadata.json').read_text())
    assert original['enable_gpu'] is gpu and original['machine_shape']==('SYNTHETIC_AUTHORIZED_SHAPE' if gpu else '')
    document=json.loads((root/'prepared/worker.ipynb').read_text())
    assert document['metadata']['kaggle']['isGpuEnabled'] is gpu
    value['operation']='submit'
    for key,replacement in [('enable_gpu',not gpu),('machine_shape','UNAUTHORIZED_SHAPE'),('is_private',False)]:
        changed=copy.deepcopy(original);changed[key]=replacement
        (root/'prepared/kernel-metadata.json').write_text(json.dumps(changed))
        with pytest.raises(RemoteError,match='REMOTE_KAGGLE_POLICY'):call(value)
    assert calls==[]
    (root/'prepared/kernel-metadata.json').write_text(json.dumps(original))
    assert call(value)['version']==1
    assert len(calls)==1 and calls[0]['acc'] is None
    assert ('timeout' in calls[0]) is (not gpu)


def test_gpu_submission_requires_saved_run_cpu_binding_evidence(case,monkeypatch):
    root,_,store,_,_=case;profile,request=kaggle_request(case,monkeypatch,True)
    class Never:
        def prepare(self,*a):pytest.fail('provider called')
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_SECRET_BINDING_UNVERIFIED'):
        submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused',adapter=Never())

def test_explicit_kaggle_image_is_immutable_vendor_pin(case,monkeypatch):
    from fsf.remote.v2 import validate_profile
    from fsf.remote.v2_transports import metadata
    profile,request=kaggle_request(case,monkeypatch,False)
    image='gcr.io/kaggle-private-byod/python@sha256:'+'a'*64
    profile['settings']['docker_image']=image
    validate_profile(profile)
    assert metadata(request,profile,'synthetic-owner')['docker_image']==image
    profile['settings']['docker_image']='untrusted.example/image:latest'
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_IMAGE_PIN'):validate_profile(profile)


def test_fixed_worker_notebook_cannot_target_unrelated_notebook(case,monkeypatch):
    from fsf.remote.v2 import validate_profile
    from fsf.remote.v2_transports import metadata
    profile,request=kaggle_request(case,monkeypatch,False)
    profile['settings']['worker_notebook']='moss-image-t4-test'
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_WORKER_NOTEBOOK'):validate_profile(profile)
    profile['settings']['worker_notebook']='fsf-worker-rc8'
    validate_profile(profile)
    assert metadata(request,profile,'synthetic-owner')['id']=='synthetic-owner/fsf-worker-rc8'


def test_fixed_notebook_status_uses_pinned_version_and_source(case,monkeypatch,gateway_sdk):
    from fsf.remote import kaggle_v2_api as gateway
    from fsf.remote.v2_transports import notebook
    root,*_=case
    profile,request=kaggle_request(case,monkeypatch,False)
    profile['settings']['worker_notebook']='fsf-worker-rc8'
    class API:
        config_values={'username':'synthetic_owner'}
        def authenticate(self):pass
        def kernels_status(self,*args):pytest.fail('must not query latest status')
    monkeypatch.setitem(sys.modules,'kaggle.api.kaggle_api_extended',SimpleNamespace(KaggleApi=API))
    locator={'artifact_id':'request','store_id':'store','object_key':'jobs/request.json','version_id':'fixed','sha256':'a'*64,'size_bytes':500,'role':'data','media_type':'application/json'}
    (root/'prepared/ledger.json').write_text(json.dumps({'locator':locator}))
    observed=[]
    saved=SimpleNamespace(metadata=SimpleNamespace(is_private=True,current_version_number=7),blob=SimpleNamespace(source=json.dumps(notebook(request,locator))))
    monkeypatch.setattr(gateway,'_saved',lambda api,ref,version=None:(observed.append(('source',version)) or saved))
    monkeypatch.setattr(gateway,'_version_status',lambda api,ref,version:(observed.append(('status',version)) or SimpleNamespace(status='COMPLETE')))
    value={'operation':'status','request':request,'profile':profile,'directory':str(root/'prepared'),
        'remote':{'kernel':'synthetic_owner/fsf-worker-rc8','version':7}}
    assert gateway.call(value)=={'status':'completed'}
    assert observed==[('source',None),('status',7)]
    saved.metadata.current_version_number=8
    assert gateway.call(value)=={'status':'unknown'}
    saved.metadata.current_version_number=7
    saved.blob.source=json.dumps({'nbformat':4,'cells':[]})
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_VERSION_AMBIGUOUS'):gateway.call(value)


def test_secret_template_does_not_reflect_secrets_on_failure(monkeypatch,capsys):
    from fsf.remote.v2_transports import secret_setup_notebook
    class Secrets:
        def get_secret(self,name):raise ValueError('synthetic-private-value')
    monkeypatch.setitem(sys.modules,'kaggle_secrets',SimpleNamespace(UserSecretsClient=Secrets))
    source=''.join(secret_setup_notebook()['cells'][0]['source'])
    with pytest.raises(SystemExit) as exc:exec(compile(source,'secret-smoke','exec'),{})
    assert exc.value.code==2
    output=capsys.readouterr().out
    assert 'FSF_SECRET_PROBE_FAILED' in output and 'synthetic-private-value' not in output


def test_bootstrap_child_can_import_notebook_host_sdk(tmp_path,monkeypatch):
    import os,pathlib,subprocess
    from fsf.remote import v2_transports
    sdk=tmp_path/'host_sdk';sdk.mkdir()
    (sdk/'notebook_host_sdk.py').write_text('VALUE = "synthetic"\n')
    marker=tmp_path/'imported.txt'
    bootstrap=tmp_path/'bootstrap.py'
    bootstrap.write_text('from notebook_host_sdk import VALUE\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(VALUE)\n')
    monkeypatch.syspath_prepend(str(sdk))
    monkeypatch.setattr(v2_transports,'validate_request',lambda request:request)
    document=v2_transports.notebook({'store_binding':{'store_id':'synthetic'},'policy':{'deadline_seconds':10}}, {})
    lines=''.join(document['cells'][0]['source']).splitlines()
    # Execute the emitted environment+subprocess statements with a small real
    # bootstrap; no installed provider SDK or actual secret is involved.
    exec('\n'.join(lines[2:]),{'os':os,'pathlib':pathlib,'subprocess':subprocess,'sys':sys,'p':bootstrap})
    assert marker.read_text()=='synthetic'


@pytest.mark.parametrize('status,retryable',[(400,False),(401,False),(403,False),(429,True),(503,True)])
def test_secret_failure_reports_wrapped_http_without_provider_body(status,retryable):
    from io import BytesIO
    from urllib.error import HTTPError
    from fsf.tools.remote_bootstrap_v2 import failure_report
    cause=HTTPError('https://synthetic.invalid/private',status,'synthetic-private-value',{},BytesIO(b'synthetic-private-value'))
    failure=ConnectionError('synthetic-private-value');failure.__cause__=cause
    report=failure_report(failure,'host_secret_config')
    assert report=={'ok':False,'code':'REMOTE_HOST_SECRET_ACCESS_FAILED','phase':'host_secret_config',
                    'http_status':status,'retryable':retryable}
    assert 'synthetic-private-value' not in json.dumps(report)
    # Diagnosing a failure must neither consume nor reflect a provider response.
    assert cause.read()==b'synthetic-private-value'


def test_secret_failure_does_not_claim_missing_grant_or_retry_unknown_error():
    from fsf.tools.remote_bootstrap_v2 import failure_report
    failure=ConnectionError('synthetic-private-value');failure.__cause__=failure
    assert failure_report(failure,'host_secret_access_key')=={
        'ok':False,'code':'REMOTE_HOST_SECRET_ACCESS_FAILED','phase':'host_secret_access_key'}
    assert failure_report(ValueError('synthetic-private-value'),'request_download')=={
        'ok':False,'code':'REMOTE_BOOTSTRAP_FAILED','phase':'request_download'}
    assert failure_report(KeyboardInterrupt(),'host_secret_config')['code']=='REMOTE_CANCELLED'


def test_browser_activation_never_calls_push_and_requires_new_exact_version(case,monkeypatch,gateway_sdk):
    from fsf.remote import kaggle_v2_api as gateway
    from fsf.remote.v2_transports import notebook
    root,*_=case
    profile,request=kaggle_request(case,monkeypatch,False)
    profile['settings'].update(worker_notebook='fsf-worker-synthetic',activation_mode='agent_browser')
    class API:
        config_values={'username':'synthetic_owner'}
        def authenticate(self):pass
        def kernels_push(self,*a,**kw):pytest.fail('browser activation must not push')
    monkeypatch.setitem(sys.modules,'kaggle.api.kaggle_api_extended',SimpleNamespace(KaggleApi=API))
    locator={'artifact_id':'request','store_id':'store','object_key':'jobs/request.json','version_id':'fixed','sha256':'a'*64,'size_bytes':500,'role':'data','media_type':'application/json'}
    saved=SimpleNamespace(metadata=SimpleNamespace(is_private=True,current_version_number=4),
        blob=SimpleNamespace(source=json.dumps({'nbformat':4,'cells':[]})))
    monkeypatch.setattr(gateway,'_saved',lambda *a,**kw:saved)
    monkeypatch.setattr(gateway,'_version_status',lambda *a,**kw:SimpleNamespace(status='COMPLETE'))
    value={'operation':'prepare','request':request,'profile':profile,'locator':locator,'directory':str(root/'prepared')}
    gateway.call(value)
    value['operation']='submit'
    assert gateway.call(value)['activation_pending'] is True
    (root/'prepared/ledger.json').write_text(json.dumps({'locator':locator}))
    value['operation']='reconcile'
    assert gateway.call(value) is None
    saved.metadata.current_version_number=5
    assert gateway.call(value) is None
    saved.blob.source=json.dumps(notebook(request,locator))
    assert gateway.call(value)['version']==5
    activation=root/'prepared/browser-activation.json'
    altered=json.loads(activation.read_text());altered['gpu']=True
    activation.write_text(json.dumps(altered))
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_POLICY'):gateway.call(value)


@pytest.mark.parametrize('settings',[
    {'accelerator':'CPU','activation_mode':'unknown'},
    {'accelerator':'CPU','activation_mode':'agent_browser'},
])
def test_browser_activation_profile_requires_known_mode_and_dedicated_notebook(case,settings):
    from fsf.remote.v2 import validate_profile
    profile=copy.deepcopy(case[3]);profile['transport']='kaggle';profile['settings']=settings
    with pytest.raises(RemoteError):validate_profile(profile)


def test_waiting_browser_activation_does_not_claim_submission_or_completion(case):
    from fsf.remote.v2_dispatch import update
    root,_,store,profile,request=case
    calls=[]
    class Browser:
        observation=None
        def prepare(self,*a):calls.append('prepare')
        def submit(self,*a):calls.append('submit');return {'activation_pending':True}
        def reconcile(self,*a):return self.observation
        def status(self,*a):
            assert self.observation is not None
            return 'completed'
    adapter=Browser()
    args={'profile':profile,'store':store,'state_root':root/'state','store_config':'unused','adapter':adapter}
    first=submit(request,**args)
    assert first['status']=='awaiting_activation' and first['submitted_count']==0
    assert submit(request,**args)==first and calls==['prepare','submit']
    assert update(request['request_digest'],**args)==first
    adapter.observation={'request_digest':request['request_digest'],'version':5}
    observed=update(request['request_digest'],**args)
    assert observed['status']=='remote_completed' and observed['submitted_count']==1
    assert observed['result_digest'] is None
    assert not (root/'state/kaggle-cpu-ready.json').exists()
