"""Deterministic Mock & Test Video Executor for FSF."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fsf.executors.base import BaseVideoExecutor


class DeterministicMockExecutor(BaseVideoExecutor):
    """Deterministic in-memory/disk Mock Video Executor for testing and verification."""

    def __init__(self, executor_id: str = "mock.studio.video"):
        self._id = executor_id

    @property
    def executor_id(self) -> str:
        return self._id

    @property
    def tool_name(self) -> str:
        return "studio.video.generate"

    @property
    def display_name(self) -> str:
        return f"Mock Video Executor ({self._id})"

    def is_available(self) -> bool:
        return True

    @property
    def execution_kind(self) -> str:
        return "mock"

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shot_id = task["id"]
        target_file = output_dir / f"{shot_id}.mock.bin"

        # Deterministic dummy video payload
        content = f"DETERMINISTIC_VIDEO_BYTES:{shot_id}:{task.get('normalized_prompt')}".encode("utf-8")
        target_file.write_bytes(content)

        sha = hashlib.sha256(content).hexdigest()
        return self.build_tool_result(
            request_id=f"req:mock:{shot_id}",
            shot_id=shot_id,
            artifact_path=target_file,
            artifact_sha256=sha,
            size_bytes=len(content),
            duration_seconds=float(task.get("duration_seconds", 5.0)),
        )
