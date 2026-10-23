from fractions import Fraction

import pytest
from fsf.runtime.mechanism_control import MechanismSampleError, sample_mechanism


def pose_at(frame):
    return {"angle": frame / 24}


def readout_of(pose):
    return {"angle": pose["angle"]}


def test_sample_uses_the_rational_clock_and_the_same_pose():
    sample = sample_mechanism(pose_at, readout_of, clock={"num": 24000, "den": 1001}, start_frame=0, end_frame=4)
    assert sample["motion_review"] == "not_reviewed"
    assert Fraction(sample["clock"]["num"], sample["clock"]["den"]) == Fraction(24000, 1001)
    assert [row["readout"]["angle"] for row in sample["frames"]] == [0, 1 / 24, 2 / 24, 3 / 24]


def test_mouse_capture_is_not_a_control_master():
    with pytest.raises(MechanismSampleError, match="MECHANISM_PROVENANCE"):
        sample_mechanism(pose_at, readout_of, clock={"num": 24, "den": 1}, start_frame=0, end_frame=1, provenance="mouse_capture")
