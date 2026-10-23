"""Operator-owned allocation ledger; a new client state does not reset cost."""
from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock,read_json
from fsf.remote.contracts import RemoteError,digest,seal,write_json


def reserve(profile,request,*,owner):
    allocation=profile['allocation'];root=_root(allocation['state_root'])
    path=root/(allocation['allocation_id']+'.json')
    authority=digest({'allocation':{k:v for k,v in allocation.items() if k!='state_root'},
        'policy':profile['policy'],'gpu_limits':profile['gpu_limits'],'group_limits':profile['group_limits'],'store_binding':profile['store_binding']})
    with edit_lock(root/'.allocation-target'):
        value=read_json(path) if path.exists() else {'authority':authority,'requests':{},'claims':{}}
        if value['authority']!=authority or (path.exists() and seal(value,'budget_digest')['budget_digest']!=value.get('budget_digest')):
            raise RemoteError('REMOTE_ALLOCATION_CHANGED')
        key=request['request_digest'];seconds=request['policy']['deadline_seconds']
        owner=digest(str(owner))
        claim=digest({'job_id':request['parent']['job_id'],'step_id':request['parent']['step_id'],
            'attempt_id':request['parent']['attempt_id'],'segment':request['segment']})
        if claim in value['claims'] and value['claims'][claim]!=key:raise RemoteError('REMOTE_INTENT_CONFLICT')
        if key in value['requests']:
            if value['requests'][key]!={'seconds':seconds,'owner':owner}:raise RemoteError('REMOTE_INTENT_CONFLICT')
            return
        if len(value['requests'])>=allocation['max_submissions'] or sum(item['seconds'] for item in value['requests'].values())+seconds>allocation['max_execution_seconds']:
            raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
        value['requests'][key]={'seconds':seconds,'owner':owner};value['claims'][claim]=key
        write_json(path,seal(value,'budget_digest'))
