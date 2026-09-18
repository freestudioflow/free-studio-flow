"""Tests for FSF-S P2 assets and P3 model/data Studio flows."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.cli.main import main
from fsf.intake.compiler import compile_job
from fsf.runtime.plan import build_execution_plan
from fsf.studio.catalog import list_studio_flows
from fsf.studio.runner import run_studio_flow

P2_SKILLS = [
    "studio.asset.3d",
    "studio.motion.mocap",
    "studio.image.design",
]

P3_SKILLS = [
    "studio.model.lora",
    "studio.dataset.prepare",
    "studio.model.pdx_specialist",
]


def test_p2_p3_catalog_implemented() -> None:
    flows = list_studio_flows()
    assert all(item["status"] == "implemented" for item in flows)
    assert [item["skill"] for item in flows if item["phase"] == "P2"] == P2_SKILLS
    assert [item["skill"] for item in flows if item["phase"] == "P3"] == P3_SKILLS
    for item in flows:
        for transport in ("kaggle", "api", "local"):
            assert item["transports"][transport]["status"] == "shape"


def test_p2_p3_transports_do_not_mutate_source(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, profile = compile_job(sample_docx)
    task = manifest["tasks"][0]
    original_prompt = task["shot_spec"]["source_prompt"]
    original_sha = task["shot_spec"]["provenance"]["source_sha256"]
    assert original_sha == profile["source"]["sha256"]

    for skill in P2_SKILLS + P3_SKILLS:
        for transport in ("kaggle", "api", "local"):
            out = tmp_path / skill.replace(".", "_") / transport
            results = run_studio_flow(skill, [task], out, transport=transport)
            assert results[0]["schema_version"] == "pdx_tool_result_v1"
            assert task["shot_spec"]["source_prompt"] == original_prompt
            assert task["shot_spec"]["provenance"]["source_sha256"] == original_sha
            assert original_prompt not in json.dumps(results[0]["outputs"])


def test_lora_records_kaggle_plan_ref(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    out = tmp_path / "lora"
    run_studio_flow("studio.model.lora", [manifest["tasks"][0]], out, transport="kaggle")
    payload = (out / "task_001.lora.json").read_text(encoding="utf-8")
    assert "unresolved:engine_kaggle_plan" in payload


def test_p2_p3_pipeline_plans(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    p2 = build_execution_plan(manifest, pipeline="p2")
    p3 = build_execution_plan(manifest, pipeline="p3")
    p2_tools = {step["tool"] for step in p2["steps"]}
    p3_tools = {step["tool"] for step in p3["steps"]}
    for skill in P2_SKILLS:
        assert skill in p2_tools
    for skill in P2_SKILLS + P3_SKILLS:
        assert skill in p3_tools
    # 3 shots * (11 studio + 1 QC) ; 3 shots * (14 studio + 1 QC)
    assert len(p2["steps"]) == 36
    assert len(p3["steps"]) == 45


def test_cli_p2_p3_flow_run(sample_docx: Path, tmp_path: Path) -> None:
    out_3d = tmp_path / "asset3d"
    assert main(
        [
            "flow",
            "run",
            "studio.asset.3d",
            str(sample_docx),
            "--out",
            str(out_3d),
            "--transport",
            "local",
            "--shape",
        ]
    ) == 0
    assert (out_3d / "task_001.asset3d.json").is_file()

    out_lora = tmp_path / "lora"
    assert main(
        [
            "flow",
            "run",
            "studio.model.lora",
            str(sample_docx),
            "--out",
            str(out_lora),
            "--transport",
            "kaggle",
            "--shape",
        ]
    ) == 0
    assert (out_lora / "task_001.lora.json").is_file()
