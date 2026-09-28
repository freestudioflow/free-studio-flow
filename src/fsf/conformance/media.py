"""FSF Media Conformance Consumer (FSF-M3).

Compiles FSF shot specs and output contracts into PDX Media expected specifications,
and delegates evaluation directly to `pdx-adapter-media` without duplicate ffprobe logic.

Shape/mock/non-AV policy refusals are FSF host decisions, not Media reports.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from pdx_adapter_media import (
    FfprobeRunner,
    build_technical_profile_v2,
    evaluate_media_conformance,
    not_evaluated_result,
)

# Standard B-roll / Studio Pad baseline expected contract
DEFAULT_BROLL_EXPECTED = {
    "containers": ["mp4"],
    "video_codecs": ["h264"],
    "width": 1024,
    "height": 576,
    "fps": 24,
    "fps_tolerance": 0.01,
    "duration_seconds": 5.0,
    "duration_tolerance_seconds": 0.25,
    "audio": "forbidden",
    "max_black_frame_ratio": 0.01,
    "max_freeze_duration_seconds": 0.5,
}

CONTRACT_SHAPE_SUFFIXES = (".shape.bin", ".mock.bin")
NON_AV_SUFFIXES = (".glb", ".gltf")
SHAPE_EXECUTION_KINDS = frozenset({"mock", "shape"})
POLICY_DECISION_SCHEMA = "fsf_policy_decision_v1"
MEDIA_CONFORMANCE_SCHEMA = "pdx_media_conformance_result_v1"
RECEIPT_SCHEMA = "fsf_media_conformance_receipt_v1"

HOST_CONFORMANCE_MAPPINGS = {
    "not_evaluated": {
        "create_binding": False,
        "check_terminal_outcome": None,
        "host_action": "continue_without_technical_check",
    },
    "evaluation_failed": {
        "create_binding": True,
        "check_terminal_outcome": "failed",
        "host_action": "fail_block_or_retry",
    },
    "does_not_conform": {
        "create_binding": True,
        "check_terminal_outcome": "succeeded",
        "host_action": "block_next_product_step",
    },
    "conforms": {
        "create_binding": True,
        "check_terminal_outcome": "succeeded",
        "host_action": "permit_next_step",
    },
}

POLICY_REFUSAL_HOST_ACTION = {
    "create_binding": False,
    "check_terminal_outcome": "failed",
    "host_action": "fail_block_or_retry",
}


def build_expected_contract(
    task_or_spec: dict[str, Any],
    *,
    width: int = 1024,
    height: int = 576,
    fps: int = 24,
    audio: str = "forbidden",
    containers: list[str] | None = None,
    video_codecs: list[str] | None = None,
) -> dict[str, Any]:
    """Compile a task or shot spec into a pdx_media_conformance expected contract."""
    duration = float(task_or_spec.get("duration_seconds", 5.0))
    expected = {
        "containers": containers or ["mp4"],
        "video_codecs": video_codecs or ["h264"],
        "width": width,
        "height": height,
        "fps": fps,
        "fps_tolerance": 0.01,
        "duration_seconds": duration,
        "duration_tolerance_seconds": 0.25,
        "audio": audio,
        "max_black_frame_ratio": 0.01,
        "max_freeze_duration_seconds": 0.5,
    }
    return expected


def is_contract_shape_artifact(path: Path) -> bool:
    """Suffix guard for FSF shape/mock payloads. Provenance/kind is the authority."""
    return path.name.casefold().endswith(CONTRACT_SHAPE_SUFFIXES)


def is_non_av_artifact(path: Path) -> bool:
    """Suffix guard for assets that must not enter the audiovisual verify entry."""
    return path.suffix.casefold() in NON_AV_SUFFIXES


def _policy_schema() -> dict[str, Any]:
    raw = (
        resources.files("fsf.conformance")
        .joinpath("fsf_policy_decision.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def _validate_policy_decision(decision: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(_policy_schema()).iter_errors(decision),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise ValueError(
            "invalid fsf_policy_decision_v1: "
            + "; ".join(error.message for error in errors)
        )


def canonical_contract_digest(value: Any) -> str:
    """Same canonical digest Media uses for profile/expected/report hashes."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _media_schema(name: str) -> dict[str, Any]:
    return json.loads(
        (resources.files("pdx_adapter_media.schemas") / name).read_text(encoding="utf-8")
    )


