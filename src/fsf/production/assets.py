"""Fixed library snapshots; retrieval cannot silently replace compiled references."""
from fsf.core.asset_store import lookup_asset
from fsf.production.contracts import ProductionError, digest, key, seal


def export_pack(snapshot, *, library, output_root):
    """Publish only declared resources, atomically as a self-contained library."""
    from fsf.core.asset_library import _root,_no_links,_open_reader
    from fsf.core.edit_store import edit_lock
    from fsf.production.contracts import publish
    from fsf.production.control import file_identity
    import os
    from pathlib import Path
    import tempfile
    verify_snapshot(snapshot,library=library)
    source,root=_root(library),_root(output_root)
    with edit_lock(root/'.asset-pack'):
        with tempfile.TemporaryDirectory(prefix='.asset-pack-',dir=root) as temp:
            scratch=Path(temp)
            (scratch/'assets').mkdir()
            resources=[]
            total=0
            for entry in snapshot['assets']:
                name=entry['asset_id']+'@'+entry['version']
                record=lookup_asset(source,entry['asset_id'],entry['version'])
                incoming=source/name
                names=['record.json']+[a['logical_path'] for a in record['artifacts']]
                for relative in names:
                    original=incoming/relative
                    _no_links(original)
                    identity=file_identity(original)
                    total+=identity['size_bytes']
                    if total>64*1024**3:raise ProductionError('PRODUCTION_ASSET_LIMIT')
                    target=scratch/'assets'/name/relative
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with _open_reader(original) as inp,target.open('xb') as out:
                        copied=0
                        while block:=inp.read(1024*1024):
                            copied+=len(block)
                            if copied>identity['size_bytes']:raise ProductionError('PRODUCTION_ASSET_CHANGED')
                            out.write(block)
                        out.flush();os.fsync(out.fileno())
                    if file_identity(target)!=identity:raise ProductionError('PRODUCTION_ASSET_CHANGED')
                    resources.append({'path':target.relative_to(scratch).as_posix(),**identity})
            verify_snapshot(snapshot,library=source)
            manifest=seal({'schema_version':'fsf_asset_pack_v1','snapshot':snapshot,
                           'resources':resources,'total_bytes':total},'pack_digest')
            publish(scratch/'pack.json',manifest)
            verify_pack(scratch)
            destination=root/manifest['pack_digest']
            if destination.exists():
                if verify_pack(destination)!=manifest:raise ProductionError('PRODUCTION_ASSET_CONFLICT')
            else:
                os.rename(scratch,destination)
                # TemporaryDirectory cleanup needs its original empty directory.
                scratch.mkdir()
            return {'bundle':destination.name,'pack_digest':manifest['pack_digest']}


def verify_pack(folder):
    """No unrelated library files or hidden credential files enter an asset pack."""
    from fsf.core.asset_library import _root,_path_parts
    from fsf.core.edit_store import read_json
    from fsf.production.control import file_identity
    root=_root(folder)
    value=read_json(root/'pack.json')
    if (set(value)!={'schema_version','snapshot','resources','total_bytes','pack_digest'}
            or value['schema_version']!='fsf_asset_pack_v1' or seal(value,'pack_digest')!=value):
        raise ProductionError('PRODUCTION_ASSET_INVALID')
    if not isinstance(value['resources'], list):
        raise ProductionError('PRODUCTION_ASSET_INVALID')
    files=set()
    portable=set()
    total=0
    for item in value['resources']:
        if set(item)!={'path','sha256','size_bytes'}:raise ProductionError('PRODUCTION_ASSET_INVALID')
        _path_parts(item['path'])
        if item['path'].casefold() in portable or not item['path'].startswith('assets/'):
            raise ProductionError('PRODUCTION_ASSET_INVALID')
        portable.add(item['path'].casefold())
        from fsf.production.contracts import integer
        integer(item['size_bytes'], 1, 64*1024**3)
        if file_identity(root/item['path'])!={k:item[k] for k in ('sha256','size_bytes')}:
            raise ProductionError('PRODUCTION_ASSET_CHANGED')
        files.add(item['path']);total+=item['size_bytes']
        if total > 64*1024**3:
            raise ProductionError('PRODUCTION_ASSET_LIMIT')
    if total!=value['total_bytes']:raise ProductionError('PRODUCTION_ASSET_INVALID')
    declared=set()
    for entry in value['snapshot']['assets']:
        name=entry['asset_id']+'@'+entry['version']
        record=lookup_asset(root/'assets',entry['asset_id'],entry['version'])
        declared.add('assets/'+name+'/record.json')
        declared.update('assets/'+name+'/'+a['logical_path'] for a in record['artifacts'])
    if files!=declared:raise ProductionError('PRODUCTION_ASSET_INVALID')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual!=files|{'pack.json'}:raise ProductionError('PRODUCTION_ASSET_INVALID')
    verify_snapshot(value['snapshot'],library=root/'assets')
    return value


