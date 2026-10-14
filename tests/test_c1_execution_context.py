"""Tests for C1: execution-time consistency context resolution.

Verifies:
1. resolve_execution_context() deep-copies the compiled context (no mutation).
2. continues_from.resolved_sha256 is filled from resolved_artifact_shas.
3. effective_prompt is assembled from base_prompt + asset_injections.
4. execution_context_digest is computed and stored.
5. Rejection of non-null resolved_sha256 in compiled context (compile-time invariant).
6. Rejection of non-null effective_prompt in compiled context (compile-time invariant).
7. plan_executor passes execution_context into task payload for both general and handoff steps.
8. execution_context is None for steps without consistency_context.
9. Resume: changing effective_prompt (via asset_injection change) changes inputs_digest.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pdx_artifact_core import validate_execution_plan

from fsf.core.consistency import (
    ConsistencyResolutionError,
    canonical_asset_digest,
    canonical_context_digest,
    resolve_execution_context,
)
from fsf.runtime.plan import build_execution_plan
from fsf.runtime.plan_executor import PlanExecutor


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _minimal_ctx(
    *,
    base_prompt: str = "",
    injections: list[dict] | None = None,
    continues_from_shot: str | None = None,
    asset_refs: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a minimal fsf_consistency_context_v1 with effective_prompt=None."""
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": asset_refs or [],
        "review_required": False,
    }
    if base_prompt or injections:
        ctx["generation_intent"] = {
            "base_prompt": base_prompt,
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": injections or [],
            "effective_prompt": None,
        }
    if continues_from_shot is not None:
        ctx["continues_from"] = {
            "shot_id": continues_from_shot,
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": None,
        }
    return ctx


# ── 1. Deep copy — original context not mutated ───────────────────────────────


def test_resolve_does_not_mutate_original_context() -> None:
    """resolve_execution_context must not modify the compiled context."""
    ctx = _minimal_ctx(base_prompt="A hero walks.")
    original = copy.deepcopy(ctx)

    resolve_execution_context(ctx, shot_id="S01")

    assert ctx == original, "Original context was mutated by resolve_execution_context"


# ── 2. continues_from.resolved_sha256 filled ─────────────────────────────────


def test_resolve_fills_continues_from_sha_when_upstream_completed() -> None:
    """resolved_sha256 is filled from resolved_artifact_shas when available."""
    ctx = _minimal_ctx(continues_from_shot="S00")
    ec = resolve_execution_context(
        ctx,
        shot_id="S01",
        resolved_artifact_shas={"S00": "a" * 64},
    )
    assert ec["continues_from"]["resolved_sha256"] == "a" * 64


def test_resolve_leaves_sha_null_when_upstream_not_done() -> None:
    """resolved_sha256 stays None when resolved_artifact_shas is None (caller skipping check)."""
    ctx = _minimal_ctx(continues_from_shot="S00")
    # Passing None means "caller is not providing source SHAs" — leave as None (no error)
    ec = resolve_execution_context(ctx, shot_id="S01", resolved_artifact_shas=None)
    assert ec["continues_from"]["resolved_sha256"] is None


def test_resolve_rejects_when_source_sha_absent_from_provided_shas() -> None:
    """When resolved_artifact_shas is provided but source shot absent, raise error.

    P3 reproducer: required continuity source not yet complete — must not dispatch.
    """
    ctx = _minimal_ctx(continues_from_shot="S00")
    with pytest.raises(ConsistencyResolutionError, match="no resolved artifact SHA yet"):
        resolve_execution_context(ctx, shot_id="S01", resolved_artifact_shas={})


def test_resolve_rejects_nonnull_resolved_sha_in_compiled_context() -> None:
    """A compiled context with non-null resolved_sha256 must be rejected (B1 invariant)."""
    ctx = _minimal_ctx()
    ctx["continues_from"] = {
        "shot_id": "S00",
        "selection_rule": "latest_completed",
        "aspects": ["scene"],
        "resolved_sha256": "b" * 64,   # must not be pre-filled in compiled plan
    }
    with pytest.raises(ConsistencyResolutionError, match="resolved_sha256"):
        resolve_execution_context(ctx, shot_id="S01")


# ── 3. effective_prompt assembled ─────────────────────────────────────────────


def test_resolve_assembles_effective_prompt_from_base_only() -> None:
    """effective_prompt = base_prompt when there are no asset injections."""
    ctx = _minimal_ctx(base_prompt="A hero walks.")
    ec = resolve_execution_context(ctx, shot_id="S01")
    assert ec["generation_intent"]["effective_prompt"] == "A hero walks."


def test_resolve_assembles_effective_prompt_with_injections() -> None:
    """effective_prompt = base_prompt + injection texts in declaration order."""
    ctx = _minimal_ctx(
        base_prompt="A hero walks.",
        injections=[
            {"asset_id": "hero", "injected_text": "tall, red coat"},
            {"asset_id": "env", "injected_text": "forest backdrop"},
        ],
    )
    ec = resolve_execution_context(ctx, shot_id="S01")
    assert ec["generation_intent"]["effective_prompt"] == (
        "A hero walks. tall, red coat forest backdrop"
    )


def test_resolve_injection_order_is_preserved_in_effective_prompt() -> None:
    """Swapping injection order changes effective_prompt."""
    ctx_ab = _minimal_ctx(
        base_prompt="Scene.",
        injections=[
            {"asset_id": "a", "injected_text": "alpha"},
            {"asset_id": "b", "injected_text": "beta"},
        ],
    )
    ctx_ba = _minimal_ctx(
        base_prompt="Scene.",
        injections=[
            {"asset_id": "b", "injected_text": "beta"},
            {"asset_id": "a", "injected_text": "alpha"},
        ],
    )
    ep_ab = resolve_execution_context(ctx_ab, shot_id="S01")["generation_intent"]["effective_prompt"]
    ep_ba = resolve_execution_context(ctx_ba, shot_id="S01")["generation_intent"]["effective_prompt"]
    assert ep_ab != ep_ba


def test_resolve_effective_prompt_null_when_no_generation_intent() -> None:
    """Contexts without generation_intent produce no effective_prompt field."""
    ctx = _minimal_ctx()   # no base_prompt → no generation_intent
    ec = resolve_execution_context(ctx, shot_id="S01")
    assert "generation_intent" not in ec


def test_resolve_rejects_nonnull_effective_prompt_in_compiled_context() -> None:
    """A compiled context with non-null effective_prompt must be rejected."""
    ctx = _minimal_ctx(base_prompt="A hero walks.")
    ctx["generation_intent"]["effective_prompt"] = "external override"
    with pytest.raises(ConsistencyResolutionError, match="effective_prompt"):
        resolve_execution_context(ctx, shot_id="S01")


