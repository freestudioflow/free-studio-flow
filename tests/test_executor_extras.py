"""Operator-added API/local executors and local transport shape (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fsf.cli.main import main
from fsf.executors.extras import executors_from_config, executors_from_file
from fsf.executors.registry import DEFAULT_REGISTRY, ExecutorRegistry
from fsf.intake.compiler import compile_job
from fsf.live.local_check import verify_local_transport
from fsf.studio.runner import run_studio_flow


def _sample_extras() -> dict:
    return {
        "schema_version": "fsf_executors_v1",
        "executors": [
            {
                "executor_id": "api.test.video",
                "transport": "api",
                "tool_name": "studio.video.generate",
                "display_name": "Test extra API",
                "key_env": "FSF_TEST_API_KEY",
                "endpoint_env": "FSF_TEST_API_BASE",
            },
            {
                "executor_id": "local.test.video",
                "transport": "local",
                "tool_name": "studio.video.generate",
                "display_name": "Test extra local",
                "require_env": "FSF_TEST_LOCAL_AVAILABLE",
            },
        ],
    }


def test_local_transport_shape_exists_without_live_model(
    sample_docx: Path, tmp_path: Path
) -> None:
    report = verify_local_transport(sample_docx, tmp_path / "local", ping_live_model=False)
    assert report["shape_ok"] is True
    assert report["ok"] is True
    assert report["skipped"] is True
    assert report["live_model"]["skipped"] is True
    assert report["shape"]["executor"] == "local.comfy.video"
    ids = {row["executor_id"] for row in report["executors"]}
    assert "local.comfy.video" in ids
    assert "local.ffmpeg.remaster" in ids


def test_builtin_api_and_local_are_plural_slots() -> None:
    registry = ExecutorRegistry(load_extras=False)
    api_ids = {row["executor_id"] for row in registry.list_for_transport("api")}
    local_ids = {row["executor_id"] for row in registry.list_for_transport("local")}
    assert "api.vendor.video" in api_ids
    assert len(api_ids) > 1
    assert "local.comfy.video" in local_ids
    assert len(local_ids) > 1


def test_extras_file_registers_multiple_api_and_local(
    sample_docx: Path, tmp_path: Path
) -> None:
    path = tmp_path / "fsf-executors.json"
    path.write_text(json.dumps(_sample_extras()), encoding="utf-8")
    extras = executors_from_file(path)
    assert {item.executor_id for item in extras} == {"api.test.video", "local.test.video"}

    registry = ExecutorRegistry(load_extras=False)
    for extra in extras:
        registry.register(extra)
    assert registry.get("api.test.video").tool_name == "studio.video.generate"
    assert registry.get("local.test.video").is_available() is False

    manifest, _, _ = compile_job(sample_docx)
    DEFAULT_REGISTRY.register(extras[0])
    DEFAULT_REGISTRY.register(extras[1])
    try:
        api_result = run_studio_flow(
            "studio.video.generate",
            manifest["tasks"][:1],
            tmp_path / "extra_api",
            transport="api",
            executor_id="api.test.video",
        )
        local_result = run_studio_flow(
            "studio.video.generate",
            manifest["tasks"][:1],
            tmp_path / "extra_local",
            transport="local",
            executor_id="local.test.video",
        )
        assert api_result[0]["outputs"]["executor"] == "api.test.video"
        assert local_result[0]["outputs"]["executor"] == "local.test.video"
    finally:
        DEFAULT_REGISTRY.unregister("api.test.video")
        DEFAULT_REGISTRY.unregister("local.test.video")


def test_cli_flow_run_selects_extra_executor(sample_docx: Path, tmp_path: Path) -> None:
    extras = executors_from_config(_sample_extras())
    extra = extras[1]
    DEFAULT_REGISTRY.register(extra)
    try:
        out = tmp_path / "cli_extra"
        rc = main(
            [
                "flow",
                "run",
                "studio.video.generate",
                str(sample_docx),
                "--transport",
                "local",
                "--executor",
                extra.executor_id,
                "--shape",
                "--out",
                str(out),
            ]
        )
        assert rc == 0
        assert (out / "task_001.shape.bin").is_file()
    finally:
        DEFAULT_REGISTRY.unregister(extra.executor_id)


def test_extras_reject_embedded_secrets_and_kaggle() -> None:
    with pytest.raises(ValueError, match="must not embed"):
        executors_from_config(
            {
                "executors": [
                    {
                        "executor_id": "api.bad.video",
                        "transport": "api",
                        "tool_name": "studio.video.generate",
                        "api_key": "do-not-put-keys-here",
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match=r"must match api\.\* or local\.\*"):
        executors_from_config(
            {
                "executors": [
                    {
                        "executor_id": "kaggle.extra.video",
                        "transport": "kaggle",
                        "tool_name": "studio.video.generate",
                    }
                ]
            }
        )
    with pytest.raises(ValueError, match="does not match transport"):
        executors_from_config(
            {
                "executors": [
                    {
                        "executor_id": "api.wrong.local",
                        "transport": "local",
                        "tool_name": "studio.video.generate",
                    }
                ]
            }
        )


def test_extras_reject_builtin_collision(tmp_path: Path) -> None:
    path = tmp_path / "fsf-executors.json"
    path.write_text(
        json.dumps(
            {
                "executors": [
                    {
                        "executor_id": "local.comfy.video",
                        "transport": "local",
                        "tool_name": "studio.video.generate",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = ExecutorRegistry(load_extras=False)
    with pytest.raises(ValueError, match="collides with builtin"):
        registry.load_extras(path)
