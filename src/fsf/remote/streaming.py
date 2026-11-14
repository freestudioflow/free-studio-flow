"""Bounded data plane for the explicit streaming API and artifact commands."""
import hashlib
import http.client
from pathlib import Path
import os
import re
import shutil
import tempfile
import time
import zipfile
import stat
from contextlib import contextmanager

from fsf.core.asset_library import _no_links, _open_reader, _path_parts
from fsf.core.edit_store import edit_lock
from fsf.remote.contracts import RemoteError

CHUNK = 1024 * 1024
MAX_INTEGER = 2**63 - 1
FIELDS = frozenset("""
input_bytes http_request_bytes control_response_bytes output_bytes archive_compressed_bytes
archive_unpacked_bytes archive_members member_bytes manifest_bytes manifest_line_bytes
worker_package_bytes process_tree_rss_bytes process_tree_commit_bytes pending_bytes disk_bytes
disk_reserve_bytes seconds
""".split())


def validate_limits(value):
    if not isinstance(value, dict) or set(value) != FIELDS | {"schema_version"}:
        raise RemoteError("STREAM_LIMITS_SCHEMA")
    if value["schema_version"] != "fsf_resource_limits_v1" or any(
        type(value[key]) is not int or not 0 < value[key] <= MAX_INTEGER for key in FIELDS
    ):
        raise RemoteError("STREAM_LIMITS_SCHEMA")
    if value["control_response_bytes"] > 64 * CHUNK or value["manifest_line_bytes"] > CHUNK:
        raise RemoteError("STREAM_PARSER_LIMIT")
    if (value["process_tree_rss_bytes"] < 64 * CHUNK
            or value["process_tree_commit_bytes"] < value["process_tree_rss_bytes"]):
        raise RemoteError("STREAM_PROCESS_LIMIT")
    return value


@contextmanager
def resource_scope(limits):
    from fsf.production.resources import Governor, ResourcePause, resource_profile

    profile = resource_profile("low-memory-v1")
    profile.update(rss_bytes=limits["process_tree_rss_bytes"],
                   commit_bytes=limits["process_tree_commit_bytes"])
    guard = Governor(profile)
    try:
        with guard.activate():
            guard.admit(deadline=time.monotonic()+limits["seconds"])
            yield
    except ResourcePause:
        raise RemoteError("STREAM_RESOURCE_PAUSED") from None


def content_length(headers):
    values = [part.strip() for raw in (headers.get_all("Content-Length") or []) for part in raw.split(",")]
    transfer = headers.get_all("Transfer-Encoding") or []
    if transfer and (values or [part.strip().lower() for part in transfer] != ["chunked"]):
        raise RemoteError("STREAM_LENGTH_INVALID")
    encoding = headers.get_all("Content-Encoding") or []
    if any(value.strip().lower() != "identity" for value in encoding):
        raise RemoteError("STREAM_ENCODING")
    if not values:
        return None
    if any(not re.fullmatch(r"[0-9]{1,19}", item) for item in values):
        raise RemoteError("STREAM_LENGTH_INVALID")
    counts = set(map(int, values))
    if len(counts) != 1 or max(counts) > MAX_INTEGER:
        raise RemoteError("STREAM_LENGTH_INVALID")
    return counts.pop()


