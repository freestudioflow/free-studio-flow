"""Operator-approved full-film submission backed by the indexed production store."""
import json
import re
import sqlite3
import time
from pathlib import Path

from fsf.production.command_driver import CommandDriver
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, key, publish, seal
from fsf.production.control import file_identity
from fsf.production.ledger import Ledger
from fsf.production.recovery import ReadRetries, _temporary
from fsf.production.runner import validate_plan
from fsf.production.store import ProductionStore, _dump, authority_registry
from fsf.production.store_runner import run_store_batch
from fsf.remote.contracts import sha

DDL = """
CREATE TABLE film_submission(execution TEXT PRIMARY KEY REFERENCES executions(id),
    binding TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE film_workorders(execution TEXT NOT NULL REFERENCES executions(id),
    workorder TEXT NOT NULL, step TEXT NOT NULL, scope TEXT NOT NULL,
    PRIMARY KEY(execution,workorder), UNIQUE(execution,step));
CREATE TABLE film_attempts(execution TEXT NOT NULL, workorder TEXT NOT NULL,
    attempt INTEGER NOT NULL, request TEXT NOT NULL UNIQUE REFERENCES attempts(request),
    dispatch TEXT UNIQUE, scope TEXT NOT NULL, job TEXT, job_version TEXT NOT NULL DEFAULT '',
    remote TEXT, revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(execution,workorder,attempt), UNIQUE(scope,job,job_version),
    FOREIGN KEY(execution,workorder) REFERENCES film_workorders(execution,workorder));
CREATE TRIGGER film_attempt_revision AFTER UPDATE ON attempts BEGIN
    UPDATE film_attempts SET revision=revision+1 WHERE request=new.request;
END;
"""


def _fields(value, names, code):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ProductionError(code)


def _sha(value, code):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ProductionError(code)
    return value


def _inputs(value):
    code = 'EXECUTION_INPUT_UNBOUND'
    _fields(value, 'assets anchors channels', code)
    for bindings in value.values():
        if not isinstance(bindings, dict):
            raise ProductionError(code)
        for name, artifact in bindings.items():
            key(name)
            _fields(artifact, 'path sha256', code)
            expected = _sha(artifact['sha256'], code)
            try:
                path = Path(artifact['path'])
                if not path.is_absolute():
                    raise ProductionError(code)
                actual = file_identity(path)
            except (OSError, ValueError, TypeError) as exc:
                if _temporary(exc):
                    raise
                raise ProductionError(code) from None
            if actual['sha256'] != expected:
                raise ProductionError(code)


def _reuse(item, task, policy):
    code = 'EXECUTION_INPUT_UNBOUND'
    _fields(item, 'step_id reason policy_digest source_store source_request result_digest', code)
    _sha(item['source_request'], code)
    _sha(item['result_digest'], code)
    if not isinstance(item['source_store'], str) or not Path(item['source_store']).is_absolute():
        raise ProductionError(code)
    try:
        source = ProductionStore(item['source_store'])
        try:
            prior = source.attempt(item['source_request'])
            original = source.task(prior['execution'], prior['task'])
            report = json.loads(prior['evidence'])
            if (prior['status'] != 'machine_accepted' or prior['invalidated']
                    or original['request'] != item['source_request']
                    or json.loads(original['value'])['input_digest'] != task['input_digest']
                    or report['report_digest'] != item['result_digest']
                    or seal(report, 'report_digest') != report or report['policy_digest'] != policy
                    or report['verdict'] != 'accept'
                    or file_identity(Path(prior['artifact']))['sha256'] != report['artifact_sha256']):
                raise ProductionError(code)
            return prior['artifact'], report
        finally:
            source.close()
    except (OSError, ValueError, TypeError, KeyError):
        raise ProductionError(code) from None


