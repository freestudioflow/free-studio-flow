"""Operator command lifecycle for a durable production parent, not a sandbox."""
from pathlib import Path
import copy
import re
from fsf.core.asset_library import _root,_no_links
from fsf.core.edit_store import read_json,edit_lock
from fsf.remote.command_adapter import CommandAdapter,validate_settings
from fsf.remote.contracts import safe_path,reject_inline_credentials
from fsf.production.contracts import ProductionError,publish,digest,key
from fsf.production.control import file_identity
from fsf.production.quality import content_verdict


class CommandDriver:
    """submit creates once; reconcile/status/fetch must never create paid work.

    Provider, loader and evaluator implementations are installed/trusted by the
    operator. The task cannot select an executable or an arbitrary output root.
    """
    def __init__(self,config,*,plan,state_root):
        config=copy.deepcopy(config);plan=copy.deepcopy(plan)
        reject_inline_credentials(config)
        if not isinstance(config,dict) or set(config)!={'schema_version','settings','artifact_root','evaluator_digest'} or config['schema_version']!='fsf_production_command_driver_v1':
            raise ProductionError('PRODUCTION_DRIVER_INVALID')
        validate_settings(config['settings'])
        if not isinstance(config['evaluator_digest'],str) or not re.fullmatch('[0-9a-f]{64}',config['evaluator_digest']):
            raise ProductionError('PRODUCTION_DRIVER_INVALID')
        self.root=_root(state_root);self.artifacts=_root(config['artifact_root'])
        self.plan=plan;self.config=config
        self.adapter=CommandAdapter({'settings':config['settings']},self.root/'no-store-config.json')
        self.plan_digest=plan['plan_digest'];self.policy_digest=plan['policy_digest']
        self.tasks={t['task_id']:copy.deepcopy(t) for t in plan['tasks']}
        # Persist the operator implementation before even a reconcile call.
        # A crash before provider.json must not allow another executable to
        # reinterpret the same durable submission intent.
        binding=self.root/'driver-binding.json'
        snapshot={'driver_digest':digest(config),'plan_digest':self.plan_digest,
                  'policy_digest':self.policy_digest}
        with edit_lock(binding):
            if binding.exists():
                if read_json(binding)!=snapshot:raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            else:publish(binding,snapshot)

    def _folder(self,request_id):
        key(request_id)
        folder=self.root/request_id;folder.mkdir(exist_ok=True);_no_links(folder)
        return folder

    def _call(self,operation,task,request_id,remote=None):
        if not isinstance(task,dict) or task!=self.tasks.get(task.get('task_id')):
            raise ProductionError('PRODUCTION_BINDING_CHANGED')
        request={'request_digest':request_id,'production_plan_digest':self.plan_digest,
                 'policy_digest':self.policy_digest,'task':task}
        return self.adapter.call(operation,request,{'artifact_root':str(self.artifacts)},self._folder(request_id),remote)

    def _save_identity(self,request_id,identity):
        if not isinstance(identity,dict) or not identity:raise ProductionError('PRODUCTION_PROVIDER_INVALID')
        path=self._folder(request_id)/'provider.json'
        value={'request_id':request_id,'driver_digest':digest(self.config),'remote':identity}
        with edit_lock(path):
            if path.exists():
                if read_json(path)!=value:raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            else:publish(path,value)
        return identity

    def _identity(self,task,request_id):
        path=self._folder(request_id)/'provider.json'
        if path.exists():
            value=read_json(path)
            if not isinstance(value,dict) or set(value)!={'request_id','driver_digest','remote'} or value.get('request_id')!=request_id or value.get('driver_digest')!=digest(self.config) or not isinstance(value['remote'],dict) or not value['remote']:
                raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            return value['remote']
        found=self._call('reconcile',task,request_id)
        return self._save_identity(request_id,found) if found is not None else None

    def submit(self,task,request_id):
        folder=self._folder(request_id)
        # The durable parent writes its unknown/submitted intent before this
        # call, so a lost process reply is reconciled rather than submitted again.
        if (folder/'provider.json').exists():return self._identity(task,request_id)
        return self._save_identity(request_id,self._call('submit',task,request_id))

    def observe(self,task,request_id):
        identity=self._identity(task,request_id)
        if identity is None:return {'status':'unknown'}
        status=self._call('status',task,request_id,identity)
        return {'status':{'completed':'produced','queued':'running'}.get(status,status)}

    def verify(self,task,request_id):
        identity=self._identity(task,request_id)
        if identity is None:raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
        reply=self._call('fetch',task,request_id,identity)
        if set(reply)!={'artifact','report'} or not isinstance(reply['artifact'],str) or not isinstance(reply['report'],dict):
            raise ProductionError('PRODUCTION_EVALUATOR_INVALID')
        path=safe_path(self.artifacts,request_id+'/'+reply['artifact'])
        observed=file_identity(path);report=reply['report'];samples=report.get('sample_hashes')
        if not isinstance(samples,list) or not 1<=len(samples)<=4096 or any(not isinstance(s,str) or not re.fullmatch('[0-9a-f]{64}',s) for s in samples):
            raise ProductionError('PRODUCTION_EVALUATOR_INVALID')
        evidence=content_verdict(report,artifact_sha256=observed['sha256'],evaluator_digest=self.config['evaluator_digest'],
                                 policy_digest=self.policy_digest,sample_hashes=samples)
        return path,evidence
