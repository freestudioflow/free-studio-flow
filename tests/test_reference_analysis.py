"""Regression anchors for the observed reference timeline."""
import io

import pytest

from fsf.production.contracts import ProductionError
from fsf.production.reference_analysis import STRIDE, _changes, _timeline


def test_variable_pts_remain_in_source_ticks():
    ticks = [0, 1000, 3000, 4000]
    timeline = _timeline([{"pts": p, "duration": 1000} for p in ticks],
                         {"time_base": "1/24000", "r_frame_rate": "24/1"})
    assert timeline["pts"] == ticks
    assert timeline["time_base"] == [1, 24000]
    assert timeline["clock"] is None
    assert timeline["timestamp_kind"] == "vfr"
    assert timeline["end_pts"] == 5000


def test_duplicate_pts_fail_closed():
    with pytest.raises(ProductionError, match="REFERENCE_PTS_UNAVAILABLE"):
        _timeline([{"pts": 10}, {"pts": 10}], {"time_base": "1/24"})


def test_spatially_local_motion_does_not_create_a_cut():
    dark = bytes(STRIDE)
    local_change = bytes([255])*96 + dark[96:]
    bright = bytes([180])*STRIDE
    cuts, fades, frames = _changes(io.BytesIO(dark + local_change + bright))
    assert cuts == [2]
    assert not fades and frames == 3
