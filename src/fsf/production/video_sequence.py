"""Materialize global control windows and durably resume generated frame sequences.

Model providers are explicit callbacks. Checkpoints prove bytes and mapping,
not semantic quality, human approval or model qualification.
"""
from fractions import Fraction
from pathlib import Path
import shutil,tempfile,time
from PIL import Image
from fsf.core.asset_library import _root,_no_links
from fsf.core.edit_store import edit_lock,read_json
from fsf.conformance.media import ensure_media_tools_on_path
from fsf.production.contracts import ProductionError,seal,publish,integer
from fsf.production.control import verify_control,file_identity,_run,inspect_control
from fsf.production.segments import validate_segments


def _hashes(folder,count):
    return [file_identity(folder/f'{i:06d}.png')['sha256'] for i in range(count)]


def run_sequence(bundle,plan,*,output_root,generator,seconds=300,max_new_segments=None,allow_probe=False):
    """Generator(frames, initialized_frames, segment, deadline) returns PIL frames.

    Initialized prefix frames are actual previous generated pixels, never the
    last control frame. The callback must honor its finite deadline. Its model
    identity belongs to the capability lock; no model selection occurs here.
    """
    integer(seconds,1,5400)
    if max_new_segments is not None:integer(max_new_segments,0,172800)
    plan=validate_segments(plan);source=verify_control(bundle)
    if plan['qualification_scope']=='probe_only' and allow_probe is not True:
        raise ProductionError('PRODUCTION_PROBE_ONLY')
    if any(plan[k]!=source[v] for k,v in [('source_digest','source_digest'),('source_sha256','sha256'),('clock','clock'),('frame_count','frame_count')]):
        raise ProductionError('PRODUCTION_SOURCE_CHANGED')
    if not callable(generator):raise ProductionError('PRODUCTION_BACKEND_REQUIRED')
    ffmpeg,ffprobe=ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:raise ProductionError('PRODUCTION_TOOLS_UNAVAILABLE')
    root=_root(output_root);rate=Fraction(source['clock']['num'],source['clock']['den'])
    # The finite model window, rather than full input length, bounds image RAM.
    if max(s['model_frames'] for s in plan['segments'])*source['width']*source['height']*3>256*1024**2:
        raise ProductionError('PRODUCTION_WINDOW_MEMORY_LIMIT')
    end=time.monotonic()+seconds;new=0;accepted=[]
    folder=root/plan['plan_digest'];folder.mkdir(exist_ok=True);_no_links(folder)
    with edit_lock(folder/'.sequence'):
        for segment in plan['segments']:
            sid=segment['segment_id'];target=folder/sid;receipt=target/'segment.json'
            left,right=segment['context'];start,stop=segment['core'];prefix=start-left
            initialization=[];initialized=[]
            if segment['depends_on']:
                if segment['depends_on'] != [accepted[-1]['segment']['segment_id']]:
                    raise ProductionError('PRODUCTION_DEPENDENCY_INVALID')
                prior=accepted[-1];prior_left=prior['segment']['context'][0]
                for frame in range(left,start):
                    p=folder/prior['segment']['segment_id']/'frames'/f'{frame-prior_left:06d}.png'
                    initialization.append({'global_frame':frame,'sha256':file_identity(p)['sha256']})
                    with Image.open(p) as image:initialized.append(image.convert('RGB'))
            binding={'plan_digest':plan['plan_digest'],'segment':segment,'initialization':initialization}
            if receipt.exists():
                value=read_json(receipt)
                if (set(value)!={'schema_version',*binding.keys(),'control_frame_hashes','frame_hashes','core_identity','motion_review','checkpoint_digest'}
                        or value['schema_version']!='fsf_video_segment_checkpoint_v1'
                        or seal(value,'checkpoint_digest')!=value or any(value[k]!=v for k,v in binding.items())
                        or _hashes(target/'frames',segment['model_frames'])!=value['frame_hashes']
                        or file_identity(target/'core.mp4')!=value['core_identity']):
                    raise ProductionError('PRODUCTION_CHECKPOINT_CHANGED')
                accepted.append(value);continue
            if (max_new_segments is not None and new>=max_new_segments) or time.monotonic()>=end:
                return {'status':'paused','completed_segments':len(accepted),'new_segments':new,'bundle':plan['plan_digest']}
            scratch=Path(tempfile.mkdtemp(prefix='.segment-',dir=folder))
            try:
                controls=scratch/'control';controls.mkdir()
                # Exact rational global seek with original PTS selection.
                seek=(Fraction(left,1)/rate*1000000).__floor__()
                probe=__import__('json').loads(_run([ffprobe,'-v','error','-show_streams','-of','json',str(Path(bundle)/'control.mp4')],min(30,max(1,end-time.monotonic()))))
                tb=Fraction(probe['streams'][0]['time_base']);tick=Fraction(left,1)/rate/tb
                if tick.denominator!=1:raise ProductionError('PRODUCTION_FRAME_MAPPING')
                args=[ffmpeg,'-nostdin','-v','error','-xerror','-copyts']
                if left:args+=['-ss',f'{seek//1000000}.{seek%1000000:06d}']
                args+=['-i',str(Path(bundle)/'control.mp4'),'-vf',f'select=gte(pts\\,{tick.numerator})',
                       '-fps_mode','passthrough','-frames:v',str(right-left),'-threads','1','-start_number','0',str(controls/'%06d.png')]
                _run(args,max(1,end-time.monotonic()))
                frames=[]
                control_hashes=_hashes(controls,right-left)
                for i in range(right-left):
                    with Image.open(controls/f'{i:06d}.png') as image:frames.append(image.convert('RGB'))
                frames += [frames[-1].copy() for _ in range(segment['padding_frames'])]
                generated=list(generator(frames,initialized,segment,end))
                if len(generated)!=segment['model_frames']:raise ProductionError('PRODUCTION_MODEL_FRAMES')
                rendered=scratch/'frames';rendered.mkdir()
                for i,image in enumerate(generated):
                    if not isinstance(image,Image.Image) or image.size!=(source['width'],source['height']):
                        raise ProductionError('PRODUCTION_MODEL_FRAMES')
                    image.convert('RGB').save(rendered/f'{i:06d}.png')
                _run([ffmpeg,'-nostdin','-v','error','-framerate',str(rate),'-start_number',str(prefix),
                      '-i',str(rendered/'%06d.png'),'-frames:v',str(stop-start),'-an','-c:v','libx264',
                      '-preset','fast','-crf','18','-pix_fmt','yuv420p','-bf','0',
                      '-video_track_timescale',str(rate.numerator),'-threads','1',str(scratch/'core.mp4')],max(1,end-time.monotonic()))
                value=seal({'schema_version':'fsf_video_segment_checkpoint_v1',**binding,
                            'control_frame_hashes':control_hashes,'frame_hashes':_hashes(rendered,segment['model_frames']),
                            'core_identity':file_identity(scratch/'core.mp4'),'motion_review':'not_reviewed'},'checkpoint_digest')
                shutil.rmtree(controls)
                publish(scratch/'segment.json',value)
                if target.exists():raise ProductionError('PRODUCTION_TARGET_EXISTS')
                scratch.rename(target);accepted.append(value);new+=1
            finally:
                if scratch.exists():shutil.rmtree(scratch)
        if verify_control(bundle)!=source:raise ProductionError('PRODUCTION_SOURCE_CHANGED')
        complete=folder/'sequence.json'
        if complete.exists():
            value=read_json(complete)
            if (seal(value,'receipt_digest')!=value or value['checkpoints']!=[v['checkpoint_digest'] for v in accepted]
                    or file_identity(folder/'movie.mp4')!=value['output']):
                raise ProductionError('PRODUCTION_CHECKPOINT_CHANGED')
            return {**value,'new_segments':new,'status':'completed','bundle':plan['plan_digest']}
        listing=folder/'concat.txt'
        listing.write_text(''.join(f"file '{s['segment_id']}/core.mp4'\n" for s in plan['segments']),encoding='utf-8')
        partial=folder/'movie.partial.mp4'
        _run([ffmpeg,'-nostdin','-v','error','-f','concat','-safe','1','-i',str(listing),
              '-vf',f'setpts=N*{rate.denominator}/({rate.numerator}*TB)',
              '-fps_mode','cfr','-enc_time_base',f'{rate.denominator}/{rate.numerator}',
              '-r',str(rate),
              '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-bf','0',
              '-video_track_timescale',str(rate.numerator),'-threads','1','-y',str(partial)],max(1,end-time.monotonic()))
        check=folder/'final-decode';check.mkdir(exist_ok=True)
        measured=inspect_control(partial,check,timeout=max(1,int(end-time.monotonic())))
        if measured['frame_count']!=source['frame_count'] or measured['clock']!=source['clock']:
            raise ProductionError('PRODUCTION_FRAME_MAPPING')
        partial.replace(folder/'movie.mp4')
        value=seal({'plan_digest':plan['plan_digest'],'checkpoints':[v['checkpoint_digest'] for v in accepted],
                    'output':file_identity(folder/'movie.mp4'),'measured':measured,
                    'qualification_scope':plan['qualification_scope'],'motion_review':'not_reviewed',
                    'semantic_quality':'not_evaluated'},'receipt_digest')
        publish(complete,value)
        return {**value,'status':'completed','new_segments':new,'bundle':plan['plan_digest']}
