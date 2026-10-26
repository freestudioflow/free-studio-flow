"""Synthetic inventory/stop observations test admission, never GPU execution."""
import copy
import multiprocessing as mp
import time
import pytest
from fsf.production.resource_pool import ResourcePool
from fsf.production.contracts import ProductionError


def authority(vram=8192,slots=4,bypasses=2):
    return {'schema_version':'fsf_resource_pool_authority_v1','pool_id':'shared_worker','authorized':True,
        'devices':[{'uuid':'synthetic_GPU_A','vram_mb':vram,'slots':slots}],
        'max_lease_seconds':600,'max_bypasses':bypasses,'max_queued':64,'max_resident_models':16}


def ticket(request_id,resident='model_service',resident_mb=3072,workspace_mb=1024,mode='shared',lease=60):
    return {'request_id':request_id,'job_id':'job_'+request_id,'resident_id':resident,
        'model_digest':'a'*64,'runtime_digest':'b'*64,'resident_mb':resident_mb,'workspace_mb':workspace_mb,
        'mode':mode,'eligible_uuids':['synthetic_GPU_A'],'lease_seconds':lease}


def inventory(vram=8192,free=None,managed=0):
    return lambda:[{'uuid':'synthetic_GPU_A','total_mb':vram,'free_mb':vram if free is None else free,'managed_used_mb':managed}]


def stopped(scope):
    def observe(item):
        return {'scope':scope,'target_id':item['request_id' if scope=='task' else 'resident_key'],
            'epoch':item['epoch'],'stopped':True,'observation_digest':'c'*64}
    return observe


def test_shared_residency_counts_once_but_independent_processes_do_not(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority(vram=6144,slots=4))
    pool.enqueue(ticket('one'));first=pool.admit('one',inventory=inventory(vram=6144))
    pool.enqueue(ticket('two'));second=pool.admit('two',inventory=inventory(vram=6144))
    assert first['state']==second['state']=='running'
    state=pool.inspect()
    assert len(state['residents'])==1
    assert sum(v['workspace_mb'] for v in state['claims'].values())==2048
    pool.enqueue(ticket('independent',resident='separate_process'))
    assert pool.admit('independent',inventory=inventory(vram=6144))['state']=='queued'


