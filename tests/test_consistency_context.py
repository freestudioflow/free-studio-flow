"""Tests for B1: fsf_consistency_context_v1 schema, builder, and compiler integration.

Covers:
- Schema loads from installed package (wheel-accessible).
- Valid context passes validate_consistency_context.
- Plan containing consistency_context passes validate_execution_plan.
- Original shot_spec and asset_spec are not mutated.
- Asset order in asset_refs and ref_images is preserved in digest.
- Invalid inputs are rejected (unknown asset, unknown shot, cycle,
  resolved_sha256 at compile time, base_prompt mismatch, schema violations,
  asset_injection references undeclared asset).
- build_execution_plan rejects raw (non-builder) contexts with schema errors.
- build_execution_plan rejects raw contexts referencing unknown assets.
- Continuity depends_on is inserted automatically into the correct step.
- Deep copy: external mutation after build does not affect the plan.
- Isolated wheel install: schema accessible from installed package.
"""

from __future__ import annotations

import copy
import json

import pytest

from pdx_artifact_core import validate_execution_plan

from fsf.core.consistency import (
    ConsistencyCompileError,
    build_consistency_context,
    canonical_asset_digest,
    canonical_context_digest,
    validate_consistency_context,
)
from fsf.runtime.plan import build_execution_plan


# ?? Fixtures ??????????????????????????????????????????????????????????????????

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _make_shot_spec(shot_id: str, prompt: str = "A hero walks.") -> dict:
    return {
        "schema_version": "fsf_shot_spec_v1",
        "shot_id": shot_id,
        "source_prompt": prompt,
        "generation_prompt": prompt,
        "mode": "text_to_video",
        "duration_seconds": 5.0,
        "reference_image": None,
        "semantics": {
            "subjects": [], "setting": None, "action_beats": [],
            "camera": {"shot_scale": None, "movement": None, "composition": None},
            "audio": {"dialogue": [], "ambience": [], "music": None},
        },
        "constraints": {
            "preserve": ["source_prompt_verbatim"],
            "forbid": ["unrequested_visible_text"],
            "unresolved": ["subjects", "setting", "action_beats", "camera"],
        },
        "provenance": {
            "source_kind": "docx",
            "source_document": "test.docx",
            "source_sha256": "0" * 64,
            "source_index": 0,
            "planner": "none:deterministic-baseline",
        },
        "needs_semantic_review": True,
    }


def _make_asset_ref(asset_id: str, version: str = "1.0") -> dict:
    content_hash = canonical_asset_digest(
        asset_id=asset_id,
        version=version,
        source_prompt=f"Asset {asset_id}",
        kind="unspecified",
        constraints={"preserve": [], "forbid": [], "unresolved": []},
        ref_images=[],
    )
    return {"asset_id": asset_id, "version": version, "content_hash": content_hash}


def _make_asset_ref_for_spec(spec: dict, version: str = "1.0") -> dict:
    """Build an asset_ref whose content_hash matches the given asset_spec."""
    content_hash = canonical_asset_digest(
        asset_id=spec["asset_id"],
        version=version,
        source_prompt=spec.get("source_prompt", ""),
        kind=spec.get("kind", "unspecified"),
        constraints=spec.get("constraints", {}),
        ref_images=[],
    )
    return {"asset_id": spec["asset_id"], "version": version, "content_hash": content_hash}


def _minimal_context(
    *,
    asset_refs: list | None = None,
    shot_id: str = "T01",
    shot_spec: dict | None = None,
    known_asset_ids: set | None = None,
    known_shot_ids: set | None = None,
    shot_order: list | None = None,
) -> dict:
    if asset_refs is None:
        asset_refs = []
    if shot_spec is None:
        shot_spec = _make_shot_spec(shot_id)
    if known_asset_ids is None:
        known_asset_ids = set()
    if known_shot_ids is None:
        known_shot_ids = {shot_id}
    if shot_order is None:
        shot_order = [shot_id]
    return build_consistency_context(
        asset_refs=asset_refs,
        shot_id=shot_id,
        shot_spec=shot_spec,
        known_asset_ids=known_asset_ids,
        known_shot_ids=known_shot_ids,
        shot_order=shot_order,
    )


def _three_shot_job() -> dict:
    """Build a minimal fsf_video_job_v1 with 3 shots and 1 asset."""
    return {
        "schema_version": "fsf_video_job_v1",
        "source_document": "test.docx",
        "source_sha256": "0" * 64,
        "tasks": [
            {
                "id": "T01",
                "kind": "shot",
                "prompt": "A hero walks.",
                "mode": "text_to_video",
                "duration_seconds": 5.0,
                "shot_spec": _make_shot_spec("T01", "A hero walks."),
                "expected_media_contract": {"width": 1024, "height": 576, "fps": 24,
                                            "duration_seconds": 5.0, "audio": "allowed"},
                "expected_media_contract_source": "intake_default",
            },
            {
                "id": "T02",
                "kind": "shot",
                "prompt": "The hero runs.",
                "mode": "text_to_video",
                "duration_seconds": 5.0,
                "shot_spec": _make_shot_spec("T02", "The hero runs."),
                "expected_media_contract": {"width": 1024, "height": 576, "fps": 24,
                                            "duration_seconds": 5.0, "audio": "allowed"},
                "expected_media_contract_source": "intake_default",
            },
            {
                "id": "T03",
                "kind": "shot",
                "prompt": "The hero stops.",
                "mode": "text_to_video",
                "duration_seconds": 5.0,
                "shot_spec": _make_shot_spec("T03", "The hero stops."),
                "expected_media_contract": {"width": 1024, "height": 576, "fps": 24,
                                            "duration_seconds": 5.0, "audio": "allowed"},
                "expected_media_contract_source": "intake_default",
            },
        ],
    }


