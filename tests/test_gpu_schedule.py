"""Concurrent worker tests use barriers, never a sequential scheduling substitute."""
import copy
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from fsf.remote.gpu_schedule import run_schedule, validate_schedule
from fsf.remote.contracts import RemoteError


def devices(n=4):
    return [{'index': i, 'uuid': f'GPU-{i:032x}', 'name': 'test', 'memory_mb': 24576} for i in range(n)]


def plan(n=3):
    return {'schema_version': 'fsf_gpu_schedule_v1', 'authorized_devices': n, 'deadline_seconds': 20,
            'tasks': [{'task_id': str(i), 'deps': [], 'device_count': 1, 'max_attempts': 1,
                       'args': ['test'], 'outputs': ['result.json']} for i in range(5)]}


def test_n_cards_concurrent_distinct_and_refill_then_reuse(tmp_path):
    barrier = threading.Barrier(3, timeout=5)
    lock = threading.Lock()
    active = set()
    seen = []
    def runner(args, **kw):
        device = kw['env']['CUDA_VISIBLE_DEVICES']
        key = kw['cwd'].parent.name
        with lock:
            assert device not in active
            active.add(device)
            seen.append((key, device, len(active)))
        if int(key) < 3:
            barrier.wait()
        (kw['cwd'] / 'result.json').write_text(json.dumps({'device': device}))
        with lock:
            active.remove(device)
        return SimpleNamespace(returncode=0, timed_out=False)
    result = run_schedule(plan(), output_root=tmp_path, visible=devices(), runner=runner)
    assert result['status'] == 'completed'
    assert max(v[2] for v in seen) == 3
    assert len(seen) == 5 and len({v[0] for v in seen}) == 5
    assert {v[1] for v in seen} <= {v['uuid'] for v in devices(3)}
    assert result['gpu_execution_verified'] is False
    assert run_schedule(plan(), output_root=tmp_path, visible=devices(), runner=lambda *a, **kw: pytest.fail('must reuse'))['status'] == 'completed'


def test_real_subprocess_receives_only_assigned_uuid(tmp_path):
    value = plan(2)
    script = "import os,json,pathlib;pathlib.Path('result.json').write_text(json.dumps({'gpu':os.environ['CUDA_VISIBLE_DEVICES']}))"
    for task in value['tasks']:
        task['args'] = [sys.executable, '-c', script]
    result = run_schedule(value, output_root=tmp_path, visible=devices())
    assert result['status'] == 'completed'
    for entry in result['tasks'].values():
        payload = json.loads((tmp_path / entry['outputs'][0]['path']).read_text())
        assert payload['gpu'] == entry['devices'][0]['uuid']


def test_bounded_redo_then_dependency_and_corrupt_output(tmp_path):
    value = plan(1)
    value['tasks'] = value['tasks'][:2]
    value['tasks'][0]['max_attempts'] = 2
    value['tasks'][1]['deps'] = ['0']
    calls = []
    def runner(args, **kw):
        key = kw['cwd'].parent.name
        calls.append(key)
        if len(calls) == 1:
            return SimpleNamespace(returncode=2, timed_out=False)
        (kw['cwd'] / 'result.json').write_text('ok')
        return SimpleNamespace(returncode=0, timed_out=False)
    result = run_schedule(value, output_root=tmp_path, visible=devices(), runner=runner)
    assert result['status'] == 'completed' and calls == ['0', '0', '1']
    (tmp_path / result['tasks']['0']['outputs'][0]['path']).write_text('changed')
    with pytest.raises(RemoteError, match='REMOTE_OUTPUT_CHANGED'):
        run_schedule(value, output_root=tmp_path, visible=devices(), runner=runner)


@pytest.mark.parametrize('mutation', ['cycle', 'unknown', 'excess', 'negative', 'duplicate', 'path'])
def test_invalid_schedule_never_executes(mutation, tmp_path):
    value = copy.deepcopy(plan())
    if mutation == 'cycle':
        value['tasks'][0]['deps'] = ['1']; value['tasks'][1]['deps'] = ['0']
    if mutation == 'unknown': value['tasks'][0]['deps'] = ['missing']
    if mutation == 'excess': value['tasks'][0]['device_count'] = 4
    if mutation == 'negative': value['authorized_devices'] = -1
    if mutation == 'duplicate': value['tasks'][1]['task_id'] = '0'
    if mutation == 'path': value['tasks'][0]['outputs'] = ['../escape']
    with pytest.raises(RemoteError):
        run_schedule(value, output_root=tmp_path, visible=devices(), runner=lambda *a, **kw: pytest.fail('must not launch'))


