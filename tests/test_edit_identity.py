"""Edit identities, revision fencing and unambiguous timeline location."""
import copy
import json
from pathlib import Path

import pytest

from fsf.conformance.digest import canonical_contract_digest
from fsf.core.editing import (
    EditValidationError, apply_edit_patch, create_edit, inspect_edit,
    locate_clips, timing_anchor_digest, validate_edit, diff_edits,
)


def edit_fixture(*, tracks=None, subtitles=None):
    source_ref = {
        "source_execution_id": "exec_A", "source_job_id": "job_A", "source_run_id": "run_A",
        "source_plan_digest": "a" * 64, "source_journal_digest": "b" * 64,
        "producer_step_id": "step_001_T01_generate", "artifact_ref": "artifact:T01.mp4",
        "artifact_sha256": "c" * 64, "purpose": "video",
    }
    return create_edit(
        edit_id="film_A", clock_rate={"num": 24, "den": 1},
        sources=[{"source_id": "S01", "shot_id": "T01", "label": "Officer", "duration_frames": 240,
                  "source_ref": source_ref}],
        clips=[{"clip_id": "C01", "source_id": "S01", "label": "Officer wide", "in_frame": 24, "out_frame": 120, "lineage": []},
               {"clip_id": "C02", "source_id": "S01", "label": "Officer close", "in_frame": 120, "out_frame": 240, "lineage": []}],
        audio_tracks=tracks, subtitle_tracks=subtitles,
    )


def patch(edit, *ops):
    return {"schema_version": "fsf_edit_patch_v1", "edit_id": edit["edit_id"],
            "base_revision": edit["revision"], "base_digest": edit["edit_digest"], "operations": list(ops)}


def reseal(edit):
    edit["edit_digest"] = canonical_contract_digest({k: v for k, v in edit.items() if k != "edit_digest"})


def relative_track():
    return {"schema_version": "fsf_edit_subtitle_track_v1", "track_id": "SUB01", "clock_rate": {"num": 24, "den": 1},
            "timebase": "clip_relative", "cues": [{"cue_id": "Q01", "clip_id": "C01", "start_frame": 0, "end_frame": 24, "text": "Signal detected."}]}


def test_inspect_has_shared_ids_and_distinct_source_and_output_time():
    edit = edit_fixture(tracks=[{"track_id": "VO01", "role": "vo", "label": "Narrator"}])
    view = inspect_edit(edit)
    assert view["revision"] == 1 and view["total_frames"] == 216
    assert view["clips"][0]["timeline_start"] == "00:00:00.000"
    assert view["clips"][0]["source_in"] == "00:00:01.000"
    assert view["clips"][1]["timeline_start_frame"] == 96
    assert view["audio_tracks"][0]["track_id"] == "VO01"
    view["clips"][0]["label"] = "Changed view"
    assert edit["clips"][0]["label"] == "Officer wide"


def test_reorder_preserves_identity_and_repositions_relative_subtitle():
    edit = edit_fixture(subtitles=[relative_track()])
    before = copy.deepcopy(edit)
    changed = apply_edit_patch(edit, patch(edit, {"op": "reorder", "clip_ids": ["C02", "C01"]}))
    assert edit == before
    assert changed["revision"] == 2 and changed["parent_digest"] == edit["edit_digest"]
    assert inspect_edit(changed)["cues"][0]["timeline_start_frame"] == 120
    assert changed["clips"][1]["clip_id"] == "C01"


def test_three_revisions_reject_old_locators_and_patches():
    first = edit_fixture()
    second = apply_edit_patch(first, patch(first, {"op": "reorder", "clip_ids": ["C02", "C01"]}))
    third = apply_edit_patch(second, patch(second, {"op": "rename", "clip_id": "C01", "label": "Antenna"}))
    assert third["revision"] == 3
    with pytest.raises(EditValidationError, match="outdated"):
        apply_edit_patch(third, patch(first, {"op": "remove", "clip_id": "C02"}))
    with pytest.raises(EditValidationError, match="outdated"):
        locate_clips(third, revision=first["revision"], digest=first["edit_digest"], position=1)
    assert locate_clips(third, revision=3, digest=third["edit_digest"], position=1)[0]["clip_id"] == "C02"


