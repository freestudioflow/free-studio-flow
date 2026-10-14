"""Integration tests for Phase D review gate (P1 bypass fixes).

Tests 27-31: full execute_plan() scenarios verifying that:
27. First run: assembly blocked when no review exists (uses in-memory step_results, not journal).
28. Revoke review after run → re-run → assembly not reused.
29. Tampered verdict / cross-job review / unknown verdict → rejected.
30. Non-handoff step with review_required=True + missing output_files → blocked.
31. Modify disk artifact after review → old review invalidated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fsf.conformance.digest import canonical_contract_digest
from fsf.runtime.consistency_review import (
    VALID_VERDICTS,
    build_review_record,
    check_assembly_gate,
    save_reviews,
)


# ── Shared helper ─────────────────────────────────────────────────────────────

_SHOT_STEP_ID = "step_001_S01_generate"
_ASSEMBLY_STEP_ID = "step_002_assembly_concat"


def _build_review_required_job(job_id: str = "d_int") -> dict[str, Any]:
    """Build a 1-shot job manifest with review_required=True on the shot."""
    return {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": job_id,
        "tasks": [
            {
                "id": "S01",
                "unit_kind": "shot",
                "shot_spec": {
                    "generation_prompt": "A hero walks.",
                    "duration_seconds": 5.0,
                    "source_prompt": "A hero walks.",
                    "aspect_ratio": "16:9",
                },
                "consistency_context": {
                    "schema_version": "fsf_consistency_context_v1",
                    "asset_refs": [],
                    "review_required": True,
                    "generation_intent": {
                        "base_prompt": "A hero walks.",
                        "injection_rule_version": "fsf_injection_v1",
                        "asset_injections": [],
                        "effective_prompt": None,  # set by executor at runtime
                    },
                },
            }
        ],
    }


def _build_plan(job: dict[str, Any]) -> dict[str, Any]:
    """Compile job → plan with no media-QC step so shape mode can complete."""
    from fsf.runtime.plan import build_execution_plan
    return build_execution_plan(
        job,
        executor_tool="studio.video.generate",
        shape=True,
        enable_assembly=True,
        enable_media_conformance=False,  # skip QC so shape artifacts don't fail QC
    )


def _minimal_approved_review(
    *,
    step_id: str,
    job_id: str,
    run_id: str,
    workorder_id: str,
    artifact_sha256: str,
    requirements_digest: str | None,
) -> dict[str, Any]:
    return build_review_record(
        step_id=step_id,
        job_id=job_id,
        run_id=run_id,
        workorder_id=workorder_id,
        artifact_sha256=artifact_sha256,
        requirements_digest=requirements_digest,
        acknowledged_digest=None,
        verdict="approved",
        reviewer="tester",
    )


# ── 27. First-run assembly blocked when no review exists ──────────────────────


def test_integration_first_run_assembly_blocked_without_review(tmp_path: Path) -> None:
    """P1-1: The gate must use in-memory step_results (not the on-disk journal).

    On first run the journal file doesn't exist yet.  Without P1-1, reading an
    empty journal would show no shots → gate passes → assembly proceeds without review.
    With the fix, step_results are projected into a live snapshot and the gate
    correctly sees the generate step with review_required=True and no approved review.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("d_int_first")
    plan = _build_plan(job)
    executor = PlanExecutor(tmp_path, job_id="d_int_first", run_id="run01")
    result = executor.execute_plan(plan)

    steps = result.get("steps", {})
    gen_step = steps.get(_SHOT_STEP_ID, {})
    assert gen_step.get("status") in ("completed", "reused"), (
        f"{_SHOT_STEP_ID!r} should complete in shape mode, got status={gen_step.get('status')}.  "
        f"All steps: {list(steps.keys())}"
    )

    assembly = steps.get(_ASSEMBLY_STEP_ID, {})
    assert assembly.get("status") == "blocked", (
        f"Assembly must be blocked (review gate), got status={assembly.get('status')}.  "
        f"error={assembly.get('error')}"
    )
    assert "review" in (assembly.get("error") or "").lower(), (
        f"Assembly blocked but not by review gate: {assembly.get('error')}"
    )


# ── 28. Revoke review → re-run → assembly not reused ─────────────────────────


