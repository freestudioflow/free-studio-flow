"""Versioned SigV4 data plane with bounded, verified transfer checkpoints."""
from contextlib import ExitStack,contextmanager
import base64
import copy
import hashlib
from importlib import resources
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from fsf.core.asset_library import _root, _no_links, _open_reader, _path_parts
from fsf.core.edit_store import edit_lock, read_json
from fsf.remote.contracts import RemoteError, seal, digest, sha, write_json

CHUNK_BYTES = 8*1024*1024


def shape(value,name):
    schema=json.loads(resources.files('fsf.schemas').joinpath(name+'.schema.json').read_text('utf-8'))
    if not Draft202012Validator(schema).is_valid(value):raise RemoteError('REMOTE_ARTIFACT_SCHEMA')
    json.dumps(value,allow_nan=False)
    return value


def validate_store(profile):
    if profile.get('schema_version') in {'fsf_kaggle_native_store_v1','fsf_kaggle_native_store_v2'}:
        from fsf.remote.native_store import validate
        return validate(profile)
    shape(profile,'fsf_artifact_store_v1')
    if not profile['authorized']:raise RemoteError('REMOTE_STORE_FORBIDDEN')
    parsed=urlsplit(profile['endpoint'])
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise RemoteError('REMOTE_STORE_ENDPOINT')
    if not re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',profile['bucket']):raise RemoteError('REMOTE_STORE_ENDPOINT')
    for prefix in profile['read_prefixes']+profile['write_prefixes']:
        if not prefix.endswith('/') or not re.fullmatch('[A-Za-z0-9_./-]+',prefix):raise RemoteError('REMOTE_STORE_NAMESPACE')
        _path_parts(prefix[:-1])
    bindings=profile['credential_bindings']
    if profile['credential_mode']=='aws_default_chain' and any(bindings.values()):raise RemoteError('REMOTE_STORE_AUTH_REQUIRED')
    if profile['credential_mode']=='bindings' and not all(bindings[k] for k in ('access_key','secret_key')):raise RemoteError('REMOTE_STORE_AUTH_REQUIRED')
    return copy.deepcopy(profile)


def store_identity(profile):
    profile=validate_store(profile)
    value={k:v for k,v in profile.items() if k!='credential_bindings'}
    # Local secret locations never leave the host; binding revision/scope still
    # participates in the operator configuration identity.
    if profile['credential_mode']=='bindings':
        value['binding_identities']={k:digest(read_json(p)) for k,p in profile['credential_bindings'].items() if p}
    return digest(value)


def store_public_identity(profile):
    """Same data authority can use different host-local secret mechanisms."""
    profile=validate_store(profile)
    if profile['schema_version'] in {'fsf_kaggle_native_store_v1','fsf_kaggle_native_store_v2'}:
        return digest({k:v for k,v in profile.items() if k!='root'})
    return digest({k:v for k,v in profile.items() if k not in {'credential_bindings','credential_mode'}})


def _key(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_./-]+',value):raise RemoteError('REMOTE_STORE_NAMESPACE')
    _path_parts(value)
    return value


def validate_reference(ref,profile,*,write=False):
    shape(ref,'fsf_artifact_reference_v1')
    if ref['version_id'].strip().lower() in ('null','latest','none') or any(c.isspace() for c in ref['version_id']):
        raise RemoteError('REMOTE_ARTIFACT_VERSION_REQUIRED')
    _key(ref['object_key'])
    if ref['store_id']!=profile['store_id'] or ref['size_bytes']>profile['max_artifact_bytes']:
        raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    if not any(ref['object_key'].startswith(p) for p in profile['write_prefixes' if write else 'read_prefixes']):
        raise RemoteError('REMOTE_STORE_FORBIDDEN')
    return ref


def validate_manifest(value,profile,identity):
    shape(value,'fsf_artifact_manifest_v1')
    if seal(value,'manifest_digest')['manifest_digest']!=value['manifest_digest']:
        raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    if any(value.get(k)!=v for k,v in identity.items()):raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
    ids=set();total=0
    for ref in value['artifacts']:
        validate_reference(ref,profile)
        if ref['artifact_id'] in ids:raise RemoteError('REMOTE_DUPLICATE_ID')
        ids.add(ref['artifact_id']);total+=ref['size_bytes']
    if total>profile['max_total_bytes']:raise RemoteError('REMOTE_SIZE_LIMIT')
    return value


