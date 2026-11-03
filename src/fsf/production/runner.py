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


def _run_task(task_id, task, current, *, plan, flow, ledger, driver, v2, end):
    item = current['tasks'][task_id]
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
                return 'repair_wait'
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
                                    "repair_round":repair_round if v2 else len(item["attempts"])+(1 if fresh else 0)}
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
        return "waiting" if status == "running" else "progressed"
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
    return "progressed"


def _stamp(path):
    """Invalidation hint only: a stamp never replaces an initial SHA/evidence check."""
    from fsf.core.asset_library import _no_links
    try:
        path = Path(path)
        _no_links(path)
        value = path.stat()
        return [value.st_dev, value.st_ino, value.st_mode, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns]
    except OSError:
        raise ProductionError('PRODUCTION_SOURCE_CHANGED') from None


def _verify_accepted(task, item, driver):
    from fsf.production.control import file_identity
    artifact, evidence = driver.verify(task, item['attempts'][-1]['request_id'])
    before = _stamp(artifact)
    if (evidence != item['accepted']
            or file_identity(artifact)['sha256'] != evidence['artifact_sha256']
            or _stamp(artifact) != before):
        raise ProductionError('PRODUCTION_SOURCE_CHANGED')
    return {'path': str(Path(artifact).resolve()), 'stamp': before}


class _BatchProgress:
    """A restartable scan, never a replacement for workflow/evidence authority."""
    def __init__(self, path, plan, driver):
        self.path, self.plan = path, plan
        config = getattr(driver, 'config', None)
        self.binding = digest(config) if isinstance(config, dict) else None
        self.value = self.fresh()
        if path.exists():
            value = read_json(path)
            fields = set(self.value)
            if (not isinstance(value, dict) or set(value) != fields
                    or value.get('schema_version') != 'fsf_production_batch_progress_v1'
                    or seal(value, 'progress_digest') != value
                    or value.get('plan_digest') != plan['plan_digest']
                    or value.get('phase') not in {'work', 'verify', 'complete'}
                    or type(value.get('position')) is not int
                    or not 0 <= value['position'] <= len(plan['tasks'])
                    or not isinstance(value.get('checks'), list)):
                raise ProductionError('PRODUCTION_INTEGRITY')
            if value['phase'] == 'verify':
                if (not isinstance(value.get('workflow_digest'), str)
                        or not re.fullmatch('[0-9a-f]{64}', value['workflow_digest'])
                        or len(value['checks']) != value['position']):
                    raise ProductionError('PRODUCTION_INTEGRITY')
                for check in value['checks']:
                    if (not isinstance(check, dict) or set(check) != {'path', 'stamp'}
                            or not isinstance(check['path'], str)
                            or not isinstance(check['stamp'], list) or len(check['stamp']) != 6
                            or any(type(n) is not int for n in check['stamp'])):
                        raise ProductionError('PRODUCTION_INTEGRITY')
            elif value['checks'] or value['workflow_digest'] is not None:
                raise ProductionError('PRODUCTION_INTEGRITY')
            if value['driver_binding'] == self.binding:
                self.value = value

    def fresh(self):
        return seal({'schema_version': 'fsf_production_batch_progress_v1',
                     'plan_digest': self.plan['plan_digest'], 'driver_binding': self.binding,
                     'phase': 'work', 'position': 0, 'workflow_digest': None,
                     'checks': []}, 'progress_digest')

    def save(self):
        self.value = seal(self.value, 'progress_digest')
        publish(self.path, self.value)

    def work(self, position=0):
        self.value = self.fresh()
        self.value['position'] = position
        self.save()

    def verify(self, state):
        self.value.update(phase='verify', position=0,
                          workflow_digest=state['state_digest'], checks=[])
        self.save()

    def invalidate_changed_prefix(self):
        # Checked bytes/evidence stay bound to the immutable workflow snapshot.
        # Local changes invalidate that checkpoint and require SHA verification again.
        for index, check in enumerate(self.value['checks']):
            if _stamp(check['path']) != check['stamp']:
                self.value['position'] = index
                del self.value['checks'][index:]
                self.save()
                break


