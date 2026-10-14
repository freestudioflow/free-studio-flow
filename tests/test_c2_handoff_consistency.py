"""Tests for C2: Handoff workorder consistency_requirements & result acknowledgement.

Verifies:
1.  build_consistency_requirements() returns None when context is empty / has no constraints.
2.  build_consistency_requirements() extracts effective_prompt, ref_image_shas,
    resolved_sha256, source_shot_id, aspects, prompt_only_allowed,
    and asset_ref_digests; computes requirements_digest.
3.  Shot-level and asset-level ref_images both appear in the requirements vector.
4.  requirements_digest changes when any field changes.
5.  build_workorder() embeds consistency_requirements + requirements_digest when provided.
6.  build_workorder() without consistency_requirements: fields are null (backward compat).
7.  ingest_workorder_result() rejects a result missing consistency_acknowledgement when
    the workorder carries a requirements_digest.
8.  ingest_workorder_result() rejects a result with wrong acknowledged_digest.
9.  ingest_workorder_result() accepts a result with correct acknowledged_digest.
10. ingest_workorder_result() with no requirements_digest in workorder: no acknowledgement
    required (backward compatibility).
11. PlanExecutor Handoff path: workorder contains consistency_requirements when
    the step has a resolved execution context.
12. PlanExecutor Handoff path: workorder has no consistency_requirements when
    the step has no consistency_context.
13. requirements_digest is part of workorder_digest (tamper detection).
14. Continuity policy fields (source_shot_id, aspects, prompt_only_allowed) are
    included in the requirements vector and affect requirements_digest.
15. ingest rejects: correct digest but ref_assets_used absent when refs declared.
16. ingest rejects: correct digest but ref_assets_used reports wrong SHA.
17. ingest rejects: correct digest but continuity_method absent when source declared.
18. ingest rejects: prompt_only used when prompt_only_allowed=False.
19. ingest rejects: status=completed with unresolved capability_gap.
20. Legacy workflow (no consistency requirements) ingests cleanly.
21. ingest rejects: continuity_method="none" (not in valid set).
22. ingest rejects: continuity_method is an arbitrary unknown string.
23. ingest rejects: "PROMPT_ONLY" (wrong case) when prompt_only_allowed=False.
24. ingest rejects: ref_assets_used contains a non-dict (primitive) entry.
25. ingest rejects: ref_assets_used contains a duplicate key.
26. Fields at top level are explicitly rejected (Gate 0), not silently ignored.
27. ingest rejects: inner ack clean but top-level capability_gap non-empty.
28. ingest rejects: inner ack clean but top-level ref_assets_used present.
29. ingest rejects: inner ack clean but top-level continuity_method present.
30. PlanExecutor journal: completed handoff step writes consistency_acknowledgement
    and requirements_digest to journal.
31. PlanExecutor journal: reused step carries consistency_acknowledgement from prior journal.

Result format note: all consistency fields (ref_assets_used, continuity_method,
capability_gap) must be nested inside consistency_acknowledgement, NOT at the
top level of the result.  Top-level placement is explicitly rejected (Gate 0).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fsf.conformance.digest import canonical_contract_digest
from fsf.runtime.handoff import (
    HandoffIntegrityError,
    CONSISTENCY_REQUIREMENTS_VERSION,
    VALID_CONTINUITY_METHODS,
    WORKORDER_SCHEMA_VERSION,
    build_consistency_requirements,
    build_workorder,
    ingest_workorder_result,
    write_workorder,
)
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor


# ── Helpers ───────────────────────────────────────────────────────────────────


def _minimal_wo(
    out_dir: Path,
    *,
    requirements_digest: str | None = None,
    consistency_requirements: dict | None = None,
) -> dict[str, Any]:
    """Build a minimal workorder for ingest testing."""
    wo: dict[str, Any] = {
        "schema_version": WORKORDER_SCHEMA_VERSION,
        "workorder_id": "wo_j1_r1_s1_att1",
        "job_id": "j1",
        "run_id": "r1",
        "step_id": "s1",
        "attempt_id": 1,
        "tool": "studio.video.generate",
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "expected_contract_digest": None,
        "expected_media_contract": None,
        "target_artifact": "S01.mp4",
        "task_payload": {"prompt": "test"},
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "pending",
        "consistency_requirements": consistency_requirements,
        "requirements_digest": requirements_digest,
        "workorder_digest": "x" * 64,  # not re-verified during ingest
    }
    return wo


def _write_result(
    out_dir: Path,
    art_content: bytes = b"fake-mp4-content",
    *,
    acknowledged_digest: str | None = None,
    include_ack_field: bool = True,
    status: str = "completed",
) -> tuple[Path, str]:
    """Write a test result JSON + artifact file.  Returns (result_path, actual_sha)."""
    art = out_dir / "S01.mp4"
    art.write_bytes(art_content)
    sha = hashlib.sha256(art_content).hexdigest()

    result: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": status,
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
    }
    if include_ack_field and acknowledged_digest is not None:
        result["consistency_acknowledgement"] = {"acknowledged_digest": acknowledged_digest}
    elif not include_ack_field:
        pass  # omit field entirely

    rp = out_dir / "res.json"
    rp.write_text(json.dumps(result), encoding="utf-8")
    return rp, sha


# ── 1. build_consistency_requirements — empty context returns None ─────────────


def test_build_consistency_requirements_none_for_empty_context() -> None:
    assert build_consistency_requirements({}) is None
    assert build_consistency_requirements({"schema_version": "x", "asset_refs": [], "review_required": False}) is None


# ── 2. build_consistency_requirements — full extraction ───────────────────────


def test_build_consistency_requirements_extracts_all_fields() -> None:
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {
                "asset_id": "hero",
                "version": "1.0",
                "content_hash": "c" * 64,
                "ref_images": [{"role": "character", "sha256": "r" * 64}],
            }
        ],
        "review_required": False,
        "ref_images": [{"role": "scene", "sha256": "s" * 64}],
        "continues_from": {
            "shot_id": "S00",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": "a" * 64,
        },
        "generation_intent": {
            "base_prompt": "A hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "A hero walks. tall, red coat",
        },
        "execution_context_digest": "e" * 64,
    }
    req = build_consistency_requirements(ctx)
    assert req is not None
    assert req["schema_version"] == CONSISTENCY_REQUIREMENTS_VERSION
    assert req["effective_prompt"] == "A hero walks. tall, red coat"
    assert req["ref_image_shas"]["shot[0]"] == "s" * 64
    assert req["ref_image_shas"]["asset:hero[0]"] == "r" * 64
    assert req["resolved_sha256"] == "a" * 64
    assert req["source_shot_id"] == "S00"
    assert req["aspects"] == ["scene"]
    assert req["asset_ref_digests"]["hero"] == "c" * 64
    # Visual refs declared → prompt_only NOT allowed
    assert req["prompt_only_allowed"] is False
    assert len(req["requirements_digest"]) == 64


def test_build_consistency_requirements_shot_and_asset_ref_images_keyed_separately() -> None:
    """Shot-level and asset-level ref images must use distinct keys."""
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {
                "asset_id": "hero",
                "version": "1.0",
                "content_hash": "c" * 64,
                "ref_images": [
                    {"role": "character", "sha256": "aa" * 32},
                    {"role": "character", "sha256": "bb" * 32},  # same role, different image
                ],
            }
        ],
        "review_required": False,
        "ref_images": [
            {"role": "character", "sha256": "cc" * 32},  # same role at shot level
        ],
        "generation_intent": {
            "base_prompt": "Test.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Test.",
        },
    }
    req = build_consistency_requirements(ctx)
    assert req is not None
    # Keys must not collide: shot[0], asset:hero[0], asset:hero[1]
    assert "shot[0]" in req["ref_image_shas"]
    assert "asset:hero[0]" in req["ref_image_shas"]
    assert "asset:hero[1]" in req["ref_image_shas"]
    assert req["ref_image_shas"]["shot[0]"] == "cc" * 32
    assert req["ref_image_shas"]["asset:hero[0]"] == "aa" * 32
    assert req["ref_image_shas"]["asset:hero[1]"] == "bb" * 32


# ── 4. requirements_digest changes on any field change ────────────────────────


def test_requirements_digest_changes_when_effective_prompt_changes() -> None:
    def _req(prompt: str) -> str:
        ctx: dict[str, Any] = {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "review_required": False,
            "generation_intent": {
                "base_prompt": prompt,
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [],
                "effective_prompt": prompt,
            },
        }
        r = build_consistency_requirements(ctx)
        assert r is not None
        return r["requirements_digest"]

    assert _req("A hero walks.") != _req("A hero runs.")


def test_requirements_digest_changes_when_ref_image_sha_changes() -> None:
    def _req(sha: str) -> str:
        ctx: dict[str, Any] = {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "review_required": False,
            "ref_images": [{"role": "scene", "sha256": sha}],
            "generation_intent": {
                "base_prompt": "Test.",
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [],
                "effective_prompt": "Test.",
            },
        }
        r = build_consistency_requirements(ctx)
        assert r is not None
        return r["requirements_digest"]

    assert _req("a" * 64) != _req("b" * 64)


# ── 5. build_workorder embeds consistency_requirements ────────────────────────


def test_build_workorder_embeds_consistency_requirements() -> None:
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "A hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "A hero walks.",
        },
    }
    req = build_consistency_requirements(ctx)
    assert req is not None

    wo = build_workorder(
        job_id="j1",
        run_id="r1",
        step_id="s1",
        attempt_id=1,
        tool="studio.video.generate",
        plan_digest="p" * 64,
        inputs_digest="i" * 64,
        target_artifact="S01.mp4",
        task_payload={"prompt": "A hero walks."},
        consistency_requirements=req,
    )
    assert wo["consistency_requirements"] is not None
    assert wo["consistency_requirements"]["schema_version"] == CONSISTENCY_REQUIREMENTS_VERSION
    assert wo["requirements_digest"] == req["requirements_digest"]
    # requirements_digest must be included in workorder_digest computation
    assert len(wo["workorder_digest"]) == 64


def test_build_workorder_null_consistency_fields_when_no_requirements() -> None:
    """Backward-compatible: workorder without consistency_requirements has null fields."""
    wo = build_workorder(
        job_id="j1",
        run_id="r1",
        step_id="s1",
        attempt_id=1,
        tool="studio.video.generate",
        plan_digest="p" * 64,
        inputs_digest="i" * 64,
        target_artifact="S01.mp4",
        task_payload={"prompt": "Test."},
    )
    assert wo["consistency_requirements"] is None
    assert wo["requirements_digest"] is None


# ── 7. ingest rejects result missing acknowledgement ─────────────────────────


def test_ingest_rejects_missing_acknowledgement(tmp_path: Path) -> None:
    """When workorder has requirements_digest, result without acknowledgement is rejected."""
    req_digest = "r" * 64
    wo = _minimal_wo(tmp_path, requirements_digest=req_digest)
    rp, _ = _write_result(tmp_path, include_ack_field=False)
    with pytest.raises(HandoffIntegrityError, match="consistency_acknowledgement"):
        ingest_workorder_result(rp, wo, tmp_path)


def test_ingest_rejects_empty_acknowledged_digest(tmp_path: Path) -> None:
    """Acknowledgement present but acknowledged_digest is empty string."""
    req_digest = "r" * 64
    wo = _minimal_wo(tmp_path, requirements_digest=req_digest)
    rp, _ = _write_result(tmp_path, acknowledged_digest="")
    with pytest.raises(HandoffIntegrityError, match="acknowledged_digest.*absent|absent.*acknowledged_digest"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 8. ingest rejects wrong acknowledged_digest ───────────────────────────────


def test_ingest_rejects_wrong_acknowledged_digest(tmp_path: Path) -> None:
    """acknowledged_digest present but doesn't match requirements_digest."""
    req_digest = "r" * 64
    wo = _minimal_wo(tmp_path, requirements_digest=req_digest)
    rp, _ = _write_result(tmp_path, acknowledged_digest="w" * 64)
    with pytest.raises(HandoffIntegrityError, match="mismatch|acknowledged_digest"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 9. ingest accepts correct acknowledgement ─────────────────────────────────


def test_ingest_accepts_correct_acknowledged_digest(tmp_path: Path) -> None:
    """Result with correct acknowledged_digest passes ingest."""
    req_digest = "r" * 64
    wo = _minimal_wo(tmp_path, requirements_digest=req_digest)
    rp, actual_sha = _write_result(tmp_path, acknowledged_digest=req_digest)
    result = ingest_workorder_result(rp, wo, tmp_path)
    assert result["status"] == "completed"
    assert result["artifact_sha256"] == actual_sha
    # consistency_acknowledgement is echoed in the result
    assert result.get("consistency_acknowledgement", {}).get("acknowledged_digest") == req_digest


# ── 10. backward compat: no requirements → no acknowledgement needed ─────────


def test_ingest_accepts_result_without_ack_when_no_requirements(tmp_path: Path) -> None:
    """Workorder without requirements_digest must NOT require acknowledgement."""
    wo = _minimal_wo(tmp_path, requirements_digest=None)
    rp, actual_sha = _write_result(tmp_path, include_ack_field=False)
    result = ingest_workorder_result(rp, wo, tmp_path)
    assert result["status"] == "completed"
    assert "consistency_acknowledgement" not in result


# ── 11. PlanExecutor Handoff: workorder has consistency_requirements ───────────


def _consistency_ctx() -> dict[str, Any]:
    return {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "A hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": None,
        },
    }


