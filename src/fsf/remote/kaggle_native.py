"""Kaggle API control and native outputs; no remote object-store credentials."""
import base64
import contextlib
from importlib import resources
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,digest,sha,write_json,safe_path,seal
from fsf.remote.bundle import pack,unpack
from fsf.remote.native_store import NativeStore,SCHEMA
from fsf.remote import streaming


@contextlib.contextmanager
def _dataset_phase(code):
    try:
        yield
    except RemoteError:
        raise
    except Exception:
        raise RemoteError(code) from None

def prepare(request,store,directory):
    """Freeze only this request's inputs, not the entire host staging folder."""
    directory=Path(directory);folder=directory/'native-input'
    folder.mkdir(exist_ok=True)
    request_path=folder/'request.json';write_json(request_path,request)
    portable={**store.profile,'root':'objects'};write_json(folder/'store.json',portable)
    paths=['request.json','store.json']
    for ref in [request['worker_wheel'],*(a['reference'] for a in request['inputs'])]:
        name='blobs/'+ref['sha256']
        target=safe_path(folder,name,exists=False)
        store.get(ref,output=target,state_root=directory)
        paths.append(name)
    bundle=directory/'native-input.zip'
    if 'limits' in request:
        if not bundle.exists():
            streaming.pack_archive(folder,paths,bundle,request['limits'])
        else:
            with tempfile.TemporaryDirectory(dir=directory) as temp:
                target=Path(temp)/'unpacked'
                streaming.extract_archive(bundle,target,request['limits'],None)
                names={p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
                if names!=set(paths) or any(sha(target/name)!=sha(folder/name) for name in names):
                    raise RemoteError('REMOTE_KAGGLE_POLICY')
        raise RemoteError('REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE')
    if not bundle.exists():pack(folder,sorted(set(paths)),bundle)
    else:
        # Do not trust a stale or manually replaced prepared bundle.
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            names=unpack(bundle,temp)
            if names!=set(paths) or any(sha(Path(temp)/name)!=sha(folder/name) for name in paths):
                raise RemoteError('REMOTE_KAGGLE_POLICY')
    doc=notebook(request,bundle)
    if len(json.dumps(doc).encode())>store.profile['max_inline_notebook_bytes']:
        raise RemoteError('REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE')
    write_json(directory/'worker.ipynb',doc)
    return doc

def notebook(request,bundle):
    bootstrap=resources.files('fsf.tools').joinpath('remote_bootstrap_native.py').read_bytes()
    code='import base64,pathlib\n'
    code+="b=pathlib.Path('/tmp/fsf-native-input.zip');b.write_bytes(base64.b64decode("+repr(base64.b64encode(Path(bundle).read_bytes()).decode())+"))\n"
    code+='scope={"BUNDLE":str(b),"BUNDLE_SHA":'+repr(sha(bundle))+',"DEADLINE":'+str(request['policy']['deadline_seconds'])+'}\n'
    code+='exec(compile(base64.b64decode('+repr(base64.b64encode(bootstrap).decode())+'),"fsf_native_bootstrap","exec"),scope)\n'
    return {'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'},
        'kaggle':{'isGpuEnabled':request['policy']['gpu'],'isInternetEnabled':True}},
        'cells':[{'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':code.splitlines(keepends=True)}]}

def _payload_slug(request):
    return 'fsf-native-'+request['request_digest'][:20]

