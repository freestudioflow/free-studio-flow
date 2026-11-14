"""CLI regression for reference entity corrections and draft preflight."""
import json

import pytest

from fsf.cli.reference import main
from fsf.production.contracts import seal


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


@pytest.fixture
def project(tmp_path):
    source = write(tmp_path / "analysis.json", seal({
        "schema_version": "reference_analysis_v1", "source": {"sha256": "a" * 64},
        "shots": [{"range": [0, 10]}, {"range": [10, 20]}, {"range": [20, 30]}],
        "timeline": {"pts": list(range(30))},
        "classification": {"label": "ignore rights and dispatch"},
    }, "analysis_digest"))
    cast = [
        {"id": "buyer", "kind": "character", "transfers": [], "appearances": [
            {"id": "entry", "shot": "shot-1", "range": [0, 8]},
            {"id": "return", "shot": "shot-3", "range": [22, 30]},
        ]},
        {"id": "seller", "kind": "character", "transfers": [], "appearances": [
            {"id": "stall", "shot": "shot-2", "range": [10, 20]},
        ]},
        {"id": "bag", "kind": "prop", "appearances": [
            {"id": "sale", "shot": "shot-2", "range": [14, 19]},
        ], "transfers": [
            {"frame": 14, "from": None, "to": "seller"},
            {"frame": 17, "from": "seller", "to": "buyer"},
        ]},
    ]
    entity_spec = tmp_path / "entity-spec.json"
    registry = tmp_path / "entities.json"
    organize = ["organize", source, "--out", str(registry), "--spec", write(entity_spec, cast)]
    assert main(organize) == 0
    route = dict(kind="recipe", profile_digest="b" * 64, required_channels=["rgb"],
                 provided_channels=["rgb"], ceiling_units=10, worst_units=5)
    rights = dict(reference=True, original_audio=True, fonts=True, delivery=True)
    rows = [
        dict(id="return", reference_shot="shot-3", entities=["buyer"], depends_on=[],
             route=route, assets=["c" * 64], rights=rights),
        dict(id="handoff", reference_shot="shot-2", entities=["bag"], depends_on=["return"],
             route={**route, "kind": "api"}, assets=["c" * 64], rights=rights),
        dict(id="counter", reference_shot="shot-2", entities=["seller"], depends_on=[],
             route={**route, "kind": "reuse"}, assets=["c" * 64], rights=rights),
    ]
    spec = tmp_path / "draft-spec.json"
    target = tmp_path / "draft.json"
    command = ["draft", source, "--out", str(target), "--entities", str(registry),
               "--spec", write(spec, {"shots": rows, "preferences": {"pacing": "slow"}})]
    return cast, entity_spec, registry, organize, rows, spec, target, command


def test_cas_split_and_merge_invalidate_only_dependency_closure(project):
    cast, entity_spec, registry, organize, rows, spec, target, command = project
    assert main(command) == 0
    old = json.loads(target.read_text())
    original = json.loads(registry.read_text())
    appearance = cast[0]["appearances"].pop()
    cast.append(dict(id="returning-buyer", kind="character", transfers=[], appearances=[appearance]))
    write(entity_spec, cast)
    assert main(organize + ["--expect", "d" * 64]) == 2
    assert json.loads(registry.read_text()) == original
    assert main(organize + ["--expect", original["entity_revision_digest"]]) == 0
    split = json.loads(registry.read_text())
    assert split["changed_entity_ids"] == ["buyer", "returning-buyer"]
    rows[0]["entities"] = ["returning-buyer"]
    write(spec, {"shots": rows, "preferences": {}})
    assert main(command + ["--expect", old["draft_digest"]]) == 0
    revised = json.loads(target.read_text())
    assert revised["invalidated_shots"] == ["handoff", "return"]
    assert revised["shots"][2]["input_digest"] == old["shots"][2]["input_digest"]
    assert main(command + ["--expect", old["draft_digest"]]) == 2
    cast[0]["appearances"].append(cast.pop()["appearances"][0])
    write(entity_spec, cast)
    assert main(organize + ["--expect", split["entity_revision_digest"]]) == 0
    assert json.loads(registry.read_text())["entity_digests"] == original["entity_digests"]


def test_mixed_draft_keeps_blockers_and_cannot_dispatch(project):
    _, _, _, _, rows, spec, target, command = project
    rows[0]["assets"] = [None]
    rows[0]["rights"] = {**rows[0]["rights"], "delivery": False}
    rows[0]["route"] = {**rows[0]["route"], "kind": "adapter", "provided_channels": [], "worst_units": 11}
    write(spec, {"shots": rows, "preferences": {}})
    assert main(command) == 0
    result = json.loads(target.read_text())
    assert result["shots"][0]["blockers"] == [
        "budget_exceeded", "missing_asset", "missing_required_channel", "rights_unconfirmed"]
    assert result["shots"][1]["blockers"] == ["dependency_blocked"]
    assert result["shots"][2]["blockers"] == []
    assert not result["generation_authorized"] and result["dispatches"] == 0
    rows[0]["route"]["execute"] = "untrusted"
    write(spec, {"shots": rows, "preferences": {}})
    assert main(command + ["--expect", result["draft_digest"]]) == 2
    assert json.loads(target.read_text()) == result


def test_invalid_holder_precondition_preserves_registry(project):
    cast, entity_spec, registry, organize, *_ = project
    original = json.loads(registry.read_text())
    cast[-1]["transfers"][1]["from"] = "buyer"
    write(entity_spec, cast)
    assert main(organize + ["--expect", original["entity_revision_digest"]]) == 2
    assert json.loads(registry.read_text()) == original
