"""Multichannel bytes, analytic geometry and immutable CLI imports; no model."""
import copy
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

from PIL import Image
import pytest

from fsf.production.contracts import digest,seal,ProductionError
from fsf.production.control_bundle import import_bundle,verify,compare_rgb,Budget


def reference(root,name):
    data=(root/name).read_bytes()
    import hashlib
    return {'path':name,'sha256':hashlib.sha256(data).hexdigest(),'size_bytes':len(data)}


def fixture(root,count=2,clock=None):
    source=root/'resources';source.mkdir();out=root/'library';out.mkdir()
    (source/'frames').mkdir();(source/'manifests').mkdir()
    Image.new('RGB',(32,32),(80,100,120)).save(source/'frames/rgb.png')
    Image.new('L',(32,32),128).save(source/'frames/edge.png')
    (source/'frames/depth.bin').write_bytes(struct.pack('<f',2)*1024)
    (source/'frames/normal.bin').write_bytes(struct.pack('<fff',0,0,1)*1024)
    (source/'frames/valid.bin').write_bytes(bytes([1])*1024)
    (source/'frames/pose.json').write_text(json.dumps({'space':'image_normalized','points':[{'id':'root','x':0.5,'y':0.5,'z':0,'confidence':1}]}))
    clock=clock or {'num':24,'den':1}
    value={'schema_version':'rc11_control_bundle_v1','control_asset_id':'scene_control','clock':clock,'width':32,'height':32,
        'frame_count':count,'mapping_digest':digest({'clock':clock,'frame_count':count,'range':[0,count]}),'channels':[]}
    semantics={'rgb':{'color_space':'srgb','render':None},
        'depth':{'convention':'camera_z','units':'m','near':0.1,'far':100,'representation':'metric','reverse':False,'invalid_policy':'mask_zero_invalid'},
        'normal':{'space':'world','axes':[[1,0,0],[0,1,0],[0,0,1]],'handedness':'right','orientation':'outward','face_forward':False,'invalid_policy':'mask_zero_invalid'},
        'edge':{'generator':'synthetic-edge','parameters_digest':'a'*64},
        'pose':{'space':'image_normalized','generator':'synthetic-pose','parameters_digest':'b'*64}}
    enc={'rgb':'png_rgb8','depth':'float32_le','normal':'float32_le','edge':'png_gray8','pose':'pose_json_v1'}
    for kind in semantics:
        c={k:copy.deepcopy(value[k]) for k in ('clock','width','height','frame_count','mapping_digest')}
        c.update(id=kind,kind=kind,required=kind in {'rgb','depth','normal'},source_method='analytic_ray_cast' if kind in {'depth','normal'} else 'external',
                 encoding=enc[kind],semantics=semantics[kind],semantics_digest=digest(semantics[kind]),
                 provenance={'producer':'local-fixture','version':'1','binary_sha256':None,'qualification':'declared'})
        suffix='png' if kind in {'rgb','edge'} else 'json' if kind=='pose' else 'bin'
        rows=[]
        for n in range(count):
            row={'global_frame':n,'artifact':reference(source,'frames/'+kind+'.'+suffix)}
            if kind in {'depth','normal'}:row['invalid_mask']=reference(source,'frames/valid.bin')
            rows.append(row)
        manifest=source/'manifests'/f'{kind}.jsonl'
        manifest.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
        c['manifest']=reference(source,'manifests/'+kind+'.jsonl');value['channels'].append(c)
    return source,out,seal(value,'bundle_digest')


def revise_manifest(source,value,kind,update):
    value=copy.deepcopy(value);c=next(c for c in value['channels'] if c['kind']==kind)
    path=source/c['manifest']['path'];rows=[json.loads(x) for x in path.read_text().splitlines()]
    update(rows)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
    c['manifest']=reference(source,c['manifest']['path'])
    return seal(value,'bundle_digest')


