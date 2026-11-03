"""Fixed asset versions and atomic packs never copy unrelated library files."""
import copy
import pytest
from test_asset_library import record,seal
from fsf.core.asset_store import register_asset
from fsf.production.assets import snapshot_assets,verify_snapshot,export_pack,verify_pack
from fsf.production.contracts import ProductionError


def library(tmp_path):
    source=tmp_path/'source';source.mkdir()
    root=tmp_path/'library';root.mkdir()
    value=record(source,role='other',media='application/octet-stream')
    register_asset(root,value,resource_root=source)
    return source,root,value


def test_snapshot_keeps_fixed_version_and_pack_excludes_unrelated_files(tmp_path):
    source,root,first=library(tmp_path)
    snapshot=snapshot_assets([{'asset_id':'prop_A','version':'1'}],library=root)
    second=copy.deepcopy(first);second['asset_version']='2';second['source_prompt']='Another asset version';seal(second)
    register_asset(root,second,resource_root=source)
    assert verify_snapshot(snapshot,library=root)==snapshot
    (root/'prop_A@1'/'operator-private.txt').write_text('Not a declared asset resource')
    output=tmp_path/'packs';output.mkdir()
    result=export_pack(snapshot,library=root,output_root=output)
    assert verify_pack(output/result['bundle'])['snapshot']==snapshot
    assert not list((output/result['bundle']).rglob('operator-private.txt'))
    assert export_pack(snapshot,library=root,output_root=output)==result


def test_pack_extra_file_or_mutation_refused(tmp_path):
    _,root,_=library(tmp_path)
    snapshot=snapshot_assets([{'asset_id':'prop_A','version':'1'}],library=root)
    output=tmp_path/'packs';output.mkdir()
    result=export_pack(snapshot,library=root,output_root=output)
    folder=output/result['bundle']
    (folder/'unexpected.txt').write_text('extra')
    with pytest.raises(ProductionError):verify_pack(folder)
    (folder/'unexpected.txt').unlink()
    (folder/'assets/prop_A@1/reference.bin').write_bytes(b'changed')
    with pytest.raises(ProductionError):verify_pack(folder)


def test_snapshot_duplicate_or_retired_alias_not_selected(tmp_path):
    _,root,_=library(tmp_path)
    selection=[{'asset_id':'prop_A','version':'1'}]
    with pytest.raises(ProductionError):snapshot_assets(selection+selection,library=root)
    with pytest.raises(Exception):snapshot_assets([{'asset_id':'prop_A','version':'latest'}],library=root)


def test_import_roundtrip_delta_and_failure_keep_prior_generation(tmp_path, monkeypatch):
    from fsf.production.assets import import_pack, pack_delta
    from fsf.core.asset_store import lookup_asset
    from fsf.core.edit_store import EditStoreError
    import fsf.production.contracts as contracts
    source, root, first = library(tmp_path)
    packs = tmp_path / 'packs'
    packs.mkdir()
    selection = [{'asset_id': 'prop_A', 'version': '1'}]
    one = export_pack(snapshot_assets(selection, library=root), library=root, output_root=packs)
    original = packs / one['bundle']
    imported = tmp_path / 'clean-generation'
    assert import_pack(original, output=imported)['reused'] is False
    assert lookup_asset(imported / 'assets', 'prop_A', '1')['record_digest'] == first['record_digest']
    assert import_pack(original, output=imported)['reused'] is True
    extra = copy.deepcopy(first)
    extra['asset_id'] = 'prop_B'
    seal(extra)
    register_asset(root, extra, resource_root=source)
    selection.append({'asset_id': 'prop_B', 'version': '1'})
    two = export_pack(snapshot_assets(selection, library=root), library=root, output_root=packs)
    target = packs / two['bundle']
    delta = pack_delta(original, target)
    assert delta['added'] and delta['reused'] and delta['removed'] == []
    assert all(item['path'].startswith('assets/prop_B@1/') for item in delta['added'])
    with pytest.raises(ProductionError, match='CONFLICT'):
        import_pack(target, output=imported)
    destination = tmp_path / 'updated-generation'
    publish = contracts.publish

    def fail_publish(path, value):
        raise OSError('injected commit failure')

    monkeypatch.setattr(contracts, 'publish', fail_publish)
    with pytest.raises(EditStoreError, match='EDIT_IO'):
        import_pack(target, output=destination, base_bundle=original)
    assert not destination.exists()
    assert verify_pack(imported) == verify_pack(original)
    assert not list(tmp_path.glob('.pack-import-*'))
    monkeypatch.setattr(contracts, 'publish', publish)
    result = import_pack(target, output=destination, base_bundle=original)
    assert result['base_resource_count'] == len(delta['reused'])
    assert verify_pack(destination) == verify_pack(target)
