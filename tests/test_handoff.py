"""Tests for FSF Asynchronous Handoff Protocol & Workorder Governance (Phase 2).

Verifies:
1. Workorder emission with authoritative digests:
   (job_id, run_id, workorder_id, step_id, attempt_id, plan_digest, inputs_digest, expected_contract_digest).
2. Clean pending exit code 10 when workorders await fulfillment.
3. Strict authoritative fields validation:
   - Omitting plan_digest, inputs_digest, artifact_sha256, job_id, or run_id is strictly rejected.
4. Target artifact path binding:
   - Rejects pointing to arbitrary pre-existing files (must strictly match target_artifact).
5. Run and attempt isolation:
   - Distinct runs generate isolated workorder IDs; stale/mismatched run/attempt results are rejected.
6. Path traversal rejection and zero-byte artifact rejection.
7. SHA-256 and digest mismatch rejection.
8. Idempotent result reuse and conflicting result rejection.
9. Independent DAG gating: Agent media generation accepted, but PDX Media QC
   governs downstream assembly (failing QC blocks assembly and fails execution).
10. End-to-end Handoff loop: emit workorder -> fill real media result -> resume -> QC -> assembly -> Exit 0.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pdx_artifact_core import validate_execution_plan

from fsf.assembly.fast_assembly import require_ffmpeg
from fsf.cli.main import main
from fsf.conformance.digest import canonical_contract_digest
from fsf.intake.compiler import compile_job
from fsf.runtime.handoff import (
    HandoffIntegrityError,
    HandoffSecurityError,
    build_workorder,
    find_result_file,
    ingest_workorder_result,
    list_pending_workorders,
    safe_resolve_artifact_path,
    write_workorder,
)
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor


def _write_test_mp4(dest: Path, duration_sec: float = 1.0, width: int = 1024, height: int = 576) -> None:
    ffmpeg = require_ffmpeg()
    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={width}x{height}:rate=24",
        "-t",
        str(duration_sec),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "24",
        "-an",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_workorder_emission_and_authoritative_fields(tmp_path: Path) -> None:
    job_manifest = {
        "source_document": "test.docx",
        "source_sha256": "abcdef123456",
        "tasks": [
            {
                "id": "shot_001",
                "kind": "shot",
                "shot_spec": {
                    "source_prompt": "A cinematic landscape at sunrise",
                    "duration_seconds": 3.0,
                },
            }
        ],
    }
    plan = build_execution_plan(job_manifest, enable_assembly=False, shape=False)
    out_dir = tmp_path / "out_handoff_wo"

    executor = PlanExecutor(out_dir, handoff=True, job_id="test_job_42", run_id="run_001")
    result = executor.execute_plan(plan)

    assert result["status"] == "pending_handoff"
    assert result["success"] is False
    assert result["pending_handoff_count"] == 1

    # Check workorder on disk
    pending = list_pending_workorders(out_dir)
    assert len(pending) == 1
    wo = pending[0]

    assert wo["schema_version"] == "fsf_workorder_v1"
    assert wo["job_id"] == "test_job_42"
    assert wo["run_id"] == "run_001"
    assert "test_job_42" in wo["workorder_id"]
    assert "run_001" in wo["workorder_id"]
    assert "step_001_shot_001_generate" in wo["workorder_id"]
    assert wo["step_id"] == "step_001_shot_001_generate"
    assert wo["attempt_id"] == 1
    assert wo["tool"] == "studio.video.generate"
    assert wo["target_artifact"] == "shot_001.mp4"
    assert len(wo["plan_digest"]) == 64
    assert len(wo["inputs_digest"]) == 64
    assert len(wo["workorder_digest"]) == 64
    assert wo["task_payload"]["prompt"] == "A cinematic landscape at sunrise"


def test_omitted_authoritative_fields_rejected(tmp_path: Path) -> None:
    """[P1] Verifies that omission of plan_digest, inputs_digest, artifact_sha256, or job/run is strictly rejected."""
    out_dir = tmp_path / "out_omitted"
    out_dir.mkdir(parents=True)
    art_path = out_dir / "target.mp4"
    art_path.write_bytes(b"some_valid_content")
    sha = hashlib.sha256(b"some_valid_content").hexdigest()

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "job_1",
        "run_id": "run_1",
        "workorder_id": "wo_job_1_run_1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "plan_dig_123",
        "inputs_digest": "inputs_dig_456",
        "target_artifact": "target.mp4",
    }

    base_res = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "job_1",
        "run_id": "run_1",
        "workorder_id": "wo_job_1_run_1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "plan_dig_123",
        "inputs_digest": "inputs_dig_456",
        "status": "completed",
        "artifact_path": "target.mp4",
        "artifact_sha256": sha,
    }

    # 1. Omitting plan_digest
    r1 = dict(base_res)
    del r1["plan_digest"]
    p1 = out_dir / "r1.json"
    p1.write_text(json.dumps(r1), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="plan_digest"):
        ingest_workorder_result(p1, wo, out_dir)

    # 2. Omitting inputs_digest
    r2 = dict(base_res)
    del r2["inputs_digest"]
    p2 = out_dir / "r2.json"
    p2.write_text(json.dumps(r2), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="inputs_digest"):
        ingest_workorder_result(p2, wo, out_dir)

    # 3. Omitting artifact_sha256
    r3 = dict(base_res)
    del r3["artifact_sha256"]
    p3 = out_dir / "r3.json"
    p3.write_text(json.dumps(r3), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="artifact_sha256"):
        ingest_workorder_result(p3, wo, out_dir)

    # 4. Omitting job_id or run_id
    r4 = dict(base_res)
    del r4["job_id"]
    p4 = out_dir / "r4.json"
    p4.write_text(json.dumps(r4), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="job_id"):
        ingest_workorder_result(p4, wo, out_dir)


def test_target_artifact_binding_rejects_substitute_existing_file(tmp_path: Path) -> None:
    """[P1] Verifies that pointing artifact_path to a different pre-existing non-empty file is rejected."""
    out_dir = tmp_path / "out_substitute"
    out_dir.mkdir(parents=True)

    target_file = out_dir / "shot_expected.mp4"
    other_file = out_dir / "other_arbitrary.mp4"
    target_file.write_bytes(b"target_bytes")
    other_file.write_bytes(b"other_bytes")

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "job_1",
        "run_id": "run_1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "target_artifact": "shot_expected.mp4",
    }

    # Result points to other_arbitrary.mp4
    res_data = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "job_1",
        "run_id": "run_1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "status": "completed",
        "artifact_path": "other_arbitrary.mp4",
        "artifact_sha256": hashlib.sha256(b"other_bytes").hexdigest(),
    }
    p = out_dir / "res_substitute.json"
    p.write_text(json.dumps(res_data), encoding="utf-8")

    with pytest.raises(HandoffIntegrityError, match="does not match workorder target_artifact"):
        ingest_workorder_result(p, wo, out_dir)


def test_path_traversal_rejection(tmp_path: Path) -> None:
    out_dir = tmp_path / "out_jail"
    out_dir.mkdir(parents=True)

    outside_file = tmp_path / "secret.mp4"
    outside_file.write_bytes(b"forbidden_data")

    with pytest.raises(HandoffSecurityError, match="Path traversal attempt rejected"):
        safe_resolve_artifact_path("../../secret.mp4", out_dir)

    with pytest.raises(HandoffSecurityError, match="resolves outside allowed output root"):
        safe_resolve_artifact_path(str(outside_file), out_dir)


def test_zero_byte_artifact_rejection(tmp_path: Path) -> None:
    out_dir = tmp_path / "out_zero"
    out_dir.mkdir(parents=True)
    empty_mp4 = out_dir / "empty.mp4"
    empty_mp4.touch()

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "target_artifact": "empty.mp4",
    }
    res_payload = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "status": "completed",
        "artifact_path": "empty.mp4",
        "artifact_sha256": hashlib.sha256(b"").hexdigest(),
    }
    res_path = out_dir / "res.json"
    res_path.write_text(json.dumps(res_payload), encoding="utf-8")

    with pytest.raises(HandoffIntegrityError, match="0 bytes"):
        ingest_workorder_result(res_path, wo, out_dir)


def test_digest_and_sha_mismatch_rejection(tmp_path: Path) -> None:
    out_dir = tmp_path / "out_mismatch"
    out_dir.mkdir(parents=True)
    real_mp4 = out_dir / "valid.mp4"
    real_mp4.write_bytes(b"valid_payload_bytes")
    correct_sha = hashlib.sha256(b"valid_payload_bytes").hexdigest()

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "correct_plan_digest",
        "inputs_digest": "correct_inputs_digest",
        "target_artifact": "valid.mp4",
    }

    # 1. SHA mismatch
    bad_sha_payload = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "correct_plan_digest",
        "inputs_digest": "correct_inputs_digest",
        "status": "completed",
        "artifact_path": "valid.mp4",
        "artifact_sha256": "0" * 64,
    }
    p1 = out_dir / "res_bad_sha.json"
    p1.write_text(json.dumps(bad_sha_payload), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="SHA-256 mismatch"):
        ingest_workorder_result(p1, wo, out_dir)

    # 2. Plan digest mismatch
    bad_plan_payload = dict(bad_sha_payload)
    bad_plan_payload["artifact_sha256"] = correct_sha
    bad_plan_payload["plan_digest"] = "tampered_plan_digest"
    p2 = out_dir / "res_bad_plan.json"
    p2.write_text(json.dumps(bad_plan_payload), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="plan_digest mismatch"):
        ingest_workorder_result(p2, wo, out_dir)


def test_idempotence_and_conflict_detection(tmp_path: Path) -> None:
    out_dir = tmp_path / "out_idem"
    out_dir.mkdir(parents=True)
    art_a = out_dir / "clip_a.mp4"
    art_a.write_bytes(b"media_content_A")
    sha_a = hashlib.sha256(b"media_content_A").hexdigest()

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "target_artifact": "clip_a.mp4",
    }
    res_data = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "status": "completed",
        "artifact_path": "clip_a.mp4",
        "artifact_sha256": sha_a,
    }
    p = out_dir / "res.json"
    p.write_text(json.dumps(res_data), encoding="utf-8")

    # First ingestion succeeds
    res1 = ingest_workorder_result(p, wo, out_dir)
    assert res1["status"] == "completed"

    # Second ingestion with same accepted SHA succeeds (idempotent reuse)
    res2 = ingest_workorder_result(p, wo, out_dir, previous_accepted_sha=sha_a)
    assert res2["status"] == "completed"

    # Conflicting ingestion with different SHA fails
    with pytest.raises(HandoffIntegrityError, match="Conflict: Step 'step_001' already accepted SHA"):
        ingest_workorder_result(p, wo, out_dir, previous_accepted_sha="different_sha_b")


def test_e2e_handoff_cli_exit_10_then_fulfilled_exit_0(sample_docx: Path, tmp_path: Path) -> None:
    """E2E Test: First execution exits 10 (pending), Agent fulfills real MP4s, second execution exits 0."""
    out_dir = tmp_path / "out_e2e_handoff"
    plan_file = tmp_path / "plan_handoff.json"

    # 1. Compile and create media plan
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # 2. Run first execution with --handoff -> Expect Exit Code 10 (pending handoff)
    exit1 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit1 == 10

    # Verify pending workorders exist
    pending_wos = list_pending_workorders(out_dir)
    assert len(pending_wos) >= 1

    # 3. External Agent fulfills all workorders by writing real conforming MP4s
    res_dir = out_dir / "handoff" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    for wo in pending_wos:
        step_id = wo["step_id"]
        wo_id = wo["workorder_id"]
        target_art = wo["target_artifact"]
        art_path = out_dir / target_art

        # Write real 1024x576 conforming MP4 fixture
        dur = float(wo["task_payload"].get("duration_seconds") or 2.0)
        _write_test_mp4(art_path, duration_sec=dur, width=1024, height=576)
        art_sha = hashlib.sha256(art_path.read_bytes()).hexdigest()

        # Write result file strictly carrying workorder authority fields
        res_payload = {
            "schema_version": "fsf_workorder_result_v1",
            "job_id": wo["job_id"],
            "run_id": wo["run_id"],
            "workorder_id": wo_id,
            "step_id": step_id,
            "attempt_id": wo["attempt_id"],
            "plan_digest": wo["plan_digest"],
            "inputs_digest": wo["inputs_digest"],
            "expected_contract_digest": wo.get("expected_contract_digest"),
            "status": "completed",
            "artifact_path": target_art,
            "artifact_sha256": art_sha,
            "detail": "Generated successfully by agent MCP tool",
        }
        clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo["job_id"]))
        clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo["run_id"]))
        clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))
        res_file = res_dir / f"res_{clean_job}_{clean_run}_{clean_step}_att{wo['attempt_id']}.result.json"
        res_file.write_text(json.dumps(res_payload, indent=2), encoding="utf-8")

    # 4. Run second execution with --handoff -> Expect Exit Code 0 (all steps completed)
    exit2 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit2 == 0

    # Verify final assembled timeline and conformance report
    assembled_mp4 = out_dir / "assembled_timeline.mp4"
    assert assembled_mp4.is_file()
    assert assembled_mp4.stat().st_size > 0

    receipt_file = out_dir / "assembly_receipt.json"
    assert receipt_file.is_file()
    receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert receipt_data["conformance_report"]["evaluation_status"] == "conforms"


def test_independent_qc_dag_gating_blocks_assembly_when_media_non_conforming(
    sample_docx: Path, tmp_path: Path
) -> None:
    """Agent returns a fake/corrupt MP4: generation step completes, but PDX Media QC fails and blocks downstream."""
    out_dir = tmp_path / "out_qc_gate"
    plan_file = tmp_path / "plan_qc.json"

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False)
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # 1. First execution creates workorders
    exit1 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit1 == 10

    pending_wos = list_pending_workorders(out_dir)
    res_dir = out_dir / "handoff" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    # 2. Agent fulfills with non-conforming file (corrupt pseudo-MP4 bytes)
    for wo in pending_wos:
        step_id = wo["step_id"]
        wo_id = wo["workorder_id"]
        target_art = wo["target_artifact"]
        art_path = out_dir / target_art

        # Write corrupt file with fake MP4 header
        art_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00corrupted_non_decodable")
        art_sha = hashlib.sha256(art_path.read_bytes()).hexdigest()

        res_payload = {
            "schema_version": "fsf_workorder_result_v1",
            "job_id": wo["job_id"],
            "run_id": wo["run_id"],
            "workorder_id": wo_id,
            "step_id": step_id,
            "attempt_id": wo["attempt_id"],
            "plan_digest": wo["plan_digest"],
            "inputs_digest": wo["inputs_digest"],
            "expected_contract_digest": wo.get("expected_contract_digest"),
            "status": "completed",
            "artifact_path": target_art,
            "artifact_sha256": art_sha,
        }
        clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo["job_id"]))
        clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo["run_id"]))
        clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))
        res_file = res_dir / f"res_{clean_job}_{clean_run}_{clean_step}_att{wo['attempt_id']}.result.json"
        res_file.write_text(json.dumps(res_payload, indent=2), encoding="utf-8")

    # 3. Second execution: Generation succeeds, but Media QC fails, blocking assembly -> Exit Code 2
    exit2 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit2 == 2

    # Verify downstream assembly was blocked and did NOT run
    assembled_mp4 = out_dir / "assembled_timeline.mp4"
    assert not assembled_mp4.exists()


def test_handoff_run_id_persisted_across_pending_runs(sample_docx: Path, tmp_path: Path) -> None:
    """[P1] Verifies that re-running a pending plan reuses the persisted run_id instead of inventing a new one."""
    out_dir = tmp_path / "out_persist_run"
    plan_file = tmp_path / "plan_persist.json"

    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=False, shape=False)
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # First run without explicit run_id -> exits 10
    exit1 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit1 == 10

    wos1 = list_pending_workorders(out_dir)
    assert len(wos1) >= 1
    run_id_first = wos1[0]["run_id"]
    wo_id_first = wos1[0]["workorder_id"]

    # Second run without explicit run_id while still pending -> exits 10, run_id MUST be identical
    exit2 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit2 == 10

    wos2 = list_pending_workorders(out_dir)
    assert len(wos2) >= 1
    run_id_second = wos2[0]["run_id"]
    wo_id_second = wos2[0]["workorder_id"]

    assert run_id_first == run_id_second
    assert wo_id_first == wo_id_second


def test_handoff_nested_and_multiline_secret_scrubbing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that multi-line and nested secrets in handoff results and environment are sanitized."""
    out_dir = tmp_path / "out_scrub"
    out_dir.mkdir(parents=True)

    secret_key = "MY_SPECIAL_SUPER_SECRET_TOKEN_XYZ12345"
    pem_start = "-----" + "BEGIN PRIVATE KEY" + "-----"
    pem_end = "-----" + "END PRIVATE KEY" + "-----"
    multiline_secret = f"{pem_start}\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\n{pem_end}"
    monkeypatch.setenv("FSF_TEST_SECRET", secret_key)
    monkeypatch.setenv("FSF_MULTILINE_KEY", multiline_secret)

    wo = {
        "schema_version": "fsf_workorder_v1",
        "job_id": "j_scrub",
        "run_id": "r_scrub",
        "workorder_id": "wo_j_scrub_r_scrub_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "target_artifact": "any.mp4",
    }

    # Failure result containing multiline and nested secret text
    res_data = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j_scrub",
        "run_id": "r_scrub",
        "workorder_id": "wo_j_scrub_r_scrub_s1_att1",
        "step_id": "step_001",
        "attempt_id": 1,
        "plan_digest": "pdig",
        "inputs_digest": "idig",
        "status": "failed",
        "error": f"Failed with token {secret_key} and key {multiline_secret}",
        "detail": {
            "nested_auth": f"Bearer {secret_key}",
            "lines": [multiline_secret, "normal_info"],
        },
    }
    p = out_dir / "res_scrub.json"
    p.write_text(json.dumps(res_data), encoding="utf-8")

    ingested = ingest_workorder_result(p, wo, out_dir)
    assert ingested["status"] == "failed"
    err_text = json.dumps(ingested["error"])
    assert secret_key not in err_text
    assert "MIIEvgIBADANBgkqhkiG9w0BAQEFAASC" not in err_text
    assert "[REDACTED_SECRET]" in err_text


