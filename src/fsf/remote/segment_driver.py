"""Blender segment recipe driver over the v2 dispatch and object data plane."""
import copy
import json
from pathlib import Path
import shutil
import tempfile

from fsf.core.asset_library import _root
from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,digest,seal,write_json,sha
from fsf.remote.bundle import unpack,pack
from fsf.remote.v2 import prepare_request,worker_identity
from fsf.remote.v2_dispatch import submit,update
from fsf.remote.segments import create_plan
from fsf.runtime.blender import verify_bundle,validate_request as validate_blender_request


def generation_binding(template):
    content=copy.deepcopy(template)
    content.pop('worker_wheel',None)
    content['recipe'].pop('recipe_digest',None)
    content['recipe']['execution_requirements'].pop('encode',None)
    return {'template_digest':digest(content),'source_manifest_digest':digest(template['inputs']),
            'recipe_digest':digest(content['recipe']),'worker_digest':worker_identity(),
            'animation_digest':template['animation_digest'],'render_digest':digest(template['recipe']['execution_requirements']['render'])}


def compile_segments(template,*,group_id,clock,start_frame,end_frame,segment_frames=24,budget_seconds=900):
    if set(template)!={'schema_version','definition','recipe','inputs','worker_wheel','blender_input_id','animation_digest'} or template['schema_version']!='fsf_segment_template_v1':
        raise RemoteError('REMOTE_SEGMENT_MAPPING')
    from fsf.remote.v2 import validate_recipe
    validate_recipe(template['recipe'])
    identifier=template['blender_input_id']
    if identifier not in template['inputs']:raise RemoteError('REMOTE_INPUTS')
    return create_plan(group_id=group_id,clock=clock,start_frame=start_frame,end_frame=end_frame,segment_frames=segment_frames,
        generation=generation_binding(template),encoder_digest=digest(template['recipe']['execution_requirements']['encode']),budget_seconds=budget_seconds)


