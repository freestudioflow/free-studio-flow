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
from fsf.intake.parser import is_shot_unit
from fsf.runtime.external_op import (
    build_resumed_job,
    checkpoint_record_from_tool_result,
    save_checkpoint,
    scrub_secrets,
)
from fsf.runtime.plan import build_execution_plan
from fsf.studio.catalog import (
    TRANSPORTS,
    executor_id_for,
    list_studio_flows,
    resolve_skill_name,
)
from fsf.studio.runner import run_studio_flow, select_tasks_for_skill


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
    subparsers.add_parser("production", help="Long control sources and durable production preparation")
    subparsers.add_parser("edit", help="Versioned edits and post-production from verified sources")
    subparsers.add_parser("remote", help="Approved parent/worker dispatch and lifecycle operations")
    subparsers.add_parser("api", help="Typed model API profiles and verified outputs")
    subparsers.add_parser("blender", help="Controlled Blender and external control-video workflows")
    subparsers.add_parser("asset", help="Immutable asset registration and lookup")

    # intake (M1)
    intake_cmd = subparsers.add_parser(
        "intake",
        help="Compile a DOCX prompt sheet into FSF job manifest and PDX tool request via Kernel intake",
    )
    intake_cmd.add_argument("docx", help="Path to Word DOCX prompt sheet")
    intake_cmd.add_argument("--out", "-o", default=None, help="Output directory for job manifest and artifacts")
    intake_cmd.add_argument("--tool", default="broll.library.ltx23.t2v", help="PDX tool identifier")
    intake_cmd.add_argument('--media-contracts',help='Operator JSON mapping of shot IDs to explicit media contracts')

    # plan (M2)
    plan_cmd = subparsers.add_parser(
        "plan",
        help="Compile a DOCX prompt sheet into a pdx_execution_plan_v1",
    )
    plan_cmd.add_argument("docx", help="Path to Word DOCX prompt sheet")
    plan_cmd.add_argument('--media-contracts',help='Same operator media-contract mapping used by intake')
    plan_cmd.add_argument("--post-sources", action="store_true", help="Add media QC for every video producer, including remaster and VFX")
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
    plan_cmd.add_argument(
        "--assembly",
        action="store_true",
        default=False,
        help="Include Fast Assembly transform and QC steps in the execution plan",
    )
    plan_cmd.add_argument(
        "--subtitles",
        choices=["none", "sidecar", "burned"],
        default="none",
        help="Subtitle policy: none, sidecar (.srt), or burned",
    )
    plan_cmd.add_argument(
        "--audio",
        choices=["keep", "strip", "require"],
        default="keep",
        help="Audio policy for assembly: 'keep' (default, preserve audio), 'strip', or 'require'",
    )
    plan_cmd.add_argument(
        "--bgm",
        default=None,
        help="Optional BGM audio artifact reference or file",
    )
    plan_cmd.add_argument(
        "--media",
        action="store_true",
        default=False,
        help="Compile execution plan for real media artifacts (.mp4, .wav, etc.) instead of contract shape stubs",
    )
    plan_cmd.add_argument(
        "--shape",
        action="store_true",
        default=False,
        help="Explicitly compile execution plan with contract shape stubs (default)",
    )
    plan_cmd.add_argument("--out", "-o", default=None, help="Output file for execution plan JSON")

    # execute (Plan Executor)
    execute_cmd = subparsers.add_parser(
        "execute",
        help="Execute a pdx_execution_plan_v1 plan (orchestrates tools, transforms, QC gates)",
    )
    execute_cmd.add_argument("plan_file", help="Path to pdx_execution_plan_v1 JSON plan file")
    execute_cmd.add_argument("--out", "-o", default="output/execute", help="Output directory for execution artifacts")
    execute_cmd.add_argument("--no-resume", action="store_true", help="Force re-execution of all steps")
    execute_cmd.add_argument("--recovery-policy", help="Authorized bounded per-shot QC retry policy JSON")
    execute_cmd.add_argument("--resume-paused", action="store_true", help="Explicitly resume configured auth/quota pauses; never replay unknown submissions")
    execute_cmd.add_argument("--executor", default=None, help="Optional executor override ID")
    execute_cmd.add_argument(
        "--handoff",
        action="store_true",
        help="Delegate media generation steps to asynchronous external agent workorders",
    )
    execute_cmd.add_argument("--job-id", default=None, help="Explicit job ID for workorder authority binding")
    execute_cmd.add_argument("--run-id", default=None, help="Explicit run ID for workorder authority binding")
    execute_cmd.add_argument("--audio-resources", default=None, help="Operator-controlled audio resource map")
    execute_cmd.add_argument("--source-executions", default=None, help="Operator-controlled source execution map for post-production plans")

    # workorder (Handoff inspection)
    wo_cmd = subparsers.add_parser(
        "workorder",
        help="Inspect asynchronous Handoff workorders and agent results",
    )
    wo_sub = wo_cmd.add_subparsers(dest="wo_action")
    wo_list = wo_sub.add_parser("list", help="List Handoff workorders in an output directory")
    wo_list.add_argument("out_dir", help="Output directory containing handoff/workorders")
    wo_list.add_argument("--json", action="store_true", help="Output in JSON format")
    wo_list.add_argument("--active-only", action="store_true", help="Show only active pending workorders")
    wo_list.add_argument("--summary", action="store_true", help="Include active/historical classification in JSON")
    wo_input = wo_sub.add_parser("input", help="Verify the input of an active local Handoff workorder")
    wo_input.add_argument("out_dir")
    wo_input.add_argument("workorder_id")
    wo_fulfill = wo_sub.add_parser("fulfill", help="Validate output/failure and write a workorder result")
    wo_fulfill.add_argument("out_dir", help="Job output directory")
    wo_fulfill.add_argument("workorder_id", help="Active workorder ID")
    wo_fulfill.add_argument("--artifact", default=None, help="Produced artifact path matching target_artifact")
    wo_fulfill.add_argument("--status", choices=("completed", "failed"), default="completed", help="Outcome status")
    wo_fulfill.add_argument("--continuity-method", default=None, help="Explicitly declare continuity method used")
    wo_fulfill.add_argument("--ref-asset", action="append", default=None, help="Declared ref asset usage as KEY=SHA256")
    wo_fulfill.add_argument("--capability-gap", default=None, help="Unresolved capability gap (for --status failed)")
    wo_fulfill.add_argument("--detail", default=None, help="Optional operator/agent detail")

    # review (Consistency review gate)
    review_cmd = subparsers.add_parser(
        "review",
        help="Manage consistency review records for completed shots",
    )
    review_sub = review_cmd.add_subparsers(dest="review_action")

    rev_list = review_sub.add_parser(
        "list",
        help="List shots awaiting review or with invalidated/rejected records",
    )
    rev_list.add_argument("out_dir", help="Output directory containing execution journal")
    rev_list.add_argument("--json", action="store_true", help="Output in JSON format")

    rev_status = review_sub.add_parser(
        "status",
        help="Show overall consistency review gate status for an output directory",
    )
    rev_status.add_argument("out_dir", help="Output directory containing execution journal")
    rev_status.add_argument("--json", action="store_true", help="Output in JSON format")

    rev_approve = review_sub.add_parser(
        "approve",
        help="Approve a shot's consistency review (binds to current artifact SHA + digest)",
    )
    rev_approve.add_argument("out_dir", help="Output directory containing execution journal")
    rev_approve.add_argument("step_id", help="Step ID of the shot to approve")
    rev_approve.add_argument(
        "--reviewer",
        default="human",
        help="Reviewer name or identifier (default: 'human')",
    )
    rev_approve.add_argument("--notes", default="", help="Optional review notes")

    rev_reject = review_sub.add_parser(
        "reject",
        help="Reject a shot's consistency review",
    )
    rev_reject.add_argument("out_dir", help="Output directory containing execution journal")
    rev_reject.add_argument("step_id", help="Step ID of the shot to reject")
    rev_reject.add_argument(
        "--reviewer",
        default="human",
        help="Reviewer name or identifier (default: 'human')",
    )
    rev_reject.add_argument(
        "--notes",
        default="",
        help="Rejection reason / notes (recommended)",
    )
    rev_reject.add_argument(
        "--needs-rework",
        action="store_true",
        default=False,
        help="Use verdict 'needs_rework' instead of 'rejected'",
    )

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
    verify_cmd.add_argument("media_file", nargs="?", help="Path to video or audio file")
    verify_cmd.add_argument("--expected", default=None, help="Path to expected JSON contract file or JSON string")
    verify_cmd.add_argument("--job", help="Verify the assembled MP4 against its execution journal and plan")
    verify_cmd.add_argument("--artifact", help="Job-relative artifact to verify in place of the assembly")
    verify_cmd.add_argument("--plan", help="Original plan for a job without plan.json in its output directory")
    demo_cmd = subparsers.add_parser("demo", help="Produce and verify a short local synthetic MP4")
    demo_cmd.add_argument("--out", default="demo-output", help="New output directory (default: demo-output)")
    demo_cmd.add_argument("--handoff", action="store_true", help="Demonstrate closed synthetic Handoff, continuity, and review lifecycle")
    status_cmd = subparsers.add_parser("status", help="Show a local job's last recorded state and next action")
    status_cmd.add_argument("out_dir", help="Job output directory")
    status_cmd.add_argument("--json", action="store_true", help="Output a structured status report")
    sheet_cmd = subparsers.add_parser("sheet", help="Create or inspect starter DOCX shot sheets")
    sheet_sub = sheet_cmd.add_subparsers(dest="sheet_action")
    sheet_new = sheet_sub.add_parser("new", help="Write a starter DOCX shot sheet")
    sheet_new.add_argument("--out", default="shot_list.docx", help="Output DOCX file (default: shot_list.docx)")
    sheet_new.add_argument("--shots", type=int, default=2, help="Number of starter shots (default: 2)")
    sheet_new.add_argument("--handoff", action="store_true", help="Include ID, CONTINUITY, and REVIEW_REQUIRED tags")
    sheet_new.add_argument("--force", action="store_true", help="Overwrite existing file")

    # executors (M4)
    subparsers.add_parser(
        "executors",
        help="List available video generation executors",
    )

    # FSF-S studio flows (CLI / skill; no desk)
    flows_cmd = subparsers.add_parser(
        "flows",
        help="List fourteen Studio flows and kaggle/api/local shape transport status",
    )
    flows_cmd.add_argument("--json", action="store_true", help="Output versioned machine JSON (fsf_flows_v2)")
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
    check_cmd = subparsers.add_parser(
        "check",
        help="Check environment dependencies, media capabilities, and execution health",
    )
    check_cmd.add_argument(
        "--capabilities",
        action="store_true",
        help="Inspect runtime media capabilities, subprocess safety, and tool paths",
    )
    check_cmd.add_argument(
        "--smoke",
        action="store_true",
        help="Run a quick media conformance smoke test with stage timings",
    )
    check_cmd.add_argument(
        "--json",
        action="store_true",
        help="Output check report in JSON format",
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


def handle_check(args: argparse.Namespace) -> int:
    import importlib.metadata as m
    import shutil
    import tempfile
    import time
    from fsf.executors.subprocess_safety import run_safe_subprocess
    from fsf.conformance.media import (
        DEFAULT_BROLL_EXPECTED,
        evaluate_media_file,
        get_last_evaluation_timings,
    )

    t0 = time.perf_counter()
    packages = ["prodocux", "pdx-artifact-engine", "pdx-adapter-media", "python-docx", "kaggle"]
    pkg_status: dict[str, Any] = {}
    all_ok = True

    for pkg in packages:
        try:
            ver = m.version(pkg)
            pkg_status[pkg] = {"installed": True, "version": ver}
        except m.PackageNotFoundError:
            pkg_status[pkg] = {"installed": False, "version": None}
            all_ok = False

    # Tool resolution & timings
    from fsf.conformance.media import ensure_media_tools_on_path

    t_res0 = time.perf_counter()
    ffmpeg_exe, ffprobe_exe = ensure_media_tools_on_path()
    tool_res_ms = round((time.perf_counter() - t_res0) * 1000.0, 2)

    ffmpeg_info: dict[str, Any] = {
        "available": bool(ffmpeg_exe),
        "path": ffmpeg_exe,
        "version": None,
        "probe_ms": tool_res_ms,
    }
    if ffmpeg_exe:
        sub = run_safe_subprocess([ffmpeg_exe, "-version"], timeout_seconds=5.0)
        if sub.ok and sub.stdout:
            ffmpeg_info["version"] = sub.stdout.splitlines()[0]
    else:
        all_ok = False

    ffprobe_info: dict[str, Any] = {
        "available": bool(ffprobe_exe),
        "path": ffprobe_exe,
        "version": None,
    }
    if ffprobe_exe:
        sub = run_safe_subprocess([ffprobe_exe, "-version"], timeout_seconds=5.0)
        if sub.ok and sub.stdout:
            ffprobe_info["version"] = sub.stdout.splitlines()[0]
    else:
        all_ok = False

    # Scratch write test
    t_scratch0 = time.perf_counter()
    scratch_ok = False
    try:
        with tempfile.NamedTemporaryFile("w", delete=True) as tf:
            tf.write("fsf_check_ok")
            scratch_ok = True
    except Exception:
        scratch_ok = False
        all_ok = False
    scratch_ms = round((time.perf_counter() - t_scratch0) * 1000.0, 2)

    smoke_result: dict[str, Any] | None = None
    if getattr(args, "smoke", False) and ffmpeg_exe:
        t_smoke0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="fsf_smoke_") as smk_dir:
            test_mp4 = Path(smk_dir) / "smoke.mp4"
            # Generate deterministic 1.0s dynamic test video (avoids false freeze detection)
            gen_cmd = [
                ffmpeg_exe,
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=1024x576:rate=24",
                "-t",
                "1.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "24",
                "-an",
                str(test_mp4),
            ]
            gen_res = run_safe_subprocess(gen_cmd, timeout_seconds=15.0)
            if gen_res.ok and test_mp4.is_file():
                expected = {
                    "containers": ["mp4"],
                    "video_codecs": ["h264"],
                    "width": 1024,
                    "height": 576,
                    "fps": 24,
                    "duration_seconds": 1.0,
                    "duration_tolerance_seconds": 0.25,
                    "audio": "forbidden",
                    "max_black_frame_ratio": 0.01,
                    "max_freeze_duration_seconds": 0.5,
                }
                report, host_action = evaluate_media_file(test_mp4, expected)
                timings = get_last_evaluation_timings()
                smoke_result = {
                    "ok": report.get("evaluation_status") == "conforms",
                    "evaluation_status": report.get("evaluation_status"),
                    "host_action": host_action.get("host_action"),
                    "generation_ms": gen_res.elapsed_ms,
                    "eval_timings_ms": timings,
                    "total_ms": round((time.perf_counter() - t_smoke0) * 1000.0, 2),
                }
                if not smoke_result["ok"]:
                    all_ok = False
            else:
                smoke_result = {
                    "ok": False,
                    "error": "Failed to generate smoke fixture MP4",
                    "stderr": gen_res.stderr[-300:],
                }
                all_ok = False

    total_check_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    report_payload = {
        "status": "ok" if all_ok else "failed",
        "packages": pkg_status,
        "tools": {
            "ffmpeg": ffmpeg_info,
            "ffprobe": ffprobe_info,
        },
        "capabilities": {
            "process_safety": "verified",
            "scratch_writable": scratch_ok,
            "scratch_ms": scratch_ms,
            "handoff_mode": "supported",
            "audio_policies": ["keep", "strip", "require"],
        },
        "smoke": smoke_result,
        "total_check_ms": total_check_ms,
    }

    if getattr(args, "json", False):
        print(json.dumps(report_payload, indent=2, ensure_ascii=False))
        return 0 if all_ok else 1

    print("=== Free Studio Flow Dependency Status & Capability Check ===")
    print(f"Status: {'[PASSED]' if all_ok else '[FAILED]'} (completed in {total_check_ms}ms)")
    print("\n[Packages]")
    for pkg, pdata in pkg_status.items():
        if pdata["installed"]:
            print(f"  [OK] {pkg}: {pdata['version']}")
        else:
            print(f"  [MISSING] {pkg}")

    print("\n[Media Tools]")
    print(f"  FFmpeg:  {'[OK] ' + str(ffmpeg_info['path']) if ffmpeg_info['available'] else '[MISSING]'}")
    print(f"  FFprobe: {'[OK] ' + str(ffprobe_info['path']) if ffprobe_info['available'] else '[MISSING]'}")
    print(f"  Scratch: {'[OK] Writable' if scratch_ok else '[FAIL] Cannot write temporary files'}")

    if getattr(args, "capabilities", False):
        print("\n[Runtime Capabilities]")
        print("  Process Safety: POSIX start_new_session + Windows taskkill tree cleanup")
        print("  Handoff Mode: Authoritative workorders & idempotent result ingestion")
        print("  Audio Policies: keep (default normalization), strip, require")

    if smoke_result:
        print("\n[Media Conformance Smoke Test]")
        if smoke_result.get("ok"):
            print(f"  [OK] Status: {smoke_result.get('evaluation_status')} ({smoke_result.get('total_ms')}ms)")
            print(f"       Timings: {smoke_result.get('eval_timings_ms')}")
        else:
            print(f"  [FAIL] {smoke_result}")

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


