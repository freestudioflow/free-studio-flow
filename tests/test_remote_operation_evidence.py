"""Unknown submissions, private diagnostics, wheel inspection and routing."""
import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import zipfile

import pytest

from fsf.remote.contracts import RemoteError, seal, write_json
from fsf.remote.diagnostics import safe_text, now, record, inspect
from fsf.remote.destination import resolve, authorize_create, require_secret_capability
from fsf.remote.v2 import validate_profile, verify_worker_wheel
from fsf.remote.v2_dispatch import submit,update
from test_remote_v2 import case,remote_wheel,Adapter


@pytest.mark.parametrize('secret',['synthetic-boundary-secret','ghs_'+'x'*40])
def test_redaction_precedes_utf8_cut_and_masks_urls_paths(secret):
    text='a'*9000+'Bearer '+secret+' https://example.invalid/?key='+secret+' /private/work/file '+chr(0)
    actual=safe_text(text,env={'API_TOKEN':secret})
    assert secret not in actual and 'example.invalid' not in actual and '/private/work' not in actual
    assert len(actual.encode())<=8192 and chr(0) not in actual


def test_report_timing_exit_tail_integrity_and_replay(tmp_path):
    started=now();clock=time.monotonic()
    record(tmp_path,request_id='r',phase='install',started_at=started,started_clock=clock,
        operation_status='failed',result=SimpleNamespace(returncode=7,timed_out=False,stdout='ok',stderr='S2_CUSTOM_FAILURE'))
    first=(tmp_path/'operation-reports/00000001.json').read_bytes()
    record(tmp_path,request_id='r',phase='status',started_at=now(),started_clock=time.monotonic(),operation_status='succeeded')
    values=inspect(tmp_path)
    assert (tmp_path/'operation-reports/00000001.json').read_bytes()==first
    assert values[0]['timing']['returncode']==7 and values[0]['stderr_tail']=='S2_CUSTOM_FAILURE'
    assert values[1]['timing']['returncode'] is None
    assert values[0]['timing']['started_at']==started
    path=tmp_path/'operation-reports/00000001.json'
    value=json.loads(path.read_text());value['safe_code']='tampered';write_json(path,value)
    with pytest.raises(RemoteError,match='REMOTE_INTEGRITY'):inspect(tmp_path)


def test_lost_submit_diagnostic_survives_status_without_redispatch(case):
    root,_,store,profile,request=case
    adapter=Adapter(store,root);adapter.lose=True
    first=submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused',adapter=adapter)
    assert first['status']=='submission_unknown'
    directory=root/'state'/request['request_digest']
    evidence=inspect(directory)
    assert evidence[-1]['submission_status']=='unknown' and evidence[-1]['safe_code']=='REMOTE_OPERATION_FAILED'
    frozen=(directory/'operation-reports/00000001.json').read_bytes()
    update(request['request_digest'],profile=profile,store=store,state_root=root/'state',store_config='unused',adapter=adapter)
    assert adapter.count==1 and (directory/'operation-reports/00000001.json').read_bytes()==frozen


def test_aliases_never_silently_choose_destination():
    profile={'transport':'kaggle','settings':{'native_notebook':'fsf-worker-one','worker_notebook':'fsf-worker-two'}}
    with pytest.raises(RemoteError,match='REMOTE_DESTINATION_CONFLICT'):resolve(profile)
    profile['settings']['worker_notebook']='fsf-worker-one'
    assert resolve(profile)['slug']=='fsf-worker-one'


@pytest.mark.parametrize('proof',[None,{'status':404},{'state':'absent'},{'owner':'other','slug':'fsf-worker-new','state':'absent','can_create_private':True}])
def test_first_slot_requires_positive_scoped_evidence(proof):
    profile={'transport':'kaggle','settings':{'worker_notebook':'fsf-worker-new'},
             'destination':{'intent':'create_private','model_sources':[],'secret_aliases':[]}}
    with pytest.raises(RemoteError,match='REMOTE_DESTINATION_UNVERIFIED'):authorize_create(profile,owner='synthetic-owner',proof=proof)


def test_local_positive_first_slot_policy_and_secret_unavailable():
    profile={'transport':'kaggle','settings':{'worker_notebook':'fsf-worker-new'},
             'destination':{'intent':'create_private','model_sources':[],'secret_aliases':[]}}
    proof={'owner':'synthetic-owner','slug':'fsf-worker-new','state':'absent','can_create_private':True}
    assert authorize_create(profile,owner='synthetic-owner',proof=proof)==proof
    profile['destination']['secret_aliases']=['HOST_TOKEN']
    with pytest.raises(RemoteError,match='REMOTE_SECRET_CAPABILITY_UNAVAILABLE'):require_secret_capability(profile)


def test_parent_checks_wheel_without_import(case,tmp_path):
    _,_,_,_,request=case
    bad=tmp_path/'bad.whl'
    with zipfile.ZipFile(bad,'w') as archive:
        archive.writestr('../escape.py','raise AssertionError()')
    with pytest.raises(RemoteError,match='REMOTE_WHEEL'):verify_worker_wheel(bad,request['worker_wheel'],request['worker_core_sha256'])


def test_profile_v4_sources_are_version_pinned(case):
    _,_,store,profile,_=case
    value=copy.deepcopy(profile);value['schema_version']='fsf_remote_profile_v4'
    value['allow_recipe_experiments']=True
    value['destination']={'intent':'existing','model_sources':[],'secret_aliases':[]}
    assert validate_profile(value,store)['destination']==value['destination']
    value['transport']='kaggle';value['settings']={'accelerator':'synthetic-shape','native_notebook':'fsf-worker-test'}
    value['destination']['model_sources']=['owner/model/framework/variant/latest']
    with pytest.raises(RemoteError):validate_profile(value,store)
    value['destination']['model_sources']=['owner/model/framework/variant/1']
    assert validate_profile(value,store)['destination']['model_sources'][-1].endswith('/1')


