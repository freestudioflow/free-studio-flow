"""Durable segment coordination: pause, reconcile and retry bounded attempts."""
import copy
from fractions import Fraction
from pathlib import Path
import time

from fsf.core.asset_library import _root,_no_links
from fsf.core.edit_store import edit_lock,read_json
from fsf.remote.artifacts import shape
from fsf.remote.contracts import RemoteError,digest,seal,write_json


def validate_plan(plan):
    shape(plan,'fsf_segment_plan_v1')
    if seal(plan,'plan_digest')['plan_digest']!=plan['plan_digest'] or digest(plan['generation'])!=plan['generation_digest']:
        raise RemoteError('REMOTE_INTEGRITY')
    if (plan['clock']['num'],plan['clock']['den']) not in {(24,1),(24000,1001)}:raise RemoteError('REMOTE_SEGMENT_MAPPING')
    position=plan['start_frame'];seen=set()
    for segment in plan['segments']:
        if segment['segment_id'] in seen or segment['start_frame']!=position or not position<segment['end_frame']<=position+96:
            raise RemoteError('REMOTE_SEGMENT_MAPPING')
        seen.add(segment['segment_id']);position=segment['end_frame']
    if position!=plan['end_frame'] or plan['max_total_attempts']<len(plan['segments']):raise RemoteError('REMOTE_SEGMENT_MAPPING')
    return copy.deepcopy(plan)


def create_plan(*,group_id,clock,start_frame,end_frame,segment_frames,generation,encoder_digest,
                budget_seconds=900,max_attempts_per_segment=3,max_total_attempts=None):
    if type(segment_frames) is not int or not 1<=segment_frames<=96:raise RemoteError('REMOTE_SEGMENT_MAPPING')
    segments=[]
    for index,start in enumerate(range(start_frame,end_frame,segment_frames)):
        segments.append({'segment_id':f'segment_{index+1:04d}','start_frame':start,'end_frame':min(start+segment_frames,end_frame)})
    return validate_plan(seal({'schema_version':'fsf_segment_plan_v1','group_id':group_id,'clock':clock,
        'start_frame':start_frame,'end_frame':end_frame,'generation':generation,'generation_digest':digest(generation),
        'encoder_digest':encoder_digest,'segments':segments,'budget_seconds':budget_seconds,
        'max_attempts_per_segment':max_attempts_per_segment,'max_total_attempts':max_total_attempts or len(segments)*max_attempts_per_segment},'plan_digest'))


def _state(path,plan):
    if path.exists():
        value=read_json(path)
        if seal(value,'state_digest')['state_digest']!=value.get('state_digest') or value['plan_digest']!=plan['plan_digest']:
            raise RemoteError('REMOTE_INTEGRITY')
        return value
    return {'schema_version':'fsf_segment_state_v1','plan_digest':plan['plan_digest'],'created_at':time.time(),
            'attempt_count':0,'segments':{s['segment_id']:{'attempts':[],'complete':None} for s in plan['segments']}}


def pause(plan,*,state_root):
    plan=validate_plan(plan);root=_root(state_root);directory=root/plan['plan_digest'];directory.mkdir(exist_ok=True)
    _no_links(directory)
    # This request does not wait for the renderer lock. In-flight work finishes;
    # no further segment is submitted once the marker is observed.
    marker=directory/'pause.request'
    if marker.exists():_no_links(marker)
    marker.touch(exist_ok=True)
    return {'paused':True,'inflight_policy':'finish_then_checkpoint','plan_digest':plan['plan_digest']}


def resume(plan,*,state_root):
    plan=validate_plan(plan);root=_root(state_root)
    with edit_lock(root/'.segment-resume'):
        (root/plan['plan_digest']/'pause.request').unlink(missing_ok=True)
    return {'paused':False,'plan_digest':plan['plan_digest']}


def validate_complete(complete,plan,segment,request,attempt):
    shape(complete,'fsf_segment_complete_v1')
    if (seal(complete,'complete_digest')['complete_digest']!=complete['complete_digest']
            or complete['plan_digest']!=plan['plan_digest'] or complete['generation_digest']!=plan['generation_digest']
            or complete['segment']!=segment or complete['request_digest']!=request['request_digest'] or complete['attempt_id']!=attempt
            or complete['frame_count']!=segment['end_frame']-segment['start_frame']):raise RemoteError('REMOTE_SEGMENT_MAPPING')
    return complete


def consumed_budget(state):
    return state.get('delivery_seconds',0)+sum(
        a.get('charged_seconds',a.get('budget_seconds',0))
        for item in state['segments'].values() for a in item['attempts']
        if a.get('status')!='reused_render')


