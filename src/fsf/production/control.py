"""Bounded long control imports. Originals are retained; no implicit conversion."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from fsf.conformance.media import ensure_media_tools_on_path
from fsf.core.asset_library import _root, _no_links, _open_reader
from fsf.core.edit_store import edit_lock, read_json
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.contracts import ProductionError, digest, key, publish, seal, validate

MAX_BYTES = 64 * 1024**3


def file_identity(path, limit=MAX_BYTES):
    path = Path(path).absolute()
    _no_links(path)
    h, count = hashlib.sha256(), 0
    with _open_reader(path) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ProductionError("PRODUCTION_SOURCE_INVALID")
        while block := stream.read(1024*1024):
            count += len(block)
            if count > limit:
                raise ProductionError("PRODUCTION_SOURCE_LIMIT")
            h.update(block)
    if count == 0:
        raise ProductionError("PRODUCTION_SOURCE_INVALID")
    return {"sha256": h.hexdigest(), "size_bytes": count}


def _run(args, timeout):
    result = run_safe_subprocess(args, timeout_seconds=timeout, output_limit_bytes=1024*1024)
    if result.timed_out:
        raise ProductionError("PRODUCTION_DECODE_TIMEOUT")
    if result.returncode:
        raise ProductionError("PRODUCTION_DECODE_FAILED")
    return result.stdout


def inspect_control(path, scratch, *, timeout=5400):
    if type(timeout) is not int or not 1 <= timeout <= 5400:
        raise ProductionError("PRODUCTION_INVALID")
    ffmpeg, ffprobe = ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:
        raise ProductionError("PRODUCTION_TOOLS_UNAVAILABLE")
    info = json.loads(_run([ffprobe,"-v","error","-protocol_whitelist","file",
                           "-show_streams","-show_format","-of","json",str(path)],min(timeout,120)))
    streams = info.get("streams",[])
    if len(streams) != 1 or streams[0].get("codec_type") != "video" or "mp4" not in info.get("format",{}).get("format_name","").split(","):
        raise ProductionError("PRODUCTION_STREAM_INVALID")
    stream = streams[0]
    width,height = stream.get("width"),stream.get("height")
    if any(type(v) is not int or not 32 <= v <= 3840 or v%2 for v in (width,height)):
        raise ProductionError("PRODUCTION_DIMENSIONS_UNSUPPORTED")
    if (stream.get("sample_aspect_ratio","1:1") not in ("1:1","N/A")
            or stream.get("tags",{}).get("rotate") or stream.get("side_data_list")):
        raise ProductionError("PRODUCTION_GEOMETRY_UNSUPPORTED")
    rate = Fraction(stream["avg_frame_rate"])
    timebase = Fraction(stream["time_base"])
    if rate not in (Fraction(24),Fraction(24000,1001)) or Fraction(stream["r_frame_rate"]) != rate:
        raise ProductionError("PRODUCTION_CLOCK_UNSUPPORTED")
    declared = int(stream.get("nb_frames",0))
    if not 1 <= declared <= int(7200*rate):
        raise ProductionError("PRODUCTION_SOURCE_LIMIT")
    deadline = time.monotonic()+timeout
    # Each verified framehash chunk is durable, content-bound and independently
    # checked on resume. Raw decoded frames never accumulate in RAM or on disk.
    identity = digest({"source":file_identity(path),"width":width,"height":height,
                       "clock":str(rate),"count":declared,"ffmpeg":file_identity(ffmpeg)["sha256"]})
    chunks = scratch/"decode-checkpoints"
    chunks.mkdir(exist_ok=True)
    _no_links(chunks)
    pts_hash,frames_hash = hashlib.sha256(),hashlib.sha256()
    count = 0
    for start in range(0,declared,1440):
        end = min(start+1440,declared)
        target = chunks/f"{start:08d}.framehash"
        receipt = chunks/f"{start:08d}.json"
        for existing in (target,receipt):
            if existing.exists():_no_links(existing)
        if receipt.exists():
            prior = read_json(receipt)
            if prior != {"identity":identity,"start":start,"end":end,"file":file_identity(target)}:
                raise ProductionError("PRODUCTION_CHECKPOINT_CHANGED")
        else:
            remaining = int(deadline-time.monotonic())
            if remaining < 1:raise ProductionError("PRODUCTION_DECODE_TIMEOUT")
            # Seek slightly before the exact global frame, then select by original
            # PTS. The rational PTS check below refuses any offset/rounding drift.
            seek_us = (Fraction(start,1)/rate*1000000).__floor__()
            ticks = Fraction(start,1)/rate/timebase
            if ticks.denominator != 1:
                raise ProductionError("PRODUCTION_TIMING_INVALID")
            args=[ffmpeg,"-nostdin","-v","error","-xerror","-protocol_whitelist","file","-copyts"]
            if start:args += ["-ss",f"{seek_us//1000000}.{seek_us%1000000:06d}"]
            args += ["-i",str(path),"-map","0:v:0","-an","-vf",f"select=gte(pts\\,{ticks.numerator})",
                     "-fps_mode","passthrough","-enc_time_base:v",str(timebase),"-threads","1",
                     "-pix_fmt","rgb24","-frames:v",str(end-start+(end==declared)),
                     "-f","framehash","-hash","sha256","-y",str(target)]
            _run(args,remaining)
        if target.stat().st_size > 256*(end-start)+65536:
            raise ProductionError("PRODUCTION_DECODE_FAILED")
        decoded_tb = None
        with target.open("r",encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("#tb 0:"):
                    decoded_tb = Fraction(line.split(":",1)[1].strip())
                if line.startswith("#"):
                    continue
                cells = [c.strip() for c in line.split(",")]
                if (len(cells) != 6 or cells[0] != "0" or decoded_tb is None or count >= end
                        or int(cells[2])*decoded_tb != Fraction(count,1)/rate
                        or int(cells[4]) != width*height*3 or len(cells[5]) != 64):
                    raise ProductionError("PRODUCTION_TIMING_INVALID")
                pts_hash.update(f"{count}:{Fraction(count,1)/rate}\n".encode())
                frames_hash.update((cells[5]+"\n").encode())
                count += 1
        if count != end:
            raise ProductionError("PRODUCTION_TIMING_INVALID")
        publish(receipt,{"identity":identity,"start":start,"end":end,"file":file_identity(target)})
    if count != declared:
        raise ProductionError("PRODUCTION_TIMING_INVALID")
    return {"clock":{"num":rate.numerator,"den":rate.denominator},"frame_count":count,
            "width":width,"height":height,"timestamps_digest":pts_hash.hexdigest(),
            "decoded_frames_digest":frames_hash.hexdigest()}


def verify_control(folder):
    root = _root(folder)
    value = validate(read_json(root/"source.json"),"fsf_control_source_v2","source_digest")
    if file_identity(root/"control.mp4") != {k:value[k] for k in ("sha256","size_bytes")}:
        raise ProductionError("PRODUCTION_SOURCE_CHANGED")
    return value


def import_control(source, *, output_root, control_asset_id, timeout=5400):
    key(control_asset_id)
    source = Path(source).absolute()
    before = file_identity(source)
    root = _root(output_root)
    with edit_lock(root/".long-control"):
        scratch = root/(".long-control-"+digest([before,control_asset_id]))
        scratch.mkdir(exist_ok=True)
        _no_links(scratch)
        try:
            target = scratch/"control.mp4"
            if not target.exists():
                partial = scratch/"copy.partial"
                if partial.exists():_no_links(partial)
                with _open_reader(source) as incoming, partial.open("wb") as outgoing:
                    total = 0
                    while block := incoming.read(1024*1024):
                        total += len(block)
                        if total > before["size_bytes"]:
                            raise ProductionError("PRODUCTION_SOURCE_CHANGED")
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                os.replace(partial,target)
            if file_identity(target) != before or file_identity(source) != before:
                raise ProductionError("PRODUCTION_SOURCE_CHANGED")
            measured = inspect_control(target,scratch,timeout=timeout)
            value = seal({"schema_version":"fsf_control_source_v2","control_asset_id":control_asset_id,
                          "origin":"operator_control_video_import","clay_profile":"unverified",
                          "motion_review":"not_reviewed",**before,**measured},"source_digest")
            validate(value,"fsf_control_source_v2","source_digest")
            publish(scratch/"source.json",value)
            folder = root/value["source_digest"]
            if folder.exists():
                if verify_control(folder) != value:
                    raise ProductionError("PRODUCTION_INTEGRITY")
            else:
                os.rename(scratch,folder)
            if scratch.exists():shutil.rmtree(scratch)
            return {"bundle":folder.name,"source":value,"generation_dispatches":0}
        except BaseException:
            # Unpublished data remains resumable; readers never fall back to it.
            raise
