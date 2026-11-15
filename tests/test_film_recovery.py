"""R13-2 process and accounting faults against a local CPU fixture."""
import errno
import time

import pytest

from fsf.core.edit_store import EditStoreError
from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.recovery import ReadRetries
from fsf.production.store import ProductionStore
from fsf.remote.contracts import sha
from test_production_film import film
from test_resource_pool import authority, inventory, ResourcePool, stopped, ticket


def test_negative_receipt_is_required_to_resubmit_after_nonzero(film):
    spec, root, cli = film
    script = root.parent / 'operator_backend.py'
    original = script.read_text()
    script.write_text(original.replace("if op=='submit':", """if request['attempt_id']==1 and rid not in known:
 if op=='submit':
  target.write_text(json.dumps(dict(schema_version='fsf_command_adapter_error_v1',
      request_digest=rid,operation=op,code='KAGGLE_API_FAILED')))
  sys.exit(2)
 if op=='reconcile' and not Path('negative-proof').exists():
  target.write_text(json.dumps(dict(schema_version='fsf_command_adapter_reply_v1',
      request_digest=rid,operation=op,result=None)))
  sys.exit(0)
 if op=='reconcile':
  import time
  loc=call['locator']
  proof=dict(disposition='not_received',request_digest=rid,scope=loc['transport_scope'],
      attempt_id=1,revision=loc['attempt_revision'],observed_at=int(time.time()),evidence_digest='a'*64)
  target.write_text(json.dumps(dict(schema_version='fsf_command_adapter_reply_v1',
      request_digest=rid,operation=op,result=proof)))
  sys.exit(0)
if op=='submit':""").replace("request['attempt_id']==1 and rid not in known",
                             "request['attempt_id']==1 and request['step_id']=='a' and rid not in known"))
    spec['driver']['settings']['files'][1]['sha256'] = sha(script)
    spec['driver']['evaluator_digest'] = sha(script)
    cli('film-submit', error='KAGGLE_API_FAILED')
    assert cli('film-resume')['status'] == 'unknown'
    pending, = cli('film-status')['attempts']
    cli('film-retire-prepared', 'WO_0004', '1', '--revision', str(pending['attempt_revision']),
        error='PRODUCTION_RECONCILE_REQUIRED')
    (root.parent / 'negative-proof').touch()
    assert cli('film-resume')['status'] == 'machine_accepted'
    status = cli('film-status')
    assert [row['status'] for row in status['attempts']] == ['failed', 'machine_accepted', 'machine_accepted']
    assert status['used_units'] == 30


def test_early_lease_return_leaves_unknown_film_charge(film, monkeypatch):
    from fsf.production.film import FilmDriver, run

    spec, root, cli = film
    with monkeypatch.context() as patch:
        def lose_reply(*_):
            raise RuntimeError('lost acknowledgement')
        patch.setattr(FilmDriver, '_save_identity', lose_reply)
        with pytest.raises(ProductionError, match='PRODUCTION_RECONCILE_REQUIRED'):
            run('film-submit', root, spec=spec)
    assert cli('film-cancel', 'WO_0004', '1')['fees_released'] is False
    pending, = cli('film-status')['attempts']
    request = pending['request_digest']
    pool = ResourcePool(root.parent / 'pool.json')
    pool.create(authority(slots=1))
    pool.enqueue(ticket(request))
    claim = pool.admit(request, inventory=inventory())
    store = ProductionStore(root)
    try:
        store.link_allocation(request, 1, request)
        released = pool.release(claim, confirm_stopped=stopped('task'))
        assert pool.release(claim, confirm_stopped=None) == released
        assert not pool.inspect()['claims']
        charge = store.db.execute('SELECT status,worst FROM reservations WHERE request=?', (request,)).fetchone()
        assert tuple(charge) == ('unknown', 10)
        assert store.inspect()['used_units'] == 10
    finally:
        store.close()


