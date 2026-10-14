"""FSF Asynchronous Handoff Protocol & Workorder Management.

Enables decoupled execution where media generation is handed off to external
agent environments (e.g. Muse MCP/internal tools, human operators, remote workers):
- Emits self-contained, tamper-evident Workorders with authoritative digests:
  (job_id, run_id, workorder_id, step_id, attempt_id, plan_digest, inputs_digest, expected_contract_digest).
- Ingests Workorder Results with strict cryptographic integrity & path containment:
  * Prevents path traversal (rejects references escaping output directory).
  * Validates sha256 checksums and non-zero artifact sizes.
  * Ensures idempotent result reuse (safely reuses verified matching results).
  * Rejects stale attempts, conflicting digests, and unauthenticated alterations.
  * Sanitizes all diagnostics, errors, and detail messages.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fsf.conformance.digest import canonical_contract_digest
from fsf.executors.subprocess_safety import sanitize_process_text

WORKORDER_SCHEMA_VERSION = "fsf_workorder_v1"
RESULT_SCHEMA_VERSION = "fsf_workorder_result_v1"
CONSISTENCY_REQUIREMENTS_VERSION = "fsf_consistency_requirements_v1"

# Canonical allowed values for consistency_acknowledgement.continuity_method.
# All comparisons are done after lowercasing the reported value.
# Any value outside this set is rejected during ingest, including "none".
# "prompt_only" is additionally rejected when prompt_only_allowed=False in
# the workorder's consistency_requirements.
VALID_CONTINUITY_METHODS: frozenset[str] = frozenset({
    "first_frame_init",
    "ip_adapter",
    "reference_latent",
    "reference_image",
    "video_continuation",
    "prompt_only",
})


def build_consistency_requirements(
    execution_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract an immutable consistency-requirements vector from a resolved execution context.

    The vector is embedded in the workorder so the external agent knows what
    constraints to honour.  The result must echo ``requirements_digest`` in
    ``consistency_acknowledgement.acknowledged_digest`` — this confirms the
    agent **received** the requirements.  It does NOT prove the agent actually
    complied; visual review remains the authoritative compliance gate.

    Returns None when *execution_context* is empty or carries no declared
    constraints (no generation_intent, no ref_images, no continues_from).

    The returned dict has the structure::

        {
            "schema_version":       "fsf_consistency_requirements_v1",
            "effective_prompt":     str | None,
            "ref_image_shas":       {"{context}[{i}]": sha256, ...},
            "resolved_sha256":      str | None,     # continuity source SHA
            "source_shot_id":       str | None,     # continuity source shot
            "aspects":              list[str],       # what to preserve
            "prompt_only_allowed":  bool,            # may agent fall back to prompt-only?
            "asset_ref_digests":    {asset_id: content_hash, ...},
            "requirements_digest":  sha256-hex of the canonical vector,
        }

    ``prompt_only_allowed`` is ``False`` whenever any visual constraint is
    declared (ref_images or continuity source).  The agent must not degrade
    to prompt-only continuity unless this flag is explicitly ``True``.
    """
    if not isinstance(execution_context, dict) or not execution_context:
        return None

    # -- effective_prompt
    gi = execution_context.get("generation_intent") or {}
    effective_prompt: str | None = gi.get("effective_prompt") or None

    # -- ref_image_shas: keyed by "{context}[{index}]" to support multi-role images
    ref_image_shas: dict[str, str] = {}
    for idx, ri in enumerate(execution_context.get("ref_images", [])):
        sha = ri.get("sha256", "")
        if sha:
            ref_image_shas[f"shot[{idx}]"] = sha
    for ar in execution_context.get("asset_refs", []):
        aid = ar.get("asset_id", "")
        for idx, ri in enumerate(ar.get("ref_images", [])):
            sha = ri.get("sha256", "")
            if sha:
                ref_image_shas[f"asset:{aid}[{idx}]"] = sha

    # -- resolved_sha256 + continuity policy from continues_from
    cf = execution_context.get("continues_from") or {}
    resolved_sha256: str | None = cf.get("resolved_sha256") or None
    source_shot_id: str | None = cf.get("shot_id") or None
    aspects: list[str] = list(cf.get("aspects") or [])

    # prompt_only_allowed: False whenever any visual constraint is declared.
    # An agent must not silently degrade to prompt-only if ref images or a
    # continuity source are required.
    has_visual_refs = bool(ref_image_shas) or (resolved_sha256 is not None)
    prompt_only_allowed: bool = not has_visual_refs

    # -- asset_ref_digests: {asset_id -> content_hash}
    asset_ref_digests: dict[str, str] = {}
    for ar in execution_context.get("asset_refs", []):
        aid = ar.get("asset_id", "")
        ch = ar.get("content_hash", "")
        if aid and ch:
            asset_ref_digests[aid] = ch

    # Nothing declared → no requirements vector
    if (
        effective_prompt is None
        and not ref_image_shas
        and resolved_sha256 is None
        and not asset_ref_digests
    ):
        return None

    vector: dict[str, Any] = {
        "schema_version": CONSISTENCY_REQUIREMENTS_VERSION,
        "effective_prompt": effective_prompt,
        "ref_image_shas": ref_image_shas,
        "resolved_sha256": resolved_sha256,
        "source_shot_id": source_shot_id,
        "aspects": aspects,
        "prompt_only_allowed": prompt_only_allowed,
        "asset_ref_digests": asset_ref_digests,
    }
    vector["requirements_digest"] = canonical_contract_digest(vector)
    return vector


