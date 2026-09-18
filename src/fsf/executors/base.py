"""Base Executor Protocol for FSF Video Generation (FSF-M4).

Defines the abstract interface for all backend executors (Kaggle, ComfyUI, Cloud GPUs, Mock).
Ensures backend switching does NOT mutate source prompt, shot specs, or document provenance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pdx_artifact_core import validate_tool_result

SHAPE_MEDIA_SUFFIXES = {".mp4", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".m4a", ".mov"}


def shape_media_filename(name: str) -> str:
    """Rename media-looking shape payloads so they cannot pass as real files."""
    path = Path(name)
    if path.suffix.lower() in SHAPE_MEDIA_SUFFIXES:
        return f"{path.stem}.shape.bin"
    return path.name


class BaseVideoExecutor(ABC):
    """Abstract interface for video generation executors."""

    @property
    @abstractmethod
    def executor_id(self) -> str:
        """Unique identifier of the executor (e.g. 'kaggle.ltx23.t2v', 'local.comfy.video')."""
        ...

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """Standardized PDX tool name (e.g. 'studio.video.generate' or 'broll.library.ltx23.t2v')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable display name for the executor."""
        return self.executor_id

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this executor's hardware/API requirements are met."""
        ...

    @property
    def execution_kind(self) -> str:
        """How execute_shot behaves: ``mock`` or ``shape``.

        Live GPU / vendor HTTP inference is not wired. Shape writes
        deterministic bytes and must not be treated as media.
        """
        return "shape"

    @abstractmethod
    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        """Execute a single shot generation and return a validated pdx_tool_result_v1."""
        ...

    def build_tool_result(
        self,
        *,
        request_id: str,
        shot_id: str,
        artifact_path: Path,
        artifact_sha256: str,
        size_bytes: int,
        duration_seconds: float,
        state: str = "completed",
    ) -> dict[str, Any]:
        """Construct and validate a standard pdx_tool_result_v1."""
        result = {
            "schema_version": "pdx_tool_result_v1",
            "status": state,
            "outputs": {
                "request_id": request_id,
                "tool": self.tool_name,
                "shot_id": shot_id,
                "artifact_file": str(artifact_path.name),
                "artifact_sha256": artifact_sha256,
                "size_bytes": size_bytes,
                "duration_seconds": duration_seconds,
                "executor": self.executor_id,
            },
            "tool_provider": self.executor_id,
        }
        errors = validate_tool_result(result)
        if errors:
            raise ValueError(f"Invalid pdx_tool_result_v1:\n" + "\n".join(errors))
        return result
