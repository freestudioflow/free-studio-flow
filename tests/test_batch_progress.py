"""Finite progress and revalidation fixtures; no neural/media qualification."""
import copy
import json
from pathlib import Path

import pytest

from fsf.production.contracts import ProductionError, digest, publish, seal
from fsf.production.ledger import Ledger
from fsf.production.runner import run_batch
from fsf.production.workflow import Workflow
from test_production_runner import Driver, setup


class Clock:
    value = 0.0
    def monotonic(self):
        return self.value


class TimedDriver(Driver):
    def __init__(self, root, clock):
        super().__init__(root)
        self.clock = clock
        self.cost = 0.0
        self.verifications = []
    def observe(self, task, request_id):
        self.clock.value += self.cost
        value = super().observe(task, request_id)
        value['settled_units'] = min(value['settled_units'], task['worst_units'])
        return value
    def verify(self, task, request_id):
        self.clock.value += self.cost
        self.verifications.append(task['task_id'])
        return super().verify(task, request_id)


def chain(tmp_path, count):
    plan, _ = setup(tmp_path)
    tasks = []
    for i in range(count):
        name = f'task_{i:06d}'
        tasks.append(dict(task_id=name, depends_on=[tasks[-1]['task_id']] if tasks else [],
                          input_digest=digest(name), route='local', worst_units=1,
                          generated_frames=1, logical_shot_id='shot_A', segment_id=name))
    plan['tasks'] = tasks
    plan['binding_digest'] = digest({k: plan[k] for k in ('production_id', 'policy_digest', 'tasks')})
    plan = seal(plan, 'plan_digest')
    auth = seal(dict(schema_version='fsf_production_budget_v1', production_id='film',
                     currency='USD', unit_scale=100, authorized=True, total_units=count * 4,
                     cleanup_units=count, fixed_delivery_units=1, route_limits={'local': count * 3},
                     max_generated_frames=count * 3, logical_shot_ids=['shot_A']), 'authorization_digest')
    book = Ledger(tmp_path / 'large-ledger.json')
    book.create(auth)
    return plan, book


def cursor(tmp_path, plan):
    return tmp_path / plan['plan_digest'] / 'batch-progress.json'


def test_short_batches_advance_past_growing_accepted_prefix(tmp_path, monkeypatch):
    plan, book = chain(tmp_path, 12)
    clock = Clock()
    monkeypatch.setattr('fsf.production.runner.time.monotonic', clock.monotonic)
    driver = TimedDriver(tmp_path, clock)
    driver.cost = 0.6
    counts = []
    phases = []
    for _ in range(50):
        result = run_batch(plan, state_root=tmp_path, ledger=Ledger(book.path), driver=driver, seconds=2)
        counts.append(len(driver.submitted))
        saved = json.loads(cursor(tmp_path, plan).read_text())
        phases.append(saved['phase'])
        if result['status'] == 'machine_accepted':
            break
        assert result['status'] == 'paused_batch'
    assert result['status'] == 'machine_accepted'
    assert len(driver.submitted) == len(set(driver.submitted)) == 12
    assert all(b > a for a, b in zip(counts, counts[1:]) if a < 12)
    assert 'verify' in phases
    assert result['human_review'] == 'not_reviewed'


def completed(tmp_path, monkeypatch):
    plan, book = setup(tmp_path)
    clock = Clock()
    monkeypatch.setattr('fsf.production.runner.time.monotonic', clock.monotonic)
    driver = TimedDriver(tmp_path, clock)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver)['status'] == 'machine_accepted'
    driver.cost = 1.1
    driver.verifications.clear()
    return plan, book, driver


def test_revalidation_deadline_never_accepts_only_a_prefix(tmp_path, monkeypatch):
    plan, book, driver = completed(tmp_path, monkeypatch)
    first = run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)
    assert first['status'] == 'paused_batch'
    assert json.loads(cursor(tmp_path, plan).read_text())['position'] == 1
    second = run_batch(plan, state_root=tmp_path, ledger=Ledger(book.path), driver=driver, seconds=1)
    assert second['status'] == 'machine_accepted'
    assert driver.verifications == ['a', 'b']
    assert len(driver.submitted) == 2
    # A completed sweep never acts as permanent approval for a later reuse.
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    assert driver.verifications[-1] == 'a'


