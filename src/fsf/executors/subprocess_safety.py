"""Process Safety and Subprocess Lifecycle Governance for FSF.

Provides deterministic, leak-free subprocess execution across Windows and POSIX:
- POSIX: Uses `start_new_session=True` to create a dedicated process group.
  On timeout/cancellation: SIGTERM to process group, grace period, SIGKILL fallback,
  and process reclamation via wait/communicate to prevent orphan/zombie processes.
- Windows: Uses `taskkill /F /T /PID` to terminate the entire process tree.
- Deadlock prevention: Defaults to `stdin=subprocess.DEVNULL`.
- Measurement: High-resolution timing (elapsed_ms).
- Leak prevention: Comprehensive secret masking on stdout, stderr, and exception text.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

def scrub_string_secrets(text: str) -> str:
    """Scrub tokens, API keys, and sensitive paths from raw strings."""
    # Scrub Windows user profile paths (single and double backslashes)
    text = re.sub(r"[A-Za-z]:(?:\\\\|\\)[Uu]sers(?:\\\\|\\)[^\\\"'\s]+", "[REDACTED_USER_PATH]", text)
    text = re.sub(r"/" r"Users/[^/\"'\s]+", "[REDACTED_USER_PATH]", text)
    text = re.sub(r"/" r"home/[^/\"'\s]+", "[REDACTED_USER_PATH]", text)
    # Scrub tokens
    text = re.sub(r"HF_[A-Za-z0-9_]+", "[REDACTED_SECRET]", text, flags=re.I)
    text = re.sub(r"ghp_[A-Za-z0-9]+", "[REDACTED_SECRET]", text)
    text = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_SECRET]", text)
    return text


def scrub_secrets(text: str) -> str:
    """General text scrubbing helper."""
    return scrub_string_secrets(text)


_SENSITIVE_KEY_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "AUTH",
    "CREDENTIAL",
    "BEARER",
    "PRIVATE",
    "APIKEY",
)


def sanitize_process_text(
    text: Any,
    *,
    env: dict[str, str] | None = None,
    extra_tokens: Sequence[str] | None = None,
) -> Any:
    """Sanitize process output, errors, and nested metadata against secret leaks.

    Masks:
    1. Values of environment variables with sensitive key names (both os.environ and custom env),
       including multiline secrets and key blocks.
    2. Explicit sensitive tokens or argument values (e.g. from command args).
    3. Common inline pattern pairs (e.g. 'api_key=xxx', 'Bearer xxx', 'token: xxx').
    4. Well-known credential signatures (e.g. sk-, ghp-, HF_).
    5. User directory paths (Windows/Unix profile paths).
    6. Recursively scrubs nested dicts and lists.
    """
    if text is None:
        return ""

    if isinstance(text, dict):
        return {
            str(k): sanitize_process_text(v, env=env, extra_tokens=extra_tokens)
            for k, v in text.items()
        }

    if isinstance(text, (list, tuple)):
        return [sanitize_process_text(x, env=env, extra_tokens=extra_tokens) for x in text]

    out = str(text)
    vals_to_redact: set[str] = set()

    # Collect from active environments (both os.environ and passed env)
    env_sources: list[Any] = [os.environ]
    if env is not None:
        env_sources.append(env)

    for env_map in env_sources:
        for k, v in env_map.items():
            if not v:
                continue
            v_str = str(v).strip()
            if len(v_str) < 3:
                continue
            k_upper = str(k).upper()
            if any(term in k_upper for term in _SENSITIVE_KEY_SUBSTRINGS):
                vals_to_redact.add(v_str)
                # Also redact individual non-empty lines of multiline secrets
                for line in v_str.splitlines():
                    cleaned_line = line.strip()
                    if len(cleaned_line) >= 4:
                        vals_to_redact.add(cleaned_line)

    # Collect from extra tokens or argument vector
    if extra_tokens:
        for i, tok in enumerate(extra_tokens):
            if not tok:
                continue
            tok_str = str(tok).strip()
            tok_lower = tok_str.lower()
            if any(term in tok_lower for term in ("key", "token", "secret", "password", "auth", "bearer")):
                if "=" in tok_str:
                    parts = tok_str.split("=", 1)
                    if len(parts) > 1 and len(parts[1].strip()) >= 3:
                        vals_to_redact.add(parts[1].strip())
                elif i + 1 < len(extra_tokens):
                    nxt = str(extra_tokens[i + 1]).strip()
                    if len(nxt) >= 3 and not nxt.startswith("-"):
                        vals_to_redact.add(nxt)

    # Replace specific collected sensitive values (longest first)
    for val in sorted(vals_to_redact, key=len, reverse=True):
        out = out.replace(val, "[REDACTED_SECRET]")

    # Mask common regex key/value pairs in error text
    out = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|token|password|passwd|bearer)\b([\s=:\"']+)([^\s\"',;&]+)",
        r"\1\2[REDACTED_SECRET]",
        out,
    )

    # Apply general scrub_secrets (handles tokens, Windows/POSIX user profile paths)
    out = scrub_secrets(out)
    return out


@dataclass(frozen=True)
class SafeSubprocessResult:
    """Outcome of a safe subprocess execution."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float
    timed_out: bool = False
    raw_stdout: str = ""
    raw_stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class ProcessCleanupError(RuntimeError):
    """Raised when child or descendant process tree cannot be terminated."""