def _three_shot_job_with_asset() -> dict:
    """Build a minimal fsf_video_job_v1 with 3 shots and 1 'hero' asset task."""
    job = _three_shot_job()
    job["tasks"].insert(0, {
        "id": "hero",
        "unit_kind": "asset",          # is_shot_unit() checks unit_kind, not kind
        "kind": "asset",
        "prompt": "A tall hero in a red coat.",
        "asset_spec": {
            "schema_version": "fsf_asset_spec_v1",
            "asset_id": "hero",
            "source_prompt": "A tall hero in a red coat.",
            "kind": "unspecified",
            "constraints": {"preserve": [], "forbid": [], "unresolved": []},
            "provenance": {
                "source_kind": "docx",
                "source_document": "test.docx",
                "source_sha256": "0" * 64,
                "source_index": 0,
                "planner": "none:deterministic-baseline",
            },
            "needs_semantic_review": True,
        },
    })
    return job


# ?? Schema loading tests ???????????????????????????????????????????????????????


def test_schema_loadable_from_package() -> None:
    """Schema must be loadable via importlib.resources (wheel-accessible)."""
    from importlib import resources
    root = resources.files("fsf.schemas")
    text = (root / "fsf_consistency_context_v1.schema.json").read_text(encoding="utf-8")
    schema = json.loads(text)
    assert schema["$id"].endswith("consistency_context.v1.schema.json")
    assert schema["properties"]["schema_version"]["const"] == "fsf_consistency_context_v1"


def test_schema_valid_minimal_context() -> None:
    """A minimal context (no optionals) passes schema validation."""
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
    }
    errors = validate_consistency_context(ctx)
    assert errors == [], errors


def test_schema_valid_full_context() -> None:
    """A fully populated context passes schema validation."""
    ar = _make_asset_ref("hero")
    ar["ref_images"] = [{"role": "character", "sha256": _SHA_A, "path": "/tmp/hero.png"}]
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [ar],
        "ref_images": [{"role": "scene", "sha256": _SHA_B, "path": "/tmp/bg.png"}],
        "continues_from": {
            "shot_id": "T01",
            "selection_rule": "latest_completed",
            "aspects": ["appearance", "costume"],
            "resolved_sha256": None,
        },
        "generation_intent": {
            "base_prompt": "The hero runs.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [{"asset_id": "hero", "injected_text": "[hero: tall, red coat]"}],
            "effective_prompt": None,
        },
        "review_required": True,
    }
    errors = validate_consistency_context(ctx)
    assert errors == [], errors


# ?? Schema rejection tests ?????????????????????????????????????????????????????


def test_schema_rejects_missing_schema_version() -> None:
    ctx = {"asset_refs": [], "review_required": False}
    errors = validate_consistency_context(ctx)
    assert any("schema_version" in e for e in errors)


def test_schema_rejects_wrong_schema_version() -> None:
    ctx = {"schema_version": "fsf_consistency_context_v2", "asset_refs": [], "review_required": False}
    errors = validate_consistency_context(ctx)
    assert errors  # const mismatch


def test_schema_rejects_bad_sha256() -> None:
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "x", "version": "1", "content_hash": "not_a_sha"}],
        "review_required": False,
    }
    errors = validate_consistency_context(ctx)
    assert errors


def test_schema_rejects_extra_top_level_field() -> None:
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "unexpected_field": "bad",
    }
    errors = validate_consistency_context(ctx)
    assert errors


def test_schema_rejects_specific_artifact_without_artifact_ref() -> None:
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "T01",
            "selection_rule": "specific_artifact",
            # artifact_ref missing
            "aspects": ["appearance"],
            "resolved_sha256": None,
        },
    }
    errors = validate_consistency_context(ctx)
    assert errors


def test_schema_rejects_invalid_aspect() -> None:
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "T01",
            "selection_rule": "latest_completed",
            "aspects": ["hair_color"],  # not in enum
            "resolved_sha256": None,
        },
    }
    errors = validate_consistency_context(ctx)
    assert errors


def test_schema_rejects_invalid_ref_image_role() -> None:
    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "ref_images": [{"role": "weapon", "sha256": _SHA_A}],  # not in enum
    }
    errors = validate_consistency_context(ctx)
    assert errors


# ?? Compile-time integrity tests ???????????????????????????????????????????????