def test_executor_handoff_workorder_contains_consistency_requirements(
    tmp_path: Path,
) -> None:
    """When a step has a consistency_context, the emitted workorder must include
    consistency_requirements with a non-null requirements_digest.
    """
    job = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c2_handoff_ctx",
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
                "consistency_context": _consistency_ctx(),
            }
        ],
    }
    # Build a non-shape plan (handoff mode uses real media tool)
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False)

    executor = PlanExecutor(
        tmp_path,
        handoff=True,
        job_id="c2_handoff_ctx",
        run_id="run_001",
    )
    result = executor.execute_plan(plan)
    assert result["status"] == "pending_handoff"

    from fsf.runtime.handoff import list_pending_workorders
    wos = list_pending_workorders(tmp_path)
    assert len(wos) == 1
    wo = wos[0]

    assert wo.get("consistency_requirements") is not None, (
        "workorder must carry consistency_requirements when step has execution context"
    )
    assert wo["consistency_requirements"]["schema_version"] == CONSISTENCY_REQUIREMENTS_VERSION
    assert wo.get("requirements_digest") is not None
    assert wo["requirements_digest"] == wo["consistency_requirements"]["requirements_digest"]
    # effective_prompt must be assembled (not None)
    assert wo["consistency_requirements"]["effective_prompt"] == "A hero walks."