@pytest.mark.parametrize('clock',[{'num':24,'den':1},{'num':24000,'den':1001}])
def test_all_channels_long_import_idempotence_and_exact_window(tmp_path,clock):
    source,out,value=fixture(tmp_path,120,clock)
    original=(source/'frames/rgb.png').read_bytes()
    assert import_bundle(value,resources_root=source,output_root=out)==value
    target=out/'scene_control'
    assert (target/'frames/rgb.png').read_bytes()==original
    assert import_bundle(value,resources_root=source,output_root=out)==value
    report=verify(target)
    assert report['content_conformance']=='passed' and report['provenance_attested'] is False
    assert report['rgb_quality']=='not_evaluated' and report['motion_review']=='not_reviewed'
    caps={'schema_version':'rc11_control_capability_v1','channels':[{k:c[k] for k in ('kind','encoding','semantics_digest')} for c in value['channels']]}
    window=verify(target,interval=(24,48),required=['pose'],capability=caps)
    assert window['scope']=='window_only' and all([r['global_frame'] for r in c['frames']]==list(range(24,48)) for c in window['channels'])
    with pytest.raises(ProductionError,match='CONTROL_REQUIRED_UNSUPPORTED'):verify(target,interval=(24,48),capability={**caps,'channels':[]})
    assert not list(out.glob('.control-bundle-*'))


@pytest.mark.parametrize('fault',['clock','dimensions','near_far','axes','normal_flip','digest','source_method','duplicate'])
def test_semantic_mismatch_rejected_before_publish(tmp_path,fault):
    source,out,value=fixture(tmp_path)
    depth=value['channels'][1];normal=value['channels'][2]
    if fault=='clock':depth['clock']={'num':25,'den':1}
    elif fault=='dimensions':depth['width']=64
    elif fault=='near_far':depth['semantics']['near']=101
    elif fault=='axes':normal['semantics']['axes'][1]=[1,0,0]
    elif fault=='normal_flip':normal['semantics']['face_forward']=True
    elif fault=='digest':depth['semantics_digest']='0'*64
    elif fault=='source_method':depth['source_method']='cycles_by_filename'
    elif fault=='duplicate':value['channels'][1]['id']='rgb'
    if fault!='digest':
        for c in value['channels']:c['semantics_digest']=digest(c['semantics'])
    with pytest.raises(ProductionError):import_bundle(seal(value,'bundle_digest'),resources_root=source,output_root=out)
    assert not (out/'scene_control').exists()


@pytest.mark.parametrize('fault',['missing','duplicate','wrong_sha','wrong_size','escape','reserved','ref_conflict'])
def test_manifest_and_reference_faults_leave_no_partial_bundle(tmp_path,fault):
    source,out,value=fixture(tmp_path)
    def edit(rows):
        if fault=='missing':rows.pop()
        elif fault=='duplicate':rows[1]['global_frame']=0
        elif fault=='wrong_sha':rows[0]['artifact']['sha256']='0'*64
        elif fault=='wrong_size':rows[0]['artifact']['size_bytes']+=1
        elif fault=='escape':rows[0]['artifact']['path']='../escape'
        elif fault=='reserved':rows[0]['artifact']['path']='bundle.json'
        else:rows[1]['artifact']['sha256']='0'*64
    value=revise_manifest(source,value,'rgb',edit)
    with pytest.raises(ValueError):import_bundle(value,resources_root=source,output_root=out)
    assert not (out/'scene_control').exists() and not list(out.glob('.control-bundle-*'))


@pytest.mark.parametrize('kind,payload',[('depth',struct.pack('<f',float('nan'))),('depth',struct.pack('<f',101)),('normal',struct.pack('<fff',0,0,0))])
def test_invalid_numeric_payload_cannot_pass_by_rehashing(tmp_path,kind,payload):
    source,out,value=fixture(tmp_path)
    (source/f'frames/{kind}.bin').write_bytes(payload*1024)
    value=revise_manifest(source,value,kind,lambda rows:[r.update(artifact=reference(source,f'frames/{kind}.bin')) for r in rows])
    with pytest.raises(ProductionError):import_bundle(value,resources_root=source,output_root=out)


