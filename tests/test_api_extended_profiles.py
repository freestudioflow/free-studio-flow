"""New operator limits are not inferred from provider or template names."""
import pytest
import time
import hashlib
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from test_model_api_capabilities import api_server, profile_for
from fsf.remote.model_api import validate_profile,run_api
from fsf.remote.contracts import RemoteError,seal,write_json
from fsf.core.edit_store import read_json


def test_extended_profile_accepts_non_template_limits_and_keeps_v1_strict():
    p=profile_for('video','https://operator.example')
    p.update(schema_version='fsf_model_api_profile_v2',max_tokens=32768,timeout_seconds=1800,
        input_max_bytes=16777216,request_max_bytes=33554432)
    p['model']='operator/unlisted-video-model'
    p['output'].update(max_dimension=16384,max_duration_seconds=7200)
    assert validate_profile(p)==p
    p['schema_version']='fsf_model_api_profile_v1'
    with pytest.raises(RemoteError):validate_profile(p)


def test_unlisted_http_model_over_template_token_limit_really_sends(api_server,tmp_path,monkeypatch):
    state,url=api_server;state['payload']=b'custom model result'
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p=profile_for('text',url)
    p.update(schema_version='fsf_model_api_profile_v2',model='unlisted/operator-model',max_tokens=32768,
        input_max_bytes=16777216,request_max_bytes=33554432)
    p['parameter_schema']={'type':'object','properties':{'max_tokens':{'type':'integer'}},'additionalProperties':False}
    p['http']['body']={'model':'{model}','prompt':'{prompt}','max_tokens':'{params.max_tokens}'}
    request={'schema_version':'fsf_model_api_request_v1','request_id':'custom','prompt':'test',
        'parameters':{'max_tokens':16384},'inputs':{},'expected_media_contract':None}
    (tmp_path/'state').mkdir()
    result=run_api(request,p,inputs_root=tmp_path,state_root=tmp_path/'state')
    assert result['status']=='completed' and state['body']['max_tokens']==16384


def test_large_url_download_is_streamed_and_failed_partial_is_removed(tmp_path,monkeypatch):
    from fsf.remote.model_api import _http
    from pathlib import Path
    total=65*1024*1024
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            self.send_response(200);self.end_headers()
            try:
                for _ in range(65):self.wfile.write(b'x'*1024*1024)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    p=profile_for('video',url)
    p.update(schema_version='fsf_model_api_profile_v2',input_max_bytes=1024,request_max_bytes=1024)
    p['http']['response_mode']='url';p['artifact_hosts']=['127.0.0.1']
    p['output']['max_bytes']=total
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    import tempfile
    monkeypatch.setattr(tempfile,'tempdir',str(tmp_path))
    try:
        validate_profile(p)
        path=_http(p,'GET','',deadline=time.monotonic()+30,artifact_url=url+'/artifact')
        assert isinstance(path,Path) and path.stat().st_size==total
        path.unlink()
        p['output']['max_bytes']=1024
        with pytest.raises(RemoteError,match='API_OUTPUT_LIMIT'):
            _http(p,'GET','',deadline=time.monotonic()+30,artifact_url=url+'/artifact')
        assert not list(tmp_path.glob('fsf-api-download-*'))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def async_profile(endpoint):
    p=profile_for('text',endpoint)
    p.update(schema_version='fsf_model_api_profile_v2',input_max_bytes=1024,request_max_bytes=4096,
             timeout_seconds=1,observation_timeout_seconds=5)
    p['http'].update(job_id_pointer='/job_id',status_pointer='/status',response_mode='text')
    p['http']['routes'].update(status='/jobs/{job_id}',reconcile='/requests/{request_id}',cancel='/jobs/{job_id}/cancel')
    request={'schema_version':'fsf_model_api_request_v1','request_id':'async-custom','prompt':'test',
             'parameters':{},'inputs':{},'expected_media_contract':None}
    return p,request


