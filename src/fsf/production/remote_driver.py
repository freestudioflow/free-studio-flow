"""Production parent bridge to existing native remote dispatch and collection."""
from __future__ import annotations

import copy
from pathlib import Path

from fsf.core.asset_library import _root,_no_links
from fsf.core.edit_store import edit_lock,read_json
from fsf.production.contracts import ProductionError,digest,publish,key
from fsf.remote.artifacts import ArtifactStore
from fsf.remote.v2 import prepare_request,collect_result
from fsf.remote.v2_dispatch import submit,update


class NativeRecipeDriver:
    """Operator-supplied templates and verifier, never model-produced host roots.

    A verifier is mandatory and is responsible for current source/review and
    semantic-policy checks. Provider completion never implies content acceptance.
    Its cost must be included in the parent's worst-case reservation.
    """
    def __init__(self,templates,*,profile,store_config,state_root,verifier,adapter=None):
        if not callable(verifier):raise ProductionError("PRODUCTION_EVALUATOR_REQUIRED")
        self.templates=copy.deepcopy(templates)
        self.profile=copy.deepcopy(profile)
        self.config=Path(store_config)
        self.store=ArtifactStore(read_json(self.config))
        self.root=_root(state_root)
        self.dispatches=self.root/'dispatches';self.dispatches.mkdir(exist_ok=True)
        self.verifier=verifier;self.adapter=adapter

    def _binding(self,task):
        value=self.templates.get(task['task_id'])
        if not isinstance(value,dict) or set(value)!={'definition','recipe','inputs','wheel','segment'}:
            raise ProductionError('PRODUCTION_BINDING_INVALID')
        if digest(value)!=task['input_digest']:
            raise ProductionError('PRODUCTION_BINDING_CHANGED')
        return copy.deepcopy(value)

    def _request(self,task,request_id):
        key(request_id)
        value=self._binding(task)
        folder=self.root/request_id;folder.mkdir(exist_ok=True)
        _no_links(folder)
        # Deterministic native run identity makes a lost parent reply reconcilable.
        definition=value['definition']
        definition['parent']['run_id']='run_'+request_id
        # Allocation claims intentionally ignore run_id. Give each new parent
        # attempt a durable, increasing native attempt rather than colliding
        # with the first claim after a quality rejection.
        slots=self.root/'native-attempts.json'
        namespace=digest({'job':definition['parent']['job_id'],'step':definition['parent']['step_id'],
                          'segment':value['segment']})
        with edit_lock(slots):
            table=read_json(slots) if slots.exists() else {}
            group=table.setdefault(namespace,{'binding_digest':task['input_digest'],'requests':{}})
            if group['binding_digest']!=task['input_digest']:
                raise ProductionError('PRODUCTION_BINDING_CHANGED')
            if request_id not in group['requests']:
                group['requests'][request_id]=definition['parent']['attempt_id']+len(group['requests'])
                publish(slots,table)
            definition['parent']['attempt_id']=group['requests'][request_id]
        request=prepare_request(definition,value['recipe'],profile=self.profile,store=self.store,
            inputs=value['inputs'],wheel=value['wheel'],segment=value['segment'],output=folder)
        path=folder/'request.json'
        with edit_lock(path):
            if path.exists():
                if read_json(path)!=request:raise ProductionError('PRODUCTION_BINDING_CHANGED')
            else:publish(path,request)
        return request

    def submit(self,task,request_id):
        request=self._request(task,request_id)
        return submit(request,profile=self.profile,store=self.store,state_root=self.dispatches,
                      store_config=self.config,adapter=self.adapter)

    def observe(self,task,request_id):
        request=self._request(task,request_id)
        folder=self.dispatches/request['request_digest']
        if not (folder/'ledger.json').exists():
            # No remote intent exists. The parent's reserved slot can be retried
            # after recording this conclusive local observation as failed.
            return {'status':'failed','settled_units':0}
        result=update(request['request_digest'],profile=self.profile,store=self.store,
            state_root=self.dispatches,store_config=self.config,adapter=self.adapter)
        mapping={'verified':'produced','remote_completed':'produced','submitted':'running',
                 'submission_unknown':'unknown','awaiting_activation':'unknown','running':'running',
                 'queued':'running','remote_failed':'failed','remote_cancelled':'cancelled'}
        # No provider fee is inferred from status; reservation remains until
        # separately reconciled. Native quota limits still apply independently.
        return {'status':mapping.get(result['status'],'unknown')}

    def verify(self,task,request_id):
        request=self._request(task,request_id)
        folder=self.dispatches/request['request_digest']
        if not (folder/'accepted').exists():
            update(request['request_digest'],profile=self.profile,store=self.store,state_root=self.dispatches,
                   store_config=self.config,action='fetch',adapter=self.adapter)
        # Recollect from immutable references even for reused results. This
        # checks current bytes and evidence without starting another provider job.
        result=collect_result(request,store=self.store,directory=folder)
        return self.verifier(task,request,result,folder/'accepted')
