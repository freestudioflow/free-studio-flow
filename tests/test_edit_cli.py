"""Subprocess edit commands, sanitized errors and cooperative writer races."""
import copy
import itertools
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fsf.core.edit_store import edit_lock
from test_edit_identity import edit_fixture, patch

ROOT = Path(__file__).resolve().parents[1]


def cli(*args, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "fsf.cli", "edit", *map(str, args)],
                          cwd=cwd or ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=35)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def definition():
    edit = edit_fixture()
    return {"schema_version": "fsf_edit_definition_v1", **{key: edit[key] for key in
            ("edit_id", "clock_rate", "sources", "clips", "audio_tracks", "subtitle_tracks")}}


def result(completed, code=0):
    assert completed.returncode == code, completed.stdout
    assert completed.stderr == ""
    body = json.loads(completed.stdout)
    assert completed.stdout.count("\n") == 1
    assert body["ok"] == (code == 0)
    return body


def setup_edit(tmp_path):
    source = write(tmp_path / "definition.json", definition())
    target = tmp_path / "edit.json"
    edit = result(cli("create", source, "--out", target))["result"]
    return target, edit


def test_cli_three_revisions_inspect_locate_diff_and_read_only(tmp_path):
    target, edit = setup_edit(tmp_path)
    original = write(tmp_path / "original.json", edit)
    for index in range(2):
        payload = write(tmp_path / "patch.json", patch(edit, {"op": "rename", "clip_id": "C01", "label": f"Shared label {index}"}))
        previous = edit
        edit = result(cli("patch", target, "--patch", payload))["result"]
        assert edit["revision"] == previous["revision"] + 1
        assert edit["parent_digest"] == previous["edit_digest"]
    before = target.read_bytes()
    view = result(cli("inspect", target))["result"]
    assert view["revision"] == 3
    located = result(cli("locate", target, "--revision", "3", "--digest", edit["edit_digest"], "--label", "Shared label 1"))["result"]
    assert located[0]["clip_id"] == "C01"
    change = result(cli("diff", original, target))["result"]
    assert change["before_revision"] == 1 and change["after_revision"] == 3
    assert target.read_bytes() == before
    stale = result(cli("locate", target, "--revision", "1", "--digest", previous["edit_digest"], "--frame", "0"), 4)
    assert stale["error"]["code"] == "EDIT_CONFLICT" and "candidates" not in stale


@pytest.mark.parametrize("flag,value", [("--clip-id", "C01"), ("--position", "1"), ("--frame", "0"), ("--label", "Officer wide")])
def test_each_locator_and_option_order(tmp_path, flag, value):
    target, edit = setup_edit(tmp_path)
    found = result(cli("locate", "--revision", "1", flag, value, target, "--digest", edit["edit_digest"]))
    assert found["result"][0]["clip_id"] == "C01"


@pytest.mark.parametrize("arguments", [
    [], ["--frame", "0", "--position", "1"], ["--frame", "0", "--frame", "0"],
    ["--revision", "1", "--frame", "0"], ["--digest", "a" * 64, "--frame", "0"],
    ["--revision", "true", "--digest", "a" * 64, "--frame", "0"],
    ["--revision", "1.5", "--digest", "a" * 64, "--frame", "0"],
    ["--revision", "-1", "--digest", "a" * 64, "--frame", "0"],
    ["--revision", "0", "--digest", "a" * 64, "--frame", "0"],
    ["--revision", "1", "--digest", "bad", "--frame", "0"],
    ["--revision", "1", "--digest", "a" * 64, "--position", "0"],
    ["--revision", "1", "--digest", "a" * 64, "--label"],
    ["--revision", "1", "--digest", "a" * 64, "--label", " "],
    ["--revision", "1", "--digest", "a" * 64, "--fra", "0"],
    ["--unknown", "SYNTHETIC_SECRET /private/workstation"],
])
def test_usage_errors_are_single_sanitized_json(arguments):
    completed = cli("locate", "missing.json", *arguments)
    body = result(completed, 2)
    assert body["error"]["code"] == "EDIT_USAGE"
    assert "SYNTHETIC_SECRET" not in completed.stdout and "Traceback" not in completed.stdout


