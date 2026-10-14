"""Tests for FSF-M2: Execution Plans, Checkpoints, Secret Scrubbing, and Resume."""

from __future__ import annotations

import json
from pathlib import Path

from fsf.intake.compiler import compile_job
from fsf.runtime.external_op import (
    build_resumed_job,
    compute_file_sha256,
    compute_job_digest,
    create_fsf_external_op,
    inspect_existing_artifacts,
    make_artifact_checkpoint,
    save_checkpoint,
    scrub_secrets,
)
from fsf.runtime.plan import build_execution_plan


def test_build_execution_plan(sample_docx: Path) -> None:
    manifest, _, _ = compile_job(sample_docx)
    plan = build_execution_plan(manifest, executor_tool="studio.video.generate")

    assert plan["schema_version"] == "pdx_execution_plan_v1"
    assert len(plan["steps"]) == 6  # 3 tasks * 2 steps (gen + qc)
    assert plan["steps"][0]["tool"] == "studio.video.generate"
    assert plan["steps"][1]["tool"] == "pdx.media.technical_conform"
    assert plan["steps"][1]["depends_on"] == [plan["steps"][0]["id"]]


def test_secret_scrubbing() -> None:
    win_user = "C:" + r"\Users\Administrator\secret.txt"
    dirty_text = json.dumps(
        {
            "api_key": "sk-" + "12345678901234567890abcdef",
            "token": "HF_" + "mysecrettoken",
            "kaggle_token": "ghp_" + "supersecret123",
            "path": win_user,
        }
    )
    clean = scrub_secrets(dirty_text)
    assert "12345678901234567890abcdef" not in clean
    assert "mysecrettoken" not in clean
    assert "supersecret123" not in clean
    assert "Administrator" not in clean
    assert "[REDACTED_SECRET]" in clean
    assert "[REDACTED_USER_PATH]" in clean


def test_create_external_operation() -> None:
    op = create_fsf_external_op(
        operation_id="op:test:001",
        provider="kaggle",
        inputs={"prompt": "Locked-off shot 5s", "api_key": "secret_12345"},
    )
    assert op["schema_version"] == "pdx_external_operation_v1"
    assert op["operation_id"] == "op:test:001"
    assert op["provider"] == "kaggle"
    assert "secret_12345" not in json.dumps(op)


def test_resume_only_runs_pending_tasks(sample_docx: Path, tmp_path: Path) -> None:
    """M2: checkpoint-authorized mock/shape may be reused as shape_generated, not production."""
    from fsf.executors.registry import get_executor
    from fsf.runtime.external_op import checkpoint_record_from_tool_result

    out = tmp_path / "resume_run"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    executor = get_executor("mock.studio.video")
    result = executor.execute_shot(manifest["tasks"][0], out)

    status_before = inspect_existing_artifacts(manifest, out)
    assert status_before["task_001"]["completed"] is False

    save_checkpoint(
        out,
        manifest,
        {
            "task_001": checkpoint_record_from_tool_result(
                manifest, manifest["tasks"][0], result, executor.execution_kind
            )
        },
    )

    status_map = inspect_existing_artifacts(
        manifest, out, executor_id=executor.executor_id
    )
    assert status_map["task_001"]["completed"] is True
    assert status_map["task_001"]["production_completed"] is False
    assert status_map["task_001"]["resume_kind"] == "shape_generated"
    assert status_map["task_002"]["completed"] is False
    assert status_map["task_003"]["completed"] is False

    resumed_manifest, pending, completed = build_resumed_job(
        manifest, out, executor_id=executor.executor_id
    )
    assert resumed_manifest["is_resumed"] is True
    assert len(completed) == 1
    assert completed[0]["id"] == "task_001"
    assert completed[0]["execution_status"] == "reused"
    assert len(pending) == 2
    assert pending[0]["id"] == "task_002"
    assert pending[1]["id"] == "task_003"
    assert (out / "fsf_checkpoint.json").is_file()


