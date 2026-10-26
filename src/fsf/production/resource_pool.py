"""Cooperative cross-job GPU admission; reservations are not OS isolation.

Inventory and stopped observations come from operator-owned host adapters.
An expired lease remains occupied/unknown; it is never permission to steal a
device. Model residency survives task completion until explicitly unloaded.
"""
import copy
import re
import time
import math
from pathlib import Path

from fsf.core.edit_store import edit_lock,read_json
from fsf.production.contracts import ProductionError,digest,integer,key,publish,seal


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ProductionError('PRODUCTION_POOL_BINDING')


def _authority(value):
    fields={'schema_version','pool_id','authorized','devices','max_lease_seconds','max_bypasses','max_queued','max_resident_models'}
    if not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='fsf_resource_pool_authority_v1' or type(value['authorized']) is not bool:
        raise ProductionError('PRODUCTION_POOL_AUTHORITY')
    key(value['pool_id']);integer(value['max_lease_seconds'],1,5400)
    integer(value['max_bypasses'],0,64);integer(value['max_queued'],1,4096);integer(value['max_resident_models'],1,1024)
    devices=value['devices']
    if not isinstance(devices,list) or not devices or len(devices)>64:raise ProductionError('PRODUCTION_POOL_AUTHORITY')
    seen=set()
    for device in devices:
        if not isinstance(device,dict) or set(device)!={'uuid','vram_mb','slots'} or not isinstance(device['uuid'],str) or not device['uuid'] or len(device['uuid'])>256 or device['uuid'] in seen:
            raise ProductionError('PRODUCTION_POOL_AUTHORITY')
        seen.add(device['uuid']);integer(device['vram_mb'],1);integer(device['slots'],1,1024)
    return copy.deepcopy(value)


def _ticket(value,authority):
    fields={'request_id','job_id','resident_id','model_digest','runtime_digest','resident_mb','workspace_mb','mode','eligible_uuids','lease_seconds'}
    if not isinstance(value,dict) or set(value)!=fields:raise ProductionError('PRODUCTION_POOL_BINDING')
    key(value['request_id']);key(value['job_id']);key(value['resident_id']);_digest(value['model_digest']);_digest(value['runtime_digest'])
    integer(value['resident_mb'],1);integer(value['workspace_mb'],0)
    integer(value['lease_seconds'],1,authority['max_lease_seconds'])
    eligible=value['eligible_uuids']
    if value['mode'] not in ('shared','exclusive') or not isinstance(eligible,list) or not eligible or any(not isinstance(x,str) for x in eligible) or len(eligible)!=len(set(eligible)) or not set(eligible)<={d['uuid'] for d in authority['devices']}:
        raise ProductionError('PRODUCTION_POOL_BINDING')
    if not any(value['resident_mb']+value['workspace_mb']<=d['vram_mb'] for d in authority['devices'] if d['uuid'] in eligible):
        raise ProductionError('PRODUCTION_POOL_MEMORY_LIMIT')
    return copy.deepcopy(value)


