"""Versioned edit and verified preview commands with sanitized argument errors."""
from __future__ import annotations

import argparse
import json
import re

from fsf.core.edit_store import EditStoreError, create_edit_file, patch_edit_file, read_edit, read_json
from fsf.core.editing import (
    EditValidationError, _shape, create_edit, diff_edits, inspect_edit, locate_clips,
)

EXIT_CODES = {"EDIT_CANCELLED": 130, "EDIT_USAGE": 2, "EDIT_INVALID": 3, "EDIT_CONFLICT": 4,
              "EDIT_NOT_FOUND": 5, "EDIT_AMBIGUOUS": 5, "EDIT_IO": 6,
              "EDIT_LOCK_TIMEOUT": 7, "EDIT_TARGET_EXISTS": 8, "EDIT_INTERNAL": 1}


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        # argparse messages may contain unknown arguments or arbitrary values.
        raise EditStoreError("EDIT_USAGE")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise EditStoreError("EDIT_USAGE")
        setattr(namespace, self.dest, values)


def _integer(text):
    if not re.fullmatch(r"[0-9]+", text):
        raise EditStoreError("EDIT_USAGE")
    try:
        return int(text)
    except ValueError:
        raise EditStoreError("EDIT_USAGE") from None


def build_edit_parser():
    parser = _Parser(prog="fsf edit", description="Versioned edit metadata, post-production plans and verified previews")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("audio", help="Inspect, import, analyze and audition version-bound audio")
    create = commands.add_parser("create", help="Create revision one from an edit definition")
    create.add_argument("definition")
    create.add_argument("--out", required=True, action=_Once)
    source_import = commands.add_parser("import", help="Create an edit from explicitly selected, verified production outputs")
    source_import.add_argument("definition")
    source_import.add_argument("--source-executions", required=True, action=_Once)
    source_import.add_argument("--out", required=True, action=_Once)
    patch = commands.add_parser("patch", help="Compare and atomically publish a metadata patch")
    patch.add_argument("edit")
    patch.add_argument("--patch", required=True, action=_Once)
    impact=commands.add_parser('patch-impact',help='Read-only potential cue/anchor impact; never apply or confirm')
    impact.add_argument('edit')
    impact.add_argument('--patch',required=True,action=_Once)
    inspect = commands.add_parser("inspect", help="Show clip, track and cue identities")
    inspect.add_argument("edit")
    diff = commands.add_parser("diff", help="Compare two revisions of one edit")
    diff.add_argument("before")
    diff.add_argument("after")
    locate = commands.add_parser("locate", help="Locate clips against an explicit revision and digest")
    locate.add_argument("edit")
    locate.add_argument("--revision", required=True, type=_integer, action=_Once)
    locate.add_argument("--digest", required=True, action=_Once)
    group = locate.add_mutually_exclusive_group(required=True)
    group.add_argument("--clip-id", action=_Once)
    group.add_argument("--position", type=_integer, action=_Once)
    group.add_argument("--frame", type=_integer, action=_Once)
    group.add_argument("--label", type=str, action=_Once)
    plan = commands.add_parser("plan", help="Compile a generation-free post-production plan")
    plan.add_argument("edit")
    plan.add_argument("--out", required=True, action=_Once)
    preview = commands.add_parser("preview", help="Render a version-bound preview of one clip")
    preview.add_argument("edit")
    preview.add_argument("--out", required=True, action=_Once)
    preview.add_argument("--audio-resources",action=_Once)
    preview.add_argument("--source-executions", required=True, action=_Once)
    preview.add_argument("--revision", required=True, type=_integer, action=_Once)
    preview.add_argument("--digest", required=True, action=_Once)
    preview.add_argument("--clip-id", required=True, action=_Once)
    preview.add_argument("--start-frame", type=_integer, action=_Once)
    preview.add_argument("--end-frame", type=_integer, action=_Once)
    for cmd in (plan, preview):
        cmd.add_argument("--width", type=_integer, action=_Once)
        cmd.add_argument("--height", type=_integer, action=_Once)
        cmd.add_argument("--subtitles", choices=("none", "sidecar", "burned"), action=_Once)
        cmd.add_argument("--subtitle-track", action=_Once)
    plan.add_argument("--audio", choices=("keep", "strip", "require"), action=_Once)
    return parser