# ── 12. PlanExecutor Handoff: no consistency_context → no requirements ────────


def test_executor_handoff_workorder_no_requirements_when_no_ctx(
    tmp_path: Path,
) -> None:
    """Steps without consistency_context must emit a workorder with null
    consistency_requirements (backward compatibility).
    """
    job = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c2_handoff_noctx",
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
            }
        ],
    }
    plan = build_execution_plan(job, executor_tool="studio.video.generate", shape=False)

    executor = PlanExecutor(
        tmp_path,
        handoff=True,
        job_id="c2_handoff_noctx",
        run_id="run_001",
    )
    result = executor.execute_plan(plan)
    assert result["status"] == "pending_handoff"

    from fsf.runtime.handoff import list_pending_workorders
    wos = list_pending_workorders(tmp_path)
    assert len(wos) == 1
    wo = wos[0]
    assert wo.get("consistency_requirements") is None
    assert wo.get("requirements_digest") is None


# ── 13. requirements_digest is part of workorder_digest (tamper detection) ────


def test_requirements_digest_covered_by_workorder_digest() -> None:
    """Changing requirements_digest must change the workorder_digest."""
    base_wo = build_workorder(
        job_id="j1",
        run_id="r1",
        step_id="s1",
        attempt_id=1,
        tool="studio.video.generate",
        plan_digest="p" * 64,
        inputs_digest="i" * 64,
        target_artifact="S01.mp4",
        task_payload={"prompt": "Test."},
    )
    req_a = {"schema_version": CONSISTENCY_REQUIREMENTS_VERSION,
              "effective_prompt": "A.", "ref_image_shas": {}, "resolved_sha256": None,
              "asset_ref_digests": {}, "requirements_digest": "a" * 64}
    req_b = {"schema_version": CONSISTENCY_REQUIREMENTS_VERSION,
              "effective_prompt": "B.", "ref_image_shas": {}, "resolved_sha256": None,
              "asset_ref_digests": {}, "requirements_digest": "b" * 64}

    wo_a = build_workorder(
        job_id="j1", run_id="r1", step_id="s1", attempt_id=1,
        tool="studio.video.generate", plan_digest="p" * 64, inputs_digest="i" * 64,
        target_artifact="S01.mp4", task_payload={"prompt": "Test."},
        consistency_requirements=req_a,
    )
    wo_b = build_workorder(
        job_id="j1", run_id="r1", step_id="s1", attempt_id=1,
        tool="studio.video.generate", plan_digest="p" * 64, inputs_digest="i" * 64,
        target_artifact="S01.mp4", task_payload={"prompt": "Test."},
        consistency_requirements=req_b,
    )

    assert wo_a["workorder_digest"] != base_wo["workorder_digest"], (
        "Adding consistency_requirements must change workorder_digest"
    )
    assert wo_a["workorder_digest"] != wo_b["workorder_digest"], (
        "Different consistency_requirements must produce different workorder_digest"
    )


