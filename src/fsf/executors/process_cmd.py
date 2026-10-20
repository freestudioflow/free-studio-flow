"""Process Command Executor for FSF (Real External Tool Adapter).

Executes an external process with an explicit argument vector (args: list[str])
without a shell (shell=False by default). Provides timeout handling, cancellation,
exit code checking, and output artifact validation (existence, non-zero size,
expected suffix, SHA-256).

Note: Output file existence and SHA-256 are necessary but do not prove technical
conformance to media contracts; technical conformance is verified by PDX Media QC.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence

from fsf.conformance.digest import canonical_contract_digest
from fsf.executors.base import BaseVideoExecutor
from fsf.executors.subprocess_safety import run_safe_subprocess, sanitize_process_text


class ProcessCommandExecutor(BaseVideoExecutor):
    """Executes external commands via argument vectors without shell invocation."""

    def __init__(
        self,
        *,
        executor_id: str,
        tool_name: str = "studio.video.generate",
        command_args: Sequence[str],
        display_name: str | None = None,
        timeout_seconds: float = 120.0,
        expected_suffix: str = ".mp4",
        shell: bool = False,
        env: dict[str, str] | None = None,
        working_dir: Path | str | None = None,
    ) -> None:
        self._executor_id = executor_id
        self._tool_name = tool_name
        self._command_args = list(command_args)
        self._display_name = display_name or executor_id
        self._timeout_seconds = float(timeout_seconds)
        self._expected_suffix = expected_suffix
        self._shell = bool(shell)
        self._env = dict(env) if env is not None else None
        self._working_dir = Path(working_dir) if working_dir else None

    @property
    def executor_id(self) -> str:
        return self._executor_id

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def execution_kind(self) -> str:
        return "process"

    @property
    def command_args(self) -> list[str]:
        return list(self._command_args)

    @property
    def config_digest(self) -> str:
        """Deterministic digest of this executor's configuration."""
        resolved_exe = None
        if self._command_args:
            exe = self._command_args[0]
            resolved = shutil.which(exe) or (str(Path(exe).resolve()) if Path(exe).is_file() else None)
            resolved_exe = resolved or exe

        cfg = {
            "executor_id": self._executor_id,
            "tool_name": self._tool_name,
            "command_args": self._command_args,
            "resolved_exe": resolved_exe,
            "expected_suffix": self._expected_suffix,
            "shell": self._shell,
            "working_dir": str(self._working_dir.resolve()) if self._working_dir else None,
            "env": dict(sorted(self._env.items())) if self._env else None,
            "execution_kind": "process",
        }
        return canonical_contract_digest(cfg)

    def is_available(self) -> bool:
        if not self._command_args:
            return False
        if not self._shell:
            exe = self._command_args[0]
            if not shutil.which(exe) and not Path(exe).is_file():
                return False
        return True

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        """Execute external command for a single shot task and return tool result.

        Uses an isolated temporary output location so that pre-existing files in
        the output directory cannot be falsely attributed to this execution run.
        """
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        shot_id = str(task.get("id", "shot_001"))
        shot_spec = task.get("shot_spec") or task.get("asset_spec") or {}
        prompt = str(shot_spec.get("source_prompt") or task.get("prompt", ""))
        duration = float(task.get("duration_seconds", 5.0))
        request_id = str(task.get("request_id") or f"req:{shot_id}")

        artifact_name = task.get("artifact_filename") or f"{shot_id}{self._expected_suffix}"
        out_file = output_dir / artifact_name

        input_video = str(task.get("input_video") or task.get("input_artifact") or "")
        input_artifact = str(task.get("input_artifact") or task.get("input_video") or "")

        # Isolated scratch directory to guarantee zero collision with old artifacts
        scratch_dir = Path(tempfile.mkdtemp(prefix=f"fsf_exec_{shot_id}_"))
        scratch_out_file = scratch_dir / artifact_name

        try:
            replacements = {
                "{shot_id}": shot_id,
                "{prompt}": prompt,
                "{output}": str(scratch_out_file),
                "{input}": input_artifact,
                "{input_video}": input_video,
                "{duration}": f"{duration:.3f}",
                "{duration_int}": str(int(round(duration))),
                "{generation_attempt}": str(task.get("generation_attempt",1)),
            }

            resolved_args: list[str] = []
            for arg in self._command_args:
                val = arg
                for token, replacement in replacements.items():
                    val = val.replace(token, replacement)
                resolved_args.append(val)

            sub_env = os.environ.copy()
            if self._env:
                sub_env.update(self._env)

            from fsf.executors.subprocess_safety import run_safe_subprocess

            res = run_safe_subprocess(
                resolved_args,
                timeout_seconds=self._timeout_seconds,
                cwd=self._working_dir,
                env=sub_env,
                extra_tokens=resolved_args,
            )

            if res.timed_out:
                snippet = res.stderr.strip() or res.stdout.strip()
                lines = [line.strip() for line in snippet.splitlines() if line.strip()]
                clean_snippet = " | ".join(lines[-3:]) if lines else "timed out"
                if len(clean_snippet) > 200:
                    clean_snippet = clean_snippet[:200] + "... [truncated]"
                clean_stderr = f": {clean_snippet}" if clean_snippet else ""
                raise TimeoutError(
                    f"Executor '{self.executor_id}' timed out after {self._timeout_seconds:.1f}s ({res.elapsed_ms}ms){clean_stderr}"
                )

            if res.returncode != 0:
                raw_err = res.stderr.strip() or res.stdout.strip()
                lines = [line.strip() for line in raw_err.splitlines() if line.strip()]
                clean_err = " | ".join(lines[-3:]) if lines else f"exit code {res.returncode}"
                if len(clean_err) > 300:
                    clean_err = clean_err[:300] + "... [truncated]"
                raise RuntimeError(
                    f"Executor '{self.executor_id}' command failed (exit code {res.returncode}, {res.elapsed_ms}ms): {clean_err}"
                )

            if not scratch_out_file.is_file():
                raise FileNotFoundError(
                    f"Executor '{self.executor_id}' reported success but scratch output artifact not found: {scratch_out_file}"
                )

            size_bytes = scratch_out_file.stat().st_size
            if size_bytes == 0:
                raise ValueError(
                    f"Executor '{self.executor_id}' produced 0-byte artifact: {scratch_out_file}"
                )

            if not scratch_out_file.name.lower().endswith(self._expected_suffix.lower()):
                raise ValueError(
                    f"Executor '{self.executor_id}' output suffix mismatch: expected '{self._expected_suffix}', got '{scratch_out_file.name}'"
                )

            artifact_sha = hashlib.sha256(scratch_out_file.read_bytes()).hexdigest()

            # Atomically publish verified output file to target output path
            # Stages to a sibling temporary file in the destination folder, then os.replace
            stage_out_file = out_file.with_name(
                f".{out_file.name}.stage_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            )
            try:
                shutil.copy2(scratch_out_file, stage_out_file)
                os.replace(stage_out_file, out_file)
            finally:
                if stage_out_file.exists():
                    stage_out_file.unlink(missing_ok=True)

            return self.build_tool_result(
                request_id=request_id,
                shot_id=shot_id,
                artifact_path=out_file,
                artifact_sha256=artifact_sha,
                size_bytes=size_bytes,
                duration_seconds=duration,
                state="completed",
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
