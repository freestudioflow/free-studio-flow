"""Tests for FSF-M3: Media Conformance Consumption & Template Conformance."""

from __future__ import annotations

from pathlib import Path
from docx import Document

from pdx_adapter_media import ProbeUnavailable
from fsf.conformance.media import (
    build_expected_contract,
    evaluate_media_file,
    map_conformance_status_to_host,
)
from fsf.cli.main import main
from fsf.conformance.template import evaluate_table_template_conformance


class MockGoodProbe:
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


class MockMismatchProbe:
    def probe(self, path: Path) -> dict:
        return {
            "format": {"duration": "5.0", "format_name": "mov,mp4"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24/1",
                    "nb_frames": "120",
                }
            ],
            "black_frame_ratio": 0.0,
            "freeze_ranges": [],
        }


class MockFailingProbe:
    def probe(self, path: Path) -> dict:
        raise ProbeUnavailable("ffprobe is not installed on this system")


def test_build_expected_contract() -> None:
    task = {"duration_seconds": 10.0}
    expected = build_expected_contract(task, width=1024, height=576, fps=24)
    assert expected["width"] == 1024
    assert expected["height"] == 576
    assert expected["duration_seconds"] == 10.0
    assert expected["audio"] == "forbidden"


def test_media_conformance_four_statuses(tmp_path: Path) -> None:
    """M3 Exit Criterion: Media technical conformance evaluation without private ffprobe reimplementation."""
    sample_media = tmp_path / "sample.mp4"
    sample_media.write_bytes(b"TEST_MEDIA_BYTES")

    expected = build_expected_contract({"duration_seconds": 5.0})

    # 1. Conforms
    rep_good, act_good = evaluate_media_file(
        sample_media, expected, probe_runner=MockGoodProbe(), allow_injected_probe=True
    )
    assert rep_good["evaluation_status"] == "conforms"
    assert act_good["host_action"] == "permit_next_step"
    assert act_good["create_binding"] is True

    # 2. Does Not Conform (Dimensions Mismatch)
    rep_bad, act_bad = evaluate_media_file(
        sample_media, expected, probe_runner=MockMismatchProbe(), allow_injected_probe=True
    )
    assert rep_bad["evaluation_status"] == "does_not_conform"
    assert any(i["code"] == "MEDIA_DIMENSIONS_MISMATCH" for i in rep_bad["issues"])
    assert act_bad["host_action"] == "block_next_product_step"

    # 3. Evaluation Failed (Analyzer fails)
    rep_fail, act_fail = evaluate_media_file(
        sample_media, expected, probe_runner=MockFailingProbe(), allow_injected_probe=True
    )
    assert rep_fail["evaluation_status"] == "evaluation_failed"
    assert act_fail["host_action"] == "fail_block_or_retry"
    assert act_fail["create_binding"] is True
    assert rep_fail["schema_version"] == "pdx_media_conformance_result_v1"

    # 4. Not Evaluated (Explicit skip / no analyzer requested)
    rep_skip, act_skip = evaluate_media_file(
        sample_media,
        expected,
        probe_runner=MockGoodProbe(),
        skip_evaluation=True,
        allow_injected_probe=True,
    )
    assert rep_skip["evaluation_status"] == "not_evaluated"
    assert "request_id" not in rep_skip
    assert act_skip["host_action"] == "continue_without_technical_check"
    assert act_skip["create_binding"] is False


def test_unknown_media_status_does_not_create_binding() -> None:
    action = map_conformance_status_to_host("not_a_real_status")
    assert action["create_binding"] is False
    assert action["check_terminal_outcome"] == "failed"
    assert action["host_action"] == "fail_block_or_retry"