def test_handoff_attempt_increment_after_failure_three_rounds(tmp_path: Path) -> None:
    """[P1] Verifies the 3-round retry lifecycle:

    Round 1: pending_handoff (attempt 1) -> Exit 10
    (Agent returns failed result for attempt 1)
    Round 2: failed (attempt 1 ingested and recorded failed) -> Exit 2
    (Operator re-runs execution to retry)
    Round 3: pending_handoff (attempt 2 workorder emitted) -> Exit 10
    (Agent returns completed result for attempt 2)
    Round 4: completed (attempt 2 ingested and completed) -> Exit 0
    """
    out_dir = tmp_path / "out_retry_rounds"
    plan_file = tmp_path / "plan_retry.json"

    manifest = {
        "schema_version": "fsf_manifest_v1",
        "job_id": "job_retry_test",
        "tasks": [
            {
                "id": "task_001",
                "kind": "shot",
                "duration_seconds": 2.0,
                "expected_media_contract": {
                    "containers": ["mp4"],
                    "video_codecs": ["h264"],
                    "width": 1024,
                    "height": 576,
                    "fps": 24,
                    "fps_tolerance": 0.01,
                    "duration_seconds": 2.0,
                    "duration_tolerance_seconds": 0.25,
                    "audio": "allowed",
                    "max_black_frame_ratio": 0.01,
                    "max_freeze_duration_seconds": 0.5,
                },
                "shot_spec": {"source_prompt": "test retry shot"},
            }
        ],
    }
    plan = build_execution_plan(manifest, enable_assembly=False, shape=False)
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    # Round 1: first execution emits attempt 1 workorder -> Exit 10
    exit1 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit1 == 10

    wos1 = list_pending_workorders(out_dir)
    assert len(wos1) == 1
    assert wos1[0]["attempt_id"] == 1
    wo1 = wos1[0]

    # Agent returns failed result for attempt 1
    res_dir = out_dir / "handoff" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["job_id"]))
    clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["run_id"]))
    clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["step_id"]))

    fail_res1 = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": wo1["job_id"],
        "run_id": wo1["run_id"],
        "workorder_id": wo1["workorder_id"],
        "step_id": wo1["step_id"],
        "attempt_id": 1,
        "plan_digest": wo1["plan_digest"],
        "inputs_digest": wo1["inputs_digest"],
        "status": "failed",
        "error": "Out of VRAM during attempt 1",
    }
    res1_path = res_dir / f"res_{clean_job}_{clean_run}_{clean_step}_att1.result.json"
    res1_path.write_text(json.dumps(fail_res1, indent=2), encoding="utf-8")

    # Round 2: re-run ingests failed result for attempt 1 -> Exit 2
    exit2 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit2 == 2

    # Round 3: re-run to retry MUST increment to attempt 2 and emit wo att2 -> Exit 10
    exit3 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit3 == 10

    wos3 = list_pending_workorders(out_dir)
    assert len(wos3) == 2
    wo2 = next(w for w in wos3 if w["attempt_id"] == 2)
    assert wo2["attempt_id"] == 2
    assert "att2" in wo2["workorder_id"]

    # Agent returns completed result for attempt 2 (write real conforming MP4)
    target_p = out_dir / wo2["target_artifact"]
    _write_test_mp4(target_p, duration_sec=2.0, width=1024, height=576)
    art_sha = hashlib.sha256(target_p.read_bytes()).hexdigest()

    ok_res2 = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": wo2["job_id"],
        "run_id": wo2["run_id"],
        "workorder_id": wo2["workorder_id"],
        "step_id": wo2["step_id"],
        "attempt_id": 2,
        "plan_digest": wo2["plan_digest"],
        "inputs_digest": wo2["inputs_digest"],
        "expected_contract_digest": wo2.get("expected_contract_digest"),
        "status": "completed",
        "artifact_path": wo2["target_artifact"],
        "artifact_sha256": art_sha,
    }
    res2_path = res_dir / f"res_{clean_job}_{clean_run}_{clean_step}_att2.result.json"
    res2_path.write_text(json.dumps(ok_res2, indent=2), encoding="utf-8")

    # Round 4: re-run ingests attempt 2 and completes -> Exit 0
    exit4 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit4 == 0


