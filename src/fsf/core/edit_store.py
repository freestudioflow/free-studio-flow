"""Local edit persistence with cooperative interprocess compare-and-publish.

All writers must use this protocol. Network filesystems and writes by unrelated
programs are outside the guarantee. Atomic visibility is not power-loss durability.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import errno
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from fsf.core.editing import EditValidationError, apply_edit_patch, validate_edit

MAX_JSON_BYTES = 8_388_608
MAX_JSON_DEPTH = 64
MAX_LOCK_WAIT_SECONDS = 10
LOCK_RETRY_SECONDS = 0.05


class EditStoreError(ValueError):
    """Sanitized persistence failure with a stable machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _invalid():
    raise EditStoreError("EDIT_INVALID") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _depth(text):
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _invalid()
        elif char in "]}":
            depth -= 1


def decode_json(data: bytes):
    """Bounded strict UTF-8 JSON; reject duplicate keys and non-finite numbers."""
    if len(data) > MAX_JSON_BYTES:
        _invalid()
    try:
        text = data.decode("utf-8-sig")
        _depth(text)
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda _: _invalid())
        # Overflowing finite-looking JSON numbers can also become infinity.
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                _invalid()
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        return value
    except (UnicodeError, ValueError, RecursionError):
        _invalid()


def _regular(path: Path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(info.st_mode)):
        raise EditStoreError("EDIT_IO")


def _target(path) -> Path:
    """Normalize aliases without silently traversing symlinks or reparse points."""
    try:
        raw = Path(os.path.abspath(os.fspath(path)))
        # Inspect the original lexical components too, including before '..'.
        lexical = Path(path).absolute()
        for parent in reversed((lexical, *lexical.parents)):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise EditStoreError("EDIT_IO")
        if not raw.parent.is_dir():
            raise EditStoreError("EDIT_IO")
        _regular(raw, missing=True)
        return raw.resolve(strict=False)
    except (OSError, ValueError):
        raise EditStoreError("EDIT_IO") from None


def _open_reader(target):
    if os.name != "nt":
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return os.fdopen(fd, "rb")
    # Python's ordinary open lacks FILE_SHARE_DELETE, blocking concurrent replace.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(target), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("Reader open failed")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(handle)
        raise
    return os.fdopen(fd, "rb")


def read_json(path):
    try:
        target = _target(path)
        with _open_reader(target) as stream:
            return decode_json(stream.read(MAX_JSON_BYTES + 1))
    except EditStoreError:
        raise
    except OSError:
        raise EditStoreError("EDIT_IO") from None


def read_edit(path) -> dict:
    value = read_json(path)
    _validate(value)
    return value


def _validate(value):
    try:
        validate_edit(value)
    except __import__("fsf.core.audio_edit", fromlist=["AudioError"]).AudioError:
        raise
    except (EditValidationError, TypeError, KeyError, ValueError):
        _invalid()


@contextmanager
def edit_lock(path, *, timeout=MAX_LOCK_WAIT_SECONDS):
    """Stable lock file; OS releases its byte/flock lock on process exit."""
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout < 0:
        _invalid()
    stream = None
    acquired = False
    try:
        target = _target(path)
        lock_path = target.with_name(target.name + ".lock")
        _regular(lock_path, missing=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        stream = os.fdopen(fd, "r+b", buffering=0)
        # Byte-range locks may extend beyond EOF; never race to initialize data.
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EditStoreError("EDIT_LOCK_TIMEOUT") from None
                time.sleep(min(LOCK_RETRY_SECONDS, remaining))
        yield target
    except EditStoreError:
        raise
    except OSError:
        raise EditStoreError("EDIT_IO") from None
    finally:
        if stream is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                raise EditStoreError("EDIT_IO") from None
            finally:
                try:
                    stream.close()
                except OSError:
                    raise EditStoreError("EDIT_IO") from None


def _publish(target, edit):
    temporary = None
    try:
        data = (json.dumps(edit, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        _validate(decode_json(data))
        fd, temporary = tempfile.mkstemp(prefix=".fsf-edit-", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if read_edit(temporary) != edit:
            _invalid()
        _regular(target, missing=True)
        # Windows path metadata readers may briefly hold a non-delete-sharing
        # handle even when our data reader permits delete sharing. Retry only
        # those OS sharing/access failures, bounded by a monotonic deadline.
        deadline = time.monotonic() + MAX_LOCK_WAIT_SECONDS
        while True:
            try:
                os.replace(temporary, target)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                    raise
                time.sleep(min(LOCK_RETRY_SECONDS, remaining))
        temporary = None
    except EditStoreError:
        raise
    except (OSError, ValueError, TypeError):
        raise EditStoreError("EDIT_IO") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def create_edit_file(path, edit: dict, *, timeout=MAX_LOCK_WAIT_SECONDS) -> dict:
    """Publish an already constructed revision-one edit without overwriting."""
    edit = copy.deepcopy(edit)
    _validate(edit)
    if edit["revision"] != 1:
        _invalid()
    with edit_lock(path, timeout=timeout) as target:
        if target.exists():
            raise EditStoreError("EDIT_TARGET_EXISTS")
        _publish(target, edit)
    return edit


def patch_edit_file(path, patch: dict, *, timeout=MAX_LOCK_WAIT_SECONDS) -> dict:
    """Re-read and compare the patch base while holding the destination lock."""
    from fsf.core.editing import _shape
    patch = copy.deepcopy(patch)
    try:
        _shape(patch, "fsf_edit_patch_v1")
    except (EditValidationError, TypeError, ValueError):
        _invalid()
    with edit_lock(path, timeout=timeout) as target:
        current = read_edit(target)
        if patch["edit_id"] != current["edit_id"]:
            _invalid()
        if (patch["base_revision"], patch["base_digest"]) != (current["revision"], current["edit_digest"]):
            raise EditStoreError("EDIT_CONFLICT")
        try:
            result = apply_edit_patch(current, patch)
        except __import__("fsf.core.audio_edit", fromlist=["AudioError"]).AudioError:
            raise
        except (EditValidationError, ValueError, TypeError, KeyError):
            _invalid()
        _publish(target, result)
    return result
