"""FSF-M1 Compiler: Single Document Source of Truth Compiler.

Compiles Kernel `prodocux_docx_profile_v1` into `fsf_video_job_v1` and `fsf_shot_spec_v1`.
Eliminates duplicate DOCX parsing in FSF by consuming ONLY the Kernel's normalized structure.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pdx_artifact_core import validate_tool_request
from prodocux_kernel.intake.docx import profile_docx, profile_docx_bytes

from fsf.core.shot_spec import baseline_shot_spec, pdx_tool_request
from fsf.intake.parser import (
    extract_duration_seconds,
    is_separator_paragraph,
    normalize_prompt_text,
)


def compile_tasks_from_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile structured tasks directly from Kernel's prodocux_docx_profile_v1."""
    if profile.get("schema_version") != "prodocux_docx_profile_v1":
        raise ValueError(
            f"Unsupported profile schema: {profile.get('schema_version')}. "
            "Expected 'prodocux_docx_profile_v1'."
        )

    source_name = profile.get("source", {}).get("name", "unknown.docx")
    source_sha256 = profile.get("source", {}).get("sha256", "")
    paragraphs = profile.get("paragraphs", [])

    tasks: list[dict[str, Any]] = []
    pending_texts: list[str] = []
    first_paragraph_index: int | None = None

    def flush_task() -> None:
        nonlocal pending_texts, first_paragraph_index
        if not pending_texts:
            return
        combined_text = " ".join(pending_texts).strip()
        if combined_text:
            task_id = f"task_{len(tasks) + 1:03d}"
            duration = extract_duration_seconds(combined_text)
            normalized = normalize_prompt_text(combined_text)
            task_dict: dict[str, Any] = {
                "id": task_id,
                "mode": "text_to_video",
                "prompt": combined_text,
                "normalized_prompt": normalized,
                "duration_seconds": duration,
                "image": None,
                "source_paragraph_index": first_paragraph_index,
            }
            task_dict["shot_spec"] = baseline_shot_spec(
                task_dict,
                source_name=source_name,
                source_sha256=source_sha256,
                source_index=len(tasks) + 1,
            )
            tasks.append(task_dict)
        pending_texts = []
        first_paragraph_index = None

    for p in paragraphs:
        text = str(p.get("text", "")).strip()
        if not text:
            continue

        if is_separator_paragraph(text):
            flush_task()
            continue

        if first_paragraph_index is None:
            first_paragraph_index = p.get("index", 1)
        pending_texts.append(text)

    flush_task()
    return tasks


def compile_job_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Compile a full fsf_video_job_v1 manifest from a Kernel docx profile."""
    tasks = compile_tasks_from_profile(profile)
    source_info = profile.get("source", {})

    return {
        "version": 2,
        "schema_version": "fsf_video_job_v1",
        "source_document": source_info.get("name", "unknown.docx"),
        "source_sha256": source_info.get("sha256", ""),
        "task_count": len(tasks),
        "t2v_supported": True,
        "i2v_supported_on_t4": False,
        "tasks": tasks,
    }


def compile_job(
    docx_path: str | Path,
    *,
    out_dir: str | Path | None = None,
    tool_name: str = "broll.library.ltx23.t2v",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Intake a Word DOCX via Kernel profile and compile FSF job artifacts.

    Returns:
        tuple of (manifest, pdx_request, kernel_profile)
    """
    path = Path(docx_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"DOCX file not found: {path}")

    # Single source of truth: Kernel docx intake
    profile = profile_docx(path)
    manifest = compile_job_from_profile(profile)
    request = pdx_tool_request(manifest, tool=tool_name)

    # Validate against PDX Artifact Core contracts
    errors = validate_tool_request(request)
    if errors:
        raise ValueError(f"Invalid pdx_tool_request_v1:\n" + "\n".join(errors))

    if out_dir is not None:
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)

        (out / "job_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (out / "pdx_tool_request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (out / "prodocux_docx_profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(path, out / f"source_{path.name}")

    return manifest, request, profile
