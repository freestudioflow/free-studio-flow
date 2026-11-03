"""Bounded optional native-tool workers; no executable comes from an asset pack."""
import os
from pathlib import Path
import time

from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.contracts import ProductionError
from fsf.production.control import file_identity


class ProcessMemory:
    def __init__(self, pid):
        self.pid, self.peak, self.handle = pid, 0, None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
            kernel.OpenProcess.restype = w.HANDLE
            kernel.CloseHandle.argtypes = [w.HANDLE]
            self.kernel = kernel
            self.handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
            if not self.handle:
                raise ProductionError('PRODUCTION_TOOL_MEMORY_UNOBSERVED')
        self.sample()

    def sample(self):
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes as w
            class Counters(ctypes.Structure):
                _fields_ = [('cb',w.DWORD),('faults',w.DWORD)]+[(name,ctypes.c_size_t) for name in
                    ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
            value = Counters()
            value.cb = ctypes.sizeof(value)
            function = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
            function.argtypes = [w.HANDLE,ctypes.POINTER(Counters),w.DWORD]
            if function(self.handle,ctypes.byref(value),value.cb):
                self.peak = max(self.peak,value.peak)
        else:
            try:
                values = dict(line.split(':',1) for line in Path(f'/proc/{self.pid}/status').read_text().splitlines() if ':' in line)
                self.peak = max(self.peak,int(values['VmHWM'].split()[0])*1024)
            except (FileNotFoundError,KeyError):
                pass
        return self.peak

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_delivery_tool(tool, arguments, *, cwd, deadline, rss_limit=2*1024**3, cancel_check=None):
    if not isinstance(tool,dict) or set(tool) != {'path','sha256'} or not Path(tool['path']).is_absolute():
        raise ProductionError('PRODUCTION_TOOL_CONFIG')
    if file_identity(tool['path'])['sha256'] != tool['sha256']:
        raise ProductionError('PRODUCTION_TOOL_CHANGED')
    remaining = min(120,deadline-time.monotonic())
    if remaining <= 0:
        raise ProductionError('PRODUCTION_TOOL_DEADLINE')
    memory = []

    def started(pid):
        memory.append(ProcessMemory(pid))

    def cancelled():
        if memory and memory[0].sample() > rss_limit:
            raise ProductionError('PRODUCTION_TOOL_MEMORY_LIMIT')
        return cancel_check() if cancel_check else False

    try:
        host_data = Path(cwd)/'.host-data'
        host_data.mkdir(exist_ok=True)
        environment = {name:os.environ[name] for name in ('PATH','SystemRoot','WINDIR','TEMP','TMP') if name in os.environ}
        environment.update(APPDATA=str(host_data), LOCALAPPDATA=str(host_data), XDG_DATA_HOME=str(host_data),
                           XDG_CACHE_HOME=str(host_data))
        result = run_safe_subprocess([tool['path'],*arguments], cwd=cwd,
            env=environment,
            inherit_env=False, timeout_seconds=remaining, output_limit_bytes=8*1024**2,
            cancel_check=cancelled, process_started=started, scrub_stdout=False)
        if result.timed_out:
            raise ProductionError('PRODUCTION_TOOL_DEADLINE')
        if not memory or memory[0].sample() == 0:
            raise ProductionError('PRODUCTION_TOOL_MEMORY_UNOBSERVED')
        if memory[0].peak > rss_limit:
            raise ProductionError('PRODUCTION_TOOL_MEMORY_LIMIT')
        if file_identity(tool['path'])['sha256'] != tool['sha256']:
            raise ProductionError('PRODUCTION_TOOL_CHANGED')
        return result, {'tool_sha256': tool['sha256'], 'peak_rss_bytes': memory[0].peak,
                        'rss_enforcement': 'sampled_os_peak_with_cancellation', 'elapsed_ms': result.elapsed_ms}
    finally:
        for monitor in memory:
            monitor.close()
