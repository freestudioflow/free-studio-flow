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
from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,digest,sha,write_json,safe_path,seal
from fsf.remote.bundle import pack,unpack
from fsf.remote.native_store import NativeStore,SCHEMA

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
    pack(store.root,paths,target)

def import_outputs(request,store,bundle):
    from fsf.remote.v2 import validate_result
    with tempfile.TemporaryDirectory(prefix='.native-output-',dir=store.root) as temp:
        names=unpack(bundle,temp);root=Path(temp)
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
    if settings.get('activation_mode','api_push')!='api_push':raise RemoteError('REMOTE_KAGGLE_ACTIVATION_MODE')
    fixed=settings.get('native_notebook')
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
        store=NativeStore(read_json(value['store_config']));prepare(request,store,directory)
        write_json(directory/'kernel-metadata.json',expected)
        return {'prepared':True,'gpu':request['policy']['gpu'],'verified_execution':False,'data_plane':'kaggle_native_v1'}
    if operation=='submit':
        if read_json(directory/'kernel-metadata.json')!=expected or read_json(directory/'worker.ipynb')!=notebook(request,directory/'native-input.zip'):
            raise RemoteError('REMOTE_KAGGLE_POLICY')
        if fixed and _saved(api,ref).metadata.current_version_number!=previous:
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
            or _normalized_notebook(saved.blob.source)!=_normalized_notebook(notebook(request,directory/'native-input.zip'))):
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
        target=directory/'native-result.zip';target.unlink(missing_ok=True);_download(matches[0].url,target)
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
