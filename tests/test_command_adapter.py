"""A new provider can implement the protocol without modifying FSF."""
import json
from pathlib import Path
import sys
import pytest
from fsf.remote.command_adapter import CommandAdapter
from fsf.remote.contracts import RemoteError, sha
from test_kaggle_native import native, case, remote_wheel


def adapter(tmp_path):
    script=tmp_path/'provider.py'
    script.write_text('''import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text());op=r['operation']
value='completed' if op=='status' else 'requested' if op=='cancel' else {'provider_job':'one'}
Path(sys.argv[2]).write_text(json.dumps({'schema_version':'fsf_command_adapter_reply_v1',
'request_digest':r['request']['request_digest'],'operation':op,'result':value}))
''')
    profile={'settings':{'command':[sys.executable,str(script)],'files':[
        {'path':sys.executable,'sha256':sha(Path(sys.executable))},
        {'path':str(script),'sha256':sha(script)}], 'cwd':str(tmp_path),
        'timeout_seconds':5,'credential_env_names':[]}}
    return CommandAdapter(profile,tmp_path/'store.json'),script


def test_custom_provider_complete_lifecycle(tmp_path):
    a,_=adapter(tmp_path);request={'request_digest':'a'*64}
    for op in ('prepare','submit','status','reconcile','fetch','cancel'):
        result=a.call(op,request,None,tmp_path/'state')
        assert result==('completed' if op=='status' else 'requested' if op=='cancel' else {'provider_job':'one'})


@pytest.mark.parametrize('escaped',[False,True])
def test_adapter_reply_cannot_persist_known_environment_secret(tmp_path,monkeypatch,escaped):
    a,script=adapter(tmp_path)
    script.write_text('''import json,os,sys
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
reply=dict(schema_version='fsf_command_adapter_reply_v1',request_digest=r['request']['request_digest'],operation=r['operation'],result=dict(provider_job=os.environ['CUSTOM_TEST_SECRET']))
raw=json.dumps(reply)
if '''+repr(escaped)+''':raw=raw.replace('sentinel','\\\\u0073entinel')
Path(sys.argv[2]).write_text(raw)
''')
    a.settings['files'][1]['sha256']=sha(script)
    a.settings['credential_env_names']=['CUSTOM_TEST_SECRET']
    monkeypatch.setenv('CUSTOM_TEST_SECRET','sentinel-only-synthetic')
    directory=tmp_path/'state'
    with pytest.raises(RemoteError,match='REMOTE_SECRET_OUTPUT'):
        a.call('submit',{'request_digest':'a'*64},None,directory)
    assert not (directory/'adapter-submit-response.json').exists()


def test_hold_stops_before_custom_program_and_mutation_refuses(tmp_path):
    a,script=adapter(tmp_path);directory=tmp_path/'state';directory.mkdir()
    (directory/'submit-hold.json').write_text('{}')
    with pytest.raises(RemoteError,match='REMOTE_SUBMIT_HELD'):
        a.call('submit',{'request_digest':'a'*64},None,directory)
    assert not (directory/'adapter-submit-request.json').exists()
    script.write_text('raise SystemExit(0)')
    with pytest.raises(RemoteError,match='REMOTE_ADAPTER_CHANGED'):
        a.call('status',{'request_digest':'a'*64},None,directory)


def test_bad_identity_and_raw_secret_error_are_not_exposed(tmp_path):
    a,script=adapter(tmp_path)
    script.write_text('import sys;print("secret-sentinel",file=sys.stderr);raise SystemExit(1)')
    a.settings['files'][1]['sha256']=sha(script)
    with pytest.raises(RemoteError,match='REMOTE_ADAPTER_FAILED') as exc:
        a.call('prepare',{'request_digest':'a'*64},None,tmp_path/'state')
    assert 'sentinel' not in str(exc.value)


def test_v2_adapter_routes_all_custom_operations_and_old_profile_refuses(native,tmp_path):
    from fsf.remote.v2_transports import V2Adapter
    from fsf.remote.v2 import validate_profile
    _,_,profile,request=native
    a,_=adapter(tmp_path)
    profile.update(schema_version='fsf_remote_profile_v3',transport='command',settings=a.settings)
    routed=V2Adapter(profile,store_config=tmp_path/'store.json')
    directory=tmp_path/'calls'
    assert routed.prepare(request,None,directory)=={'provider_job':'one'}
    assert routed.submit(request,None,directory)=={'provider_job':'one'}
    assert routed.status(request,{},directory)=='completed'
    assert routed.reconcile(request,None,directory)=={'provider_job':'one'}
    assert routed.fetch_into_store(request,{},directory)=={'provider_job':'one'}
    assert routed.cancel(request,{},directory)=='requested'
    profile['schema_version']='fsf_remote_profile_v2'
    with pytest.raises(RemoteError):validate_profile(profile)
