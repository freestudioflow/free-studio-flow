import json
from importlib import resources
from pathlib import Path

import pytest
from fsf.remote.contracts import RemoteError
from fsf.remote.dispatch_hold import write_submit_hold
from fsf.remote.kaggle_api import call as kaggle_v1_call
from fsf.remote.kaggle_native import call as kaggle_native_call
from fsf.remote.kaggle_v2_api import call as kaggle_v2_call
from fsf.remote.native_ssh import NativeSsh
from fsf.remote.transports import SshAdapter
from fsf.remote.v2_transports import V2Adapter
from fsf.runtime.anatomy_control import AnatomyError, import_anatomy_control, sample_anatomy
from fsf.runtime.blender import verify_bundle


CHECKOUT = Path(__file__).resolve().parents[3] / "anatomy-1482e846"
JOB = {"order": ["prop"], "stagger": 0.035, "stiffness": 64, "damping": 14.2, "substeps": 4}


def _checkout():
    if not (CHECKOUT / "kit" / "audit.mjs").is_file():
        pytest.skip("pinned anatomy checkout is not available")
    return CHECKOUT


def test_docker_sampler_registers_control_video(tmp_path):
    result = import_anatomy_control(_checkout(), {**JOB, "clock": {"num": 24, "den": 1}}, tmp_path,
        control_id="prop_A", runner="docker")
    manifest = verify_bundle(tmp_path / "controls" / result["control"]["bundle"])
    assert manifest["provenance"] == "operator_control_video_import"
    assert manifest["motion_review"] == "not_reviewed"
    assert manifest["frame_count"] == result["frames"]
    assert 1 <= result["frames"] <= 96
    sample = json.loads((tmp_path / "sample.json").read_text(encoding="utf-8"))
    assert sample["frames"][0]["readout"]["shares"] == sample["frames"][0]["pose"]["shares"]
    share = sample["frames"][0]["pose"]["shares"]["prop"]
    assert 0 < share < 1


def test_process_runner_samples_the_fractional_clock(tmp_path):
    sample = sample_anatomy(_checkout(), {**JOB, "clock": {"num": 24000, "den": 1001}}, tmp_path / "sample.json")
    assert sample["clock"] == {"num": 24000, "den": 1001}
    assert sample["motion_review"] == "not_reviewed"


def test_mouse_capture_never_starts_the_model(tmp_path):
    with pytest.raises(AnatomyError, match="MECHANISM_PROVENANCE"):
        sample_anatomy(_checkout(), {**JOB, "clock": {"num": 24, "den": 1}, "provenance": "mouse_capture"},
            tmp_path / "sample.json")


def test_held_dispatch_stops_before_kaggle_or_ssh(tmp_path, monkeypatch):
    visible = [{"index": index, "uuid": f"GPU-01234567-89ab-cdef-0123-456789abcde{index}",
        "name": f"GPU {index}", "memory_mb": 24576} for index in range(4)]
    segments = [{"segment_id": name, "device_count": 1} for name in ("a", "b", "c")]
    held = write_submit_hold(tmp_path, transport="kaggle", visible=visible, authorized=3, segments=segments)
    assert held["authorized"] == 3 and held["assigned"] == ["a", "b", "c"] and held["occupied"]
    payload = {"operation": "submit", "directory": str(tmp_path)}
    with pytest.raises(RemoteError, match="REMOTE_SUBMIT_HELD"):
        kaggle_v1_call(payload)
    with pytest.raises(RemoteError, match="REMOTE_SUBMIT_HELD"):
        kaggle_v2_call(payload)
    with pytest.raises(RemoteError, match="REMOTE_SUBMIT_HELD"):
        kaggle_native_call(payload)
    profile = json.loads(resources.files("fsf.remote.templates").joinpath("ssh.profile.json").read_text())
    with pytest.raises(RemoteError, match="REMOTE_SUBMIT_HELD"):
        SshAdapter(profile).submit({}, None, tmp_path)
    with pytest.raises(RemoteError, match="REMOTE_SUBMIT_HELD"):
        NativeSsh(SshAdapter(profile), str(tmp_path / "missing-store.json")).submit({}, tmp_path)
    calls = []
    for transport in ('kaggle', 'ssh'):
        adapter = object.__new__(V2Adapter)
        adapter.profile = {'transport': transport}
        monkeypatch.setattr(adapter, '_kaggle', lambda *a, **kw: calls.append('kaggle'))
        monkeypatch.setattr(adapter, '_native', lambda: calls.append('native'))
        monkeypatch.setattr(adapter, '_ssh', lambda: calls.append('ssh'))
        with pytest.raises(RemoteError, match='REMOTE_SUBMIT_HELD'):
            adapter.submit({'request_digest': 'not-used'}, None, tmp_path)
    assert calls == []


@pytest.mark.parametrize('name', ['LICENSE', 'kit/audit.mjs', 'kit/iso-kit.mjs', 'kit/lathe.mjs', 'kit/tube.mjs'])
def test_checked_source_cannot_be_modified(name, tmp_path):
    from fsf.runtime.anatomy_control import _SOURCE_HASHES, verify_anatomy_checkout
    for entry in _SOURCE_HASHES:
        target = tmp_path / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((_checkout() / entry).read_bytes())
    target = tmp_path / name
    target.write_bytes(target.read_bytes() + b'\nmodified')
    with pytest.raises(AnatomyError, match='ANATOMY_LICENSE' if name == 'LICENSE' else 'ANATOMY_PIN'):
        verify_anatomy_checkout(tmp_path)


def test_control_draws_each_part_and_readout_from_same_state():
    from fsf.runtime.anatomy_control import render_mechanism_frame
    shares = {'a': 0.0, 'b': 1.0}
    row = {'frame': 0, 'pose': {'shares': shares}, 'readout': {'shares': shares}}
    image = render_mechanism_frame(row)
    assert image.getpixel((100, 48)) == (230, 165, 66)
    assert image.getpixel((256, 160)) == (230, 165, 66)
    changed = {'a': 1.0, 'b': 0.0}
    other = render_mechanism_frame({'frame': 1, 'pose': {'shares': changed}, 'readout': {'shares': changed}})
    assert other.getpixel((100, 48)) != image.getpixel((100, 48))
    assert other.getpixel((256, 160)) != image.getpixel((256, 160))
    assert len(set(image.getdata())) > 4
    with pytest.raises(AnatomyError, match='ANATOMY_READOUT'):
        render_mechanism_frame({**row, 'readout': {'shares': changed}})
