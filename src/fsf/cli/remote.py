"""Parent/worker command line with non-reflective JSON errors."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from importlib import resources

from fsf.cli.edit import _Parser, _Once, _integer
from fsf.core.edit_store import read_json, EditStoreError
from fsf.remote.contracts import RemoteError, digest, validate_recipe, validate_profile, compile_child_plan, write_json


def wait_dispatch(key, *, profile, state_root):
    from fsf.remote.dispatch import update_dispatch
    end = time.monotonic()+profile["policy"]["deadline_seconds"]
    while time.monotonic()<end:
        value=update_dispatch(key,profile=profile,state_root=state_root)
        if value["status"] in {"remote_failed","expired","cancelled","cancel_unsupported"}:
            raise RemoteError("REMOTE_JOB_NOT_COMPLETED")
        if value["status"] in {"remote_completed","verified"}:
            return update_dispatch(key,profile=profile,state_root=state_root,action="fetch")
        time.sleep(0.25 if profile["transport"] in {"local","http"} else 3)
    raise RemoteError("REMOTE_DEADLINE_EXCEEDED")


def main(argv=None):
    parser=_Parser(prog="fsf remote",description="One-level approved worker jobs; separate from legacy shape transports")
    commands=parser.add_subparsers(dest="operation",required=True)
    templates=commands.add_parser("templates",help="Copy versioned, disabled operator templates")
    templates.add_argument("--out",required=True,action=_Once)
    validate=commands.add_parser("validate",help="Validate a recipe and its fixed PDX child plan")
    validate.add_argument("recipe")
    compare=commands.add_parser("recipe-diff",help="List changed recipe paths and digests; never approve automatically")
    compare.add_argument("before")
    compare.add_argument("after")
    bind=commands.add_parser("recipe-environment",help="Bind a pinned environment plan into a new unapproved recipe")
    bind.add_argument("recipe")
    bind.add_argument("plan")
    bind.add_argument("--out",required=True,action=_Once)
    promote=commands.add_parser('recipe-v2',help='Create an unapproved v2 recipe with explicit render/encode requirements')
    promote.add_argument('recipe')
    promote.add_argument('--requirements',required=True,action=_Once)
    promote.add_argument('--out',required=True,action=_Once)
    encoder=commands.add_parser('encoder-pin',help='Measure an exact FFmpeg 8.0.1 binary with a one-frame CPU smoke')
    encoder.add_argument('--ffmpeg',required=True,action=_Once)
    encoder.add_argument('--out',required=True,action=_Once)
    prepare=commands.add_parser("prepare",help="Build an immutable parent/child request bundle")
    prepare.add_argument("definition")
    for name in ("recipe","inputs","wheel","profile","out"):
        prepare.add_argument("--"+name,required=True,action=_Once)
    preflight=commands.add_parser("preflight",help="Read-only checks; unverified capability does not mean ready")
    preflight.add_argument("definition")
    for name in ("recipe","profile"):
        preflight.add_argument("--"+name,required=True,action=_Once)
    for name in ("inputs","wheel"):
        preflight.add_argument("--"+name,action=_Once)
    preflight.add_argument("--probe",action="store_true",help="Explicit connection probe; never submit or install jobs")
    store_probe=commands.add_parser('artifact-probe',help='Check an explicitly authorized versioned object store')
    secrets_template=commands.add_parser('kaggle-secrets-template',help='Write an operator-run private CPU secret binding notebook; no submit')
    secrets_template.add_argument('--out',required=True,action=_Once)
    store_probe.add_argument('--store',required=True,action=_Once)
    artifact_put=commands.add_parser('artifact-put',help='Upload immutable bytes; checkpoint multipart progress')
    artifact_put.add_argument('file')
    for name in ('store','state','artifact-id','key','role','media-type'):
        artifact_put.add_argument('--'+name,required=True,action=_Once)
    artifact_put.add_argument('--out',action=_Once)
    artifact_get=commands.add_parser('artifact-get',help='Fetch a fixed version and independently verify bytes')
    artifact_get.add_argument('reference')
    for name in ('store','state','out'):artifact_get.add_argument('--'+name,required=True,action=_Once)
    prepare2=commands.add_parser('prepare-v2',help='Prepare a small sealed request over fixed object references')
    prepare2.add_argument('definition')
    for name in ('recipe','profile','store','inputs','wheel','segment','out'):
        prepare2.add_argument('--'+name,required=True,action=_Once)
    preflight2=commands.add_parser('preflight-v2',help='Check v2 authority and fixed versions without submitting')
    preflight2.add_argument('request')
    for name in ('profile','store'):preflight2.add_argument('--'+name,required=True,action=_Once)
    preflight2.add_argument('--probe',action='store_true')
    dispatch2=commands.add_parser('dispatch-v2',help='Submit one v2 intent once; no inline data bundle')
    dispatch2.add_argument('request')
    dispatch2.add_argument('--wait',action='store_true')
    for name in ('profile','store','state'):dispatch2.add_argument('--'+name,required=True,action=_Once)
    for name in ('status-v2','reconcile-v2','fetch-v2','cancel-v2'):
        child=commands.add_parser(name)
        child.add_argument('request_digest')
        for field in ('profile','store','state'):child.add_argument('--'+field,required=True,action=_Once)
        if name=='fetch-v2':child.add_argument('--recover',action='store_true',help='Explicitly accept fully verified recovery from terminal failed/cancelled provider')
    worker2=commands.add_parser('worker-v2',help='Run a sealed v2 request using this host object-store binding')
    worker2.add_argument('request')
    for name in ('store','out','recipe-digest'):worker2.add_argument('--'+name,required=True,action=_Once)
    segment_plan=commands.add_parser('segments-plan',help='Create global half-open Blender segment intervals')
    segment_plan.add_argument('template')
    for name in ('group-id','clock','out'):segment_plan.add_argument('--'+name,required=True,action=_Once)
    for name,default in [('start-frame',0),('end-frame',96),('segment-frames',24),('budget-seconds',900)]:
        segment_plan.add_argument('--'+name,type=_integer,default=None,action=_Once)
    for name in ('segments-pause','segments-resume'):
        child=commands.add_parser(name);child.add_argument('plan');child.add_argument('--state',required=True,action=_Once)
    segment_run=commands.add_parser('segments-run',help='Continue only unfinished segments with durable attempt budgets')
    segment_run.add_argument('plan')
    for name in ('template','profile','store','state'):segment_run.add_argument('--'+name,required=True,action=_Once)
    segment_run.add_argument('--wait',action='store_true')
    segment_deliver=commands.add_parser('segments-assemble',help='Verify every global frame and encode once with pinned CPU x264')
    segment_deliver.add_argument('plan')
    for name in ('template','profile','store','state','out','ffmpeg'):segment_deliver.add_argument('--'+name,required=True,action=_Once)
    dispatch=commands.add_parser("dispatch",help="Persist intent and submit once")
    dispatch.add_argument("bundle")
    dispatch.add_argument("--wait",action="store_true")
    for name in ("status","reconcile","fetch","cancel"):
        child=commands.add_parser(name)
        child.add_argument("dispatch_id")
        child.add_argument("--profile",required=True,action=_Once)
        child.add_argument("--state",required=True,action=_Once)
    dispatch.add_argument("--profile",required=True,action=_Once)
    dispatch.add_argument("--state",required=True,action=_Once)
    doctor=commands.add_parser("diagnose",help="Report configuration separately from authentication and verified work")
    doctor.add_argument("--profile",required=True,action=_Once)
    doctor.add_argument("--probe",action="store_true")
    worker=commands.add_parser("worker",help="Run the approved child plan on an operator-managed worker")
    worker.add_argument("bundle")
    worker.add_argument("--out",required=True,action=_Once)
    worker.add_argument("--recipe-digest",required=True,action=_Once)
    worker.add_argument("--remaining-seconds",type=_integer,action=_Once)
    serve=commands.add_parser("serve-reference",help="Run a loopback async contract reference service")
    for name in ("profile","state","worker-root"):
        serve.add_argument("--"+name,required=True,action=_Once)
    serve.add_argument("--python",default=sys.executable)
    serve.add_argument("--port",required=True,type=_integer,action=_Once)
    try:
        args=parser.parse_args(argv)
        if args.operation=='segments-plan':
            for name,default in [('start_frame',0),('end_frame',96),('segment_frames',24),('budget_seconds',900)]:
                if getattr(args,name) is None:setattr(args,name,default)
            from fsf.remote.segment_driver import compile_segments
            from fractions import Fraction
            clock=Fraction(args.clock)
            result=compile_segments(read_json(args.template),group_id=args.group_id,clock={'num':clock.numerator,'den':clock.denominator},
                start_frame=args.start_frame,end_frame=args.end_frame,segment_frames=args.segment_frames,budget_seconds=args.budget_seconds)
            if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
            write_json(args.out,result)
            result={'plan_digest':result['plan_digest'],'segment_count':len(result['segments']),'submitted':False}
        elif args.operation in {'segments-pause','segments-resume'}:
            from fsf.remote.segments import pause,resume
            result=(pause if args.operation=='segments-pause' else resume)(read_json(args.plan),state_root=args.state)
        elif args.operation in {'segments-run','segments-assemble'}:
            from fsf.remote.artifacts import ArtifactStore
            from fsf.remote.segment_driver import BlenderSegmentDriver
            store=ArtifactStore(read_json(args.store));template=read_json(args.template);plan=read_json(args.plan)
            driver_root=Path(args.state)/'driver';driver_root.mkdir(exist_ok=True)
            driver=BlenderSegmentDriver(template,profile=read_json(args.profile),store=store,store_config=args.store,state_root=driver_root)
            if args.operation=='segments-assemble':
                from fsf.remote.segment_delivery import assemble
                result=assemble(plan,driver=driver,state_root=args.state,output_root=args.out,
                    encoder=template['recipe']['execution_requirements']['encode'],ffmpeg=args.ffmpeg)
            else:
                from fsf.remote.segments import run
                end=time.monotonic()+plan['budget_seconds']
                while True:
                    result=run(plan,state_root=args.state,driver=driver)
                    if not args.wait or result['status']!='running':break
                    if time.monotonic()>=end:raise RemoteError('REMOTE_GROUP_BUDGET_EXCEEDED')
                    time.sleep(.25 if driver.profile['transport']=='local' else 3)
                if result['status'] not in {'completed','paused'}:
                    print(json.dumps({'ok':False,'result':result}));return 3 if result['status'] in {'running','reconcile_required'} else 2
        elif args.operation=='preflight-v2':
            from fsf.remote.preflight_v2 import preflight
            from fsf.remote.artifacts import ArtifactStore
            result=preflight(read_json(args.request),profile=read_json(args.profile),store=ArtifactStore(read_json(args.store)),store_config=args.store,probe=args.probe)
            print(json.dumps({'ok':result['status']=='passed','result':result}));return {'passed':0,'failed':2,'unverified':3}[result['status']]
        elif args.operation=='kaggle-secrets-template':
            from fsf.remote.v2_transports import secret_setup_notebook
            if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
            write_json(args.out,secret_setup_notebook())
            result={'created':True,'submitted':False,'gpu_render':False,'model_inference':False}
        elif args.operation in {'artifact-probe','artifact-put','artifact-get','prepare-v2','dispatch-v2','worker-v2','status-v2','reconcile-v2','fetch-v2','cancel-v2'}:
            from fsf.remote.artifacts import ArtifactStore
            store=ArtifactStore(read_json(args.store))
            if args.operation=='artifact-probe':result=store.probe()
            elif args.operation=='artifact-put':
                result=store.put(args.file,artifact_id=args.artifact_id,key=args.key,role=args.role,media_type=args.media_type,state_root=args.state)
                if args.out:
                    if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
                    write_json(args.out,result)
            elif args.operation=='artifact-get':result=store.get(read_json(args.reference),output=args.out,state_root=args.state)
            elif args.operation=='prepare-v2':
                from fsf.remote.v2 import prepare_request
                request=prepare_request(read_json(args.definition),read_json(args.recipe),profile=read_json(args.profile),store=store,
                    inputs=read_json(args.inputs),wheel=read_json(args.wheel),segment=read_json(args.segment),output=args.out)
                result={'request_digest':request['request_digest'],'prepared':True,'submitted':False}
            elif args.operation=='worker-v2':
                from fsf.remote.v2 import run_worker as run2
                value=run2(read_json(args.request),store=store,output_root=args.out,approved_recipe=args.recipe_digest)
                result={k:value[k] for k in ('request_digest','status','result_digest')}
                if value['status']!='completed':raise RemoteError(value['error_code'] or 'REMOTE_WORKER_FAILED')
            else:
                from fsf.remote.v2_dispatch import submit as submit2,update as update2
                profile=read_json(args.profile)
                if args.operation=='dispatch-v2':
                    request=read_json(args.request)
                    result=submit2(request,profile=profile,store=store,state_root=args.state,store_config=args.store)
                    if args.wait:
                        end=time.monotonic()+request['policy']['deadline_seconds']
                        while time.monotonic()<end:
                            result=update2(request['request_digest'],profile=profile,store=store,state_root=args.state,store_config=args.store)
                            if result['provider_status']=='completed':
                                result=update2(request['request_digest'],profile=profile,store=store,state_root=args.state,store_config=args.store,action='fetch');break
                            if result['status'] in {'remote_failed','remote_cancelled','recovery_review_required'}:raise RemoteError('REMOTE_JOB_NOT_COMPLETED')
                            time.sleep(.25 if profile['transport']=='local' else 3)
                        else:raise RemoteError('REMOTE_DEADLINE_EXCEEDED')
                else:
                    result=update2(args.request_digest,profile=profile,store=store,state_root=args.state,store_config=args.store,
                        action='cancel' if args.operation=='cancel-v2' else 'fetch' if args.operation=='fetch-v2' else 'status',recover=getattr(args,'recover',False))
                    if result['status']=='cancel_unsupported':
                        print(json.dumps({'ok':False,'result':result}));return 3
        elif args.operation=="templates":
            from fsf.core.asset_library import _root
            root=_root(args.out)
            items=[p for p in resources.files("fsf.remote.templates").iterdir() if p.name.endswith((".json",".md"))]
            if any((root/p.name).exists() for p in items):raise RemoteError("REMOTE_TARGET_EXISTS")
            for p in items:(root/p.name).write_bytes(p.read_bytes())
            result={"files":sorted(p.name for p in items),"enabled":False}
        elif args.operation=='encoder-pin':
            from fsf.remote.segment_delivery import encoder_pin
            result=encoder_pin(args.ffmpeg)
            if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
            write_json(args.out,result)
        elif args.operation=='recipe-v2':
            from fsf.remote.v2 import validate_recipe as validate_v2
            from fsf.remote.contracts import seal
            result=validate_recipe(read_json(args.recipe))
            result.update(schema_version='fsf_remote_recipe_v2',execution_requirements=read_json(args.requirements))
            result=validate_v2(seal(result,'recipe_digest'))
            if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
            write_json(args.out,result)
            result={'recipe_digest':result['recipe_digest'],'approved':False}
        elif args.operation=="recipe-environment":
            from fsf.remote.environment import bind_environment
            result=bind_environment(read_json(args.recipe),read_json(args.plan))
            if Path(args.out).exists():raise RemoteError('REMOTE_TARGET_EXISTS')
            write_json(args.out,result)
            result={'recipe_digest':result['recipe_digest'],'approved':False}
        elif args.operation=="recipe-diff":
            from fsf.remote.environment import recipe_diff
            result=recipe_diff(read_json(args.before),read_json(args.after))
        elif args.operation=="validate":
            recipe=validate_recipe(read_json(args.recipe))
            result={"recipe_digest":recipe["recipe_digest"],"child_plan":compile_child_plan(recipe)}
        elif args.operation=="prepare":
            from fsf.remote.bundle import prepare_bundle
            result=prepare_bundle(read_json(args.definition),read_json(args.recipe),inputs_root=args.inputs,wheel=args.wheel,
                                  profile=read_json(args.profile),output_root=args.out)
        elif args.operation=="preflight":
            from fsf.remote.preflight import preflight as check
            result=check(read_json(args.definition),read_json(args.recipe),read_json(args.profile),
                         inputs_root=args.inputs,wheel=args.wheel,probe=args.probe)
            print(json.dumps({"ok":result["status"]=="passed","result":result},ensure_ascii=True))
            return {"passed":0,"failed":2,"unverified":3}[result["status"]]
        elif args.operation=="worker":
            from fsf.remote.worker import run_worker
            result=run_worker(args.bundle,output_root=args.out,approved_recipe=args.recipe_digest,remaining_seconds=args.remaining_seconds)
            if result["status"]!="completed":raise RemoteError(result.get("error_code") or "REMOTE_WORKER_FAILED")
        elif args.operation=="serve-reference":
            from fsf.remote.reference import make_server
            server=make_server(validate_profile(read_json(args.profile)),state_root=args.state,worker_root=args.worker_root,
                               python=args.python,port=args.port)
            server.serve_forever()
            return 0
        elif args.operation=="diagnose":
            profile=validate_profile(read_json(args.profile))
            result={"profile_id":profile["profile_id"],"transport":profile["transport"],"supported":True,
                    "configured":True,"authenticated":None,"reachable":None,
                    "submission_authorized":profile["authorized"],"verified_execution":False}
            if profile["transport"]=="http":result["configured"]=bool(os.environ.get(profile["settings"]["auth_env"]))
            if args.probe:
                from fsf.remote.transports import adapter_for
                adapter=adapter_for(profile)
                with tempfile.TemporaryDirectory() as temp:
                    if profile["transport"]=="kaggle":
                        adapter._call("diagnose",{"request_digest":"0"*64,"policy":profile["policy"]},Path(temp))
                    elif profile["transport"]=="http":adapter._call("GET","/capabilities")
                    elif profile["transport"]=="local":adapter.prepare(None,None,None)
                    else:adapter.prepare(None,None,None)
                result.update(authenticated=True,reachable=True)
        else:
            profile=validate_profile(read_json(args.profile))
            from fsf.remote.dispatch import submit_bundle,update_dispatch
            if args.operation=="dispatch":
                result=submit_bundle(args.bundle,profile=profile,state_root=args.state)
                if args.wait:result=wait_dispatch(result["dispatch_id"],profile=profile,state_root=args.state)
            else:result=update_dispatch(args.dispatch_id,profile=profile,state_root=args.state,action=args.operation)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        code="REMOTE_CANCELLED"
    except (RemoteError,EditStoreError) as exc:
        code=exc.code
    except Exception:
        code="REMOTE_INVALID"
    print(json.dumps({"ok":False,"error":{"code":code,"message":"Operation could not be completed"}}))
    return 130 if code=="REMOTE_CANCELLED" else 2
