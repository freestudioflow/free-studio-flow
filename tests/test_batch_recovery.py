"""Legacy JSON, interrupted checkpoints and real process restart, not media QA."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from fsf.production.contracts import ProductionError, publish, seal
from fsf.core.edit_store import EditStoreError
from fsf.production.runner import run_batch
from fsf.production.workflow import Workflow
from test_batch_progress import completed, cursor
from test_production_command_driver import driver
from test_production_runner import Driver, setup
from test_runner_budget_v2 import case as v2_case


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_pre_cursor_workflow_and_budget_reuse_preserves_history(tmp_path, version):
    plan, book = setup(tmp_path) if version == 'v1' else v2_case(tmp_path)
    backend = Driver(tmp_path)
    folder = tmp_path / plan['plan_digest']
    folder.mkdir()
    flow = Workflow(folder / 'workflow.json')
    flow.create(production_id=plan['production_id'], binding_digest=plan['binding_digest'],
                dependencies={task['task_id']: task['depends_on'] for task in plan['tasks']})
    # Write through the pre-cursor public workflow/ledger APIs, not run_batch.
    for task in plan['tasks']:
        claim = flow.claim(task['task_id'], owner='legacy')
        token = {k: claim[k] for k in ('epoch', 'request_id')}
        book.reserve(claim['request_id'], route=task['route'], worst_units=task['worst_units'],
                     generated_frames=task['generated_frames'], binding_digest=task['input_digest'],
                     generation_slot={'logical_shot_id': task['logical_shot_id'],
                                      'segment_id': task['segment_id'], 'repair_round': 1})
        flow.record(task['task_id'], status='submitted', **token)
        backend.submit(task, claim['request_id'])
        flow.record(task['task_id'], status='produced', **token)
        artifact, evidence = backend.verify(task, claim['request_id'])
        for status in ('verified', 'machine_accepted'):
            flow.record(task['task_id'], status=status, artifact_path=artifact, evidence=evidence, **token)
        book.reconcile(claim['request_id'], settled_units=5)
    before = (flow.path.read_bytes(), book.path.read_bytes())
    assert not cursor(tmp_path, plan).exists()
    for _ in range(2):
        assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)['status'] == 'machine_accepted'
    assert (flow.path.read_bytes(), book.path.read_bytes()) == before
    assert len(backend.submitted) == 2


@pytest.mark.parametrize('phase', ['work', 'verify'])
def test_checkpoint_write_failure_repeats_validation_not_dispatch(tmp_path, monkeypatch, phase):
    import fsf.production.runner as runner
    plan, book = setup(tmp_path)
    backend = Driver(tmp_path)
    original = runner.publish
    failed = []
    def interrupted(path, value):
        if Path(path).name == 'batch-progress.json' and value['phase'] == phase and not failed:
            failed.append(True)
            raise OSError('injected checkpoint publication failure')
        return original(path, value)
    monkeypatch.setattr(runner, 'publish', interrupted)
    with pytest.raises(EditStoreError, match='EDIT_IO'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)
    first_ids = list(backend.submitted)
    monkeypatch.setattr(runner, 'publish', original)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)['status'] == 'machine_accepted'
    assert len(backend.submitted) == len(set(backend.submitted)) == 2
    assert backend.submitted[:len(first_ids)] == first_ids
    assert len(book.inspect()['reservations']) == 2


def test_cancelled_verification_resumes_without_new_attempt(tmp_path, monkeypatch):
    plan, book, backend = completed(tmp_path, monkeypatch)
    before = book.path.read_bytes()
    original = backend.verify
    def interrupt(*args):
        raise KeyboardInterrupt()
    backend.verify = interrupt
    with pytest.raises(KeyboardInterrupt):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)
    backend.verify = original
    backend.cost = 0
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)['status'] == 'machine_accepted'
    assert book.path.read_bytes() == before and len(backend.submitted) == 2


def test_new_reuse_rechecks_revoked_evidence_without_repair_dispatch(tmp_path):
    plan, book = setup(tmp_path)
    backend = Driver(tmp_path)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)['status'] == 'machine_accepted'
    before = book.path.read_bytes()
    original = backend.verify
    def revoked(task, request_id):
        artifact, report = original(task, request_id)
        return artifact, seal({**report, 'verdict': 'reject'}, 'report_digest')
    backend.verify = revoked
    with pytest.raises(ProductionError, match='SOURCE_CHANGED'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)
    assert book.path.read_bytes() == before and len(backend.submitted) == 2


@pytest.mark.parametrize('mutation', ['version', 'plan', 'phase', 'position', 'checks'])
def test_unknown_or_malformed_progress_never_submits(tmp_path, monkeypatch, mutation):
    plan, book, backend = completed(tmp_path, monkeypatch)
    assert run_batch(plan, state_root=tmp_path, ledger=book, driver=backend, seconds=1)['status'] == 'paused_batch'
    path = cursor(tmp_path, plan)
    value = json.loads(path.read_text())
    changes = {'version': ('schema_version', 'unknown'), 'plan': ('plan_digest', 'f' * 64),
               'phase': ('phase', 'skip_verification'), 'position': ('position', 99),
               'checks': ('checks', [{'path': 7, 'stamp': []}])}
    key, replacement = changes[mutation]
    value[key] = replacement
    publish(path, seal(value, 'progress_digest'))
    with pytest.raises(ProductionError, match='INTEGRITY'):
        run_batch(plan, state_root=tmp_path, ledger=book, driver=backend)
    assert len(backend.submitted) == 2


def cli_arguments(tmp_path, plan, book, config):
    for name, value in (('plan', plan), ('driver', config)):
        (tmp_path / (name + '.json')).write_text(json.dumps(value))
    return ['production', 'run-batch', str(tmp_path / 'plan.json'),
            '--driver', str(tmp_path / 'driver.json'), '--ledger', str(book.path),
            '--state', str(tmp_path / 'cli-state'), '--seconds', '60']


HARNESS = '''import json,sys,time
from pathlib import Path
from fsf.cli.production import main
from fsf.production.command_driver import CommandDriver
import fsf.production.runner as runner
mode,marker,*args=sys.argv[1:]
marker=Path(marker)
def reached(request_id):
 marker.write_text(json.dumps({'mode':mode,'request_id':request_id}))
 deadline=time.monotonic()+60
 while time.monotonic()<deadline:time.sleep(0.01)
 raise RuntimeError('fault harness was not terminated')
if mode=='lost_reply':
 original=CommandDriver._save_identity
 def save(self,request_id,identity):reached(request_id);return original(self,request_id,identity)
 CommandDriver._save_identity=save
elif mode=='checkpoint':
 original=runner.publish
 def publish(path,value):
  result=original(path,value)
  if Path(path).name=='batch-progress.json' and value['phase']=='work':reached('checkpoint')
  return result
 runner.publish=publish
else:
 original=runner._verify_accepted
 def verify(task,item,driver):
  value=original(task,item,driver)
  progress=next((marker.parent/'cli-state').glob('*/batch-progress.json'),None)
  if progress and json.loads(progress.read_text())['phase']=='verify':reached(item['attempts'][-1]['request_id'])
  return value
 runner._verify_accepted=verify
assert args[0]=='production'
raise SystemExit(main(args[1:]))
'''


@pytest.mark.parametrize('fault', ['lost_reply', 'checkpoint', 'verification'])
def test_independent_cli_restart_after_acknowledged_fault(tmp_path, fault):
    plan, book = setup(tmp_path)
    _, config = driver(tmp_path, plan)
    args = cli_arguments(tmp_path, plan, book, config)
    script = tmp_path / 'fault_harness.py'
    script.write_text(HARNESS)
    marker = tmp_path / 'fault-reached.json'
    child = subprocess.Popen([sys.executable, str(script), fault, str(marker), *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), 'worker did not acknowledge the fault point'
        acknowledged = json.loads(marker.read_text())
        assert acknowledged['mode'] == fault
        before_ids = json.loads((tmp_path / 'submitted.json').read_text())
        if fault == 'lost_reply':
            assert len(before_ids) == 1
            assert not (tmp_path / 'cli-state' / 'driver' / before_ids[0] / 'provider.json').exists()
        if fault == 'verification':
            assert len(before_ids) == 2
            # The OS-held parent lock blocks a second real CLI, without submits.
            other = subprocess.run([sys.executable, '-m', 'fsf.cli', *args],
                                   capture_output=True, text=True, timeout=30)
            assert other.returncode == 2
            assert json.loads(other.stdout)['error']['code'] == 'PRODUCTION_LOCK_TIMEOUT'
            assert json.loads((tmp_path / 'submitted.json').read_text()) == before_ids
        child.kill()
        child.communicate(timeout=10)
        for _ in range(2):
            resumed = subprocess.run([sys.executable, '-m', 'fsf.cli', *args],
                                     capture_output=True, text=True, timeout=90)
            assert resumed.returncode == 0, resumed.stdout + resumed.stderr
            assert json.loads(resumed.stdout)['result']['status'] == 'machine_accepted'
        calls = [json.loads(line) for line in (tmp_path / 'operations.jsonl').read_text().splitlines()]
        submits = [call['request_id'] for call in calls if call['operation'] == 'submit']
        assert len(submits) == len(set(submits)) == 2
        if fault == 'lost_reply':
            assert any(call['operation'] == 'reconcile' and call['request_id'] == before_ids[0] for call in calls)
        ledger = book.inspect()
        assert len(ledger['reservations']) == 2
        # The generic command status has no billing receipt. Accepted artifacts
        # must not invent settlement or release their worst-case reservations.
        assert all(entry['status'] == 'unknown' and entry['settled_units'] is None
                   and entry['worst_units'] == 10 for entry in ledger['reservations'].values())
        state = json.loads((tmp_path / 'cli-state' / plan['plan_digest'] / 'workflow.json').read_text())
        assert all(item['accepted'] and len(item['attempts']) == 1 for item in state['tasks'].values())
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
