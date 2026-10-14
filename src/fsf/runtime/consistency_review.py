"""FSF Consistency Review Gate.

Provides the review-gate layer between shot generation and assembly:

- Each completed shot with ``review_required=True`` in the plan must have a
  valid approved review record before the assembly step can proceed.
- A review record is bound (via ``review_digest``) to the specific artifact SHA,
  requirements_digest, and workorder identity at review time.  Any change to
  those values, or any tampering with the record, invalidates it.
- Reused steps (resume) are subject to the same gate as freshly completed steps.

Scope: ``review_required`` (from the plan's ``consistency_context``) determines
which steps need review, not ``requirements_digest`` alone.  This correctly
covers non-handoff steps that still require visual sign-off and avoids forcing
review on handoff steps that explicitly opted out.

Public API
----------
build_review_record(...)          Build a review record with a bound digest.
                                  NOTE: returns an ordinary dict — immutability
                                  is not enforced at runtime; the bound
                                  review_digest is the integrity mechanism.
is_review_valid(review, ...)      Check whether a record is still valid and
                                  approved for the current artifact/digest state.
load_reviews(out_dir)             Load the review store from disk.
save_reviews(reviews, out_dir)    Persist the review store to disk.
list_shots_pending_review(...)    List steps that need review or are invalidated.
check_assembly_gate(...)          Return (gate_ok, blocking_reasons).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fsf.conformance.digest import canonical_contract_digest

REVIEW_SCHEMA_VERSION = "fsf_consistency_review_v1"
REVIEW_STORE_FILENAME = "fsf_consistency_reviews.json"

# Valid verdict strings.  Only "approved" passes the gate.
VALID_VERDICTS: frozenset[str] = frozenset({"approved", "rejected", "needs_rework"})


# ── Review record ─────────────────────────────────────────────────────────────


def build_review_record(
    *,
    step_id: str,
    job_id: str,
    run_id: str,
    workorder_id: str,
    artifact_sha256: str,
    requirements_digest: str | None,
    acknowledged_digest: str | None,
    verdict: str,
    reviewer: str,
    notes: str = "",
) -> dict[str, Any]:
    """Build a review record bound to specific digests via ``review_digest``.

    The record is an ordinary dict; ``review_digest`` is the integrity
    mechanism — ``is_review_valid()`` recomputes it to detect tampering.

    ``verdict`` must be one of: "approved", "rejected", "needs_rework".

    Raises ValueError for invalid inputs.
    """
    if not step_id:
        raise ValueError("build_review_record: step_id is required")
    if not artifact_sha256:
        raise ValueError("build_review_record: artifact_sha256 is required")
    verdict_norm = verdict.strip().lower()
    if verdict_norm not in VALID_VERDICTS:
        raise ValueError(
            f"build_review_record: invalid verdict '{verdict}'.  "
            f"Accepted: {sorted(VALID_VERDICTS)!r}"
        )
    reviewer_clean = (reviewer or "").strip()
    if not reviewer_clean:
        raise ValueError("build_review_record: reviewer is required (use 'human' or a name)")

    record: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "step_id": step_id,
        "job_id": job_id,
        "run_id": run_id,
        "workorder_id": workorder_id,
        "reviewed_at": datetime.now(tz=timezone.utc).isoformat(),
        "reviewer": reviewer_clean,
        "verdict": verdict_norm,
        "notes": notes.strip(),
        # Binding digests
        "artifact_sha256": artifact_sha256.lower().strip(),
        "requirements_digest": requirements_digest or None,
        "acknowledged_digest": acknowledged_digest or None,
    }
    record["review_digest"] = canonical_contract_digest(record)
    return record


# ── Validity check ────────────────────────────────────────────────────────────


def is_review_valid(
    review: dict[str, Any],
    *,
    current_artifact_sha: str,
    current_requirements_digest: str | None,
    current_workorder_id: str,
    expected_step_id: str | None = None,
    expected_job_id: str | None = None,
    expected_run_id: str | None = None,  # accepted but not enforced; see NOTE below
) -> tuple[bool, str]:
    """Check whether a review record is still valid and approved.

    Returns ``(True, "")`` when valid and approved, ``(False, reason)`` otherwise.

    Checks (in order):
    1. ``review_digest`` integrity — recomputed and verified to detect tampering.
    2. ``verdict`` must be exactly "approved"; anything else (including unknown
       strings) blocks the gate.
    3. ``step_id`` identity when supplied — prevents cross-step review reuse.
    4. ``job_id`` identity when supplied — prevents cross-job review reuse.
       ``run_id`` is **not** enforced: a review approved on run01 is valid for
       subsequent resume runs of the same job as long as all other fields match.
    5. Artifact SHA match.
    6. Requirements digest match.
    7. Workorder ID match.

    Reused steps must also pass with their carried artifact SHA and digest.
    """
    if not isinstance(review, dict) or review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        return False, "Review record has wrong schema_version or is not a dict"

    # ── 1. Integrity: recompute review_digest ───────────────────────────── #
    stored_digest = str(review.get("review_digest") or "").strip()
    if not stored_digest:
        return False, "Review record is missing review_digest"
    record_without_digest = {k: v for k, v in review.items() if k != "review_digest"}
    recomputed_digest = canonical_contract_digest(record_without_digest)
    if stored_digest != recomputed_digest:
        return False, (
            f"Review record integrity failure: stored digest={stored_digest[:16]}…, "
            f"recomputed={recomputed_digest[:16]}….  Record may have been tampered."
        )

    # ── 2. Verdict: only "approved" passes ─────────────────────────────── #
    verdict = str(review.get("verdict") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        return False, (
            f"Review record contains unknown verdict='{verdict}'.  "
            f"Valid verdicts: {sorted(VALID_VERDICTS)!r}"
        )
    if verdict != "approved":
        return False, (
            f"Review verdict is '{verdict}', not 'approved'.  "
            f"Only an approved review allows assembly to proceed."
        )

    # ── 3. Identity checks ─────────────────────────────────────────────── #
    if expected_step_id is not None:
        recorded_sid = str(review.get("step_id") or "").strip()
        if recorded_sid != expected_step_id:
            return False, (
                f"Review step_id mismatch: recorded='{recorded_sid}', "
                f"expected='{expected_step_id}'.  Cross-step review reuse is forbidden."
            )
    if expected_job_id is not None:
        recorded_jid = str(review.get("job_id") or "").strip()
        if recorded_jid != expected_job_id:
            return False, (
                f"Review job_id mismatch: recorded='{recorded_jid}', "
                f"expected='{expected_job_id}'.  Cross-job review reuse is forbidden."
            )
    # NOTE: run_id is recorded in the review for audit purposes but is NOT
    # checked for equality here.  A review approved on run01 is valid for
    # subsequent runs of the same job as long as the artifact SHA, requirements
    # digest, workorder ID, and job identity all match.  Blocking on run_id
    # would force re-review on every resume run, which breaks the expected
    # workflow.  Cross-run reuse within the same job is intentional.

    # ── 4. Artifact SHA ─────────────────────────────────────────────────── #
    recorded_sha = (review.get("artifact_sha256") or "").lower().strip()
    current_sha_norm = current_artifact_sha.lower().strip()
    if recorded_sha != current_sha_norm:
        return False, (
            f"Artifact SHA changed since review: "
            f"recorded={recorded_sha[:16]}…, current={current_sha_norm[:16]}…"
        )

    # ── 5. Requirements digest ──────────────────────────────────────────── #
    recorded_req = review.get("requirements_digest") or None
    current_req = current_requirements_digest or None
    if recorded_req != current_req:
        return False, (
            f"Requirements digest changed since review: "
            f"recorded={str(recorded_req)[:16]}…, current={str(current_req)[:16]}…"
        )

    # ── 6. Workorder ID ─────────────────────────────────────────────────── #
    recorded_wo = str(review.get("workorder_id") or "").strip()
    current_wo = str(current_workorder_id or "").strip()
    if recorded_wo != current_wo:
        return False, (
            f"Workorder ID changed since review: "
            f"recorded='{recorded_wo}', current='{current_wo}'"
        )

    return True, ""


# ── Persistence ───────────────────────────────────────────────────────────────


def load_reviews(out_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the review store from ``out_dir/fsf_consistency_reviews.json``.

    Returns a dict keyed by step_id.  Returns an empty dict when the file
    does not exist or cannot be parsed.
    """
    store_path = Path(out_dir) / REVIEW_STORE_FILENAME
    if not store_path.is_file():
        return {}
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        reviews = raw.get("reviews", {})
        if not isinstance(reviews, dict):
            return {}
        return reviews
    except Exception:
        return {}


