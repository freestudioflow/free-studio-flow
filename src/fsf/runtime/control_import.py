"""Import operator-created control movies without claiming Blender provenance."""
from fractions import Fraction
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from fsf.runtime.blender import BlenderError, artifact, digest, sha, shape
from fsf.core.asset_library import _root, _no_links, _open_reader
from fsf.core.edit_store import edit_lock, read_json, EditStoreError
from fsf.conformance.media import ensure_media_tools_on_path
from fsf.executors.subprocess_safety import run_safe_subprocess


def _source_sha(path):
    _no_links(path)
    h = hashlib.sha256()
    with _open_reader(path) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BlenderError("CONTROL_SOURCE_INVALID")
        total = 0
        while data := stream.read(1024*1024):
            total += len(data)
            if total > 64*1024*1024:
                raise BlenderError("CONTROL_SOURCE_INVALID")
            h.update(data)
    return h.hexdigest()


def _inspect(path, scratch, *, deadline=None):
    ffmpeg, ffprobe = ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:
        raise BlenderError("CONTROL_TOOLS_UNAVAILABLE")
    deadline = min(deadline,time.monotonic()+120) if deadline is not None else time.monotonic()+120

    def run(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BlenderError("CONTROL_DEADLINE_EXCEEDED")
        result = run_safe_subprocess(args, timeout_seconds=remaining, output_limit_bytes=1024*1024)
        if result.timed_out:
            raise BlenderError("CONTROL_DEADLINE_EXCEEDED")
        if result.returncode:
            raise BlenderError("CONTROL_DECODE_FAILED")
        return result.stdout

    info = json.loads(run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-show_streams",
                          "-show_format", "-of", "json", str(path)]))
    streams = info.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "video" or "mp4" not in info.get("format", {}).get("format_name", "").split(","):
        raise BlenderError("CONTROL_STREAMS_INVALID")
    stream = streams[0]
    width, height = stream["width"], stream["height"]
    if any(type(v) is not int or not 32 <= v <= 512 or v % 2 for v in (width, height)):
        raise BlenderError("CONTROL_DIMENSIONS_UNSUPPORTED")
    rate = Fraction(stream["avg_frame_rate"])
    if rate not in {Fraction(24), Fraction(24000, 1001)} or Fraction(stream["r_frame_rate"]) != rate:
        raise BlenderError("CONTROL_CLOCK_UNSUPPORTED")
    if stream.get("sample_aspect_ratio", "1:1") not in {"1:1", "N/A"} or stream.get("tags", {}).get("rotate") or stream.get("side_data_list"):
        raise BlenderError("CONTROL_GEOMETRY_UNSUPPORTED")
    count = int(stream.get("nb_frames", 0))
    if not 1 <= count <= 96:
        raise BlenderError("CONTROL_FRAME_LIMIT")
    frames = json.loads(run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp", "-of", "json", str(path)]))["frames"]
    if len(frames) != count:
        raise BlenderError("CONTROL_TIMING_INVALID")
    timebase = Fraction(stream["time_base"])
    mapping = []
    for index, frame in enumerate(frames):
        timestamp = int(frame["best_effort_timestamp"]) * timebase
        if timestamp != Fraction(index, 1) / rate:
            raise BlenderError("CONTROL_TIMING_INVALID")
        mapping.append({"input_frame": index, "output_frame": index,
                        "time": {"num": timestamp.numerator, "den": timestamp.denominator}})
    # Decode every frame; -xerror refuses corrupt packets instead of concealing them.
    raw = scratch / "decoded.rgb"
    run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file", "-i", str(path),
         "-map", "0:v:0", "-an", "-vsync", "0", "-frames:v", "97", "-pix_fmt", "rgb24", "-f", "rawvideo", "-y", str(raw)])
    if raw.stat().st_size != count * width * height * 3:
        raise BlenderError("CONTROL_DECODE_FAILED")
    decoded_sha = sha(raw)
    raw.unlink()
    return {"clock": {"num": rate.numerator, "den": rate.denominator}, "frame_count": count,
            "width": width, "height": height, "frame_mapping": mapping, "decoded_rgb_sha256": decoded_sha}


