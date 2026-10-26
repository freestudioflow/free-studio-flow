"""Strict production documents; digests bind content, not creative correctness."""
from __future__ import annotations

import copy
from importlib import resources
import json
import os
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.edit_store import decode_json, read_json, _target
from fsf.core.filesystem import native_path


class ProductionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ProductionError("PRODUCTION_ID_INVALID")
    return value


def integer(value, minimum=0, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProductionError("PRODUCTION_INVALID")
    return value


def seal(value, field):
    result = copy.deepcopy(value)
    result.pop(field, None)
    result[field] = digest(result)
    return result


def validate(value, schema, field):
    try:
        raw = resources.files("fsf.schemas").joinpath(schema + ".schema.json").read_text(encoding="utf-8")
        # In-memory callers receive the same finite-number and depth checks as files.
        decode_json(json.dumps(value, allow_nan=False).encode("utf-8"))
        if not Draft202012Validator(json.loads(raw)).is_valid(value):
            raise ValueError()
        if seal(value, field) != value:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ProductionError("PRODUCTION_INVALID") from None
    return copy.deepcopy(value)


def publish(path, value):
    """Caller holds the stable document lock; atomic visibility, not disk durability."""
    target = _target(native_path(path))
    data = (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
    decode_json(data)
    fd, temp = tempfile.mkstemp(prefix=".production-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(native_path(temp), native_path(target))
        if read_json(target) != value:
            raise ProductionError("PRODUCTION_IO")
    finally:
        Path(temp).unlink(missing_ok=True)
