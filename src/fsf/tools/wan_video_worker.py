"""Optional pinned Wan VACE sequence recipe. No core model dependencies."""
from pathlib import Path
import argparse,time,shutil,json,os,sys

_PHASE='IMPORT'


def generation_parameters(value):
    required={'weight_bytes','prompt','negative_prompt','seed','inference_steps','window_frames','overlap_frames'}
    if not isinstance(value,dict) or set(value)!=required:raise ValueError('REMOTE_PARAMETERS')
    if (any(type(value[k]) is not int for k in ('weight_bytes','seed','inference_steps','window_frames','overlap_frames'))
            or value['weight_bytes']<1 or not 0<=value['seed']<2**32 or not 1<=value['inference_steps']<=50
            or value['window_frames'] not in {17,33,49,65,81} or not 1<=value['overlap_frames']<value['window_frames']
            or not isinstance(value['prompt'],str) or not 1<=len(value['prompt'])<=2048
            or not isinstance(value['negative_prompt'],str) or len(value['negative_prompt'])>2048):
        raise ValueError('REMOTE_PARAMETERS')
    return dict(value)


def prompt_acknowledgement(parameters,requirements):
    from fsf.remote.contracts import digest,RemoteError
    if requirements is None:return None
    if (not isinstance(requirements,dict) or requirements.get('schema_version')!='fsf_consistency_requirements_v1'
            or requirements.get('requirements_digest')!=digest({k:v for k,v in requirements.items() if k!='requirements_digest'})):
        raise RemoteError('REMOTE_REQUIREMENTS_DIGEST')
    if (requirements.get('ref_image_shas') or requirements.get('asset_ref_digests') or requirements.get('resolved_sha256')
            or requirements.get('prompt_only_allowed') is not True):
        raise RemoteError('REMOTE_CONDITION_REQUIRED')
    if requirements.get('effective_prompt')!=parameters['prompt']:
        raise RemoteError('REMOTE_PARAMETERS')
    return {'acknowledged_digest':requirements['requirements_digest'],'capability_gap':None,
            'ref_assets_used':[],'continuity_method':'prompt_only'}


def acknowledgement_output(ack):
    return {'consistency_acknowledgement':ack,'condition_acknowledgement':None}


def failure_codes(phase,exc):
    phases={'IMPORT','DOWNLOAD','HASH','LOAD','CONTROL','CUDA_MEASUREMENT','INFERENCE','ASSEMBLY','PACKAGE','RECEIPT'}
    kinds={'ImportError','ModuleNotFoundError','AttributeError','TypeError','ValueError','RuntimeError',
           'OSError','FileNotFoundError','OutOfMemoryError','HfHubHTTPError','LocalEntryNotFoundError',
           'ProductionError','EditStoreError','AssetValidationError','RemoteError'}
    phase=phase if phase in phases else 'IMPORT'
    kind=type(exc).__name__ if type(exc).__name__ in kinds else 'RuntimeError'
    result=['REMOTE_WAN_'+phase+'_'+kind.upper()]
    known={'PRODUCTION_DECODE_FAILED','PRODUCTION_TOOLS_UNAVAILABLE','PRODUCTION_GEOMETRY_UNSUPPORTED',
           'PRODUCTION_CLOCK_UNSUPPORTED','PRODUCTION_TIMING_INVALID','PRODUCTION_SOURCE_CHANGED',
           'PRODUCTION_INVALID','EDIT_IO','EDIT_LOCK_TIMEOUT','ASSET_PATH','REMOTE_PARAMETERS',
           'PRODUCTION_STREAM_INVALID','PRODUCTION_SOURCE_LIMIT','PRODUCTION_DECODE_TIMEOUT'}
    if getattr(exc,'code',None) in known:result.append(exc.code)
    message=str(exc).lower()
    for word in ('protobuf','xet','cuda','sentencepiece','torchvision','safetensors'):
        if word in message:result.append('REMOTE_WAN_HINT_'+word.upper())
    return result


