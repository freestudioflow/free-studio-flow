"""FSF Runtime & Execution Plan compiler (FSF-M2 + FSF-S P0 pipeline).

Compiles FSF Video Jobs into PDX Execution Plans (pdx_execution_plan_v1),
providing deterministic stage sequencing for generation and Studio flows.
"""

from __future__ import annotations

from typing import Any, Sequence

from pdx_artifact_core import validate_execution_plan

from fsf.conformance.media import (
    build_expected_contract,
    canonical_contract_digest,
    expected_contract_for_task,
)
import copy

from fsf.core.consistency import (
    ConsistencyCompileError,
    detect_continuity_cycles,
    validate_context_in_plan,
)
from fsf.intake.parser import is_shot_unit, skill_applies_to_task
from fsf.studio.catalog import parse_skill_list, resolve_skill_name

P0_PIPELINE_TOOLS = (
    "studio.storyboard.first_frame",
    "studio.video.generate",
    "studio.video.remaster",
    "studio.audio.post",
)

P1_PIPELINE_TOOLS = (
    "studio.storyboard.first_frame",
    "studio.video.generate",
    "studio.video.remaster",
    "studio.vfx.prepare",
    "studio.audio.post",
    "studio.voice.dub",
    "studio.music.trailer",
    "studio.audio.sfx",
)

P2_PIPELINE_TOOLS = P1_PIPELINE_TOOLS + (
    "studio.asset.3d",
    "studio.motion.mocap",
    "studio.image.design",
)

P3_PIPELINE_TOOLS = P2_PIPELINE_TOOLS + (
    "studio.model.lora",
    "studio.dataset.prepare",
    "studio.model.pdx_specialist",
)

_SKILL_ARTIFACT = {
    "studio.storyboard.first_frame": ("Storyboard first-frame for {shot_id}", "artifact:{shot_id}.first_frame.shape.bin"),
    "studio.video.generate": ("Generate video for {shot_id}", "artifact:{shot_id}.shape.bin"),
    "studio.video.remaster": ("Remaster video for {shot_id}", "artifact:{shot_id}.remaster.shape.bin"),
    "studio.audio.post": ("Audio post for {shot_id}", "artifact:{shot_id}.audio.json"),
    "studio.vfx.prepare": ("VFX prepare for {shot_id}", "artifact:{shot_id}.vfx.json"),
    "studio.voice.dub": ("Voice / dub for {shot_id}", "artifact:{shot_id}.dub.json"),
    "studio.music.trailer": ("Trailer music cue for {shot_id}", "artifact:{shot_id}.trailer_cue.json"),
    "studio.audio.sfx": ("Sound effects for {shot_id}", "artifact:{shot_id}.sfx.json"),
    "studio.asset.3d": ("3D asset for {shot_id}", "artifact:{shot_id}.asset3d.json"),
    "studio.motion.mocap": ("Motion / mocap for {shot_id}", "artifact:{shot_id}.mocap.json"),
    "studio.image.design": ("Image / design for {shot_id}", "artifact:{shot_id}.design.shape.bin"),
    "studio.model.lora": ("LoRA training for {shot_id}", "artifact:{shot_id}.lora.json"),
    "studio.dataset.prepare": ("Dataset preparation for {shot_id}", "artifact:{shot_id}.dataset.json"),
    "studio.model.pdx_specialist": ("PDX specialist traces for {shot_id}", "artifact:{shot_id}.pdx_specialist.json"),
}