def external_notebook(request,bundle_sha,slug):
    """Mount an immutable private dataset instead of inlining the input archive."""
    bootstrap=resources.files('fsf.tools').joinpath('remote_bootstrap_native.py').read_bytes()
    code='import base64,hashlib,pathlib\n'
    code+='src=pathlib.Path('+repr('/kaggle/input/'+slug+'/payload.bin')+')\n'
    if 'limits' in request:
        code+='candidates=[p for p in pathlib.Path("/kaggle/input").glob('+repr('**/'+slug+'/payload.bin')+') if p.is_file()]\n'
        code+='if len(candidates)!=1:\n print("REMOTE_KAGGLE_MOUNT_NOT_UNIQUE",len(candidates))\n raise SystemExit(2)\n'
        code+='b=candidates[0]\n'
        scope={key:request[key] for key in ('limits','limits_digest','request_digest')}
        code+='scope='+repr(scope)+'\n'
        code+='scope.update(BUNDLE=str(b), BUNDLE_SHA='+repr(bundle_sha)+', DEADLINE='+str(request['policy']['deadline_seconds'])+')\n'
    else:
        code+='data=src.read_bytes()\n'
        code+='if hashlib.sha256(data).hexdigest()!='+repr(bundle_sha)+':\n raise SystemExit(2)\n'
        code+="b=pathlib.Path('/tmp/fsf-native-input.zip');b.write_bytes(data)\n"
        code+='scope={"BUNDLE":str(b),"BUNDLE_SHA":'+repr(bundle_sha)+',"DEADLINE":'+str(request['policy']['deadline_seconds'])+'}\n'
    code+='exec(compile(base64.b64decode('+repr(base64.b64encode(bootstrap).decode())+'),"fsf_native_bootstrap","exec"),scope)\n'
    return {'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'},
        'kaggle':{'isGpuEnabled':request['policy']['gpu'],'isInternetEnabled':True}},
        'cells':[{'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':code.splitlines(keepends=True)}]}

def stage_notebook(request,store,directory):
    try:return prepare(request,store,directory)
    except RemoteError as exc:
        if exc.code!='REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE':raise
    directory=Path(directory);bundle=directory/'native-input.zip';slug=_payload_slug(request)
    doc=external_notebook(request,sha(bundle),slug)
    if len(json.dumps(doc).encode())>store.profile['max_inline_notebook_bytes']:
        raise RemoteError('REMOTE_KAGGLE_NATIVE_INPUT_TOO_LARGE')
    write_json(directory/'worker.ipynb',doc)
    write_json(directory/'external-payload.json',{'slug':slug,'sha256':sha(bundle),'filename':'payload.bin'})
    return doc

def dataset_source(request,owner,directory):
    path=Path(directory)/'external-payload.json'
    if not path.exists():return None
    bundle=Path(directory)/'native-input.zip';slug=_payload_slug(request)
    if read_json(path)!={'slug':slug,'sha256':sha(bundle),'filename':'payload.bin'}:
        raise RemoteError('REMOTE_KAGGLE_POLICY')
    return owner+'/'+slug

def _private_flag(value):
    if isinstance(value,dict):
        for key in ('isPrivate','is_private'):
            if isinstance(value.get(key),bool):return value[key]
        for child in value.values():
            found=_private_flag(child)
            if found is not None:return found
    elif isinstance(value,list):
        for child in value:
            found=_private_flag(child)
            if found is not None:return found
    return None

def verify_dataset_file(api,ref,bundle,limits):
    from kagglesdk.datasets.types.dataset_api_service import ApiDownloadDatasetRequest
    from fsf.remote.kaggle_api import _download

    query=ApiDownloadDatasetRequest()
    query.owner_slug,query.dataset_slug=ref.split('/')
    query.file_name='payload.bin'
    try:
        with api.build_kaggle_client() as client:
            with client.datasets.dataset_api_client.download_dataset(query) as response:
                url=response.url
        return _download(url,None,limits=limits,expected_sha=sha(bundle),
                         expected_size=bundle.stat().st_size)
    except RemoteError:
        raise
    except Exception:
        raise RemoteError('REMOTE_KAGGLE_DATASET_VERIFY') from None


