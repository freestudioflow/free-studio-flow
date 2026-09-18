"""FSF-S live verification runner: Kaggle work + APIs + local transport shape.

Live local-model ping stays skipped unless skip_local=False. The local
transport itself is always shape-checked; it is not an optional feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fsf.live.api_check import verify_apis
from fsf.live.kaggle_check import verify_kaggle_work
from fsf.live.local_check import verify_local_transport


def run_live_check(
    *,
    docx_path: Path,
    out_dir: Path,
    kaggle: bool = True,
    api: bool = True,
    skip_local: bool = True,
) -> dict[str, Any]:
    skip_live_model = bool(skip_local)
    report: dict[str, Any] = {
        "schema_version": "fsf_live_check_v1",
        "skip_local": skip_live_model,
    }
    ok = True
    if kaggle:
        report["kaggle"] = verify_kaggle_work(docx_path, Path(out_dir) / "kaggle")
        ok = ok and bool(report["kaggle"].get("ok"))
    if api:
        report["api"] = verify_apis()
        ok = ok and bool(report["api"].get("ok"))
    report["local"] = verify_local_transport(
        docx_path,
        Path(out_dir) / "local",
        ping_live_model=not skip_live_model,
    )
    ok = ok and bool(report["local"].get("shape_ok"))
    report["ok"] = ok
    return report
