"""Quiescent migration of actual local command results, never paid-model evidence."""
import copy
import json

import pytest

from fsf.production.contracts import ProductionError, digest, publish, seal
from fsf.production.control import file_identity
from fsf.production.runner import run_batch
from fsf.production.store import ProductionStore
from fsf.production.store_migration import migrate_store, recover_migration
from test_production_runner import setup
from test_production_command_driver import driver


def _migration_crash_worker(manifest, root, target, point, reached, release):
    import fsf.production.store_migration as migration
    original = migration.publish

    def checkpoint(path, value):
        result = original(path, value)
        if (point == 'receipt' and path.name == 'migration-receipt.json'
                or point == 'descriptor' and path.name == 'store-manifest.json'
                or point == 'retirement' and path.name.endswith('.retired.json')):
            reached.set()
            if not release.wait(60):
                raise RuntimeError('test controller did not terminate worker')
        return result

    migration.publish = checkpoint
    migration.migrate_store(manifest, source_root=root, output_root=target)


def source_case(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    plan, book = setup(root)
    backend, config = driver(root, plan)
    assert run_batch(plan, state_root=root, ledger=book, driver=backend)['status'] == 'machine_accepted'
    sources = []
    for name, kind, path, field in [
        ('plan', 'plan', root / plan['plan_digest'] / 'plan.json', 'plan_digest'),
        ('workflow', 'workflow', root / plan['plan_digest'] / 'workflow.json', 'state_digest'),
        ('ledger', 'ledger', book.path, 'ledger_digest'),
        ('driver', 'driver-binding', root / 'driver/driver-binding.json', None),
    ]:
        value = json.loads(path.read_text())
        sources.append({'source_id': name, 'kind': kind, 'path': path.relative_to(root).as_posix(),
                        **file_identity(path), 'logical_digest': value[field] if field else digest(value)})
    artifacts = []
    for path in (root / 'artifacts').glob('*/artifact.bin'):
        artifacts.append({'request_id': path.parent.name, 'path': path.relative_to(root).as_posix(), **file_identity(path)})
    manifest = seal({'schema_version': 'fsf_store_migration_manifest_v1',
                     'authorization_digest': book.inspect()['authorization']['authorization_digest'],
                     'execution_ids': ['film'], 'execution_bindings': [{'execution_id': 'film',
                     'workflow_source_id': 'workflow', 'source_binding_digest': plan['binding_digest']}],
                     'old_writers_retired': True, 'provider_inflight_count': 0,
                     'source_scope_attestation': 'complete_authorization_domain',
                     'sources': sources, 'artifacts': artifacts}, 'manifest_digest')
    target = tmp_path / 'indexed'
    target.mkdir()
    return root, target, plan, book, backend, manifest


def test_quiescent_migration_preserves_requests_costs_and_freezes_old_writer(tmp_path):
    root, target, plan, book, backend, manifest = source_case(tmp_path)
    before = book.path.read_bytes()
    receipt = migrate_store(manifest, source_root=root, output_root=target)
    assert receipt['reservations'] == 2 and receipt['used_units'] == 20
    assert book.path.read_bytes() == before
    store = ProductionStore(target)
    assert store.check()['reservations'] == 2
    assert store.inspect()['used_units'] == 20
    old_requests = set(book.inspect()['reservations'])
    assert {row[0] for row in store.db.execute('SELECT request FROM attempts')} == old_requests
    assert all(row[0] == 'unknown' for row in store.db.execute('SELECT status FROM reservations'))
    with pytest.raises(ProductionError, match='AUTHORITY_CONFLICT'):
        run_batch(plan, state_root=root, ledger=book, driver=backend)
    assert len(json.loads((root / 'submitted.json').read_text())) == 2
    output = tmp_path / 'snapshot.json'
    exported = store.export(output)
    assert exported['read_only'] and json.loads(output.read_text())['read_only']
    store.close()


@pytest.mark.parametrize('mutation', ['active', 'scope', 'artifact', 'alias', 'source', 'request'])
def test_migration_refuses_missing_or_conflicting_authority(tmp_path, mutation):
    root, target, plan, book, backend, manifest = source_case(tmp_path)
    if mutation == 'active':
        manifest['provider_inflight_count'] = 1
    elif mutation == 'scope':
        manifest['source_scope_attestation'] = 'partial'
    elif mutation == 'artifact':
        manifest['artifacts'] = []
    elif mutation == 'alias':
        manifest['execution_ids'].append('alias')
        manifest['execution_bindings'].append({**manifest['execution_bindings'][0], 'execution_id': 'alias'})
    elif mutation == 'source':
        manifest['sources'][0]['sha256'] = 'f' * 64
    else:
        # An unrepresented financial request may not silently disappear.
        value = book.inspect()
        value['reservations']['missing_scope'] = copy.deepcopy(next(iter(value['reservations'].values())))
        publish(book.path, seal(value, 'ledger_digest'))
        entry = next(item for item in manifest['sources'] if item['kind'] == 'ledger')
        entry.update(file_identity(book.path))
        entry['logical_digest'] = json.loads(book.path.read_text())['ledger_digest']
    manifest = seal(manifest, 'manifest_digest')
    with pytest.raises(ProductionError):
        migrate_store(manifest, source_root=root, output_root=target)
    if (target / 'store-manifest.json').exists():
        with pytest.raises(ProductionError, match='RECOVERY_REQUIRED'):
            ProductionStore(target)
    assert not book.path.with_name(book.path.name + '.retired.json').exists()


def test_sealed_cutover_recovers_after_descriptor_publication_failure(tmp_path, monkeypatch):
    import fsf.production.store_migration as migration
    root, target, plan, book, backend, manifest = source_case(tmp_path)
    original = migration.publish
    def interrupted(path, value):
        if path.name == 'store-manifest.json':
            raise RuntimeError('cutover fault')
        return original(path, value)
    monkeypatch.setattr(migration, 'publish', interrupted)
    with pytest.raises(RuntimeError, match='cutover fault'):
        migrate_store(manifest, source_root=root, output_root=target)
    with pytest.raises(ProductionError, match='RECOVERY_REQUIRED'):
        ProductionStore(target)
    monkeypatch.setattr(migration, 'publish', original)
    receipt = recover_migration(target, source_root=root)
    assert receipt['used_units'] == 20
    recovered = ProductionStore(target)
    assert recovered.check()['reservations'] == 2
    recovered.close()
    assert len(json.loads((root / 'submitted.json').read_text())) == 2


@pytest.mark.parametrize('point', ['receipt', 'descriptor', 'retirement'])
def test_recovery_after_durable_cutover_publication_is_idempotent(tmp_path, monkeypatch, point):
    import fsf.production.store_migration as migration
    root, target, plan, book, backend, manifest = source_case(tmp_path)
    source_before = {entry['path']: (root / entry['path']).read_bytes()
                     for entry in manifest['sources']}
    original = migration.publish
    reached = []

    def interrupted(path, value):
        result = original(path, value)
        matches = (point == 'receipt' and path.name == 'migration-receipt.json'
                   or point == 'descriptor' and path.name == 'store-manifest.json'
                   or point == 'retirement' and path.name.endswith('.retired.json'))
        if matches:
            reached.append(path.name)
            raise RuntimeError('after durable publication')
        return result

    monkeypatch.setattr(migration, 'publish', interrupted)
    with pytest.raises(RuntimeError, match='after durable publication'):
        migrate_store(manifest, source_root=root, output_root=target)
    assert len(reached) == 1
    with pytest.raises(ProductionError, match='RECOVERY_REQUIRED'):
        ProductionStore(target)
    monkeypatch.setattr(migration, 'publish', original)
    first = recover_migration(target, source_root=root)
    assert recover_migration(target, source_root=root) == first
    assert source_before == {name: (root / name).read_bytes() for name in source_before}
    store = ProductionStore(target)
    try:
        assert store.inspect()['used_units'] == 20
        assert store.check()['reservations'] == 2
    finally:
        store.close()
    assert len(json.loads((root / 'submitted.json').read_text())) == 2


@pytest.mark.parametrize('point', ['receipt', 'descriptor', 'retirement'])
def test_independent_process_termination_after_confirmed_cutover_point(tmp_path, point):
    import multiprocessing
    root, target, plan, book, backend, manifest = source_case(tmp_path)
    before = book.path.read_bytes()
    context = multiprocessing.get_context('spawn')
    reached, release = context.Event(), context.Event()
    child = context.Process(target=_migration_crash_worker,
                            args=(manifest, root, target, point, reached, release))
    child.start()
    try:
        assert reached.wait(45), 'durable fault point was not confirmed'
        child.terminate()
        child.join(15)
        assert not child.is_alive() and child.exitcode != 0
    finally:
        # Terminating a process can leave multiprocessing synchronization locks
        # owned by that process. Do not touch its shared Event after the kill.
        if child.is_alive():
            child.terminate()
            child.join(15)
    assert book.path.read_bytes() == before
    with pytest.raises(ProductionError, match='RECOVERY_REQUIRED'):
        ProductionStore(target)
    receipt = recover_migration(target, source_root=root)
    assert recover_migration(target, source_root=root) == receipt
    store = ProductionStore(target)
    try:
        assert store.check()['reservations'] == 2
        assert store.inspect()['used_units'] == 20
    finally:
        store.close()
    assert len(json.loads((root / 'submitted.json').read_text())) == 2
