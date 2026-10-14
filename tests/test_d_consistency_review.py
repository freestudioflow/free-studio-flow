"""Tests for Phase D: Consistency Review Gate.

Verifies:
1.  build_review_record() produces a valid record with all required fields.
2.  build_review_record() rejects invalid inputs (empty step_id, bad verdict, empty reviewer).
3.  is_review_valid(): True when artifact SHA, requirements_digest, and workorder_id match.
4.  is_review_valid(): False when artifact SHA changes (step regenerated).
5.  is_review_valid(): False when requirements_digest changes (workorder re-declared).
6.  is_review_valid(): False when workorder_id changes (step re-dispatched).
7.  is_review_valid(): False for wrong schema_version.
8.  save_reviews() / load_reviews() round-trip persists and restores review records.
9.  load_reviews() returns {} when file does not exist.
10. list_shots_pending_review(): missing review → status="missing".
11. list_shots_pending_review(): invalidated review → status="invalidated".
12. list_shots_pending_review(): rejected review → status="rejected".
13. list_shots_pending_review(): approved valid review → NOT in pending list.
14. list_shots_pending_review(): step without requirements_digest → NOT in pending list.
15. check_assembly_gate(): passes when all shots are approved.
16. check_assembly_gate(): blocks when any shot is missing a review.
17. check_assembly_gate(): blocks when any shot has an invalidated review.
18. check_assembly_gate(): reused step with no review → blocks assembly.
19. check_assembly_gate(): reused step with valid approved review → passes.
20. Assembly gate is called in _execute_transform; blocks when review missing.
21. Assembly gate is bypassed for steps with no consistency_requirements.
22. CLI 'fsf review list': prints pending items.
23. CLI 'fsf review status': exit 0 when gate passes, exit 1 when blocked.
24. CLI 'fsf review approve': writes approved record; subsequent status passes.
25. CLI 'fsf review reject': writes rejected record; subsequent list shows it.
26. CLI 'fsf review approve': step not in journal → exit 1.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fsf.runtime.consistency_review import (
    REVIEW_SCHEMA_VERSION,
    VALID_VERDICTS,
    build_review_record,
    check_assembly_gate,
    is_review_valid,
    list_shots_pending_review,
    load_reviews,
    save_reviews,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _minimal_review(
    *,
    step_id: str = "S01",
    artifact_sha256: str = "a" * 64,
    requirements_digest: str | None = "r" * 64,
    workorder_id: str = "wo_j1_r1_S01_att1",
    verdict: str = "approved",
    reviewer: str = "human",
) -> dict[str, Any]:
    return build_review_record(
        step_id=step_id,
        job_id="j1",
        run_id="r1",
        workorder_id=workorder_id,
        artifact_sha256=artifact_sha256,
        requirements_digest=requirements_digest,
        acknowledged_digest="ack" + "x" * 61,
        verdict=verdict,
        reviewer=reviewer,
    )


def _make_journal_and_review(
    tmp_path: Path,
    *,
    step_id: str = "S01",
    requirements_digest: str | None = "r" * 64,
    workorder_id: str = "wo_j1_r1_S01_att1",
    status: str = "completed",
    verdict: str = "approved",
    artifact_content: bytes = b"artifact-data",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a (journal, review_record) pair with a real artifact on disk.

    The artifact file is written to ``tmp_path`` and the SHA is computed from
    its bytes, so disk-SHA verification in ``list_shots_pending_review`` passes.
    The review record's ``artifact_sha256`` matches the journal's sha.
    """
    journal = _journal_with_step(
        step_id=step_id,
        requirements_digest=requirements_digest,
        workorder_id=workorder_id,
        status=status,
        review_required=True,
        tmp_path=tmp_path,
        artifact_content=artifact_content,
    )
    art_sha = journal["steps"][step_id]["output_files"][0]["sha256"]
    rec = _minimal_review(
        step_id=step_id,
        artifact_sha256=art_sha,
        requirements_digest=requirements_digest,
        workorder_id=workorder_id,
        verdict=verdict,
    )
    return journal, rec


