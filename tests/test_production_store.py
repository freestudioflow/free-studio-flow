"""Indexed authority tests; CPU fixtures are not film or device qualification."""
import json
import sqlite3
import time

import pytest

from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.store import ProductionStore
from fsf.production.store_runner import run_store_batch
from test_production_runner import setup, Driver
from test_runner_budget_v2 import case as budget_case


@pytest.mark.parametrize('epoch', [True, 0, 1.0])
def test_cancellation_epoch_rejects_alias_values_before_provider_contact(tmp_path, epoch):
    from fsf.production.store_runner import cancel_store_request
    plan,store=store_case(tmp_path)
    backend=Driver(tmp_path)
    try:
        with pytest.raises(ProductionError):
            cancel_store_request(plan,store=store,driver=backend,request_id='a'*64,epoch=epoch)
        assert backend.submitted==[] and store.inspect()['used_units']==0
    finally:store.close()


def _competing_claim(root, execution, barrier, replies):
    """Independent connections, synchronized before either write transaction."""
    store = ProductionStore(root)
    try:
        barrier.wait(timeout=20)
        try:
            value = store.claim(execution, 'a', owner='contender')
            replies.put(('claimed', value['request']))
        except ProductionError as exc:
            replies.put(('rejected', exc.code))
    finally:
        store.close()