@pytest.mark.parametrize('next_worst', [10, 11])
def test_positive_active_fee_is_counted_at_admission_cap(film, next_worst):
    from fsf.production.film import preflight

    spec, root, _ = film
    plan = spec['plan']
    first, second = plan['tasks']
    first['worst_units'], second['worst_units'], second['depends_on'] = 890, next_worst, []
    plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
    spec['plan'] = seal(plan, 'plan_digest')
    execution, _ = preflight(spec)
    root.mkdir()
    store = ProductionStore.create(root, spec['plan'], spec['authorization'], execution_id=execution, _film_spec=spec)
    try:
        claim = store.claim(execution, 'a', owner='fixture')
        assert store.db.execute('SELECT status FROM reservations WHERE request=?',
                                (claim['request'],)).fetchone()[0] == 'active'
        if next_worst == 10:
            store.claim(execution, 'b', owner='fixture')
            assert store.inspect()['used_units'] == 900
        else:
            with pytest.raises(ProductionError, match='PRODUCTION_BUDGET_EXCEEDED'):
                store.claim(execution, 'b', owner='fixture')
            assert store.inspect()['used_units'] == 890
        store.check()
    finally:
        store.close()


def test_partial_fetch_is_retried_after_restart_without_new_submit(film):
    spec, root, cli = film
    script = root.parent / 'operator_backend.py'
    script.write_text(script.read_text().replace("elif op=='fetch':", """elif op=='fetch':
 partial=Path('partial')
 if not partial.exists():
  partial.write_bytes(b'truncated')
  sys.exit(1)
 partial.unlink() if partial.read_bytes()==b'truncated' else None
 partial.write_bytes(b'retrieved')"""))
    spec['driver']['settings']['files'][1]['sha256'] = sha(script)
    spec['driver']['evaluator_digest'] = sha(script)
    cli('film-submit', error='REMOTE_ADAPTER_FAILED')
    prior, = cli('film-status')['attempts']
    assert prior['status'] == 'produced'
    cli('film-fetch', 'WO_0004', '1', error='PRODUCTION_NOT_READY')
    assert cli('film-resume')['status'] == 'machine_accepted'
    receipt = cli('film-fetch', 'WO_0004', '1')
    assert receipt['request_digest'] == prior['request_digest']
    assert cli('film-status')['used_units'] == 20


@pytest.mark.parametrize('after_commit', [False, True])
def test_receipt_publication_interruption_reopens_original_attempt(film, monkeypatch, after_commit):
    from fsf.production.film import run

    spec, root, cli = film
    accept = ProductionStore.accept

    def interrupt(*args):
        if after_commit:
            accept(*args)
        raise OSError(errno.EIO, 'interrupted at acceptance boundary')

    with monkeypatch.context() as patch:
        patch.setattr(ProductionStore, 'accept', interrupt)
        with pytest.raises(EditStoreError, match='EDIT_IO'):
            run('film-submit', root, spec=spec)
    prior, = cli('film-status')['attempts']
    assert prior['status'] == ('machine_accepted' if after_commit else 'produced')
    assert cli('film-resume')['status'] == 'machine_accepted'
    assert cli('film-fetch', 'WO_0004', '1')['request_digest'] == prior['request_digest']
    assert cli('film-status')['used_units'] == 20


def test_job_version_is_part_of_provider_identity(film):
    spec, root, cli = film
    script = root.parent / 'operator_backend.py'
    script.write_text(script.read_text().replace("result={'job_id':rid}\nelif op=='reconcile'",
        "result={'job_id':'fixture-notebook','version':1 if request['step_id']=='a' else 2}\nelif op=='reconcile'"))
    spec['driver']['settings']['files'][1]['sha256'] = sha(script)
    spec['driver']['evaluator_digest'] = sha(script)
    assert cli('film-submit')['status'] == 'machine_accepted'
    rows = cli('film-status')['attempts']
    assert [(row['provider_job_id'], row['provider_job_version']) for row in rows] == [
        ('fixture-notebook', 1), ('fixture-notebook', 2)]


@pytest.mark.parametrize('code,expected', [(errno.EBUSY, [1, 2, 4]), (errno.EACCES, [])])
def test_phase_read_retry_is_bounded_and_rejects_permanent_errors(monkeypatch, code, expected):
    sleeps = []
    monkeypatch.setattr(time, 'sleep', sleeps.append)

    def fail():
        raise OSError(code, 'injected read failure')

    with pytest.raises(OSError):
        ReadRetries(time.monotonic() + 60).call(fail)
    assert sleeps == expected