def test_all_invalid_mask_is_not_ready_required_data(tmp_path):
    source,out,value=fixture(tmp_path)
    (source/'frames/valid.bin').write_bytes(bytes(1024))
    for kind in ('depth','normal'):
        value=revise_manifest(source,value,kind,lambda rows:[r.update(invalid_mask=reference(source,'frames/valid.bin')) for r in rows])
    with pytest.raises(ProductionError,match='CONTROL_REQUIRED_UNMEASURABLE'):import_bundle(value,resources_root=source,output_root=out)


def test_sparse_big_error_fails_local_conversion_ceiling(tmp_path):
    a=tmp_path/'a.png';b=tmp_path/'b.png'
    Image.new('RGB',(32,32),(50,50,50)).save(a)
    image=Image.open(a);image.putpixel((0,0),(219,50,50));image.save(b);image.close()
    with pytest.raises(ProductionError,match='CONTROL_CONVERSION'):compare_rgb(a,b,32,32,'lossy_rgb_reference_v1_1',Budget(30))
    b.write_bytes(a.read_bytes())
    assert compare_rgb(a,b,32,32,'lossless_samples_v1',Budget(30))['max_abs_error_8bit']==0


def test_cli_import_verify_window_and_digest_conflict(tmp_path):
    source,out,value=fixture(tmp_path)
    definition=tmp_path/'definition.json';definition.write_text(json.dumps(value))
    def cli(*args):
        return subprocess.run([sys.executable,'-m','fsf.cli','blender',*map(str,args)],capture_output=True,text=True,timeout=30)
    result=cli('bundle-import',definition,'--resources',source,'--out',out)
    assert result.returncode==0,result.stdout
    result=cli('bundle-verify',out/'scene_control');assert result.returncode==0,result.stdout
    result=cli('bundle-verify',out/'scene_control','--seconds','5401')
    assert result.returncode==2 and 'CONTROL_LIMIT' in result.stdout
    result=cli('bundle-verify',out/'scene_control','--seconds','30');assert result.returncode==0,result.stdout
    caps=tmp_path/'caps.json';caps.write_text(json.dumps({'schema_version':'rc11_control_capability_v1','channels':[]}))
    result=cli('bundle-window',out/'scene_control','--start','0','--end','1','--capability',caps,'--out',tmp_path/'window.json')
    assert result.returncode==2 and 'CONTROL_REQUIRED_UNSUPPORTED' in result.stdout
    changed=copy.deepcopy(value);changed['channels'][0]['provenance']['version']='2'
    with pytest.raises(ProductionError,match='CONTROL_CONFLICT'):import_bundle(seal(changed,'bundle_digest'),resources_root=source,output_root=out)


def test_analytic_plane_depth_normal_and_projection_have_independent_equation(tmp_path):
    source,out,value=fixture(tmp_path,1)
    # Analytic ray intersections with z = 2 + 0.2*x, not a renderer pass.
    rays=[((x+0.5-16)/32,(y+0.5-16)/32,1) for y in range(32) for x in range(32)]
    depths=[2/(1-0.2*r[0]) for r in rays]
    normal=(-0.2/math.sqrt(1.04),0,1/math.sqrt(1.04))
    (source/'frames/depth.bin').write_bytes(struct.pack('<'+'f'*1024,*depths))
    (source/'frames/normal.bin').write_bytes(struct.pack('<fff',*normal)*1024)
    for kind in ('depth','normal'):
        value=revise_manifest(source,value,kind,lambda rows:[r.update(artifact=reference(source,f'frames/{kind}.bin')) for r in rows])
    import_bundle(value,resources_root=source,output_root=out)
    target=out/'scene_control'
    actual=struct.unpack('<'+'f'*1024,(target/'frames/depth.bin').read_bytes())
    for i,(r,z) in enumerate(zip(rays,actual)):
        point=(r[0]*z,r[1]*z,z)
        assert abs(point[2]-0.2*point[0]-2)<0.001
        assert abs(point[0]/point[2]*32+16-(i%32+0.5))<0.25
    n=struct.unpack('<fff',(target/'frames/normal.bin').read_bytes()[:12])
    angle=math.degrees(math.acos(min(1,max(-1,sum(a*b for a,b in zip(n,normal))/math.sqrt(sum(a*a for a in n))))))
    assert angle<0.1
    report=verify(target)
    assert report['geometry_accuracy']=='not_evaluated' and not report['provenance_attested']
    assert report==seal(report,'verification_digest')


