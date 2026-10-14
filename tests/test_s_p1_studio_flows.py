"""Tests for FSF-S P1 Studio flows: VFX, voice, trailer music, SFX."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.cli.main import main
from fsf.intake.compiler import compile_job
from fsf.runtime.plan import build_execution_plan
from fsf.studio.catalog import list_studio_flows
from fsf.studio.runner import run_studio_flow

P1_SKILLS = [
    "studio.vfx.prepare",
    "studio.voice.dub",
    "studio.music.trailer",
    "studio.audio.sfx",
]


def test_p1_catalog_implemented() -> None:
    p1 = [item for item in list_studio_flows() if item["phase"] == "P1"]
    assert [item["skill"] for item in p1] == P1_SKILLS
    assert all(item["status"] == "implemented" for item in p1)
    for item in p1:
        for transport in ("kaggle", "api", "local"):
            assert item["transports"][transport]["status"] == "shape"


def test_p1_transports_do_not_mutate_source(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, profile = compile_job(sample_docx)
    task = dict(manifest["tasks"][0])
    task["rights_policy"] = "talent_consent_on_file"
    task["cue_seconds"] = 30
    original_prompt = task["shot_spec"]["source_prompt"]
    original_sha = task["shot_spec"]["provenance"]["source_sha256"]
    assert original_sha == profile["source"]["sha256"]

    for skill in P1_SKILLS:
        for transport in ("kaggle", "api", "local"):
            out = tmp_path / skill.replace(".", "_") / transport
            results = run_studio_flow(skill, [task], out, transport=transport)
            assert results[0]["schema_version"] == "pdx_tool_result_v1"
            assert task["shot_spec"]["source_prompt"] == original_prompt
            assert task["shot_spec"]["provenance"]["source_sha256"] == original_sha
            dumped = json.dumps(results[0]["outputs"])
            assert original_prompt not in dumped


def test_voice_records_unresolved_rights_policy(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    task = manifest["tasks"][0]
    out = tmp_path / "dub"
    run_studio_flow("studio.voice.dub", [task], out, transport="local")
    payload = (out / "task_001.dub.json").read_text(encoding="utf-8")
    assert "unresolved:consent_and_rights_policy" in payload


def test_trailer_music_defaults_to_30s_cue(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    out = tmp_path / "cue"
    run_studio_flow("studio.music.trailer", [manifest["tasks"][0]], out, transport="local")
    payload = (out / "task_001.trailer_cue.json").read_text(encoding="utf-8")
    assert "cue_seconds=30" in payload


def test_p1_pipeline_plan(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, pipeline="p1")
    tools = [step["tool"] for step in plan["steps"]]
    for skill in P1_SKILLS:
        assert skill in tools
    assert "studio.video.generate" in tools
    # 3 shots * (8 studio tools + 1 media QC after generate)
    assert len(plan["steps"]) == 27


def test_cli_p1_flow_run(sample_docx: Path, tmp_path: Path) -> None:
    out = tmp_path / "vfx"
    rc = main(
        [
            "flow",
            "run",
            "studio.vfx.prepare",
            str(sample_docx),
            "--out",
            str(out),
            "--transport",
            "local",
            "--shape",
        ]
    )
    assert rc == 0
    assert (out / "task_001.vfx.json").is_file()