def handle_flows(args: argparse.Namespace | None = None) -> int:
    flows = list_studio_flows()
    if getattr(args, "json", False):
        payload = {
            "schema_version": "fsf_flows_v2",
            "transport_mode": "shape",
            "live_backend_wired": False,
            "flows": [
                {
                    **flow,
                    "interface_status": flow["status"],
                    "transport_mode": "shape",
                    "live_backend_wired": False,
                }
                for flow in flows
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print("=== FSF-S Studio Flows (CLI / skill interface; shape-only transports, not live backends) ===")
    for flow in flows:
        print(f"  [{flow['status']:<11}] [shape-only] {flow['phase']} {flow['skill']}  {flow['title']}")
        bits = []
        for name in TRANSPORTS:
            row = flow["transports"][name]
            bits.append(f"{name}={row['status']}")
        print(f"               transports (shape stubs): {', '.join(bits)}")
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
            select_tasks_for_skill(manifest["tasks"], args.skill),
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
    print(
        f"[FSF Run] Job: {manifest['source_document']} | "
        f"Tasks: {manifest['task_count']} | "
        f"Shots: {manifest.get('shot_count', manifest['task_count'])} | "
        f"Assets: {manifest.get('asset_count', 0)}"
    )
    print(f"[FSF Run] Executor: {executor.display_name} ({executor.executor_id}) kind={kind}")
    if kind == "mock":
        print("[FSF Run] MOCK artifacts are not media; filenames use .mock.bin")
    elif kind == "shape":
        print("[FSF Run] SHAPE artifacts are not media; filenames use .shape.bin")

    if not args.no_resume:
        resumed_manifest, pending_tasks, completed_tasks = build_resumed_job(
            manifest, out_dir, executor_id=executor.executor_id
        )
        skipped_count = resumed_manifest.get("skipped_count", 0)
        skip_msg = f" | Skipped: {skipped_count}" if skipped_count > 0 else ""
        print(f"[FSF Resume] Pending: {len(pending_tasks)} | Reused: {len(completed_tasks)}{skip_msg}")
        active_tasks = pending_tasks
        completed_artifacts = {
            task["id"]: dict(task["artifact"])
            for task in completed_tasks
            if isinstance(task.get("artifact"), dict)
        }
    else:
        active_tasks = [task for task in manifest["tasks"] if is_shot_unit(task)]
        completed_artifacts = {}

    for task in active_tasks:
        shot_id = task["id"]
        print(f"  -> Generating {shot_id}...")
        res = executor.execute_shot(task, out_dir)
        completed_artifacts[shot_id] = checkpoint_record_from_tool_result(
            manifest, task, res, kind
        )

    save_checkpoint(out_dir, manifest, completed_artifacts)
    print(f"[FSF Run] Finished. Checkpoint saved to '{out_dir / 'fsf_checkpoint.json'}'")
    return 0


def handle_verify(args: argparse.Namespace) -> int:
    if args.job is not None:
        if args.media_file is not None or args.expected is not None:
            print("[FSF Verify] Use --job with optional --artifact and --plan.", file=sys.stderr)
            return 2
        from fsf.runtime.job_media import JobMediaError, verify_job
        try:
            result = verify_job(args.job, artifact=args.artifact, plan=args.plan)
        except JobMediaError as exc:
            return report_local_media_error("Verify", str(exc))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            return report_local_media_error("Verify", "JOB_INVALID")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result["evaluation_status"] == "conforms" else 1
    if not args.media_file or args.artifact is not None or args.plan is not None:
        print("[FSF Verify] Specify a media file or --job.", file=sys.stderr)
        return 2
    media_path = Path(args.media_file)
    if media_path.is_dir():
        print(f"[FSF Verify] '{args.media_file}' is a directory. Use --job '{args.media_file}'.", file=sys.stderr)
        return 2
    expected = DEFAULT_BROLL_EXPECTED
    if args.expected:
        exp_path = Path(args.expected)
        if exp_path.is_file():
            expected = json.loads(exp_path.read_text(encoding="utf-8"))
        else:
            expected = json.loads(args.expected)

    report, host_action = evaluate_media_file(media_path, expected)
    if report.get("schema_version") == "fsf_policy_decision_v1":
        print("=== FSF Policy Decision (not a Media conformance report) ===")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print("\n=== Host Action Mapping ===")
        print(json.dumps(host_action, indent=2, ensure_ascii=False))
        return 1
    print("=== PDX Media Conformance Report ===")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("\n=== Host Action Mapping ===")
    print(json.dumps(host_action, indent=2, ensure_ascii=False))
    return 0 if report.get("evaluation_status") == "conforms" else 1


def report_local_media_error(command, code):
    hint = " Put working ffmpeg and ffprobe executables on PATH." if code == "FFMPEG_REQUIRED" else ""
    print(f"[FSF {command}] {code}.{hint}", file=sys.stderr)
    return 1


def handle_demo(args: argparse.Namespace) -> int:
    from fsf.runtime.demo import run_demo
    from fsf.runtime.job_media import JobMediaError
    handoff = bool(getattr(args, "handoff", False))
    try:
        result = run_demo(args.out, handoff=handoff)
    except KeyboardInterrupt:
        print("[FSF Demo] Cancelled.", file=sys.stderr)
        return 130
    except JobMediaError as exc:
        return report_local_media_error("Demo", str(exc))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return report_local_media_error("Demo", "DEMO_OUTPUT_OR_EXECUTION_INVALID")
    print(json.dumps({**result,"output_dir":args.out,
                     "media_path":str(Path(args.out) / result["artifact"])}, ensure_ascii=True, indent=2))
    return 0


def handle_sheet(args: argparse.Namespace) -> int:
    from docx import Document

    action = getattr(args, "sheet_action", None)
    if action != "new":
        print("[FSF Sheet] Use 'fsf sheet new --out shot_list.docx'.", file=sys.stderr)
        return 2
    shots = int(getattr(args, "shots", 2) or 0)
    if shots < 1:
        print("[FSF Sheet] --shots must be at least 1.", file=sys.stderr)
        return 2
    target = Path(args.out)
    if target.exists() and not getattr(args, "force", False):
        print(f"[FSF Sheet] '{target}' already exists. Pass --force to overwrite.", file=sys.stderr)
        return 1
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    handoff = bool(getattr(args, "handoff", False))
    for index in range(1, shots + 1):
        if index > 1:
            doc.add_paragraph("-----")
        shot_id = f"T{index:02d}"
        if handoff:
            doc.add_paragraph(f"ID: {shot_id}")
            if index > 1:
                doc.add_paragraph(f"CONTINUITY: T{index - 1:02d}")
                doc.add_paragraph("CONTINUITY_ASPECTS: appearance")
            doc.add_paragraph("REVIEW_REQUIRED: true")
        doc.add_paragraph(f"SHOT: Synthetic shot {shot_id} description. 2 seconds.")
    doc.save(target)
    print(json.dumps({"schema_version": "fsf_sheet_starter_v1", "path": str(target), "shots": shots, "handoff": handoff}, ensure_ascii=True))
    return 0


def handle_status(args: argparse.Namespace) -> int:
    from fsf.runtime.job_status import inspect_status

    exit_code = 0
    try:
        report = inspect_status(args.out_dir)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        report = {"schema_version": "fsf_job_status_v1", "out_dir": args.out_dir,
                  "job_id": None, "run_id": None, "state": "unknown", "workorders": [],
                  "blockers": [{"code": "STATUS_RECORD_INVALID"}],
                  "next_action": {"kind": "inspect_records", "argv": None,
                                  "instruction": "Check that the job records are readable and intact."}}
        exit_code = 1
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(f"Job state: {report['state']}")
        for order in report["workorders"]:
            print(f"  {order['workorder_id']} (attempt {order['attempt_id']})")
        for blocker in report["blockers"]:
            print(f"  {blocker['code']} {blocker.get('step_id', '')}".rstrip())
        print(report["next_action"]["instruction"])
    return exit_code


def handle_workorder(args: argparse.Namespace) -> int:
    from fsf.runtime.handoff import list_pending_workorders

    action = getattr(args, "wo_action", None)
    if action == "input":
        import re
        from fsf.core.asset_library import _no_links
        from fsf.core.edit_store import read_json
        from fsf.runtime.handoff import resolve_workorder_input

        try:
            root = Path(args.out_dir).resolve(strict=True)
            identifier = args.workorder_id
            if not re.fullmatch(r"wo_[A-Za-z0-9_-]+", identifier):
                raise ValueError()
            workorder_path = root / "handoff" / "workorders" / f"{identifier}.workorder.json"
            journal_path = root / "fsf_execution_journal.json"
            _no_links(workorder_path)
            _no_links(journal_path)
            order, journal = read_json(workorder_path), read_json(journal_path)
            step = journal["steps"][order["step_id"]]
            if (order["workorder_id"] != identifier or step["workorder_id"] != identifier
                    or step["workorder_digest"] != order["workorder_digest"]
                    or type(step["attempt_id"]) is not int or order["attempt_id"] != step["attempt_id"]
                    or step["status"] != "pending_handoff"
                    or any(order[key] != journal[key] for key in ("job_id", "run_id", "plan_digest"))):
                raise ValueError()
            path = resolve_workorder_input(order, root)
        except (KeyError, OSError, TypeError, ValueError, AttributeError):
            print("[FSF Handoff] Cannot verify the active workorder input.", file=sys.stderr)
            return 1
        print(json.dumps({"path":path.relative_to(root).as_posix(),
                          "sha256":order["task_payload"]["input_artifact_sha256"]}, ensure_ascii=True))
        return 0
    if action == "fulfill":
        from fsf.runtime.handoff import HandoffIntegrityError, HandoffSecurityError, fulfill_workorder
        from fsf.runtime.job_media import JobMediaError

        ref_assets: list[tuple[str, str]] | None = None
        if getattr(args, "ref_asset", None):
            ref_assets = []
            for entry in args.ref_asset:
                if "=" not in entry:
                    print("[FSF Handoff] --ref-asset must use KEY=SHA256.", file=sys.stderr)
                    return 2
                key, sha_val = entry.split("=", 1)
                ref_assets.append((key.strip(), sha_val.strip()))
        try:
            receipt = fulfill_workorder(
                args.out_dir,
                args.workorder_id,
                artifact=args.artifact,
                status=args.status,
                continuity_method=args.continuity_method,
                ref_assets=ref_assets,
                capability_gap=args.capability_gap,
                detail=args.detail,
            )
        except (HandoffIntegrityError, HandoffSecurityError, JobMediaError, OSError, ValueError) as exc:
            print(f"[FSF Handoff] {exc}", file=sys.stderr)
            return 1
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
        return 0
    if action == "list":
        from fsf.runtime.handoff import list_active_workorders, summarize_workorders

        out_dir = Path(args.out_dir).resolve()
        active_only = bool(getattr(args, "active_only", False))
        use_summary = bool(getattr(args, "summary", False))
        if getattr(args, "json", False):
            if use_summary:
                workorders = summarize_workorders(out_dir, active_only=active_only)
            else:
                workorders = list_active_workorders(out_dir) if active_only else list_pending_workorders(out_dir)
            print(json.dumps(workorders, indent=2, ensure_ascii=False))
            return 0

        summaries = summarize_workorders(out_dir, active_only=active_only)
        active_rows = [row for row in summaries if row["state"] == "active_pending"]
        history_rows = [row for row in summaries if row["state"] != "active_pending"]
        print(f"=== Active Handoff Workorders in '{out_dir}' ===")
        if not active_rows:
            print("  No active pending workorders.")
        for row in active_rows:
            contract = row.get("media_summary")
            spec = (f" media={contract.get('width')}x{contract.get('height')}@{contract.get('fps')}fps/"
                    f"{contract.get('duration_seconds')}s/audio={contract.get('audio')}"
                    if contract else "")
            cont = " continuity=required" if row.get("continuity_required") else ""
            res_tag = " [result-staged]" if row.get("result_present") else ""
            print(f"  [{row['workorder_id']}] step={row['step_id']} att={row['attempt_id']} "
                  f"tool={row['tool']} target={row['target_artifact']}{spec}{cont}{res_tag}")
        if not active_only and history_rows:
            print("=== Historical / Superseded Workorders ===")
            for row in history_rows:
                print(f"  [{row['state']}] [{row['workorder_id']}] step={row['step_id']} att={row['attempt_id']} target={row['target_artifact']}")
        return 0
    return 1


def handle_review(args: argparse.Namespace) -> int:
    """Handle the 'fsf review' sub-commands (list / status / approve / reject)."""
    from fsf.runtime.consistency_review import (
        build_review_record,
        check_assembly_gate,
        list_shots_pending_review,
        load_reviews,
        review_workorder_valid,
        save_reviews,
    )

    action = getattr(args, "review_action", None)
    if not action:
        print("[FSF Review] No sub-command.  Use: list | status | approve | reject", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir).resolve()
    journal_path = out_dir / "fsf_execution_journal.json"

    # Load journal ─────────────────────────────────────────────────────────
    if not journal_path.is_file():
        print(
            f"[FSF Review] No execution journal found in '{out_dir}'.  "
            "Run 'fsf execute' first.",
            file=sys.stderr,
        )
        return 1
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FSF Review] Failed to parse journal: {exc}", file=sys.stderr)
        return 1

    reviews = load_reviews(out_dir)

    # ── list ──────────────────────────────────────────────────────────────
    if action == "list":
        pending = list_shots_pending_review(journal, reviews, out_dir)
        if getattr(args, "json", False):
            print(json.dumps(pending, indent=2, ensure_ascii=False))
            return 0
        if not pending:
            print("[FSF Review] All shots with consistency requirements are approved.")
            return 0
        print(f"=== Shots Awaiting or Requiring Review in '{out_dir}' ===")
        for item in pending:
            print(
                f"  [{item['status'].upper()}] {item['step_id']}: {item['reason']}"
            )
        return 0

    # ── status ────────────────────────────────────────────────────────────
    if action == "status":
        gate_ok, reasons = check_assembly_gate(journal, reviews, out_dir)
        if getattr(args, "json", False):
            print(json.dumps({"gate_ok": gate_ok, "blocking_reasons": reasons}, indent=2))
            return 0 if gate_ok else 1
        if gate_ok:
            print("[FSF Review] Gate: PASS — all shots approved, assembly may proceed.")
            return 0
        print(f"[FSF Review] Gate: BLOCKED ({len(reasons)} issue(s)):")
        for r in reasons:
            print(f"  {r}")
        return 1

    # ── approve / reject / needs_rework ───────────────────────────────────
    if action in ("approve", "reject"):
        step_id = args.step_id
        reviewer = getattr(args, "reviewer", "human")
        notes = getattr(args, "notes", "")
        needs_rework = getattr(args, "needs_rework", False)

        if action == "approve":
            verdict = "approved"
        elif needs_rework:
            verdict = "needs_rework"
        else:
            verdict = "rejected"

        # Resolve step metadata from journal
        steps = journal.get("steps", {})
        if step_id not in steps:
            print(
                f"[FSF Review] Step '{step_id}' not found in journal.  "
                f"Available: {list(steps.keys())}",
                file=sys.stderr,
            )
            return 1
        step_data = steps[step_id]
        if step_data.get("status") not in ("completed", "reused"):
            print(
                f"[FSF Review] Step '{step_id}' has status '{step_data.get('status')}', "
                "not completed or reused — cannot review.",
                file=sys.stderr,
            )
            return 1
        if action == "approve" and not review_workorder_valid(
                step_data, step_id=step_id, job_id=journal.get("job_id"), run_id=journal.get("run_id")):
            print("[FSF Review] Handoff workorder or attempt identity is missing or invalid.", file=sys.stderr)
            return 1

        out_files = step_data.get("output_files", [])
        if not out_files:
            print(f"[FSF Review] Step '{step_id}' has no output_files in journal.", file=sys.stderr)
            return 1
        artifact_sha = str(out_files[0].get("sha256", "")).strip()
        if not artifact_sha:
            print(f"[FSF Review] Step '{step_id}' output_files[0] has no sha256.", file=sys.stderr)
            return 1

        req_dig = step_data.get("requirements_digest")
        ack = step_data.get("consistency_acknowledgement") or {}
        ack_dig = ack.get("acknowledged_digest")
        wo_id = str(step_data.get("workorder_id") or "")

        try:
            record = build_review_record(
                step_id=step_id,
                job_id=journal.get("job_id", ""),
                run_id=journal.get("run_id", ""),
                workorder_id=wo_id,
                artifact_sha256=artifact_sha,
                requirements_digest=req_dig,
                acknowledged_digest=ack_dig,
                verdict=verdict,
                reviewer=reviewer,
                notes=notes,
            )
        except ValueError as exc:
            print(f"[FSF Review Error] {exc}", file=sys.stderr)
            return 1

        reviews[step_id] = record
        save_reviews(reviews, out_dir)
        print(
            f"[FSF Review] {verdict.upper()}: '{step_id}' "
            f"(artifact={artifact_sha[:12]}…, reviewer='{reviewer}')"
        )
        return 0

    print(f"[FSF Review] Unknown action '{action}'", file=sys.stderr)
    return 1


def _persist_bound_plan(out_dir: Path, plan_data: dict[str, Any]) -> None:
    import os
    import tempfile
    from fsf.runtime.handoff import _ensure_no_internal_links, _publish_workorder_result

    if not out_dir.is_dir():
        return
    target = out_dir / "plan.json"
    if os.path.lexists(target):
        return
    _ensure_no_internal_links(out_dir, target)
    data = json.dumps(plan_data, allow_nan=False, indent=2).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=out_dir)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _publish_workorder_result(staged, target)
        except FileExistsError:
            pass
    finally:
        staged.unlink(missing_ok=True)