def test_pause_without_launch_then_resume(tmp_path):
    value = plan(1)
    first = run_schedule(value, output_root=tmp_path, visible=devices(), cancelled=lambda: True)
    assert first['status'] == 'paused' and first['tasks'] == {}
    def runner(args, **kw):
        (kw['cwd'] / 'result.json').write_text('ok')
        return SimpleNamespace(returncode=0, timed_out=False)
    assert run_schedule(value, output_root=tmp_path, visible=devices(), runner=runner)['status'] == 'completed'


def shared_plan(n=1,slots=2):
    value=plan(n)
    value['sharing']={'max_tasks_per_device':slots,'memory_budget_mb':12000,'headroom_mb':2048}
    for task in value['tasks']:task['vram_mb_per_device']=4096
    return value


def memory_sample(n=4,used=4000):
    return {d['uuid']:{'used_mb':used,'total_mb':d['memory_mb'],'compute_processes':2} for d in devices(n)}


@pytest.mark.parametrize('n,slots',[(1,2),(2,2),(3,2)])
def test_multiple_cards_and_within_card_sharing_combine(n,slots,tmp_path):
    value=shared_plan(n,slots)
    value['tasks']=value['tasks'][:min(5,n*slots)]
    barrier=threading.Barrier(len(value['tasks']),timeout=5)
    lock=threading.Lock();active={};observed=[]
    def runner(args,**kw):
        uuid=kw['env']['CUDA_VISIBLE_DEVICES']
        with lock:
            active[uuid]=active.get(uuid,0)+1
            assert active[uuid]<=slots
            observed.append((uuid,active[uuid]))
        barrier.wait()
        (kw['cwd']/'result.json').write_text('ok')
        with lock:active[uuid]-=1
        return SimpleNamespace(returncode=0,timed_out=False)
    result=run_schedule(value,output_root=tmp_path,visible=devices(),runner=runner,memory_probe=memory_sample)
    assert result['status']=='completed'
    assert len(set(d for d,_ in observed))==n
    assert max(c for _,c in observed)==slots


def test_vram_reservations_queue_even_when_slot_is_free(tmp_path):
    value=shared_plan(1,3)
    value['tasks']=value['tasks'][:2]
    for task in value['tasks']:task['vram_mb_per_device']=7000
    lock=threading.Lock();active=0
    def runner(args,**kw):
        nonlocal active
        with lock:
            active+=1;assert active==1
        (kw['cwd']/'result.json').write_text('ok')
        with lock:active-=1
        return SimpleNamespace(returncode=0,timed_out=False)
    assert run_schedule(value,output_root=tmp_path,visible=devices(),runner=runner,memory_probe=memory_sample)['status']=='completed'


def test_observed_memory_limit_stops_before_launch(tmp_path):
    value=shared_plan()
    result=run_schedule(value,output_root=tmp_path,visible=devices(),
        runner=lambda *a,**kw:pytest.fail('over budget'),memory_probe=lambda:memory_sample(used=13000))
    assert result['status']=='failed' and result['error_code']=='REMOTE_GPU_MEMORY_LIMIT'
    assert result['tasks']=={}


def test_one_oom_does_not_relabel_other_jobs_or_retry_unbounded(tmp_path):
    value=shared_plan();value['tasks']=value['tasks'][:2]
    def runner(args,**kw):
        if kw['cwd'].parent.name=='0':
            return SimpleNamespace(returncode=2,timed_out=False,stderr='BLENDER_OUT_OF_MEMORY')
        (kw['cwd']/'result.json').write_text('ok')
        return SimpleNamespace(returncode=0,timed_out=False)
    result=run_schedule(value,output_root=tmp_path,visible=devices(),runner=runner,memory_probe=memory_sample)
    assert result['status']=='failed'
    assert result['tasks']['0']['error_code']=='BLENDER_OUT_OF_MEMORY'
    assert result['tasks']['0']['attempt']==1 and result['tasks']['1']['status']=='accepted'


def test_pause_drains_running_jobs_and_resume_skips_completed(tmp_path):
    value=shared_plan();pause=threading.Event();barrier=threading.Barrier(2,timeout=5);seen=[]
    def runner(args,**kw):
        seen.append(kw['cwd'].parent.name)
        barrier.wait();pause.set()
        (kw['cwd']/'result.json').write_text('ok')
        return SimpleNamespace(returncode=0,timed_out=False)
    first=run_schedule(value,output_root=tmp_path,visible=devices(),runner=runner,memory_probe=memory_sample,paused=pause.is_set)
    assert first['status']=='paused' and len(first['tasks'])==2
    assert all(t['status']=='accepted' for t in first['tasks'].values())
    def rest(args,**kw):
        assert kw['cwd'].parent.name not in seen
        (kw['cwd']/'result.json').write_text('ok')
        return SimpleNamespace(returncode=0,timed_out=False)
    assert run_schedule(value,output_root=tmp_path,visible=devices(),runner=rest,memory_probe=memory_sample)['status']=='completed'