def test_unfit_old_ticket_does_not_block_disjoint_free_gpu(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');auth=authority(bypasses=0)
    auth['devices'].append({'uuid':'synthetic_GPU_B','vram_mb':8192,'slots':4});pool.create(auth)
    pool.enqueue(ticket('too_large',resident_mb=4096,workspace_mb=0))
    b=ticket('free_b');b['eligible_uuids']=['synthetic_GPU_B'];pool.enqueue(b)
    inv=lambda:[*inventory(free=1024)(),{'uuid':'synthetic_GPU_B','total_mb':8192,'free_mb':8192,'managed_used_mb':0}]
    assert pool.admit('free_b',inventory=inv)['state']=='running'


def test_conflicting_queued_resident_cannot_poison_unrelated_admission(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('running'));pool.admit('running',inventory=inventory())
    bad=ticket('changed');bad['model_digest']='e'*64;pool.enqueue(bad)
    pool.enqueue(ticket('unrelated',resident='another_model',resident_mb=512,workspace_mb=0))
    assert pool.admit('unrelated',inventory=inventory())['state']=='running'


def test_release_frees_workspace_not_resident_and_stop_evidence_is_required(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('one'));claim=pool.admit('one',inventory=inventory())
    with pytest.raises(ProductionError,match='STOP_PROBE_REQUIRED'):pool.release(claim,confirm_stopped=None)
    with pytest.raises(ProductionError,match='STOP_UNCONFIRMED'):
        pool.release(claim,confirm_stopped=lambda item:{**stopped('task')(item),'stopped':False})
    assert pool.inspect()['claims']
    released=pool.release(claim,confirm_stopped=stopped('task'))
    state=pool.inspect();assert not state['claims'] and len(state['residents'])==1
    assert pool.release(claim,confirm_stopped=None)==released
    assert pool.enqueue(ticket('one'))==released
    assert pool.admit('one',inventory=None)==released
    with pytest.raises(ProductionError,match='BINDING'):pool.enqueue(ticket('one',workspace_mb=2048))
    pool.evict(claim['resident_key'],epoch=claim['resident_epoch'],confirm_stopped=stopped('model'))
    assert not pool.inspect()['residents']


def test_expired_lease_is_unknown_not_permission_to_steal_and_tokens_are_fenced(tmp_path):
    now=[100];pool=ResourcePool(tmp_path/'pool.json',clock=lambda:now[0]);pool.create(authority())
    pool.enqueue(ticket('one',lease=5));claim=pool.admit('one',inventory=inventory())
    now[0]=106
    state=pool.inspect();assert state['claims']['one']['state']=='unknown'
    pool.enqueue(ticket('two'))
    assert pool.admit('two',inventory=inventory())['state']=='queued'
    with pytest.raises(ProductionError,match='RECONCILE_REQUIRED'):pool.renew(claim,seconds=5)
    pool.release(claim,confirm_stopped=stopped('task'))
    assert pool.admit('two',inventory=inventory())['state']=='queued'  # unknown resident still occupies card
    pool.evict(claim['resident_key'],epoch=claim['resident_epoch'],confirm_stopped=stopped('model'))
    newer=pool.admit('two',inventory=inventory());assert newer['epoch']>claim['epoch']
    with pytest.raises(ProductionError,match='FENCED'):pool.renew(claim,seconds=5)
    with pytest.raises(ProductionError,match='FENCED'):
        pool.evict(claim['resident_key'],epoch=claim['resident_epoch'],confirm_stopped=stopped('model'))
    pool.release(claim,confirm_stopped=None)
    assert 'two' in pool.inspect()['claims']


def test_fifo_bypass_is_bounded_and_queue_cancel_cannot_kill_an_active_task(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority(bypasses=2))
    pool.enqueue(ticket('holding',resident_mb=4096,workspace_mb=0));holding=pool.admit('holding',inventory=inventory())
    pool.enqueue(ticket('large',resident='other',resident_mb=4096,workspace_mb=1024))
    for request in ('small1','small2'):
        pool.enqueue(ticket(request,resident_mb=4096,workspace_mb=512))
        claim=pool.admit(request,inventory=inventory());assert claim['state']=='running'
        pool.release(claim,confirm_stopped=stopped('task'))
    assert pool.inspect()['tickets']['large']['bypasses']==2
    pool.enqueue(ticket('small3',resident_mb=4096,workspace_mb=512))
    assert pool.admit('small3',inventory=inventory())['reason']=='fairness'
    with pytest.raises(ProductionError,match='BUSY'):pool.cancel_queued('holding')
    assert pool.cancel_queued('large')['state']=='cancelled'
    assert pool.admit('small3',inventory=inventory())['state']=='running'


def test_exclusive_mode_and_live_free_memory_are_enforced(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('one',mode='exclusive'))
    assert pool.admit('one',inventory=inventory(free=1024))['state']=='queued'
    claim=pool.admit('one',inventory=inventory());assert claim['state']=='running'
    pool.enqueue(ticket('two'))
    assert pool.admit('two',inventory=inventory())['state']=='queued'
    pool.release(claim,confirm_stopped=stopped('task'))
    assert pool.admit('two',inventory=inventory())['state']=='running'


def test_unmanaged_and_pending_memory_are_not_double_spent(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('one',resident_mb=4096,workspace_mb=1024));pool.admit('one',inventory=inventory())
    pool.enqueue(ticket('two',resident_mb=4096,workspace_mb=1024))
    # 5120 MB reserved but not yet visible in hardware; only 4096 MB currently
    # free due to another process. Conservatively refuse a second workspace.
    assert pool.admit('two',inventory=inventory(free=4096))['state']=='queued'
    # Host attributes the first reservation's 5120 MB to THIS pool; a further
    # 1024 MB workspace now fits without counting those bytes twice.
    assert pool.admit('two',inventory=inventory(free=3072,managed=5120))['state']=='running'


def test_changed_model_or_memory_binding_needs_a_new_resident_identity(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('one'));pool.admit('one',inventory=inventory())
    changed=ticket('two');changed['model_digest']='d'*64;pool.enqueue(changed)
    with pytest.raises(ProductionError,match='MODEL_CHANGED'):pool.admit('two',inventory=inventory())
    pool.cancel_queued('two')
    pool.enqueue(ticket('three',resident_mb=4096))
    with pytest.raises(ProductionError,match='MODEL_CHANGED'):pool.admit('three',inventory=inventory())


def test_probe_and_resource_authorization_are_mandatory(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    with pytest.raises(ProductionError,match='MEMORY_LIMIT'):
        pool.enqueue(ticket('huge',resident_mb=9000))
    pool.enqueue(ticket('one'))
    with pytest.raises(ProductionError,match='PROBE_REQUIRED'):pool.admit('one',inventory=None)
    with pytest.raises(ProductionError,match='PROBE_INVALID'):
        pool.admit('one',inventory=lambda:[{'uuid':'synthetic_GPU_A','total_mb':8192,'free_mb':True,'managed_used_mb':0}])
    assert pool.admit('one',inventory=lambda:[])['state']=='queued'
    changed=authority();changed['devices'][0]['vram_mb']=9000
    with pytest.raises(ProductionError,match='AUTHORITY_CHANGED'):pool.create(changed)


def test_stop_transaction_recovers_after_history_publish_before_state_commit(tmp_path,monkeypatch):
    now=[100];pool=ResourcePool(tmp_path/'pool.json',clock=lambda:now[0]);pool.create(authority())
    pool.enqueue(ticket('one',lease=5));claim=pool.admit('one',inventory=inventory())
    original=pool._save
    def interrupt(*_):raise RuntimeError('synthetic crash after stop history')
    monkeypatch.setattr(pool,'_save',interrupt)
    with pytest.raises(RuntimeError):pool.release(claim,confirm_stopped=stopped('task'))
    monkeypatch.setattr(pool,'_save',original);now[0]=106
    state=pool.inspect()
    assert not state['claims'] and not state['tickets']
    assert state['residents'][claim['resident_key']]['state']=='unknown'
    assert pool.release(claim,confirm_stopped=None)['state']=='released'


def test_independent_resident_heartbeat_does_not_release_or_resurrect_a_model(tmp_path):
    now=[100];pool=ResourcePool(tmp_path/'pool.json',clock=lambda:now[0]);pool.create(authority())
    pool.enqueue(ticket('one',lease=5));claim=pool.admit('one',inventory=inventory())
    pool.release(claim,confirm_stopped=stopped('task'));now[0]=104
    renewed=pool.renew_resident(claim['resident_key'],epoch=claim['resident_epoch'],seconds=10)
    assert renewed['lease_until']==114 and not pool.inspect()['claims']
    now[0]=115
    with pytest.raises(ProductionError,match='RECONCILE_REQUIRED'):
        pool.renew_resident(claim['resident_key'],epoch=claim['resident_epoch'],seconds=10)


def test_empty_and_boolean_tokens_cannot_pass_fencing_and_probe_errors_are_sanitized(tmp_path):
    pool=ResourcePool(tmp_path/'pool.json');pool.create(authority())
    pool.enqueue(ticket('one'));claim=pool.admit('one',inventory=inventory())
    for token in (None,{**claim,'epoch':True},{**claim,'epoch':claim['epoch']+1}):
        with pytest.raises(ProductionError,match='FENCED'):pool.renew(token,seconds=10)
        with pytest.raises(ProductionError,match='FENCED'):pool.release(token,confirm_stopped=stopped('task'))
    def private_failure(*_):raise ValueError('synthetic-secret-not-for-output')
    pool.enqueue(ticket('two'))
    with pytest.raises(ProductionError,match='PROBE_FAILED') as exc:pool.admit('two',inventory=private_failure)
    assert 'secret' not in str(exc.value)
    with pytest.raises(ProductionError,match='STOP_UNCONFIRMED') as exc:pool.release(claim,confirm_stopped=private_failure)
    assert 'secret' not in str(exc.value)


def test_ready_jobs_fill_two_distinct_devices_then_reuse_a_freed_slot(tmp_path):
    auth=authority(slots=1);auth['devices'].append({'uuid':'synthetic_GPU_B','vram_mb':8192,'slots':1})
    pool=ResourcePool(tmp_path/'pool.json');pool.create(auth)
    def both():return inventory()()+[{'uuid':'synthetic_GPU_B','total_mb':8192,'free_mb':8192,'managed_used_mb':0}]
    for rid in ('one','two','three'):
        value=ticket(rid);value['eligible_uuids']=['synthetic_GPU_A','synthetic_GPU_B'];pool.enqueue(value)
    first=pool.admit('one',inventory=both);second=pool.admit('two',inventory=both)
    assert {first['uuid'],second['uuid']}=={'synthetic_GPU_A','synthetic_GPU_B'}
    assert pool.admit('three',inventory=both)['state']=='queued'
    pool.release(first,confirm_stopped=stopped('task'))
    third=pool.admit('three',inventory=both)
    assert third['state']=='running' and third['uuid']==first['uuid'] and third['epoch']>first['epoch']


def _compete(path,request_id,barrier,results):
    pool=ResourcePool(path)
    barrier.wait(timeout=20)
    results.put(pool.admit(request_id,inventory=inventory())['state'])


def test_independent_processes_cannot_admit_two_jobs_into_one_memory_envelope(tmp_path):
    path=tmp_path/'pool.json';pool=ResourcePool(path);pool.create(authority())
    for request in ('first','second'):
        pool.enqueue(ticket(request,resident=request,resident_mb=4096,workspace_mb=1024))
    ctx=mp.get_context('spawn');barrier=ctx.Barrier(3);results=ctx.Queue()
    processes=[ctx.Process(target=_compete,args=(path,r,barrier,results)) for r in ('first','second')]
    for p in processes:p.start()
    try:
        barrier.wait(timeout=20)
        for p in processes:p.join(timeout=30);assert p.exitcode==0
        assert sorted(results.get(timeout=5) for _ in processes)==['queued','running']
        assert len(pool.inspect()['claims'])==1
    finally:
        for p in processes:
            if p.is_alive():p.terminate();p.join(timeout=5)
