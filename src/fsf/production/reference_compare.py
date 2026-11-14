"""Map reference frames to an independently edited version."""
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PIL import Image, ImageDraw

from fsf.conformance.media import ensure_media_tools_on_path
from fsf.core.editing import inspect_edit
from fsf.production.contracts import seal
from fsf.production.control import file_identity
from fsf.production.reference_analysis import _run
from fsf.production.reference_draft import analysis_binding, fields, require
from fsf.runtime.post_runtime import verify_post_receipt


def compare(analysis, edit, mapping):
    binding, reference_shots = analysis_binding(analysis)
    view = inspect_edit(edit)
    clips = {clip["clip_id"]: clip for clip in view["clips"]}
    clock = view["clock_rate"]
    frame_time = Fraction(clock["den"], clock["num"])
    timeline = analysis["timeline"]
    pts = timeline["pts"]
    time_base = Fraction(*timeline["time_base"])
    rows = []
    used_clips = set()
    require(isinstance(mapping, list) and mapping, "REFERENCE_COMPARE_INVALID")
    for match in mapping:
        fields(match, "reference_shot clip_id")
        shot = match["reference_shot"]
        clip_id = match["clip_id"]
        require(shot in reference_shots and clip_id in clips and clip_id not in used_clips,
                "REFERENCE_COMPARE_INVALID")
        used_clips.add(clip_id)
        clip = clips[clip_id]
        start, end = reference_shots[shot]
        reference_pts = [pts[start], pts[end] if end < len(pts) else timeline["end_pts"]]
        require(type(reference_pts[1]) is int and reference_pts[1] > reference_pts[0],
                "REFERENCE_COMPARE_TIMING_UNKNOWN")
        output_range = [clip["timeline_start_frame"], clip["timeline_end_frame"]]
        reference_seconds = (reference_pts[1] - reference_pts[0]) * time_base
        output_seconds = (output_range[1] - output_range[0]) * frame_time
        ratio = output_seconds / reference_seconds
        rows.append({
            **match, "reference_frame_range": [start, end], "reference_pts": reference_pts,
            "output_frame_range": output_range,
            "edit_source_frame_range": [clip["in_frame"], clip["out_frame"]],
            "edit_source_sha256": clip["source_sha256"],
            "duration_ratio": [ratio.numerator, ratio.denominator],
        })
    return seal({
        "schema_version": "reference_comparison_v1", **binding,
        "edit_digest": view["edit_digest"], "edit_revision": view["revision"],
        "reference_time_base": timeline["time_base"], "output_clock": clock,
        "matches": rows,
        "unmapped_reference_shots": sorted(reference_shots.keys() - {row["reference_shot"] for row in rows}),
        "unmapped_clip_ids": sorted(clips.keys() - used_clips),
        "metric_geometry": {"status": "unknown", "reason": "no_calibrated_geometry"},
        "semantic_quality": "not_evaluated",
    }, "comparison_digest")


def attach_sheet(report, source, delivery, target, *, seconds):
    """Render paired midpoint frames from the original and verified delivery."""
    source, delivery, target = map(lambda path: Path(path).absolute(), (source, delivery, target))
    require(len(report["matches"]) <= 100, "REFERENCE_COMPARE_LIMIT")
    require(verify_post_receipt(delivery), "REFERENCE_COMPARE_DELIVERY")
    from fsf.core.edit_store import read_json

    receipt = read_json(delivery / "receipt.json")
    require((receipt["edit_digest"], receipt["revision"]) ==
            (report["edit_digest"], report["edit_revision"]), "REFERENCE_COMPARE_DELIVERY")
    movie = delivery / "movie.mp4"
    original = file_identity(source)
    output = file_identity(movie)
    require(original["sha256"] == report["source_sha256"], "REFERENCE_SOURCE_CHANGED")
    require(any(item["path"] == "movie.mp4" and item["sha256"] == output["sha256"]
                for item in receipt["files"]), "REFERENCE_COMPARE_DELIVERY")
    pairs = [[(start + end - 1) // 2 for start, end in
              (row["reference_frame_range"], row["output_frame_range"])]
             for row in report["matches"]]
    ffmpeg, _ = ensure_media_tools_on_path()
    deadline = time.monotonic() + seconds
    width, height = 160, 90
    stride = width * height * 3
    frames = []
    with TemporaryDirectory(prefix=".reference-compare-", dir=target.parent) as temporary:
        for column, path in enumerate((source, movie)):
            indices = sorted({pair[column] for pair in pairs})
            select = "+".join(f"eq(n\\,{index})" for index in indices)
            raw = Path(temporary) / f"column-{column}.rgb"
            _run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file",
                  "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,nut,avi,mpegts,mpeg",
                  "-i", str(path), "-map", "0:v:0", "-vf",
                  f"select={select},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=rgb24",
                  "-fps_mode", "passthrough", "-frames:v", str(len(indices)),
                  "-threads", "1", "-f", "rawvideo", str(raw)], deadline)
            pixels = raw.read_bytes()
            require(len(pixels) == stride * len(indices), "REFERENCE_COMPARE_FRAME_MAPPING")
            frames.append({index: Image.frombytes("RGB", (width, height), pixels[i * stride:(i + 1) * stride])
                           for i, index in enumerate(indices)})
    require(file_identity(source) == original and file_identity(movie) == output
            and read_json(delivery / "receipt.json") == receipt, "REFERENCE_SOURCE_CHANGED")
    sheet = Image.new("RGB", (width * 2, (height + 18) * len(pairs)))
    draw = ImageDraw.Draw(sheet)
    for row, pair in enumerate(pairs):
        y = row * (height + 18)
        for column, index in enumerate(pair):
            x = column * width
            sheet.paste(frames[column][index], (x, y))
            label = "Reference" if column == 0 else "Delivery"
            draw.text((x + 4, y + height), f"{label} frame {index}", fill="white")
    with target.open("xb") as stream:
        sheet.save(stream, format="PNG")
    return seal({**report, "comparison_sheet": {
        "artifact": file_identity(target), "frame_pairs": pairs,
        "delivery_receipt_digest": receipt["receipt_digest"], "delivery_movie": output,
    }}, "comparison_digest")
