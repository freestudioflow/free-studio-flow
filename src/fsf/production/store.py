"""Indexed single-host production authority. External calls never hold a transaction."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from fsf.core.asset_library import _root, _no_links
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, key, publish, seal
from fsf.production.ledger import Ledger
from fsf.production.runner import validate_plan


DDL = '''
CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1), uuid TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version=1), activation TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, cost_revision INTEGER NOT NULL DEFAULT 0,
 used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0), frames INTEGER NOT NULL DEFAULT 0 CHECK(frames>=0));
CREATE TABLE authorization(id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL UNIQUE,
 forecast TEXT, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE TABLE authorization_documents(id INTEGER PRIMARY KEY REFERENCES authorization(id), value TEXT NOT NULL);
CREATE TABLE executions(id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL UNIQUE,
 binding TEXT NOT NULL, policy TEXT NOT NULL, driver TEXT,
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE execution_plans(execution TEXT PRIMARY KEY REFERENCES executions(id), value TEXT NOT NULL);
CREATE TABLE tasks(execution TEXT NOT NULL REFERENCES executions(id), id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, shot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
 remaining INTEGER NOT NULL CHECK(remaining>=0), epoch INTEGER NOT NULL DEFAULT 0,
 request TEXT, accepted TEXT, artifact TEXT,
 PRIMARY KEY(execution,id));
CREATE UNIQUE INDEX task_order ON tasks(execution,ordinal);
CREATE INDEX ready_tasks ON tasks(execution,status,ordinal)
 WHERE remaining=0 AND status IN ('ready','failed','cancelled','rejected');
CREATE INDEX task_shot ON tasks(execution,shot);
CREATE TABLE task_documents(execution TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(execution,id), FOREIGN KEY(execution,id) REFERENCES tasks(execution,id));
CREATE TABLE dependencies(execution TEXT NOT NULL, task TEXT NOT NULL, parent TEXT NOT NULL,
 PRIMARY KEY(execution,task,parent), FOREIGN KEY(execution,task) REFERENCES tasks(execution,id),
 FOREIGN KEY(execution,parent) REFERENCES tasks(execution,id));
CREATE INDEX dependent_tasks ON dependencies(execution,parent,task);
CREATE TABLE attempts(request TEXT NOT NULL UNIQUE, execution TEXT NOT NULL, task TEXT NOT NULL,
 epoch INTEGER NOT NULL, owner TEXT NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL,
 invalidated INTEGER NOT NULL DEFAULT 0, evidence TEXT, artifact TEXT,
 outbox_state TEXT NOT NULL DEFAULT 'prepared' CHECK(outbox_state IN ('prepared','sending','observed')),
 cancel_state TEXT,
 charge_authorization INTEGER CHECK(charge_authorization=1) REFERENCES authorization(id),
 charge_shot TEXT, charge_segment TEXT, charge_round INTEGER, charge_route TEXT,
 charge_worst INTEGER, charge_settled INTEGER, charge_frames INTEGER,
 charge_status TEXT, charge_binding TEXT, charge_essential INTEGER,
 CHECK(CASE WHEN charge_authorization IS NULL THEN
   charge_shot IS NULL AND charge_segment IS NULL AND charge_round IS NULL AND charge_route IS NULL
   AND charge_worst IS NULL AND charge_settled IS NULL AND charge_frames IS NULL
   AND charge_status IS NULL AND charge_binding IS NULL AND charge_essential IS NULL
 ELSE charge_route IS NOT NULL AND charge_worst IS NOT NULL AND charge_worst>=0
   AND (charge_settled IS NULL OR (charge_settled>=0 AND charge_settled<=charge_worst))
   AND charge_frames IS NOT NULL AND charge_frames>=0
   AND charge_status IS NOT NULL AND charge_status IN ('active','unknown','settled')
   AND charge_binding IS NOT NULL AND charge_essential IS NOT NULL AND charge_essential IN (0,1)
   AND ((charge_frames=0 AND charge_shot IS NULL AND charge_segment IS NULL AND charge_round IS NULL)
     OR (charge_frames>0 AND charge_shot IS NOT NULL AND charge_segment IS NOT NULL
       AND charge_round IS NOT NULL AND charge_round BETWEEN 1 AND 3)) END),
 PRIMARY KEY(execution,task,epoch), FOREIGN KEY(execution,task) REFERENCES tasks(execution,id)) WITHOUT ROWID;
CREATE TABLE attempt_results(request TEXT PRIMARY KEY REFERENCES attempts(request),
 evidence TEXT NOT NULL, artifact TEXT) WITHOUT ROWID;
CREATE UNIQUE INDEX generation_slot ON attempts(charge_authorization,execution,charge_shot,charge_segment,charge_round)
 WHERE charge_frames>0;
CREATE VIEW reservations AS SELECT request,charge_authorization AS authorization,execution,
 charge_shot AS shot,charge_segment AS segment,charge_round AS round,charge_route AS route,
 charge_worst AS worst,charge_settled AS settled,charge_frames AS frames,charge_status AS status,
 charge_binding AS binding,charge_essential AS essential FROM attempts WHERE charge_authorization IS NOT NULL;
CREATE TRIGGER reservation_insert INSTEAD OF INSERT ON reservations BEGIN
 SELECT CASE WHEN NEW.authorization IS NULL OR NEW.route IS NULL OR NEW.worst IS NULL
   OR NEW.frames IS NULL OR NEW.status IS NULL OR NEW.binding IS NULL OR NEW.essential IS NULL
   OR NOT EXISTS(SELECT 1 FROM attempts WHERE request=NEW.request AND execution=NEW.execution
     AND charge_authorization IS NULL) THEN RAISE(ABORT,'invalid reservation binding') END;
 UPDATE attempts SET charge_authorization=NEW.authorization,charge_shot=NEW.shot,charge_segment=NEW.segment,
   charge_round=NEW.round,charge_route=NEW.route,charge_worst=NEW.worst,charge_settled=NEW.settled,
   charge_frames=NEW.frames,charge_status=NEW.status,charge_binding=NEW.binding,charge_essential=NEW.essential
   WHERE request=NEW.request;
END;
CREATE TRIGGER reservation_update INSTEAD OF UPDATE ON reservations BEGIN
 SELECT CASE WHEN NEW.request IS NOT OLD.request OR NEW.execution IS NOT OLD.execution
   OR NEW.authorization IS NOT OLD.authorization OR NEW.shot IS NOT OLD.shot OR NEW.segment IS NOT OLD.segment
   OR NEW.round IS NOT OLD.round OR NEW.route IS NOT OLD.route OR NEW.worst IS NOT OLD.worst
   OR NEW.frames IS NOT OLD.frames OR NEW.binding IS NOT OLD.binding OR NEW.essential IS NOT OLD.essential
   THEN RAISE(ABORT,'immutable reservation binding') END;
 UPDATE attempts SET charge_status=NEW.status,charge_settled=NEW.settled WHERE request=OLD.request;
END;
CREATE TRIGGER funded_attempt_delete BEFORE DELETE ON attempts WHEN OLD.charge_authorization IS NOT NULL BEGIN
 SELECT RAISE(ABORT,'funded attempt cannot be deleted');
END;
CREATE TRIGGER funded_attempt_identity BEFORE UPDATE OF request,execution,task,epoch,charge_authorization,
 charge_shot,charge_segment,charge_round,charge_route,charge_worst,charge_frames,charge_binding,charge_essential ON attempts
 WHEN OLD.charge_authorization IS NOT NULL AND
 (NEW.request IS NOT OLD.request OR NEW.execution IS NOT OLD.execution OR NEW.task IS NOT OLD.task
  OR NEW.epoch IS NOT OLD.epoch OR NEW.charge_authorization IS NOT OLD.charge_authorization
  OR NEW.charge_shot IS NOT OLD.charge_shot OR NEW.charge_segment IS NOT OLD.charge_segment
  OR NEW.charge_round IS NOT OLD.charge_round OR NEW.charge_route IS NOT OLD.charge_route
  OR NEW.charge_worst IS NOT OLD.charge_worst OR NEW.charge_frames IS NOT OLD.charge_frames
  OR NEW.charge_binding IS NOT OLD.charge_binding OR NEW.charge_essential IS NOT OLD.charge_essential) BEGIN
 SELECT RAISE(ABORT,'immutable funded attempt');
END;
CREATE TABLE route_totals(route TEXT PRIMARY KEY, used INTEGER NOT NULL CHECK(used>=0));
CREATE TABLE rounds(execution TEXT NOT NULL REFERENCES executions(id), shot TEXT NOT NULL,
 round INTEGER NOT NULL CHECK(round BETWEEN 1 AND 3), PRIMARY KEY(execution,shot));
CREATE VIEW outbox AS SELECT request,outbox_state AS state,cancel_state FROM attempts;
CREATE TRIGGER outbox_insert INSTEAD OF INSERT ON outbox BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM attempts WHERE request=NEW.request) THEN RAISE(ABORT,'missing intent') END;
 UPDATE attempts SET outbox_state=NEW.state,cancel_state=NEW.cancel_state WHERE request=NEW.request;
END;
CREATE TRIGGER outbox_update INSTEAD OF UPDATE ON outbox BEGIN
 UPDATE attempts SET outbox_state=NEW.state,cancel_state=NEW.cancel_state WHERE request=OLD.request;
END;
CREATE TABLE events(seq INTEGER PRIMARY KEY, execution TEXT, task TEXT,
 request TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE verification_runs(id TEXT PRIMARY KEY, execution TEXT NOT NULL REFERENCES executions(id),
 revision INTEGER NOT NULL, driver TEXT NOT NULL, policy TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
 phase TEXT NOT NULL CHECK(phase IN ('verify','complete','invalid')), kind TEXT NOT NULL);
CREATE INDEX current_verification ON verification_runs(execution,phase);
CREATE TABLE verification_items(run TEXT NOT NULL REFERENCES verification_runs(id), task TEXT NOT NULL,
 ordinal INTEGER NOT NULL, request TEXT NOT NULL, evidence TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
 observed REAL NOT NULL, PRIMARY KEY(run,task), UNIQUE(run,ordinal));
'''


def _dump(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _load(value):
    return json.loads(value) if value is not None else None


def _add(*values):
    return integer(sum(values))


def authority_registry(root):
    """Operator-scoped local registry, not a cross-host or adversarial lease service."""
    path = Path(os.environ.get('FSF_PRODUCTION_AUTHORITY_ROOT', str(Path(root).parent / '.fsf-production-authorities')))
    path.mkdir(parents=True, exist_ok=True)
    return _root(path)


def _registry_value(root, auth, store_uuid):
    return seal({'schema_version': 'fsf_production_authority_v1', 'authorization_digest': auth,
                 'store_uuid': store_uuid, 'root_digest': digest(str(Path(root).resolve()))}, 'authority_digest')


class ProductionStore:
    """Cooperating local writers; receipts are checksums, not rollback-proof signatures."""

    def __init__(self, root, *, recovery=False):
        self.root = _root(root)
        self.path = self.root / 'state.sqlite3'
        _no_links(self.path)
        descriptor = read_json(self.root / 'store-manifest.json')
        from fsf.production.contracts import validate
        validate(descriptor, 'fsf_production_store_manifest_v1', 'manifest_digest')
        expected = {'schema_version', 'store_uuid', 'store_format', 'database', 'sqlite_profile',
                    'authorization_digest', 'activation_state', 'authority_epoch',
                    'migration_receipt_digest', 'manifest_digest'}
        if (set(descriptor) != expected or seal(descriptor, 'manifest_digest') != descriptor
                or descriptor['schema_version'] != 'fsf_production_store_manifest_v1'
                or descriptor['store_format'] != 'fsf_production_store_v1'
                or descriptor['database'] != 'state.sqlite3' or descriptor['sqlite_profile'] != 'delete_full_v1'):
            raise ProductionError('PRODUCTION_STORE_VERSION')
        try:
            self.db = self._connect(self.path)
            meta = self.db.execute('SELECT * FROM meta WHERE id=1').fetchone()
            auth = self.db.execute('SELECT * FROM authorization WHERE id=1').fetchone()
            if (meta['version'] != 1 or meta['uuid'] != descriptor['store_uuid']
                    or auth['digest'] != descriptor['authorization_digest']
                    or descriptor['authority_epoch'] != 1):
                raise ProductionError('PRODUCTION_AUTHORITY_CONFLICT')
            if meta['activation'] != 'active' and not recovery:
                raise ProductionError('PRODUCTION_MIGRATION_RECOVERY_REQUIRED')
            document = self.db.execute('SELECT value FROM authorization_documents WHERE id=1').fetchone()
            self.authorization = Ledger._authorization(_load(document['value']))
            if self.authorization['authorization_digest'] != auth['digest']:
                raise ProductionError('PRODUCTION_STORE_CORRUPT')
            registry_file = authority_registry(self.root) / (auth['digest'] + '.json')
            if not registry_file.is_file() or read_json(registry_file) != _registry_value(self.root, auth['digest'], meta['uuid']):
                raise ProductionError('PRODUCTION_AUTHORITY_CONFLICT')
            self.descriptor = descriptor
            self._result_table = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempt_results'").fetchone() is not None
            auth_type = next(row['type'] for row in self.db.execute('PRAGMA table_info(reservations)')
                             if row['name'] == 'authorization')
            if auth_type not in {'INTEGER', 'TEXT'}:
                raise ProductionError('PRODUCTION_STORE_VERSION')
            # The singleton ID refers to the verified immutable digest above.
            # Older stores retain their digest-valued foreign key unchanged.
            self._authorization_key = 1 if auth_type == 'INTEGER' else auth['digest']
            self._inline_charges = self.db.execute(
                "SELECT type FROM sqlite_master WHERE name='reservations'").fetchone()['type'] == 'view'
        except ProductionError:
            if hasattr(self, 'db'):
                self.db.close()
            raise
        except (sqlite3.Error, KeyError, TypeError):
            if hasattr(self, 'db'):
                self.db.close()
            raise ProductionError('PRODUCTION_STORE_CORRUPT') from None

    @staticmethod
    def _connect(path, *, creating=False):
        for suffix in ('-journal', '-wal', '-shm'):
            related = Path(str(path) + suffix)
            if related.exists():
                _no_links(related)
        db = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        db.row_factory = sqlite3.Row
        if creating:
            db.execute('PRAGMA page_size=4096')
        if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
            db.close()
            raise ProductionError('PRODUCTION_STORE_VERSION')
        for statement in ('PRAGMA synchronous=FULL', 'PRAGMA foreign_keys=ON',
                          'PRAGMA cache_size=-32768', 'PRAGMA mmap_size=0', 'PRAGMA locking_mode=NORMAL'):
            db.execute(statement)
        return db

    @classmethod
    def create(cls, root, plan, authorization, *, execution_id=None, _activation='active'):
        root = _root(root)
        validate_plan(plan)
        auth = Ledger._authorization(authorization)
        for name in ('unit_scale', 'total_units', 'cleanup_units', 'fixed_delivery_units', 'max_generated_frames'):
            integer(auth[name])
        for amount in auth['route_limits'].values():
            integer(amount)
        if _activation not in ('active', 'building'):
            raise ProductionError('PRODUCTION_INVALID')
        execution_id = key(execution_id or plan['production_id'])
        cls._validate_scope(plan, auth)
        if auth['cleanup_units'] < max((auth['total_units'] + 9) // 10, auth['fixed_delivery_units']):
            raise ProductionError('PRODUCTION_BUDGET_INVALID')
        if sum(auth['route_limits'].values()) > auth['total_units']:
            raise ProductionError('PRODUCTION_BUDGET_INVALID')
        registry_file = authority_registry(root) / (auth['authorization_digest'] + '.json')
        with edit_lock(registry_file), edit_lock(root / '.store-create'):
            if registry_file.exists():
                raise ProductionError('PRODUCTION_AUTHORITY_CONFLICT')
            if (root / 'state.sqlite3').exists() or (root / 'store-manifest.json').exists():
                raise ProductionError('PRODUCTION_TARGET_EXISTS')
            store_id = uuid.uuid4().hex
            db = cls._connect(root / 'state.sqlite3', creating=True)
            try:
                db.executescript(DDL)
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO meta(id,uuid,version,activation) VALUES(1,?,1,?)', (store_id, _activation))
                db.execute('INSERT INTO authorization(id,digest) VALUES(1,?)', (auth['authorization_digest'],))
                db.execute('INSERT INTO authorization_documents(id,value) VALUES(1,?)', (_dump(auth),))
                db.executemany('INSERT INTO route_totals(route,used) VALUES(?,0)', [(route,) for route in auth['route_limits']])
                cls._insert_execution(db, execution_id, plan, auth)
                db.execute('COMMIT')
                publish(root / 'store-manifest.json', seal({
                    'schema_version': 'fsf_production_store_manifest_v1', 'store_uuid': store_id,
                    'store_format': 'fsf_production_store_v1', 'database': 'state.sqlite3',
                    'sqlite_profile': 'delete_full_v1', 'authorization_digest': auth['authorization_digest'],
                    'activation_state': 'active', 'authority_epoch': 1, 'migration_receipt_digest': None,
                }, 'manifest_digest'))
                publish(registry_file, _registry_value(root, auth['authorization_digest'], store_id))
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
            finally:
                db.close()
        return cls(root, recovery=_activation != 'active')

    @staticmethod
    def _validate_scope(plan, auth):
        if plan['production_id'] != auth['production_id']:
            raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
        if auth['schema_version'] == 'fsf_production_budget_v2' and plan['plan_digest'] != auth['plan_digest']:
            raise ProductionError('PRODUCTION_FORECAST_PLAN')
        shots = set(auth['logical_shot_ids'])
        segments = {shot: set(values) for shot, values in auth.get('segment_ids_by_shot', {}).items()}
        for task in plan['tasks']:
            if task['route'] not in auth['route_limits'] or task['logical_shot_id'] not in shots:
                raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
            if (task['generated_frames'] and auth['schema_version'] == 'fsf_production_budget_v2'
                    and task['segment_id'] not in segments[task['logical_shot_id']]):
                raise ProductionError('PRODUCTION_SEGMENT_NOT_AUTHORIZED')

    @staticmethod
    def _insert_execution(db, execution, plan, auth):
        db.execute('INSERT INTO executions(id,plan_digest,binding,policy) VALUES(?,?,?,?)',
                   (execution, plan['plan_digest'], plan['binding_digest'], plan['policy_digest']))
        db.execute('INSERT INTO execution_plans(execution,value) VALUES(?,?)', (execution, _dump(plan)))
        db.executemany('INSERT INTO tasks(execution,id,ordinal,shot,remaining) VALUES(?,?,?,?,?)',
                       [(execution, task['task_id'], ordinal, task['logical_shot_id'], len(task['depends_on']))
                        for ordinal, task in enumerate(plan['tasks'])])
        db.executemany('INSERT INTO task_documents(execution,id,value) VALUES(?,?,?)',
                       [(execution, task['task_id'], _dump(task)) for task in plan['tasks']])
        db.executemany('INSERT INTO dependencies(execution,task,parent) VALUES(?,?,?)',
                       [(execution, task['task_id'], parent) for task in plan['tasks'] for parent in task['depends_on']])
        db.executemany('INSERT INTO rounds(execution,shot,round) VALUES(?,?,1)',
                       [(execution, shot) for shot in auth['logical_shot_ids']])

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, *, seconds=10):
        end = time.monotonic() + min(10, max(0, seconds))
        while True:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise ProductionError('PRODUCTION_STORE_CORRUPT') from None
                if time.monotonic() >= end:
                    raise ProductionError('PRODUCTION_STORE_BUSY') from None
                time.sleep(min(0.05, max(0, end - time.monotonic())))
        try:
            yield
            while True:
                try:
                    self.db.execute('COMMIT')
                    break
                except sqlite3.OperationalError as exc:
                    # DELETE journals allow readers during the write phase, but
                    # publishing needs their shared locks to drain. A busy
                    # COMMIT leaves this transaction active and may be retried.
                    if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                        raise
                    if time.monotonic() >= end:
                        raise ProductionError('PRODUCTION_STORE_BUSY') from None
                    time.sleep(min(0.05, max(0, end - time.monotonic())))
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(exc, sqlite3.IntegrityError):
                raise ProductionError('PRODUCTION_REQUEST_CONFLICT') from None
            if isinstance(exc, sqlite3.Error):
                raise ProductionError('PRODUCTION_STORE_CORRUPT') from None
            raise

    def _event(self, kind, execution=None, task=None, request=None, payload=None, *, execution_change=False):
        data = _dump(payload or {})
        if len(data.encode()) > 65536:
            raise ProductionError('PRODUCTION_INVALID')
        self.db.execute('UPDATE meta SET revision=revision+1 WHERE id=1')
        revision = self.db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
        self.db.execute('INSERT INTO events(execution,task,request,kind,payload,revision) VALUES(?,?,?,?,?,?)',
                        (execution, task, request, kind, data, revision))
        if execution_change:
            self.db.execute('UPDATE executions SET revision=revision+1 WHERE id=?', (execution,))
            self.db.execute("UPDATE verification_runs SET phase='invalid' WHERE execution=? AND phase='verify'", (execution,))

    def execution(self, plan):
        row = self.db.execute('''SELECT e.id,p.value FROM executions e JOIN execution_plans p ON p.execution=e.id
            WHERE e.plan_digest=?''', (plan['plan_digest'],)).fetchone()
        if not row or _load(row['value']) != plan:
            raise ProductionError('PRODUCTION_BINDING_CHANGED')
        return row['id']

    def bind_driver(self, execution, config):
        binding = digest(config)
        with self.transaction():
            row = self.db.execute('SELECT driver FROM executions WHERE id=?', (execution,)).fetchone()
            if not row or row['driver'] not in (None, binding):
                raise ProductionError('PRODUCTION_PROVIDER_CHANGED')
            if row['driver'] is None:
                self.db.execute('UPDATE executions SET driver=? WHERE id=?', (binding, execution))
                self._event('driver_bound', execution, payload={'digest': binding})
        return binding

    def _active(self, execution):
        meta = self.db.execute('SELECT activation FROM meta WHERE id=1').fetchone()
        row = self.db.execute('SELECT paused FROM executions WHERE id=?', (execution,)).fetchone()
        if meta['activation'] != 'active' or not row:
            raise ProductionError('PRODUCTION_AUTHORITY_CONFLICT')
        if row['paused']:
            raise ProductionError('PRODUCTION_NOT_READY')
        if self.db.execute('SELECT revoked FROM authorization WHERE id=1').fetchone()[0]:
            raise ProductionError('PRODUCTION_NOT_AUTHORIZED')
        Ledger._active_authority(self.authorization)
        if (self.root / 'submit-hold.json').exists():
            raise ProductionError('PRODUCTION_SUBMIT_HELD')

    def task(self, execution, task_id):
        row = self.db.execute('''SELECT t.*,d.value FROM tasks t JOIN task_documents d
            ON d.execution=t.execution AND d.id=t.id WHERE t.execution=? AND t.id=?''', (execution, task_id)).fetchone()
        if not row:
            raise ProductionError('PRODUCTION_NOT_FOUND')
        return self._task_row(row)

    def _task_row(self, row):
        """Hydrate a bounded task row without duplicating the stored report."""
        value = dict(row)
        if value['accepted'] is not None:
            marker = _load(value['accepted'])
            if isinstance(marker, str):
                attempt = self.attempt(value['request'])
                evidence = _load(attempt['evidence'])
                if (not isinstance(evidence, dict) or evidence.get('report_digest') != marker
                        or seal(evidence, 'report_digest') != evidence or value['artifact'] is not None):
                    raise ProductionError('PRODUCTION_STORE_CORRUPT')
                value.update(accepted=attempt['evidence'], artifact=attempt['artifact'])
        return value

    def next_ready(self, execution):
        # Each eligible status seeks its first ordinal in the ready index.
        # An IN query ordered only by ordinal can otherwise sort the full queue.
        candidates = []
        for status in ('ready', 'failed', 'cancelled', 'rejected'):
            row = self.db.execute('''SELECT id,ordinal FROM tasks INDEXED BY ready_tasks
                WHERE execution=? AND status=? AND remaining=0
                AND status IN ('ready','failed','cancelled','rejected') ORDER BY ordinal LIMIT 1''',
                                  (execution, status)).fetchone()
            if row is not None:
                candidates.append(row)
        if not candidates:
            return None
        return self.task(execution, min(candidates, key=lambda row: row['ordinal'])['id'])

    def attempt(self, request):
        row = self.db.execute('SELECT * FROM attempts WHERE request=?', (request,)).fetchone()
        if not row:
            raise ProductionError('PRODUCTION_NOT_FOUND')
        value = dict(row)
        if self._inline_charges and value['charge_authorization'] != self._authorization_key:
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        result = self.db.execute('SELECT evidence,artifact FROM attempt_results WHERE request=?',
                                 (request,)).fetchone() if self._result_table else None
        if result is not None:
            if value['evidence'] is not None or value['artifact'] is not None:
                raise ProductionError('PRODUCTION_STORE_CORRUPT')
            value.update(evidence=result['evidence'], artifact=result['artifact'])
        return value

    def _record_result(self, request, evidence, artifact=None):
        """Separate immutable reports from frequently updated lease/status rows."""
        data = _dump(evidence)
        if self._result_table:
            self.db.execute('INSERT INTO attempt_results(request,evidence,artifact) VALUES(?,?,?)',
                            (request, data, artifact))
            return _dump(evidence['report_digest']), None
        self.db.execute('UPDATE attempts SET evidence=?,artifact=? WHERE request=?', (data, artifact, request))
        return data, artifact

    def claim(self, execution, task_id, *, owner, seconds=300):
        key(owner)
        integer(seconds, 1, 5400)
        with self.transaction():
            self._active(execution)
            row = self.task(execution, task_id)
            task = _load(row['value'])
            if row['accepted'] or row['remaining']:
                raise ProductionError('PRODUCTION_NOT_READY')
            if row['request']:
                prior = self.attempt(row['request'])
                if prior['status'] not in ('failed', 'cancelled', 'rejected') and not prior['invalidated']:
                    raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
            count = self.db.execute('SELECT count(*) FROM attempts WHERE execution=? AND task=?',
                                    (execution, task_id)).fetchone()[0]
            if count >= 3:
                raise ProductionError('PRODUCTION_ATTEMPTS_EXCEEDED')
            auth = self.authorization
            round_value = self.db.execute('SELECT round FROM rounds WHERE execution=? AND shot=?',
                                          (execution, task['logical_shot_id'])).fetchone()[0]
            if row['request'] and task['generated_frames']:
                prior_res = self.db.execute('SELECT round FROM reservations WHERE request=?', (row['request'],)).fetchone()
                if prior_res and prior_res['round'] == round_value:
                    live = self.db.execute('''SELECT 1 FROM attempts a JOIN reservations r ON a.request=r.request
                        WHERE a.execution=? AND r.shot=? AND r.round=? AND a.task<>?
                        AND a.status IN ('reserved','submitted','running','unknown','produced','verified') LIMIT 1''',
                                           (execution, task['logical_shot_id'], round_value, task_id)).fetchone()
                    if live:
                        raise ProductionError('PRODUCTION_REPAIR_WAIT')
                    if round_value >= auth.get('max_repair_rounds', 3):
                        raise ProductionError('PRODUCTION_REPAIR_EXHAUSTED')
                    round_value += 1
                    self.db.execute('UPDATE rounds SET round=? WHERE execution=? AND shot=?',
                                    (round_value, execution, task['logical_shot_id']))
            worst, frames = task['worst_units'], task['generated_frames']
            meta = self.db.execute('SELECT used,frames,cost_revision FROM meta WHERE id=1').fetchone()
            route_used = self.db.execute('SELECT used FROM route_totals WHERE route=?', (task['route'],)).fetchone()[0]
            if (_add(meta['used'], worst, auth['cleanup_units']) > auth['total_units']
                    or _add(route_used, worst) > auth['route_limits'][task['route']]
                    or _add(meta['frames'], frames) > auth['max_generated_frames']):
                raise ProductionError('PRODUCTION_BUDGET_EXCEEDED')
            forecast = _load(self.db.execute('SELECT forecast FROM authorization WHERE id=1').fetchone()[0])
            if auth['schema_version'] == 'fsf_production_budget_v2' and (meta['used'] + worst) * 100 >= auth['total_units'] * 80:
                if not task.get('essential', True):
                    raise ProductionError('PRODUCTION_OPTIONAL_WORK_PAUSED')
                if not forecast or forecast['basis_revision'] != meta['cost_revision'] or forecast['remaining_worst_units'] < worst:
                    raise ProductionError('PRODUCTION_FORECAST_REQUIRED')
                if not forecast['affordable']:
                    raise ProductionError('PRODUCTION_FORECAST_UNAFFORDABLE')
            epoch = row['epoch'] + 1
            request = digest([self.descriptor['store_uuid'], execution, task_id, epoch])
            expires = math.ceil(time.time() + seconds)
            self.db.execute('INSERT INTO attempts(request,execution,task,epoch,owner,expires,status) VALUES(?,?,?,?,?,?,?)',
                            (request, execution, task_id, epoch, owner, expires, 'reserved'))
            slot = (task['logical_shot_id'], task['segment_id'], round_value) if frames else (None, None, None)
            self.db.execute('''INSERT INTO reservations(request,authorization,execution,shot,segment,round,route,worst,
                            frames,status,binding,essential) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (request, self._authorization_key, execution, *slot, task['route'], worst,
                             frames, 'active', task['input_digest'], int(task.get('essential', True))))
            self.db.execute("INSERT INTO outbox(request,state) VALUES(?,'prepared')", (request,))
            self.db.execute('UPDATE tasks SET epoch=?,request=?,status=? WHERE execution=? AND id=?',
                            (epoch, request, 'reserved', execution, task_id))
            self.db.execute('UPDATE meta SET used=used+?,frames=frames+?,cost_revision=cost_revision+1 WHERE id=1', (worst, frames))
            self.db.execute('UPDATE route_totals SET used=used+? WHERE route=?', (worst, task['route']))
            if forecast:
                forecast.update(basis_revision=meta['cost_revision'] + 1,
                                remaining_worst_units=max(0, forecast['remaining_worst_units'] - worst))
                forecast['affordable'] = meta['used'] + worst + forecast['remaining_worst_units'] + auth['cleanup_units'] <= auth['total_units']
                self.db.execute('UPDATE authorization SET forecast=? WHERE id=1', (_dump(forecast),))
            self._event('reserved', execution, task_id, request, execution_change=True)
        return self.attempt(request)

    def _fence(self, request, epoch):
        prior = self.attempt(request)
        row = self.task(prior['execution'], prior['task'])
        if type(epoch) is not int or prior['epoch'] != epoch or row['epoch'] != epoch or row['request'] != request:
            raise ProductionError('PRODUCTION_FENCED')
        return prior

    def mark_sending(self, request, epoch):
        with self.transaction():
            prior = self._fence(request, epoch)
            self._active(prior['execution'])
            if prior['expires'] <= time.time():
                raise ProductionError('PRODUCTION_LEASE_EXPIRED')
            updated = self.db.execute("UPDATE attempts SET outbox_state='sending' WHERE request=? AND outbox_state='prepared'", (request,))
            if updated.rowcount != 1:
                raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
            self.db.execute("UPDATE attempts SET status='submitted' WHERE request=?", (request,))
            self.db.execute("UPDATE tasks SET status='submitted' WHERE execution=? AND id=?", (prior['execution'], prior['task']))
            self._event('sending', prior['execution'], prior['task'], request, execution_change=True)

    def renew_prepared(self, request, epoch, *, owner, seconds=300):
        """Renew an unsent intent under its original fence; never release held fees."""
        key(owner)
        integer(seconds, 1, 5400)
        with self.transaction():
            prior = self._fence(request, epoch)
            self._active(prior['execution'])
            state = self.db.execute('SELECT state FROM outbox WHERE request=?', (request,)).fetchone()[0]
            if prior['owner'] != owner or state != 'prepared':
                raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
            self.db.execute('UPDATE attempts SET expires=? WHERE request=?', (math.ceil(time.time() + seconds), request))
            self._event('prepared_lease_renewed', prior['execution'], prior['task'], request)

    def cancel_intent(self, request, epoch):
        """Durable cancellation intent; the caller contacts the provider after commit."""
        with self.transaction():
            prior = self._fence(request, epoch)
            self.db.execute("UPDATE outbox SET cancel_state='requested' WHERE request=?", (request,))
            self._event('cancel_requested', prior['execution'], prior['task'], request)
        return {'request_id': request, 'cancel_state': 'requested', 'fees_released': False}

    def cancel_observed(self, request, epoch, result):
        if result not in {'requested', 'cancelled', 'unsupported', 'unknown'}:
            raise ProductionError('PRODUCTION_PROVIDER_INVALID')
        with self.transaction():
            prior = self._fence(request, epoch)
            self.db.execute('UPDATE outbox SET cancel_state=? WHERE request=?', (result, request))
            self._event('cancel_observed', prior['execution'], prior['task'], request, payload={'status': result})
        # A confirmed stop is not a billing receipt. Settlement remains separate.
        if result == 'cancelled':
            self.observe(request, epoch, 'cancelled')
            self.settle(request)
        return {'request_id': request, 'cancel_state': result, 'fees_released': False}

    def link_allocation(self, request, epoch, allocation_id):
        """Record a resource-pool correlation; this does not debit either ledger."""
        key(allocation_id)
        with self.transaction():
            prior = self._fence(request, epoch)
            links = self.db.execute("SELECT payload FROM events WHERE request=? AND kind='allocation_linked'", (request,)).fetchall()
            expected = {'allocation_id': allocation_id}
            if links:
                if any(_load(row[0]) != expected for row in links):
                    raise ProductionError('PRODUCTION_REQUEST_CONFLICT')
            else:
                self._event('allocation_linked', prior['execution'], prior['task'], request, payload=expected)
        return {'request_id': request, **expected, 'independent_resource_authority': True}

    def observe(self, request, epoch, status):
        if status not in {'running', 'produced', 'failed', 'cancelled', 'unknown'}:
            raise ProductionError('PRODUCTION_PROVIDER_INVALID')
        with self.transaction():
            prior = self._fence(request, epoch)
            if prior['status'] in {'machine_accepted', 'rejected', 'failed', 'cancelled'}:
                if prior['status'] != status:
                    raise ProductionError('PRODUCTION_STATE_CONFLICT')
                return
            if prior['status'] == status:
                return
            self.db.execute('UPDATE attempts SET status=? WHERE request=?', (status, request))
            self.db.execute('UPDATE tasks SET status=? WHERE execution=? AND id=?', (status, prior['execution'], prior['task']))
            if status != 'unknown':
                self.db.execute("UPDATE outbox SET state='observed' WHERE request=?", (request,))
            self._event(status, prior['execution'], prior['task'], request, execution_change=True)

    def settle(self, request, *, settled_units=None):
        with self.transaction():
            prior = self.db.execute('SELECT * FROM reservations WHERE request=?', (request,)).fetchone()
            if not prior:
                raise ProductionError('PRODUCTION_NOT_FOUND')
            if settled_units is None:
                if prior['status'] != 'settled' and prior['status'] != 'unknown':
                    self.db.execute("UPDATE reservations SET status='unknown' WHERE request=?", (request,))
                    self._event('fee_unknown', prior['execution'], request=request)
                return
            integer(settled_units, 0, prior['worst'])
            if prior['status'] == 'settled':
                if prior['settled'] != settled_units:
                    raise ProductionError('PRODUCTION_REQUEST_CONFLICT')
                return
            delta = settled_units - prior['worst']
            self.db.execute("UPDATE reservations SET status='settled',settled=? WHERE request=?", (settled_units, request))
            self.db.execute('UPDATE meta SET used=used+?,cost_revision=cost_revision+1 WHERE id=1', (delta,))
            self.db.execute('UPDATE route_totals SET used=used+? WHERE route=?', (delta, prior['route']))
            forecast = _load(self.db.execute('SELECT forecast FROM authorization WHERE id=1').fetchone()[0])
            if forecast:
                meta = self.db.execute('SELECT used,cost_revision FROM meta WHERE id=1').fetchone()
                forecast.update(basis_revision=meta['cost_revision'],
                                affordable=meta['used'] + forecast['remaining_worst_units'] + self.authorization['cleanup_units'] <= self.authorization['total_units'])
                self.db.execute('UPDATE authorization SET forecast=? WHERE id=1', (_dump(forecast),))
            self._event('fee_settled', prior['execution'], request=request, payload={'units': settled_units})

    def forecast(self, *, remaining_worst_units):
        integer(remaining_worst_units)
        with self.transaction():
            meta = self.db.execute('SELECT used,cost_revision FROM meta WHERE id=1').fetchone()
            value = {'basis_revision': meta['cost_revision'], 'remaining_worst_units': remaining_worst_units,
                     'affordable': _add(meta['used'], remaining_worst_units, self.authorization['cleanup_units']) <= self.authorization['total_units']}
            self.db.execute('UPDATE authorization SET forecast=? WHERE id=1', (_dump(value),))
            self._event('forecast', payload=value)
        return value

    def accept(self, request, epoch, artifact, evidence):
        from fsf.production.control import file_identity
        if (not isinstance(evidence, dict) or seal(evidence, 'report_digest') != evidence
                or evidence.get('verdict') != 'accept' or file_identity(artifact)['sha256'] != evidence.get('artifact_sha256')):
            raise ProductionError('PRODUCTION_EVIDENCE_REQUIRED')
        with self.transaction():
            prior = self._fence(request, epoch)
            policy = self.db.execute('SELECT policy FROM executions WHERE id=?', (prior['execution'],)).fetchone()[0]
            if evidence.get('policy_digest') != policy or prior['status'] not in {'produced', 'verified'}:
                raise ProductionError('PRODUCTION_EVIDENCE_REQUIRED')
            data, location = self._record_result(request, evidence, str(Path(artifact).absolute()))
            self.db.execute("UPDATE attempts SET status='machine_accepted' WHERE request=?", (request,))
            self.db.execute("UPDATE tasks SET status='machine_accepted',accepted=?,artifact=? WHERE execution=? AND id=?",
                            (data, location, prior['execution'], prior['task']))
            self.db.execute('''UPDATE tasks SET remaining=remaining-1 WHERE execution=? AND id IN
                           (SELECT task FROM dependencies WHERE execution=? AND parent=?)''',
                            (prior['execution'], prior['execution'], prior['task']))
            shot = _load(self.task(prior['execution'], prior['task'])['value'])['logical_shot_id']
            self.db.execute("UPDATE tasks SET status='rejected' WHERE execution=? AND shot=? AND status='repair_wait'",
                            (prior['execution'], shot))
            self._event('machine_accepted', prior['execution'], prior['task'], request, execution_change=True)

    def reject(self, request, epoch, evidence):
        with self.transaction():
            prior = self._fence(request, epoch)
            if prior['status'] != 'produced' or evidence.get('verdict') != 'reject' or seal(evidence, 'report_digest') != evidence:
                raise ProductionError('PRODUCTION_EVIDENCE_REQUIRED')
            self._record_result(request, evidence)
            self.db.execute("UPDATE attempts SET status='rejected' WHERE request=?", (request,))
            self.db.execute("UPDATE tasks SET status='rejected' WHERE execution=? AND id=?", (prior['execution'], prior['task']))
            self._event('rejected', prior['execution'], prior['task'], request, execution_change=True)

    def pause(self, execution, enabled):
        if type(enabled) is not bool:
            raise ProductionError('PRODUCTION_INVALID')
        with self.transaction():
            if self.db.execute('UPDATE executions SET paused=? WHERE id=?', (int(enabled), execution)).rowcount != 1:
                raise ProductionError('PRODUCTION_NOT_FOUND')
            self._event('pause', execution, payload={'enabled': enabled}, execution_change=True)

    def inspect(self, *, execution=None, after=-1, limit=256):
        integer(limit, 1, 256)
        integer(after, -1)
        meta = dict(self.db.execute('SELECT * FROM meta WHERE id=1').fetchone())
        rows = [] if execution is None else [dict(row) for row in self.db.execute(
            'SELECT id,ordinal,status,epoch,request FROM tasks WHERE execution=? AND ordinal>? ORDER BY ordinal LIMIT ?', (execution, after, limit))]
        return {'schema_version': 'fsf_production_store_inspection_v1', 'store_uuid': meta['uuid'],
                'revision': meta['revision'], 'used_units': meta['used'], 'generated_frames': meta['frames'],
                'tasks': rows, 'next_after': rows[-1]['ordinal'] if len(rows) == limit else None}

    def check(self):
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        if self._inline_charges and self.db.execute(
                'SELECT 1 FROM attempts WHERE charge_authorization IS NULL LIMIT 1').fetchone():
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        meta = self.db.execute('SELECT used,frames FROM meta WHERE id=1').fetchone()
        totals = self.db.execute("SELECT coalesce(sum(CASE WHEN status='settled' THEN settled ELSE worst END),0),coalesce(sum(frames),0) FROM reservations").fetchone()
        if tuple(meta) != tuple(totals):
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        for row in self.db.execute('SELECT route,used FROM route_totals'):
            actual = self.db.execute("SELECT coalesce(sum(CASE WHEN status='settled' THEN settled ELSE worst END),0) FROM reservations WHERE route=?", (row['route'],)).fetchone()[0]
            if actual != row['used']:
                raise ProductionError('PRODUCTION_STORE_CORRUPT')
        if (meta['used'] + self.authorization['cleanup_units'] > self.authorization['total_units']
                or meta['frames'] > self.authorization['max_generated_frames']):
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        from fsf.production.control import file_identity
        if self.db.execute('SELECT count(*) FROM tasks').fetchone()[0] != self.db.execute('SELECT count(*) FROM task_documents').fetchone()[0]:
            raise ProductionError('PRODUCTION_STORE_CORRUPT')
        for row in self.db.execute('SELECT t.*,d.value FROM tasks t JOIN task_documents d ON d.execution=t.execution AND d.id=t.id'):
            task = _load(row['value'])
            actual = self.db.execute('''SELECT count(*) FROM dependencies d JOIN tasks t
                ON d.execution=t.execution AND d.parent=t.id WHERE d.execution=? AND d.task=? AND t.accepted IS NULL''',
                                     (row['execution'], row['id'])).fetchone()[0]
            if task['task_id'] != row['id'] or task['logical_shot_id'] != row['shot'] or actual != row['remaining']:
                raise ProductionError('PRODUCTION_STORE_CORRUPT')
            if row['accepted']:
                row = self.task(row['execution'], row['id'])
                evidence = _load(row['accepted'])
                attempt = self.attempt(row['request'])
                policy = self.db.execute('SELECT policy FROM executions WHERE id=?', (row['execution'],)).fetchone()[0]
                if (row['status'] != 'machine_accepted' or not row['artifact']
                        or seal(evidence, 'report_digest') != evidence
                        or evidence.get('policy_digest') != policy or evidence.get('verdict') != 'accept'
                        or _load(attempt['evidence']) != evidence or attempt['invalidated']
                        or file_identity(row['artifact'])['sha256'] != evidence.get('artifact_sha256')):
                    raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        return {'integrity': 'passed', 'reservations': self.db.execute('SELECT count(*) FROM reservations').fetchone()[0]}

    def revoke(self):
        with self.transaction():
            self.db.execute('UPDATE authorization SET revoked=1 WHERE id=1')
            self._event('authorization_revoked')
        return {'authorized_for_new_work': False, 'existing_costs_retained': True}

    def invalidate(self, execution, changed):
        from collections import deque
        affected = set()
        pending = deque(changed)
        for task in changed:
            self.task(execution, key(task))
        while pending:
            task = pending.popleft()
            if task in affected:
                continue
            affected.add(task)
            pending.extend(row[0] for row in self.db.execute(
                'SELECT task FROM dependencies WHERE execution=? AND parent=?', (execution, task)))
        with self.transaction():
            for task in sorted(affected):
                row = self.task(execution, task)
                if row['request']:
                    old = self.attempt(row['request'])
                    if old['status'] not in {'failed', 'cancelled', 'rejected', 'machine_accepted'}:
                        raise ProductionError('PRODUCTION_RECONCILE_REQUIRED')
                    self.db.execute('UPDATE attempts SET invalidated=1 WHERE request=?', (row['request'],))
                self.db.execute("UPDATE tasks SET accepted=NULL,status='ready',epoch=epoch+1 WHERE execution=? AND id=?", (execution, task))
            for task in affected:
                remaining = self.db.execute('''SELECT count(*) FROM dependencies d JOIN tasks t
                    ON d.execution=t.execution AND d.parent=t.id WHERE d.execution=? AND d.task=? AND t.accepted IS NULL''',
                                            (execution, task)).fetchone()[0]
                self.db.execute('UPDATE tasks SET remaining=? WHERE execution=? AND id=?', (remaining, execution, task))
            self._event('invalidated', execution, payload={'affected_count': len(affected),
                        'closure_digest': digest(sorted(affected)), 'changed_digest': digest(sorted(changed))}, execution_change=True)
            self.db.execute('UPDATE authorization SET forecast=NULL WHERE id=1')
        return sorted(affected)

    def remaining_forecast(self):
        """Unreserved bounded future attempts; held requests are already in used."""
        total = 0
        maximum = self.authorization.get('max_repair_rounds', 3)
        rows = self.db.execute('''SELECT d.value,t.request,r.round,res.round AS prior_round,
            (SELECT count(*) FROM attempts a WHERE a.execution=t.execution AND a.task=t.id) AS attempts
            FROM tasks t JOIN task_documents d ON d.execution=t.execution AND d.id=t.id
            JOIN rounds r ON r.execution=t.execution AND r.shot=t.shot
            LEFT JOIN reservations res ON res.request=t.request WHERE t.accepted IS NULL''')
        for row in rows:
            task = _load(row['value'])
            count = maximum - row['round'] + 1 - int(row['prior_round'] == row['round']) if task['generated_frames'] else 3 - row['attempts']
            total += max(0, count) * task['worst_units']
            if total > self.authorization['total_units']:
                raise ProductionError('PRODUCTION_FORECAST_UNAFFORDABLE')
        return total

    def backup(self, output):
        """Consistent SQLite backup; it is data, not another executable authority."""
        import tempfile
        from fsf.core.edit_store import _target
        from fsf.production.control import file_identity
        target = _target(output)
        with edit_lock(target):
            if target.exists():
                raise ProductionError('PRODUCTION_TARGET_EXISTS')
            fd, temporary = tempfile.mkstemp(prefix='.store-backup-', dir=target.parent)
            os.close(fd)
            backup = None
            try:
                backup = sqlite3.connect(temporary)
                self.db.backup(backup, pages=128, sleep=0.05)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ProductionError('PRODUCTION_STORE_CORRUPT')
                meta = backup.execute('SELECT uuid,revision FROM meta WHERE id=1').fetchone()
                backup.close()
                backup = None
                with open(temporary, 'r+b') as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                return {'schema_version': 'fsf_production_store_backup_v1', 'read_only': True,
                        'store_uuid': meta[0], 'revision': meta[1], **file_identity(target)}
            finally:
                if backup is not None:
                    backup.close()
                Path(temporary).unlink(missing_ok=True)

    def export(self, output):
        """Consistent read-only JSON snapshot; never an executable second authority."""
        import os
        import tempfile
        from fsf.core.edit_store import _target
        target = _target(output)
        tables = ['meta', 'authorization', 'authorization_documents', 'executions', 'execution_plans', 'tasks', 'task_documents', 'dependencies', 'attempts',
                  'reservations', 'route_totals', 'rounds', 'events', 'outbox',
                  'verification_runs', 'verification_items']
        if self._result_table:
            tables.insert(tables.index('reservations'), 'attempt_results')
        with edit_lock(target):
            if target.exists():
                raise ProductionError('PRODUCTION_TARGET_EXISTS')
            fd, temporary = tempfile.mkstemp(prefix='.store-export-', dir=target.parent)
            try:
                self.db.execute('BEGIN')
                revision = self.db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                    stream.write('{"schema_version":"fsf_production_store_export_v1","read_only":true,')
                    stream.write('"revision":' + str(revision) + ',"tables":{')
                    for index, table in enumerate(tables):
                        if index:
                            stream.write(',')
                        stream.write(_dump(table) + ':[')
                        ordering = 'request' if table in ('outbox','reservations','attempts','attempt_results') else 'rowid'
                        for count, row in enumerate(self.db.execute('SELECT * FROM ' + table + ' ORDER BY ' + ordering)):
                            if count:
                                stream.write(',')
                            stream.write(_dump(dict(row)))
                        stream.write(']')
                    stream.write('}}\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                self.db.execute('COMMIT')
                os.replace(temporary, target)
                from fsf.production.control import file_identity
                identity = file_identity(target)
                return {'schema_version': 'fsf_production_store_export_receipt_v1',
                        'revision': revision, 'read_only': True, **identity}
            finally:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                Path(temporary).unlink(missing_ok=True)