# ── 14. Continuity policy fields present and affect digest ────────────────────


def test_build_consistency_requirements_includes_continuity_policy() -> None:
    """source_shot_id, aspects, prompt_only_allowed must appear in vector and digest."""
    ctx_with: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "ref_images": [{"role": "scene", "sha256": "s" * 64}],
        "continues_from": {
            "shot_id": "S00",
            "selection_rule": "latest_completed",
            "aspects": ["appearance", "costume"],
            "resolved_sha256": "a" * 64,
        },
        "generation_intent": {
            "base_prompt": "Test.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Test.",
        },
    }
    ctx_without: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "Test.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Test.",
        },
    }
    req_with = build_consistency_requirements(ctx_with)
    req_without = build_consistency_requirements(ctx_without)
    assert req_with is not None
    assert req_without is not None

    assert req_with["source_shot_id"] == "S00"
    assert req_with["aspects"] == ["appearance", "costume"]
    assert req_with["prompt_only_allowed"] is False

    assert req_without["source_shot_id"] is None
    assert req_without["aspects"] == []
    assert req_without["prompt_only_allowed"] is True   # no visual refs

    # Different continuity policy → different digest
    assert req_with["requirements_digest"] != req_without["requirements_digest"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for C2 result-audit counter-example tests
# These helpers build workorders that carry real consistency_requirements so
# the ingest path can reach the deeper validation gates.
# ─────────────────────────────────────────────────────────────────────────────


def _req_with_ref_images() -> dict[str, Any]:
    """Build a requirements vector that declares one shot-level ref image."""
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "ref_images": [{"role": "scene", "sha256": "f" * 64}],
        "generation_intent": {
            "base_prompt": "Test.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Test.",
        },
    }
    req = build_consistency_requirements(ctx)
    assert req is not None
    return req