def _binding(spec):
    coverage = 'EXECUTION_COVERAGE_INVALID'
    feature = isinstance(spec, dict) and spec.get('schema_version') == 'fsf_film_submission_v2'
    fields = 'schema_version idempotency_key plan authorization driver routes workorders exclusions'
    _fields(spec, fields + (' feature' if feature else '') + (' quality' if feature and 'quality' in spec else ''), coverage)
    if spec['schema_version'] not in {'fsf_film_submission_v1', 'fsf_film_submission_v2'} or not isinstance(spec['workorders'], list):
        raise ProductionError(coverage)
    return 'film-' + digest(key(spec['idempotency_key'])), digest(spec)


def preflight(spec):
    coverage = 'EXECUTION_COVERAGE_INVALID'
    unbound = 'EXECUTION_INPUT_UNBOUND'
    execution, binding = _binding(spec)
    plan = spec['plan']
    validate_plan(plan)
    if 'feature' in spec:
        from fsf.production.feature import validate_feature
        validate_feature(spec['feature'], plan)
        if spec['exclusions']:
            raise ProductionError('FEATURE_COVERAGE')
        story = {shot['step_id']: shot for shot in spec['feature']['shots']}
        if 'quality' in spec:
            from fsf.production.feature_quality import validate_gate
            validate_gate(spec['quality'], plan)
    authorization = Ledger._authorization(spec['authorization'])
    ProductionStore._validate_scope(plan, authorization)
    CommandDriver.validate_config(spec['driver'])
    for pinned in spec['driver']['settings']['files']:
        path = Path(pinned['path'])
        if not path.is_file() or sha(path) != pinned['sha256']:
            raise ProductionError(unbound)
    tasks = {task['task_id']: task for task in plan['tasks'] if task['generated_frames']}
    if not isinstance(spec['exclusions'], list) or not isinstance(spec['routes'], dict):
        raise ProductionError(coverage)
    excluded = set()
    removed = set()
    for item in spec['exclusions']:
        if not isinstance(item, dict):
            raise ProductionError(coverage)
        reason = item.get('reason')
        if reason != 'verified_reuse':
            _fields(item, 'step_id reason policy_digest', coverage)
        step = key(item['step_id'])
        if (step in excluded or step not in tasks
                or (reason == 'operator_removed_optional' and tasks[step].get('essential', True))
                or reason not in {'operator_removed_optional', 'verified_reuse'}
                or item.get('policy_digest') != plan['policy_digest']):
            raise ProductionError(coverage)
        if reason == 'verified_reuse':
            _reuse(item, tasks[step], plan['policy_digest'])
        else:
            removed.add(step)
        excluded.add(step)
    if any(removed.intersection(task['depends_on']) for task in plan['tasks']
           if task['task_id'] not in removed):
        raise ProductionError(coverage)
    workorders = {}
    ids = set()
    for row in spec['workorders']:
        _fields(row, 'workorder_id step_id transport_scope capability_digest requires inputs', coverage)
        step, workorder = key(row['step_id']), key(row['workorder_id'])
        if step not in tasks or step in excluded or step in workorders or workorder in ids:
            raise ProductionError(coverage)
        workorders[step] = row
        ids.add(workorder)
    if not tasks or set(workorders) | excluded != tasks.keys():
        raise ProductionError(coverage)
    for step, row in workorders.items():
        key(row['transport_scope'])
        _sha(row['capability_digest'], unbound)
        route = spec['routes'].get(tasks[step]['route'])
        _fields(route, 'transport_scope capability_digest', unbound)
        if any(route[name] != row[name] for name in route):
            raise ProductionError(unbound)
        _fields(row['requires'], 'assets anchors channels', unbound)
        _inputs(row['inputs'])
        if 'feature' in spec and (
                set(story[step]['asset_ids']) != row['inputs']['assets'].keys()
                or digest(story[step]) != row['inputs']['assets'].get('story', {}).get('sha256')):
            raise ProductionError('FEATURE_INPUT_UNBOUND')
        if 'quality' in spec:
            policy = spec['quality']['policies'][step]
            if (policy['reference_digest'] != digest(story[step])
                    or policy['route_digest'] != digest(route)
                    or policy['capability_digest'] != row['capability_digest']):
                raise ProductionError('QC_BINDING')
        for kind, names in row['requires'].items():
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise ProductionError(unbound)
            if len(names) != len(set(names)) or not set(names) <= row['inputs'][kind].keys():
                raise ProductionError(unbound)
        if tasks[step]['input_digest'] != digest({name: row[name] for name in
                                                ('inputs', 'requires', 'capability_digest', 'transport_scope')}):
            raise ProductionError(unbound)
    return execution, binding


