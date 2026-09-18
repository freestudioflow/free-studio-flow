"""FSF Runtime & Execution Plan compiler (FSF-M2 + FSF-S P0 pipeline).

Compiles FSF Video Jobs into PDX Execution Plans (pdx_execution_plan_v1),
providing deterministic stage sequencing for generation and Studio flows.
"""

from __future__ import annotations

from typing import Any, Sequence

from pdx_artifact_core import validate_execution_plan

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
) -> list[tuple[str, str, str]]:
    skill_ids = resolve_pipeline_skills(pipeline, skills=skills)
    rows: list[tuple[str, str, str]] = []
    for skill in skill_ids:
        title_tmpl, output_tmpl = _SKILL_ARTIFACT[skill]
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


def build_execution_plan(
    job_manifest: dict[str, Any],
    *,
    executor_tool: str = "studio.video.generate",
    enable_media_conformance: bool = True,
    pipeline: str = "generate",
    skills: Sequence[str] | str | None = None,
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
    """
    source_doc = job_manifest.get("source_document", "unknown.docx")
    source_sha = job_manifest.get("source_sha256", "")
    request_id = f"req:fsf:plan:{source_doc}:{source_sha[:12]}"

    steps: list[dict[str, Any]] = []
    step_num = 1

    for task in job_manifest.get("tasks", []):
        shot_id = task["id"]
        shot_spec = task.get("shot_spec", {})
        last_id: str | None = None

        flow_tools = _flow_tools_for(pipeline, shot_id, executor_tool, skills=skills)

        for tool, title, output in flow_tools:
            step_id = f"step_{step_num:03d}_{shot_id}_{tool.rsplit('.', 1)[-1]}"
            depends = [last_id] if last_id else []
            steps.append(
                _tool_step(
                    step_id=step_id,
                    name=title,
                    tool=tool,
                    inputs={
                        "shot_id": shot_id,
                        "shot_spec": shot_spec,
                        "source_document": source_doc,
                        "source_sha256": source_sha,
                    },
                    depends_on=depends,
                    output=output,
                )
            )
            last_id = step_id
            step_num += 1

            if enable_media_conformance and tool == "studio.video.generate":
                conf_step_id = f"step_{step_num:03d}_qc_{shot_id}"
                steps.append(
                    _tool_step(
                        step_id=conf_step_id,
                        name=f"Media technical conformance for {shot_id}",
                        tool="pdx.media.technical_conform",
                        inputs={
                            "shot_id": shot_id,
                            "target_artifact": output,
                            "duration_seconds": task.get("duration_seconds", 5.0),
                        },
                        depends_on=[step_id],
                        output=f"report:{shot_id}_conformance.json",
                    )
                )
                last_id = conf_step_id
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
