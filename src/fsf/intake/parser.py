"""Profile-based prompt parsing and normalization helpers."""

from __future__ import annotations

import re
from typing import Any

DURATION_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:seconds?|s)\.?\s*$", re.I)


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
