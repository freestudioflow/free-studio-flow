"""Independent CLI invocations against a local synthetic full-film provider."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.control import file_identity
from fsf.production.film import FilmDriver, run
from fsf.production.store import ProductionStore
from test_production_command_driver import driver
from test_production_runner import setup


@pytest.fixture
def film(tmp_path):
    plan, ledger = setup(tmp_path)
    _, config = driver(tmp_path, plan)
    source = tmp_path / 'source.bin'
    source.write_bytes(b'synthetic reference')
    inputs = dict(assets={}, channels={}, anchors={
        'opening': dict(path=str(source), sha256=file_identity(source)['sha256'])})
    workorders = []
    for task, name in zip(plan['tasks'], ('WO_0004', 'WO_0012')):
        binding = dict(inputs=inputs, requires=dict(assets=[], channels=[], anchors=['opening']),
                       capability_digest=digest('synthetic CPU'), transport_scope='local-fixture')
        task['input_digest'] = digest(binding)
        workorders.append(dict(step_id=task['task_id'], workorder_id=name, **binding))
    plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
    spec = dict(schema_version='fsf_film_submission_v1', idempotency_key='approved-film',
                plan=seal(plan, 'plan_digest'), authorization=ledger.inspect()['authorization'],
                driver=config, workorders=workorders, exclusions=[],
                routes={'local': {'transport_scope': 'local-fixture', 'capability_digest': digest('synthetic CPU')}})
    source_file = tmp_path / 'film.json'
    store = tmp_path / 'store'

    def cli(operation, *args, error=None):
        source_file.write_text(json.dumps(spec), encoding='utf-8')
        targets = [str(source_file), '--out', str(store)] if operation == 'film-submit' else [str(store)]
        call = subprocess.run([sys.executable, '-m', 'fsf.cli', 'production', operation, *targets, *args],
                              capture_output=True, text=True, timeout=90)
        assert call.stdout, call.stderr
        result = json.loads(call.stdout)
        assert call.returncode == (2 if error else 0), result
        if error:
            assert result['error']['code'] == error
            return None
        return result['result']

    return spec, store, cli


def test_full_film_reopens_and_fetches_by_workorder(film):
    spec, store, cli = film
    assert cli('film-submit', '--dry-run')['dispatches'] == 0
    assert not store.exists()
    assert cli('film-submit')['status'] == 'machine_accepted'
    first = cli('film-status')
    assert [row['workorder_id'] for row in first['attempts']] == ['WO_0004', 'WO_0012']
    assert len({row['dispatch_id'] for row in first['attempts']}) == 2
    assert all(row['provider_job_id'] == row['request_digest'] for row in first['attempts'])
    assert all(row['result_digest'] and row['attempt_revision'] > 0 for row in first['attempts'])
    result = cli('film-fetch', 'WO_0012', '1')
    assert result['request_digest'] == first['attempts'][1]['request_digest']
    assert cli('film-resume')['status'] == 'machine_accepted'
    assert cli('film-submit')['execution_id'] == first['execution_id']
    assert cli('film-status') == first
    alias = store.with_name('another-output')
    assert run('film-submit', alias, spec=spec)['store'] == str(store)
    assert not alias.exists()
    operations = (store.parent / 'operations.jsonl').read_text().splitlines()
    assert sum(json.loads(line)['operation'] == 'submit' for line in operations) == 2
    with_store = ProductionStore(store)
    try:
        assert with_store.db.execute('SELECT used FROM meta').fetchone()[0] == 20
    finally:
        with_store.close()
    spec['workorders'].reverse()
    cli('film-submit', error='EXECUTION_BINDING_CONFLICT')
    Path(result['artifact']).write_bytes(b'stale bytes under the original filename')
    cli('film-fetch', 'WO_0012', '1', error='POST_SOURCE_CHANGED')


def test_lost_reply_keeps_attempt_and_reconciles_after_restart(film, monkeypatch):
    spec, store, cli = film

    def lose_reply(*_):
        raise RuntimeError('lost acknowledgement')

    with monkeypatch.context() as patch:
        patch.setattr(FilmDriver, '_save_identity', lose_reply)
        with pytest.raises(ProductionError, match='PRODUCTION_RECONCILE_REQUIRED'):
            run('film-submit', store, spec=spec)
    pending, = cli('film-status')['attempts']
    assert pending['status'] == 'unknown'
    assert pending['dispatch_id'] is pending['provider_job_id'] is None
    cli('film-fetch', 'WO_0004', '1', error='PRODUCTION_NOT_READY')
    assert cli('film-resume')['status'] == 'machine_accepted'
    resolved = cli('film-fetch', 'WO_0004', '1')
    assert resolved['request_digest'] == pending['request_digest']
    operations = [json.loads(line) for line in (store.parent / 'operations.jsonl').read_text().splitlines()]
    assert sum(row['operation'] == 'submit' for row in operations) == 2
    assert any(row['operation'] == 'reconcile' and row['request_id'] == pending['request_digest']
               for row in operations)


def test_optional_exclusion_and_required_dependency(film):
    spec, _, cli = film
    plan = spec['plan']
    plan['schema_version'] = 'fsf_production_batch_v2'
    for task in plan['tasks']:
        task['essential'] = task['task_id'] == 'b'
    spec['workorders'].pop(0)
    spec['exclusions'].append(dict(step_id='a', reason='operator_removed_optional',
                                  policy_digest=plan['policy_digest']))

    def reseal():
        plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
        spec['plan'] = seal(plan, 'plan_digest')

    reseal()
    cli('film-submit', error='EXECUTION_COVERAGE_INVALID')
    plan['tasks'][1]['depends_on'] = []
    reseal()
    assert cli('film-submit')['status'] == 'machine_accepted'
    status = cli('film-status')
    assert status['used_units'] == 10
    assert [row['step_id'] for row in status['attempts']] == ['b']


def test_new_attempt_changes_request_without_changing_execution(film):
    spec, root, _ = film
    root.mkdir()
    from fsf.production.film import preflight
    execution, _ = preflight(spec)
    store = ProductionStore.create(root, spec['plan'], spec['authorization'],
                                   execution_id=execution, _film_spec=spec)
    try:
        first = store.claim(execution, 'a', owner='fixture')
        store.mark_sending(first['request'], first['epoch'])
        store.observe(first['request'], first['epoch'], 'failed')
        second = store.claim(execution, 'a', owner='fixture')
        assert (first['epoch'], second['epoch']) == (1, 2)
        assert first['request'] != second['request']
    finally:
        store.close()


@pytest.mark.parametrize('marker', ['store-manifest.json', 'authorization'])
def test_committed_film_creation_recovers_missing_marker(film, monkeypatch, marker):
    from fsf.production import store as module

    spec, root, cli = film
    publish = module.publish
    target = (spec['authorization']['authorization_digest'] + '.json'
              if marker == 'authorization' else marker)

    def crash(path, value):
        if path.name == target:
            raise RuntimeError('interrupted after database commit')
        publish(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(module, 'publish', crash)
        with pytest.raises(RuntimeError, match='interrupted after database commit'):
            run('film-submit', root, spec=spec)
    assert (root / 'state.sqlite3').is_file()
    assert not (root.parent / 'operations.jsonl').exists()
    assert cli('film-submit')['status'] == 'machine_accepted'
    assert len(cli('film-status')['attempts']) == 2


def test_prepared_retirement_is_fenced_and_fetch_selects_new_attempt(film, monkeypatch):
    spec, root, cli = film

    def stop_before_contact(*_):
        raise RuntimeError('interrupted before provider boundary')

    with monkeypatch.context() as patch:
        patch.setattr(ProductionStore, 'mark_sending', stop_before_contact)
        with pytest.raises(RuntimeError, match='before provider boundary'):
            run('film-submit', root, spec=spec)
    pending, = cli('film-status')['attempts']
    assert not (root.parent / 'operations.jsonl').exists()
    revision = pending['attempt_revision']
    cli('film-retire-prepared', 'WO_0004', '1', '--revision', str(revision + 1),
        error='EXECUTION_BINDING_CONFLICT')
    assert cli('film-retire-prepared', 'WO_0004', '1', '--revision', str(revision))['status'] == 'retired'
    assert cli('film-resume')['status'] == 'machine_accepted'
    cli('film-fetch', 'WO_0004', '1', error='PRODUCTION_NOT_READY')
    receipt = cli('film-fetch', 'WO_0004', '2')
    assert receipt['request_digest'] != pending['request_digest']
    assert receipt['execution_id'] == pending['execution_id']


def test_reuse_requires_accepted_current_receipt_and_matching_bytes(film):
    spec, root, cli = film
    original = copy.deepcopy(spec)
    original['idempotency_key'] = 'fixture-original'
    original['authorization']['total_units'] += 1
    original['authorization']['cleanup_units'] += 1
    original['authorization'] = seal(original['authorization'], 'authorization_digest')
    source_root = root.with_name('previous')
    assert run('film-submit', source_root, spec=original)['status'] == 'machine_accepted'
    receipt = run('film-fetch', source_root, workorder='WO_0004', attempt=1)
    plan = spec['plan']
    plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
    spec['plan'] = seal(plan, 'plan_digest')
    spec['workorders'].pop(0)
    exclusion = dict(step_id='a', reason='verified_reuse', policy_digest=plan['policy_digest'],
                     source_store=str(source_root), source_request=receipt['request_digest'],
                     result_digest='0' * 64)
    spec['exclusions'].append(exclusion)
    cli('film-submit', error='EXECUTION_INPUT_UNBOUND')
    exclusion['result_digest'] = receipt['result_digest']
    assert cli('film-submit')['status'] == 'machine_accepted'
    status = cli('film-status')
    assert status['used_units'] == 10
    assert [row['step_id'] for row in status['attempts']] == ['b']
    Path(receipt['artifact']).write_bytes(b'unverified replacement')
    cli('film-resume', error='EXECUTION_INPUT_UNBOUND')


@pytest.mark.parametrize('fault,code', [
    ('missing', 'EXECUTION_COVERAGE_INVALID'), ('duplicate', 'EXECUTION_COVERAGE_INVALID'),
    ('extra', 'EXECUTION_COVERAGE_INVALID'), ('anchor', 'EXECUTION_INPUT_UNBOUND'),
    ('route', 'EXECUTION_INPUT_UNBOUND'),
])
def test_preflight_stops_before_creating_authority_or_submitting(film, fault, code):
    spec, store, cli = film
    if fault == 'missing':
        spec['workorders'].pop(0)
    elif fault == 'duplicate':
        spec['workorders'][1]['workorder_id'] = 'WO_0004'
    elif fault == 'extra':
        spec['workorders'].append(dict(spec['workorders'][0], workorder_id='extra', step_id='extra'))
    elif fault == 'route':
        spec['routes']['local']['transport_scope'] = 'different'
    else:
        spec['workorders'][0]['inputs']['anchors'].clear()
    cli('film-submit', error=code)
    assert not store.exists()
    assert not (store.parent / 'operations.jsonl').exists()