def install(db, execution, spec):
    # sqlite3.executescript commits outstanding transactions. Execute each
    # statement separately so the film binding and plan become visible together.
    statement = ''
    for line in DDL.splitlines():
        statement += line + '\n'
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    db.execute('INSERT INTO film_submission VALUES(?,?,?)', (execution, digest(spec), _dump(spec)))
    db.executemany('INSERT INTO film_workorders VALUES(?,?,?,?)',
                   [(execution, row['workorder_id'], row['step_id'], row['transport_scope'])
                    for row in spec['workorders']])
    for row in spec['exclusions']:
        step = row['step_id']
        status = 'excluded_optional' if row['reason'] == 'operator_removed_optional' else 'verified_reuse'
        db.execute("UPDATE tasks SET status=? WHERE execution=? AND id=?", (status, execution, step))
        if status == 'verified_reuse':
            db.execute('''UPDATE tasks SET remaining=remaining-1 WHERE execution=? AND id IN
                           (SELECT task FROM dependencies WHERE execution=? AND parent=?)''',
                       (execution, execution, step))


def dispatch_binding(store, execution, step, attempt):
    row = store.db.execute('SELECT workorder,scope FROM film_workorders WHERE execution=? AND step=?',
                           (execution, step)).fetchone()
    if row is None:
        raise ProductionError('EXECUTION_COVERAGE_INVALID')
    request = digest(dict(execution_id=execution, workorder_id=row['workorder'], step_id=step,
                          attempt_id=attempt, submission_digest=store.film_binding(execution)))
    return row['workorder'], row['scope'], request


