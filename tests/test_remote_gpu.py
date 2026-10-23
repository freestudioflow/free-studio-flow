"""Device selection never falls back; CPU v2 is validated with real Blender."""
import copy
from types import SimpleNamespace

import pytest
from fsf.remote.contracts import RemoteError
from fsf.remote.gpu import assign_segments,authorize_gpu,cuda_inventory,select_devices
from fsf.tools.blender_worker import configure_device


def render(device='gpu'):
    return {'engine':'CYCLES','device':device,'backend':'cuda' if device=='gpu' else 'none',
            'device_count':1 if device=='gpu' else 0,'min_vram_mb':16000 if device=='gpu' else 0,
            'samples':4,'seed':0,'threads':2}


@pytest.mark.parametrize('text,expected',[
    ('ffmpeg version 8.0.1 Copyright',True),
    ('ffmpeg version n8.0.1-48-g0592be14ff Copyright',True),
    ('ffmpeg version 8.0.10 Copyright',False),
    ('ffmpeg version n8.1.0 Copyright',False),
    ('ffmpeg version master Copyright',False),
])
def test_encoder_release_tag_keeps_exact_patch_version(text,expected):
    from fsf.runtime.blender import encoder_version_matches
    assert encoder_version_matches(text,'8.0.1') is expected


@pytest.mark.parametrize('marker',['core 165','core 165 r3223 0480cb0'])
def test_encoder_build_marker_reports_only_observed_information(marker):
    from fsf.runtime.blender import encoder_build_marker
    text='[libx264 @ 0x123] 264 - '+marker+' - H.264/MPEG-4 AVC codec - options: test'
    assert encoder_build_marker(text)==marker
    assert encoder_build_marker(text+'\n'+text) is None
    assert encoder_build_marker('core 165 arbitrary text') is None


def test_gpu_authorization_is_requirement_not_allowance(monkeypatch):
    limits={'allowed':True,'backends':['cuda'],'max_devices':1,'max_vram_mb':24576}
    monkeypatch.delenv('FSF_REMOTE_GPU',raising=False)
    assert authorize_gpu({'render':render('cpu')},limits) is False
    with pytest.raises(RemoteError,match='REMOTE_GPU_NOT_AUTHORIZED'):authorize_gpu({'render':render()},limits)
    monkeypatch.setenv('FSF_REMOTE_GPU','1')
    assert authorize_gpu({'render':render()},limits)
    monkeypatch.delenv('FSF_REMOTE_GPU')
    assert authorize_gpu({'render':render()},limits,new_submission=False)


def test_real_inventory_absence_is_not_ready(monkeypatch):
    monkeypatch.setattr('fsf.remote.gpu.shutil.which',lambda name:None)
    with pytest.raises(RemoteError,match='REMOTE_GPU_UNAVAILABLE'):cuda_inventory()


@pytest.mark.parametrize('count',[1,2,3])
def test_actual_worker_evidence_is_accepted_by_manifest_schema(count):
    import json
    from importlib.resources import files
    from jsonschema import Draft202012Validator
    scene=SimpleNamespace(render=SimpleNamespace(engine='BLENDER_EEVEE'),cycles=SimpleNamespace(device='CPU'))
    cards=[SimpleNamespace(type='CUDA',name=f'GPU {i}',id=f'cuda-{i}',use=False) for i in range(count)]
    selected=[device(i) for i in range(count)]
    evidence=configure_device(scene,fake_preferences(cards),{**render(),'device_count':count},selected[0] if count==1 else selected)
    schema=json.loads(files('fsf.schemas').joinpath('fsf_blender_manifest_v2.schema.json').read_text())['properties']['execution_evidence']
    Draft202012Validator(schema).validate(evidence)
    assert evidence['device_count']==count and len(evidence['devices'])==count


