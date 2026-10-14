"""FSF-S Studio flow catalog (CLI / skill first; no desk pixels).

Fourteen film functions from the V3 map. A flow is a skill name, not a model
or a platform. Transports (kaggle / api / local) are executors behind the skill.
"""

from __future__ import annotations

from typing import Any, Literal

Transport = Literal["kaggle", "api", "local"]
FlowStatus = Literal["implemented", "planned"]
TransportStatus = Literal["shape", "implemented", "planned", "not_applicable"]

TRANSPORTS: tuple[Transport, ...] = ("kaggle", "api", "local")

# skill -> transport -> executor_id (shape transports; live inference is not wired)
SKILL_EXECUTOR_IDS: dict[str, dict[Transport, str]] = {
    "studio.video.generate": {
        "kaggle": "kaggle.ltx23.t2v",
        "api": "api.vendor.video",
        "local": "local.comfy.video",
    },
    "studio.video.remaster": {
        "kaggle": "kaggle.video.remaster",
        "api": "api.vendor.remaster",
        "local": "local.ffmpeg.remaster",
    },
    "studio.storyboard.first_frame": {
        "kaggle": "kaggle.storyboard.t2i",
        "api": "api.vendor.image",
        "local": "local.comfy.image",
    },
    "studio.audio.post": {
        "kaggle": "kaggle.audio.post",
        "api": "api.vendor.audio",
        "local": "local.ffmpeg.audio",
    },
    "studio.vfx.prepare": {
        "kaggle": "kaggle.vfx.prepare",
        "api": "api.vendor.vfx",
        "local": "local.comfy.vfx",
    },
    "studio.voice.dub": {
        "kaggle": "kaggle.voice.dub",
        "api": "api.vendor.voice",
        "local": "local.voice.dub",
    },
    "studio.music.trailer": {
        "kaggle": "kaggle.music.trailer",
        "api": "api.vendor.music",
        "local": "local.music.trailer",
    },
    "studio.audio.sfx": {
        "kaggle": "kaggle.audio.sfx",
        "api": "api.vendor.sfx",
        "local": "local.audio.sfx",
    },
    "studio.asset.3d": {
        "kaggle": "kaggle.asset.3d",
        "api": "api.vendor.3d",
        "local": "local.asset.3d",
    },
    "studio.motion.mocap": {
        "kaggle": "kaggle.motion.mocap",
        "api": "api.vendor.mocap",
        "local": "local.motion.mocap",
    },
    "studio.image.design": {
        "kaggle": "kaggle.image.design",
        "api": "api.vendor.design",
        "local": "local.comfy.design",
    },
    "studio.model.lora": {
        "kaggle": "kaggle.model.lora",
        "api": "api.vendor.lora",
        "local": "local.model.lora",
    },
    "studio.dataset.prepare": {
        "kaggle": "kaggle.dataset.prepare",
        "api": "api.vendor.dataset",
        "local": "local.dataset.prepare",
    },
    "studio.model.pdx_specialist": {
        "kaggle": "kaggle.model.pdx_specialist",
        "api": "api.vendor.pdx_specialist",
        "local": "local.model.pdx_specialist",
    },
}


def _transports(
    *,
    status: TransportStatus = "shape",
    notes: dict[Transport, str] | None = None,
    not_applicable: tuple[Transport, ...] = (),
) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    extra = notes or {}
    for name in TRANSPORTS:
        if name in not_applicable:
            row_status: TransportStatus = "not_applicable"
        else:
            row_status = status
        row = {"status": row_status}
        if extra.get(name):
            row["note"] = extra.get(name, "")
        if row_status == "shape" and "note" not in row:
            row["note"] = "execute_shot writes contract-shape bytes, not media"
        rows[name] = row
    return rows