class HandoffSecurityError(RuntimeError):
    """Raised when a workorder or result violates path confinement or integrity."""


class HandoffIntegrityError(ValueError):
    """Raised when digest or attempt verification fails."""


def safe_resolve_artifact_path(raw_path: str, root_dir: Path) -> Path:
    """Safely resolve an artifact path, guaranteeing containment within root_dir."""
    root = Path(root_dir).resolve()
    clean_str = raw_path.strip().replace("artifact:", "").replace("report:", "")

    # Check for obvious traversal sequences
    parts = Path(clean_str).parts
    if ".." in parts:
        raise HandoffSecurityError(
            f"Path traversal attempt rejected in artifact_path: '{raw_path}'"
        )

    cand = Path(clean_str)
    if not cand.is_absolute():
        resolved = (root / cand).resolve()
    else:
        resolved = cand.resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise HandoffSecurityError(
            f"Artifact path '{raw_path}' resolves outside allowed output root '{root}': '{resolved}'"
        ) from None

    return resolved


def build_workorder(
    *,
    job_id: str,
    run_id: str,
    step_id: str,
    attempt_id: int,
    tool: str,
    plan_digest: str,
    inputs_digest: str,
    target_artifact: str,
    task_payload: dict[str, Any],
    expected_media_contract: dict[str, Any] | None = None,
    expected_contract_digest: str | None = None,
    consistency_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct an authoritative fsf_workorder_v1.

    consistency_requirements: optional vector produced by
    build_consistency_requirements().  When present it is embedded in the
    workorder so the agent knows what constraints to honour, and its
    requirements_digest is used to verify the result's acknowledgement.
    """
    now = datetime.now(timezone.utc).isoformat()
    clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(job_id))
    clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))
    clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))
    workorder_id = f"wo_{clean_job}_{clean_run}_{clean_step}_att{attempt_id}"

    if expected_media_contract and not expected_contract_digest:
        expected_contract_digest = canonical_contract_digest(expected_media_contract)

    # Sanitize task prompts/specs before emission (recursively handles dicts/lists/strings)
    scrubbed_task = sanitize_process_text(task_payload)

    workorder: dict[str, Any] = {
        "schema_version": WORKORDER_SCHEMA_VERSION,
        "workorder_id": workorder_id,
        "job_id": str(job_id),
        "run_id": str(run_id),
        "step_id": str(step_id),
        "attempt_id": int(attempt_id),
        "tool": str(tool),
        "plan_digest": str(plan_digest),
        "inputs_digest": str(inputs_digest),
        "expected_contract_digest": expected_contract_digest,
        "expected_media_contract": copy.deepcopy(expected_media_contract) if expected_media_contract else None,
        "target_artifact": str(target_artifact),
        "task_payload": scrubbed_task,
        "created_at": now,
        "status": "pending",
    }
    # Embed consistency requirements when present.
    # Store the full vector (for agent use) and the digest (for result verification).
    if consistency_requirements:
        workorder["consistency_requirements"] = copy.deepcopy(consistency_requirements)
        workorder["requirements_digest"] = consistency_requirements.get("requirements_digest")
    else:
        workorder["consistency_requirements"] = None
        workorder["requirements_digest"] = None

    workorder["workorder_digest"] = canonical_contract_digest(workorder)
    return workorder


def write_workorder(workorder: dict[str, Any], output_dir: Path | str) -> Path:
    """Write workorder to <output_dir>/handoff/workorders/<workorder_id>.workorder.json."""
    out = Path(output_dir).resolve()
    wo_dir = out / "handoff" / "workorders"
    wo_dir.mkdir(parents=True, exist_ok=True)

    wo_id = workorder["workorder_id"]
    wo_file = wo_dir / f"{wo_id}.workorder.json"
    payload = json.dumps(workorder, indent=2, ensure_ascii=False)
    wo_file.write_text(payload, encoding="utf-8")
    return wo_file


def list_pending_workorders(output_dir: Path | str) -> list[dict[str, Any]]:
    """List all pending workorders in <output_dir>/handoff/workorders/."""
    out = Path(output_dir).resolve()
    wo_dir = out / "handoff" / "workorders"
    if not wo_dir.is_dir():
        return []

    workorders = []
    for p in sorted(wo_dir.glob("*.workorder.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("schema_version") == WORKORDER_SCHEMA_VERSION:
                workorders.append(data)
        except Exception:
            continue
    return workorders


def find_result_file(
    output_dir: Path | str,
    *,
    job_id: str,
    run_id: str,
    step_id: str,
    attempt_id: int,
) -> Path | None:
    """Find matching result file under <output_dir>/handoff/results/ strictly bound to job, run, step, and attempt."""
    out = Path(output_dir).resolve()
    res_dir = out / "handoff" / "results"
    if not res_dir.is_dir():
        return None

    clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(job_id))
    clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))
    clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))

    # Canonical naming pattern
    patterns = [
        f"res_{clean_job}_{clean_run}_{clean_step}_att{attempt_id}.result.json",
        f"res_{clean_job}_{clean_run}_{clean_step}_att{attempt_id}.json",
        f"res_{clean_step}_att{attempt_id}.result.json",
        f"res_{clean_step}_att{attempt_id}.json",
    ]
    for pat in patterns:
        cand = res_dir / pat
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if (
                    data.get("schema_version") == RESULT_SCHEMA_VERSION
                    and str(data.get("step_id")) == step_id
                    and int(data.get("attempt_id", -1)) == attempt_id
                    and str(data.get("job_id")) == job_id
                    and str(data.get("run_id")) == run_id
                ):
                    return cand
            except Exception:
                continue

    # Scan directory for matching content
    for p in sorted(res_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if (
                data.get("schema_version") == RESULT_SCHEMA_VERSION
                and str(data.get("step_id")) == step_id
                and int(data.get("attempt_id", -1)) == attempt_id
                and str(data.get("job_id")) == job_id
                and str(data.get("run_id")) == run_id
            ):
                return p
        except Exception:
            continue

    return None


def ingest_workorder_result(
    result_path: Path | str,
    workorder: dict[str, Any],
    output_dir: Path | str,
    *,
    previous_accepted_sha: str | None = None,
) -> dict[str, Any]:
    """Validate and ingest an agent-filled workorder result.

    Enforces:
    1. Schema version and authoritative identity binding (mandatory, non-omittable):
       - job_id, run_id, workorder_id, step_id, attempt_id match workorder exactly.
       - plan_digest, inputs_digest match workorder exactly.
       - expected_contract_digest matches workorder if defined.
    2. Target artifact path binding:
       - Declared artifact_path must resolve to the exact target_artifact specified in workorder.
       - Rejects path traversal and attempts to substitute arbitrary pre-existing files.
    3. Artifact validation:
       - File exists, size > 0.
       - artifact_sha256 is mandatory and matches computed SHA-256.
    4. Idempotence & conflict detection:
       - If previous_accepted_sha exists and matches current sha, result is reused idempotently.
       - If previous_accepted_sha exists but differs, raises conflict error.
    5. Masking:
       - Error and detail strings are sanitized against sensitive values.
    """
    out_root = Path(output_dir).resolve()
    res_p = Path(result_path).resolve()
    if not res_p.is_file():
        raise FileNotFoundError(f"Result file not found: {res_p}")

    try:
        data = json.loads(res_p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HandoffIntegrityError(f"Failed to parse result JSON from '{res_p}': {exc}") from exc

    if data.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise HandoffIntegrityError(
            f"Result schema version mismatch: expected '{RESULT_SCHEMA_VERSION}', got '{data.get('schema_version')}'"
        )

    # Authority bindings (all required, non-omittable)
    if not data.get("job_id") or str(data.get("job_id")) != str(workorder.get("job_id")):
        raise HandoffIntegrityError(
            f"Result job_id mismatch: '{data.get('job_id')}' != '{workorder.get('job_id')}'"
        )

    if not data.get("run_id") or str(data.get("run_id")) != str(workorder.get("run_id")):
        raise HandoffIntegrityError(
            f"Result run_id mismatch: '{data.get('run_id')}' != '{workorder.get('run_id')}'"
        )

    if not data.get("step_id") or str(data.get("step_id")) != str(workorder.get("step_id")):
        raise HandoffIntegrityError(
            f"Result step_id mismatch: '{data.get('step_id')}' != '{workorder.get('step_id')}'"
        )

    if int(data.get("attempt_id", -1)) != int(workorder.get("attempt_id", 0)):
        raise HandoffIntegrityError(
            f"Result attempt_id mismatch: '{data.get('attempt_id')}' != '{workorder.get('attempt_id')}'"
        )

    if not data.get("workorder_id") or str(data.get("workorder_id")) != str(workorder.get("workorder_id")):
        raise HandoffIntegrityError(
            f"Result workorder_id mismatch: '{data.get('workorder_id')}' != '{workorder.get('workorder_id')}'"
        )

    # Mandatory digests
    if not data.get("plan_digest") or data["plan_digest"] != workorder.get("plan_digest"):
        raise HandoffIntegrityError(
            f"Result plan_digest mismatch: '{data.get('plan_digest')}' != '{workorder.get('plan_digest')}'"
        )

    if not data.get("inputs_digest") or data["inputs_digest"] != workorder.get("inputs_digest"):
        raise HandoffIntegrityError(
            f"Result inputs_digest mismatch: '{data.get('inputs_digest')}' != '{workorder.get('inputs_digest')}'"
        )

    exp_dig = workorder.get("expected_contract_digest")
    if exp_dig:
        if not data.get("expected_contract_digest") or data["expected_contract_digest"] != exp_dig:
            raise HandoffIntegrityError(
                f"Result expected_contract_digest mismatch: '{data.get('expected_contract_digest')}' != '{exp_dig}'"
            )

    # Consistency-requirements acknowledgement.
    # Purpose: confirms the agent RECEIVED the requirements vector.
    # Limitation: echoing the digest does NOT prove the agent actually complied.
    # Visual review remains the authoritative compliance gate; ingest only
    # performs structural checks, not semantic validation of the output.
    #
    # Gate 0 — reject top-level misplacement of consistency fields.
    # An agent must NOT place capability_gap, ref_assets_used, or
    # continuity_method at the top level of the result; they belong exclusively
    # inside consistency_acknowledgement.  Top-level placement is rejected
    # outright (not silently ignored) to prevent a split-report exploit where
    # the inner ack looks clean while a real gap hides at the top level.
    req_digest = workorder.get("requirements_digest")
    if req_digest:
        _CONSISTENCY_TOP_LEVEL_FORBIDDEN = ("capability_gap", "ref_assets_used", "continuity_method")
        for field in _CONSISTENCY_TOP_LEVEL_FORBIDDEN:
            top_val = data.get(field)
            if top_val is not None and top_val != "" and top_val != []:
                raise HandoffIntegrityError(
                    f"Result places '{field}' at the top level of the result object.  "
                    f"All consistency fields (capability_gap, ref_assets_used, "
                    f"continuity_method) must be nested inside "
                    f"'consistency_acknowledgement', not at the top level.  "
                    f"Top-level placement is rejected to prevent split-report exploits."
                )

    # Gate 1 — acknowledged_digest presence and match (always required when
    # the workorder carries a requirements_digest).
    if req_digest:
        ack = data.get("consistency_acknowledgement")
        if not ack or not isinstance(ack, dict):
            raise HandoffIntegrityError(
                f"Result is missing required 'consistency_acknowledgement'.  "
                f"Workorder '{workorder.get('workorder_id')}' carries consistency "
                f"requirements (digest={req_digest[:16]}…) that the result must acknowledge."
            )
        ack_dig = str(ack.get("acknowledged_digest", "")).strip()
        if not ack_dig:
            raise HandoffIntegrityError(
                f"Result 'consistency_acknowledgement.acknowledged_digest' is absent or empty.  "
                f"Expected digest={req_digest[:16]}…"
            )
        if ack_dig != req_digest:
            raise HandoffIntegrityError(
                f"Result consistency_acknowledgement.acknowledged_digest mismatch: "
                f"got '{ack_dig[:16]}…', expected '{req_digest[:16]}…'.  "
                f"The agent must operate against the workorder's consistency_requirements "
                f"and echo their digest verbatim."
            )

    status = str(data.get("status", "")).lower()
    if status != "completed":
        clean_err = sanitize_process_text(str(data.get("error") or data.get("detail") or "Agent reported failure"))
        return {
            "status": "failed",
            "error": clean_err,
            "step_id": workorder["step_id"],
            "workorder_id": workorder["workorder_id"],
            "attempt_id": workorder["attempt_id"],
        }

    raw_art = str(data.get("artifact_path") or "")
    if not raw_art:
        raise HandoffIntegrityError("Result declared status 'completed' but missing artifact_path")

    # Path confinement and target artifact binding
    resolved_artifact = safe_resolve_artifact_path(raw_art, out_root)
    expected_target = safe_resolve_artifact_path(workorder["target_artifact"], out_root)
    if resolved_artifact != expected_target:
        raise HandoffIntegrityError(
            f"Result artifact '{resolved_artifact.name}' does not match workorder target_artifact '{expected_target.name}'"
        )

    if not resolved_artifact.is_file():
        raise FileNotFoundError(
            f"Result artifact file not found at resolved location: '{resolved_artifact}'"
        )

    size = resolved_artifact.stat().st_size
    if size == 0:
        raise HandoffIntegrityError(
            f"Result artifact '{resolved_artifact.name}' is 0 bytes (empty file rejected)"
        )

    # Mandatory declared SHA-256 validation
    declared_sha = str(data.get("artifact_sha256") or "").strip().lower()
    if not declared_sha:
        raise HandoffIntegrityError("Result declared status 'completed' but missing required 'artifact_sha256'")

    actual_sha = hashlib.sha256(resolved_artifact.read_bytes()).hexdigest().lower()
    if declared_sha != actual_sha:
        raise HandoffIntegrityError(
            f"Result SHA-256 mismatch for '{resolved_artifact.name}': "
            f"declared '{declared_sha}', computed '{actual_sha}'"
        )

    # Idempotence & conflict validation
    if previous_accepted_sha and previous_accepted_sha.lower() != actual_sha.lower():
        raise HandoffIntegrityError(
            f"Conflict: Step '{workorder['step_id']}' already accepted SHA '{previous_accepted_sha}', "
            f"received conflicting SHA '{actual_sha}'"
        )

    # ------------------------------------------------------------------ #
    # Consistency result audit (completed path only).                      #
    # Gate 2 — capability_gap: a completed result must not carry an        #
    # unresolved capability gap.  The agent must report failure instead.   #
    # Gate 3 — ref_assets_used: every ref asset declared in               #
    # requirements.ref_image_shas must be reported with its correct SHA.  #
    # Gate 4 — continuity_method: when a continuity source is declared,   #
    # the agent must report how it was used; prompt_only is rejected when  #
    # prompt_only_allowed is False.                                        #
    #                                                                      #
    # NOTE: These structural gates check reported metadata only.  They do  #
    # not guarantee visual quality or semantic compliance; human visual     #
    # review remains the authoritative compliance step.                    #
    # ------------------------------------------------------------------ #
    if req_digest and ack:
        req_obj = workorder.get("consistency_requirements") or {}

        # Gate 2 — capability_gap
        cap_gap = (ack.get("capability_gap") or "").strip()
        if cap_gap:
            raise HandoffIntegrityError(
                f"Result declares status 'completed' but reports an unresolved "
                f"capability_gap: '{sanitize_process_text(cap_gap)}'.  "
                f"A result with an unresolved capability gap must not be marked completed; "
                f"report status='failed' with the gap description instead."
            )

        # Gate 3 — ref_assets_used
        declared_shas: dict[str, str] = req_obj.get("ref_image_shas") or {}
        if declared_shas:
            used_list = ack.get("ref_assets_used") or []
            if not used_list:
                raise HandoffIntegrityError(
                    f"Consistency requirements declare {len(declared_shas)} reference "
                    f"image(s) but result.consistency_acknowledgement.ref_assets_used "
                    f"is absent or empty.  The agent must report each asset it used "
                    f"with its key and sha256."
                )
            # Build lookup by key — strict: every entry must be a dict with
            # both 'key' and 'sha256'; duplicate keys are rejected because
            # they can silently override a legitimate entry with a bad SHA.
            used_by_key: dict[str, str] = {}
            for i, entry in enumerate(used_list):
                if not isinstance(entry, dict):
                    raise HandoffIntegrityError(
                        f"ref_assets_used[{i}] is not an object "
                        f"(got {type(entry).__name__!r}).  "
                        f"Each entry must be a JSON object with 'key' and 'sha256'."
                    )
                k = str(entry.get("key", "")).strip()
                v = str(entry.get("sha256", "")).strip().lower()
                if not k:
                    raise HandoffIntegrityError(
                        f"ref_assets_used[{i}] is missing required field 'key'."
                    )
                if not v:
                    raise HandoffIntegrityError(
                        f"ref_assets_used[{i}] (key='{k}') is missing required "
                        f"field 'sha256'."
                    )
                if k in used_by_key:
                    raise HandoffIntegrityError(
                        f"ref_assets_used contains duplicate key '{k}'.  "
                        f"Each reference asset must appear exactly once; "
                        f"duplicate entries could mask a conflicting SHA."
                    )
                used_by_key[k] = v
            for key, expected_sha in declared_shas.items():
                if key not in used_by_key:
                    raise HandoffIntegrityError(
                        f"Required reference asset '{key}' (sha={expected_sha[:16]}…) "
                        f"is not reported in ref_assets_used.  "
                        f"The agent must report every declared reference asset."
                    )
                reported_sha = used_by_key[key]
                if reported_sha != expected_sha.lower():
                    raise HandoffIntegrityError(
                        f"Reference asset '{key}' SHA mismatch: "
                        f"declared={expected_sha[:16]}…, "
                        f"reported={reported_sha[:16] if reported_sha else '(empty)'}….  "
                        f"The agent must operate against the exact declared assets."
                    )

        # Gate 4 — continuity_method
        if req_obj.get("resolved_sha256"):
            cont_method_raw = (ack.get("continuity_method") or "").strip()
            if not cont_method_raw:
                raise HandoffIntegrityError(
                    f"Consistency requirements declare a continuity source "
                    f"(resolved_sha256={req_obj['resolved_sha256'][:16]}…) "
                    f"but result.consistency_acknowledgement.continuity_method is absent.  "
                    f"The agent must report how it used the continuity source."
                )
            # Normalize to lowercase so "PROMPT_ONLY" and "Prompt_Only" are
            # treated identically.  Any value not in VALID_CONTINUITY_METHODS
            # is rejected, including "none" which signals a skipped usage.
            cont_method = cont_method_raw.lower()
            if cont_method not in VALID_CONTINUITY_METHODS:
                raise HandoffIntegrityError(
                    f"Result reports unknown continuity_method='{sanitize_process_text(cont_method_raw)}'.  "
                    f"Accepted values: {sorted(VALID_CONTINUITY_METHODS)!r}.  "
                    f"The agent must declare how it used the continuity source; "
                    f"'none' is not a valid report when a source is required."
                )
            prompt_only_ok: bool = bool(req_obj.get("prompt_only_allowed", True))
            if cont_method == "prompt_only" and not prompt_only_ok:
                raise HandoffIntegrityError(
                    f"Result reports continuity_method='prompt_only' but the workorder "
                    f"does not permit prompt-only continuity (prompt_only_allowed=False) "
                    f"for this shot.  The agent must use the declared continuity source "
                    f"(resolved_sha256={req_obj['resolved_sha256'][:16]}…)."
                )

    clean_detail = sanitize_process_text(str(data.get("detail") or ""))

    result: dict[str, Any] = {
        "status": "completed",
        "job_id": workorder["job_id"],
        "run_id": workorder["run_id"],
        "step_id": workorder["step_id"],
        "workorder_id": workorder["workorder_id"],
        "attempt_id": workorder["attempt_id"],
        "artifact_path": str(resolved_artifact),
        "artifact_sha256": actual_sha,
        "size_bytes": size,
        "detail": clean_detail,
        "result_digest": canonical_contract_digest(data),
    }
    # Include the verified consistency_acknowledgement in the result so the
    # executor can record it in the journal for auditability.
    # This records what the agent REPORTED; visual compliance is not implied.
    if req_digest and ack:
        result["consistency_acknowledgement"] = {
            "acknowledged_digest": ack_dig,
            "ref_assets_used": ack.get("ref_assets_used"),
            "continuity_method": ack.get("continuity_method") or None,
        }
    return result