class BlenderSegmentDriver:
    def __init__(self,template,*,profile,store,store_config,state_root,adapter=None):
        self.template=template;self.profile=profile;self.store=store;self.store_config=store_config
        self.root=_root(state_root);self.adapter=adapter
        self.dispatch_root=self.root/'dispatches';self.dispatch_root.mkdir(exist_ok=True)
        self.transfer=self.root/'transfers';self.transfer.mkdir(exist_ok=True)

    def prepare(self,plan,segment,attempt,remaining):
        if generation_binding(self.template)!=plan['generation']:raise RemoteError('REMOTE_SEGMENT_MAPPING')
        from fsf.remote.v2 import validate_profile,validate_recipe
        from fsf.remote.gpu import authorize_gpu
        profile=validate_profile(self.profile,self.store);recipe=validate_recipe(self.template['recipe'])
        if not profile['authorized'] or recipe['recipe_digest'] not in profile['approved_recipes']:raise RemoteError('REMOTE_NOT_AUTHORIZED')
        authorize_gpu(recipe['execution_requirements'],profile['gpu_limits'])
        from fsf.remote.preflight import validate_definition
        projection={k:v for k,v in recipe.items() if k!='execution_requirements'};projection['schema_version']='fsf_remote_recipe_v1'
        validate_definition(self.template['definition'],seal(projection,'recipe_digest'))
        limits=self.profile['group_limits']
        if (plan['budget_seconds']>limits['max_budget_seconds'] or plan['max_total_attempts']>limits['max_total_attempts']
                or plan['max_attempts_per_segment']>limits['max_attempts_per_segment'] or plan['end_frame']-plan['start_frame']>limits['max_frames']):
            raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
        scratch=Path(tempfile.mkdtemp(prefix='.segment-',dir=self.root))
        try:
            identifier=self.template['blender_input_id'];source_ref=self.template['inputs'][identifier]
            archive=scratch/'source.zip';self.store.get(source_ref,output=archive,state_root=self.transfer)
            scene=scratch/'scene';scene.mkdir();unpack(archive,scene)
            original=read_json(scene/'request.json')
            if original.get('schema_version')!='fsf_blender_request_v2':raise RemoteError('REMOTE_SEGMENT_MAPPING')
            if digest(original['motions'])!=self.template['animation_digest']:raise RemoteError('REMOTE_SEGMENT_MAPPING')
            request=copy.deepcopy(original)
            request.update(start_frame=segment['start_frame'],end_frame=segment['end_frame'],clock=plan['clock'],
                render=self.template['recipe']['execution_requirements']['render'],encode=self.template['recipe']['execution_requirements']['encode'])
            request['animation_range']={'start_frame':plan['start_frame'],'end_frame':plan['end_frame']}
            validate_blender_request(request);write_json(scene/'request.json',request)
            output=scratch/'scene.zip'
            pack(scene,[p.relative_to(scene).as_posix() for p in scene.rglob('*') if p.is_file()],output)
            prefix=self.profile['store_binding']['input_prefix']+plan['generation_digest']+'/'+segment['segment_id']+'/attempt-'+str(attempt)+'/'
            reference=self.store.put(output,artifact_id=identifier,key=prefix+'scene.zip',role=source_ref['role'],media_type=source_ref['media_type'],state_root=self.transfer)
            inputs=copy.deepcopy(self.template['inputs']);inputs[identifier]=reference
            definition=copy.deepcopy(self.template['definition']);definition['parent']['attempt_id']=attempt
            identity={**segment,'group_id':plan['group_id'],'generation_digest':plan['generation_digest'],'clock':plan['clock']}
            result=prepare_request(definition,self.template['recipe'],profile=self.profile,store=self.store,inputs=inputs,
                wheel=self.template['worker_wheel'],segment=identity,output=self.root)
            result['policy']['deadline_seconds']=min(result['policy']['deadline_seconds'],remaining)
            return seal(result,'request_digest')
        finally:shutil.rmtree(scratch)

    def submit(self,request):
        return submit(request,profile=self.profile,store=self.store,state_root=self.dispatch_root,store_config=self.store_config,adapter=self.adapter)

    def observe(self,request):
        return update(request['request_digest'],profile=self.profile,store=self.store,state_root=self.dispatch_root,store_config=self.store_config,adapter=self.adapter)

    def accept(self,request,plan,segment,attempt):
        summary=update(request['request_digest'],profile=self.profile,store=self.store,state_root=self.dispatch_root,
            store_config=self.store_config,action='fetch',adapter=self.adapter)
        directory=self.dispatch_root/request['request_digest']/'accepted'
        candidates=[]
        import zipfile
        for path in (directory/'outputs').rglob('*'):
            if path.is_file() and zipfile.is_zipfile(path):
                unpacked=self.root/('frames-'+sha(path)[:32])
                if not unpacked.exists():
                    scratch=Path(tempfile.mkdtemp(prefix='.frame-open-',dir=self.root))
                    try:unpack(path,scratch);__import__('os').replace(scratch,unpacked)
                    finally:
                        if scratch.exists():shutil.rmtree(scratch)
                if (unpacked/'manifest.json').is_file():
                    manifest=verify_bundle(unpacked)
                    if manifest['schema_version']=='fsf_blender_manifest_v2':candidates.append((unpacked,manifest))
        if len(candidates)!=1:raise RemoteError('REMOTE_SEGMENT_INCOMPLETE')
        _,manifest=candidates[0]
        if (manifest['clock']!=plan['clock'] or manifest['render_profile']!=request['recipe']['execution_requirements']['render']
                or manifest['encode_profile']!=request['recipe']['execution_requirements']['encode']
                or len({f['view_id'] for f in manifest['frame_mapping']})!=1
                or [f['scene_frame'] for f in manifest['frame_mapping']]!=list(range(segment['start_frame'],segment['end_frame']))):
            raise RemoteError('REMOTE_FRAME_SEQUENCE')
        return seal({'schema_version':'fsf_segment_complete_v1','plan_digest':plan['plan_digest'],'generation_digest':plan['generation_digest'],
            'segment':segment,'request_digest':request['request_digest'],'attempt_id':attempt,'result_digest':summary['result_digest'],
            'manifest_digest':manifest['manifest_digest'],'frame_mapping_digest':manifest['frame_mapping_digest'],
            'frame_count':manifest['frame_count'],'motion_review':'not_reviewed'},'complete_digest')

    def frame_bundle(self,complete):
        candidates=[]
        for path in self.root.glob('frames-*/manifest.json'):
            manifest=verify_bundle(path.parent)
            if manifest['manifest_digest']==complete['manifest_digest']:candidates.append((path.parent,manifest))
        if len(candidates)!=1:raise RemoteError('REMOTE_SEGMENT_INCOMPLETE')
        return candidates[0]

    def reuse(self,complete,request,plan,segment):
        """Reuse verified render evidence, never label an old encode as new."""
        from fsf.remote.v2 import validate_profile,check_authorization
        from fsf.remote.gpu import authorize_gpu
        current=validate_profile(self.profile,self.store)
        if (not current['authorized'] or self.template['recipe']['recipe_digest'] not in current['approved_recipes']
                or generation_binding(self.template)!=plan['generation']
                or request['segment']['generation_digest']!=plan['generation_digest']
                or request['recipe']['execution_requirements']['render']!=self.template['recipe']['execution_requirements']['render']
                or request['store_binding']!=current['store_binding']):raise RemoteError('REMOTE_NOT_AUTHORIZED')
        authorize_gpu(request['recipe']['execution_requirements'],current['gpu_limits'],new_submission=False)
        directory=self.dispatch_root/request['request_digest']
        original_profile=read_json(directory/'profile-snapshot.json')
        check_authorization(request,original_profile,self.store,new_submission=False)
        from fsf.remote.v2_transports import V2Adapter
        old_profile=self.profile;old_adapter=self.adapter
        try:
            self.profile=original_profile
            if old_adapter is None:self.adapter=V2Adapter(original_profile,store_config=self.store_config)
            fresh=self.accept(request,plan,segment,complete['attempt_id'])
            if fresh['manifest_digest']!=complete['manifest_digest'] or fresh['frame_mapping_digest']!=complete['frame_mapping_digest']:
                raise RemoteError('REMOTE_SEGMENT_MAPPING')
            return fresh
        finally:self.profile=old_profile;self.adapter=old_adapter
