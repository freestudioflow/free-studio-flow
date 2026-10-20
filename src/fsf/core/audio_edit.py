"""Audio identities, sample positions and atomic metadata patch semantics."""
import copy
from fractions import Fraction

from fsf.core.editing import EditValidationError


class AudioError(EditValidationError):
    """Stable, bounded audio diagnostic without operator-supplied text."""
    def __init__(self, code="AUDIO_INVALID"):
        self.code = code
        super().__init__(code)


def enabled(edit):
    return bool(edit.get("audio_mix", {}).get("enabled", False))


def frame_sample(frame, clock):
    return round(Fraction(frame * clock["den"] * 48000, clock["num"]))


def event_layout(edit):
    from fsf.core.editing import timing_anchor_digest
    starts, clips, position = {}, {}, 0
    for clip in edit["clips"]:
        starts[clip["clip_id"]] = position
        clips[clip["clip_id"]] = clip
        position += clip["out_frame"] - clip["in_frame"]
    clock = edit["clock_rate"]
    total = frame_sample(position, clock)
    result = {}
    for event in edit["audio_mix"]["events"]:
        placement = event["placement"]
        offset, limit = 0, total
        if placement["timebase"] == "clip_relative":
            clip = clips.get(placement["clip_id"])
            if clip is None:
                raise AudioError()
            offset = starts[clip["clip_id"]]
            if placement["start_frame"] >= clip["out_frame"] - clip["in_frame"]:
                raise AudioError()
            limit = frame_sample(offset + clip["out_frame"] - clip["in_frame"], clock)
        elif placement["timing_anchor_digest"] != timing_anchor_digest(edit):
            raise AudioError("AUDIO_STALE")
        start = frame_sample(offset + placement["start_frame"], clock)
        length = event["source_out_sample"] - event["source_in_sample"]
        if length < 2 or not 0 <= start < start + length <= limit:
            raise AudioError()
        result[event["event_id"]] = {"start_sample": start, "content_samples": length,
            "end_sample": limit if event["tail_policy"] == "silence_to_anchor_end" else start + length}
    return result


def _unique(rows, key):
    ids = [item[key] for item in rows]
    if len(ids) != len(set(ids)):
        raise AudioError()
    return {item[key]: item for item in rows}


def validate_audio(edit):
    if "audio_mix" not in edit:
        return
    mix = edit["audio_mix"]
    tracks = _unique(mix["track_settings"], "track_id")
    identities = _unique(edit["audio_tracks"], "track_id")
    if tracks.keys() != identities.keys():
        raise AudioError()
    sources = _unique(mix["sources"], "audio_source_id")
    events = _unique(mix["events"], "event_id")
    regions = _unique([r for e in events.values() for r in e["regions"]], "region_id")
    for kind, ids in [("track", tracks), ("source", sources), ("event", events), ("region", regions)]:
        if set(mix["retired_ids"][kind]) & ids.keys():
            raise AudioError("AUDIO_ID_RETIRED")
    if set(mix["retired_ids"]["track"]) & {t["track_id"] for t in edit["subtitle_tracks"]}:
        raise AudioError("AUDIO_ID_RETIRED")
    video_sources = {s["source_id"]: s for s in edit["sources"]}
    for source in sources.values():
        binding = source["binding"]
        if binding["kind"] == "clip_original":
            video = video_sources.get(binding["video_source_id"])
            if video is None or video["source_ref"]["artifact_sha256"] != binding["video_sha256"]:
                raise AudioError("AUDIO_SOURCE_INVALID")
    layout = event_layout(edit)
    ranges = {}
    for event in events.values():
        track = tracks.get(event["track_id"])
        source = sources.get(event["audio_source_id"])
        if track is None or source is None or event["placement"]["timebase"] != track["timebase"]:
            raise AudioError()
        binding = source["binding"]
        if binding["kind"] == "operator_audio_import" and event["source_out_sample"] > binding["canonical_samples"]:
            raise AudioError("AUDIO_SOURCE_INVALID")
        span = layout[event["event_id"]]
        ranges.setdefault(event["track_id"], []).append((span["start_sample"], span["end_sample"]))
        ordered = sorted(event["regions"], key=lambda r: r["start_sample"])
        end = 0
        for region in ordered:
            if not end <= region["start_sample"] < region["end_sample"] <= span["content_samples"] or region["end_sample"]-region["start_sample"] < 2:
                raise AudioError()
            end = region["end_sample"]
            if not -60 <= mix["settings"]["master_gain_db"] + track["gain_db"] + event["gain_db"] + region["gain_db"] <= 12:
                raise AudioError()
        if not -60 <= mix["settings"]["master_gain_db"] + track["gain_db"] + event["gain_db"] <= 12:
            raise AudioError()
    for spans in ranges.values():
        spans.sort()
        if any(a[1] > b[0] for a,b in zip(spans, spans[1:])):
            raise AudioError()
    norm = mix["settings"]["normalization"]
    groups = _unique(norm["groups"], "group_id")
    if (norm["mode"] == "dialogue_groups") != bool(groups):
        raise AudioError()
    used = set()
    for group in groups.values():
        for eid in group["event_ids"]:
            if eid not in events or eid in used or identities[events[eid]["track_id"]]["role"] in {"bgm","sfx"}:
                raise AudioError()
            used.add(eid)


