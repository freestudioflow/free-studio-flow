"""Finite parent lifecycle and targeted repair; simulations do not prove inference."""
import copy
import pytest
from fsf.production.contracts import ProductionError,digest,seal
from fsf.production.control import file_identity
from fsf.production.ledger import Ledger
from fsf.production.runner import run_batch


def setup(tmp_path):
    auth=seal({"schema_version":"fsf_production_budget_v1","production_id":"film",
        "currency":"USD","unit_scale":100,"authorized":True,"total_units":1000,
        "cleanup_units":100,"fixed_delivery_units":80,"route_limits":{"local":900},
        "max_generated_frames":1000,"logical_shot_ids":["shot_A"]},"authorization_digest")
    ledger=Ledger(tmp_path/"ledger.json");ledger.create(auth)
    tasks=[{"task_id":name,"depends_on":deps,"input_digest":digest(name),"route":"local",
            "worst_units":10,"generated_frames":24,"logical_shot_id":"shot_A","segment_id":name} for name,deps in (("a",[]),("b",["a"]))]
    base={"schema_version":"fsf_production_batch_v1","production_id":"film","policy_digest":"a"*64,"tasks":tasks}
    base['binding_digest']=digest({k:base[k] for k in ('production_id','policy_digest','tasks')})
    plan=seal(base,"plan_digest")
    return plan,ledger


class Driver:
    def __init__(self,root):
        self.root=root;self.submitted=[];self.reject_first=False;self.raise_after_submit=False;self.unknown=False
    def submit(self,task,request_id):
        self.submitted.append((task['task_id'],request_id))
        (self.root/request_id).write_bytes(task['task_id'].encode())
        if self.raise_after_submit:raise RuntimeError('Injected lost reply')
    def observe(self,task,request_id):
        return {'status':'unknown' if self.unknown else 'produced','settled_units':5}
    def verify(self,task,request_id):
        p=self.root/request_id
        rejected=self.reject_first and len([x for x in self.submitted if x[0]==task['task_id']])==1
        report=seal({'artifact_sha256':file_identity(p)['sha256'],'policy_digest':'a'*64,
                     'verdict':'reject' if rejected else 'accept'},'report_digest')
        return p,report


def test_two_tasks_resume_without_new_generation_and_recheck_bytes(tmp_path):
    plan,ledger=setup(tmp_path);driver=Driver(tmp_path)
    assert run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)['status']=='machine_accepted'
    assert len(driver.submitted)==2
    assert run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)['status']=='machine_accepted'
    assert len(driver.submitted)==2
    (tmp_path/driver.submitted[0][1]).write_bytes(b'changed')
    with pytest.raises(ProductionError,match='SOURCE_CHANGED'):
        run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)


def test_reply_lost_reconciles_without_second_submit(tmp_path):
    plan,ledger=setup(tmp_path);driver=Driver(tmp_path);driver.raise_after_submit=True
    with pytest.raises(ProductionError,match='RECONCILE_REQUIRED'):
        run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)
    driver.raise_after_submit=False
    assert run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)['status']=='machine_accepted'
    assert len([x for x in driver.submitted if x[0]=='a'])==1


def test_rejected_attempt_retries_with_new_id_and_keeps_cost(tmp_path):
    plan,ledger=setup(tmp_path);driver=Driver(tmp_path);driver.reject_first=True
    result=run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)
    assert result['status']=='machine_accepted'
    assert len(driver.submitted)==4
    assert len(ledger.inspect()['reservations'])==4
    assert ledger.inspect()['consumed_frames']==96


def test_unknown_does_not_resubmit(tmp_path):
    plan,ledger=setup(tmp_path);driver=Driver(tmp_path);driver.unknown=True
    assert run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)['status']=='unknown'
    assert run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)['status']=='unknown'
    assert len(driver.submitted)==1


def test_forged_plan_rejected_before_driver(tmp_path):
    plan,ledger=setup(tmp_path);driver=Driver(tmp_path)
    plan['tasks'][0]['generated_frames']=0
    plan=seal(plan,'plan_digest')
    with pytest.raises(ProductionError):run_batch(plan,state_root=tmp_path,ledger=ledger,driver=driver)
    assert driver.submitted==[]
