"""FSF Consistency Context v1 — compile-time builder and digest utilities.

This module is the single authoritative source for:
- Building and validating fsf_consistency_context_v1 objects.
- Computing canonical asset and context digests.
- Compiler-time referential integrity checks (asset existence, continuity
  cycle detection, automatic depends_on insertion).

The original shot_spec and asset_spec in step.inputs are NEVER modified.
All rc5 consistency information lives exclusively in the context object
returned by build_consistency_context().
"""

from __future__ import annotations

import copy
import hashlib
import json
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from fsf.conformance.digest import canonical_contract_digest

# ── Schema loading ──────────────────────────────────────────────────────────

_SCHEMA_NAME = "fsf_consistency_context_v1.schema.json"
_INJECTION_RULE_VERSION = "fsf_injection_v1"  # bump when injection logic changes


def _load_context_schema() -> dict[str, Any]:
    root = resources.files("fsf.schemas")
    return json.loads((root / _SCHEMA_NAME).read_text(encoding="utf-8"))


def validate_consistency_context(ctx: dict[str, Any]) -> list[str]:
    """Return a list of validation error strings, empty if valid."""
    schema = _load_context_schema()
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(ctx), key=lambda e: list(e.path))
    ]


# ── Canonical digest helpers ─────────────────────────────────────────────────


def canonical_asset_digest(
    asset_id: str,
    version: str,
    source_prompt: str,
    kind: str,
    constraints: dict[str, Any],
    ref_images: list[dict[str, str]],
) -> str:
    """Compute the content_hash for an asset_ref.

    Covers: asset_id, version, source_prompt, kind, constraints,
    ref_images[].{role, sha256} in original declaration order.
    Excludes: path fields, human-readable summaries.

    Note: ref_images order is preserved (not sorted) so that the same images
    in a different order produce different hashes.
    """
    payload = {
        "asset_id": asset_id,
        "version": version,
        "source_prompt": source_prompt,
        "kind": kind,
        "constraints": constraints,
        "ref_images": [
            {"role": ri["role"], "sha256": ri["sha256"]} for ri in ref_images
        ],
    }
    return canonical_contract_digest(payload)


def canonical_context_digest(ctx: dict[str, Any]) -> str:
    """Compute the execution-time digest of a consistency context.

    Covers all fields that affect generation output:
    - asset_refs[].{asset_id, version, content_hash}  (original order)
    - ref_images[].{role, sha256}                     (original order)
    - continues_from.{shot_id, aspects[], resolved_sha256}
    - generation_intent.{base_prompt, injection_rule_version,
                         asset_injections[].{asset_id, injected_text}}
    - review_required

    Excludes: ref_images[].path, generation_intent.effective_prompt.

    Order is always original declaration order (never sorted) so that
    swapping two items produces a different digest.
    """
    asset_refs_payload = [
        {
            "asset_id": ar["asset_id"],
            "version": ar["version"],
            "content_hash": ar["content_hash"],
        }
        for ar in ctx.get("asset_refs", [])
    ]

    # Original order preserved — swapping roles/images changes the digest.
    shot_ref_images_payload = [
        {"role": ri["role"], "sha256": ri["sha256"]}
        for ri in ctx.get("ref_images", [])
    ]

    cf = ctx.get("continues_from")
    continues_payload = None
    if cf is not None:
        continues_payload = {
            "shot_id": cf["shot_id"],
            # aspects are a set-like declaration; sort for stability
            "aspects": sorted(cf.get("aspects", [])),
            "resolved_sha256": cf.get("resolved_sha256"),
        }

    gi = ctx.get("generation_intent", {})
    gen_intent_payload = {
        "base_prompt": gi.get("base_prompt", ""),
        "injection_rule_version": gi.get("injection_rule_version", ""),
        # asset_injections order is preserved (order matters for prompt assembly)
        "asset_injections": [
            {"asset_id": inj["asset_id"], "injected_text": inj["injected_text"]}
            for inj in gi.get("asset_injections", [])
        ],
    }

    payload = {
        "schema_version": ctx["schema_version"],
        "asset_refs": asset_refs_payload,
        "ref_images": shot_ref_images_payload,
        "continues_from": continues_payload,
        "generation_intent": gen_intent_payload,
        "review_required": ctx.get("review_required", False),
    }
    return canonical_contract_digest(payload)


