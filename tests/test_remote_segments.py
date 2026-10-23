"""Durable segment boundaries, retry, pause and ambiguous-response handling."""
import copy
import multiprocessing
from fractions import Fraction

import pytest
from fsf.remote.contracts import RemoteError,digest,seal
from fsf.remote.segments import create_plan,run,pause,resume,validate_plan


def plan(clock=None):
    return create_plan(group_id='group',clock=clock or {'num':24,'den':1},start_frame=0,end_frame=96,segment_frames=24,
        generation={k:chr(97+i)*64 for i,k in enumerate(['template_digest','source_manifest_digest','recipe_digest','worker_digest','animation_digest','render_digest'])},encoder_digest='f'*64)


class Driver:
    def __init__(self):self.calls=[];self.fail_segment=None;self.unknown=False;self.pause_after=None;self.root=None;self.plan=None
    def prepare(self,plan,segment,attempt,remaining):
        return {'request_digest':digest({'segment':segment,'attempt':attempt}),'segment':segment,'attempt':attempt}
    def submit(self,request):
        self.calls.append((request['segment']['segment_id'],request['attempt']))
        return {'status':'submission_unknown' if self.unknown else 'submitted'}
    def observe(self,request):
        if self.unknown:return {'status':'submission_unknown','provider_status':'unknown'}
        if request['segment']['segment_id']==self.fail_segment:return {'status':'failed','provider_status':'failed'}
        return {'status':'remote_completed','provider_status':'completed'}
    def accept(self,request,plan,segment,attempt):
        if self.pause_after==segment['segment_id']:
            pause(plan,state_root=self.root);self.pause_after=None
        return seal({'schema_version':'fsf_segment_complete_v1','plan_digest':plan['plan_digest'],'generation_digest':plan['generation_digest'],
            'segment':segment,'request_digest':request['request_digest'],'attempt_id':attempt,'result_digest':'a'*64,
            'manifest_digest':digest(segment),'frame_mapping_digest':digest(segment),'frame_count':segment['end_frame']-segment['start_frame'],
            'motion_review':'not_reviewed'},'complete_digest')


@pytest.mark.parametrize('clock,total',[({'num':24,'den':1},Fraction(4)),({'num':24000,'den':1001},Fraction(1001,250))])
def test_two_clocks_have_exact_nonoverlapping_global_coverage(clock,total):
    value=plan(clock)
    frames=[f for s in value['segments'] for f in range(s['start_frame'],s['end_frame'])]
    assert frames==list(range(96))
    assert Fraction(len(frames)*clock['den'],clock['num'])==total


def test_failure_third_segment_only_retries_third(tmp_path):
    value=plan();driver=Driver();driver.fail_segment='segment_0003'
    assert run(value,state_root=tmp_path,driver=driver)['status']=='failed_segment'
    driver.fail_segment=None
    final=run(value,state_root=tmp_path,driver=driver)
    assert final['status']=='completed' and final['attempt_count']==5
    assert driver.calls==[('segment_0001',1),('segment_0002',1),('segment_0003',1),('segment_0003',2),('segment_0004',1)]
    assert run(value,state_root=tmp_path,driver=driver)['attempt_count']==5
    assert len(driver.calls)==5


def test_pause_checkpoint_then_new_parent_resumes(tmp_path):
    value=plan();driver=Driver();driver.pause_after='segment_0001';driver.root=tmp_path
    assert run(value,state_root=tmp_path,driver=driver)['status']=='paused'
    assert driver.calls==[('segment_0001',1)]
    resume(value,state_root=tmp_path)
    restarted=Driver()
    assert run(value,state_root=tmp_path,driver=restarted)['status']=='completed'
    assert restarted.calls==[('segment_0002',1),('segment_0003',1),('segment_0004',1)]


