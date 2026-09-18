"""Operator-added API and local executors (plural; no secrets in files).

Built-in catalog still has one default executor per skill+transport. Extra
backends are additional IDs selected with ``fsf flow run --executor``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fsf.studio.skill_executors import StudioSkillExecutor

ALLOWED_TRANSPORTS = frozenset({"api", "local"})
_ID_RE = re.compile(r"^(api|local)\.[a-z0-9][a-z0-9_.-]*$", re.IGNORECASE)
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "kaggle_key",
        "password",
        "secret",
        "token",
        "username",
    }
)

ARTIFACT_BY_SKILL: dict[str, str] = {
    "studio.video.generate": "{shot_id}.mp4",
    "studio.video.remaster": "{shot_id}.remaster.mp4",
    "studio.storyboard.first_frame": "{shot_id}.first_frame.png",
    "studio.audio.post": "{shot_id}.audio.json",
    "studio.vfx.prepare": "{shot_id}.vfx.json",
    "studio.voice.dub": "{shot_id}.dub.json",
    "studio.music.trailer": "{shot_id}.trailer_cue.json",
    "studio.audio.sfx": "{shot_id}.sfx.json",
    "studio.asset.3d": "{shot_id}.asset3d.json",
    "studio.motion.mocap": "{shot_id}.mocap.json",
    "studio.image.design": "{shot_id}.design.png",
    "studio.model.lora": "{shot_id}.lora.json",
    "studio.dataset.prepare": "{shot_id}.dataset.json",
    "studio.model.pdx_specialist": "{shot_id}.pdx_specialist.json",
}

EXTRA_FIELDS_BY_SKILL: dict[str, tuple[str, ...]] = {
    "studio.voice.dub": ("rights_policy",),
    "studio.music.trailer": ("cue_seconds",),
    "studio.model.lora": ("kaggle_plan_ref",),
}


class ExtraTransportExecutor(StudioSkillExecutor):
    """Deterministic extra API/local backend; secrets only via env names."""

    def __init__(
        self,
        *,
        executor_id: str,
        tool_name: str,
        display_name: str,
        transport: str,
        artifact_name: str,
        payload_prefix: str,
        extra_fields: tuple[str, ...] = (),
        key_env: str | None = None,
        require_env: str | None = None,
        endpoint: str | None = None,
        endpoint_env: str | None = None,
    ) -> None:
        super().__init__(
            executor_id=executor_id,
            tool_name=tool_name,
            display_name=display_name,
            artifact_name=artifact_name,
            payload_prefix=payload_prefix,
            availability="always",
            extra_fields=extra_fields,
        )
        self.transport = transport
        self.key_env = key_env
        self.require_env = require_env
        self.endpoint = endpoint
        self.endpoint_env = endpoint_env

    def is_available(self) -> bool:
        if self.transport == "api":
            env_name = self.key_env or "FSF_VENDOR_API_KEY"
            return bool(os.environ.get(env_name))
        if self.require_env:
            return os.environ.get(self.require_env) == "1"
        return True


def extra_executors_path() -> Path | None:
    """Resolve operator extras file: FSF_EXECUTORS_FILE or ./fsf-executors.json."""
    env = os.environ.get("FSF_EXECUTORS_FILE", "").strip()
    if env:
        path = Path(env)
        if not path.is_file():
            raise FileNotFoundError(f"FSF_EXECUTORS_FILE is set but not a file: {path}")
        return path
    cwd = Path.cwd() / "fsf-executors.json"
    if cwd.is_file():
        return cwd
    return None


def executors_from_file(path: Path) -> list[ExtraTransportExecutor]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return executors_from_config(payload, source=str(path))


def executors_from_config(
    payload: Any,
    *,
    source: str = "config",
) -> list[ExtraTransportExecutor]:
    rows = _extract_executor_rows(payload, source=source)
    seen: set[str] = set()
    executors: list[ExtraTransportExecutor] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{source} executor[{index}] must be an object")
        _reject_embedded_secrets(row, source=source, index=index)
        executor = executor_from_row(row, source=source, index=index)
        if executor.executor_id in seen:
            raise ValueError(f"{source} duplicate extra executor_id '{executor.executor_id}'")
        seen.add(executor.executor_id)
        executors.append(executor)
    return executors


def executor_from_row(row: dict[str, Any], *, source: str, index: int) -> ExtraTransportExecutor:
    executor_id = str(row.get("executor_id") or "").strip()
    transport = str(row.get("transport") or "").strip().lower()
    tool_name = str(row.get("tool_name") or "").strip()
    display_name = str(row.get("display_name") or executor_id).strip()
    if not _ID_RE.match(executor_id):
        raise ValueError(
            f"{source} executor[{index}] executor_id must match api.* or local.* (got {executor_id!r})"
        )
    if transport not in ALLOWED_TRANSPORTS:
        raise ValueError(
            f"{source} executor[{index}] transport must be api or local (got {transport!r})"
        )
    prefix = executor_id.split(".", 1)[0].lower()
    if prefix != transport:
        raise ValueError(
            f"{source} executor[{index}] executor_id prefix '{prefix}' does not match transport '{transport}'"
        )
    if tool_name not in ARTIFACT_BY_SKILL:
        known = ", ".join(sorted(ARTIFACT_BY_SKILL))
        raise ValueError(
            f"{source} executor[{index}] unknown tool_name {tool_name!r}. Known: {known}"
        )
    key_env = _optional_env_name(row.get("key_env"), field="key_env", source=source, index=index)
    require_env = _optional_env_name(
        row.get("require_env"), field="require_env", source=source, index=index
    )
    endpoint_env = _optional_env_name(
        row.get("endpoint_env"), field="endpoint_env", source=source, index=index
    )
    endpoint = row.get("endpoint")
    if endpoint is not None:
        endpoint = str(endpoint).strip() or None
    slug = executor_id.replace(".", "_").upper()
    return ExtraTransportExecutor(
        executor_id=executor_id,
        tool_name=tool_name,
        display_name=display_name,
        transport=transport,
        artifact_name=ARTIFACT_BY_SKILL[tool_name],
        payload_prefix=f"EXTRA_{slug}",
        extra_fields=EXTRA_FIELDS_BY_SKILL.get(tool_name, ()),
        key_env=key_env,
        require_env=require_env,
        endpoint=endpoint,
        endpoint_env=endpoint_env,
    )


def load_extra_executors(path: Path | None = None) -> list[ExtraTransportExecutor]:
    """Load extras from an explicit path and/or process env."""
    rows: list[ExtraTransportExecutor] = []
    seen: set[str] = set()
    file_path = path if path is not None else extra_executors_path()
    if file_path is not None:
        for executor in executors_from_file(file_path):
            rows.append(executor)
            seen.add(executor.executor_id)
    raw = os.environ.get("FSF_EXTRA_EXECUTORS", "").strip()
    if raw:
        payload = json.loads(raw)
        for executor in executors_from_config(payload, source="FSF_EXTRA_EXECUTORS"):
            if executor.executor_id in seen:
                rows = [item for item in rows if item.executor_id != executor.executor_id]
            rows.append(executor)
            seen.add(executor.executor_id)
    return rows


def _extract_executor_rows(payload: Any, *, source: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError(f"{source} must be a JSON object or array")
    schema = payload.get("schema_version")
    if schema not in (None, "fsf_executors_v1"):
        raise ValueError(f"{source} unsupported schema_version {schema!r}")
    rows = payload.get("executors")
    if not isinstance(rows, list):
        raise ValueError(f"{source} must contain an executors array")
    return rows


def _reject_embedded_secrets(row: dict[str, Any], *, source: str, index: int) -> None:
    for key in row:
        lowered = str(key).lower().replace("-", "_")
        if lowered in _SECRET_KEYS:
            raise ValueError(
                f"{source} executor[{index}] must not embed '{key}'; use an env name (key_env / require_env)"
            )


def _optional_env_name(value: Any, *, field: str, source: str, index: int) -> str | None:
    if value is None or value == "":
        return None
    name = str(value).strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError(
            f"{source} executor[{index}] {field} must be an env var name (got {name!r})"
        )
    return name