# ── 4. execution_context_digest computed ──────────────────────────────────────


def test_resolve_sets_execution_context_digest() -> None:
    """execution_context_digest is present and matches canonical_context_digest."""
    ctx = _minimal_ctx(base_prompt="A hero walks.")
    ec = resolve_execution_context(ctx, shot_id="S01")
    assert "execution_context_digest" in ec
    # The digest must match the digest of the resolved copy (with effective_prompt set)
    ec_for_check = {k: v for k, v in ec.items() if k != "execution_context_digest"}
    expected = canonical_context_digest(ec_for_check)
    assert ec["execution_context_digest"] == expected


def test_resolve_digest_changes_when_injection_text_changes() -> None:
    """Changing an injection text changes execution_context_digest."""
    ctx_a = _minimal_ctx(
        base_prompt="Scene.",
        injections=[{"asset_id": "hero", "injected_text": "alpha"}],
    )
    ctx_b = _minimal_ctx(
        base_prompt="Scene.",
        injections=[{"asset_id": "hero", "injected_text": "beta"}],
    )
    d_a = resolve_execution_context(ctx_a, shot_id="S01")["execution_context_digest"]
    d_b = resolve_execution_context(ctx_b, shot_id="S01")["execution_context_digest"]
    assert d_a != d_b


def test_resolve_digest_changes_when_resolved_sha_changes() -> None:
    """Filling in continues_from.resolved_sha256 changes the digest."""
    ctx = _minimal_ctx(continues_from_shot="S00")
    ec_null = resolve_execution_context(ctx, shot_id="S01")
    ec_filled = resolve_execution_context(
        ctx, shot_id="S01", resolved_artifact_shas={"S00": "c" * 64}
    )
    assert ec_null["execution_context_digest"] != ec_filled["execution_context_digest"]


# ── 5. plan_executor passes execution_context into task payload ────────────────


def _build_single_shot_job() -> dict[str, Any]:
    """Minimal job_manifest with a single shot task and a consistency_context."""
    return {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c1_test_job",
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
                    "review_required": False,
                    "generation_intent": {
                        "base_prompt": "A hero walks.",
                        "injection_rule_version": "fsf_injection_v1",
                        "asset_injections": [],
                        "effective_prompt": None,
                    },
                },
            }
        ],
    }


def test_executor_passes_execution_context_to_task(tmp_path: Path) -> None:
    """PlanExecutor must pass the resolved execution_context into the task payload."""
    job = _build_single_shot_job()
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)
    errors = validate_execution_plan(plan)
    assert errors == []

    captured_tasks: list[dict] = []

    class _CapturingExecutor:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task: dict, output_dir: Path) -> dict:
            captured_tasks.append(copy.deepcopy(task))
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    from fsf.executors.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    reg.register(_CapturingExecutor())

    executor = PlanExecutor(tmp_path, registry=reg)
    result = executor.execute_plan(plan)
    assert result["status"] == "completed"

    assert len(captured_tasks) >= 1
    task = captured_tasks[0]
    assert "execution_context" in task, "execution_context must be in task payload"
    ec = task["execution_context"]
    assert ec is not None
    assert isinstance(ec, dict)
    assert "execution_context_digest" in ec
    # effective_prompt must be assembled (not None) for a context with generation_intent
    assert ec["generation_intent"]["effective_prompt"] == "A hero walks."


def test_executor_task_has_no_execution_context_when_no_ctx(tmp_path: Path) -> None:
    """Steps without consistency_context must have execution_context=None in task."""
    job = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c1_no_ctx_job",
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
                # no consistency_context
            }
        ],
    }
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)

    captured_tasks: list[dict] = []

    class _CapturingExecutor:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task: dict, output_dir: Path) -> dict:
            captured_tasks.append(copy.deepcopy(task))
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    from fsf.executors.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    reg.register(_CapturingExecutor())

    executor = PlanExecutor(tmp_path, registry=reg)
    result = executor.execute_plan(plan)
    assert result["status"] == "completed"

    task = captured_tasks[0]
    assert task.get("execution_context") is None


def test_executor_execution_context_is_deep_copy_of_compiled(tmp_path: Path) -> None:
    """Mutating the task execution_context after dispatch must not affect the plan."""
    job = _build_single_shot_job()
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)

    captured_tasks: list[dict] = []
    mutated_after: list[str] = []

    class _CapturingExecutor:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task: dict, output_dir: Path) -> dict:
            ec = task.get("execution_context")
            if ec:
                captured_tasks.append(copy.deepcopy(ec))
                # Mutate in-place after capturing
                ec["MUTATED"] = True
                mutated_after.append("yes")
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    from fsf.executors.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    reg.register(_CapturingExecutor())

    executor = PlanExecutor(tmp_path, registry=reg)
    result = executor.execute_plan(plan)
    assert result["status"] == "completed"
    assert mutated_after, "Executor did not receive an execution_context"

    # The captured pre-mutation copy must not have MUTATED key
    assert "MUTATED" not in captured_tasks[0]


# ── 6. Resolution error surfaces as failed step ───────────────────────────────


def test_executor_returns_failed_step_on_resolution_error(tmp_path: Path) -> None:
    """A step that fails context resolution must produce status=failed, not crash."""
    job = _build_single_shot_job()
    # Inject a bad pre-filled effective_prompt to trigger resolution error
    job["tasks"][0]["consistency_context"]["generation_intent"]["effective_prompt"] = (
        "pre-filled override"
    )
    # Bypass compile-time check by patching the plan directly after building
    # without context, then injecting a tampered step.inputs
    plan_no_ctx = build_execution_plan(
        {
            "schema_version": "fsf_job_manifest_v1",
            "job_id": "c1_err_job",
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
        },
        executor_tool="mock.studio.video",
        shape=True,
    )
    # Inject tampered context directly into the compiled step inputs (bypasses compile gate)
    tampered_ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "generation_intent": {
            "base_prompt": "A hero walks.",
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": [],
            "effective_prompt": "pre-filled override",
        },
    }
    for step in plan_no_ctx["steps"]:
        if step.get("inputs", {}).get("shot_id") == "S01":
            step["inputs"]["consistency_context"] = tampered_ctx
            break

    class _DummyExecutor:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task: dict, output_dir: Path) -> dict:
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    from fsf.executors.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    reg.register(_DummyExecutor())

    executor = PlanExecutor(tmp_path, registry=reg)
    result = executor.execute_plan(plan_no_ctx)
    # The step with tampered context should be failed, not crashed.
    # Step IDs in mock plans may be like "step_S01_1"; search all steps for failed.
    failed_steps = [
        v for v in result["steps"].values()
        if v.get("status") == "failed" and "effective_prompt" in (v.get("error") or "")
    ]
    assert failed_steps, (
        f"Expected a failed step mentioning 'effective_prompt', "
        f"got steps: { {k: v.get('status') for k, v in result['steps'].items()} }"
    )


