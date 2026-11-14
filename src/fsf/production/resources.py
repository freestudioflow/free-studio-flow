"""Opt-in bounded local resources; legacy calls have no active governor."""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import os
from pathlib import Path
import time

from fsf.production.contracts import ProductionError, digest

MIB = 1024**2
ACTIVE = ContextVar("fsf_local_resource_governor", default=None)


def resource_profile(name):
    if name not in ("low-memory-v1", "roomy-memory-v1"):
        raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    rss, commit = (512*MIB, 768*MIB) if name == "low-memory-v1" else (2048*MIB, 3072*MIB)
    return {"schema_version": "fsf_local_resources_v1", "name": name,
            "rss_bytes": rss, "commit_bytes": commit, "max_parallel": 1,
            "chunk_bytes": (2 if name == "low-memory-v1" else 8)*MIB, "admission_wait_seconds": 120,
            "resume_watch_seconds": 300, "hard_duration_seconds": 5,
            "cleanup_seconds": 10, "host_hard_bytes": 256*MIB,
            "task_commit_bytes": 64*MIB}


def validate_profile(value):
    reference = resource_profile("low-memory-v1")
    if not isinstance(value, dict) or set(value) != set(reference):
        raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    if value["schema_version"] != reference["schema_version"]:
        raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    if not isinstance(value["name"], str) or not value["name"] or len(value["name"]) > 96:
        raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    limits = {"rss_bytes": (64*MIB, 2**63-1), "commit_bytes": (64*MIB, 2**63-1),
              "max_parallel": (1, 64), "chunk_bytes": (4096, 8*MIB),
              "admission_wait_seconds": (1, 5400), "resume_watch_seconds": (1, 5400),
              "hard_duration_seconds": (1, 60), "cleanup_seconds": (1, 10),
              "host_hard_bytes": (64*MIB, 2048*MIB), "task_commit_bytes": (1, 2**63-1)}
    for name, (minimum, maximum) in limits.items():
        item = value[name]
        if type(item) is not int or not minimum <= item <= maximum:
            raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    if (value["commit_bytes"] < value["rss_bytes"] or value["chunk_bytes"] > value["rss_bytes"]//8
            or value["task_commit_bytes"] > value["commit_bytes"]):
        raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
    return copy.deepcopy(value)


class ResourcePause(ProductionError):
    def __init__(self):
        super().__init__("PRODUCTION_RESOURCE_PAUSED")


