"""Cooperative batch admission never shortens an already admitted submit call."""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from fsf.production.contracts import ProductionError
from fsf.production.store_runner import run_store_batch
from test_production_command_driver import driver
from test_production_store import store_case
from test_driver_settlement import billed_driver


def controlled_admission_clock(monkeypatch, clock):
    # Replace only the coordinator clocks. Process cleanup keeps a real clock.
    monkeypatch.setattr('fsf.production.store_runner.time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
    monkeypatch.setattr('fsf.production.command_driver._processing_time',lambda:clock[0])


@pytest.mark.parametrize('renew', [False, True])
@pytest.mark.parametrize('wall', [100.0, 100.9999])
def test_prepared_lease_keeps_requested_second_across_wall_boundary(tmp_path, monkeypatch, renew, wall):
    plan, store = store_case(tmp_path, 'v1')
    clock = [wall]
    monkeypatch.setattr('fsf.production.store.time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep))
    try:
        task = plan['tasks'][0]
        attempt = store.claim(store.execution(plan), task['task_id'], owner='parent', seconds=1)
        if renew:
            clock[0] += 10
            store.renew_prepared(attempt['request'], attempt['epoch'], owner='parent', seconds=1)
        started = clock[0]
        expires = store.attempt(attempt['request'])['expires']
        assert 1 <= expires - started < 2
        clock[0] = started + 0.0002
        store.mark_sending(attempt['request'], attempt['epoch'])
        assert store.attempt(attempt['request'])['status'] == 'submitted'
        assert store.check()['reservations'] == 1
    finally:
        store.close()


def test_prepared_lease_still_rejects_at_actual_expiration(tmp_path, monkeypatch):
    plan, store = store_case(tmp_path, 'v1')
    clock = [100.9999]
    monkeypatch.setattr('fsf.production.store.time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep))
    try:
        task = plan['tasks'][0]
        attempt = store.claim(store.execution(plan), task['task_id'], owner='parent', seconds=1)
        clock[0] = store.attempt(attempt['request'])['expires']
        with pytest.raises(ProductionError, match='PRODUCTION_LEASE_EXPIRED'):
            store.mark_sending(attempt['request'], attempt['epoch'])
        assert store.attempt(attempt['request'])['status'] == 'reserved'
        assert store.check()['reservations'] == 1
    finally:
        store.close()


def test_admission_survives_intent_commit_but_new_calls_stop_at_boundary(tmp_path, monkeypatch):
    plan,store=store_case(tmp_path,'v1')
    backend,_=driver(tmp_path,plan)
    settings=copy.deepcopy(backend.adapter.settings)
    clock=[100.0]
    observed=[]
    controlled_admission_clock(monkeypatch,clock)

    def call(operation,request,locator,directory,remote=None,*,timeout_seconds=None):
        observed.append((operation,timeout_seconds))
        return {'job_id':request['request_digest']}

    monkeypatch.setattr(backend.adapter,'call',call)
    try:
        with backend.cooperative_execution(101):
            with backend.admit_submission() as admitted:
                assert admitted
                clock[0]=102  # Durable intent publication consumed the tail.
                backend._call('submit',plan['tasks'][0],'a'*64)
            with pytest.raises(ProductionError,match='DEADLINE_EXCEEDED'):
                backend._call('status',plan['tasks'][0],'a'*64)
            with backend.admit_submission() as admitted:
                assert admitted is False
        assert observed==[('submit',None)]
        assert backend.adapter.settings==settings
    finally:
        store.close()


def test_real_submit_at_commit_boundary_is_not_killed_or_repeated(tmp_path,monkeypatch):
    plan,store=store_case(tmp_path,'v1')
    backend=billed_driver(tmp_path,plan)
    clock=[100.0]
    original=store.mark_sending

    def slow_commit(*args):
        value=original(*args)
        clock[0]=102
        return value

    controlled_admission_clock(monkeypatch,clock)
    monkeypatch.setattr(store,'mark_sending',slow_commit)
    try:
        first=run_store_batch(plan,store=store,driver=backend,seconds=1)
        assert first['status']=='paused_batch'
        submitted=json.loads((tmp_path/'submitted.json').read_text())
        assert len(submitted)==1
        assert [json.loads(line)['operation'] for line in (tmp_path/'operations.jsonl').read_text().splitlines()]==['submit']
        assert store.inspect()['used_units']==10
        monkeypatch.setattr(store,'mark_sending',original)
        clock[0]=200
        assert run_store_batch(plan,store=store,driver=backend,seconds=1)['status']=='machine_accepted'
        assert len(json.loads((tmp_path/'submitted.json').read_text()))==2
        assert store.check()['reservations']==2
        assert store.inspect()['used_units']==2
    finally:
        store.close()


def test_status_completes_after_boundary_and_billing_is_deferred_not_zero(tmp_path,monkeypatch):
    plan,store=store_case(tmp_path,'v1')
    backend=billed_driver(tmp_path,plan)
    clock=[100.0]
    original=backend.adapter.call

    def status_boundary(operation,*args,**kwargs):
        result=original(operation,*args,**kwargs)
        if operation=='status':clock[0]=102
        return result

    controlled_admission_clock(monkeypatch,clock)
    monkeypatch.setattr(backend.adapter,'call',status_boundary)
    try:
        assert run_store_batch(plan,store=store,driver=backend,seconds=1)['status']=='paused_batch'
        operations=[json.loads(line)['operation'] for line in (tmp_path/'operations.jsonl').read_text().splitlines()]
        assert operations==['submit','status']
        row=store.db.execute('SELECT status,settled,worst FROM reservations').fetchone()
        assert tuple(row)==('unknown',None,10)
        monkeypatch.setattr(backend.adapter,'call',original)
        clock[0]=200
        assert run_store_batch(plan,store=store,driver=backend,seconds=1)['status']=='machine_accepted'
        assert len(json.loads((tmp_path/'submitted.json').read_text()))==2
        assert store.inspect()['used_units']==2
    finally:
        store.close()


def test_non_deadline_provider_failure_is_not_swallowed_as_pause(tmp_path,monkeypatch):
    plan,store=store_case(tmp_path,'v1')
    backend=billed_driver(tmp_path,plan)
    clock=[100.0]
    controlled_admission_clock(monkeypatch,clock)
    def fail(*args):
        clock[0]=102
        raise ProductionError('PRODUCTION_PROVIDER_INVALID')
    monkeypatch.setattr(backend,'observe',fail)
    try:
        with pytest.raises(ProductionError,match='PROVIDER_INVALID'):
            run_store_batch(plan,store=store,driver=backend,seconds=1)
        assert len(json.loads((tmp_path/'submitted.json').read_text()))==1
        assert store.inspect()['used_units']==10
    finally:
        store.close()
