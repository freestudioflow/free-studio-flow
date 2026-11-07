import pytest

from fsf.production.contracts import ProductionError, seal
from fsf.production.segment_quality import LocalSegmentGate, motion_checks, reference_checks
from fsf.production.store import ProductionStore
from fsf.production.store_runner import run_store_batch
from test_production_runner import Driver, setup
from test_production_store import store_case


def gate(evaluate, scope="fixture"):
    return LocalSegmentGate(policy_digest="a" * 64, evaluator_digest="b" * 64,
                            required=["intra_segment", "seam"], evaluate=evaluate, scope=scope)


def test_unknown_reopen_keeps_attempt_and_evaluation_bound(tmp_path):
    plan, store = store_case(tmp_path, "v1")
    driver = Driver(tmp_path)
    calls = []

    def evaluator(observation, artifact, deadline):
        calls.append(observation["request_id"])
        return {"intra_segment": "accept"}

    root = store.root
    for _ in range(5):
        assert run_store_batch(plan, store=store, driver=driver,
                               quality_gate=gate(evaluator))["status"] == "quality_unknown"
        store.close()
        store = ProductionStore(root)
    assert [task for task, _ in driver.submitted] == ["a"]
    assert len(calls) == 3 and len(set(calls)) == 1
    request = calls[0]
    attempt = store.attempt(request)
    artifact, technical = driver.verify(plan["tasks"][0], request)
    with pytest.raises(ProductionError, match="EVIDENCE_REQUIRED"):
        store.accept(request, attempt["epoch"], artifact, technical)
    with pytest.raises(ProductionError, match="PROVIDER_CHANGED"):
        run_store_batch(plan, store=store, driver=driver)
    store.close()


def test_repair_uses_new_reservation_and_unlocks_only_after_accept(tmp_path):
    plan, store = store_case(tmp_path, "v1")
    driver = Driver(tmp_path)

    def evaluator(observation, artifact, deadline):
        rejected = artifact == tmp_path / driver.submitted[0][1]
        return {"intra_segment": "reject" if rejected else "accept", "seam": "accept"}

    quality = gate(evaluator)
    assert run_store_batch(plan, store=store, driver=driver, quality_gate=quality)["status"] == "machine_accepted"
    assert [task for task, _ in driver.submitted] == ["a", "a", "b"]
    assert store.attempt(driver.submitted[0][1])["status"] == "rejected"
    assert (tmp_path / driver.submitted[0][1]).exists()
    assert run_store_batch(plan, store=store, driver=driver, quality_gate=quality)["status"] == "machine_accepted"
    assert len(driver.submitted) == 3
    store.close()


@pytest.mark.parametrize("limit", ["round", "frames", "money"])
def test_repair_cannot_exceed_round_or_budget(tmp_path, limit):
    plan, ledger = setup(tmp_path)
    auth = ledger.inspect()["authorization"]
    if limit == "frames":
        auth = seal({**auth, "max_generated_frames": 24}, "authorization_digest")
    if limit == "money":
        auth = seal({**auth, "route_limits": {"local": 10}}, "authorization_digest")
    root = tmp_path / "store"
    root.mkdir()
    store = ProductionStore.create(root, plan, auth)
    driver = Driver(tmp_path)
    code = "BUDGET_EXCEEDED" if limit != "round" else "ATTEMPTS_EXCEEDED|REPAIR_EXHAUSTED"
    with pytest.raises(ProductionError, match=code):
        run_store_batch(plan, store=store, driver=driver,
                        quality_gate=gate(lambda *_: {"intra_segment": "reject"}))
    assert len(driver.submitted) == (3 if limit == "round" else 1)
    assert all(task == "a" for task, _ in driver.submitted)
    assert store.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == len(driver.submitted)
    store.close()


def test_uncalibrated_callback_cannot_accept(tmp_path):
    plan, store = store_case(tmp_path, "v1")
    driver = Driver(tmp_path)
    quality = gate(lambda *_: {"intra_segment": "accept", "seam": "accept"}, "uncalibrated")
    assert run_store_batch(plan, store=store, driver=driver, quality_gate=quality)["status"] == "quality_unknown"
    assert len(driver.submitted) == 1
    store.close()


@pytest.mark.parametrize("offset", range(5))
def test_reference_lighting_and_frame_mapping(offset):
    reference = [[30 + offset] * 3, [50 + offset] * 3, [70 + offset] * 3]
    expected = [100, 101, 102]
    assert reference_checks(reference, reference, expected, expected) == {
        "intra_segment": "accept", "seam": "accept"}
    tinted = [[r + 10, g, b] for r, g, b in reference]
    assert reference_checks(tinted, reference, expected, expected)["intra_segment"] == "reject"
    for bad in ([100, 102, 101], [100, 101, 101], [100, 102], [100, 200, 201]):
        assert reference_checks(reference, reference, bad, expected)["seam"] == "reject"
    assert reference_checks(None, reference, None, expected) == {
        "intra_segment": "unknown", "seam": "unknown"}


def test_motion_requires_observable_metric_reference():
    values = dict(metric_scale=True, coverage=.95, unknown_span_s=.25,
                  speed_relative_error=.1, step_relative_error=.1,
                  cross_track_p95_m=.1, endpoint_error_m=.2, event_error_s=.2)
    assert motion_checks(values) == {"speed": "accept", "path": "accept"}
    assert motion_checks(dict(values, speed_relative_error=.5))["speed"] == "reject"
    assert motion_checks(dict(values, cross_track_p95_m=1))["path"] == "reject"
    assert motion_checks(dict(values, event_error_s=1))["path"] == "reject"
    for override in (dict(metric_scale=False), dict(unknown_span_s=.51), dict(coverage=.89)):
        assert motion_checks(dict(values, **override)) == {"speed": "unknown", "path": "unknown"}


def test_fixture_gate_cannot_run_delivery(tmp_path):
    plan, store = store_case(tmp_path, "v1")
    driver = Driver(tmp_path)
    with pytest.raises(ProductionError, match="QUALITY_UNQUALIFIED"):
        run_store_batch(plan, store=store, driver=driver, quality_gate=gate(None), delivery=True)
    assert not driver.submitted
    store.close()
