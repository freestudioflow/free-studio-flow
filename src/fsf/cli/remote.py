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
    prepare=commands.add_parser("prepare",help="Build an immutable parent/child request bundle")
    prepare.add_argument("definition")
    for name in ("recipe","inputs","wheel","profile","out"):
        prepare.add_argument("--"+name,required=True,action=_Once)
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
        if args.operation=="templates":
            from fsf.core.asset_library import _root
            root=_root(args.out)
            items=[p for p in resources.files("fsf.remote.templates").iterdir() if p.name.endswith((".json",".md"))]
            if any((root/p.name).exists() for p in items):raise RemoteError("REMOTE_TARGET_EXISTS")
            for p in items:(root/p.name).write_bytes(p.read_bytes())
            result={"files":sorted(p.name for p in items),"enabled":False}
        elif args.operation=="validate":
            recipe=validate_recipe(read_json(args.recipe))
            result={"recipe_digest":recipe["recipe_digest"],"child_plan":compile_child_plan(recipe)}
        elif args.operation=="prepare":
            from fsf.remote.bundle import prepare_bundle
            result=prepare_bundle(read_json(args.definition),read_json(args.recipe),inputs_root=args.inputs,wheel=args.wheel,
                                  profile=read_json(args.profile),output_root=args.out)
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