def test_integration_revoked_review_blocks_assembly_resume(tmp_path: Path) -> None:
    """P1-2: After a review is revoked, the assembly step must NOT be reused.

    The main-loop resume gate checks reviews before allowing a transform step
    to be reused.  Without P1-2, a prior completed assembly would be reused
    even after its review was withdrawn.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("d_int_revoke")
    plan = _build_plan(job)

    # Run 1 — no review → generate completes, assembly blocked
    ex1 = PlanExecutor(tmp_path, job_id="d_int_revoke", run_id="run01")
    r1 = ex1.execute_plan(plan)
    gen_r1 = r1.get("steps", {}).get(_SHOT_STEP_ID, {})
    assert gen_r1.get("status") in ("completed", "reused"), (
        f"Generate step should complete in shape mode: {gen_r1}"
    )

    # Write an approved review so run 2 can assemble
    out_files = gen_r1.get("output_files") or []
    if not out_files:
        pytest.skip("Generate step produced no output_files in shape mode")
    art_sha = out_files[0]["sha256"]
    rec = _minimal_approved_review(
        step_id=_SHOT_STEP_ID,
        job_id="d_int_revoke",
        run_id="run01",
        workorder_id=str(gen_r1.get("workorder_id") or ""),
        artifact_sha256=art_sha,
        requirements_digest=gen_r1.get("requirements_digest"),
    )
    save_reviews({_SHOT_STEP_ID: rec}, tmp_path)

    # Run 2 — approved review present — assembly NOT blocked by review gate
    ex2 = PlanExecutor(tmp_path, job_id="d_int_revoke", run_id="run02")
    r2 = ex2.execute_plan(plan)
    asm_r2 = r2.get("steps", {}).get(_ASSEMBLY_STEP_ID, {})
    assert asm_r2.get("status") != "blocked" or "review" not in (asm_r2.get("error") or "").lower(), (
        "Assembly must not be blocked by review gate when review is approved"
    )

    # REVOKE the review
    save_reviews({}, tmp_path)

    # Run 3 — review revoked → assembly must be blocked, not reused
    ex3 = PlanExecutor(tmp_path, job_id="d_int_revoke", run_id="run03")
    r3 = ex3.execute_plan(plan)
    asm_r3 = r3.get("steps", {}).get(_ASSEMBLY_STEP_ID, {})
    assert asm_r3.get("status") == "blocked", (
        "Assembly must be blocked after review is revoked — even if prior assembly "
        f"existed in the journal (P1-2 resume gate).  Got status={asm_r3.get('status')}"
    )


# ── 29. Tampered verdict / cross-job review / unknown verdict → rejected ──────


def test_integration_tampered_and_cross_job_reviews_rejected(tmp_path: Path) -> None:
    """P1-3: review_digest integrity check, identity check, and unknown-verdict
    check all block the gate.
    """
    req_dig = "r" * 64
    wo_id = "wo_j1_r1_step001_att1"
    artifact_content = b"original-shape-data"
    art_sha = hashlib.sha256(artifact_content).hexdigest()

    # Write the artifact to disk so disk-SHA check passes
    art_rel = f"{_SHOT_STEP_ID}.shape.bin"
    (tmp_path / art_rel).write_bytes(artifact_content)

    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1",
        "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [{"path": art_rel, "sha256": art_sha}],
                "requirements_digest": req_dig,
                "workorder_id": wo_id,
                "review_required": True,
            }
        },
    }

    # (a) Tampered record — change a field without updating review_digest
    rec_ok = _minimal_approved_review(
        step_id=_SHOT_STEP_ID, job_id="j1", run_id="r1",
        workorder_id=wo_id, artifact_sha256=art_sha, requirements_digest=req_dig,
    )
    rec_tampered = dict(rec_ok)
    rec_tampered["notes"] = "OVERRIDE: this is now approved by me"
    # review_digest NOT updated → integrity check fails
    gate_ok_a, reasons_a = check_assembly_gate(journal, {_SHOT_STEP_ID: rec_tampered}, tmp_path)
    assert gate_ok_a is False, "Tampered record must block gate"
    assert any("integrity" in r.lower() or "tamper" in r.lower() for r in reasons_a), reasons_a

    # (b) Cross-job review — correct step_id but wrong job_id
    rec_cross = build_review_record(
        step_id=_SHOT_STEP_ID, job_id="ANOTHER_JOB", run_id="r1",
        workorder_id=wo_id, artifact_sha256=art_sha,
        requirements_digest=req_dig, acknowledged_digest=None,
        verdict="approved", reviewer="attacker",
    )
    gate_ok_b, reasons_b = check_assembly_gate(journal, {_SHOT_STEP_ID: rec_cross}, tmp_path)
    assert gate_ok_b is False, "Cross-job review must block gate"
    assert any("job_id" in r for r in reasons_b), reasons_b

    # (c) Unknown verdict injected after building (bypasses build_review_record validation)
    rec_base = build_review_record(
        step_id=_SHOT_STEP_ID, job_id="j1", run_id="r1",
        workorder_id=wo_id, artifact_sha256=art_sha,
        requirements_digest=req_dig, acknowledged_digest=None,
        verdict="approved", reviewer="test",
    )
    rec_unknown = {k: v for k, v in rec_base.items() if k != "review_digest"}
    rec_unknown["verdict"] = "conditional"
    rec_unknown["review_digest"] = canonical_contract_digest(rec_unknown)
    gate_ok_c, reasons_c = check_assembly_gate(journal, {_SHOT_STEP_ID: rec_unknown}, tmp_path)
    assert gate_ok_c is False, "Unknown verdict must block gate"
    assert any("verdict" in r.lower() for r in reasons_c), reasons_c


# ── 30. review_required + missing output_files → blocked (not skipped) ────────


def test_integration_review_required_missing_output_files_blocks(tmp_path: Path) -> None:
    """P1-4: Missing output_files must cause gate to block, not silently pass."""
    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1",
        "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [],  # empty → required data missing
                "review_required": True,
            }
        },
    }
    gate_ok, reasons = check_assembly_gate(journal, {}, tmp_path)
    assert gate_ok is False, "Missing output_files must block gate"
    assert any(_SHOT_STEP_ID in r for r in reasons)
    assert any("output_files" in r or "artifact" in r.lower() for r in reasons), reasons


# ── 31. Modify disk artifact → old review is invalidated ──────────────────────


def test_integration_modified_disk_artifact_invalidates_review(tmp_path: Path) -> None:
    """P1-4: Disk SHA re-verification detects post-review file edits."""
    art_bytes = b"original-frame-data"
    art_file = tmp_path / f"{_SHOT_STEP_ID}.shape.bin"
    art_file.write_bytes(art_bytes)
    art_sha = hashlib.sha256(art_bytes).hexdigest()

    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1",
        "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [
                    {"path": f"{_SHOT_STEP_ID}.shape.bin", "sha256": art_sha}
                ],
                "requirements_digest": "r" * 64,
                "workorder_id": "wo_j1_r1_step001_att1",
                "review_required": True,
            }
        },
    }

    rec = _minimal_approved_review(
        step_id=_SHOT_STEP_ID, job_id="j1", run_id="r1",
        workorder_id="wo_j1_r1_step001_att1",
        artifact_sha256=art_sha, requirements_digest="r" * 64,
    )
    reviews = {_SHOT_STEP_ID: rec}

    # Gate passes initially
    gate_ok, reasons = check_assembly_gate(journal, reviews, tmp_path)
    assert gate_ok is True, f"Expected gate to pass: {reasons}"

    # Operator edits the artifact file
    art_file.write_bytes(b"edited-frame-data-after-review")

    # Gate must now block — disk SHA changed
    gate_ok2, reasons2 = check_assembly_gate(journal, reviews, tmp_path)
    assert gate_ok2 is False, "Gate must block after artifact is modified on disk"
    assert any("SHA" in r or "sha" in r.lower() for r in reasons2), reasons2


# ═══════════════════════════════════════════════════════════════════════════════
# New counterexample tests (P1-F1, P1-F2, P2-F3 fixes)
# ═══════════════════════════════════════════════════════════════════════════════

# ── 32. Missing file path, deleted file, read failure → gate blocked ──────────


def test_f1_missing_artifact_path_blocks_gate(tmp_path: Path) -> None:
    """P1-F1a: output_files[0].path absent → gate must block."""
    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [{"path": "", "sha256": "a" * 64}],  # empty path
                "review_required": True,
            }
        },
    }
    gate_ok, reasons = check_assembly_gate(journal, {}, tmp_path)
    assert gate_ok is False, "Empty path must block gate"
    assert any("path" in r.lower() or "locate" in r.lower() for r in reasons), reasons


def test_f1_deleted_artifact_blocks_gate(tmp_path: Path) -> None:
    """P1-F1b: artifact file deleted after journal write → gate must block."""
    art_bytes = b"artifact"
    art_sha = hashlib.sha256(art_bytes).hexdigest()
    art_rel = f"{_SHOT_STEP_ID}.shape.bin"
    art_file = tmp_path / art_rel
    art_file.write_bytes(art_bytes)

    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [{"path": art_rel, "sha256": art_sha}],
                "review_required": True,
            }
        },
    }
    rec = build_review_record(
        step_id=_SHOT_STEP_ID, job_id="j1", run_id="r1",
        workorder_id="", artifact_sha256=art_sha,
        requirements_digest=None, acknowledged_digest=None,
        verdict="approved", reviewer="tester",
    )
    # Gate passes while file exists
    gate_ok, _ = check_assembly_gate(journal, {_SHOT_STEP_ID: rec}, tmp_path)
    assert gate_ok is True, "Gate should pass before deletion"

    # Delete the file
    art_file.unlink()

    gate_ok2, reasons2 = check_assembly_gate(journal, {_SHOT_STEP_ID: rec}, tmp_path)
    assert gate_ok2 is False, "Gate must block after artifact file is deleted"
    assert any("not found" in r.lower() or "deleted" in r.lower() or "missing" in r.lower()
               for r in reasons2), reasons2


# ── 33. Non-handoff requirements change invalidates review ────────────────────


def test_f2_nonhandoff_requirements_change_invalidates_review(tmp_path: Path) -> None:
    """P1-F2: When a non-handoff step's consistency requirements change (different
    effective_prompt / asset refs), the requirements_digest changes and the old
    review must be rejected even if the artifact bytes are identical.

    The executor now computes requirements_digest for ALL review_required=True steps,
    not only handoff steps.  This test verifies that:
    - A review built with req_dig_v1 is valid for req_dig_v1.
    - After requirements change (req_dig_v2), the same artifact SHA + old review
      is invalidated.
    """
    art_bytes = b"shape-artifact"
    art_sha = hashlib.sha256(art_bytes).hexdigest()
    art_rel = f"{_SHOT_STEP_ID}.shape.bin"
    (tmp_path / art_rel).write_bytes(art_bytes)

    req_dig_v1 = "r" * 64
    req_dig_v2 = "s" * 64  # requirements changed (new asset ref, new prompt)
    wo_id = "wo_j1_r1_step001_att1"

    journal_v1: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [{"path": art_rel, "sha256": art_sha}],
                "requirements_digest": req_dig_v1,
                "workorder_id": wo_id,
                "review_required": True,
            }
        },
    }
    rec_v1 = build_review_record(
        step_id=_SHOT_STEP_ID, job_id="j1", run_id="r1",
        workorder_id=wo_id, artifact_sha256=art_sha,
        requirements_digest=req_dig_v1, acknowledged_digest=None,
        verdict="approved", reviewer="tester",
    )

    # Gate passes with v1 requirements + matching review
    gate_ok, reasons = check_assembly_gate(journal_v1, {_SHOT_STEP_ID: rec_v1}, tmp_path)
    assert gate_ok is True, f"v1 requirements should pass: {reasons}"

    # Simulate requirements change: same artifact SHA, different requirements_digest
    journal_v2: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            _SHOT_STEP_ID: {
                "status": "completed",
                "output_files": [{"path": art_rel, "sha256": art_sha}],
                "requirements_digest": req_dig_v2,   # changed!
                "workorder_id": wo_id,
                "review_required": True,
            }
        },
    }

    # Old review (built with v1 req_dig) must be rejected for v2 context
    gate_ok2, reasons2 = check_assembly_gate(journal_v2, {_SHOT_STEP_ID: rec_v1}, tmp_path)
    assert gate_ok2 is False, "Old review must be invalidated when requirements change"
    assert any(
        "requirements" in r.lower() or "digest" in r.lower() for r in reasons2
    ), reasons2


# ── 34. assembled_step_artifacts restricts gate to actual inputs ──────────────


def test_f3_gate_checks_only_assembled_steps(tmp_path: Path) -> None:
    """P2-F3: When assembled_step_artifacts is provided, the gate checks only
    the steps listed — not every completed review_required step.

    Scenario: two generate steps (S01, S02) both have review_required=True.
    The assembly only uses S01.  S02 has no review.  Gate must PASS because
    S02 is not being assembled.
    """
    art_bytes_s01 = b"s01-artifact"
    art_sha_s01 = hashlib.sha256(art_bytes_s01).hexdigest()
    art_rel_s01 = "step_001_S01_generate.shape.bin"
    (tmp_path / art_rel_s01).write_bytes(art_bytes_s01)

    art_bytes_s02 = b"s02-artifact"
    art_sha_s02 = hashlib.sha256(art_bytes_s02).hexdigest()
    art_rel_s02 = "step_001_S02_generate.shape.bin"
    (tmp_path / art_rel_s02).write_bytes(art_bytes_s02)

    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            "step_001_S01_generate": {
                "status": "completed",
                "output_files": [{"path": art_rel_s01, "sha256": art_sha_s01}],
                "requirements_digest": "r" * 64,
                "workorder_id": "wo_s01",
                "review_required": True,
            },
            "step_001_S02_generate": {
                "status": "completed",
                "output_files": [{"path": art_rel_s02, "sha256": art_sha_s02}],
                "requirements_digest": "r" * 64,
                "workorder_id": "wo_s02",
                "review_required": True,
            },
        },
    }
    # Only S01 has an approved review; S02 has none
    rec_s01 = build_review_record(
        step_id="step_001_S01_generate", job_id="j1", run_id="r1",
        workorder_id="wo_s01", artifact_sha256=art_sha_s01,
        requirements_digest="r" * 64, acknowledged_digest=None,
        verdict="approved", reviewer="tester",
    )
    reviews = {"step_001_S01_generate": rec_s01}

    # Without scope restriction: S02 missing review → gate blocks
    gate_no_scope, reasons_no_scope = check_assembly_gate(journal, reviews, tmp_path)
    assert gate_no_scope is False, "Without scope, S02 missing review should block"

    # With scope restriction to S01 only: gate must PASS (S02 not in assembly)
    gate_scoped, reasons_scoped = check_assembly_gate(
        journal, reviews, tmp_path,
        assembled_artifacts=[
            {
                "step_id": "step_001_S01_generate",
                "artifact_ref": "step_001_S01_generate.shape.bin",
                "canonical_path": art_rel_s01,
                "sha256": art_sha_s01,
            }
        ],
    )
    assert gate_scoped is True, (
        f"With scope=S01-only, gate should pass (S02 not assembled): {reasons_scoped}"
    )


# ── A4-1. Missing producer / missing SHA must block ───────────────────────────


def test_a4_missing_producer_blocks_assembly(tmp_path: Path) -> None:
    """A4-1: _resolve_assembly_inputs must block when the producer step is absent.

    If an artifact_input declares a shot but no step_results entry matches the
    path or (shot_id, tool_suffix), the resolution must fail with a blocking error.
    Silently dropping the input would shrink gate scope and allow unreviewed shots.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("a4_missing_producer")
    plan = _build_plan(job)

    # Run the plan once so the generate step completes
    ex = PlanExecutor(tmp_path, job_id="a4_missing_producer", run_id="run01")
    result = ex.execute_plan(plan)

    # Assembly is blocked (no review) — confirm generate succeeded
    gen = result["steps"].get(_SHOT_STEP_ID, {})
    assert gen.get("status") in ("completed", "reused"), f"Generate should succeed: {gen}"

    # Manually test _resolve_assembly_inputs with step_results that are MISSING
    # the generate step — simulates the case where a producer record was lost.
    art_inputs = plan["steps"][-1]["inputs"]["artifact_inputs"]
    empty_step_results: dict[str, Any] = {}
    resolved, errors = ex._resolve_assembly_inputs(art_inputs, empty_step_results)

    assert errors, (
        "Must return errors when step_results is empty (no producer found); "
        f"got resolved={resolved}, errors={errors}"
    )
    assert len(resolved) == 0, (
        f"resolved must be empty when no producer is found; got {resolved}"
    )
    # Error message should mention the artifact
    assert any("No producer" in e or "producer" in e.lower() for e in errors), (
        f"Error should mention missing producer: {errors}"
    )


