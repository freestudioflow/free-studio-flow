"""Deterministic shot specifications and task contracts for FSF."""

from __future__ import annotations

from typing import Any


def baseline_shot_spec(
    task: dict[str, Any],
    *,
    source_name: str,
    source_sha256: str,
    source_index: int,
) -> dict[str, Any]:
    """Create a baseline fsf_shot_spec_v1 anchored to Kernel document provenance."""
    prompt = str(task["prompt"]).strip()
    unresolved = ["subjects", "setting", "action_beats", "camera"]
    if task["mode"] == "image_to_video":
        unresolved.append("reference_image_semantics")

    return {
        "schema_version": "fsf_shot_spec_v1",
        "shot_id": task["id"],
        "source_prompt": prompt,
        "generation_prompt": prompt,
        "mode": task["mode"],
        "duration_seconds": float(task["duration_seconds"]),
        "reference_image": task.get("image"),
        "semantics": {
            "subjects": [],
            "setting": None,
            "action_beats": [],
            "camera": {"shot_scale": None, "movement": None, "composition": None},
            "audio": {"dialogue": [], "ambience": [], "music": None},
        },
        "constraints": {
            "preserve": ["source_prompt_verbatim"],
            "forbid": ["unrequested_visible_text", "planner_instructions_in_output"],
            "unresolved": unresolved,
        },
        "provenance": {
            "source_kind": "docx",
            "source_document": source_name,
            "source_sha256": source_sha256,
            "source_index": source_index,
            "planner": "none:deterministic-baseline",
        },
        "needs_semantic_review": True,
    }


def pdx_tool_request(manifest: dict[str, Any], *, tool: str = "broll.library.ltx23.t2v") -> dict[str, Any]:
    """Create a standard pdx_tool_request_v1 from an FSF job manifest."""
    return {
        "schema_version": "pdx_tool_request_v1",
        "tool": tool,
        "request_id": f"fsf:{manifest['source_document']}:{manifest['source_sha256'][:12]}",
        "inputs": {
            "job_schema_version": manifest["schema_version"],
            "source_document": manifest["source_document"],
            "source_sha256": manifest["source_sha256"],
            "shots": [task["shot_spec"] for task in manifest["tasks"]],
            "packages": {
                "prodocux": "0.3.0rc7",
                "pdx_artifact_engine": "0.3.0a8",
                "pdx_adapter_media": "0.2.0a3",
            },
        },
        "policies": {"max_retries": 1, "timeout_seconds": 43200},
    }
