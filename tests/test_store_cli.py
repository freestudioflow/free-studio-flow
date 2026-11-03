"""Public indexed CLI with actual local command-driver processes."""
import json
import subprocess
import sys

import pytest

from fsf.production.contracts import publish
from fsf.production.store import ProductionStore
from test_production_runner import setup
from test_production_command_driver import driver


def call(*args):
    child = subprocess.run([sys.executable, '-m', 'fsf.cli', 'production', *map(str, args)],
                           capture_output=True, text=True, timeout=60)
    assert 'Traceback' not in child.stdout + child.stderr
    return child.returncode, json.loads(child.stdout)


def test_indexed_cli_actual_processes_pause_export_and_reuse(tmp_path):
    plan, book = setup(tmp_path)
    _, config = driver(tmp_path, plan)
    for name, value in [('plan', plan), ('authorization', book.inspect()['authorization']), ('driver', config)]:
        publish(tmp_path / (name + '.json'), value)
    root = tmp_path / 'indexed'
    code, reply = call('store-create', tmp_path / 'plan.json', '--authorization', tmp_path / 'authorization.json', '--out', root)
    assert code == 0 and reply['result']['used_units'] == 0
    args = ('run-batch-v2', tmp_path / 'plan.json', '--store', root, '--driver', tmp_path / 'driver.json', '--seconds', '60')
    for _ in range(2):
        code, reply = call(*args)
        assert code == 0 and reply['result']['status'] == 'machine_accepted'
    calls = [json.loads(line) for line in (tmp_path / 'operations.jsonl').read_text().splitlines()]
    assert sum(value['operation'] == 'submit' for value in calls) == 2
    assert call('store-check', root)[1]['result']['reservations'] == 2
    assert call('store-pause', root, '--execution-id', 'film')[0] == 0
    assert call(*args)[1]['result']['status'] == 'paused'
    assert call('store-resume', root, '--execution-id', 'film')[0] == 0
    assert call(*args)[1]['result']['status'] == 'machine_accepted'
    code, reply = call('store-export', root, '--out', tmp_path / 'snapshot.json')
    assert code == 0 and reply['result']['read_only']
    assert call('store-revoke', root)[1]['result']['authorized_for_new_work'] is False


def test_cancel_cli_reconciles_without_resubmission_and_keeps_unknown_fees(tmp_path):
    plan, book=setup(tmp_path)
    backend,config=driver(tmp_path,plan)
    for name,value in [('plan',plan),('driver',config)]:publish(tmp_path/(name+'.json'),value)
    root=tmp_path/'indexed'
    root.mkdir()
    store=ProductionStore.create(root,plan,book.inspect()['authorization'])
    try:
        execution=store.execution(plan)
        store.bind_driver(execution,config)
        task=plan['tasks'][0]
        attempt=store.claim(execution,task['task_id'],owner='fixture')
        store.mark_sending(attempt['request'],attempt['epoch'])
        backend.submit(task,attempt['request'])
        store.observe(attempt['request'],attempt['epoch'],'running')
        store.settle(attempt['request'])
        used=store.inspect()['used_units']
    finally:store.close()
    code,reply=call('store-cancel',root,'--plan',tmp_path/'plan.json','--driver',tmp_path/'driver.json',
                    '--request-id',attempt['request'],'--epoch',str(attempt['epoch']))
    assert code==0 and reply['result']['cancel_state']=='unsupported'
    assert reply['result']['fees_released'] is False
    store=ProductionStore(root)
    try:
        assert store.inspect()['used_units']==used
        assert store.db.execute('SELECT status FROM reservations').fetchone()[0]=='unknown'
    finally:store.close()
    operations=[json.loads(line)['operation'] for line in (tmp_path/'operations.jsonl').read_text().splitlines()]
    assert operations.count('submit')==1 and operations.count('cancel')==1


@pytest.mark.parametrize('extra', [['--ledger', 'other.json'], ['--seconds', '0'], ['--seconds', '1.5'],
                                  ['--seconds', '60', '--seconds', '60'], ['--sec', '60']])
def test_indexed_cli_invalid_arguments_do_not_submit(tmp_path, extra):
    plan, book = setup(tmp_path)
    _, config = driver(tmp_path, plan)
    for name, value in [('plan', plan), ('authorization', book.inspect()['authorization']), ('driver', config)]:
        publish(tmp_path / (name + '.json'), value)
    root = tmp_path / 'indexed'
    assert call('store-create', tmp_path / 'plan.json', '--authorization', tmp_path / 'authorization.json', '--out', root)[0] == 0
    code, reply = call('run-batch-v2', tmp_path / 'plan.json', '--store', root, '--driver', tmp_path / 'driver.json', *extra)
    assert code == 2 and not reply['ok']
    assert not (tmp_path / 'submitted.json').exists()