_SKILL_MEDIA_ARTIFACT = {
    "studio.storyboard.first_frame": ("Storyboard first-frame for {shot_id}", "artifact:{shot_id}.first_frame.png"),
    "studio.video.generate": ("Generate video for {shot_id}", "artifact:{shot_id}.mp4"),
    "studio.video.remaster": ("Remaster video for {shot_id}", "artifact:{shot_id}.remaster.mp4"),
    "studio.audio.post": ("Audio post for {shot_id}", "artifact:{shot_id}.audio.wav"),
    "studio.vfx.prepare": ("VFX prepare for {shot_id}", "artifact:{shot_id}.vfx.mp4"),
    "studio.voice.dub": ("Voice / dub for {shot_id}", "artifact:{shot_id}.dub.wav"),
    "studio.music.trailer": ("Trailer music cue for {shot_id}", "artifact:{shot_id}.trailer_cue.wav"),
    "studio.audio.sfx": ("Sound effects for {shot_id}", "artifact:{shot_id}.sfx.wav"),
    "studio.asset.3d": ("3D asset for {shot_id}", "artifact:{shot_id}.asset3d.json"),
    "studio.motion.mocap": ("Motion / mocap for {shot_id}", "artifact:{shot_id}.mocap.json"),
    "studio.image.design": ("Image / design for {shot_id}", "artifact:{shot_id}.design.png"),
    "studio.model.lora": ("LoRA training for {shot_id}", "artifact:{shot_id}.lora.json"),
    "studio.dataset.prepare": ("Dataset preparation for {shot_id}", "artifact:{shot_id}.dataset.json"),
    "studio.model.pdx_specialist": ("PDX specialist traces for {shot_id}", "artifact:{shot_id}.pdx_specialist.json"),
}

_PIPELINES = {
    "generate": ("studio.video.generate",),
    "p0": P0_PIPELINE_TOOLS,
    "p1": P1_PIPELINE_TOOLS,
    "p2": P2_PIPELINE_TOOLS,
    "p3": P3_PIPELINE_TOOLS,
}


def resolve_pipeline_skills(
    pipeline: str,
    *,
    skills: Sequence[str] | str | None = None,
) -> tuple[str, ...]:
    """Named ladder (generate/p0–p3) or an explicit skill sequence."""
    if isinstance(skills, str):
        try:
            requested = parse_skill_list(skills)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
    elif skills:
        try:
            requested = tuple(resolve_skill_name(item) for item in skills)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if len(set(requested)) != len(requested):
            raise ValueError(f"Duplicate skills in list: {', '.join(requested)}")
    else:
        requested = ()

    if requested:
        return requested
    named = _PIPELINES.get(pipeline)
    if named is None:
        if pipeline == "custom":
            raise ValueError("pipeline 'custom' requires --skills")
        raise ValueError(
            f"Unknown pipeline '{pipeline}'. Expected 'generate', 'p0', 'p1', 'p2', 'p3', or 'custom'."
        )
    return named


def _flow_tools_for(
    pipeline: str,
    shot_id: str,
    executor_tool: str,
    *,
    skills: Sequence[str] | str | None = None,
    shape: bool = True,
) -> list[tuple[str, str, str]]:
    skill_ids = resolve_pipeline_skills(pipeline, skills=skills)
    lookup = _SKILL_ARTIFACT if shape else _SKILL_MEDIA_ARTIFACT
    rows: list[tuple[str, str, str]] = []
    for skill in skill_ids:
        title_tmpl, output_tmpl = lookup[skill]
        tool = executor_tool if skill == "studio.video.generate" else skill
        rows.append(
            (
                tool,
                title_tmpl.format(shot_id=shot_id),
                output_tmpl.format(shot_id=shot_id),
            )
        )
    return rows


def _tool_step(
    *,
    step_id: str,
    name: str,
    tool: str,
    inputs: dict[str, Any],
    depends_on: list[str],
    output: str,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "kind": "tool",
        "name": name,
        "tool": tool,
        "inputs": inputs,
        "depends_on": depends_on,
        "outputs": [output],
    }


def _transform_step(
    *,
    step_id: str,
    name: str,
    transform: str,
    inputs: dict[str, Any],
    depends_on: list[str],
    output: str,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "kind": "transform",
        "name": name,
        "transform": transform,
        "inputs": inputs,
        "depends_on": depends_on,
        "outputs": [output],
    }


