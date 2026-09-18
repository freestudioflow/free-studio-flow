"""CLI tests for FSF."""

from __future__ import annotations

from pathlib import Path
from fsf.cli.main import main


def test_cli_check(capsys) -> None:
    exit_code = main(["check"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Free Studio Flow Dependency Status" in captured.out
    assert "[OK] prodocux" in captured.out


def test_cli_intake(sample_docx: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "cli_out"
    exit_code = main(["intake", str(sample_docx), "--out", str(out)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "[FSF Intake] Compiled 3 tasks" in captured.out
    assert (out / "job_manifest.json").is_file()
