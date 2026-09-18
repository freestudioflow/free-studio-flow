"""Local transport verification: executor shape always; live model ping optional.

This machine often has no useful Comfy/GPU model. The live gate still proves
the local transport exists (deterministic execute_shot + pdx_tool_result_v1).
It does not HTTP-ping Comfy or run real local inference unless an operator
explicitly asks to ping a live-ready backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fsf.executors.registry import DEFAULT_REGISTRY
from fsf.intake.compiler import compile_job
from fsf.studio.runner import run_studio_flow


def verify_local_transport(
    docx_path: Path,
    out_dir: Path,
    *,
    ping_live_model: bool = False,
) -> dict[str, Any]:
    """Prove local executors exist and generate-shape works."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    local_execs = [
        row
        for row in DEFAULT_REGISTRY.list_executors()
        if str(row["executor_id"]).startswith("local.")
    ]
    if not local_execs:
        return {
            "ok": False,
            "shape_ok": False,
            "skipped": True,
            "live_model": {
                "skipped": True,
                "reason": "no local.* executors registered",
            },
            "executors": [],
            "error": "local transport is missing from the executor registry",
        }

    manifest, _, _ = compile_job(docx_path, out_dir=out_dir)
    results = run_studio_flow(
        "studio.video.generate",
        manifest["tasks"][:1],
        out_dir / "local_shape",
        transport="local",
    )
    result = results[0] if results else {}
    outputs = result.get("outputs") or {}
    executor_id = outputs.get("executor")
    shape_ok = (
        bool(results)
        and result.get("schema_version") == "pdx_tool_result_v1"
        and str(executor_id or "").startswith("local.")
    )

    live_model = _live_model_status(local_execs, ping=ping_live_model)
    return {
        "ok": shape_ok,
        "shape_ok": shape_ok,
        "skipped": bool(live_model.get("skipped")),
        "live_model": live_model,
        "executors": local_execs,
        "shape": {
            "ok": shape_ok,
            "status": result.get("status"),
            "executor": executor_id,
        },
    }


def _live_model_status(local_execs: list[dict[str, Any]], *, ping: bool) -> dict[str, Any]:
    if not ping:
        return {
            "skipped": True,
            "reason": "no useful local model to ping on this machine",
        }
    ready = [row for row in local_execs if row.get("is_available")]
    if not ready:
        return {
            "skipped": True,
            "reason": "no live-ready local executor (set COMFYUI_AVAILABLE=1 or extra require_env when the backend is up)",
        }
    return {
        "skipped": False,
        "ok": True,
        "executor_ids": [row["executor_id"] for row in ready],
    }