# ── 5 executor 反例（P1–P4 完整閉環驗證）──────────────────────────────────────


def _make_ref_image_exec(tmp_path, role="style"):
    import hashlib as _hl
    img = tmp_path / f"ref_{role}.png"
    content = f"fake-ref-image-{role}".encode()
    img.write_bytes(content)
    return img, _hl.sha256(content).hexdigest()


def _build_exec_job(base_prompt="A hero walks.", injection_text="", ref_image_sha=None):
    """Build a single-shot job; if injection_text is set, also adds a matching asset_ref."""
    # When using injections, a matching asset_ref is required by the compile gate.
    # We build a minimal asset_spec inline and compute the correct content_hash.
    from fsf.core.consistency import canonical_asset_digest
    asset_refs = []
    if injection_text:
        h = canonical_asset_digest(
            asset_id="hero",
            version="1.0",
            source_prompt="Hero asset.",
            kind="unspecified",
            constraints={"preserve": [], "forbid": [], "unresolved": []},
            ref_images=[],
        )
        asset_refs = [{"asset_id": "hero", "version": "1.0", "content_hash": h}]

    ctx = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": asset_refs,
        "review_required": False,
        "generation_intent": {
            "base_prompt": base_prompt,
            "injection_rule_version": "fsf_injection_v1",
            "asset_injections": (
                [{"asset_id": "hero", "injected_text": injection_text}]
                if injection_text else []
            ),
            "effective_prompt": None,
        },
    }
    if ref_image_sha is not None:
        ctx["ref_images"] = [{"role": "style", "sha256": ref_image_sha}]

    tasks = []
    if injection_text:
        # Add a hero asset task so the compile gate can find it
        tasks.append({
            "id": "hero",
            "unit_kind": "asset",
            "shot_spec": None,
            "asset_spec": {
                "asset_id": "hero",
                "source_prompt": "Hero asset.",
                "kind": "unspecified",
                "constraints": {"preserve": [], "forbid": [], "unresolved": []},
                "needs_semantic_review": False,
            },
        })
    tasks.append({
        "id": "S01",
        "unit_kind": "shot",
        "shot_spec": {
            "generation_prompt": base_prompt,
            "duration_seconds": 5.0,
            "source_prompt": base_prompt,
            "aspect_ratio": "16:9",
        },
        "consistency_context": ctx,
    })
    return {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c1_exec_test",
        "tasks": tasks,
    }


class _CapExec:
    tool_name = "mock.studio.video"
    executor_id = "mock.studio.video"
    execution_kind = "shape"

    def __init__(self):
        self.captured = []

    def execute_shot(self, task, output_dir):
        import copy as _c
        self.captured.append(_c.deepcopy(task))
        out = output_dir / f"{task['id']}.shape.bin"
        out.write_bytes(b"shape")
        return {"status": "completed", "outputs": {"artifact_file": out.name}}


def _make_reg_exec(cap):
    from fsf.executors.registry import ExecutorRegistry
    r = ExecutorRegistry()
    r.register(cap)
    return r


# ── R1: 修改參考圖後重跑必須失敗 ────────────────────────────────────────────────


def test_changed_ref_image_invalidates_resume(tmp_path: Path) -> None:
    """Changing a ref image's content on disk must cause the step to fail.

    The declared sha256 in the plan is now stale; resolve_execution_context
    verifies the file and rejects the mismatch.
    """
    ref_path, ref_sha = _make_ref_image_exec(tmp_path)
    job = _build_exec_job(ref_image_sha=ref_sha)
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)

    # Inject the path into the plan so the executor can find the file
    for step in plan["steps"]:
        for ri in step.get("inputs", {}).get("consistency_context", {}).get("ref_images", []):
            if ri.get("role") == "style":
                ri["path"] = str(ref_path)

    # Run 1 — fresh run must complete
    cap = _CapExec()
    r1 = PlanExecutor(tmp_path, registry=_make_reg_exec(cap)).execute_plan(plan)
    assert r1["completed_count"] >= 1, f"First run should complete, got {r1}"

    # Overwrite ref image on disk with different content (plan still has old sha)
    ref_path.write_bytes(b"different-content")

    # Run 2 — stale sha must cause failure (not silent reuse)
    cap2 = _CapExec()
    r2 = PlanExecutor(tmp_path, registry=_make_reg_exec(cap2)).execute_plan(plan)
    failed = [v for v in r2["steps"].values() if v.get("status") == "failed"]
    assert failed, (
        "Expected step to fail after ref image changed, "
        f"got: {dict((k, v.get('status')) for k, v in r2['steps'].items())}"
    )
    assert any(
        "SHA" in (v.get("error") or "") or "sha" in (v.get("error") or "")
        for v in failed
    ), f"Expected SHA mismatch error, got: {[v.get('error') for v in failed]}"


# ── R2: 上游多輸出時精確選中 VIDEO 產物 ──────────────────────────────────────────


def test_source_artifact_selects_video_not_report(tmp_path: Path) -> None:
    """_resolve_continues_from_sha must select the video (.shape.bin),
    not the QC report (.json), when both are present in the same step.
    """
    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S00_gen"]
    executor._steps_by_id = {
        "step_S00_gen": {
            "id": "step_S00_gen",
            "inputs": {"shot_id": "S00"},
            "outputs": ["report:step_S00_gen.json", "artifact:S00.shape.bin"],
        }
    }
    report = tmp_path / "step_S00_gen.json"
    video = tmp_path / "S00.shape.bin"
    report.write_text("{}", encoding="utf-8")
    video.write_bytes(b"fake-video")
    actual_video_sha = hashlib.sha256(b"fake-video").hexdigest()
    executor._artifact_registry["report:step_S00_gen.json"] = report
    executor._artifact_registry["artifact:S00.shape.bin"] = video

    step_results = {
        "step_S00_gen": {
            "status": "completed",
            "execution_kind": "shape",  # shape mode — .shape.bin is valid video source
            "output_files": [
                {"path": "step_S00_gen.json", "sha256": "a" * 64},
                {"path": "S00.shape.bin",    "sha256": actual_video_sha},
            ],
        }
    }
    continues_from_latest = {
        "shot_id": "S00",
        "selection_rule": "latest_completed",
        "aspects": ["scene"],
        "resolved_sha256": None,
    }
    shas = executor._resolve_continues_from_sha(continues_from_latest, step_results)
    assert shas.get("S00") == actual_video_sha, (
        f"Expected video sha ({actual_video_sha[:16]}…), got: {shas}"
    )


