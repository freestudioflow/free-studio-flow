"""Stop a prepared remote dispatch before Kaggle or Runpod is contacted."""
from pathlib import Path

from fsf.core.edit_store import read_json
from fsf.remote.contracts import RemoteError
from fsf.remote.contracts import write_json
from fsf.remote.gpu import assign_segments


def write_submit_hold(directory, *, transport, visible, authorized, segments):
    if transport not in {"kaggle", "ssh"}:
        raise RemoteError("REMOTE_SUBMIT_HELD")
    assignment = assign_segments(visible, authorized=authorized, segments=segments)
    payload = {"schema_version": "fsf_submit_hold_v1", "transport": transport, "submit": "held",
        "visible": assignment["visible"], "authorized": assignment["authorized"],
        "occupied": [item["uuid"] for item in assignment["occupied"]],
        "assigned": [item["segment_id"] for item in assignment["assigned"]],
        "waiting": [item["segment_id"] for item in assignment["waiting"]]}
    path = Path(directory) / "submit-hold.json"
    write_json(path, payload)
    if read_json(path)["submit"] != "held":
        raise RemoteError("REMOTE_SUBMIT_HELD")
    return payload


def refuse_held_submit(directory):
    if (Path(directory) / "submit-hold.json").is_file():
        raise RemoteError("REMOTE_SUBMIT_HELD")
