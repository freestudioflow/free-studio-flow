"""CLI entry point for reference-video observations."""
import json
from pathlib import Path

from fsf.cli.edit import _Parser
from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, publish
from fsf.production.reference_analysis import analyze


def main(argv=None):
    parser=_Parser(prog="fsf reference")
    parser.add_argument("operation",choices=["analyze", "organize", "draft", "compare"])
    parser.add_argument("source")
    parser.add_argument("--out",required=True)
    parser.add_argument("--seconds",type=int,default=120)
    parser.add_argument("--audio-source",help="Explicit mono 16-bit WAV sidecar starting at zero")
    parser.add_argument("--classifier",help="Trusted local Python classifier, called with source path and SHA-256")
    parser.add_argument("--detector",help="Trusted local Python transition detector, called with source path and SHA-256")
    parser.add_argument("--contacts",help="New PNG contact sheet")
    parser.add_argument("--spec", help="Operator entity or draft specification")
    parser.add_argument("--entities", help="Entity revision for draft")
    parser.add_argument("--edit", help="Edit document for comparison")
    parser.add_argument("--reference-media", help="Original analysis source for a comparison sheet")
    parser.add_argument("--delivery", help="Verified post-production delivery directory")
    parser.add_argument("--expect", help="Current output digest for revision CAS")
    try:
        args=parser.parse_args(argv)
        if args.operation == "compare":
            from fsf.production.reference_compare import attach_sheet, compare

            if (not args.spec or not args.edit or
                    any((args.entities, args.expect, args.audio_source, args.classifier,
                         args.detector)) or len({bool(args.contacts), bool(args.reference_media),
                                                bool(args.delivery)}) != 1
                    or not 1 <= args.seconds <= 5400):
                raise ProductionError("REFERENCE_COMPARE_INVALID")
            target = Path(args.out).absolute()
            with edit_lock(target):
                if target.exists():
                    raise ProductionError("REFERENCE_DESTINATION_EXISTS")
                report = compare(read_json(args.source), read_json(args.edit), read_json(args.spec))
                if args.contacts:
                    sheet = Path(args.contacts).absolute()
                    if sheet.resolve() == target.resolve():
                        raise ProductionError("REFERENCE_COMPARE_INVALID")
                    report = attach_sheet(report, args.reference_media, args.delivery, sheet,
                                          seconds=args.seconds)
                    try:
                        publish(target, report)
                    except BaseException:
                        sheet.unlink(missing_ok=True)
                        raise
                else:
                    publish(target, report)
            print(json.dumps({"ok": True, "digest": report["comparison_digest"]}))
            return 0
        if any((args.edit, args.reference_media, args.delivery)):
            raise ProductionError("REFERENCE_INVALID")
        if args.operation != "analyze":
            from fsf.production.reference_draft import update

            if (not args.spec or (args.operation == "draft") != bool(args.entities)
                    or any((args.audio_source, args.classifier, args.detector, args.contacts))):
                raise ProductionError("REFERENCE_DRAFT_INVALID")
            target = Path(args.out).absolute()
            inputs = [Path(path).resolve() for path in (args.source, args.spec, args.entities) if path]
            if target.resolve() in inputs:
                raise ProductionError("REFERENCE_DRAFT_INVALID")
            result = update(args.operation, target, read_json(args.source), read_json(args.spec),
                            expected=args.expect, registry=read_json(args.entities) if args.entities else None)
            print(json.dumps({"ok": True, "digest": result}))
            return 0
        if any((args.spec, args.entities, args.expect)):
            raise ProductionError("REFERENCE_INVALID")
        if not 1<=args.seconds<=5400:
            raise ProductionError("REFERENCE_DEADLINE")
        target=Path(args.out).absolute()
        with edit_lock(target):
            if target.exists():
                raise ProductionError("REFERENCE_DESTINATION_EXISTS")
            result=analyze(args.source,target.parent,seconds=args.seconds,audio_source=args.audio_source,
                           classifier=args.classifier,detector=args.detector,contacts=args.contacts)
            publish(target,result)
        print(json.dumps({"ok":True,"analysis_digest":result["analysis_digest"]}))
        return 0
    except KeyboardInterrupt:
        code="REFERENCE_CANCELLED"
    except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError,IndexError,OverflowError,ZeroDivisionError) as exc:
        code=getattr(exc,"code","REFERENCE_INVALID")
    print(json.dumps({"ok":False,"error":{"code":code}}))
    return 130 if code=="REFERENCE_CANCELLED" else 2
