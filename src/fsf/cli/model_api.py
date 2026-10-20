"""Typed model API commands; generation and service diagnostics are separate."""
import json
import os
from pathlib import Path

from fsf.cli.edit import _Parser,_Once
from fsf.core.edit_store import read_json,EditStoreError
from fsf.remote.contracts import RemoteError
from fsf.remote.model_api import run_api,validate_profile,verify_receipt


def main(argv=None):
    parser=_Parser(prog="fsf api",description="Typed text/image/video/audio/upscale APIs with explicit provider profiles")
    commands=parser.add_subparsers(dest="operation",required=True)
    setup=commands.add_parser("setup",help="Write unauthorized provider and host credential templates without connecting")
    setup.add_argument("--provider",required=True,choices=["nebius","google","bedrock","custom"])
    setup.add_argument("--capability",default="text",choices=["text","image","video","audio","upscale"])
    setup.add_argument("--model",required=True)
    setup.add_argument("--region")
    setup.add_argument("--source",default="environment",choices=["environment","aws_default_chain","host_secret_manager","os_credential_store"])
    setup.add_argument("--reference")
    setup.add_argument("--resolver-config",help="Operator-owned JSON argv array for a trusted secret resolver")
    setup.add_argument("--out",required=True)
    check=commands.add_parser("check",help="Validate profile and optionally probe a read-only API endpoint")
    check.add_argument("--profile",required=True,action=_Once)
    check.add_argument("--probe",action="store_true")
    check.add_argument("--binding")
    check.add_argument("--state")
    check.add_argument("--recheck",action="store_true")
    for name in ("run","status","fetch","reconcile","cancel"):
        child=commands.add_parser(name)
        child.add_argument("request")
        for flag in ("profile","inputs","state"):child.add_argument("--"+flag,required=True,action=_Once)
        child.add_argument("--no-wait",action="store_true")
        child.add_argument("--binding")
        child.add_argument("--recheck",action="store_true")
    verify=commands.add_parser("verify",help="Verify a collected model artifact and receipt")
    verify.add_argument("bundle")
    try:
        args=parser.parse_args(argv)
        if args.operation=="setup":
            from fsf.remote.onboarding import setup as make_setup
            from fsf.remote.contracts import write_json
            profile,binding,result=make_setup(args.provider,args.capability,args.model,source=args.source,
                reference=args.reference,region=args.region,resolver=read_json(args.resolver_config) if args.resolver_config else None)
            target=Path(args.out)
            # A setup never overwrites an existing operator configuration.
            target.mkdir(exist_ok=False,parents=True)
            write_json(target/"profile.json",profile)
            write_json(target/"binding.json",binding)
            write_json(target/"guidance.json",result)
        elif args.operation=="check":
            profile=validate_profile(read_json(args.profile))
            binding=read_json(args.binding) if args.binding else None
            if binding:
                from fsf.remote.credentials import validate_binding
                validate_binding(binding,profile)
            result={"profile_id":profile["profile_id"],"capability":profile["capability"],"protocol":profile["protocol"],
                "configured":None if binding else bool(os.environ.get(profile["key_env"])) if profile["key_env"] else None,
                "authorized":profile["authorized"],"authenticated":None,"reachable":None,"verified_inference":False}
            if args.probe:
                from fsf.remote.model_api import _http
                import time
                protocol=profile["protocol"]
                route="/models" if protocol=="openai_chat_v1" else "/models/"+profile["model"] if protocol=="gemini_content_v1" else None
                if protocol=="json_http_v1":route=profile["http"]["routes"].get("check")
                if route is None:raise RemoteError("API_READ_ONLY_PROBE_UNSUPPORTED")
                from fsf.remote.credentials import credential_scope
                from fsf.remote.onboarding import connection_attempt
                from fsf.core.asset_library import _root
                state_root=Path(args.state) if args.state else Path(args.profile).resolve().parent/".fsf-api-state"
                state_root.mkdir(exist_ok=True)
                with connection_attempt(profile,binding,_root(state_root),recheck=args.recheck),credential_scope(profile,binding):
                    _http(profile,"GET",route,deadline=time.monotonic()+profile["timeout_seconds"])
                result.update(authenticated=True,reachable=True)
        elif args.operation=="verify":
            receipt=verify_receipt(args.bundle)
            result={"capability":receipt["capability"],"receipt_digest":receipt["receipt_digest"],"quality":receipt["quality"],"review":receipt["review"]}
        else:
            result=run_api(read_json(args.request),read_json(args.profile),inputs_root=args.inputs,state_root=args.state,
                           action=args.operation,wait=not args.no_wait,
                           binding=read_json(args.binding) if args.binding else None,recheck=args.recheck)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=True))
        return 0
    except (RemoteError,EditStoreError) as exc:code=exc.code
    except KeyboardInterrupt:code="API_CANCELLED"
    except Exception:code="API_INVALID"
    print(json.dumps({"ok":False,"error":{"code":code,"message":"Operation could not be completed"}}))
    return 130 if code=="API_CANCELLED" else 2