STUDIO_FLOWS: list[dict[str, Any]] = [
    {
        "index": 1,
        "flow_id": "p0.video.generate",
        "skill": "studio.video.generate",
        "phase": "P0",
        "title": "Video Generation",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 2,
        "flow_id": "p0.video.remaster",
        "skill": "studio.video.remaster",
        "phase": "P0",
        "title": "Video Remaster",
        "status": "implemented",
        "transports": _transports(
            notes={
                "local": "Primary: deterministic remux / spec normalize",
                "kaggle": "Heavy upscale notebooks",
                "api": "Vendor upscale / frame-interp APIs",
            },
        ),
    },
    {
        "index": 3,
        "flow_id": "p0.storyboard.first_frame",
        "skill": "studio.storyboard.first_frame",
        "phase": "P0",
        "title": "Storyboard / First-Frame",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 4,
        "flow_id": "p0.audio.post",
        "skill": "studio.audio.post",
        "phase": "P0",
        "title": "Audio Post",
        "status": "implemented",
        "transports": _transports(
            notes={
                "local": "Primary: stems / loudness sidecar",
                "api": "Vendor STT / denoise APIs",
                "kaggle": "Batch ASR notebooks",
            },
        ),
    },
    {
        "index": 5,
        "flow_id": "p1.vfx.prepare",
        "skill": "studio.vfx.prepare",
        "phase": "P1",
        "title": "VFX Preparation",
        "status": "implemented",
        "transports": _transports(
            notes={
                "local": "Segmentation / depth / tracking sidecars",
                "kaggle": "Batch VFX notebooks",
                "api": "Vendor matte / depth APIs",
            },
        ),
    },
    {
        "index": 6,
        "flow_id": "p1.voice.dub",
        "skill": "studio.voice.dub",
        "phase": "P1",
        "title": "Voice / Dubbing",
        "status": "implemented",
        "transports": _transports(
            notes={
                "local": "Requires consent and rights policy on the job",
                "kaggle": "Batch dub notebooks; same rights policy",
                "api": "Vendor TTS / dub APIs; same rights policy",
            },
        ),
    },
    {
        "index": 7,
        "flow_id": "p1.music.trailer",
        "skill": "studio.music.trailer",
        "phase": "P1",
        "title": "Trailer Music",
        "status": "implemented",
        "transports": _transports(
            notes={
                "local": "15 / 30 / 60 / 90 second cues only; not arbitrary-length songs",
                "kaggle": "Batch cue notebooks",
                "api": "Vendor music APIs with the same cue lengths",
            },
        ),
    },
    {
        "index": 8,
        "flow_id": "p1.audio.sfx",
        "skill": "studio.audio.sfx",
        "phase": "P1",
        "title": "Sound Effects",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 9,
        "flow_id": "p2.asset.3d",
        "skill": "studio.asset.3d",
        "phase": "P2",
        "title": "3D Asset",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 10,
        "flow_id": "p2.motion.mocap",
        "skill": "studio.motion.mocap",
        "phase": "P2",
        "title": "Motion / Mocap",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 11,
        "flow_id": "p2.image.design",
        "skill": "studio.image.design",
        "phase": "P2",
        "title": "Image / Design",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 12,
        "flow_id": "p3.model.lora",
        "skill": "studio.model.lora",
        "phase": "P3",
        "title": "LoRA Training",
        "status": "implemented",
        "transports": _transports(
            notes={
                "kaggle": "Shares Engine KAGGLE_PLAN transport; skill id stays separate",
                "api": "Vendor fine-tune APIs",
                "local": "Local trainer / Comfy LoRA",
            },
        ),
    },
    {
        "index": 13,
        "flow_id": "p3.dataset.prepare",
        "skill": "studio.dataset.prepare",
        "phase": "P3",
        "title": "Dataset Preparation",
        "status": "implemented",
        "transports": _transports(),
    },
    {
        "index": 14,
        "flow_id": "p3.model.pdx_specialist",
        "skill": "studio.model.pdx_specialist",
        "phase": "P3",
        "title": "PDX Specialist Training traces",
        "status": "implemented",
        "transports": _transports(
            notes={"kaggle": "Traces only; does not copy Kernel/Media schemas into Engine"},
        ),
    },
]


def list_studio_flows() -> list[dict[str, Any]]:
    """Return a copy of the fourteen Studio flow records."""
    return [dict(item) for item in STUDIO_FLOWS]


def get_studio_flow(skill_or_id: str) -> dict[str, Any]:
    """Look up a flow by skill name or flow_id."""
    key = skill_or_id.strip()
    for item in STUDIO_FLOWS:
        if item["skill"] == key or item["flow_id"] == key:
            return dict(item)
    known = ", ".join(item["skill"] for item in STUDIO_FLOWS)
    raise KeyError(f"Unknown Studio flow '{skill_or_id}'. Known skills: {known}")


SKILL_ALIASES: dict[str, str] = {
    "generate": "studio.video.generate",
    "video": "studio.video.generate",
    "remaster": "studio.video.remaster",
    "storyboard": "studio.storyboard.first_frame",
    "first_frame": "studio.storyboard.first_frame",
    "audio": "studio.audio.post",
    "post": "studio.audio.post",
    "vfx": "studio.vfx.prepare",
    "dub": "studio.voice.dub",
    "voice": "studio.voice.dub",
    "music": "studio.music.trailer",
    "trailer": "studio.music.trailer",
    "sfx": "studio.audio.sfx",
    "3d": "studio.asset.3d",
    "mocap": "studio.motion.mocap",
    "design": "studio.image.design",
    "lora": "studio.model.lora",
    "dataset": "studio.dataset.prepare",
    "specialist": "studio.model.pdx_specialist",
}


def resolve_skill_name(skill_or_alias: str) -> str:
    """Resolve skill, flow_id, or short alias to a canonical skill name."""
    key = skill_or_alias.strip()
    if not key:
        raise KeyError("Empty Studio skill name")
    alias = SKILL_ALIASES.get(key.lower())
    if alias:
        return alias
    try:
        return get_studio_flow(key)["skill"]
    except KeyError:
        suffix_hits = [item["skill"] for item in STUDIO_FLOWS if item["skill"].rsplit(".", 1)[-1] == key]
        if len(suffix_hits) == 1:
            return suffix_hits[0]
        raise


def parse_skill_list(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated skill list and resolve each name."""
    if raw is None:
        return ()
    parts = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    resolved = tuple(resolve_skill_name(item) for item in parts)
    if len(set(resolved)) != len(resolved):
        raise ValueError(f"Duplicate skills in list: {', '.join(resolved)}")
    return resolved


def executor_id_for(skill: str, transport: Transport) -> str:
    """Resolve skill + transport to an executor id. Raises if not implemented."""
    flow = get_studio_flow(skill)
    transport_row = flow["transports"][transport]
    if transport_row["status"] == "not_applicable":
        raise ValueError(
            f"Transport '{transport}' is not applicable for skill '{flow['skill']}'."
        )
    if flow["status"] not in {"implemented", "shape"} or transport_row["status"] not in {
        "implemented",
        "shape",
    }:
        raise NotImplementedError(
            f"Studio flow '{flow['skill']}' transport '{transport}' is not implemented yet "
            f"(CLI/skill first; no desk). Phase {flow['phase']}."
        )
    mapping = SKILL_EXECUTOR_IDS.get(flow["skill"], {})
    if transport not in mapping:
        raise NotImplementedError(
            f"No executor registered for '{flow['skill']}' / '{transport}'."
        )
    return mapping[transport]
