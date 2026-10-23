"""Pinned tool preparation, immutable receipts and installation-only network."""
import copy
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from fsf.remote.contracts import RemoteError, seal, sha
from fsf.remote.environment import (prepare_environment, validate_environment, recipe_diff,
                                    install_environment, INSTALL_NETWORK, bind_environment)


def plan(tools=None):
    tool = {'tool_id':'python','mode':'preinstalled','version':platform.python_version(),
            'os':platform.system().lower(),'architecture':platform.machine().lower(),
            'archive_path':None,'archive_sha256':None,'archive_size_bytes':0,
            'expanded_limit_bytes':1024,'executable':Path(sys.executable).name,
            'version_args':['--version'],'version_pattern':r'Python [0-9.]+','smoke_args':['-c','print(1+1)']}
    return seal({'schema_version':'fsf_environment_plan_v1','python':f'3.{sys.version_info.minor}',
                 'deadline_seconds':30,'tools':[tool] if tools is None else tools},'environment_digest')


def roots(tmp_path):
    source=tmp_path/'inputs'; source.mkdir()
    out=tmp_path/'installed'; out.mkdir()
    return source,out


def test_real_preinstalled_python_and_revalidation(tmp_path,monkeypatch):
    source,out=roots(tmp_path)
    monkeypatch.setenv('PATH',str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''))
    p=plan()
    first=prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)
    second=prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)
    assert first==second
    assert first['tools'][0]['binary_sha256']==sha(sys.executable)
    assert str(tmp_path) not in json.dumps(first)


@pytest.mark.parametrize('field,value,code',[
    ('version','0.0.1','REMOTE_TOOL_VERSION'),
    ('os','windows' if platform.system().lower()=='linux' else 'linux','REMOTE_TOOL_PLATFORM'),
    ('executable','missing-synthetic-tool-081','REMOTE_TOOL_MISSING'),
])
def test_preinstalled_rejections_do_not_publish(tmp_path,monkeypatch,field,value,code):
    source,out=roots(tmp_path)
    monkeypatch.setenv('PATH',str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''))
    p=plan();p['tools'][0][field]=value;p=seal(p,'environment_digest')
    with pytest.raises(RemoteError,match=code):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)
    assert not (out/p['environment_digest']).exists()
    assert not list(out.glob('.environment-*'))


def archive_plan(source,entries):
    path=source/'tools.zip'
    with zipfile.ZipFile(path,'w') as z:
        for name,data in entries: z.writestr(name,data)
    tool=plan()['tools'][0]
    tool.update(mode='archive',version='1.2.3',archive_path='tools.zip',archive_sha256=sha(path),
                archive_size_bytes=path.stat().st_size,executable='bin/tool',
                version_pattern=r'Tool [0-9.]+',smoke_args=[])
    return plan([tool])


def test_archive_install_and_changed_disk_rejected(tmp_path,monkeypatch):
    source,out=roots(tmp_path);p=archive_plan(source,[('bin/tool',b'fake executable')])
    monkeypatch.setattr('fsf.remote.environment.run_safe_subprocess',lambda *a,**k:SimpleNamespace(stdout='Tool 1.2.3',returncode=0,timed_out=False))
    receipt=prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)
    assert receipt['installed_files']=={'python/bin/tool':sha(out/p['environment_digest']/'tools/python/bin/tool')}
    (out/p['environment_digest']/'tools/python/bin/tool').write_bytes(b'changed')
    with pytest.raises(RemoteError,match='REMOTE_ENVIRONMENT_CHANGED'):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)


@pytest.mark.parametrize('mutation,code',[
    ('hash','REMOTE_TOOL_INTEGRITY'),('size','REMOTE_TOOL_INTEGRITY'),
    ('limit','REMOTE_TOOL_ARCHIVE_LIMIT'),('path',''),('timeout','REMOTE_ENVIRONMENT_TIMEOUT'),
])
def test_archive_counterexamples(tmp_path,mutation,code):
    source,out=roots(tmp_path);p=archive_plan(source,[('../outside' if mutation=='path' else 'bin/tool',b'12345678')])
    if mutation=='hash':p['tools'][0]['archive_sha256']='0'*64
    if mutation=='size':p['tools'][0]['archive_size_bytes']+=1
    if mutation=='limit':p['tools'][0]['expanded_limit_bytes']=1
    p=seal(p,'environment_digest')
    with pytest.raises((RemoteError,ValueError),match=code):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out,
                            remaining_seconds=0 if mutation=='timeout' else None)
    assert not (out/p['environment_digest']).exists()
    assert not (tmp_path/'outside').exists()


