"""V2 uses real child CPU commands, immutable store objects and provider doubles."""
import copy
import json
from pathlib import Path
import sys

import pytest
from fsf.remote.artifacts import ArtifactStore,store_public_identity
from fsf.remote.contracts import seal,RemoteError
from fsf.remote.v2 import (prepare_request,validate_request,run_worker,collect_result,validate_result,check_authorization)
from fsf.remote.v2_dispatch import submit,update
from fsf.remote.v2_transports import notebook,metadata
from test_remote_artifacts import Store,profile as store_profile
from test_remote_dispatch import remote_wheel,template


@pytest.fixture
def case(tmp_path,remote_wheel):
    for name in ('state','worker','prepared','transfer','accepted','budget'):(tmp_path/name).mkdir()
    service=Store();store=ArtifactStore(store_profile(),client=service)
    recipe=template('cpu.recipe.json');recipe['schema_version']='fsf_remote_recipe_v2'
    recipe['environment']['python']=f'3.{sys.version_info.minor}'
    recipe['execution_requirements']={'render':{'engine':'CYCLES','device':'cpu','backend':'none','device_count':0,'min_vram_mb':0,'samples':4,'seed':0,'threads':2},
        'encode':{'profile':'rc8_control_x264_v1','device':'cpu','encoder':'libx264','ffmpeg_version':'8.0.1','ffmpeg_sha256':'a'*64,'ffmpeg_build_digest':'b'*64,'libx264_build':'core 165 r3222 b35605a'}}
    recipe=seal(recipe,'recipe_digest')
    profile=template('local.profile.json');profile.update(schema_version='fsf_remote_profile_v2',authorized=True,approved_recipes=[recipe['recipe_digest']],
        settings={'python':sys.executable,'worker_root':str(tmp_path/'worker')},
        gpu_limits={'allowed':False,'backends':[],'max_devices':0,'max_vram_mb':0},
        store_binding={'store_id':store.profile['store_id'],'config_digest':store_public_identity(store.profile),'input_prefix':'jobs/inputs/','output_prefix':'jobs/outputs/'})
    profile.update(group_limits={'max_budget_seconds':3600,'max_total_attempts':16,'max_attempts_per_segment':3,'max_frames':96},
        allocation={'allocation_id':'test_allocation','state_root':str(tmp_path/'budget'),'max_submissions':16,'max_execution_seconds':10000})
    definition={'parent':{'job_id':'parent_job','run_id':'parent_run','step_id':'producer','attempt_id':1,
        'plan_digest':'a'*64,'inputs_digest':'b'*64,'expected_contract_digest':None,'requirements_digest':None},
        'parameters':{'value':7},'inputs':{},'video_condition':None,'consistency_requirements':None,'expected_media_contract':None}
    wheel=store.put(remote_wheel,artifact_id='worker_wheel',key='jobs/inputs/'+remote_wheel.name,role='worker_wheel',media_type='application/zip',state_root=tmp_path/'transfer')
    segment={'group_id':'group','segment_id':'segment1','generation_digest':'c'*64,'start_frame':0,'end_frame':24,'clock':{'num':24,'den':1}}
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=wheel,segment=segment,output=tmp_path/'prepared')
    return tmp_path,service,store,profile,request


def test_real_cpu_worker_and_parent_verified_collection(case):
    root,service,store,profile,request=case
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    assert result['status']=='completed'
    collected=collect_result(request,store=store,directory=root/'accepted')
    assert collected['result_digest']==result['result_digest']
    assert json.loads((root/'accepted/accepted/outputs/result.json').read_text())['square']==49
    assert collected['motion_review']=='not_reviewed'
    assert run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])==result


def test_small_notebook_is_independent_of_large_input_sizes(case):
    *_,profile,request=case
    locator={'artifact_id':'request','store_id':'store','object_key':'jobs/request.json','version_id':'fixed',
             'sha256':'a'*64,'size_bytes':8000,'role':'data','media_type':'application/json'}
    document=notebook(request,locator)
    assert len(json.dumps(document).encode())<32*1024
    assert 'request.zip' not in json.dumps(document)
    profile['settings']['accelerator']='synthetic-approved-shape'
    request['policy']['gpu']=False
    cpu=metadata(request,profile,'synthetic-owner')
    assert cpu['enable_gpu'] is False and cpu['machine_shape']==''
    request['policy']['gpu']=True
    gpu=metadata(request,profile,'synthetic-owner')
    assert gpu['enable_gpu'] is True and gpu['machine_shape']=='synthetic-approved-shape'