def snapshot_assets(selections, *, library):
    if not isinstance(selections,list) or len(selections) > 4096:
        raise ProductionError("PRODUCTION_ASSET_INVALID")
    seen,entries = set(),[]
    for item in selections:
        if not isinstance(item,dict) or set(item) != {"asset_id","version"}:
            raise ProductionError("PRODUCTION_ASSET_INVALID")
        key(item["asset_id"]);key(item["version"])
        if item["asset_id"] in seen:
            raise ProductionError("PRODUCTION_ASSET_CONFLICT")
        seen.add(item["asset_id"])
        record = lookup_asset(library,item["asset_id"],item["version"])
        entries.append({**item,"record_digest":record["record_digest"],
                        "content_hash":record["content_hash"]})
    return seal({"schema_version":"fsf_production_assets_v1","assets":entries},"snapshot_digest")


def verify_snapshot(snapshot, *, library):
    if (not isinstance(snapshot,dict) or set(snapshot) != {"schema_version","assets","snapshot_digest"}
            or snapshot["schema_version"] != "fsf_production_assets_v1"
            or seal(snapshot,"snapshot_digest") != snapshot):
        raise ProductionError("PRODUCTION_ASSET_INVALID")
    selected = [{"asset_id":e["asset_id"],"version":e["version"]} for e in snapshot["assets"]]
    if snapshot_assets(selected,library=library) != snapshot:
        raise ProductionError("PRODUCTION_ASSET_CHANGED")
    return snapshot


def pack_delta(base_bundle, target_bundle):
    """Describe immutable resource changes; same path with different bytes is a conflict."""
    base, target = verify_pack(base_bundle), verify_pack(target_bundle)
    old = {item['path']: item for item in base['resources']}
    new = {item['path']: item for item in target['resources']}
    if any(old[path] != new[path] for path in old.keys() & new.keys()):
        raise ProductionError('PRODUCTION_ASSET_CONFLICT')
    return seal({'schema_version': 'fsf_asset_pack_delta_v1',
                 'base_pack_digest': base['pack_digest'], 'target_pack_digest': target['pack_digest'],
                 'added': [new[path] for path in sorted(new.keys() - old.keys())],
                 'reused': [new[path] for path in sorted(new.keys() & old.keys())],
                 'removed': sorted(old.keys() - new.keys())}, 'delta_digest')


def import_pack(bundle, *, output, base_bundle=None):
    """Publish a clean immutable generation atomically, preserving the source pack.

    When a base is supplied, matching resources are read from that verified base.
    Readers switch generations explicitly; no live library is updated file by file.
    """
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from fsf.core.asset_library import _root, _open_reader, _no_links
    from fsf.core.edit_store import _target, edit_lock
    from fsf.production.contracts import publish
    from fsf.production.control import file_identity
    source, target = _root(bundle), Path(output).absolute()
    _root(target.parent)
    if target.exists() or target.is_symlink():
        _no_links(target)
    manifest = verify_pack(source)
    delta = pack_delta(base_bundle, source) if base_bundle is not None else None
    base = _root(base_bundle) if base_bundle is not None else None
    reused = {item['path'] for item in delta['reused']} if delta else set()
    with edit_lock(target.with_name(target.name + '.import')):
        if target.exists():
            if verify_pack(target) != manifest:
                raise ProductionError('PRODUCTION_ASSET_CONFLICT')
            return {'pack_digest': manifest['pack_digest'], 'reused': True, 'copied_bytes': 0}
        scratch = Path(tempfile.mkdtemp(prefix='.pack-import-', dir=target.parent))
        copied = 0
        try:
            for item in manifest['resources']:
                original = (base if item['path'] in reused else source) / item['path']
                destination = scratch / item['path']
                destination.parent.mkdir(parents=True, exist_ok=True)
                expected = {name: item[name] for name in ('sha256', 'size_bytes')}
                with _open_reader(original) as incoming, destination.open('xb') as outgoing:
                    count = 0
                    while block := incoming.read(1024 * 1024):
                        count += len(block)
                        if count > item['size_bytes']:
                            raise ProductionError('PRODUCTION_ASSET_CHANGED')
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if file_identity(destination) != expected or file_identity(original) != expected:
                    raise ProductionError('PRODUCTION_ASSET_CHANGED')
                copied += count
            publish(scratch / 'pack.json', manifest)
            verify_pack(scratch)
            verify_pack(source)
            if base is not None:
                verify_pack(base)
            os.rename(scratch, target)
            return {'pack_digest': manifest['pack_digest'], 'reused': False,
                    'copied_bytes': copied, 'base_resource_count': len(reused),
                    'delta_digest': delta['delta_digest'] if delta else None}
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)