def _client(profile):
    try:
        import boto3
        from botocore.config import Config
        config=Config(signature_version='s3v4',connect_timeout=10,read_timeout=30,
                      retries={'max_attempts':0},s3={'addressing_style':'path'})
        kwargs={}
        with ExitStack() as stack:
            if profile['credential_mode']=='bindings':
                from fsf.remote.credentials import credential_scope,credential_value
                for field,path in profile['credential_bindings'].items():
                    if not path:continue
                    credential_profile={'profile_id':profile['store_id']+'.'+field,'protocol':'s3_sigv4','key_env':None}
                    stack.enter_context(credential_scope(credential_profile,read_json(path)))
                    kwargs[{'access_key':'aws_access_key_id','secret_key':'aws_secret_access_key','session_token':'aws_session_token'}[field]]=credential_value(credential_profile)
            client=boto3.client('s3',endpoint_url=profile['endpoint'],region_name=profile['region'],config=config,**kwargs)
        allowed=urlsplit(profile['endpoint']).netloc
        def forbid_redirect(request,**kwargs):
            url=urlsplit(request.url)
            if url.scheme!='https' or url.netloc!=allowed:raise RemoteError('REMOTE_STORE_ENDPOINT')
        client.meta.events.register('before-send.s3',forbid_redirect)
        return client
    except RemoteError:raise
    except Exception:raise RemoteError('REMOTE_STORE_AUTH_REQUIRED') from None