def save_reviews(
    reviews: dict[str, dict[str, Any]],
    out_dir: Path,
) -> None:
    """Persist the review store to ``out_dir/fsf_consistency_reviews.json``."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    store_path = Path(out_dir) / REVIEW_STORE_FILENAME
    payload = {
        "schema_version": "fsf_consistency_review_store_v1",
        "reviews": reviews,
    }
    store_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Pending-review listing ────────────────────────────────────────────────────


def list_shots_pending_review(
    journal: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    out_dir: Path,
    *,
    assembled_artifacts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of steps that require or have an invalidated review.

    **Scope**: a step is subject to the review gate when its journal entry has
    ``review_required=True`` (set by the executor from the plan's
    ``consistency_context.review_required``).

    When ``assembled_artifacts`` is supplied it must be a list of dicts, each with:
        step_id        – the step that produced this artifact
        artifact_ref   – the original artifact ref from the plan
        canonical_path – relative path from output_dir
        sha256         – SHA of the artifact **actually consumed** by assembly

    In that case only the listed artifacts are checked; other completed steps
    whose output is not being assembled are transparent.  The disk verification
    and review SHA check use the per-artifact ``canonical_path`` and ``sha256``
    directly — not ``output_files[0]``.  This correctly handles steps that
    produce multiple outputs (e.g. video + audio) and prevents binding the
    review to the wrong file.

    When ``None`` (backward-compat / CLI status checks), all completed/reused
    steps with ``review_required=True`` in the journal are scanned.

    Each entry in the returned list is a dict with:
        step_id, status ("missing" | "invalidated"), artifact_sha256,
        requirements_digest, workorder_id, reason.

    **Missing data is fatal**: if a step has ``review_required=True`` but its
    output_files or SHA is absent, it is listed as "missing" and the gate blocks.
    Silent skipping would allow unapproved shots through.

    **Disk-file verification**: the artifact MUST be locatable, present, and
    readable.  Missing path, deleted file, or read failure all block the gate.
    When the file is found, its SHA is recomputed and compared with the journal
    record to detect post-review edits.
    """
    pending: list[dict[str, Any]] = []
    steps = journal.get("steps", {})
    job_id = journal.get("job_id")
    run_id = journal.get("run_id")

    # ── Path A: per-artifact scope (assembled_artifacts provided) ─────────── #
    if assembled_artifacts is not None:
        for item in assembled_artifacts:
            step_id = item.get("step_id", "")
            canonical_path = item.get("canonical_path", "")
            artifact_sha = str(item.get("sha256", "")).lower().strip()

            step_data = steps.get(step_id, {})
            if not step_data:
                # Step not in journal — no review possible
                pending.append({
                    "step_id": step_id,
                    "status": "missing",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": None,
                    "workorder_id": "",
                    "reason": (
                        f"Assembled artifact from step '{step_id}' is not in the journal.  "
                        f"Cannot verify review gate."
                    ),
                })
                continue

            if not step_data.get("review_required", False):
                # Step does not require review — pass transparently
                continue

            if not canonical_path or not artifact_sha:
                pending.append({
                    "step_id": step_id,
                    "status": "missing",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": step_data.get("requirements_digest"),
                    "workorder_id": str(step_data.get("workorder_id") or ""),
                    "reason": (
                        f"Assembled artifact for step '{step_id}' is missing "
                        f"canonical_path or sha256.  Cannot verify review gate."
                    ),
                })
                continue

            # Disk-file verification using the specific assembled artifact path
            disk_path = Path(out_dir) / canonical_path
            try:
                disk_bytes = disk_path.read_bytes()
            except FileNotFoundError:
                pending.append({
                    "step_id": step_id,
                    "status": "missing",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": step_data.get("requirements_digest"),
                    "workorder_id": str(step_data.get("workorder_id") or ""),
                    "reason": (
                        f"Assembled artifact file not found on disk: '{canonical_path}'.  "
                        f"File may have been deleted or moved after the review was approved."
                    ),
                })
                continue
            except OSError as exc:
                pending.append({
                    "step_id": step_id,
                    "status": "missing",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": step_data.get("requirements_digest"),
                    "workorder_id": str(step_data.get("workorder_id") or ""),
                    "reason": (
                        f"Assembled artifact file read failed: '{canonical_path}': {exc}.  "
                        f"Cannot verify SHA for review gate."
                    ),
                })
                continue

            disk_sha = hashlib.sha256(disk_bytes).hexdigest().lower()
            if disk_sha != artifact_sha:
                pending.append({
                    "step_id": step_id,
                    "status": "invalidated",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": step_data.get("requirements_digest"),
                    "workorder_id": str(step_data.get("workorder_id") or ""),
                    "reason": (
                        f"Assembled artifact SHA mismatch for '{canonical_path}': "
                        f"expected={artifact_sha[:16]}…, disk={disk_sha[:16]}…."
                    ),
                })
                continue

            # Check review for this specific artifact SHA
            req_dig = step_data.get("requirements_digest")
            wo_id = str(step_data.get("workorder_id") or "").strip()
            review = reviews.get(step_id)
            if review is None:
                pending.append({
                    "step_id": step_id,
                    "status": "missing",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": req_dig,
                    "workorder_id": wo_id,
                    "reason": "No review record found for this step.",
                })
                continue

            valid, reason = is_review_valid(
                review,
                current_artifact_sha=artifact_sha,
                current_requirements_digest=req_dig,
                current_workorder_id=wo_id,
                expected_step_id=step_id,
                expected_job_id=job_id,
                expected_run_id=run_id,
            )
            if not valid:
                pending.append({
                    "step_id": step_id,
                    "status": "invalidated",
                    "artifact_sha256": artifact_sha,
                    "requirements_digest": req_dig,
                    "workorder_id": wo_id,
                    "reason": reason,
                })

        return pending

    # ── Path B: full-journal scan (assembled_artifacts not provided) ──────── #
    # Used by CLI status/list commands and as a fallback when no artifact_inputs.
    for step_id, step_data in steps.items():
        if step_data.get("status") not in ("completed", "reused"):
            continue

        # Scope: only steps explicitly marked review_required=True
        if not step_data.get("review_required", False):
            continue

        out_files = step_data.get("output_files") or []

        if not out_files:
            # Required data missing → block, don't skip
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": "",
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    "Step has review_required=True but no output_files in journal.  "
                    "Cannot verify artifact for review gate."
                ),
            })
            continue

        artifact_sha = str(out_files[0].get("sha256", "")).strip()
        artifact_path_rel = str(out_files[0].get("path", "")).strip()

        if not artifact_sha:
            # SHA missing → block
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": "",
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    "Step has review_required=True but output_files[0].sha256 is "
                    "absent in journal.  Cannot verify artifact for review gate."
                ),
            })
            continue

        # ── Disk-file verification ─────────────────────────────────────────#
        if not artifact_path_rel:
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": artifact_sha,
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    "output_files[0].path is absent in journal.  "
                    "Cannot locate artifact on disk for review gate."
                ),
            })
            continue

        disk_path = Path(out_dir) / artifact_path_rel
        try:
            disk_bytes = disk_path.read_bytes()
        except FileNotFoundError:
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": artifact_sha,
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    f"Artifact file not found on disk: '{artifact_path_rel}'.  "
                    f"File may have been deleted or moved after the review was approved."
                ),
            })
            continue
        except OSError as exc:
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": artifact_sha,
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    f"Artifact file read failed: '{artifact_path_rel}': {exc}.  "
                    f"Cannot verify SHA for review gate."
                ),
            })
            continue

        disk_sha = hashlib.sha256(disk_bytes).hexdigest().lower()
        if disk_sha != artifact_sha.lower():
            pending.append({
                "step_id": step_id,
                "status": "invalidated",
                "artifact_sha256": artifact_sha,
                "requirements_digest": step_data.get("requirements_digest"),
                "workorder_id": str(step_data.get("workorder_id") or ""),
                "reason": (
                    f"Artifact file SHA changed since journal write: "
                    f"journal={artifact_sha[:16]}…, disk={disk_sha[:16]}….  "
                    f"Any prior review is invalidated."
                ),
            })
            continue

        req_dig = step_data.get("requirements_digest")
        wo_id = str(step_data.get("workorder_id") or "").strip()

        review = reviews.get(step_id)
        if review is None:
            pending.append({
                "step_id": step_id,
                "status": "missing",
                "artifact_sha256": artifact_sha,
                "requirements_digest": req_dig,
                "workorder_id": wo_id,
                "reason": "No review record found for this step.",
            })
            continue

        valid, reason = is_review_valid(
            review,
            current_artifact_sha=artifact_sha,
            current_requirements_digest=req_dig,
            current_workorder_id=wo_id,
            expected_step_id=step_id,
            expected_job_id=job_id,
            expected_run_id=run_id,
        )
        if not valid:
            pending.append({
                "step_id": step_id,
                "status": "invalidated",
                "artifact_sha256": artifact_sha,
                "requirements_digest": req_dig,
                "workorder_id": wo_id,
                "reason": reason,
            })
        # is_review_valid() enforces verdict=="approved"; (True, "") means valid+approved.

    return pending