def test_worker_failure_has_real_exit_and_new_family_without_secret(case):
    from fsf.remote.v2 import prepare_request,run_worker
    root,_,store,profile,old=case
    recipe=copy.deepcopy(old['recipe'])
    recipe['steps'][0]['args']=['{python}','-c',"import sys; print('CUSTOM_ENGINE_FAILED',file=sys.stderr); sys.exit(7)"]
    recipe=seal(recipe,'recipe_digest');profile['approved_recipes']=[recipe['recipe_digest']]
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
    assert result['status']=='failed'
    reports=inspect(root/'worker'/request['request_digest'])
    failure=[r for r in reports if r['timing']['returncode']==7]
    assert len(failure)==1 and 'CUSTOM_ENGINE_FAILED' in failure[0]['stderr_tail']
    environment=json.loads((root/'worker'/request['request_digest']/'environment.json').read_text())
    assert 'CUSTOM_ENGINE_FAILED' in environment['command_failures'][0]['codes']
    from fsf.remote.v2 import collect_diagnostics
    evidence=collect_diagnostics(request,store=store,directory=root/'accepted')
    assert evidence['media_accepted'] is False and not (root/'accepted/accepted').exists()
    recovered=json.loads((root/'accepted'/evidence['path']).read_text())
    assert recovered['reports'][-1]['timing']['returncode']==7


def test_gateway_clears_stale_reply_and_records_real_failed_call(case,monkeypatch):
    import contextlib
    from fsf.remote.v2_transports import V2Adapter
    root,_,_,profile,request=case
    target=root/'prepared'
    write_json(target/'provider-v2-response.json',{'ok':True,'result':{'old':True}})
    @contextlib.contextmanager
    def credentials():yield {}
    monkeypatch.setattr('fsf.live.credentials.kaggle_credential_environment',credentials)
    def child(*args,**kwargs):
        assert not (target/'provider-v2-response.json').exists()
        return SimpleNamespace(returncode=9,timed_out=False,stdout='',stderr='NEW_WORKER_FAILURE')
    monkeypatch.setattr('fsf.executors.subprocess_safety.run_safe_subprocess',child)
    adapter=V2Adapter(profile,store_config=root/'missing-config')
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_API_FAILED'):adapter._kaggle('submit',request,{},target)
    evidence=inspect(target)
    assert evidence[0]['timing']['returncode']==9 and evidence[0]['submission_status']=='unknown'


@pytest.mark.parametrize('cancel',[True,False])
def test_worker_interruption_reports_unknown_exit_instead_of_zero(case,monkeypatch,cancel):
    from fsf.remote.v2 import run_worker
    root,_,store,_,request=case
    def interrupted(*args,**kwargs):
        if cancel:raise KeyboardInterrupt()
        raise RuntimeError('PROCESS_OUTPUT_LIMIT')
    monkeypatch.setattr('fsf.remote.v2.run_safe_subprocess',interrupted)
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    assert result['status']==('cancelled' if cancel else 'failed')
    reports=inspect(root/'worker'/request['request_digest'])
    assert reports[-1]['timing']['returncode'] is None
    assert reports[-1]['timing']['missing_reason']=='subprocess_result_not_observed'
    assert reports[-1]['safe_code']==('REMOTE_CANCELLED' if cancel else 'REMOTE_COMMAND_OUTPUT_LIMIT')


def test_matching_core_does_not_allow_root_site_hook(case,tmp_path):
    _,_,_,_,request=case
    path=tmp_path/'hostile.whl'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('unexpected.pth','import malicious_code')
    with pytest.raises(RemoteError,match='REMOTE_WHEEL'):verify_worker_wheel(path,request['worker_wheel'],request['worker_core_sha256'])


def test_native_bootstrap_only_forwards_explicit_worker_credentials(tmp_path,monkeypatch):
    import ast
    import os
    from importlib import resources
    from datetime import datetime,timezone
    code=resources.files('fsf.tools').joinpath('remote_bootstrap_native.py').read_text()
    tree=ast.parse(code)
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='run')
    calls=[]
    def process(args,**kwargs):
        calls.append(kwargs['env'])
        return SimpleNamespace(returncode=0,timed_out=False,stderr='',stdout='')
    monkeypatch.setenv('HF_TOKEN','synthetic-host-secret')
    namespace={'time':time,'os':os,'datetime':datetime,'timezone':timezone,'DEADLINE':10,'started':time.monotonic(),
               'root':tmp_path,'env':{},'request':{'recipe':{'environment':{'credential_env_names':['HF_TOKEN']}},
                'policy':{'credential_env_names':['HF_TOKEN']}},'safety_module':SimpleNamespace(run_safe_subprocess=process)}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'bootstrap-function','exec'),namespace)
    namespace['run'](['installation'])
    namespace['run'](['worker'],worker=True)
    assert 'HF_TOKEN' not in calls[0] and calls[1]['HF_TOKEN']=='synthetic-host-secret'
    namespace['request']['policy']['credential_env_names']=[]
    with pytest.raises(ValueError,match='REMOTE_CREDENTIAL_NOT_AUTHORIZED'):namespace['run'](['worker'],worker=True)
    assert len(calls)==2