def run_batch(plan, *, state_root, ledger, driver, seconds=300):
    """Advance bounded work and revalidation without restarting a growing prefix.

    Revalidation is a resumable sweep, not an atomic or perpetual approval of
    external state. Each new reuse after completion starts a fresh sweep. Direct
    accepted inputs are revalidated before new work; completion requires checking
    every accepted task, never just the prefix that fits in this time budget.
    """
    from fsf.production.contracts import assert_live_path
    if hasattr(ledger, 'path'):
        assert_live_path(ledger.path)
    graph = validate_plan(plan)
    initial_book = ledger.inspect()
    config = getattr(driver, 'config', {})
    config = config if isinstance(config, dict) else {}
    if config.get('schema_version') == 'fsf_production_command_driver_v2':
        if any(config['settlement'][name] != initial_book['authorization'][name] for name in ('currency','unit_scale')):
            raise ProductionError('PRODUCTION_SETTLEMENT_INVALID')
    v2 = initial_book['authorization']['schema_version'] == 'fsf_production_budget_v2'
    if v2 and (initial_book['authorization']['production_id'] != plan['production_id']
               or initial_book['authorization']['plan_digest'] != plan['plan_digest']):
        raise ProductionError('PRODUCTION_FORECAST_PLAN')
    integer(seconds, 1, 5400)
    root = _root(state_root)
    folder = root / plan['plan_digest']
    folder.mkdir(exist_ok=True)
    flow = Workflow(folder / 'workflow.json')
    flow.create(production_id=plan['production_id'], binding_digest=plan['binding_digest'], dependencies=graph)
    end = time.monotonic() + seconds
    tasks = plan['tasks']
    by_id = {task['task_id']: task for task in tasks}
    with edit_lock(folder / '.parent-run'):
        stored = folder / 'plan.json'
        if stored.exists() and read_json(stored) != plan:
            raise ProductionError('PRODUCTION_PLAN_INVALID')
        if not stored.exists():
            publish(stored, plan)
        progress = _BatchProgress(folder / 'batch-progress.json', plan, driver)
        state = flow.inspect()
        if progress.value['phase'] == 'complete':
            progress.verify(state) if all(t['accepted'] for t in state['tasks'].values()) else progress.work()
        while time.monotonic() < end:
            state = flow.inspect()
            if state['paused']:
                return {'status': 'paused', 'plan_digest': plan['plan_digest']}
            if progress.value['phase'] == 'verify':
                if state['state_digest'] != progress.value['workflow_digest']:
                    progress.work()
                    continue
                progress.invalidate_changed_prefix()
                frozen_stamp = _stamp(flow.path)
                while progress.value['position'] < len(tasks) and time.monotonic() < end:
                    index = progress.value['position']
                    task = tasks[index]
                    item = state['tasks'][task['task_id']]
                    if not item['accepted']:
                        raise ProductionError('PRODUCTION_INTEGRITY')
                    check = _verify_accepted(task, item, driver)
                    if _stamp(flow.path) != frozen_stamp:
                        break
                    progress.value['checks'].append(check)
                    progress.value['position'] += 1
                    # Reverification is side-effect free: a crash can safely
                    # repeat at most this bounded chunk, never a paid dispatch.
                    if progress.value['position'] % 32 == 0:
                        progress.save()
                progress.save()
                current = flow.inspect()
                if current['state_digest'] != state['state_digest']:
                    progress.work()
                    continue
                if progress.value['position'] == len(tasks):
                    progress.invalidate_changed_prefix()
                    if progress.value['position'] == len(tasks):
                        progress.value.update(phase='complete', position=0, workflow_digest=None, checks=[])
                        progress.save()
                        return {'status': 'machine_accepted', 'plan_digest': plan['plan_digest'],
                                'human_review': 'not_reviewed'}
                break
            progressed = repair_wait = False
            visited = 0
            while visited < len(tasks) and time.monotonic() < end:
                index = progress.value['position'] % len(tasks)
                task = tasks[index]
                task_id = task['task_id']
                visited += 1
                touched = False
                if not state['tasks'][task_id]['accepted']:
                    state = flow.inspect()
                    if state['paused']:
                        return {'status': 'paused', 'plan_digest': plan['plan_digest']}
                    if not state['tasks'][task_id]['accepted'] and all(state['tasks'][k]['accepted'] for k in graph[task_id]):
                        for dependency in graph[task_id]:
                            _verify_accepted(by_id[dependency], state['tasks'][dependency], driver)
                        if time.monotonic() >= end:
                            break
                        touched = True
                        result = _run_task(task_id, task, state, plan=plan, flow=flow, ledger=ledger,
                                           driver=driver, v2=v2, end=end)
                        if isinstance(result, dict):
                            return result
                        progressed |= result == 'progressed'
                        repair_wait |= result == 'repair_wait'
                        state = flow.inspect()
                progress.value['position'] = (index + 1) % len(tasks)
                if touched:
                    progress.save()
            progress.save()
            state = flow.inspect()
            if all(t['accepted'] for t in state['tasks'].values()):
                progress.verify(state)
                continue
            if visited == len(tasks) and not progressed:
                return {'status': 'waiting_repair_drain' if repair_wait else 'waiting',
                        'plan_digest': plan['plan_digest']}
        return {'status': 'paused_batch', 'plan_digest': plan['plan_digest']}
