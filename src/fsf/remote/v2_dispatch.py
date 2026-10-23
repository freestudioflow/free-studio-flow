"""Durable v2 submission, reconciliation and parent-owned acceptance."""
from pathlib import Path
import time
from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock,read_json
from fsf.remote.contracts import RemoteError,digest,seal,write_json
from fsf.remote.v2 import check_authorization,collect_result
from fsf.remote.v2_transports import V2Adapter


def _read(path):
    ledger=read_json(path)
    if seal(ledger,'ledger_digest')['ledger_digest']!=ledger.get('ledger_digest'):raise RemoteError('REMOTE_INTEGRITY')
    return ledger


def _save(path,value):
    value=seal(value,'ledger_digest');write_json(path,value);return value


def summary(ledger):
    return {k:ledger[k] for k in ('request_digest','status','provider_status','submitted_count','result_digest')}


def submit(request,*,profile,store,state_root,store_config,adapter=None):
    request=check_authorization(request,profile,store)
    root=_root(state_root);key=request['request_digest'];directory=root/key
    adapter=adapter or V2Adapter(profile,store_config=store_config)
    with edit_lock(root/'.v2-submit'):
        if directory.exists():
            ledger=_read(directory/'ledger.json')
            if ledger['profile_digest']!=digest(profile):raise RemoteError('REMOTE_NOT_AUTHORIZED')
            if ledger['status']!='prepared':return summary(ledger)
        else:
            # A changed attempt cannot bypass an unresolved submission for the
            # same parent step / segment.
            for path in root.glob('*/ledger.json'):
                old=_read(path)
                if old['status'] in {'submission_unknown','awaiting_activation'}:
                    prior=read_json(path.parent/'request.json')
                    if prior['parent']['job_id']==request['parent']['job_id'] and prior['segment']['segment_id']==request['segment']['segment_id']:
                        raise RemoteError('REMOTE_PREVIOUS_SUBMISSION_UNKNOWN')
            if profile['transport']=='kaggle' and request['policy']['gpu']:
                evidence=root/'kaggle-cpu-ready.json'
                if not evidence.exists():raise RemoteError('REMOTE_KAGGLE_SECRET_BINDING_UNVERIFIED')
                smoke=read_json(evidence)
                smoke_dir=root/smoke['request_digest']
                old_request=read_json(smoke_dir/'request.json');old_ledger=_read(smoke_dir/'ledger.json')
                if (old_ledger['status']!='verified' or old_request['policy']['gpu'] or old_request['store_binding']!=request['store_binding']
                        or old_request['profile_id']!=request['profile_id']):raise RemoteError('REMOTE_KAGGLE_SECRET_BINDING_UNVERIFIED')
                collect_result(old_request,store=store,directory=smoke_dir)
            directory.mkdir();write_json(directory/'request.json',request)
            write_json(directory/'profile-snapshot.json',profile)
            ledger={'request_digest':key,'profile_digest':digest(profile),'status':'prepared','provider_status':'unknown',
                    'submitted_count':0,'result_digest':None,'created_at':time.time(),'remote':{},'locator':None}
            _save(directory/'ledger.json',ledger)
        if time.time()>ledger['created_at']+request['policy']['deadline_seconds']:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
        locator=store.put(directory/'request.json',artifact_id='dispatch_request',
            key=request['store_binding']['input_prefix']+key+'/request.json',role='data',media_type='application/json',state_root=directory)
        ledger['locator']=locator;_save(directory/'ledger.json',ledger)
        adapter.prepare(request,locator,directory)
        from fsf.remote.budget import reserve
        reserve(profile,request,owner=root)
        ledger.update(status='submission_unknown',submitted_count=1)
        _save(directory/'ledger.json',ledger)
        try:
            ledger['remote']=adapter.submit(request,locator,directory)
            if ledger['remote'].get('activation_pending') is True:
                ledger['status']='awaiting_activation';ledger['submitted_count']=0
            else:
                ledger['status']='submitted'
        except KeyboardInterrupt:
            _save(directory/'ledger.json',ledger);raise RemoteError('REMOTE_CANCELLED') from None
        except Exception:
            pass
        return summary(_save(directory/'ledger.json',ledger))