@pytest.mark.parametrize('failure',['gpu_missing','python_mismatch'])
def test_worker_capability_failure_has_bound_failed_result(case,monkeypatch,failure):
    root,_,store,profile,original=case
    recipe=copy.deepcopy(original['recipe'])
    if failure=='gpu_missing':
        recipe['execution_requirements']['render'].update(device='gpu',backend='cuda',device_count=1,min_vram_mb=14000)
        profile['policy']['gpu']=True
        profile['gpu_limits']={'allowed':True,'backends':['cuda'],'max_devices':1,'max_vram_mb':24576}
        monkeypatch.setenv('FSF_REMOTE_GPU','1')
        def unavailable(**kwargs):raise RemoteError('REMOTE_GPU_UNAVAILABLE')
        monkeypatch.setattr('fsf.remote.v2.cuda_inventory',unavailable)
        expected='REMOTE_GPU_UNAVAILABLE'
    else:
        recipe['environment']['python']='3.13' if sys.version_info.minor!=13 else '3.12'
        expected='REMOTE_ENVIRONMENT_MISMATCH'
    recipe=seal(recipe,'recipe_digest');profile['approved_recipes']=[recipe['recipe_digest']]
    definition={k:original[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={}
    request=prepare_request(definition,recipe,profile=profile,store=store,inputs={},wheel=original['worker_wheel'],segment=original['segment'],output=root/'prepared')
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=recipe['recipe_digest'])
    assert result['status']=='failed' and result['error_code']==expected
    assert {item['artifact_id'] for item in result['artifacts']}=={'environment','child_journal'}
    validate_result(result,request,store)
    directory=root/'worker'/request['request_digest']
    assert json.loads((directory/'child-journal.json').read_text())['steps']=={}
    assert json.loads((directory/'environment.json').read_text())['gpu_render_verified'] is False
    with pytest.raises(RemoteError,match='REMOTE_JOB_NOT_COMPLETED'):
        collect_result(request,store=store,directory=root/'accepted')


def test_gpu_allowance_does_not_turn_cpu_job_into_gpu(case,monkeypatch):
    _,_,store,profile,request=case
    profile['policy']['gpu']=True;profile['gpu_limits']={'allowed':True,'backends':['cuda'],'max_devices':1,'max_vram_mb':24576}
    request['adapter_digest']=__import__('fsf.remote.contracts',fromlist=['digest']).digest(profile)
    request=seal(request,'request_digest')
    monkeypatch.delenv('FSF_REMOTE_GPU',raising=False)
    assert check_authorization(request,profile,store)['policy']['gpu'] is False


class Adapter:
    def __init__(self,store,root):self.store=store;self.root=root;self.count=0;self.provider='completed';self.lose=False
    def prepare(self,*args):pass
    def submit(self,request,locator,directory):
        self.count+=1
        run_worker(request,store=self.store,output_root=self.root/'worker',approved_recipe=request['recipe']['recipe_digest'])
        if self.lose:raise ConnectionError('synthetic response lost')
        return {'request_digest':request['request_digest']}
    def status(self,*args):return self.provider
    def reconcile(self,request,*args):return {'request_digest':request['request_digest']}


def test_durable_dispatch_does_not_resubmit_after_response_loss(case):
    root,_,store,profile,request=case;adapter=Adapter(store,root);adapter.lose=True
    first=submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused-private-path',adapter=adapter)
    assert first['status']=='submission_unknown' and first['submitted_count']==1
    second=submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused-private-path',adapter=adapter)
    assert second==first and adapter.count==1
    final=update(request['request_digest'],profile=profile,store=store,state_root=root/'state',store_config='unused-private-path',action='fetch',adapter=adapter)
    assert final['status']=='verified' and adapter.count==1


def test_cancelled_provider_requires_explicit_recovery(case):
    root,_,store,profile,request=case;adapter=Adapter(store,root)
    submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused',adapter=adapter)
    adapter.provider='cancelled'
    with pytest.raises(RemoteError,match='REMOTE_RECOVERY_REVIEW_REQUIRED'):
        update(request['request_digest'],profile=profile,store=store,state_root=root/'state',store_config='unused',action='fetch',adapter=adapter)
    final=update(request['request_digest'],profile=profile,store=store,state_root=root/'state',store_config='unused',action='fetch',adapter=adapter,recover=True)
    assert final['status']=='recovered_verified' and final['provider_status']=='cancelled'


def test_new_client_state_cannot_reset_shared_allocation_or_duplicate_intent(case):
    root,_,store,profile,request=case;adapter=Adapter(store,root)
    submit(request,profile=profile,store=store,state_root=root/'state',store_config='unused',adapter=adapter)
    another=root/'another-state';another.mkdir()
    with pytest.raises(RemoteError,match='REMOTE_INTENT_CONFLICT'):
        submit(request,profile=profile,store=store,state_root=another,store_config='unused',adapter=adapter)
    assert adapter.count==1


def test_v2_static_preflight_does_not_contact_store(case,monkeypatch):
    root,service,store,profile,request=case
    from fsf.remote.preflight_v2 import preflight
    monkeypatch.setattr(store,'probe',lambda:pytest.fail('network called'))
    result=preflight(request,profile=profile,store=store,store_config='unused',probe=False)
    assert result['status']=='unverified' and not result['ready_for_dispatch'] and not result['verified_execution']


@pytest.mark.parametrize('field,value', [('parent',None),('segment',None),('worker_core_sha256','0'*64)])
def test_result_identity_tampering_rejected(case,field,value):
    root,_,store,profile,request=case
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    result[field]=value;result=seal(result,'result_digest')
    with pytest.raises(RemoteError):validate_result(result,request,store)
