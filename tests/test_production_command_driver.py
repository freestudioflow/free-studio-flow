"""Real child processes exercise the provider-neutral production command IPC."""
import hashlib,json,os,sys,subprocess
from pathlib import Path
import pytest
from test_production_runner import setup
from fsf.production.command_driver import CommandDriver
from fsf.production.runner import run_batch
from fsf.production.contracts import ProductionError
from fsf.remote.contracts import sha,write_json


def driver(tmp_path,plan):
    script=tmp_path/'operator_backend.py'
    script.write_text('''import json,sys,hashlib
from pathlib import Path
source,target=map(Path,sys.argv[1:]);call=json.loads(source.read_text());request=call['request']
rid=request['request_digest'];op=call['operation'];state=Path('submitted.json')
known=json.loads(state.read_text()) if state.exists() else []
artifact_root=Path(call['locator']['artifact_root']);folder=artifact_root/rid
if op=='submit':
 if rid not in known:
  known.append(rid);state.write_text(json.dumps(known))
  folder.mkdir();(folder/'artifact.bin').write_bytes(b'actual CPU fixture, not generated media')
 result={'job_id':rid}
elif op=='reconcile':result={'job_id':rid} if rid in known else None
elif op=='status':result='completed' if rid in known else 'unknown'
elif op=='fetch':
 artifact=folder/'artifact.bin';value=hashlib.sha256(artifact.read_bytes()).hexdigest()
 result={'artifact':'../escape.bin' if Path('escape').exists() else 'artifact.bin','report':{
 'artifact_sha256':value,'evaluator_digest':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
 'policy_digest':request['policy_digest'],'sample_hashes':[value],'verdict':'accept','defects':[]}}
else:result='unsupported'
target.write_text(json.dumps({'schema_version':'fsf_command_adapter_reply_v1',
 'request_digest':rid,'operation':op,'result':result}))
''')
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    config={'schema_version':'fsf_production_command_driver_v1',
        'settings':{'command':[sys.executable,str(script)],'cwd':str(tmp_path),'timeout_seconds':10,'credential_env_names':[],
            'files':[{'path':sys.executable,'sha256':sha(Path(sys.executable))},{'path':str(script),'sha256':sha(script)}]},
        'artifact_root':str(artifacts),'evaluator_digest':sha(script)}
    root=tmp_path/'driver';root.mkdir()
    return CommandDriver(config,plan=plan,state_root=root),config


def test_actual_command_parent_reuse_and_mutation_guard(tmp_path):
    plan,book=setup(tmp_path);backend,_=driver(tmp_path,plan)
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=backend)['status']=='machine_accepted'
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=backend)['status']=='machine_accepted'
    ids=json.loads((tmp_path/'submitted.json').read_text());assert len(ids)==2
    (tmp_path/'artifacts'/ids[0]/'artifact.bin').write_bytes(b'changed')
    with pytest.raises(ProductionError,match='SOURCE_CHANGED'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=backend)


def test_lost_reply_is_reconciled_without_duplicate_process_submit(tmp_path,monkeypatch):
    plan,book=setup(tmp_path);backend,_=driver(tmp_path,plan);save=backend._save_identity
    def fail(*_):raise RuntimeError('lost after actual submission')
    monkeypatch.setattr(backend,'_save_identity',fail)
    with pytest.raises(ProductionError,match='RECONCILE_REQUIRED'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=backend)
    assert len(json.loads((tmp_path/'submitted.json').read_text()))==1
    monkeypatch.setattr(backend,'_save_identity',save)
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=backend)['status']=='machine_accepted'
    assert len(json.loads((tmp_path/'submitted.json').read_text()))==2


def test_real_cli_run_batch_reuses_same_intent(tmp_path):
    plan,book=setup(tmp_path);_,config=driver(tmp_path,plan)
    write_json(tmp_path/'plan.json',plan);write_json(tmp_path/'driver.json',config)
    args=[sys.executable,'-m','fsf.cli','production','run-batch',str(tmp_path/'plan.json'),
        '--driver',str(tmp_path/'driver.json'),'--ledger',str(book.path),'--state',str(tmp_path/'cli-state')]
    for _ in range(2):
        child=subprocess.run(args,capture_output=True,text=True,timeout=90)
        assert child.returncode==0,child.stdout+child.stderr
        assert json.loads(child.stdout)['result']['status']=='machine_accepted'
    assert len(json.loads((tmp_path/'submitted.json').read_text()))==2


def test_driver_copies_operator_configuration_and_refuses_unbound_task(tmp_path):
    plan,_=setup(tmp_path);backend,config=driver(tmp_path,plan)
    config['settings']['command'][0]='changed'
    assert backend.config['settings']['command'][0]!=config['settings']['command'][0]
    task=dict(plan['tasks'][0]);task['worst_units']=0
    with pytest.raises(ProductionError,match='BINDING_CHANGED'):
        backend.submit(task,'unbound')
    assert not (tmp_path/'submitted.json').exists()