def publish_private_dataset(api,ref,bundle,directory,*,limits=None):
    bundle=Path(bundle)
    try:existing=api.dataset_status(ref)
    except Exception:existing=None
    if existing is None:
        folder=Path(directory)/'external-dataset'
        if folder.exists():shutil.rmtree(folder)
        folder.mkdir()
        shutil.copyfile(bundle,folder/'payload.bin')
        write_json(folder/'dataset-metadata.json',{'title':ref.split('/',1)[1],'id':ref,
            'licenses':[{'name':'other'}],'isPrivate':True,
            'description':'Private execution payload. Each included file retains its existing ownership and license. No public redistribution rights are granted by this transport metadata.'})
        with _dataset_phase('REMOTE_KAGGLE_DATASET_CREATE'):
            created=api.dataset_create_new(str(folder),public=False,quiet=True,convert_to_csv=False)
            if getattr(created,'error',None):raise RemoteError('REMOTE_KAGGLE_DATASET_CREATE')
    elif limits is None:
        listed=api.dataset_list_files(ref);rows=list(getattr(listed,'files',None) or [])
        if (len(rows)!=1 or getattr(rows[0],'name',None)!='payload.bin'
                or int(getattr(rows[0],'size',-1))!=bundle.stat().st_size):
            raise RemoteError('REMOTE_KAGGLE_DATASET')
    with _dataset_phase('REMOTE_KAGGLE_DATASET_READY'):
        deadline=time.monotonic()+90
        while True:
            state=str(api.dataset_status(ref)).lower()
            if state=='ready':break
            if state in {'error','failed'} or time.monotonic()>=deadline:
                raise RemoteError('REMOTE_KAGGLE_DATASET_READY')
            time.sleep(3)
    meta_dir=Path(directory)/'external-metadata';meta_dir.mkdir(exist_ok=True)
    with _dataset_phase('REMOTE_KAGGLE_DATASET_VERIFY'):
        api.dataset_metadata(ref,str(meta_dir))
    if _private_flag(read_json(meta_dir/'dataset-metadata.json')) is not True:raise RemoteError('REMOTE_KAGGLE_POLICY')
    if limits is not None:
        with _dataset_phase('REMOTE_KAGGLE_DATASET_VERIFY'):
            listed=api.dataset_list_files(ref);rows=list(getattr(listed,'files',None) or [])
        if len(rows)!=1 or getattr(rows[0],'name',None)!='payload.bin':
            raise RemoteError('REMOTE_KAGGLE_DATASET')
        size=getattr(rows[0],'size',-1)
        if type(size) is not int or size not in (-1,bundle.stat().st_size):
            raise RemoteError('REMOTE_KAGGLE_DATASET')
        identity=verify_dataset_file(api,ref,bundle,limits)
        write_json(Path(directory)/'dataset-verification.json',{
            'reported_size':None if size==-1 else size,'identity':identity,
            'private':True,'reused':existing is not None})

def expected_notebook(request,directory):
    directory=Path(directory);bundle=directory/'native-input.zip'
    if not (directory/'external-payload.json').exists():return notebook(request,bundle)
    slug=_payload_slug(request)
    if read_json(directory/'external-payload.json')!={'slug':slug,'sha256':sha(bundle),'filename':'payload.bin'}:
        raise RemoteError('REMOTE_KAGGLE_POLICY')
    return external_notebook(request,sha(bundle),slug)

def export_outputs(request,store,target):
    paths=[]
    prefix=request['store_binding']['output_prefix']+request['request_digest']+'/'
    for meta in (store.root/'references').glob('*.json'):
        ref=read_json(meta)
        if not ref['object_key'].startswith(prefix):continue
        path=store._path(ref)
        if sha(path)!=ref['sha256']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
        paths.extend([meta.relative_to(store.root).as_posix(),path.relative_to(store.root).as_posix()])
    if not paths:raise RemoteError('REMOTE_OUTPUTS')
    if 'limits' in request:streaming.pack_archive(store.root,paths,target,request['limits'])
    else:pack(store.root,paths,target)

