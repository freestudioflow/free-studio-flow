"""Static scene delivery and FSF local spatial metadata, not an XR runtime."""
import math

from fsf.production.contracts import ProductionError, seal, validate


def read_glb_metadata(path, *, profile):
    """Read only the GLB JSON header; mesh/buffer validation belongs to Khronos."""
    import struct
    from fsf.core.asset_library import _open_reader
    from fsf.core.edit_store import decode_json
    from fsf.production.control import file_identity
    identity = file_identity(path, profile['budgets']['glb_bytes_max'])
    with _open_reader(path) as stream:
        header = stream.read(20)
        if len(header) != 20:
            raise ProductionError('PRODUCTION_GLB_HEADER')
        magic, version, length, size, kind = struct.unpack('<4sIIII',header)
        if magic != b'glTF' or version != 2 or length != identity['size_bytes'] or kind != 0x4e4f534a or size>8*1024**2:
            raise ProductionError('PRODUCTION_GLB_HEADER')
        document = decode_json(stream.read(size))
    if document.get('asset',{}).get('version') != '2.0':
        raise ProductionError('PRODUCTION_GLB_HEADER')
    if any('uri' in item for field in ('buffers','images') for item in document.get(field,[])):
        raise ProductionError('PRODUCTION_GLB_EXTERNAL_REFERENCE')
    if (document.get('extensionsRequired') or document.get('skins') or document.get('animations')
            or len(document.get('scenes',[])) != 1):
        raise ProductionError('PRODUCTION_DELIVERY_UNSUPPORTED')
    if len(document.get('nodes',[])) > profile['budgets']['nodes_max']:
        raise ProductionError('PRODUCTION_DELIVERY_LIMIT')
    for mesh in document.get('meshes',[]):
        for primitive in mesh.get('primitives',[]):
            if primitive.get('mode',4) != 4 or primitive.get('targets'):
                raise ProductionError('PRODUCTION_DELIVERY_UNSUPPORTED')
    return document, identity


def static_room_profile():
    return seal({'schema_version': 'fsf_gaming_delivery_profile_v1', 'profile_id': 'godot-static-room-v1',
                 'engine_version': '4.5.1-stable', 'validator_version': '2.0.0-dev.3.10',
                 'asset_format': 'glTF-2.0-GLB',
                 'budgets': {'package_bytes_max': 512*1024**2, 'glb_bytes_max': 256*1024**2,
                             'nodes_max': 512, 'triangles_total_max': 250000, 'triangles_prop_max': 50000,
                             'materials_max': 16, 'texture_edge_max': 2048, 'images_bytes_max': 64*1024**2},
                 'required_extensions': [], 'collision_modes': ['none','convex','static_trimesh'],
                 'tool_deadline_seconds': 120, 'harness_deadline_seconds': 300,
                 'tool_rss_bytes_max': 2*1024**3, 'bounds_tolerance_m': 1e-4,
                 'collision_tolerance_m': 1e-3, 'material_tolerance': 1e-4}, 'profile_digest')


def validate_spatial(spatial, *, profile, pack_digest):
    """Check declared transforms and identities; engine-measured bounds are separate."""
    validate(profile, 'fsf_gaming_delivery_profile_v1', 'profile_digest')
    validate(spatial, 'fsf_spatial_delivery_v1', 'spatial_digest')
    if spatial['profile_digest'] != profile['profile_digest'] or spatial['pack_digest'] != pack_digest:
        raise ProductionError('PRODUCTION_DELIVERY_BINDING')
    matrix = spatial['root_transform_column_major']
    if any(not math.isfinite(value) for value in matrix):
        raise ProductionError('PRODUCTION_SPATIAL_TRANSFORM')
    if any(abs(matrix[index] - expected) > 1e-8 for index, expected in ((3,0),(7,0),(11,0),(15,1))):
        raise ProductionError('PRODUCTION_SPATIAL_TRANSFORM')
    columns = [[matrix[column*4+row] for row in range(3)] for column in range(3)]
    norms = [math.sqrt(sum(value*value for value in column)) for column in columns]
    if min(norms) <= 1e-8 or max(norms)-min(norms) > 1e-6*max(norms):
        raise ProductionError('PRODUCTION_SPATIAL_TRANSFORM')
    for first in range(3):
        for second in range(first+1,3):
            if abs(sum(a*b for a,b in zip(columns[first],columns[second]))) > 1e-6*norms[first]*norms[second]:
                raise ProductionError('PRODUCTION_SPATIAL_TRANSFORM')
    a,b,c = columns
    determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-b[0]*(a[1]*c[2]-a[2]*c[1])+c[0]*(a[1]*b[2]-a[2]*b[1])
    if determinant <= 0:
        raise ProductionError('PRODUCTION_SPATIAL_TRANSFORM')
    identities, nodes = set(), set()
    for obj in spatial['objects']:
        identity = obj['object_id']
        node = (obj['scene_index'],obj['node_index'])
        if identity in identities or node in nodes:
            raise ProductionError('PRODUCTION_SPATIAL_IDENTITY')
        identities.add(identity)
        nodes.add(node)
        bounds = obj['expected_bounds_m']
        if any(not low < high for low, high in zip(bounds['minimum'], bounds['maximum'])):
            raise ProductionError('PRODUCTION_SPATIAL_BOUNDS')
    return spatial