def test_shape_and_mock_bin_cannot_verify_as_media(tmp_path: Path) -> None:
    """Contract-shape payloads are FSF policy refusals, not Media reports."""
    expected = build_expected_contract({"duration_seconds": 5.0})
    for name in ("task_001.shape.bin", "task_001.mock.bin"):
        artifact = tmp_path / name
        artifact.write_bytes(b"NOT_MEDIA")
        report, action = evaluate_media_file(
            artifact, expected, probe_runner=MockGoodProbe(), allow_injected_probe=True
        )
        assert report["schema_version"] == "fsf_policy_decision_v1"
        assert report["decision"] == "refuse"
        assert "evaluation_status" not in report
        assert any(i["code"] == "FSF_CONTRACT_SHAPE_NOT_MEDIA" for i in report["issues"])
        assert action["host_action"] == "fail_block_or_retry"
        assert action["create_binding"] is False
        assert action["check_terminal_outcome"] == "failed"


def test_glb_is_policy_refusal_not_media_report(tmp_path: Path) -> None:
    artifact = tmp_path / "prop.glb"
    artifact.write_bytes(b"glTF")
    report, action = evaluate_media_file(
        artifact,
        build_expected_contract({"duration_seconds": 5.0}),
        probe_runner=MockGoodProbe(),
        allow_injected_probe=True,
    )
    assert report["schema_version"] == "fsf_policy_decision_v1"
    assert report["reason_code"] == "FSF_NON_AV_NOT_MEDIA"
    assert action["create_binding"] is False
    assert action["check_terminal_outcome"] == "failed"


def test_injected_probe_without_allow_is_refused(tmp_path: Path) -> None:
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"TEST_MEDIA_BYTES")
    try:
        evaluate_media_file(
            sample,
            build_expected_contract({"duration_seconds": 5.0}),
            probe_runner=MockGoodProbe(),
        )
    except ValueError as exc:
        assert "injected probe_runner" in str(exc)
    else:
        raise AssertionError("production path must refuse injected probes")


def test_artifact_kind_shape_refuses_even_if_named_mp4(tmp_path: Path) -> None:
    renamed = tmp_path / "task_001.mp4"
    renamed.write_bytes(b"DETERMINISTIC_VIDEO_BYTES:task_001")
    report, action = evaluate_media_file(
        renamed,
        build_expected_contract({"duration_seconds": 5.0}),
        artifact_kind="shape",
        probe_runner=MockGoodProbe(),
        allow_injected_probe=True,
    )
    assert report["schema_version"] == "fsf_policy_decision_v1"
    assert report["reason_code"] == "FSF_CONTRACT_SHAPE_NOT_MEDIA"
    assert action["create_binding"] is False


def test_cli_verify_refuses_shape_bin(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "clip.shape.bin"
    artifact.write_bytes(b"NOT_MEDIA")
    rc = main(["verify", str(artifact)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FSF Policy Decision" in captured.out
    assert "FSF_CONTRACT_SHAPE_NOT_MEDIA" in captured.out
    assert '"create_binding": false' in captured.out


def test_template_conformance_check() -> None:
    """M3 Template Conformance: Reference vs Candidate table check."""
    doc_ref = Document()
    t_ref = doc_ref.add_table(rows=2, cols=2)
    t_ref.cell(0, 0).text = "Shot"
    t_ref.cell(0, 1).text = "Duration"
    t_ref.cell(1, 0).text = "task_001"
    t_ref.cell(1, 1).text = "5.0"

    doc_cand = Document()
    t_cand = doc_cand.add_table(rows=2, cols=2)
    t_cand.cell(0, 0).text = "Shot"
    t_cand.cell(0, 1).text = "Duration"
    t_cand.cell(1, 0).text = "task_001"
    t_cand.cell(1, 1).text = "5.0"

    from io import BytesIO
    b_ref = BytesIO()
    doc_ref.save(b_ref)
    b_cand = BytesIO()
    doc_cand.save(b_cand)

    res = evaluate_table_template_conformance(b_ref.getvalue(), b_cand.getvalue())
    assert res["conforms"] is True
    assert not res.get("issues")