def run(plan,*,state_root,driver):
    """Driver is a product adapter; it cannot mark a segment complete on submit."""
    plan=validate_plan(plan);root=_root(state_root);directory=root/plan['plan_digest'];directory.mkdir(exist_ok=True)
    _no_links(directory)
    with edit_lock(directory/'.run-target'):
        path=directory/'state.json';state=_state(path,plan)
        def save():write_json(path,seal(state,'state_digest'))
        if (directory/'plan.json').exists():
            if read_json(directory/'plan.json')!=plan:raise RemoteError('REMOTE_INTEGRITY')
        else:write_json(directory/'plan.json',plan)
        save()
        for segment in plan['segments']:
            current=state['segments'][segment['segment_id']]
            if not current['attempts'] and hasattr(driver,'reuse'):
                for previous_plan_path in root.glob('*/plan.json'):
                    if previous_plan_path.parent==directory:continue
                    old_plan=validate_plan(read_json(previous_plan_path))
                    if old_plan['generation_digest']!=plan['generation_digest'] or old_plan['clock']!=plan['clock']:continue
                    old_state=_state(previous_plan_path.parent/'state.json',old_plan)
                    old_entry=old_state['segments'].get(segment['segment_id'])
                    if not old_entry or not old_entry['complete'] or old_entry['complete']['segment']!=segment:continue
                    complete=old_entry['complete'];attempt=old_entry['attempts'][-1]
                    request=read_json(previous_plan_path.parent/(segment['segment_id']+'-'+str(attempt['attempt_id'])+'.json'))
                    fresh=driver.reuse(complete,request,plan,segment)
                    validate_complete(fresh,plan,segment,request,attempt['attempt_id'])
                    current['complete']=fresh
                    current['attempts']=[{**attempt,'status':'reused_render'}]
                    write_json(directory/(segment['segment_id']+'-'+str(attempt['attempt_id'])+'.json'),request)
                    write_json(directory/(segment['segment_id']+'.complete.json'),fresh);save();break
            if current['complete']:
                previous=current['attempts'][-1]
                request=read_json(directory/(segment['segment_id']+'-'+str(previous['attempt_id'])+'.json'))
                complete=driver.reuse(current['complete'],request,plan,segment) if previous['status']=='reused_render' else driver.accept(request,plan,segment,previous['attempt_id'])
                validate_complete(complete,plan,segment,request,previous['attempt_id'])
                if complete!=current['complete']:raise RemoteError('REMOTE_SEGMENT_MAPPING')
                continue
            if (directory/'pause.request').exists():return {'status':'paused','plan_digest':plan['plan_digest'],'attempt_count':state['attempt_count']}
            attempts=current['attempts']
            attempt=attempts[-1] if attempts and attempts[-1]['status'] not in {'failed','cancelled'} else None
            if attempt is None:
                remaining=plan['budget_seconds']-consumed_budget(state)
                if remaining<1:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
                if len(attempts)>=plan['max_attempts_per_segment'] or state['attempt_count']>=plan['max_total_attempts']:
                    raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
                number=len(attempts)+1
                request=driver.prepare(plan,segment,number,int(remaining))
                write_json(directory/(segment['segment_id']+'-'+str(number)+'.json'),request)
                attempt={'attempt_id':number,'request_digest':request['request_digest'],'status':'submission_intent','provider_status':'unknown',
                         'started_at':time.time(),'budget_seconds':request.get('policy',{}).get('deadline_seconds',int(remaining))}
                attempts.append(attempt);state['attempt_count']+=1;save()
                observation=driver.submit(request)
                attempt.update(status=observation['status'],provider_status=observation.get('provider_status','unknown'));save()
            else:
                request=read_json(directory/(segment['segment_id']+'-'+str(attempt['attempt_id'])+'.json'))
            observation=driver.observe(request)
            attempt.update(status=observation['status'],provider_status=observation['provider_status']);save()
            if observation['provider_status'] in {'completed','failed','cancelled'}:
                attempt.setdefault('charged_seconds',min(attempt['budget_seconds'],max(0,time.time()-attempt['started_at'])))
                save()
            if observation['provider_status'] in {'failed','cancelled'}:
                attempt['status']=observation['provider_status'];save()
                return {'status':'failed_segment','segment_id':segment['segment_id'],'attempt_count':state['attempt_count']}
            if observation['status']=='submission_unknown' or observation['provider_status']=='unknown':
                return {'status':'reconcile_required','segment_id':segment['segment_id'],'attempt_count':state['attempt_count']}
            if observation['provider_status']!='completed':
                return {'status':'running','segment_id':segment['segment_id'],'attempt_count':state['attempt_count']}
            try:complete=driver.accept(request,plan,segment,attempt['attempt_id'])
            except RemoteError as exc:
                if exc.code in {'REMOTE_QC_FAILED','REMOTE_GPU_DEVICE_MISMATCH','REMOTE_FRAME_SEQUENCE'}:
                    attempt.update(status='failed',error_code=exc.code);save()
                    return {'status':'failed_segment','segment_id':segment['segment_id'],'attempt_count':state['attempt_count']}
                raise
            current['complete']=validate_complete(complete,plan,segment,request,attempt['attempt_id']);save()
            write_json(directory/(segment['segment_id']+'.complete.json'),complete)
        return {'status':'completed','plan_digest':plan['plan_digest'],'attempt_count':state['attempt_count'],
                'complete_digests':[state['segments'][s['segment_id']]['complete']['complete_digest'] for s in plan['segments']]}
