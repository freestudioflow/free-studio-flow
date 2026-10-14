"""Tests for PlanExecutor (FSF Orchestration & QC Gate Closed Loop).

Verifies:
- DAG topological step execution (tools, transforms, QC gates)
- Real media generation, assembly, and conformance QC
- Strict QC Gate: any non-conforming QC status blocks downstream assembly (Fail-Closed)
- Compile-time artifact routing: Remaster planned -> routes to Remaster; if Remaster fails, does NOT fallback to Generate
- Authoritative Resume (5 cycles):
  Cycle 1: Fresh run completes all steps.
  Cycle 2: Unchanged run reuses all steps without losing artifact metadata.
  Cycle 3: Deleted artifact forces re-execution (proves metadata was NOT lost in Cycle 2).
  Cycle 4: Tampered artifact forces re-execution (strict assertion: status == 'completed', NOT reused).
  Cycle 5: Executor config change forces re-execution (binds executor_config_digest).
- Multi-skill artifact chaining: upstream video {input} passed to downstream Remaster {output}
- Transform fail-closed: non-conforming assembly halts execution
- Real CLI execution via fresh subprocess reading `FSF_EXECUTORS_FILE` config (Muse zero-code proof)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pdx_artifact_core import validate_execution_plan

from fsf.cli.main import main
from fsf.conformance.media import (
    DEFAULT_BROLL_EXPECTED,
    build_expected_contract,
    canonical_contract_digest,
)
from fsf.executors.process_cmd import ProcessCommandExecutor
from fsf.executors.registry import ExecutorRegistry
from fsf.intake.compiler import compile_job
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor


def _require_ffmpeg() -> str:
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if not exe:
        pytest.skip("ffmpeg not found on PATH; skipping plan executor tests")
    return exe


def _create_generator_script(tmp_path: Path, ffmpeg: str) -> Path:
    """Creates a script that generates a 1024x576 24fps valid MP4 via ffmpeg matching duration."""
    script = tmp_path / "generate_clip.py"
    script.write_text(
        f"import subprocess, sys\n"
        f"out = sys.argv[1]\n"
        f"dur = sys.argv[2] if len(sys.argv) > 2 else '5.0'\n"
        f"subprocess.run([\n"
        f"    r'{ffmpeg}', '-y', '-f', 'lavfi',\n"
        f"    '-i', 'testsrc=size=1024x576:rate=24',\n"
        f"    '-t', dur,\n"
        f"    '-pix_fmt', 'yuv420p',\n"
        f"    '-c:v', 'libx264',\n"
        f"    '-an', out\n"
        f"], check=True)\n",
        encoding="utf-8",
    )
    return script


def _create_remaster_script(tmp_path: Path, ffmpeg: str) -> Path:
    """Creates a script that consumes {input} and produces {output} for remastering."""
    script = tmp_path / "remaster_clip.py"
    script.write_text(
        f"import subprocess, sys, pathlib\n"
        f"inp = sys.argv[1]\n"
        f"out = sys.argv[2]\n"
        f"assert pathlib.Path(inp).is_file(), f'Input file not found: {{inp}}'\n"
        f"subprocess.run([\n"
        f"    r'{ffmpeg}', '-y', '-i', inp,\n"
        f"    '-vf', 'eq=brightness=0.05:contrast=1.05',\n"
        f"    '-pix_fmt', 'yuv420p',\n"
        f"    '-c:v', 'libx264',\n"
        f"    '-an', out\n"
        f"], check=True)\n",
        encoding="utf-8",
    )
    return script


def _create_dub_script(tmp_path: Path, ffmpeg: str) -> Path:
    """Creates a script that generates a valid WAV audio clip matching duration."""
    script = tmp_path / "dub_clip.py"
    script.write_text(
        f"import subprocess, sys\n"
        f"out = sys.argv[1]\n"
        f"dur = sys.argv[2] if len(sys.argv) > 2 else '5.0'\n"
        f"subprocess.run([\n"
        f"    r'{ffmpeg}', '-y', '-f', 'lavfi',\n"
        f"    '-i', 'sine=frequency=440:duration=' + dur,\n"
        f"    '-t', dur,\n"
        f"    '-c:a', 'pcm_s16le',\n"
        f"    out\n"
        f"], check=True)\n",
        encoding="utf-8",
    )
    return script


def _create_broken_generator_script(tmp_path: Path) -> Path:
    """Creates a script that generates non-media dummy bytes."""
    script = tmp_path / "generate_broken.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_bytes(b'NOT_A_VALID_MP4_CORRUPTED_BYTES')\n",
        encoding="utf-8",
    )
    return script


def test_plan_executor_dag_execution_with_real_media_conforms(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies end-to-end DAG execution: tool -> QC -> transform -> QC with real conforming media."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)

    registry = ExecutorRegistry(load_extras=False)
    gen_executor = ProcessCommandExecutor(
        executor_id="local.test.generator",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(gen_script), "{output}", "{duration}"],
        expected_suffix=".mp4",
    )
    registry.register(gen_executor)

    out_dir = tmp_path / "out_e2e"
    executor = PlanExecutor(out_dir, registry=registry, executor_override="local.test.generator")
    result = executor.execute_plan(plan)

    assert result["success"] is True, f"Execution failed: {result}"
    assert result["failed_count"] == 0
    assert result["blocked_count"] == 0
    assert (out_dir / "assembled_timeline.mp4").is_file()
    assert (out_dir / "assembly_receipt.json").is_file()

    assembled_qc = next(s for sid, s in result["steps"].items() if "qc_assembled" in sid)
    assert assembled_qc["status"] == "completed"

    rep_path = Path(assembled_qc["report_path"])
    rep_data = json.loads(rep_path.read_text(encoding="utf-8"))
    assert rep_data["evaluation_status"] == "conforms"


def test_plan_executor_qc_failure_blocks_downstream_assembly(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies that when a shot's Media QC fails, downstream assembly is BLOCKED (Fail-Closed)."""
    _require_ffmpeg()
    broken_script = _create_broken_generator_script(tmp_path)

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)

    registry = ExecutorRegistry(load_extras=False)
    broken_executor = ProcessCommandExecutor(
        executor_id="local.test.broken",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(broken_script), "{output}"],
        expected_suffix=".mp4",
    )
    registry.register(broken_executor)

    out_dir = tmp_path / "out_blocked"
    executor = PlanExecutor(out_dir, registry=registry)
    result = executor.execute_plan(plan)

    assert result["success"] is False
    assert result["failed_count"] > 0
    assert result["blocked_count"] > 0

    transform_step = next(s for sid, s in result["steps"].items() if s.get("kind") == "transform")
    assert transform_step["status"] == "blocked"
    assert not (out_dir / "assembled_timeline.mp4").exists()