def test_lossless_samples_allow_different_png_compression(tmp_path):
    a=tmp_path/'a.png';b=tmp_path/'b.png'
    im=Image.new('RGB',(32,32),(42,73,101))
    im.save(a,compress_level=0);im.save(b,compress_level=9)
    assert a.read_bytes()!=b.read_bytes()
    assert compare_rgb(a,b,32,32,'lossless_samples_v1',Budget(30))['ssim']==1


@pytest.mark.parametrize('fault',['engine','denoiser','asset_name','capability','deadline'])
def test_metadata_and_bounded_execution_fail_closed(tmp_path,monkeypatch,fault):
    from fsf.production import control_bundle as module
    source,out,value=fixture(tmp_path,1)
    if fault in {'engine','denoiser'}:
        render={'engine':'CYCLES','samples':64,'seed':17,'denoiser':None,'material_profile':'clay_v1','world_profile':'gray'}
        if fault=='engine':render['engine']=[]
        else:render['denoiser']={'name':'test','version':'1','sha256':'not-a-sha','settings_digest':'a'*64}
        c=value['channels'][0];c['semantics']['render']=render;c['semantics_digest']=digest(c['semantics'])
    elif fault=='asset_name':value['control_asset_id']='CON'
    value=seal(value,'bundle_digest')
    if fault=='capability':
        import_bundle(value,resources_root=source,output_root=out)
        with pytest.raises(ProductionError,match='CONTROL_CAPABILITY'):
            verify(out/'scene_control',capability={'schema_version':'rc11_control_capability_v1','channels':[{'kind':'rgb'}]})
        return
    if fault=='deadline':
        counter=iter(range(0,100000,301));monkeypatch.setattr(module.time,'monotonic',lambda:next(counter))
    with pytest.raises(ValueError):import_bundle(value,resources_root=source,output_root=out)
    assert not (out/value['control_asset_id']).exists() and not list(out.glob('.control-bundle-*'))


def test_read_time_rgb_change_is_not_accepted(tmp_path,monkeypatch):
    from fsf.production import control_bundle as module
    source,out,value=fixture(tmp_path,1)
    import_bundle(value,resources_root=source,output_root=out)
    target=out/'scene_control';original=module._image
    def change_after_decode(path,*args):
        image=original(path,*args)
        if path.name=='rgb.png':path.write_bytes(path.read_bytes()+b'changed')
        return image
    monkeypatch.setattr(module,'_image',change_after_decode)
    with pytest.raises(ProductionError):verify(target)


@pytest.mark.parametrize('points',[[],[{'id':'root','x':0.5,'y':0.5,'z':0,'confidence':0}]])
def test_empty_or_unobserved_required_pose_is_not_usable(tmp_path,points):
    source,out,value=fixture(tmp_path,1)
    (source/'frames/pose.json').write_text(json.dumps({'space':'image_normalized','points':points}))
    value=revise_manifest(source,value,'pose',lambda rows:[r.update(artifact=reference(source,'frames/pose.json')) for r in rows])
    value['channels'][-1]['required']=True;value=seal(value,'bundle_digest')
    with pytest.raises(ProductionError,match='CONTROL_REQUIRED_UNMEASURABLE'):import_bundle(value,resources_root=source,output_root=out)
