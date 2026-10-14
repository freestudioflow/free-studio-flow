"""Deterministic Studio-skill executors for remaster, storyboard, and audio.

Each skill keeps the same tool name across kaggle / api / local. Switching
transport must not mutate source_prompt or Kernel provenance.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from fsf.executors.base import BaseVideoExecutor, shape_media_filename
from fsf.executors.kaggle import KaggleLtx23Executor


class StudioSkillExecutor(BaseVideoExecutor):
    """Generic skill executor that writes a deterministic artifact for one transport."""

    def __init__(
        self,
        *,
        executor_id: str,
        tool_name: str,
        display_name: str,
        artifact_name: str,
        payload_prefix: str,
        availability: str = "always",
        extra_fields: tuple[str, ...] = (),
    ) -> None:
        self._executor_id = executor_id
        self._tool_name = tool_name
        self._display_name = display_name
        self._artifact_name = artifact_name
        self._payload_prefix = payload_prefix
        self._availability = availability
        self._extra_fields = extra_fields

    @property
    def executor_id(self) -> str:
        return self._executor_id

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def display_name(self) -> str:
        return self._display_name

    def is_available(self) -> bool:
        if self._availability == "always":
            return True
        if self._availability == "kaggle":
            return KaggleLtx23Executor().is_available()
        if self._availability == "api":
            return bool(os.environ.get("FSF_VENDOR_API_KEY"))
        if self._availability == "comfy":
            return os.environ.get("COMFYUI_AVAILABLE") == "1"
        return False

    def execute_shot(
        self,
        task: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shot_id = task["id"]
        filename = shape_media_filename(self._artifact_name.format(shot_id=shot_id))
        target_file = output_dir / filename
        prompt = task.get("normalized_prompt") or task.get("prompt") or ""
        extras: list[str] = []
        for key in self._extra_fields:
            value = task.get(key)
            if key == "cue_seconds" and value is None:
                value = 30
            if key == "rights_policy" and not value:
                value = "unresolved:consent_and_rights_policy"
            if key == "kaggle_plan_ref" and not value:
                value = "unresolved:engine_kaggle_plan"
            extras.append(f"{key}={value}")
        payload = f"{self._payload_prefix}:{shot_id}:{prompt}:{';'.join(extras)}".encode("utf-8")
        target_file.write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        return self.build_tool_result(
            request_id=f"req:{self.executor_id}:{shot_id}",
            shot_id=shot_id,
            artifact_path=target_file,
            artifact_sha256=sha,
            size_bytes=len(payload),
            duration_seconds=float(task.get("duration_seconds", 5.0)),
        )


def p0_skill_executors() -> list[StudioSkillExecutor]:
    """All implemented P0 remaster / storyboard / audio transport executors."""
    return [
        StudioSkillExecutor(
            executor_id="kaggle.video.remaster",
            tool_name="studio.video.remaster",
            display_name="Kaggle remaster (upscale notebook)",
            artifact_name="{shot_id}.remaster.mp4",
            payload_prefix="KAGGLE_REMASTER",
            availability="kaggle",
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.remaster",
            tool_name="studio.video.remaster",
            display_name="Vendor / cloud API remaster",
            artifact_name="{shot_id}.remaster.mp4",
            payload_prefix="API_REMASTER",
            availability="api",
        ),
        StudioSkillExecutor(
            executor_id="local.ffmpeg.remaster",
            tool_name="studio.video.remaster",
            display_name="Local remaster (ffmpeg / spec normalize)",
            artifact_name="{shot_id}.remaster.mp4",
            payload_prefix="LOCAL_REMASTER",
            availability="always",
        ),
        StudioSkillExecutor(
            executor_id="kaggle.storyboard.t2i",
            tool_name="studio.storyboard.first_frame",
            display_name="Kaggle storyboard / first-frame",
            artifact_name="{shot_id}.first_frame.png",
            payload_prefix="KAGGLE_FIRST_FRAME",
            availability="kaggle",
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.image",
            tool_name="studio.storyboard.first_frame",
            display_name="Vendor / cloud API first-frame",
            artifact_name="{shot_id}.first_frame.png",
            payload_prefix="API_FIRST_FRAME",
            availability="api",
        ),
        StudioSkillExecutor(
            executor_id="local.comfy.image",
            tool_name="studio.storyboard.first_frame",
            display_name="Local ComfyUI first-frame",
            artifact_name="{shot_id}.first_frame.png",
            payload_prefix="LOCAL_FIRST_FRAME",
            availability="comfy",
        ),
        StudioSkillExecutor(
            executor_id="kaggle.audio.post",
            tool_name="studio.audio.post",
            display_name="Kaggle audio post (batch ASR)",
            artifact_name="{shot_id}.audio.json",
            payload_prefix="KAGGLE_AUDIO_POST",
            availability="kaggle",
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.audio",
            tool_name="studio.audio.post",
            display_name="Vendor / cloud API audio post",
            artifact_name="{shot_id}.audio.json",
            payload_prefix="API_AUDIO_POST",
            availability="api",
        ),
        StudioSkillExecutor(
            executor_id="local.ffmpeg.audio",
            tool_name="studio.audio.post",
            display_name="Local audio post (stems / loudness sidecar)",
            artifact_name="{shot_id}.audio.json",
            payload_prefix="LOCAL_AUDIO_POST",
            availability="always",
        ),
    ]


def p1_skill_executors() -> list[StudioSkillExecutor]:
    """P1 post/sound: VFX, voice/dub, trailer music, SFX."""
    return [
        StudioSkillExecutor(
            executor_id="kaggle.vfx.prepare",
            tool_name="studio.vfx.prepare",
            display_name="Kaggle VFX prepare (seg / depth / track)",
            artifact_name="{shot_id}.vfx.json",
            payload_prefix="KAGGLE_VFX",
            availability="kaggle",
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.vfx",
            tool_name="studio.vfx.prepare",
            display_name="Vendor / cloud API VFX prepare",
            artifact_name="{shot_id}.vfx.json",
            payload_prefix="API_VFX",
            availability="api",
        ),
        StudioSkillExecutor(
            executor_id="local.comfy.vfx",
            tool_name="studio.vfx.prepare",
            display_name="Local ComfyUI VFX prepare",
            artifact_name="{shot_id}.vfx.json",
            payload_prefix="LOCAL_VFX",
            availability="comfy",
        ),
        StudioSkillExecutor(
            executor_id="kaggle.voice.dub",
            tool_name="studio.voice.dub",
            display_name="Kaggle voice / dubbing",
            artifact_name="{shot_id}.dub.json",
            payload_prefix="KAGGLE_VOICE_DUB",
            availability="kaggle",
            extra_fields=("rights_policy",),
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.voice",
            tool_name="studio.voice.dub",
            display_name="Vendor / cloud API voice / dubbing",
            artifact_name="{shot_id}.dub.json",
            payload_prefix="API_VOICE_DUB",
            availability="api",
            extra_fields=("rights_policy",),
        ),
        StudioSkillExecutor(
            executor_id="local.voice.dub",
            tool_name="studio.voice.dub",
            display_name="Local voice / dubbing",
            artifact_name="{shot_id}.dub.json",
            payload_prefix="LOCAL_VOICE_DUB",
            availability="always",
            extra_fields=("rights_policy",),
        ),
        StudioSkillExecutor(
            executor_id="kaggle.music.trailer",
            tool_name="studio.music.trailer",
            display_name="Kaggle trailer music cues",
            artifact_name="{shot_id}.trailer_cue.json",
            payload_prefix="KAGGLE_TRAILER_MUSIC",
            availability="kaggle",
            extra_fields=("cue_seconds",),
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.music",
            tool_name="studio.music.trailer",
            display_name="Vendor / cloud API trailer music",
            artifact_name="{shot_id}.trailer_cue.json",
            payload_prefix="API_TRAILER_MUSIC",
            availability="api",
            extra_fields=("cue_seconds",),
        ),
        StudioSkillExecutor(
            executor_id="local.music.trailer",
            tool_name="studio.music.trailer",
            display_name="Local trailer music cues (15/30/60/90s)",
            artifact_name="{shot_id}.trailer_cue.json",
            payload_prefix="LOCAL_TRAILER_MUSIC",
            availability="always",
            extra_fields=("cue_seconds",),
        ),
        StudioSkillExecutor(
            executor_id="kaggle.audio.sfx",
            tool_name="studio.audio.sfx",
            display_name="Kaggle sound effects",
            artifact_name="{shot_id}.sfx.json",
            payload_prefix="KAGGLE_SFX",
            availability="kaggle",
        ),
        StudioSkillExecutor(
            executor_id="api.vendor.sfx",
            tool_name="studio.audio.sfx",
            display_name="Vendor / cloud API sound effects",
            artifact_name="{shot_id}.sfx.json",
            payload_prefix="API_SFX",
            availability="api",
        ),
        StudioSkillExecutor(
            executor_id="local.audio.sfx",
            tool_name="studio.audio.sfx",
            display_name="Local sound effects",
            artifact_name="{shot_id}.sfx.json",
            payload_prefix="LOCAL_SFX",
            availability="always",
        ),
    ]


def _transport_triple(
    *,
    skill: str,
    artifact_name: str,
    kaggle_id: str,
    api_id: str,
    local_id: str,
    prefix: str,
    local_availability: str = "always",
    extra_fields: tuple[str, ...] = (),
    kaggle_title: str,
    api_title: str,
    local_title: str,
) -> list[StudioSkillExecutor]:
    return [
        StudioSkillExecutor(
            executor_id=kaggle_id,
            tool_name=skill,
            display_name=kaggle_title,
            artifact_name=artifact_name,
            payload_prefix=f"KAGGLE_{prefix}",
            availability="kaggle",
            extra_fields=extra_fields,
        ),
        StudioSkillExecutor(
            executor_id=api_id,
            tool_name=skill,
            display_name=api_title,
            artifact_name=artifact_name,
            payload_prefix=f"API_{prefix}",
            availability="api",
            extra_fields=extra_fields,
        ),
        StudioSkillExecutor(
            executor_id=local_id,
            tool_name=skill,
            display_name=local_title,
            artifact_name=artifact_name,
            payload_prefix=f"LOCAL_{prefix}",
            availability=local_availability,
            extra_fields=extra_fields,
        ),
    ]


def p2_skill_executors() -> list[StudioSkillExecutor]:
    """P2 assets: 3D, mocap, image/design."""
    rows: list[StudioSkillExecutor] = []
    rows.extend(
        _transport_triple(
            skill="studio.asset.3d",
            artifact_name="{shot_id}.asset3d.json",
            kaggle_id="kaggle.asset.3d",
            api_id="api.vendor.3d",
            local_id="local.asset.3d",
            prefix="ASSET_3D",
            kaggle_title="Kaggle 3D asset",
            api_title="Vendor / cloud API 3D asset",
            local_title="Local 3D asset",
        )
    )
    rows.extend(
        _transport_triple(
            skill="studio.motion.mocap",
            artifact_name="{shot_id}.mocap.json",
            kaggle_id="kaggle.motion.mocap",
            api_id="api.vendor.mocap",
            local_id="local.motion.mocap",
            prefix="MOCAP",
            kaggle_title="Kaggle motion / mocap",
            api_title="Vendor / cloud API mocap",
            local_title="Local motion / mocap",
        )
    )
    rows.extend(
        _transport_triple(
            skill="studio.image.design",
            artifact_name="{shot_id}.design.png",
            kaggle_id="kaggle.image.design",
            api_id="api.vendor.design",
            local_id="local.comfy.design",
            prefix="IMAGE_DESIGN",
            local_availability="comfy",
            kaggle_title="Kaggle image / design",
            api_title="Vendor / cloud API image / design",
            local_title="Local ComfyUI image / design",
        )
    )
    return rows


def p3_skill_executors() -> list[StudioSkillExecutor]:
    """P3 models and data: LoRA, dataset, PDX specialist traces."""
    rows: list[StudioSkillExecutor] = []
    rows.extend(
        _transport_triple(
            skill="studio.model.lora",
            artifact_name="{shot_id}.lora.json",
            kaggle_id="kaggle.model.lora",
            api_id="api.vendor.lora",
            local_id="local.model.lora",
            prefix="LORA",
            extra_fields=("kaggle_plan_ref",),
            kaggle_title="Kaggle LoRA (KAGGLE_PLAN transport; separate skill)",
            api_title="Vendor / cloud API LoRA",
            local_title="Local LoRA trainer",
        )
    )
    rows.extend(
        _transport_triple(
            skill="studio.dataset.prepare",
            artifact_name="{shot_id}.dataset.json",
            kaggle_id="kaggle.dataset.prepare",
            api_id="api.vendor.dataset",
            local_id="local.dataset.prepare",
            prefix="DATASET",
            kaggle_title="Kaggle dataset preparation",
            api_title="Vendor / cloud API dataset preparation",
            local_title="Local dataset preparation",
        )
    )
    rows.extend(
        _transport_triple(
            skill="studio.model.pdx_specialist",
            artifact_name="{shot_id}.pdx_specialist.json",
            kaggle_id="kaggle.model.pdx_specialist",
            api_id="api.vendor.pdx_specialist",
            local_id="local.model.pdx_specialist",
            prefix="PDX_SPECIALIST",
            kaggle_title="Kaggle PDX specialist traces",
            api_title="Vendor / cloud API specialist traces",
            local_title="Local PDX specialist traces",
        )
    )
    return rows


def implemented_skill_executors() -> list[StudioSkillExecutor]:
    """All implemented Studio-skill executors except generate backends."""
    return p0_skill_executors() + p1_skill_executors() + p2_skill_executors() + p3_skill_executors()
