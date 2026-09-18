"""FSF External Operation & Durable Resume Runtime (FSF-M2).

Manages external operations (e.g. Kaggle / Remote GPU workers),
implements checkpointing, secret scrubbing, and artifact reuse for partial resumes.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def inspect_existing_artifacts(
    job_manifest: dict[str, Any],
    artifacts_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Inspect output directory to check which tasks already have valid completed artifacts.

    Returns a mapping of task_id -> artifact_status info.
    """
    artifacts_dir = Path(artifacts_dir)
    status_map: dict[str, dict[str, Any]] = {}

    checkpoint_file = artifacts_dir / "fsf_checkpoint.json"
    cached_records: dict[str, Any] = {}
    if checkpoint_file.is_file():
        try:
            cached_records = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except Exception:
            cached_records = {}

    for task in job_manifest.get("tasks", []):
        task_id = task["id"]
        expected_filename = f"{task_id}.mp4"
        candidates = [
            artifacts_dir / f"{task_id}.mock.bin",
            artifacts_dir / f"{task_id}.shape.bin",
            artifacts_dir / expected_filename,
        ]
        artifact_path = next((path for path in candidates if path.is_file()), None)

        if artifact_path is not None and artifact_path.stat().st_size > 0:
            current_sha = compute_file_sha256(artifact_path)
            cached_info = cached_records.get("completed_artifacts", {}).get(task_id, {})
            # If checkpoint has a recorded hash, verify match
            if cached_info and cached_info.get("sha256") == current_sha:
                status_map[task_id] = {
                    "completed": True,
                    "reused": True,
                    "artifact_path": str(artifact_path),
                    "sha256": current_sha,
                    "size_bytes": artifact_path.stat().st_size,
                }
                continue
            elif not cached_info:
                # File exists and is non-empty
                status_map[task_id] = {
                    "completed": True,
                    "reused": True,
                    "artifact_path": str(artifact_path),
                    "sha256": current_sha,
                    "size_bytes": artifact_path.stat().st_size,
                }
                continue

        status_map[task_id] = {
            "completed": False,
            "reused": False,
            "artifact_path": None,
            "sha256": None,
        }

    return status_map


def build_resumed_job(
    job_manifest: dict[str, Any],
    artifacts_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a resumed job manifest that skips completed tasks and isolates pending tasks.

    Returns:
        (resumed_manifest, pending_tasks, completed_tasks)
    """
    status_map = inspect_existing_artifacts(job_manifest, artifacts_dir)

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


def save_checkpoint(
    artifacts_dir: Path,
    job_manifest: dict[str, Any],
    completed_artifacts: dict[str, Any],
) -> None:
    """Save durable checkpoint to disk."""
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = artifacts_dir / "fsf_checkpoint.json"

    data = {
        "schema_version": "fsf_checkpoint_v1",
        "source_document": job_manifest.get("source_document"),
        "source_sha256": job_manifest.get("source_sha256"),
        "completed_artifacts": completed_artifacts,
    }
    raw = json.dumps(scrub_dict_secrets(data), indent=2, ensure_ascii=False) + "\n"
    checkpoint_file.write_text(raw, encoding="utf-8")
