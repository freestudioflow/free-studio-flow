"""Quiescent JSON-to-indexed migration. Never fabricates execution evidence."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil

from fsf.core.asset_library import _root, _path_parts, _no_links, _open_reader
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, key, publish, seal
from fsf.production.control import file_identity
from fsf.production.ledger import Ledger
from fsf.production.runner import validate_plan
from fsf.production.store import ProductionStore, _dump, _load


def migrate_store(manifest, *, source_root, output_root):
    from fsf.production.contracts import validate
    validate(manifest, 'fsf_store_migration_manifest_v1', 'manifest_digest')
    expected = {'schema_version', 'authorization_digest', 'execution_ids', 'execution_bindings',
                'old_writers_retired', 'provider_inflight_count', 'sources',
                'source_scope_attestation', 'manifest_digest'}
    if (not isinstance(manifest, dict) or set(manifest) not in (expected, expected | {'artifacts'})
            or manifest['schema_version'] != 'fsf_store_migration_manifest_v1'
            or seal(manifest, 'manifest_digest') != manifest or manifest['old_writers_retired'] is not True
            or type(manifest['provider_inflight_count']) is not int or manifest['provider_inflight_count'] != 0
            or manifest['source_scope_attestation'] != 'complete_authorization_domain'):
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    source, target = _root(source_root), _root(output_root)
    if source == target or source in target.parents or target in source.parents:
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    sources = {}
    for item in manifest['sources']:
        if set(item) != {'source_id', 'kind', 'path', 'sha256', 'size_bytes', 'logical_digest'}:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        name = key(item['source_id'])
        if name in sources:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        _path_parts(item['path'])
        path = source.joinpath(*item['path'].split('/'))
        if file_identity(path) != {k: item[k] for k in ('sha256', 'size_bytes')}:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        value = read_json(path)
        field = {'plan': 'plan_digest', 'workflow': 'state_digest', 'ledger': 'ledger_digest',
                 'progress': 'progress_digest'}.get(item['kind'])
        if item['kind'] == 'driver-binding':
            if set(value) != {'driver_digest', 'plan_digest', 'policy_digest'} or digest(value) != item['logical_digest']:
                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        elif not field or value.get(field) != item['logical_digest'] or seal(value, field) != value:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        sources[name] = {'item': item, 'path': path, 'value': value}
    ledgers = [value for value in sources.values() if value['item']['kind'] == 'ledger']
    if len(ledgers) != 1:
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    ledger = ledgers[0]['value']
    auth = Ledger._authorization(ledger['authorization'])
    if auth['authorization_digest'] != manifest['authorization_digest']:
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    bindings, execution_ids, workflow_ids, requests = [], set(), set(), set()
    plans = [value['value'] for value in sources.values() if value['item']['kind'] == 'plan']
    for plan in plans:
        validate_plan(plan)
        ProductionStore._validate_scope(plan, auth)
    for binding in manifest['execution_bindings']:
        if set(binding) != {'execution_id', 'workflow_source_id', 'source_binding_digest'}:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        execution = key(binding['execution_id'])
        name = binding['workflow_source_id']
        if execution in execution_ids or name in workflow_ids or name not in sources:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        entry = sources[name]
        flow = entry['value']
        matching = [plan for plan in plans if plan['binding_digest'] == flow['binding_digest']]
        if (entry['item']['kind'] != 'workflow' or len(matching) != 1
                or binding['source_binding_digest'] != flow['binding_digest']):
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        plan = matching[0]
        graph = {task['task_id']: task['depends_on'] for task in plan['tasks']}
        if (flow['dependencies'] != graph or set(flow['tasks']) != set(graph)
                or flow['production_id'] != plan['production_id']):
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        for task in flow['tasks'].values():
            if task['attempts'] and task['attempts'][-1]['status'] not in {'failed', 'cancelled', 'rejected', 'machine_accepted'}:
                raise ProductionError('PRODUCTION_MIGRATION_ACTIVE')
            for attempt in task['attempts']:
                request = key(attempt['request_id'])
                if request in requests:
                    raise ProductionError('PRODUCTION_REQUEST_CONFLICT')
                requests.add(request)
        bindings.append((execution, plan, flow))
        execution_ids.add(execution)
        workflow_ids.add(name)
    declared_workflows = {name for name, entry in sources.items() if entry['item']['kind'] == 'workflow'}
    if (not bindings or len(manifest['execution_ids']) != len(execution_ids)
            or set(manifest['execution_ids']) != execution_ids or declared_workflows != workflow_ids
            or set(ledger['reservations']) != requests):
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    artifacts = {}
    for item in manifest.get('artifacts', []):
        if set(item) != {'request_id', 'path', 'sha256', 'size_bytes'}:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        request = key(item['request_id'])
        if request not in requests or request in artifacts:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        _path_parts(item['path'])
        original = source.joinpath(*item['path'].split('/'))
        if file_identity(original) != {k: item[k] for k in ('sha256', 'size_bytes')}:
            raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        artifacts[request] = (original, item)
    # Stable ordered source locks prevent cooperating legacy writers changing the cutover basis.
    with ExitStack() as locks:
        for entry in sorted(sources.values(), key=lambda value: str(value['path'])):
            locks.enter_context(edit_lock(entry['path']))
            if file_identity(entry['path']) != {k: entry['item'][k] for k in ('sha256', 'size_bytes')}:
                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
        publish(target / 'migration-intent.json', seal({'schema_version': 'fsf_store_migration_intent_v1',
                                                      'manifest': manifest}, 'intent_digest'))
        first_execution, first_plan, _ = bindings[0]
        store = ProductionStore.create(target, first_plan, auth, execution_id=first_execution, _activation='building')
        try:
            original_folder = target / 'legacy-snapshots'
            original_folder.mkdir()
            for name, entry in sources.items():
                shutil.copyfile(entry['path'], original_folder / (name + '.json'))
            used = frames = 0
            with store.transaction():
                for index, (execution, plan, flow) in enumerate(bindings):
                    if index:
                        ProductionStore._insert_execution(store.db, execution, plan, auth)
                    drivers = [item['value'] for item in sources.values() if item['item']['kind'] == 'driver-binding'
                               and item['value']['plan_digest'] == plan['plan_digest']]
                    if len(drivers) != 1 or drivers[0]['policy_digest'] != plan['policy_digest']:
                        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                    store.db.execute('UPDATE executions SET driver=? WHERE id=?', (drivers[0]['driver_digest'], execution))
                    tasks = {value['task_id']: value for value in plan['tasks']}
                    store.db.execute('UPDATE executions SET paused=?,revision=? WHERE id=?',
                                     (int(flow['paused']), flow['revision'], execution))
                    for task_id, state in flow['tasks'].items():
                        task = tasks[task_id]
                        for attempt in state['attempts']:
                            request = attempt['request_id']
                            entry = ledger['reservations'][request]
                            if (entry['route'] != task['route'] or entry['binding_digest'] != task['input_digest']
                                    or entry['worst_units'] != task['worst_units'] or entry['generated_frames'] != task['generated_frames']):
                                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                            store.db.execute('''INSERT INTO attempts(request,execution,task,epoch,owner,expires,status,invalidated,evidence)
                                              VALUES(?,?,?,?,?,?,?,?,?)''',
                                             (request, execution, task_id, attempt['epoch'], attempt['owner'], int(attempt['expires_at']),
                                              attempt['status'], int(attempt.get('invalidated', False)),
                                              _dump(attempt['evidence']) if attempt.get('evidence') else None))
                            slot = entry['generation_slot']
                            if slot and (slot['logical_shot_id'] != task['logical_shot_id'] or slot['segment_id'] != task['segment_id']):
                                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                            status = 'settled' if entry['status'] == 'settled' else 'unknown'
                            store.db.execute('''INSERT INTO reservations(request,authorization,execution,shot,segment,round,route,
                                                worst,settled,frames,status,binding,essential) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                             (request, store._authorization_key, execution,
                                              slot['logical_shot_id'] if slot else None, slot['segment_id'] if slot else None,
                                              slot['repair_round'] if slot else None, entry['route'], entry['worst_units'],
                                              entry['settled_units'], entry['generated_frames'], status,
                                              entry['binding_digest'], int(entry.get('essential', True))))
                            store.db.execute("INSERT INTO outbox(request,state) VALUES(?,'observed')", (request,))
                            used += Ledger._cost(entry)
                            frames += entry['generated_frames']
                        latest = state['attempts'][-1] if state['attempts'] else None
                        accepted = state['accepted']
                        artifact = None
                        if accepted:
                            if not latest or latest['status'] != 'machine_accepted' or latest['evidence'] != accepted:
                                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                            request = latest['request_id']
                            if request not in artifacts:
                                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                            original, item = artifacts[request]
                            if accepted['artifact_sha256'] != item['sha256'] or seal(accepted, 'report_digest') != accepted:
                                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                            folder = target / 'artifacts' / request[:2]
                            folder.mkdir(parents=True, exist_ok=True)
                            destination = folder / request
                            with _open_reader(original) as inp, destination.open('xb') as out:
                                shutil.copyfileobj(inp, out, length=1024 * 1024)
                                out.flush()
                                os.fsync(out.fileno())
                            if file_identity(destination) != {k: item[k] for k in ('sha256', 'size_bytes')}:
                                raise ProductionError('PRODUCTION_SOURCE_CHANGED')
                            artifact = str(destination)
                            store.db.execute('UPDATE attempts SET artifact=? WHERE request=?', (artifact, request))
                        remaining = sum(flow['tasks'][parent]['accepted'] is None for parent in task['depends_on'])
                        store.db.execute('UPDATE tasks SET epoch=?,request=?,status=?,accepted=?,artifact=?,remaining=? WHERE execution=? AND id=?',
                                         (state['epoch'], latest['request_id'] if latest else None,
                                          'machine_accepted' if accepted else (latest['status'] if latest else 'ready'),
                                          _dump(accepted) if accepted else None, artifact, remaining, execution, task_id))
                    for shot, round_value in ledger.get('repair_rounds', {}).items():
                        store.db.execute('UPDATE rounds SET round=? WHERE execution=? AND shot=?', (round_value, execution, shot))
                    if 'repair_rounds' not in ledger:
                        for shot in auth['logical_shot_ids']:
                            high = store.db.execute('SELECT coalesce(max(round),1) FROM reservations WHERE execution=? AND shot=?',
                                                    (execution, shot)).fetchone()[0]
                            store.db.execute('UPDATE rounds SET round=? WHERE execution=? AND shot=?', (high, execution, shot))
                if frames != ledger['consumed_frames'] or used + auth['cleanup_units'] > auth['total_units']:
                    raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                store.db.execute('UPDATE meta SET used=?,frames=?,cost_revision=? WHERE id=1', (used, frames, ledger['revision']))
                forecast = ledger.get('forecast')
                if forecast:
                    projected = {'basis_revision': ledger['revision'], 'remaining_worst_units': forecast['remaining_worst_units'],
                                 'affordable': used + forecast['remaining_worst_units'] + auth['cleanup_units'] <= auth['total_units']}
                    store.db.execute('UPDATE authorization SET forecast=? WHERE id=1', (_dump(projected),))
                for route in auth['route_limits']:
                    amount = sum(Ledger._cost(value) for value in ledger['reservations'].values() if value['route'] == route)
                    if amount > auth['route_limits'][route]:
                        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
                    store.db.execute('UPDATE route_totals SET used=? WHERE route=?', (amount, route))
                store._event('migrated', payload={'manifest_digest': manifest['manifest_digest']})
                store.db.execute("UPDATE meta SET activation='sealed_prepared' WHERE id=1")
            store.check()
            receipt = seal({'schema_version': 'fsf_store_migration_receipt_v1', 'store_uuid': store.descriptor['store_uuid'],
                            'source_manifest_digest': manifest['manifest_digest'], 'reservations': len(requests),
                            'used_units': used, 'generated_frames': frames, 'execution_ids': sorted(execution_ids)}, 'receipt_digest')
            publish(target / 'migration-receipt.json', receipt)
            descriptor = {**store.descriptor, 'migration_receipt_digest': receipt['receipt_digest']}
            publish(target / 'store-manifest.json', seal(descriptor, 'manifest_digest'))
            for entry in sources.values():
                if entry['item']['kind'] in {'workflow', 'ledger'}:
                    publish(entry['path'].with_name(entry['path'].name + '.retired.json'),
                            {'store_uuid': receipt['store_uuid'], 'receipt_digest': receipt['receipt_digest']})
            with store.transaction():
                store.db.execute("UPDATE meta SET activation='active' WHERE id=1")
            return receipt
        finally:
            store.close()


def recover_migration(root, *, source_root):
    """Finish only a sealed cutover; never promote a partially imported database."""
    target, source = _root(root), _root(source_root)
    intent = read_json(target / 'migration-intent.json')
    receipt = read_json(target / 'migration-receipt.json')
    if (intent.get('schema_version') != 'fsf_store_migration_intent_v1'
            or seal(intent, 'intent_digest') != intent
            or receipt.get('schema_version') != 'fsf_store_migration_receipt_v1'
            or seal(receipt, 'receipt_digest') != receipt
            or receipt['source_manifest_digest'] != intent['manifest']['manifest_digest']):
        raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
    with ExitStack() as locks:
        paths = []
        for entry in sorted(intent['manifest']['sources'], key=lambda value: value['path']):
            _path_parts(entry['path'])
            path = source.joinpath(*entry['path'].split('/'))
            locks.enter_context(edit_lock(path))
            if file_identity(path) != {k: entry[k] for k in ('sha256', 'size_bytes')}:
                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
            paths.append((path, entry['kind']))
        store = ProductionStore(target, recovery=True)
        try:
            phase = store.db.execute('SELECT activation FROM meta WHERE id=1').fetchone()[0]
            if phase not in ('sealed_prepared', 'active') or receipt['store_uuid'] != store.descriptor['store_uuid']:
                raise ProductionError('PRODUCTION_MIGRATION_RECOVERY_REQUIRED')
            result = store.check()
            if result['reservations'] != receipt['reservations'] or store.inspect()['used_units'] != receipt['used_units']:
                raise ProductionError('PRODUCTION_MIGRATION_INTEGRITY')
            publish(target / 'store-manifest.json', seal({**store.descriptor,
                    'migration_receipt_digest': receipt['receipt_digest']}, 'manifest_digest'))
            for path, kind in paths:
                if kind in {'workflow', 'ledger'}:
                    publish(path.with_name(path.name + '.retired.json'),
                            {'store_uuid': receipt['store_uuid'], 'receipt_digest': receipt['receipt_digest']})
            with store.transaction():
                store.db.execute("UPDATE meta SET activation='active' WHERE id=1")
            return receipt
        finally:
            store.close()
