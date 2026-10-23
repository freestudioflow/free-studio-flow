"""Native transport retains the v2 evidence gates without host cloud secrets."""
import copy
import json
from pathlib import Path
import pytest
import sys
from types import SimpleNamespace
from fsf.remote.artifacts import ArtifactStore,store_public_identity
from fsf.remote.contracts import RemoteError,seal
from fsf.remote.v2 import prepare_request,run_worker,collect_result
from fsf.remote.kaggle_native import prepare,export_outputs,import_outputs
from test_remote_v2 import case,remote_wheel

def config(root):
    return {'schema_version':'fsf_kaggle_native_store_v1','store_id':'native_test','authorized':True,'root':str(root),
        'read_prefixes':['jobs/'],'write_prefixes':['jobs/'],'max_artifact_bytes':64*1024*1024,
        'max_total_bytes':64*1024*1024,'deadline_seconds':300,'max_inline_notebook_bytes':1048576}

@pytest.fixture
def native(case,remote_wheel):
    root,_,_,profile,old=case
    store=ArtifactStore(config(root/'native'))
    profile=copy.deepcopy(profile)
    profile['store_binding'].update(store_id=store.profile['store_id'],config_digest=store_public_identity(store.profile))
    wheel=store.put(remote_wheel,artifact_id='worker_wheel',key='jobs/inputs/'+remote_wheel.name,role='worker_wheel',media_type='application/zip',state_root=root)
    definition={k:old[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')};definition['inputs']={}
    request=prepare_request(definition,old['recipe'],profile=profile,store=store,inputs={},wheel=wheel,segment=old['segment'],output=root/'prepared')
    return root,store,profile,request

def test_native_real_cpu_roundtrip_no_credential_manager(native,monkeypatch):
    root,store,profile,request=native
    monkeypatch.setattr('fsf.remote.artifacts._client',lambda p:pytest.fail('external storage client used'))
    result=run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    bundle=root/'native-result.zip';export_outputs(request,store,bundle)
    receiver=ArtifactStore({**store.profile,'root':str(root/'receiver')})
    imported=import_outputs(request,receiver,bundle)
    assert imported==result
    collected=collect_result(request,store=receiver,directory=root/'accepted')
    assert collected['status']=='completed'
    assert json.loads((root/'accepted/accepted/outputs/result.json').read_text())['square']==49
    assert collected['motion_review']=='not_reviewed'

def test_native_notebook_has_no_secret_binding_and_binds_gpu(native):
    root,store,_,request=native
    doc=prepare(request,store,root/'prepared')
    code=''.join(doc['cells'][0]['source'])
    assert 'UserSecretsClient' not in code and 'get_secret' not in code
    assert doc['metadata']['kaggle']['isGpuEnabled'] is False
    assert store_public_identity(store.profile)==store_public_identity({**store.profile,'root':'another-host'})

def test_native_overlimit_rejected_before_submission(native):
    root,store,_,request=native
    store.profile['max_inline_notebook_bytes']=1024
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE'):prepare(request,store,root/'prepared')

def test_native_corrupt_bytes_and_version_fail_closed(native):
    root,store,_,request=native
    ref=request['worker_wheel'];changed={**ref,'version_id':'latest'}
    with pytest.raises(RemoteError):store.get(changed,output=root/'bad',state_root=root)
    store._path(ref).write_bytes(b'corrupt')
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_INTEGRITY'):store.get(ref,output=root/'bad',state_root=root)

@pytest.mark.parametrize('field,value',[('object_key','../escape'),('store_id','another'),('version_id','b'*64)])
def test_native_reference_identity_not_basename_or_hash_only(native,field,value):
    root,store,_,request=native
    ref={**request['worker_wheel'],field:value}
    with pytest.raises(RemoteError):store.get(ref,output=root/'bad',state_root=root)

def test_native_api_submission_and_tamper_guard(native,monkeypatch):
    from fsf.remote.kaggle_native import call
    root,store,profile,request=native
    profile=copy.deepcopy(profile);profile['transport']='kaggle'
    profile['settings']={'accelerator':'NvidiaTeslaT4','owner_env':'SYNTHETIC_KAGGLE_OWNER'}
    store_path=root/'native.json';store_path.write_text(json.dumps(store.profile))
    calls=[]
    class API:
        config_values={'username':'synthetic-owner'}
        def authenticate(self):pass
        def kernels_push(self,path,**kwargs):
            meta=json.loads((Path(path)/'kernel-metadata.json').read_text())
            calls.append((meta,kwargs))
            return SimpleNamespace(error=False,version_number=1)
    monkeypatch.setitem(sys.modules,'kaggle.api.kaggle_api_extended',SimpleNamespace(KaggleApi=API))
    value={'operation':'prepare','request':request,'profile':profile,'directory':str(root/'prepared'),'store_config':str(store_path)}
    assert call(value)['data_plane']=='kaggle_native_v1'
    value['operation']='submit'
    good=(root/'prepared/worker.ipynb').read_bytes()
    (root/'prepared/worker.ipynb').write_text('{}')
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_POLICY'):call(value)
    assert calls==[]
    (root/'prepared/worker.ipynb').write_bytes(good)
    assert call(value)['version']==1
    assert calls[0][0]['is_private'] is True and calls[0][0]['enable_gpu'] is False
    assert calls[0][1]['acc'] is None

def test_native_result_extra_file_rejected_atomically(native):
    import zipfile
    root,store,_,request=native
    run_worker(request,store=store,output_root=root/'worker',approved_recipe=request['recipe']['recipe_digest'])
    bundle=root/'native-result.zip';export_outputs(request,store,bundle)
    with zipfile.ZipFile(bundle,'a') as archive:archive.writestr('unexpected.json','{}')
    receiver=ArtifactStore({**store.profile,'root':str(root/'receiver')})
    with pytest.raises(RemoteError,match='REMOTE_ARCHIVE'):import_outputs(request,receiver,bundle)
    assert not (receiver.root/'references').exists()

def test_native_ssh_uses_existing_disk_and_scp(native):
    from fsf.remote.native_ssh import NativeSsh
    root,store,_,request=native
    config_path=root/'native.json';config_path.write_text(json.dumps(store.profile))
    calls=[]
    class SSH:
        settings={'remote_root':'/workspace/fsf','python':'python3','host_alias':'approved-worker'}
        def prepare(self,*args):calls.append(('prepare',))
        def _ssh(self,args):calls.append(('ssh',args));return 'launched'
        def _scp(self,source,target):calls.append(('scp',str(source),str(target)))
    adapter=NativeSsh(SSH(),str(config_path))
    adapter.prepare(request,root/'prepared')
    result=adapter.submit(request,root/'prepared')
    assert result['data_plane']=='ssh_native_v1'
    transfers=[v for v in calls if v[0]=='scp']
    assert len(transfers)==2 and all('/workspace/fsf/native-' in v[2] for v in transfers)
    launcher=(root/'prepared/native-launch.py').read_text()
    assert 'WORK_ROOT' in launcher and 'RESULT_OUT' in launcher

def test_existing_native_notebook_is_bound_to_exact_new_version(native,monkeypatch):
    from fsf.remote.kaggle_native import call
    from fsf.remote import kaggle_v2_api as gateway
    root,store,profile,request=native
    profile=copy.deepcopy(profile);profile['transport']='kaggle'
    profile['settings']={'accelerator':'NvidiaTeslaT4','native_notebook':'fsf-existing-test'}
    store_path=root/'native.json';store_path.write_text(json.dumps(store.profile))
    saved=SimpleNamespace(metadata=SimpleNamespace(is_private=True,current_version_number=4,id=42),blob=SimpleNamespace(source='{}'))
    calls=[]
    class API:
        config_values={'username':'synthetic-owner'}
        def authenticate(self):pass
        def kernels_push(self,path,**kwargs):
            calls.append(json.loads((Path(path)/'kernel-metadata.json').read_text()))
            saved.metadata.current_version_number=5
            saved.blob.source=(Path(path)/'worker.ipynb').read_text()
            return SimpleNamespace(error=False,version_number=5)
    monkeypatch.setitem(sys.modules,'kaggle.api.kaggle_api_extended',SimpleNamespace(KaggleApi=API))
    monkeypatch.setattr(gateway,'_saved',lambda *args:saved)
    monkeypatch.setattr(gateway,'_version_status',lambda *args:SimpleNamespace(status=SimpleNamespace(name='COMPLETE')))
    value={'operation':'prepare','request':request,'profile':profile,'directory':str(root/'prepared'),'store_config':str(store_path)}
    call(value);value['operation']='submit';remote=call(value)
    assert remote['version']==5 and calls[0]['id_no']==42
    assert calls[0]['id']=='synthetic-owner/fsf-existing-test'
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_VERSION_AMBIGUOUS'):call(value)
    value.update(operation='status',remote=remote)
    assert call(value)['status']=='completed'
    saved.metadata.current_version_number=6
    with pytest.raises(RemoteError,match='REMOTE_KAGGLE_VERSION_AMBIGUOUS'):call(value)