# ── Compile-time referential integrity ───────────────────────────────────────


class ConsistencyCompileError(ValueError):
    """Raised when consistency context fails compile-time validation."""


def _collect_shot_ids(tasks: list[dict[str, Any]]) -> set[str]:
    from fsf.intake.parser import is_shot_unit
    return {t["id"] for t in tasks if is_shot_unit(t)}


def _collect_asset_ids(tasks: list[dict[str, Any]]) -> set[str]:
    from fsf.intake.parser import is_shot_unit
    return {t["id"] for t in tasks if not is_shot_unit(t)}


def check_asset_refs(
    ctx: dict[str, Any],
    shot_id: str,
    known_asset_ids: set[str],
) -> None:
    """Raise ConsistencyCompileError if any asset_ref.asset_id is unknown."""
    for ar in ctx.get("asset_refs", []):
        aid = ar.get("asset_id", "")
        if aid not in known_asset_ids:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': consistency_context references unknown asset '{aid}'. "
                f"Known assets: {sorted(known_asset_ids)}"
            )


def check_asset_content_hashes(
    ctx: dict[str, Any],
    shot_id: str,
    asset_specs_by_id: dict[str, dict[str, Any]],
) -> None:
    """Raise ConsistencyCompileError if any asset_ref.content_hash does not match
    the canonical digest recomputed from the authoritative asset_spec.

    asset_specs_by_id: {asset_id -> asset_spec dict} from the job manifest.
    Every asset_id that appears in asset_refs MUST have a corresponding entry;
    a missing or absent asset_spec is itself an error — it is not skipped.
    (Unknown asset IDs are caught earlier by check_asset_refs; this check
    verifies that the spec *content* is present and the hash matches.)
    """
    for ar in ctx.get("asset_refs", []):
        aid = ar.get("asset_id", "")
        spec = asset_specs_by_id.get(aid)
        if spec is None:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': asset_ref '{aid}' references an asset task that "
                f"has no authoritative asset_spec in the job manifest. "
                f"Every referenced asset must carry a valid asset_spec; "
                f"content_hash cannot be verified without it."
            )

        # ── Identity check: spec.asset_id must exactly match the referenced aid ──
        # We never fall back to `aid` as a default: a missing or mismatched
        # asset_id in the spec means the spec does not belong to this asset.
        spec_aid = spec.get("asset_id")
        if spec_aid is None:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': asset_spec for task '{aid}' is missing the "
                f"asset_id field. asset_spec.asset_id must be present and equal "
                f"to the task id before content_hash can be verified."
            )
        if spec_aid != aid:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': asset_spec.asset_id '{spec_aid}' does not match "
                f"the referenced asset_ref.asset_id '{aid}'. "
                f"The spec must belong to the same asset as the reference; "
                f"content_hash cannot be verified across mismatched identities."
            )

        version = ar.get("version", "")
        declared_hash = ar.get("content_hash", "")
        expected_hash = canonical_asset_digest(
            asset_id=spec_aid,   # identity already verified above
            version=version,
            source_prompt=spec.get("source_prompt", ""),
            kind=spec.get("kind", "unspecified"),
            constraints=spec.get("constraints", {}),
            ref_images=ar.get("ref_images", []),
        )
        if declared_hash != expected_hash:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': asset_ref '{aid}' content_hash mismatch. "
                f"Declared: {declared_hash[:16]}…, "
                f"Expected (recomputed from asset_spec): {expected_hash[:16]}…. "
                f"Recompute with canonical_asset_digest() after any spec change."
            )


def check_continues_from(
    ctx: dict[str, Any],
    shot_id: str,
    known_shot_ids: set[str],
    shot_order: list[str],
) -> None:
    """Raise ConsistencyCompileError if continues_from is invalid.

    Rules:
    - shot_id must be a known shot.
    - Source shot must appear before this shot in declaration order.
    - resolved_sha256 must be null at compile time.
    """
    cf = ctx.get("continues_from")
    if cf is None:
        return
    src = cf.get("shot_id", "")
    if src not in known_shot_ids:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': continues_from references unknown shot '{src}'"
        )
    if src == shot_id:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': continues_from cannot reference itself"
        )
    try:
        src_idx = shot_order.index(src)
        dst_idx = shot_order.index(shot_id)
    except ValueError:
        # shot_order may be partial; skip order check only if truly absent
        return
    if src_idx >= dst_idx:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': continues_from '{src}' must be declared before '{shot_id}' "
            f"in job order (found at positions {src_idx} and {dst_idx})"
        )
    if cf.get("resolved_sha256") is not None:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': continues_from.resolved_sha256 must be null at compile time; "
            f"it is filled by the executor at runtime"
        )


