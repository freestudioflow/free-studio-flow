"""Explicit DOCX metadata mapped to the existing consistency contract."""

from __future__ import annotations

import re
from typing import Any

from fsf.core.consistency import build_consistency_context, canonical_asset_digest

_TAGS = frozenset({
    "ID", "ASSET_REFS", "CONTINUITY", "CONTINUITY_ASPECTS", "REVIEW_REQUIRED",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def split_metadata(texts: list[str]) -> tuple[list[str], dict[str, str]]:
    """Recognize whole metadata paragraphs, rejecting duplicate or empty tags."""
    prompt: list[str] = []
    metadata: dict[str, str] = {}
    for paragraph in texts:
        lines = paragraph.splitlines()
        if not any(line.partition(":")[1] and line.partition(":")[0].strip().upper() in _TAGS for line in lines):
            prompt.append(paragraph)
            continue
        # A DOCX paragraph may contain explicit line breaks.
        for line in lines:
            key, colon, value = line.partition(":")
            key = key.strip().upper()
            if not colon or key not in _TAGS:
                prompt.append(line)
                continue
            if key in metadata or not value.strip():
                raise ValueError(f"Duplicate or empty DOCX metadata: {key}")
            metadata[key] = value.strip()
    return prompt, metadata


def task_identifier(metadata: dict[str, str], default: str) -> str:
    identifier = metadata.get("ID", default)
    if not _ID.fullmatch(identifier):
        raise ValueError(f"Invalid DOCX task ID: {identifier!r}")
    return identifier


def _csv(value: str, label: str) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items) or len(set(items)) != len(items):
        raise ValueError(f"{label} must contain unique, non-empty comma-separated values")
    return items


def attach_contexts(tasks: list[dict[str, Any]], declarations: dict[str, dict[str, str]]) -> None:
    """Resolve explicit declarations after all task identities are known."""
    assets = {t["id"]: t["asset_spec"] for t in tasks if "asset_spec" in t}
    shots = [t["id"] for t in tasks if "shot_spec" in t]
    for task in tasks:
        metadata = declarations[task["id"]]
        consistency = set(metadata) - {"ID"}
        if not consistency:
            continue
        if "shot_spec" not in task:
            raise ValueError("Consistency tags are only supported on SHOT blocks")
        if "CONTINUITY_ASPECTS" in metadata and "CONTINUITY" not in metadata:
            raise ValueError("CONTINUITY_ASPECTS requires CONTINUITY")
        review = metadata.get("REVIEW_REQUIRED", "true").lower()
        if review not in {"true", "false"}:
            raise ValueError("REVIEW_REQUIRED must be true or false")
        refs = []
        injections = []
        for aid in _csv(metadata["ASSET_REFS"], "ASSET_REFS") if "ASSET_REFS" in metadata else []:
            if aid not in assets:
                raise ValueError(f"Unknown DOCX asset reference: {aid}")
            spec = assets[aid]
            refs.append({
                "asset_id": aid, "version": "1",
                "content_hash": canonical_asset_digest(
                    aid, "1", spec["source_prompt"], spec["kind"], spec["constraints"], [],
                ),
            })
            injections.append({"asset_id": aid, "injected_text": spec["source_prompt"]})
        continuity = None
        if "CONTINUITY" in metadata:
            continuity = {
                "shot_id": metadata["CONTINUITY"],
                "selection_rule": "latest_completed",
                "aspects": _csv(metadata.get("CONTINUITY_ASPECTS", "appearance"), "CONTINUITY_ASPECTS"),
                "resolved_sha256": None,
            }
        task["consistency_context"] = build_consistency_context(
            asset_refs=refs, continues_from=continuity,
            generation_intent={
                "base_prompt": task["shot_spec"]["generation_prompt"],
                "injection_rule_version": "docx-explicit-assets-v1",
                "asset_injections": injections,
            },
            review_required=review == "true", shot_id=task["id"],
            shot_spec=task["shot_spec"], known_asset_ids=set(assets),
            known_shot_ids=set(shots), shot_order=shots,
        )
