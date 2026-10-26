"""Versioned financial authority, without interpreting bills as job states."""
import copy
import multiprocessing as mp
import time
from pathlib import Path
import pytest
from fsf.production.ledger import Ledger
from fsf.production.contracts import ProductionError,seal
from test_production_foundation import auth


def authorization(**changes):
    value=auth()
    value.update(schema_version='fsf_production_budget_v2',expires_at_unix=int(time.time())+3600,
        plan_digest='a'*64,segment_ids_by_shot={'shot_A':['S1','S2']},max_repair_rounds=3,reforecast_percent=80)
    value.update(changes)
    return seal(value,'authorization_digest')


def ledger(tmp_path,**changes):
    value=Ledger(tmp_path/'budget.json');value.create(authorization(**changes));return value


def reserve(book,name,amount,*,segment=None,round=1,essential=True):
    return book.reserve(name,route='local',worst_units=amount,generated_frames=1 if segment else 0,binding_digest='b'*64,
        generation_slot={'logical_shot_id':'shot_A','segment_id':segment,'repair_round':round} if segment else None,
        essential=essential)


def test_expiry_blocks_new_work_not_reuse_reconciliation_or_inspect(tmp_path,monkeypatch):
    now=int(time.time());book=ledger(tmp_path,expires_at_unix=now+10)
    before=reserve(book,'existing',200)
    monkeypatch.setattr('fsf.production.ledger.time.time',lambda:now+10)
    assert reserve(book,'existing',200)==before
    assert book.admission_status(worst_units=1)['expired']
    with pytest.raises(ProductionError,match='PRODUCTION_AUTHORIZATION_EXPIRED'):reserve(book,'new',1)
    assert book.reconcile('existing')['status']=='unknown'
    assert book.admission_status()['unknown_units']==200
    assert book.reconcile('existing',settled_units=100)['settled_units']==100


@pytest.mark.parametrize('change',[{'total_units':1000.0},{'expires_at_unix':True},
    {'reforecast_percent':80.0},{'route_limits':{'local':1000.0}}, {'cleanup_units':99},
    {'segment_ids_by_shot':{'other':['S1']}}])
def test_v2_rejects_inexact_or_mismatched_authority(tmp_path,change):
    with pytest.raises(ProductionError):ledger(tmp_path,**change)


def test_cleanup_rounds_up_and_cannot_reauthorize_by_reopening(tmp_path):
    with pytest.raises(ProductionError):ledger(tmp_path,total_units=1001,cleanup_units=100)
    book=ledger(tmp_path,total_units=1001,cleanup_units=101)
    reserve(book,'one',10)
    with pytest.raises(ProductionError,match='PRODUCTION_BUDGET_CONFLICT'):
        book.create(authorization(total_units=2000,cleanup_units=200))


def test_crossing_eighty_requires_forecast_and_never_enables_optional(tmp_path):
    book=ledger(tmp_path);reserve(book,'base',790)
    assert book.admission_status(worst_units=10)['forecast_required']
    with pytest.raises(ProductionError,match='PRODUCTION_FORECAST_REQUIRED'):reserve(book,'cross',10)
    assert book.record_forecast(remaining_worst_units=110,plan_digest='a'*64)['affordable']
    with pytest.raises(ProductionError,match='PRODUCTION_OPTIONAL_WORK_PAUSED'):reserve(book,'enhancement',10,essential=False)
    reserve(book,'cross',10)
    state=book.inspect();assert state['forecast']['remaining_worst_units']==100
    assert book.admission_status(worst_units=100)['global_admission_allowed']
    reserve(book,'remaining',100)
    assert book.inspect()['forecast']['remaining_worst_units']==0
    with pytest.raises(ProductionError,match='PRODUCTION_BUDGET_EXCEEDED'):reserve(book,'too_much',1)


def test_failed_forecast_and_unknown_fees_do_not_disappear(tmp_path):
    book=ledger(tmp_path);reserve(book,'base',790);book.reconcile('base')
    value=book.record_forecast(remaining_worst_units=111,plan_digest='a'*64)
    assert not value['affordable'] and book.admission_status()['unknown_units']==790
    with pytest.raises(ProductionError,match='PRODUCTION_FORECAST_UNAFFORDABLE'):reserve(book,'cross',10)
    with pytest.raises(ProductionError,match='PRODUCTION_FORECAST_PLAN'):
        book.record_forecast(remaining_worst_units=10,plan_digest='c'*64)
    assert book.reconcile('base',settled_units=700)['status']=='settled'
    assert book.inspect()['forecast']['affordable']
    assert book.inspect()['forecast']['remaining_worst_units']==111


def test_shot_round_cas_retains_prior_reservations_and_rejects_aliases(tmp_path):
    book=ledger(tmp_path,max_repair_rounds=2)
    assert book.current_repair_round('shot_A')==1
    with pytest.raises(ProductionError,match='PRODUCTION_REPAIR_ROUND_EMPTY'):
        book.advance_repair_round('shot_A',expected_round=1)
    old=reserve(book,'first',10,segment='S1')
    with pytest.raises(ProductionError,match='PRODUCTION_REPAIR_ROUND_CONFLICT'):
        reserve(book,'premature',10,segment='S2',round=2)
    assert book.advance_repair_round('shot_A',expected_round=1)==2
    assert reserve(book,'first',10,segment='S1')==old
    assert len(book.inspect()['reservations'])==1
    with pytest.raises(ProductionError,match='PRODUCTION_REPAIR_ROUND_CONFLICT'):
        Ledger(book.path).advance_repair_round('shot_A',expected_round=1)
    with pytest.raises(ProductionError,match='PRODUCTION_SEGMENT_NOT_AUTHORIZED'):
        reserve(book,'renamed',10,segment='Alias',round=2)
    with pytest.raises(ProductionError,match='PRODUCTION_REPAIR_ROUND_CONFLICT'):
        reserve(book,'stale',10,segment='S2',round=1)
    reserve(book,'retry',10,segment='S1',round=2)
    with pytest.raises(ProductionError,match='PRODUCTION_REPAIR_EXHAUSTED'):
        book.advance_repair_round('shot_A',expected_round=2)
    assert book.inspect()['consumed_frames']==2


def test_optional_identity_cannot_change_and_empty_generation_universe_is_valid(tmp_path):
    book=ledger(tmp_path,logical_shot_ids=[],segment_ids_by_shot={})
    reserve(book,'optional',10,essential=False)
    with pytest.raises(ProductionError,match='PRODUCTION_REQUEST_CONFLICT'):reserve(book,'optional',10)


def _race(path,barrier,queue,name):
    try:
        barrier.wait(timeout=30)
        reserve(Ledger(Path(path)),name,100)
        queue.put('accepted')
    except ProductionError as exc:queue.put(exc.code)


def test_true_process_forecast_consumption_cannot_overspend(tmp_path):
    book=ledger(tmp_path);reserve(book,'base',790)
    book.record_forecast(remaining_worst_units=110,plan_digest='a'*64)
    ctx=mp.get_context('spawn');barrier=ctx.Barrier(3);queue=ctx.Queue()
    children=[ctx.Process(target=_race,args=(str(book.path),barrier,queue,name)) for name in ('A','B')]
    for child in children:child.start()
    try:
        barrier.wait(timeout=30)
        replies=[queue.get(timeout=30) for _ in children]
        for child in children:child.join(timeout=30);assert child.exitcode==0
        assert sorted(replies)==['PRODUCTION_BUDGET_EXCEEDED','accepted']
        assert book.admission_status()['used_units']==890
        assert book.inspect()['forecast']['remaining_worst_units']==10
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(timeout=10)