def test_a4_missing_sha_blocks_assembly(tmp_path: Path) -> None:
    """A4-1b: _resolve_assembly_inputs must block when SHA is absent.

    A step_results entry with an empty sha256 field cannot be verified.
    The gate must block — not silently skip or use an empty SHA.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("a4_missing_sha")
    plan = _build_plan(job)
    ex = PlanExecutor(tmp_path, job_id="a4_missing_sha", run_id="run01")

    # Build step_results with the generate step but with empty SHA
    step_results_no_sha: dict[str, Any] = {
        _SHOT_STEP_ID: {
            "id": _SHOT_STEP_ID,
            "status": "completed",
            "review_required": True,
            "output_files": [{"path": "S01.shape.bin", "sha256": ""}],
        }
    }

    art_inputs = plan["steps"][-1]["inputs"]["artifact_inputs"]
    resolved, errors = ex._resolve_assembly_inputs(art_inputs, step_results_no_sha)

    assert errors, (
        "Must return errors when sha256 is empty; "
        f"got resolved={resolved}, errors={errors}"
    )
    assert len(resolved) == 0, (
        f"resolved must be empty when SHA is absent; got {resolved}"
    )
    # The error should reference the missing SHA
    assert any("SHA" in e or "sha" in e.lower() for e in errors), (
        f"Error should mention missing SHA: {errors}"
    )


# ── A4-2. Same step producing multiple assembled outputs ──────────────────────


def test_a4_same_step_multiple_inputs_both_verified(tmp_path: Path) -> None:
    """A4-2: A step that produces multiple artifacts consumed by assembly must
    have ALL of them verified independently — not just the first/last.

    Previously the dict-based step_id→SHA mapping would overwrite earlier
    entries with later ones (last write wins), causing only one artifact to
    be checked.  The new list-based structure keeps each entry separate.
    """
    # Create two distinct artifacts from the same logical step (different roles)
    bytes_a = b"artifact-A-content"
    bytes_b = b"artifact-B-content"
    sha_a = hashlib.sha256(bytes_a).hexdigest()
    sha_b = hashlib.sha256(bytes_b).hexdigest()
    assert sha_a != sha_b  # sanity — they must be distinct

    (tmp_path / "S01_a.shape.bin").write_bytes(bytes_a)
    (tmp_path / "S01_b.shape.bin").write_bytes(bytes_b)

    step_id = "step_001_S01_generate"

    # assembled_artifacts: two entries, both from the same step_id
    assembled_artifacts = [
        {
            "step_id": step_id,
            "artifact_ref": "artifact:S01_a.shape.bin",
            "canonical_path": "S01_a.shape.bin",
            "sha256": sha_a,
        },
        {
            "step_id": step_id,
            "artifact_ref": "artifact:S01_b.shape.bin",
            "canonical_path": "S01_b.shape.bin",
            "sha256": sha_b,
        },
    ]

    req_dig = "req_" + "d" * 60
    wo_id = "wo_s01"
    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            step_id: {
                "status": "completed",
                "review_required": True,
                "requirements_digest": req_dig,
                "workorder_id": wo_id,
            },
        },
    }

    # A review that covers artifact A — should pass check for A
    rec_a = build_review_record(
        step_id=step_id, job_id="j1", run_id="r1",
        workorder_id=wo_id, artifact_sha256=sha_a,
        requirements_digest=req_dig, acknowledged_digest=None,
        verdict="approved", reviewer="tester",
    )

    # Case 1: review for A exists — A passes, B is still pending (no review for B)
    gate_a, reasons_a = check_assembly_gate(
        journal, {step_id: rec_a}, tmp_path,
        assembled_artifacts=assembled_artifacts,
    )
    # B has no matching review (different SHA) → gate must block
    assert gate_a is False, (
        f"Gate must block because artifact B has no review; got gate={gate_a}, reasons={reasons_a}"
    )
    # Both items should be evaluated; at least B should appear as missing/invalidated
    blocking_step_ids = [r.split("'")[1] for r in reasons_a if "'" in r]
    assert step_id in blocking_step_ids or any("S01" in r for r in reasons_a), (
        f"Blocking reason should reference {step_id}: {reasons_a}"
    )

    # Case 2: review for A only — the list-based gate must find that artifact B (sha_b)
    # does not match the approved review's artifact_sha256 (sha_a) → invalidated
    # (Previously, dict-based collapsing would have sha_b overwrite sha_a for this
    # step_id, causing only sha_b to be checked — and then finding no review for it,
    # OR finding the review but with wrong SHA.  The list-based approach checks each
    # entry independently with its own SHA.)
    from fsf.runtime.consistency_review import list_shots_pending_review
    pending = list_shots_pending_review(journal, {step_id: rec_a}, tmp_path,
                                        assembled_artifacts=assembled_artifacts)
    # There should be exactly one pending entry (for B) — A passes
    assert len(pending) == 1, (
        f"Exactly one item (B) should be pending; got {len(pending)}: {pending}"
    )
    assert pending[0]["step_id"] == step_id
    # B should be invalidated (review SHA sha_a != artifact sha_b)
    assert pending[0]["status"] in ("missing", "invalidated"), (
        f"B should be missing or invalidated: {pending[0]}"
    )


# ── A4-3. SHA-collision: two files share same SHA; assembled file deleted ──────


def test_a4_sha_collision_deleted_actual_file_blocks(tmp_path: Path) -> None:
    """A4-3: When two distinct disk files share identical content (same SHA),
    and the file actually selected by assembly is deleted, the gate must block.

    Previously the SHA→path reverse-lookup could find the *other* file with the
    same SHA and silently pass.  With the per-artifact canonical_path approach
    the gate checks only the specific file listed in assembled_artifacts —
    not any file that happens to share its SHA.
    """
    identical_content = b"shared-identical-content"
    sha = hashlib.sha256(identical_content).hexdigest()

    # Two files with identical content → same SHA
    file_keep = "S01_keep.shape.bin"     # exists on disk — NOT the assembled one
    file_assembled = "S01_assembled.shape.bin"  # assembled file — will be deleted

    (tmp_path / file_keep).write_bytes(identical_content)
    (tmp_path / file_assembled).write_bytes(identical_content)

    step_id = "step_001_S01_generate"
    req_dig = "req_" + "e" * 60
    wo_id = "wo_s01"

    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1", "run_id": "r1",
        "steps": {
            step_id: {
                "status": "completed",
                "review_required": True,
                "requirements_digest": req_dig,
                "workorder_id": wo_id,
            },
        },
    }

    rec = build_review_record(
        step_id=step_id, job_id="j1", run_id="r1",
        workorder_id=wo_id, artifact_sha256=sha,
        requirements_digest=req_dig, acknowledged_digest=None,
        verdict="approved", reviewer="tester",
    )

    # Sanity: before deletion, gate passes (assembled file exists)
    assembled_artifacts_before = [
        {
            "step_id": step_id,
            "artifact_ref": "artifact:S01_assembled.shape.bin",
            "canonical_path": file_assembled,
            "sha256": sha,
        }
    ]
    gate_before, reasons_before = check_assembly_gate(
        journal, {step_id: rec}, tmp_path,
        assembled_artifacts=assembled_artifacts_before,
    )
    assert gate_before is True, (
        f"Gate should pass before deletion: {reasons_before}"
    )

    # Delete ONLY the assembled file; keep the other file with same SHA
    (tmp_path / file_assembled).unlink()
    assert (tmp_path / file_keep).exists(), "The 'keep' file must still exist"

    # Gate must now BLOCK — the specific assembled file is missing
    # It must NOT silently resolve using file_keep (same SHA ≠ same file)
    assembled_artifacts_after = [
        {
            "step_id": step_id,
            "artifact_ref": "artifact:S01_assembled.shape.bin",
            "canonical_path": file_assembled,   # <-- deleted
            "sha256": sha,                      # same SHA as file_keep
        }
    ]
    gate_after, reasons_after = check_assembly_gate(
        journal, {step_id: rec}, tmp_path,
        assembled_artifacts=assembled_artifacts_after,
    )
    assert gate_after is False, (
        f"Gate must block when the specific assembled file is deleted "
        f"(even though another file with same SHA exists): reasons={reasons_after}"
    )
    # The reason must mention the missing/deleted file
    combined = " ".join(reasons_after).lower()
    assert "not found" in combined or "missing" in combined or "deleted" in combined, (
        f"Reason should mention the missing file: {reasons_after}"
    )


# ── B4. Declared path and alias path both exist with different content ─────────


def test_b4_verified_canonical_path_is_assembled_not_declared(tmp_path: Path) -> None:
    """B4: When both the declared path (S01.shape.bin) and the alias path
    (S01.mock.bin) exist on disk with DIFFERENT content, the gate must verify
    and assembly must use the actual producer output (the file in output_files),
    NOT the independently re-resolved declared name.

    This test confirms the verified → assembled chain is unbroken:
    • _resolve_assembly_inputs() resolves the artifact to canonical_path=S01.mock.bin
      (the actual file from output_files)
    • check_assembly_gate() verifies S01.mock.bin (correct SHA)
    • The timeline is built using S01.mock.bin (not S01.shape.bin)

    If the code were to re-resolve art_ref independently, it would get S01.shape.bin
    (which has DIFFERENT content) — the gate would have verified the wrong file.
    """
    from fsf.runtime.plan_executor import PlanExecutor
    from fsf.runtime.consistency_review import save_reviews

    job = _build_review_required_job("b4_path_mismatch")
    plan = _build_plan(job)

    # Ensure the produce step ID is what we expect
    gen_step_id = _SHOT_STEP_ID  # "step_001_S01_generate"

    # Run once to generate the actual mock output (S01.mock.bin)
    ex = PlanExecutor(tmp_path, job_id="b4_path_mismatch", run_id="run01")
    r1 = ex.execute_plan(plan)
    gen_r1 = r1["steps"].get(gen_step_id, {})
    assert gen_r1.get("status") in ("completed", "reused"), f"Generate must succeed: {gen_r1}"

    out_files = gen_r1.get("output_files") or []
    if not out_files:
        pytest.skip("Generate step produced no output_files in shape mode")

    actual_path_rel = out_files[0]["path"]     # e.g. "S01.mock.bin"
    actual_sha = out_files[0]["sha256"]

    # Now plant a DIFFERENT file at the declared name (S01.shape.bin)
    # so both paths exist on disk but with different content
    declared_path_rel = "S01.shape.bin"
    decoy_content = b"DECOY-different-content-not-reviewed"
    (tmp_path / declared_path_rel).write_bytes(decoy_content)
    decoy_sha = hashlib.sha256(decoy_content).hexdigest()

    assert actual_path_rel != declared_path_rel, (
        "Test only applies when actual path differs from declared path; "
        f"actual={actual_path_rel!r}, declared={declared_path_rel!r}"
    )
    assert actual_sha != decoy_sha, "Actual and decoy SHAs must differ for this test to be meaningful"

    # Write an approved review for the ACTUAL file (actual_sha)
    art_sha = actual_sha
    rec = _minimal_approved_review(
        step_id=gen_step_id,
        job_id="b4_path_mismatch",
        run_id="run01",
        workorder_id=str(gen_r1.get("workorder_id") or ""),
        artifact_sha256=art_sha,
        requirements_digest=gen_r1.get("requirements_digest"),
    )
    save_reviews({gen_step_id: rec}, tmp_path)

    # Run 2: assembly should proceed — and must use the gate-verified actual_path,
    # not re-resolve art_ref to S01.shape.bin (which has different content)
    ex2 = PlanExecutor(tmp_path, job_id="b4_path_mismatch", run_id="run02")
    r2 = ex2.execute_plan(plan)
    asm_r2 = r2["steps"].get(_ASSEMBLY_STEP_ID, {})

    # The gate must NOT block (the actual file has a valid review)
    assert asm_r2.get("status") != "blocked" or (
        "review" not in (asm_r2.get("error") or "").lower()
    ), (
        f"Assembly must not be blocked by the review gate when the actual file has "
        f"a valid review.  actual_path={actual_path_rel!r}, error={asm_r2.get('error')}"
    )

    # The assembly step may succeed (shape mode → mock result) or fail for other
    # reasons, but it must NOT fail because it tried to assemble S01.shape.bin
    # (the decoy) with a different SHA than what the gate approved.
    # If it erroneously used S01.shape.bin, the gate SHA check would fail since
    # the decoy SHA != approved SHA, and we'd see a "blocked" with "invalidated".
    if asm_r2.get("status") == "blocked":
        err = (asm_r2.get("error") or "").lower()
        assert "invalidated" not in err and "sha" not in err, (
            f"Assembly must not be blocked by an SHA mismatch — that would indicate "
            f"it used the decoy file instead of the gate-verified file.  "
            f"error={asm_r2.get('error')!r}"
        )


def test_b4_ambiguous_accepted_paths_blocks(tmp_path: Path) -> None:
    """B4b: When a single producer step records both accepted paths
    (declared + alias), resolution is ambiguous and must block.

    This guards against a pathological case where the executor writes both
    S01.shape.bin AND S01.mock.bin with different SHAs — we cannot determine
    which one the assembly should consume.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("b4_ambiguous")
    plan = _build_plan(job)
    ex = PlanExecutor(tmp_path, job_id="b4_ambiguous", run_id="run01")

    # Fabricate step_results where the producer step recorded BOTH accepted paths
    bytes_shape = b"shape-content"
    bytes_mock = b"mock-content"
    sha_shape = hashlib.sha256(bytes_shape).hexdigest()
    sha_mock = hashlib.sha256(bytes_mock).hexdigest()
    assert sha_shape != sha_mock

    (tmp_path / "S01.shape.bin").write_bytes(bytes_shape)
    (tmp_path / "S01.mock.bin").write_bytes(bytes_mock)

    step_results_ambiguous: dict[str, Any] = {
        _SHOT_STEP_ID: {
            "id": _SHOT_STEP_ID,
            "status": "completed",
            "review_required": True,
            # execution_kind="mock" triggers shape-alias expansion — both S01.shape.bin
            # and S01.mock.bin are in accepted_paths, making the match ambiguous.
            "execution_kind": "mock",
            "output_files": [
                {"path": "S01.shape.bin", "sha256": sha_shape},
                {"path": "S01.mock.bin",  "sha256": sha_mock},
            ],
        }
    }

    art_inputs = plan["steps"][-1]["inputs"]["artifact_inputs"]
    resolved, errors = ex._resolve_assembly_inputs(art_inputs, step_results_ambiguous)

    assert errors, (
        "Must error when both declared and alias paths are recorded by the same "
        f"producer step: got resolved={resolved}, errors={errors}"
    )
    assert len(resolved) == 0, f"resolved must be empty on ambiguous match: {resolved}"
    assert any("ambiguous" in e.lower() for e in errors), (
        f"Error should mention ambiguity: {errors}"
    )


