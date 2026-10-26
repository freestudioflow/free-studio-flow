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
