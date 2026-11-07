"""Bounded indexed coordination and explicit observation-vs-delivery verification."""
import json
from pathlib import Path
import time
import uuid
from contextlib import nullcontext

from fsf.core.asset_library import _no_links
from fsf.core.edit_store import edit_lock
from fsf.production.contracts import ProductionError, digest, integer, seal
from fsf.production.control import file_identity
from fsf.production.runner import validate_plan
from fsf.production.store import _load, _dump


def cancel_store_request(plan, *, store, driver, request_id, epoch, seconds=30):
    """Persist intent before provider contact; stopping never releases unknown fees."""
    validate_plan(plan)
    integer(seconds, 1, 5400)
    integer(epoch, 1)
    execution = store.execution(plan)
    row = store.attempt(request_id)
    if row['execution'] != execution or row['epoch'] != epoch:
        raise ProductionError('PRODUCTION_STALE_ATTEMPT')
    if row['status'] in {'machine_accepted', 'rejected', 'failed', 'cancelled'}:
        raise ProductionError('PRODUCTION_STATE_CONFLICT')
    prior_binding = store.db.execute("SELECT payload FROM events WHERE execution=? AND kind='driver_bound'",
                                     (execution,)).fetchone()
    config = getattr(driver, 'config', {}) or {'driver_class': type(driver).__qualname__}
    quality_binding = _load(prior_binding[0]).get('quality_binding') if prior_binding else None
    if quality_binding is not None:
        config = {'driver': config, 'quality_binding': quality_binding}
    store.bind_driver(execution, config)
    task = _load(store.task(execution, row['task'])['value'])
    store.cancel_intent(request_id, epoch)
    deadline = time.monotonic()+seconds
    bounded = driver.bounded_execution(deadline) if hasattr(driver,'bounded_execution') else nullcontext()
    with bounded:
        try:
            result = driver.cancel(task, request_id)
        except Exception:
            store.cancel_observed(request_id, epoch, 'unknown')
            raise ProductionError('PRODUCTION_CANCEL_UNKNOWN') from None
    return store.cancel_observed(request_id, epoch, result)


def _verify(task, row, driver, *, store=None, deadline=None):
    accepted = _load(row['accepted'])
    if isinstance(accepted, dict) and 'publication_request' in accepted:
        from fsf.production.publication import verify_publication
        return verify_publication(store, row, accepted, deadline)
    if callable(getattr(driver,'verify_observed',None)):
        from fsf.production.control import ArtifactObservation
        challenge = object()
        artifact,evidence,observation = driver.verify_observed(task,row['request'],challenge=challenge)
        if not isinstance(observation,ArtifactObservation):
            raise ProductionError('PRODUCTION_OBSERVATION_INVALID')
        actual = observation.consume(artifact,challenge)
    else:
        artifact,evidence = driver.verify(task,row['request'])
        actual = file_identity(artifact)
    technical = accepted.get('technical_evidence', accepted)
    if evidence != technical or actual['sha256'] != accepted['artifact_sha256']:
        raise ProductionError('PRODUCTION_SOURCE_CHANGED')
    return str(Path(artifact).absolute()), accepted


def _expired_call(exc, deadline):
    return isinstance(exc,ProductionError) and exc.code=='PRODUCTION_DEADLINE_EXCEEDED' and time.monotonic()>=deadline