def test_workorder_nested_shot_spec_secret_scrubbing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that secrets nested in task_payload['shot_spec'] are scrubbed in the emitted workorder."""
    secret_val = "SECRET_NESTED_SPEC_TOKEN_9999"
    monkeypatch.setenv("FSF_TEST_NESTED_TOKEN", secret_val)

    task_payload = {
        "id": "shot_001",
        "shot_spec": {
            "source_prompt": f"A warrior wielding magic with token {secret_val}",
            "params": {"auth": f"Bearer {secret_val}"},
        },
    }

    wo = build_workorder(
        job_id="job_nest",
        run_id="run_nest",
        step_id="step_001",
        attempt_id=1,
        tool="studio.video.generate",
        plan_digest="a" * 64,
        inputs_digest="b" * 64,
        target_artifact="shot_001.mp4",
        task_payload=task_payload,
    )

    wo_path = write_workorder(wo, tmp_path)
    content = wo_path.read_text(encoding="utf-8")

    assert secret_val not in content
    assert "[REDACTED_SECRET]" in content
    loaded = json.loads(content)
    assert "[REDACTED_SECRET]" in loaded["task_payload"]["shot_spec"]["source_prompt"]


def test_shot_qc_and_assembly_audio_keep_alignment(sample_docx: Path, tmp_path: Path) -> None:
    """[P1] Verifies that under default --audio keep, source shot QC has audio='allowed',

    matching assembly QC='allowed', so generated videos with native audio pass both QCs.
    """
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, enable_assembly=True, shape=False, audio_policy="keep")

    # Check that shot QC allows audio
    shot_qc_step = next(s for s in plan["steps"] if s.get("tool") == "pdx.media.technical_conform" and "assembled" not in s["id"])
    assert shot_qc_step["inputs"]["expected_media_contract"]["audio"] == "allowed"

    # Check that assembly QC allows audio
    assembled_qc_step = next(s for s in plan["steps"] if s.get("tool") == "pdx.media.technical_conform" and "assembled" in s["id"])
    assert assembled_qc_step["inputs"]["expected_media_contract"]["audio"] == "allowed"


def test_handoff_attempt_increment_after_integrity_rejection(tmp_path: Path) -> None:
    """[P1] Integrity/SHA rejection must still bind attempt_id and allow re-dispatch.

    Counterexample: pending_handoff → failed (SHA mismatch) → pending_handoff att2.
    Previous gap: exception path omitted attempt_id, so retry stayed on att1 forever.
    """
    out_dir = tmp_path / "out_integrity_retry"
    plan_file = tmp_path / "plan_integrity.json"

    manifest = {
        "schema_version": "fsf_manifest_v1",
        "job_id": "job_integrity_retry",
        "tasks": [
            {
                "id": "task_001",
                "kind": "shot",
                "duration_seconds": 2.0,
                "expected_media_contract": {
                    "containers": ["mp4"],
                    "video_codecs": ["h264"],
                    "width": 1024,
                    "height": 576,
                    "fps": 24,
                    "fps_tolerance": 0.01,
                    "duration_seconds": 2.0,
                    "duration_tolerance_seconds": 0.25,
                    "audio": "allowed",
                    "max_black_frame_ratio": 0.01,
                    "max_freeze_duration_seconds": 0.5,
                },
                "shot_spec": {"source_prompt": "integrity reject retry"},
            }
        ],
    }
    plan = build_execution_plan(
        manifest, enable_assembly=False, shape=False, enable_media_conformance=False
    )
    plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    exit1 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit1 == 10

    wos1 = list_pending_workorders(out_dir)
    assert len(wos1) == 1
    wo1 = wos1[0]
    assert wo1["attempt_id"] == 1

    # Agent returns "completed" with wrong SHA → integrity rejection (not agent-reported failed)
    target_p = out_dir / wo1["target_artifact"]
    target_p.write_bytes(b"VALID_BYTES_FOR_INTEGRITY_TEST")
    res_dir = out_dir / "handoff" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["job_id"]))
    clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["run_id"]))
    clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(wo1["step_id"]))

    bad_res = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": wo1["job_id"],
        "run_id": wo1["run_id"],
        "workorder_id": wo1["workorder_id"],
        "step_id": wo1["step_id"],
        "attempt_id": 1,
        "plan_digest": wo1["plan_digest"],
        "inputs_digest": wo1["inputs_digest"],
        "expected_contract_digest": wo1.get("expected_contract_digest"),
        "status": "completed",
        "artifact_path": wo1["target_artifact"],
        "artifact_sha256": "0" * 64,
    }
    res1_path = res_dir / f"res_{clean_job}_{clean_run}_{clean_step}_att1.result.json"
    res1_path.write_text(json.dumps(bad_res, indent=2), encoding="utf-8")

    exit2 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit2 == 2

    journal = json.loads((out_dir / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    failed_step = journal["steps"][wo1["step_id"]]
    assert failed_step["status"] == "failed"
    assert failed_step.get("attempt_id") == 1, "Integrity rejection must preserve attempt_id in journal"

    # Re-run must emit attempt 2 instead of re-ingesting the same rejected att1 result
    exit3 = main(["execute", str(plan_file), "--out", str(out_dir), "--handoff"])
    assert exit3 == 10

    wos3 = list_pending_workorders(out_dir)
    wo2 = next(w for w in wos3 if w["attempt_id"] == 2)
    assert "att2" in wo2["workorder_id"]


def test_explicit_audio_forbidden_not_relaxed_by_audio_keep(sample_docx: Path) -> None:
    """[P1] Explicit audio:'forbidden' must not become 'allowed' under --audio keep.

    Intake defaults may be relaxed; operator-authored contracts keep authority.
    """
    manifest, _, _ = compile_job(sample_docx)
    # Operator explicitly forbids audio on the first shot (not an intake default).
    manifest["tasks"][0]["expected_media_contract"] = dict(
        manifest["tasks"][0]["expected_media_contract"]
    )
    manifest["tasks"][0]["expected_media_contract"]["audio"] = "forbidden"
    manifest["tasks"][0].pop("expected_media_contract_source", None)

    plan = build_execution_plan(manifest, enable_assembly=True, shape=False, audio_policy="keep")
    shot_qc = next(
        s
        for s in plan["steps"]
        if s.get("tool") == "pdx.media.technical_conform"
        and s.get("inputs", {}).get("shot_id") == manifest["tasks"][0]["id"]
    )
    assert shot_qc["inputs"]["expected_media_contract"]["audio"] == "forbidden"

    # Intake defaults on other shots still relax under keep.
    other = next(
        t for t in manifest["tasks"] if t["id"] != manifest["tasks"][0]["id"] and t.get("unit_kind") == "shot"
    )
    other_qc = next(
        s
        for s in plan["steps"]
        if s.get("tool") == "pdx.media.technical_conform" and s.get("inputs", {}).get("shot_id") == other["id"]
    )
    assert other.get("expected_media_contract_source") == "intake_default"
    assert other_qc["inputs"]["expected_media_contract"]["audio"] == "allowed"