def check_effective_prompt_is_null(
    ctx: dict[str, Any],
    shot_id: str,
) -> None:
    """Raise ConsistencyCompileError if generation_intent.effective_prompt is not null.

    effective_prompt is an executor-written field set at runtime only.
    Any non-null value in a compiled plan means an external caller attempted to
    override it, which is forbidden.
    """
    gi = ctx.get("generation_intent")
    if gi is None:
        return
    ep = gi.get("effective_prompt")
    if ep is not None:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': generation_intent.effective_prompt must be null "
            f"at compile time (got {ep!r}). "
            f"effective_prompt is set by the executor at runtime and cannot be "
            f"provided by the caller."
        )


def check_generation_intent_matches_spec(
    ctx: dict[str, Any],
    shot_spec: dict[str, Any],
    shot_id: str,
) -> None:
    """Raise ConsistencyCompileError if base_prompt differs from shot_spec.generation_prompt."""
    gi = ctx.get("generation_intent")
    if gi is None:
        return
    base = gi.get("base_prompt", "")
    spec_prompt = shot_spec.get("generation_prompt", "")
    if base != spec_prompt:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': generation_intent.base_prompt does not match "
            f"shot_spec.generation_prompt. "
            f"base_prompt={base!r}, generation_prompt={spec_prompt!r}. "
            f"The original spec must not be rewritten."
        )


def check_asset_injections(
    ctx: dict[str, Any],
    shot_id: str,
) -> None:
    """Raise ConsistencyCompileError if any asset_injection references an asset
    not declared in this shot's asset_refs."""
    gi = ctx.get("generation_intent")
    if not gi:
        return
    asset_ref_ids = {ar["asset_id"] for ar in ctx.get("asset_refs", [])}
    for inj in gi.get("asset_injections", []):
        aid = inj.get("asset_id", "")
        if aid not in asset_ref_ids:
            raise ConsistencyCompileError(
                f"Shot '{shot_id}': asset_injections references asset '{aid}' "
                f"which is not declared in this shot's asset_refs. "
                f"Declared asset_refs: {sorted(asset_ref_ids)}"
            )