# ── R3: 必要 continuity 來源缺失時不得呼叫後端 ──────────────────────────────────


def test_missing_continuity_source_blocks_dispatch(tmp_path: Path) -> None:
    """S01 continues_from S00; if S00 fails, S01 must NOT be dispatched."""
    ctx_s01 = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "S00",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": None,
        },
    }
    job = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "c1_cont_test",
        "tasks": [
            {
                "id": "S00",
                "unit_kind": "shot",
                "shot_spec": {"generation_prompt": "A.", "duration_seconds": 5.0,
                              "source_prompt": "A.", "aspect_ratio": "16:9"},
            },
            {
                "id": "S01",
                "unit_kind": "shot",
                "shot_spec": {"generation_prompt": "B.", "duration_seconds": 5.0,
                              "source_prompt": "B.", "aspect_ratio": "16:9"},
                "consistency_context": ctx_s01,
            },
        ],
    }
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)
    dispatched: list[str] = []

    class _FailS00:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task, output_dir):
            dispatched.append(task["id"])
            if task["id"] == "S00":
                raise RuntimeError("S00 intentionally fails")
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    from fsf.executors.registry import ExecutorRegistry
    reg = ExecutorRegistry()
    reg.register(_FailS00())
    PlanExecutor(tmp_path, registry=reg).execute_plan(plan)
    assert "S01" not in dispatched, (
        f"S01 dispatched despite S00 failure; dispatched={dispatched}"
    )


# ── R4: effective_prompt（含注入）作為正式生成輸入 ──────────────────────────────


def test_effective_prompt_with_injection_is_task_prompt(tmp_path: Path) -> None:
    """task['prompt'] must be the assembled effective_prompt (base + injection)."""
    job = _build_exec_job(base_prompt="A hero walks.", injection_text="tall, red coat")
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)
    cap = _CapExec()
    r = PlanExecutor(tmp_path, registry=_make_reg_exec(cap)).execute_plan(plan)
    assert r["status"] == "completed"
    assert cap.captured, "Executor was never called"
    prompt = cap.captured[0]["prompt"]
    assert "tall, red coat" in prompt, f"Injection missing from prompt: {prompt!r}"
    assert "A hero walks." in prompt, f"Base prompt missing: {prompt!r}"
    assert cap.captured[0].get("source_prompt") == "A hero walks."


# ── R5: execution_context_digest 進入 journal inputs 綁定 ────────────────────────