def verify_import(folder):
    folder = _root(folder)
    manifest = read_json(folder / "manifest.json")
    shape(manifest, "fsf_control_import_v1")
    if digest({k: v for k, v in manifest.items() if k != "manifest_digest"}) != manifest["manifest_digest"]:
        raise BlenderError("CONTROL_INTEGRITY")
    if digest(manifest["frame_mapping"]) != manifest["frame_mapping_digest"]:
        raise BlenderError("CONTROL_INTEGRITY")
    if manifest["artifact"] != artifact(folder, "control.mp4", "control_video", "video/mp4"):
        raise BlenderError("CONTROL_INTEGRITY")
    rate = Fraction(manifest["clock"]["num"], manifest["clock"]["den"])
    if rate not in {Fraction(24), Fraction(24000, 1001)} or len(manifest["frame_mapping"]) != manifest["frame_count"]:
        raise BlenderError("CONTROL_INTEGRITY")
    for index, item in enumerate(manifest["frame_mapping"]):
        t = Fraction(index, 1) / rate
        if item != {"input_frame": index, "output_frame": index, "time": {"num": t.numerator, "den": t.denominator}}:
            raise BlenderError("CONTROL_INTEGRITY")
    return manifest


def import_control_video(source, *, output_root, control_id, revision=1):
    """Copy and verify an unchanged MP4. No implicit resampling or clay certification."""
    try:
        source = Path(source).absolute()
        _no_links(source)
        if not source.is_file() or not 0 < source.stat().st_size <= 64*1024*1024:
            raise BlenderError("CONTROL_SOURCE_INVALID")
        root = _root(output_root)
        with edit_lock(root / "control-import"):
            scratch = Path(tempfile.mkdtemp(prefix=".control-import-", dir=root))
            try:
                target = scratch / "control.mp4"
                before = _source_sha(source)
                # Bounded copy even when an input changes while being read.
                with _open_reader(source) as incoming, target.open("wb") as outgoing:
                    if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                        raise BlenderError("CONTROL_SOURCE_INVALID")
                    total = 0
                    while data := incoming.read(1024*1024):
                        total += len(data)
                        if total > 64*1024*1024:
                            raise BlenderError("CONTROL_SOURCE_INVALID")
                        outgoing.write(data)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if sha(target) != before or _source_sha(source) != before:
                    raise BlenderError("CONTROL_SOURCE_CHANGED")
                measured = _inspect(target, scratch)
                manifest = {"schema_version": "fsf_control_import_v1", "control_id": control_id, "revision": revision,
                    "provenance": "operator_control_video_import", "preprocess": "none", "motion_review": "not_reviewed",
                    "clay_profile": "unverified", "generation_dispatches": 0,
                    "artifact": artifact(scratch, "control.mp4", "control_video", "video/mp4"), **measured}
                manifest["frame_mapping_digest"] = digest(manifest["frame_mapping"])
                manifest["manifest_digest"] = digest(manifest)
                shape(manifest, "fsf_control_import_v1")
                (scratch / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                verify_import(scratch)
                destination = root / manifest["manifest_digest"]
                if destination.exists():
                    if verify_import(destination) != manifest:
                        raise BlenderError("CONTROL_INTEGRITY")
                else:
                    os.replace(scratch, destination)
                return {"bundle": destination.name, "manifest_digest": manifest["manifest_digest"], "generation_dispatches": 0}
            finally:
                if scratch.exists():
                    shutil.rmtree(scratch)
    except KeyboardInterrupt:
        raise BlenderError("BLENDER_CANCELLED") from None
    except BlenderError:
        raise
    except EditStoreError as exc:
        raise BlenderError("CONTROL_LOCK_TIMEOUT" if exc.code == "EDIT_LOCK_TIMEOUT" else "CONTROL_IMPORT_FAILED") from None
    except Exception:
        raise BlenderError("CONTROL_IMPORT_FAILED") from None
