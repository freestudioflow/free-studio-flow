"""Local ComfyUI / Dedicated GPU Video Executor (FSF-M4 Second Executor Shape)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from fsf.executors.base import BaseVideoExecutor, shape_media_filename


class LocalComfyExecutor(BaseVideoExecutor):
    """Local ComfyUI / Dedicated GPU executor for Studio Video Generation."""

    def __init__(self, api_endpoint: str = "http://127.0.0.1:8188"):
        self.api_endpoint = api_endpoint

    @property
    def executor_id(self) -> str:
        return "local.comfy.video"

    @property
    def tool_name(self) -> str:
        return "studio.video.generate"

    @property
    def display_name(self) -> str:
        return f"Local ComfyUI ({self.api_endpoint})"

    def is_available(self) -> bool:
        """Check if local comfy is configured or available."""
        return bool(os.environ.get("COMFYUI_AVAILABLE") == "1")

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shot_id = task["id"]
        target_file = output_dir / shape_media_filename(f"{shot_id}.mp4")

        # Deterministic simulation payload
        payload = f"COMFYUI_LOCAL_GPU:{shot_id}:{task.get('normalized_prompt')}".encode("utf-8")
        target_file.write_bytes(payload)

        sha = hashlib.sha256(payload).hexdigest()
        return self.build_tool_result(
            request_id=f"req:comfy:{shot_id}",
            shot_id=shot_id,
            artifact_path=target_file,
            artifact_sha256=sha,
            size_bytes=len(payload),
            duration_seconds=float(task.get("duration_seconds", 5.0)),
        )