def _journal_with_step(
    step_id: str = "S01",
    *,
    artifact_sha256: str | None = None,  # None = auto-compute from file content
    requirements_digest: str | None = "r" * 64,
    workorder_id: str = "wo_j1_r1_S01_att1",
    status: str = "completed",
    review_required: bool = True,
    tmp_path: Path | None = None,          # when provided, artifact file is created
    artifact_content: bytes = b"artifact-data",  # content of the created artifact
) -> dict[str, Any]:
    """Build a minimal journal dict with one shot step.

    When ``tmp_path`` is supplied the artifact file ``{step_id}.mp4`` is created
    there with ``artifact_content`` bytes, and the sha256 in the journal is
    computed from those bytes.  Passing ``artifact_sha256`` together with
    ``tmp_path`` overrides the computed sha (useful for SHA-mismatch tests,
    where the file and journal intentionally disagree).

    Tests that exercise the disk-SHA verification path MUST pass ``tmp_path``
    so the gate can locate and read the artifact.
    """
    rel_path = f"{step_id}.mp4"
    if tmp_path is not None:
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(artifact_content)
        computed_sha = hashlib.sha256(artifact_content).hexdigest()
        final_sha = artifact_sha256 if artifact_sha256 is not None else computed_sha
    else:
        # No file created — callers must not rely on disk-SHA verification
        final_sha = artifact_sha256 if artifact_sha256 is not None else "a" * 64

    return {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1",
        "run_id": "r1",
        "steps": {
            step_id: {
                "status": status,
                "output_files": [{"path": rel_path, "sha256": final_sha}],
                "requirements_digest": requirements_digest,
                "workorder_id": workorder_id,
                "review_required": review_required,
            }
        },
    }


# ── 1. build_review_record basic structure ────────────────────────────────────


def test_build_review_record_produces_valid_record() -> None:
    rec = _minimal_review()
    assert rec["schema_version"] == REVIEW_SCHEMA_VERSION
    assert rec["step_id"] == "S01"
    assert rec["verdict"] == "approved"
    assert rec["reviewer"] == "human"
    assert len(rec["review_digest"]) == 64
    assert rec["artifact_sha256"] == "a" * 64
    assert rec["requirements_digest"] == "r" * 64


def test_build_review_record_all_verdicts_accepted() -> None:
    for verdict in VALID_VERDICTS:
        rec = _minimal_review(verdict=verdict)
        assert rec["verdict"] == verdict


# ── 2. build_review_record rejects invalid inputs ─────────────────────────────


def test_build_review_record_rejects_empty_step_id() -> None:
    with pytest.raises(ValueError, match="step_id"):
        build_review_record(
            step_id="", job_id="j1", run_id="r1", workorder_id="wo",
            artifact_sha256="a" * 64, requirements_digest=None,
            acknowledged_digest=None, verdict="approved", reviewer="human",
        )


def test_build_review_record_rejects_bad_verdict() -> None:
    with pytest.raises(ValueError, match="verdict"):
        _minimal_review(verdict="maybe")


def test_build_review_record_rejects_empty_reviewer() -> None:
    with pytest.raises(ValueError, match="reviewer"):
        build_review_record(
            step_id="S01", job_id="j1", run_id="r1", workorder_id="wo",
            artifact_sha256="a" * 64, requirements_digest=None,
            acknowledged_digest=None, verdict="approved", reviewer="",
        )


# ── 3. is_review_valid passes when all digests match ─────────────────────────


def test_is_review_valid_true_when_all_match() -> None:
    rec = _minimal_review()
    valid, reason = is_review_valid(
        rec,
        current_artifact_sha="a" * 64,
        current_requirements_digest="r" * 64,
        current_workorder_id="wo_j1_r1_S01_att1",
    )
    assert valid is True
    assert reason == ""


# ── 4. is_review_valid False when artifact SHA changes ────────────────────────


def test_is_review_valid_false_when_artifact_sha_changes() -> None:
    rec = _minimal_review(artifact_sha256="a" * 64)
    valid, reason = is_review_valid(
        rec,
        current_artifact_sha="b" * 64,
        current_requirements_digest="r" * 64,
        current_workorder_id="wo_j1_r1_S01_att1",
    )
    assert valid is False
    assert "SHA" in reason or "sha" in reason.lower()


# ── 5. is_review_valid False when requirements_digest changes ─────────────────


def test_is_review_valid_false_when_requirements_digest_changes() -> None:
    rec = _minimal_review(requirements_digest="r" * 64)
    valid, reason = is_review_valid(
        rec,
        current_artifact_sha="a" * 64,
        current_requirements_digest="z" * 64,
        current_workorder_id="wo_j1_r1_S01_att1",
    )
    assert valid is False
    assert "requirements" in reason.lower() or "digest" in reason.lower()


# ── 6. is_review_valid False when workorder_id changes ───────────────────────


