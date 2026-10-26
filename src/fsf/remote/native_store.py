"""Host-local immutable staging for Kaggle's native input/output transport.

These are content versions, not S3 VersionIds or remote durability promises.
The local root is host configuration and never part of portable authority.
"""
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import shutil
import tempfile
import time
from fsf.core.asset_library import _root,_no_links,_open_reader,AssetValidationError
from fsf.core.edit_store import edit_lock,read_json
from fsf.remote.contracts import RemoteError,digest,sha,write_json,safe_path

SCHEMA='fsf_kaggle_native_store_v1'
SCHEMAS={SCHEMA,'fsf_kaggle_native_store_v2'}

def validate(profile):
    from fsf.remote.artifacts import shape,_key
    version=profile.get('schema_version')
    if version not in SCHEMAS:raise RemoteError('REMOTE_ARTIFACT_SCHEMA')
    shape(profile,version)
    if not profile['authorized']:raise RemoteError('REMOTE_STORE_FORBIDDEN')
    for prefix in profile['read_prefixes']+profile['write_prefixes']:
        if not prefix.endswith('/'):raise RemoteError('REMOTE_STORE_NAMESPACE')
        _key(prefix[:-1])
    return copy.deepcopy(profile)

class NativeStore:
    def __init__(self,profile,**kwargs):
        self.profile=validate(profile)
        path=Path(profile['root']).absolute();_no_links(path.parent)
        path.mkdir(parents=True,exist_ok=True);self.root=_root(path)
        self.cancelled=kwargs.get('cancelled');self.external_deadline=None

    @contextmanager
    def budget(self,deadline):
        previous=self.external_deadline
        self.external_deadline=min(previous,deadline) if previous is not None else deadline
        try:yield self
        finally:self.external_deadline=previous

    def _check(self):
        if self.cancelled and self.cancelled():raise RemoteError('REMOTE_CANCELLED')
        if self.external_deadline is not None and time.monotonic()>=self.external_deadline:
            raise RemoteError('REMOTE_TRANSFER_DEADLINE')

    def _meta(self,key):
        from fsf.remote.artifacts import _key
        _key(key)
        return self.root/'references'/(digest(key)+'.json')

    def _path(self,ref):
        from fsf.remote.artifacts import validate_reference
        try:validate_reference(ref,self.profile)
        except AssetValidationError:raise RemoteError('REMOTE_PATH') from None
        if ref['version_id']!=ref['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
        return safe_path(self.root,'objects/'+digest({'key':ref['object_key'],'version':ref['version_id']}),exists=False)

    def _copy(self,source,target,expected,size):
        self._check();_no_links(source);target.parent.mkdir(parents=True,exist_ok=True);_no_links(target.parent)
        fd,name=tempfile.mkstemp(prefix='.native-',dir=target.parent)
        try:
            count=0
            with os.fdopen(fd,'wb') as out,_open_reader(source) as inp:
                while block:=inp.read(1024*1024):
                    self._check();count+=len(block)
                    if count>size:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
                    out.write(block)
                out.flush();os.fsync(out.fileno())
            if count!=size or sha(name)!=expected:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            if target.exists():
                _no_links(target)
                if target.stat().st_size!=size or sha(target)!=expected:raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
            else:os.replace(name,target)
        finally:Path(name).unlink(missing_ok=True)

    def put(self,path,*,artifact_id,key,role,media_type,state_root):
        from fsf.remote.artifacts import validate_reference
        path=Path(path).absolute();_no_links(path);size=path.stat().st_size
        if not 0<size<=self.profile['max_artifact_bytes']:raise RemoteError('REMOTE_SIZE_LIMIT')
        checksum=sha(path)
        ref={'artifact_id':artifact_id,'store_id':self.profile['store_id'],'object_key':key,
             'version_id':checksum,'sha256':checksum,'size_bytes':size,'role':role,'media_type':media_type}
        validate_reference(ref,self.profile,write=True)
        with edit_lock(self.root/'.native-store'):
            meta=self._meta(key)
            if meta.exists() and read_json(meta)!=ref:raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
            self._copy(path,self._path(ref),checksum,size)
            meta.parent.mkdir(exist_ok=True);write_json(meta,ref)
        return ref

    def get(self,ref,*,output,state_root):
        self._check();path=self._path(ref)
        if not path.is_file():raise RemoteError('REMOTE_ARTIFACT_MISSING')
        target=Path(output);reused=target.exists()
        self._copy(path,target,ref['sha256'],ref['size_bytes'])
        return {'verified':True,'reused':reused}

    def _head(self,key,deadline,version=None):
        self._check();meta=self._meta(key)
        if not meta.is_file():raise RemoteError('REMOTE_ARTIFACT_MISSING')
        ref=read_json(meta);path=self._path(ref)
        if ref['object_key']!=key or (version is not None and version!=ref['version_id']):raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
        if not path.is_file() or sha(path)!=ref['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
        return {'VersionId':ref['version_id'],'Metadata':{'sha256':ref['sha256']},'ContentLength':path.stat().st_size}

    def probe(self):
        return {'connected':False,'local_staging':True,'verified_transfer':False,'remote_durability':False}