def test_build_context_unknown_asset_rejected() -> None:
    with pytest.raises(ConsistencyCompileError, match="unknown asset"):
        _minimal_context(
            asset_refs=[_make_asset_ref("nonexistent")],
            known_asset_ids=set(),
        )


def test_build_context_unknown_continues_from_shot_rejected() -> None:
    with pytest.raises(ConsistencyCompileError, match="unknown shot"):
        build_consistency_context(
            asset_refs=[],
            continues_from={
                "shot_id": "GHOST",
                "selection_rule": "latest_completed",
                "aspects": ["appearance"],
                "resolved_sha256": None,
            },
            shot_id="T02",
            shot_spec=_make_shot_spec("T02"),
            known_asset_ids=set(),
            known_shot_ids={"T01", "T02"},
            shot_order=["T01", "T02"],
        )


def test_build_context_continues_from_self_rejected() -> None:
    with pytest.raises(ConsistencyCompileError, match="cannot reference itself"):
        build_consistency_context(
            asset_refs=[],
            continues_from={
                "shot_id": "T01",
                "selection_rule": "latest_completed",
                "aspects": ["scene"],
                "resolved_sha256": None,
            },
            shot_id="T01",
            shot_spec=_make_shot_spec("T01"),
            known_asset_ids=set(),
            known_shot_ids={"T01"},
            shot_order=["T01"],
        )


def test_build_context_continues_from_future_shot_rejected() -> None:
    """A shot cannot continue from a shot declared later in job order."""
    with pytest.raises(ConsistencyCompileError, match="must be declared before"):
        build_consistency_context(
            asset_refs=[],
            continues_from={
                "shot_id": "T03",
                "selection_rule": "latest_completed",
                "aspects": ["appearance"],
                "resolved_sha256": None,
            },
            shot_id="T02",
            shot_spec=_make_shot_spec("T02"),
            known_asset_ids=set(),
            known_shot_ids={"T01", "T02", "T03"},
            shot_order=["T01", "T02", "T03"],
        )


def test_build_context_resolved_sha256_at_compile_time_rejected() -> None:
    """resolved_sha256 must be null at compile time."""
    with pytest.raises(ConsistencyCompileError, match="must be null at compile time"):
        build_consistency_context(
            asset_refs=[],
            continues_from={
                "shot_id": "T01",
                "selection_rule": "latest_completed",
                "aspects": ["scene"],
                "resolved_sha256": _SHA_A,  # must be null
            },
            shot_id="T02",
            shot_spec=_make_shot_spec("T02"),
            known_asset_ids=set(),
            known_shot_ids={"T01", "T02"},
            shot_order=["T01", "T02"],
        )


def test_build_context_base_prompt_mismatch_rejected() -> None:
    """base_prompt must match shot_spec.generation_prompt."""
    spec = _make_shot_spec("T01", "The hero walks.")
    with pytest.raises(ConsistencyCompileError, match="does not match shot_spec.generation_prompt"):
        build_consistency_context(
            asset_refs=[],
            generation_intent={
                "base_prompt": "DIFFERENT PROMPT",
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [],
                "effective_prompt": None,
            },
            shot_id="T01",
            shot_spec=spec,
            known_asset_ids=set(),
            known_shot_ids={"T01"},
            shot_order=["T01"],
        )


def test_build_context_effective_prompt_always_null_at_compile_time() -> None:
    """effective_prompt is forced to None at compile time regardless of input."""
    spec = _make_shot_spec("T01", "The hero walks.")
    ctx = build_consistency_context(
        asset_refs=[],
        generation_intent={
            "base_prompt": "The hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "THIS MUST BE OVERRIDDEN",  # caller attempt to set it
        },
        shot_id="T01",
        shot_spec=spec,
        known_asset_ids=set(),
        known_shot_ids={"T01"},
        shot_order=["T01"],
    )
    assert ctx["generation_intent"]["effective_prompt"] is None


# ?? Cycle detection ????????????????????????????????????????????????????????????