def test_changed_already_checked_bytes_invalidate_partial_sweep(tmp_path, monkeypatch):
    plan, book, driver = completed(tmp_path, monkeypatch)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    (tmp_path / driver.submitted[0][1]).write_bytes(b'modified after the first verification window')
    with pytest.raises(ProductionError, match='SOURCE_CHANGED'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)
    assert len(driver.submitted) == 2


def test_workflow_invalidation_discards_partial_verification(tmp_path, monkeypatch):
    plan, book, driver = completed(tmp_path, monkeypatch)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    flow = Workflow(tmp_path / plan['plan_digest'] / 'workflow.json')
    flow.invalidate(['a'])
    driver.cost = 0
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver)['status'] == 'machine_accepted'
    assert len(driver.submitted) == 4


@pytest.mark.parametrize('mutation', ['digest', 'position', 'checks'])
def test_corrupt_scan_checkpoint_fails_closed(tmp_path, monkeypatch, mutation):
    plan, book, driver = completed(tmp_path, monkeypatch)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    path = cursor(tmp_path, plan)
    saved = json.loads(path.read_text())
    if mutation == 'digest':
        saved['progress_digest'] = 'f' * 64
    else:
        if mutation == 'position':
            saved['position'] = True
        else:
            saved['checks'] = []
        saved = seal(saved, 'progress_digest')
    path.write_text(json.dumps(saved))
    with pytest.raises(ProductionError, match='INTEGRITY'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=driver)
    assert len(driver.submitted) == 2


def test_2160_task_fixture_resumes_work_and_full_verification(tmp_path, monkeypatch):
    # This seeded prefix isolates scan progress. It is not a fresh 2160-task
    # CLI acceptance run or evidence of neural/media generation.
    plan, book = chain(tmp_path, 2160)
    clock = Clock()
    monkeypatch.setattr('fsf.production.runner.time.monotonic', clock.monotonic)
    driver = TimedDriver(tmp_path, clock)
    root = tmp_path / plan['plan_digest']
    root.mkdir()
    flow = Workflow(root / 'workflow.json')
    state = flow.create(production_id=plan['production_id'], binding_digest=plan['binding_digest'],
                        dependencies={t['task_id']: t['depends_on'] for t in plan['tasks']})
    artifact = tmp_path / 'seeded-prefix.bin'
    artifact.write_bytes(b'CPU fixture, not generated media')
    import hashlib
    evidence = seal(dict(artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                         policy_digest=plan['policy_digest'], verdict='accept'), 'report_digest')
    for i, task in enumerate(plan['tasks'][:-1]):
        item = state['tasks'][task['task_id']]
        item.update(epoch=1, accepted=copy.deepcopy(evidence), attempts=[dict(
            epoch=1, owner='fixture', expires_at=1, request_id=digest(i),
            status='machine_accepted', evidence=copy.deepcopy(evidence))])
    publish(flow.path, seal(state, 'state_digest'))
    original = driver.verify
    def verify(task, request_id):
        if task['task_id'] != plan['tasks'][-1]['task_id']:
            driver.clock.value += 0.01
            return artifact, copy.deepcopy(evidence)
        return original(task, request_id)
    driver.verify = verify
    outcomes = []
    for _ in range(40):
        result = run_batch(plan, state_root=tmp_path, ledger=Ledger(book.path), driver=driver, seconds=1)
        outcomes.append(result['status'])
        if result['status'] == 'machine_accepted':
            break
    assert result['status'] == 'machine_accepted'
    assert 'paused_batch' in outcomes
    assert len(driver.submitted) == 1
    assert driver.submitted[0][0] == plan['tasks'][-1]['task_id']
    final = flow.inspect()
    assert all(t['accepted'] for t in final['tasks'].values())


def test_driver_change_cannot_reuse_old_partial_validation(tmp_path, monkeypatch):
    plan, book, driver = completed(tmp_path, monkeypatch)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    assert driver.verifications == ['a']
    driver.config = {'implementation_version': 'new'}
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=driver, seconds=1)['status'] == 'paused_batch'
    assert driver.verifications == ['a', 'a']
    assert len(driver.submitted) == 2


def test_policy_change_on_fresh_reuse_is_not_accepted(tmp_path, monkeypatch):
    plan, book, driver = completed(tmp_path, monkeypatch)
    original = driver.verify
    def changed(task, request_id):
        artifact, report = original(task, request_id)
        return artifact, seal({**report, 'policy_digest': 'f' * 64}, 'report_digest')
    driver.verify = changed
    with pytest.raises(ProductionError, match='SOURCE_CHANGED'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=driver)
    assert len(driver.submitted) == 2
