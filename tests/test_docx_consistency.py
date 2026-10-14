"""DOCX consistency declarations and CLI compilation regression coverage."""

import json
import subprocess
import sys

import pytest
from docx import Document

from fsf.core.consistency import canonical_asset_digest
from fsf.intake.compiler import compile_job_from_profile
from fsf.runtime.plan import build_execution_plan


def profile(*blocks):
    texts = []
    for block in blocks:
        if texts:
            texts.append("-----")
        texts.extend(block)
    return {
        "schema_version": "prodocux_docx_profile_v1",
        "source": {"name": "sheet.docx", "sha256": "a" * 64},
        "paragraphs": [{"index": i, "text": t} for i, t in enumerate(texts, 1)],
    }


def test_assets_continuity_and_prompt_integrity():
    job = compile_job_from_profile(profile(
        ["ASSET: Silver coat", "ID: hero"],
        ["SHOT: A wide view. 2 seconds", "ID: T01", "ASSET_REFS: hero"],
        ["SHOT: A close view. 2 seconds", "ID: T02", "CONTINUITY: T01",
         "CONTINUITY_ASPECTS: appearance,costume", "REVIEW_REQUIRED: false"],
    ))
    asset, first, second = job["tasks"]
    ctx = first["consistency_context"]
    assert first["prompt"] == "A wide view. 2 seconds"
    assert first["shot_spec"]["generation_prompt"] == first["prompt"]
    assert first["duration_seconds"] == 2
    spec = asset["asset_spec"]
    assert ctx["asset_refs"][0]["content_hash"] == canonical_asset_digest(
        "hero", "1", spec["source_prompt"], spec["kind"], spec["constraints"], [],
    )
    assert ctx["generation_intent"]["asset_injections"][0]["injected_text"] == "Silver coat"
    assert ctx["review_required"] is True
    assert second["consistency_context"]["review_required"] is False
    plan = build_execution_plan(job, pipeline="generate", shape=False, enable_assembly=True)
    steps = [s for s in plan["steps"] if s.get("inputs", {}).get("consistency_context")]
    assert len(steps) == 2
    assert steps[1]["inputs"]["consistency_context"]["continues_from"]["shot_id"] == "T01"
    assert steps[1]["depends_on"]


@pytest.mark.parametrize("blocks", [
    [["SHOT: A", "ID: X", "ID: Y"]],
    [["SHOT: A", "ID: ../X"]],
    [["SHOT: A", "ID: X"], ["SHOT: B", "ID: X"]],
    [["SHOT: A", "ASSET_REFS: missing"]],
    [["SHOT: A", "CONTINUITY: missing"]],
    [["SHOT: A", "ID: X", "CONTINUITY: X"]],
    [["SHOT: A", "CONTINUITY: T02"], ["SHOT: B", "ID: T02"]],
    [["SHOT: A", "CONTINUITY_ASPECTS: costume"]],
    [["SHOT: A", "REVIEW_REQUIRED: yes"]],
    [["ASSET: A", "REVIEW_REQUIRED: true"]],
    [["SHOT: A", "ASSET_REFS:"]],
    [["SHOT:", "REVIEW_REQUIRED: true"]],
    [["SHOT: A", "ID: X"], ["SHOT: B", "CONTINUITY: X", "CONTINUITY_ASPECTS: unknown"]],
])
def test_invalid_declarations_rejected(blocks):
    with pytest.raises(ValueError):
        compile_job_from_profile(profile(*blocks))


def test_untagged_sheet_unchanged():
    job = compile_job_from_profile(profile(["SHOT: Line one\nLine two. 3 seconds"]))
    task = job["tasks"][0]
    assert task["id"] == "task_001"
    assert task["prompt"] == "Line one\nLine two. 3 seconds"
    assert "consistency_context" not in task


def test_cli_docx_intake_and_media_plan(tmp_path):
    doc = Document()
    for line in ["SHOT: Wide shot. 2 seconds", "ID: T01", "REVIEW_REQUIRED: true",
                 "-----", "SHOT: Close shot. 2 seconds", "ID: T02", "CONTINUITY: T01"]:
        doc.add_paragraph(line)
    sheet = tmp_path / "sheet.docx"
    doc.save(sheet)
    intake = tmp_path / "intake"
    plan_path = tmp_path / "plan.json"
    for args in [
        ["intake", str(sheet), "--out", str(intake)],
        ["plan", str(sheet), "--media", "--assembly", "--out", str(plan_path)],
    ]:
        res = subprocess.run([sys.executable, "-m", "fsf.cli", *args], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
    manifest = json.loads((intake / "job_manifest.json").read_text())
    assert manifest["tasks"][1]["consistency_context"]["continues_from"]["shot_id"] == "T01"
    plan = json.loads(plan_path.read_text())
    assert any(s["kind"] == "transform" for s in plan["steps"])
    assert any(s.get("inputs", {}).get("consistency_context", {}).get("review_required") for s in plan["steps"])
