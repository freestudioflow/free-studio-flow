"""Tests for live Kaggle work, public APIs, and local transport shape.

Live local-model ping stays skipped; the local transport itself must exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsf.live.runner import run_live_check

pytestmark = pytest.mark.live


def test_live_kaggle_and_api_local_shape(sample_docx: Path, tmp_path: Path) -> None:
    report = run_live_check(
        docx_path=sample_docx,
        out_dir=tmp_path / "live",
        kaggle=True,
        api=True,
        skip_local=True,
    )
    assert report["skip_local"] is True
    assert report["local"]["shape_ok"] is True
    assert report["local"]["skipped"] is True
    assert report["local"]["live_model"]["skipped"] is True
    assert str(report["local"]["shape"]["executor"]).startswith("local.")
    assert report["kaggle"]["ok"] is True, report["kaggle"]
    assert report["kaggle"]["api"]["ok"] is True, report["kaggle"]["api"]
    assert report["kaggle"]["job"]["is_private"] is True
    assert not report["kaggle"]["job"].get("secrets_leaked")
    assert report["api"]["ok"] is True, report["api"]
    assert all(item["pin_published"] for item in report["api"]["pypi"])
    assert report["ok"] is True
