"""Audio commands with bounded, stable JSON diagnostics."""
import json
from fsf.cli.edit import _Parser, _Once
from fsf.core.audio_edit import AudioError
from fsf.core.edit_store import EditStoreError, read_edit, read_json, edit_lock

CODES = {"AUDIO_CANCELLED":130,"AUDIO_USAGE":2,"AUDIO_INVALID":3,"AUDIO_ID_RETIRED":3,"AUDIO_STALE":4,"AUDIO_AB_MISMATCH":4,
    "AUDIO_SOURCE_INVALID":6,"AUDIO_LOCK_TIMEOUT":7,"AUDIO_TOO_SHORT":9,"AUDIO_SILENT":9,"AUDIO_TARGET_UNREACHABLE":9,
    "AUDIO_ANALYSIS_FAILED":10,"AUDIO_FILTER_UNAVAILABLE":10,"AUDIO_DEADLINE_EXCEEDED":10}


def main(argv):
    try:
        parser = _Parser(prog="fsf edit audio")
        commands = parser.add_subparsers(dest="operation",required=True)
        inspect = commands.add_parser("inspect");inspect.add_argument("edit")
        importing = commands.add_parser("import");importing.add_argument("edit")
        importing.add_argument("--definition",required=True,action=_Once)
        importing.add_argument("--audio-resources",required=True,action=_Once)
        importing.add_argument("--out",required=True,action=_Once)
        analyze = commands.add_parser("analyze");analyze.add_argument("plan")
        audition = commands.add_parser("audition");audition.add_argument("before");audition.add_argument("after")
        audition.add_argument("--request",required=True,action=_Once)
        for command in (analyze,audition):
            command.add_argument("--source-executions",required=True,action=_Once)
            command.add_argument("--audio-resources",action=_Once)
            command.add_argument("--out",required=True,action=_Once)
        args = parser.parse_args(argv)
        from fsf.runtime.audio_workflow import import_audio, inspect_audio, audition as listen
        from fsf.runtime.post_runtime import write_json
        if args.operation == "inspect":
            result = inspect_audio(read_edit(args.edit))
        elif args.operation == "import":
            result = import_audio(read_edit(args.edit),read_json(args.definition),read_json(args.audio_resources))
            with edit_lock(args.out) as destination:
                if destination.exists():
                    raise AudioError()
                write_json(destination,result)
        elif args.operation == "audition":
            result = listen(read_edit(args.before),read_edit(args.after),read_json(args.request),read_json(args.source_executions),
                read_json(args.audio_resources) if args.audio_resources else None,args.out)
        else:
            from fsf.runtime.audio_analysis import analyze_plan
            result = analyze_plan(read_json(args.plan),read_json(args.source_executions),
                read_json(args.audio_resources) if args.audio_resources else None,args.out)
        print(json.dumps({"ok":True,"result":result},ensure_ascii=True,allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok":False,"error":{"code":"AUDIO_CANCELLED","message":"Audio operation interrupted"}}))
        return 130
    except Exception as exc:
        code = exc.code if isinstance(exc,AudioError) else "AUDIO_USAGE" if isinstance(exc,EditStoreError) and exc.code == "EDIT_USAGE" else "AUDIO_LOCK_TIMEOUT" if isinstance(exc,EditStoreError) and exc.code == "EDIT_LOCK_TIMEOUT" else "AUDIO_INVALID"
        print(json.dumps({"ok":False,"error":{"code":code,"message":"Audio operation could not be completed"}}))
        return CODES[code]