def validate_context_in_plan(
    ctx: dict[str, Any],
    shot_id: str,
    shot_spec: dict[str, Any],
    known_asset_ids: set[str],
    known_shot_ids: set[str],
    shot_order: list[str],
    asset_specs_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Run all compile-time checks on a context from plan.build_execution_plan.

    Called for every task that carries a consistency_context, whether the
    context was built via build_consistency_context() or injected directly.
    Raises ConsistencyCompileError on any violation.

    asset_specs_by_id: optional {asset_id -> asset_spec} for content_hash
    verification. When provided, each asset_ref.content_hash is recomputed
    and compared against the authoritative spec.
    """
    # 0. Type check — must be a non-empty dict (also caught earlier in plan.py,
    #    but kept here for use via build_consistency_context directly).
    if not isinstance(ctx, dict) or not ctx:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': consistency_context must be a non-empty dict, "
            f"got {type(ctx).__name__!r} with value {ctx!r}"
        )

    # 1. Schema validation (catches unknown fields, bad SHA patterns, etc.)
    errors = validate_consistency_context(ctx)
    if errors:
        raise ConsistencyCompileError(
            f"Shot '{shot_id}': consistency_context schema errors:\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    # 2. Asset references must point to known assets
    check_asset_refs(ctx, shot_id, known_asset_ids)

    # 3. Content hash must match canonical digest recomputed from asset_spec.
    #    When asset_specs_by_id is not None (plan compiler path), every asset_ref
    #    MUST have a matching spec — missing spec is an error, not a skip.
    #    When asset_specs_by_id is None (build_consistency_context builder path),
    #    the caller is responsible for supplying correct hashes.
    if asset_specs_by_id is not None:
        check_asset_content_hashes(ctx, shot_id, asset_specs_by_id)

    # 4. Continuity source must exist, precede this shot, and have null resolved_sha256
    check_continues_from(ctx, shot_id, known_shot_ids, shot_order)

    # 5. base_prompt must match shot_spec.generation_prompt
    if ctx.get("generation_intent") is not None:
        check_generation_intent_matches_spec(ctx, shot_spec, shot_id)

    # 6. effective_prompt must be null at compile time (executor-only field)
    check_effective_prompt_is_null(ctx, shot_id)

    # 7. asset_injections must only reference assets in asset_refs
    check_asset_injections(ctx, shot_id)


def detect_continuity_cycles(
    contexts: dict[str, dict[str, Any]],
) -> list[str]:
    """Return list of shot IDs involved in a continues_from cycle, empty if none.

    contexts: {shot_id -> consistency_context} for all shots that have a context.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    cycle_found: list[str] = []

    def dfs(u: str, stack: list[str]) -> bool:
        color[u] = GRAY
        stack.append(u)
        cf = contexts.get(u, {}).get("continues_from")
        if cf is not None:
            v = cf.get("shot_id", "")
            if v in contexts:
                if color.get(v, WHITE) == GRAY:
                    idx = stack.index(v)
                    cycle_found.extend(stack[idx:])
                    return True
                if color.get(v, WHITE) == WHITE and dfs(v, stack):
                    return True
        stack.pop()
        color[u] = BLACK
        return False

    for node in list(contexts.keys()):
        if color.get(node, WHITE) == WHITE:
            if dfs(node, []):
                return cycle_found
    return []


# ── Public build entry point ──────────────────────────────────────────────────


def build_consistency_context(
    *,
    asset_refs: list[dict[str, Any]],
    ref_images: list[dict[str, Any]] | None = None,
    continues_from: dict[str, Any] | None = None,
    generation_intent: dict[str, Any] | None = None,
    review_required: bool = False,
    shot_id: str,
    shot_spec: dict[str, Any],
    known_asset_ids: set[str],
    known_shot_ids: set[str],
    shot_order: list[str],
) -> dict[str, Any]:
    """Build and validate a fsf_consistency_context_v1 dict.

    Performs all compile-time integrity checks:
    - Asset references exist in the job.
    - continues_from target exists, precedes this shot, resolved_sha256 is null.
    - generation_intent.base_prompt matches shot_spec.generation_prompt.
    - asset_injections only reference assets in asset_refs.
    - Schema validation passes.

    Returns a deep-copied context dict (never mutates shot_spec or any input).
    Raises ConsistencyCompileError on any violation.
    """
    # Deep-copy all mutable inputs so external mutations cannot affect the result.
    safe_asset_refs = copy.deepcopy(asset_refs)
    safe_ref_images = copy.deepcopy(ref_images) if ref_images is not None else None
    safe_continues_from = copy.deepcopy(continues_from) if continues_from is not None else None

    gi_with_defaults: dict[str, Any] | None = None
    if generation_intent is not None:
        gi_src = copy.deepcopy(generation_intent)
        gi_with_defaults = {
            "base_prompt": gi_src.get("base_prompt", ""),
            "injection_rule_version": gi_src.get(
                "injection_rule_version", _INJECTION_RULE_VERSION
            ),
            "asset_injections": gi_src.get("asset_injections", []),
            "effective_prompt": None,  # always null at compile time
        }

    ctx: dict[str, Any] = {
        "schema_version": "fsf_consistency_context_v1",
        "asset_refs": safe_asset_refs,
        "review_required": review_required,
    }
    if safe_ref_images is not None:
        ctx["ref_images"] = safe_ref_images
    if safe_continues_from is not None:
        ctx["continues_from"] = safe_continues_from
    if gi_with_defaults is not None:
        ctx["generation_intent"] = gi_with_defaults

    # ── Compile-time checks ──────────────────────────────────────────────────
    validate_context_in_plan(
        ctx,
        shot_id=shot_id,
        shot_spec=shot_spec,
        known_asset_ids=known_asset_ids,
        known_shot_ids=known_shot_ids,
        shot_order=shot_order,
        # Note: builder callers don't provide asset_specs_by_id; content_hash
        # is the caller's responsibility when using build_consistency_context directly.
        # The plan compiler always provides asset_specs_by_id for full verification.
        asset_specs_by_id=None,
    )

    return ctx


# ── C1: Execution-time context resolution ────────────────────────────────────


class ConsistencyResolutionError(ValueError):
    """Raised when execution-time resolution of a consistency_context fails."""


def _verify_ref_image_item(
    ri: dict[str, Any],
    *,
    shot_id: str,
    context_label: str,
    ref_image_files: "dict[str, Path] | None",
    item_index: int,
) -> None:
    """Verify a single ref_image entry, filling ri["path"] from ref_image_files if needed.

    ref_image_files is keyed by **expected sha256** (not role), allowing multiple
    images sharing the same role to be individually resolved and verified.

    Raises ConsistencyResolutionError when:
    - The image cannot be located (no path in ri AND no match in ref_image_files)
    - The file does not exist on disk
    - The file cannot be read
    - The computed SHA-256 does not match ri["sha256"]
    """
    from pathlib import Path as _Path

    declared_sha = ri.get("sha256", "")
    role = ri.get("role", f"index-{item_index}")
    label = f"{context_label} ref_image[{item_index}] role='{role}'"

    # 1. Resolve path: prefer ri["path"]; fall back to ref_image_files[declared_sha]
    path_str = ri.get("path")
    if not path_str:
        if ref_image_files and declared_sha and declared_sha in ref_image_files:
            path_str = str(ref_image_files[declared_sha])
            ri["path"] = path_str
        else:
            # Cannot locate the file — declared sha and no path is an error
            if declared_sha:
                raise ConsistencyResolutionError(
                    f"Shot '{shot_id}': {label} has sha256='{declared_sha[:16]}…' but no "
                    f"resolvable path.  Add the image path to the compiled context or "
                    f"supply it via ref_image_files[sha256]."
                )
            else:
                raise ConsistencyResolutionError(
                    f"Shot '{shot_id}': {label} has neither path nor sha256 — "
                    f"cannot verify.  All reference images must be fully declared."
                )

    img_path = _Path(path_str)

    # 2. Existence check
    if not img_path.is_file():
        raise ConsistencyResolutionError(
            f"Shot '{shot_id}': {label} file not found: {img_path}"
        )

    # 3. Read & verify SHA
    try:
        actual_sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConsistencyResolutionError(
            f"Shot '{shot_id}': {label} could not be read: {exc}"
        ) from exc

    if not declared_sha:
        raise ConsistencyResolutionError(
            f"Shot '{shot_id}': {label} has no declared sha256.  "
            f"All ref_image entries must carry a sha256 for integrity verification."
        )

    if actual_sha != declared_sha:
        raise ConsistencyResolutionError(
            f"Shot '{shot_id}': {label} SHA-256 mismatch. "
            f"Declared: {declared_sha[:16]}…, "
            f"Actual: {actual_sha[:16]}…. "
            f"Recompute ref_image sha256 after changing the reference image file."
        )


def resolve_execution_context(
    ctx: dict[str, Any],
    *,
    shot_id: str,
    resolved_artifact_shas: dict[str, str] | None = None,
    ref_image_files: "dict[str, Path] | None" = None,
) -> dict[str, Any]:
    """Build an execution-time copy of a consistency_context.

    This function is called by the plan executor immediately before dispatching
    a generation step.  It performs — in the mandated order:

    1. Deep copy: the original compiled context is never mutated.
    2. continues_from.resolved_sha256: if the upstream shot has completed,
       fill in its output artifact SHA from resolved_artifact_shas
       {shot_id -> sha256}.  Still None is accepted when upstream is not yet
       done (deferred jobs); an explicit non-null value in the compiled context
       is rejected (compile-time invariant already enforced by B1).
       When resolved_artifact_shas is provided and the source shot_id is absent,
       raises ConsistencyResolutionError (required source not yet available).
    3. ref_images SHA verification (shot-level AND asset-level):
       ALL ref_image entries — in top-level ref_images[] AND in every
       asset_refs[i].ref_images[] — are individually located and verified.
       ref_image_files is keyed by expected sha256 (not role) to support
       multiple images with the same role.  An entry without a path AND
       without a matching sha256 in ref_image_files raises
       ConsistencyResolutionError.  Missing files, unreadable files, or SHA
       mismatches always raise ConsistencyResolutionError, including on resume.
    4. effective_prompt: assembled from generation_intent if present.
       Rule: base_prompt + each asset_injection.injected_text (in declaration
       order), separated by a single space.  Stored in
       generation_intent.effective_prompt (executor-writable field).
    5. execution_context_digest: canonical_context_digest() of the resolved
       copy (after filling resolved_sha256 and effective_prompt).  Stored
       under the top-level key "execution_context_digest" so the plan executor
       can include it in the step inputs_digest.

    Returns the resolved execution context dict.

    Raises ConsistencyResolutionError on any resolution failure.
    """
    from pathlib import Path as _Path  # noqa: F401 (used in _verify_ref_image_item)

    if not isinstance(ctx, dict) or not ctx:
        raise ConsistencyResolutionError(
            f"Shot '{shot_id}': consistency_context must be a non-empty dict"
        )

    # 1. Deep copy — never mutate the compiled plan
    ec: dict[str, Any] = copy.deepcopy(ctx)

    # 2. continues_from.resolved_sha256 — fill from upstream completed artifact
    cf = ec.get("continues_from")
    if cf is not None:
        if cf.get("resolved_sha256") is not None:
            raise ConsistencyResolutionError(
                f"Shot '{shot_id}': continues_from.resolved_sha256 must be null "
                f"in the compiled context (got {cf['resolved_sha256']!r}). "
                f"This is a compile-time invariant enforced by B1."
            )
        src_shot_id = cf.get("shot_id", "")
        if resolved_artifact_shas is not None:
            if src_shot_id in resolved_artifact_shas:
                sha = resolved_artifact_shas[src_shot_id]
                if not sha:
                    raise ConsistencyResolutionError(
                        f"Shot '{shot_id}': continues_from source shot '{src_shot_id}' "
                        f"completed but produced an empty SHA-256. "
                        f"Cannot dispatch without a verified source artifact."
                    )
                cf["resolved_sha256"] = sha
            else:
                raise ConsistencyResolutionError(
                    f"Shot '{shot_id}': continues_from references shot '{src_shot_id}' "
                    f"but that shot has no resolved artifact SHA yet. "
                    f"This step cannot be dispatched until its continuity source is complete."
                )
        # else resolved_artifact_shas is None → leave as None (caller skipping source check)

    # 3a. Shot-level ref_images verification
    for idx, ri in enumerate(ec.get("ref_images", [])):
        _verify_ref_image_item(
            ri,
            shot_id=shot_id,
            context_label="shot-level",
            ref_image_files=ref_image_files,
            item_index=idx,
        )

    # 3b. Asset-level ref_images verification (asset_refs[i].ref_images)
    for asset_ref in ec.get("asset_refs", []):
        asset_id = asset_ref.get("asset_id", "<unknown>")
        for idx, ri in enumerate(asset_ref.get("ref_images", [])):
            _verify_ref_image_item(
                ri,
                shot_id=shot_id,
                context_label=f"asset '{asset_id}'",
                ref_image_files=ref_image_files,
                item_index=idx,
            )

    # 4. effective_prompt — assemble from generation_intent
    gi = ec.get("generation_intent")
    if gi is not None:
        if gi.get("effective_prompt") is not None:
            raise ConsistencyResolutionError(
                f"Shot '{shot_id}': generation_intent.effective_prompt must be "
                f"null before execution-time resolution (compile-time invariant). "
                f"Got {gi['effective_prompt']!r}."
            )
        parts: list[str] = []
        base = gi.get("base_prompt", "").strip()
        if base:
            parts.append(base)
        for inj in gi.get("asset_injections", []):
            injected = inj.get("injected_text", "").strip()
            if injected:
                parts.append(injected)
        gi["effective_prompt"] = " ".join(parts) if parts else None

    # 5. execution_context_digest
    ec["execution_context_digest"] = canonical_context_digest(ec)

    return ec