@pytest.mark.parametrize('failure',['API_AUTH_MISSING','API_SECRET_EXPIRED','API_SECRET_UNAVAILABLE'])
@pytest.mark.parametrize('version',['v1','v2'])
def test_configured_credential_resumes_prepared_intent_without_resetting_budget(api_server,tmp_path,monkeypatch,failure,version):
    from fsf.remote import model_api as module
    state,url=api_server;p,r=async_profile(url);p['timeout_seconds']=60
    if version=='v1':
        p['schema_version']='fsf_model_api_profile_v1'
        for name in ('input_max_bytes','request_max_bytes','observation_timeout_seconds'):p.pop(name)
    def unavailable(_):raise RemoteError(failure)
    monkeypatch.setattr(module,'credential_value',unavailable)
    with pytest.raises(RemoteError,match=failure):run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    path=next(tmp_path.glob('api_*/ledger.json'));before=read_json(path)
    assert before['status']=='prepared' and before['submission_started'] is False and state['calls']==0
    monkeypatch.setattr(module,'credential_value',lambda _:'synthetic-contract-credential')
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False,recheck=True)['status']=='running'
    assert state['calls']==1 and read_json(path)['created_at']==before['created_at']


@pytest.mark.parametrize('version',['v1','v2'])
def test_missing_credential_during_poll_preserves_known_job(api_server,tmp_path,monkeypatch,version):
    from fsf.remote import model_api as module
    state,url=api_server;p,r=async_profile(url)
    if version=='v1':
        p['schema_version']='fsf_model_api_profile_v1';p['timeout_seconds']=60
        for name in ('input_max_bytes','request_max_bytes','observation_timeout_seconds'):p.pop(name)
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False)
    original=module.credential_value
    monkeypatch.setattr(module,'credential_value',lambda _:None)
    with pytest.raises(RemoteError,match='API_AUTH_MISSING'):run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='fetch')
    ledger=read_json(next(tmp_path.glob('api_*/ledger.json')))
    assert ledger['job_id']=='job1' and ledger['status']=='running'
    monkeypatch.setattr(module,'credential_value',original)
    state['poll_response']={'job_id':'job1','status':'completed','output':'valid output'}
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='fetch')['status']=='completed'
    assert state['calls']==1


@pytest.mark.parametrize('changed',['job2',None,123,{},''])
def test_reconcile_cannot_switch_or_destroy_known_provider_identity(api_server,tmp_path,monkeypatch,changed):
    state,url=api_server;p,r=async_profile(url)
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False)
    state['poll_response']={'job_id':changed,'status':'running'}
    with pytest.raises(RemoteError,match='API_JOB_ID_'):run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='reconcile')
    assert read_json(next(tmp_path.glob('api_*/ledger.json')))['job_id']=='job1'
    assert state['calls']==1


def test_published_receipt_recovers_ledger_crash_without_second_post(api_server,tmp_path,monkeypatch):
    from fsf.remote import model_api as module
    state,url=api_server;p,r=async_profile(url)
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    state.update(response={'job_id':'job1'},poll_response={'status':'completed','job_id':'job1','output':'valid output'})
    original=module.write_json;fired=[]
    def injected(path,value):
        if str(path).endswith('ledger.json') and value.get('status')=='completed' and not fired:
            fired.append(True);raise OSError('injected publication crash')
        return original(path,value)
    monkeypatch.setattr(module,'write_json',injected)
    with pytest.raises(RemoteError):run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    monkeypatch.setattr(module,'write_json',original)
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='fetch')['status']=='completed'
    assert fired and state['calls']==1


def expire_submission(root):
    path=next(root.glob('api_*/ledger.json'))
    value=read_json(path);value['created_at']=time.time()-3600
    write_json(path,seal(value,'ledger_digest'))
    return path


def test_expired_async_job_can_fetch_without_resubmitting(api_server,tmp_path,monkeypatch):
    state,url=api_server;monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p,r=async_profile(url)
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False)['status']=='running'
    expire_submission(tmp_path)
    state['poll_response']={'status':'completed','output':'late valid output','job_id':'job1'}
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='fetch')['status']=='completed'
    assert state['calls']==1


def test_unknown_submit_reconciles_after_initial_deadline_without_another_post(api_server,tmp_path,monkeypatch):
    state,url=api_server;monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p,r=async_profile(url);state['http_status']=500
    with pytest.raises(RemoteError,match='API_SUBMISSION_UNKNOWN'):
        run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    expire_submission(tmp_path)
    state['poll_response']={'job_id':'job1','status':'completed','output':'reconciled result'}
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='reconcile')['status']=='completed'
    assert state['calls']==1