def import_outputs(request,store,bundle):
    from fsf.remote.v2 import validate_result
    with tempfile.TemporaryDirectory(prefix='.native-output-',dir=store.root) as temp:
        root=Path(temp)
        if 'limits' in request:
            root=root/'unpacked'
            streaming.extract_archive(bundle,root,request['limits'],None)
            names={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        else:names=unpack(bundle,temp)
        staged=NativeStore({**store.profile,'root':str(root)})
        prefix=request['store_binding']['output_prefix']+request['request_digest']+'/'
        refs=[];expected=set()
        for meta in (root/'references').glob('*.json'):
            ref=read_json(meta)
            if not ref['object_key'].startswith(prefix) or meta.name!=digest(ref['object_key'])+'.json':
                raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
            source=staged._path(ref)
            if not source.is_file() or source.stat().st_size!=ref['size_bytes'] or sha(source)!=ref['sha256']:
                raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
            refs.append(ref);expected.update([meta.relative_to(root).as_posix(),source.relative_to(root).as_posix()])
        if names!=expected:raise RemoteError('REMOTE_ARCHIVE')
        result_ref=next((r for r in refs if r['object_key']==prefix+'result.json'),None)
        if not result_ref:raise RemoteError('REMOTE_OUTPUTS')
        result=validate_result(read_json(staged._path(result_ref)),request,staged)
        if {r['object_key'] for r in refs}!={result_ref['object_key'],*(a['reference']['object_key'] for a in result['artifacts'])}:
            raise RemoteError('REMOTE_OUTPUTS')
        for ref in refs:
            actual=store.put(staged._path(ref),artifact_id=ref['artifact_id'],key=ref['object_key'],role=ref['role'],media_type=ref['media_type'],state_root=store.root)
            if actual!=ref:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
        return result

def call(value):
    if value.get("operation") == "submit":
        from fsf.remote.dispatch_hold import refuse_held_submit
        refuse_held_submit(value["directory"])
    if value["request"].get("schema_version")=="fsf_remote_request_v5":
        from fsf.remote.v2 import check_authorization
        check_authorization(value["request"],value["profile"],NativeStore(read_json(value["store_config"])),
                            new_submission=value["operation"]=="submit")
    from kaggle.api.kaggle_api_extended import KaggleApi
    from fsf.remote.kaggle_v2_api import _saved,_normalized_notebook,_version_status
    from fsf.remote.v2_transports import metadata
    import os
    api=KaggleApi();api.authenticate()
    request=value['request'];profile=value['profile'];directory=Path(value['directory'])
    owner=os.environ.get(profile['settings'].get('owner_env','KAGGLE_USERNAME')) or api.config_values.get('username')
    if not owner:raise RemoteError('REMOTE_KAGGLE_OWNER')
    # Unique immutable notebook per intent; no inherited Secret bindings needed.
    settings=profile['settings']
    from fsf.remote.destination import authorize_create,resolve
    authorize_create(profile,owner=owner)
    if settings.get('activation_mode','api_push')!='api_push':raise RemoteError('REMOTE_KAGGLE_ACTIVATION_MODE')
    fixed=resolve(profile)['slug']
    projected={**profile,'settings':{k:v for k,v in settings.items() if k not in {'worker_notebook','activation_mode','native_notebook'}}}
    if fixed:projected['settings']['worker_notebook']=fixed
    expected=metadata(request,projected,owner);ref=expected['id'];operation=value['operation']
    activation_path=directory/'native-activation.json'
    if operation=='prepare' and fixed:
        baseline=_saved(api,ref);number=baseline.metadata.current_version_number
        raw=str(getattr(_version_status(api,ref,number).status,'name','')).lower()
        if not baseline.metadata.is_private:raise RemoteError('REMOTE_KAGGLE_POLICY')
        if raw in {'queued','running'}:raise RemoteError('REMOTE_KAGGLE_SLOT_BUSY')
        activation=seal({'request_digest':request['request_digest'],'profile_digest':digest(profile),
            'previous_version':number,'kernel_id':baseline.metadata.id},'activation_digest')
        if activation_path.exists() and read_json(activation_path)!=activation:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        write_json(activation_path,activation)
    previous=0
    if fixed:
        activation=read_json(activation_path)
        if (seal(activation,'activation_digest')['activation_digest']!=activation.get('activation_digest')
                or activation['request_digest']!=request['request_digest'] or activation['profile_digest']!=digest(profile)):
            raise RemoteError('REMOTE_KAGGLE_POLICY')
        previous=activation['previous_version'];expected['id_no']=activation['kernel_id']
    if operation=='prepare':
        store=NativeStore(read_json(value['store_config']));stage_notebook(request,store,directory)
        source=dataset_source(request,owner,directory)
        if source:
            publish_private_dataset(api,source,directory/'native-input.zip',directory,limits=request.get('limits'))
            expected['dataset_sources']=[source]
        write_json(directory/'kernel-metadata.json',expected)
        return {'prepared':True,'gpu':request['policy']['gpu'],'verified_execution':False,'data_plane':'kaggle_native_v1'}
    if operation=='submit':
        source=dataset_source(request,owner,directory)
        if source:expected['dataset_sources']=[source]
        if 'limits' in request:
            proof=read_json(directory/'dataset-verification.json')
            bundle=directory/'native-input.zip'
            if proof.get('private') is not True or proof.get('identity')!={'size_bytes':bundle.stat().st_size,'sha256':sha(bundle)}:
                raise RemoteError('REMOTE_KAGGLE_DATASET_VERIFY')
        if read_json(directory/'kernel-metadata.json')!=expected or read_json(directory/'worker.ipynb')!=expected_notebook(request,directory):
            raise RemoteError('REMOTE_KAGGLE_POLICY')
        if fixed:
            observed=_saved(api,ref).metadata
            if observed.current_version_number!=previous or observed.id!=activation['kernel_id']:
                raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        # GPU provider timeout overrides can leave an otherwise runnable saved
        # notebook queued. The embedded worker retains its shared hard budget;
        # do not confuse that execution budget with provider session admission.
        kwargs={'acc':None}
        if not request['policy']['gpu']:kwargs['timeout']=str(request['policy']['deadline_seconds'])
        reply=api.kernels_push(str(directory),**kwargs)
        if reply.error or reply.version_number!=previous+1:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        return {'kernel':ref,'version':reply.version_number,'request_digest':request['request_digest'],'data_plane':'kaggle_native_v1'}
    try:saved=_saved(api,ref)
    except Exception:
        if operation=='reconcile':return None
        raise RemoteError('REMOTE_KAGGLE_STATUS') from None
    target_version=previous+1
    if operation=='reconcile' and fixed and saved.metadata.current_version_number==previous:return None
    if (saved.metadata.current_version_number!=target_version or not saved.metadata.is_private
            or (fixed and saved.metadata.id!=activation['kernel_id'])
            or _normalized_notebook(saved.blob.source)!=_normalized_notebook(expected_notebook(request,directory))):
        raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
    if operation=='reconcile':return {'kernel':ref,'version':target_version,'request_digest':request['request_digest'],'data_plane':'kaggle_native_v1'}
    remote=value.get('remote') or {}
    if remote.get('kernel')!=ref or remote.get('version')!=target_version:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
    status=_version_status(api,ref,target_version)
    raw=str(getattr(status.status,'name',status.status)).lower().rsplit('.',1)[-1].replace('_','')
    mapped={'complete':'completed','completed':'completed','running':'running','queued':'queued','error':'failed','failed':'failed','cancelacknowledged':'cancelled'}.get(raw,'unknown')
    if operation=='status':return {'status':mapped}
    if operation=='fetch':
        if mapped not in {'completed','failed','cancelled'}:raise RemoteError('REMOTE_NOT_READY')
        from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
        from fsf.remote.kaggle_api import _download
        token=None;matches=[];slug=ref.split('/')[1]
        for _ in range(4):
            query=ApiListKernelSessionOutputRequest();query.user_name=owner;query.kernel_slug=slug
            api._set_paging(query,20,token)
            with api.build_kaggle_client() as client:reply=client.kernels.kernels_api_client.list_kernel_session_output(query)
            matches.extend(item for item in reply.files or [] if item.file_name=='fsf-native-result.zip')
            token=reply.next_page_token
            if not token:break
        if token or len(matches)!=1:raise RemoteError('REMOTE_KAGGLE_OUTPUT_NOT_READY')
        target=directory/'native-result.zip';target.unlink(missing_ok=True)
        if 'limits' in request:_download(matches[0].url,target,limits=request['limits'])
        else:_download(matches[0].url,target)
        # Bracket native latest-output endpoint with immutable version checks.
        if _saved(api,ref).metadata.current_version_number!=target_version:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        store=NativeStore(read_json(value['store_config']))
        result=import_outputs(request,store,target)
        return {'downloaded':True,'result_digest':result['result_digest']}
    raise RemoteError('REMOTE_ACTION')

def main():
    value=read_json(sys.argv[1]);directory=Path(value['directory'])
    try:
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):result=call(value)
        reply={'ok':True,'result':result}
    except Exception as exc:reply={'ok':False,'code':exc.code if isinstance(exc,RemoteError) else 'REMOTE_KAGGLE_API_FAILED'}
    write_json(directory/'provider-v2-response.json',reply)
    return 0 if reply['ok'] else 2

if __name__=='__main__':raise SystemExit(main())