def run(request_path,control_path,output,archive_path,receipt_path):
    global _PHASE
    # Use the plain authenticated/public HTTPS downloader, not an additional
    # implicit Xet credential/transport path on isolated worker hosts.
    os.environ['HF_HUB_DISABLE_XET']='1'
    import torch
    from PIL import Image
    from diffusers import WanVACEPipeline,AutoencoderKLWan
    from huggingface_hub import snapshot_download
    from fsf.remote.contracts import read_json,sha,seal,write_json,digest,RemoteError
    from fsf.remote.gpu import cuda_inventory
    from fsf.remote.bundle import pack
    from fsf.production.control import import_control,verify_control
    from fsf.production.segments import compile_segments
    from fsf.production.video_sequence import run_sequence
    request=read_json(request_path);spec=request['recipe']['execution_requirements']['model'];lock=spec['lock']
    parameters=generation_parameters(request['parameters'])
    ack=prompt_acknowledgement(parameters,request.get('consistency_requirements'))
    if ack is not None and not request['recipe']['acknowledgement_output']:
        raise RemoteError('REMOTE_ACK_OUTPUT')
    if lock['model_id']!='Wan-AI/Wan2.1-VACE-1.3B-diffusers' or lock['precision']!='float16':
        raise RemoteError('REMOTE_MODEL_INTEGRITY')
    root=Path(output).parent
    # The host may ship FFmpeg 4.x. Configure the recipe's declared toolchain
    # in THIS process, then reject bad controls before fetching large weights.
    import static_ffmpeg
    static_ffmpeg.add_paths()
    _PHASE='CONTROL';controls=root/'controls';controls.mkdir(exist_ok=True)
    imported=import_control(control_path,output_root=controls,control_asset_id='control_A')
    bundle=controls/imported['bundle'];source=verify_control(bundle)
    # Refuse an inadequate disk before downloading; no silent smaller model.
    required=parameters['weight_bytes']+4*1024**3
    if shutil.disk_usage(root).free<required:raise RemoteError('REMOTE_DISK_LIMIT')
    _PHASE='DOWNLOAD'
    folder=Path(snapshot_download(lock['model_id'],revision=lock['revision'],allow_patterns=list(lock['files']),
                                 cache_dir=root/'model-cache',token=False))
    _PHASE='HASH';observed={name:sha(folder/name) for name in lock['files']}
    if observed!=lock['files']:raise RemoteError('REMOTE_MODEL_INTEGRITY')
    torch.set_num_threads(2)
    _PHASE='LOAD'
    vae=AutoencoderKLWan.from_pretrained(folder,subfolder='vae',torch_dtype=torch.float32,local_files_only=True)
    pipe=WanVACEPipeline.from_pretrained(folder,vae=vae,torch_dtype=torch.float16,local_files_only=True)
    pipe.enable_model_cpu_offload(gpu_id=0);pipe.vae.enable_tiling()
    # This small recipe has an explicit 64-token conditioning envelope. Refuse
    # overlong intent instead of letting the tokenizer truncate silently.
    for text in (parameters['prompt'],parameters['negative_prompt']):
        if len(pipe.tokenizer(text,truncation=False,add_special_tokens=True)['input_ids'])>64:
            raise RemoteError('REMOTE_PARAMETERS')
    cap=seal({'schema_version':'fsf_model_capability_v1','model_id':'wan_vace_13b',
      'model_revision':spec['lock_digest'],'runner_digest':request['worker_core_sha256'],'environment_digest':digest(request['recipe']['environment']),
      'qualification':'unverified','qualification_evidence_digest':'0'*64,'modalities':['control_video'],
      'clocks':[source['clock']],'dimensions':[[source['width'],source['height']]],'max_frames':parameters['window_frames'],'frame_step':4,
      'overlap_frames':parameters['overlap_frames'],'continuation':'previous_frame','tail_padding':'repeat_last_explicit'},'capability_digest')
    generation_digest=digest({'recipe_digest':request['recipe']['recipe_digest'],'parameters':parameters,'source_digest':source['source_digest']})
    plan=compile_segments(source,cap,shot_id='control_A',policy_digest=generation_digest,qualification_probe=True)
    sequence=root/'sequence';sequence.mkdir(exist_ok=True)
    events=[];calls=0;started=time.monotonic_ns()
    def generate(frames,initialized,segment,deadline):
        global _PHASE
        nonlocal calls
        _PHASE='INFERENCE'
        video=[im.copy() for im in frames]
        masks=[Image.new('RGB',im.size,'white') for im in frames]
        for i,image in enumerate(initialized):video[i]=image;masks[i]=Image.new('RGB',image.size,'black')
        def check(_pipe,index,timestep,kwargs):
            if time.monotonic()>deadline:raise RemoteError('REMOTE_DEADLINE_EXCEEDED')
            return kwargs
        a,b=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True);a.record()
        with torch.inference_mode():
            images=pipe(prompt=parameters['prompt'],negative_prompt=parameters['negative_prompt'],
                video=video,mask=masks,height=source['height'],width=source['width'],num_frames=len(video),
                num_inference_steps=parameters['inference_steps'],guidance_scale=3.0,
                generator=torch.Generator('cpu').manual_seed((parameters['seed']+segment['core'][0])%2**32),
                output_type='pil',max_sequence_length=64,callback_on_step_end=check).frames[0]
        b.record();torch.cuda.synchronize();events.append(float(a.elapsed_time(b)));calls+=1
        _PHASE='ASSEMBLY'
        return images
    from fsf.remote.model_evidence import begin_cuda_measurement
    _PHASE='CUDA_MEASUREMENT';begin_cuda_measurement(torch)
    first=run_sequence(bundle,plan,output_root=sequence,generator=generate,seconds=900,max_new_segments=2,allow_probe=True)
    checkpoint=sequence/plan['plan_digest']/plan['segments'][0]['segment_id']/'segment.json'
    before=sha(checkpoint);first_calls=calls
    result=run_sequence(bundle,plan,output_root=sequence,generator=generate,seconds=900,allow_probe=True)
    if sha(checkpoint)!=before:raise RemoteError('REMOTE_CHECKPOINT_CHANGED')
    if result['status']!='completed':raise RemoteError('REMOTE_DEADLINE_EXCEEDED')
    _PHASE='PACKAGE';shutil.copyfile(sequence/plan['plan_digest']/'movie.mp4',output)
    evidence=root/'evidence';evidence.mkdir(exist_ok=True)
    write_json(evidence/'segments.json',plan);write_json(evidence/'sequence.json',result)
    write_json(evidence/'probe.json',{'scope':'control_generation_probe_not_calibrated_story',
      'first_status':first['status'],'calls_before_pause':first_calls,'model_calls':calls,
      'resume_reused_first_checkpoint':True,'source_sha256':source['sha256'],'motion_review':'not_reviewed',
      'semantic_quality':'not_evaluated','inference_steps':parameters['inference_steps'],
      'generation_digest':generation_digest,'seed_strategy':'seed_plus_global_core_start',
      'conditioning_token_limit':64,'vae_dtype':'float32','model_dtype':'float16'})
    for segment in plan['segments']:
        shutil.copyfile(sequence/plan['plan_digest']/segment['segment_id']/'segment.json',evidence/(segment['segment_id']+'.json'))
    pack(evidence,[p.name for p in evidence.iterdir()],archive_path)
    if ack is not None:
        from fsf.remote.contracts import safe_path
        write_json(safe_path(root,request['recipe']['acknowledgement_output'],exists=False),acknowledgement_output(ack))
    _PHASE='RECEIPT';devices=cuda_inventory()
    produced={s['path']:sha(root/s['path']) for step in request['recipe']['steps'] for s in step['outputs']
              if s['path']!=spec['receipt_path']}
    value=seal({'schema_version':'fsf_model_execution_receipt_v1','request_digest':request['request_digest'],
      'lock_digest':spec['lock_digest'],'observed_files':observed,'devices':[d['uuid'] for d in devices if d['index']==0],
      'precision':'float16','model_calls':calls,'cuda_event_ms':sum(events),'started_ns':started,
      'finished_ns':time.monotonic_ns(),'peak_allocated_bytes':int(torch.cuda.max_memory_allocated()),
      'outputs':produced},'receipt_digest')
    write_json(receipt_path,value)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('request','control','out','archive','receipt'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    try:run(args.request,args.control,args.out,args.archive,args.receipt)
    except Exception as exc:
        print(' '.join(failure_codes(_PHASE,exc)),file=sys.stderr)
        raise SystemExit(2) from None
