"""Encode verified global PNG sequence once, with a fixed CPU x264 profile."""
from pathlib import Path
import os
import re
import shutil
import tempfile
import time

from fsf.core.asset_library import _root
from fsf.core.edit_store import read_json,edit_lock
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import RemoteError,digest,sha,seal,write_json
from fsf.remote.segments import validate_plan,validate_complete,consumed_budget
from fsf.remote.artifacts import shape
from fsf.runtime.blender import encoder_version_matches,encoder_build_marker


def encoder_pin(ffmpeg):
    """Observe this exact binary/build with a one-frame CPU encoding smoke."""
    version=run_safe_subprocess([str(ffmpeg),'-version'],timeout_seconds=10,output_limit_bytes=65536)
    if version.timed_out or version.returncode or not encoder_version_matches(version.stdout,'8.0.1'):
        raise RemoteError('REMOTE_ENCODER_VERSION')
    with tempfile.TemporaryDirectory(prefix='fsf-encoder-pin-') as tmp:
        encoded=run_safe_subprocess([str(ffmpeg),'-nostdin','-y','-f','lavfi','-i','color=c=gray:s=32x32:r=24',
            '-frames:v','1','-an','-c:v','libx264',str(Path(tmp)/'smoke.mp4')],timeout_seconds=10,output_limit_bytes=65536)
    found=encoder_build_marker(encoded.stderr)
    if encoded.timed_out or encoded.returncode or found is None:raise RemoteError('REMOTE_ENCODER_VERSION')
    return {'profile':'rc8_control_x264_v1','device':'cpu','encoder':'libx264','ffmpeg_version':'8.0.1',
            'ffmpeg_sha256':sha(ffmpeg),'ffmpeg_build_digest':digest(version.stdout),'libx264_build':found}


def assemble(plan,*,driver,state_root,output_root,encoder,ffmpeg):
    plan=validate_plan(plan);root=_root(state_root);directory=root/plan['plan_digest']
    with edit_lock(directory/'.run-target'):
        started=time.monotonic()
        try:return _assemble(plan,driver=driver,state_root=state_root,output_root=output_root,encoder=encoder,ffmpeg=ffmpeg)
        finally:
            path=directory/'state.json'
            state=read_json(path)
            if seal(state,'state_digest')['state_digest']!=state.get('state_digest'):raise RemoteError('REMOTE_INTEGRITY')
            state['delivery_seconds']=state.get('delivery_seconds',0)+(time.monotonic()-started)
            write_json(path,seal(state,'state_digest'))


