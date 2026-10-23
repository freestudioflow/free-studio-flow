"""Batch receipt rejection tests. Synthetic manifests are not live GPU evidence."""
import copy
from pathlib import Path

import pytest
from fsf.remote.contracts import RemoteError, digest, seal, sha, write_json
from fsf.core.edit_store import read_json
from fsf.remote.bundle import pack
from fsf.remote.gpu_batch import verify_gpu_batch
from test_gpu_schedule import plan, devices


def batch(tmp_path, monkeypatch, mutation=None,shared=False):
    value=plan(2);value['tasks']=value['tasks'][:4]
    if shared:
        value['authorized_devices']=1
        value['sharing']={'max_tasks_per_device':2,'memory_budget_mb':12000,'headroom_mb':2048}
        for task in value['tasks']:task['vram_mb_per_device']=4096
    for task in value['tasks']:task['outputs']=['segment.zip']
    requirements={'render':{'device':'gpu','device_count':value['authorized_devices']},'encode':{'encoder':'libx264'},'schedule_digest':digest(value)}
    request={'recipe':{'execution_requirements':requirements},'segment':{'start_frame':0,'end_frame':4,'clock':{'num':24,'den':1}}}
    root=tmp_path/'bundle';root.mkdir()
    state={'plan_digest':digest(value),'status':'completed','authorized_devices':value['authorized_devices'],'selected_devices':devices(value['authorized_devices']),'tasks':{}}
    if shared:state['memory_samples']={devices(1)[0]['uuid']:{'sampled_peak_mb':2000,'max_compute_processes':2,'samples':4}}
    names=['schedule-receipt.json']
    for i,task in enumerate(value['tasks']):
        d=devices(2)[0 if shared else i%2]
        chosen=d if mutation!='wrong_device' else devices(4)[3]
        manifest={'schema_version':'fsf_blender_manifest_v2','render_profile':{**requirements['render'],'device_count':1},
            'encode_profile':requirements['encode'],'clock':{'num':24,'den':1},
            'execution_evidence':{'device':'CPU' if mutation=='cpu' else 'GPU','selection':chosen},
            'frame_mapping':[{'scene_frame':0 if mutation=='duplicate_frame' else i}]}
        source=tmp_path/f'manifest-{i}';source.mkdir();write_json(source/'manifest.json',manifest)
        relative=f'{i}/1/segment.zip';target=root/relative;target.parent.mkdir(parents=True)
        pack(source,['manifest.json'],target)
        state['tasks'][str(i)]={'status':'accepted','attempt':1,'devices':[d],
            'started':0 if mutation=='overlap' else i//2,'finished':2 if mutation=='overlap' else i//2+1,
            'outputs':[{'path':relative,'sha256':'a'*64 if mutation=='hash' else sha(target),'size_bytes':target.stat().st_size}]}
        names.append(relative)
    if mutation=='plan':state['plan_digest']='a'*64
    write_json(root/'schedule-receipt.json',seal(state,'receipt_digest'))
    archive=tmp_path/'batch.zip';pack(root,names,archive)
    monkeypatch.setattr('fsf.remote.gpu_batch.verify_bundle',lambda p:read_json(Path(p)/'manifest.json'))
    return archive,request,value


def test_batch_checks_each_device_and_full_frame_coverage(tmp_path,monkeypatch):
    archive,request,value=batch(tmp_path,monkeypatch)
    assert verify_gpu_batch(archive,request,value,tmp_path)['frame_count']==4


@pytest.mark.parametrize('mutation',['wrong_device','cpu','duplicate_frame','overlap','hash','plan'])
def test_batch_rejects_bad_evidence(tmp_path,monkeypatch,mutation):
    archive,request,value=batch(tmp_path,monkeypatch,mutation)
    with pytest.raises(RemoteError):verify_gpu_batch(archive,request,value,tmp_path)


def test_shared_batch_keeps_explicit_slots_and_memory_evidence(tmp_path,monkeypatch):
    archive,request,value=batch(tmp_path,monkeypatch,shared=True)
    result=verify_gpu_batch(archive,request,value,tmp_path)
    assert result['authorized_devices']==1 and result['sharing']['max_tasks_per_device']==2


def test_shared_batch_still_rejects_excess_concurrency(tmp_path,monkeypatch):
    archive,request,value=batch(tmp_path,monkeypatch,mutation='overlap',shared=True)
    with pytest.raises(RemoteError):verify_gpu_batch(archive,request,value,tmp_path)
