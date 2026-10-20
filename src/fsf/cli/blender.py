"""Blender and immutable asset commands with fixed, non-reflective diagnostics."""
import json
from pathlib import Path

from fsf.cli.edit import _Parser, _Once
from fsf.core.edit_store import read_json, EditStoreError
from fsf.core.asset_library import AssetValidationError
from fsf.runtime.blender import BlenderError


def main(argv=None, *, assets=False):
    parser = _Parser(prog="fsf asset" if assets else "fsf blender")
    commands = parser.add_subparsers(dest="operation", required=True)
    if assets:
        reg = commands.add_parser("register", help="Register a verified immutable asset bundle")
        reg.add_argument("record")
        reg.add_argument("--resources", required=True, action=_Once)
        reg.add_argument("--library", required=True, action=_Once)
        get = commands.add_parser("get", help="Verify and read one fixed asset version")
        get.add_argument("asset_id")
        get.add_argument("version")
        get.add_argument("--library", required=True, action=_Once)
    else:
        imported = commands.add_parser("import-control", help="Import an operator-created control MP4 without conversion")
        imported.add_argument("source")
        imported.add_argument("--out", required=True, action=_Once)
        imported.add_argument("--control-id", required=True, action=_Once)
        check = commands.add_parser("check", help="Probe a Blender executable; not a render smoke")
        check.add_argument("--executable", required=True, action=_Once)
        plan = commands.add_parser("plan", help="Compile a fixed Blender transform plan")
        plan.add_argument("request")
        plan.add_argument("--out", required=True, action=_Once)
        run = commands.add_parser("execute", help="Execute a trusted-scene Blender plan")
        run.add_argument("plan")
        run.add_argument("--source-root", required=True, action=_Once)
        run.add_argument("--out", required=True, action=_Once)
        run.add_argument("--executable", required=True, action=_Once)
        run.add_argument("--trusted-scene", action="store_true")
        run.add_argument("--no-resume", action="store_true")
        verify = commands.add_parser("verify", help="Verify an existing bundle and its files")
        verify.add_argument("bundle")
        condition = commands.add_parser("condition", help="Build a portable video condition")
        condition.add_argument("bundle")
        condition.add_argument("--view-id", required=True, action=_Once)
        condition.add_argument("--out", required=True, action=_Once)
        record = commands.add_parser("record", help="Create an asset record from a verified render bundle")
        record.add_argument("bundle")
        record.add_argument("--asset-id", required=True, action=_Once)
        record.add_argument("--asset-version", required=True, action=_Once)
        record.add_argument("--prompt", required=True, action=_Once)
        record.add_argument("--out", required=True, action=_Once)
    try:
        args = parser.parse_args(argv)
        from fsf.runtime.blender import probe_blender, compile_blender_plan, execute_blender_plan, verify_bundle, condition_request
        from fsf.core.asset_store import register_asset, lookup_asset
        if assets:
            result = register_asset(args.library, read_json(args.record), resource_root=args.resources) if args.operation == "register" else lookup_asset(args.library, args.asset_id, args.version)
        elif args.operation == "import-control":
            from fsf.runtime.control_import import import_control_video
            result = import_control_video(args.source, output_root=args.out, control_id=args.control_id)
        elif args.operation == "check":
            result = probe_blender(args.executable)
        elif args.operation == "plan":
            result = compile_blender_plan(read_json(args.request))
            _write(args.out, result)
        elif args.operation == "execute":
            result = execute_blender_plan(read_json(args.plan), source_root=args.source_root, output_root=args.out,
                executable=args.executable, trusted_scene=args.trusted_scene, no_resume=args.no_resume)
        elif args.operation == "verify":
            result = verify_bundle(args.bundle)
        elif args.operation == "record":
            from fsf.runtime.blender import bundle_asset_record
            result = bundle_asset_record(args.bundle, asset_id=args.asset_id, version=args.asset_version, source_prompt=args.prompt)
            _write(args.out, result)
        else:
            result = condition_request(args.bundle, args.view_id)
            _write(args.out, result)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
        return 0
    except (BlenderError, AssetValidationError, EditStoreError) as exc:
        code = exc.code
    except KeyboardInterrupt:
        code = "BLENDER_CANCELLED"
    except Exception:
        code = "BLENDER_INVALID"
    print(json.dumps({"ok": False, "error": {"code": code, "message": "Operation could not be completed"}}))
    return 130 if code == "BLENDER_CANCELLED" else 2


def _write(path, value):
    from fsf.core.edit_store import edit_lock, _target
    import os
    import tempfile
    target = _target(path)
    with edit_lock(target):
        if target.exists():
            raise BlenderError("BLENDER_TARGET_EXISTS")
        fd, temp = tempfile.mkstemp(prefix=".blender-json-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