def _media_record(manifest: dict, task_id: str, file_name: str, sha: str, size: int, **extra):
    digest = compute_job_digest(manifest)
    return make_artifact_checkpoint(
        task_id=task_id,
        source_sha256=manifest["source_sha256"],
        job_digest=digest,
        executor=extra.get("executor", "mock.studio.video"),
        execution_kind=extra.get("execution_kind", "media"),
        file=file_name,
        sha256=sha,
        size_bytes=size,
        conformance_receipt=extra.get("conformance_receipt"),
    )


def _write_checkpoint(out: Path, manifest: dict, artifacts: dict) -> None:
    payload = {
        "schema_version": "fsf_checkpoint_v3",
        "source_document": manifest.get("source_document"),
        "source_sha256": manifest.get("source_sha256"),
        "job_digest": compute_job_digest(manifest),
        "completed_artifacts": artifacts,
    }
    (out / "fsf_checkpoint.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def test_resume_rejects_nonempty_mp4_without_checkpoint(
    sample_docx: Path, tmp_path: Path
) -> None:
    out = tmp_path / "orphan_mp4"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    leftover = out / "task_001.mp4"
    leftover.write_bytes(b"LOOKS_LIKE_VIDEO_BUT_HAS_NO_RECEIPT")
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is False
    assert status["task_001"]["production_completed"] is False


def test_resume_rejects_checkpoint_with_different_job_digest(
    sample_docx: Path, tmp_path: Path
) -> None:
    out = tmp_path / "digest_mismatch"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    payload = b"BYTES"
    artifact = out / "task_001.mp4"
    artifact.write_bytes(payload)
    sha = compute_file_sha256(artifact)
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest,
                "task_001",
                "task_001.mp4",
                sha,
                len(payload),
            )
        },
    )
    other = dict(manifest)
    other["source_sha256"] = "0" * 64
    status = inspect_existing_artifacts(other, out)
    assert status["task_001"]["completed"] is False


def test_resume_rejects_sha_mismatch(sample_docx: Path, tmp_path: Path) -> None:
    out = tmp_path / "sha_mismatch"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"CURRENT_BYTES")
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest,
                "task_001",
                "task_001.mp4",
                "ab" * 32,
                len(b"CURRENT_BYTES"),
            )
        },
    )
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is False


def test_shape_bin_cannot_upgrade_to_production_completed(
    sample_docx: Path, tmp_path: Path
) -> None:
    out = tmp_path / "shape_upgrade"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    payload = b"SHAPE_BYTES"
    artifact = out / "task_001.shape.bin"
    artifact.write_bytes(payload)
    sha = compute_file_sha256(artifact)
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest,
                "task_001",
                "task_001.shape.bin",
                sha,
                len(payload),
                execution_kind="media",
            )
        },
    )
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is True
    assert status["task_001"]["production_completed"] is False
    assert status["task_001"]["resume_kind"] == "shape_generated"


def test_media_reuse_requires_conformance_receipt(
    sample_docx: Path, tmp_path: Path
) -> None:
    out = tmp_path / "media_no_qc"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    payload = b"MAYBE_MP4"
    artifact = out / "task_001.mp4"
    artifact.write_bytes(payload)
    sha = compute_file_sha256(artifact)
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest, "task_001", "task_001.mp4", sha, len(payload)
            )
        },
    )
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is False
    assert status["task_001"]["production_completed"] is False


def test_resume_rejects_status_strings_without_receipt(
    sample_docx: Path, tmp_path: Path
) -> None:
    out = tmp_path / "fake_strings"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    payload = b"MAYBE_MP4"
    artifact = out / "task_001.mp4"
    artifact.write_bytes(payload)
    sha = compute_file_sha256(artifact)
    record = _media_record(manifest, "task_001", "task_001.mp4", sha, len(payload))
    record["conformance_status"] = "conforms"
    record["conformance_schema"] = "pdx_media_conformance_result_v1"
    _write_checkpoint(out, manifest, {"task_001": record})
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is False
    assert status["task_001"]["production_completed"] is False


class _GoodProbe:
    def probe(self, path: Path) -> dict:
        return {
            "format": {"duration": "5.0", "format_name": "mov,mp4"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1024,
                    "height": 576,
                    "avg_frame_rate": "24/1",
                    "nb_frames": "120",
                }
            ],
            "black_frame_ratio": 0.0,
            "freeze_ranges": [],
        }