def compare_engine_observation(observation, *, spatial, profile, pack_digest):
    """Evaluate measured node-index bounds; declarations cannot stand in for import."""
    validate_spatial(spatial, profile=profile, pack_digest=pack_digest)
    if not isinstance(observation, dict) or set(observation) != {'engine_version','objects','materials','images','surfaces','triangles','rays'}:
        raise ProductionError('PRODUCTION_ENGINE_REPORT')
    if observation['engine_version'] != '4.5.1.stable.official.f62fdbde1':
        raise ProductionError('PRODUCTION_ENGINE_VERSION')
    observed = observation['objects']
    if not isinstance(observed, list):
        raise ProductionError('PRODUCTION_ENGINE_REPORT')
    mapping = {}
    for item in observed:
        node = (item['scene_index'],item['node_index'])
        if node in mapping:
            raise ProductionError('PRODUCTION_ENGINE_REPORT')
        mapping[node] = item
    tolerance = profile['bounds_tolerance_m']
    for expected in spatial['objects']:
        item = mapping.get((expected['scene_index'],expected['node_index']))
        if item is None:
            raise ProductionError('PRODUCTION_SPATIAL_IDENTITY')
        for field in ('minimum','maximum'):
            numbers = item['bounds_m'][field]
            if len(numbers) != 3 or any(not math.isfinite(value) or abs(value-target)>tolerance
                    for value,target in zip(numbers,expected['expected_bounds_m'][field])):
                raise ProductionError('PRODUCTION_SPATIAL_BOUNDS')
        if len(item['pivot_m']) != 3 or any(not math.isfinite(value) or abs(value-target)>tolerance
                for value,target in zip(item['pivot_m'],expected['pivot_m'])):
            raise ProductionError('PRODUCTION_SPATIAL_BOUNDS')
    budgets = profile['budgets']
    if observation['triangles'] > budgets['triangles_total_max'] or len(observed) > budgets['nodes_max']:
        raise ProductionError('PRODUCTION_DELIVERY_LIMIT')
    return {'profile': 'passed', 'engine_import': 'passed', 'appearance': 'not_evaluated',
            'human_review': 'not_reviewed', 'device': 'not_tested',
            'spatial_digest': spatial['spatial_digest'], 'pack_digest': pack_digest,
            'profile_digest': profile['profile_digest']}


