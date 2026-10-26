"""Parent-to-worker bridge uses actual CPU child work and immutable references."""
from pathlib import Path

from fsf.production.contracts import digest,seal
from fsf.production.control import file_identity
from fsf.production.ledger import Ledger
from fsf.production.remote_driver import NativeRecipeDriver
from fsf.production.runner import run_batch
from fsf.remote.contracts import write_json
from fsf.core.edit_store import read_json
from fsf.remote.v2 import run_worker
from test_kaggle_native import native,case,remote_wheel


def test_real_cpu_child_bridge_reuses_without_reexecution(native):
    root,store,profile,old=native
    config=root/'native-config.json';write_json(config,store.profile)
    binding={k:old[k] for k in ('recipe','segment')}
    binding['wheel']=old['worker_wheel'];binding['inputs']={}
    binding['definition']={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    binding['definition']['inputs']={}
    auth=seal({'schema_version':'fsf_production_budget_v1','production_id':'film','currency':'USD','unit_scale':100,
        'authorized':True,'total_units':1000,'cleanup_units':100,'fixed_delivery_units':80,'route_limits':{'local':900},
        'max_generated_frames':1,'logical_shot_ids':[]},'authorization_digest')
    ledger=Ledger(root/'parent-budget.json');ledger.create(auth)
    tasks=[{'task_id':'square','depends_on':[],'input_digest':digest(binding),'route':'local','worst_units':10,
            'generated_frames':0,'logical_shot_id':'diagnostic','segment_id':'square'}]
    base={'schema_version':'fsf_production_batch_v1','production_id':'film','policy_digest':'a'*64,'tasks':tasks}
    base['binding_digest']=digest({k:base[k] for k in ('production_id','policy_digest','tasks')})
    plan=seal(base,'plan_digest')
    calls=[]
    class Adapter:
        def prepare(self,*args):pass
        def submit(self,request,locator,directory):
            calls.append(request['request_digest'])
            run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
            return {'request_digest':request['request_digest']}
        def status(self,*args):return 'completed'
    def verify(task,request,result,folder):
        target=folder/'outputs/result.json'
        assert result['status']=='completed'
        assert read_json(target)=={'operation':'integer_square','input':7,'square':49}
        report=seal({'artifact_sha256':file_identity(target)['sha256'],'policy_digest':'a'*64,
                     'verdict':'accept','scope':'diagnostic_data_not_movie'},'report_digest')
        return target,report
    driver_root=root/'parent-driver';driver_root.mkdir()
    driver=NativeRecipeDriver({'square':binding},profile=profile,store_config=config,state_root=driver_root,
                              verifier=verify,adapter=Adapter())
    assert run_batch(plan,state_root=root,ledger=ledger,driver=driver)['status']=='machine_accepted'
    assert run_batch(plan,state_root=root,ledger=ledger,driver=driver)['status']=='machine_accepted'
    assert len(calls)==1
    # A second parent attempt cannot reuse the allocation claim number merely
    # because its native run_id changed. Re-reading either ID is idempotent.
    first=driver._request(tasks[0],'native_retry_first')
    second=driver._request(tasks[0],'native_retry_second')
    assert second['parent']['attempt_id']==first['parent']['attempt_id']+1
    assert driver._request(tasks[0],'native_retry_first')==first
