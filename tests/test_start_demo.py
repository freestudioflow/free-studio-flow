"""START03/04 CLI and fail-closed job-media regressions."""
import json
import shutil
import subprocess

import pytest

from fsf.cli.main import main
from fsf.conformance.digest import canonical_contract_digest
from fsf.runtime.blender import sha
from fsf.runtime.job_media import local_media_tools


def test_demo_and_verify_job_cli(tmp_path, capsys):
    root = tmp_path / "synthetic 影片"
    demo = ["demo","--out",str(root)]
    assert main(demo) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["media_kind"] == "synthetic"
    assert not first["reused"] and first["evaluation_status"] == "conforms"
    assert (root/"sample.docx").is_file() and (root/"assembly_receipt.json").is_file()
    artifact = root/first["artifact"]
    assert sha(artifact) == first["sha256"]
    assert main(demo) == 0
    reused = json.loads(capsys.readouterr().out)
    assert reused == {**first,"reused":True}
    relocated = tmp_path/"moved"
    shutil.move(root,relocated)
    verify = ["verify","--job",str(relocated)]
    assert main(verify) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sha256"] == first["sha256"]
    assert result["contract_digest"] == first["contract_digest"]
    plan = relocated/"plan.json"
    document = json.loads(plan.read_bytes())
    shot = next(s["inputs"]["target_artifact"].removeprefix("artifact:")
                for s in document["steps"] if s.get("tool")=="pdx.media.technical_conform")
    assert main([*verify,"--artifact",shot]) == 0
    assert json.loads(capsys.readouterr().out)["artifact"] == shot

    plan_copy = relocated/"external-plan.json"
    plan.rename(plan_copy)
    assert main([*verify,"--plan",str(plan_copy)]) == 0
    capsys.readouterr()
    plan_copy.rename(plan)
    original_plan = plan.read_bytes()
    shutil.copyfile(plan,tmp_path/"other-plan.json")
    document["request_id"] += "-foreign"
    (tmp_path/"other-plan.json").write_text(json.dumps(document))
    assert main([*verify,"--plan",str(tmp_path/"other-plan.json")]) == 1
    assert "JOB_PLAN_MISMATCH" in capsys.readouterr().err
    assert plan.read_bytes() == original_plan
    assert main([*verify,"--artifact","../outside.mp4"]) == 1
    assert main([*verify,"--artifact","missing.mp4"]) == 1
    assert main([*verify,"--expected","{}"]) == 2
    capsys.readouterr()
    journal = relocated/"fsf_execution_journal.json"
    original_journal = journal.read_bytes()
    state = json.loads(original_journal)
    assembly = next(s for s in document["steps"] if s.get("transform") == "fsf.assembly.fast_concat")
    document["request_id"] = json.loads(original_plan)["request_id"]
    document["steps"].append({**assembly, "id":"alternate", "outputs":["artifact:alternate.mp4"]})
    plan.write_text(json.dumps(document),encoding="utf-8")
    state["plan_digest"] = canonical_contract_digest(document)
    journal.write_text(json.dumps(state),encoding="utf-8")
    assert main(verify) == 1
    assert "JOB_ARTIFACT_SELECTION_REQUIRED" in capsys.readouterr().err
    plan.write_bytes(original_plan)
    journal.write_bytes(original_journal)
    final = relocated/first["artifact"]
    final.write_bytes(b"changed")
    assert main(verify) == 1
    assert "JOB_ARTIFACT_CHANGED" in capsys.readouterr().err


@pytest.mark.parametrize("clock,duration,error",[(24,1,"MEDIA_DURATION_MISMATCH"),(30,4,"MEDIA_FPS_MISMATCH")])
def test_job_reports_wrong_media_against_recorded_contract(tmp_path, capsys, clock, duration, error):
    root = tmp_path/"job"
    assert main(["demo","--out",str(root)]) == 0
    demo = json.loads(capsys.readouterr().out)
    artifact = root/demo["artifact"]
    with local_media_tools() as ffmpeg:
        subprocess.run([ffmpeg,"-nostdin","-y","-loglevel","error","-f","lavfi",
            "-i",f"testsrc2=size=1024x576:rate={clock}","-t",str(duration),
            "-threads","2","-c:v","libx264","-an",str(artifact)],check=True)
    # A completed job may itself have a bad output. Simulate its recorded
    # artifact identity so technical conformance, rather than SHA, rejects it.
    journal = root/"fsf_execution_journal.json"
    state = json.loads(journal.read_bytes())
    for step in state["steps"].values():
        for row in step.get("output_files",[]):
            if row["path"] == demo["artifact"]:
                row["sha256"] = sha(artifact)
    journal.write_text(json.dumps(state),encoding="utf-8")
    before = journal.read_bytes()
    assert main(["verify","--job",str(root)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["evaluation_status"] == "does_not_conform"
    assert error in {item["code"] for item in output["report"]["issues"]}
    assert journal.read_bytes() == before


def test_demo_preserves_existing_output_and_reports_missing_ffmpeg(tmp_path, capsys, monkeypatch):
    old = tmp_path/"existing"
    old.mkdir()
    marker = old/"keep.txt"
    marker.write_text("keep")
    assert main(["demo","--out",str(old)]) == 1
    assert marker.read_text() == "keep"
    import fsf.runtime.job_media as media
    monkeypatch.setattr(media.shutil,"which",lambda *args,**kw:None)
    empty = tmp_path/"absent"
    assert main(["demo","--out",str(empty)]) == 1
    assert "FFMPEG_REQUIRED" in capsys.readouterr().err
    assert not empty.exists()


def test_demo_cancelled_attempt_is_cleaned(tmp_path, capsys, monkeypatch):
    from fsf.runtime.plan_executor import PlanExecutor
    def cancel(*_):
        raise KeyboardInterrupt
    monkeypatch.setattr(PlanExecutor,"execute_plan",cancel)
    out = tmp_path/"cancelled"
    assert main(["demo","--out",str(out)]) == 130
    assert "Cancelled" in capsys.readouterr().err
    assert not out.exists()


def test_demo_handoff_closed_synthetic_cycle_and_verify_dir_hint(tmp_path, capsys):
    out = tmp_path / "handoff-demo"
    assert main(["demo", "--handoff", "--out", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["demo_mode"] == "handoff"
    assert payload["media_kind"] == "synthetic"
    assert payload["review_authority"] == "synthetic_demo_only_not_for_production"
    assert payload["synthetic_roles"] == ["synthetic_worker", "synthetic_demo_reviewer"]
    assert payload["evaluation_status"] == "conforms"
    assert not payload["reused"]
    trace = json.loads((out / "demo-handoff-trace.json").read_text(encoding="utf-8"))
    stages = trace["stages"]
    assert stages["initial_dependency_block"] == "DEPENDENCY_PENDING"
    assert stages["rejected_attempt"] == 1
    assert stages["retried_attempt"] == 2
    assert stages["continuity_input"] == "T01.mp4"
    assert stages["pre_review_assembly_blocker"] == "REVIEW_REQUIRED"
    assert stages["tamper_invalidation_blocked"] is True

    assert main(["demo", "--handoff", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["reused"] is True

    assert main(["demo", "--out", str(out)]) == 1
    assert "DEMO_OUTPUT_CONFLICT" in capsys.readouterr().err

    assert main(["verify", "--job", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["evaluation_status"] == "conforms"

    assert main(["verify", str(out)]) == 2
    assert "Use --job" in capsys.readouterr().err