def test_tar_symlink_rejected(tmp_path):
    source,out=roots(tmp_path);p=archive_plan(source,[('bin/tool',b'x')])
    path=source/'tool.tar'
    with tarfile.open(path,'w') as archive:
        item=tarfile.TarInfo('link');item.type=tarfile.SYMTYPE;item.linkname='../outside';archive.addfile(item)
    p['tools'][0].update(archive_path='tool.tar',archive_sha256=sha(path),archive_size_bytes=path.stat().st_size)
    p=seal(p,'environment_digest')
    with pytest.raises(RemoteError,match='REMOTE_TOOL_ARCHIVE_LINK'):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out)


def test_wrong_plan_digest_and_cancel_do_not_run_tools(tmp_path,monkeypatch):
    source,out=roots(tmp_path);p=plan()
    monkeypatch.setattr('fsf.remote.environment.run_safe_subprocess',lambda *a,**k:pytest.fail('tool started'))
    with pytest.raises(RemoteError,match='REMOTE_ENVIRONMENT_DIGEST'):
        prepare_environment(p,expected_digest='0'*64,input_root=source,output_root=out)
    with pytest.raises(RemoteError,match='REMOTE_CANCELLED'):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out,cancelled=lambda:True)


def test_network_settings_are_scoped_and_authorized():
    secret='http://synthetic-user:synthetic-password@127.0.0.1:8999'
    source={'FSF_REMOTE_INSTALL_HTTPS_PROXY':secret}
    original={'PATH':'synthetic'}
    with pytest.raises(RemoteError,match='REMOTE_INSTALL_NETWORK_NOT_AUTHORIZED'):
        install_environment(original,[],source=source)
    installed=install_environment(original,list(INSTALL_NETWORK),source=source)
    assert installed['HTTPS_PROXY']==secret
    assert original=={'PATH':'synthetic'}
    assert not any(k.startswith('FSF_REMOTE_INSTALL_') for k in installed)


def test_probe_timeout_uses_shared_environment_budget(tmp_path,monkeypatch):
    source,out=roots(tmp_path);p=plan()
    seen=[]
    def timeout(*args,**kwargs):
        seen.append(kwargs['timeout_seconds'])
        return SimpleNamespace(stdout='',returncode=1,timed_out=True)
    monkeypatch.setattr('fsf.remote.environment.run_safe_subprocess',timeout)
    monkeypatch.setattr('fsf.remote.environment.shutil.which',lambda name:sys.executable)
    with pytest.raises(RemoteError,match='REMOTE_ENVIRONMENT_TIMEOUT'):
        prepare_environment(p,expected_digest=p['environment_digest'],input_root=source,output_root=out,remaining_seconds=1)
    assert len(seen)==1 and 0<seen[0]<=1


def test_recipe_diff_does_not_approve_or_echo_values():
    from importlib import resources
    original=json.loads(resources.files('fsf.remote.templates').joinpath('cpu.recipe.json').read_text('utf-8'))
    changed=copy.deepcopy(original);changed['environment']['python']='3.13';changed=seal(changed,'recipe_digest')
    result=recipe_diff(original,changed)
    assert result['changed_paths']==['/environment/python']
    assert result['approval_required'] and not result['automatic_approval']
    assert original['recipe_digest']!=result['after_digest']


def test_bound_recipe_changes_digest_but_not_original():
    from importlib import resources
    original=json.loads(resources.files('fsf.remote.templates').joinpath('cpu.recipe.json').read_text('utf-8'))
    before=copy.deepcopy(original)
    bound=bind_environment(original,plan([]))
    assert original==before
    assert bound['recipe_digest']!=original['recipe_digest']
    assert bound['environment']['setup'][0]['source']=='fsf:bundled-tools'
    assert bound['inputs']==[{'artifact_id':'environment_plan','role':'data','media_type':'application/json'}]


def test_real_worker_environment_preparation(remote_case):
    from fsf.remote.bundle import prepare_bundle,unpack
    from fsf.remote.contracts import write_json
    from fsf.remote.worker import run_worker
    root,recipe,profile,definition,wheel=remote_case
    p=plan([])
    recipe=bind_environment(recipe,p)
    write_json(root/'inputs/environment.json',p)
    definition['inputs']={'environment_plan':'environment.json'}
    profile['approved_recipes']=[recipe['recipe_digest']]
    profile['policy']['network_sources'].append('fsf:bundled-tools')
    prepared=prepare_bundle(definition,recipe,inputs_root=root/'inputs',wheel=wheel,profile=profile,output_root=root/'state')
    result=run_worker(root/'state'/prepared['bundle'],output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
    assert result['status']=='completed'
    report=json.loads((root/'worker'/result['request_digest']/'environment.json').read_text())
    assert report['prepared_tools'][0]['environment_digest']==p['environment_digest']


from test_remote_dispatch import remote_case,remote_wheel