class FilmDriver(CommandDriver):
    def __init__(self, spec, store, execution):
        self.store = store
        self.execution = execution
        self.read_retries = None
        self.scope = {row['step_id']: row for row in spec['workorders']}
        self.reused = {row['step_id']: row for row in spec['exclusions'] if row['reason'] == 'verified_reuse'}
        root = store.root / 'driver'
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(spec['driver'], plan=spec['plan'], state_root=root)

    def verify_reuse(self, task):
        return _reuse(self.reused[task['task_id']], task, self.policy_digest)

    def _verify(self, task, request_id, challenge=None):
        verify = super()._verify
        if self.read_retries is None:
            self.read_retries = ReadRetries(self._execution_deadline or time.monotonic() + 30)
        return self.read_retries.call(lambda: verify(task, request_id, challenge))

    def _locator(self, operation, request_id):
        locator = super()._locator(operation, request_id)
        if operation == 'reconcile':
            row = self.store.db.execute('SELECT revision,scope FROM film_attempts WHERE request=?',
                                        (request_id,)).fetchone()
            if row is not None:
                locator.update(attempt_revision=row['revision'], transport_scope=row['scope'])
        return locator

    def _request(self, task, request_id):
        request = super()._request(task, request_id)
        if task['task_id'] in self.scope:
            row = self.scope[task['task_id']]
            attempt = self.store.attempt(request_id)
            request.update(execution_id=self.execution, step_id=row['step_id'],
                           workorder_id=row['workorder_id'], attempt_id=attempt['epoch'],
                           submission_digest=self.store.film_binding(self.execution),
                           inputs=row['inputs'], capability_digest=row['capability_digest'])
        return request

    def _save_identity(self, request_id, identity):
        row = self.store.db.execute('SELECT * FROM film_attempts WHERE request=?',
                                    (request_id,)).fetchone()
        if row is None:
            return super()._save_identity(request_id, identity)
        job = identity.get('job_id') if isinstance(identity, dict) else None
        if not isinstance(job, str) or not job or len(job) > 512:
            raise ProductionError('PRODUCTION_PROVIDER_INVALID')
        version = identity.get('version')
        if version is not None:
            version = str(integer(version, 1))
        dispatch = identity.get('dispatch_id', digest(['command', row['scope'], request_id]))
        dispatch = _sha(dispatch, 'PRODUCTION_PROVIDER_INVALID')
        with self.store.transaction():
            if row['job'] is None:
                try:
                    changed = self.store.db.execute(
                        '''UPDATE film_attempts SET job=?,job_version=?,dispatch=?,remote=?,revision=revision+1
                           WHERE request=? AND revision=?''',
                        (job, version or '', dispatch, _dump(identity), request_id, row['revision'])).rowcount
                except sqlite3.IntegrityError:
                    raise ProductionError('EXECUTION_BINDING_CONFLICT') from None
                if not changed:
                    raise ProductionError('EXECUTION_BINDING_CONFLICT')
            elif (row['job'], row['dispatch'], row['remote']) != (job, dispatch, _dump(identity)):
                raise ProductionError('EXECUTION_BINDING_CONFLICT')
        return identity

    def submit(self, task, request_id):
        if task['task_id'] not in self.scope:
            return super().submit(task, request_id)
        return self._save_identity(request_id, self._call('submit', task, request_id))

    def _identity(self, task, request_id):
        row = self.store.db.execute('SELECT remote,revision FROM film_attempts WHERE request=?', (request_id,)).fetchone()
        if row is None:
            return super()._identity(task, request_id)
        if row['remote'] is not None:
            return json.loads(row['remote'])
        found = self._call('reconcile', task, request_id)
        if isinstance(found, dict) and found.get('disposition') == 'not_received':
            self.store.retire_film_claim(request_id, row['revision'], proof=found)
            return None
        return self._save_identity(request_id, found) if found is not None else None

    def observe(self, task, request_id):
        result = super().observe(task, request_id)
        if result['status'] == 'unknown' and self.store.attempt(request_id)['status'] == 'failed':
            return {'status': 'failed'}
        return result


def _document(store):
    if not store._film:
        raise ProductionError('PRODUCTION_NOT_FOUND')
    records = store.db.execute('SELECT * FROM film_submission').fetchall()
    if len(records) != 1:
        raise ProductionError('PRODUCTION_STORE_CORRUPT')
    row = records[0]
    spec = json.loads(row['value'])
    if digest(spec) != row['binding']:
        raise ProductionError('PRODUCTION_STORE_CORRUPT')
    return row['execution'], spec


def _record(store, row):
    attempt = store.attempt(row['request'])
    evidence = json.loads(attempt['evidence']) if attempt['evidence'] else None
    return dict(execution_id=row['execution'], workorder_id=row['workorder'], attempt_id=row['attempt'],
                step_id=row['step'], request_digest=row['request'], dispatch_id=row['dispatch'],
                transport_scope=row['scope'], provider_job_id=row['job'], attempt_revision=row['revision'],
                provider_job_version=int(row['job_version']) if row['job_version'] else None,
                result_digest=evidence['report_digest'] if evidence else None, status=attempt['status'])


