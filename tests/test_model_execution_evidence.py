"""Neural GPU evidence never becomes a Cycles render or content approval."""
import copy
import pytest
from fsf.remote.contracts import RemoteError,seal,digest,write_json,sha
from fsf.remote.v2 import validate_recipe
from fsf.remote.model_evidence import verify_model_receipt
from test_remote_dispatch import template


def model_recipe():
    recipe=template('cpu.recipe.json')
    recipe['schema_version']='fsf_remote_recipe_v3'
    lock={'model_id':'Example/Model','revision':'a'*40,'files':{'weights.safetensors':'b'*64},'precision':'float16'}
    recipe['execution_requirements']={'render':{'engine':'MODEL','device':'gpu','backend':'cuda','device_count':1,'min_vram_mb':8192},
        'model':{'lock':lock,'lock_digest':digest(lock),'receipt_path':'model-receipt.json'}}
    recipe['steps'][0]['outputs'].append({'artifact_id':'model_receipt','path':'model-receipt.json','role':'data','media_type':'application/json'})
    return seal(recipe,'recipe_digest')


def case(tmp_path):
    recipe=model_recipe();validate_recipe(recipe)
    request={'recipe':recipe,'request_digest':'c'*64}
    (tmp_path/'result.json').write_text('model output')
    visible=[{'uuid':'GPU-1234567890abcdef'}]
    value=seal({'schema_version':'fsf_model_execution_receipt_v1','request_digest':request['request_digest'],
        'lock_digest':recipe['execution_requirements']['model']['lock_digest'],'observed_files':{'weights.safetensors':'b'*64},
        'devices':[visible[0]['uuid']],'precision':'float16','model_calls':1,'cuda_event_ms':20.0,
        'started_ns':100,'finished_ns':200,'peak_allocated_bytes':1024,'outputs':{'result.json':sha(tmp_path/'result.json')}},'receipt_digest')
    return request,visible,value


def test_new_contract_does_not_loosen_old_v2():
    value=model_recipe()
    assert validate_recipe(value)==value
    value['schema_version']='fsf_remote_recipe_v2'
    with pytest.raises(RemoteError):validate_recipe(seal(value,'recipe_digest'))


@pytest.mark.parametrize('mutate',[
    lambda r:r.update(model_calls=0),lambda r:r.update(cuda_event_ms=0),
    lambda r:r.update(cuda_event_ms=True),lambda r:r.update(devices=[]),
    lambda r:r.update(precision='float32'),lambda r:r.update(observed_files={'weights.safetensors':'d'*64}),
    lambda r:r.update(request_digest='e'*64),lambda r:r.update(finished_ns=100),
    lambda r:r.update(outputs={}),lambda r:r.update(peak_allocated_bytes=0),
])
def test_incomplete_cpu_or_wrong_model_evidence_refused(tmp_path,mutate):
    request,visible,value=case(tmp_path);mutate(value)
    write_json(tmp_path/'model-receipt.json',seal(value,'receipt_digest'))
    with pytest.raises(RemoteError):verify_model_receipt(tmp_path,request,visible)


def test_parent_rechecks_actual_output_sha(tmp_path):
    request,visible,value=case(tmp_path)
    write_json(tmp_path/'model-receipt.json',value)
    assert verify_model_receipt(tmp_path,request,visible)==value
    (tmp_path/'result.json').write_text('modified')
    with pytest.raises(RemoteError):verify_model_receipt(tmp_path,request,visible)


def test_non_torch_runtime_observation_is_experimental_and_not_measured(tmp_path):
    request,visible,value=case(tmp_path)
    request['execution_mode']='experimental'
    value.update(schema_version='fsf_model_execution_receipt_v2',runtime='operator-sd-cli',
                 runtime_metrics={'process_gpu_samples':2},verification_level='observed')
    value.pop('cuda_event_ms')
    value=seal(value,'receipt_digest');write_json(tmp_path/'model-receipt.json',value)
    assert verify_model_receipt(tmp_path,request,visible)['verification_level']=='observed'
    request.pop('execution_mode')
    with pytest.raises(RemoteError):verify_model_receipt(tmp_path,request,visible)


def test_model_lock_is_not_a_huggingface_or_float_only_whitelist():
    value=model_recipe();spec=value['execution_requirements']['model']
    spec['lock'].update(model_id='operator-tool',revision='release-2026.10',precision='Q3_K_M')
    spec['lock_digest']=digest(spec['lock'])
    assert validate_recipe(seal(value,'recipe_digest'))['execution_requirements']['model']['lock']['precision']=='Q3_K_M'


def test_long_model_sequence_needs_versioned_profile_and_exact_frame_limit():
    from fsf.remote.v2 import validate_profile
    profile=template('local.profile.json')
    profile.update(schema_version='fsf_remote_profile_v3',
        gpu_limits={'allowed':False,'backends':[],'max_devices':0,'max_vram_mb':0},
        store_binding={'store_id':'fixture','config_digest':'a'*64,'input_prefix':'jobs/inputs/','output_prefix':'jobs/outputs/'},
        group_limits={'max_budget_seconds':5400,'max_total_attempts':12,'max_attempts_per_segment':3,'max_frames':101},
        allocation={'allocation_id':'fixture','state_root':'allocation','max_submissions':2,'max_execution_seconds':4800})
    assert validate_profile(profile)==profile
    profile['schema_version']='fsf_remote_profile_v2'
    with pytest.raises(RemoteError):validate_profile(profile)
    profile['schema_version']='fsf_remote_profile_v3';profile['group_limits']['max_frames']=172801
    with pytest.raises(RemoteError):validate_profile(profile)


def test_offloaded_backend_initializes_allocator_before_measurement_reset():
    from types import SimpleNamespace
    from fsf.remote.model_evidence import begin_cuda_measurement
    allocated=[]
    def allocate(*a,**kw):allocated.append(kw['device']);return object()
    def reset(device):
        if not allocated:raise RuntimeError('allocator has no statistics yet')
        assert allocated==['cuda:0'] and device==0
    fake=SimpleNamespace(float32='float32',empty=allocate,cuda=SimpleNamespace(is_available=lambda:True,
        synchronize=lambda device:None,reset_peak_memory_stats=reset))
    begin_cuda_measurement(fake)
    fake.cuda.is_available=lambda:False
    with pytest.raises(RemoteError):begin_cuda_measurement(fake)