def test_driver_runtime_is_kept_without_credentials_and_binding_is_respected(monkeypatch):
    from fsf.remote.gpu import gpu_runtime_environment
    monkeypatch.setenv('LD_LIBRARY_PATH','/driver/lib')
    monkeypatch.setenv('SYNTHETIC_API_KEY','must-not-pass')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES','GPU-01234567-89ab-cdef-0123-456789abcde1')
    monkeypatch.setattr('fsf.remote.gpu.shutil.which',lambda name:'synthetic-nvidia-smi')
    def run(*args,**kw):
        assert kw['env']['LD_LIBRARY_PATH']=='/driver/lib'
        assert 'SYNTHETIC_API_KEY' not in kw['env']
        return SimpleNamespace(returncode=0,timed_out=False,stdout='\n'.join(
            f'{i}, GPU-01234567-89ab-cdef-0123-456789abcde{i}, Synthetic GPU, 24576' for i in range(2)))
    monkeypatch.setattr('fsf.remote.gpu.run_safe_subprocess',run)
    assert [d['index'] for d in cuda_inventory()]==[1]
    assert 'SYNTHETIC_API_KEY' not in gpu_runtime_environment()


def test_memory_sampling_distinguishes_usage_and_resident_processes(monkeypatch):
    from fsf.remote.gpu import memory_inventory
    monkeypatch.setattr('fsf.remote.gpu.shutil.which',lambda name:'synthetic-nvidia-smi')
    def run(args,**kw):
        if '--query-gpu=uuid,memory.used,memory.total' in args:
            text='GPU-example, 2048, 15360\n'
        else:text='GPU-example, 123\nGPU-example, 456\n'
        return SimpleNamespace(returncode=0,timed_out=False,stdout=text)
    monkeypatch.setattr('fsf.remote.gpu.run_safe_subprocess',run)
    assert memory_inventory()=={'GPU-example':{'used_mb':2048,'total_mb':15360,'compute_processes':2}}


def test_inventory_parsing_and_vram_gate(monkeypatch):
    monkeypatch.setattr('fsf.remote.gpu.shutil.which',lambda name:'synthetic-nvidia-smi')
    monkeypatch.setattr('fsf.remote.gpu.run_safe_subprocess',lambda *a,**k:SimpleNamespace(returncode=0,timed_out=False,
        stdout='0, GPU-01234567-89ab-cdef-0123-456789abcdef, Synthetic CUDA GPU, 24576\n'))
    assert cuda_inventory(min_vram_mb=16000)[0]['memory_mb']==24576
    with pytest.raises(RemoteError,match='REMOTE_GPU_DEVICE_MISMATCH'):cuda_inventory(min_vram_mb=48000)


def fake_preferences(devices):
    return SimpleNamespace(devices=devices,get_devices=lambda:None,compute_device_type=None)


def test_cycles_cuda_selects_gpu_and_disables_cpu():
    gpu=SimpleNamespace(type='CUDA',name='Synthetic GPU',id='synthetic-cuda',use=False)
    cpu=SimpleNamespace(type='CPU',name='CPU',id='cpu',use=True)
    scene=SimpleNamespace(render=SimpleNamespace(engine='BLENDER_EEVEE'),cycles=SimpleNamespace(device='CPU'))
    prefs=fake_preferences([gpu,cpu])
    evidence=configure_device(scene,prefs,render(),{'index':0,'uuid':'GPU-0123456789abcdef','name':'Synthetic GPU','memory_mb':24576})
    assert scene.render.engine=='CYCLES' and scene.cycles.device=='GPU'
    assert gpu.use and not cpu.use and evidence['backend']=='cuda'


@pytest.mark.parametrize('types',[[],['CPU'],['OPTIX'],['CUDA','CUDA']])
def test_missing_or_ambiguous_cuda_never_falls_back(types):
    scene=SimpleNamespace(render=SimpleNamespace(engine='BLENDER_EEVEE'),cycles=SimpleNamespace(device='CPU'))
    devices=[SimpleNamespace(type=t,use=False,name=t,id=t) for t in types]
    with pytest.raises(ValueError):configure_device(scene,fake_preferences(devices),render(),{'memory_mb':24576})
    assert scene.cycles.device!='GPU'


