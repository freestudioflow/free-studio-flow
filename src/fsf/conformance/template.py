"""FSF Template Conformance verification (FSF-M3).

Integrates ProDocuX Kernel template-to-artifact table conformance checks.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from prodocux_kernel.verification import (
    compare_template_conformance,
    profile_docx_table_structure_bytes,
)


def evaluate_table_template_conformance(
    reference_docx_raw: bytes,
    candidate_docx_raw: bytes,
    *,
    request_id: str = "request:fsf:template_check",
    allowed: list[str] | None = None,
    protected: list[str] | None = None,
) -> dict[str, Any]:
    """Compare candidate filled document against reference template using Kernel conformance engine."""
    reference_profile = profile_docx_table_structure_bytes(reference_docx_raw)
    candidate_profile = profile_docx_table_structure_bytes(candidate_docx_raw)

    policy_protected = protected or [
        "table_count",
        "table_order",
        "topology",
        "headers",
        "merge_ranges",
        "column_widths",
        "style",
        "fixed_row_labels",
    ]
    policy_allowed = allowed or ["body_text"]

    request = {
        "schema_version": "prodocux_template_conformance_request_v1",
        "request_id": request_id,
        "reference": reference_profile,
        "candidate": candidate_profile,
        "policy": {
            "table_matching": "table_id",
            "protected": policy_protected,
            "allowed": policy_allowed,
            "column_width_tolerance_points": 0.1,
        },
    }

    result = compare_template_conformance(request)
    return result
