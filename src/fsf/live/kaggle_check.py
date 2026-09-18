"""Live Kaggle work verification: auth ping + private job envelope.

Does not submit a GPU kernel unless FSF_KAGGLE_SUBMIT=1.
Never writes tokens into the job payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from fsf.executors.kaggle import KaggleLtx23Executor
from fsf.intake.compiler import compile_job
from fsf.live.credentials import apply_kaggle_config_env, inspect_kaggle_credentials
from fsf.runtime.external_op import create_fsf_external_op, scrub_secrets
from fsf.studio.catalog import executor_id_for
from fsf.studio.runner import run_studio_flow


def kaggle_bin() -> str | None:
    scripts = Path(sys.executable).resolve().parent
    for name in ("kaggle.exe", "kaggle"):
        candidate = scripts / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("kaggle")


def ping_kaggle_api(*, timeout_seconds: int = 60) -> dict[str, Any]:
    """Call Kaggle: list this account's kernels (page size 1)."""
    binary = kaggle_bin()
    if not binary:
        return {"ok": False, "error": "kaggle CLI is not installed (pip install kaggle)"}
    try:
        completed = subprocess.run(
            [binary, "kernels", "list", "--mine", "--page-size", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=apply_kaggle_config_env(),
        )
    except FileNotFoundError:
        return {"ok": False, "error": "kaggle CLI is not installed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "kaggle API timed out"}

    text = (completed.stdout or "") + (completed.stderr or "")
    lowered = text.lower()
    if completed.returncode == 0:
        return {"ok": True, "error": None, "listed": True}
    if "401" in text or "unauthorized" in lowered or "invalid" in lowered:
        return {"ok": False, "error": "Kaggle API rejected credentials (unauthorized)"}
    if "failed to resolve" in lowered or "name or service not known" in lowered or "getaddrinfo" in lowered:
        return {"ok": False, "error": "kaggle API unreachable (network)"}
    return {
        "ok": False,
        "error": f"kaggle kernels list --mine failed (exit {completed.returncode})",
    }


def build_private_kaggle_job(docx_path: Path, out_dir: Path) -> dict[str, Any]:
    """Intake DOCX and emit a private Kaggle job + external operation (no push)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest, request, profile = compile_job(docx_path, out_dir=out_dir)
    executor = KaggleLtx23Executor()
    job_file = executor.build_notebook_job(manifest["tasks"], out_dir)
    job_data = json.loads(job_file.read_text(encoding="utf-8"))

    op = create_fsf_external_op(
        operation_id=f"op:fsf:live:{manifest['source_sha256'][:12]}",
        provider="kaggle",
        request_id=request["request_id"],
        inputs={
            "source_document": manifest["source_document"],
            "source_sha256": manifest["source_sha256"],
            "is_private": True,
            "tool": "studio.video.generate",
            "executor_id": executor_id_for("studio.video.generate", "kaggle"),
        },
    )

    raw = json.dumps({"job": job_data, "request": request, "operation": op})
    scrubbed = scrub_secrets(raw)
    forbidden = ("HF_", "ghp_", "sk-", "KAGGLE_KEY")
    leaked = [token for token in forbidden if token.lower() in scrubbed.lower()]

    return {
        "ok": job_data.get("is_private") is True and not leaked,
        "job_file": str(job_file),
        "is_private": job_data.get("is_private") is True,
        "task_count": manifest["task_count"],
        "source_sha256": manifest["source_sha256"],
        "profile_schema": profile.get("schema_version"),
        "operation_id": op.get("operation_id"),
        "executor_id": executor.executor_id,
        "secrets_leaked": leaked,
    }


def verify_kaggle_work(docx_path: Path, out_dir: Path) -> dict[str, Any]:
    """Live Kaggle verification: credentials, API ping, private job envelope.

    GPU kernel push stays opt-in via FSF_KAGGLE_SUBMIT=1 (not done here).
    """
    creds = inspect_kaggle_credentials()
    if not creds.get("ok"):
        return {
            "ok": False,
            "skipped_local": True,
            "credentials": creds,
            "api": {"ok": False, "error": "no credentials"},
            "job": {"ok": False},
        }

    api = ping_kaggle_api()
    job = build_private_kaggle_job(docx_path, out_dir)
    shape_ok = (
        job.get("is_private") is True
        and not job.get("secrets_leaked")
        and job.get("task_count", 0) >= 1
    )

    # Exercise the kaggle generate transport against the same intake (deterministic
    # artifact). Live GPU render is not required for this gate.
    tasks = json.loads((Path(out_dir) / "job_manifest.json").read_text(encoding="utf-8"))["tasks"]
    results = run_studio_flow(
        "studio.video.generate",
        tasks[:1],
        Path(out_dir) / "kaggle_shape",
        transport="kaggle",
    )

    ok = bool(creds.get("ok") and api.get("ok") and shape_ok and results)
    return {
        "ok": ok,
        "skipped_local": True,
        "submit_gpu": os.environ.get("FSF_KAGGLE_SUBMIT") == "1",
        "credentials": {
            "ok": creds.get("ok"),
            "username": creds.get("username"),
            "config_dir": creds.get("config_dir"),
            "has_key": creds.get("has_key"),
        },
        "api": api,
        "job": job,
        "generate_shape": {
            "ok": bool(results),
            "status": results[0].get("status") if results else None,
            "executor": results[0].get("outputs", {}).get("executor") if results else None,
        },
    }