def run_store_batch(plan, *, store, driver, seconds=300, delivery=False, quality_gate=None):
    validate_plan(plan)
    integer(seconds, 1, 5400)
    if type(delivery) is not bool:
        raise ProductionError('PRODUCTION_INVALID')
    execution = store.execution(plan)
    config = getattr(driver, 'config', {})
    config = config if isinstance(config, dict) else {}
    if config.get('schema_version') == 'fsf_production_command_driver_v2':
        if any(config['settlement'][name] != store.authorization[name] for name in ('currency','unit_scale')):
            raise ProductionError('PRODUCTION_SETTLEMENT_INVALID')
    config = config or {'driver_class': type(driver).__qualname__}
    quality_binding = None
    if quality_gate is not None:
        from fsf.production.segment_quality import LocalSegmentGate
        if not isinstance(quality_gate, LocalSegmentGate) or quality_gate.config['policy_digest'] != plan['policy_digest']:
            raise ProductionError('PRODUCTION_QUALITY_CONFIG')
        if delivery:
            raise ProductionError('PRODUCTION_QUALITY_UNQUALIFIED')
        quality_binding = digest(quality_gate.config)
        config = {'driver': config, 'quality_binding': quality_binding}
    binding = store.bind_driver(execution, config, quality_binding=quality_binding)
    deadline = time.monotonic() + seconds
    db = store.db
    files = driver.execution_files() if hasattr(driver,'execution_files') else nullcontext()
    bounded = (driver.cooperative_execution(deadline) if hasattr(driver,'cooperative_execution') else
               driver.bounded_execution(deadline) if hasattr(driver,'bounded_execution') else nullcontext())
    with edit_lock(store.root / '.parent-run'), files, bounded:
        with store.transaction():
            db.execute("UPDATE tasks SET status='rejected' WHERE execution=? AND status='repair_wait'", (execution,))
        active_position = -1
        while time.monotonic() < deadline:
            if db.execute('SELECT paused FROM executions WHERE id=?', (execution,)).fetchone()[0]:
                return {'status': 'paused', 'plan_digest': plan['plan_digest']}
            row = store.next_ready(execution)
            if row is None:
                row = db.execute("""SELECT t.*,d.value FROM tasks t INDEXED BY task_order
                    JOIN task_documents d ON d.execution=t.execution AND d.id=t.id WHERE t.execution=? AND t.ordinal>?
                    AND t.status IN ('reserved','submitted','running','unknown','produced','verified')
                    ORDER BY t.ordinal LIMIT 1""", (execution, active_position)).fetchone()
                if row is not None:
                    active_position = row['ordinal']
            if row is None:
                break
            row = dict(row)
            task = _load(row['value'])
            expired=False
            for dep in task['depends_on']:
                source = store.task(execution, dep)
                try:
                    _verify(_load(source['value']), source, driver, store=store, deadline=deadline)
                except ProductionError as exc:
                    if not _expired_call(exc,deadline):raise
                    expired=True
                    break
            if expired or time.monotonic() >= deadline:
                break
            if row['status'] in {'ready', 'failed', 'cancelled', 'rejected'}:
                try:
                    attempt = store.claim(execution, row['id'], owner='parent', seconds=max(1, int(deadline - time.monotonic())))
                except ProductionError as exc:
                    if exc.code == 'PRODUCTION_REPAIR_WAIT':
                        with store.transaction():
                            db.execute("UPDATE tasks SET status='repair_wait' WHERE execution=? AND id=?", (execution, row['id']))
                        continue
                    if exc.code != 'PRODUCTION_FORECAST_REQUIRED':
                        raise
                    # An explicit bound on all unaccepted attempts, not a new authorization.
                    remaining = store.remaining_forecast()
                    store.forecast(remaining_worst_units=remaining)
                    attempt = store.claim(execution, row['id'], owner='parent', seconds=max(1, int(deadline - time.monotonic())))
            else:
                attempt = store.attempt(row['request'])
            request, epoch = attempt['request'], attempt['epoch']
            if quality_gate is not None and attempt['status'] in {'produced', 'verified'}:
                from fsf.production.publication import publication_intent, stage_acceptance
                if publication_intent(store, request) is not None:
                    try:
                        stage_acceptance(store, request, epoch, deadline)
                    except ProductionError as exc:
                        if not _expired_call(exc, deadline):
                            raise
                        break
                    continue
            outbox = db.execute('SELECT state FROM outbox WHERE request=?', (request,)).fetchone()[0]
            if outbox == 'prepared':
                if time.monotonic() >= deadline:
                    break
                admission=driver.admit_submission() if hasattr(driver,'admit_submission') else nullcontext(True)
                with admission as admitted:
                    if type(admitted) is not bool:raise ProductionError('PRODUCTION_PROVIDER_INVALID')
                    if not admitted:break
                    if attempt['expires'] <= time.time():
                        store.renew_prepared(request, epoch, owner=attempt['owner'], seconds=max(1, int(deadline - time.monotonic())))
                    store.mark_sending(request, epoch)
                    try:
                        driver.submit(task, request)
                    except Exception:
                        store.observe(request, epoch, 'unknown')
                        store.settle(request)
                        raise ProductionError('PRODUCTION_RECONCILE_REQUIRED') from None
            if time.monotonic() >= deadline:
                break
            # Sending or observed requests only observe/reconcile, never re-submit.
            try:
                result = driver.observe(task, request)
            except ProductionError as exc:
                if not _expired_call(exc,deadline):raise
                break
            if not isinstance(result, dict) or result.get('status') not in {'running', 'produced', 'unknown', 'failed', 'cancelled'}:
                raise ProductionError('PRODUCTION_PROVIDER_INVALID')
            store.observe(request, epoch, result['status'])
            store.settle(request, settled_units=result.get('settled_units'))
            if result['status'] == 'unknown':
                return {'status': 'unknown', 'request_id': request, 'plan_digest': plan['plan_digest']}
            if result['status'] != 'produced':
                continue
            if time.monotonic() >= deadline:
                break
            try:
                artifact, evidence = driver.verify(task, request)
            except ProductionError as exc:
                if not _expired_call(exc,deadline):raise
                break
            if evidence.get('policy_digest') != plan['policy_digest']:
                raise ProductionError('PRODUCTION_POLICY_CHANGED')
            if quality_gate is not None and evidence.get('verdict') == 'accept':
                quality = quality_gate.assess(store, task, request, epoch, artifact, evidence, deadline)
                if quality['verdict'] == 'unknown':
                    return {'status': 'quality_unknown', 'request_id': request, 'plan_digest': plan['plan_digest']}
                evidence = seal({**evidence, 'verdict': quality['verdict'],
                                 'technical_evidence': evidence, 'segment_quality': quality}, 'report_digest')
            if evidence.get('verdict') == 'reject':
                store.reject(request, epoch, evidence,
                             artifact=artifact if quality_gate is not None else None)
            elif evidence.get('verdict') == 'unknown':
                return {'status': 'quality_unknown', 'plan_digest': plan['plan_digest']}
            else:
                if quality_gate is None:
                    store.accept(request, epoch, artifact, evidence)
                else:
                    from fsf.production.publication import stage_acceptance
                    try:
                        stage_acceptance(store, request, epoch, deadline, artifact=artifact, evidence=evidence)
                    except ProductionError as exc:
                        if not _expired_call(exc, deadline):
                            raise
                        break

        if db.execute("SELECT 1 FROM tasks WHERE execution=? AND status<>'machine_accepted' LIMIT 1", (execution,)).fetchone():
            return {'status': 'paused_batch' if time.monotonic() >= deadline else 'waiting', 'plan_digest': plan['plan_digest']}
        current = db.execute('SELECT revision FROM executions WHERE id=?', (execution,)).fetchone()[0]
        sweep = db.execute("SELECT * FROM verification_runs WHERE execution=? AND phase='verify' ORDER BY rowid DESC LIMIT 1", (execution,)).fetchone()
        # Delivery never inherits a partial observation window.
        if delivery or not sweep or sweep['revision'] != current or sweep['driver'] != binding:
            run_id = uuid.uuid4().hex
            with store.transaction():
                db.execute("UPDATE verification_runs SET phase='invalid' WHERE execution=? AND phase='verify'", (execution,))
                db.execute('INSERT INTO verification_runs(id,execution,revision,driver,policy,phase,kind) VALUES(?,?,?,?,?,?,?)',
                           (run_id, execution, current, binding, plan['policy_digest'], 'verify',
                            'delivery_validation' if delivery else 'completed_observation_sweep'))
            position = 0
        else:
            run_id, position = sweep['id'], sweep['position']
        while time.monotonic() < deadline:
            rows = db.execute('''SELECT t.*,d.value FROM tasks t JOIN task_documents d
                ON d.execution=t.execution AND d.id=t.id WHERE t.execution=? AND t.ordinal>=?
                ORDER BY t.ordinal LIMIT 32''', (execution, position)).fetchall()
            if not rows:
                with store.transaction():
                    if db.execute('SELECT revision FROM executions WHERE id=?', (execution,)).fetchone()[0] != current:
                        raise ProductionError('PRODUCTION_SOURCE_CHANGED')
                    db.execute("UPDATE verification_runs SET phase='complete' WHERE id=?", (run_id,))
                return {'status': 'machine_accepted', 'plan_digest': plan['plan_digest'],
                        'verification_kind': 'delivery_validation' if delivery else 'completed_observation_sweep',
                        'delivery_ready': delivery, 'snapshot_consistency': 'bounded_observations_not_atomic_snapshot',
                        'file_check_mode': getattr(driver,'last_file_check_mode','driver_defined'),
                        'human_review': 'not_reviewed'}
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                row = store._task_row(row)
                try:
                    artifact, evidence = _verify(_load(row['value']), row, driver, store=store, deadline=deadline)
                except ProductionError as exc:
                    if not _expired_call(exc,deadline):raise
                    break
                if delivery:
                    # Re-hash each current external authority within this window.
                    # Consumers must still revalidate before mutable external delivery.
                    _no_links(Path(artifact))
                with store.transaction():
                    if db.execute('SELECT revision FROM executions WHERE id=?', (execution,)).fetchone()[0] != current:
                        raise ProductionError('PRODUCTION_SOURCE_CHANGED')
                    db.execute('INSERT INTO verification_items(run,task,ordinal,request,evidence,artifact_sha256,observed) VALUES(?,?,?,?,?,?,?)',
                               (run_id, row['id'], row['ordinal'], row['request'], _dump(evidence), evidence['artifact_sha256'], time.time()))
                    position = row['ordinal'] + 1
                    db.execute('UPDATE verification_runs SET position=? WHERE id=?', (position, run_id))
        return {'status': 'needs_validation' if delivery else 'paused_batch', 'plan_digest': plan['plan_digest']}
