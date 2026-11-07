import multiprocessing
import os
import sys
import time

import pytest

from fsf.production.contracts import ProductionError
from fsf.production.segment_quality import LocalSegmentGate
from fsf.production.store import ProductionStore
from fsf.production.store_runner import run_store_batch
from test_production_runner import Driver
from test_production_store import store_case


def gate():
    return LocalSegmentGate(policy_digest="a" * 64, evaluator_digest="b" * 64,
                            required=["seam"], evaluate=lambda *_: {"seam": "accept"})


def interrupt(root, work, plan, boundary, ready):
    from fsf.production import publication
    store = ProductionStore(root)

    def stop(*args, **kwargs):
        ready.set()
        os._exit(71)

    quality = gate()
    if boundary == "during_evaluation":
        quality.evaluate = stop
    elif boundary == "after_intent":
        publication._read = stop
    elif boundary == "after_accept":
        accept = store.accept

        def accept_then_stop(*args, **kwargs):
            accept(*args, **kwargs)
            stop()

        store.accept = accept_then_stop
    else:
        store.accept = stop
    run_store_batch(plan, store=store, driver=Driver(work), quality_gate=quality)


@pytest.mark.parametrize("boundary", ["during_evaluation", "after_intent", "after_rename", "after_accept"])
def test_independent_restart_finishes_same_publication(tmp_path, boundary):
    plan, store = store_case(tmp_path, "v1")
    root = store.root
    store.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=interrupt, args=(root, tmp_path, plan, boundary, ready))
    worker.start()
    try:
        assert ready.wait(30)
        worker.join(15)
        assert worker.exitcode == 71
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(10)
    store = ProductionStore(root)
    try:
        execution = store.execution(plan)
        first = store.task(execution, "a")
        request = first["request"]
        committed = boundary == "after_accept"
        assert first["status"] == ("machine_accepted" if committed else "produced")
        assert bool(first["accepted"]) == committed
        assert store.task(execution, "b")["remaining"] == (0 if committed else 1)
        assert (root / "published" / request).exists() == (boundary in {"after_rename", "after_accept"})
        assert store.inspect()["used_units"] == 5
        driver = Driver(tmp_path)
        assert run_store_batch(plan, store=store, driver=driver, quality_gate=gate())["status"] == "machine_accepted"
        assert [task for task, _ in driver.submitted] == ["b"]
        assert store.task(execution, "a")["request"] == request
        assert store.inspect()["used_units"] == 10
        assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 2
        evaluations = store.db.execute("SELECT count(*) FROM events WHERE request=? AND kind='segment_quality_started'",
                                       (request,)).fetchone()[0]
        assert evaluations == (2 if boundary == "during_evaluation" else 1)
        (root / "published" / request).write_bytes(b"changed")
        with pytest.raises(ProductionError, match="SOURCE_CHANGED"):
            run_store_batch(plan, store=store, driver=driver, quality_gate=gate())
        assert len(driver.submitted) == 1
    finally:
        store.close()


def test_bounded_command_evaluator_stays_unknown_on_timeout(tmp_path):
    plan, store = store_case(tmp_path, "v1")
    driver = Driver(tmp_path)
    quality = LocalSegmentGate.command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        policy_digest="a" * 64, evaluator_digest="b" * 64, required=["seam"])
    started = time.monotonic()
    try:
        result = run_store_batch(plan, store=store, driver=driver, quality_gate=quality, seconds=2)
        assert result["status"] == "quality_unknown"
        assert time.monotonic() - started < 15
        assert [task for task, _ in driver.submitted] == ["a"]
    finally:
        store.close()