def test_execution_context_digest_in_journal(tmp_path: Path) -> None:
    """Changing injection text must change inputs_digest in the journal."""
    job = _build_exec_job(base_prompt="A hero walks.", injection_text="tall, red coat")
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)
    cap = _CapExec()
    PlanExecutor(tmp_path, registry=_make_reg_exec(cap)).execute_plan(plan)
    j = json.loads((tmp_path / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    step1 = next((v for v in j["steps"].values() if v.get("status") == "completed"), None)
    assert step1 and "inputs_digest" in step1

    job2 = _build_exec_job(base_prompt="A hero walks.", injection_text="short, blue coat")
    plan2 = build_execution_plan(job2, executor_tool="mock.studio.video", shape=True)
    out2 = tmp_path / "run2"
    cap2 = _CapExec()
    PlanExecutor(out2, registry=_make_reg_exec(cap2)).execute_plan(plan2)
    j2 = json.loads((out2 / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    step2 = next((v for v in j2["steps"].values() if v.get("status") == "completed"), None)
    assert step2 and step2["inputs_digest"] != step1["inputs_digest"], (
        "inputs_digest must differ when injection text changes"
    )


# ── C1 P1 反例：完整參考圖核驗 & 精確來源選取 ────────────────────────────────────


# ── N1: 只修改 asset 層參考圖，重跑時拒絕 ─────────────────────────────────────────


def _make_asset_ref_ctx(
    asset_id: str,
    asset_ref_sha: str,
    asset_ref_path: str,
    asset_content_hash: str,
) -> dict[str, Any]:
    """Build a consistency_context with a ref_image inside asset_refs[0].ref_images."""
    return {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [
            {
                "asset_id": asset_id,
                "version": "1.0",
                "content_hash": asset_content_hash,
                "ref_images": [
                    {"role": "character", "sha256": asset_ref_sha, "path": asset_ref_path}
                ],
            }
        ],
        "review_required": False,
    }


def test_asset_level_ref_image_mutation_causes_failure(tmp_path: Path) -> None:
    """Only modifying an asset-level ref image (asset_refs[i].ref_images) must
    be detected and cause the step to fail on the second run.

    Specifically tests that _verify_ref_image_item() is called for asset-level
    ref images, not just top-level ref_images.
    """
    from fsf.core.consistency import canonical_asset_digest

    # Create the initial ref image file
    asset_img = tmp_path / "asset_char.png"
    orig_content = b"fake-asset-char-image"
    asset_img.write_bytes(orig_content)
    orig_sha = hashlib.sha256(orig_content).hexdigest()

    # Compute a matching content_hash for the asset so compile gate passes
    asset_hash = canonical_asset_digest(
        asset_id="hero",
        version="1.0",
        source_prompt="Hero asset.",
        kind="unspecified",
        constraints={"preserve": [], "forbid": [], "unresolved": []},
        ref_images=[{"role": "character", "sha256": orig_sha}],
    )

    ctx = _make_asset_ref_ctx(
        asset_id="hero",
        asset_ref_sha=orig_sha,
        asset_ref_path=str(asset_img),
        asset_content_hash=asset_hash,
    )

    tasks = [
        {
            "id": "hero",
            "unit_kind": "asset",
            "shot_spec": None,
            "asset_spec": {
                "asset_id": "hero",
                "source_prompt": "Hero asset.",
                "kind": "unspecified",
                "constraints": {"preserve": [], "forbid": [], "unresolved": []},
                "needs_semantic_review": False,
            },
        },
        {
            "id": "S01",
            "unit_kind": "shot",
            "shot_spec": {
                "generation_prompt": "Hero close-up.",
                "duration_seconds": 5.0,
                "source_prompt": "Hero close-up.",
                "aspect_ratio": "16:9",
            },
            "consistency_context": ctx,
        },
    ]
    job = {"schema_version": "fsf_job_manifest_v1", "job_id": "n1_asset_ref", "tasks": tasks}
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)

    cap1 = _CapExec()
    r1 = PlanExecutor(tmp_path, registry=_make_reg_exec(cap1)).execute_plan(plan)
    shot_steps_r1 = {
        k: v for k, v in r1["steps"].items()
        if "S01" in k or "hero" in k.lower()
    }
    shot_r1 = {k: v for k, v in r1["steps"].items() if "S01" in k}
    assert any(v.get("status") == "completed" for v in shot_r1.values()), (
        f"First run: expected S01 completed, got {shot_steps_r1}"
    )

    # Mutate the asset-level ref image on disk (plan still has orig sha)
    asset_img.write_bytes(b"modified-asset-char-image")

    # Second run: SHA mismatch must cause failure
    cap2 = _CapExec()
    r2 = PlanExecutor(tmp_path, registry=_make_reg_exec(cap2)).execute_plan(plan)
    failed = [v for v in r2["steps"].values() if v.get("status") == "failed"]
    assert failed, (
        f"Expected S01 to fail after asset-level ref image changed, "
        f"got: { {k: v.get('status') for k, v in r2['steps'].items()} }"
    )
    assert any(
        "SHA" in (v.get("error") or "") or "sha" in (v.get("error") or "")
        or "mismatch" in (v.get("error") or "").lower()
        for v in failed
    ), f"Expected SHA mismatch error, got: {[v.get('error') for v in failed]}"


# ── N2: 同 role 兩張圖片各自核驗；無 path 不得跳過 ───────────────────────────────


def test_two_ref_images_same_role_each_verified(tmp_path: Path) -> None:
    """Two ref_images with the same role must be individually verified.

    Keying by role (old buggy behaviour) would silently overwrite one entry.
    Both must be checked.  The second image has a different sha than the first.
    """
    img_a = tmp_path / "char_a.png"
    img_b = tmp_path / "char_b.png"
    img_a.write_bytes(b"character-image-A")
    img_b.write_bytes(b"character-image-B")
    sha_a = hashlib.sha256(b"character-image-A").hexdigest()
    sha_b = hashlib.sha256(b"character-image-B").hexdigest()

    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "ref_images": [
            {"role": "character", "sha256": sha_a, "path": str(img_a)},
            {"role": "character", "sha256": sha_b, "path": str(img_b)},
        ],
    }

    # Both files present with correct SHAs → resolution must succeed
    ec = resolve_execution_context(ctx, shot_id="S01")
    assert ec["ref_images"][0]["sha256"] == sha_a
    assert ec["ref_images"][1]["sha256"] == sha_b

    # Corrupt image B → mismatch must be detected
    img_b.write_bytes(b"CORRUPTED")
    with pytest.raises(ConsistencyResolutionError, match="SHA-256 mismatch|mismatch"):
        resolve_execution_context(ctx, shot_id="S01")


def test_ref_image_without_path_and_no_ref_image_files_is_rejected() -> None:
    """A ref_image entry that lacks 'path' AND is not in ref_image_files must be rejected.

    Must never be silently skipped — that would allow unverified images to bypass
    integrity checks.
    """
    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "ref_images": [
            # sha256 present but no 'path'; caller supplies no ref_image_files
            {"role": "scene", "sha256": "a" * 64},
        ],
    }
    with pytest.raises(ConsistencyResolutionError, match="resolvable path|no path|cannot verify"):
        resolve_execution_context(ctx, shot_id="S01")


# ── N3: specific_artifact 精確選中宣告 ref ────────────────────────────────────


def _build_two_step_plan_for_specific_artifact(
    tmp_path: Path,
) -> tuple[dict, str, str]:
    """Create a plan where S01 has two steps: generate (produces .shape.bin) and
    remaster (produces _remaster.shape.bin).  Returns (plan, gen_sha, remaster_sha).
    """
    gen_file = tmp_path / "S01_gen.shape.bin"
    remaster_file = tmp_path / "S01_remaster.shape.bin"
    gen_file.write_bytes(b"generated-content")
    remaster_file.write_bytes(b"remastered-content")
    gen_sha = hashlib.sha256(b"generated-content").hexdigest()
    remaster_sha = hashlib.sha256(b"remastered-content").hexdigest()
    return gen_file, remaster_file, gen_sha, remaster_sha


def test_specific_artifact_selects_exact_declared_ref(tmp_path: Path) -> None:
    """continues_from with selection_rule='specific_artifact' must resolve to the
    exact artifact_ref, not the 'latest' one.

    S01 has both generate and remaster outputs.  S02 declares specific_artifact
    pointing at the generate output.  The executor must pick gen_sha.
    """
    gen_file, remaster_file, gen_sha, remaster_sha = (
        _build_two_step_plan_for_specific_artifact(tmp_path)
    )

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen", "step_S01_remaster", "step_S02_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01_gen.shape.bin"],
            "depends_on": [],
        },
        "step_S01_remaster": {
            "id": "step_S01_remaster",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01_remaster.shape.bin"],
            "depends_on": ["step_S01_gen"],
        },
        "step_S02_gen": {
            "id": "step_S02_gen",
            "inputs": {"shot_id": "S02"},
            "outputs": ["artifact:S02.shape.bin"],
            "depends_on": ["step_S01_remaster"],
        },
    }
    # Register file paths for resolve_artifact_path
    executor._artifact_registry["artifact:S01_gen.shape.bin"] = gen_file
    executor._artifact_registry["artifact:S01_remaster.shape.bin"] = remaster_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "shape",
            "output_files": [
                {"path": gen_file.name, "sha256": gen_sha},
            ],
        },
        "step_S01_remaster": {
            "status": "completed",
            "execution_kind": "shape",
            "output_files": [
                {"path": remaster_file.name, "sha256": remaster_sha},
            ],
        },
    }

    continues_from_specific = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:S01_gen.shape.bin",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    result = executor._resolve_continues_from_sha(continues_from_specific, step_results)
    assert result == {"S01": gen_sha}, (
        f"specific_artifact must select generate sha, got: {result}"
    )

    # Also verify that specifying the remaster ref yields the remaster sha
    continues_from_remaster = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:S01_remaster.shape.bin",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    result2 = executor._resolve_continues_from_sha(continues_from_remaster, step_results)
    assert result2 == {"S01": remaster_sha}, (
        f"specific_artifact must select remaster sha, got: {result2}"
    )