def update(key,*,profile,store,state_root,store_config,action='status',adapter=None,recover=False):
    if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise RemoteError('REMOTE_PATH')
    root=_root(state_root);directory=root/key
    adapter=adapter or V2Adapter(profile,store_config=store_config)
    with edit_lock(root/'.v2-submit'):
        request=check_authorization(read_json(directory/'request.json'),profile,store,new_submission=False)
        ledger=_read(directory/'ledger.json')
        if ledger['profile_digest']!=digest(profile) or ledger['request_digest']!=request['request_digest']:raise RemoteError('REMOTE_NOT_AUTHORIZED')
        if (profile['transport']=='kaggle' and (profile['settings'].get('worker_notebook') or profile['settings'].get('native_notebook'))
                and ledger['status'] in {'verified','recovered_verified'} and action!='cancel'):
            # A later saved version in the operator slot cannot become evidence
            # about an older accepted request. Revalidate its immutable result.
            if action=='fetch':
                result=collect_result(request,store=store,directory=directory,recovery=ledger['status']=='recovered_verified')
                if result['result_digest']!=ledger['result_digest']:raise RemoteError('REMOTE_RESULT_CONFLICT')
            return summary(ledger)
        if action=='cancel':
            outcome=adapter.cancel(request,ledger['remote'],directory)
            if outcome not in {'requested','unsupported'}:raise RemoteError('REMOTE_PROVIDER_STATUS')
            ledger['status']='cancel_requested' if outcome=='requested' else 'cancel_unsupported'
            return summary(_save(directory/'ledger.json',ledger))
        if ledger['status'] in {'submission_unknown','awaiting_activation'}:
            observation=adapter.reconcile(request,ledger['locator'],directory)
            if observation:
                ledger['remote']=observation;ledger['status']='submitted';ledger['submitted_count']=1
            elif action=='fetch':raise RemoteError('REMOTE_PREVIOUS_SUBMISSION_UNKNOWN')
            elif ledger['status']=='awaiting_activation':return summary(ledger)
        observed=adapter.status(request,ledger['remote'],directory)
        if observed not in {'completed','failed','cancelled','running','queued','unknown'}:raise RemoteError('REMOTE_PROVIDER_STATUS')
        ledger['provider_status']=observed
        if action=='fetch':
            if observed in {'running','queued','unknown'}:raise RemoteError('REMOTE_NOT_READY')
            recovery=observed in {'failed','cancelled'}
            if recovery and not recover:
                ledger['status']='recovery_review_required';_save(directory/'ledger.json',ledger)
                raise RemoteError('REMOTE_RECOVERY_REVIEW_REQUIRED')
            if hasattr(adapter,'fetch_into_store'):
                adapter.fetch_into_store(request,ledger['remote'],directory)
            result=collect_result(request,store=store,directory=directory,recovery=recovery)
            if ledger['result_digest'] not in (None,result['result_digest']):raise RemoteError('REMOTE_RESULT_CONFLICT')
            ledger.update(status='recovered_verified' if recovery else 'verified',result_digest=result['result_digest'])
            if profile['transport']=='kaggle' and not request['policy']['gpu'] and not recovery:
                write_json(root/'kaggle-cpu-ready.json',{'request_digest':key})
        elif observed in {'failed','cancelled'}:ledger['status']='remote_'+observed
        elif observed=='completed' and ledger['status']!='verified':ledger['status']='remote_completed'
        elif ledger['status'] not in {'verified','recovered_verified','submission_unknown'}:ledger['status']=observed
        return summary(_save(directory/'ledger.json',ledger))
