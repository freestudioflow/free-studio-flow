"""Finite durable parent batches. Drivers own provider and quality evidence."""
from __future__ import annotations

from pathlib import Path
import time
import re

from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, key, publish, seal
from fsf.production.workflow import Workflow
from fsf.production.segments import affected_tasks


def validate_plan(plan):
    fields={"schema_version","production_id","binding_digest","policy_digest","tasks","plan_digest"}
    if (not isinstance(plan,dict) or set(plan) != fields or plan["schema_version"] not in {"fsf_production_batch_v1","fsf_production_batch_v2"}
            or seal(plan,"plan_digest") != plan or not isinstance(plan["tasks"],list) or not plan["tasks"]):
        raise ProductionError("PRODUCTION_PLAN_INVALID")
    key(plan["production_id"])
    if not isinstance(plan['policy_digest'],str) or not re.fullmatch('[0-9a-f]{64}',plan['policy_digest']):
        raise ProductionError('PRODUCTION_PLAN_INVALID')
    if plan["binding_digest"] != digest({k:plan[k] for k in ("production_id","policy_digest","tasks")}):
        raise ProductionError("PRODUCTION_PLAN_INVALID")
    seen,graph=set(),{}
    for task in plan["tasks"]:
        expected={"task_id","depends_on","input_digest","route","worst_units","generated_frames","logical_shot_id","segment_id"}
        if plan['schema_version']=='fsf_production_batch_v2':expected.add('essential')
        if not isinstance(task,dict) or set(task) != expected:
            raise ProductionError("PRODUCTION_PLAN_INVALID")
        if 'essential' in task and type(task['essential']) is not bool:raise ProductionError('PRODUCTION_PLAN_INVALID')
        key(task["task_id"]);key(task["route"])
        key(task["logical_shot_id"]);key(task["segment_id"])
        integer(task["worst_units"]);integer(task["generated_frames"])
        if not isinstance(task['input_digest'],str) or not re.fullmatch('[0-9a-f]{64}',task['input_digest']):
            raise ProductionError('PRODUCTION_PLAN_INVALID')
        if task["task_id"] in seen or not isinstance(task["depends_on"],list) or len(task["depends_on"]) != len(set(task["depends_on"])):
            raise ProductionError("PRODUCTION_DEPENDENCY_INVALID")
        seen.add(task["task_id"]);graph[task["task_id"]]=task["depends_on"]
    affected_tasks(graph,[])
    return graph


def _remaining_forecast(plan, state, book):
    """Unreserved worst-case future attempts; held/unknown bills stay in used."""
    reservations=book['reservations'];auth=book['authorization'];total=0
    for task in plan['tasks']:
        item=state['tasks'][task['task_id']]
        if item['accepted']:continue
        attempts=item['attempts'];last=attempts[-1] if attempts else None
        if task['generated_frames']:
            count=auth['max_repair_rounds']-book['repair_rounds'][task['logical_shot_id']]+1
            if last and last['request_id'] in reservations:
                slot=reservations[last['request_id']]['generation_slot']
                if slot['repair_round']==book['repair_rounds'][task['logical_shot_id']]:count-=1
        else:
            count=3-len(attempts)
            if last and last['request_id'] not in reservations:count+=1
        total+=max(0,count)*task['worst_units']
    return total


