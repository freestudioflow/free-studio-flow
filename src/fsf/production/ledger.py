"""Single-file parent budget authority with exact integer money and OS locks."""
from __future__ import annotations

import re
import time

from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, key, publish, seal, validate


class Ledger:
    """All writers cooperate; forged/rolled-back files are outside the trust boundary."""

    def __init__(self, path):
        self.path = path

    def create(self, authorization):
        auth = self._authorization(authorization)
        minimum = max((auth["total_units"]+9)//10, auth["fixed_delivery_units"])
        if auth["cleanup_units"] < minimum or sum(auth["route_limits"].values()) > auth["total_units"]:
            raise ProductionError("PRODUCTION_BUDGET_INVALID")
        state = seal({"schema_version": "fsf_production_ledger_v1", "authorization": auth,
                      "revision": 1, "reservations": {}, "consumed_frames": 0}, "ledger_digest")
        if auth['schema_version']=='fsf_production_budget_v2':
            state.update(schema_version='fsf_production_ledger_v2',forecast=None,
                         repair_rounds={shot:1 for shot in auth['logical_shot_ids']},round_history=[])
            state=seal(state,'ledger_digest')
        with edit_lock(self.path) as path:
            if path.exists():
                if self._read()["authorization"] != auth:
                    raise ProductionError("PRODUCTION_BUDGET_CONFLICT")
                return self._read()
            publish(path, state)
        return state

    def _read(self):
        value = read_json(self.path)
        if seal(value, "ledger_digest") != value or value.get("schema_version") not in {"fsf_production_ledger_v1","fsf_production_ledger_v2"}:
            raise ProductionError("PRODUCTION_INTEGRITY")
        auth=self._authorization(value['authorization'])
        version='fsf_production_ledger_v2' if auth['schema_version']=='fsf_production_budget_v2' else 'fsf_production_ledger_v1'
        if value['schema_version']!=version:raise ProductionError('PRODUCTION_INTEGRITY')
        if version=='fsf_production_ledger_v2':
            rounds=value.get('repair_rounds')
            if (not isinstance(rounds,dict) or set(rounds)!=set(auth['logical_shot_ids'])
                    or any(type(v) is not int or not 1<=v<=auth['max_repair_rounds'] for v in rounds.values())
                    or not isinstance(value.get('round_history'),list) or 'forecast' not in value):
                raise ProductionError('PRODUCTION_INTEGRITY')
        return value

    @staticmethod
    def _authorization(value):
        version='fsf_production_budget_v2' if isinstance(value,dict) and value.get('schema_version')=='fsf_production_budget_v2' else 'fsf_production_budget_v1'
        auth=validate(value,version,'authorization_digest')
        if version=='fsf_production_budget_v2':
            for name in ('unit_scale','total_units','cleanup_units','fixed_delivery_units','max_generated_frames',
                         'expires_at_unix','max_repair_rounds','reforecast_percent'):
                integer(auth[name])
            for amount in auth['route_limits'].values():integer(amount)
            if set(auth['segment_ids_by_shot'])!=set(auth['logical_shot_ids']):
                raise ProductionError('PRODUCTION_BUDGET_INVALID')
        return auth

    @staticmethod
    def _cost(item):
        return item['settled_units'] if item['status']=='settled' else item['worst_units']

    @classmethod
    def _basis(cls,state):
        return digest({name:cls._cost(item) for name,item in state['reservations'].items()})

    @staticmethod
    def _v2(state):
        if state['authorization']['schema_version']!='fsf_production_budget_v2':
            raise ProductionError('PRODUCTION_POLICY_UNSUPPORTED')

    @staticmethod
    def _active_authority(auth):
        if not auth['authorized']:raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
        if auth['schema_version']=='fsf_production_budget_v2' and time.time()>=auth['expires_at_unix']:
            raise ProductionError('PRODUCTION_AUTHORIZATION_EXPIRED')

    def inspect(self):
        return self._read()

    def reserve(self, request_id, *, route, worst_units, generated_frames, binding_digest, generation_slot=None, essential=True):
        key(request_id)
        key(route)
        integer(worst_units)
        integer(generated_frames)
        if type(essential) is not bool:raise ProductionError('PRODUCTION_INVALID')
        if not isinstance(binding_digest, str) or not re.fullmatch("[0-9a-f]{64}",binding_digest):
            raise ProductionError("PRODUCTION_INVALID")
        if generated_frames:
            if not isinstance(generation_slot,dict) or set(generation_slot) != {"logical_shot_id","segment_id","repair_round"}:
                raise ProductionError("PRODUCTION_GENERATION_SLOT_REQUIRED")
            key(generation_slot["logical_shot_id"]);key(generation_slot["segment_id"])
            integer(generation_slot["repair_round"],1,3)
        elif generation_slot is not None:
            raise ProductionError("PRODUCTION_INVALID")
        entry = {"route": route, "worst_units": worst_units, "generated_frames": generated_frames,
                 "binding_digest": binding_digest, "status": "reserved", "settled_units": None,
                 "generation_slot":generation_slot}
        with edit_lock(self.path) as path:
            state = self._read()
            auth = state["authorization"]
            v2=auth['schema_version']=='fsf_production_budget_v2'
            if v2:entry['essential']=essential
            elif essential is not True:raise ProductionError('PRODUCTION_POLICY_UNSUPPORTED')
            old = state["reservations"].get(request_id)
            if old:
                fields=("route", "worst_units", "generated_frames", "binding_digest","generation_slot")+(('essential',) if v2 else ())
                if any(old[k] != entry[k] for k in fields):
                    raise ProductionError("PRODUCTION_REQUEST_CONFLICT")
                return old
            self._active_authority(auth)
            if not auth["authorized"] or route not in auth["route_limits"]:
                raise ProductionError("PRODUCTION_NOT_AUTHORIZED")
            if generated_frames and generation_slot["logical_shot_id"] not in auth["logical_shot_ids"]:
                raise ProductionError("PRODUCTION_NOT_AUTHORIZED")
            if generated_frames and v2:
                shot=generation_slot['logical_shot_id']
                if generation_slot['segment_id'] not in auth['segment_ids_by_shot'][shot]:
                    raise ProductionError('PRODUCTION_SEGMENT_NOT_AUTHORIZED')
                if generation_slot['repair_round']!=state['repair_rounds'][shot]:
                    raise ProductionError('PRODUCTION_REPAIR_ROUND_CONFLICT')
            if generation_slot is not None and any(e["generation_slot"] == generation_slot for e in state["reservations"].values()):
                raise ProductionError("PRODUCTION_GENERATION_SLOT_USED")
            cost = self._cost
            entries = state["reservations"].values()
            used = sum(cost(item) for item in entries)
            route_used = sum(cost(item) for item in entries if item["route"] == route)
            if (used+worst_units+auth["cleanup_units"] > auth["total_units"]
                    or route_used+worst_units > auth["route_limits"][route]
                    or state["consumed_frames"]+generated_frames > auth["max_generated_frames"]):
                raise ProductionError("PRODUCTION_BUDGET_EXCEEDED")
            if v2 and (used+worst_units)*100>=auth['total_units']*auth['reforecast_percent']:
                if not essential:raise ProductionError('PRODUCTION_OPTIONAL_WORK_PAUSED')
                forecast=state['forecast']
                if (not forecast or forecast['basis_digest']!=self._basis(state)
                        or forecast['remaining_worst_units']<worst_units):
                    raise ProductionError('PRODUCTION_FORECAST_REQUIRED')
                if not forecast['affordable']:raise ProductionError('PRODUCTION_FORECAST_UNAFFORDABLE')
            state["reservations"][request_id] = entry
            state["consumed_frames"] += generated_frames
            if v2 and state['forecast'] is not None:
                forecast=state['forecast']
                forecast['remaining_worst_units']=max(0,forecast['remaining_worst_units']-worst_units)
                forecast['basis_digest']=self._basis(state)
                forecast['used_units']=used+worst_units
                forecast['affordable']=forecast['used_units']+forecast['remaining_worst_units']+auth['cleanup_units']<=auth['total_units']
            self._save(path,state)
            return entry

    def reconcile(self, request_id, *, settled_units=None):
        """Unknown retains its full reservation; settling cannot exceed its bound."""
        key(request_id)
        with edit_lock(self.path) as path:
            state = self._read()
            if request_id not in state["reservations"]:
                raise ProductionError("PRODUCTION_NOT_FOUND")
            entry = state["reservations"][request_id]
            if settled_units is None:
                if entry["status"] != "settled":
                    entry["status"] = "unknown"
            else:
                integer(settled_units, maximum=entry["worst_units"])
                if entry["status"] == "settled" and entry["settled_units"] != settled_units:
                    raise ProductionError("PRODUCTION_REQUEST_CONFLICT")
                entry.update(status="settled", settled_units=settled_units)
            if state['authorization']['schema_version']=='fsf_production_budget_v2' and state['forecast'] is not None:
                forecast=state['forecast'];auth=state['authorization']
                forecast['basis_digest']=self._basis(state)
                forecast['used_units']=sum(self._cost(e) for e in state['reservations'].values())
                forecast['affordable']=forecast['used_units']+forecast['remaining_worst_units']+auth['cleanup_units']<=auth['total_units']
            self._save(path,state)
            return entry

    def admission_status(self, *, worst_units=0, essential=True):
        """Global status only; reserve still checks route, frames, slots and CAS."""
        integer(worst_units)
        if type(essential) is not bool:raise ProductionError('PRODUCTION_INVALID')
        state=self._read();self._v2(state);auth=state['authorization']
        used=sum(self._cost(e) for e in state['reservations'].values())
        threshold=(used+worst_units)*100>=auth['total_units']*auth['reforecast_percent']
        forecast=state['forecast']
        current=bool(forecast and forecast['basis_digest']==self._basis(state)
                     and forecast['remaining_worst_units']>=worst_units and forecast['affordable'])
        expired=time.time()>=auth['expires_at_unix']
        reason=None
        if not auth['authorized']:reason='PRODUCTION_NOT_AUTHORIZED'
        elif expired:reason='PRODUCTION_AUTHORIZATION_EXPIRED'
        elif used+worst_units+auth['cleanup_units']>auth['total_units']:reason='PRODUCTION_BUDGET_EXCEEDED'
        elif threshold and not essential:reason='PRODUCTION_OPTIONAL_WORK_PAUSED'
        elif threshold and not current:reason='PRODUCTION_FORECAST_REQUIRED'
        return {'used_units':used,'available_units':max(0,auth['total_units']-used-auth['cleanup_units']),
                'unknown_units':sum(e['worst_units'] for e in state['reservations'].values() if e['status']=='unknown'),
                'threshold_reached':threshold,'forecast_required':threshold and not current,
                'forecast_current':current,'expired':expired,'global_admission_allowed':reason is None,'reason':reason}

    def record_forecast(self, *, remaining_worst_units, plan_digest):
        """Estimate unreserved work and bounded retries, excluding held costs.

        This is an automated forecast, not a new authorization. Reservations
        consume its remaining estimate; settlement never releases a claim or
        increases the original authority. Recompute when future work changes.
        """
        integer(remaining_worst_units)
        with edit_lock(self.path) as path:
            state=self._read();self._v2(state);auth=state['authorization']
            if plan_digest!=auth['plan_digest']:raise ProductionError('PRODUCTION_FORECAST_PLAN')
            used=sum(self._cost(e) for e in state['reservations'].values())
            forecast={'plan_digest':plan_digest,'remaining_worst_units':remaining_worst_units,'used_units':used,
                      'basis_digest':self._basis(state),'created_at_unix':int(time.time()),
                      'affordable':used+remaining_worst_units+auth['cleanup_units']<=auth['total_units']}
            state['forecast']=forecast;self._save(path,state)
            return forecast

    def current_repair_round(self, logical_shot_id):
        key(logical_shot_id);state=self._read();self._v2(state)
        if logical_shot_id not in state['repair_rounds']:raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
        return state['repair_rounds'][logical_shot_id]

    def advance_repair_round(self, logical_shot_id, *, expected_round):
        """CAS after caller reconciles all jobs in the shot's previous round.

        Bill settlement is not job completion. The workflow coordinator must
        reject advance while a job is submitted/running/unknown/produced, and
        may reuse accepted bytes. This financial ledger does not invent those
        external observations. Unknown fees remain fully held across advance.
        """
        key(logical_shot_id);integer(expected_round,1,3)
        with edit_lock(self.path) as path:
            state=self._read();self._v2(state);auth=state['authorization'];self._active_authority(auth)
            if logical_shot_id not in state['repair_rounds']:raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
            current=state['repair_rounds'][logical_shot_id]
            if current!=expected_round:raise ProductionError('PRODUCTION_REPAIR_ROUND_CONFLICT')
            if current>=auth['max_repair_rounds']:raise ProductionError('PRODUCTION_REPAIR_EXHAUSTED')
            if not any(e['generation_slot'] and e['generation_slot']['logical_shot_id']==logical_shot_id
                       and e['generation_slot']['repair_round']==current for e in state['reservations'].values()):
                raise ProductionError('PRODUCTION_REPAIR_ROUND_EMPTY')
            state['repair_rounds'][logical_shot_id]=current+1
            state['round_history'].append({'logical_shot_id':logical_shot_id,'from':current,'to':current+1})
            self._save(path,state)
            return current+1

    @staticmethod
    def _save(path,state):
        state["revision"] += 1
        publish(path,seal(state,"ledger_digest"))
