"""Tests for ProcessCommandExecutor (FSF Process Adapter).

Verifies:
- Argument vector execution without shell (shell=False by default)
- Parameter template replacement ({prompt}, {output}, {shot_id}, {duration})
- Timeout and cancellation handling
- Exit code non-zero failure handling
- Artifact validation (file existence, non-zero size, suffix matching, SHA-256)
- File existence + SHA does NOT equal media conformance (QC gate required)
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pdx_artifact_core import validate_tool_result

from fsf.conformance.media import DEFAULT_BROLL_EXPECTED, evaluate_media_file
from fsf.executors.process_cmd import ProcessCommandExecutor


def test_process_executor_argument_template_and_success(tmp_path: Path) -> None:
    """Verifies argument token replacement and successful tool result creation."""
    script = tmp_path / "gen_script.py"
    script.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "out.write_bytes(b'DUMMY_MP4_CONTENT_12345')\n",
        encoding="utf-8",
    )

    executor = ProcessCommandExecutor(
        executor_id="local.test.generator",
        tool_name="studio.video.generate",
        command_args=[sys.executable, str(script), "{output}"],
        timeout_seconds=30.0,
        expected_suffix=".mp4",
        shell=False,
    )

    task = {
        "id": "shot_001",
        "duration_seconds": 3.0,
        "shot_spec": {"source_prompt": "An astronaut walking"},
    }

    out_dir = tmp_path / "out"
    res = executor.execute_shot(task, out_dir)

    assert res["status"] == "completed"
    assert res["outputs"]["shot_id"] == "shot_001"
    assert res["outputs"]["artifact_file"] == "shot_001.mp4"
    assert res["outputs"]["size_bytes"] == len(b"DUMMY_MP4_CONTENT_12345")
    expected_sha = hashlib.sha256(b"DUMMY_MP4_CONTENT_12345").hexdigest()
    assert res["outputs"]["artifact_sha256"] == expected_sha
    assert (out_dir / "shot_001.mp4").is_file()

    # Invariant: File existence and SHA do NOT mean the file conforms to Media contract!
    report, action = evaluate_media_file(out_dir / "shot_001.mp4", DEFAULT_BROLL_EXPECTED)
    assert report["evaluation_status"] != "conforms", "Dummy bytes must not pass Media conformance"


def test_process_executor_timeout_raises_timeout_error(tmp_path: Path) -> None:
    """Verifies that an external process exceeding timeout_seconds raises TimeoutError."""
    script = tmp_path / "sleep_script.py"
    script.write_text("import time\ntime.sleep(5.0)\n", encoding="utf-8")

    executor = ProcessCommandExecutor(
        executor_id="local.test.timeout",
        command_args=[sys.executable, str(script)],
        timeout_seconds=0.5,
    )

    task = {"id": "shot_timeout", "duration_seconds": 1.0}
    with pytest.raises(TimeoutError, match="timed out"):
        executor.execute_shot(task, tmp_path / "out")


def test_process_executor_nonzero_exit_code_raises_runtime_error(tmp_path: Path) -> None:
    """Verifies that a non-zero exit code raises RuntimeError with stderr capture."""
    script = tmp_path / "fail_script.py"
    script.write_text("import sys\nsys.stderr.write('CRITICAL GPU ERROR')\nsys.exit(42)\n", encoding="utf-8")

    executor = ProcessCommandExecutor(
        executor_id="local.test.fail",
        command_args=[sys.executable, str(script)],
        timeout_seconds=10.0,
    )

    task = {"id": "shot_fail"}
    with pytest.raises(RuntimeError, match="CRITICAL GPU ERROR"):
        executor.execute_shot(task, tmp_path / "out")


def test_process_executor_missing_output_file_raises_filenotfound(tmp_path: Path) -> None:
    """Verifies that exiting 0 without creating the expected output raises FileNotFoundError."""
    script = tmp_path / "noop_script.py"
    script.write_text("pass\n", encoding="utf-8")

    executor = ProcessCommandExecutor(
        executor_id="local.test.missing",
        command_args=[sys.executable, str(script)],
        timeout_seconds=10.0,
        expected_suffix=".mp4",
    )

    task = {"id": "shot_missing"}
    with pytest.raises(FileNotFoundError, match="output artifact not found"):
        executor.execute_shot(task, tmp_path / "out")


def test_process_executor_zero_byte_artifact_raises_valueerror(tmp_path: Path) -> None:
    """Verifies that producing a 0-byte file is refused as invalid artifact."""
    script = tmp_path / "empty_script.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_bytes(b'')\n",
        encoding="utf-8",
    )

    executor = ProcessCommandExecutor(
        executor_id="local.test.empty",
        command_args=[sys.executable, str(script), "{output}"],
        expected_suffix=".mp4",
    )

    task = {"id": "shot_empty"}
    with pytest.raises(ValueError, match="0-byte artifact"):
        executor.execute_shot(task, tmp_path / "out")


def test_process_executor_suffix_mismatch_raises_valueerror(tmp_path: Path) -> None:
    """Verifies that output suffix must match expected_suffix."""
    script = tmp_path / "bad_ext.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_bytes(b'video')\n",
        encoding="utf-8",
    )

    executor = ProcessCommandExecutor(
        executor_id="local.test.badext",
        command_args=[sys.executable, str(script), "{output}"],
        expected_suffix=".mp4",
    )

    task = {"id": "shot_badext", "artifact_filename": "shot_badext.mov"}
    with pytest.raises(ValueError, match="output suffix mismatch"):
        executor.execute_shot(task, tmp_path / "out")


def test_process_executor_does_not_credit_old_file_when_command_does_not_write(tmp_path: Path) -> None:
    """Verifies that an existing pre-placed file in output directory is NOT credited if command doesn't write."""
    script = tmp_path / "noop_script.py"
    script.write_text("pass\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    old_file = out_dir / "shot_preexist.mp4"
    old_file.write_bytes(b"OLD_STALE_CONTENT")

    executor = ProcessCommandExecutor(
        executor_id="local.test.noop",
        command_args=[sys.executable, str(script)],
        timeout_seconds=10.0,
        expected_suffix=".mp4",
    )

    task = {"id": "shot_preexist"}
    with pytest.raises(FileNotFoundError, match="output artifact not found"):
        executor.execute_shot(task, out_dir)

    assert old_file.read_bytes() == b"OLD_STALE_CONTENT"


def test_process_executor_config_digest_binds_cwd_and_env(tmp_path: Path) -> None:
    """Verifies that config_digest changes when working_dir or env changes."""
    e1 = ProcessCommandExecutor(
        executor_id="local.test.digest",
        command_args=["python", "test.py"],
        working_dir=tmp_path / "dir1",
        env={"MODEL_TAG": "v1"},
    )
    e2 = ProcessCommandExecutor(
        executor_id="local.test.digest",
        command_args=["python", "test.py"],
        working_dir=tmp_path / "dir2",
        env={"MODEL_TAG": "v1"},
    )
    e3 = ProcessCommandExecutor(
        executor_id="local.test.digest",
        command_args=["python", "test.py"],
        working_dir=tmp_path / "dir1",
        env={"MODEL_TAG": "v2"},
    )

    assert e1.config_digest != e2.config_digest
    assert e1.config_digest != e3.config_digest
    assert e2.config_digest != e3.config_digest


def test_process_executor_masks_secrets_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: verifies that process failures mask secrets from env and args."""
    secret_env_val = "synth_secret_env_val_xyz888"
    secret_custom_val = "synth_secret_custom_val_abc999"
    monkeypatch.setenv("SYNTHETIC_API_KEY", secret_env_val)

    script = tmp_path / "fail_with_secrets.py"
    script.write_text(
        "import os, sys\n"
        "api_key = os.environ.get('SYNTHETIC_API_KEY', '')\n"
        "custom = os.environ.get('CUSTOM_SECRET', '')\n"
        "sys.stderr.write(f'Fatal crash: api_key={api_key} and custom_secret={custom}\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    executor = ProcessCommandExecutor(
        executor_id="local.test.masked_fail",
        command_args=[sys.executable, str(script)],
        env={"CUSTOM_SECRET": secret_custom_val},
        timeout_seconds=10.0,
    )

    task = {"id": "shot_masked"}
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_shot(task, tmp_path / "out")

    err_str = str(exc_info.value)
    assert secret_env_val not in err_str, "Synthetic env secret leaked in error message!"
    assert secret_custom_val not in err_str, "Custom secret leaked in error message!"
    assert "[REDACTED_SECRET]" in err_str


def test_process_executor_masks_secrets_and_avoids_full_cmd_on_timeout(tmp_path: Path) -> None:
    """Regression test: verifies that TimeoutError does not return full command args or leak secrets."""
    secret_arg_val = "synth_timeout_secret_token_777"
    script = tmp_path / "sleep_long.py"
    script.write_text("import time\ntime.sleep(5.0)\n", encoding="utf-8")

    executor = ProcessCommandExecutor(
        executor_id="local.test.masked_timeout",
        command_args=[sys.executable, str(script), "--token", secret_arg_val],
        timeout_seconds=0.3,
    )

    task = {"id": "shot_timeout_masked"}
    with pytest.raises(TimeoutError) as exc_info:
        executor.execute_shot(task, tmp_path / "out")

    err_str = str(exc_info.value)
    assert secret_arg_val not in err_str, "Command argument secret leaked in timeout error!"
    assert "Command '['" not in err_str, "Full command vector leaked in TimeoutExpired string!"
    assert "timed out after 0.3s" in err_str


def test_process_executor_masks_long_secret_crossing_truncation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: verifies that secrets exceeding truncation threshold (>400 chars) are masked

    before line extraction and length truncation, preventing any secret prefix from leaking.
    """
    long_secret = "LONG_SECRET_PREFIX_" + ("x" * 450) + "_LONG_SECRET_SUFFIX"
    monkeypatch.setenv("REVIEW_API_KEY", long_secret)

    script = tmp_path / "fail_long_secret.py"
    script.write_text(
        "import os, sys\n"
        "key = os.environ.get('REVIEW_API_KEY', '')\n"
        "sys.stderr.write(f'line1: prelude\\nline2: token={key}\\nline3: postlude\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    executor = ProcessCommandExecutor(
        executor_id="local.test.long_secret",
        command_args=[sys.executable, str(script)],
        timeout_seconds=10.0,
    )

    task = {"id": "shot_long_secret"}
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_shot(task, tmp_path / "out")

    err_str = str(exc_info.value)
    assert long_secret not in err_str, "Full long secret leaked in error!"
    assert "LONG_SECRET_PREFIX_" not in err_str, "Long secret prefix leaked before redaction!"
    assert "[REDACTED_SECRET]" in err_str, "Redaction marker missing from error message!"


def test_subprocess_secret_masking_without_explicit_env_and_cmd_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[P1] Verifies that when env is None (inheriting os.environ), secrets from os.environ are redacted,

    and raw command arguments (such as secret tokens in cmd_args[:2]) are not leaked in exception text.
    """
    secret_token = "synthetic_raw_token_xyz999"
    monkeypatch.setenv("SYNTHETIC_BEARER_TOKEN", secret_token)

    script = tmp_path / "fail_with_inherited_secret.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('Authorization failure for synthetic_raw_token_xyz999\\n')\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )

    # Put a secret in cmd_args[:2] directly
    cmd = [sys.executable, f"--api-key={secret_token}", str(script)]
    executor = ProcessCommandExecutor(
        executor_id="local.test.inherited_secret",
        command_args=cmd,
        env=None,  # Intentionally None: inherit os.environ
        timeout_seconds=5.0,
    )

    task = {"id": "shot_inherited_leak"}
    with pytest.raises(RuntimeError) as exc_info:
        executor.execute_shot(task, tmp_path / "out")

    err_text = str(exc_info.value)
    assert secret_token not in err_text, "Inherited os.environ secret or cmd_args token leaked in exception!"
    assert "[REDACTED_SECRET]" in err_text


def test_process_tree_termination_kills_descendants_on_timeout(tmp_path: Path) -> None:
    """[P1] Verifies that when a parent process times out or is terminated, all descendant processes

    spawned by it are also terminated and reaped, leaving zero orphan processes.
    """
    import os
    import time
    from fsf.executors.subprocess_safety import run_safe_subprocess

    pid_file = tmp_path / "child.pid"

    # Child script that spawns a long-sleeping grandchild process and writes grandchild's PID to disk
    launcher_script = tmp_path / "spawn_child.py"
    launcher_script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "pid_file = pathlib.Path(sys.argv[1])\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pid_file.write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    cmd = [sys.executable, str(launcher_script), str(pid_file)]
    res = run_safe_subprocess(cmd, timeout_seconds=0.8, check=False)
    assert res.timed_out is True

    # Read grandchild PID
    assert pid_file.is_file(), "Launcher failed to write grandchild PID"
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())

    # Wait briefly for OS cleanup
    time.sleep(0.5)

    def is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            chk = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in chk.stdout
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    assert not is_pid_alive(grandchild_pid), f"Grandchild process {grandchild_pid} leaked after parent timeout!"


def test_safe_subprocess_cancellation_reclaims_and_rethrows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that cancellation (BaseException/KeyboardInterrupt) reclaims process tree and re-throws."""
    from fsf.executors.subprocess_safety import run_safe_subprocess
    import subprocess

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    orig_communicate = subprocess.Popen.communicate

    def mock_communicate(self, timeout=None):
        raise KeyboardInterrupt("Simulated user cancellation via SIGINT")

    monkeypatch.setattr(subprocess.Popen, "communicate", mock_communicate)

    with pytest.raises(KeyboardInterrupt, match="Simulated user cancellation via SIGINT"):
        run_safe_subprocess(cmd, timeout_seconds=10.0)


def test_safe_subprocess_multiline_secret_scrubbing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies multiline keys, timeout, and failure messages are scrubbed without leaking command details."""
    from fsf.executors.subprocess_safety import run_safe_subprocess

    header = "-----" + "BEGIN RSA PRIVATE KEY" + "-----"
    footer = "-----" + "END RSA PRIVATE KEY" + "-----"
    multiline_key = f"{header}\nMIIEowIBAAKCAQEAz3u\n{footer}"
    monkeypatch.setenv("FSF_TEST_PRIVATE_KEY", multiline_key)

    # 1. Non-zero exit code printing multiline key
    script = tmp_path / "leak_script.py"
    script.write_text(
        f"import sys\nprint({multiline_key!r}, file=sys.stderr)\nsys.exit(1)\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as exc_info:
        run_safe_subprocess([sys.executable, str(script)], check=True)

    err = str(exc_info.value)
    assert "MIIEowIBAAKCAQEAz3u" not in err
    assert "[REDACTED_SECRET]" in err
    # Confirm command args don't leak full raw paths
    assert str(script) not in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
def test_termination_does_not_signal_callers_shared_process_group(tmp_path: Path) -> None:
    from fsf.executors.subprocess_safety import run_safe_subprocess

    # Run the counterexample in a separate session, so a regression cannot
    # terminate pytest or the CI runner itself.
    script = tmp_path / "shared_group.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "from fsf.executors.subprocess_safety import terminate_process_tree\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "try:\n"
        "    assert os.getpgid(child.pid) == os.getpgrp()\n"
        "    assert terminate_process_tree(child.pid, grace_period_sec=0.1)\n"
        "    child.wait(timeout=2)\n"
        "    assert not terminate_process_tree(os.getpid())\n"
        "    print('caller survived')\n"
        "finally:\n"
        "    if child.poll() is None: child.kill()\n"
        "    child.wait()\n",
        encoding="utf-8",
    )
    result = run_safe_subprocess([sys.executable, str(script)], timeout_seconds=5, check=True)
    assert "caller survived" in result.stdout


def test_process_tree_termination_fallback_when_taskkill_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that when taskkill fails, fallback termination (os.kill / Win32) is invoked and kills process."""
    from fsf.executors.subprocess_safety import terminate_process_tree, _is_process_alive

    if sys.platform != "win32":
        pytest.skip("Windows taskkill fallback only")

    # Spawn a long sleeping process
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert _is_process_alive(child.pid)

    # Mock taskkill to simulate failure (e.g. exit code 1)
    real_run = subprocess.run

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "taskkill":
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Access denied")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    # terminate_process_tree should invoke fallback and successfully terminate child
    terminated = terminate_process_tree(child.pid, grace_period_sec=0.5)
    assert terminated is True
    assert not _is_process_alive(child.pid), "Fallback failed to terminate process when taskkill failed!"


def test_windows_fallback_kills_descendants_when_taskkill_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] When taskkill fails, descendants must die — not only the parent PID.

    Counterexample: parent killed, grandchild still alive, function returned True.
    """
    import time

    from fsf.executors.subprocess_safety import (
        _is_process_alive,
        _list_windows_descendants,
        terminate_process_tree,
    )

    if sys.platform != "win32":
        pytest.skip("Windows-only descendant cleanup counterexample")

    # Parent spawns a grandchild that outlives a plain parent kill.
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])\n"
                "time.sleep(90)\n"
            ),
        ]
    )
    # Wait until at least one descendant is visible
    deadline = time.perf_counter() + 5.0
    descendants: list[int] = []
    while time.perf_counter() < deadline:
        descendants = _list_windows_descendants(parent.pid)
        if descendants:
            break
        time.sleep(0.05)
    assert descendants, "Failed to spawn observable descendant for counterexample"
    grandchild_pid = descendants[0]
    assert _is_process_alive(grandchild_pid)

    real_run = subprocess.run

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "taskkill":
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Access denied")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_run)

    terminated = terminate_process_tree(parent.pid, grace_period_sec=1.0)
    assert terminated is True
    assert not _is_process_alive(parent.pid), "Parent should be terminated"
    assert not _is_process_alive(grandchild_pid), (
        "Descendant remained alive after taskkill failure — fallback must kill the full tree"
    )


def test_process_cleanup_error_raised_if_child_remains_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] Verifies that if process remains alive after all termination attempts, ProcessCleanupError is raised."""
    from fsf.executors.subprocess_safety import (
        ProcessCleanupError,
        terminate_process_tree,
        run_safe_subprocess,
    )
    import fsf.executors.subprocess_safety as safe_mod

    # Mock _is_process_alive to simulate a zombie/unkillable process
    monkeypatch.setattr(safe_mod, "_is_process_alive", lambda pid: True)

    with pytest.raises(ProcessCleanupError, match="still alive"):
        terminate_process_tree(99999, grace_period_sec=0.05, raise_on_failure=True)

    # In run_safe_subprocess, final barrier checks _is_process_alive and raises ProcessCleanupError
    with pytest.raises(ProcessCleanupError, match="Process cleanup failed"):
        run_safe_subprocess([sys.executable, "-c", "import sys; sys.exit(0)"])
