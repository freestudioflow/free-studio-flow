"""Credential-free local demonstration of the installed FSF workflow."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

from docx import Document

from fsf.core.edit_store import read_json
from fsf.executors.process_cmd import ProcessCommandExecutor
from fsf.executors.registry import ExecutorRegistry
from fsf.intake.compiler import compile_job
from fsf.remote.contracts import write_json
from fsf.runtime.job_media import JobMediaError, _job_file, local_media_tools, verify_job
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor


def _build_demo_docx(path: Path, *, handoff: bool) -> None:
    sheet = Document()
    if handoff:
        for idx, lines in enumerate((
            ("ID: T01", "REVIEW_REQUIRED: true", "SHOT: Synthetic scene one, moving color bars. 2 seconds."),
            ("ID: T02", "CONTINUITY: T01", "CONTINUITY_ASPECTS: appearance",
             "REVIEW_REQUIRED: true", "SHOT: Synthetic scene two continuing from T01. 2 seconds."),
        )):
            if idx:
                sheet.add_paragraph("-----")
            for line in lines:
                sheet.add_paragraph(line)
    else:
        sheet.add_paragraph("SHOT: Synthetic moving test pattern. 2 seconds.")
        sheet.add_paragraph("-----")
        sheet.add_paragraph("SHOT: Synthetic test pattern continues. 2 seconds.")
    sheet.save(path)


def _render_synthetic_mp4(ffmpeg: str, target: Path, *, source: Path | None = None) -> None:
    if source is not None:
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
               "-vf", "hue=h=60", "-t", "2", "-r", "24", "-s", "1024x576",
               "-threads", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(target)]
    else:
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", "testsrc2=size=1024x576:rate=24", "-t", "2",
               "-threads", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise JobMediaError("DEMO_EXECUTION_FAILED")


def _active_workorder(root: Path, step_id: str) -> dict:
    journal = read_json(root / "fsf_execution_journal.json")
    wo_id = journal["steps"][step_id]["workorder_id"]
    return read_json(root / "handoff" / "workorders" / f"{wo_id}.workorder.json")


def _record_synthetic_review(root: Path, step_id: str) -> dict:
    from fsf.runtime.consistency_review import build_review_record, load_reviews, save_reviews

    journal = read_json(root / "fsf_execution_journal.json")
    step = journal["steps"][step_id]
    reviews = load_reviews(root)
    record = build_review_record(
        step_id=step_id,
        job_id=journal["job_id"],
        run_id=journal["run_id"],
        workorder_id=step["workorder_id"],
        artifact_sha256=step["output_files"][0]["sha256"],
        requirements_digest=step.get("requirements_digest"),
        acknowledged_digest=(step.get("consistency_acknowledgement") or {}).get("acknowledged_digest"),
        verdict="approved",
        reviewer="synthetic_demo_reviewer",
        notes="SYNTHETIC DEMO ONLY: closed-fixture automation; real jobs require authorized human review.",
    )
    reviews[step_id] = record
    save_reviews(reviews, root)
    return record


def _run_handoff_demo(root: Path, plan: dict, ffmpeg: str) -> dict:
    from fsf.runtime.consistency_review import check_assembly_gate, load_reviews
    from fsf.runtime.handoff import fulfill_workorder, resolve_workorder_input

    def execute():
        return PlanExecutor(root, handoff=True).execute_plan(plan)

    t01_step = next(s["id"] for s in plan["steps"] if s.get("inputs", {}).get("shot_id") == "T01" and s.get("tool") == "studio.video.generate")
    t02_step = next(s["id"] for s in plan["steps"] if s.get("inputs", {}).get("shot_id") == "T02" and s.get("tool") == "studio.video.generate")

    # 1. Initial emission: T01 pending_handoff, T02 blocked on dependency
    first = execute()
    if first["status"] != "pending_handoff":
        raise JobMediaError("DEMO_EXECUTION_FAILED")
    wo_t01_att1 = _active_workorder(root, t01_step)

    # 2. Intentional rejection (missing consistency_acknowledgement) -> attempt 2
    t01_target = root / wo_t01_att1["target_artifact"]
    _render_synthetic_mp4(ffmpeg, t01_target)
    from fsf.runtime.blender import sha
    bad_result = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": wo_t01_att1["job_id"],
        "run_id": wo_t01_att1["run_id"],
        "workorder_id": wo_t01_att1["workorder_id"],
        "step_id": wo_t01_att1["step_id"],
        "attempt_id": wo_t01_att1["attempt_id"],
        "plan_digest": wo_t01_att1["plan_digest"],
        "inputs_digest": wo_t01_att1["inputs_digest"],
        "expected_contract_digest": wo_t01_att1.get("expected_contract_digest"),
        "status": "completed",
        "artifact_path": wo_t01_att1["target_artifact"],
        "artifact_sha256": sha(t01_target),
    }
    res_dir = root / "handoff" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    write_json(res_dir / f"{wo_t01_att1['workorder_id']}.result.json", bad_result)
    rejected = execute()
    if rejected["steps"][t01_step]["status"] != "failed":
        raise JobMediaError("DEMO_EXECUTION_FAILED")
    retry = execute()
    if retry["status"] != "pending_handoff":
        raise JobMediaError("DEMO_EXECUTION_FAILED")
    wo_t01_att2 = _active_workorder(root, t01_step)

    # 3. Valid T01 fulfillment + synthetic review + T02 emission with continuity
    _render_synthetic_mp4(ffmpeg, root / wo_t01_att2["target_artifact"])
    fulfill_workorder(root, wo_t01_att2["workorder_id"], artifact=wo_t01_att2["target_artifact"])
    mid = execute()
    if mid["steps"][t01_step]["status"] != "completed" or mid["steps"][t02_step]["status"] != "pending_handoff":
        raise JobMediaError("DEMO_EXECUTION_FAILED")
    _record_synthetic_review(root, t01_step)

    # 4. Resolve T02 continuity input, fulfill T02, observe assembly blocked before T02 review
    wo_t02 = _active_workorder(root, t02_step)
    continuity_source = resolve_workorder_input(wo_t02, root)
    _render_synthetic_mp4(ffmpeg, root / wo_t02["target_artifact"], source=continuity_source)
    fulfill_workorder(
        root,
        wo_t02["workorder_id"],
        artifact=wo_t02["target_artifact"],
        continuity_method="first_frame_init",
    )
    blocked = execute()
    assembly_id = next(s["id"] for s in plan["steps"] if s.get("transform") == "fsf.assembly.fast_concat")
    if blocked["steps"][assembly_id].get("blocker") != "REVIEW_REQUIRED":
        raise JobMediaError("DEMO_EXECUTION_FAILED")

    # 5. Synthetic review T02 -> assemble -> non-destructive tamper invalidation check
    _record_synthetic_review(root, t02_step)
    assembled = execute()
    if not assembled["success"]:
        raise JobMediaError("DEMO_EXECUTION_FAILED")

    with tempfile.TemporaryDirectory() as probe_tmp:
        probe_dir = Path(probe_tmp) / "tamper-probe"
        shutil.copytree(root, probe_dir)
        (probe_dir / wo_t01_att2["target_artifact"]).write_bytes(b"tampered")
        tamper_ok, tamper_reasons = check_assembly_gate(
            read_json(probe_dir / "fsf_execution_journal.json"),
            load_reviews(probe_dir),
            probe_dir,
        )
    if tamper_ok:
        raise JobMediaError("DEMO_EXECUTION_FAILED")

    trace = {
        "schema_version": "fsf_demo_handoff_trace_v1",
        "review_authority": "synthetic_demo_only_not_for_production",
        "synthetic_roles": ["synthetic_worker", "synthetic_demo_reviewer"],
        "stages": {
            "initial_dependency_block": first["steps"][t02_step].get("blocker"),
            "rejected_attempt": wo_t01_att1["attempt_id"],
            "retried_attempt": wo_t01_att2["attempt_id"],
            "continuity_input": wo_t02["task_payload"]["input_artifact"],
            "pre_review_assembly_blocker": blocked["steps"][assembly_id].get("blocker"),
            "tamper_invalidation_blocked": not tamper_ok,
            "tamper_reasons": tamper_reasons,
        },
    }
    write_json(root / "demo-handoff-trace.json", trace)
    return trace


def run_demo(output, *, handoff: bool = False):
    root = Path(output).absolute()
    if root.exists() or root.is_symlink():
        root = root.resolve(strict=True)
        try:
            metadata = read_json(_job_file(root, "demo-result.json"))
        except (OSError, ValueError):
            raise JobMediaError("DEMO_OUTPUT_CONFLICT") from None
        verified = verify_job(root)
        if metadata != _receipt(verified, handoff=handoff) or verified["evaluation_status"] != "conforms":
            raise JobMediaError("DEMO_OUTPUT_CONFLICT")
        if handoff:
            _job_file(root, "demo-handoff-trace.json")
        return {**metadata, "reused": True}

    # Resolve the real parent so cleanup is confined to the directory created here.
    with local_media_tools() as ffmpeg:
        root = root.parent.resolve(strict=True) / root.name
        root.mkdir()
        try:
            _build_demo_docx(root / "sample.docx", handoff=handoff)
            manifest, _, _ = compile_job(root / "sample.docx", out_dir=root)
            plan = build_execution_plan(manifest, executor_tool="studio.video.generate", shape=False,
                                        enable_assembly=True, enable_media_conformance=True, audio_policy="strip")
            write_json(root / "plan.json", plan)
            if handoff:
                _run_handoff_demo(root, plan, ffmpeg)
            else:
                registry = ExecutorRegistry(load_extras=False)
                worker = ProcessCommandExecutor(
                    executor_id="local.demo.generate", timeout_seconds=15,
                    command_args=[ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                                  "-i", "testsrc2=size=1024x576:rate=24", "-t", "{duration}",
                                  "-threads", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "{output}"],
                )
                registry.register(worker)
                result = PlanExecutor(root, registry=registry, executor_override=worker.executor_id).execute_plan(plan)
                if not result["success"]:
                    raise JobMediaError("DEMO_EXECUTION_FAILED")
            verified = verify_job(root)
            if verified["evaluation_status"] != "conforms":
                raise JobMediaError("DEMO_CONFORMANCE_FAILED")
            receipt = _receipt(verified, handoff=handoff)
            write_json(root / "demo-result.json", receipt)
        except BaseException:
            if root.resolve() == root and root.is_dir():
                shutil.rmtree(root)
            raise
    return {**receipt, "reused": False}


def _receipt(verified, *, handoff: bool = False):
    payload = {
        "schema_version": "fsf_demo_result_v1",
        "media_kind": "synthetic",
        **{key: verified[key] for key in ("job_id", "run_id", "plan_digest", "artifact", "sha256",
                                          "contract_digest", "evaluation_status")},
    }
    if handoff:
        payload.update(
            demo_mode="handoff",
            review_authority="synthetic_demo_only_not_for_production",
            synthetic_roles=["synthetic_worker", "synthetic_demo_reviewer"],
            trace_path="demo-handoff-trace.json",
        )
    return payload
