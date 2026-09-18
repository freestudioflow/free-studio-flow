"""Build a wheel, install it into a temporary venv, and smoke-test imports."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="retain temporary files")
    args = parser.parse_args()
    temp = None if args.keep else tempfile.TemporaryDirectory(prefix="fsf-release-")
    work = Path(tempfile.mkdtemp(prefix="fsf-release-")) if args.keep else Path(temp.name)
    try:
        wheels = work / "wheels"
        wheels.mkdir()
        _run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(wheels),
            str(ROOT),
            cwd=work,
        )
        wheel = next(wheels.glob("free_studio_flow-*.whl"))
        env_dir = work / "venv"
        _run(sys.executable, "-m", "venv", str(env_dir), cwd=work)
        python = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        _run(str(python), "-m", "pip", "install", "--upgrade", "pip", cwd=work)
        _run(str(python), "-m", "pip", "install", str(wheel), cwd=work)
        _run(str(python), "-c", "import fsf; print('fsf', fsf.__version__)", cwd=work)
        _run(
            str(python),
            "-c",
            "from fsf.cli.main import main; raise SystemExit(main(['check']))",
            cwd=work,
        )
        print(f"clean-install PASS: {wheel.name}")
        print(f"isolated cwd: {work}")
        if args.keep:
            print(f"retained evidence directory: {work}")
        return 0
    finally:
        if temp is not None:
            temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