@pytest.mark.parametrize('failure',['API_DEADLINE_EXCEEDED','API_CONNECTION_UNKNOWN'])
def test_observation_failure_retains_job_for_later_fetch(api_server,tmp_path,monkeypatch,failure):
    import fsf.remote.model_api as module
    state,url=api_server;monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p,r=async_profile(url)
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False)
    path=expire_submission(tmp_path);original=module._http
    def fail_observation(*args,**kwargs):raise RemoteError(failure)
    monkeypatch.setattr(module,'_http',fail_observation)
    with pytest.raises(RemoteError,match=failure):
        run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='status',wait=False)
    current=read_json(path)
    assert current['status']=='running' and current['job_id']=='job1'
    monkeypatch.setattr(module,'_http',original)
    state['poll_response']={'status':'completed','output':'retried observation','job_id':'job1'}
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='fetch')['status']=='completed'
    assert state['calls']==1


def test_cancel_has_fresh_budget_and_terminal_state_cannot_be_reconciled_alive(api_server,tmp_path,monkeypatch):
    state,url=api_server;monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p,r=async_profile(url)
    state.update(response={'job_id':'job1'},poll_response={'status':'running','job_id':'job1'})
    run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,wait=False)
    path=expire_submission(tmp_path)
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='cancel')['status']=='cancel_requested'
    state['poll_response']={'status':'cancelled','job_id':'job1'}
    with pytest.raises(RemoteError,match='API_PROVIDER_FAILED'):
        run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='status',wait=False)
    assert read_json(path)['status']=='cancelled'
    state['poll_response']={'status':'running','job_id':'job1'}
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path,action='reconcile')['status']=='cancelled'
    assert state['calls']==2  # initial submit plus explicit cancel, never re-submit


def test_explicit_anonymous_http_requires_v2_opt_in(api_server,tmp_path,monkeypatch):
    import fsf.remote.model_api as module
    state,url=api_server;state['payload']=b'anonymous endpoint result'
    monkeypatch.delenv('FSF_MODEL_API_KEY',raising=False)
    p,r=async_profile(url);p['key_env']=None;p['http']['auth_header']='none'
    p['http']['job_id_pointer']=None;p['http']['response_mode']='base64'
    monkeypatch.setattr(module,'credential_value',lambda *_:pytest.fail('anonymous endpoint resolved a secret'))
    assert run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)['status']=='completed'
    assert state['authorization'] is None
    p['schema_version']='fsf_model_api_profile_v1'
    p.pop('input_max_bytes');p.pop('request_max_bytes');p.pop('observation_timeout_seconds')
    with pytest.raises(RemoteError,match='API_AUTH_CONFIG'):validate_profile(p)


def test_observation_budget_is_finite_and_expired_prepared_intent_does_not_post(api_server,tmp_path,monkeypatch):
    from fsf.remote.model_api import _run_api
    state,url=api_server;monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    p,r=async_profile(url)
    bad={**p,'observation_timeout_seconds':5401}
    with pytest.raises(RemoteError,match='API_SCHEMA'):validate_profile(bad)
    p['authorized']=False
    with pytest.raises(RemoteError,match='API_NOT_AUTHORIZED'):
        _run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    # Establish a prepared intent using read-only action after an initial
    # credential refusal, then expire it without ever contacting the service.
    p['authorized']=True;monkeypatch.delenv('FSF_MODEL_API_KEY')
    with pytest.raises(RemoteError,match='API_AUTH_MISSING'):
        _run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    path=expire_submission(tmp_path);value=read_json(path);value['status']='prepared'
    write_json(path,seal(value,'ledger_digest'))
    monkeypatch.setenv('FSF_MODEL_API_KEY','synthetic-contract-credential')
    with pytest.raises(RemoteError,match='API_DEADLINE_EXCEEDED'):
        _run_api(r,p,inputs_root=tmp_path,state_root=tmp_path)
    assert state['calls']==0