def test_ambiguous_label_returns_all_candidates_and_frame_boundaries_are_half_open():
    edit = edit_fixture()
    args = {"revision": 1, "digest": edit["edit_digest"]}
    assert len(locate_clips(edit, label="Officer", **args)) == 2
    assert locate_clips(edit, frame=95, **args)[0]["clip_id"] == "C01"
    assert locate_clips(edit, frame=96, **args)[0]["clip_id"] == "C02"
    with pytest.raises(EditValidationError):
        locate_clips(edit, frame=216, **args)


def test_duplicate_and_split_have_distinct_ids_and_lineage():
    edit = edit_fixture()
    dupe = apply_edit_patch(edit, patch(edit, {"op": "duplicate", "clip_id": "C01", "new_clip_id": "C03"}))
    assert [c["clip_id"] for c in dupe["clips"]] == ["C01", "C03", "C02"]
    assert dupe["clips"][1]["lineage"] == ["C01"]
    assert dupe["clips"][1]["source_id"] == dupe["clips"][0]["source_id"]
    split = apply_edit_patch(dupe, patch(dupe, {"op": "split", "clip_id": "C03", "at_frame": 48, "new_clip_ids": ["C04", "C05"]}))
    assert split["retired_clip_ids"] == ["C03"]
    assert [(c["in_frame"], c["out_frame"]) for c in split["clips"][1:3]] == [(24, 72), (72, 120)]
    assert inspect_edit(split)["total_frames"] == inspect_edit(dupe)["total_frames"]


def test_deleted_id_cannot_be_reused_or_resolved_by_old_ordinal():
    edit = edit_fixture()
    changed = apply_edit_patch(edit, patch(edit, {"op": "remove", "clip_id": "C01"}))
    with pytest.raises(EditValidationError):
        apply_edit_patch(changed, patch(changed, {"op": "duplicate", "clip_id": "C02", "new_clip_id": "C01"}))
    with pytest.raises(EditValidationError):
        locate_clips(changed, revision=2, digest=changed["edit_digest"], clip_id="C01")


@pytest.mark.parametrize("op", [
    {"op": "reorder", "clip_ids": ["C01"]},
    {"op": "reorder", "clip_ids": ["C01", "C01"]},
    {"op": "duplicate", "clip_id": "C01", "new_clip_id": "C02"},
    {"op": "split", "clip_id": "C01", "at_frame": 96, "new_clip_ids": ["C03", "C04"]},
    {"op": "split", "clip_id": "C01", "at_frame": 0, "new_clip_ids": ["C03", "C04"]},
    {"op": "remove", "clip_id": "MISSING"},
    {"op": "run_script", "clip_id": "C01"},
])
def test_invalid_patch_is_atomic(op):
    edit = edit_fixture(); before = copy.deepcopy(edit)
    with pytest.raises(EditValidationError):
        apply_edit_patch(edit, patch(edit, {"op": "rename", "clip_id": "C01", "label": "Temporary"}, op))
    assert edit == before


@pytest.mark.parametrize("start,end", [(24, 24), (24, 12), (0, 97)])
@pytest.mark.parametrize("timebase", ["clip_relative", "timeline_absolute"])
def test_subtitle_semantic_intervals_rejected(start, end, timebase):
    edit = edit_fixture(); track = relative_track()
    track["cues"][0].update(start_frame=start, end_frame=end)
    if timebase == "timeline_absolute":
        track["timebase"] = timebase; track["cues"][0].pop("clip_id")
        track["timing_anchor_digest"] = timing_anchor_digest(edit)
        if end == 97: track["cues"][0]["end_frame"] = 217
    with pytest.raises(EditValidationError):
        edit_fixture(subtitles=[track])


