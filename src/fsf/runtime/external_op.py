"""FSF External Operation & Durable Resume Runtime (FSF-M2).

Manages external operations (e.g. Kaggle / Remote GPU workers),
implements checkpointing, secret scrubbing, and artifact reuse for partial resumes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pdx_artifact_core import (
    create_external_operation,
    validate_external_operation,
)


def scrub_dict_secrets(data: Any) -> Any:
    """Recursively scrub secrets from dictionaries, lists, and strings."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if any(term in k.lower() for term in ["key", "token", "secret", "password", "auth"]):
                cleaned[k] = "[REDACTED_SECRET]"
            else:
                cleaned[k] = scrub_dict_secrets(v)
        return cleaned
    elif isinstance(data, list):
        return [scrub_dict_secrets(item) for item in data]
    elif isinstance(data, str):
        return scrub_string_secrets(data)
    else:
        return data


def scrub_string_secrets(text: str) -> str:
    """Scrub tokens, API keys, and sensitive paths from raw strings."""
    # Scrub Windows user profile paths (single and double backslashes)
    text = re.sub(r"[A-Za-z]:(?:\\\\|\\)[Uu]sers(?:\\\\|\\)[^\\\"'\s]+", "[REDACTED_USER_PATH]", text)
    text = re.sub(r"/Users/[^/\"'\s]+", "[REDACTED_USER_PATH]", text)
    text = re.sub(r"/home/[^/\"'\s]+", "[REDACTED_USER_PATH]", text)
    # Scrub tokens
    text = re.sub(r"HF_[A-Za-z0-9_]+", "[REDACTED_SECRET]", text, flags=re.I)
    text = re.sub(r"ghp_[A-Za-z0-9]+", "[REDACTED_SECRET]", text)
    text = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_SECRET]", text)
    return text