def test_cycle_detection_direct() -> None:
    from fsf.core.consistency import detect_continuity_cycles
    contexts = {
        "T01": {"continues_from": {"shot_id": "T02", "aspects": ["scene"], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
        "T02": {"continues_from": {"shot_id": "T01", "aspects": ["scene"], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
    }
    cycle = detect_continuity_cycles(contexts)
    assert len(cycle) > 0
    assert set(cycle).issubset({"T01", "T02"})


def test_cycle_detection_three_node() -> None:
    from fsf.core.consistency import detect_continuity_cycles
    contexts = {
        "T01": {"continues_from": {"shot_id": "T03", "aspects": [], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
        "T02": {"continues_from": {"shot_id": "T01", "aspects": [], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
        "T03": {"continues_from": {"shot_id": "T02", "aspects": [], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
    }
    cycle = detect_continuity_cycles(contexts)
    assert len(cycle) > 0


def test_cycle_detection_dag_no_cycle() -> None:
    from fsf.core.consistency import detect_continuity_cycles
    contexts = {
        "T02": {"continues_from": {"shot_id": "T01", "aspects": ["scene"], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
        "T03": {"continues_from": {"shot_id": "T02", "aspects": ["appearance"], "resolved_sha256": None,
                                   "selection_rule": "latest_completed"}},
    }
    cycle = detect_continuity_cycles(contexts)
    assert cycle == []


# ?? Digest tests ???????????????????????????????????????????????????????????????


def test_asset_order_preserved_in_context_digest() -> None:
    """Changing asset_refs order changes the digest (order is significant)."""
    ar1 = _make_asset_ref("hero", "1.0")
    ar2 = _make_asset_ref("scene", "1.0")

    def _ctx(refs):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": refs,
            "review_required": False,
        }

    d1 = canonical_context_digest(_ctx([ar1, ar2]))
    d2 = canonical_context_digest(_ctx([ar2, ar1]))
    assert d1 != d2, "Asset order must affect the digest"


def test_path_change_does_not_affect_context_digest() -> None:
    """Changing ref_images[].path must NOT change the digest."""
    def _ctx(path):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "ref_images": [{"role": "scene", "sha256": _SHA_A, "path": path}],
            "review_required": False,
        }

    d1 = canonical_context_digest(_ctx("/old/path/bg.png"))
    d2 = canonical_context_digest(_ctx("/new/path/bg.png"))
    assert d1 == d2, "Path change must not affect digest"


def test_sha256_change_affects_context_digest() -> None:
    """Changing ref_images[].sha256 must change the digest."""
    def _ctx(sha):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "ref_images": [{"role": "scene", "sha256": sha, "path": "/img.png"}],
            "review_required": False,
        }

    d1 = canonical_context_digest(_ctx(_SHA_A))
    d2 = canonical_context_digest(_ctx(_SHA_B))
    assert d1 != d2


def test_effective_prompt_excluded_from_digest() -> None:
    """effective_prompt changes must NOT change the context digest."""
    def _ctx(effective):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "review_required": False,
            "generation_intent": {
                "base_prompt": "hero walks",
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [],
                "effective_prompt": effective,
            },
        }

    d1 = canonical_context_digest(_ctx(None))
    d2 = canonical_context_digest(_ctx("hero walks [hero: tall man]"))
    assert d1 == d2, "effective_prompt must not affect digest"


def test_injected_text_included_in_digest() -> None:
    """Changing asset_injections[].injected_text must change the digest."""
    def _ctx(text):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "review_required": False,
            "generation_intent": {
                "base_prompt": "hero walks",
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [{"asset_id": "hero", "injected_text": text}],
                "effective_prompt": None,
            },
        }

    d1 = canonical_context_digest(_ctx("tall man in red"))
    d2 = canonical_context_digest(_ctx("short woman in blue"))
    assert d1 != d2


def test_injection_rule_version_in_digest() -> None:
    """Changing injection_rule_version must change the digest."""
    def _ctx(ver):
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "review_required": False,
            "generation_intent": {
                "base_prompt": "hero walks",
                "injection_rule_version": ver,
                "asset_injections": [],
                "effective_prompt": None,
            },
        }

    d1 = canonical_context_digest(_ctx("fsf_injection_v1"))
    d2 = canonical_context_digest(_ctx("fsf_injection_v2"))
    assert d1 != d2


# ?? Plan integration tests ?????????????????????????????????????????????????????


def test_plan_with_context_passes_pdx_validation() -> None:
    """A plan with consistency_context in step.inputs passes validate_execution_plan."""
    job = _three_shot_job_with_asset()
    hero_spec = job["tasks"][0]["asset_spec"]  # tasks[0] is the asset
    ar = _make_asset_ref_for_spec(hero_spec)
    ctx = _minimal_context(
        asset_refs=[ar],
        shot_id="T01",
        shot_spec=job["tasks"][1]["shot_spec"],
        known_asset_ids={"hero"},
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    job["tasks"][1]["consistency_context"] = ctx

    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    errors = validate_execution_plan(plan)
    assert errors == [], f"PDX validation errors: {errors}"


def test_plan_without_context_unchanged() -> None:
    """A plan without consistency_context compiles exactly as before (no regression)."""
    job = _three_shot_job()
    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    errors = validate_execution_plan(plan)
    assert errors == []
    # No step should have consistency_context in inputs
    for step in plan["steps"]:
        assert "consistency_context" not in step.get("inputs", {})


def test_original_shot_spec_not_mutated() -> None:
    """build_execution_plan must not mutate the original shot_spec."""
    job = _three_shot_job_with_asset()
    t01_task = job["tasks"][1]  # tasks[0] is the asset
    original_spec = copy.deepcopy(t01_task["shot_spec"])
    hero_spec = job["tasks"][0]["asset_spec"]
    ar = _make_asset_ref_for_spec(hero_spec)
    ctx = _minimal_context(
        asset_refs=[ar],
        shot_id="T01",
        shot_spec=t01_task["shot_spec"],
        known_asset_ids={"hero"},
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    t01_task["consistency_context"] = ctx

    build_execution_plan(job, executor_tool="studio.video.generate")

    assert t01_task["shot_spec"] == original_spec, (
        "build_execution_plan must not mutate the original shot_spec"
    )


def test_context_is_in_step_inputs_not_shot_spec() -> None:
    """consistency_context appears in step.inputs, not inside shot_spec."""
    job = _three_shot_job_with_asset()
    hero_spec = job["tasks"][0]["asset_spec"]
    ar = _make_asset_ref_for_spec(hero_spec)
    ctx = _minimal_context(
        asset_refs=[ar],
        shot_id="T01",
        shot_spec=job["tasks"][1]["shot_spec"],  # tasks[0] is asset
        known_asset_ids={"hero"},
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    job["tasks"][1]["consistency_context"] = ctx
    plan = build_execution_plan(job, executor_tool="studio.video.generate")

    t01_steps = [s for s in plan["steps"] if s.get("inputs", {}).get("shot_id") == "T01"]
    assert t01_steps, "No steps found for T01"
    first_step = t01_steps[0]
    assert "consistency_context" in first_step["inputs"]
    assert "consistency_context" not in first_step["inputs"].get("shot_spec", {})


def test_continuity_depends_on_inserted() -> None:
    """T03 continues_from T02 ? T03's first step must depend on T02's last step."""
    job = _three_shot_job()

    # T02 continues from T01
    ctx_t02 = build_consistency_context(
        asset_refs=[],
        continues_from={
            "shot_id": "T01",
            "selection_rule": "latest_completed",
            "aspects": ["appearance"],
            "resolved_sha256": None,
        },
        shot_id="T02",
        shot_spec=job["tasks"][1]["shot_spec"],
        known_asset_ids=set(),
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    job["tasks"][1]["consistency_context"] = ctx_t02

    # T03 continues from T02
    ctx_t03 = build_consistency_context(
        asset_refs=[],
        continues_from={
            "shot_id": "T02",
            "selection_rule": "latest_completed",
            "aspects": ["scene", "appearance"],
            "resolved_sha256": None,
        },
        shot_id="T03",
        shot_spec=job["tasks"][2]["shot_spec"],
        known_asset_ids=set(),
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    job["tasks"][2]["consistency_context"] = ctx_t03

    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    errors = validate_execution_plan(plan)
    assert errors == [], f"PDX validation errors: {errors}"

    # Find the first step of T02 and T03, and the last step of T01 and T02
    steps_by_shot: dict[str, list[dict]] = {}
    for s in plan["steps"]:
        sid = s.get("inputs", {}).get("shot_id")
        if sid:
            steps_by_shot.setdefault(sid, []).append(s)

    t01_last = steps_by_shot["T01"][-1]["id"]
    t02_first = steps_by_shot["T02"][0]["id"]
    t02_last = steps_by_shot["T02"][-1]["id"]
    t03_first = steps_by_shot["T03"][0]["id"]

    t02_first_step = next(s for s in plan["steps"] if s["id"] == t02_first)
    t03_first_step = next(s for s in plan["steps"] if s["id"] == t03_first)

    assert t01_last in t02_first_step["depends_on"], (
        f"T02 first step should depend on T01 last step {t01_last}; "
        f"got {t02_first_step['depends_on']}"
    )
    assert t02_last in t03_first_step["depends_on"], (
        f"T03 first step should depend on T02 last step {t02_last}; "
        f"got {t03_first_step['depends_on']}"
    )


def test_compiler_rejects_continuity_cycle() -> None:
    """build_execution_plan rejects a job whose contexts form a cycle."""
    job = _three_shot_job()
    # T01 ? T02 ? T01 (cycle)
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "T02",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": None,
        },
    }
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "T01",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": None,
        },
    }
    with pytest.raises((ConsistencyCompileError, ValueError), match="[Cc]ycle"):
        build_execution_plan(job, executor_tool="studio.video.generate")


# ?w?w P1 ???Gbuild_execution_plan ?????????l context ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_compiler_rejects_raw_context_with_schema_error() -> None:
    """build_execution_plan rejects a context injected directly that fails schema validation.

    This is the P1 reproducer: injecting {"unexpected": True} must be caught
    by the compiler even though the builder was not used.
    """
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "unexpected": True,
    }
    with pytest.raises((ConsistencyCompileError, ValueError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_raw_context_with_unknown_asset() -> None:
    """build_execution_plan rejects a raw context referencing an unknown asset."""
    job = _three_shot_job()
    ar = _make_asset_ref("ghost_asset")
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [ar],
        "review_required": False,
    }
    with pytest.raises((ConsistencyCompileError, ValueError), match="unknown asset"):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_raw_context_with_bad_sha_in_asset_ref() -> None:
    """build_execution_plan rejects a raw context whose content_hash is malformed."""
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "x", "version": "1", "content_hash": "not64chars"}],
        "review_required": False,
    }
    with pytest.raises((ConsistencyCompileError, ValueError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


# ?w?w P1 ???G????????????v?T?K?n ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_ref_images_order_affects_context_digest() -> None:
    """Swapping two ref_images entries must produce a different context digest."""
    def _ctx(role_first: str, role_second: str) -> dict:
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "ref_images": [
                {"role": role_first,  "sha256": _SHA_A, "path": "/img1.png"},
                {"role": role_second, "sha256": _SHA_B, "path": "/img2.png"},
            ],
            "review_required": False,
        }

    d1 = canonical_context_digest(_ctx("character", "scene"))
    d2 = canonical_context_digest(_ctx("scene", "character"))
    assert d1 != d2, "Swapping character/scene ref_image order must change the digest"


def test_asset_ref_images_order_affects_asset_digest() -> None:
    """Swapping ref_images order in canonical_asset_digest must change the hash."""
    h1 = canonical_asset_digest(
        "hero", "1.0", "hero prompt", "character",
        {"preserve": [], "forbid": [], "unresolved": []},
        [{"role": "character", "sha256": _SHA_A}, {"role": "style", "sha256": _SHA_B}],
    )
    h2 = canonical_asset_digest(
        "hero", "1.0", "hero prompt", "character",
        {"preserve": [], "forbid": [], "unresolved": []},
        [{"role": "style", "sha256": _SHA_B}, {"role": "character", "sha256": _SHA_A}],
    )
    assert h1 != h2, "ref_images order must affect canonical_asset_digest"


def test_same_ref_images_same_order_same_digest() -> None:
    """Same ref_images in same order always produces the same digest (stability)."""
    def _make() -> dict:
        return {
            "schema_version": "fsf_consistency_context_v1",
            "asset_refs": [],
            "ref_images": [
                {"role": "character", "sha256": _SHA_A},
                {"role": "scene",     "sha256": _SHA_B},
            ],
            "review_required": False,
        }
    assert canonical_context_digest(_make()) == canonical_context_digest(_make())


# ?w?w asset_injections ??????i?? ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_builder_rejects_injection_of_undeclared_asset() -> None:
    """asset_injections must only reference assets declared in asset_refs."""
    spec = _make_shot_spec("T01", "hero walks")
    with pytest.raises(ConsistencyCompileError, match="not declared in this shot"):
        build_consistency_context(
            asset_refs=[],
            generation_intent={
                "base_prompt": "hero walks",
                "injection_rule_version": "fsf_injection_v1",
                "asset_injections": [{"asset_id": "hero", "injected_text": "tall man"}],
                "effective_prompt": None,
            },
            shot_id="T01",
            shot_spec=spec,
            known_asset_ids={"hero"},
            known_shot_ids={"T01"},
            shot_order=["T01"],
        )


def test_builder_accepts_injection_of_declared_asset() -> None:
    """asset_injections referencing a declared asset_ref must pass."""
    ar = _make_asset_ref("hero")
    spec = _make_shot_spec("T01", "hero walks")
    ctx = build_consistency_context(
        asset_refs=[ar],
        generation_intent={
            "base_prompt": "hero walks",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [{"asset_id": "hero", "injected_text": "tall man"}],
            "effective_prompt": None,
        },
        shot_id="T01",
        shot_spec=spec,
        known_asset_ids={"hero"},
        known_shot_ids={"T01"},
        shot_order=["T01"],
    )
    assert ctx["generation_intent"]["asset_injections"][0]["asset_id"] == "hero"


# ?w?w P2 ?`?????G?~??????v?T?w?s????? ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_external_mutation_of_asset_refs_does_not_affect_built_context() -> None:
    """Mutating the input asset_refs list after build must not change the context."""
    ar = _make_asset_ref("hero")
    original_version = ar["version"]
    asset_refs = [ar]
    spec = _make_shot_spec("T01")
    ctx = build_consistency_context(
        asset_refs=asset_refs,
        shot_id="T01",
        shot_spec=spec,
        known_asset_ids={"hero"},
        known_shot_ids={"T01"},
        shot_order=["T01"],
    )
    # Mutate original inputs
    asset_refs[0]["version"] = "MUTATED"
    asset_refs.append(_make_asset_ref("extra"))

    assert ctx["asset_refs"][0]["version"] == original_version
    assert len(ctx["asset_refs"]) == 1


def test_external_mutation_of_task_context_does_not_affect_plan() -> None:
    """Mutating a task consistency_context after plan compilation must not affect the plan."""
    job = _three_shot_job_with_asset()
    hero_spec = job["tasks"][0]["asset_spec"]
    ar = _make_asset_ref_for_spec(hero_spec)
    t01_task = job["tasks"][1]  # tasks[0] is asset
    ctx = build_consistency_context(
        asset_refs=[ar],
        shot_id="T01",
        shot_spec=t01_task["shot_spec"],
        known_asset_ids={"hero"},
        known_shot_ids={"T01", "T02", "T03"},
        shot_order=["T01", "T02", "T03"],
    )
    t01_task["consistency_context"] = ctx

    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    t01_steps = [s for s in plan["steps"] if s.get("inputs", {}).get("shot_id") == "T01"]
    version_in_plan = t01_steps[0]["inputs"]["consistency_context"]["asset_refs"][0]["version"]

    # Mutate the task context after compilation
    ctx["asset_refs"][0]["version"] = "MUTATED_AFTER_COMPILE"

    version_after = t01_steps[0]["inputs"]["consistency_context"]["asset_refs"][0]["version"]
    assert version_after == version_in_plan, (
        "Mutating task context after compilation must not affect the compiled plan"
    )


# ?w?w P1 ???Gcontent_hash ???????????]?w ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_compiler_rejects_context_with_correct_format_wrong_content_hash() -> None:
    """A 64-char hex string that is not the canonical digest must be rejected.

    This is the P1 reproducer: valid SHA-256 format but wrong content.
    """
    job = _three_shot_job_with_asset()
    # Forge a plausible-looking but wrong hash
    wrong_hash = "d" * 64
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "hero", "version": "1.0", "content_hash": wrong_hash}],
        "review_required": False,
    }
    with pytest.raises((ConsistencyCompileError, ValueError), match="content_hash mismatch"):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_stale_hash_after_spec_change() -> None:
    """A context built from the old spec must be rejected after the spec changes.

    Simulates the operator re-using an old content_hash after editing source_prompt.
    """
    job = _three_shot_job_with_asset()
    asset_task = job["tasks"][0]  # the hero asset task

    # Compute correct hash based on original spec
    spec = asset_task["asset_spec"]
    correct_hash = canonical_asset_digest(
        asset_id=spec["asset_id"],
        version="1.0",
        source_prompt=spec["source_prompt"],
        kind=spec["kind"],
        constraints=spec["constraints"],
        ref_images=[],
    )

    # Now mutate the spec (operator edited the description)
    asset_task["asset_spec"] = {**spec, "source_prompt": "A short hero in a blue coat."}

    # But still use the old hash
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "hero", "version": "1.0", "content_hash": correct_hash}],
        "review_required": False,
    }
    with pytest.raises((ConsistencyCompileError, ValueError), match="content_hash mismatch"):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_accepts_correct_content_hash() -> None:
    """A context with a correctly recomputed content_hash must be accepted."""
    job = _three_shot_job_with_asset()
    spec = job["tasks"][0]["asset_spec"]   # hero asset is tasks[0]
    correct_hash = canonical_asset_digest(
        asset_id=spec["asset_id"],
        version="1.0",
        source_prompt=spec["source_prompt"],
        kind=spec["kind"],
        constraints=spec["constraints"],
        ref_images=[],
    )
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "hero", "version": "1.0", "content_hash": correct_hash}],
        "review_required": False,
    }
    # Must compile without error
    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    errors = validate_execution_plan(plan)
    assert errors == []


