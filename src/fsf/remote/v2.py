"""Additive remote v2 contracts and worker over immutable artifact references."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import zipfile

from fsf import __version__
from fsf.core.asset_library import _root,_no_links
from fsf.core.edit_store import edit_lock,read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.remote.contracts import (RemoteError,seal,digest,sha,write_json,safe_path,reject_inline_credentials,
    validate_recipe as recipe_v1,validate_profile as profile_v1,compile_child_plan)
from fsf.remote.artifacts import (ArtifactStore,shape,validate_reference,store_public_identity)
from fsf.remote.gpu import authorize_gpu,cuda_inventory


def worker_identity():
    root=Path(__file__).resolve().parents[1]
    return digest({p.relative_to(root).as_posix():sha(p) for p in sorted(root.rglob('*'))
                   if p.is_file() and p.suffix in {'.py','.json'} and '__pycache__' not in p.parts})


def wheel_identity(path):
    with zipfile.ZipFile(path) as archive:
        return digest({name[4:]:hashlib.sha256(archive.read(name)).hexdigest() for name in sorted(archive.namelist())
                       if name.startswith('fsf/') and Path(name).suffix in {'.py','.json'} and '__pycache__' not in name.split('/')})


def validate_recipe(value):
    shape(value,'fsf_remote_recipe_v2');reject_inline_credentials(value)
    if seal(value,'recipe_digest')['recipe_digest']!=value['recipe_digest']:raise RemoteError('REMOTE_INTEGRITY')
    base={k:v for k,v in value.items() if k!='execution_requirements'}
    base['schema_version']='fsf_remote_recipe_v1';recipe_v1(seal(base,'recipe_digest'))
    if any(spec['artifact_id'] in {'environment','child_journal'} for step in value['steps'] for spec in step['outputs']):raise RemoteError('REMOTE_DUPLICATE_ID')
    render=value['execution_requirements']['render']
    if render['device']=='cpu':
        if render['backend']!='none' or render['device_count'] or render['min_vram_mb']:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    elif render['backend']!='cuda' or not 1<=render['device_count']<=16 or render['min_vram_mb']<1:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    if 'schedule_digest' in value['execution_requirements']:
        if render['device']!='gpu' or not any(i['artifact_id']=='gpu_schedule' and i['role']=='data' for i in value['inputs']):
            raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
    return copy.deepcopy(value)


def child_plan(recipe):
    recipe=validate_recipe(recipe)
    base={k:v for k,v in recipe.items() if k!='execution_requirements'}
    base['schema_version']='fsf_remote_recipe_v1'
    plan=compile_child_plan(seal(base,'recipe_digest'))
    plan['request_id']='child_'+recipe['recipe_digest'][:24]
    for step in plan['steps']:step['inputs']['recipe_digest']=recipe['recipe_digest']
    return plan


def validate_profile(value,store=None):
    shape(value,'fsf_remote_profile_v2');reject_inline_credentials(value)
    base={k:v for k,v in value.items() if k not in {'gpu_limits','store_binding','group_limits','allocation'}}
    base=copy.deepcopy(base)
    if value['transport']=='kaggle':
        native_slot=base['settings'].pop('native_notebook',None)
        if native_slot is not None and (not isinstance(native_slot,str) or not re.fullmatch(r'fsf-[a-z0-9][a-z0-9-]{0,70}',native_slot)):
            raise RemoteError('REMOTE_KAGGLE_WORKER_NOTEBOOK')
        image=base['settings'].pop('docker_image',None)
        if image is not None and (not isinstance(image,str) or not re.fullmatch(r'gcr\.io/kaggle(?:-private-byod|-gpu-images)?/python@sha256:[0-9a-f]{64}',image)):
            raise RemoteError('REMOTE_KAGGLE_IMAGE_PIN')
        activation=base['settings'].pop('activation_mode','api_push')
        if activation not in {'api_push','agent_browser'}:
            raise RemoteError('REMOTE_KAGGLE_ACTIVATION_MODE')
        worker_notebook=base['settings'].pop('worker_notebook',None)
        if worker_notebook is not None and (not isinstance(worker_notebook,str) or not re.fullmatch(r'fsf-worker-[a-z0-9][a-z0-9-]{0,47}',worker_notebook)):
            raise RemoteError('REMOTE_KAGGLE_WORKER_NOTEBOOK')
        if activation=='agent_browser' and worker_notebook is None:
            raise RemoteError('REMOTE_KAGGLE_WORKER_NOTEBOOK')
        accelerator=base['settings'].pop('accelerator',None)
        if accelerator is None or (value['gpu_limits']['allowed'] and not re.fullmatch('[A-Za-z0-9_-]{1,96}',accelerator)):
            raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    base['schema_version']='fsf_remote_profile_v1';profile_v1(base)
    binding=value['store_binding']
    for name in ('input_prefix','output_prefix'):
        from fsf.remote.artifacts import _key
        if not binding[name].endswith('/'):raise RemoteError('REMOTE_STORE_NAMESPACE')
        _key(binding[name][:-1])
    if value['gpu_limits']['allowed']!=value['policy']['gpu']:raise RemoteError('REMOTE_GPU_NOT_AUTHORIZED')
    if store:
        if binding['store_id']!=store.profile['store_id'] or binding['config_digest']!=store_public_identity(store.profile):
            raise RemoteError('REMOTE_STORE_FORBIDDEN')
        for name in ('input_prefix','output_prefix'):
            if not any(binding[name].startswith(p) for p in store.profile['read_prefixes']):raise RemoteError('REMOTE_STORE_FORBIDDEN')
    return copy.deepcopy(value)


def validate_request(request,store=None):
    shape(request,'fsf_remote_request_v2');reject_inline_credentials(request)
    if seal(request,'request_digest')['request_digest']!=request['request_digest']:raise RemoteError('REMOTE_INTEGRITY')
    recipe=validate_recipe(request['recipe'])
    if request['fsf_version']!=__version__ or request['worker_core_sha256']!=worker_identity() or request['child']['plan']!=child_plan(recipe):
        raise RemoteError('REMOTE_WORKER_VERSION')
    binding=digest({'parent':request['parent'],'recipe':recipe['recipe_digest'],'inputs':request['inputs'],'segment':request['segment']})
    if request['child']!={'job_id':'child_'+binding[:24],'run_id':'run_'+binding[24:48],'attempt_id':request['parent']['attempt_id'],'plan':child_plan(recipe)}:
        raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
    from fsf.remote.preflight import validate_definition
    projection={k:v for k,v in recipe.items() if k!='execution_requirements'}
    projection['schema_version']='fsf_remote_recipe_v1';projection=seal(projection,'recipe_digest')
    definition={k:request[k] for k in ('parent','parameters','video_condition','consistency_requirements','expected_media_contract')}
    definition['inputs']={a['artifact_id']:a['path'] for a in request['inputs']}
    validate_definition(definition,projection)
    segment=request['segment']
    from fractions import Fraction
    if segment['end_frame']<=segment['start_frame'] or segment['end_frame']-segment['start_frame']>96 or Fraction(**{'numerator':segment['clock']['num'],'denominator':segment['clock']['den']}) not in {Fraction(24),Fraction(24000,1001)}:
        raise RemoteError('REMOTE_SEGMENT_MAPPING')
    if request['policy']['gpu']!=(recipe['execution_requirements']['render']['device']=='gpu'):
        raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
    expected={a['artifact_id']:a for a in recipe['inputs']}
    seen=set()
    for item in request['inputs']:
        identifier=item['artifact_id'];reference=item['reference']
        if identifier in seen or identifier not in expected or reference['artifact_id']!=identifier or reference['role']!=expected[identifier]['role'] or reference['media_type']!=expected[identifier]['media_type']:
            raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
        seen.add(identifier)
        from fsf.core.asset_library import _path_parts
        parts=_path_parts(item['path'])
        if len(parts)!=2 or parts[0]!='inputs' or not parts[1].startswith(identifier+'.'):raise RemoteError('REMOTE_PATH')
    if seen!=set(expected):raise RemoteError('REMOTE_INPUTS')
    all_refs=[request['worker_wheel'],*(a['reference'] for a in request['inputs'])]
    if request['worker_wheel']['artifact_id']!='worker_wheel' or request['worker_wheel']['role']!='worker_wheel':raise RemoteError('REMOTE_WHEEL')
    filename=Path(request['worker_wheel']['object_key']).name
    if not filename.startswith('free_studio_flow-') or not filename.endswith('-py3-none-any.whl') or request['worker_wheel']['size_bytes']>64*1024*1024:
        raise RemoteError('REMOTE_WHEEL')
    if store:
        if request['store_binding']['config_digest']!=store_public_identity(store.profile):raise RemoteError('REMOTE_STORE_FORBIDDEN')
        total=0
        for ref in all_refs:
            validate_reference(ref,store.profile)
            if not ref['object_key'].startswith(request['store_binding']['input_prefix']):raise RemoteError('REMOTE_STORE_FORBIDDEN')
            total+=ref['size_bytes']
        if total>store.profile['max_total_bytes']:raise RemoteError('REMOTE_SIZE_LIMIT')
    return copy.deepcopy(request)


def check_authorization(request,profile,store,*,new_submission=True):
    profile=validate_profile(profile,store);request=validate_request(request,store)
    if os.environ.get('FSF_REMOTE_DEPTH'):raise RemoteError('REMOTE_NESTING_FORBIDDEN')
    if not profile['authorized'] or request['profile_id']!=profile['profile_id'] or request['adapter_digest']!=digest(profile) or request['recipe']['recipe_digest'] not in profile['approved_recipes']:
        raise RemoteError('REMOTE_NOT_AUTHORIZED')
    if request['store_binding']!=profile['store_binding']:raise RemoteError('REMOTE_STORE_FORBIDDEN')
    for key,value in request['policy'].items():
        limit=profile['policy'][key]
        if isinstance(value,list):
            if not set(value)<=set(limit):raise RemoteError('REMOTE_NOT_AUTHORIZED')
        elif key!='gpu' and (type(value) is not int or value>limit):raise RemoteError('REMOTE_NOT_AUTHORIZED')
    authorize_gpu(request['recipe']['execution_requirements'],profile['gpu_limits'],new_submission=new_submission)
    return request


def prepare_request(definition,recipe,*,profile,store,inputs,wheel,segment,output):
    recipe=validate_recipe(recipe);profile=validate_profile(profile,store)
    if not profile['authorized'] or recipe['recipe_digest'] not in profile['approved_recipes']:raise RemoteError('REMOTE_NOT_AUTHORIZED')
    authorize_gpu(recipe['execution_requirements'],profile['gpu_limits'])
    from fsf.remote.preflight import validate_definition
    projection={k:v for k,v in recipe.items() if k!='execution_requirements'};projection['schema_version']='fsf_remote_recipe_v1'
    validate_definition(definition,seal(projection,'recipe_digest'))
    if set(inputs)!=set(definition['inputs']):raise RemoteError('REMOTE_INPUTS')
    root=_root(output);references=[]
    for spec in recipe['inputs']:
        identifier=spec['artifact_id'];ref=inputs[identifier]
        suffix=Path(ref['object_key']).suffix
        if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):suffix='.bin'
        references.append({'artifact_id':identifier,'path':'inputs/'+identifier+suffix,'reference':ref})
    binding=digest({'parent':definition['parent'],'recipe':recipe['recipe_digest'],'inputs':references,'segment':segment})
    policy=copy.deepcopy(profile['policy']);policy['gpu']=recipe['execution_requirements']['render']['device']=='gpu'
    request={'schema_version':'fsf_remote_request_v2','protocol_version':'2','fsf_version':__version__,
             'parent':definition['parent'],'profile_id':profile['profile_id'],'adapter_digest':digest(profile),
             'child':{'job_id':'child_'+binding[:24],'run_id':'run_'+binding[24:48],'attempt_id':definition['parent']['attempt_id'],'plan':child_plan(recipe)},
             'recipe':recipe,'parameters':definition['parameters'],'inputs':references,'policy':policy,
             'worker_core_sha256':worker_identity(),'worker_wheel':wheel,'video_condition':definition['video_condition'],
             'consistency_requirements':definition['consistency_requirements'],'expected_media_contract':definition['expected_media_contract'],
             'store_binding':profile['store_binding'],'segment':segment}
    request=seal(request,'request_digest');check_authorization(request,profile,store)
    # The request is a small control object; input bytes are never embedded.
    path=root/(request['request_digest']+'.json')
    if path.exists():
        if read_json(path)!=request:raise RemoteError('REMOTE_INTENT_CONFLICT')
    else:write_json(path,request)
    return request


def run_worker(request,*,store,output_root,approved_recipe):
    request=validate_request(request,store)
    old_cancel=store.cancelled
    cancel_path=_root(output_root)/request['request_digest']/'cancel'
    store.cancelled=lambda:cancel_path.exists() or bool(old_cancel and old_cancel())
    try:
        with store.budget(time.monotonic()+request['policy']['deadline_seconds']):
            try:
                return _run_worker(request,store=store,output_root=output_root,approved_recipe=approved_recipe)
            except BaseException as exc:
                key=request.get('request_digest','')
                if re.fullmatch('[0-9a-f]{64}',key):
                    directory=_root(output_root)/key;directory.mkdir(exist_ok=True)
                    cancelled=isinstance(exc,KeyboardInterrupt) or isinstance(exc,RemoteError) and exc.code=='REMOTE_CANCELLED'
                    write_json(directory/'worker-status-v2.json',{'request_digest':key,'status':'cancelled' if cancelled else 'failed'})
                raise
    finally:store.cancelled=old_cancel


def _run_worker(request,*,store,output_root,approved_recipe):
    request=validate_request(request,store)
    if request['recipe']['recipe_digest']!=approved_recipe:raise RemoteError('REMOTE_RECIPE_NOT_APPROVED')
    root=_root(output_root);key=request['request_digest'];directory=root/key
    with edit_lock(root/'.v2-worker'):
        directory.mkdir(exist_ok=True);_no_links(directory)
        receipt_path=directory/'result.json'
        if receipt_path.exists():
            result=read_json(receipt_path)
            validate_result(result,request,store)
            transfer=directory/'transfer';transfer.mkdir(exist_ok=True)
            store.put(receipt_path,artifact_id='result',key=request['store_binding']['output_prefix']+key+'/result.json',
                      role='data',media_type='application/json',state_root=transfer)
            write_json(directory/'worker-status-v2.json',{'request_digest':key,'status':result['status']})
            return result
        write_json(directory/'worker-status-v2.json',{'request_digest':key,'status':'running'})
        deadline=time.monotonic()+request['policy']['deadline_seconds']
        transfer=directory/'transfer';transfer.mkdir(exist_ok=True)
        runtime=directory/'runtime';runtime.mkdir(exist_ok=True)
        package=runtime/'worker.whl'
        store.get(request['worker_wheel'],output=package,state_root=transfer)
        if wheel_identity(package)!=request['worker_core_sha256']:raise RemoteError('REMOTE_STALE_WHEEL')
        inputs=directory/'inputs';inputs.mkdir(exist_ok=True)
        outputs=directory/'outputs';outputs.mkdir(exist_ok=True)
        for item in request['inputs']:
            store.get(item['reference'],output=safe_path(directory,item['path'],exists=False),state_root=transfer)
        recipe=request['recipe'];environment={'schema_version':'fsf_remote_environment_v2','python':__import__('platform').python_version(),
            'gpu_available':None,'gpu_render_verified':False,'probes':[]}
        journal_path=directory/'child-journal.json'
        journal=read_json(journal_path) if journal_path.exists() else seal({'schema_version':'fsf_remote_child_journal_v2','request_digest':key,'recipe_digest':recipe['recipe_digest'],'steps':{}},'journal_digest')
        if journal['request_digest']!=key or seal(journal,'journal_digest')['journal_digest']!=journal.get('journal_digest'):raise RemoteError('REMOTE_CHILD_JOURNAL')
        env={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
        from fsf.remote.environment import INSTALL_NETWORK,install_environment
        for name in recipe['environment']['credential_env_names']:
            if name in {'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN'}:raise RemoteError('REMOTE_CREDENTIAL_NOT_AUTHORIZED')
            if name not in request['policy']['credential_env_names'] or name not in os.environ:raise RemoteError('REMOTE_CREDENTIAL_MISSING')
            if name not in INSTALL_NETWORK:env[name]=os.environ[name]
        env['FSF_REMOTE_DEPTH']='1'
        from fsf.remote.gpu import gpu_runtime_environment
        env.update(gpu_runtime_environment())
        replacements={'python':sys.executable,'request':str(directory/'request.json')}
        for tool in ('blender','ffmpeg'):
            executable=shutil.which(tool)
            if executable:replacements[tool]=executable
        replacements.update({'param.'+k:str(v) for k,v in request['parameters'].items()})
        replacements.update({'input.'+i['artifact_id']:str(directory/i['path']) for i in request['inputs']})
        for step in recipe['steps']:
            for spec in step['outputs']:replacements['output.'+spec['artifact_id']]=str(safe_path(outputs,spec['path'],exists=False))
        write_json(directory/'request.json',request)
        def run(args,installation=False):
            left=deadline-time.monotonic()
            if left<=0:raise RemoteError('REMOTE_DEADLINE_EXCEEDED')
            def resolve(arg):
                return re.sub(r'\{([A-Za-z0-9_.-]+)\}',lambda m:replacements[m[1]],arg)
            result=run_safe_subprocess([resolve(a) for a in args],cwd=directory,env=install_environment(env,request['policy']['credential_env_names']) if installation else env,
                inherit_env=False,timeout_seconds=left,cancel_check=lambda:(directory/'cancel').exists(),output_limit_bytes=1024*1024)
            if result.timed_out:raise RemoteError('REMOTE_DEADLINE_EXCEEDED')
            if result.returncode:
                codes=re.findall(r'\b(?:BLENDER|REMOTE|AUDIO|PROCESS)_[A-Z_]{2,64}\b',result.stderr)
                environment.setdefault('command_failures',[]).append({'returncode':result.returncode,'codes':sorted(set(codes))})
                schedule_path=directory/'schedule/schedule-receipt.json'
                if schedule_path.is_file():
                    observed=read_json(schedule_path)
                    environment['schedule_diagnostic']={k:{field:v[field] for field in ('status','attempt','error_code') if field in v} for k,v in observed.get('tasks',{}).items()}
                if installation:
                    from fsf.remote.environment import installation_error
                    raise RemoteError(installation_error(result.stderr))
                raise RemoteError('REMOTE_COMMAND_FAILED')
            if sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())>request['policy']['disk_mb']*1024*1024:raise RemoteError('REMOTE_DISK_LIMIT')
            return result
        artifacts=[];error=None;ack={}
        try:
            # Host capability failures also need a bound failed result. They
            # must not disappear as an opaque bootstrap subprocess failure.
            if recipe['environment']['os'] not in {'any',__import__('platform').system().lower()} or not environment['python'].startswith(recipe['environment']['python']):
                raise RemoteError('REMOTE_ENVIRONMENT_MISMATCH')
            if recipe['execution_requirements']['render']['device']=='gpu':
                environment['gpu_available']=cuda_inventory(min_vram_mb=recipe['execution_requirements']['render']['min_vram_mb'])
            if 'schedule_digest' in recipe['execution_requirements']:
                from fsf.remote.gpu_schedule import validate_schedule
                spec=next(i for i in request['inputs'] if i['artifact_id']=='gpu_schedule')
                schedule=validate_schedule(read_json(directory/spec['path']))
                if digest(schedule)!=recipe['execution_requirements']['schedule_digest'] or schedule['authorized_devices']!=recipe['execution_requirements']['render']['device_count']:
                    raise RemoteError('REMOTE_GPU_SCHEDULE_IDENTITY')
            for setup in recipe['environment']['setup']:
                if setup['source'] not in request['policy']['network_sources']:raise RemoteError('REMOTE_NETWORK_NOT_AUTHORIZED')
                run(setup['args'],True)
                if 'fsf.remote.environment' in setup['args']:
                    pinned=setup['args'][setup['args'].index('--digest')+1]
                    if not re.fullmatch('[0-9a-f]{64}',pinned):raise RemoteError('REMOTE_ENVIRONMENT_DIGEST')
                    receipt=read_json(safe_path(directory,'.prepared-tools/'+pinned+'/receipt.json'))
                    if seal(receipt,'receipt_digest')['receipt_digest']!=receipt.get('receipt_digest') or receipt.get('environment_digest')!=pinned:raise RemoteError('REMOTE_ENVIRONMENT_DIGEST')
                    environment.setdefault('prepared_tools',[]).append({'environment_digest':pinned,'receipt_digest':receipt['receipt_digest'],'tools':receipt['tools']})
            for probe in recipe['environment']['probes']:
                result=run([probe['program'],*probe['args']])
                if not re.search(probe['pattern'],result.stdout):raise RemoteError('REMOTE_ENVIRONMENT_MISMATCH')
                environment['probes'].append({'probe_digest':digest(probe),'matched':True})
            for model in recipe['environment']['models']:
                if sha(safe_path(directory,model['path']))!=model['sha256']:raise RemoteError('REMOTE_MODEL_INTEGRITY')
            for step in recipe['steps']:
                sid=step['step_id'];prior=journal['steps'].get(sid)
                if prior:
                    if prior['status']!='completed' or prior['step_digest']!=digest(step):raise RemoteError('REMOTE_EXECUTION_UNKNOWN')
                    for item in prior['outputs']:
                        if sha(safe_path(outputs,item['path']))!=item['sha256']:raise RemoteError('REMOTE_OUTPUT_CHANGED')
                    continue
                journal['steps'][sid]={'status':'running','step_digest':digest(step)}
                journal=seal(journal,'journal_digest');write_json(journal_path,journal)
                run(step['args'])
                observed=[]
                for spec in step['outputs']:
                    path=safe_path(outputs,spec['path'])
                    from fsf.remote.contracts import reject_credential_bytes
                    reject_credential_bytes(path,[env.get(name) for name in recipe['environment']['credential_env_names']])
                    observed.append({'path':spec['path'],'sha256':sha(path),'size_bytes':path.stat().st_size})
                journal['steps'][sid]={'status':'completed','step_digest':digest(step),'outputs':observed}
                journal=seal(journal,'journal_digest');write_json(journal_path,journal)
            for item in request['inputs']:
                if sha(directory/item['path'])!=item['reference']['sha256']:raise RemoteError('REMOTE_INPUT_INTEGRITY')
            for step in recipe['steps']:
                for spec in step['outputs']:
                    path=safe_path(outputs,spec['path'])
                    ref=store.put(path,artifact_id=spec['artifact_id'],key=request['store_binding']['output_prefix']+key+'/outputs/'+spec['path'],
                        role=spec['role'],media_type=spec['media_type'],state_root=transfer)
                    artifacts.append({'artifact_id':spec['artifact_id'],'reference':ref})
            # GPU availability alone is never certified actual GPU execution.
            if request['policy']['gpu'] and 'schedule_digest' not in recipe['execution_requirements']:
                from fsf.runtime.blender import verify_bundle
                candidates=list(outputs.rglob('receipt.json'))
                verified=False
                from fsf.remote.bundle import unpack
                for step in recipe['steps']:
                    for spec in step['outputs']:
                        path=safe_path(outputs,spec['path'])
                        if zipfile.is_zipfile(path):
                            inspection=Path(tempfile.mkdtemp(prefix='.gpu-inspect-',dir=directory))
                            unpack(path,inspection)
                            if (inspection/'receipt.json').exists():candidates.append(inspection/'receipt.json')
                for candidate in candidates:
                    manifest=verify_bundle(candidate.parent)
                    if (manifest['schema_version']=='fsf_blender_manifest_v2' and manifest['execution_evidence']['device']=='GPU'
                            and manifest['render_profile']==recipe['execution_requirements']['render']
                            and manifest['encode_profile']==recipe['execution_requirements']['encode']
                            and manifest['clock']==request['segment']['clock']
                            and sorted({f['scene_frame'] for f in manifest['frame_mapping']})==list(range(request['segment']['start_frame'],request['segment']['end_frame']))):
                        verified=True
                if not verified:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
                environment['gpu_render_verified']=True
            if 'schedule_digest' in recipe['execution_requirements']:
                from fsf.remote.gpu_batch import verify_gpu_batch
                batches=[safe_path(outputs,s['path']) for step in recipe['steps'] for s in step['outputs'] if s['artifact_id']=='gpu_batch']
                if len(batches)!=1:raise RemoteError('REMOTE_OUTPUTS')
                environment['gpu_schedule']=verify_gpu_batch(batches[0],request,schedule,directory)
                environment['gpu_render_verified']=True
            if request['video_condition'] or request['consistency_requirements']:
                if not recipe['acknowledgement_output']:raise RemoteError('REMOTE_ACK_OUTPUT')
                ack=read_json(safe_path(outputs,recipe['acknowledgement_output']))
        except KeyboardInterrupt:error='REMOTE_CANCELLED'
        except Exception as exc:error=exc.code if isinstance(exc,RemoteError) else 'REMOTE_WORKER_FAILED'
        write_json(directory/'environment.json',environment)
        journal=seal(journal,'journal_digest');write_json(journal_path,journal)
        for artifact_id,name in (('environment','environment.json'),('child_journal','child-journal.json')):
            ref=store.put(directory/name,artifact_id=artifact_id,key=request['store_binding']['output_prefix']+key+'/evidence/'+name,
                          role='data',media_type='application/json',state_root=transfer)
            artifacts.append({'artifact_id':artifact_id,'reference':ref})
        result=seal({'schema_version':'fsf_remote_result_v2','request_digest':key,'recipe_digest':recipe['recipe_digest'],
            'worker_core_sha256':worker_identity(),'parent':request['parent'],'child':request['child'],'segment':request['segment'],
            'status':'cancelled' if error=='REMOTE_CANCELLED' else ('failed' if error else 'completed'),'error_code':error,
            'artifacts':artifacts,'environment_digest':digest(environment),'child_journal_sha256':sha(journal_path),
            'condition_acknowledgement':ack.get('condition_acknowledgement'),
            'consistency_acknowledgement':ack.get('consistency_acknowledgement'),
            'motion_review':'not_reviewed'},'result_digest')
        shape(result,'fsf_remote_result_v2');write_json(receipt_path,result)
        store.put(receipt_path,artifact_id='result',key=request['store_binding']['output_prefix']+key+'/result.json',
                  role='data',media_type='application/json',state_root=transfer)
        write_json(directory/'worker-status-v2.json',{'request_digest':key,'status':result['status']})
        return result


def validate_result(result,request,store):
    shape(result,'fsf_remote_result_v2')
    if seal(result,'result_digest')['result_digest']!=result['result_digest']:raise RemoteError('REMOTE_ARTIFACT_INTEGRITY')
    for key in ('request_digest','parent','child','segment','worker_core_sha256'):
        if result[key]!=request[key]:raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
    if result['recipe_digest']!=request['recipe']['recipe_digest']:raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
    identifiers=set()
    total=0
    expected={spec['artifact_id']:{**spec,'path':'outputs/'+spec['path']} for step in request['recipe']['steps'] for spec in step['outputs']}
    expected.update({name:{'path':'evidence/'+path,'role':'data','media_type':'application/json'} for name,path in [('environment','environment.json'),('child_journal','child-journal.json')]})
    for item in result['artifacts']:
        ref=item['reference'];validate_reference(ref,store.profile)
        if item['artifact_id'] in identifiers or ref['artifact_id']!=item['artifact_id'] or not ref['object_key'].startswith(request['store_binding']['output_prefix']+request['request_digest']+'/'):
            raise RemoteError('REMOTE_ARTIFACT_IDENTITY')
        identifiers.add(item['artifact_id'])
        total+=ref['size_bytes']
        spec=expected.get(item['artifact_id'])
        if not spec or any(ref[k]!=spec[k] for k in ('role','media_type')) or ref['object_key']!=request['store_binding']['output_prefix']+request['request_digest']+'/'+spec['path']:
            raise RemoteError('REMOTE_OUTPUTS')
    if result['status']=='completed' and (identifiers!=set(expected) or result['error_code'] is not None):raise RemoteError('REMOTE_OUTPUTS')
    if total>store.profile['max_total_bytes']:raise RemoteError('REMOTE_SIZE_LIMIT')
    return result


def discover_result(request,store):
    key=request['store_binding']['output_prefix']+request['request_digest']+'/result.json'
    head=store._head(key,time.monotonic()+store.profile['deadline_seconds'])
    ref={'artifact_id':'result','store_id':store.profile['store_id'],'object_key':key,'version_id':head.get('VersionId'),
         'sha256':head.get('Metadata',{}).get('sha256'),'size_bytes':head.get('ContentLength'),'role':'data','media_type':'application/json'}
    validate_reference(ref,store.profile)
    if ref['size_bytes']>512*1024:raise RemoteError('REMOTE_SIZE_LIMIT')
    return ref


def collect_result(request,*,store,directory,recovery=False):
    directory=_root(directory);state=directory/'transfer';state.mkdir(exist_ok=True)
    scratch=Path(tempfile.mkdtemp(prefix='.v2-collect-',dir=directory))
    try:
        ref=discover_result(request,store)
        store.get(ref,output=scratch/'result.json',state_root=state)
        result=validate_result(read_json(scratch/'result.json'),request,store)
        if result['status']!='completed':raise RemoteError('REMOTE_JOB_NOT_COMPLETED')
        declared={spec['artifact_id']:spec for step in request['recipe']['steps'] for spec in step['outputs']}
        for item in result['artifacts']:
            identifier=item['artifact_id'];artifact=item['reference']
            relative={'environment':'environment.json','child_journal':'child-journal.json'}.get(identifier)
            if relative is None:relative='outputs/'+declared[identifier]['path']
            target=safe_path(scratch,relative,exists=False)
            store.get(artifact,output=target,state_root=state)
            if artifact['role']=='video':
                from fsf.conformance.media import evaluate_media_file
                report,_=evaluate_media_file(target,expected_contract=request['expected_media_contract'])
                if report.get('evaluation_status')!='conforms':raise RemoteError('REMOTE_QC_FAILED')
        environment=read_json(scratch/'environment.json');journal=read_json(scratch/'child-journal.json')
        if digest(environment)!=result['environment_digest'] or sha(scratch/'child-journal.json')!=result['child_journal_sha256']:
            raise RemoteError('REMOTE_ENVIRONMENT_INTEGRITY')
        shape(journal,'fsf_remote_child_journal_v2')
        if journal['request_digest']!=request['request_digest'] or journal['recipe_digest']!=request['recipe']['recipe_digest'] or seal(journal,'journal_digest')['journal_digest']!=journal['journal_digest']:
            raise RemoteError('REMOTE_CHILD_JOURNAL')
        if set(journal['steps'])!={s['step_id'] for s in request['recipe']['steps']}:raise RemoteError('REMOTE_CHILD_JOURNAL')
        for step in request['recipe']['steps']:
            record=journal['steps'][step['step_id']]
            if record['status']!='completed' or record['step_digest']!=digest(step):raise RemoteError('REMOTE_CHILD_JOURNAL')
            actual=[{'path':spec['path'],'sha256':sha(scratch/'outputs'/spec['path']),'size_bytes':(scratch/'outputs'/spec['path']).stat().st_size} for spec in step['outputs']]
            if record['outputs']!=actual:raise RemoteError('REMOTE_CHILD_JOURNAL')
        if request['policy']['gpu'] and environment.get('gpu_render_verified') is not True:raise RemoteError('REMOTE_GPU_DEVICE_MISMATCH')
        if 'schedule_digest' in request['recipe']['execution_requirements']:
            from fsf.remote.gpu_batch import verify_gpu_batch
            spec=next(i for i in request['inputs'] if i['artifact_id']=='gpu_schedule')
            source=scratch/'schedule.json';store.get(spec['reference'],output=source,state_root=state)
            batch=declared.get('gpu_batch')
            if batch is None:raise RemoteError('REMOTE_OUTPUTS')
            verify_gpu_batch(scratch/'outputs'/batch['path'],request,read_json(source),scratch)
        if request['video_condition']:
            from fsf.runtime.blender import validate_condition_ack
            validate_condition_ack(request['video_condition'],request['recipe']['condition_capability'],result['condition_acknowledgement'])
        if request['consistency_requirements']:
            from fsf.runtime.handoff import build_workorder,ingest_workorder_result
            parent=request['parent'];spec=next(iter(declared.values()));target='outputs/'+spec['path']
            work=build_workorder(job_id=parent['job_id'],run_id=parent['run_id'],step_id=parent['step_id'],attempt_id=parent['attempt_id'],
                tool=request['recipe']['steps'][0]['tool'],plan_digest=parent['plan_digest'],inputs_digest=parent['inputs_digest'],
                target_artifact=target,task_payload={},consistency_requirements=request['consistency_requirements'])
            projection={k:work[k] for k in ('job_id','run_id','step_id','attempt_id','workorder_id','plan_digest','inputs_digest')}
            projection.update(schema_version='fsf_workorder_result_v1',status='completed',artifact_path=target,artifact_sha256=sha(scratch/target),consistency_acknowledgement=result['consistency_acknowledgement'])
            write_json(scratch/'.ack.json',projection)
            ingest_workorder_result(scratch/'.ack.json',work,scratch);(scratch/'.ack.json').unlink()
        destination=directory/('recovered' if recovery else 'accepted')
        if destination.exists():
            if read_json(destination/'result.json')!=result:raise RemoteError('REMOTE_RESULT_CONFLICT')
            # Every fetched byte was reverified above, including on reuse.
        else:os.replace(scratch,destination)
        return result
    finally:
        if scratch.exists():shutil.rmtree(scratch)
