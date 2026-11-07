"""Portable multichannel control bytes; conformance is not model obedience."""
from fractions import Fraction
from functools import lru_cache
from importlib import resources
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sqlite3
import struct
import tempfile
import time

from jsonschema import Draft202012Validator
from PIL import Image

from fsf.core.asset_library import _root, _no_links, _open_reader, _path_parts
from fsf.core.edit_store import read_json, decode_json, edit_lock
from fsf.production.contracts import ProductionError, digest, seal, publish

MAX_BYTES = 64 * 1024**3
MAX_LINE = 65536


def _sha(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def fail(code):
    raise ProductionError('CONTROL_' + code)


@lru_cache(maxsize=4)
def validator(name):
    return Draft202012Validator(json.loads(resources.files('fsf.schemas').joinpath(name+'.schema.json').read_text('utf-8')))


class Budget:
    def __init__(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 5400:fail('LIMIT')
        self.deadline = time.monotonic()+seconds
        self.bytes = 0

    def check(self):
        if time.monotonic() >= self.deadline:fail('DEADLINE')


def path_at(root, name):
    _path_parts(name)
    path = root.joinpath(*name.split('/'))
    _no_links(path)
    if not path.is_file():fail('FILE_MISSING')
    return path


def identity(root, ref, budget, copy_to=None):
    path = path_at(root, ref['path'])
    path_before=path.stat()
    h = hashlib.sha256();count = 0
    target = None
    if copy_to is not None:
        target = copy_to.joinpath(*ref['path'].split('/'))
        target.parent.mkdir(parents=True,exist_ok=True);_no_links(target.parent)
        if target.exists():
            identity(copy_to,ref,budget)
            return path
        budget.bytes+=ref['size_bytes']
        if budget.bytes>MAX_BYTES:fail('LIMIT')
    try:
        with _open_reader(path) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):fail('SOURCE_INVALID')
            if target is not None:out = target.open('xb')
            try:
                while block := source.read(1024*1024):
                    budget.check();count += len(block)
                    if count > ref['size_bytes']:fail('SIZE')
                    h.update(block)
                    if target is not None:out.write(block)
                after = os.fstat(source.fileno())
                if target is not None:out.flush();os.fsync(out.fileno())
            finally:
                if target is not None:out.close()
        _no_links(path)
        current = path.stat()
        signature = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        # Windows path and handle stat APIs can expose different inode fields.
        # Compare each observer to itself, and still bind every byte by SHA.
        if signature(before)!=signature(after) or signature(path_before)!=signature(current):fail('SOURCE_CHANGED')
        if count != ref['size_bytes'] or h.hexdigest()!=ref['sha256']:fail('INTEGRITY')
        return path
    except OSError:
        fail('SOURCE_INVALID')


def _semantics(channel):
    s=channel['semantics'];kind=channel['kind'];encoding=channel['encoding']
    if digest(s)!=channel['semantics_digest']:fail('SEMANTICS_DIGEST')
    if kind in {'rgb','edge'}:
        if kind=='rgb':
            if set(s)!={'color_space','render'} or s['color_space'] not in {'srgb','linear'} or encoding!='png_rgb8':fail('SEMANTICS')
            render=s['render']
            if render is not None:
                if (not isinstance(render,dict) or set(render)!={'engine','samples','seed','denoiser','material_profile','world_profile'}
                        or type(render['samples']) is not int or not 1<=render['samples']<=1048576
                        or type(render['seed']) is not int):fail('SEMANTICS')
                if any(not isinstance(render[k],str) or not render[k] for k in ('engine','material_profile','world_profile')):fail('SEMANTICS')
                if render['denoiser'] is not None:
                    d=render['denoiser']
                    if not isinstance(d,dict) or set(d)!={'name','version','sha256','settings_digest'}:fail('SEMANTICS')
                    if any(not isinstance(d[k],str) or not d[k] for k in d):fail('SEMANTICS')
                    if not _sha(d['sha256']) or not _sha(d['settings_digest']):fail('SEMANTICS')
        elif set(s)!={'generator','parameters_digest'} or encoding!='png_gray8':fail('SEMANTICS')
        if kind=='edge' and (not isinstance(s['generator'],str) or not s['generator'] or not _sha(s['parameters_digest'])):fail('SEMANTICS')
    elif kind=='depth':
        if (set(s)!={'convention','units','near','far','representation','reverse','invalid_policy'}
                or encoding!='float32_le' or s['convention'] not in {'camera_z','ray_distance'} or s['units']!='m'
                or type(s['reverse']) is not bool or s['invalid_policy']!='mask_zero_invalid'
                or type(s['near']) not in (int,float) or type(s['far']) not in (int,float)
                or not 0<s['near']<s['far'] or s['representation'] not in {'metric','linear_normalized'}
                or s['representation']=='metric' and s['reverse']):fail('SEMANTICS')
    elif kind=='normal':
        if (set(s)!={'space','axes','handedness','orientation','face_forward','invalid_policy'} or encoding!='float32_le'
                or not isinstance(s['space'],str) or not s['space'] or type(s['face_forward']) is not bool
                or s['orientation'] not in {'outward','inward','face_forward'}
                or (s['orientation']=='face_forward')!=s['face_forward']
                or s['handedness'] not in {'left','right'} or s['invalid_policy']!='mask_zero_invalid'):fail('SEMANTICS')
        a=s['axes']
        if not isinstance(a,list) or len(a)!=3 or any(not isinstance(r,list) or len(r)!=3 for r in a):fail('SEMANTICS')
        if any(type(v) not in (float,int) or not math.isfinite(v) for r in a for v in r):fail('SEMANTICS')
        if any(abs(sum(a[i][k]*a[j][k] for k in range(3))-(i==j))>1e-6 for i in range(3) for j in range(3)):fail('SEMANTICS')
        det=sum(a[0][i]*(a[1][(i+1)%3]*a[2][(i+2)%3]-a[1][(i+2)%3]*a[2][(i+1)%3]) for i in range(3))
        if abs(det-(1 if s['handedness']=='right' else -1))>1e-6:fail('SEMANTICS')
    elif kind=='pose':
        if set(s)!={'space','generator','parameters_digest'} or s['space'] not in {'image_normalized','world_m'} or encoding!='pose_json_v1':fail('SEMANTICS')
        if not isinstance(s['generator'],str) or not s['generator'] or not _sha(s['parameters_digest']):fail('SEMANTICS')
    elif encoding!='opaque' or set(s)!={'namespace','format_version'}:fail('SEMANTICS')
    if kind=='custom' and any(not isinstance(s[k],str) or not s[k] for k in s):fail('SEMANTICS')
    if channel['provenance']['qualification']=='observed' and channel['provenance']['binary_sha256'] is None:fail('PROVENANCE')


def definition(value):
    decode_json(json.dumps(value,allow_nan=False).encode('utf-8'))
    if not validator('rc11_control_bundle_v1').is_valid(value) or seal(value,'bundle_digest')!=value:fail('SCHEMA')
    _path_parts(value['control_asset_id'])
    rate=Fraction(value['clock']['num'],value['clock']['den'])
    if (rate not in {Fraction(24),Fraction(24000,1001)}
            or [rate.numerator,rate.denominator]!=[value['clock']['num'],value['clock']['den']]
            or Fraction(value['frame_count'],1)/rate>7200):fail('CLOCK')
    expected=digest({'clock':value['clock'],'frame_count':value['frame_count'],'range':[0,value['frame_count']]})
    if value['mapping_digest']!=expected:fail('MAPPING')
    ids=[c['id'] for c in value['channels']]
    manifests=[c['manifest']['path'] for c in value['channels']]
    if len(ids)!=len(set(ids)) or len(manifests)!=len(set(manifests)):fail('DUPLICATE_ID')
    for c in value['channels']:
        _semantics(c)
        if any(c[k]!=value[k] for k in ('clock','width','height','frame_count','mapping_digest')):fail('ALIGNMENT')
        if c['manifest']['size_bytes']>512*1024*1024:fail('LIMIT')
        if c['manifest']['path'] in {'bundle.json','verification.json'}:fail('PATH')
    return value


def frames(root, channel, count, budget):
    path=identity(root,channel['manifest'],budget)
    seen=0
    with _open_reader(path) as stream:
        while line:=stream.readline(MAX_LINE+1):
            budget.check()
            if len(line)>MAX_LINE:fail('MANIFEST_LIMIT')
            try:frame=decode_json(line)
            except Exception:fail('MANIFEST')
            if (not validator('rc11_control_frame_v1').is_valid(frame)
                    or frame['global_frame']!=seen or seen>=count):fail('MAPPING')
            yield frame
            seen+=1
    if seen!=count:fail('MAPPING')
    identity(root,channel['manifest'],budget)


def _image(path, mode, width, height):
    image=Image.open(path)
    if image.format!='PNG' or image.mode!=mode or image.size!=(width,height) or getattr(image,'n_frames',1)!=1:
        image.close();fail('PIXEL_FORMAT')
    image.load()
    return image


def compare_rgb(source, target, width, height, profile, budget):
    # Population SSIM in fixed 8x8 RGB windows; data_range is 255.
    if profile not in {'lossless_samples_v1','lossy_rgb_reference_v1_1'}:fail('CONVERSION_PROFILE')
    with _image(source,'RGB',width,height) as a,_image(target,'RGB',width,height) as b:
        if any(a.info.get(k)!=b.info.get(k) for k in ('icc_profile','gamma','srgb')):fail('COLORSPACE')
        sums=[0,0,0];signed=[0,0,0];maximum=0;ssim_sum=0;windows=0
        for y in range(0,height,8):
            budget.check()
            for x in range(0,width,8):
                box=(x,y,min(x+8,width),min(y+8,height))
                av=a.crop(box).tobytes();bv=b.crop(box).tobytes()
                if profile=='lossless_samples_v1' and av!=bv:fail('CONVERSION')
                for k in range(3):
                    aa=av[k::3];bb=bv[k::3];n=len(aa)
                    for u,v in zip(aa,bb):
                        d=v-u;sums[k]+=abs(d);signed[k]+=d;maximum=max(maximum,abs(d))
                    ma=sum(aa)/n;mb=sum(bb)/n
                    va=sum((v-ma)**2 for v in aa)/n;vb=sum((v-mb)**2 for v in bb)/n
                    cov=sum((u-ma)*(v-mb) for u,v in zip(aa,bb))/n
                    ssim_sum+=((2*ma*mb+6.5025)*(2*cov+58.5225))/((ma*ma+mb*mb+6.5025)*(va+vb+58.5225));windows+=1
        metrics={'max_abs_error_8bit':maximum,'channel_mae':[v/(width*height) for v in sums],
                 'channel_signed_bias':[v/(width*height) for v in signed],'ssim':ssim_sum/windows}
        if profile=='lossy_rgb_reference_v1_1' and (maximum>32 or max(metrics['channel_mae'])>3
                or max(map(abs,metrics['channel_signed_bias']))>1 or metrics['ssim']<0.98):fail('CONVERSION')
        return metrics


def content(root, channel, frame, width, height, budget):
    artifact=frame['artifact'];kind=channel['kind'];pixels=width*height
    if artifact['size_bytes']>256*1024*1024:fail('FRAME_LIMIT')
    path=identity(root,artifact,budget)
    valid_pixels=pixels;total_samples=pixels;sample_unit='pixels'
    if kind not in {'depth','normal'} and 'invalid_mask' in frame:fail('MASK')
    if kind in {'rgb','edge'}:
        if 'invalid_mask' in frame:fail('MASK')
        with _image(path,'RGB' if kind=='rgb' else 'L',width,height):pass
    elif kind in {'depth','normal'}:
        components=1 if kind=='depth' else 3
        if artifact['size_bytes']!=pixels*components*4 or 'invalid_mask' not in frame:fail('PIXEL_FORMAT')
        mask=frame['invalid_mask']
        if mask['size_bytes']!=pixels:fail('MASK')
        mask_path=identity(root,mask,budget)
        valid_pixels=0
        with _open_reader(path) as data,_open_reader(mask_path) as masks:
            while flags:=masks.read(16384):
                budget.check();raw=data.read(len(flags)*components*4)
                if len(raw)!=len(flags)*components*4:fail('PIXEL_FORMAT')
                for flag,values in zip(flags,struct.iter_unpack('<'+'f'*components,raw)):
                    if flag not in (0,1):fail('MASK')
                    if not flag:continue
                    valid_pixels+=1
                    if any(not math.isfinite(v) for v in values):fail('NONFINITE')
                    if kind=='normal' and abs(sum(v*v for v in values)-1)>0.001:fail('NORMAL')
                    if kind=='depth':
                        s=channel['semantics'];lo,hi=(s['near'],s['far']) if s['representation']=='metric' else (0,1)
                        if not lo<=values[0]<=hi:fail('DEPTH')
            if data.read(1):fail('PIXEL_FORMAT')
        identity(root,mask,budget)
    elif kind=='pose':
        if artifact['size_bytes']>MAX_LINE:fail('FRAME_LIMIT')
        pose=read_json(path)
        if set(pose)!={'space','points'} or pose['space']!=channel['semantics']['space'] or not isinstance(pose['points'],list) or len(pose['points'])>4096:fail('POSE')
        ids=set()
        for point in pose['points']:
            if not isinstance(point,dict) or set(point)!={'id','x','y','z','confidence'} or not isinstance(point['id'],str) or point['id'] in ids:fail('POSE')
            ids.add(point['id'])
            if any(type(point[k]) not in (float,int) or not math.isfinite(point[k]) for k in ('x','y','z','confidence')):fail('POSE')
            if not 0<=point['confidence']<=1:fail('POSE')
            if pose['space']=='image_normalized' and not (0<=point['x']<=1 and 0<=point['y']<=1):fail('POSE')
        total_samples=len(pose['points']);valid_pixels=sum(p['confidence']>0 for p in pose['points']);sample_unit='points'
    elif kind=='custom':
        valid_pixels=total_samples=artifact['size_bytes'];sample_unit='bytes'
    if 'conversion' in frame:
        conversion=frame['conversion'];source=identity(root,conversion['source'],budget)
        if kind=='rgb':compare_rgb(source,path,width,height,conversion['profile_id'],budget)
        elif conversion['profile_id']!='lossless_samples_v1' or conversion['source']['sha256']!=artifact['sha256']:fail('CONVERSION')
        identity(root,conversion['source'],budget)
    identity(root,artifact,budget)
    return {'valid_samples':valid_pixels,'invalid_samples':total_samples-valid_pixels,'sample_unit':sample_unit}


class ReferenceIndex:
    """Ephemeral validation spill, never a production ledger or authority."""
    def __init__(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fsf-control-index-')
        self.db=sqlite3.connect(str(Path(self.temp.name)/'references.sqlite'))
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('CREATE TABLE refs(path TEXT PRIMARY KEY, sha TEXT NOT NULL, size INTEGER NOT NULL)')
        self.count=0

    def add(self,ref,budget):
        row=self.db.execute('SELECT sha,size FROM refs WHERE path=?',(ref['path'],)).fetchone()
        if row:
            if row!=(ref['sha256'],ref['size_bytes']):fail('REFERENCE_CONFLICT')
            return
        self.count+=1;budget.bytes+=ref['size_bytes']
        if self.count>1000000 or budget.bytes>MAX_BYTES:fail('LIMIT')
        self.db.execute('INSERT INTO refs VALUES(?,?,?)',(ref['path'],ref['sha256'],ref['size_bytes']))
        if self.count%1000==0:
            pages=self.db.execute('PRAGMA page_count').fetchone()[0]
            size=self.db.execute('PRAGMA page_size').fetchone()[0]
            if pages*size>1024**3:fail('INDEX_LIMIT')

    def close(self):
        self.db.close();self.temp.cleanup()


def verify(folder, *, timeout=300, interval=None, required=(), capability=None):
    root=_root(folder);value=definition(read_json(root/'bundle.json'));budget=Budget(timeout)
    start,end=interval if interval is not None else (0,value['frame_count'])
    if type(start) is not int or type(end) is not int or not 0<=start<end<=value['frame_count']:fail('RANGE')
    ids={c['id'] for c in value['channels']}
    if not set(required)<=ids:fail('REQUIRED_CHANNEL')
    if capability is not None:
        if set(capability)!={'schema_version','channels'} or capability['schema_version']!='rc11_control_capability_v1' or not isinstance(capability['channels'],list):fail('CAPABILITY')
        if len(capability['channels'])>256 or any(not isinstance(c,dict) or set(c)!={'kind','encoding','semantics_digest'}
                or not isinstance(c['kind'],str) or not isinstance(c['encoding'],str) or not _sha(c['semantics_digest'])
                for c in capability['channels']):fail('CAPABILITY')
        for c in value['channels']:
            if c['required'] or c['id'] in required:
                if {'kind':c['kind'],'encoding':c['encoding'],'semantics_digest':c['semantics_digest']} not in capability['channels']:fail('REQUIRED_UNSUPPORTED')
    reserved={'bundle.json','verification.json',*(c['manifest']['path'] for c in value['channels'])}
    windows=[];validity=[];window_bytes=0;references=ReferenceIndex()
    try:
        for c in value['channels']:
            references.add(c['manifest'],budget)
            selected=[]
            valid=invalid=0
            for f in frames(root,c,value['frame_count'],budget):
                refs=[f['artifact'],*([f['invalid_mask']] if 'invalid_mask' in f else []),*([f['conversion']['source']] if 'conversion' in f else [])]
                for ref in refs:
                    if ref['path'] in reserved:fail('PATH')
                    _path_parts(ref['path']);references.add(ref,budget)
                if start<=f['global_frame']<end:
                    measured=content(root,c,f,value['width'],value['height'],budget)
                    valid+=measured['valid_samples'];invalid+=measured['invalid_samples']
                    if interval is not None:
                        window_bytes+=len(json.dumps(f).encode('utf-8'))
                        if window_bytes>8*1024*1024:fail('WINDOW_LIMIT')
                        selected.append(f)
            if interval is not None:windows.append({'channel_id':c['id'],'semantics_digest':c['semantics_digest'],'frames':selected})
            if not valid and (c['required'] or c['id'] in required):fail('REQUIRED_UNMEASURABLE')
            validity.append({'channel_id':c['id'],'valid_samples':valid,'invalid_samples':invalid,
                             'sample_unit':'points' if c['kind']=='pose' else 'bytes' if c['kind']=='custom' else 'pixels',
                             'status':('bytes_verified' if c['kind']=='custom' else 'structurally_observed') if valid else 'unmeasurable'})
    finally:references.close()
    result={'schema_version':'rc11_control_verification_v1','bundle_digest':value['bundle_digest'],
            'range':[start,end],'scope':'full' if interval is None else 'window_only',
            'content_conformance':'passed','provenance_attested':False,'rgb_quality':'not_evaluated',
            'geometry_accuracy':'not_evaluated','full_bundle_content_verified':interval is None,
            'motion_review':'not_reviewed','channel_validity':validity,'channels':windows}
    if len(json.dumps(result).encode('utf-8'))>8*1024*1024:fail('WINDOW_LIMIT')
    return seal(result,'verification_digest')


def import_bundle(value, *, resources_root, output_root, timeout=300):
    value=definition(value);source=_root(resources_root);root=_root(output_root);budget=Budget(timeout)
    destination=root/value['control_asset_id']
    with edit_lock(root/'.control-bundle'):
        if destination.exists():
            _no_links(destination)
            old=definition(read_json(destination/'bundle.json'))
            if old!=value:fail('CONFLICT')
            verify(destination,timeout=timeout)
            return value
        scratch=Path(tempfile.mkdtemp(prefix='.control-bundle-',dir=root))
        try:
            for c in value['channels']:
                identity(source,c['manifest'],budget,copy_to=scratch)
                for f in frames(source,c,value['frame_count'],budget):
                    refs=[f['artifact'],*([f['invalid_mask']] if 'invalid_mask' in f else []),*([f['conversion']['source']] if 'conversion' in f else [])]
                    for ref in refs:
                        if ref['path'] in {'bundle.json','verification.json',*(x['manifest']['path'] for x in value['channels'])}:fail('PATH')
                        if ref['size_bytes']>256*1024*1024:fail('FRAME_LIMIT')
                        identity(source,ref,budget,copy_to=scratch)
            publish(scratch/'bundle.json',value)
            remaining=int(budget.deadline-time.monotonic())
            if remaining<1:fail('DEADLINE')
            report=verify(scratch,timeout=remaining)
            publish(scratch/'verification.json',report)
            os.rename(scratch,destination)
            return value
        finally:
            if scratch.exists():
                if scratch.resolve().parent!=root.resolve() or not scratch.name.startswith('.control-bundle-'):fail('PATH')
                shutil.rmtree(scratch)
