"""CLI must not fake-success shape executors as media."""

from __future__ import annotations

from pathlib import Path

from fsf.cli.main import main
from fsf.executors.local_comfy import LocalComfyExecutor
from fsf.intake.compiler import compile_job


def test_flow_run_without_shape_is_refused(sample_docx: Path, tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "flow",
            "run",
            "studio.video.generate",
            str(sample_docx),
            "--out",
            str(tmp_path / "gen"),
            "--transport",
            "local",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "refuse" in err
    assert "--shape" in err
    assert not (tmp_path / "gen" / "task_001.mp4").exists()
    assert not (tmp_path / "gen" / "task_001.shape.bin").exists()


def test_flow_run_live_is_refused(sample_docx: Path, tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "flow",
            "run",
            "studio.video.generate",
            str(sample_docx),
            "--out",
            str(tmp_path / "live"),
            "--transport",
            "local",
            "--live",
        ]
    )
    assert rc == 2
    assert "live inference is not wired" in capsys.readouterr().err


def test_flow_run_shape_writes_bin_not_mp4(sample_docx: Path, tmp_path: Path) -> None:
    out = tmp_path / "shape"
    rc = main(
        [
            "flow",
            "run",
            "studio.video.generate",
            str(sample_docx),
            "--out",
            str(out),
            "--transport",
            "local",
            "--shape",
        ]
    )
    assert rc == 0
    assert (out / "task_001.shape.bin").is_file()
    assert not (out / "task_001.mp4").exists()
    payload = (out / "task_001.shape.bin").read_bytes()
    assert payload.startswith(b"COMFYUI_LOCAL_GPU:")


def test_run_non_mock_without_shape_is_refused(sample_docx: Path, tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "run",
            str(sample_docx),
            "--out",
            str(tmp_path / "run"),
            "--executor",
            "local.comfy.video",
        ]
    )
    assert rc == 2
    assert "refuse" in capsys.readouterr().err


def test_flow_run_mock_override_without_shape_is_refused(
    sample_docx: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "mock_flow"
    rc = main(
        [
            "flow",
            "run",
            "studio.video.generate",
            str(sample_docx),
            "--out",
            str(out),
            "--transport",
            "local",
            "--executor",
            "mock.studio.video",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "refuse" in err
    assert "--shape" in err
    assert not (out / "task_001.mp4").exists()
    assert not (out / "task_001.mock.bin").exists()


def test_flow_run_mock_override_with_shape_is_refused(
    sample_docx: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "mock_shape"
    rc = main(
        [
            "flow",
            "run",
            "studio.video.generate",
            str(sample_docx),
            "--out",
            str(out),
            "--transport",
            "local",
            "--executor",
            "mock.studio.video",
            "--shape",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "not a Studio transport" in err
    assert not (out / "task_001.mp4").exists()
    assert not (out / "task_001.mock.bin").exists()


def test_run_mock_writes_mock_bin(sample_docx: Path, tmp_path: Path) -> None:
    out = tmp_path / "mock_run"
    rc = main(["run", str(sample_docx), "--out", str(out), "--executor", "mock.studio.video"])
    assert rc == 0
    assert (out / "task_001.mock.bin").is_file()
    assert not (out / "task_001.mp4").exists()


def test_library_shape_generate_is_not_mp4(sample_docx: Path, tmp_path: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    result = LocalComfyExecutor().execute_shot(manifest["tasks"][0], tmp_path)
    assert result["outputs"]["artifact_file"] == "task_001.shape.bin"
    assert (tmp_path / "task_001.shape.bin").is_file()