def run_batch(plan, *, state_root, ledger, driver, seconds=300):
    """Observe/reconcile before retry; callbacks must not conceal provider calls.

    Driver methods: submit(task, request_id), observe(task, request_id),
    verify(task, request_id). Observe returns status and optional settled_units;
    verify returns an actual local artifact path and sealed acceptance evidence.
    No browser operations, automatic model selection or inferred human approvals.
    """
    graph=validate_plan(plan)
    initial_book=ledger.inspect();v2=initial_book['authorization']['schema_version']=='fsf_production_budget_v2'
    if v2 and (initial_book['authorization']['production_id']!=plan['production_id'] or initial_book['authorization']['plan_digest']!=plan['plan_digest']):
        raise ProductionError('PRODUCTION_FORECAST_PLAN')
    integer(seconds,1,5400)
    root=_root(state_root)
    folder=root/plan["plan_digest"]
    folder.mkdir(exist_ok=True)
    flow=Workflow(folder/"workflow.json")
    flow.create(production_id=plan["production_id"],binding_digest=plan["binding_digest"],dependencies=graph)
    end=time.monotonic()+seconds
    by_id={t["task_id"]:t for t in plan["tasks"]}
    with edit_lock(folder/".parent-run"):
        stored=folder/"plan.json"
        if stored.exists() and read_json(stored) != plan:
            raise ProductionError("PRODUCTION_PLAN_INVALID")
        if not stored.exists():publish(stored,plan)
        while time.monotonic()<end:
            state=flow.inspect()
            if state["paused"]:return {"status":"paused","plan_digest":plan["plan_digest"]}
            all_accepted=True
            progressed=False
            repair_wait=False
            for task_id,task in by_id.items():
                if time.monotonic()>=end:break
                item=flow.inspect()["tasks"][task_id]
                if item["accepted"]:
                    # Re-validate current sources, quality policy and bytes even
                    # when a prior attempt is reused. A receipt is not timeless.
                    artifact,evidence=driver.verify(task,item["attempts"][-1]["request_id"])
                    from fsf.production.control import file_identity
                    if evidence != item["accepted"] or file_identity(artifact)["sha256"] != evidence["artifact_sha256"]:
                        raise ProductionError("PRODUCTION_SOURCE_CHANGED")
                    continue
                all_accepted=False
                current=flow.inspect()
                if any(current["tasks"][k]["accepted"] is None for k in graph[task_id]):continue
                prior=item["attempts"][-1] if item["attempts"] else None
                fresh=prior is None or prior["status"] in {"failed","cancelled","rejected"} or prior.get("invalidated",False)
                submit_required=fresh or prior['status']=='reserved'
                repair_round=None
                if v2 and task['generated_frames']:
                    repair_round=ledger.current_repair_round(task['logical_shot_id'])
                    old=ledger.inspect()['reservations'].get(prior['request_id']) if prior else None
                    if not fresh and old and old['generation_slot']:
                        repair_round=old['generation_slot']['repair_round']
                    if fresh and old and old['generation_slot']['repair_round']==repair_round:
                        # No financial settlement is treated as a provider stop.
                        siblings=[t for t in plan['tasks'] if t['logical_shot_id']==task['logical_shot_id'] and t['task_id']!=task_id]
                        live={'reserved','submitted','running','unknown','produced','verified'}
                        if any(current['tasks'][t['task_id']]['attempts'] and current['tasks'][t['task_id']]['attempts'][-1]['status'] in live for t in siblings):
                            repair_wait=True
                            continue
                        repair_round=ledger.advance_repair_round(task['logical_shot_id'],expected_round=repair_round)
                if fresh:
                    claim=flow.claim(task_id,owner="parent",seconds=max(1,int(end-time.monotonic())))
                else:claim=prior
                token={"epoch":claim["epoch"],"request_id":claim["request_id"]}
                if v2:
                    book=ledger.inspect()
                    if claim['request_id'] not in book['reservations']:
                        admission=ledger.admission_status(worst_units=task['worst_units'],essential=task.get('essential',True))
                        if admission['forecast_required'] and task.get('essential',True):
                            ledger.record_forecast(remaining_worst_units=_remaining_forecast(plan,flow.inspect(),book),plan_digest=plan['plan_digest'])
                ledger.reserve(claim["request_id"],route=task["route"],worst_units=task["worst_units"],
                               generated_frames=task["generated_frames"],binding_digest=task["input_digest"],
                               generation_slot={"logical_shot_id":task["logical_shot_id"],"segment_id":task["segment_id"],
                                                "repair_round":repair_round if v2 else len(flow.inspect()["tasks"][task_id]["attempts"])}
                               if task["generated_frames"] else None,essential=task.get('essential',True))
                if submit_required:
                    # An admission failure can leave a conclusively pre-submit
                    # reserved intent. Reuse its ID, but do not let an existing
                    # financial reservation renew permission to send new work.
                    authority=ledger.inspect()['authorization']
                    if not authority['authorized']:raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
                    if v2 and time.time()>=authority['expires_at_unix']:
                        raise ProductionError('PRODUCTION_AUTHORIZATION_EXPIRED')
                    # Intent and reservation are durable before the external call.
                    flow.record(task_id,status="submitted",**token)
                    try:driver.submit(task,claim["request_id"])
                    except Exception:
                        flow.record(task_id,status="unknown",**token)
                        ledger.reconcile(claim["request_id"])
                        raise ProductionError("PRODUCTION_RECONCILE_REQUIRED") from None
                observation=driver.observe(task,claim["request_id"])
                if not isinstance(observation,dict) or observation.get("status") not in {"running","produced","failed","cancelled","unknown"}:
                    raise ProductionError("PRODUCTION_PROVIDER_INVALID")
                status=observation["status"]
                latest=flow.inspect()["tasks"][task_id]["attempts"][-1]["status"]
                if status == "unknown":
                    if latest != "unknown":flow.record(task_id,status="unknown",**token)
                    ledger.reconcile(claim["request_id"])
                    return {"status":"unknown","plan_digest":plan["plan_digest"]}
                if status in {"running","failed","cancelled"}:
                    if latest != status:flow.record(task_id,status=status,**token)
                    ledger.reconcile(claim["request_id"],settled_units=observation.get("settled_units"))
                    progressed |= status != "running"
                    continue
                if latest not in {"produced","verified"}:flow.record(task_id,status="produced",**token)
                artifact,evidence=driver.verify(task,claim["request_id"])
                if evidence.get("policy_digest") != plan["policy_digest"]:
                    raise ProductionError("PRODUCTION_POLICY_CHANGED")
                if evidence.get("verdict") == "reject":
                    flow.record(task_id,status="rejected",evidence=evidence,**token)
                elif evidence.get("verdict") == "unknown":
                    return {"status":"quality_unknown","plan_digest":plan["plan_digest"]}
                else:
                    if latest != "verified":flow.record(task_id,status="verified",artifact_path=artifact,evidence=evidence,**token)
                    flow.record(task_id,status="machine_accepted",artifact_path=artifact,evidence=evidence,**token)
                ledger.reconcile(claim["request_id"],settled_units=observation.get("settled_units"))
                progressed=True
            if all_accepted:return {"status":"machine_accepted","plan_digest":plan["plan_digest"],"human_review":"not_reviewed"}
            if not progressed:
                return {"status":"waiting_repair_drain" if repair_wait else "waiting","plan_digest":plan["plan_digest"]}
        return {"status":"paused_batch","plan_digest":plan["plan_digest"]}
