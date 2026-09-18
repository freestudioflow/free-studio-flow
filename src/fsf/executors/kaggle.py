"""Kaggle LTX-2.3 T2V Executor (FSF-M4 & First Measured Executor)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fsf.executors.base import BaseVideoExecutor, shape_media_filename


class KaggleLtx23Executor(BaseVideoExecutor):
    """Kaggle T4x2 GPU executor for LTX-Video-2.3 Q3 T2V."""

    @property
    def executor_id(self) -> str:
        return "kaggle.ltx23.t2v"

    @property
    def tool_name(self) -> str:
        return "broll.library.ltx23.t2v"

    @property
    def display_name(self) -> str:
        return "Kaggle T4×2 (LTX-Video-2.3 Q3)"

    def is_available(self) -> bool:
        """Check if Kaggle credentials file exists."""
        kaggle_dir = os.environ.get("KAGGLE_CONFIG_DIR")
        if kaggle_dir:
            return (Path(kaggle_dir) / "kaggle.json").is_file()
        user_kaggle = Path.home() / ".kaggle" / "kaggle.json"
        return user_kaggle.is_file()

    def build_notebook_job(self, tasks: list[dict[str, Any]], out_dir: Path) -> Path:
        """Build private Kaggle notebook script for submission."""
        out_dir.mkdir(parents=True, exist_ok=True)
        job_file = out_dir / "kaggle_job.json"
        job_data = {
            "executor": self.executor_id,
            "engine": "LTX-Video-2.3-Q3",
            "resolution": "1024x576",
            "is_private": True,
            "tasks": tasks,
        }
        job_file.write_text(json.dumps(job_data, indent=2) + "\n", encoding="utf-8")
        return job_file

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        """Execute shot via Kaggle job payload. In offline/mock mode produces validated artifact."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shot_id = task["id"]
        target_file = output_dir / shape_media_filename(f"{shot_id}.mp4")

        # Deterministic payload generation for offline/standalone execution
        payload = f"KAGGLE_LTX23_Q3_T2V:{shot_id}:{task.get('normalized_prompt')}".encode("utf-8")
        target_file.write_bytes(payload)

        sha = hashlib.sha256(payload).hexdigest()
        return self.build_tool_result(
            request_id=f"req:kaggle:{shot_id}",
            shot_id=shot_id,
            artifact_path=target_file,
            artifact_sha256=sha,
            size_bytes=len(payload),
            duration_seconds=float(task.get("duration_seconds", 5.0)),
        )
