"""Pinned Kaggle SDK gateway for the additive v2 control plane."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError,digest,seal,write_json
from fsf.remote.v2_transports import notebook,metadata


def _normalized_notebook(value):
    value=json.loads(value) if isinstance(value,str) else value
    for cell in value.get('cells',[]):
        if isinstance(cell.get('source'),list):cell['source']=''.join(cell['source'])
        if cell.get('cell_type')=='code':cell['outputs']=[];cell['execution_count']=None
        cell.pop('id',None)
    return {'nbformat':value.get('nbformat'),'cells':value.get('cells')}


def _saved(api,ref,version=None):
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
    owner,slug=ref.split('/')
    request=ApiGetKernelRequest();request.user_name=owner
    request.kernel_slug=slug+('/'+str(version) if version is not None else '')
    with api.build_kaggle_client() as client:
        return client.kernels.kernels_api_client.get_kernel(request)


def _version_status(api,ref,version):
    # The deployed public endpoint rejects version_label. Never misattribute
    # its latest-run status: bracket the call with exact current-version checks.
    before=_saved(api,ref)
    if before.metadata.current_version_number!=version:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
    reply=api.kernels_status(ref)
    after=_saved(api,ref)
    if after.metadata.current_version_number!=version:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
    return reply


def _browser_activation(request,profile,locator,version):
    if type(version) is not int or version<1:raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
    return seal({'operation':'activate_existing_private_notebook','request_digest':request['request_digest'],
        'profile_digest':digest(profile),'worker_notebook':profile['settings']['worker_notebook'],
        'previous_version':version,'notebook_digest':digest(_normalized_notebook(notebook(request,locator))),
        'gpu':request['policy']['gpu'],'deadline_seconds':request['policy']['deadline_seconds'],
        'requires_browser_agent':True,'human_reauthorization_required':False},'activation_digest')


def _read_browser_activation(directory,request,profile,locator):
    value=read_json(directory/'browser-activation.json')
    if value!=_browser_activation(request,profile,locator,value.get('previous_version')):
        raise RemoteError('REMOTE_KAGGLE_POLICY')
    return value


def call(value):
    if value.get("operation") == "submit":
        from fsf.remote.dispatch_hold import refuse_held_submit
        refuse_held_submit(value["directory"])
    import importlib.metadata
    if importlib.metadata.version('kaggle')!='2.2.4':raise RemoteError('REMOTE_KAGGLE_SDK_VERSION')
    from kaggle.api.kaggle_api_extended import KaggleApi
    api=KaggleApi();api.authenticate()
    request=value['request'];profile=value['profile'];directory=Path(value['directory'])
    owner=os.environ.get(profile['settings'].get('owner_env','KAGGLE_USERNAME')) or api.config_values.get('username')
    if not owner or not re.fullmatch('[A-Za-z0-9_-]+',owner):raise RemoteError('REMOTE_KAGGLE_OWNER')
    operation=value['operation'];expected=metadata(request,profile,owner)
    fixed_slot=bool(profile['settings'].get('worker_notebook'))
    browser_activation=profile['settings'].get('activation_mode')=='agent_browser'
    if browser_activation and not fixed_slot:raise RemoteError('REMOTE_KAGGLE_WORKER_NOTEBOOK')
    if operation=='prepare':
        if fixed_slot:
            previous=_saved(api,expected['id'])
            if not previous.metadata.is_private:raise RemoteError('REMOTE_KAGGLE_POLICY')
            current=_version_status(api,expected['id'],previous.metadata.current_version_number)
            raw=str(getattr(current.status,'name',current.status)).lower().rsplit('.',1)[-1]
            if raw in {'running','queued'}:raise RemoteError('REMOTE_KAGGLE_SLOT_BUSY')
        write_json(directory/'worker.ipynb',notebook(request,value['locator']))
        write_json(directory/'kernel-metadata.json',expected)
        if browser_activation:
            write_json(directory/'browser-activation.json',_browser_activation(request,profile,value['locator'],previous.metadata.current_version_number))
        return {'prepared':True,'gpu':request['policy']['gpu'],'verified_execution':False}
    if operation=='submit':
        if read_json(directory/'kernel-metadata.json')!=expected or read_json(directory/'worker.ipynb')!=notebook(request,value['locator']):
            raise RemoteError('REMOTE_KAGGLE_POLICY')
        if browser_activation:
            saved=_saved(api,expected['id'])
            activation=_read_browser_activation(directory,request,profile,value['locator'])
            if not saved.metadata.is_private or saved.metadata.current_version_number!=activation['previous_version']:
                raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
            return {'activation_pending':True,'request_digest':request['request_digest']}
        if fixed_slot:
            previous=_saved(api,expected['id'])
            if not previous.metadata.is_private:raise RemoteError('REMOTE_KAGGLE_POLICY')
            current=_version_status(api,expected['id'],previous.metadata.current_version_number)
            raw=str(getattr(current.status,'name',current.status)).lower().rsplit('.',1)[-1]
            if raw in {'running','queued'}:raise RemoteError('REMOTE_KAGGLE_SLOT_BUSY')
        # acc must stay None: a caller override cannot widen sealed metadata.
        kwargs={'acc':None}
        if not request['policy']['gpu']:kwargs['timeout']=str(request['policy']['deadline_seconds'])
        reply=api.kernels_push(str(directory),**kwargs)
        if reply.error or type(reply.version_number) is not int or reply.version_number<1 or (not fixed_slot and reply.version_number!=1):raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        return {'kernel':expected['id'],'version':reply.version_number,'request_digest':request['request_digest']}
    if fixed_slot:
        if operation=='reconcile':
            saved=_saved(api,expected['id'])
            locator=read_json(directory/'ledger.json')['locator']
            if browser_activation and saved.metadata.current_version_number<=_read_browser_activation(directory,request,profile,locator)['previous_version']:
                return None
            if not saved.metadata.is_private or _normalized_notebook(saved.blob.source)!=_normalized_notebook(notebook(request,locator)):
                return None
            return {'kernel':expected['id'],'version':saved.metadata.current_version_number,'request_digest':request['request_digest']}
        remote=value.get('remote') or {}
        if remote.get('kernel')!=expected['id'] or type(remote.get('version')) is not int or remote['version']<1:
            raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        saved=_saved(api,expected['id'])
        if saved.metadata.current_version_number!=remote['version']:
            return {'status':'unknown'}
        locator=read_json(directory/'ledger.json')['locator']
        if not saved.metadata.is_private or _normalized_notebook(saved.blob.source)!=_normalized_notebook(notebook(request,locator)):
            raise RemoteError('REMOTE_KAGGLE_VERSION_AMBIGUOUS')
        reply=_version_status(api,expected['id'],remote['version'])
    else:
        try:reply=api.kernels_status(expected['id'])
        except Exception:
            if operation=='reconcile':return None
            raise RemoteError('REMOTE_KAGGLE_STATUS') from None
    if operation=='reconcile':return {'kernel':expected['id'],'request_digest':request['request_digest'],'version_proof':'unique_request_slug'}
    raw=str(getattr(reply.status,'name',reply.status)).lower().rsplit('.',1)[-1].replace('_','')
    status={'complete':'completed','completed':'completed','running':'running','queued':'queued',
            'error':'failed','failed':'failed','cancelacknowledged':'cancelled','canceled':'cancelled','cancelled':'cancelled'}.get(raw,'unknown')
    return {'status':status}


def main():
    value=read_json(sys.argv[1]);directory=Path(value['directory'])
    try:
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):result=call(value)
        reply={'ok':True,'result':result}
    except Exception as exc:reply={'ok':False,'code':exc.code if isinstance(exc,RemoteError) else 'REMOTE_KAGGLE_API_FAILED'}
    write_json(directory/'provider-v2-response.json',reply)
    return 0 if reply['ok'] else 2


if __name__=='__main__':raise SystemExit(main())
