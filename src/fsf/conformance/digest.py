"""Canonical contract hashing and deterministic digest utilities for FSF."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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