def _is_process_alive(pid: int) -> bool:
    """Check whether a given PID is still active in the OS."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                import ctypes.wintypes
                exit_code = ctypes.wintypes.DWORD()
                res = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(h)
                # 259 is STILL_ACTIVE
                return bool(res and exit_code.value == 259)
        except Exception:
            pass
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


def _list_windows_descendants(root_pid: int) -> list[int]:
    """Enumerate all descendant PIDs of root_pid via Toolhelp32 (BFS)."""
    if root_pid <= 0:
        return []
    try:
        import ctypes
        import ctypes.wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.wintypes.DWORD),
                ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h_snap == -1 or h_snap == 0xFFFFFFFF:
            return []
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        parent_map: dict[int, int] = {}
        if kernel32.Process32First(h_snap, ctypes.byref(pe)):
            while True:
                parent_map[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                if not kernel32.Process32Next(h_snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(h_snap)

        descendants: list[int] = []
        seen: set[int] = set()
        queue = [root_pid]
        while queue:
            curr = queue.pop(0)
            for child_pid, ppid in parent_map.items():
                if ppid == curr and child_pid not in seen and child_pid != root_pid:
                    seen.add(child_pid)
                    descendants.append(child_pid)
                    queue.append(child_pid)
        return descendants
    except Exception:
        return []


def _force_kill_windows_pid(pid: int) -> None:
    """Force-terminate a single Windows PID via TerminateProcess / os.kill."""
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            ctypes.windll.kernel32.TerminateProcess(h, 1)
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass


def _create_windows_job_object() -> int | None:
    """Create a Job Object with KILL_ON_JOB_CLOSE so descendants die with the job."""
    try:
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.wintypes.DWORD),
                ("SchedulingClass", ctypes.wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(h_job)
            return None
        return int(h_job)
    except Exception:
        return None


def _assign_pid_to_windows_job(job_handle: int, pid: int) -> bool:
    """Assign a live process to a Job Object so its descendants are tracked."""
    if not job_handle or pid <= 0:
        return False
    try:
        import ctypes

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_INFORMATION = 0x0200
        access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SET_INFORMATION
        h_proc = ctypes.windll.kernel32.OpenProcess(access, False, pid)
        if not h_proc:
            # Fallback broader access for older Windows variants
            h_proc = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not h_proc:
            return False
        try:
            return bool(ctypes.windll.kernel32.AssignProcessToJobObject(job_handle, h_proc))
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)
    except Exception:
        return False


def _terminate_windows_job(job_handle: int | None) -> None:
    """Terminate every process in a Job Object and close the handle."""
    if not job_handle:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.TerminateJobObject(job_handle, 1)
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(job_handle)
    except Exception:
        pass


def terminate_process_tree(
    pid: int,
    *,
    grace_period_sec: float = 0.5,
    raise_on_failure: bool = False,
    job_handle: int | None = None,
) -> bool:
    """Terminate a process and all of its descendants across platforms.

    Returns True if the process was confirmed terminated, False otherwise.
    If raise_on_failure=True and process is still alive after fallbacks, raises ProcessCleanupError.

    On Windows, descendants are captured *before* killing the parent so orphan
    grandchildren cannot escape when taskkill fails. Prefer Job Object when available.
    """
    if pid <= 0:
        return True
    if pid == os.getpid():
        if raise_on_failure:
            raise ProcessCleanupError("Refusing to terminate the calling process")
        return False

    tracked_pids = [pid]
    if sys.platform == "win32":
        # Capture the full tree before any kill so descendants survive parent exit.
        descendants = _list_windows_descendants(pid)
        tracked_pids = [pid] + descendants

        # Prefer Job Object termination when the caller owns a job handle.
        if job_handle:
            _terminate_windows_job(job_handle)
            job_handle = None

        taskkill_ok = False
        try:
            # taskkill /F /T terminates process and all child processes spawned by it
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            taskkill_ok = res.returncode == 0
        except Exception:
            taskkill_ok = False

        # Effective termination fallback: kill every known descendant, then parent.
        still_alive = [p for p in tracked_pids if _is_process_alive(p)]
        if not taskkill_ok or still_alive:
            # Refresh descendant list in case new children appeared during taskkill race.
            for child_pid in _list_windows_descendants(pid):
                if child_pid not in tracked_pids:
                    tracked_pids.append(child_pid)
            # Kill deepest descendants first, then the parent.
            for child_pid in reversed(tracked_pids[1:]):
                _force_kill_windows_pid(child_pid)
            _force_kill_windows_pid(pid)
    else:
        # On POSIX with start_new_session=True, pgid == pid even if the session leader has already exited.
        cand_pgid = None
        try:
            cand_pgid = os.getpgid(pid)
        except Exception:
            cand_pgid = pid

        # Only signal an isolated group led by the target, never the caller's
        # shared group (for example a directly spawned child of pytest).
        if cand_pgid == pid and cand_pgid != os.getpgrp():
            try:
                os.killpg(cand_pgid, signal.SIGTERM)
            except Exception:
                pass
            time.sleep(grace_period_sec)
            try:
                os.killpg(cand_pgid, signal.SIGKILL)
            except Exception:
                pass
            try:
                os.waitpid(-cand_pgid, os.WNOHANG)
            except Exception:
                pass

        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    # Verification: parent and all previously observed descendants must be gone.
    deadline = time.perf_counter() + max(0.1, grace_period_sec)
    while time.perf_counter() < deadline:
        if sys.platform == "win32":
            for child_pid in _list_windows_descendants(pid):
                if child_pid not in tracked_pids:
                    tracked_pids.append(child_pid)
        else:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        if not any(_is_process_alive(p) for p in tracked_pids):
            return True
        time.sleep(0.05)

    survivors = [p for p in tracked_pids if _is_process_alive(p)]
    if survivors:
        if raise_on_failure:
            raise ProcessCleanupError(
                f"Failed to terminate process tree for PID {pid}; "
                f"still alive: {survivors}"
            )
        return False
    return True


def run_safe_subprocess(
    args: Sequence[str],
    *,
    timeout_seconds: float = 60.0,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = None,
    extra_tokens: Sequence[str] | None = None,
    check: bool = False,
    grace_period_sec: float = 0.5,
    scrub_stdout: bool = True,
    inherit_env: bool = True,
    output_limit_bytes: int | None = None,
    cancel_check: Any = None,
) -> SafeSubprocessResult:
    """Run a command safely with timeout, process group termination, and secret masking.

    - POSIX uses `start_new_session=True` so the spawned process leads a new process group.
    - Windows cleans up descendant processes via `taskkill /F /T`.
    - stdin defaults to `subprocess.DEVNULL` to prevent blocking on terminal input.
    - High-resolution timing records precise elapsed milliseconds.
    - stdout and stderr are sanitized against secrets before return.
    """
    cmd_args = [str(a) for a in args]
    cwd_str = str(cwd) if cwd is not None else None

    # Merge environment if provided
    sub_env = (os.environ.copy() if env is not None else None) if inherit_env else {}
    if sub_env is not None and env is not None:
        sub_env.update(env)

    # Support unittest monkeypatching of subprocess.run
    if getattr(subprocess.run, "__module__", None) != "subprocess":
        t0 = time.perf_counter()
        try:
            m_res = subprocess.run(
                cmd_args,
                cwd=cwd_str,
                env=sub_env,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            clean_stdout = sanitize_process_text(m_res.stdout or "", env=sub_env, extra_tokens=extra_tokens or cmd_args)
            clean_stderr = sanitize_process_text(m_res.stderr or "", env=sub_env, extra_tokens=extra_tokens or cmd_args)
            return SafeSubprocessResult(
                args=cmd_args,
                returncode=m_res.returncode,
                stdout=clean_stdout if scrub_stdout else (m_res.stdout or ""),
                stderr=clean_stderr,
                elapsed_ms=elapsed_ms,
                timed_out=False,
                raw_stdout=m_res.stdout or "",
                raw_stderr=m_res.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            raw_err = exc.stderr or ""
            if isinstance(raw_err, bytes):
                raw_err = raw_err.decode("utf-8", errors="replace")
            clean_stderr = sanitize_process_text(raw_err, env=sub_env, extra_tokens=extra_tokens or cmd_args)
            return SafeSubprocessResult(
                args=cmd_args,
                returncode=-1,
                stdout="",
                stderr=clean_stderr,
                elapsed_ms=elapsed_ms,
                timed_out=True,
                raw_stdout="",
                raw_stderr=raw_err,
            )

    popen_kwargs: dict[str, Any] = {
        "stdin": stdin if stdin is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
        "cwd": cwd_str,
        "env": sub_env,
    }

    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    job_handle: int | None = None
    if sys.platform == "win32":
        job_handle = _create_windows_job_object()

    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd_args, **popen_kwargs)
    if job_handle is not None:
        if not _assign_pid_to_windows_job(job_handle, proc.pid):
            _terminate_windows_job(job_handle)
            job_handle = None

    timed_out = False
    raw_stdout = ""
    raw_stderr = ""

    try:
        if output_limit_bytes is None and cancel_check is None:
            raw_stdout, raw_stderr = proc.communicate(timeout=timeout_seconds)
        else:
            raw_stdout, raw_stderr = _communicate_limited(proc, timeout_seconds, output_limit_bytes or 1024*1024, cancel_check)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc.pid, grace_period_sec=grace_period_sec, job_handle=job_handle)
        job_handle = None
        try:
            if output_limit_bytes is None:
                raw_stdout, raw_stderr = proc.communicate(timeout=grace_period_sec + 2.0)
            else:
                # The bounded drain threads own the pipes. Never return a token
                # fragment cut off by cancellation before full-value masking.
                proc.wait(timeout=grace_period_sec + 2.0)
                raw_stdout, raw_stderr = "", "Process timed out"
        except Exception:
            raw_stdout, raw_stderr = "", "Process terminated after timeout"
        finally:
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
    except BaseException:
        # Handles cancellations (KeyboardInterrupt), termination signals, or other unhandled exceptions
        terminate_process_tree(proc.pid, grace_period_sec=grace_period_sec, job_handle=job_handle)
        job_handle = None
        try:
            proc.wait(timeout=grace_period_sec + 2.0)
        except Exception:
            pass
        raise
    finally:
        t1 = time.perf_counter()
        elapsed_ms = round((t1 - t0) * 1000.0, 2)
        # Final reclamation barrier to guarantee zero orphan processes
        descendants = _list_windows_descendants(proc.pid) if sys.platform == "win32" else []
        if proc.poll() is None or _is_process_alive(proc.pid) or any(
            _is_process_alive(p) for p in descendants
        ):
            terminate_process_tree(proc.pid, grace_period_sec=0.2, job_handle=job_handle)
            job_handle = None
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            survivors = [proc.pid] + (
                _list_windows_descendants(proc.pid) if sys.platform == "win32" else []
            )
            survivors = [p for p in survivors if _is_process_alive(p)]
            if survivors:
                raise ProcessCleanupError(
                    f"Process cleanup failed: PID {proc.pid} tree still alive: {survivors}"
                )
        elif job_handle is not None:
            # Normal completion: close job handle without TerminateJobObject.
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(job_handle)
            except Exception:
                pass
            job_handle = None

    # Sanitize stdout and stderr
    clean_stdout = sanitize_process_text(raw_stdout or "", env=sub_env, extra_tokens=extra_tokens or cmd_args)
    clean_stderr = sanitize_process_text(raw_stderr or "", env=sub_env, extra_tokens=extra_tokens or cmd_args)

    result = SafeSubprocessResult(
        args=cmd_args,
        returncode=proc.returncode if not timed_out else -1,
        stdout=clean_stdout if scrub_stdout else (raw_stdout or ""),
        stderr=clean_stderr,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
        raw_stdout=raw_stdout or "",
        raw_stderr=raw_stderr or "",
    )

    cmd_name = Path(cmd_args[0]).name if cmd_args else "Command"
    cmd_label = sanitize_process_text(cmd_name, env=sub_env, extra_tokens=extra_tokens or cmd_args)

    if timed_out and check:
        clean_snippet = clean_stderr.strip() or clean_stdout.strip()
        lines = [line.strip() for line in clean_snippet.splitlines() if line.strip()]
        snippet = " | ".join(lines[-3:]) if lines else "timed out"
        raise TimeoutError(
            f"Command '{cmd_label}' timed out after {timeout_seconds:.1f}s ({elapsed_ms}ms): {snippet}"
        )

    if check and result.returncode != 0:
        clean_snippet = clean_stderr.strip() or clean_stdout.strip()
        lines = [line.strip() for line in clean_snippet.splitlines() if line.strip()]
        snippet = " | ".join(lines[-4:]) if lines else f"exit code {result.returncode}"
        raise RuntimeError(
            f"Command '{cmd_label}' failed (code {result.returncode}, {elapsed_ms}ms): {snippet}"
        )

    return result


def _communicate_limited(proc, timeout, limit, cancel_check=None):
    """Drain both pipes with capped buffers; discard truncated secret fragments."""
    import threading
    if type(limit) is not int or limit < 1:
        raise ValueError("Invalid capture limit")
    exceeded = threading.Event()
    buffers = [[], []]
    def drain(stream, index):
        size = 0
        while True:
            # Text pipes: a character needs at most four UTF-8 bytes.
            chunk = stream.read(1024)
            if not chunk:
                break
            size += len(chunk.encode("utf-8", errors="replace"))
            if size > limit:
                exceeded.set()
            elif not exceeded.is_set():
                buffers[index].append(chunk)
    threads = [threading.Thread(target=drain, args=(stream, i), daemon=True)
               for i, stream in enumerate((proc.stdout, proc.stderr))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic()+timeout
    while proc.poll() is None:
        if cancel_check is not None and cancel_check():
            raise KeyboardInterrupt()
        if exceeded.is_set():
            raise RuntimeError("PROCESS_OUTPUT_LIMIT")
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("bounded process", timeout)
        time.sleep(0.02)
    for thread in threads:
        thread.join(timeout=max(0.01, deadline-time.monotonic()))
    if exceeded.is_set():
        raise RuntimeError("PROCESS_OUTPUT_LIMIT")
    if any(thread.is_alive() for thread in threads):
        raise subprocess.TimeoutExpired("bounded process", timeout)
    return "".join(buffers[0]), "".join(buffers[1])
