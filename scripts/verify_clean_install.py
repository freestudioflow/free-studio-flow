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
        # Write a small helper so we can use multi-line Python without -c quoting issues.
        # We only verify that required packages are installed; FFmpeg is not required here
        # (static-ffmpeg is lazy-downloaded at runtime, not at install time).
        checker = work / "_fsf_pkg_check.py"
        checker.write_text(
            "import json, sys\n"
            "from fsf.cli.main import main\n"
            "import io, contextlib\n"
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    main(['check', '--json'])\n"
            "report = json.loads(buf.getvalue())\n"
            "pkgs = report.get('packages', {})\n"
            "bad = [k for k, v in pkgs.items() if not v.get('installed')]\n"
            "if bad:\n"
            "    print('MISSING packages:', bad, file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('packages OK:', list(pkgs))\n",
            encoding="utf-8",
        )
        _run(str(python), str(checker), cwd=work)
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
