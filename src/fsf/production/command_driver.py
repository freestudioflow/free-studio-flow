"""Operator command lifecycle for a durable production parent, not a sandbox."""
from pathlib import Path
import copy
import re
from contextlib import contextmanager
from time import monotonic as _processing_time
from fsf.core.asset_library import _root,_no_links,_path_parts
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
        base_fields = {'schema_version','settings','artifact_root','evaluator_digest'}
        version = config.get('schema_version') if isinstance(config,dict) else None
        expected = base_fields | ({'settlement'} if version == 'fsf_production_command_driver_v2' else set())
        if not isinstance(config,dict) or set(config)!=expected or version not in {'fsf_production_command_driver_v1','fsf_production_command_driver_v2'}:
            raise ProductionError('PRODUCTION_DRIVER_INVALID')
        if version == 'fsf_production_command_driver_v2':
            units = config['settlement']
            if (not isinstance(units,dict) or set(units)!={'operation','currency','unit_scale'}
                    or units['operation']!='settlement' or not isinstance(units['currency'],str)
                    or not re.fullmatch('[A-Z]{3}',units['currency'])
                    or type(units['unit_scale']) is not int or not 1<=units['unit_scale']<=10**9):
                raise ProductionError('PRODUCTION_DRIVER_INVALID')
        validate_settings(config['settings'])
        if not isinstance(config['evaluator_digest'],str) or not re.fullmatch('[0-9a-f]{64}',config['evaluator_digest']):
            raise ProductionError('PRODUCTION_DRIVER_INVALID')
        self.root=_root(state_root);self.artifacts=_root(config['artifact_root'])
        self.plan=plan;self.config=config
        self.adapter=CommandAdapter({'settings':config['settings']},self.root/'no-store-config.json')
        self.plan_digest=plan['plan_digest'];self.policy_digest=plan['policy_digest']
        self.tasks={t['task_id']:copy.deepcopy(t) for t in plan['tasks']}
        self._execution_pins = None
        self._identity_cache = {}
        self._pin_config_digest = None
        self.last_file_check_mode = 'fresh_per_call_hash'
        self._execution_deadline = None
        self._cooperative_budget = False
        self._submission_admitted = False
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

    @contextmanager
    def bounded_execution(self, deadline, *, cooperative=False):
        """Share the parent processing budget without changing authorized settings."""
        previous = self._execution_deadline,self._cooperative_budget
        self._execution_deadline = deadline
        self._cooperative_budget = cooperative
        try:
            yield
        finally:
            self._execution_deadline,self._cooperative_budget = previous

    def cooperative_execution(self, deadline):
        """Stop admitting calls at the boundary; bound admitted calls by settings."""
        return self.bounded_execution(deadline,cooperative=True)

    @contextmanager
    def admit_submission(self):
        """Admit before durable intent, so commit latency cannot kill a new send."""
        admitted=self._execution_deadline is None or _processing_time()<self._execution_deadline
        previous=self._submission_admitted
        self._submission_admitted=admitted and self._cooperative_budget
        try:
            yield admitted
        finally:
            self._submission_admitted=previous

    @contextmanager
    def execution_files(self):
        """Release every lease on normal exit, cancellation, or lost provider reply."""
        with self.adapter.pinned_files() as pins:
            self._execution_pins = pins
            self._pin_config_digest = digest(self.config)
            self.last_file_check_mode = 'win32_locked_read_set' if pins is not None else 'fresh_per_call_hash'
            try:
                yield
            finally:
                self._identity_cache.clear()
                self._execution_pins = None
                self._pin_config_digest = None

    def _cache_identity(self, request_id, value):
        if self._execution_pins is not None:
            try:
                observed = self._execution_pins.json(self.root/request_id/'provider.json')
            except OSError:
                return
            if observed != value:
                raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            self._identity_cache[request_id] = copy.deepcopy(value['remote'])

    def _folder(self,request_id):
        key(request_id)
        if self._execution_pins is not None and request_id in self._identity_cache:
            return self.root/request_id
        folder=self.root/request_id;folder.mkdir(exist_ok=True);_no_links(folder)
        return folder

    def _call(self,operation,task,request_id,remote=None):
        if self._pin_config_digest is not None and digest(self.config)!=self._pin_config_digest:
            raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
        if not isinstance(task,dict) or task!=self.tasks.get(task.get('task_id')):
            raise ProductionError('PRODUCTION_BINDING_CHANGED')
        request={'request_digest':request_id,'production_plan_digest':self.plan_digest,
                 'policy_digest':self.policy_digest,'task':task}
        timeout = None
        if self._execution_deadline is not None:
            timeout = self._execution_deadline - _processing_time()
            if timeout <= 0 and not (self._cooperative_budget and self._submission_admitted and operation=='submit'):
                raise ProductionError('PRODUCTION_DEADLINE_EXCEEDED')
            if self._cooperative_budget:
                timeout=None
        return self.adapter.call(operation,request,{'artifact_root':str(self.artifacts)},self._folder(request_id),remote,
                                 timeout_seconds=timeout)

    def _save_identity(self,request_id,identity):
        if not isinstance(identity,dict) or not identity:raise ProductionError('PRODUCTION_PROVIDER_INVALID')
        path=self._folder(request_id)/'provider.json'
        value={'request_id':request_id,'driver_digest':digest(self.config),'remote':identity}
        with edit_lock(path):
            if path.exists():
                if read_json(path)!=value:raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            else:publish(path,value)
        self._cache_identity(request_id,value)
        return identity

    def _identity(self,task,request_id):
        if self._pin_config_digest is not None and digest(self.config)!=self._pin_config_digest:
            raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
        if self._execution_pins is not None and request_id in self._identity_cache:
            return copy.deepcopy(self._identity_cache[request_id])
        path=self._folder(request_id)/'provider.json'
        if path.exists():
            value=read_json(path)
            if not isinstance(value,dict) or set(value)!={'request_id','driver_digest','remote'} or value.get('request_id')!=request_id or value.get('driver_digest')!=digest(self.config) or not isinstance(value['remote'],dict) or not value['remote']:
                raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            self._cache_identity(request_id,value)
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
        result = {'status':{'completed':'produced','queued':'running'}.get(status,status)}
        if self.config['schema_version']=='fsf_production_command_driver_v2' and result['status'] in {'produced','failed','cancelled'}:
            if self._cooperative_budget and self._execution_deadline is not None and _processing_time()>=self._execution_deadline:
                # Keep worst-case fees held; a later observation can settle them.
                return result
            receipt = self._call('settlement',task,request_id,identity)
            if receipt is not None:
                from fsf.production.contracts import integer,seal
                units=self.config['settlement']
                if (not isinstance(receipt,dict) or set(receipt)!={'request_id','units','currency','unit_scale','receipt_digest'}
                        or receipt['request_id']!=request_id or receipt['currency']!=units['currency']
                        or receipt['unit_scale']!=units['unit_scale'] or seal(receipt,'receipt_digest')!=receipt):
                    raise ProductionError('PRODUCTION_SETTLEMENT_INVALID')
                result['settled_units']=integer(receipt['units'])
        return result

    def cancel(self, task, request_id):
        identity = self._identity(task, request_id)
        if identity is None:
            return 'unknown'
        return self._call('cancel', task, request_id, identity)

    def _verify(self,task,request_id,challenge=None):
        identity=self._identity(task,request_id)
        if identity is None:raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
        reply=self._call('fetch',task,request_id,identity)
        if set(reply)!={'artifact','report'} or not isinstance(reply['artifact'],str) or not isinstance(reply['report'],dict):
            raise ProductionError('PRODUCTION_EVALUATOR_INVALID')
        # The following content read checks every ancestor, the leaf type and
        # actual bytes. Do not repeat that same filesystem traversal beforehand.
        path=self.artifacts.joinpath(*_path_parts(request_id+'/'+reply['artifact']))
        observation = None
        if challenge is not None:
            from fsf.production.control import ArtifactObservation
            observation = ArtifactObservation(path,challenge)
            observed = observation._identity
        else:
            observed = file_identity(path)
        report=reply['report'];samples=report.get('sample_hashes')
        if not isinstance(samples,list) or not 1<=len(samples)<=4096 or any(not isinstance(s,str) or not re.fullmatch('[0-9a-f]{64}',s) for s in samples):
            raise ProductionError('PRODUCTION_EVALUATOR_INVALID')
        evidence=content_verdict(report,artifact_sha256=observed['sha256'],evaluator_digest=self.config['evaluator_digest'],
                                 policy_digest=self.policy_digest,sample_hashes=samples)
        return path,evidence,observation

    def verify(self,task,request_id):
        path,evidence,_ = self._verify(task,request_id)
        return path,evidence

    def verify_observed(self,task,request_id,*,challenge):
        if challenge is None:
            raise ProductionError('PRODUCTION_OBSERVATION_INVALID')
        if getattr(self.verify,'__func__',None) is not CommandDriver.verify:
            from fsf.production.control import ArtifactObservation
            path,evidence = self.verify(task,request_id)
            return path,evidence,ArtifactObservation(path,challenge)
        return self._verify(task,request_id,challenge)