def test_plan_executor_remaster_artifact_routing_and_no_fallback(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies compile-time routing to Remaster and refusal to fallback to Generate upon failure.

    When shape=False (real media) and no step_results are provided, the
    _resolve_assembly_inputs() fail-closed gate must block assembly because no
    producer step has been recorded for the remaster artifacts.  This verifies
    both that Remaster is the routed skill (all artifact_inputs are 'video_remaster')
    and that the gate refuses to proceed without verified producer records.
    """
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, pipeline="p0", enable_assembly=True, shape=False)

    transform_step = next(s for s in plan["steps"] if s["kind"] == "transform")
    artifact_inputs = transform_step["inputs"]["artifact_inputs"]
    for art_in in artifact_inputs:
        assert art_in["role"] == "video_remaster"
        assert ".remaster.mp4" in art_in["artifact"]

    out_dir = tmp_path / "out_remaster_fail"
    out_dir.mkdir(parents=True, exist_ok=True)

    for t in manifest["tasks"]:
        (out_dir / f"{t['id']}.mp4").write_bytes(b"GENERATED_MP4")

    executor = PlanExecutor(out_dir)
    # With no step_results, _resolve_assembly_inputs cannot find any producer for the
    # remaster artifacts.  The gate must return blocked (not raise FileNotFoundError).
    res = executor._execute_transform_step(transform_step)
    assert res["status"] == "blocked", (
        f"Assembly must be blocked when no producer found for remaster artifacts: {res}"
    )
    assert "producer" in (res.get("error") or "").lower() or "resolve" in (res.get("error") or "").lower(), (
        f"Blocked error should mention unresolved producer: {res.get('error')}"
    )


def test_plan_executor_authoritative_resume_multicycle(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies authoritative resume over multiple cycles with strict assertions against drift."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)

    registry = ExecutorRegistry(load_extras=False)
    gen_executor = ProcessCommandExecutor(
        executor_id="local.test.generator",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(gen_script), "{output}", "{duration}"],
        expected_suffix=".mp4",
    )
    registry.register(gen_executor)

    alt_gen_executor = ProcessCommandExecutor(
        executor_id="local.test.generator_alt",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(gen_script), "{output}", "{duration}"],
        expected_suffix=".mp4",
    )
    registry.register(alt_gen_executor)

    out_dir = tmp_path / "out_resume_cycles"
    executor = PlanExecutor(out_dir, registry=registry, executor_override="local.test.generator")

    # Cycle 1: First run - all steps complete fresh
    res1 = executor.execute_plan(plan)
    assert res1["success"] is True
    assert res1["completed_count"] > 0
    assert res1["reused_count"] == 0

    # Cycle 2: Second run without changes - all steps reused
    res2 = executor.execute_plan(plan)
    assert res2["success"] is True
    assert res2["reused_count"] > 0
    assert res2["completed_count"] == 0

    first_shot_id = manifest["tasks"][0]["id"]
    first_shot_file = out_dir / f"{first_shot_id}.mp4"

    # Cycle 3: Delete output file after Cycle 2 reuse
    # If journal lost output files in Cycle 2, it would wrongly report reused here.
    first_shot_file.unlink()
    res3 = executor.execute_plan(plan)
    assert res3["success"] is True
    # First shot must NOT be reused; it had to re-execute!
    assert res3["steps"][f"step_001_{first_shot_id}_generate"]["status"] == "completed"
    assert res3["steps"][f"step_001_{first_shot_id}_generate"]["status"] != "reused"

    # Cycle 4: Tamper with output file content
    first_shot_file.write_bytes(b"TAMPERED_CONTENT_TEST")
    res4 = executor.execute_plan(plan)
    assert res4["success"] is True
    assert res4["steps"][f"step_001_{first_shot_id}_generate"]["status"] == "completed"
    assert res4["steps"][f"step_001_{first_shot_id}_generate"]["status"] != "reused"

    # Cycle 5: Switch executor from local.test.generator to local.test.generator_alt
    # Executor config digest mismatch must force re-execution of all generation steps
    executor_alt = PlanExecutor(out_dir, registry=registry, executor_override="local.test.generator_alt")
    res5 = executor_alt.execute_plan(plan)
    assert res5["success"] is True
    for t in manifest["tasks"]:
        step_name = f"step_001_{t['id']}_generate" if t['id'] == "task_001" else f"step_003_{t['id']}_generate" if t['id'] == "task_002" else f"step_005_{t['id']}_generate"
        if step_name in res5["steps"]:
            assert res5["steps"][step_name]["status"] == "completed"
            assert res5["steps"][step_name]["status"] != "reused"


def test_multiskill_pipeline_with_remaster_and_input_artifact_chaining(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies multi-skill tool execution where Remaster receives {input} from Generate."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)
    remaster_script = _create_remaster_script(tmp_path, ffmpeg)

    manifest, _, _ = compile_job(sample_docx)
    # Pipeline with Generate and Remaster
    plan = build_execution_plan(manifest, skills=["generate", "remaster"], enable_assembly=True, shape=False)

    registry = ExecutorRegistry(load_extras=False)
    gen_executor = ProcessCommandExecutor(
        executor_id="local.tool.generator",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(gen_script), "{output}", "{duration}"],
        expected_suffix=".mp4",
    )
    remaster_executor = ProcessCommandExecutor(
        executor_id="local.tool.remaster",
        tool_name="studio.video.remaster",
        command_args=[sys.executable, str(remaster_script), "{input}", "{output}"],
        expected_suffix=".remaster.mp4",
    )
    registry.register(gen_executor)
    registry.register(remaster_executor)

    out_dir = tmp_path / "out_multiskill"
    executor = PlanExecutor(
        out_dir,
        registry=registry,
        executor_override={
            "studio.video.generate": "local.tool.generator",
            "studio.video.remaster": "local.tool.remaster",
        },
    )
    result = executor.execute_plan(plan)

    assert result["success"] is True, f"Multi-skill execution failed: {result}"
    for t in manifest["tasks"]:
        sid = t["id"]
        assert (out_dir / f"{sid}.mp4").is_file()
        assert (out_dir / f"{sid}.remaster.mp4").is_file()
    assert (out_dir / "assembled_timeline.mp4").is_file()


def test_transform_fails_closed_when_assembly_conformance_fails(sample_docx: Path, tmp_path: Path) -> None:
    """Verifies that if assembled media does not conform, the transform step fails closed."""
    import hashlib as _hashlib
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)

    transform_step = next(s for s in plan["steps"] if s["kind"] == "transform")
    # Only include actual generate steps (not QC/conform steps) as producers
    generate_steps = [
        s for s in plan["steps"]
        if s["kind"] != "transform" and "generate" in s.get("tool", "")
    ]

    out_dir = tmp_path / "out_fail_assembly"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate valid short clips (1.0s each) which ffprobe can parse cleanly,
    # but the compiled expected contract expects total ~20s duration.
    for t in manifest["tasks"]:
        subprocess.run(
            [sys.executable, str(gen_script), str(out_dir / f"{t['id']}.mp4"), "1.0"],
            check=True,
        )

    # Build step_results so _resolve_assembly_inputs can find each producer.
    # Map each generate step to its corresponding .mp4 output_file.
    step_results: dict[str, Any] = {}
    for gs in generate_steps:
        gid = gs["id"]
        shot_id = gs.get("inputs", {}).get("shot_id", "")
        mp4_path = out_dir / f"{shot_id}.mp4" if shot_id else None
        if mp4_path and mp4_path.is_file():
            sha = _hashlib.sha256(mp4_path.read_bytes()).hexdigest()
            rel = str(mp4_path.relative_to(out_dir)).replace("\\", "/")
            step_results[gid] = {
                "id": gid,
                "status": "completed",
                "review_required": False,
                "output_files": [{"path": rel, "sha256": sha}],
            }

    executor = PlanExecutor(out_dir)
    res = executor._execute_transform_step(transform_step, step_results=step_results)
    assert res["status"] == "failed", (
        f"Assembly should fail closed on conformance error, got: {res.get('status')!r}: {res.get('error')}"
    )
    assert "failed media technical conformance" in res["error"]


def test_cli_execute_with_config_file_in_fresh_subprocess(sample_docx: Path, tmp_path: Path) -> None:
    """Proves Muse deployment: fresh CLI subprocess reads FSF_EXECUTORS_FILE config without in-memory code."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)

    # Create operator executors configuration file (fsf-executors.json)
    cfg_file = tmp_path / "fsf-executors.json"
    cfg_payload = {
        "schema_version": "fsf_executors_v1",
        "executors": [
            {
                "executor_id": "local.muse.video",
                "transport": "local",
                "tool_name": "studio.video.generate",
                "kind": "process",
                "command_args": [sys.executable, str(gen_script), "{output}", "{duration}"],
                "expected_suffix": ".mp4"
            }
        ]
    }
    cfg_file.write_text(json.dumps(cfg_payload, indent=2), encoding="utf-8")

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)

    plan_file = tmp_path / "muse_plan.json"
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    out_dir = tmp_path / "out_muse_cli"

    env = os.environ.copy()
    env["FSF_EXECUTORS_FILE"] = str(cfg_file)

    # Launch FRESH python process via CLI module
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "execute",
            str(plan_file),
            "--out",
            str(out_dir),
            "--executor",
            "local.muse.video",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"CLI execution failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert (out_dir / "assembled_timeline.mp4").is_file()
    assert (out_dir / "assembly_receipt.json").is_file()
    assert (out_dir / "fsf_execution_journal.json").is_file()


