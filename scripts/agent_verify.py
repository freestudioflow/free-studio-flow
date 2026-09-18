"""Agent verification entry: offline suite, then live Kaggle + API.

Local transport shape is included; live local-model ping stays skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run(args: list[str]) -> int:
    print("+", " ".join(args), flush=True)
    completed = subprocess.run(args, cwd=ROOT, env=_env())
    return completed.returncode


def main() -> int:
    python = sys.executable
    hygiene = _run([python, str(ROOT / "scripts" / "check_public_hygiene.py")])
    if hygiene != 0:
        print("public hygiene scan failed", file=sys.stderr)
        return hygiene
    offline = _run(
        [
            python,
            "-m",
            "pytest",
            str(ROOT / "tests"),
            "-m",
            "not live",
            "-v",
        ]
    )
    if offline != 0:
        print("offline pytest failed", file=sys.stderr)
        return offline
    live = _run(
        [
            python,
            "-m",
            "pytest",
            str(ROOT / "tests" / "test_live_kaggle_api.py"),
            "-m",
            "live",
            "-v",
        ]
    )
    if live != 0:
        print("live Kaggle/API pytest failed", file=sys.stderr)
        return live
    cli = _run([python, "-m", "fsf.cli", "live-check"])
    if cli != 0:
        print("fsf live-check failed", file=sys.stderr)
        return cli
    print(
        "agent_verify: hygiene + offline + live Kaggle/API passed; "
        "local transport shape OK; live local model skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
