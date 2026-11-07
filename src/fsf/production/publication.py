"""Recoverable filesystem publication behind the indexed acceptance transaction."""

import hashlib
import os
from pathlib import Path
import stat
import time

from fsf.core.asset_library import _no_links, _open_reader
from fsf.production.contracts import ProductionError, seal
from fsf.production.store import _load


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise ProductionError("PRODUCTION_DEADLINE_EXCEEDED")


def _read(path, deadline, target=None):
    _no_links(path)
    checksum = hashlib.sha256()
    size = 0
    with _open_reader(path) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ProductionError("PRODUCTION_SOURCE_INVALID")
        while True:
            _deadline(deadline)
            block = source.read(1024 * 1024)
            if not block:
                break
            checksum.update(block)
            size += len(block)
            if size > 64 * 1024**3:
                raise ProductionError("PRODUCTION_SOURCE_LIMIT")
            if target is not None:
                target.write(block)
    return checksum.hexdigest()


def publication_intent(store, request):
    row = store.db.execute("""SELECT payload FROM events WHERE request=?
        AND kind='publication_intent' ORDER BY seq DESC LIMIT 1""", (request,)).fetchone()
    return _load(row[0]) if row else None


def stage_acceptance(store, request, epoch, deadline, *, artifact=None, evidence=None):
    """Persist intent, copy verified bytes, then expose acceptance through SQLite."""
    intent = publication_intent(store, request)
    if intent is None:
        evidence = seal({**evidence, "publication_request": request}, "report_digest")
        intent = {"artifact": str(Path(artifact).absolute()), "evidence": evidence}
        with store.transaction():
            attempt = store._fence(request, epoch)
            if attempt["status"] not in {"produced", "verified"}:
                raise ProductionError("PRODUCTION_EVIDENCE_REQUIRED")
            store._event("publication_intent", attempt["execution"], attempt["task"], request, intent)
    evidence = intent["evidence"]
    folder = store.root / "published"
    folder.mkdir(exist_ok=True)
    _no_links(folder)
    target = folder / request
    pending = folder / (request + ".partial")
    if target.exists():
        if _read(target, deadline) != evidence["artifact_sha256"]:
            raise ProductionError("PRODUCTION_SOURCE_CHANGED")
    else:
        if pending.exists() or pending.is_symlink():
            _no_links(pending)
            pending.unlink()
        with pending.open("xb") as output:
            sha = _read(Path(intent["artifact"]), deadline, output)
            if sha != evidence["artifact_sha256"]:
                raise ProductionError("PRODUCTION_SOURCE_CHANGED")
            output.flush()
            os.fsync(output.fileno())
        _deadline(deadline)
        os.replace(pending, target)
    _deadline(deadline)
    store.accept(request, epoch, target, evidence)


def verify_publication(store, row, evidence, deadline):
    request = row["request"]
    artifact = store.root / "published" / request
    intent = publication_intent(store, request)
    if (evidence.get("publication_request") != request or not intent
            or intent["evidence"] != evidence
            or _read(artifact, deadline) != evidence["artifact_sha256"]):
        raise ProductionError("PRODUCTION_SOURCE_CHANGED")
    return str(artifact.absolute()), evidence
