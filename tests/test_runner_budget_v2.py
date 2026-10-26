import time
import pytest
from test_production_runner import setup,Driver
from fsf.production.ledger import Ledger
from fsf.production.contracts import seal,ProductionError,digest
from fsf.production.runner import run_batch


def case(tmp_path,optional=False):
    plan,old=setup(tmp_path)
    if optional:
        plan['schema_version']='fsf_production_batch_v2'
        for t in plan['tasks']:t['essential']=False
        plan['binding_digest']=digest({k:plan[k] for k in ('production_id','policy_digest','tasks')})
        plan=seal(plan,'plan_digest')
    auth=old.inspect()['authorization'];auth.update(schema_version='fsf_production_budget_v2',
        expires_at_unix=int(time.time())+3600,plan_digest=plan['plan_digest'],
        segment_ids_by_shot={'shot_A':['a','b']},max_repair_rounds=3,reforecast_percent=80)
    book=Ledger(tmp_path/'v2.json');book.create(seal(auth,'authorization_digest'))
    return plan,book


def test_parent_automatically_reforecasts_no_human_loop(tmp_path):
    plan,book=case(tmp_path)
    book.reserve('prior',route='local',worst_units=790,generated_frames=0,binding_digest='b'*64)
    driver=Driver(tmp_path)
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)['status']=='machine_accepted'
    state=book.inspect();assert state['forecast']['plan_digest']==plan['plan_digest']
    assert state['reservations']['prior']['settled_units'] is None
    assert len(driver.submitted)==2


def test_parent_advances_global_round_without_reusing_slots(tmp_path):
    plan,book=case(tmp_path);driver=Driver(tmp_path);driver.reject_first=True
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)['status']=='machine_accepted'
    assert book.current_repair_round('shot_A')==3
    assert len(driver.submitted)==4
    slots=[v['generation_slot'] for v in book.inspect()['reservations'].values()]
    assert len({(s['segment_id'],s['repair_round']) for s in slots})==4


def test_optional_work_is_not_started_at_threshold(tmp_path):
    plan,book=case(tmp_path,optional=True)
    book.reserve('prior',route='local',worst_units=790,generated_frames=0,binding_digest='b'*64)
    driver=Driver(tmp_path)
    with pytest.raises(ProductionError,match='OPTIONAL_WORK_PAUSED'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    assert not driver.submitted


def test_admission_failure_reuses_reserved_intent_and_submits_exactly_once(tmp_path,monkeypatch):
    from fsf.production.workflow import Workflow
    plan,book=case(tmp_path);driver=Driver(tmp_path);original=book.reserve
    def refuse(*args,**kwargs):raise ProductionError('PRODUCTION_FORECAST_REQUIRED')
    monkeypatch.setattr(book,'reserve',refuse)
    with pytest.raises(ProductionError,match='FORECAST_REQUIRED'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    flow=Workflow(tmp_path/plan['plan_digest']/'workflow.json')
    reserved=flow.inspect()['tasks']['a']['attempts'][0]['request_id']
    assert not driver.submitted
    monkeypatch.setattr(book,'reserve',original)
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)['status']=='machine_accepted'
    assert driver.submitted[0]==('a',reserved) and len(driver.submitted)==2
    assert len(flow.inspect()['tasks']['a']['attempts'])==1


def test_permission_expiry_after_reservation_never_submits(tmp_path,monkeypatch):
    from fsf.production.workflow import Workflow
    plan,book=case(tmp_path);driver=Driver(tmp_path);original=book.reserve
    expires=book.inspect()['authorization']['expires_at_unix']
    real_time=time.time
    def reserve_then_expire(*args,**kwargs):
        value=original(*args,**kwargs)
        monkeypatch.setattr(time,'time',lambda:expires+1)
        return value
    monkeypatch.setattr(book,'reserve',reserve_then_expire)
    with pytest.raises(ProductionError,match='AUTHORIZATION_EXPIRED'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    assert not driver.submitted
    flow=Workflow(tmp_path/plan['plan_digest']/'workflow.json')
    reserved=flow.inspect()['tasks']['a']['attempts'][0]['request_id']
    monkeypatch.setattr(time,'time',real_time);monkeypatch.setattr(book,'reserve',original)
    assert run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)['status']=='machine_accepted'
    assert driver.submitted[0]==('a',reserved)


@pytest.mark.parametrize('first_status',['running','unknown'])
def test_repair_wait_still_observes_existing_sibling(tmp_path,first_status):
    plan,old=case(tmp_path)
    plan['tasks'][1]['depends_on']=[]
    plan['binding_digest']=digest({k:plan[k] for k in ('production_id','policy_digest','tasks')})
    plan=seal(plan,'plan_digest')
    auth=old.inspect()['authorization'];auth['plan_digest']=plan['plan_digest']
    book=Ledger(tmp_path/'drain.json');book.create(seal(auth,'authorization_digest'))
    class AsyncDriver(Driver):
        observations=0
        def observe(self,task,request_id):
            if task['task_id']=='b':
                self.observations+=1
                if self.observations==1:return {'status':first_status}
            return {'status':'produced','settled_units':5}
        def verify(self,task,request_id):
            self.reject_first=task['task_id']=='a'
            return super().verify(task,request_id)
    driver=AsyncDriver(tmp_path)
    result=run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    if first_status=='unknown':
        assert result['status']=='unknown'
        result=run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    assert result['status']=='machine_accepted' and driver.observations>=2
    assert [t for t,_ in driver.submitted].count('b')==1


@pytest.mark.parametrize('field',['policy_digest','input_digest'])
def test_malformed_binding_digest_is_rejected_before_dispatch(tmp_path,field):
    plan,book=case(tmp_path)
    if field=='policy_digest':plan[field]='invalid'
    else:plan['tasks'][0][field]='invalid'
    plan['binding_digest']=digest({k:plan[k] for k in ('production_id','policy_digest','tasks')})
    plan=seal(plan,'plan_digest');driver=Driver(tmp_path)
    with pytest.raises(ProductionError,match='PLAN_INVALID'):
        run_batch(plan,state_root=tmp_path,ledger=book,driver=driver)
    assert not driver.submitted