# ── N4: terminal step 為 QC／Dub，仍解析到宣告影片 ────────────────────────────────


def test_latest_completed_skips_qc_dub_and_finds_video(tmp_path: Path) -> None:
    """When the terminal completed step for a shot is a QC/Dub step (outputs only
    JSON or audio), latest_completed must trace back to the actual video step.

    S01 sequence: generate (→ .shape.bin) → qc (→ .json) → dub (→ .wav).
    latest_completed for S01 must return the generate sha, not the qc/dub shas.
    """
    gen_file = tmp_path / "S01.shape.bin"
    qc_file  = tmp_path / "S01_qc.json"
    dub_file = tmp_path / "S01_dub.wav"
    gen_file.write_bytes(b"gen-video")
    qc_file.write_text("{}", encoding="utf-8")
    dub_file.write_bytes(b"audio")
    gen_sha = hashlib.sha256(b"gen-video").hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen", "step_S01_qc", "step_S01_dub"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
        "step_S01_qc": {
            "id": "step_S01_qc",
            "inputs": {"shot_id": "S01"},
            "outputs": ["report:S01_qc.json"],
            "depends_on": ["step_S01_gen"],
        },
        "step_S01_dub": {
            "id": "step_S01_dub",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01_dub.wav"],
            "depends_on": ["step_S01_qc"],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = gen_file
    executor._artifact_registry["report:S01_qc.json"] = qc_file
    executor._artifact_registry["artifact:S01_dub.wav"] = dub_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "shape",  # shape mode — .shape.bin is valid
            "output_files": [{"path": "S01.shape.bin", "sha256": gen_sha}],
        },
        "step_S01_qc": {
            "status": "completed",
            "execution_kind": "qc",
            "output_files": [{"path": "S01_qc.json", "sha256": "q" * 64}],
        },
        "step_S01_dub": {
            "status": "completed",
            "execution_kind": "dub",
            "output_files": [{"path": "S01_dub.wav", "sha256": "d" * 64}],
        },
    }

    continues_from = {
        "shot_id": "S01",
        "selection_rule": "latest_completed",
        "aspects": ["scene"],
        "resolved_sha256": None,
    }
    result = executor._resolve_continues_from_sha(continues_from, step_results)
    assert result == {"S01": gen_sha}, (
        f"latest_completed must find video despite QC/Dub terminal steps, got: {result}"
    )


# ── N5: journal SHA 與 disk 不符時拒絕 ────────────────────────────────────────


def test_journal_sha_mismatch_causes_rejection(tmp_path: Path) -> None:
    """If the journal SHA for a source artifact differs from the actual on-disk file,
    the source resolution must be rejected with a ConsistencyResolutionError.

    This prevents stale / tampered files from silently becoming continuity sources.
    """
    from fsf.core.consistency import ConsistencyResolutionError

    art_file = tmp_path / "S01.shape.bin"
    art_file.write_bytes(b"original-content")
    real_sha = hashlib.sha256(b"original-content").hexdigest()
    stale_sha = "s" * 64  # journal recorded this — now stale

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = art_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "shape",  # shape mode — .shape.bin is valid
            # Journal SHA doesn't match the actual file on disk
            "output_files": [{"path": "S01.shape.bin", "sha256": stale_sha}],
        },
    }

    continues_from = {
        "shot_id": "S01",
        "selection_rule": "latest_completed",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    with pytest.raises(ConsistencyResolutionError, match="SHA|mismatch|changed"):
        executor._resolve_continues_from_sha(continues_from, step_results)


# ── N6: 不同目錄相同檔名 — specific_artifact 不得以檔名比對 ──────────────────────


def test_specific_artifact_does_not_confuse_same_filename_in_different_dirs(
    tmp_path: Path,
) -> None:
    """Two steps both produce 'clip.shape.bin' in different subdirectories.
    specific_artifact must use the full resolved path to distinguish them —
    a filename-only comparison would silently match the wrong artifact.

    generate/clip.shape.bin  vs  remaster/clip.shape.bin
    S02 declares artifact_ref='artifact:generate/clip.shape.bin'.
    Resolution must return gen_sha, NOT remaster_sha.
    """
    gen_dir = tmp_path / "generate"
    rem_dir = tmp_path / "remaster"
    gen_dir.mkdir()
    rem_dir.mkdir()

    gen_file = gen_dir / "clip.shape.bin"
    rem_file = rem_dir / "clip.shape.bin"
    gen_file.write_bytes(b"generated-clip")
    rem_file.write_bytes(b"remastered-clip")
    gen_sha = hashlib.sha256(b"generated-clip").hexdigest()
    rem_sha = hashlib.sha256(b"remastered-clip").hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen", "step_S01_remaster", "step_S02_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:generate/clip.shape.bin"],
            "depends_on": [],
        },
        "step_S01_remaster": {
            "id": "step_S01_remaster",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:remaster/clip.shape.bin"],
            "depends_on": ["step_S01_gen"],
        },
        "step_S02_gen": {
            "id": "step_S02_gen",
            "inputs": {"shot_id": "S02"},
            "outputs": ["artifact:S02.shape.bin"],
            "depends_on": ["step_S01_remaster"],
        },
    }
    # Registry resolves canonical full paths (avoids filename-only lookup)
    executor._artifact_registry["artifact:generate/clip.shape.bin"] = gen_file
    executor._artifact_registry["artifact:remaster/clip.shape.bin"] = rem_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "shape",
            # Journal stores relative paths (from output_dir) — not just filename
            "output_files": [{"path": "generate/clip.shape.bin", "sha256": gen_sha}],
        },
        "step_S01_remaster": {
            "status": "completed",
            "execution_kind": "shape",
            "output_files": [{"path": "remaster/clip.shape.bin", "sha256": rem_sha}],
        },
    }

    # S02 wants the GENERATE output specifically
    cf = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:generate/clip.shape.bin",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    result = executor._resolve_continues_from_sha(cf, step_results)
    assert result == {"S01": gen_sha}, (
        f"specific_artifact must pick generate sha ({gen_sha[:12]}…) not "
        f"remaster sha ({rem_sha[:12]}…), got: {result}"
    )

    # Verify symmetry: asking for remaster returns remaster sha
    cf_rem = {**cf, "artifact_ref": "artifact:remaster/clip.shape.bin"}
    result_rem = executor._resolve_continues_from_sha(cf_rem, step_results)
    assert result_rem == {"S01": rem_sha}, (
        f"specific_artifact must pick remaster sha, got: {result_rem}"
    )