def scrub_secrets(text: str) -> str:
    """General text scrubbing helper."""
    return scrub_string_secrets(text)


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_fsf_external_op(
    *,
    operation_id: str,
    provider: str = "kaggle",
    execution_mode: str = "asynchronous",
    request_id: str = "req:fsf:op:001",
    idempotency_key: str | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and validate a pdx_external_operation_v1 for FSF."""
    scrubbed_inputs = scrub_dict_secrets(inputs or {})
    inp_json = json.dumps(scrubbed_inputs, sort_keys=True)
    inp_digest = hashlib.sha256(inp_json.encode("utf-8")).hexdigest()
    req_digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    idem = idempotency_key or f"idem:{operation_id}"

    op = create_external_operation(
        operation_id=operation_id,
        provider=provider,
        execution_mode=execution_mode,
        request_id=request_id,
        idempotency_key=idem,
        request_digest=req_digest,
        input_digest=inp_digest,
        submitted_at=now,
        status="submitted",
    )
    errors = validate_external_operation(op)
    if errors:
        raise ValueError("Invalid external operation:\n" + "\n".join(errors))
    return op


CHECKPOINT_SCHEMA_VERSION = "fsf_checkpoint_v3"
SHAPE_EXECUTION_KINDS = frozenset({"mock", "shape"})
MEDIA_EXECUTION_KIND = "media"
MEDIA_CONFORMANCE_SCHEMA = "pdx_media_conformance_result_v1"
_ARTIFACT_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_JOB_DIGEST_EXCLUDE_JOB_KEYS = frozenset(
    {"pending_count", "completed_count", "is_resumed"}
)
_JOB_DIGEST_EXCLUDE_TASK_KEYS = frozenset({"execution_status", "artifact"})


def compute_job_digest(job_manifest: dict[str, Any]) -> str:
    """Canonical digest of the immutable job execution projection.

    Mutable resume annotations are excluded by whitelist:
    job ``pending_count`` / ``completed_count`` / ``is_resumed``;
    task ``execution_status`` / ``artifact``.
    Everything else on the job and each task is bound, including prompt, shot
    spec, duration, mode, image, ordinal, and the compiled expected media contract.
    Task order is the execution order (not sorted).
    """
    from fsf.conformance.media import build_expected_contract

    job_proj = {
        key: value
        for key, value in job_manifest.items()
        if key not in _JOB_DIGEST_EXCLUDE_JOB_KEYS
    }
    tasks = []
    for task in job_manifest.get("tasks", []):
        if not isinstance(task, dict):
            continue
        projected = {
            key: value
            for key, value in task.items()
            if key not in _JOB_DIGEST_EXCLUDE_TASK_KEYS
        }
        projected["expected_media_contract"] = build_expected_contract(task)
        tasks.append(projected)
    job_proj["tasks"] = tasks
    encoded = json.dumps(
        job_proj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_safe_artifact_basename(name: str) -> bool:
    """True only for a single basename that cannot escape the artifact directory."""
    if not isinstance(name, str) or not name:
        return False
    if name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or ":" in name:
        return False
    if "\x00" in name:
        return False
    sep = os.path.sep
    alt = os.path.altsep
    if sep and sep in name:
        return False
    if alt and alt in name:
        return False
    if Path(name).is_absolute() or Path(name).name != name:
        return False
    return bool(_ARTIFACT_BASENAME.fullmatch(name))


def resolve_checkpoint_artifact(artifacts_dir: Path, recorded_name: str) -> Path | None:
    """Return the artifact path only if it is a direct child of ``artifacts_dir``."""
    if not is_safe_artifact_basename(recorded_name):
        return None
    root = artifacts_dir.resolve()
    candidate = (root / recorded_name).resolve()
    if candidate.parent != root:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def make_artifact_checkpoint(
    *,
    task_id: str,
    source_sha256: str,
    job_digest: str,
    executor: str,
    execution_kind: str,
    file: str,
    sha256: str,
    size_bytes: int,
    conformance_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authoritative per-task checkpoint record. Files on disk are not authority."""
    record = {
        "task_id": task_id,
        "source_sha256": source_sha256,
        "job_digest": job_digest,
        "executor": executor,
        "execution_kind": execution_kind,
        "file": file,
        "sha256": sha256,
        "size_bytes": int(size_bytes),
        "conformance_receipt": conformance_receipt,
    }
    return record


def checkpoint_record_from_tool_result(
    job_manifest: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    execution_kind: str,
    *,
    conformance_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outputs = result.get("outputs") or {}
    return make_artifact_checkpoint(
        task_id=task["id"],
        source_sha256=str(job_manifest.get("source_sha256") or ""),
        job_digest=compute_job_digest(job_manifest),
        executor=str(outputs.get("executor") or ""),
        execution_kind=execution_kind,
        file=str(outputs.get("artifact_file") or ""),
        sha256=str(outputs.get("artifact_sha256") or ""),
        size_bytes=int(outputs.get("size_bytes") or 0),
        conformance_receipt=conformance_receipt,
    )


def _not_reusable() -> dict[str, Any]:
    return {
        "completed": False,
        "production_completed": False,
        "resume_kind": None,
        "reused": False,
        "artifact_path": None,
        "sha256": None,
        "size_bytes": None,
        "execution_kind": None,
        "file": None,
        "executor": None,
        "conformance_receipt": None,
    }


def _load_checkpoint(artifacts_dir: Path) -> dict[str, Any]:
    checkpoint_file = artifacts_dir / "fsf_checkpoint.json"
    if not checkpoint_file.is_file():
        return {}
    try:
        payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def inspect_existing_artifacts(
    job_manifest: dict[str, Any],
    artifacts_dir: Path,
    *,
    executor_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Inspect which tasks may be reused. Disk files without a matching checkpoint are pending."""
    artifacts_dir = Path(artifacts_dir)
    status_map: dict[str, dict[str, Any]] = {}
    cached_records = _load_checkpoint(artifacts_dir)
    source_sha = str(job_manifest.get("source_sha256") or "")
    job_digest = compute_job_digest(job_manifest)
    checkpoint_ok = (
        cached_records.get("schema_version") == CHECKPOINT_SCHEMA_VERSION
        and cached_records.get("source_sha256") == source_sha
        and cached_records.get("job_digest") == job_digest
    )
    completed_records = cached_records.get("completed_artifacts") if checkpoint_ok else {}
    if not isinstance(completed_records, dict):
        completed_records = {}

    for task in job_manifest.get("tasks", []):
        task_id = task["id"]
        cached_info = completed_records.get(task_id)
        if not isinstance(cached_info, dict):
            status_map[task_id] = _not_reusable()
            continue

        recorded_name = cached_info.get("file")
        recorded_sha = cached_info.get("sha256")
        recorded_size = cached_info.get("size_bytes")
        recorded_kind = str(cached_info.get("execution_kind") or "")
        recorded_executor = str(cached_info.get("executor") or "")
        if (
            cached_info.get("task_id") not in (None, task_id)
            or not recorded_name
            or not recorded_sha
            or recorded_size is None
            or cached_info.get("source_sha256") != source_sha
            or cached_info.get("job_digest") != job_digest
        ):
            status_map[task_id] = _not_reusable()
            continue
        if executor_id is not None and recorded_executor != executor_id:
            status_map[task_id] = _not_reusable()
            continue

        artifact_path = resolve_checkpoint_artifact(artifacts_dir, str(recorded_name))
        if artifact_path is None or not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
            status_map[task_id] = _not_reusable()
            continue
        current_sha = compute_file_sha256(artifact_path)
        current_size = artifact_path.stat().st_size
        if current_sha != recorded_sha or current_size != int(recorded_size):
            status_map[task_id] = _not_reusable()
            continue

        name = artifact_path.name.casefold()
        is_shape_file = name.endswith(".shape.bin") or name.endswith(".mock.bin")
        production_completed = False
        resume_kind = None
        reusable = False
        if recorded_kind in SHAPE_EXECUTION_KINDS or is_shape_file:
            reusable = True
            production_completed = False
            resume_kind = "shape_generated"
        elif recorded_kind == MEDIA_EXECUTION_KIND and not is_shape_file:
            from fsf.conformance.media import (
                build_expected_contract,
                canonical_contract_digest,
                receipt_authorizes_production,
            )

            required_expected_digest = canonical_contract_digest(
                build_expected_contract(task)
            )
            if receipt_authorizes_production(
                cached_info.get("conformance_receipt"),
                artifact_sha256=current_sha,
                artifact_name=str(recorded_name),
                required_expected_digest=required_expected_digest,
            ) and str(cached_info.get("sha256") or "").casefold() == current_sha.casefold():
                reusable = True
                production_completed = True
                resume_kind = "production_completed"
            else:
                reusable = False

        if not reusable:
            status_map[task_id] = _not_reusable()
            continue

        status_map[task_id] = {
            "completed": True,
            "production_completed": production_completed,
            "resume_kind": resume_kind,
            "reused": True,
            "artifact_path": str(artifact_path),
            "sha256": current_sha,
            "size_bytes": current_size,
            "execution_kind": recorded_kind,
            "file": str(recorded_name),
            "executor": recorded_executor,
            "task_id": task_id,
            "source_sha256": source_sha,
            "job_digest": job_digest,
            "conformance_receipt": cached_info.get("conformance_receipt"),
        }

    return status_map


def build_resumed_job(
    job_manifest: dict[str, Any],
    artifacts_dir: Path,
    *,
    executor_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a resumed job manifest that skips checkpoint-authorized completed tasks.

    Returns:
        (resumed_manifest, pending_tasks, completed_tasks)
    """
    status_map = inspect_existing_artifacts(
        job_manifest, artifacts_dir, executor_id=executor_id
    )

    pending_tasks: list[dict[str, Any]] = []
    completed_tasks: list[dict[str, Any]] = []

    for task in job_manifest.get("tasks", []):
        task_id = task["id"]
        if status_map.get(task_id, {}).get("completed"):
            task_copy = dict(task)
            task_copy["execution_status"] = "reused"
            task_copy["artifact"] = status_map[task_id]
            completed_tasks.append(task_copy)
        else:
            task_copy = dict(task)
            task_copy["execution_status"] = "pending"
            pending_tasks.append(task_copy)

    resumed_manifest = dict(job_manifest)
    resumed_manifest["tasks"] = pending_tasks + completed_tasks
    resumed_manifest["pending_count"] = len(pending_tasks)
    resumed_manifest["completed_count"] = len(completed_tasks)
    resumed_manifest["is_resumed"] = len(completed_tasks) > 0

    return resumed_manifest, pending_tasks, completed_tasks


_CHECKPOINT_RECORD_KEYS = (
    "task_id",
    "source_sha256",
    "job_digest",
    "executor",
    "execution_kind",
    "file",
    "sha256",
    "size_bytes",
    "conformance_receipt",
)


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_checkpoint(
    artifacts_dir: Path,
    job_manifest: dict[str, Any],
    completed_artifacts: dict[str, Any],
) -> None:
    """Save durable checkpoint to disk. Only authoritative records are persisted."""
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = artifacts_dir / "fsf_checkpoint.json"
    source_sha = str(job_manifest.get("source_sha256") or "")
    job_digest = compute_job_digest(job_manifest)
    cleaned: dict[str, Any] = {}
    for task_id, raw in completed_artifacts.items():
        if not isinstance(raw, dict):
            continue
        file_name = raw.get("file") or (
            Path(raw["artifact_path"]).name if raw.get("artifact_path") else None
        )
        if not is_safe_artifact_basename(str(file_name or "")):
            continue
        execution_kind = str(raw.get("execution_kind") or "")
        name_folded = str(file_name).casefold()
        is_shape_file = name_folded.endswith(".shape.bin") or name_folded.endswith(".mock.bin")
        receipt = raw.get("conformance_receipt")
        if is_shape_file:
            if execution_kind not in SHAPE_EXECUTION_KINDS:
                execution_kind = "shape"
            receipt = None
        elif execution_kind == MEDIA_EXECUTION_KIND:
            from fsf.conformance.media import (
                build_expected_contract,
                canonical_contract_digest,
                receipt_authorizes_production,
            )

            current_task = next(
                (
                    item
                    for item in job_manifest.get("tasks", [])
                    if isinstance(item, dict) and item.get("id") == task_id
                ),
                None,
            )
            if current_task is None:
                continue
            if not receipt_authorizes_production(
                receipt if isinstance(receipt, dict) else None,
                artifact_sha256=str(raw.get("sha256") or ""),
                artifact_name=str(file_name),
                required_expected_digest=canonical_contract_digest(
                    build_expected_contract(current_task)
                ),
            ):
                continue
        record = make_artifact_checkpoint(
            task_id=str(raw.get("task_id") or task_id),
            source_sha256=str(raw.get("source_sha256") or source_sha),
            job_digest=str(raw.get("job_digest") or job_digest),
            executor=str(raw.get("executor") or ""),
            execution_kind=execution_kind,
            file=str(file_name or ""),
            sha256=str(raw.get("sha256") or ""),
            size_bytes=int(raw.get("size_bytes") or 0),
            conformance_receipt=receipt if isinstance(receipt, dict) else None,
        )
        if not record["file"] or not record["sha256"] or not record["execution_kind"]:
            continue
        cleaned[str(task_id)] = {key: record[key] for key in _CHECKPOINT_RECORD_KEYS}

    data = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "source_document": job_manifest.get("source_document"),
        "source_sha256": source_sha,
        "job_digest": job_digest,
        "completed_artifacts": cleaned,
    }
    payload = json.dumps(scrub_dict_secrets(data), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(checkpoint_file, payload)
