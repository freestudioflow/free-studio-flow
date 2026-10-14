"""Vendor / cloud API video executor (FSF-S transport: api)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from fsf.executors.base import BaseVideoExecutor, shape_media_filename


class VendorApiVideoExecutor(BaseVideoExecutor):
    """Original-vendor / cloud API shape for studio.video.generate.

    Offline execution writes a deterministic payload so tests can prove the
    transport without calling a live vendor. Secrets stay in env; never in
    manifests.
    """

    @property
    def executor_id(self) -> str:
        return "api.vendor.video"

    @property
    def tool_name(self) -> str:
        return "studio.video.generate"

    @property
    def display_name(self) -> str:
        return "Vendor / cloud API (video generate)"

    def is_available(self) -> bool:
        return bool(os.environ.get("FSF_VENDOR_API_KEY"))

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shot_id = task["id"]
        target_file = output_dir / shape_media_filename(f"{shot_id}.mp4")
        payload = f"VENDOR_API_T2V:{shot_id}:{task.get('normalized_prompt')}".encode("utf-8")
        target_file.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        return self.build_tool_result(
            request_id=f"req:api:{shot_id}",
            shot_id=shot_id,
            artifact_path=target_file,
            artifact_sha256=sha,
            size_bytes=len(payload),
            duration_seconds=float(task.get("duration_seconds", 5.0)),
        )