def _assemble(plan,*,driver,state_root,output_root,encoder,ffmpeg):
    plan=validate_plan(plan);root=_root(state_root);output=_root(output_root)
    if digest(encoder)!=plan['encoder_digest']:raise RemoteError('REMOTE_INTEGRITY')
    directory=root/plan['plan_digest'];state=read_json(directory/'state.json')
    if seal(state,'state_digest')['state_digest']!=state.get('state_digest'):raise RemoteError('REMOTE_INTEGRITY')
    remaining=plan['budget_seconds']-consumed_budget(state)
    if remaining<=0:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
    deadline=time.monotonic()+remaining
    def run(args):
        left=deadline-time.monotonic()
        if left<=0:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
        result=run_safe_subprocess(args,timeout_seconds=left,output_limit_bytes=1024*1024)
        if result.timed_out:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
        if result.returncode:raise RemoteError('REMOTE_ENCODING_FAILED')
        return result
    version=run([str(ffmpeg),'-version']).stdout
    if sha(ffmpeg)!=encoder['ffmpeg_sha256'] or digest(version)!=encoder['ffmpeg_build_digest'] or not encoder_version_matches(version,encoder['ffmpeg_version']):
        raise RemoteError('REMOTE_ENCODER_VERSION')
    with edit_lock(output/'.segment-delivery'):
        scratch=Path(tempfile.mkdtemp(prefix='.delivery-',dir=output))
        try:
            frames=scratch/'frames';frames.mkdir();mapping=[];completes=[]
            for segment in plan['segments']:
                entry=state['segments'][segment['segment_id']]
                if not entry['complete']:raise RemoteError('REMOTE_SEGMENT_INCOMPLETE')
                attempt=entry['attempts'][-1]['attempt_id']
                request=read_json(directory/(segment['segment_id']+'-'+str(attempt)+'.json'))
                actual=driver.reuse(entry['complete'],request,plan,segment) if entry['attempts'][-1]['status']=='reused_render' else driver.accept(request,plan,segment,attempt)
                validate_complete(actual,plan,segment,request,attempt)
                if actual!=entry['complete']:raise RemoteError('REMOTE_SEGMENT_MAPPING')
                folder,manifest=driver.frame_bundle(actual);completes.append(actual['complete_digest'])
                for frame in manifest['frame_mapping']:
                    expected=plan['start_frame']+len(mapping)
                    if frame['scene_frame']!=expected:raise RemoteError('REMOTE_FRAME_SEQUENCE')
                    source=folder/frame['path']
                    target=frames/(f'{len(mapping):06d}.png');shutil.copyfile(source,target)
                    mapping.append({'scene_frame':expected,'sha256':sha(target)})
            count=plan['end_frame']-plan['start_frame']
            if len(mapping)!=count:raise RemoteError('REMOTE_FRAME_SEQUENCE')
            rate=f"{plan['clock']['num']}/{plan['clock']['den']}"
            video=scratch/'control.mp4'
            result=run([str(ffmpeg),'-nostdin','-y','-framerate',rate,'-i',str(frames/'%06d.png'),
                '-frames:v',str(count),'-an','-c:v','libx264','-preset','medium','-crf','16','-threads','2',
                '-bf','0','-g','24','-pix_fmt','yuv420p','-fps_mode','cfr','-video_track_timescale','24000',str(video)])
            if encoder_build_marker(result.stderr)!=encoder['libx264_build']:raise RemoteError('REMOTE_ENCODER_VERSION')
            score=run([str(ffmpeg),'-nostdin','-i',str(video),'-framerate',rate,'-i',str(frames/'%06d.png'),
                '-filter_complex','[0:v]format=yuv420p,setpts=PTS-STARTPTS[a];[1:v]format=yuv420p,setpts=PTS-STARTPTS[b];[a][b]ssim',
                '-an','-f','null','-'])
            values=re.findall(r'All:([0-9.]+)',score.stderr)
            if not values or float(values[-1])<.98:raise RemoteError('REMOTE_CONTROL_SSIM')
            from fsf.runtime.control_import import _inspect
            quality=_inspect(video,scratch,deadline=deadline)
            if quality['frame_count']!=count or quality['clock']!=plan['clock']:raise RemoteError('REMOTE_FRAME_SEQUENCE')
            manifest=seal({'schema_version':'fsf_segment_delivery_v1','plan_digest':plan['plan_digest'],
                'generation_digest':plan['generation_digest'],'encoder_digest':plan['encoder_digest'],
                'encoder_profile':encoder,'control_id':plan['group_id'][:32]+'_'+plan['plan_digest'][:24],'revision':1,
                'artifact':{'path':'control.mp4','role':'control_video','media_type':'video/mp4','sha256':sha(video),'size_bytes':video.stat().st_size},
                'start_frame':plan['start_frame'],'end_frame':plan['end_frame'],'width':quality['width'],'height':quality['height'],
                'complete_digests':completes,'frame_sequence_digest':digest(mapping),'frame_mapping_digest':digest(mapping),'frame_sequence':mapping,'frame_count':count,'clock':plan['clock'],
                'artifact_sha256':sha(video),'size_bytes':video.stat().st_size,'quality':quality,'ssim':float(values[-1]),
                'motion_review':'not_reviewed','listening_review':'not_reviewed'},'delivery_digest')
            shape(manifest,'fsf_segment_delivery_v1')
            write_json(scratch/'manifest.json',manifest)
            receipt=seal({'schema_version':'fsf_segment_delivery_receipt_v1','delivery_digest':manifest['delivery_digest'],
                'plan_digest':plan['plan_digest'],'artifact_sha256':manifest['artifact_sha256'],'execution':'completed','motion_review':'not_reviewed'},'receipt_digest')
            write_json(scratch/'receipt.json',receipt)
            shutil.rmtree(frames)
            destination=output/manifest['delivery_digest']
            if destination.exists():
                if read_json(destination/'manifest.json')!=manifest or read_json(destination/'receipt.json')!=receipt or sha(destination/'control.mp4')!=manifest['artifact_sha256']:raise RemoteError('REMOTE_ARTIFACT_CONFLICT')
            else:os.replace(scratch,destination)
            return {'status':'verified_control_delivery','delivery_digest':manifest['delivery_digest'],'motion_review':'not_reviewed'}
        finally:
            if scratch.exists():shutil.rmtree(scratch)


def verify_delivery(folder):
    folder=_root(folder);value=read_json(folder/'manifest.json');receipt=read_json(folder/'receipt.json')
    shape(value,'fsf_segment_delivery_v1');shape(receipt,'fsf_segment_delivery_receipt_v1')
    if (seal(value,'delivery_digest')['delivery_digest']!=value['delivery_digest']
            or seal(receipt,'receipt_digest')['receipt_digest']!=receipt['receipt_digest']
            or receipt['delivery_digest']!=value['delivery_digest'] or receipt['plan_digest']!=value['plan_digest']
            or receipt['artifact_sha256']!=value['artifact_sha256']
            or digest(value['encoder_profile'])!=value['encoder_digest']
            or digest(value['frame_sequence'])!=value['frame_sequence_digest'] or value['frame_mapping_digest']!=value['frame_sequence_digest']
            or [f['scene_frame'] for f in value['frame_sequence']]!=list(range(value['start_frame'],value['end_frame']))):raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    artifact=value['artifact']
    if artifact['path']!='control.mp4' or artifact['sha256']!=value['artifact_sha256'] or artifact['size_bytes']!=value['size_bytes'] or sha(folder/'control.mp4')!=artifact['sha256']:
        raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    with tempfile.TemporaryDirectory(prefix='fsf-delivery-check-') as tmp:
        from fsf.runtime.control_import import _inspect
        quality=_inspect(folder/'control.mp4',Path(tmp))
    if quality!=value['quality'] or any(quality[k]!=value[k] for k in ('clock','frame_count','width','height')):
        raise RemoteError('REMOTE_FRAME_SEQUENCE')
    return value
