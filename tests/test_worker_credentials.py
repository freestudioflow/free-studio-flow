"""Install/worker credential separation across all three launch paths."""
import ast
import copy
from datetime import datetime,timezone
from importlib import resources
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from fsf.remote.contracts import RemoteError,seal
from fsf.remote.environment import install_environment
from test_remote_v2 import case,remote_wheel


def test_installer_strips_worker_credentials_without_mutating_worker_environment():
    base={'PATH':'tools','HF_TOKEN':'synthetic-worker-secret','HTTPS_PROXY':'ambient'}
    authorized=['HF_TOKEN','FSF_REMOTE_INSTALL_HTTPS_PROXY']
    result=install_environment(base,authorized,source={'FSF_REMOTE_INSTALL_HTTPS_PROXY':'http://proxy.invalid:8080'})
    assert result=={'PATH':'tools','HTTPS_PROXY':'http://proxy.invalid:8080'}
    assert base['HF_TOKEN']=='synthetic-worker-secret' and base['HTTPS_PROXY']=='ambient'
    with pytest.raises(RemoteError,match='REMOTE_INSTALL_NETWORK_NOT_AUTHORIZED'):
        install_environment(base,['HF_TOKEN'],source={'FSF_REMOTE_INSTALL_HTTPS_PROXY':'http://proxy.invalid:8080'})


@pytest.mark.parametrize('state',['present','missing','empty','unauthorized'])
def test_actual_parent_setup_never_receives_worker_credentials(case,monkeypatch,state):
    from fsf.remote.v2 import prepare_request,run_worker
    root,_,store,profile,old=case
    recipe=copy.deepcopy(old['recipe'])
    recipe['environment']['credential_env_names']=['HF_TOKEN']
    recipe['environment']['setup']=[{'source':'https://pypi.org/simple','args':['{python}','-c',
        "import os; assert 'HF_TOKEN' not in os.environ; assert os.environ.get('HTTPS_PROXY')=='http://proxy.invalid:8080'"]}]
    recipe['steps'][0]['args']=['{python}','-c',
        "import os,json,sys,pathlib; assert os.environ.get('HF_TOKEN'); assert 'HTTPS_PROXY' not in os.environ; pathlib.Path(sys.argv[1]).write_text(json.dumps({'square':49}))",
        '{output.result}']
    profile['policy']['network_sources']=['https://pypi.org/simple']
    profile['policy']['credential_env_names']=['FSF_REMOTE_INSTALL_HTTPS_PROXY']+([] if state=='unauthorized' else ['HF_TOKEN'])
    monkeypatch.setenv('FSF_REMOTE_INSTALL_HTTPS_PROXY','http://proxy.invalid:8080')
    if state=='missing':monkeypatch.delenv('HF_TOKEN',raising=False)
    else:monkeypatch.setenv('HF_TOKEN','' if state=='empty' else 'synthetic-worker-secret')
    recipe=seal(recipe,'recipe_digest');profile['approved_recipes']=[recipe['recipe_digest']]
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=old['worker_wheel'],segment=old['segment'],output=root/'prepared')
    if state=='present':
        result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
        assert result['status']=='completed'
    else:
        code='REMOTE_CREDENTIAL_NOT_AUTHORIZED' if state=='unauthorized' else 'REMOTE_CREDENTIAL_MISSING'
        with pytest.raises(RemoteError,match=code):run_worker(request,store=store,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
        assert not (root/'worker'/request['request_digest']/'child-journal.json').exists()


def bootstrap_function(name,tmp_path,source,authorized):
    text=resources.files('fsf.tools').joinpath(name).read_text()
    function=next(n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef) and n.name=='run')
    calls=[]
    def process(args,**kwargs):
        calls.append(kwargs['env'])
        return SimpleNamespace(returncode=0,timed_out=False,stdout='',stderr='')
    module=SimpleNamespace(run_safe_subprocess=process)
    ns={'time':time,'datetime':datetime,'timezone':timezone,'os':SimpleNamespace(environ=source),
        'started':time.monotonic(),'DEADLINE':30,'deadline':time.monotonic()+30,'env':{},'root':tmp_path,
        'request':{'recipe':{'environment':{'credential_env_names':['HF_TOKEN']}},'policy':{'credential_env_names':authorized}},
        'module':module,'safety_module':module,'secret_client':None,'config_path':'operator-store.json'}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'bootstrap-run','exec'),ns)
    return ns,calls


@pytest.mark.parametrize('name',['remote_bootstrap_native.py','remote_bootstrap_v2.py'])
@pytest.mark.parametrize('state',['present','missing','empty','unauthorized'])
def test_bootstrap_paths_match_parent_required_credential_rule(name,state,tmp_path):
    source={} if state=='missing' else {'HF_TOKEN':'' if state=='empty' else 'synthetic-worker-secret'}
    ns,calls=bootstrap_function(name,tmp_path,source,[] if state=='unauthorized' else ['HF_TOKEN'])
    ns['run'](['installation'])
    assert 'HF_TOKEN' not in calls[0]
    if state=='present':
        ns['run'](['worker'],worker=True)
        assert calls[1]['HF_TOKEN']=='synthetic-worker-secret'
    else:
        code='REMOTE_CREDENTIAL_NOT_AUTHORIZED' if state=='unauthorized' else 'REMOTE_CREDENTIAL_MISSING'
        with pytest.raises(ValueError,match=code):ns['run'](['worker'],worker=True)
        assert len(calls)==1


def test_host_secret_read_failure_is_not_missing_or_invalid_token(tmp_path):
    from fsf.tools.remote_bootstrap_v2 import failure_report
    ns,calls=bootstrap_function('remote_bootstrap_v2.py',tmp_path,{},['HF_TOKEN'])
    def fail(name):raise ConnectionError('synthetic-private-service-body')
    ns['secret_client']=SimpleNamespace(get_secret=fail)
    with pytest.raises(ValueError,match='REMOTE_HOST_SECRET_ACCESS_FAILED') as exc:ns['run'](['worker'],worker=True)
    assert 'synthetic-private-service-body' not in str(exc.value) and not calls
    assert failure_report(exc.value,'worker_execution')['code']=='REMOTE_HOST_SECRET_ACCESS_FAILED'
    ns['secret_client']=SimpleNamespace(get_secret=lambda name:'synthetic-host-secret')
    ns['run'](['worker'],worker=True)
    assert calls[-1]['HF_TOKEN']=='synthetic-host-secret'