def check_space(folder, amount, limits):
    try:
        disk = shutil.disk_usage(folder)
    except OSError:
        raise RemoteError("STREAM_DISK_UNKNOWN") from None
    reserve = max(limits["disk_reserve_bytes"], 1024 * CHUNK, disk.total // 100)
    if amount > limits["disk_bytes"] or disk.free - reserve < amount:
        raise RemoteError("STREAM_DISK_LIMIT")


def check_block(folder, total, block, limits):
    if total > limits["disk_bytes"]:
        raise RemoteError("STREAM_DISK_LIMIT")
    check_space(folder, block, limits)


def chunk_size(limits):
    from fsf.production.resources import ResourcePause, read_chunk_bytes
    try:
        return min(CHUNK, limits["pending_bytes"], read_chunk_bytes())
    except ResourcePause:
        raise RemoteError("STREAM_RESOURCE_PAUSED") from None


def validate_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise RemoteError("STREAM_SHA_INVALID")
    return value


def copy_stream(source, sink, *, maximum, deadline, declared=None, expected_sha=None,
                space=None, limits=None):
    """Read at most one byte beyond the active bound, and hash actual bytes."""
    if expected_sha is not None:
        validate_sha(expected_sha)
    if declared is not None and (type(declared) is not int or declared < 0):
        raise RemoteError("STREAM_LENGTH_INVALID")
    if declared is not None and declared > maximum:
        raise RemoteError("STREAM_SIZE_LIMIT")
    limit = min(maximum, declared) if declared is not None else maximum
    digest = hashlib.sha256()
    total = 0
    read = getattr(source, "read1", source.read)
    while True:
        if time.monotonic() >= deadline:
            raise RemoteError("API_DEADLINE_EXCEEDED")
        try:
            block = read(min(chunk_size(limits) if limits else CHUNK, limit - total + 1))
        except http.client.IncompleteRead:
            raise RemoteError("STREAM_TRUNCATED") from None
        if time.monotonic() >= deadline:
            raise RemoteError("API_DEADLINE_EXCEEDED")
        if not block:
            break
        total += len(block)
        if total > maximum:
            raise RemoteError("STREAM_SIZE_LIMIT")
        if total > limit:
            raise RemoteError("STREAM_LENGTH_MISMATCH")
        if space is not None:
            space(total, len(block))
        digest.update(block)
        if sink is not None:
            sink.write(block)
    if declared is not None and total != declared:
        raise RemoteError("STREAM_TRUNCATED")
    checksum = digest.hexdigest()
    if expected_sha is not None and checksum != expected_sha:
        raise RemoteError("STREAM_SHA_MISMATCH")
    return {"size_bytes": total, "sha256": checksum}


def download(response, folder, limits, deadline, expected_sha, *, maximum):
    declared = content_length(response.headers)
    if declared is not None and declared > maximum:
        raise RemoteError("STREAM_SIZE_LIMIT")
    check_space(folder, declared or 0, limits)
    fd, name = tempfile.mkstemp(prefix=".stream-", dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as sink:
            copy_stream(response, sink, maximum=maximum, deadline=deadline, declared=declared,
                        expected_sha=expected_sha,
                        limits=limits,
                        space=lambda total, block: check_block(folder, total, block, limits))
            sink.flush()
            os.fsync(sink.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class Upload:
    """One fixed-length PUT body. Never replay an iterator on redirect."""
    def __init__(self, path, checksum, length, limits, deadline):
        if length > limits["input_bytes"]:
            raise RemoteError("API_INPUT_LIMIT")
        if length > limits["http_request_bytes"]:
            raise RemoteError("API_REQUEST_LIMIT")
        self.path, self.checksum, self.length, self.deadline = path, checksum, length, deadline
        self.limits = limits
        self.complete = False

    def __iter__(self):
        remaining = self.length
        digest = hashlib.sha256()
        with _open_reader(self.path) as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise RemoteError("API_INPUT_CHANGED")
            while remaining:
                if time.monotonic() >= self.deadline:
                    raise RemoteError("API_DEADLINE_EXCEEDED")
                block = source.read(min(chunk_size(self.limits), remaining))
                if not block:
                    raise RemoteError("API_INPUT_CHANGED")
                digest.update(block)
                remaining -= len(block)
                yield block
            if source.read(1) or digest.hexdigest() != self.checksum:
                raise RemoteError("API_INPUT_CHANGED")
        self.complete = True


def extract_archive(path, destination, limits, expected_sha):
    """Publish a complete ZIP tree, or remove the private extraction directory."""
    validate_limits(limits)
    with resource_scope(limits):
        return _extract_archive(path, destination, limits, expected_sha)


def _extract_archive(path, destination, limits, expected_sha):
    if expected_sha is not None:
        validate_sha(expected_sha)
    path, destination = Path(path).absolute(), Path(destination).absolute()
    _no_links(path)
    _no_links(destination.parent)
    deadline = time.monotonic() + limits["seconds"]
    with edit_lock(destination), _open_reader(path) as source:
        if destination.exists():
            raise RemoteError("STREAM_DESTINATION_EXISTS")
        size = os.fstat(source.fileno()).st_size
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise RemoteError("STREAM_ARCHIVE_INVALID")
        identity = copy_stream(source, None, maximum=limits["archive_compressed_bytes"],
                               declared=size, expected_sha=expected_sha, deadline=deadline, limits=limits)
        expected_sha = identity["sha256"]
        source.seek(0)
        scratch = Path(tempfile.mkdtemp(prefix=".stream-archive-", dir=destination.parent))
        try:
            total = 0
            seen = set()
            # Bound the central directory before ZipFile materializes entries.
            end = zipfile._EndRecData(source)
            if end is None or end[zipfile._ECD_SIZE] > min(limits["manifest_bytes"], 8 * CHUNK):
                raise RemoteError("STREAM_ARCHIVE_DIRECTORY")
            if end[zipfile._ECD_ENTRIES_TOTAL] > limits["archive_members"]:
                raise RemoteError("STREAM_ARCHIVE_MEMBERS")
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > limits["archive_members"]:
                    raise RemoteError("STREAM_ARCHIVE_MEMBERS")
                check_space(destination.parent, sum(entry.file_size for entry in entries), limits)
                for entry in entries:
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise RemoteError("STREAM_ARCHIVE_MEMBER")
                    parts = _path_parts(entry.orig_filename.rstrip("/") if entry.is_dir() else entry.orig_filename)
                    key = tuple(part.casefold() for part in parts)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    if key in seen or kind not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                        raise RemoteError("STREAM_ARCHIVE_MEMBER")
                    seen.add(key)
                    target = scratch.joinpath(*parts)
                    if entry.is_dir():
                        if entry.file_size:
                            raise RemoteError("STREAM_ARCHIVE_MEMBER")
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    maximum = min(limits["member_bytes"], limits["archive_unpacked_bytes"] - total)
                    with archive.open(entry) as member, target.open("xb") as sink:
                        result = copy_stream(member, sink, maximum=maximum, deadline=deadline,
                                             declared=entry.file_size,
                                             limits=limits,
                                             space=lambda _, block: check_space(destination.parent, block, limits))
                        sink.flush()
                        os.fsync(sink.fileno())
                    total += result["size_bytes"]
            source.seek(0)
            copy_stream(source, None, maximum=limits["archive_compressed_bytes"],
                        declared=size, expected_sha=expected_sha, deadline=deadline, limits=limits)
            os.rename(scratch, destination)
            return {"entries":len(seen), "unpacked_bytes":total, "archive_sha256":expected_sha}
        except RemoteError:
            raise
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
            raise RemoteError("STREAM_ARCHIVE_INVALID") from None
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)


def pack_archive(folder, paths, target, limits):
    """Stage an uncompressed ZIP with bounded writes and atomic publication."""
    from fsf.remote.contracts import safe_path

    target = Path(target)
    deadline = time.monotonic()+limits["seconds"]
    names = sorted(set(paths))
    if len(names) > limits["archive_members"]:
        raise RemoteError("STREAM_ARCHIVE_MEMBERS")
    members = [(name, safe_path(folder,name)) for name in names]
    total = sum(path.stat().st_size for _,path in members)
    overhead = 256 + sum(256 + 4*len(name.encode("utf-8")) for name in names)
    if total > limits["archive_unpacked_bytes"] or total+overhead > limits["archive_compressed_bytes"]:
        raise RemoteError("STREAM_SIZE_LIMIT")
    check_space(target.parent,total+overhead,limits)
    fd, name = tempfile.mkstemp(prefix=".stream-pack-",dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_STORED) as archive:
            for member,path in members:
                with _open_reader(path) as source, archive.open(member,"w",force_zip64=True) as sink:
                    size = os.fstat(source.fileno()).st_size
                    copy_stream(source,sink,declared=size,deadline=deadline,limits=limits,
                                maximum=min(limits["member_bytes"],total),
                                space=lambda _, block: check_space(target.parent,block,limits))
                    total -= size
        if temporary.stat().st_size > limits["archive_compressed_bytes"]:
            raise RemoteError("STREAM_SIZE_LIMIT")
        os.rename(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