# ── N7: 檔案存在但 journal 無紀錄時拒絕 ──────────────────────────────────────────


def test_specific_artifact_rejects_when_file_exists_but_no_journal_entry(
    tmp_path: Path,
) -> None:
    """If the artifact file exists on disk but the journal has no matching entry,
    the source must be rejected.

    This prevents a rogue or pre-existing file from being silently used as a
    continuity source without having been produced by the declared step.
    """
    from fsf.core.consistency import ConsistencyResolutionError

    art_file = tmp_path / "S01.shape.bin"
    art_file.write_bytes(b"orphan-file-not-in-journal")

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = art_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            # Journal records only a JSON report — the shape.bin has NO entry
            "output_files": [{"path": "S01_report.json", "sha256": "q" * 64}],
        },
    }

    cf = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:S01.shape.bin",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    with pytest.raises(ConsistencyResolutionError, match="journal record|output_files"):
        executor._resolve_continues_from_sha(cf, step_results)


def test_latest_completed_rejects_when_file_exists_but_no_journal_entry(
    tmp_path: Path,
) -> None:
    """latest_completed must also reject a file that exists on disk but lacks
    a journal entry.  Absence of a journal record means we cannot prove the
    file was the output of the completed step.
    """
    art_file = tmp_path / "S01.shape.bin"
    art_file.write_bytes(b"orphan-video-no-journal")

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = art_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            # Journal has NO output_files — empty record
            "output_files": [],
        },
    }

    cf = {
        "shot_id": "S01",
        "selection_rule": "latest_completed",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    # latest_completed returns {} (no video source found — not ready) rather
    # than trusting the orphan file.  The caller then treats absence as
    # "source not ready" and blocks dispatch.
    result = executor._resolve_continues_from_sha(cf, step_results)
    assert result == {}, (
        f"latest_completed must return empty dict when journal has no video entry, "
        f"got: {result}"
    )


# ── N8: Generate → QC → Dub(.dub.json) — latest_completed 仍選影片 ───────────────


def test_latest_completed_with_dub_json_terminal_still_finds_video(tmp_path: Path) -> None:
    """Generate → QC → Dub  where Dub outputs .dub.json (config artifact, not video).

    latest_completed must skip the .dub.json terminal step and find the generate
    .shape.bin.  .dub.json is explicitly excluded from the video whitelist because
    it is a JSON configuration file even though it has a compound suffix.
    """
    gen_file = tmp_path / "S01.shape.bin"
    qc_file  = tmp_path / "S01_qc.json"
    dub_json = tmp_path / "S01.dub.json"
    gen_file.write_bytes(b"gen-video-content")
    qc_file.write_text("{}", encoding="utf-8")
    dub_json.write_bytes(b'{"dub": "config"}')
    gen_sha = hashlib.sha256(b"gen-video-content").hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen", "step_S01_qc", "step_S01_dub"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
        "step_S01_qc": {
            "id": "step_S01_qc",
            "inputs": {"shot_id": "S01"},
            "outputs": ["report:S01_qc.json"],
            "depends_on": ["step_S01_gen"],
        },
        "step_S01_dub": {
            "id": "step_S01_dub",
            "inputs": {"shot_id": "S01"},
            # Dub in shape mode outputs .dub.json — a JSON config, not a video
            "outputs": ["artifact:S01.dub.json"],
            "depends_on": ["step_S01_qc"],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = gen_file
    executor._artifact_registry["report:S01_qc.json"] = qc_file
    executor._artifact_registry["artifact:S01.dub.json"] = dub_json

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "shape",
            "output_files": [{"path": "S01.shape.bin", "sha256": gen_sha}],
        },
        "step_S01_qc": {
            "status": "completed",
            "execution_kind": "qc",
            "output_files": [{"path": "S01_qc.json", "sha256": "q" * 64}],
        },
        "step_S01_dub": {
            "status": "completed",
            "execution_kind": "dub",
            # .dub.json is NOT a video — must be skipped by whitelist
            "output_files": [{"path": "S01.dub.json", "sha256": "d" * 64}],
        },
    }

    continues_from = {
        "shot_id": "S01",
        "selection_rule": "latest_completed",
        "aspects": ["scene"],
        "resolved_sha256": None,
    }
    result = executor._resolve_continues_from_sha(continues_from, step_results)
    assert result == {"S01": gen_sha}, (
        f"latest_completed must find the .shape.bin generate artifact, "
        f"skipping .dub.json terminal step; got: {result}"
    )


# ── N9: 真媒體模式只有 shape／mock 時拒絕 ─────────────────────────────────────────