def apply_audio_operation(edit, op):
    name = op["op"]
    if name == "initialize_audio_mix":
        if "audio_mix" in edit:
            raise AudioError()
        edit["audio_mix"] = copy.deepcopy(op["audio_mix"])
        return
    mix = edit.get("audio_mix")
    if not mix:
        raise AudioError()
    if name == "set_audio_enabled":
        mix["enabled"] = op["enabled"]
        return
    if name == "set_audio_settings":
        mix["settings"] = copy.deepcopy(op["settings"])
        return
    if name == "confirm_audio_timeline_anchor":
        from fsf.core.editing import timing_anchor_digest
        event = next((e for e in mix["events"] if e["event_id"] == op["event_id"]), None)
        if event is None or event["placement"]["timebase"] != "timeline_absolute":
            raise AudioError()
        event["placement"]["timing_anchor_digest"] = timing_anchor_digest(edit)
        return
    if name == "upsert_audio_region":
        event = next((e for e in mix["events"] if e["event_id"] == op["event_id"]), None)
        if event is None:
            raise AudioError()
        rows, kind, key, item = event["regions"], "region", "region_id", op["region"]
    elif name == "remove_audio_region":
        matches = [e for e in mix["events"] if any(r["region_id"] == op["region_id"] for r in e["regions"])]
        if len(matches) != 1:
            raise AudioError()
        rows, kind, key, item = matches[0]["regions"], "region", "region_id", None
    else:
        kind = "track" if name.endswith("track") else "source" if name.endswith("source") else "event"
        key = {"track":"track_id","source":"audio_source_id","event":"event_id"}[kind]
        rows = mix[{"track":"track_settings","source":"sources","event":"events"}[kind]]
        item = op.get(kind)
    identifier = item[key] if item is not None else op[key]
    matches = [r for r in rows if r[key] == identifier]
    if name.startswith("remove_"):
        if len(matches) != 1:
            raise AudioError()
        if kind == "event":
            mix["retired_ids"]["region"].extend(r["region_id"] for r in matches[0]["regions"])
        rows.remove(matches[0])
        mix["retired_ids"][kind].append(identifier)
        if kind == "track":
            edit["audio_tracks"] = [t for t in edit["audio_tracks"] if t["track_id"] != identifier]
        return
    if identifier in mix["retired_ids"][kind]:
        raise AudioError("AUDIO_ID_RETIRED")
    if kind == "source" and matches:
        raise AudioError("AUDIO_SOURCE_INVALID")
    if kind == "region":
        for event in mix["events"]:
            if event["regions"] is not rows and any(r["region_id"] == identifier for r in event["regions"]):
                raise AudioError()
    if kind == "event" and matches:
        old = {r["region_id"] for r in matches[0]["regions"]}
        new = {r["region_id"] for r in item["regions"]}
        if new & set(mix["retired_ids"]["region"]):
            raise AudioError("AUDIO_ID_RETIRED")
        mix["retired_ids"]["region"].extend(sorted(old - new))
    if matches:
        rows[rows.index(matches[0])] = copy.deepcopy(item)
    else:
        rows.append(copy.deepcopy(item))
    if kind == "track":
        if op["identity"]["track_id"] != identifier:
            raise AudioError()
        edit["audio_tracks"] = [t for t in edit["audio_tracks"] if t["track_id"] != identifier] + [copy.deepcopy(op["identity"])]


def ab_projection(edit):
    from fsf.core.editing import timing_anchor_digest
    mix = edit.get("audio_mix")
    if not mix or not mix["enabled"]:
        raise AudioError("AUDIO_AB_MISMATCH")
    return {"edit_id":edit["edit_id"], "video_anchor":timing_anchor_digest(edit),
        "sources":copy.deepcopy(mix["sources"]), "tracks":[{k:t[k] for k in ("track_id","timebase")} for t in mix["track_settings"]],
        "events":[{k:copy.deepcopy(e[k]) for k in ("event_id","track_id","audio_source_id","source_in_sample","source_out_sample","placement","tail_policy")} for e in mix["events"]]}


def check_ab(before, after):
    if ab_projection(before) != ab_projection(after):
        raise AudioError("AUDIO_AB_MISMATCH")