def test_all_locator_pairs_rejected_before_io(tmp_path):
    pairs = [("--clip-id", "C01"), ("--position", "1"), ("--frame", "0"), ("--label", "label")]
    for left, right in itertools.combinations(pairs, 2):
        body = result(cli("locate", "missing.json", "--revision", "1", "--digest", "a" * 64, *left, *right), 2)
        assert body["error"]["code"] == "EDIT_USAGE"
    for count in (3, 4):
        flags = list(itertools.chain.from_iterable(pairs[:count]))
        assert result(cli("locate", "missing.json", "--revision", "1", "--digest", "a" * 64, *flags), 2)["error"]["code"] == "EDIT_USAGE"


@pytest.mark.parametrize("flag,value", [("--revision", "1"), ("--digest", "a" * 64), ("--frame", "0")])
def test_duplicate_flag_rejected_with_otherwise_complete_arguments(flag, value):
    body = result(cli("locate", "missing.json", "--revision", "1", "--digest", "a" * 64, "--frame", "0", flag, value), 2)
    assert body["error"]["code"] == "EDIT_USAGE"


def test_ambiguous_missing_and_half_open_boundary(tmp_path):
    target, edit = setup_edit(tmp_path)
    base = ["locate", target, "--revision", "1", "--digest", edit["edit_digest"]]
    body = result(cli(*base, "--label", "Officer"), 5)
    assert body["error"]["code"] == "EDIT_AMBIGUOUS" and len(body["candidates"]) == 2
    assert all("label" not in row for row in body["candidates"])
    assert result(cli(*base, "--frame", "96"))["result"][0]["clip_id"] == "C02"
    assert result(cli(*base, "--frame", "216"), 5)["error"]["code"] == "EDIT_NOT_FOUND"


@pytest.mark.parametrize("forbidden", ["revision", "parent_digest", "edit_digest", "retired_clip_ids", "unknown"])
def test_create_definition_rejects_generated_and_unknown_fields(tmp_path, forbidden):
    value = definition()
    value[forbidden] = "SYNTHETIC_SECRET"
    source = write(tmp_path / "bad.json", value)
    target = tmp_path / "edit.json"
    body = result(cli("create", source, "--out", target), 3)
    assert body["error"]["code"] == "EDIT_INVALID" and not target.exists()
    assert "SYNTHETIC_SECRET" not in json.dumps(body)


def test_errors_io_invalid_conflict_exists_and_duplicate_flags(tmp_path):
    target, edit = setup_edit(tmp_path)
    source = tmp_path / "definition.json"
    assert result(cli("create", source, "--out", target), 8)["error"]["code"] == "EDIT_TARGET_EXISTS"
    assert result(cli("inspect", tmp_path / "secret-path.json"), 6)["error"]["code"] == "EDIT_IO"
    bad = tmp_path / "bad.json"
    bad.write_text('{"x":1,"x":2}', encoding="utf-8")
    assert result(cli("create", bad, "--out", tmp_path / "bad-edit.json"), 3)["error"]["code"] == "EDIT_INVALID"
    payload = write(tmp_path / "patch.json", patch(edit, {"op": "rename", "clip_id": "C01", "label": "New"}))
    result(cli("patch", target, "--patch", payload))
    assert result(cli("patch", target, "--patch", payload), 4)["error"]["code"] == "EDIT_CONFLICT"
    assert result(cli("patch", target, "--patch", payload, "--patch", payload), 2)["error"]["code"] == "EDIT_USAGE"


def _cli_race(arguments, barrier, queue):
    barrier.wait(25)
    completed = cli(*arguments)
    queue.put((completed.returncode, json.loads(completed.stdout), completed.stderr))