def test_absolute_subtitle_requires_explicit_new_anchor_after_reorder():
    edit = edit_fixture(); track = relative_track()
    track["timebase"] = "timeline_absolute"; track["cues"][0].pop("clip_id")
    track["timing_anchor_digest"] = timing_anchor_digest(edit)
    edit = edit_fixture(subtitles=[track])
    with pytest.raises(EditValidationError, match="anchor"):
        apply_edit_patch(edit, patch(edit, {"op": "reorder", "clip_ids": ["C02", "C01"]}))
    changed = apply_edit_patch(edit, patch(edit, {"op": "reorder", "clip_ids": ["C02", "C01"]}, {"op": "confirm_timeline_anchor", "track_id": "SUB01"}))
    assert changed["subtitle_tracks"][0]["timing_anchor_digest"] == timing_anchor_digest(changed)
    renamed = apply_edit_patch(changed, patch(changed, {"op": "rename", "clip_id": "C01", "label": "New label"}))
    assert timing_anchor_digest(renamed) == timing_anchor_digest(changed)


def test_remove_cannot_silently_drop_referenced_subtitles():
    edit = edit_fixture(subtitles=[relative_track()])
    with pytest.raises(EditValidationError, match="missing clip"):
        apply_edit_patch(edit, patch(edit, {"op": "remove", "clip_id": "C01"}))


@pytest.mark.parametrize("mutation", ["clip", "source", "track", "clock", "digest"])
def test_invalid_edit_metadata_rejected(mutation):
    edit = edit_fixture()
    if mutation == "clip": edit["clips"].append(copy.deepcopy(edit["clips"][0]))
    elif mutation == "source": edit["clips"][0]["source_id"] = "MISSING"
    elif mutation == "track": edit["audio_tracks"] = [{"track_id": "X", "role": "vo", "label": ""}] * 2
    elif mutation == "clock": edit["clock_rate"]["den"] = 0
    else: edit["clips"][0]["label"] = "Tampered"
    if mutation != "digest": reseal(edit)
    with pytest.raises(EditValidationError): validate_edit(edit)


def test_non_integer_clock_renders_timecodes_without_float_drift():
    edit = edit_fixture(); edit["clock_rate"] = {"num": 24000, "den": 1001}; reseal(edit)
    assert inspect_edit(edit)["clips"][0]["source_in"] == "00:00:01.001"


def test_diff_reports_old_and_new_locations_and_rejects_other_edit():
    edit = edit_fixture()
    changed = apply_edit_patch(edit, patch(edit, {"op": "reorder", "clip_ids": ["C02", "C01"]}))
    diff = diff_edits(edit, changed)
    assert len(diff["clip_changes"]) == 2
    c1 = diff["clip_changes"][0]
    assert c1["clip_id"] == "C01"
    assert c1["before"]["position"] == 1 and c1["after"]["position"] == 2
    assert c1["before"]["timeline_start_frame"] == 0 and c1["after"]["timeline_start_frame"] == 120
    other = copy.deepcopy(edit); other["edit_id"] = "OTHER"; reseal(other)
    with pytest.raises(EditValidationError): diff_edits(edit, other)


def test_lineage_cycle_rejected_even_with_matching_digest():
    edit = edit_fixture()
    edit["clips"][0]["lineage"] = ["C02"]
    edit["clips"][1]["lineage"] = ["C01"]
    reseal(edit)
    with pytest.raises(EditValidationError, match="Cyclic"):
        validate_edit(edit)


def test_boolean_revision_cannot_select_first_revision():
    edit = edit_fixture()
    with pytest.raises(EditValidationError):
        locate_clips(edit, revision=True, digest=edit["edit_digest"], position=1)


@pytest.mark.parametrize("filename", ["edit_post_plan_shape.json", "edit_blender_plan_shape.json"])
def test_post_and_blender_envelopes_fit_pdx_shape_only(filename):
    # Future operation names test PDX envelope compatibility, not dispatch.
    # These minimal shapes are not executable media/render demonstrations.
    from pdx_artifact_core import validate_execution_plan

    plan = json.loads((Path(__file__).parent / "fixtures" / filename).read_text("utf-8"))
    assert validate_execution_plan(plan) == []
