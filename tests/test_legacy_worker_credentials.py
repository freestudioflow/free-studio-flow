"""Legacy launch paths retain the same install/worker secret boundary."""
import ast
import json
from importlib import resources
import time
from types import SimpleNamespace

import pytest

from fsf.remote.contracts import seal
from fsf.remote.worker import run_worker
from test_remote_dispatch import remote_case,remote_wheel,prepare


@pytest.mark.parametrize('state',['present','missing','empty','unauthorized'])
def test_legacy_worker_required_credentials_and_install_isolation(remote_case,monkeypatch,state):
    root,recipe,profile,definition,wheel=remote_case
    recipe['environment']['credential_env_names']=['HF_TOKEN']
    recipe['environment']['setup']=[{'source':'https://pypi.org/simple','args':['{python}','-c',
        "import os; assert 'HF_TOKEN' not in os.environ; assert os.environ.get('HTTPS_PROXY')=='http://proxy.invalid:8080'"]}]
    recipe['steps'][0]['args']=['{python}','-c',
        "import os,pathlib,sys,json; assert os.environ.get('HF_TOKEN'); assert 'HTTPS_PROXY' not in os.environ; pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(square=49)))",'{output.result}']
    profile['policy']['credential_env_names']=['FSF_REMOTE_INSTALL_HTTPS_PROXY','HF_TOKEN']
    profile['policy']['network_sources']=['https://pypi.org/simple']
    monkeypatch.setenv('FSF_REMOTE_INSTALL_HTTPS_PROXY','http://proxy.invalid:8080')
    if state=='missing':monkeypatch.delenv('HF_TOKEN',raising=False)
    else:monkeypatch.setenv('HF_TOKEN','' if state=='empty' else 'synthetic-worker-secret')
    recipe=seal(recipe,'recipe_digest');profile['approved_recipes']=[recipe['recipe_digest']]
    _,bundle=prepare((root,recipe,profile,definition,wheel))
    if state=='unauthorized':
        # Exercise the worker's independent check with a reduced-permission
        # incoming request. Preserve the original prepared archive.
        from fsf.remote.bundle import unpack,pack
        from fsf.remote.contracts import write_json
        folder=root/'reduced-policy';folder.mkdir()
        names=unpack(bundle,folder)
        request=json.loads((folder/'request.json').read_text())
        request['policy']['credential_env_names']=['FSF_REMOTE_INSTALL_HTTPS_PROXY']
        write_json(folder/'request.json',seal(request,'request_digest'))
        bundle=root/'reduced-policy.zip';pack(folder,sorted(names),bundle)
    result=run_worker(bundle,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
    if state=='present':assert result['status']=='completed'
    else:
        assert result['status']=='failed'
        assert result['error_code']==('REMOTE_CREDENTIAL_NOT_AUTHORIZED' if state=='unauthorized' else 'REMOTE_CREDENTIAL_MISSING')
        manifest=json.loads((root/'worker'/result['request_digest']/'result.json').read_text())
        assert not manifest['artifacts']


@pytest.mark.parametrize('state',['present','missing','empty','unauthorized'])
def test_legacy_bootstrap_install_isolation_and_worker_rejection(tmp_path,state):
    text=resources.files('fsf.tools').joinpath('remote_bootstrap.py').read_text()
    function=next(n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef) and n.name=='run')
    source={} if state=='missing' else {'HF_TOKEN':'' if state=='empty' else 'synthetic-worker-secret'}
    source['FSF_REMOTE_INSTALL_HTTPS_PROXY']='http://proxy.invalid:8080'
    calls=[]
    def process(args,**kwargs):
        calls.append(kwargs['env'])
        return SimpleNamespace(returncode=0,timed_out=False)
    ns={'time':time,'os':SimpleNamespace(environ=source),'deadline':time.monotonic()+30,
        'env':{'SSL_CERT_FILE':'ambient-ca'},'phase':'install','root':tmp_path,'key':'a'*64,
        'request':{'recipe':{'environment':{'credential_env_names':['HF_TOKEN']}},
            'policy':{'credential_env_names':['FSF_REMOTE_INSTALL_HTTPS_PROXY']+([] if state=='unauthorized' else ['HF_TOKEN'])}},
        'install_network':{'FSF_REMOTE_INSTALL_HTTPS_PROXY':'HTTPS_PROXY','FSF_REMOTE_INSTALL_CA':'SSL_CERT_FILE'},
        'safety':SimpleNamespace(run_safe_subprocess=process)}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'legacy-bootstrap-run','exec'),ns)
    ns['run'](['installation'])
    assert 'HF_TOKEN' not in calls[0] and 'SSL_CERT_FILE' not in calls[0]
    assert calls[0]['HTTPS_PROXY']=='http://proxy.invalid:8080'
    ns['phase']='worker'
    if state=='present':
        ns['run'](['worker']);assert calls[1]['HF_TOKEN']=='synthetic-worker-secret'
    else:
        code='REMOTE_CREDENTIAL_NOT_AUTHORIZED' if state=='unauthorized' else 'REMOTE_CREDENTIAL_MISSING'
        with pytest.raises(ValueError,match=code):ns['run'](['worker'])
        assert len(calls)==1
