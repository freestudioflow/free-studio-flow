"""Diagnostics preserve original errors and redact before truncation."""
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from fsf.executors.subprocess_safety import SafeSubprocessResult
from fsf.runtime.anatomy_control import AnatomyError, _run_docker
from fsf.runtime.post_sources import PostError, file_sha


def test_sampler_failure_keeps_code_and_captures_redacted_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("FSF_SYNTHETIC_TOKEN", "synthetic-diagnostic-value")
    private_path = str(tmp_path/"private"/"job")
    result = SafeSubprocessResult(args=["docker"], returncode=19, stdout="partial",
        stderr="synthetic-diagnostic-value failed at "+private_path,
        elapsed_ms=12, timed_out=False)
    monkeypatch.setattr("fsf.runtime.anatomy_control.run_safe_subprocess", lambda *a, **k: result)
    monkeypatch.setattr("fsf.runtime.anatomy_control.shutil.which", lambda name: "docker")
    with pytest.raises(AnatomyError, match="ANATOMY_SAMPLER"):
        _run_docker(tmp_path, tmp_path/"job.json", tmp_path/"sample.json")
    path = next((tmp_path/"operation-reports").glob("*.json"))
    raw = path.read_text()
    report = json.loads(raw)
    assert report["timing"]["returncode"] == 19
    assert report["safe_code"] == "ANATOMY_SAMPLER"
    assert "synthetic-diagnostic-value" not in raw and private_path not in raw
    assert report["timing"]["elapsed_seconds"] >= 0


def test_source_stat_change_reports_without_exposing_path(tmp_path, monkeypatch):
    source = tmp_path/"source.bin"
    source.write_bytes(b"a"*32)
    diagnostics = tmp_path/"diagnostics"
    diagnostics.mkdir()
    monkeypatch.setenv("FSF_PRIVATE_DIAGNOSTICS_DIR", str(diagnostics))
    import fsf.runtime.post_sources as module
    original = module._open_reader

    @contextmanager
    def changed(path):
        with original(path) as stream:
            class Reader:
                def fileno(self):
                    return stream.fileno()
                def read(self, *args):
                    data = stream.read(*args)
                    if data:
                        import os
                        value = source.stat()
                        os.utime(source, ns=(value.st_atime_ns, value.st_mtime_ns+10_000_000))
                    return data
            yield Reader()
    monkeypatch.setattr(module, "_open_reader", changed)
    with pytest.raises(PostError, match="POST_SOURCE_CHANGED"):
        file_sha(source)
    report = json.loads(next(diagnostics.glob("*.json")).read_text())
    assert report["phase"] == "file_stat_changed"
    assert report["before"] != report["after"]
    assert report["observed_monotonic_ns"] > 0
    assert str(source) not in json.dumps(report)