class ArtifactStore:
    def __new__(cls,profile,**kwargs):
        if profile.get('schema_version') in {'fsf_kaggle_native_store_v1','fsf_kaggle_native_store_v2'}:
            from fsf.remote.native_store import NativeStore
            return NativeStore(profile,**kwargs)
        return super().__new__(cls)
    def __init__(self,profile,*,client=None,cancelled=None):
        self.profile=validate_store(profile)
        self._client=client
        self.cancelled=cancelled
        self.external_deadline=None

    @property
    def client(self):
        if self._client is None:self._client=_client(self.profile)
        return self._client

    @contextmanager
    def budget(self,deadline):
        previous=self.external_deadline
        self.external_deadline=min(previous,deadline) if previous is not None else deadline
        try:yield self
        finally:self.external_deadline=previous

    def _call(self,operation,deadline,**kwargs):
        if self.external_deadline is not None:deadline=min(deadline,self.external_deadline)
        if self.cancelled and self.cancelled():raise RemoteError('REMOTE_CANCELLED')
        if time.monotonic()>=deadline:raise RemoteError('REMOTE_TRANSFER_DEADLINE')
        try:return getattr(self.client,operation)(Bucket=self.profile['bucket'],**kwargs)
        except RemoteError:raise
        except Exception as exc:
            code=str(getattr(exc,'response',{}).get('Error',{}).get('Code',''))
            mapping={'403':'REMOTE_STORE_FORBIDDEN','AccessDenied':'REMOTE_STORE_FORBIDDEN',
                     '401':'REMOTE_STORE_AUTH_REQUIRED','InvalidAccessKeyId':'REMOTE_STORE_AUTH_REQUIRED',
                     'ExpiredToken':'REMOTE_STORE_AUTH_REQUIRED','NoSuchKey':'REMOTE_ARTIFACT_MISSING',
                     'NoSuchVersion':'REMOTE_ARTIFACT_MISSING','404':'REMOTE_ARTIFACT_MISSING',
                     'PreconditionFailed':'REMOTE_ARTIFACT_CONFLICT','412':'REMOTE_ARTIFACT_CONFLICT'}
            mapping['NoSuchUpload']='REMOTE_UPLOAD_EXPIRED'
            raise RemoteError(mapping.get(code,'REMOTE_STORE_UNAVAILABLE')) from None

    def _check_time(self,deadline):
        if self.cancelled and self.cancelled():raise RemoteError('REMOTE_CANCELLED')
        if self.external_deadline is not None:deadline=min(deadline,self.external_deadline)
        if time.monotonic()>=deadline:raise RemoteError('REMOTE_TRANSFER_DEADLINE')

    def _sha(self,path,deadline):
        value=hashlib.sha256()
        with _open_reader(path) as stream:
            while data:=stream.read(1024*1024):
                self._check_time(deadline);value.update(data)
        return value.hexdigest()

    def probe(self):
        deadline=time.monotonic()+min(30,self.profile['deadline_seconds'])
        value=self._call('get_bucket_versioning',deadline)
        if value.get('Status')!='Enabled':raise RemoteError('REMOTE_ARTIFACT_VERSION_REQUIRED')
        return {'connected':True,'versioning':True,'verified_transfer':False}

    def _head(self,key,deadline,version=None):
        kwargs={'Key':key}
        if version is not None:kwargs['VersionId']=version
        return self._call('head_object',deadline,**kwargs)

    def put(self,path,*,artifact_id,key,role,media_type,state_root):
        deadline=time.monotonic()+self.profile['deadline_seconds']
        path=Path(path).absolute();_no_links(path);_key(key)
        size=path.stat().st_size
        if not 0<size<=self.profile['max_artifact_bytes']:raise RemoteError('REMOTE_SIZE_LIMIT')
        if not any(key.startswith(p) for p in self.profile['write_prefixes']):raise RemoteError('REMOTE_STORE_FORBIDDEN')
        checksum=self._sha(path,deadline)
        identity={'artifact_id':artifact_id,'store_id':self.profile['store_id'],'object_key':key,
                  'sha256':checksum,'size_bytes':size,'role':role,'media_type':media_type}
        # Validate everything except the version, which is assigned by S3.
        validate_reference(dict(identity,version_id='pending'),self.profile,write=True)
        root=_root(state_root);binding=digest({'identity':identity,'store':store_public_identity(self.profile)})
        checkpoint=root/(binding[:32]+'.upload.json')
        with edit_lock(root/'.upload-target'):
            self.probe()
            try:existing=self._head(key,deadline)
            except RemoteError as exc:
                if exc.code!='REMOTE_ARTIFACT_MISSING':raise
                existing=None
            if existing:
                if existing.get('ContentLength')!=size or existing.get('Metadata',{}).get('sha256')!=checksum:
                    raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
                ref=dict(identity,version_id=existing.get('VersionId'))
                validate_reference(ref,self.profile,write=True)
                checkpoint.unlink(missing_ok=True)
                return ref
            state=read_json(checkpoint) if checkpoint.exists() else None
            if state and (seal(state,'checkpoint_digest')['checkpoint_digest']!=state.get('checkpoint_digest') or state['binding']!=binding):
                raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            def save():write_json(checkpoint,seal(state,'checkpoint_digest'))
            if state is None:
                state={'binding':binding,'created_at':time.time(),'upload_id':None,'parts':[]};save()
            left=self.profile['deadline_seconds']-(time.time()-state['created_at'])
            if left<=0:raise RemoteError('REMOTE_TRANSFER_DEADLINE')
            deadline=min(deadline,time.monotonic()+left)
            def upload_call(operation,**kwargs):
                try:return self._call(operation,deadline,**kwargs)
                except RemoteError as exc:
                    if exc.code=='REMOTE_UPLOAD_EXPIRED':
                        state['upload_id']=None;state['parts']=[];save()
                    raise
            if size<=CHUNK_BYTES:
                with _open_reader(path) as stream:data=stream.read(CHUNK_BYTES+1)
                if len(data)!=size or hashlib.sha256(data).hexdigest()!=checksum:raise RemoteError('REMOTE_SOURCE_CHANGED')
                reply=self._call('put_object',deadline,Key=key,Body=data,ContentType=media_type,
                    Metadata={'sha256':checksum},IfNoneMatch='*',ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode())
            else:
                if not state['upload_id']:
                    upload=self._call('create_multipart_upload',deadline,Key=key,ContentType=media_type,
                        Metadata={'sha256':checksum},ChecksumAlgorithm='SHA256')
                    state['upload_id']=upload['UploadId'];save()
                with _open_reader(path) as stream:
                    index=0
                    while data:=stream.read(CHUNK_BYTES):
                        index+=1
                        chunk_sha=base64.b64encode(hashlib.sha256(data).digest()).decode()
                        if index<=len(state['parts']):
                            if state['parts'][index-1]['ChecksumSHA256']!=chunk_sha:raise RemoteError('REMOTE_SOURCE_CHANGED')
                            continue
                        part=upload_call('upload_part',Key=key,UploadId=state['upload_id'],PartNumber=index,
                            Body=data,ChecksumSHA256=chunk_sha)
                        if part.get('ChecksumSHA256')!=chunk_sha:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                        state['parts'].append({'PartNumber':index,'ETag':part['ETag'],'ChecksumSHA256':chunk_sha});save()
                if self._sha(path,deadline)!=checksum or path.stat().st_size!=size:raise RemoteError('REMOTE_SOURCE_CHANGED')
                reply=upload_call('complete_multipart_upload',Key=key,UploadId=state['upload_id'],
                    MultipartUpload={'Parts':state['parts']},IfNoneMatch='*')
            ref=dict(identity,version_id=reply.get('VersionId'))
            validate_reference(ref,self.profile,write=True)
            head=self._head(key,deadline,ref['version_id'])
            if head.get('ContentLength')!=size or head.get('Metadata',{}).get('sha256')!=checksum:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            checkpoint.unlink(missing_ok=True)
            return ref

    def get(self,ref,*,output,state_root):
        validate_reference(ref,self.profile)
        output=Path(output).absolute();_no_links(output.parent)
        root=_root(state_root);binding=digest({'reference':ref,'store':store_public_identity(self.profile)})
        partial=root/(binding[:32]+'.partial');checkpoint=root/(binding[:32]+'.download.json')
        deadline=time.monotonic()+self.profile['deadline_seconds']
        with edit_lock(root/'.download-target'):
            if output.exists():
                _no_links(output)
                if output.stat().st_size==ref['size_bytes'] and self._sha(output,deadline)==ref['sha256']:return {'verified':True,'reused':True}
                raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
            head=self._head(ref['object_key'],deadline,ref['version_id'])
            if head.get('ContentLength')!=ref['size_bytes'] or head.get('VersionId')!=ref['version_id']:
                raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            state=read_json(checkpoint) if checkpoint.exists() else {'binding':binding,'created_at':time.time(),'chunks':[]}
            if state['binding']!=binding or (checkpoint.exists() and seal(state,'checkpoint_digest')['checkpoint_digest']!=state.get('checkpoint_digest')):
                raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            left=self.profile['deadline_seconds']-(time.time()-state['created_at'])
            if left<=0:raise RemoteError('REMOTE_TRANSFER_DEADLINE')
            deadline=min(deadline,time.monotonic()+left)
            write_json(checkpoint,seal(state,'checkpoint_digest'))
            offset=0
            if partial.exists():
                _no_links(partial)
                with _open_reader(partial) as stream:
                    for item in state['chunks']:
                        block=stream.read(item['size'])
                        if len(block)!=item['size'] or hashlib.sha256(block).hexdigest()!=item['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                        offset+=len(block)
                    incomplete_tail=bool(stream.read(1))
                if incomplete_tail:
                    # A crash can occur after fsync(data), before checkpoint.
                    # Only the already reverified prefix is authoritative.
                    with partial.open('r+b') as stream:stream.truncate(offset)
            elif state['chunks']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            while offset<ref['size_bytes']:
                end=min(offset+CHUNK_BYTES,ref['size_bytes'])-1
                reply=self._call('get_object',deadline,Key=ref['object_key'],VersionId=ref['version_id'],Range=f'bytes={offset}-{end}')
                body=reply['Body']
                try:
                    data=body.read(end-offset+2)
                except Exception:
                    raise RemoteError('REMOTE_STORE_UNAVAILABLE') from None
                finally:body.close()
                self._check_time(deadline)
                if len(data)!=end-offset+1 or reply.get('VersionId')!=ref['version_id'] or reply.get('ContentRange')!=f'bytes {offset}-{end}/{ref["size_bytes"]}':
                    raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                with partial.open('ab') as stream:
                    stream.write(data);stream.flush()
                    import os
                    os.fsync(stream.fileno())
                state['chunks'].append({'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                write_json(checkpoint,seal(state,'checkpoint_digest'));offset+=len(data)
            if partial.stat().st_size!=ref['size_bytes'] or self._sha(partial,deadline)!=ref['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            # Output and checkpoint may be on different volumes: publish through
            # a same-directory temporary file and verify its bytes first.
            import os,shutil,tempfile
            fd,tmp=tempfile.mkstemp(prefix='.artifact-',dir=output.parent)
            try:
                with os.fdopen(fd,'wb') as target,_open_reader(partial) as source:
                    shutil.copyfileobj(source,target,1024*1024);target.flush();os.fsync(target.fileno())
                if self._sha(tmp,deadline)!=ref['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                self._check_time(deadline)
                if output.exists():raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
                os.replace(tmp,output)
            finally:Path(tmp).unlink(missing_ok=True)
            partial.unlink();checkpoint.unlink(missing_ok=True)
            return {'verified':True,'reused':False}