class Governor:
    """Bound admission and subprocess observation without changing job identities."""

    def __init__(self, profile, *, report=None, sampler=None, clock=time.monotonic, sleep=time.sleep):
        self.profile = validate_profile(profile)
        if sampler is None:
            from fsf.executors.resource_sample import sample
            sampler = sample
        self.sampler = sampler
        self.clock, self.sleep = clock, sleep
        self.report = Path(report) if report is not None else None
        self.low_since = None
        self.reason = None
        self.latest = None
        self.last_sample = -float("inf")
        self.peak_rss = self.peak_commit = 0
        self.report_sequence = 0
        self.last_state = None
        self.pending_bytes = self.profile["rss_bytes"]//4
        self.parallel = 1
        self.io_chunk_bytes = self.profile["chunk_bytes"]

    def observe(self, *, force=False):
        now = self.clock()
        if not force and self.latest is not None and now-self.last_sample < 0.1:
            return self.latest
        try:
            row = self.sampler()
        except (OSError, ValueError):
            self.reason = "measurement_unavailable"
            self._record({"state": "paused_resource", "reason": self.reason}, force=True)
            raise ResourcePause() from None
        required = ("tree_rss_bytes", "tree_commit_bytes", "host_available_bytes",
                    "host_total_bytes", "host_commit_available_bytes", "host_commit_limit_bytes")
        if any(type(row.get(name)) is not int or row[name] < 0 for name in required):
            self.reason = "measurement_unavailable"
            self._record({"state": "paused_resource", "reason": self.reason}, force=True)
            raise ResourcePause()
        self.latest, self.last_sample = row, now
        self.peak_rss = max(self.peak_rss, row["tree_rss_bytes"])
        self.peak_commit = max(self.peak_commit, row["tree_commit_bytes"])
        hard = self.profile["host_hard_bytes"]
        enforce_commit = row.get("host_commit_kind") != "linux_advisory"
        host_low = row["host_available_bytes"] < hard or (
            enforce_commit and row["host_commit_available_bytes"] < hard)
        cgroup = row.get("cgroup")
        group_hard = cgroup is not None and cgroup["limit_bytes"] is not None and (
            cgroup["current_bytes"]*100 >= cgroup["limit_bytes"]*95)
        self.low_since = (now if self.low_since is None else self.low_since) if host_low or group_hard else None
        owned_high = (row["tree_rss_bytes"] > self.profile["rss_bytes"]
                      or row["tree_commit_bytes"] > self.profile["commit_bytes"])
        host_warning = max(512*MIB, min(1536*MIB, row["host_total_bytes"]//20))
        commit_warning = max(512*MIB, min(2048*MIB, row["host_commit_limit_bytes"]//20))
        warning = owned_high or host_low or group_hard or row["host_available_bytes"] < host_warning or (
            enforce_commit and row["host_commit_available_bytes"] < commit_warning)
        if cgroup is not None and cgroup["limit_bytes"] is not None:
            warning = warning or cgroup["current_bytes"]*100 >= cgroup["limit_bytes"]*80
        if owned_high or (self.low_since is not None and now-self.low_since >= self.profile["hard_duration_seconds"]):
            self.reason = "tree_limit" if owned_high else "sustained_memory_pressure"
        # A custom worker can request this bound before allocating its queue.
        headroom = max(0, self.profile["rss_bytes"]-row["tree_rss_bytes"])
        self.pending_bytes = min(self.profile["rss_bytes"]//4, headroom//2)
        self.parallel = 1 if warning else min(self.profile["max_parallel"],
            max(1, headroom//max(self.profile["chunk_bytes"], 1)))
        self.io_chunk_bytes = min(self.profile["chunk_bytes"], 64*1024) if warning else self.profile["chunk_bytes"]
        state = "paused_resource" if self.reason else "backpressure" if warning else "ready"
        self.latest = {**row, "state": state}
        self._record({**row, "state": state, "reason": self.reason,
                      "low_seconds": 0 if self.low_since is None else now-self.low_since,
                      "pending_bytes": self.pending_bytes, "parallel": self.parallel,
                      "io_chunk_bytes": self.io_chunk_bytes})
        if self.reason:
            raise ResourcePause()
        return {**row, "state": state}

    def _record(self, row, *, force=False):
        if self.report is None:
            return
        # One observation per second plus state changes, bounded by batch deadline.
        state = row["state"]
        if not force and state == self.last_state and self.report_sequence and self.clock()-getattr(self, "_last_report", 0) < 1:
            return
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report_sequence += 1
        value = {"sequence": self.report_sequence, "observed_unix": time.time(),
                 "policy_digest": digest(self.profile), "peak_tree_rss_bytes": self.peak_rss,
                 "peak_tree_commit_bytes": self.peak_commit, **row}
        with self.report.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, allow_nan=False)+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.last_state, self._last_report = state, self.clock()

    def admit(self, *, required_commit_bytes=0, deadline=None):
        if type(required_commit_bytes) is not int or required_commit_bytes < 0:
            raise ProductionError("PRODUCTION_RESOURCE_PROFILE")
        end = self.clock()+self.profile["admission_wait_seconds"]
        if deadline is not None:
            end = min(end, deadline)
        while True:
            row = self.observe(force=True)
            host_headroom = (row["host_available_bytes"] if row.get("host_commit_kind") == "linux_advisory"
                             else row["host_commit_available_bytes"])
            group = row.get("cgroup")
            if group is not None and group["limit_bytes"] is not None:
                host_headroom = min(host_headroom, max(0, group["limit_bytes"]-group["current_bytes"]))
            enough = (host_headroom >= required_commit_bytes+self.profile["host_hard_bytes"]
                      and row["tree_commit_bytes"]+required_commit_bytes <= self.profile["commit_bytes"])
            if row["state"] == "ready" and enough:
                return
            if self.clock() >= end:
                self.reason = "admission_timeout"
                self._record({**row, "state": "paused_resource", "reason": self.reason}, force=True)
                raise ResourcePause()
            self.sleep(min(0.1, max(0, end-self.clock())))

    def recover(self, *, deadline):
        end = min(deadline, self.clock()+self.profile["resume_watch_seconds"])
        while self.clock() < end:
            previous = self.reason
            self.reason = None
            try:
                row = self.observe(force=True)
            except ResourcePause:
                if self.reason == "measurement_unavailable":
                    return False
            else:
                if row["state"] == "ready":
                    self._record({**row, "state": "recovered", "reason": None}, force=True)
                    return True
                self.reason = previous
            self.sleep(min(0.1, max(0, end-self.clock())))
        return False

    def check_running(self):
        self.observe()
        return False

    @contextmanager
    def activate(self):
        token = ACTIVE.set(self)
        try:
            yield self
        finally:
            ACTIVE.reset(token)


def current_governor():
    return ACTIVE.get()


def read_chunk_bytes(default=1024*1024):
    governor = current_governor()
    if governor is None:
        return default
    governor.check_running()
    return governor.io_chunk_bytes