def test_resume_accepts_validated_media_receipt(sample_docx: Path, tmp_path: Path) -> None:
    from fsf.conformance.media import (
        build_expected_contract,
        evaluate_media_with_receipt,
    )

    out = tmp_path / "media_receipt"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"TEST_MEDIA_BYTES")
    sha = compute_file_sha256(artifact)
    _report, _action, receipt = evaluate_media_with_receipt(
        artifact,
        build_expected_contract({"duration_seconds": 5.0}),
        probe_runner=_GoodProbe(),
        allow_injected_probe=True,
    )
    assert receipt is not None
    assert receipt["evaluation_status"] == "conforms"
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest,
                "task_001",
                "task_001.mp4",
                sha,
                artifact.stat().st_size,
                executor="local.comfy.video",
                conformance_receipt=receipt,
            )
        },
    )
    status = inspect_existing_artifacts(manifest, out, executor_id="local.comfy.video")
    assert status["task_001"]["completed"] is True
    assert status["task_001"]["production_completed"] is True
    assert status["task_001"]["resume_kind"] == "production_completed"


def test_resume_rejects_receipt_when_file_sha_changes(
    sample_docx: Path, tmp_path: Path
) -> None:
    from fsf.conformance.media import (
        build_expected_contract,
        evaluate_media_with_receipt,
    )

    out = tmp_path / "receipt_sha"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"TEST_MEDIA_BYTES")
    sha = compute_file_sha256(artifact)
    _report, _action, receipt = evaluate_media_with_receipt(
        artifact,
        build_expected_contract({"duration_seconds": 5.0}),
        probe_runner=_GoodProbe(),
        allow_injected_probe=True,
    )
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": _media_record(
                manifest,
                "task_001",
                "task_001.mp4",
                sha,
                artifact.stat().st_size,
                executor="local.comfy.video",
                conformance_receipt=receipt,
            )
        },
    )
    artifact.write_bytes(b"TEST_MEDIA_BYTES_CHANGED")
    status = inspect_existing_artifacts(manifest, out, executor_id="local.comfy.video")
    assert status["task_001"]["completed"] is False


def test_resume_rejects_path_traversal(sample_docx: Path, tmp_path: Path) -> None:
    from fsf.runtime.external_op import resolve_checkpoint_artifact

    out = tmp_path / "art"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    outside = tmp_path / "outside.mp4"
    payload = b"OUTSIDE"
    outside.write_bytes(payload)
    sha = compute_file_sha256(outside)
    record = _media_record(manifest, "task_001", "../outside.mp4", sha, len(payload))
    _write_checkpoint(out, manifest, {"task_001": record})
    status = inspect_existing_artifacts(manifest, out)
    assert status["task_001"]["completed"] is False
    assert resolve_checkpoint_artifact(out, "../outside.mp4") is None
    assert resolve_checkpoint_artifact(out, str(outside.resolve())) is None

    save_checkpoint(out, manifest, {"task_001": record})
    saved = json.loads((out / "fsf_checkpoint.json").read_text(encoding="utf-8"))
    assert saved["completed_artifacts"] == {}


def test_job_digest_binds_task_prompt(sample_docx: Path, tmp_path: Path) -> None:
    from fsf.executors.registry import get_executor
    from fsf.runtime.external_op import checkpoint_record_from_tool_result

    out = tmp_path / "prompt_bind"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    executor = get_executor("mock.studio.video")
    result = executor.execute_shot(manifest["tasks"][0], out)
    save_checkpoint(
        out,
        manifest,
        {
            "task_001": checkpoint_record_from_tool_result(
                manifest, manifest["tasks"][0], result, executor.execution_kind
            )
        },
    )
    mutated = dict(manifest)
    mutated["tasks"] = [dict(task) for task in manifest["tasks"]]
    mutated["tasks"][0] = dict(mutated["tasks"][0])
    mutated["tasks"][0]["prompt"] = "CHANGED PROMPT THAT MUST INVALIDATE RESUME"
    assert compute_job_digest(mutated) != compute_job_digest(manifest)
    status = inspect_existing_artifacts(mutated, out, executor_id=executor.executor_id)
    assert status["task_001"]["completed"] is False


