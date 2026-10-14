"""Tests for FSF-S P0 Studio flows: catalog, three transports, CLI, no UI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fsf.cli.main import main
from fsf.intake.compiler import compile_job
from fsf.runtime.plan import build_execution_plan
from fsf.studio.catalog import list_studio_flows
from fsf.studio.runner import run_studio_flow


def test_catalog_has_fourteen_flows() -> None:
    flows = list_studio_flows()
    assert len(flows) == 14
    assert {item["index"] for item in flows} == set(range(1, 15))
    p0 = [item for item in flows if item["phase"] == "P0"]
    assert [item["skill"] for item in p0] == [
        "studio.video.generate",
        "studio.video.remaster",
        "studio.storyboard.first_frame",
        "studio.audio.post",
    ]
    assert all(item["status"] == "implemented" for item in p0)
    assert all(item["status"] == "implemented" for item in list_studio_flows())
    for item in p0:
        for transport in ("kaggle", "api", "local"):
            assert item["transports"][transport]["status"] == "shape"


def test_p0_transports_do_not_mutate_source(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, profile = compile_job(sample_docx)
    task = manifest["tasks"][0]
    original_prompt = task["shot_spec"]["source_prompt"]
    original_sha = task["shot_spec"]["provenance"]["source_sha256"]
    assert original_sha == profile["source"]["sha256"]

    skills = [
        "studio.video.generate",
        "studio.video.remaster",
        "studio.storyboard.first_frame",
        "studio.audio.post",
    ]
    for skill in skills:
        for transport in ("kaggle", "api", "local"):
            out = tmp_path / skill.replace(".", "_") / transport
            results = run_studio_flow(skill, [task], out, transport=transport)
            assert results[0]["schema_version"] == "pdx_tool_result_v1"
            assert task["shot_spec"]["source_prompt"] == original_prompt
            assert task["shot_spec"]["provenance"]["source_sha256"] == original_sha
            assert "source_prompt" not in json.dumps(results[0]["outputs"])


def test_unknown_flow_refuses_run(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    with pytest.raises(KeyError, match="Unknown Studio flow"):
        run_studio_flow("studio.not.a.skill", manifest["tasks"], tmp_path, transport="local")


def test_p0_pipeline_plan(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, pipeline="p0")
    tools = [step["tool"] for step in plan["steps"]]
    assert "studio.storyboard.first_frame" in tools
    assert "studio.video.generate" in tools
    assert "studio.video.remaster" in tools
    assert "studio.audio.post" in tools
    assert "pdx.media.technical_conform" in tools
    # 3 shots * (4 studio tools + 1 media QC after generate)
    assert len(plan["steps"]) == 15


def test_cli_flows_and_flow_run(sample_docx: Path, tmp_path: Path, capsys) -> None:
    assert main(["flows"]) == 0
    listed = capsys.readouterr().out
    assert "studio.video.remaster" in listed
    assert "studio.vfx.prepare" in listed
    assert "studio.asset.3d" in listed
    assert "kaggle=shape" in listed
    assert "[implemented]" in listed

    out = tmp_path / "remaster"
    rc = main(
        [
            "flow",
            "run",
            "studio.video.remaster",
            str(sample_docx),
            "--out",
            str(out),
            "--transport",
            "local",
            "--shape",
        ]
    )
    assert rc == 0
    assert (out / "task_001.remaster.shape.bin").is_file()

    rc_unknown = main(
        [
            "flow",
            "run",
            "studio.not.a.skill",
            str(sample_docx),
            "--out",
            str(tmp_path / "vfx"),
            "--transport",
            "local",
        ]
    )
    assert rc_unknown == 1


def test_custom_skill_pipeline_plan(sample_docx: Path, tmp_path: Path, capsys) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(
        manifest,
        pipeline="custom",
        skills="storyboard,generate,dub",
    )
    tools = [step["tool"] for step in plan["steps"]]
    assert tools.count("studio.storyboard.first_frame") == 3
    assert tools.count("studio.video.generate") == 3
    assert tools.count("studio.voice.dub") == 3
    assert "studio.vfx.prepare" not in tools
    assert "pdx.media.technical_conform" in tools

    with pytest.raises(ValueError, match="custom"):
        build_execution_plan(manifest, pipeline="custom")
    with pytest.raises(ValueError, match="Unknown Studio flow"):
        build_execution_plan(manifest, pipeline="custom", skills="not-a-skill")

    out = tmp_path / "custom-plan.json"
    rc = main(
        [
            "plan",
            str(sample_docx),
            "--pipeline",
            "custom",
            "--skills",
            "generate,dub",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "studio.voice.dub" in {step["tool"] for step in payload["steps"]}
    assert "studio.video.remaster" not in {step["tool"] for step in payload["steps"]}
    capsys.readouterr()
