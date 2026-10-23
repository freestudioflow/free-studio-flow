"""Versioned object transfer with a deterministic in-memory S3 contract double."""
import copy
import hashlib
import io
from pathlib import Path

import pytest

from fsf.remote.artifacts import ArtifactStore,CHUNK_BYTES,validate_store,validate_reference,validate_manifest
from fsf.remote.contracts import RemoteError,seal


class Error(Exception):
    def __init__(self,code):self.response={'Error':{'Code':code}}


class Store:
    def __init__(self):
        self.objects={};self.uploads={};self.puts=0;self.parts=0;self.gets=0;self.interrupt_part=None;self.interrupt_get=None
    def get_bucket_versioning(self,**kwargs):return {'Status':'Enabled'}
    def head_object(self,Key,VersionId=None,**kwargs):
        if Key not in self.objects:raise Error('NoSuchKey')
        value=self.objects[Key]
        if VersionId and VersionId!=value['VersionId']:raise Error('NoSuchVersion')
        return {'VersionId':value['VersionId'],'ContentLength':len(value['data']),'Metadata':value['metadata']}
    def put_object(self,Key,Body,Metadata,IfNoneMatch,**kwargs):
        if Key in self.objects:raise Error('PreconditionFailed')
        assert IfNoneMatch=='*';self.puts+=1
        self.objects[Key]={'VersionId':'v1','data':Body,'metadata':Metadata}
        return {'VersionId':'v1'}
    def create_multipart_upload(self,Key,Metadata,**kwargs):
        self.uploads['upload1']={'key':Key,'metadata':Metadata,'parts':{}}
        return {'UploadId':'upload1'}
    def upload_part(self,UploadId,PartNumber,Body,ChecksumSHA256,**kwargs):
        if self.interrupt_part==PartNumber:
            self.interrupt_part=None;raise ConnectionError('synthetic secret never echoed')
        self.parts+=1;self.uploads[UploadId]['parts'][PartNumber]=Body
        return {'ETag':str(PartNumber),'ChecksumSHA256':ChecksumSHA256}
    def complete_multipart_upload(self,Key,UploadId,MultipartUpload,IfNoneMatch,**kwargs):
        if Key in self.objects:raise Error('PreconditionFailed')
        assert IfNoneMatch=='*'
        upload=self.uploads[UploadId]
        data=b''.join(upload['parts'][p['PartNumber']] for p in MultipartUpload['Parts'])
        self.objects[Key]={'VersionId':'v1','data':data,'metadata':upload['metadata']}
        return {'VersionId':'v1'}
    def get_object(self,Key,VersionId,Range,**kwargs):
        self.gets+=1
        if self.interrupt_get==self.gets:raise ConnectionError('synthetic private endpoint')
        value=self.objects[Key];assert value['VersionId']==VersionId
        start,end=map(int,Range[6:].split('-'))
        return {'Body':io.BytesIO(value['data'][start:end+1]),'VersionId':VersionId,
                'ContentRange':f'bytes {start}-{end}/{len(value["data"])}'}


def profile():
    return {'schema_version':'fsf_artifact_store_v1','store_id':'private_store','authorized':True,
            'endpoint':'https://s3.example.invalid','region':'region-1','bucket':'fsf-private-fixtures',
            'read_prefixes':['jobs/'],'write_prefixes':['jobs/'],
            'max_artifact_bytes':64*1024*1024,'max_total_bytes':128*1024*1024,'deadline_seconds':30,
            'credential_mode':'aws_default_chain','credential_bindings':{'access_key':None,'secret_key':None,'session_token':None}}


@pytest.fixture
def case(tmp_path):
    state=tmp_path/'state';state.mkdir()
    service=Store();client=ArtifactStore(profile(),client=service)
    return tmp_path,state,service,client


def put(case,data=b'small payload'):
    root,state,service,client=case
    path=root/'input.bin';path.write_bytes(data)
    ref=client.put(path,artifact_id='asset',key='jobs/job1/asset.bin',role='data',media_type='application/octet-stream',state_root=state)
    return path,ref


def test_small_transfer_is_versioned_verified_and_idempotent(case):
    root,state,service,client=case
    path,ref=put(case)
    again=client.put(path,artifact_id='asset',key=ref['object_key'],role='data',media_type='application/octet-stream',state_root=state)
    assert again==ref and service.puts==1
    result=client.get(ref,output=root/'output.bin',state_root=state)
    assert result=={'verified':True,'reused':False}
    assert (root/'output.bin').read_bytes()==path.read_bytes()
    assert client.get(ref,output=root/'output.bin',state_root=state)['reused']


