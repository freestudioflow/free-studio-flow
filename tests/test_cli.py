"""CLI tests for FSF."""

from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from fsf.cli.main import main


def test_cli_check(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from fsf.conformance import media
    from fsf.executors import subprocess_safety

    monkeypatch.setattr(media, "ensure_media_tools_on_path", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        subprocess_safety, "run_safe_subprocess",
        lambda *args, **kwargs: SimpleNamespace(ok=True, stdout="fixture version\n"),
    )
    exit_code = main(["check"])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    assert "Free Studio Flow Dependency Status" in captured.out
    assert "[OK] prodocux" in captured.out


def test_cli_check_reports_missing_media_tools(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from fsf.conformance import media

    monkeypatch.setattr(media, "ensure_media_tools_on_path", lambda: (None, None))
    exit_code = main(["check", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["status"] == "failed"
    assert all(pkg["installed"] for pkg in report["packages"].values())
    assert report["tools"]["ffmpeg"]["available"] is False
    assert report["tools"]["ffprobe"]["available"] is False


def test_cli_intake(sample_docx: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "cli_out"
    exit_code = main(["intake", str(sample_docx), "--out", str(out)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[FSF Intake] Compiled 3 tasks" in captured.out
    assert (out / "job_manifest.json").is_file()


def test_cli_plan_shape_mode_by_default(sample_docx: Path, tmp_path: Path) -> None:
    plan_out = tmp_path / "plan_shape.json"
    exit_code = main([
        "plan",
        str(sample_docx),
        "--skills",
        "generate,dub",
        "--assembly",
        "-o",
        str(plan_out),
    ])
    assert exit_code == 0
    import json
    data = json.loads(plan_out.read_text(encoding="utf-8"))
    gen_step = next(s for s in data["steps"] if s["tool"] == "studio.video.generate")
    dub_step = next(s for s in data["steps"] if s["tool"] == "studio.voice.dub")
    transform_step = next(s for s in data["steps"] if s["kind"] == "transform")

    # In default shape mode, outputs are shape stubs and dub output matches assembly audio reference
    assert gen_step["outputs"] == ["artifact:task_001.shape.bin"]
    assert dub_step["outputs"] == ["artifact:task_001.dub.json"]
    first_in = transform_step["inputs"]["artifact_inputs"][0]
    assert first_in["artifact"] == "artifact:task_001.shape.bin"
    assert first_in["audio_artifact"] == "artifact:task_001.dub.json"


def test_cli_plan_media_mode(sample_docx: Path, tmp_path: Path) -> None:
    plan_out = tmp_path / "plan_media.json"
    exit_code = main([
        "plan",
        str(sample_docx),
        "--skills",
        "generate,dub",
        "--assembly",
        "--media",
        "-o",
        str(plan_out),
    ])
    assert exit_code == 0
    import json
    data = json.loads(plan_out.read_text(encoding="utf-8"))
    gen_step = next(s for s in data["steps"] if s["tool"] == "studio.video.generate")
    dub_step = next(s for s in data["steps"] if s["tool"] == "studio.voice.dub")
    transform_step = next(s for s in data["steps"] if s["kind"] == "transform")

    # In media mode, outputs are real media filenames and dub matches assembly reference
    assert gen_step["outputs"] == ["artifact:task_001.mp4"]
    assert dub_step["outputs"] == ["artifact:task_001.dub.wav"]
    first_in = transform_step["inputs"]["artifact_inputs"][0]
    assert first_in["artifact"] == "artifact:task_001.mp4"
    assert first_in["audio_artifact"] == "artifact:task_001.dub.wav"


def test_cli_plan_rejects_conflicting_media_and_shape(sample_docx: Path, capsys) -> None:
    exit_code = main(["plan", str(sample_docx), "--media", "--shape"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Cannot specify both --media and --shape" in captured.err