def handle_execute(args: argparse.Namespace) -> int:
    from fsf.runtime.plan_executor import PlanExecutor

    if getattr(args, "source_executions", None):
        from fsf.core.edit_store import read_json
        try:
            plan_data = read_json(args.plan_file)
            sources = read_json(args.source_executions)
            executor = PlanExecutor(args.out, no_resume=args.no_resume, executor_override=args.executor,
                                    handoff=args.handoff, job_id=args.job_id, run_id=args.run_id, source_executions=sources, audio_resources=read_json(args.audio_resources) if args.audio_resources else None)
            result = executor.execute_plan(plan_data)
        except KeyboardInterrupt:
            print(json.dumps({"status":"cancelled","success":False,"error":"POST_CANCELLED"},ensure_ascii=True))
            return 130
        except Exception:
            result = {"status": "blocked", "success": False, "error": "POST_INVALID"}
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("success") else 2

    plan_path = Path(args.plan_file).resolve()
    if not plan_path.is_file():
        print(f"[FSF Execute Error] Plan file not found: {plan_path}", file=sys.stderr)
        return 1

    try:
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FSF Execute Error] Failed to parse plan JSON: {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out).resolve()
    try:
        from fsf.core.edit_store import read_json
        recovery_policy=read_json(args.recovery_policy) if getattr(args,"recovery_policy",None) else None
    except Exception:
        print(json.dumps({"status":"failed","success":False,"error":"RECOVERY_POLICY_INVALID"}))
        return 2
    executor = PlanExecutor(
        out_dir,
        no_resume=args.no_resume,
        executor_override=args.executor,
        handoff=getattr(args, "handoff", False),
        job_id=getattr(args, "job_id", None),
        run_id=getattr(args, "run_id", None),
        recovery_policy=recovery_policy,
        resume_paused=getattr(args,"resume_paused",False),
    )

    try:
        result = executor.execute_plan(plan_data)
        _persist_bound_plan(out_dir, plan_data)
    except Exception as exc:
        print(f"[FSF Execute Error] Execution failed: {scrub_secrets(str(exc))}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    status = result.get("status")
    if status == "completed" or (result.get("success") and status != "pending_handoff"):
        print(
            f"[FSF Execute] Success: completed {result.get('completed_count')} steps, "
            f"reused {result.get('reused_count')} steps."
        )
        return 0
    elif status == "pending_handoff":
        pending_count = result.get("pending_handoff_count", 0)
        wo_dir = out_dir / "handoff" / "workorders"
        print(
            f"[FSF Execute] Pending handoff: waiting for {pending_count} workorder(s) in '{wo_dir}'. "
            f"Run 'fsf status {out_dir} --json' for next_action.",
            file=sys.stderr,
        )
        return 10
    elif status in {"waiting_external","paused_auth","paused_quota","paused_unknown","paused_batch","exhausted"}:
        print(f"[FSF Execute] Delivery blocked: {status}.",file=sys.stderr)
        return 2
    else:
        failed_count = int(result.get("failed_count") or 0)
        blocked_count = int(result.get("blocked_count") or 0)
        steps_map = result.get("steps") or {}
        if failed_count == 0 and blocked_count > 0:
            review_blocked = any(
                isinstance(s, dict) and s.get("blocker") == "REVIEW_REQUIRED"
                for s in steps_map.values()
            )
            if review_blocked:
                print(
                    f"[FSF Execute] Waiting for consistency review: 0 failed, {blocked_count} blocked. "
                    f"Run 'fsf status {out_dir} --json' or 'fsf review list {out_dir}'.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[FSF Execute] Blocked: 0 failed, {blocked_count} blocked. "
                    f"Run 'fsf status {out_dir} --json'.",
                    file=sys.stderr,
                )
        else:
            print(
                f"[FSF Execute] Failed: {failed_count} failed, {blocked_count} blocked. "
                f"Run 'fsf status {out_dir} --json'.",
                file=sys.stderr,
            )
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "reference":
        from fsf.cli.reference import main as reference_main
        return reference_main(arguments[1:])
    if arguments and arguments[0] == "production":
        from fsf.cli.production import main as production_main
        return production_main(arguments[1:])
    if arguments and arguments[0] == "api":
        from fsf.cli.model_api import main as model_api_main
        return model_api_main(arguments[1:])
    if arguments and arguments[0] == "remote":
        from fsf.cli.remote import main as remote_main
        return remote_main(arguments[1:])
    if arguments and arguments[0] in {"blender", "asset"}:
        from fsf.cli.blender import main as blender_main
        return blender_main(arguments[1:], assets=arguments[0] == "asset")
    if arguments and arguments[0] == "edit":
        from fsf.cli.edit import main as edit_main
        return edit_main(arguments[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "execute":
        return handle_execute(args)
    elif args.command == "intake":
        docx_path = Path(args.docx)
        try:
            from fsf.core.edit_store import read_json
            manifest, request, profile = compile_job(
                docx_path, out_dir=args.out, tool_name=args.tool,
                media_contracts=read_json(args.media_contracts) if args.media_contracts else None,
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
            if getattr(args, "media", False) and getattr(args, "shape", False):
                print("[FSF Plan Error] Cannot specify both --media and --shape", file=sys.stderr)
                return 2
            shape_mode = not getattr(args, "media", False)
            if getattr(args, "post_sources", False) and shape_mode:
                print("[FSF Plan Error] --post-sources requires --media", file=sys.stderr)
                return 2
            from fsf.core.edit_store import read_json
            manifest, _, _ = compile_job(docx_path,media_contracts=read_json(args.media_contracts) if args.media_contracts else None)
            plan = build_execution_plan(
                manifest,
                executor_tool=args.tool,
                pipeline=args.pipeline,
                skills=args.skills,
                enable_assembly=getattr(args, "assembly", False),
                shape=shape_mode,
                subtitle_policy=getattr(args, "subtitles", "none"),
                bgm_artifact=getattr(args, "bgm", None),
                audio_policy=getattr(args, "audio", "keep"),
                enable_post_source_qc=getattr(args, "post_sources", False),
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
    elif args.command == "demo":
        return handle_demo(args)
    elif args.command == "sheet":
        return handle_sheet(args)
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
        return handle_flows(args)
    elif args.command == "flow":
        if getattr(args, "flow_command", None) == "run":
            return handle_flow_run(args)
        parser.parse_args(["flow", "--help"])
        return 0
    elif args.command == "check":
        return handle_check(args)
    elif args.command == "workorder":
        return handle_workorder(args)
    elif args.command == "status":
        return handle_status(args)
    elif args.command == "review":
        return handle_review(args)
    elif args.command == "live-check":
        return handle_live_check(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