def test_plan_executor_e2e_short_film_with_subtitles_and_audio_mix(sample_docx: Path, tmp_path: Path) -> None:
    """Increment 2 E2E: Full short film flow with video generation, subtitles, BGM mix, and QC."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)

    bgm_file = tmp_path / "soundtrack.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=20.0",
            "-c:a",
            "pcm_s16le",
            str(bgm_file),
        ],
        check=True,
        capture_output=True,
    )

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(
        manifest,
        enable_assembly=True,
        shape=False,
        subtitle_policy="sidecar",
        bgm_artifact=str(bgm_file),
    )

    registry = ExecutorRegistry(load_extras=False)
    gen_executor = ProcessCommandExecutor(
        executor_id="local.test.generator",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(gen_script), "{output}", "{duration}"],
        expected_suffix=".mp4",
    )
    registry.register(gen_executor)

    out_dir = tmp_path / "out_e2e_film"
    executor = PlanExecutor(out_dir, registry=registry, executor_override="local.test.generator")
    result = executor.execute_plan(plan)

    assert result["success"] is True, f"Execution failed: {result}"
    assert result["failed_count"] == 0
    assert result["blocked_count"] == 0

    assembled_mp4 = out_dir / "assembled_timeline.mp4"
    assembled_srt = out_dir / "assembled_timeline.srt"
    receipt_json = out_dir / "assembly_receipt.json"

    assert assembled_mp4.is_file()
    assert assembled_srt.is_file()
    assert receipt_json.is_file()

    receipt_data = json.loads(receipt_json.read_text(encoding="utf-8"))
    assert receipt_data["subtitle_policy"] == "sidecar"
    assert receipt_data["subtitle_scheduling"] == "coarse"
    assert receipt_data["subtitle_sha256"] is not None
    assert receipt_data["bgm_sha256"] is not None
    assert receipt_data["conformance_report"]["evaluation_status"] == "conforms"


def test_plan_executor_generate_dub_assembly_bgm_qc_and_resume_invalidation(sample_docx: Path, tmp_path: Path) -> None:
    """Config-driven E2E: Generate -> Dub -> Assembly+BGM -> QC, then mutate Dub -> verify downstream re-run."""
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)
    dub_script = _create_dub_script(tmp_path, ffmpeg)

    # 1. Background music track
    bgm_file = tmp_path / "global_bgm.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=25.0",
            "-c:a",
            "pcm_s16le",
            str(bgm_file),
        ],
        check=True,
        capture_output=True,
    )

    # 2. Config file with both ProcessCommandExecutors (local.tool.video, local.tool.dub)
    cfg_file = tmp_path / "fsf-executors.json"
    cfg_payload = {
        "schema_version": "fsf_executors_v1",
        "executors": [
            {
                "executor_id": "local.tool.generator",
                "transport": "local",
                "tool_name": "studio.video.generate",
                "kind": "process",
                "command_args": [sys.executable, str(gen_script), "{output}", "{duration}"],
                "expected_suffix": ".mp4"
            },
            {
                "executor_id": "local.tool.dub",
                "transport": "local",
                "tool_name": "studio.voice.dub",
                "kind": "process",
                "command_args": [sys.executable, str(dub_script), "{output}", "{duration}"],
                "expected_suffix": ".dub.wav"
            }
        ]
    }
    cfg_file.write_text(json.dumps(cfg_payload, indent=2), encoding="utf-8")

    # 3. Compile job and build plan with generate + dub and assembly
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(
        manifest,
        skills=["generate", "dub"],
        enable_assembly=True,
        bgm_artifact=str(bgm_file),
        shape=False,
    )

    # Verify plan compiles explicit audio routing and contracts
    transform_step = next(s for s in plan["steps"] if s["kind"] == "transform")
    for art_in in transform_step["inputs"]["artifact_inputs"]:
        sid = art_in["shot_id"]
        assert art_in["audio_artifact"] == f"artifact:{sid}.dub.wav"
    assert transform_step["inputs"]["expected_media_contract"] is not None
    assert transform_step["inputs"]["expected_media_contract_digest"] is not None
    assert transform_step["inputs"]["audio_policy"] == "keep"
    assert transform_step["inputs"]["expected_media_contract"]["audio"] == "required"

    assembled_qc = next(s for s in plan["steps"] if s["id"] == "step_011_qc_assembled_timeline")
    assert assembled_qc["inputs"]["expected_media_contract_digest"] == transform_step["inputs"]["expected_media_contract_digest"]

    # 4. Cycle 1: First full run
    out_dir = tmp_path / "out_generate_dub_e2e"
    registry = ExecutorRegistry(load_extras=False)
    registry.load_extras(cfg_file)

    executor = PlanExecutor(
        out_dir,
        registry=registry,
        executor_override={
            "studio.video.generate": "local.tool.generator",
            "studio.voice.dub": "local.tool.dub",
        },
    )

    res1 = executor.execute_plan(plan)
    assert res1["success"] is True, f"Cycle 1 failed: {res1}"
    assert res1["completed_count"] > 0
    assert res1["reused_count"] == 0

    first_shot_id = manifest["tasks"][0]["id"]
    assert (out_dir / f"{first_shot_id}.mp4").is_file()
    assert (out_dir / f"{first_shot_id}.dub.wav").is_file()
    assert (out_dir / "assembled_timeline.mp4").is_file()
    assert (out_dir / "assembly_receipt.json").is_file()

    # 5. Cycle 2: Unchanged run -> all steps reused
    res2 = executor.execute_plan(plan)
    assert res2["success"] is True
    assert res2["reused_count"] > 0
    assert res2["completed_count"] == 0

    # 6. Mutate Dub audio for first shot (s1.dub.wav)
    first_dub_file = out_dir / f"{first_shot_id}.dub.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=5.0",
            "-t",
            "5.0",
            "-c:a",
            "pcm_s16le",
            str(first_dub_file),
        ],
        check=True,
    )

    # 7. Cycle 3: Resume with Dub modified
    # Must re-run task_001_dub, assembly_concat, and qc_assembled_timeline.
    # Must reuse task_001_generate and all task_002 / task_003 steps!
    res3 = executor.execute_plan(plan)
    assert res3["success"] is True, f"Cycle 3 failed: {res3}"

    # Generate step was untouched -> reused!
    assert res3["steps"][f"step_001_{first_shot_id}_generate"]["status"] == "reused"

    # Dub step was modified -> detected SHA mismatch -> re-executed (completed)!
    assert res3["steps"][f"step_003_{first_shot_id}_dub"]["status"] == "completed"

    # Assembly transform step had inputs_digest change due to dub SHA change -> re-executed (completed)!
    assert res3["steps"]["step_010_assembly_concat"]["status"] == "completed"

    # QC step also re-executed
    assert res3["steps"]["step_011_qc_assembled_timeline"]["status"] == "completed"

    # Other shot was unaffected -> reused!
    second_shot_id = manifest["tasks"][1]["id"]
    assert res3["steps"][f"step_004_{second_shot_id}_generate"]["status"] == "reused"
    assert res3["steps"][f"step_006_{second_shot_id}_dub"]["status"] == "reused"


def test_cli_subprocess_full_pipeline_media_plan_execute_and_resume(
    sample_docx: Path, tmp_path: Path
) -> None:
    """Rigorous end-to-end CLI validation entirely through fresh subprocesses:
    CLI intake -> CLI plan --media -> FSF_EXECUTORS_FILE config loading -> CLI execute -> Generate+Dub+BGM+QC -> CLI execute resume.
    No plan JSON manual modification, no Python API path patching.
    """
    ffmpeg = _require_ffmpeg()
    gen_script = _create_generator_script(tmp_path, ffmpeg)
    dub_script = _create_dub_script(tmp_path, ffmpeg)

    # 1. Background Music artifact (880Hz sine wave)
    bgm_file = tmp_path / "global_bgm.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=20.0",
            "-c:a",
            "pcm_s16le",
            str(bgm_file),
        ],
        check=True,
        capture_output=True,
    )

    # 2. Config file registering process executors for Generate and Dub
    cfg_file = tmp_path / "fsf-executors.json"
    cfg_payload = {
        "schema_version": "fsf_executors_v1",
        "executors": [
            {
                "executor_id": "local.tool.generator",
                "transport": "local",
                "tool_name": "studio.video.generate",
                "kind": "process",
                "command_args": [sys.executable, str(gen_script), "{output}", "{duration}"],
                "expected_suffix": ".mp4",
            },
            {
                "executor_id": "local.tool.dub",
                "transport": "local",
                "tool_name": "studio.voice.dub",
                "kind": "process",
                "command_args": [sys.executable, str(dub_script), "{output}", "{duration}"],
                "expected_suffix": ".dub.wav",
            },
        ],
    }
    cfg_file.write_text(json.dumps(cfg_payload, indent=2), encoding="utf-8")

    # Step A: CLI intake via fresh subprocess
    intake_dir = tmp_path / "cli_intake"
    proc_intake = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "intake",
            str(sample_docx),
            "--out",
            str(intake_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc_intake.returncode == 0
    assert (intake_dir / "job_manifest.json").is_file()

    # Step B: CLI plan --media via fresh subprocess
    plan_file = tmp_path / "cli_media_plan.json"
    proc_plan = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "plan",
            str(sample_docx),
            "--skills",
            "generate,dub",
            "--assembly",
            "--bgm",
            str(bgm_file),
            "--media",
            "--out",
            str(plan_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc_plan.returncode == 0
    assert plan_file.is_file()

    # Verify generated plan contents directly (without modifying anything)
    raw_plan = json.loads(plan_file.read_text(encoding="utf-8"))
    first_gen = next(s for s in raw_plan["steps"] if s["tool"] == "studio.video.generate")
    first_dub = next(s for s in raw_plan["steps"] if s["tool"] == "studio.voice.dub")
    transform_step = next(s for s in raw_plan["steps"] if s["kind"] == "transform")

    assert first_gen["outputs"] == ["artifact:task_001.mp4"]
    assert first_dub["outputs"] == ["artifact:task_001.dub.wav"]
    assert transform_step["inputs"]["artifact_inputs"][0]["artifact"] == "artifact:task_001.mp4"
    assert transform_step["inputs"]["artifact_inputs"][0]["audio_artifact"] == "artifact:task_001.dub.wav"
    assert transform_step["inputs"]["bgm_artifact"] == str(bgm_file)

    # Step C: CLI execute via fresh subprocess (Cycle 1: full initial run)
    exec_dir = tmp_path / "cli_exec_out"
    env = os.environ.copy()
    env["FSF_EXECUTORS_FILE"] = str(cfg_file)

    proc_exec_1 = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "execute",
            str(plan_file),
            "--out",
            str(exec_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_exec_1.returncode == 0, f"CLI execute 1 failed:\nSTDOUT: {proc_exec_1.stdout}\nSTDERR: {proc_exec_1.stderr}"

    assembled_mp4 = exec_dir / "assembled_timeline.mp4"
    assembly_receipt = exec_dir / "assembly_receipt.json"
    journal_file = exec_dir / "fsf_execution_journal.json"

    assert assembled_mp4.is_file()
    assert assembly_receipt.is_file()
    assert journal_file.is_file()

    from fsf.assembly.fast_assembly import clip_has_audio
    assert clip_has_audio(assembled_mp4) is True

    journal_1 = json.loads(journal_file.read_text(encoding="utf-8"))
    assert journal_1["schema_version"] == "fsf_plan_execution_journal_v1"
    # All steps completed
    assert all(step_info["status"] == "completed" for step_info in journal_1["steps"].values())

    # Step D: CLI execute resume via fresh subprocess (Cycle 2: zero execution, full resume)
    proc_exec_2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "execute",
            str(plan_file),
            "--out",
            str(exec_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_exec_2.returncode == 0, f"CLI execute 2 failed:\nSTDOUT: {proc_exec_2.stdout}\nSTDERR: {proc_exec_2.stderr}"
    assert "completed 0 steps" in proc_exec_2.stdout
    assert "reused" in proc_exec_2.stdout

    exec_res_2 = json.loads(proc_exec_2.stdout[:proc_exec_2.stdout.find("[FSF Execute]")].strip())
    assert exec_res_2["success"] is True
    assert exec_res_2["completed_count"] == 0
    assert exec_res_2["reused_count"] > 0
    assert all(step_info["status"] == "reused" for step_info in exec_res_2["steps"].values())

    # Step E: Mutate Dub audio for shot 1 and re-run execute via fresh subprocess (Cycle 3)
    first_dub_file = exec_dir / "task_001.dub.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=5.0",
            "-t",
            "5.0",
            "-c:a",
            "pcm_s16le",
            str(first_dub_file),
        ],
        check=True,
        capture_output=True,
    )

    proc_exec_3 = subprocess.run(
        [
            sys.executable,
            "-m",
            "fsf.cli",
            "execute",
            str(plan_file),
            "--out",
            str(exec_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_exec_3.returncode == 0, f"CLI execute 3 failed:\nSTDOUT: {proc_exec_3.stdout}\nSTDERR: {proc_exec_3.stderr}"

    exec_res_3 = json.loads(proc_exec_3.stdout[:proc_exec_3.stdout.find("[FSF Execute]")].strip())
    assert exec_res_3["success"] is True
    # task_001 generate is untouched -> reused
    assert exec_res_3["steps"]["step_001_task_001_generate"]["status"] == "reused"
    # task_001 dub was modified -> completed
    assert exec_res_3["steps"]["step_003_task_001_dub"]["status"] == "completed"
    # assembly transform depends on dub -> completed
    assert exec_res_3["steps"]["step_010_assembly_concat"]["status"] == "completed"
    # assembly qc -> completed
    assert exec_res_3["steps"]["step_011_qc_assembled_timeline"]["status"] == "completed"
    # other shot remained untouched -> reused
    assert exec_res_3["steps"]["step_004_task_002_generate"]["status"] == "reused"
    assert exec_res_3["steps"]["step_006_task_002_dub"]["status"] == "reused"


def test_plan_executor_masks_secrets_in_result_on_step_failure(sample_docx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: verifies that PlanExecutor masks secrets on failure at the output boundary."""
    secret_val = "synth_plan_fail_secret_key_555"
    monkeypatch.setenv("SYNTHETIC_PROCESS_KEY", secret_val)

    script = tmp_path / "leaking_failure.py"
    script.write_text(
        "import os, sys\n"
        "key = os.environ.get('SYNTHETIC_PROCESS_KEY', '')\n"
        "sys.stderr.write(f'Crash with key: {key}\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=False, shape=False)

    registry = ExecutorRegistry(load_extras=False)
    fail_executor = ProcessCommandExecutor(
        executor_id="local.test.fail_leak",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(script), "{output}"],
    )
    registry.register(fail_executor)

    out_dir = tmp_path / "out_leak"
    executor = PlanExecutor(out_dir, registry=registry)
    result = executor.execute_plan(plan)

    assert result["success"] is False
    serialized = json.dumps(result)
    assert secret_val not in serialized, "Synthetic secret leaked in PlanExecutor result payload!"
    assert "[REDACTED_SECRET]" in serialized
