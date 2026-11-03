"""Declaration safeguards; these arithmetic tests do not qualify an engine/device."""
import copy
import json
import struct

import pytest

from fsf.production.contracts import ProductionError, seal
from fsf.production.delivery import static_room_profile, validate_spatial, read_glb_metadata


def fixture():
    profile = static_room_profile()
    spatial = seal({'schema_version':'fsf_spatial_delivery_v1','pack_digest':'a'*64,
        'profile_digest':profile['profile_digest'],'meters_per_unit':1,'handedness':'right','up':'+Y','authored_front':'+Z',
        'root_transform_column_major':[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
        'ground_plane':[0,1,0,0],'placement_mode':'local_ground','local_anchor_id':'ground','target_consumer':'godot_static',
        'objects':[{'object_id':'cube','scene_index':0,'node_index':0,'pivot_m':[0,0,0],
                    'expected_bounds_m':{'minimum':[-0.5,0,-0.5],'maximum':[0.5,1,0.5]},'collision_mode':'convex'}]},'spatial_digest')
    return profile, spatial


def test_schema_and_identity_bindings_do_not_attest_runtime():
    profile, spatial = fixture()
    assert validate_spatial(spatial,profile=profile,pack_digest='a'*64)==spatial
    with pytest.raises(ProductionError,match='BINDING'):
        validate_spatial(spatial,profile=profile,pack_digest='b'*64)


@pytest.mark.parametrize('change', ['centimeters','up','front','mirror','singular','nonuniform','shear','nan','duplicate_id','duplicate_node','reversed_bounds'])
def test_invalid_units_matrices_and_object_maps_rejected(change):
    profile, spatial = fixture()
    matrix = spatial['root_transform_column_major']
    if change=='centimeters':spatial['meters_per_unit']=0.01
    elif change=='up':spatial['up']='+Z'
    elif change=='front':spatial['authored_front']='-Z'
    elif change=='mirror':matrix[0]=-1
    elif change=='singular':matrix[0]=0
    elif change=='nonuniform':matrix[0]=2
    elif change=='shear':matrix[4]=0.2
    elif change=='nan':matrix[0]=float('nan')
    elif change.startswith('duplicate'):
        spatial['objects'].append(copy.deepcopy(spatial['objects'][0]))
        if change=='duplicate_node':spatial['objects'][1]['object_id']='another'
        else:spatial['objects'][1]['node_index']=1
    else:spatial['objects'][0]['expected_bounds_m']['maximum'][0]=-1
    if change=='nan':
        with pytest.raises(ValueError):seal(spatial,'spatial_digest')
        return
    spatial=seal(spatial,'spatial_digest')
    with pytest.raises(ProductionError):validate_spatial(spatial,profile=profile,pack_digest='a'*64)


@pytest.mark.parametrize('uri', ['../outside.bin','https://example.test/data.bin','data:application/octet-stream;base64,AA=='])
def test_glb_external_resources_refused_before_native_tool(tmp_path, uri):
    document={'asset':{'version':'2.0'},'scenes':[{'nodes':[]}],'buffers':[{'byteLength':1,'uri':uri}]}
    content=json.dumps(document).encode()
    content+=b' '*((-len(content))%4)
    path=tmp_path/'external.glb'
    path.write_bytes(struct.pack('<4sIIII',b'glTF',2,20+len(content),len(content),0x4e4f534a)+content)
    with pytest.raises(ProductionError,match='EXTERNAL_REFERENCE'):
        read_glb_metadata(path,profile=static_room_profile())