from test_blender_workflow import real_scene,request


def device(index):
    return {'index':index,'uuid':f'GPU-01234567-89ab-cdef-0123-456789abcde{index}','name':f'GPU {index}','memory_mb':24576}


def test_authorized_count_is_not_a_hardcoded_pair():
    visible=[device(i) for i in range(4)]
    assert [item['index'] for item in select_devices(visible,authorized=2,requested=1)]==[0]
    assert [item['index'] for item in select_devices(visible,authorized=3,requested=3)]==[0,1,2]
    with pytest.raises(RemoteError,match='REMOTE_GPU_UNAVAILABLE'):select_devices(visible[:2],authorized=3,requested=1)
    with pytest.raises(RemoteError,match='REMOTE_GPU_NOT_AUTHORIZED'):select_devices(visible,authorized=2,requested=3)


def test_segment_assignment_pins_distinct_devices_up_to_n():
    visible=[device(i) for i in range(4)]
    segments=[{'segment_id':name,'device_count':1} for name in ('a','b','c')]
    result=assign_segments(visible,authorized=2,segments=segments)
    assert [item['segment_id'] for item in result['assigned']]==['a','b']
    assert [item['segment_id'] for item in result['waiting']]==['c']
    assert [item['index'] for item in result['occupied']]==[0,1]
    with pytest.raises(RemoteError,match='REMOTE_GPU_DEVICE_MISMATCH'):
        assign_segments(visible,authorized=2,segments=[{'segment_id':'a'},{'segment_id':'a'}])


def test_declared_multi_gpu_process_uses_only_that_count():
    cards=[SimpleNamespace(type='CUDA',name=f'GPU {i}',id=f'cuda-{i}',use=False) for i in range(3)]
    cpu=SimpleNamespace(type='CPU',name='CPU',id='cpu',use=True)
    scene=SimpleNamespace(render=SimpleNamespace(engine='BLENDER_EEVEE'),cycles=SimpleNamespace(device='CPU'))
    prefs=fake_preferences([*cards,cpu])
    render_n={**render(),'device_count':3}
    evidence=configure_device(scene,prefs,render_n,[device(i) for i in range(3)])
    assert evidence['device_count']==3 and all(card.use for card in cards) and not cpu.use
    scene.cycles.device='CPU'
    with pytest.raises(ValueError):configure_device(scene,prefs,render(),device(0))


def test_real_blender_v2_cpu_global_interval_and_pinned_encoding(real_scene,tmp_path):
    from fsf.assembly.fast_assembly import require_ffmpeg
    from fsf.remote.segment_delivery import encoder_pin
    from fsf.runtime.blender import compile_blender_plan,execute_blender_plan,verify_bundle
    executable,source,record=real_scene
    pin=encoder_pin(require_ffmpeg())
    value=request(record,schema_version='fsf_blender_request_v2',start_frame=2,end_frame=4,
        render=render('cpu'),encode=pin,animation_range={'start_frame':0,'end_frame':4},motions=[{'object_id':'Camera','keys':[
            {'frame':0,'location':[0,0,8],'rotation_euler':[0,0,0]}, {'frame':3,'location':[1,0,8],'rotation_euler':[0,0,0]}]}])
    result=execute_blender_plan(compile_blender_plan(value),source_root=source,output_root=tmp_path,executable=executable,trusted_scene=True)
    manifest=verify_bundle(tmp_path/result['bundle'])
    assert manifest['schema_version']=='fsf_blender_manifest_v2'
    assert [f['scene_frame'] for f in manifest['frame_mapping']]==[2,3]
    assert manifest['execution_evidence']['device']=='CPU' and manifest['motion_review']=='not_reviewed'