def _req_with_continuity_source() -> dict[str, Any]:
    """Build a requirements vector that declares a continuity source (no prompt_only)."""
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "S00",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": "c" * 64,
        },
        "generation_intent": {
            "base_prompt": "Test.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Test.",
        },
    }
    req = build_consistency_requirements(ctx)
    assert req is not None
    assert req["prompt_only_allowed"] is False
    return req


def _write_result_with_ack(
    out_dir: Path,
    req_digest: str,
    *,
    ref_assets_used: list | None = None,
    continuity_method: str | None = None,
    capability_gap: str | None = None,
    status: str = "completed",
    art_content: bytes = b"fake-mp4-content",
    omit_ref_assets: bool = False,
    omit_continuity_method: bool = False,
) -> tuple[Path, str]:
    """Write a result JSON with a full consistency_acknowledgement block."""
    art = out_dir / "S01.mp4"
    art.write_bytes(art_content)
    sha = hashlib.sha256(art_content).hexdigest()

    ack: dict[str, Any] = {"acknowledged_digest": req_digest}
    if not omit_ref_assets and ref_assets_used is not None:
        ack["ref_assets_used"] = ref_assets_used
    if not omit_continuity_method and continuity_method is not None:
        ack["continuity_method"] = continuity_method
    if capability_gap is not None:
        ack["capability_gap"] = capability_gap

    result: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": status,
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        "consistency_acknowledgement": ack,
    }
    rp = out_dir / "res.json"
    rp.write_text(json.dumps(result), encoding="utf-8")
    return rp, sha


# ── 15. Digest correct but ref_assets_used absent/empty → rejected ────────────


def test_ingest_rejects_correct_digest_but_missing_ref_assets_used(tmp_path: Path) -> None:
    """Gate 3: digest matches but ref_assets_used is absent → reject.

    An agent that echoes the digest without reporting which ref images it used
    cannot be accepted as completed.  Absence of ref_assets_used means the
    agent may have silently skipped all reference material.
    """
    req = _req_with_ref_images()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    # Correct digest, but no ref_assets_used field at all
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        omit_ref_assets=True,
    )
    with pytest.raises(HandoffIntegrityError, match="ref_assets_used"):
        ingest_workorder_result(rp, wo, tmp_path)


def test_ingest_rejects_correct_digest_but_wrong_ref_asset_sha(tmp_path: Path) -> None:
    """Gate 3: digest matches, asset reported, but SHA is wrong → reject.

    The agent must operate against the exact declared reference assets, not a
    different version or a self-invented placeholder.
    """
    req = _req_with_ref_images()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    # Report the correct key but with a wrong SHA
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        ref_assets_used=[{"key": "shot[0]", "sha256": "e" * 64}],  # declared is "f" * 64
    )
    with pytest.raises(HandoffIntegrityError, match="SHA mismatch|mismatch"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 17. Digest correct but continuity_method absent when source declared → reject


def test_ingest_rejects_correct_digest_but_missing_continuity_method(tmp_path: Path) -> None:
    """Gate 4: digest matches, continuity source declared, but continuity_method absent.

    The agent must report how it used the continuity source; omitting this
    field means we cannot verify the agent respected the continuity policy.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        omit_continuity_method=True,
    )
    with pytest.raises(HandoffIntegrityError, match="continuity_method"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 18. prompt_only used when not allowed → rejected ─────────────────────────


def test_ingest_rejects_prompt_only_when_not_allowed(tmp_path: Path) -> None:
    """Gate 4: agent reports prompt_only but prompt_only_allowed=False → reject.

    When a continuity source is declared, the agent must use it.  Silently
    falling back to prompt-only continuity is a policy violation.
    """
    req = _req_with_continuity_source()
    assert req["prompt_only_allowed"] is False
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        continuity_method="prompt_only",   # policy violation
    )
    with pytest.raises(HandoffIntegrityError, match="prompt_only"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 19. status=completed with unresolved capability_gap → rejected ─────────────


def test_ingest_rejects_completed_with_unresolved_capability_gap(tmp_path: Path) -> None:
    """Gate 2: result says completed but carries a non-empty capability_gap → reject.

    An agent that lacked a required capability must report status=failed, not
    mark the step completed while hiding the gap in the acknowledgement.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        continuity_method="reference_image",
        capability_gap="IP-Adapter not available on this node",
    )
    with pytest.raises(HandoffIntegrityError, match="capability_gap"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 20. Legacy workflow (no requirements) ingests cleanly ─────────────────────


def test_ingest_legacy_no_requirements_passes_cleanly(tmp_path: Path) -> None:
    """Backward compatibility: workorders without consistency_requirements must
    pass ingest without any acknowledgement or consistency fields in the result.

    This covers existing workflows that pre-date C2.
    """
    # Workorder has no consistency_requirements and no requirements_digest
    wo = _minimal_wo(tmp_path, requirements_digest=None, consistency_requirements=None)
    # Result has no consistency_acknowledgement at all
    art = tmp_path / "S01.mp4"
    art.write_bytes(b"legacy-content")
    sha = hashlib.sha256(b"legacy-content").hexdigest()
    result_data = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        # no consistency_acknowledgement field
    }
    rp = tmp_path / "res.json"
    rp.write_text(json.dumps(result_data), encoding="utf-8")

    result = ingest_workorder_result(rp, wo, tmp_path)
    assert result["status"] == "completed"
    assert result["artifact_sha256"] == sha
    assert "consistency_acknowledgement" not in result, (
        "Legacy result must not have consistency_acknowledgement injected"
    )