def test_unknown_submit_is_reconciled_never_resent(tmp_path):
    value=plan();driver=Driver();driver.unknown=True
    assert run(value,state_root=tmp_path,driver=driver)['status']=='reconcile_required'
    assert run(value,state_root=tmp_path,driver=driver)['status']=='reconcile_required'
    assert driver.calls==[('segment_0001',1)]
    driver.unknown=False
    assert run(value,state_root=tmp_path,driver=driver)['status']=='completed'
    assert len(driver.calls)==4


def test_attempt_budget_survives_restarts(tmp_path):
    value=plan();value['max_attempts_per_segment']=1;value=seal(value,'plan_digest')
    driver=Driver();driver.fail_segment='segment_0001'
    run(value,state_root=tmp_path,driver=driver)
    with pytest.raises(RemoteError,match='REMOTE_GROUP_BUDGET_EXCEEDED'):run(value,state_root=tmp_path,driver=Driver())


def test_idle_pause_does_not_consume_execution_budget(tmp_path):
    from fsf.core.edit_store import read_json
    from fsf.remote.contracts import write_json
    value=plan();driver=Driver();driver.pause_after='segment_0001';driver.root=tmp_path
    assert run(value,state_root=tmp_path,driver=driver)['status']=='paused'
    path=tmp_path/value['plan_digest']/'state.json';state=read_json(path)
    state['created_at']-=3*86400;write_json(path,seal(state,'state_digest'))
    resume(value,state_root=tmp_path)
    assert run(value,state_root=tmp_path,driver=Driver())['status']=='completed'


@pytest.mark.parametrize('mutation',['gap','overlap','duplicate','clock','generation'])
def test_changed_mapping_and_generation_are_not_reused(mutation):
    value=plan()
    if mutation=='gap':value['segments'][1]['start_frame']+=1
    if mutation=='overlap':value['segments'][1]['start_frame']-=1
    if mutation=='duplicate':value['segments'][1]['segment_id']=value['segments'][0]['segment_id']
    if mutation=='clock':value['clock']={'num':23,'den':1}
    if mutation=='generation':value['generation']['recipe_digest']='0'*64
    value=seal(value,'plan_digest')
    with pytest.raises(RemoteError):validate_plan(value)


def _segment_race(root,value,barrier,queue):
    driver=Driver()
    try:
        barrier.wait(timeout=30)
        result=run(value,state_root=root,driver=driver)
        queue.put({'status':result['status'],'new_submissions':len(driver.calls)})
    except Exception:
        queue.put({'status':'failed','new_submissions':len(driver.calls)})


def test_independent_processes_cannot_double_dispatch_segments(tmp_path):
    context=multiprocessing.get_context('spawn');barrier=context.Barrier(3);queue=context.Queue()
    workers=[context.Process(target=_segment_race,args=(str(tmp_path),plan(),barrier,queue)) for _ in range(2)]
    for worker in workers:worker.start()
    barrier.wait(timeout=30)
    results=[queue.get(timeout=60) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        if worker.is_alive():worker.terminate();worker.join(timeout=5)
        assert worker.exitcode==0
    assert all(r['status']=='completed' for r in results)
    assert sorted(r['new_submissions'] for r in results)==[0,4]


def test_cli_explicit_segment_limits_and_duplicate_rejection(tmp_path,monkeypatch):
    import json
    from fsf.cli.remote import main
    from fsf.remote import segment_driver
    captured=[]
    def compile_fake(template,**kwargs):
        captured.append(kwargs)
        return {'plan_digest':'a'*64,'segments':[]}
    monkeypatch.setattr(segment_driver,'compile_segments',compile_fake)
    source=tmp_path/'template.json';source.write_text('{}')
    args=['segments-plan',str(source),'--group-id','synthetic','--clock','24/1','--out',str(tmp_path/'plan.json')]
    assert main(args+['--start-frame','0','--end-frame','96','--segment-frames','24','--budget-seconds','3000'])==0
    assert captured[-1]['budget_seconds']==3000 and captured[-1]['end_frame']==96
    assert main(args+['--budget-seconds','3000','--budget-seconds','3000'])!=0
    assert len(captured)==1