def test_is_review_valid_false_when_workorder_id_changes() -> None:
    rec = _minimal_review(workorder_id="wo_j1_r1_S01_att1")
    valid, reason = is_review_valid(
        rec,
        current_artifact_sha="a" * 64,
        current_requirements_digest="r" * 64,
        current_workorder_id="wo_j1_r1_S01_att2",  # different attempt
    )
    assert valid is False
    assert "workorder" in reason.lower()


# ── 7. is_review_valid False for wrong schema_version ────────────────────────


def test_is_review_valid_false_for_wrong_schema_version() -> None:
    rec = _minimal_review()
    rec["schema_version"] = "old_schema_v0"
    valid, reason = is_review_valid(
        rec,
        current_artifact_sha="a" * 64,
        current_requirements_digest="r" * 64,
        current_workorder_id="wo_j1_r1_S01_att1",
    )
    assert valid is False


# ── 8. save_reviews / load_reviews round-trip ─────────────────────────────────


def test_save_and_load_reviews_round_trip(tmp_path: Path) -> None:
    rec = _minimal_review()
    reviews = {"S01": rec}
    save_reviews(reviews, tmp_path)

    loaded = load_reviews(tmp_path)
    assert "S01" in loaded
    assert loaded["S01"]["review_digest"] == rec["review_digest"]
    assert loaded["S01"]["verdict"] == "approved"


# ── 9. load_reviews returns {} when file absent ───────────────────────────────


def test_load_reviews_returns_empty_when_file_absent(tmp_path: Path) -> None:
    result = load_reviews(tmp_path / "nonexistent")
    assert result == {}


# ── 10. list_shots_pending_review: missing review ────────────────────────────


def test_list_pending_review_missing(tmp_path: Path) -> None:
    # review_required=True, no artifact file on disk → missing
    journal = _journal_with_step(tmp_path=tmp_path)
    # Delete the artifact to simulate it being removed
    art_path = tmp_path / "S01.mp4"
    art_path.unlink()
    pending = list_shots_pending_review(journal, {}, tmp_path)
    assert len(pending) == 1
    assert pending[0]["step_id"] == "S01"
    assert pending[0]["status"] == "missing"


# ── 11. list_shots_pending_review: invalidated review ────────────────────────


def test_list_pending_review_invalidated_on_sha_change(tmp_path: Path) -> None:
    # Build journal with a real artifact file, then build a review with a *different* SHA
    journal = _journal_with_step(tmp_path=tmp_path)
    art_sha_in_journal = journal["steps"]["S01"]["output_files"][0]["sha256"]
    # The review was built against a different (old) artifact
    old_sha = "a" * 64
    assert old_sha != art_sha_in_journal, "Fixture SHAs must differ for this test"
    rec = _minimal_review(artifact_sha256=old_sha)
    pending = list_shots_pending_review(journal, {"S01": rec}, tmp_path)
    assert len(pending) == 1
    assert pending[0]["status"] == "invalidated"


# ── 12. list_shots_pending_review: rejected review ───────────────────────────


def test_list_pending_review_rejected(tmp_path: Path) -> None:
    # Build matching journal+review, but with verdict=rejected
    journal, _rec_approved = _make_journal_and_review(tmp_path)
    art_sha = journal["steps"]["S01"]["output_files"][0]["sha256"]
    rec = _minimal_review(artifact_sha256=art_sha, verdict="rejected")
    pending = list_shots_pending_review(journal, {"S01": rec}, tmp_path)
    assert len(pending) == 1
    # P1-3 design: is_review_valid() rejects any non-"approved" verdict;
    # the pending entry is reported as "invalidated" (verdict != approved).
    assert pending[0]["status"] == "invalidated"
    assert "verdict" in pending[0]["reason"].lower()


# ── 13. Approved valid review NOT in pending list ─────────────────────────────


def test_list_pending_review_approved_not_in_pending(tmp_path: Path) -> None:
    journal, rec = _make_journal_and_review(tmp_path)  # real artifact + matching review
    pending = list_shots_pending_review(journal, {"S01": rec}, tmp_path)
    assert pending == []


# ── 14. Step with review_required=False NOT in pending list ───────────────────


def test_list_pending_review_no_requirements_not_in_pending(tmp_path: Path) -> None:
    # review_required=False: step does not need review regardless of requirements_digest
    journal = _journal_with_step(review_required=False, tmp_path=tmp_path)
    pending = list_shots_pending_review(journal, {}, tmp_path)
    assert pending == [], "Steps with review_required=False must not be listed"


# ── 15. check_assembly_gate passes when all shots approved ───────────────────