class ResourcePool:
    def __init__(self,path,*,clock=time.time):
        self.path=Path(path);self.clock=clock

    def create(self,authority):
        authority=_authority(authority)
        with edit_lock(self.path) as path:
            if path.exists():
                state=self._read()
                if state['authority']!=authority:raise ProductionError('PRODUCTION_POOL_AUTHORITY_CHANGED')
                return state
            value={'schema_version':'fsf_resource_pool_state_v1','authority':authority,'epoch':0,'sequence':0,
                   'tickets':{},'claims':{},'residents':{}}
            self._save(path,value);return self._read()

    def _read(self):
        state=read_json(self.path)
        if (state.get('schema_version')!='fsf_resource_pool_state_v1' or seal(state,'pool_digest')!=state):
            raise ProductionError('PRODUCTION_POOL_INTEGRITY')
        _authority(state['authority'])
        return state

    @staticmethod
    def _save(path,state):publish(path,seal(state,'pool_digest'))

    def _now(self):
        value=self.clock()
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ProductionError('PRODUCTION_POOL_CLOCK')
        return value

    def _expire(self,state):
        # Complete a stop already durably recorded before an interrupted state
        # update. No new observation is invented and no live lease is stolen.
        for rid in list(state['tickets']):
            history=self._history(rid)
            if not history.exists():continue
            record=read_json(history)
            if seal(record,'history_digest')!=record or record.get('ticket')!=state['tickets'][rid]['ticket']:
                raise ProductionError('PRODUCTION_POOL_INTEGRITY')
            if record.get('state')=='released':
                if rid in state['claims']:self._claim(state,record['claim']);state['claims'].pop(rid)
            elif record.get('state')!='cancelled' or rid in state['claims']:
                raise ProductionError('PRODUCTION_POOL_INTEGRITY')
            state['tickets'].pop(rid)
        now=self._now()
        for item in state['claims'].values():
            if item['state']=='running' and item['lease_until']<=now:item['state']='unknown'
        for item in state['residents'].values():
            if item['state']=='resident' and item['lease_until']<=now:item['state']='unknown'

    def inspect(self):
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state);self._save(path,state);return self._read()

    def _history(self,request_id):
        key(request_id)
        return self.path.parent/(self.path.name+'.history')/(request_id+'.json')

    def enqueue(self,ticket):
        with edit_lock(self.path) as path:
            state=self._read();ticket=_ticket(ticket,state['authority']);rid=ticket['request_id']
            history=self._history(rid)
            if history.exists():
                record=read_json(history)
                if seal(record,'history_digest')!=record or record['ticket']!=ticket:raise ProductionError('PRODUCTION_POOL_BINDING')
                return record
            if rid in state['tickets']:
                if state['tickets'][rid]['ticket']!=ticket:raise ProductionError('PRODUCTION_POOL_BINDING')
                return copy.deepcopy(state['tickets'][rid])
            if len(state['tickets'])>=state['authority']['max_queued']:raise ProductionError('PRODUCTION_POOL_QUEUE_LIMIT')
            state['sequence']+=1
            state['tickets'][rid]={'ticket':ticket,'order':state['sequence'],'bypasses':0}
            self._save(path,state);return copy.deepcopy(state['tickets'][rid])

    @staticmethod
    def _inventory(probe,allowed):
        if not callable(probe):raise ProductionError('PRODUCTION_POOL_PROBE_REQUIRED')
        try:observed=probe()
        except Exception:raise ProductionError('PRODUCTION_POOL_PROBE_FAILED') from None
        if not isinstance(observed,list):raise ProductionError('PRODUCTION_POOL_PROBE_INVALID')
        result={}
        for item in observed:
            if not isinstance(item,dict) or set(item)!={'uuid','total_mb','free_mb','managed_used_mb'}:
                raise ProductionError('PRODUCTION_POOL_PROBE_INVALID')
            uuid=item['uuid']
            if not isinstance(uuid,str) or uuid in result:raise ProductionError('PRODUCTION_POOL_PROBE_INVALID')
            try:
                integer(item['total_mb'],1);integer(item['free_mb'],0,item['total_mb'])
                integer(item['managed_used_mb'],0,item['total_mb']-item['free_mb'])
            except ProductionError:raise ProductionError('PRODUCTION_POOL_PROBE_INVALID') from None
            if uuid in allowed:result[uuid]=item
        return result

    @staticmethod
    def _fit(state,ticket,observed):
        auth=state['authority'];model=digest({'resident_id':ticket['resident_id'],'model':ticket['model_digest'],'runtime':ticket['runtime_digest']})
        # Reuse means the SAME host-owned resident service, not two independent
        # processes that happen to load identical weights.
        for existing in state['residents'].values():
            if existing['resident_id']==ticket['resident_id'] and (existing['model_digest']!=ticket['model_digest'] or existing['runtime_digest']!=ticket['runtime_digest']):
                raise ProductionError('PRODUCTION_POOL_MODEL_CHANGED')
        for device in auth['devices']:
            uuid=device['uuid']
            if uuid not in ticket['eligible_uuids'] or uuid not in observed:continue
            current=observed[uuid]
            claims=[c for c in state['claims'].values() if c['uuid']==uuid]
            residents=[r for r in state['residents'].values() if r['uuid']==uuid]
            if any(c['state']=='unknown' for c in claims) or any(r['state']=='unknown' for r in residents):continue
            if len(claims)>=device['slots'] or any(c['mode']=='exclusive' for c in claims):continue
            resident_key=digest({'uuid':uuid,'model':model});existing=state['residents'].get(resident_key)
            if existing and existing['resident_mb']!=ticket['resident_mb']:raise ProductionError('PRODUCTION_POOL_MODEL_CHANGED')
            if ticket['mode']=='exclusive' and (claims or any(r['resident_key']!=resident_key for r in residents)):continue
            if not existing and len(state['residents'])>=auth['max_resident_models']:continue
            resident_reserved=sum(r['resident_mb'] for r in residents)
            reserved=resident_reserved+sum(c['workspace_mb'] for c in claims)
            if current['managed_used_mb']>reserved:raise ProductionError('PRODUCTION_POOL_MEMORY_VIOLATION')
            required=ticket['workspace_mb']+(0 if existing else ticket['resident_mb'])
            # Observed managed allocations can be deducted from outstanding
            # reservations. Without that host attribution, accounting is safely
            # conservative rather than counting the same free memory twice.
            physical=current['free_mb']-max(0,reserved-current['managed_used_mb'])
            if reserved+required>min(device['vram_mb'],current['total_mb']) or required>physical:continue
            return uuid,resident_key,model
        return None

    def admit(self,request_id,*,inventory):
        key(request_id)
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state)
            if not state['authority']['authorized']:raise ProductionError('PRODUCTION_POOL_NOT_AUTHORIZED')
            if request_id in state['claims']:
                self._save(path,state);return copy.deepcopy(state['claims'][request_id])
            history=self._history(request_id)
            if history.exists():
                value=read_json(history)
                if seal(value,'history_digest')!=value:raise ProductionError('PRODUCTION_POOL_INTEGRITY')
                self._save(path,state);return value
            if request_id not in state['tickets']:raise ProductionError('PRODUCTION_POOL_NOT_FOUND')
            observed=self._inventory(inventory,{d['uuid'] for d in state['authority']['devices']})
            item=state['tickets'][request_id];choice=self._fit(state,item['ticket'],observed)
            if choice is None:
                self._save(path,state);return {'state':'queued','request_id':request_id}
            older=[v for k,v in state['tickets'].items() if k not in state['claims'] and v['order']<item['order'] and choice[0] in v['ticket']['eligible_uuids']]
            contenders=[]
            for other in older:
                try:fit=self._fit(state,other['ticket'],{choice[0]:observed[choice[0]]})
                except ProductionError as exc:
                    if exc.code=='PRODUCTION_POOL_MODEL_CHANGED':continue
                    raise
                contenders.append(other)
                if fit is not None or other['bypasses']>=state['authority']['max_bypasses']:
                    self._save(path,state);return {'state':'queued','request_id':request_id,'reason':'fairness'}
            for other in contenders:other['bypasses']+=1
            uuid,resident_key,model=choice;ticket=item['ticket'];state['epoch']+=1
            until=self._now()+ticket['lease_seconds']
            resident=state['residents'].get(resident_key)
            if resident is None:
                resident={'resident_key':resident_key,'resident_id':ticket['resident_id'],'uuid':uuid,'model_key':model,'model_digest':ticket['model_digest'],
                          'runtime_digest':ticket['runtime_digest'],'resident_mb':ticket['resident_mb'],'epoch':state['epoch'],
                          'state':'resident','lease_until':until}
                state['residents'][resident_key]=resident
            else:resident['lease_until']=max(resident['lease_until'],until)
            claim={'request_id':request_id,'job_id':ticket['job_id'],'uuid':uuid,'epoch':state['epoch'],
                   'resident_key':resident_key,'resident_epoch':resident['epoch'],'workspace_mb':ticket['workspace_mb'],
                   'mode':ticket['mode'],'state':'running','lease_until':until}
            state['claims'][request_id]=claim;self._save(path,state);return copy.deepcopy(claim)

    @staticmethod
    def _claim(state,token):
        if not isinstance(token,dict) or type(token.get('epoch')) is not int:
            raise ProductionError('PRODUCTION_POOL_FENCED')
        claim=state['claims'].get(token.get('request_id'))
        if claim is None or any(token.get(k)!=claim[k] for k in ('request_id','uuid','epoch')):
            raise ProductionError('PRODUCTION_POOL_FENCED')
        return claim

    def renew(self,token,*,seconds):
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state);claim=self._claim(state,token)
            integer(seconds,1,state['authority']['max_lease_seconds'])
            if claim['state']!='running' or state['residents'][claim['resident_key']]['state']!='resident':
                self._save(path,state);raise ProductionError('PRODUCTION_POOL_RECONCILE_REQUIRED')
            claim['lease_until']=self._now()+seconds
            resident=state['residents'][claim['resident_key']]
            resident['lease_until']=max(resident['lease_until'],claim['lease_until'])
            self._save(path,state);return copy.deepcopy(claim)

    @staticmethod
    def _stopped(confirm,target,*,scope):
        if not callable(confirm):raise ProductionError('PRODUCTION_POOL_STOP_PROBE_REQUIRED')
        try:result=confirm(copy.deepcopy(target))
        except Exception:raise ProductionError('PRODUCTION_POOL_STOP_UNCONFIRMED') from None
        if (not isinstance(result,dict) or set(result)!={'scope','target_id','epoch','stopped','observation_digest'}
                or result['scope']!=scope or result['target_id']!=target['request_id' if scope=='task' else 'resident_key']
                or type(result['epoch']) is not int or result['epoch']!=target['epoch'] or result['stopped'] is not True):
            raise ProductionError('PRODUCTION_POOL_STOP_UNCONFIRMED')
        _digest(result['observation_digest']);return result

    def release(self,token,*,confirm_stopped):
        if not isinstance(token,dict) or type(token.get('epoch')) is not int:raise ProductionError('PRODUCTION_POOL_FENCED')
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state)
            history=self._history(token.get('request_id'))
            if history.exists():
                record=read_json(history)
                if (seal(record,'history_digest')!=record or record['state']!='released'
                        or any(token.get(k)!=record['claim'][k] for k in ('request_id','uuid','epoch'))):
                    raise ProductionError('PRODUCTION_POOL_FENCED')
                if token['request_id'] in state['claims']:
                    self._claim(state,token)
                    state['claims'].pop(token['request_id']);state['tickets'].pop(token['request_id'])
                    self._save(path,state)
                return record
            claim=self._claim(state,token)
            evidence=self._stopped(confirm_stopped,claim,scope='task')
            rid=claim['request_id'];history=self._history(rid);history.parent.mkdir(exist_ok=True)
            record=seal({'state':'released','ticket':state['tickets'][rid]['ticket'],'claim':claim,'stopped_evidence':evidence},'history_digest')
            # History is durable first; an interrupted release never frees a slot
            # without recorded stop evidence. The next release is safe to repeat.
            if history.exists() and read_json(history)!=record:raise ProductionError('PRODUCTION_POOL_BINDING')
            if not history.exists():publish(history,record)
            state['claims'].pop(rid);state['tickets'].pop(rid);self._save(path,state)
            return record

    def cancel_queued(self,request_id):
        key(request_id)
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state)
            if request_id in state['claims']:raise ProductionError('PRODUCTION_POOL_BUSY')
            history=self._history(request_id)
            if history.exists():return read_json(history)
            if request_id not in state['tickets']:raise ProductionError('PRODUCTION_POOL_NOT_FOUND')
            record=seal({'state':'cancelled','ticket':state['tickets'][request_id]['ticket']},'history_digest')
            history.parent.mkdir(exist_ok=True);publish(history,record)
            state['tickets'].pop(request_id);self._save(path,state);return record

    def evict(self,resident_key,*,epoch,confirm_stopped):
        _digest(resident_key)
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state);resident=state['residents'].get(resident_key)
            if resident is None or type(epoch) is not int or resident['epoch']!=epoch:raise ProductionError('PRODUCTION_POOL_FENCED')
            if any(c['resident_key']==resident_key for c in state['claims'].values()):raise ProductionError('PRODUCTION_POOL_BUSY')
            evidence=self._stopped(confirm_stopped,resident,scope='model')
            state['residents'].pop(resident_key);self._save(path,state)
            return {'state':'evicted','resident_key':resident_key,'epoch':epoch,'stopped_evidence':evidence}

    def renew_resident(self,resident_key,*,epoch,seconds):
        """A host-owned resident service can heartbeat while it has no task."""
        _digest(resident_key)
        with edit_lock(self.path) as path:
            state=self._read();self._expire(state);resident=state['residents'].get(resident_key)
            integer(seconds,1,state['authority']['max_lease_seconds'])
            if resident is None or type(epoch) is not int or resident['epoch']!=epoch:raise ProductionError('PRODUCTION_POOL_FENCED')
            if resident['state']!='resident':
                self._save(path,state);raise ProductionError('PRODUCTION_POOL_RECONCILE_REQUIRED')
            resident['lease_until']=max(resident['lease_until'],self._now()+seconds)
            self._save(path,state);return copy.deepcopy(resident)