def inspect_static_scene(bundle, relative, *, profile, spatial, rays, tools, work_root, cancel_check=None):
    """Run the optional pinned tools against data copied into a controlled project."""
    import json
    import os
    from pathlib import Path
    import shutil
    import tempfile
    import time
    from importlib import resources
    from fsf.core.asset_library import _root, _path_parts, _open_reader
    from fsf.core.edit_store import read_json
    from fsf.production.assets import verify_pack
    from fsf.production.contracts import digest, publish, key
    from fsf.production.control import file_identity
    from fsf.production.delivery_tools import run_delivery_tool
    root, output = _root(bundle), _root(work_root)
    pack = verify_pack(root)
    validate_spatial(spatial, profile=profile, pack_digest=pack['pack_digest'])
    _path_parts(relative)
    listed = {item['path'] for item in pack['resources']}
    if relative not in listed or not relative.lower().endswith('.glb'):
        raise ProductionError('PRODUCTION_DELIVERY_BINDING')
    denied = {'.gd','.gdc','.cs','.dll','.so','.exe','.py','.sh'}
    if any(Path(name).suffix.casefold() in denied or Path(name).name.casefold() == 'project.godot' for name in listed):
        raise ProductionError('PRODUCTION_DELIVERY_EXECUTABLE')
    if pack['total_bytes'] > profile['budgets']['package_bytes_max']:
        raise ProductionError('PRODUCTION_DELIVERY_LIMIT')
    if not isinstance(tools,dict) or set(tools) != {'godot','validator'}:
        raise ProductionError('PRODUCTION_TOOL_CONFIG')
    if not isinstance(rays,list) or len(rays)<6 or len(rays)>128:
        raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
    ray_ids, hits, misses = set(), 0, 0
    object_ids = {item['object_id'] for item in spatial['objects']}
    for ray in rays:
        if set(ray) != {'ray_id','from_m','to_m','object_id','distance_m'}:
            raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
        key(ray['ray_id'])
        if ray['ray_id'] in ray_ids:
            raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
        ray_ids.add(ray['ray_id'])
        if any(not isinstance(ray[field],list) or len(ray[field]) != 3 or any(type(number) not in (int,float) or not math.isfinite(number) for number in ray[field]) for field in ('from_m','to_m')):
            raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
        if ray['object_id'] is None:
            misses += 1
            if ray['distance_m'] is not None:
                raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
        else:
            hits += 1
            if ray['object_id'] not in object_ids or type(ray['distance_m']) not in (int,float) or not math.isfinite(ray['distance_m']) or ray['distance_m']<0:
                raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
    if min(hits,misses)<3:
        raise ProductionError('PRODUCTION_COLLISION_FIXTURE')
    source = root.joinpath(*relative.split('/'))
    metadata, identity = read_glb_metadata(source, profile=profile)
    deadline = time.monotonic()+profile['harness_deadline_seconds']
    with tempfile.TemporaryDirectory(prefix='.scene-delivery-',dir=output) as temporary:
        scratch = Path(temporary)
        def failed_tool(code, result, stats):
            import uuid
            diagnostic = {'status':'failed','code':code,'tool':stats,'returncode':result.returncode,
                          'stdout':result.stdout[:1024**2],'stderr':result.stderr[:1024**2],
                          'diagnostic_truncated':len(result.stdout)>1024**2 or len(result.stderr)>1024**2}
            log = scratch/'engine.log'
            if log.is_file():
                with log.open('rb') as stream:
                    diagnostic['native_log'] = stream.read(1024**2).decode('utf-8',errors='replace')
            publish(scratch/'failure.json',diagnostic)
            os.rename(scratch,output/('failed-'+uuid.uuid4().hex))
            scratch.mkdir()
            raise ProductionError(code)
        with _open_reader(source) as incoming, (scratch/'scene.glb').open('xb') as outgoing:
            shutil.copyfileobj(incoming,outgoing,1024*1024)
        if file_identity(scratch/'scene.glb') != identity:
            raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        validator, validator_stats = run_delivery_tool(tools['validator'],
            ['--stdout','--no-write-timestamp','--no-absolute-path',str(scratch/'scene.glb')],
            cwd=scratch,deadline=deadline,cancel_check=cancel_check)
        try:
            format_report = json.loads(validator.stdout)
        except (ValueError,TypeError):
            raise ProductionError('PRODUCTION_FORMAT_REPORT') from None
        if format_report.get('validatorVersion') != profile['validator_version']:
            raise ProductionError('PRODUCTION_VALIDATOR_VERSION')
        if validator.returncode or format_report.get('issues',{}).get('numErrors') != 0:
            failed_tool('PRODUCTION_FORMAT_REJECTED',validator,validator_stats)
        if format_report['issues']['numWarnings']:
            raise ProductionError('PRODUCTION_FORMAT_REVIEW_REQUIRED')
        harness = resources.files('fsf.production').joinpath('godot_static_probe.gd').read_bytes()
        (scratch/'probe.gd').write_bytes(harness)
        (scratch/'project.godot').write_text('[application]\nconfig/name="FSF static asset probe"\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
        publish(scratch/'probe.json',{'spatial':spatial,'rays':rays})
        engine, engine_stats = run_delivery_tool(tools['godot'],
            ['--headless','--path',str(scratch),'--log-file',str(scratch/'engine.log'),'--script','res://probe.gd'],
            cwd=scratch,deadline=deadline,cancel_check=cancel_check)
        if engine.returncode:
            failed_tool('PRODUCTION_ENGINE_IMPORT',engine,engine_stats)
        observation = read_json(scratch/'engine-report.json')
        result = compare_engine_observation(observation,spatial=spatial,profile=profile,pack_digest=pack['pack_digest'])
        source_materials = metadata.get('materials',[])
        if len(source_materials) != len(observation['materials']) or len(source_materials)>profile['budgets']['materials_max']:
            raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
        for declared, observed in zip(source_materials,observation['materials']):
            pbr = declared.get('pbrMetallicRoughness',{})
            for field, source_field in (('metallic','metallicFactor'),('roughness','roughnessFactor')):
                if abs(observed[field]-pbr.get(source_field,1))>profile['material_tolerance']:
                    raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
            if any(abs(value-target)>profile['material_tolerance'] for value,target in zip(observed['albedo'],pbr.get('baseColorFactor',[1,1,1,1]))):
                raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
            if observed['alpha_mode'] != declared.get('alphaMode','OPAQUE'):
                raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
            if observed['alpha_mode']=='MASK' and abs(observed['alpha_cutoff']-declared.get('alphaCutoff',0.5))>profile['material_tolerance']:
                raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
            for field, texture in (('albedo',pbr.get('baseColorTexture')),('normal',declared.get('normalTexture'))):
                image = metadata['textures'][texture['index']]['source'] if texture else None
                if observed[field+'_image_index'] != image:
                    raise ProductionError('PRODUCTION_MATERIAL_MISMATCH')
            if any(name in declared for name in ('occlusionTexture','emissiveTexture')) or pbr.get('metallicRoughnessTexture'):
                # These need additional channel/binding observations before this profile can attest them.
                raise ProductionError('PRODUCTION_MATERIAL_UNOBSERVED')
        if len(observation['images']) != len(metadata.get('images',[])):
            raise ProductionError('PRODUCTION_TEXTURE_MISMATCH')
        image_bytes = 0
        for declared, observed in zip(metadata.get('images',[]),observation['images']):
            if declared.get('mimeType') not in ('image/png','image/jpeg') or 'bufferView' not in declared:
                raise ProductionError('PRODUCTION_TEXTURE_MISMATCH')
            image_bytes += metadata['bufferViews'][declared['bufferView']]['byteLength']
            if max(observed['width'],observed['height'])>profile['budgets']['texture_edge_max']:
                raise ProductionError('PRODUCTION_DELIVERY_LIMIT')
        if image_bytes>profile['budgets']['images_bytes_max']:
            raise ProductionError('PRODUCTION_DELIVERY_LIMIT')
        observed_rays = {item['ray_id']:item for item in observation['rays']}
        if len(observed_rays) != len(rays):
            raise ProductionError('PRODUCTION_COLLISION_MISMATCH')
        for expected in rays:
            actual = observed_rays.get(expected['ray_id'])
            if actual is None or actual['object_id'] != expected['object_id']:
                raise ProductionError('PRODUCTION_COLLISION_MISMATCH')
            if expected['distance_m'] is not None and (type(actual['distance_m']) not in (int,float) or not math.isfinite(actual['distance_m']) or abs(actual['distance_m']-expected['distance_m'])>profile['collision_tolerance_m']):
                raise ProductionError('PRODUCTION_COLLISION_MISMATCH')
        if file_identity(source) != identity or verify_pack(root) != pack:
            raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        receipt = seal({'schema_version':'fsf_static_delivery_receipt_v1',**result,
                        'source_sha256':identity['sha256'],'harness_sha256':__import__('hashlib').sha256(harness).hexdigest(),
                        'observation_digest':digest(observation),'format_report_digest':digest(format_report),
                        'format':'passed','godot':engine_stats,'validator':validator_stats,
                        'bounds_and_collision':'passed','material_validation':'scalar_and_texture_bindings_observed'},'receipt_digest')
        publish(scratch/'format-report.json',format_report)
        publish(scratch/'receipt.json',receipt)
        destination = output/receipt['receipt_digest']
        if destination.exists():
            raise ProductionError('PRODUCTION_TARGET_EXISTS')
        os.rename(scratch,destination)
        scratch.mkdir()
        return receipt
