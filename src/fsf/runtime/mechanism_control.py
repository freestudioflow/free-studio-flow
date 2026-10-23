"""Sample a pure mechanism model onto an FSF clock. Not a studio skill."""
from fractions import Fraction


class MechanismSampleError(ValueError):
    pass


def sample_mechanism(pose_at, readout_of, *, clock, start_frame, end_frame, provenance="model_sample"):
    """Pose and readout come from the same sampled state. Mouse capture is refused."""
    if provenance != "model_sample":
        raise MechanismSampleError("MECHANISM_PROVENANCE")
    rate = Fraction(clock["num"], clock["den"])
    if rate not in {Fraction(24), Fraction(24000, 1001)}:
        raise MechanismSampleError("MECHANISM_CLOCK")
    if not 0 <= start_frame < end_frame or end_frame - start_frame > 96:
        raise MechanismSampleError("MECHANISM_RANGE")
    frames = []
    for frame in range(start_frame, end_frame):
        pose = pose_at(frame)
        if not isinstance(pose, dict):
            raise MechanismSampleError("MECHANISM_POSE")
        frames.append({"frame": frame, "pose": pose, "readout": readout_of(pose)})
    return {"provenance": "model_sample", "clock": {"num": clock["num"], "den": clock["den"]},
            "frames": frames, "motion_review": "not_reviewed"}