# ?w?w P1 ???Geffective_prompt ?D null ?????Q??? ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_compiler_rejects_raw_context_with_nonnull_effective_prompt() -> None:
    """A context injected directly into the manifest with a non-null effective_prompt
    must be rejected by the compiler.

    This is the P1 reproducer: builder forces null, but raw manifest injection
    must also be caught.
    """
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "A hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "external override",   # forbidden at compile time
        },
    }
    with pytest.raises((ConsistencyCompileError, ValueError), match="effective_prompt"):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_accepts_null_effective_prompt() -> None:
    """A context with effective_prompt explicitly set to null must be accepted."""
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {
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
    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    assert validate_execution_plan(plan) == []


def test_compiler_accepts_absent_effective_prompt() -> None:
    """A context with no generation_intent at all must be accepted."""
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        # no generation_intent key at all
    }
    plan = build_execution_plan(job, executor_tool="studio.video.generate")
    assert validate_execution_plan(plan) == []


# ?w?w P2 ???G?? context ?????Q???? ?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w?w


def test_compiler_rejects_empty_dict_context() -> None:
    """consistency_context={} must be caught and rejected, not silently skipped.

    This is the P2 reproducer: if _ctx (truthy check) skips {}.
    """
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = {}   # empty dict, falsy under bool()
    with pytest.raises((ConsistencyCompileError, ValueError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_wrong_type_context() -> None:
    """A non-dict consistency_context must be rejected."""
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = "not_a_dict"
    with pytest.raises((ConsistencyCompileError, ValueError, TypeError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_list_context() -> None:
    """A list consistency_context must be rejected."""
    job = _three_shot_job()
    job["tasks"][0]["consistency_context"] = [{"schema_version": "fsf_consistency_context_v1"}]
    with pytest.raises((ConsistencyCompileError, ValueError, TypeError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


# ── P1 反例：asset_spec 缺失時必須拒絕，不得略過 ──────────────────────────────────


def test_compiler_rejects_context_when_asset_has_no_spec() -> None:
    """An asset task that exists but has no asset_spec must cause rejection.

    P1 reproducer: asset ID is in the task list, content_hash has valid format,
    but the task carries no asset_spec field, so the hash cannot be verified.
    """
    job = _three_shot_job_with_asset()
    # Remove the asset_spec from the asset task so it falls out of asset_specs_by_id
    del job["tasks"][0]["asset_spec"]

    # Build a plausible-looking context with valid SHA-256 format
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "hero", "version": "1.0", "content_hash": "a" * 64}],
        "review_required": False,
    }
    with pytest.raises(
        (ConsistencyCompileError, ValueError),
        match="no authoritative asset_spec",
    ):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_context_when_some_assets_have_no_spec() -> None:
    """When multiple assets are referenced and only some have specs, all must be present.

    P1 partial-missing reproducer.
    """
    job = _three_shot_job_with_asset()
    hero_spec = job["tasks"][0]["asset_spec"]
    correct_hero_hash = canonical_asset_digest(
        asset_id=hero_spec["asset_id"],
        version="1.0",
        source_prompt=hero_spec["source_prompt"],
        kind=hero_spec["kind"],
        constraints=hero_spec["constraints"],
        ref_images=[],
    )

    # Add a second asset task with no asset_spec
    job["tasks"].insert(
        1,
        {
            "id": "sidekick",
            "unit_kind": "asset",
            "shot_spec": None,
            # Deliberately missing "asset_spec"
        },
    )

    # Shot T01 references both assets; hero hash is correct, sidekick has no spec
    job["tasks"][2]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {"asset_id": "hero", "version": "1.0", "content_hash": correct_hero_hash},
            {"asset_id": "sidekick", "version": "1.0", "content_hash": "b" * 64},
        ],
        "review_required": False,
    }
    with pytest.raises(
        (ConsistencyCompileError, ValueError),
        match="no authoritative asset_spec",
    ):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_context_when_spec_asset_id_mismatches_ref() -> None:
    """An asset_spec whose asset_id does not match the task id must cause a mismatch.

    P1 ID-mismatch reproducer: digest is computed from spec.asset_id, so if
    asset_spec.asset_id differs from the ref asset_id the hashes diverge.
    """
    job = _three_shot_job_with_asset()
    spec = job["tasks"][0]["asset_spec"]

    # Tamper: asset_spec carries a different asset_id than the task id
    job["tasks"][0]["asset_spec"] = {**spec, "asset_id": "NOT_hero"}

    # Content hash was computed against the original "hero" id
    hero_hash_with_original_id = canonical_asset_digest(
        asset_id="hero",          # original id
        version="1.0",
        source_prompt=spec["source_prompt"],
        kind=spec["kind"],
        constraints=spec["constraints"],
        ref_images=[],
    )
    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {"asset_id": "hero", "version": "1.0", "content_hash": hero_hash_with_original_id}
        ],
        "review_required": False,
    }
    # spec.asset_id is "NOT_hero", so recomputed hash uses "NOT_hero" ? mismatch
    with pytest.raises((ConsistencyCompileError, ValueError)):
        build_execution_plan(job, executor_tool="studio.video.generate")