def _tight_receipt(artifact: Path, task: dict):
    from fsf.conformance.media import build_expected_contract, evaluate_media_with_receipt

    report, action, receipt = evaluate_media_with_receipt(
        artifact,
        build_expected_contract(task),
        probe_runner=_GoodProbe(),
        allow_injected_probe=True,
    )
    assert receipt is not None
    assert action["create_binding"] is True
    return report, receipt


def test_resume_rejects_looser_expected_receipt(
    sample_docx: Path, tmp_path: Path
) -> None:
    from fsf.conformance.media import evaluate_media_with_receipt

    out = tmp_path / "loose_expected"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"TEST_MEDIA_BYTES")
    sha = compute_file_sha256(artifact)
    _report, _action, loose_receipt = evaluate_media_with_receipt(
        artifact,
        {"containers": ["mp4"]},
        probe_runner=_GoodProbe(),
        allow_injected_probe=True,
    )
    assert loose_receipt is not None
    record = _media_record(
        manifest,
        "task_001",
        "task_001.mp4",
        sha,
        artifact.stat().st_size,
        executor="local.comfy.video",
        conformance_receipt=loose_receipt,
    )
    _write_checkpoint(out, manifest, {"task_001": record})
    status = inspect_existing_artifacts(manifest, out, executor_id="local.comfy.video")
    assert status["task_001"]["production_completed"] is False
    assert status["task_001"]["completed"] is False


def test_resume_rejects_expected_field_changes(
    sample_docx: Path, tmp_path: Path
) -> None:
    from fsf.conformance.media import (
        build_expected_contract,
        canonical_contract_digest,
        receipt_authorizes_production,
    )

    out = tmp_path / "expected_fields"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"TEST_MEDIA_BYTES")
    sha = compute_file_sha256(artifact)
    task = manifest["tasks"][0]
    _report, receipt = _tight_receipt(artifact, task)
    kwargs = {
        "artifact_sha256": sha,
        "artifact_name": "task_001.mp4",
    }
    assert receipt_authorizes_production(
        receipt,
        required_expected_digest=canonical_contract_digest(build_expected_contract(task)),
        **kwargs,
    )
    duration_task = dict(task)
    duration_task["duration_seconds"] = 99.0
    assert not receipt_authorizes_production(
        receipt,
        required_expected_digest=canonical_contract_digest(
            build_expected_contract(duration_task)
        ),
        **kwargs,
    )
    assert not receipt_authorizes_production(
        receipt,
        required_expected_digest=canonical_contract_digest(
            build_expected_contract(task, width=1920, height=1080)
        ),
        **kwargs,
    )
    assert not receipt_authorizes_production(
        receipt,
        required_expected_digest=canonical_contract_digest(
            build_expected_contract(task, audio="required")
        ),
        **kwargs,
    )


def test_resume_rejects_rewritten_expected_digest(
    sample_docx: Path, tmp_path: Path
) -> None:
    from copy import deepcopy

    from fsf.conformance.media import (
        build_expected_contract,
        canonical_contract_digest,
        receipt_authorizes_production,
    )

    out = tmp_path / "rewritten_digest"
    manifest, _, _ = compile_job(sample_docx, out_dir=out)
    artifact = out / "task_001.mp4"
    artifact.write_bytes(b"TEST_MEDIA_BYTES")
    sha = compute_file_sha256(artifact)
    task = manifest["tasks"][0]
    _report, receipt = _tight_receipt(artifact, task)
    required = canonical_contract_digest(build_expected_contract(task))
    tampered = deepcopy(receipt)
    forged = "ab" * 32
    tampered["expected_digest"] = forged
    tampered["report"]["expected_digest"] = forged
    tampered["report_digest"] = canonical_contract_digest(tampered["report"])
    assert not receipt_authorizes_production(
        tampered,
        artifact_sha256=sha,
        artifact_name="task_001.mp4",
        required_expected_digest=required,
    )