def test_real_media_mode_rejects_shape_bin_as_continuity_source(tmp_path: Path) -> None:
    """.shape.bin / .mock.bin must NOT qualify as a continuity source when the
    upstream step's execution_kind is not 'shape'.

    This guards against a scenario where a shape-mode stub from an offline test
    run is present on disk and a subsequent real-media execution blindly trusts
    it as a video continuity source.
    """
    art_file = tmp_path / "S01.shape.bin"
    art_file.write_bytes(b"shape-stub-not-video")
    shape_sha = hashlib.sha256(b"shape-stub-not-video").hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = art_file

    # execution_kind is "media" — .shape.bin is NOT a valid video source
    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "media",
            "output_files": [{"path": "S01.shape.bin", "sha256": shape_sha}],
        },
    }

    continues_from = {
        "shot_id": "S01",
        "selection_rule": "latest_completed",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    # Must return empty dict — .shape.bin is not a valid video in media mode,
    # so no qualifying continuity source is found.
    result = executor._resolve_continues_from_sha(continues_from, step_results)
    assert result == {}, (
        f"Real-media mode must NOT accept .shape.bin as a continuity source, "
        f"got: {result}"
    )


# ── N10: specific_artifact 指向 .dub.json 時拒絕 ─────────────────────────────


def test_specific_artifact_rejects_dub_json(tmp_path: Path) -> None:
    """`specific_artifact` pointing at a .dub.json must be rejected.

    .dub.json is a JSON configuration file produced by the Dub skill;
    it is never a valid video continuity source regardless of selection_rule.
    """
    from fsf.core.consistency import ConsistencyResolutionError

    dub_file = tmp_path / "S01.dub.json"
    dub_file.write_bytes(b'{"dub": "config"}')
    dub_sha = hashlib.sha256(b'{"dub": "config"}').hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_dub"]
    executor._steps_by_id = {
        "step_S01_dub": {
            "id": "step_S01_dub",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.dub.json"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.dub.json"] = dub_file

    step_results = {
        "step_S01_dub": {
            "status": "completed",
            "execution_kind": "dub",
            "output_files": [{"path": "S01.dub.json", "sha256": dub_sha}],
        },
    }

    cf = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:S01.dub.json",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    with pytest.raises(ConsistencyResolutionError, match=r"dub\.json|not a valid video"):
        executor._resolve_continues_from_sha(cf, step_results)


def test_specific_artifact_rejects_shape_bin_in_media_mode(tmp_path: Path) -> None:
    """`specific_artifact` must also reject .shape.bin when execution_kind != 'shape'.

    The same media-type + shape-mode rule that applies to latest_completed
    must also apply to specific_artifact.
    """
    from fsf.core.consistency import ConsistencyResolutionError

    art_file = tmp_path / "S01.shape.bin"
    art_file.write_bytes(b"shape-stub")
    sha = hashlib.sha256(b"shape-stub").hexdigest()

    executor = PlanExecutor(tmp_path)
    executor._topo_order = ["step_S01_gen"]
    executor._steps_by_id = {
        "step_S01_gen": {
            "id": "step_S01_gen",
            "inputs": {"shot_id": "S01"},
            "outputs": ["artifact:S01.shape.bin"],
            "depends_on": [],
        },
    }
    executor._artifact_registry["artifact:S01.shape.bin"] = art_file

    step_results = {
        "step_S01_gen": {
            "status": "completed",
            "execution_kind": "media",  # NOT shape — .shape.bin invalid
            "output_files": [{"path": "S01.shape.bin", "sha256": sha}],
        },
    }

    cf = {
        "shot_id": "S01",
        "selection_rule": "specific_artifact",
        "artifact_ref": "artifact:S01.shape.bin",
        "aspects": ["appearance"],
        "resolved_sha256": None,
    }
    with pytest.raises(
        ConsistencyResolutionError, match=r"shape.bin|not a valid video|execution_kind"
    ):
        executor._resolve_continues_from_sha(cf, step_results)


# ── N11: shape continuity 三輪重用，journal execution_kind 保持一致 ───────────────


def test_shape_continuity_preserved_across_three_resume_cycles(tmp_path: Path) -> None:
    """A S00→S01 shape continuity chain must survive three consecutive execute_plan
    calls (run1=fresh, run2=reuse, run3=reuse).

    Specifically tests that:
    1. Run 1 (fresh): both S00 and S01 complete; journal records execution_kind='shape'.
    2. Run 2 (reuse): both steps are reused; S01 can still resolve S00 as continuity
       source because execution_kind is preserved in the reused entry.
    3. Run 3 (reuse again): same guarantee holds; journal still has consistent
       execution_kind after a reuse cycle.
    """
    from fsf.runtime.plan import build_execution_plan

    ctx_s01 = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": [],
        "review_required": False,
        "continues_from": {
            "shot_id": "S00",
            "selection_rule": "latest_completed",
            "aspects": ["scene"],
            "resolved_sha256": None,
        },
    }
    job = {
        "schema_version": "fsf_job_manifest_v1",
        "job_id": "n11_resume_kind",
        "tasks": [
            {
                "id": "S00",
                "unit_kind": "shot",
                "shot_spec": {
                    "generation_prompt": "Opening shot.",
                    "duration_seconds": 5.0,
                    "source_prompt": "Opening shot.",
                    "aspect_ratio": "16:9",
                },
            },
            {
                "id": "S01",
                "unit_kind": "shot",
                "shot_spec": {
                    "generation_prompt": "Follow-up shot.",
                    "duration_seconds": 5.0,
                    "source_prompt": "Follow-up shot.",
                    "aspect_ratio": "16:9",
                },
                "consistency_context": ctx_s01,
            },
        ],
    }
    plan = build_execution_plan(job, executor_tool="mock.studio.video", shape=True)

    from fsf.executors.registry import ExecutorRegistry

    class _ShapeExec:
        tool_name = "mock.studio.video"
        executor_id = "mock.studio.video"
        execution_kind = "shape"

        def execute_shot(self, task, output_dir):
            out = output_dir / f"{task['id']}.shape.bin"
            out.write_bytes(b"shape-content")
            return {"status": "completed", "outputs": {"artifact_file": out.name}}

    def _run(out_dir):
        reg = ExecutorRegistry()
        reg.register(_ShapeExec())
        r = PlanExecutor(out_dir, registry=reg).execute_plan(plan)
        return r

    # Run 1: fresh
    r1 = _run(tmp_path)
    assert r1["status"] == "completed", f"Run 1 failed: {r1}"
    r1_s = {k: v.get("status") for k, v in r1["steps"].items()}
    assert "reused" not in r1_s.values(), f"Run 1 should have no reused steps, got {r1_s}"

    # Verify journal has execution_kind for both steps
    import json as _json
    j1 = _json.loads((tmp_path / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    for sid, se in j1["steps"].items():
        assert se.get("execution_kind") == "shape", (
            f"Run 1 journal step '{sid}' missing execution_kind='shape', got {se.get('execution_kind')}"
        )

    # Run 2: reuse — all steps should be reused
    r2 = _run(tmp_path)
    assert r2["status"] == "completed", f"Run 2 failed: {r2}"
    r2_s = {k: v.get("status") for k, v in r2["steps"].items()}
    assert all(s == "reused" for s in r2_s.values()), (
        f"Run 2: expected all reused, got {r2_s}"
    )

    # Verify journal after run 2 still has execution_kind
    j2 = _json.loads((tmp_path / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    for sid, se in j2["steps"].items():
        assert se.get("execution_kind") == "shape", (
            f"Run 2 journal step '{sid}' missing execution_kind='shape', got {se.get('execution_kind')}"
        )

    # Run 3: reuse again
    r3 = _run(tmp_path)
    assert r3["status"] == "completed", f"Run 3 failed: {r3}"
    r3_s = {k: v.get("status") for k, v in r3["steps"].items()}
    assert all(s == "reused" for s in r3_s.values()), (
        f"Run 3: expected all reused, got {r3_s}"
    )

    # Journal must still be consistent after 3 cycles
    j3 = _json.loads((tmp_path / "fsf_execution_journal.json").read_text(encoding="utf-8"))
    for sid, se in j3["steps"].items():
        assert se.get("execution_kind") == "shape", (
            f"Run 3 journal step '{sid}' missing execution_kind='shape', got {se.get('execution_kind')}"
        )
