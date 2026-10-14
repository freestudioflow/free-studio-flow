"""Executor Registry for Free Studio Flow (FSF-M4).

Maintains available video generation executors and provides unified tool routing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fsf.executors.api import VendorApiVideoExecutor
from fsf.executors.base import BaseVideoExecutor
from fsf.executors.extras import load_extra_executors
from fsf.executors.kaggle import KaggleLtx23Executor
from fsf.executors.local_comfy import LocalComfyExecutor
from fsf.executors.mock_executor import DeterministicMockExecutor
from fsf.studio.skill_executors import implemented_skill_executors


class ExecutorRegistry:
    """Registry of video generation executors."""

    def __init__(self, *, load_extras: bool = True) -> None:
        self._executors: dict[str, BaseVideoExecutor] = {}
        # Register standard default executors
        self.register(KaggleLtx23Executor())
        self.register(LocalComfyExecutor())
        self.register(VendorApiVideoExecutor())
        self.register(DeterministicMockExecutor("mock.studio.video"))
        for skill_executor in implemented_skill_executors():
            self.register(skill_executor)
        self._builtin_ids = set(self._executors)
        if load_extras:
            self.load_extras()

    def register(self, executor: BaseVideoExecutor) -> None:
        """Register an executor instance."""
        self._executors[executor.executor_id] = executor

    def unregister(self, executor_id: str) -> None:
        """Remove an executor if present (tests / extra reload)."""
        self._executors.pop(executor_id, None)

    def load_extras(self, path: Path | None = None) -> list[str]:
        """Register operator-added api.* / local.* executors. Later extra ids replace earlier extras."""
        loaded: list[str] = []
        for executor in load_extra_executors(path):
            if executor.executor_id in self._builtin_ids:
                raise ValueError(
                    f"Extra executor_id collides with builtin '{executor.executor_id}'"
                )
            self.register(executor)
            loaded.append(executor.executor_id)
        return loaded

    def get(self, executor_id: str) -> BaseVideoExecutor:
        """Get an executor by its unique ID."""
        if executor_id not in self._executors:
            raise KeyError(
                f"Unknown executor ID: '{executor_id}'. "
                f"Available: {list(self._executors.keys())}"
            )
        return self._executors[executor_id]

    def list_executors(self) -> list[dict[str, Any]]:
        """List metadata for all registered executors."""
        return [
            {
                "executor_id": exc.executor_id,
                "tool_name": exc.tool_name,
                "display_name": exc.display_name,
                "is_available": exc.is_available(),
                "execution_kind": exc.execution_kind,
            }
            for exc in self._executors.values()
        ]

    def list_for_transport(self, transport: str) -> list[dict[str, Any]]:
        """List executors whose id starts with ``{transport}.``."""
        prefix = f"{transport}."
        return [row for row in self.list_executors() if str(row["executor_id"]).startswith(prefix)]


# Global singleton registry
DEFAULT_REGISTRY = ExecutorRegistry()


def get_executor(executor_id: str = "mock.studio.video") -> BaseVideoExecutor:
    """Get executor from default registry."""
    return DEFAULT_REGISTRY.get(executor_id)