def run(operation, root, *, spec=None, dry_run=False, seconds=300, workorder=None, attempt=None, revision=None):
    root = Path(root)
    integer(seconds, 1, 5400)
    reads = ReadRetries(time.monotonic() + seconds)
    validated = False
    if operation == 'film-submit':
        execution, binding = _binding(spec)
        if dry_run:
            reads.call(lambda: preflight(spec))
            return dict(execution_id=execution, binding_digest=binding, status='preflight_passed',
                        workorders=len(spec['workorders']), dispatches=0)
        locator = authority_registry(root) / (execution + '.json')
        with edit_lock(locator):
            if locator.exists():
                previous = read_json(locator)
                if previous['binding'] != binding:
                    raise ProductionError('EXECUTION_BINDING_CONFLICT')
                root = Path(previous['store'])
            else:
                reads.call(lambda: preflight(spec))
                validated = True
                root = root.resolve()
                if (root / 'store-manifest.json').exists() or (root / 'state.sqlite3').exists():
                    raise ProductionError('EXECUTION_BINDING_CONFLICT')
                publish(locator, {'binding': binding, 'store': str(root)})
            if (root / 'state.sqlite3').exists():
                ProductionStore.finish_film_initialization(root, execution, spec)
            elif not (root / 'store-manifest.json').exists():
                if not validated:
                    reads.call(lambda: preflight(spec))
                    validated = True
                root.mkdir(parents=True, exist_ok=True)
                store = ProductionStore.create(root, spec['plan'], spec['authorization'],
                                               execution_id=execution, _film_spec=spec)
                store.close()
    store = ProductionStore(root)
    try:
        execution, saved = _document(store)
        if spec is not None and digest(spec) != digest(saved):
            raise ProductionError('EXECUTION_BINDING_CONFLICT')
        if operation in {'film-submit', 'film-resume'}:
            if not validated:
                reads.call(lambda: preflight(saved))
            driver = FilmDriver(saved, store, execution)
            driver.read_retries = reads
            gate = None
            if 'quality' in saved:
                from fsf.production.feature_quality import make_gate
                gate = make_gate(saved['quality'], saved['plan'])
            result = run_store_batch(saved['plan'], store=store, driver=driver,
                                     seconds=seconds, quality_gate=gate)
            return dict(execution_id=execution, store=str(store.root), **result)
        query = '''SELECT a.*,w.step FROM film_attempts a JOIN film_workorders w
            USING(execution,workorder) WHERE a.execution=?'''
        if operation in {'film-fetch', 'film-retire-prepared', 'film-cancel'}:
            row = store.db.execute(query + ' AND a.workorder=? AND a.attempt=?',
                                   (execution, key(workorder), integer(attempt, 1))).fetchone()
            if row is None:
                raise ProductionError('PRODUCTION_NOT_FOUND')
            if operation == 'film-retire-prepared':
                return store.retire_film_claim(row['request'], revision)
            if operation == 'film-cancel':
                from fsf.production.store_runner import cancel_store_request
                return cancel_store_request(saved['plan'], store=store, driver=FilmDriver(saved, store, execution),
                                            request_id=row['request'], epoch=row['attempt'], seconds=seconds)
            record = _record(store, row)
            value = store.attempt(row['request'])
            if record['status'] != 'machine_accepted' or value['invalidated'] or not value['artifact']:
                raise ProductionError('PRODUCTION_NOT_READY')
            identity = reads.call(lambda: file_identity(Path(value['artifact'])))
            if identity['sha256'] != json.loads(value['evidence'])['artifact_sha256']:
                raise ProductionError('POST_SOURCE_CHANGED')
            return dict(**record, artifact=value['artifact'], identity=identity)
        rows = store.db.execute(query + ' ORDER BY a.workorder,a.attempt', (execution,))
        return dict(execution_id=execution, binding_digest=digest(saved),
                    attempts=[_record(store, row) for row in rows],
                    workorders=[{'workorder_id': row['workorder_id'], 'step_id': row['step_id']}
                                for row in saved['workorders']], exclusions=saved['exclusions'],
                    used_units=store.inspect()['used_units'])
    finally:
        store.close()