# ── Assembly gate ─────────────────────────────────────────────────────────────


def check_assembly_gate(
    journal: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    out_dir: Path,
    *,
    assembled_artifacts: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    """Check whether all shots with review_required=True have valid approved reviews.

    Returns ``(True, [])`` when the gate passes (assembly may proceed).
    Returns ``(False, blocking_reasons)`` when any shot is missing, invalidated,
    or has a non-approved verdict.

    ``assembled_artifacts`` should be supplied by the executor; it is a list of
    per-artifact dicts (step_id, artifact_ref, canonical_path, sha256) produced
    by ``_resolve_assembly_inputs()``.  When provided, only those specific
    artifacts are checked — not every completed step.  This prevents requiring
    review of intermediate products not actually consumed by assembly, and
    correctly handles steps with multiple output files by verifying the exact
    file the assembly will use.

    When ``None``, all completed/reused steps with ``review_required=True``
    in the journal are checked (used by the CLI status/list commands).

    Steps without ``review_required=True`` pass transparently.
    Reused steps are checked with the same rigour as freshly completed steps.
    """
    pending = list_shots_pending_review(
        journal, reviews, out_dir,
        assembled_artifacts=assembled_artifacts,
    )
    if not pending:
        return True, []

    reasons: list[str] = []
    for item in pending:
        sid = item["step_id"]
        state = item["status"]
        reason = item["reason"]
        reasons.append(f"Step '{sid}' [{state}]: {reason}")

    return False, reasons
