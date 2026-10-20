"""Fixed, standalone bootstrap for an authenticated FSF worker deployment."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--recipe-digest", required=True)
    parser.add_argument("--result-out")
    args = parser.parse_args()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    key = None
    phase = "validation"
    try:
        source = Path(args.bundle)
        if not 0 < source.stat().st_size <= 64*1024*1024:
            raise ValueError()
        if hashlib.sha256(source.read_bytes()).hexdigest() != args.sha256:
            raise ValueError()
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo("request.json")
            if info.file_size > 8*1024*1024:
                raise ValueError()
            request = json.loads(archive.read(info))
            key = request["request_digest"]
            if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                raise ValueError()
            wheel = request["worker_wheel"]
            filename = Path(wheel["path"]).name
            if not filename.startswith("free_studio_flow-") or not filename.endswith("-py3-none-any.whl"):
                raise ValueError()
            wheel_info = archive.getinfo(wheel["path"])
            if not 0 < wheel_info.file_size <= 64*1024*1024:
                raise ValueError()
            data = archive.read(wheel_info)
            if hashlib.sha256(data).hexdigest() != wheel["sha256"] or len(data) != wheel["size_bytes"]:
                raise ValueError()
            package = root/filename
            package.write_bytes(data)
        # Load only the known, self-contained process-safety module from the
        # authenticated wheel. Bootstrap has no installed FSF dependency yet.
        with zipfile.ZipFile(package) as archive:
            safety_info = archive.getinfo("fsf/executors/subprocess_safety.py")
            if safety_info.file_size > 1024*1024:
                raise ValueError()
            safety_path = root/"process_safety.py"
            safety_path.write_bytes(archive.read(safety_info))
        spec = importlib.util.spec_from_file_location("fsf_bootstrap_safety", safety_path)
        safety = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = safety
        spec.loader.exec_module(safety)
        budget = request["policy"]["deadline_seconds"]
        if not isinstance(budget, int) or not 1 <= budget <= 86400:
            raise ValueError()
        deadline = time.monotonic()+budget
        # The bootstrap's network source must be explicitly authorized too.
        if "https://pypi.org/simple" not in request["policy"]["network_sources"]:
            raise ValueError()
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
        env["FSF_REMOTE_DEPTH"] = "1"
        for name in request["recipe"]["environment"]["credential_env_names"]:
            if name in request["policy"]["credential_env_names"] and name in os.environ:
                env[name] = os.environ[name]
        def run(command):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            result = safety.run_safe_subprocess(command, timeout_seconds=remaining, env=env,
                inherit_env=False, cwd=root, output_limit_bytes=1024*1024,
                cancel_check=lambda:(root/(key+".cancel")).exists())
            if result.timed_out or result.returncode:
                raise RuntimeError()
        venv = root/"runtime-venv"
        phase = "venv"
        # Managed notebooks may provide pip but omit ensurepip. The host pip
        # installs into the isolated interpreter; no global package mutation.
        run([sys.executable, "-m", "venv", "--without-pip", str(venv)])
        python = venv/("Scripts/python.exe" if os.name == "nt" else "bin/python")
        phase = "install"
        run([sys.executable, "-m", "pip", "--python", str(python), "install", "--disable-pip-version-check", "--index-url", "https://pypi.org/simple", str(package)])
        phase = "worker"
        run([str(python), "-m", "fsf.cli", "remote", "worker", str(source.absolute()), "--out", str(root.absolute()),
             "--recipe-digest", args.recipe_digest, "--remaining-seconds", str(max(1,int(deadline-time.monotonic())))])
        if args.result_out:
            shutil.copyfile(root/key/"result.zip", args.result_out)
        return 0
    except BaseException as exc:
        if key is not None:
            state = root/key
            state.mkdir(exist_ok=True)
            status_path=state/"worker-status.json"
            old=json.loads(status_path.read_text()) if status_path.is_file() else {}
            if old.get("status") not in {"completed","cancelled","failed"}:
                cancelled=isinstance(exc,KeyboardInterrupt)
                status_path.write_text(json.dumps({"request_digest":key,"status":"cancelled" if cancelled else "failed",
                    "error_code":"REMOTE_CANCELLED" if cancelled else "REMOTE_BOOTSTRAP_"+phase.upper()+"_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
