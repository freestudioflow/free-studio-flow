"""Small real jobs used by approved recipes; no transport-specific scheduling."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from fsf.core.edit_store import read_json
from fsf.remote.contracts import digest, sha, write_json, safe_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="job", required=True)
    cpu = commands.add_parser("cpu")
    cpu.add_argument("--value", required=True, type=int)
    cpu.add_argument("--out", required=True)
    copy = commands.add_parser("condition-copy")
    copy.add_argument("--input", required=True)
    copy.add_argument("--request", required=True)
    copy.add_argument("--out", required=True)
    copy.add_argument("--ack", required=True)
    blender = commands.add_parser("blender")
    blender.add_argument("--input", required=True)
    blender.add_argument("--out", required=True)
    blender.add_argument("--trusted-scene", action="store_true")
    args = parser.parse_args(argv)
    if args.job == "cpu":
        write_json(args.out, {"operation": "integer_square", "input": args.value, "square": args.value*args.value})
    elif args.job == "condition-copy":
        from fsf.runtime.blender import validate_condition_capability
        request = read_json(args.request)
        condition = request["video_condition"]
        validate_condition_capability(condition, request["recipe"]["condition_capability"])
        if sha(args.input) != condition["artifact"]["sha256"]:
            raise ValueError("Control input changed")
        shutil.copyfile(args.input,args.out)
        write_json(args.ack, {"condition_acknowledgement": {"condition_digest": condition["condition_digest"],
            "artifact_sha256": condition["artifact"]["sha256"], "frame_mapping_digest": condition["frame_mapping_digest"],
            "preprocess": "none", "capability_gap": None}})
    else:
        from fsf.remote.bundle import unpack, pack
        from fsf.runtime.blender import compile_blender_plan, execute_blender_plan, verify_bundle
        root = Path(tempfile.mkdtemp(prefix="blender-job-"))
        try:
            source = root/"source";source.mkdir()
            unpack(args.input,source)
            request = read_json(source/"request.json")
            output = root/"renders";output.mkdir()
            import shutil as tools
            executable = tools.which("blender")
            result = execute_blender_plan(compile_blender_plan(request),source_root=source,output_root=output,
                                        executable=executable,trusted_scene=args.trusted_scene)
            bundle = output/result["bundle"]
            manifest = verify_bundle(bundle)
            pack(bundle,["manifest.json","receipt.json",*(a["path"] for a in manifest["artifacts"])],args.out)
        finally:
            shutil.rmtree(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
