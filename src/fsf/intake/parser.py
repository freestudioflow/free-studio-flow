"""Profile-based prompt parsing and normalization helpers."""

from __future__ import annotations

import re
from typing import Any

DURATION_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:seconds?|s)\.?\s*$", re.I)
# Colon required on body text so English "Shot with ..." is not a unit label.
_EXPLICIT_LABEL = re.compile(r"^(SHOT|ASSET)\s*:\s*", re.I)
# Heading style may omit the colon: "ASSET hero mesh".
_HEADING_LABEL = re.compile(r"^(SHOT|ASSET)\b\s*:?\s*", re.I)
SHARED_UNIT_SKILLS = frozenset({"studio.asset.3d", "studio.motion.mocap"})


def extract_duration_seconds(text: str, default: float = 5.0) -> float:
    """Extract duration in seconds from the trailing portion of a prompt string."""
    match = DURATION_PATTERN.search(text)
    return float(match.group(1)) if match else default


def normalize_prompt_text(text: str) -> str:
    """Strip trailing duration phrases and redundant whitespace from prompt text."""
    cleaned = DURATION_PATTERN.sub("", text)
    return cleaned.strip()


def is_separator_paragraph(text: str) -> bool:
    """Check if a paragraph text acts as a shot/task separator (e.g. '-----')."""
    stripped = text.strip()
    return stripped == "-----" or (len(stripped) >= 3 and set(stripped) == {"-"})


def is_heading_style(style: str) -> bool:
    """True when Kernel profile style is a Word heading."""
    return str(style or "").casefold().startswith("heading")


def classify_unit(text: str, *, style: str = "") -> tuple[str, str, str]:
    """Classify a block from operator-explicit labels only.

    Returns ``(unit_kind, remainder_text, classification)``.
    Body text requires ``SHOT:`` / ``ASSET:``. Heading style may omit the colon.
    Unlabeled blocks default to shot. FSF does not infer mesh vs clip from prose.
    """
    raw = str(text or "")
    stripped = raw.lstrip()
    pattern = _HEADING_LABEL if is_heading_style(style) else _EXPLICIT_LABEL
    match = pattern.match(stripped)
    if not match:
        return "shot", raw, "default_shot"
    kind = match.group(1).casefold()
    remainder = stripped[match.end() :].strip()
    return kind, remainder, "explicit_label"


def is_shot_unit(task: dict[str, Any]) -> bool:
    """True for shot units; missing ``unit_kind`` is treated as shot."""
    return str(task.get("unit_kind") or "shot") == "shot"


def skill_applies_to_task(skill: str, task: dict[str, Any]) -> bool:
    """3D / mocap apply to every unit; other Studio skills apply to shots only."""
    if skill in SHARED_UNIT_SKILLS:
        return True
    return is_shot_unit(task)
