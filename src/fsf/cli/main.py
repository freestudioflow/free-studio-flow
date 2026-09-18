"""Command-line interface for Free Studio Flow (FSF-M1 ~ M4 + FSF-S)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fsf import __version__
from fsf.conformance.media import (
    DEFAULT_BROLL_EXPECTED,
    evaluate_media_file,
)
from fsf.executors.registry import DEFAULT_REGISTRY, get_executor
from fsf.intake.compiler import compile_job
from fsf.runtime.external_op import (
    build_resumed_job,
    compute_file_sha256,
    save_checkpoint,
)
from fsf.runtime.plan import build_execution_plan
from fsf.studio.catalog import (
    TRANSPORTS,
    executor_id_for,
    list_studio_flows,
    resolve_skill_name,
)
from fsf.studio.runner import run_studio_flow


def _refuse_live_or_unacked_shape(
    executor,
    *,
    shape: bool,
    live: bool,
    require_shape: bool,
) -> int | None:
    if live:
        print(
            "[FSF] refuse: live inference is not wired in execute_shot. "
            "Pass --shape for the contract-shape path.",
            file=sys.stderr,
        )
        return 2
    if require_shape and not shape:
        print(
            f"[FSF] refuse: {executor.executor_id} is not live media. "
            "flow run always requires --shape. Do not treat .shape.bin or .mock.bin as video.",
            file=sys.stderr,
        )
        return 2
    if not require_shape and executor.execution_kind != "mock" and not shape:
        print(
            f"[FSF] refuse: {executor.executor_id} writes contract-shape bytes, not media. "
            "Pass --shape. Do not treat .shape.bin as a video. --live is not implemented.",
            file=sys.stderr,
        )
        return 2
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fsf",
        description="Free Studio Flow (FSF) — AI-native film factory and studio pipelines",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"fsf {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # intake (M1)
    intake_cmd = subparsers.add_parser(
        "intake",
        help="Compile a DOCX prompt sheet into FSF job manifest and PDX tool request via Kernel intake",
    )
    intake_cmd.add_argument("docx", help="Path to Word DOCX prompt sheet")
    intake_cmd.add_argument("--out", "-o", default=None, help="Output directory for job manifest and artifacts")
    intake_cmd.add_argument("--tool", default="broll.library.ltx23.t2v", help="PDX tool identifier")

    # plan (M2)
    plan_cmd = subparsers.add_parser(
        "plan",
        help="Compile a DOCX prompt sheet into a pdx_execution_plan_v1",
    )
    plan_cmd.add_argument("docx", help="Path to Word DOCX prompt sheet")
    plan_cmd.add_argument("--tool", default="studio.video.generate", help="Executor tool identifier")
    plan_cmd.add_argument(
        "--pipeline",
        default="generate",
        choices=["generate", "p0", "p1", "p2", "p3", "custom"],
        help="generate = one generate+QC per shot; p0–p3 accumulate Studio skills; custom requires --skills",
    )
    plan_cmd.add_argument(
        "--skills",
        default=None,
        help="Comma-separated skills (canonical or aliases). Overrides --pipeline when set.",
    )
    plan_cmd.add_argument("--out", "-o", default=None, help="Output file for execution plan JSON")

    # run (M2 & M4)
    run_cmd = subparsers.add_parser(
        "run",
        help="Run or resume video generation only (not a full plan runner)",
    )
    run_cmd.add_argument("docx", help="Path to Word DOCX prompt sheet")
    run_cmd.add_argument("--out", "-o", default="output/run", help="Output directory for artifacts")
    run_cmd.add_argument(
        "--executor",
        default="mock.studio.video",
        help="Executor ID. Default mock.studio.video is MOCK (not media). "
        "kaggle/api/local execute_shot is shape-only; pass --shape.",
    )
    run_cmd.add_argument("--no-resume", action="store_true", help="Force re-generation of existing artifacts")
    run_cmd.add_argument(
        "--shape",
        action="store_true",
        help="Acknowledge contract-shape bytes (required for non-mock executors)",
    )
    run_cmd.add_argument(
        "--live",
        action="store_true",
        help="Request live inference (currently refused: not wired in execute_shot)",
    )

    # verify (M3)
    verify_cmd = subparsers.add_parser(
        "verify",
        help="Verify media file technical conformance against expected contract (PDX-P1)",
    )
    verify_cmd.add_argument("media_file", help="Path to video or audio file")
    verify_cmd.add_argument("--expected", default=None, help="Path to expected JSON contract file or JSON string")

    # executors (M4)
    subparsers.add_parser(
        "executors",
        help="List available video generation executors",
    )

    # FSF-S studio flows (CLI / skill; no desk)
    subparsers.add_parser(
        "flows",
        help="List fourteen Studio flows and kaggle/api/local transport status",
    )
    flow_cmd = subparsers.add_parser(
        "flow",
        help="Run one Studio skill (CLI/skill first; no UI)",
    )
    flow_sub = flow_cmd.add_subparsers(dest="flow_command")
    flow_run = flow_sub.add_parser("run", help="Run a Studio skill against a DOCX job")
    flow_run.add_argument("skill", help="Studio skill or flow_id (e.g. studio.video.remaster)")
    flow_run.add_argument("docx", help="Path to Word DOCX prompt sheet")
    flow_run.add_argument("--out", "-o", default="output/flow", help="Output directory")
    flow_run.add_argument(
        "--transport",
        required=True,
        choices=list(TRANSPORTS),
        help="Required: kaggle, api, or local. Always pass --shape. mock executors are refused.",
    )
    flow_run.add_argument("--executor", default=None, help="Override executor id")
    flow_run.add_argument(
        "--shape",
        action="store_true",
        help="Acknowledge contract-shape bytes (required; live inference is not wired)",
    )
    flow_run.add_argument(
        "--live",
        action="store_true",
        help="Request live inference (currently refused: not wired in execute_shot)",
    )

    # doctor / check
    subparsers.add_parser(
        "check",
        help="Check environment dependencies (prodocux, pdx-artifact-engine, pdx-adapter-media)",
    )
    live_cmd = subparsers.add_parser(
        "live-check",
        help="Live-verify Kaggle work, public APIs, and local transport shape; skip live local-model ping",
    )
    live_cmd.add_argument("--out", "-o", default="output/live-check", help="Scratch directory")
    live_cmd.add_argument(
        "--docx",
        default=None,
        help="Optional DOCX prompt sheet (synthetic sheet used if omitted)",
    )

    return parser


def handle_check() -> int:
    import importlib.metadata as m

    packages = ["prodocux", "pdx-artifact-engine", "pdx-adapter-media", "python-docx", "kaggle"]
    print("=== Free Studio Flow Dependency Status ===")
    all_ok = True
    for pkg in packages:
        try:
            ver = m.version(pkg)
            print(f"  [OK] {pkg}: {ver}")
        except m.PackageNotFoundError:
            print(f"  [MISSING] {pkg}")
            all_ok = False
    return 0 if all_ok else 1


def _synthetic_prompt_sheet(out_dir: Path) -> Path:
    from docx import Document

    path = Path(out_dir) / "live_prompt_sheet.docx"
    doc = Document()
    doc.add_paragraph(
        "Locked-off wide shot of an overcast ocean. Grey water, small waves, "
        "a flat horizon, no land, no ships. Contemporary digital capture. 5 seconds"
    )
    doc.save(path)
    return path


def handle_live_check(args: argparse.Namespace) -> int:
    from fsf.live.runner import run_live_check

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    docx = Path(args.docx).resolve() if args.docx else _synthetic_prompt_sheet(out_dir)
    report = run_live_check(docx_path=docx, out_dir=out_dir, kaggle=True, api=True, skip_local=True)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("ok"):
        print(
            "[FSF live-check] Kaggle work + APIs OK; local transport shape OK; "
            "live local model skipped"
        )
        return 0
    print("[FSF live-check] FAILED", file=sys.stderr)
    return 1


def handle_flows() -> int:
    print("=== FSF-S Studio Flows (CLI / skill; no desk) ===")
    for flow in list_studio_flows():
        print(f"  [{flow['status']:<11}] {flow['phase']} {flow['skill']}  {flow['title']}")
        bits = []
        for name in TRANSPORTS:
            row = flow["transports"][name]
            bits.append(f"{name}={row['status']}")
        print(f"               transports: {', '.join(bits)}")
    return 0


def handle_flow_run(args: argparse.Namespace) -> int:
    docx_path = Path(args.docx)
    out_dir = Path(args.out).resolve()
    try:
        skill = resolve_skill_name(args.skill)
        resolved = args.executor or executor_id_for(skill, args.transport)
        executor = get_executor(resolved)
        blocked = _refuse_live_or_unacked_shape(
            executor, shape=args.shape, live=args.live, require_shape=True
        )
        if blocked is not None:
            return blocked
        if executor.execution_kind == "mock" or str(resolved).startswith("mock."):
            print(
                "[FSF] refuse: mock.studio.video is not a Studio transport. "
                "Use `fsf run` for mock; flow run is kaggle/api/local shape only.",
                file=sys.stderr,
            )
            return 2
        manifest, _, _ = compile_job(docx_path, out_dir=out_dir)
        results = run_studio_flow(
            args.skill,
            manifest["tasks"],
            out_dir,
            transport=args.transport,
            executor_id=args.executor,
        )
        print(
            f"[FSF Flow] SHAPE only (not media) {args.skill} transport={args.transport} "
            f"executor={resolved} tasks={len(results)} out='{out_dir}'"
        )
        return 0
    except NotImplementedError as exc:
        print(f"[FSF Flow] not implemented: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[FSF Flow Error] {exc}", file=sys.stderr)
        return 1


def handle_run(args: argparse.Namespace) -> int:
    docx_path = Path(args.docx)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest, request, profile = compile_job(docx_path, out_dir=out_dir)
    executor = get_executor(args.executor)
    blocked = _refuse_live_or_unacked_shape(
        executor, shape=args.shape, live=args.live, require_shape=False
    )
    if blocked is not None:
        return blocked

    kind = executor.execution_kind
    print(f"[FSF Run] Job: {manifest['source_document']} | Tasks: {manifest['task_count']}")
    print(f"[FSF Run] Executor: {executor.display_name} ({executor.executor_id}) kind={kind}")
    if kind == "mock":
        print("[FSF Run] MOCK artifacts are not media; filenames use .mock.bin")
    elif kind == "shape":
        print("[FSF Run] SHAPE artifacts are not media; filenames use .shape.bin")

    if not args.no_resume:
        resumed_manifest, pending_tasks, completed_tasks = build_resumed_job(manifest, out_dir)
        print(f"[FSF Resume] Pending: {len(pending_tasks)} | Reused: {len(completed_tasks)}")
        active_tasks = pending_tasks
    else:
        active_tasks = manifest["tasks"]

    completed_artifacts: dict[str, Any] = {}

    for task in active_tasks:
        shot_id = task["id"]
        print(f"  -> Generating {shot_id}...")
        res = executor.execute_shot(task, out_dir)
        outputs = res.get("outputs", {})
        completed_artifacts[shot_id] = {
            "file": outputs.get("artifact_file"),
            "sha256": outputs.get("artifact_sha256"),
            "size_bytes": outputs.get("size_bytes"),
            "status": "completed",
        }

    # Save checkpoint
    save_checkpoint(out_dir, manifest, completed_artifacts)
    print(f"[FSF Run] Finished. Checkpoint saved to '{out_dir / 'fsf_checkpoint.json'}'")
    return 0


def handle_verify(args: argparse.Namespace) -> int:
    media_path = Path(args.media_file)
    expected = DEFAULT_BROLL_EXPECTED
    if args.expected:
        exp_path = Path(args.expected)
        if exp_path.is_file():
            expected = json.loads(exp_path.read_text(encoding="utf-8"))
        else:
            expected = json.loads(args.expected)

    report, host_action = evaluate_media_file(media_path, expected)
    print("=== PDX Media Conformance Report ===")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("\n=== Host Action Mapping ===")
    print(json.dumps(host_action, indent=2, ensure_ascii=False))
    return 0 if report.get("evaluation_status") == "conforms" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "intake":
        docx_path = Path(args.docx)
        try:
            manifest, request, profile = compile_job(
                docx_path, out_dir=args.out, tool_name=args.tool
            )
            print(
                f"[FSF Intake] Compiled {manifest['task_count']} tasks from '{docx_path.name}' "
                f"(SHA: {manifest['source_sha256'][:12]}...)"
            )
            if args.out:
                print(f"[FSF Intake] Artifacts written to '{Path(args.out).resolve()}'")
            else:
                print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"[FSF Intake Error] {exc}", file=sys.stderr)
            return 1
    elif args.command == "plan":
        docx_path = Path(args.docx)
        try:
            manifest, _, _ = compile_job(docx_path)
            plan = build_execution_plan(
                manifest,
                executor_tool=args.tool,
                pipeline=args.pipeline,
                skills=args.skills,
            )
            if args.out:
                Path(args.out).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
                print(f"[FSF Plan] Execution plan written to '{args.out}'")
            else:
                print(json.dumps(plan, indent=2))
            return 0
        except Exception as exc:
            print(f"[FSF Plan Error] {exc}", file=sys.stderr)
            return 1
    elif args.command == "run":
        return handle_run(args)
    elif args.command == "verify":
        return handle_verify(args)
    elif args.command == "executors":
        print("=== Registered Video Executors ===")
        for exc in DEFAULT_REGISTRY.list_executors():
            avail = "[Available]" if exc["is_available"] else "[Unavailable]"
            kind = exc.get("execution_kind", "shape")
            print(
                f"  {avail} [{kind}] {exc['executor_id']} ({exc['display_name']}) -> {exc['tool_name']}"
            )
        return 0
    elif args.command == "flows":
        return handle_flows()
    elif args.command == "flow":
        if getattr(args, "flow_command", None) == "run":
            return handle_flow_run(args)
        parser.parse_args(["flow", "--help"])
        return 0
    elif args.command == "check":
        return handle_check()
    elif args.command == "live-check":
        return handle_live_check(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