def _media_registry() -> Registry:
    registry = Registry()
    for name in (
        "pdx_media_technical_profile_v2.json",
        "pdx_media_conformance_request_v1.json",
        "pdx_media_conformance_result_v1.json",
    ):
        schema = _media_schema(name)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validate_media_document(schema_filename: str, value: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(_media_schema(schema_filename), registry=_media_registry()).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise ValueError(
            "invalid media document: " + "; ".join(error.message for error in errors)
        )


def validate_media_conformance_report(report: Mapping[str, Any]) -> None:
    _validate_media_document("pdx_media_conformance_result_v1.json", report)


def validate_media_technical_profile(profile: Mapping[str, Any]) -> None:
    _validate_media_document("pdx_media_technical_profile_v2.json", profile)


def validate_media_expected(expected: Mapping[str, Any]) -> None:
    request_schema = _media_schema("pdx_media_conformance_request_v1.json")
    expected_schema = request_schema["properties"]["expected"]
    errors = sorted(
        Draft202012Validator(expected_schema).iter_errors(expected),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise ValueError(
            "invalid media expected: " + "; ".join(error.message for error in errors)
        )


def _receipt_schema() -> dict[str, Any]:
    raw = (
        resources.files("fsf.conformance")
        .joinpath("fsf_media_conformance_receipt.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def build_conformance_receipt(
    report: Mapping[str, Any],
    profile: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a schema-validated Media report bound to profile identity and expected."""
    validate_media_conformance_report(report)
    validate_media_technical_profile(profile)
    validate_media_expected(expected)
    if report.get("schema_version") != MEDIA_CONFORMANCE_SCHEMA:
        raise ValueError("report is not pdx_media_conformance_result_v1")
    profile_digest = canonical_contract_digest(profile)
    if report.get("profile_digest") != profile_digest:
        raise ValueError("report.profile_digest does not match the stored profile")
    expected_digest = canonical_contract_digest(expected)
    if report.get("expected_digest") != expected_digest:
        raise ValueError("report.expected_digest does not match the stored expected")
    identity = profile.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("profile identity is missing")
    artifact_sha256 = str(identity.get("sha256") or "")
    artifact_name = str(identity.get("name") or "")
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "report_schema": MEDIA_CONFORMANCE_SCHEMA,
        "report": dict(report),
        "profile": dict(profile),
        "expected": dict(expected),
        "report_digest": canonical_contract_digest(report),
        "profile_digest": profile_digest,
        "expected_digest": expected_digest,
        "evaluation_status": report["evaluation_status"],
        "artifact_sha256": artifact_sha256,
        "artifact_name": artifact_name,
        "schema_validated": True,
    }
    errors = sorted(
        Draft202012Validator(_receipt_schema()).iter_errors(receipt),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise ValueError(
            "invalid fsf_media_conformance_receipt_v1: "
            + "; ".join(error.message for error in errors)
        )
    return receipt


def receipt_authorizes_production(
    receipt: Mapping[str, Any] | None,
    *,
    artifact_sha256: str,
    artifact_name: str,
    required_expected_digest: str,
) -> bool:
    """True only if the receipt is schema-valid and bound to this artifact and expected."""
    if not isinstance(receipt, Mapping):
        return False
    required = (required_expected_digest or "").casefold()
    if len(required) != 64:
        return False
    try:
        errors = sorted(
            Draft202012Validator(_receipt_schema()).iter_errors(receipt),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            return False
        report = receipt["report"]
        profile = receipt["profile"]
        expected = receipt["expected"]
        validate_media_conformance_report(report)
        validate_media_technical_profile(profile)
        validate_media_expected(expected)
    except (ValueError, KeyError, TypeError):
        return False
    if receipt.get("schema_validated") is not True:
        return False
    if receipt.get("report_schema") != MEDIA_CONFORMANCE_SCHEMA:
        return False
    if receipt.get("evaluation_status") != "conforms":
        return False
    if report.get("evaluation_status") != "conforms":
        return False
    if canonical_contract_digest(report) != receipt.get("report_digest"):
        return False
    if canonical_contract_digest(profile) != receipt.get("profile_digest"):
        return False
    if report.get("profile_digest") != receipt.get("profile_digest"):
        return False
    stored_expected_digest = canonical_contract_digest(expected)
    if stored_expected_digest != receipt.get("expected_digest"):
        return False
    if report.get("expected_digest") != stored_expected_digest:
        return False
    if stored_expected_digest.casefold() != required:
        return False
    identity = profile.get("identity") if isinstance(profile.get("identity"), Mapping) else {}
    evaluated_sha = str(identity.get("sha256") or "")
    current = artifact_sha256.casefold()
    if not current or evaluated_sha.casefold() != current:
        return False
    if str(receipt.get("artifact_sha256") or "").casefold() != current:
        return False
    if str(identity.get("name") or "") != artifact_name:
        return False
    if str(receipt.get("artifact_name") or "") != artifact_name:
        return False
    return True


def policy_refusal(
    *,
    reason_code: str,
    location: str,
    message: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """FSF host policy refusal. Never a Media report; never create_binding."""
    decision = {
        "schema_version": POLICY_DECISION_SCHEMA,
        "decision": "refuse",
        "reason_code": reason_code,
        "location": location,
        "message": message,
        "issues": [
            {
                "code": reason_code,
                "location": location,
                "message": message,
                "retryable": False,
            }
        ],
        "interpretation": "none",
    }
    _validate_policy_decision(decision)
    return decision, dict(POLICY_REFUSAL_HOST_ACTION)


def evaluate_media_file(
    media_path: str | Path,
    expected_contract: dict[str, Any] | None = None,
    *,
    probe_runner: Any | None = None,
    request_id: str = "request:fsf:media:eval",
    skip_evaluation: bool = False,
    artifact_kind: str | None = None,
    allow_injected_probe: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report, action, _profile = _evaluate_media_core(
        media_path,
        expected_contract,
        probe_runner=probe_runner,
        request_id=request_id,
        skip_evaluation=skip_evaluation,
        artifact_kind=artifact_kind,
        allow_injected_probe=allow_injected_probe,
    )
    return report, action


def evaluate_media_with_receipt(
    media_path: str | Path,
    expected_contract: dict[str, Any] | None = None,
    *,
    probe_runner: Any | None = None,
    request_id: str = "request:fsf:media:eval",
    skip_evaluation: bool = False,
    artifact_kind: str | None = None,
    allow_injected_probe: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Evaluate media and, when Media produced a validated report+profile, return a receipt."""
    report, action, profile = _evaluate_media_core(
        media_path,
        expected_contract,
        probe_runner=probe_runner,
        request_id=request_id,
        skip_evaluation=skip_evaluation,
        artifact_kind=artifact_kind,
        allow_injected_probe=allow_injected_probe,
    )
    if profile is None or report.get("schema_version") != MEDIA_CONFORMANCE_SCHEMA:
        return report, action, None
    if report.get("evaluation_status") == "not_evaluated":
        return report, action, None
    try:
        expected = expected_contract or DEFAULT_BROLL_EXPECTED
        receipt = build_conformance_receipt(report, profile, expected)
    except ValueError:
        return report, action, None
    return report, action, receipt


def _evaluate_media_core(
    media_path: str | Path,
    expected_contract: dict[str, Any] | None = None,
    *,
    probe_runner: Any | None = None,
    request_id: str = "request:fsf:media:eval",
    skip_evaluation: bool = False,
    artifact_kind: str | None = None,
    allow_injected_probe: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Evaluate a media file against expected technical conformance using PDX Media Adapter.

    Production callers must not inject ``probe_runner``. Suffix guards are extra
    defense; executor ``artifact_kind`` / provenance is authoritative for shape/mock.

    Returns:
        (report_or_policy_decision, host_action_mapping, technical_profile_or_none)
    """
    path = Path(media_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")

    kind = (artifact_kind or "").casefold()
    if kind in SHAPE_EXECUTION_KINDS:
        decision, action = policy_refusal(
            reason_code="FSF_CONTRACT_SHAPE_NOT_MEDIA",
            location=path.name,
            message="shape/mock artifacts are not media; executor kind is authoritative",
        )
        return decision, action, None
    if is_contract_shape_artifact(path):
        decision, action = policy_refusal(
            reason_code="FSF_CONTRACT_SHAPE_NOT_MEDIA",
            location=path.name,
            message="shape/mock artifacts are not media; do not treat as generate success",
        )
        return decision, action, None
    if is_non_av_artifact(path):
        decision, action = policy_refusal(
            reason_code="FSF_NON_AV_NOT_MEDIA",
            location=path.name,
            message="non-audiovisual assets must not enter media verify; not a Media report",
        )
        return decision, action, None

    if probe_runner is not None and not allow_injected_probe:
        raise ValueError(
            "injected probe_runner is only allowed for tests or trusted internal callers"
        )

    expected = expected_contract or DEFAULT_BROLL_EXPECTED
    probe = probe_runner or FfprobeRunner()

    if skip_evaluation:
        profile = build_technical_profile_v2(path, probe)
        report = not_evaluated_result(profile)
    else:
        profile = build_technical_profile_v2(path, probe)
        req = {
            "schema_version": "pdx_media_conformance_request_v1",
            "request_id": request_id,
            "profile": profile,
            "expected": expected,
        }
        report = evaluate_media_conformance(req)

    if report.get("schema_version") != MEDIA_CONFORMANCE_SCHEMA:
        return report, dict(POLICY_REFUSAL_HOST_ACTION), None
    status = report.get("evaluation_status", "evaluation_failed")
    return report, map_conformance_status_to_host(status), profile


def map_conformance_status_to_host(status: str) -> dict[str, Any]:
    """Map a Media evaluation status to a host decision/action dict.

    ``create_binding`` is True only for known Media statuses on a validated
    ``pdx_media_conformance_result_v1``. Unknown statuses fail closed.
    """
    mapped = HOST_CONFORMANCE_MAPPINGS.get(status)
    if mapped is None:
        return {
            "create_binding": False,
            "check_terminal_outcome": "failed",
            "host_action": "fail_block_or_retry",
        }
    return dict(mapped)
