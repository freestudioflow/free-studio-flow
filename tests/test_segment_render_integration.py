"""Four real CPU Blender segments through v2, then frame-exact final delivery."""
import copy
import json
import os
from pathlib import Path
import sys

import pytest
from fsf.remote.artifacts import ArtifactStore,store_public_identity
from fsf.remote.bundle import pack
from fsf.remote.contracts import seal,digest,write_json,sha
from fsf.remote.v2 import run_worker
from fsf.remote.segments import run
from fsf.remote.segment_driver import compile_segments,BlenderSegmentDriver
from fsf.remote.segment_delivery import encoder_pin,assemble
from test_remote_artifacts import Store,profile as store_profile
from test_remote_dispatch import remote_wheel,template
from test_blender_workflow import real_scene,request


@pytest.mark.parametrize('clock',[{'num':24,'den':1},{'num':24000,'den':1001}])
def test_four_global_segments_retry_and_final_frame_sequence(real_scene,remote_wheel,tmp_path_factory,monkeypatch,clock):
    from fsf.assembly.fast_assembly import require_ffmpeg
    executable,source,record=real_scene
    root=tmp_path_factory.mktemp('seg')
    for name in ('state','worker','inputs','transfer','output','driver','budget'):(root/name).mkdir()
    ffmpeg=require_ffmpeg();pin=encoder_pin(ffmpeg)
    monkeypatch.setenv('PATH',str(Path(executable).parent)+os.pathsep+str(Path(ffmpeg).parent)+os.pathsep+os.environ.get('PATH',''))
    render={'engine':'CYCLES','device':'cpu','backend':'none','device_count':0,'min_vram_mb':0,'samples':4,'seed':0,'threads':2}
    motion=[{'object_id':'Camera','keys':[{'frame':0,'location':[0,0,8],'rotation_euler':[0,0,0]},
                                        {'frame':95,'location':[1,0,8],'rotation_euler':[0,0,0]}]}]
    scene=request(record,schema_version='fsf_blender_request_v2',clock=clock,start_frame=0,end_frame=96,
        render=render,encode=pin,animation_range={'start_frame':0,'end_frame':96},motions=motion)
    for name in ('record.json','scene.blend'):(root/'inputs'/name).write_bytes((source/name).read_bytes())
    write_json(root/'inputs/request.json',scene)
    source_archive=root/'source.zip';pack(root/'inputs',['record.json','scene.blend','request.json'],source_archive)
    before_source=sha(source_archive)
    service=Store();store=ArtifactStore(store_profile(),client=service)
    source_ref=store.put(source_archive,artifact_id='scene',key='jobs/inputs/source.zip',role='data',media_type='application/zip',state_root=root/'transfer')
    wheel=store.put(remote_wheel,artifact_id='worker_wheel',key='jobs/inputs/'+remote_wheel.name,role='worker_wheel',media_type='application/zip',state_root=root/'transfer')
    recipe=template('blender.recipe.json');recipe.update(schema_version='fsf_remote_recipe_v2',execution_requirements={'render':render,'encode':pin})
    recipe['environment']['python']=f'3.{sys.version_info.minor}'
    recipe['steps'][0]['args']+=['--executable',executable]
    recipe=seal(recipe,'recipe_digest')
    profile=template('local.profile.json');profile.update(schema_version='fsf_remote_profile_v2',authorized=True,approved_recipes=[recipe['recipe_digest']],
        settings={'python':sys.executable,'worker_root':str(root/'worker')},gpu_limits={'allowed':False,'backends':[],'max_devices':0,'max_vram_mb':0},
        store_binding={'store_id':store.profile['store_id'],'config_digest':store_public_identity(store.profile),'input_prefix':'jobs/inputs/','output_prefix':'jobs/outputs/'})
    profile.update(group_limits={'max_budget_seconds':3600,'max_total_attempts':16,'max_attempts_per_segment':3,'max_frames':96},
        allocation={'allocation_id':'test_allocation','state_root':str(root/'budget'),'max_submissions':16,'max_execution_seconds':10000})
    profile['policy']['deadline_seconds']=120
    definition={'parent':{'job_id':'parent','run_id':'run','step_id':'blender','attempt_id':1,'plan_digest':'a'*64,'inputs_digest':'b'*64,'expected_contract_digest':None,'requirements_digest':None},
                'parameters':{},'inputs':{'scene':'scene.zip'},'video_condition':None,'consistency_requirements':None,'expected_media_contract':None}
    value={'schema_version':'fsf_segment_template_v1','definition':definition,'recipe':recipe,'inputs':{'scene':source_ref},
           'worker_wheel':wheel,'blender_input_id':'scene','animation_digest':digest(motion)}
    plan=compile_segments(value,group_id='four_segments',clock=clock,start_frame=0,end_frame=96,budget_seconds=600)
    class Adapter:
        def __init__(self):self.calls=[];self.failed=True;self.states={}
        def prepare(self,*a):pass
        def submit(self,req,*a):
            self.calls.append((req['segment']['segment_id'],req['parent']['attempt_id']))
            if req['segment']['segment_id']=='segment_0003' and self.failed:
                self.states[req['request_digest']]='failed';self.failed=False
            else:
                result=run_worker(req,store=store,output_root=root/'worker',approved_recipe=req['recipe']['recipe_digest'])
                self.states[req['request_digest']]=result['status']
            return {'request_digest':req['request_digest']}
        def status(self,req,*a):return self.states[req['request_digest']]
        def reconcile(self,*a):return None
    adapter=Adapter()
    driver=BlenderSegmentDriver(value,profile=profile,store=store,store_config='unused',state_root=root/'driver',adapter=adapter)
    assert run(plan,state_root=root/'state',driver=driver)['status']=='failed_segment'
    assert run(plan,state_root=root/'state',driver=driver)['status']=='completed'
    assert adapter.calls==[('segment_0001',1),('segment_0002',1),('segment_0003',1),('segment_0003',2),('segment_0004',1)]
    delivered=assemble(plan,driver=driver,state_root=root/'state',output_root=root/'output',encoder=pin,ffmpeg=ffmpeg)
    manifest=json.loads((root/'output'/delivered['delivery_digest']/'manifest.json').read_text())
    assert manifest['frame_count']==96 and manifest['clock']==clock and manifest['ssim']>=.98
    assert manifest['motion_review']=='not_reviewed'
    from fsf.runtime.blender import condition_request,bundle_asset_record
    condition=condition_request(root/'output'/delivered['delivery_digest'],'assembled')
    assert condition['artifact']['sha256']==manifest['artifact_sha256'] and condition['frame_count']==96
    asset=bundle_asset_record(root/'output'/delivered['delivery_digest'],asset_id='segmented_control',version='1',source_prompt='A controlled camera move')
    assert asset['primary_artifact_id']=='control' and not asset['ref_images']
    assert len(adapter.calls)==5 and sha(source_archive)==before_source
    state=json.loads((root/'state'/plan['plan_digest']/'state.json').read_text())
    first=driver.frame_bundle(state['segments']['segment_0001']['complete'])[1]
    second=driver.frame_bundle(state['segments']['segment_0002']['complete'])[1]
    assert first['frame_mapping'][-1]['scene_frame']==23 and second['frame_mapping'][0]['scene_frame']==24
    assert second['frame_mapping'][0]['projection']['Cube'][0]==pytest.approx(.5-24/95*50/(36*8),abs=.002)
    # An encode-only intent may reuse validated PNG evidence. A bad new pin
    # must still block encoding and must never trigger another render.
    from fsf.remote.contracts import RemoteError
    changed=copy.deepcopy(value)
    changed['recipe']['execution_requirements']['encode']['ffmpeg_build_digest']='0'*64
    changed['recipe']=seal(changed['recipe'],'recipe_digest')
    new_profile=copy.deepcopy(profile);new_profile['approved_recipes'].append(changed['recipe']['recipe_digest'])
    new_driver=BlenderSegmentDriver(changed,profile=new_profile,store=store,store_config='unused',state_root=root/'driver',adapter=adapter)
    new_plan=compile_segments(changed,group_id='four_segments',clock=clock,start_frame=0,end_frame=96,budget_seconds=600)
    assert new_plan['generation_digest']==plan['generation_digest'] and new_plan['encoder_digest']!=plan['encoder_digest']
    assert run(new_plan,state_root=root/'state',driver=new_driver)['status']=='completed'
    assert len(adapter.calls)==5
    with pytest.raises(RemoteError,match='REMOTE_ENCODER_VERSION'):
        assemble(new_plan,driver=new_driver,state_root=root/'state',output_root=root/'output',encoder=changed['recipe']['execution_requirements']['encode'],ffmpeg=ffmpeg)
    assert len(adapter.calls)==5