def test_independent_processes_cannot_reserve_same_generation_slot(tmp_path):
    import multiprocessing
    plan, store = store_case(tmp_path)
    execution, root = store.execution(plan), str(store.root)
    store.close()
    context = multiprocessing.get_context('spawn')
    barrier, replies = context.Barrier(3), context.Queue()
    workers = [context.Process(target=_competing_claim, args=(root, execution, barrier, replies)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=30)
        results = [replies.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        assert sorted(value[0] for value in results) == ['claimed', 'rejected']
        assert next(value[1] for value in results if value[0] == 'rejected') == 'PRODUCTION_RECONCILE_REQUIRED'
        store = ProductionStore(root)
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 1
        assert store.inspect()['used_units'] == 10
        store.check()
        store.close()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        replies.close()


def test_expired_unsent_lease_renews_without_new_request_or_budget(tmp_path, monkeypatch):
    plan, store = store_case(tmp_path)
    claim = store.claim(store.execution(plan), 'a', owner='parent')
    clock = claim['expires'] + 1
    monkeypatch.setattr('fsf.production.store.time.time', lambda: clock)
    with pytest.raises(ProductionError, match='LEASE_EXPIRED'):
        store.mark_sending(claim['request'], claim['epoch'])
    with pytest.raises(ProductionError, match='RECONCILE_REQUIRED'):
        store.renew_prepared(claim['request'], claim['epoch'], owner='another')
    store.renew_prepared(claim['request'], claim['epoch'], owner='parent', seconds=20)
    store.mark_sending(claim['request'], claim['epoch'])
    with pytest.raises(ProductionError, match='RECONCILE_REQUIRED'):
        store.renew_prepared(claim['request'], claim['epoch'], owner='parent')
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    assert store.inspect()['used_units'] == 10
    store.close()


def test_allocation_and_cancel_do_not_release_unknown_cost(tmp_path):
    plan, store = store_case(tmp_path)
    claim = store.claim(store.execution(plan), 'a', owner='parent')
    request, epoch = claim['request'], claim['epoch']
    store.link_allocation(request, epoch, 'allocation_one')
    store.link_allocation(request, epoch, 'allocation_one')
    with pytest.raises(ProductionError, match='REQUEST_CONFLICT'):
        store.link_allocation(request, epoch, 'allocation_two')
    store.mark_sending(request, epoch)
    store.observe(request, epoch, 'unknown')
    store.settle(request)
    assert store.cancel_intent(request, epoch)['fees_released'] is False
    assert store.inspect()['used_units'] == 10
    assert store.db.execute('SELECT status FROM reservations').fetchone()[0] == 'unknown'
    assert store.db.execute('SELECT cancel_state FROM outbox').fetchone()[0] == 'requested'
    store.check()
    store.close()


def test_backup_is_consistent_read_only_data_not_new_authority(tmp_path):
    plan, store = store_case(tmp_path)
    store.claim(store.execution(plan), 'a', owner='parent')
    target = tmp_path / 'snapshot.sqlite3'
    result = store.backup(target)
    assert result['read_only'] is True
    with sqlite3.connect(target) as saved:
        assert saved.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert saved.execute('SELECT used FROM meta').fetchone()[0] == 10
        assert saved.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    with pytest.raises(ProductionError, match='TARGET_EXISTS'):
        store.backup(target)
    assert store.inspect()['used_units'] == 10
    store.close()


def store_case(tmp_path, version='v2'):
    plan, book = budget_case(tmp_path) if version == 'v2' else setup(tmp_path)
    root = tmp_path / 'indexed'
    root.mkdir()
    return plan, ProductionStore.create(root, plan, book.inspect()['authorization'])


@pytest.mark.parametrize('prior_full_index', [False, True])
def test_ready_selection_retries_terminal_failures_and_reopens_prior_index(tmp_path, prior_full_index, monkeypatch):
    if prior_full_index:
        import fsf.production.store as module
        prior = module.DDL.replace('request TEXT NOT NULL UNIQUE', 'request TEXT PRIMARY KEY')
        prior = prior.replace('PRIMARY KEY(execution,task,epoch)', 'UNIQUE(execution,task,epoch)')
        monkeypatch.setattr(module, 'DDL', prior)
    plan, store = store_case(tmp_path)
    execution, root = store.execution(plan), store.root
    if prior_full_index:
        # Databases created before the partial-index optimization remain usable;
        # opening an existing authority does not rewrite its schema or budget.
        store.db.execute('DROP INDEX ready_tasks')
        store.db.execute('CREATE INDEX ready_tasks ON tasks(execution,status,remaining,ordinal)')
    store.close()
    store = ProductionStore(root)
    try:
        for status in ('ready', 'failed', 'cancelled', 'rejected'):
            store.db.execute('UPDATE tasks SET status=? WHERE execution=? AND id=?', (status, execution, 'a'))
            assert store.next_ready(execution)['id'] == 'a'
        for status in ('reserved', 'submitted', 'running', 'unknown', 'produced', 'machine_accepted'):
            store.db.execute('UPDATE tasks SET status=? WHERE execution=? AND id=?', (status, execution, 'a'))
            assert store.next_ready(execution) is None
        assert store.inspect()['used_units'] == 0
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 0
    finally:
        store.close()


def test_attempt_identity_constraints_and_reservation_execution_are_enforced(tmp_path):
    plan, store = store_case(tmp_path)
    execution = store.execution(plan)
    try:
        claim = store.claim(execution, 'a', owner='parent')
        insert = 'INSERT INTO attempts(request,execution,task,epoch,owner,expires,status) VALUES(?,?,?,?,?,?,?)'
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute(insert, (claim['request'], execution, 'b', 1, 'other', claim['expires'], 'reserved'))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute(insert, ('f'*64, execution, 'a', claim['epoch'], 'other', claim['expires'], 'reserved'))
        store.db.execute('INSERT INTO executions(id,plan_digest,binding,policy) VALUES(?,?,?,?)',
                         ('other', 'b'*64, 'c'*64, 'd'*64))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute('UPDATE reservations SET execution=? WHERE request=?', ('other', claim['request']))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute('UPDATE reservations SET authorization=2 WHERE request=?', (claim['request'],))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute('DELETE FROM attempts WHERE request=?', (claim['request'],))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute('UPDATE attempts SET charge_authorization=NULL WHERE request=?', (claim['request'],))
        assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 1
        assert store.inspect()['used_units'] == 10
        store.check()
    finally:
        store.close()


def test_reservation_view_failure_rolls_back_claim_and_money(tmp_path):
    plan, store = store_case(tmp_path)
    execution = store.execution(plan)
    try:
        store.db.execute("""CREATE TRIGGER fail_funding BEFORE UPDATE OF charge_authorization ON attempts
            WHEN NEW.charge_authorization IS NOT NULL BEGIN SELECT RAISE(ABORT,'injected funding failure'); END""")
        with pytest.raises(ProductionError, match='REQUEST_CONFLICT'):
            store.claim(execution, 'a', owner='parent')
        assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 0
        assert store.inspect()['used_units'] == 0
        assert store.next_ready(execution)['id'] == 'a'
        store.db.execute('DROP TRIGGER fail_funding')
        claim = store.claim(execution, 'a', owner='parent')
        assert store.attempt(claim['request'])['status'] == 'reserved'
        assert store.inspect()['used_units'] == 10
        store.check()
    finally:
        store.close()


def test_existing_physical_reservation_table_reopens_and_reuses(tmp_path):
    plan, store = store_case(tmp_path)
    root = store.root
    try:
        # Model an older private store's separate table and composite FK. The
        # current reader must neither rewrite it nor create a second authority.
        store.db.execute('DROP VIEW reservations')
        store.db.execute('DROP INDEX generation_slot')
        store.db.execute('CREATE UNIQUE INDEX prior_request_execution ON attempts(request,execution)')
        store.db.executescript('''CREATE TABLE reservations(request TEXT PRIMARY KEY REFERENCES attempts(request),
          authorization TEXT NOT NULL REFERENCES authorization(digest),execution TEXT NOT NULL,
          shot TEXT,segment TEXT,round INTEGER,route TEXT NOT NULL,worst INTEGER NOT NULL CHECK(worst>=0),
          settled INTEGER CHECK(settled>=0 AND settled<=worst),frames INTEGER NOT NULL CHECK(frames>=0),
          status TEXT NOT NULL CHECK(status IN ('active','unknown','settled')),binding TEXT NOT NULL,
          essential INTEGER NOT NULL CHECK(essential IN (0,1)),
          FOREIGN KEY(request,execution) REFERENCES attempts(request,execution),
          CHECK((frames=0 AND shot IS NULL AND segment IS NULL AND round IS NULL) OR
            (frames>0 AND shot IS NOT NULL AND segment IS NOT NULL AND round BETWEEN 1 AND 3))) WITHOUT ROWID;
          CREATE UNIQUE INDEX generation_slot ON reservations(authorization,execution,shot,segment,round) WHERE frames>0;''')
    finally:
        store.close()
    store = ProductionStore(root)
    backend = Driver(tmp_path)
    try:
        for _ in range(2):
            assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
        assert len(backend.submitted) == 2
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 2
        store.check()
    finally:
        store.close()


def test_report_write_failure_rolls_back_acceptance_and_resumes_without_resubmit(tmp_path):
    plan, store = store_case(tmp_path)
    backend = Driver(tmp_path)
    try:
        store.db.execute("""CREATE TRIGGER reject_report BEFORE INSERT ON attempt_results
            BEGIN SELECT RAISE(ABORT,'injected report write failure'); END""")
        with pytest.raises(ProductionError, match='REQUEST_CONFLICT'):
            run_store_batch(plan, store=store, driver=backend)
        assert len(backend.submitted) == 1
        assert store.task(store.execution(plan), 'a')['accepted'] is None
        assert store.db.execute('SELECT count(*) FROM attempt_results').fetchone()[0] == 0
        assert store.inspect()['used_units'] == 5
        store.db.execute('DROP TRIGGER reject_report')
        assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
        assert len(backend.submitted) == 2
        store.check()
        store.db.execute("UPDATE attempt_results SET evidence='{}' WHERE request=?", (backend.submitted[0][1],))
        with pytest.raises(ProductionError, match='STORE_CORRUPT'):
            run_store_batch(plan, store=store, driver=backend)
        assert len(backend.submitted) == 2
    finally:
        store.close()


def test_existing_embedded_report_database_reopens_and_reuses(tmp_path, monkeypatch):
    import fsf.production.store as module
    old = module.DDL.replace('CREATE TABLE attempt_results(request TEXT PRIMARY KEY REFERENCES attempts(request),\n evidence TEXT NOT NULL, artifact TEXT) WITHOUT ROWID;\n', '')
    old = old.replace('authorization INTEGER NOT NULL CHECK(authorization=1) REFERENCES authorization(id)',
                      'authorization TEXT NOT NULL REFERENCES authorization(digest)')
    assert old != module.DDL
    monkeypatch.setattr(module, 'DDL', old)
    plan, store = store_case(tmp_path)
    root = store.root
    backend = Driver(tmp_path)
    try:
        assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
    finally:
        store.close()
    store = ProductionStore(root)
    try:
        assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
        assert len(backend.submitted) == 2
        store.check()
    finally:
        store.close()


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_store_runs_reuses_and_checks_current_artifacts(tmp_path, version):
    plan, store = store_case(tmp_path, version)
    driver = Driver(tmp_path)
    for _ in range(2):
        result = run_store_batch(plan, store=store, driver=driver)
        assert result['status'] == 'machine_accepted'
        assert result['verification_kind'] == 'completed_observation_sweep'
    assert len(driver.submitted) == 2
    assert store.check()['reservations'] == 2
    assert store.inspect()['used_units'] == 10
    (tmp_path / driver.submitted[0][1]).write_bytes(b'changed')
    with pytest.raises(ProductionError, match='SOURCE_CHANGED'):
        run_store_batch(plan, store=store, driver=driver)
    assert len(driver.submitted) == 2
    store.close()


def test_claim_admission_and_intent_rollback_together(tmp_path):
    plan, store = store_case(tmp_path)
    execution = store.execution(plan)
    # The event append fails after the row updates, exercising real rollback.
    store.db.execute("CREATE TRIGGER refuse_events BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(ProductionError):
        store.claim(execution, 'a', owner='fixture')
    assert store.inspect()['used_units'] == 0
    assert store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    store.db.execute('DROP TRIGGER refuse_events')
    claim = store.claim(execution, 'a', owner='fixture')
    assert store.inspect()['used_units'] == 10
    with pytest.raises(ProductionError, match='RECONCILE_REQUIRED'):
        store.claim(execution, 'a', owner='second')
    with pytest.raises(ProductionError, match='FENCED'):
        store.mark_sending(claim['request'], claim['epoch'] + 1)
    store.check()
    store.close()


def test_unknown_submit_survives_reopen_without_second_send(tmp_path):
    plan, store = store_case(tmp_path)
    backend = Driver(tmp_path)
    backend.raise_after_submit = True
    with pytest.raises(ProductionError, match='RECONCILE_REQUIRED'):
        run_store_batch(plan, store=store, driver=backend)
    root = store.root
    store.close()
    backend.raise_after_submit = False
    store = ProductionStore(root)
    assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
    assert len(backend.submitted) == 2
    assert store.check()['reservations'] == 2
    store.close()


def test_pause_hold_and_expiry_do_not_send_prepared_intent(tmp_path, monkeypatch):
    plan, store = store_case(tmp_path)
    execution = store.execution(plan)
    claim = store.claim(execution, 'a', owner='fixture')
    store.pause(execution, True)
    with pytest.raises(ProductionError, match='NOT_READY'):
        store.mark_sending(claim['request'], claim['epoch'])
    store.pause(execution, False)
    (store.root / 'submit-hold.json').write_text('{}')
    with pytest.raises(ProductionError, match='SUBMIT_HELD'):
        store.mark_sending(claim['request'], claim['epoch'])
    (store.root / 'submit-hold.json').unlink()
    monkeypatch.setattr('fsf.production.ledger.time.time', lambda: store.authorization['expires_at_unix'] + 1)
    with pytest.raises(ProductionError, match='EXPIRED'):
        store.mark_sending(claim['request'], claim['epoch'])
    assert store.db.execute('SELECT state FROM outbox').fetchone()[0] == 'prepared'
    store.close()


def test_exact_forecast_threshold_and_unknown_fees(tmp_path):
    plan, book = budget_case(tmp_path)
    prior = {**plan['tasks'][0], 'task_id': 'prior', 'segment_id': 'prior',
             'worst_units': 790, 'generated_frames': 0}
    plan['tasks'].insert(0, prior)
    plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
    plan = seal(plan, 'plan_digest')
    auth = book.inspect()['authorization']
    auth['plan_digest'] = plan['plan_digest']
    auth = seal(auth, 'authorization_digest')
    root = tmp_path / 'indexed'
    root.mkdir()
    store = ProductionStore.create(root, plan, auth)
    execution = store.execution(plan)
    old = store.claim(execution, 'prior', owner='fixture')
    store.mark_sending(old['request'], old['epoch'])
    store.observe(old['request'], old['epoch'], 'unknown')
    store.settle(old['request'])
    with pytest.raises(ProductionError, match='FORECAST_REQUIRED'):
        store.claim(execution, 'a', owner='fixture')
    assert not store.forecast(remaining_worst_units=111)['affordable']
    with pytest.raises(ProductionError, match='FORECAST_UNAFFORDABLE'):
        store.claim(execution, 'a', owner='fixture')
    assert store.forecast(remaining_worst_units=110)['affordable']
    claim = store.claim(execution, 'a', owner='fixture')
    store.settle(claim['request'])
    assert store.inspect()['used_units'] == 800
    assert store.db.execute("SELECT count(*) FROM reservations WHERE status='unknown'").fetchone()[0] == 2
    store.check()
    store.close()


def test_authority_registry_refuses_duplicate_sibling_store(tmp_path):
    plan, store = store_case(tmp_path)
    duplicate = tmp_path / 'duplicate'
    duplicate.mkdir()
    with pytest.raises(ProductionError, match='AUTHORITY_CONFLICT'):
        ProductionStore.create(duplicate, plan, store.authorization)
    import shutil
    for name in ('state.sqlite3', 'store-manifest.json'):
        shutil.copyfile(store.root / name, duplicate / name)
    with pytest.raises(ProductionError, match='AUTHORITY_CONFLICT'):
        ProductionStore(duplicate)
    store.close()


def test_slot_scope_separates_executions_but_not_money(tmp_path):
    plan, store = store_case(tmp_path, 'v1')
    from copy import deepcopy
    other = deepcopy(plan)
    other['tasks'][0]['input_digest'] = digest('independent-authorized-work')
    other['binding_digest'] = digest({k: other[k] for k in ('production_id', 'policy_digest', 'tasks')})
    other = seal(other, 'plan_digest')
    # Explicit maintenance setup, as used by a complete migration manifest.
    with store.transaction():
        ProductionStore._insert_execution(store.db, 'second', other, store.authorization)
    first = store.claim(store.execution(plan), 'a', owner='fixture')
    second = store.claim('second', 'a', owner='fixture')
    assert first['request'] != second['request']
    assert store.inspect()['used_units'] == 20
    assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 2
    with pytest.raises(ProductionError, match='RECONCILE_REQUIRED'):
        store.claim('second', 'a', owner='duplicate')
    store.check()
    store.close()


def test_connections_use_frozen_delete_full_profile_and_os_lock(tmp_path):
    plan, store = store_case(tmp_path)
    other = ProductionStore(store.root)
    assert store.db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert store.db.execute('PRAGMA synchronous').fetchone()[0] == 2
    with store.transaction():
        with pytest.raises(ProductionError, match='STORE_BUSY'):
            with other.transaction(seconds=0.01):
                pytest.fail('two simultaneous writers')
    other.close()
    store.close()


def test_busy_commit_rolls_back_without_reporting_corruption(tmp_path):
    _, store = store_case(tmp_path)
    reader = sqlite3.connect(store.path, isolation_level=None)
    try:
        reader.execute('BEGIN')
        original = reader.execute('SELECT revision FROM meta').fetchone()[0]
        with pytest.raises(ProductionError, match='PRODUCTION_STORE_BUSY'):
            with store.transaction(seconds=0.02):
                store.db.execute('UPDATE meta SET revision=revision+1')
        assert not store.db.in_transaction
        reader.execute('ROLLBACK')
        assert store.db.execute('SELECT revision FROM meta').fetchone()[0] == original
        assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    finally:
        reader.close()
        store.close()


def test_busy_commit_retries_same_transaction_when_reader_releases(tmp_path):
    import threading
    _, store = store_case(tmp_path)
    ready, committing = threading.Event(), threading.Event()
    failures = []

    def read_until_commit():
        reader = sqlite3.connect(store.path, isolation_level=None)
        try:
            reader.execute('BEGIN')
            reader.execute('SELECT revision FROM meta').fetchone()
            ready.set()
            if not committing.wait(5):
                raise AssertionError('writer did not reach commit')
            # Keep the real read lock briefly beyond the first COMMIT attempt.
            time.sleep(0.1)
            reader.execute('ROLLBACK')
        except BaseException as exc:
            failures.append(exc)
        finally:
            reader.close()

    worker = threading.Thread(target=read_until_commit)
    worker.start()
    try:
        assert ready.wait(5)
        store.db.set_trace_callback(lambda sql: committing.set() if sql == 'COMMIT' else None)
        original = store.db.execute('SELECT revision FROM meta').fetchone()[0]
        with store.transaction(seconds=2):
            store.db.execute('UPDATE meta SET revision=revision+1')
        assert store.db.execute('SELECT revision FROM meta').fetchone()[0] == original+1
        assert not store.db.in_transaction
    finally:
        committing.set()
        worker.join(5)
        store.db.set_trace_callback(None)
        store.close()
    assert not worker.is_alive()
    assert not failures


def test_partial_observation_sweep_advances_but_delivery_rechecks_prefix(tmp_path, monkeypatch):
    plan, store = store_case(tmp_path)
    backend = Driver(tmp_path)
    assert run_store_batch(plan, store=store, driver=backend)['status'] == 'machine_accepted'
    clock = [0.0]
    verify = backend.verify

    def bounded_verify(task, request):
        result = verify(task, request)
        clock[0] += 0.6
        return result

    backend.verify = bounded_verify
    monkeypatch.setattr('fsf.production.store_runner.time.monotonic', lambda: clock[0])
    first = run_store_batch(plan, store=store, driver=backend, seconds=1)
    assert first['status'] == 'paused_batch'
    position = store.db.execute("SELECT position FROM verification_runs WHERE phase='verify'").fetchone()[0]
    assert position == 2
    # The prior observations remain historical; they are not an atomic snapshot.
    (tmp_path / backend.submitted[0][1]).write_bytes(b'changed after observation')
    resumed = run_store_batch(plan, store=store, driver=backend, seconds=1)
    assert resumed['status'] == 'machine_accepted'
    assert resumed['delivery_ready'] is False
    assert resumed['snapshot_consistency'] == 'bounded_observations_not_atomic_snapshot'
    with pytest.raises(ProductionError, match='SOURCE_CHANGED'):
        run_store_batch(plan, store=store, driver=backend, seconds=1, delivery=True)
    assert len(backend.submitted) == 2
    store.close()
