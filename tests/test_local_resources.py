"""Resource pressure is tested without consuming the host's free memory."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from fsf.production.contracts import ProductionError, publish
from fsf.production.resources import Governor, MIB, ResourcePause, resource_profile, validate_profile
from fsf.executors.resource_sample import sample
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.store import ProductionStore
from fsf.production.store_runner import run_store_batch
from test_production_runner import setup, Driver


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def observation(**changes):
    value = {"tree_rss_bytes": 64*MIB, "tree_commit_bytes": 80*MIB,
             "host_available_bytes": 4*1024*MIB, "host_total_bytes": 16*1024*MIB,
             "host_commit_available_bytes": 8*1024*MIB, "host_commit_limit_bytes": 32*1024*MIB,
             "cgroup": None}
    return {**value, **changes}


@pytest.mark.parametrize("name", ["low-memory-v1", "roomy-memory-v1"])
def test_profiles_and_schema_agree(name):
    value = resource_profile(name)
    schema = json.loads((Path(__file__).parents[1]/"src/fsf/schemas/fsf_local_resources_v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    assert validate_profile(value) == value


@pytest.mark.parametrize("field,value", [
    ("rss_bytes", True), ("commit_bytes", 0), ("chunk_bytes", 0), ("chunk_bytes", 9*MIB),
    ("max_parallel", 0), ("admission_wait_seconds", float("nan")), ("extra", 1),
    ("task_commit_bytes", 2**63), ("resume_watch_seconds", -1)])
def test_invalid_profile_rejected_before_sampling(field, value):
    profile = {**resource_profile("low-memory-v1"), field: value}
    with pytest.raises(ProductionError, match="RESOURCE_PROFILE"):
        Governor(profile, sampler=lambda: pytest.fail("must not sample"))


def test_sustained_host_pressure_reset_and_pause_records_last_reading(tmp_path):
    clock = Clock()
    profile = resource_profile("low-memory-v1")
    current = observation(host_available_bytes=200*MIB)
    report = tmp_path/"memory.jsonl"
    guard = Governor(profile, sampler=lambda: current, clock=clock, sleep=clock.sleep, report=report)
    guard.observe(force=True)
    clock.now = 4.99
    guard.observe(force=True)
    current["host_available_bytes"] = 4*1024*MIB
    guard.observe(force=True)
    clock.now = 9
    current["host_available_bytes"] = 200*MIB
    guard.observe(force=True)
    clock.now = 13.99
    guard.observe(force=True)
    clock.now = 14
    with pytest.raises(ResourcePause):
        guard.observe(force=True)
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert rows[-1]["host_available_bytes"] == 200*MIB
    assert rows[-1]["low_seconds"] == 5
    assert rows[-1]["state"] == "paused_resource"


def test_backpressure_adapts_queue_and_recovers_without_quality_changes():
    profile = {**resource_profile("roomy-memory-v1"), "max_parallel": 4}
    clock = Clock()
    current = observation(host_available_bytes=600*MIB)
    guard = Governor(profile, sampler=lambda: current, clock=clock, sleep=clock.sleep)
    assert guard.observe(force=True)["state"] == "backpressure"
    assert guard.parallel == 1
    current["host_available_bytes"] = 4*1024*MIB
    assert guard.observe(force=True)["state"] == "ready"
    assert guard.parallel == 4
    low = Governor(resource_profile("low-memory-v1"), sampler=lambda: current)
    low.observe(force=True)
    assert guard.pending_bytes > low.pending_bytes
    assert guard.profile["name"] == "roomy-memory-v1"


def test_admission_timeout_is_bounded_and_measurement_unknown_is_not_zero():
    clock = Clock()
    guard = Governor({**resource_profile("low-memory-v1"), "admission_wait_seconds": 1},
                     sampler=lambda: observation(host_available_bytes=600*MIB),
                     clock=clock, sleep=clock.sleep)
    with pytest.raises(ResourcePause):
        guard.admit()
    assert clock.now == pytest.approx(1)
    assert guard.reason == "admission_timeout"
    guard = Governor(resource_profile("low-memory-v1"), sampler=lambda: {})
    with pytest.raises(ResourcePause):
        guard.observe()
    assert guard.reason == "measurement_unavailable"


def test_cgroup_pressure_has_a_separate_scope():
    clock = Clock()
    row = observation(cgroup={"current_bytes": 98*MIB, "limit_bytes": 100*MIB})
    guard = Governor(resource_profile("low-memory-v1"), sampler=lambda: row, clock=clock)
    guard.observe(force=True)
    clock.now = 5
    with pytest.raises(ResourcePause):
        guard.observe(force=True)


def test_native_sampler_accounts_child_creation_identity():
    child = subprocess.Popen([sys.executable, "-c", "import time; payload=bytearray(24*1024**2); print('ready',flush=True); time.sleep(30)"],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        row = sample()
        entry = next(p for p in row["processes"] if p["pid"] == child.pid)
        assert entry["birth"] > 0
        assert sum(p["commit"] for p in row["processes"] if p["pid"] != os.getpid()) >= 24*MIB
        assert row["tree_rss_bytes"] == sum(p["rss"] for p in row["processes"])
        assert entry["parent"] == os.getpid()
    finally:
        from fsf.executors.subprocess_safety import terminate_process_tree
        terminate_process_tree(child.pid)
        child.wait(timeout=10)


def test_subprocess_pressure_reclaims_owned_child(tmp_path):
    report = tmp_path/"measurements.jsonl"
    state = {"calls": 0}
    def monitor():
        state["calls"] += 1
        return observation(tree_rss_bytes=(600 if state["calls"] > 2 else 64)*MIB)
    guard = Governor(resource_profile("low-memory-v1"), sampler=monitor, report=report)
    with guard.activate(), pytest.raises(ResourcePause):
        run_safe_subprocess([sys.executable, "-c", "import time; time.sleep(30)"],
                            timeout_seconds=10, output_limit_bytes=1024)
    assert guard.reason == "tree_limit"
    assert json.loads(report.read_text().splitlines()[-1])["peak_tree_rss_bytes"] == 600*MIB


def test_resource_pause_preserves_budget_and_original_execution(tmp_path):
    plan, book = setup(tmp_path)
    folder = tmp_path/"indexed"
    folder.mkdir()
    store = ProductionStore.create(folder, plan, book.inspect()["authorization"])
    try:
        before = store.inspect()
        guard = Governor(resource_profile("low-memory-v1"), sampler=lambda: {})
        result = run_store_batch(plan, store=store, driver=Driver(tmp_path),
                                 seconds=1, resource_governor=guard)
        assert result["status"] == "paused_resource"
        assert store.inspect()["used_units"] == before["used_units"]
        guard = Governor(resource_profile("low-memory-v1"), sampler=observation)
        backend = Driver(tmp_path)
        assert run_store_batch(plan, store=store, driver=backend, resource_governor=guard)["status"] == "machine_accepted"
        used = store.inspect()["used_units"]
        calls = copy.deepcopy(backend.submitted)
        assert run_store_batch(plan, store=store, driver=backend, resource_governor=guard)["status"] == "machine_accepted"
        assert backend.submitted == calls and store.inspect()["used_units"] == used
        changed = Governor(resource_profile("roomy-memory-v1"), sampler=observation)
        with pytest.raises(ProductionError, match="RESOURCE_CHANGED"):
            run_store_batch(plan, store=store, driver=backend, resource_governor=changed)
    finally:
        store.close()


def test_adaptive_chunks_are_consumed_by_hashing_without_changing_content(tmp_path, monkeypatch):
    from fsf.production.control import file_identity
    from fsf.production import resources
    target = tmp_path/'payload.bin'
    target.write_bytes(bytes(range(256))*65536)
    expected = file_identity(target)
    sizes = []
    original = resources.read_chunk_bytes
    def measured(default=1024*1024):
        value = original(default)
        sizes.append(value)
        return value
    monkeypatch.setattr(resources, 'read_chunk_bytes', measured)
    for name, available, size in [('low-memory-v1', 4096*MIB, 2*MIB),
                                  ('roomy-memory-v1', 4096*MIB, 8*MIB),
                                  ('roomy-memory-v1', 600*MIB, 64*1024)]:
        guard = Governor(resource_profile(name), sampler=lambda: observation(host_available_bytes=available))
        sizes.clear()
        with guard.activate():
            assert file_identity(target) == expected
        assert sizes and set(sizes) == {size}


def test_linux_advisory_commit_does_not_reject_real_memory_headroom():
    row = observation(host_commit_kind='linux_advisory', host_commit_available_bytes=0)
    guard = Governor(resource_profile('low-memory-v1'), sampler=lambda: row)
    guard.admit(required_commit_bytes=64*MIB)
    assert guard.observe()['state'] == 'ready'


def test_invalid_batch_arguments_do_not_bind_resource_policy(tmp_path):
    plan, book = setup(tmp_path)
    folder = tmp_path/'indexed'
    folder.mkdir()
    store = ProductionStore.create(folder, plan, book.inspect()['authorization'])
    try:
        for arguments in ({'seconds': 5401}, {'delivery': 1}):
            with pytest.raises(ProductionError):
                run_store_batch(plan, store=store, driver=Driver(tmp_path),
                                resource_governor=Governor(resource_profile('low-memory-v1')),
                                **arguments)
        assert store.db.execute("SELECT count(*) FROM events WHERE kind='resource_policy_bound'").fetchone()[0] == 0
    finally:
        store.close()


def test_pressure_after_submit_reopens_unknown_without_second_reservation(tmp_path):
    plan, book = setup(tmp_path)
    folder = tmp_path/'indexed'
    folder.mkdir()
    store = ProductionStore.create(folder, plan, book.inspect()['authorization'])
    guard = Governor(resource_profile('low-memory-v1'), sampler=observation)
    class PressureDriver(Driver):
        inject = True
        def submit(self, task, request_id):
            super().submit(task, request_id)
            if self.inject:
                guard.reason = 'measurement_unavailable'
                raise ResourcePause()
    backend = PressureDriver(tmp_path)
    try:
        result = run_store_batch(plan, store=store, driver=backend, resource_governor=guard)
        assert result['status'] == 'paused_resource'
        row = store.db.execute('SELECT request,status,worst,settled FROM reservations').fetchone()
        request = row['request']
        assert row['status'] == 'unknown' and row['worst'] == 10 and row['settled'] is None
        assert store.inspect()['used_units'] == 10
    finally:
        store.close()
    store = ProductionStore(folder)
    backend.inject = False
    try:
        assert run_store_batch(plan, store=store, driver=backend,
                              resource_governor=Governor(resource_profile('low-memory-v1'), sampler=observation))['status'] == 'machine_accepted'
        assert [value for value in backend.submitted if value[1] == request] == [('a', request)]
        assert store.db.execute('SELECT count(*) FROM reservations').fetchone()[0] == 2
        used = store.inspect()['used_units']
        calls = list(backend.submitted)
        run_store_batch(plan, store=store, driver=backend,
                        resource_governor=Governor(resource_profile('low-memory-v1'), sampler=observation))
        assert store.inspect()['used_units'] == used and backend.submitted == calls
    finally:
        store.close()