def test_multipart_interruption_resumes_without_reuploading_verified_parts(case):
    root,state,service,client=case
    data=b'A'*CHUNK_BYTES+b'B'*CHUNK_BYTES+b'C'*13
    path=root/'input.bin';path.write_bytes(data);service.interrupt_part=2
    with pytest.raises(RemoteError,match='REMOTE_STORE_UNAVAILABLE'):
        client.put(path,artifact_id='asset',key='jobs/job1/asset.bin',role='data',media_type='application/octet-stream',state_root=state)
    assert service.parts==1
    ref=client.put(path,artifact_id='asset',key='jobs/job1/asset.bin',role='data',media_type='application/octet-stream',state_root=state)
    assert service.parts==3 and service.objects[ref['object_key']]['data']==data


def test_download_interruption_and_uncheckpointed_tail(case):
    root,state,service,client=case
    _,ref=put(case,b'A'*CHUNK_BYTES+b'B'*37)
    service.interrupt_get=2
    with pytest.raises(RemoteError,match='REMOTE_STORE_UNAVAILABLE'):
        client.get(ref,output=root/'output.bin',state_root=state)
    with next(state.glob('*.partial')).open('ab') as stream:stream.write(b'uncommitted crash tail')
    service.interrupt_get=None
    assert client.get(ref,output=root/'output.bin',state_root=state)['verified']
    assert service.gets==3
    assert hashlib.sha256((root/'output.bin').read_bytes()).hexdigest()==ref['sha256']


@pytest.mark.parametrize('field,value,code',[
    ('version_id','latest','REMOTE_ARTIFACT_VERSION_REQUIRED'),
    ('version_id','null','REMOTE_ARTIFACT_VERSION_REQUIRED'),
    ('object_key','jobs/../private',''),('object_key','private/asset.bin','REMOTE_STORE_FORBIDDEN'),
    ('store_id','another_store','REMOTE_ARTIFACT_INTEGRITY'),
    ('size_bytes',128*1024*1024,'REMOTE_ARTIFACT_INTEGRITY')])
def test_invalid_references_fail_before_network(case,field,value,code):
    root,state,service,client=case;_,ref=put(case);ref[field]=value
    with pytest.raises((RemoteError,ValueError),match=code):client.get(ref,output=root/'output.bin',state_root=state)
    assert service.gets==0


def test_wrong_bytes_and_truncated_ranges_never_publish(case):
    root,state,service,client=case;_,ref=put(case)
    service.objects[ref['object_key']]['data']=b'X'*ref['size_bytes']
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_INTEGRITY'):client.get(ref,output=root/'output.bin',state_root=state)
    assert not (root/'output.bin').exists()


def test_key_conflict_is_not_overwritten(case):
    root,state,service,client=case;path,ref=put(case);path.write_bytes(b'different bytes')
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_CONFLICT'):
        client.put(path,artifact_id='asset',key=ref['object_key'],role='data',media_type='application/octet-stream',state_root=state)
    assert service.puts==1


def test_manifest_binds_job_attempt_and_all_artifacts(case):
    _,_,_,client=case;_,ref=put(case)
    manifest=seal({'schema_version':'fsf_artifact_manifest_v1','job_id':'job','run_id':'run','attempt_id':1,
        'request_digest':'a'*64,'recipe_digest':'b'*64,'artifacts':[ref]},'manifest_digest')
    assert validate_manifest(manifest,client.profile,{'attempt_id':1})==manifest
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_IDENTITY'):validate_manifest(manifest,client.profile,{'attempt_id':2})
    manifest['artifacts'][0]['role']='video'
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_INTEGRITY'):validate_manifest(manifest,client.profile,{'attempt_id':1})


def test_store_auth_and_endpoint_are_explicit():
    p=profile();p['authorized']=False
    with pytest.raises(RemoteError,match='REMOTE_STORE_FORBIDDEN'):validate_store(p)
    p=profile();p['endpoint']='http://127.0.0.1:9000'
    with pytest.raises(RemoteError,match='REMOTE_STORE_ENDPOINT'):validate_store(p)


def test_versioning_required_and_cancelled_transfers_do_not_publish(case):
    root,state,service,client=case
    service.get_bucket_versioning=lambda **kwargs:{}
    with pytest.raises(RemoteError,match='REMOTE_ARTIFACT_VERSION_REQUIRED'):put(case)
    client.cancelled=lambda:True
    with pytest.raises(RemoteError,match='REMOTE_CANCELLED'):client.probe()