def test_check_assembly_gate_passes_all_approved(tmp_path: Path) -> None:
    journal, rec = _make_journal_and_review(tmp_path)
    gate_ok, reasons = check_assembly_gate(journal, {"S01": rec}, tmp_path)
    assert gate_ok is True
    assert reasons == []


# ── 16. check_assembly_gate blocks when a shot is missing a review ────────────


def test_check_assembly_gate_blocks_missing_review(tmp_path: Path) -> None:
    journal = _journal_with_step(tmp_path=tmp_path)
    gate_ok, reasons = check_assembly_gate(journal, {}, tmp_path)
    assert gate_ok is False
    assert any("S01" in r for r in reasons)


# ── 17. check_assembly_gate blocks when a review is invalidated ───────────────


def test_check_assembly_gate_blocks_invalidated_review(tmp_path: Path) -> None:
    journal = _journal_with_step(tmp_path=tmp_path)
    art_sha = journal["steps"]["S01"]["output_files"][0]["sha256"]
    old_sha = "a" * 64
    assert old_sha != art_sha
    rec = _minimal_review(artifact_sha256=old_sha)  # review for old artifact
    gate_ok, reasons = check_assembly_gate(journal, {"S01": rec}, tmp_path)
    assert gate_ok is False
    assert any("S01" in r for r in reasons)


# ── 18. Reused step with no review → blocks assembly ─────────────────────────


def test_check_assembly_gate_blocks_reused_step_missing_review(tmp_path: Path) -> None:
    """A reused step is subject to the same gate as a completed step."""
    journal = _journal_with_step(status="reused", tmp_path=tmp_path)
    gate_ok, reasons = check_assembly_gate(journal, {}, tmp_path)
    assert gate_ok is False
    assert any("S01" in r for r in reasons)


# ── 19. Reused step with valid approved review → passes ──────────────────────


def test_check_assembly_gate_reused_with_valid_review_passes(tmp_path: Path) -> None:
    journal, rec = _make_journal_and_review(tmp_path, status="reused")
    gate_ok, reasons = check_assembly_gate(journal, {"S01": rec}, tmp_path)
    assert gate_ok is True
    assert reasons == []


# ── 20. Assembly gate blocks _execute_transform when review missing ───────────


def test_execute_transform_blocked_by_review_gate(tmp_path: Path) -> None:
    """When assembly gate fails, _execute_transform returns status='blocked'."""
    from fsf.runtime.plan import build_execution_plan
    from fsf.runtime.plan_executor import PlanExecutor

    # Build a 1-shot job with consistency_context so requirements_digest appears
    job: dict[str, Any] = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "d_gate_test",
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
                        "effective_prompt": None,  # set at runtime by executor, null at compile
                    },
                },
            }
        ],
    }
    plan = build_execution_plan(
        job,
        executor_tool="studio.video.generate",
        shape=True,
        enable_assembly=True,
    )

    executor = PlanExecutor(tmp_path, job_id="d_gate_test", run_id="run01")
    result = executor.execute_plan(plan)

    # The generate step may complete (shape), but the assembly step must be blocked
    step_statuses = {sid: s.get("status") for sid, s in result.get("steps", {}).items()}
    assembly_steps = {
        sid: status
        for sid, status in step_statuses.items()
        if "assembly" in sid.lower() or "concat" in sid.lower() or "assembled" in sid.lower()
    }
    if assembly_steps:
        for sid, status in assembly_steps.items():
            assert status in ("blocked", "failed"), (
                f"Assembly step '{sid}' should be blocked by review gate but got status='{status}'"
            )


# ── 21. Steps without review_required bypass the gate ────────────────────────


def test_assembly_gate_transparent_for_steps_without_requirements(tmp_path: Path) -> None:
    """Steps without review_required=True must not block the assembly gate."""
    journal: dict[str, Any] = {
        "schema_version": "fsf_plan_execution_journal_v1",
        "job_id": "j1",
        "run_id": "r1",
        "steps": {
            "S01": {
                "status": "completed",
                "output_files": [{"path": "S01.mp4", "sha256": "a" * 64}],
                # review_required absent → not subject to review gate
            }
        },
    }
    gate_ok, reasons = check_assembly_gate(journal, {}, tmp_path)
    assert gate_ok is True
    assert reasons == []


# ── 22. CLI 'fsf review list': prints pending items ──────────────────────────


