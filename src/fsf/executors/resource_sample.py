"""Native process-tree observations. No application names or command lines."""
import ctypes as c
import os
from pathlib import Path
import time


def _linux_process(pid):
    try:
        directory = Path("/proc")/str(pid)
        stat = (directory/"stat").read_text()
        fields = stat[stat.rfind(")")+2:].split()
        status = {}
        for line in (directory/"status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmData:", "VmStk:")):
                key, value, *_ = line.split()
                status[key[:-1]] = int(value)*1024
        return {"pid": pid, "parent": int(fields[1]), "birth": int(fields[19]),
                "rss": status.get("VmRSS", 0), "commit": status.get("VmData", 0)+status.get("VmStk", 0)}
    except (FileNotFoundError, ProcessLookupError):
        return None


def _linux():
    root = _linux_process(os.getpid())
    if root is None:
        raise OSError("Resource observation unavailable")
    records = {root["pid"]: root}
    queue = [root]
    for parent in queue:
        directory = Path("/proc")/str(parent["pid"])/"task"
        try:
            children = set()
            for thread in directory.iterdir():
                try:
                    children.update(int(pid) for pid in (thread/"children").read_text().split())
                except FileNotFoundError:
                    continue
            for pid in children-records.keys():
                child = _linux_process(pid)
                if child is not None and child["parent"] == parent["pid"] and child["birth"] >= parent["birth"]:
                    records[pid] = child
                    queue.append(child)
        except FileNotFoundError:
            continue
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value, *_ = line.split()
        memory[key[:-1]] = int(value)*1024
    group = None
    relative = next((line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                     if line.startswith("0::")), None)
    if relative is not None and ".." not in Path(relative).parts:
        folder = Path("/sys/fs/cgroup")/relative.lstrip("/")
        if not (folder/"memory.current").exists() and (Path("/sys/fs/cgroup/memory.current")).exists():
            folder = Path("/sys/fs/cgroup")
        if (folder/"memory.current").exists():
            maximum = (folder/"memory.max").read_text().strip()
            group = {"current_bytes": int((folder/"memory.current").read_text()),
                     "limit_bytes": None if maximum == "max" else int(maximum),
                     "scope": "current_cgroup_includes_other_members_if_shared"}
    overcommit = Path("/proc/sys/vm/overcommit_memory").read_text().strip()
    return {"processes": list(records.values()), "host_total_bytes": memory["MemTotal"],
            "host_commit_kind": "linux_strict" if overcommit == "2" else "linux_advisory",
            "host_available_bytes": memory["MemAvailable"],
            "host_commit_limit_bytes": memory["CommitLimit"],
            "host_commit_available_bytes": max(0, memory["CommitLimit"]-memory["Committed_AS"]),
            "tree_commit_kind": "linux_data_and_stack_estimate", "cgroup": group}


def _windows():
    from ctypes import wintypes as w
    kernel = c.WinDLL("kernel32", use_last_error=True)
    psapi = c.WinDLL("psapi", use_last_error=True)
    def api(lib, name, args, restype=w.BOOL):
        function = getattr(lib, name)
        function.argtypes, function.restype = args, restype
        return function

    class Entry(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", c.c_long), ("flags", w.DWORD),
                    ("exe", w.WCHAR*260)]

    class Counters(c.Structure):
        _fields_ = [("size", w.DWORD), ("faults", w.DWORD)] + [
            (name, c.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
                "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]

    class Memory(c.Structure):
        _fields_ = [("size", w.DWORD), ("load", w.DWORD)] + [(name, c.c_ulonglong) for name in
            ("total", "available", "commit_limit", "commit_available", "virtual", "virtual_available", "extended")]

    close = api(kernel, "CloseHandle", [w.HANDLE])
    snapshot = api(kernel, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)(2, 0)
    if snapshot in (None, c.c_void_p(-1).value):
        raise OSError("Resource observation unavailable")
    parents = {}
    try:
        entry = Entry()
        entry.size = c.sizeof(entry)
        first = api(kernel, "Process32FirstW", [w.HANDLE, c.POINTER(Entry)])
        following = api(kernel, "Process32NextW", [w.HANDLE, c.POINTER(Entry)])
        found = first(snapshot, c.byref(entry))
        while found:
            parents[entry.pid] = entry.parent
            found = following(snapshot, c.byref(entry))
    finally:
        close(snapshot)
    open_process = api(kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    times = api(kernel, "GetProcessTimes", [w.HANDLE]+[c.POINTER(w.FILETIME)]*4)
    info = api(psapi, "GetProcessMemoryInfo", [w.HANDLE, c.POINTER(Counters), w.DWORD])

    def process(pid):
        handle = open_process(0x1000 | 0x10, False, pid)
        if not handle:
            if c.get_last_error() == 87:  # Process exited between observations.
                return None
            raise OSError("Resource process accounting unavailable")
        try:
            created, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
            value = Counters()
            value.size = c.sizeof(value)
            if not times(handle, c.byref(created), c.byref(exited), c.byref(kernel_time), c.byref(user_time)):
                raise OSError("Resource process accounting unavailable")
            if not info(handle, c.byref(value), value.size):
                if c.get_last_error() in (6, 87):
                    return None
                raise OSError("Resource memory accounting unavailable")
            return {"pid": pid, "parent": parents.get(pid),
                    "birth": (created.dwHighDateTime << 32)+created.dwLowDateTime,
                    "rss": value.rss, "commit": value.private}
        finally:
            close(handle)
    root = process(os.getpid())
    records = {root["pid"]: root}
    queue = [root]
    for parent in queue:
        for pid, ppid in parents.items():
            if ppid == parent["pid"] and pid not in records:
                child = process(pid)
                if child is not None and child["birth"] >= parent["birth"]:
                    records[pid] = child
                    queue.append(child)
    memory = Memory()
    memory.size = c.sizeof(memory)
    if not api(kernel, "GlobalMemoryStatusEx", [c.POINTER(Memory)])(c.byref(memory)):
        raise OSError("Host memory accounting unavailable")
    return {"processes": list(records.values()), "host_total_bytes": memory.total,
            "host_available_bytes": memory.available, "host_commit_limit_bytes": memory.commit_limit,
            "host_commit_available_bytes": memory.commit_available,
            "tree_commit_kind": "windows_private_commit", "cgroup": None}


def sample():
    value = _windows() if os.name == "nt" else _linux()
    value["tree_rss_bytes"] = sum(row["rss"] for row in value["processes"])
    value["tree_commit_bytes"] = sum(row["commit"] for row in value["processes"])
    value["observed_monotonic"] = time.monotonic()
    return value
