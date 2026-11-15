"""Bounded retry for read-only preflight and idempotent artifact retrieval."""
import errno
import time

from fsf.core.edit_store import EditStoreError


def _temporary(exc):
    if isinstance(exc, EditStoreError) and exc.code == 'EDIT_IO':
        exc = exc.__cause__ or exc.__context__
    return isinstance(exc, OSError) and (
        getattr(exc, 'winerror', None) in {32, 33}
        or exc.errno in {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT}
    )


class ReadRetries:
    """Share the retry window across failures without limiting a healthy first read."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.retry_deadline = None

    def call(self, operation):
        for delay in (1, 2, 4, None):
            try:
                return operation()
            except (EditStoreError, OSError) as exc:
                if delay is None or not _temporary(exc):
                    raise
                now = time.monotonic()
                if self.retry_deadline is None:
                    self.retry_deadline = min(self.deadline, now + 30)
                if now + delay >= self.retry_deadline:
                    raise
                time.sleep(delay)