# ?? P1 ???asset_spec.asset_id ??????????? ???????????????????????????


def test_compiler_rejects_spec_id_mismatch_even_when_hash_matches_spec() -> None:
    """spec.asset_id != ref.asset_id must be rejected even when the hash was
    recomputed from the tampered spec (so the digest is internally consistent).

    P1 reproducer:
      task / ref id  = "hero"
      spec.asset_id  = "other"
      content_hash   = canonical_asset_digest(asset_id="other", ...)  <- matches spec
    Before this fix: spec.get("asset_id", aid) fell back to "hero" causing a
    hash mismatch (rejected for the wrong reason / wrong error message).
    After fix: explicit identity check fires before hash computation.
    """
    job = _three_shot_job_with_asset()
    spec = job["tasks"][0]["asset_spec"]  # asset_id == "hero"

    # Tamper spec to carry a different asset_id
    tampered_spec = {**spec, "asset_id": "other"}
    job["tasks"][0]["asset_spec"] = tampered_spec

    # Hash computed from the tampered spec (asset_id="other") ? internally consistent
    hash_from_tampered = canonical_asset_digest(
        asset_id="other",
        version="1.0",
        source_prompt=tampered_spec["source_prompt"],
        kind=tampered_spec["kind"],
        constraints=tampered_spec["constraints"],
        ref_images=[],
    )

    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {"asset_id": "hero", "version": "1.0", "content_hash": hash_from_tampered}
        ],
        "review_required": False,
    }
    # Must be rejected explicitly for identity mismatch, not hash mismatch
    with pytest.raises(
        (ConsistencyCompileError, ValueError),
        match="does not match",
    ):
        build_execution_plan(job, executor_tool="studio.video.generate")


def test_compiler_rejects_spec_missing_asset_id_field() -> None:
    """asset_spec without an asset_id field must be rejected before hash computation.

    P1 missing-field reproducer: spec exists in job manifest but asset_id key
    is absent ? must not fall back to the ref aid or compute any hash.
    """
    job = _three_shot_job_with_asset()
    spec = job["tasks"][0]["asset_spec"]

    # Drop the asset_id field from the spec entirely
    spec_without_id = {k: v for k, v in spec.items() if k != "asset_id"}
    job["tasks"][0]["asset_spec"] = spec_without_id

    job["tasks"][1]["consistency_context"] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [{"asset_id": "hero", "version": "1.0", "content_hash": "c" * 64}],
        "review_required": False,
    }
    with pytest.raises(
        (ConsistencyCompileError, ValueError),
        match="missing the asset_id field",
    ):
        build_execution_plan(job, executor_tool="studio.video.generate")
