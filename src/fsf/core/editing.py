"""Versioned edit identities and metadata patches, without media execution.

Source declarations are not proof of file integrity or review approval. Those
must be verified by the production/post-production boundary before rendering.
Frame values use the edit clock, including source in/out positions.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from fsf.conformance.digest import canonical_contract_digest


class EditValidationError(ValueError):
    """Invalid, ambiguous or outdated edit metadata."""


def _shape(value: Any, name: str) -> None:
    schema = json.loads(resources.files("fsf.schemas").joinpath(name + ".schema.json").read_text("utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        # Never echo arbitrary input values in diagnostics.
        raise EditValidationError(f"Invalid {name} structure at " + "/".join(map(str, errors[0].absolute_path)))


def _unique(rows: list[dict], key: str, label: str) -> dict[str, dict]:
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise EditValidationError(f"Duplicate {label} ID")
    return result


def _digest(edit: dict) -> str:
    return canonical_contract_digest({k: v for k, v in edit.items() if k != "edit_digest"})


def timing_anchor_digest(edit: dict) -> str:
    """Bind ordered clips and source identity, excluding labels and audio gain."""
    sources = {s["source_id"]: s for s in edit["sources"]}
    rows = []
    position = 0
    for clip in edit["clips"]:
        source = sources.get(clip["source_id"])
        if source is None:
            raise EditValidationError("Clip source is missing")
        duration = clip["out_frame"] - clip["in_frame"]
        rows.append({"clip_id": clip["clip_id"], "source_ref": source["source_ref"],
                     "in_frame": clip["in_frame"], "out_frame": clip["out_frame"],
                     "output_start": position, "output_duration": duration})
        position += duration
    return canonical_contract_digest({"edit_id": edit["edit_id"], "clock_rate": edit["clock_rate"], "clips": rows})


def validate_edit(edit: dict) -> None:
    _shape(edit, "fsf_edit_spec_v1")
    if edit["edit_digest"] != _digest(edit):
        raise EditValidationError("Edit digest mismatch")
    if (edit["revision"] == 1) != (edit["parent_digest"] is None):
        raise EditValidationError("Revision and parent digest disagree")
    sources = _unique(edit["sources"], "source_id", "source")
    clips = _unique(edit["clips"], "clip_id", "clip")
    retired = set(edit["retired_clip_ids"])
    if retired & clips.keys():
        raise EditValidationError("Retired clip ID reused")
    for clip in clips.values():
        source = sources.get(clip["source_id"])
        if source is None:
            raise EditValidationError("Clip source is missing")
        if not 0 <= clip["in_frame"] < clip["out_frame"] <= source["duration_frames"]:
            raise EditValidationError("Invalid source frame interval")
        if clip["clip_id"] in clip["lineage"] or any(p not in clips and p not in retired for p in clip["lineage"]):
            raise EditValidationError("Invalid clip lineage")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise EditValidationError("Cyclic clip lineage")
        if identifier in visited or identifier not in clips:
            return
        visiting.add(identifier)
        for parent in clips[identifier]["lineage"]:
            visit(parent)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in clips:
        visit(identifier)
    _unique(edit["audio_tracks"] + edit["subtitle_tracks"], "track_id", "track")
    total = sum(c["out_frame"] - c["in_frame"] for c in clips.values())
    anchor = timing_anchor_digest(edit)
    cues = []
    for track in edit["subtitle_tracks"]:
        if track["clock_rate"] != edit["clock_rate"]:
            raise EditValidationError("Subtitle clock differs from edit clock")
        if track["timebase"] == "timeline_absolute" and track["timing_anchor_digest"] != anchor:
            raise EditValidationError("Subtitle timing anchor is outdated")
        intervals: dict[str, list[tuple[int, int]]] = {}
        for cue in track["cues"]:
            cues.append(cue)
            group = "timeline"
            limit = total
            if track["timebase"] == "clip_relative":
                clip = clips.get(cue["clip_id"])
                if clip is None:
                    raise EditValidationError("Subtitle refers to a missing clip")
                group = cue["clip_id"]
                limit = clip["out_frame"] - clip["in_frame"]
            if not 0 <= cue["start_frame"] < cue["end_frame"] <= limit:
                raise EditValidationError("Invalid subtitle frame interval")
            intervals.setdefault(group, []).append((cue["start_frame"], cue["end_frame"]))
        for ranges in intervals.values():
            ranges.sort()
            if any(right[0] < left[1] for left, right in zip(ranges, ranges[1:])):
                raise EditValidationError("Overlapping cues in one subtitle track")
    _unique(cues, "cue_id", "cue")
    from fsf.core.audio_edit import validate_audio
    validate_audio(edit)


def create_edit(*, edit_id: str, clock_rate: dict, sources: list[dict], clips: list[dict],
                audio_tracks: list[dict] | None = None, subtitle_tracks: list[dict] | None = None) -> dict:
    edit = copy.deepcopy({"schema_version": "fsf_edit_spec_v1", "edit_id": edit_id,
                          "revision": 1, "parent_digest": None, "clock_rate": clock_rate,
                          "sources": sources, "clips": clips, "audio_tracks": audio_tracks or [],
                          "subtitle_tracks": subtitle_tracks or [], "retired_clip_ids": []})
    edit["edit_digest"] = _digest(edit)
    validate_edit(edit)
    return edit


def apply_edit_patch(edit: dict, patch: dict) -> dict:
    """Apply an atomic in-memory metadata revision; never execute media tools."""
    validate_edit(edit)
    _shape(patch, "fsf_edit_patch_v1")
    if (patch["edit_id"], patch["base_revision"], patch["base_digest"]) != (edit["edit_id"], edit["revision"], edit["edit_digest"]):
        raise EditValidationError("Patch base revision/digest is outdated or belongs to another edit")
    result = copy.deepcopy(edit)
    used = {c["clip_id"] for c in result["clips"]} | set(result["retired_clip_ids"])
    for op in patch["operations"]:
        if "audio" in op["op"]:
            from fsf.core.audio_edit import apply_audio_operation
            apply_audio_operation(result, op)
            continue
        clips = result["clips"]
        if op["op"] == "reorder":
            by_id = {c["clip_id"]: c for c in clips}
            if set(op["clip_ids"]) != set(by_id):
                raise EditValidationError("Reorder must name every current clip exactly once")
            result["clips"] = [by_id[i] for i in op["clip_ids"]]
            continue
        if op["op"] == "confirm_timeline_anchor":
            tracks = [t for t in result["subtitle_tracks"] if t["track_id"] == op["track_id"]]
            if not tracks or tracks[0]["timebase"] != "timeline_absolute":
                raise EditValidationError("Expected an absolute subtitle track")
            tracks[0]["timing_anchor_digest"] = timing_anchor_digest(result)
            continue
        if op["op"] == "upsert_subtitle_track":
            track = copy.deepcopy(op["track"])
            matches = [i for i, t in enumerate(result["subtitle_tracks"]) if t["track_id"] == track["track_id"]]
            if matches:
                result["subtitle_tracks"][matches[0]] = track
            else:
                result["subtitle_tracks"].append(track)
            continue
        if op["op"] in ("remove_subtitle_track", "upsert_cue", "remove_cue"):
            tracks = [t for t in result["subtitle_tracks"] if t["track_id"] == op["track_id"]]
            if len(tracks) != 1:
                raise EditValidationError("Subtitle track is missing")
            track = tracks[0]
            if op["op"] == "remove_subtitle_track":
                result["subtitle_tracks"].remove(track)
            elif op["op"] == "upsert_cue":
                cue = copy.deepcopy(op["cue"])
                matches = [i for i, c in enumerate(track["cues"]) if c["cue_id"] == cue["cue_id"]]
                if matches:
                    track["cues"][matches[0]] = cue
                else:
                    track["cues"].append(cue)
            else:
                matches = [c for c in track["cues"] if c["cue_id"] == op["cue_id"]]
                if not matches:
                    raise EditValidationError("Subtitle cue is missing")
                track["cues"].remove(matches[0])
            continue
        index = next((i for i, c in enumerate(clips) if c["clip_id"] == op["clip_id"]), None)
        if index is None:
            raise EditValidationError("Patch refers to a missing or retired clip")
        clip = clips[index]
        if op["op"] == "rename":
            clip["label"] = op["label"]
        elif op["op"] == "trim":
            clip["in_frame"], clip["out_frame"] = op["in_frame"], op["out_frame"]
        elif op["op"] == "remove":
            clips.pop(index)
            result["retired_clip_ids"].append(clip["clip_id"])
        else:
            ids = [op["new_clip_id"]] if op["op"] == "duplicate" else op["new_clip_ids"]
            if any(i in used for i in ids):
                raise EditValidationError("New clip ID has already been used")
            used.update(ids)
            children = [copy.deepcopy(clip) for _ in ids]
            for child, identifier in zip(children, ids):
                child["clip_id"] = identifier
                child["lineage"] = list(dict.fromkeys(clip["lineage"] + [clip["clip_id"]]))
            if op["op"] == "duplicate":
                clips.insert(index + 1, children[0])
            else:
                at = op["at_frame"]
                if not 0 < at < clip["out_frame"] - clip["in_frame"]:
                    raise EditValidationError("Split must be strictly inside the clip")
                children[0]["out_frame"] = clip["in_frame"] + at
                children[1]["in_frame"] = clip["in_frame"] + at
                clips[index:index + 1] = children
                result["retired_clip_ids"].append(clip["clip_id"])
    result["revision"] += 1
    result["parent_digest"] = edit["edit_digest"]
    result["edit_digest"] = _digest(result)
    validate_edit(result)
    return result


def _timecode(frame: int, clock: dict) -> str:
    milliseconds = round(Fraction(frame * clock["den"] * 1000, clock["num"]))
    seconds, ms = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02}:{minute:02}:{second:02}.{ms:03}"


def inspect_edit(edit: dict) -> dict:
    """One authoritative, version-bound view for user and agent."""
    validate_edit(edit)
    sources = {s["source_id"]: s for s in edit["sources"]}
    position = 0
    rows = []
    starts = {}
    for ordinal, clip in enumerate(edit["clips"], 1):
        source = sources[clip["source_id"]]
        end = position + clip["out_frame"] - clip["in_frame"]
        starts[clip["clip_id"]] = position
        rows.append({"position": ordinal, **copy.deepcopy(clip), "shot_id": source["shot_id"],
                     "source_sha256": source["source_ref"]["artifact_sha256"],
                     "timeline_start_frame": position, "timeline_end_frame": end,
                     "timeline_start": _timecode(position, edit["clock_rate"]),
                     "timeline_end": _timecode(end, edit["clock_rate"]),
                     "source_in": _timecode(clip["in_frame"], edit["clock_rate"]),
                     "source_out": _timecode(clip["out_frame"], edit["clock_rate"])})
        position = end
    cues = []
    for track in edit["subtitle_tracks"]:
        for cue in track["cues"]:
            offset = starts[cue["clip_id"]] if track["timebase"] == "clip_relative" else 0
            cues.append({**copy.deepcopy(cue), "track_id": track["track_id"],
                         "timeline_start_frame": offset + cue["start_frame"],
                         "timeline_end_frame": offset + cue["end_frame"]})
    return {"edit_id": edit["edit_id"], "revision": edit["revision"], "edit_digest": edit["edit_digest"],
            "clock_rate": copy.deepcopy(edit["clock_rate"]), "clips": rows,
            "audio_tracks": copy.deepcopy(edit["audio_tracks"]), "cues": cues, "total_frames": position}


def locate_clips(edit: dict, *, revision: int, digest: str, clip_id: str | None = None,
                 position: int | None = None, frame: int | None = None, label: str | None = None) -> list[dict]:
    view = inspect_edit(edit)
    if type(revision) is not int or (revision, digest) != (edit["revision"], edit["edit_digest"]):
        raise EditValidationError("Locator revision/digest is outdated")
    if sum(v is not None for v in (clip_id, position, frame, label)) != 1:
        raise EditValidationError("Provide exactly one locator")
    if position is not None and (type(position) is not int or position < 1):
        raise EditValidationError("Position must be a positive integer")
    if frame is not None and (type(frame) is not int or frame < 0):
        raise EditValidationError("Frame must be a non-negative integer")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise EditValidationError("Label must be non-empty")
    rows = [r for r in view["clips"] if
            (clip_id is not None and r["clip_id"] == clip_id) or
            (position is not None and r["position"] == position) or
            (frame is not None and r["timeline_start_frame"] <= frame < r["timeline_end_frame"]) or
            (label is not None and label.casefold() in r["label"].casefold())]
    if not rows:
        raise EditValidationError("No current clip matches this locator")
    # Multiple label matches are returned as candidates, never auto-selected.
    return rows


def diff_edits(before: dict, after: dict) -> dict:
    """Describe changed identities and locations without mutating either edit."""
    left, right = inspect_edit(before), inspect_edit(after)
    if before["edit_id"] != after["edit_id"]:
        raise EditValidationError("Cannot compare different edit identities")
    old = {r["clip_id"]: r for r in left["clips"]}
    new = {r["clip_id"]: r for r in right["clips"]}
    changes = [{"clip_id": identifier, "before": old.get(identifier), "after": new.get(identifier)}
               for identifier in dict.fromkeys([*old, *new]) if old.get(identifier) != new.get(identifier)]
    return {"edit_id": before["edit_id"], "before_revision": before["revision"],
            "before_digest": before["edit_digest"], "after_revision": after["revision"],
            "after_digest": after["edit_digest"], "before_total_frames": left["total_frames"],
            "after_total_frames": right["total_frames"], "clip_changes": changes,
            "audio_tracks_changed": before["audio_tracks"] != after["audio_tracks"],
            **({"audio_mix_changed": before.get("audio_mix") != after.get("audio_mix")} if "audio_mix" in before or "audio_mix" in after else {}),
            "subtitle_tracks_changed": before["subtitle_tracks"] != after["subtitle_tracks"]}