def build_execution_plan(
    job_manifest: dict[str, Any],
    *,
    executor_tool: str = "studio.video.generate",
    enable_media_conformance: bool = True,
    pipeline: str = "generate",
    skills: Sequence[str] | str | None = None,
    enable_assembly: bool = False,
    shape: bool = True,
    subtitle_policy: str = "none",
    bgm_artifact: str | None = None,
    audio_policy: str = "keep",
    enable_post_source_qc: bool = False,
) -> dict[str, Any]:
    """Compile an fsf_video_job_v1 into a pdx_execution_plan_v1.

    pipeline:
      - generate: one generate (+ optional media QC) per shot (M2 default)
      - p0: storyboard → generate → remaster → audio
      - p1: P0 plus VFX → voice/dub → trailer music → SFX
      - p2: P1 plus 3D → mocap → image/design
      - p3: P2 plus LoRA → dataset → PDX specialist traces
      - custom: use `skills` (comma list or sequence). `--skills` also overrides
        a named pipeline when provided.
    shape:
      - True (default): outputs are contract-shape stubs (.shape.bin)
      - False: outputs are media filenames (.mp4, .png, etc.)
    """
    if enable_post_source_qc and not enable_media_conformance:
        raise ValueError("Post-production sources require media QC")
    source_doc = job_manifest.get("source_document", "unknown.docx")
    source_sha = job_manifest.get("source_sha256", "")
    request_id = f"req:fsf:plan:{source_doc}:{source_sha[:12]}"

    steps: list[dict[str, Any]] = []
    step_num = 1
    pipeline_skills = resolve_pipeline_skills(pipeline, skills=skills)

    # ── Pre-pass: validate consistency contexts and detect continuity cycles ──
    # Collect asset/shot ID sets and declaration order for referential checks.
    _known_asset_ids: set[str] = {
        t["id"] for t in job_manifest.get("tasks", []) if not is_shot_unit(t)
    }
    _known_shot_ids: set[str] = {
        t["id"] for t in job_manifest.get("tasks", []) if is_shot_unit(t)
    }
    _shot_order: list[str] = [
        t["id"] for t in job_manifest.get("tasks", []) if is_shot_unit(t)
    ]
    # Collect authoritative asset_spec for each asset task (used for content_hash verification).
    _asset_specs_by_id: dict[str, dict[str, Any]] = {
        t["id"]: t["asset_spec"]
        for t in job_manifest.get("tasks", [])
        if not is_shot_unit(t) and isinstance(t.get("asset_spec"), dict)
    }

    # Collect contexts from all shot tasks that carry one.
    # Use `is not None` so that empty dicts ({}) are also validated rather than skipped.
    _shot_contexts: dict[str, dict[str, Any]] = {}
    for _task in job_manifest.get("tasks", []):
        _ctx = _task.get("consistency_context")
        if _ctx is None or not is_shot_unit(_task):
            continue
        # Type check before adding to _shot_contexts — non-dict values cannot
        # participate in cycle detection and must be rejected immediately.
        if not isinstance(_ctx, dict) or not _ctx:
            raise ConsistencyCompileError(
                f"Shot '{_task['id']}': consistency_context must be a non-empty dict, "
                f"got {type(_ctx).__name__!r}"
            )
        _shot_contexts[_task["id"]] = _ctx

    if _shot_contexts:
        # 1. Global cycle check (fast, before per-shot validation).
        cycle = detect_continuity_cycles(_shot_contexts)
        if cycle:
            raise ConsistencyCompileError(
                f"Continuity cycle detected among shots: {cycle}. "
                f"continues_from declarations must form a DAG."
            )
        # 2. Full per-shot validation: schema + type check + asset refs + content
        #    hashes + continuity + prompt consistency + effective_prompt guard.
        #    This catches injected raw contexts that bypass the builder.
        for _task in job_manifest.get("tasks", []):
            _ctx = _task.get("consistency_context")
            if _ctx is None or not is_shot_unit(_task):
                continue
            validate_context_in_plan(
                _ctx,
                shot_id=_task["id"],
                shot_spec=_task.get("shot_spec", {}),
                known_asset_ids=_known_asset_ids,
                known_shot_ids=_known_shot_ids,
                shot_order=_shot_order,
                asset_specs_by_id=_asset_specs_by_id,
            )

    # Maps shot_id → first step_id (for continuity depends_on insertion, post-pass).
    _shot_first_step: dict[str, str] = {}
    # (downstream_step_id, source_shot_id) pairs for post-pass.
    _continuity_deps: list[tuple[str, str]] = []

    for task in job_manifest.get("tasks", []):
        shot_id = task["id"]
        is_shot = is_shot_unit(task)
        last_id: str | None = None

        flow_tools = _flow_tools_for(pipeline, shot_id, executor_tool, skills=skills, shape=shape)

        for tool, title, output in flow_tools:
            if not skill_applies_to_task(tool, task):
                continue
            step_id = f"step_{step_num:03d}_{shot_id}_{tool.rsplit('.', 1)[-1]}"
            depends = [last_id] if last_id else []
            step_inputs: dict[str, Any] = {
                "unit_kind": "shot" if is_shot else "asset",
                "shot_id": shot_id,
                "source_document": source_doc,
                "source_sha256": source_sha,
            }
            if is_shot:
                step_inputs["shot_spec"] = task.get("shot_spec", {})
                # Attach a deep copy of consistency_context — protects the plan
                # from external mutations to the original task dict.
                ctx = task.get("consistency_context")
                if ctx is not None:
                    step_inputs["consistency_context"] = copy.deepcopy(ctx)
            else:
                step_inputs["asset_spec"] = task.get("asset_spec", {})

            # Record first step per shot; queue continuity dep for post-pass.
            if shot_id not in _shot_first_step:
                _shot_first_step[shot_id] = step_id
                if is_shot:
                    ctx = task.get("consistency_context")
                    if ctx and ctx.get("continues_from"):
                        src_shot = ctx["continues_from"]["shot_id"]
                        _continuity_deps.append((step_id, src_shot))

            steps.append(
                _tool_step(
                    step_id=step_id,
                    name=title,
                    tool=tool,
                    inputs=step_inputs,
                    depends_on=depends,
                    output=output,
                )
            )
            last_id = step_id
            step_num += 1

            if enable_media_conformance and (tool == "studio.video.generate" or enable_post_source_qc and tool in ("studio.video.remaster", "studio.video.vfx")):
                conf_step_id = f"step_{step_num:03d}_qc_{shot_id}"
                expected_contract = dict(expected_contract_for_task(task))

                # Align source shot QC with audio_policy ONLY for intake defaults.
                # Explicit contracts (including audio:"forbidden") keep their authority.
                is_intake_default = task.get("expected_media_contract_source") == "intake_default"
                explicit_audio = task.get("audio") or (
                    task.get("shot_spec", {}).get("audio")
                    if isinstance(task.get("shot_spec"), dict)
                    else None
                )
                if is_intake_default and not explicit_audio:
                    norm_audio = (audio_policy or "keep").lower()
                    if norm_audio == "keep":
                        expected_contract["audio"] = "allowed"

                expected_digest = canonical_contract_digest(expected_contract)
                steps.append(
                    _tool_step(
                        step_id=conf_step_id,
                        name=f"Media technical conformance for {shot_id}",
                        tool="pdx.media.technical_conform",
                        inputs={
                            "unit_kind": "shot",
                            "shot_id": shot_id,
                            "target_artifact": output,
                            "duration_seconds": float(
                                expected_contract.get(
                                    "duration_seconds", task.get("duration_seconds", 5.0)
                                )
                            ),
                            "expected_media_contract": expected_contract,
                            "expected_media_contract_digest": expected_digest,
                        },
                        depends_on=[step_id],
                        output=f"report:{shot_id}_conformance.json",
                    )
                )
                last_id = conf_step_id
                step_num += 1

    # ── Post-pass: insert continuity depends_on edges ────────────────────────
    # For each shot that declares continues_from, the shot's first step must
    # wait for the last step of the source shot. We insert this edge here so
    # that the original step-building loop stays simple.
    if _continuity_deps:
        # Build a map of step_id → step dict for O(1) lookup.
        _steps_by_id: dict[str, dict[str, Any]] = {s["id"]: s for s in steps}
        # Build a map of shot_id → last step_id (terminal step per shot).
        _shot_last_step: dict[str, str] = {}
        for _s in steps:
            _sid = _s.get("inputs", {}).get("shot_id")
            if _sid:
                _shot_last_step[_sid] = _s["id"]

        for downstream_step_id, src_shot_id in _continuity_deps:
            src_terminal = _shot_last_step.get(src_shot_id)
            if src_terminal is None:
                raise ConsistencyCompileError(
                    f"Cannot insert continuity dependency: source shot '{src_shot_id}' "
                    f"has no steps in this plan."
                )
            target_step = _steps_by_id.get(downstream_step_id)
            if target_step is not None:
                existing = target_step.get("depends_on", [])
                if src_terminal not in existing:
                    target_step["depends_on"] = existing + [src_terminal]

    if enable_assembly:
        shot_tasks = [t for t in job_manifest.get("tasks", []) if is_shot_unit(t)]
        if shot_tasks:
            total_dur = sum(
                float(
                    expected_contract_for_task(t).get(
                        "duration_seconds", t.get("duration_seconds", 5.0)
                    )
                )
                for t in shot_tasks
            )
            shot_terminal_steps: dict[str, str] = {}
            for s in steps:
                sid = s.get("inputs", {}).get("shot_id")
                if sid:
                    shot_terminal_steps[sid] = s["id"]
            assembly_depends = list(shot_terminal_steps.values())

            # Compile-time artifact routing: prefer Remaster, fallback to Generate
            has_remaster = "studio.video.remaster" in pipeline_skills
            has_dub = "studio.voice.dub" in pipeline_skills

            # Build a per-shot index of the step that produces the video artifact
            # (generate or remaster).  This EXCLUDES QC, Dub, and other downstream
            # steps that happen to reference the same shot_id but do NOT produce the
            # video file — using shot_terminal_steps would incorrectly point to the
            # last such step (e.g. Dub or QC) whose output_files has no .mp4.
            _video_producer_tools = frozenset(
                {"studio.video.remaster"} if has_remaster else {executor_tool}
            )
            shot_video_step: dict[str, str] = {}
            for s in steps:
                sid = s.get("inputs", {}).get("shot_id")
                if sid and s.get("tool") in _video_producer_tools:
                    shot_video_step[sid] = s["id"]
            artifact_inputs: list[dict[str, Any]] = []
            shot_durations: dict[str, float] = {}
            for t in shot_tasks:
                sid = t["id"]
                dur = float(
                    expected_contract_for_task(t).get(
                        "duration_seconds", t.get("duration_seconds", 5.0)
                    )
                )
                shot_durations[sid] = dur
                if has_remaster:
                    art_file = f"{sid}.remaster.shape.bin" if shape else f"{sid}.remaster.mp4"
                    role = "video_remaster"
                    producer = "studio.video.remaster"
                else:
                    art_file = f"{sid}.shape.bin" if shape else f"{sid}.mp4"
                    role = "video_generate"
                    producer = executor_tool

                item: dict[str, Any] = {
                    "shot_id": sid,
                    "role": role,
                    "artifact": f"artifact:{art_file}",
                    "producer_tool": producer,
                    # producer_step_id is resolved at compile time from the step DAG so
                    # _resolve_assembly_inputs can look up the exact producer step without
                    # any step_id substring guessing.
                    "producer_step_id": shot_video_step.get(sid, ""),
                    "duration_seconds": dur,
                }
                if has_dub:
                    dub_file = f"{sid}.dub.json" if shape else f"{sid}.dub.wav"
                    item["audio_artifact"] = f"artifact:{dub_file}"
                artifact_inputs.append(item)

            subtitles_list: list[dict[str, Any]] = []
            if subtitle_policy != "none":
                cur_t = 0.0
                for t in shot_tasks:
                    dur = shot_durations[t["id"]]
                    text = str(
                        t.get("dialogue")
                        or t.get("prompt")
                        or t.get("shot_spec", {}).get("source_prompt", "")
                    ).strip()
                    if text:
                        subtitles_list.append({
                            "shot_id": t["id"],
                            "text": text,
                            "start_seconds": cur_t + 0.1,
                            "end_seconds": cur_t + max(0.2, dur - 0.1),
                        })
                    cur_t += dur

            first_shot_expected = expected_contract_for_task(shot_tasks[0])
            norm_audio = (audio_policy or "keep").lower()
            if norm_audio in ("strip", "forbidden"):
                audio_expected = "required" if bgm_artifact else "forbidden"
            elif norm_audio in ("require", "required"):
                audio_expected = "required"
            else:  # "keep"
                audio_expected = "required" if (bgm_artifact or has_dub) else "allowed"

            assembled_expected = build_expected_contract(
                {"duration_seconds": total_dur},
                width=first_shot_expected.get("width", 1024),
                height=first_shot_expected.get("height", 576),
                fps=first_shot_expected.get("fps", 24),
                audio=audio_expected,
            )
            assembled_digest = canonical_contract_digest(assembled_expected)

            transform_inputs: dict[str, Any] = {
                "source_document": source_doc,
                "source_sha256": source_sha,
                "shot_ids": [t["id"] for t in shot_tasks],
                "target_duration_seconds": total_dur,
                "artifact_inputs": artifact_inputs,
                "shot_durations": shot_durations,
                "width": assembled_expected["width"],
                "height": assembled_expected["height"],
                "fps": assembled_expected["fps"],
                "audio_policy": norm_audio,
                "expected_media_contract": assembled_expected,
                "expected_media_contract_digest": assembled_digest,
                "subtitle_policy": subtitle_policy,
            }
            if subtitles_list:
                transform_inputs["subtitles"] = subtitles_list
            if bgm_artifact:
                transform_inputs["bgm_artifact"] = bgm_artifact

            assemble_step_id = f"step_{step_num:03d}_assembly_concat"
            steps.append(
                _transform_step(
                    step_id=assemble_step_id,
                    name="Fast Assembly timeline concatenation",
                    transform="fsf.assembly.fast_concat",
                    inputs=transform_inputs,
                    depends_on=assembly_depends,
                    output="artifact:assembled_timeline.mp4",
                )
            )
            step_num += 1

            if enable_media_conformance:
                assembled_qc_id = f"step_{step_num:03d}_qc_assembled_timeline"
                steps.append(
                    _tool_step(
                        step_id=assembled_qc_id,
                        name="Media technical conformance for assembled timeline",
                        tool="pdx.media.technical_conform",
                        inputs={
                            "unit_kind": "shot",
                            "target_artifact": "artifact:assembled_timeline.mp4",
                            "duration_seconds": total_dur,
                            "expected_media_contract": assembled_expected,
                            "expected_media_contract_digest": assembled_digest,
                        },
                        depends_on=[assemble_step_id],
                        output="report:assembled_timeline_conformance.json",
                    )
                )
                step_num += 1

    plan = {
        "schema_version": "pdx_execution_plan_v1",
        "request_id": request_id,
        "producer": {
            "type": "fsf_planner",
            "name": "fsf.runtime.plan",
        },
        "steps": steps,
    }

    errors = validate_execution_plan(plan)
    if errors:
        raise ValueError("Invalid pdx_execution_plan_v1:\n" + "\n".join(errors))

    return plan