def test_cli_review_list_prints_pending(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stdout
    from fsf.cli.main import main

    # Write a minimal journal with a shot needing review (artifact file also created)
    journal = _journal_with_step(
        step_id="S01",
        requirements_digest="r" * 64,
        workorder_id="wo_j1_r1_S01_att1",
        review_required=True,
        tmp_path=tmp_path,
    )
    (tmp_path / "fsf_execution_journal.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["review", "list", str(tmp_path)])
    output = buf.getvalue()

    assert rc == 0, f"Expected rc=0 but got {rc}: {output}"
    assert "S01" in output
    assert "missing" in output.lower() or "MISSING" in output


# ── 23. CLI 'fsf review status': exit 0 when gate passes, 1 when blocked ─────


def test_cli_review_status_exits_correctly(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stdout
    from fsf.cli.main import main

    journal = _journal_with_step(tmp_path=tmp_path)
    art_sha = journal["steps"]["S01"]["output_files"][0]["sha256"]
    journal_path = tmp_path / "fsf_execution_journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    # No review → gate blocked → exit 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc_blocked = main(["review", "status", str(tmp_path)])
    assert rc_blocked == 1

    # Write approved review with matching artifact SHA
    rec = _minimal_review(artifact_sha256=art_sha, verdict="approved")
    save_reviews({"S01": rec}, tmp_path)

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc_ok = main(["review", "status", str(tmp_path)])
    assert rc_ok == 0


# ── 24. CLI 'fsf review approve': writes record; subsequent status passes ─────


def test_cli_review_approve_and_subsequent_status_passes(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stdout
    from fsf.cli.main import main

    journal = _journal_with_step(tmp_path=tmp_path)
    art_sha = journal["steps"]["S01"]["output_files"][0]["sha256"]
    journal_path = tmp_path / "fsf_execution_journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([
            "review", "approve", str(tmp_path), "S01",
            "--reviewer", "alice",
            "--notes", "looks good",
        ])
    assert rc == 0, f"approve exit: {rc}"

    # Verify record on disk
    reviews = load_reviews(tmp_path)
    assert "S01" in reviews
    assert reviews["S01"]["verdict"] == "approved"
    assert reviews["S01"]["reviewer"] == "alice"

    # Gate should now pass
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc_status = main(["review", "status", str(tmp_path)])
    assert rc_status == 0, "Gate should pass after approve"


# ── 25. CLI 'fsf review reject': writes rejected record ──────────────────────


def test_cli_review_reject_writes_rejected_record(tmp_path: Path) -> None:
    import io
    from contextlib import redirect_stdout
    from fsf.cli.main import main

    journal = _journal_with_step(tmp_path=tmp_path)
    journal_path = tmp_path / "fsf_execution_journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([
            "review", "reject", str(tmp_path), "S01",
            "--reviewer", "bob",
            "--notes", "wrong character colour",
        ])
    assert rc == 0

    reviews = load_reviews(tmp_path)
    assert reviews["S01"]["verdict"] == "rejected"
    assert reviews["S01"]["reviewer"] == "bob"

    # Gate must still be blocked (rejected is not approved)
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc_status = main(["review", "status", str(tmp_path)])
    assert rc_status == 1


# ── 26. CLI 'fsf review approve': step not in journal → exit 1 ────────────────


def test_cli_review_approve_unknown_step_exits_1(tmp_path: Path) -> None:
    import sys, io
    from contextlib import redirect_stdout, redirect_stderr
    from fsf.cli.main import main

    journal = _journal_with_step(step_id="S01", tmp_path=tmp_path)
    journal_path = tmp_path / "fsf_execution_journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    buf_err = io.StringIO()
    with redirect_stderr(buf_err):
        rc = main(["review", "approve", str(tmp_path), "NONEXISTENT"])
    assert rc == 1
    assert "NONEXISTENT" in buf_err.getvalue() or "not found" in buf_err.getvalue().lower()


# ===============================================================================
# Integration scenarios (P1 gate bypass fixes -- execute_plan level)
# ===============================================================================

def _build_review_required_job(job_id: str = 'd_int') -> dict:
    return {
        'schema_version': 'fsf_job_manifest_v1',
        'job_id': job_id,
        'tasks': [
            {
                'id': 'S01',
                'unit_kind': 'shot',
                'shot_spec': {
                    'generation_prompt': 'A hero walks.',
                    'duration_seconds': 5.0,
                    'source_prompt': 'A hero walks.',
                    'aspect_ratio': '16:9',
                },
                'consistency_context': {
                    'schema_version': 'fsf_consistency_context_v1',
                    'asset_refs': [],
                    'review_required': True,
                    'generation_intent': {
                        'base_prompt': 'A hero walks.',
                        'injection_rule_version': 'fsf_injection_v1',
                        'asset_injections': [],
                        'effective_prompt': None,
                    },
                },
            }
        ],
    }