@pytest.mark.parametrize("create", [False, True])
def test_pure_cli_independent_writer_races_ten_rounds(tmp_path, create):
    ctx = mp.get_context("spawn")
    for index in range(10):
        folder = tmp_path / str(index)
        folder.mkdir()
        source = write(folder / "definition.json", definition())
        target = folder / "edit.json"
        if create:
            arguments = ("create", str(source), "--out", str(target))
        else:
            edit = result(cli("create", source, "--out", target))["result"]
            payload = write(folder / "patch.json", patch(edit, {"op": "rename", "clip_id": "C01", "label": "New"}))
            arguments = ("patch", str(target), "--patch", str(payload))
        barrier, queue = ctx.Barrier(3), ctx.Queue()
        workers = [ctx.Process(target=_cli_race, args=(arguments, barrier, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            barrier.wait(25)
            outputs = [queue.get(timeout=40) for _ in workers]
            assert sorted(row[0] for row in outputs) == [0, 8 if create else 4]
            assert all(row[2] == "" for row in outputs)
            current = result(cli("inspect", target))["result"]
            assert current["revision"] == (1 if create else 2)
        finally:
            for worker in workers:
                worker.join(5)
                if worker.is_alive():
                    worker.kill()
                    worker.join(5)
            queue.close()
            queue.join_thread()


def test_cli_lock_timeout_has_stable_code(tmp_path):
    target, edit = setup_edit(tmp_path)
    payload = write(tmp_path / "patch.json", patch(edit, {"op": "rename", "clip_id": "C01", "label": "New"}))
    before = target.read_bytes()
    with edit_lock(target):
        body = result(cli("patch", target, "--patch", payload), 7)
    assert body["error"]["code"] == "EDIT_LOCK_TIMEOUT" and target.read_bytes() == before


def test_help_is_discoverable_and_subcommands_show_real_flags():
    for arguments in [("--help",), ("locate", "--help"), ("create", "--help")]:
        completed = cli(*arguments)
        assert completed.returncode == 0 and completed.stderr == ""
        assert "usage:" in completed.stdout


def test_optional_definition_tracks_and_schema_match_edit_fields(tmp_path):
    from importlib import resources
    spec = json.loads(resources.files("fsf.schemas").joinpath("fsf_edit_spec_v1.schema.json").read_text())
    schema = json.loads(resources.files("fsf.schemas").joinpath("fsf_edit_definition_v1.schema.json").read_text())
    for key in ("edit_id", "clock_rate", "sources", "clips", "audio_tracks", "subtitle_tracks"):
        assert schema["properties"][key] == spec["properties"][key]
    value = definition()
    del value["audio_tracks"]
    del value["subtitle_tracks"]
    source = write(tmp_path / "minimal.json", value)
    edit = result(cli("create", source, "--out", tmp_path / "edit.json"))["result"]
    assert edit["revision"] == 1 and edit["parent_digest"] is None
    assert edit["retired_clip_ids"] == edit["audio_tracks"] == edit["subtitle_tracks"] == []


def test_unexpected_error_is_sanitized_without_exception_chain(monkeypatch, capsys):
    from fsf.cli import edit as command
    def fail(*args):
        raise RuntimeError("SYNTHETIC_SECRET arbitrary input")
    monkeypatch.setattr(command, "read_edit", fail)
    assert command.main(["inspect", "missing.json"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["error"]["code"] == "EDIT_INTERNAL"
    assert "SYNTHETIC_SECRET" not in output.out and "Traceback" not in output.out


@pytest.mark.parametrize("raw", ['{"schema_version":"SYNTHETIC_SECRET"}', '{"x":NaN}', '{"x":1,"x":2}', '[]'])
def test_malformed_definition_errors_do_not_echo_content(tmp_path, raw):
    source = tmp_path / "invalid.json"
    source.write_text(raw, encoding="utf-8")
    completed = cli("create", source, "--out", tmp_path / "edit.json")
    assert result(completed, 3)["error"]["code"] == "EDIT_INVALID"
    assert "SYNTHETIC_SECRET" not in completed.stdout


def test_top_level_help_lists_edit_command():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run([sys.executable, "-m", "fsf.cli", "--help"], cwd=ROOT,
                               env=env, capture_output=True, text=True, timeout=35)
    assert completed.returncode == 0 and "edit" in completed.stdout


def test_unicode_label_survives_ascii_stdout_pipe(tmp_path):
    value = definition()
    label = "\u93e1\u982d \U0001f3ac"
    value["clips"][0]["label"] = label
    source = write(tmp_path / "unicode.json", value)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "ascii"
    completed = subprocess.run([sys.executable, "-m", "fsf.cli", "edit", "create", str(source),
                                "--out", str(tmp_path / "edit.json")], cwd=ROOT, env=env,
                               capture_output=True, text=True, encoding="ascii", timeout=35)
    assert result(completed)["result"]["clips"][0]["label"] == label
