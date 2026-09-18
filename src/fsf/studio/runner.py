"""Run a Studio skill against an FSF job without mutating shot provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fsf.executors.registry import get_executor
from fsf.studio.catalog import Transport, executor_id_for, get_studio_flow


def run_studio_flow(
    skill: str,
    tasks: list[dict[str, Any]],
    output_dir: Path,
    *,
    transport: Transport = "local",
    executor_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute one Studio skill on each task.

    Does not rewrite task['prompt'], shot_spec['source_prompt'], or provenance SHA.
    """
    flow = get_studio_flow(skill)
    resolved = executor_id or executor_id_for(flow["skill"], transport)
    executor = get_executor(resolved)
    allowed_tools = {flow["skill"]}
    if flow["skill"] == "studio.video.generate":
        allowed_tools.add("broll.library.ltx23.t2v")
    if executor.tool_name not in allowed_tools:
        raise ValueError(
            f"Executor '{resolved}' tool '{executor.tool_name}' does not match skill '{flow['skill']}'."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for task in tasks:
        source_prompt = task.get("shot_spec", {}).get("source_prompt")
        source_sha = task.get("shot_spec", {}).get("provenance", {}).get("source_sha256")
        result = executor.execute_shot(task, output_dir)
        if task.get("shot_spec"):
            if task["shot_spec"].get("source_prompt") != source_prompt:
                raise RuntimeError("Executor mutated source_prompt")
            if task["shot_spec"].get("provenance", {}).get("source_sha256") != source_sha:
                raise RuntimeError("Executor mutated source SHA")
        results.append(result)
    return results