# ── C1. Real-media producer writing alias path must block ─────────────────────


def test_c1_real_media_producer_with_mock_output_blocks(tmp_path: Path) -> None:
    """C1: When a producer step has execution_kind NOT in SHAPE_EXECUTION_KINDS
    (i.e. real-media mode: 'process', 'api', 'kaggle', …) but its output_files
    record the shape/mock alias path (.mock.bin instead of the declared .mp4),
    the gate must block with a clear diagnostic.

    A real-media executor that writes .mock.bin is broken — accepting it would
    let a shape test stub pass as real media.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("c1_real_media_mock")
    plan = _build_plan(job)
    ex = PlanExecutor(tmp_path, job_id="c1_real_media_mock", run_id="run01")

    # Build step_results simulating a process executor that mistakenly wrote .mock.bin
    # (execution_kind="process" → real-media mode → alias not permitted)
    mock_bytes = b"stub-not-real-media"
    mock_sha = hashlib.sha256(mock_bytes).hexdigest()
    (tmp_path / "S01.mock.bin").write_bytes(mock_bytes)

    # NOTE: the plan declares "artifact:S01.shape.bin" as the artifact ref
    # (shape=True plan).  In a shape-mode plan the declared path ends in .shape.bin.
    step_results_real: dict[str, Any] = {
        _SHOT_STEP_ID: {
            "id": _SHOT_STEP_ID,
            "status": "completed",
            "review_required": True,
            "execution_kind": "process",   # real-media — aliases must NOT be accepted
            "output_files": [{"path": "S01.mock.bin", "sha256": mock_sha}],
        }
    }

    art_inputs = plan["steps"][-1]["inputs"]["artifact_inputs"]
    resolved, errors = ex._resolve_assembly_inputs(art_inputs, step_results_real)

    assert errors, (
        "Must block when a real-media (process) producer records the shape alias "
        f"instead of the declared path; got resolved={resolved}, errors={errors}"
    )
    assert len(resolved) == 0, f"resolved must be empty: {resolved}"
    # Error must mention alias / shape / execution_kind distinction
    combined = " ".join(errors).lower()
    assert (
        "alias" in combined or "shape" in combined or "execution" in combined
    ), f"Error should mention alias/shape/execution constraint: {errors}"


def test_c1_shape_mode_producer_alias_resolves(tmp_path: Path) -> None:
    """C1b: In shape execution mode (execution_kind == 'mock'), the alias path
    .mock.bin is accepted for a declared .shape.bin artifact.  This is the
    normal offline-test flow: plan declares .shape.bin, mock executor writes .mock.bin.
    """
    from fsf.runtime.plan_executor import PlanExecutor

    job = _build_review_required_job("c1_shape_alias")
    plan = _build_plan(job)
    ex = PlanExecutor(tmp_path, job_id="c1_shape_alias", run_id="run01")

    mock_bytes = b"mock-shape-content"
    mock_sha = hashlib.sha256(mock_bytes).hexdigest()
    (tmp_path / "S01.mock.bin").write_bytes(mock_bytes)

    step_results_shape: dict[str, Any] = {
        _SHOT_STEP_ID: {
            "id": _SHOT_STEP_ID,
            "status": "completed",
            "review_required": True,
            "execution_kind": "mock",   # shape test mode — alias accepted
            "output_files": [{"path": "S01.mock.bin", "sha256": mock_sha}],
        }
    }

    art_inputs = plan["steps"][-1]["inputs"]["artifact_inputs"]
    resolved, errors = ex._resolve_assembly_inputs(art_inputs, step_results_shape)

    assert not errors, (
        f"Shape-mode alias .mock.bin should resolve without errors: {errors}"
    )
    assert len(resolved) == 1, f"Should resolve to one entry: {resolved}"
    assert resolved[0]["canonical_path"] == "S01.mock.bin", (
        f"canonical_path must be the alias path used by the producer: {resolved[0]}"
    )
    assert resolved[0]["sha256"] == mock_sha


def test_c1_alias_restriction_on_resume(tmp_path: Path) -> None:
    """C1c: The execution_kind gate on aliases also applies on the resume path.

    First run: shape mode → assembly blocked (no review).
    After approving the review: second run (resume) → assembly not blocked by alias.

    This confirms that the alias gate is consistent across first-run and resume
    paths in the executor's main loop.
    """
    from fsf.runtime.plan_executor import PlanExecutor
    from fsf.runtime.consistency_review import save_reviews

    job = _build_review_required_job("c1_resume_alias")
    plan = _build_plan(job)

    # Run 1: shape mode → generate completes, assembly blocked (no review)
    ex1 = PlanExecutor(tmp_path, job_id="c1_resume_alias", run_id="run01")
    r1 = ex1.execute_plan(plan)
    gen_r1 = r1["steps"].get(_SHOT_STEP_ID, {})
    assert gen_r1.get("status") in ("completed", "reused"), (
        f"Generate step must complete in shape mode: {gen_r1}"
    )
    asm_r1 = r1["steps"].get(_ASSEMBLY_STEP_ID, {})
    assert asm_r1.get("status") == "blocked", (
        f"Assembly must be blocked without review: {asm_r1}"
    )

    # Confirm execution_kind is a shape kind (mock/shape)
    from fsf.runtime.external_op import SHAPE_EXECUTION_KINDS
    assert gen_r1.get("execution_kind") in SHAPE_EXECUTION_KINDS, (
        f"Test requires shape execution; got execution_kind={gen_r1.get('execution_kind')!r}"
    )

    # Approve the review using the actual canonical_path produced by the executor
    out_files = gen_r1.get("output_files") or []
    if not out_files:
        pytest.skip("Generate step produced no output_files")
    art_sha = out_files[0]["sha256"]
    rec = _minimal_approved_review(
        step_id=_SHOT_STEP_ID,
        job_id="c1_resume_alias",
        run_id="run01",
        workorder_id=str(gen_r1.get("workorder_id") or ""),
        artifact_sha256=art_sha,
        requirements_digest=gen_r1.get("requirements_digest"),
    )
    save_reviews({_SHOT_STEP_ID: rec}, tmp_path)

    # Run 2 (resume): alias gate must still resolve correctly and allow assembly
    ex2 = PlanExecutor(tmp_path, job_id="c1_resume_alias", run_id="run02")
    r2 = ex2.execute_plan(plan)
    asm_r2 = r2["steps"].get(_ASSEMBLY_STEP_ID, {})

    # Assembly should NOT be blocked by the alias gate (shape mode → alias OK)
    err_lower = (asm_r2.get("error") or "").lower()
    assert not (
        asm_r2.get("status") == "blocked" and "alias" in err_lower
    ), (
        f"Resume assembly must not be blocked by alias gate in shape mode: "
        f"status={asm_r2.get('status')!r}, error={asm_r2.get('error')!r}"
    )
    # Assembly should proceed past the alias/producer gate (may still be blocked
    # by review gate if SHA doesn't match, but not by alias rejection)
    if asm_r2.get("status") == "blocked":
        assert "alias" not in err_lower and "execution_kind" not in err_lower, (
            f"If blocked, it should not be due to alias restriction: {asm_r2.get('error')}"
        )