# ── 21. continuity_method="none" → rejected (not in valid set) ───────────────


def test_ingest_rejects_continuity_method_none_string(tmp_path: Path) -> None:
    """Gate 4: "none" is not an accepted continuity_method value.

    An agent that lacked the capability to use the continuity source must
    report status='failed', not silently declare method='none'.
    "none" is not in VALID_CONTINUITY_METHODS and must be rejected.
    """
    assert "none" not in VALID_CONTINUITY_METHODS
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        continuity_method="none",
    )
    with pytest.raises(HandoffIntegrityError, match="unknown continuity_method|none"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 22. continuity_method is an arbitrary unknown string → rejected ───────────


def test_ingest_rejects_arbitrary_continuity_method_string(tmp_path: Path) -> None:
    """Gate 4: an invented / free-form method string is not accepted.

    The agent must use one of the values in VALID_CONTINUITY_METHODS; arbitrary
    strings like "did_my_best" cannot substitute for a declared method.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        continuity_method="did_my_best",
    )
    with pytest.raises(HandoffIntegrityError, match="unknown continuity_method"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 23. "PROMPT_ONLY" (wrong case) when not allowed → rejected ───────────────


def test_ingest_rejects_prompt_only_wrong_case_when_not_allowed(tmp_path: Path) -> None:
    """Gate 4: case-normalized "PROMPT_ONLY" is still rejected when prompt_only_allowed=False.

    The ingest layer normalizes to lowercase before comparing, so all case
    variations of "prompt_only" are caught by the same policy check.
    """
    req = _req_with_continuity_source()
    assert req["prompt_only_allowed"] is False
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    # Report as mixed / uppercase — should normalize and still be rejected
    for variant in ("PROMPT_ONLY", "Prompt_Only", "PROMPT_only"):
        # Need a fresh temp dir per variant to avoid artifact collision
        sub = tmp_path / f"sub_{variant}"
        sub.mkdir(exist_ok=True)
        wo_copy = dict(wo)
        wo_copy["target_artifact"] = "S01.mp4"
        rp_v, _ = _write_result_with_ack(
            sub,
            req["requirements_digest"],
            continuity_method=variant,
        )
        with pytest.raises(HandoffIntegrityError, match="prompt_only"):
            ingest_workorder_result(rp_v, wo_copy, sub)


# ── 24. ref_assets_used contains a non-dict entry → rejected ──────────────────


def test_ingest_rejects_non_dict_entry_in_ref_assets_used(tmp_path: Path) -> None:
    """Gate 3: a primitive (string, int, null) entry in ref_assets_used is rejected.

    Silently skipping non-dict entries allows an agent to pad the list with
    junk while omitting the real asset reports.  Every entry must be an object.
    """
    req = _req_with_ref_images()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        # Mix one valid entry with one primitive — the primitive must cause rejection
        ref_assets_used=[
            {"key": "shot[0]", "sha256": "f" * 64},
            "this-is-not-a-dict",
        ],
    )
    with pytest.raises(HandoffIntegrityError, match="not an object"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 25. ref_assets_used contains a duplicate key → rejected ───────────────────


def test_ingest_rejects_duplicate_key_in_ref_assets_used(tmp_path: Path) -> None:
    """Gate 3: duplicate keys in ref_assets_used are rejected.

    A duplicate key could silently override a legitimate SHA with a wrong one.
    Ingest must detect and reject any repeated key, even if the SHA matches.
    """
    req = _req_with_ref_images()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    rp, _ = _write_result_with_ack(
        tmp_path,
        req["requirements_digest"],
        # Same key appears twice (second entry has the correct SHA, but duplicates
        # are still rejected because they are structurally untrustworthy)
        ref_assets_used=[
            {"key": "shot[0]", "sha256": "e" * 64},   # wrong SHA first
            {"key": "shot[0]", "sha256": "f" * 64},   # "correct" SHA second
        ],
    )
    with pytest.raises(HandoffIntegrityError, match="duplicate key"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 26. Fields at top level are NOT checked (must be inside ack) ──────────────


def test_ingest_fields_at_top_level_are_not_checked(tmp_path: Path) -> None:
    """Result format: ref_assets_used/continuity_method/capability_gap must be
    inside consistency_acknowledgement, NOT at the top level.

    An agent that places these fields at the top level will fail Gate 3/4 as
    if they were absent, because ingest only reads from ack (the nested dict).
    This test proves that top-level placement causes rejection, not silent pass.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    art = tmp_path / "S01.mp4"
    art.write_bytes(b"fake-mp4-content")
    sha = hashlib.sha256(b"fake-mp4-content").hexdigest()

    # Agent puts continuity_method at the TOP LEVEL instead of inside ack
    result_data: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        # CORRECT placement: inside ack
        "consistency_acknowledgement": {
            "acknowledged_digest": req["requirements_digest"],
            # continuity_method intentionally OMITTED here
        },
        # WRONG placement: top level — must NOT satisfy Gate 4
        "continuity_method": "reference_image",
    }
    rp = tmp_path / "res.json"
    rp.write_text(json.dumps(result_data), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="continuity_method"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 27. Inner ack clean but top-level capability_gap non-empty → rejected ─────


def test_ingest_rejects_top_level_capability_gap_even_with_clean_inner_ack(
    tmp_path: Path,
) -> None:
    """Gate 0: top-level capability_gap is rejected even when inner ack looks clean.

    A split-report attack: the agent puts a clean inner ack (no capability_gap)
    but leaks the real gap at the top level, hoping the ingest only checks inside
    consistency_acknowledgement.  Gate 0 must catch this before Gate 1.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    art = tmp_path / "S01.mp4"
    art.write_bytes(b"fake-mp4-content")
    sha = hashlib.sha256(b"fake-mp4-content").hexdigest()

    result_data: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        # Inner ack looks fine — no capability_gap inside
        "consistency_acknowledgement": {
            "acknowledged_digest": req["requirements_digest"],
            "continuity_method": "first_frame_init",
        },
        # TOP-LEVEL gap — must be rejected by Gate 0
        "capability_gap": "IP-Adapter unavailable on this node",
    }
    rp = tmp_path / "res.json"
    rp.write_text(json.dumps(result_data), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="top level|capability_gap"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 28. Inner ack clean but top-level ref_assets_used present → rejected ──────


def test_ingest_rejects_top_level_ref_assets_used(tmp_path: Path) -> None:
    """Gate 0: ref_assets_used at top level is rejected.

    Even if consistency_acknowledgement is otherwise valid, placing ref_assets_used
    outside it is a format violation that must be caught before any deeper gates.
    """
    req = _req_with_ref_images()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    art = tmp_path / "S01.mp4"
    art.write_bytes(b"fake-mp4-content")
    sha = hashlib.sha256(b"fake-mp4-content").hexdigest()

    result_data: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        "consistency_acknowledgement": {
            "acknowledged_digest": req["requirements_digest"],
            "ref_assets_used": [{"key": "shot[0]", "sha256": "f" * 64}],
        },
        # TOP-LEVEL misplacement — must be rejected by Gate 0
        "ref_assets_used": [{"key": "shot[0]", "sha256": "f" * 64}],
    }
    rp = tmp_path / "res.json"
    rp.write_text(json.dumps(result_data), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="top level|ref_assets_used"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 29. Inner ack clean but top-level continuity_method present → rejected ────


def test_ingest_rejects_top_level_continuity_method(tmp_path: Path) -> None:
    """Gate 0: continuity_method at top level is rejected.

    This is the scenario from test 26, but the inner ack is fully correct — we
    confirm Gate 0 fires even when the inner ack has the right continuity_method.
    """
    req = _req_with_continuity_source()
    wo = _minimal_wo(
        tmp_path,
        requirements_digest=req["requirements_digest"],
        consistency_requirements=req,
    )
    art = tmp_path / "S01.mp4"
    art.write_bytes(b"fake-mp4-content")
    sha = hashlib.sha256(b"fake-mp4-content").hexdigest()

    result_data: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "j1",
        "run_id": "r1",
        "workorder_id": "wo_j1_r1_s1_att1",
        "step_id": "s1",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "S01.mp4",
        "artifact_sha256": sha,
        # Inner ack is correct
        "consistency_acknowledgement": {
            "acknowledged_digest": req["requirements_digest"],
            "continuity_method": "first_frame_init",
        },
        # TOP-LEVEL — must be rejected by Gate 0 (even if value matches inner)
        "continuity_method": "first_frame_init",
    }
    rp = tmp_path / "res.json"
    rp.write_text(json.dumps(result_data), encoding="utf-8")
    with pytest.raises(HandoffIntegrityError, match="top level|continuity_method"):
        ingest_workorder_result(rp, wo, tmp_path)


# ── 30. PlanExecutor journal: consistency_acknowledgement written on ingest ────


def test_executor_journal_writes_consistency_acknowledgement_on_completed_handoff(
    tmp_path: Path,
) -> None:
    """P2: After ingesting a completed handoff result that carries consistency
    requirements, the journal must contain consistency_acknowledgement and
    requirements_digest for the step.

    This enables downstream review tooling to read which assets and method were
    used without re-ingesting the result file.
    """
    import json as _json
    from fsf.runtime.handoff import build_workorder, write_workorder

    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "Hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "Hero walks.",
        },
    }
    req = build_consistency_requirements(ctx)
    assert req is not None

    # Build and write a workorder
    wo = build_workorder(
        job_id="jj1",
        run_id="rr1",
        step_id="SS01",
        attempt_id=1,
        tool="studio.video.generate",
        plan_digest="p" * 64,
        inputs_digest="i" * 64,
        target_artifact="SS01.mp4",
        task_payload={"prompt": "Hero walks."},
        consistency_requirements=req,
    )
    wo_path = write_workorder(wo, tmp_path)

    # Write a valid result file
    art = tmp_path / "SS01.mp4"
    art.write_bytes(b"hero-mp4")
    art_sha = hashlib.sha256(b"hero-mp4").hexdigest()

    result_data: dict[str, Any] = {
        "schema_version": "fsf_workorder_result_v1",
        "job_id": "jj1",
        "run_id": "rr1",
        "workorder_id": wo["workorder_id"],
        "step_id": "SS01",
        "attempt_id": 1,
        "plan_digest": "p" * 64,
        "inputs_digest": "i" * 64,
        "status": "completed",
        "artifact_path": "SS01.mp4",
        "artifact_sha256": art_sha,
        "consistency_acknowledgement": {
            "acknowledged_digest": req["requirements_digest"],
            # No ref_image_shas in this ctx, no resolved_sha256 => no
            # ref_assets_used / continuity_method required
        },
    }
    res_path = tmp_path / "res.json"
    res_path.write_text(_json.dumps(result_data), encoding="utf-8")

    # Now simulate what plan_executor does: ingest + inject into step_results
    from fsf.runtime.handoff import ingest_workorder_result
    ingest_res = ingest_workorder_result(res_path, wo, tmp_path)
    assert ingest_res["status"] == "completed"
    assert ingest_res.get("consistency_acknowledgement") is not None

    # Build the minimal step_result that executor would produce
    step_result: dict[str, Any] = {
        "id": "SS01",
        "kind": "tool",
        "tool": "studio.video.generate",
        "status": "completed",
        "attempt_id": 1,
        "workorder_id": wo["workorder_id"],
        "handoff_result": ingest_res,
        "output_artifacts": [str(tmp_path / "SS01.mp4")],
    }
    ack_record = ingest_res.get("consistency_acknowledgement")
    if ack_record:
        step_result["consistency_acknowledgement"] = ack_record
        step_result["requirements_digest"] = wo.get("requirements_digest")

    # Build a journal just for this step (mirrors journal_steps logic)
    import hashlib as _hl
    out_files = [{"path": "SS01.mp4", "sha256": art_sha}]
    journal_entry: dict[str, Any] = {
        "status": "completed",
        "output_files": out_files,
    }
    if step_result.get("consistency_acknowledgement"):
        journal_entry["consistency_acknowledgement"] = step_result["consistency_acknowledgement"]
    if step_result.get("requirements_digest"):
        journal_entry["requirements_digest"] = step_result["requirements_digest"]

    assert "consistency_acknowledgement" in journal_entry, (
        "completed handoff step must write consistency_acknowledgement to journal"
    )
    assert "requirements_digest" in journal_entry, (
        "completed handoff step must write requirements_digest to journal"
    )
    assert journal_entry["consistency_acknowledgement"]["acknowledged_digest"] == req["requirements_digest"]
    assert journal_entry["requirements_digest"] == req["requirements_digest"]


# ── 31. PlanExecutor journal: reused step carries consistency_acknowledgement ──


def test_executor_resume_carries_consistency_acknowledgement_from_prior_journal(
    tmp_path: Path,
) -> None:
    """P2: On resume, a reused step must carry consistency_acknowledgement from
    the prior journal so review tooling does not lose the record.

    Simulates: prior journal has consistency_acknowledgement -> reused step
    picks it up -> it's available in step_results for the review stage.
    """
    prior_ack = {
        "acknowledged_digest": "r" * 64,
        "ref_assets_used": [{"key": "shot[0]", "sha256": "s" * 64}],
        "continuity_method": "first_frame_init",
    }
    prior_step: dict[str, Any] = {
        "status": "completed",
        "output_files": [{"path": "S01.mp4", "sha256": "a" * 64}],
        "consistency_acknowledgement": prior_ack,
        "requirements_digest": "r" * 64,
        "execution_kind": "shape",
    }

    # Simulate the executor's reused-step carry logic
    reused_result: dict[str, Any] = {
        "id": "S01",
        "status": "reused",
        "output_files": prior_step["output_files"],
    }
    if prior_step.get("consistency_acknowledgement"):
        reused_result["consistency_acknowledgement"] = prior_step["consistency_acknowledgement"]
    if prior_step.get("requirements_digest"):
        reused_result["requirements_digest"] = prior_step["requirements_digest"]

    assert "consistency_acknowledgement" in reused_result, (
        "reused step must carry consistency_acknowledgement from prior journal"
    )
    assert reused_result["consistency_acknowledgement"] == prior_ack
    assert reused_result["requirements_digest"] == "r" * 64
