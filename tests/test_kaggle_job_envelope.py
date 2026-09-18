"""Offline envelope test for private Kaggle jobs (no network, no GPU)."""

from __future__ import annotations

from pathlib import Path

from fsf.live.kaggle_check import build_private_kaggle_job


def test_private_kaggle_job_envelope(sample_docx: Path, tmp_path: Path) -> None:
    result = build_private_kaggle_job(sample_docx, tmp_path / "job")
    assert result["is_private"] is True
    assert result["task_count"] == 3
    assert result["executor_id"] == "kaggle.ltx23.t2v"
    assert not result["secrets_leaked"]
    assert Path(result["job_file"]).is_file()