def _failure(code, candidates=None):
    body = {"ok": False, "error": {"code": code, "message": "Edit operation could not be completed"}}
    if candidates is not None:
        # Only structural identity/position fields, never arbitrary labels/prompts.
        body["candidates"] = [{k: row[k] for k in ("clip_id", "position", "timeline_start_frame", "timeline_end_frame")} for row in candidates]
    print(json.dumps(body, ensure_ascii=True))
    return EXIT_CODES[code]


def main(argv=None):
    if argv and argv[0] == "audio":
        from fsf.cli.audio import main as audio_main
        return audio_main(argv[1:])
    try:
        args = build_edit_parser().parse_args(argv)
        if args.operation=='patch-impact':
            from fsf.core.edit_diagnostics import patch_impact
            result=patch_impact(read_edit(args.edit),read_json(args.patch))
        elif args.operation == "locate":
            if (args.revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", args.digest)
                    or args.position is not None and args.position < 1
                    or args.label is not None and not args.label.strip()
                    or args.clip_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.clip_id)):
                raise EditStoreError("EDIT_USAGE")
            edit = read_edit(args.edit)
            if (args.revision, args.digest) != (edit["revision"], edit["edit_digest"]):
                raise EditStoreError("EDIT_CONFLICT")
            try:
                result = locate_clips(edit, revision=args.revision, digest=args.digest,
                                      clip_id=args.clip_id, position=args.position, frame=args.frame, label=args.label)
            except EditValidationError:
                return _failure("EDIT_NOT_FOUND", [])
            if len(result) > 1:
                return _failure("EDIT_AMBIGUOUS", result)
        elif args.operation in ("plan", "preview"):
            from fsf.runtime.post_plan import compile_post_plan
            from fsf.runtime.post_runtime import preview_edit, write_json
            edit = read_edit(args.edit)
            settings = {key: getattr(args, key) for key in ("width", "height", "subtitles", "subtitle_track") if getattr(args, key) is not None}
            if args.operation == "plan":
                if args.audio is not None:
                    settings["audio"] = args.audio
                result = compile_post_plan(edit, **settings)
                # Plans are immutable snapshots; refuse accidental overwrite.
                from pathlib import Path
                from fsf.core.edit_store import edit_lock
                with edit_lock(args.out) as destination:
                    if destination.exists():
                        raise EditStoreError("EDIT_TARGET_EXISTS")
                    write_json(destination, result)
            else:
                if args.revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", args.digest):
                    raise EditStoreError("EDIT_USAGE")
                if (args.revision, args.digest) != (edit["revision"], edit["edit_digest"]):
                    raise EditStoreError("EDIT_CONFLICT")
                result = preview_edit(edit, read_json(args.source_executions), args.out, revision=args.revision,
                    edit_digest=args.digest, clip_id=args.clip_id, start_frame=args.start_frame or 0, end_frame=args.end_frame, audio_resources=read_json(args.audio_resources) if args.audio_resources else None, **settings)
        elif args.operation == "import":
            from fsf.runtime.post_sources import import_edit_selection
            definition = read_json(args.definition)
            result = create_edit_file(args.out, import_edit_selection(definition, read_json(args.source_executions)))
        elif args.operation == "create":
            definition = read_json(args.definition)
            _shape(definition, "fsf_edit_definition_v1")
            inputs = {k: v for k, v in definition.items() if k != "schema_version"}
            result = create_edit_file(args.out, create_edit(**inputs))
        elif args.operation == "patch":
            result = patch_edit_file(args.edit, read_json(args.patch))
        elif args.operation == "inspect":
            result = inspect_edit(read_edit(args.edit))
        else:
            result = diff_edits(read_edit(args.before), read_edit(args.after))
        # ASCII JSON escapes preserve all Unicode values even on legacy pipes.
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        return _failure("EDIT_CANCELLED")
    except __import__("fsf.core.audio_edit", fromlist=["AudioError"]).AudioError as exc:
        from fsf.cli.audio import CODES
        print(json.dumps({"ok":False,"error":{"code":exc.code,"message":"Audio operation could not be completed"}}))
        return CODES[exc.code]
    except EditStoreError as exc:
        return _failure(exc.code)
    except EditValidationError:
        return _failure("EDIT_INVALID")
    except ValueError:
        return _failure("EDIT_INVALID")
    except Exception:
        return _failure("EDIT_INTERNAL")
