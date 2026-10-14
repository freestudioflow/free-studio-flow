"""FSF Fast Assembly Engine (Stage 6 / F12 / M5 reuse).

Deterministic FFmpeg assembly: pad/trim, audio mix, and timeline concat.
Decoupled from heavy generative models. Strictly refuses `stream_loop`.
Performs post-assembly Media a3 technical conformance evaluation and produces
tamper-evident receipts with canonical digest binding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from fsf.conformance.media import (
    DEFAULT_BROLL_EXPECTED,
    SafeFfprobeRunner,
    build_expected_contract,
    canonical_contract_digest,
    ensure_media_tools_on_path,
    evaluate_media_file,
)
from fsf.executors.subprocess_safety import run_safe_subprocess


def require_ffmpeg() -> str:
    """Ensure ffmpeg executable is available on PATH."""
    ffmpeg_exe, _ = ensure_media_tools_on_path()
    if not ffmpeg_exe:
        raise RuntimeError("ffmpeg not found on PATH; required for Fast Assembly")
    return ffmpeg_exe


def probe_clip_duration(clip_path: Path) -> float:
    """Probe duration of a media clip in seconds via safe ffprobe."""
    runner = SafeFfprobeRunner()
    raw = runner.probe(clip_path)
    if isinstance(raw, dict):
        fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
        try:
            return float(fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            pass
    return 0.0


def clip_has_audio(clip_path: Path) -> bool:
    """Check if media file contains an audio stream via safe ffprobe."""
    runner = SafeFfprobeRunner()
    raw = runner.probe(clip_path)
    if isinstance(raw, dict):
        streams = raw.get("streams", [])
        return any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
    return False


def prepare_segment_video(
    src_video: Path,
    dst_video: Path,
    *,
    target_duration: float,
    width: int = 1024,
    height: int = 576,
    fps: int = 24,
    audio_path: Path | None = None,
    audio_policy: str = "keep",
    force_audio: bool = False,
) -> None:
    """Fit a video clip to target duration and standard format.

    - If clip is shorter than target_duration: pads freeze-frame with `tpad` (NO `stream_loop`).
    - If clip is longer than target_duration: trims with `-t`.
    - Rescales and letterboxes/pillarboxes to width x height.
    - Standardizes codec to libx264, yuv420p, fixed fps.
    - If audio_path is provided, mixes audio; otherwise enforces audio_policy:
      * 'keep': preserves segment audio or mixes audio_path; if segment is silent
        and force_audio is True (timeline has audio), pads with anullsrc.
      * 'require': fails closed if segment and audio_path are both missing audio.
      * 'strip': strips segment audio (-an).
    - Declares all inputs upfront before output options and stream mapping.
    """
    ffmpeg = require_ffmpeg()
    src = Path(src_video).resolve()
    dst = Path(dst_video).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.is_file():
        raise FileNotFoundError(f"Source video segment not found: {src}")

    src_dur = probe_clip_duration(src)
    pad_needed = max(0.0, target_duration - src_dur)

    vf_filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
    ]
    if pad_needed > 0.01:
        # Strictly freeze-frame clone padding; NO stream_loop
        vf_filters.append(f"tpad=stop_mode=clone:stop_duration={pad_needed:.3f}")

    vf_arg = ",".join(vf_filters)

    has_vo = audio_path is not None and Path(audio_path).is_file()
    src_has_audio = clip_has_audio(src)

    norm_policy = audio_policy.lower()

    cmd: list[str] = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-ss",
        "0",
        "-i",
        str(src),
    ]

    use_anullsrc = False
    include_audio = False

    if norm_policy in ("strip", "forbidden"):
        include_audio = False
    elif has_vo:
        cmd.extend(["-i", str(Path(audio_path).resolve())])
        include_audio = True
    elif src_has_audio:
        include_audio = True
    elif norm_policy in ("require", "required") or force_audio:
        use_anullsrc = True
        include_audio = True
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=48000:cl=stereo:d={target_duration:.3f}",
            ]
        )

    # Output options declared after all input options
    cmd.extend(
        [
            "-t",
            f"{target_duration:.3f}",
            "-vf",
            vf_arg,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
        ]
    )

    if include_audio:
        if has_vo:
            cmd.extend(
                [
                    "-filter_complex",
                    f"[1:a]apad=whole_dur={target_duration:.3f}[a]",
                    "-map",
                    "0:v",
                    "-map",
                    "[a]",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                ]
            )
        elif use_anullsrc:
            cmd.extend(
                [
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                ]
            )
        else:
            # Source already has audio: pad with apad in case audio is shorter than target duration
            cmd.extend(
                [
                    "-filter_complex",
                    f"[0:a]apad=whole_dur={target_duration:.3f}[a]",
                    "-map",
                    "0:v",
                    "-map",
                    "[a]",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                ]
            )
    else:
        cmd.extend(["-map", "0:v", "-an"])

    cmd.append(str(dst))
    sub_res = run_safe_subprocess(cmd, timeout_seconds=120.0, check=False)
    if sub_res.returncode != 0:
        err_snippet = sub_res.stderr.strip() or sub_res.stdout.strip()
        lines = [line.strip() for line in err_snippet.splitlines() if line.strip()]
        snippet = " | ".join(lines[-4:]) if lines else f"exit code {sub_res.returncode}"
        raise RuntimeError(
            f"FFmpeg segment preparation failed for '{src.name}' (code {sub_res.returncode}, {sub_res.elapsed_ms}ms):\n{snippet}"
        )


def concat_segments(
    segment_paths: Sequence[Path],
    output_path: Path,
    *,
    fps: int = 24,
) -> None:
    """Concatenate uniform video segments into a single file via concat demuxer."""
    ffmpeg = require_ffmpeg()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if not segment_paths:
        raise ValueError("Cannot concatenate empty segment list")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        list_file = Path(f.name)
        for seg in segment_paths:
            resolved = Path(seg).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"Segment file not found for concat: {resolved}")
            escaped = resolved.as_posix().replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        # First attempt fast stream-copy
        cmd_copy = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(out),
        ]
        res = run_safe_subprocess(cmd_copy, timeout_seconds=120.0, check=False)
        if res.returncode != 0:
            # Fallback to normalized re-encode concat if timestamps differ
            cmd_reencode = [
                ffmpeg,
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-c:a",
                "copy",
                str(out),
            ]
            res2 = run_safe_subprocess(cmd_reencode, timeout_seconds=180.0, check=False)
            if res2.returncode != 0:
                raw_err = res2.stderr.strip() or res2.stdout.strip()
                lines = [line.strip() for line in raw_err.splitlines() if line.strip()]
                clean_err = " | ".join(lines[-4:]) if lines else f"exit code {res2.returncode}"
                raise RuntimeError(
                    f"FFmpeg concat failed (code {res2.returncode}, {res2.elapsed_ms}ms):\n{clean_err}"
                )
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass


def format_srt_time(seconds: float) -> str:
    """Format seconds into standard SRT timestamp HH:MM:SS,mmm."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def generate_coarse_srt(
    subtitles: Sequence[dict[str, Any]],
    output_srt_path: Path | str,
) -> Path:
    """Generate an SRT subtitle file from coarse shot schedules.

    Note: When exact word-level speech alignment is absent, this produces
    Coarse Subtitle Scheduling based on shot boundaries and dialogue text.
    """
    out_p = Path(output_srt_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    idx = 1
    for sub in subtitles:
        text = str(sub.get("text", "")).strip()
        if not text:
            continue
        start = float(sub.get("start_seconds", 0.0))
        end = float(sub.get("end_seconds", start + 1.0))
        lines.append(str(idx))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(text)
        lines.append("")
        idx += 1
    out_p.write_text("\n".join(lines), encoding="utf-8")
    return out_p


def burn_subtitles_to_video(
    input_video: Path,
    output_video: Path,
    srt_path: Path,
    *,
    fps: int = 24,
) -> bool:
    """Burn subtitles directly onto a video using FFmpeg subtitles filter.

    Returns True if successfully burned, False if filter failed (e.g. font / libass missing).
    """
    ffmpeg = require_ffmpeg()
    src = Path(input_video).resolve()
    dst = Path(output_video).resolve()
    srt = Path(srt_path).resolve()

    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-vf",
        f"subtitles=filename={srt.name}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "copy",
        str(dst),
    ]

    res = run_safe_subprocess(
        cmd,
        cwd=str(srt.parent),
        timeout_seconds=120.0,
        check=False,
    )
    if res.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
        return True
    return False


def mix_audio_tracks(
    video_path: Path,
    output_path: Path,
    *,
    duration_seconds: float,
    vo_path: Path | None = None,
    bgm_path: Path | None = None,
    vo_volume: float = 1.0,
    bgm_volume: float = 0.3,
) -> None:
    """Mix separate VO and BGM audio tracks onto an assembled video, preserving video segment audio."""
    ffmpeg = require_ffmpeg()
    vid = Path(video_path).resolve()
    dst = Path(output_path).resolve()

    has_vo = vo_path is not None and Path(vo_path).is_file()
    has_bgm = bgm_path is not None and Path(bgm_path).is_file()
    vid_has_audio = clip_has_audio(vid)

    if not has_vo and not has_bgm:
        shutil.copy2(vid, dst)
        return

    cmd = [ffmpeg, "-nostdin", "-y", "-i", str(vid)]
    inputs_count = 1

    audio_labels: list[str] = []
    filter_clauses: list[str] = []

    if vid_has_audio:
        filter_clauses.append(f"[0:a]volume=1.00,apad=whole_dur={duration_seconds:.3f}[a_vid]")
        audio_labels.append("[a_vid]")

    if has_vo:
        cmd.extend(["-i", str(Path(vo_path).resolve())])
        vo_idx = inputs_count
        inputs_count += 1
        filter_clauses.append(f"[{vo_idx}:a]volume={vo_volume:.2f},apad=whole_dur={duration_seconds:.3f}[a_vo]")
        audio_labels.append("[a_vo]")

    if has_bgm:
        cmd.extend(["-i", str(Path(bgm_path).resolve())])
        bgm_idx = inputs_count
        inputs_count += 1
        filter_clauses.append(f"[{bgm_idx}:a]volume={bgm_volume:.2f},apad=whole_dur={duration_seconds:.3f}[a_bgm]")
        audio_labels.append("[a_bgm]")

    if not audio_labels:
        shutil.copy2(vid, dst)
        return

    if len(audio_labels) == 1:
        # Single audio source: pipe directly to [aout]
        only_label = audio_labels[0]
        filter_clauses[0] = filter_clauses[0].replace(only_label, "[aout]")
        fc = filter_clauses[0]
    else:
        mix_inputs = "".join(audio_labels)
        filter_clauses.append(
            f"{mix_inputs}amix=inputs={len(audio_labels)}:duration=first:dropout_transition=2[aout]"
        )
        fc = ";".join(filter_clauses)

    cmd.extend([
        "-filter_complex", fc,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-t", f"{duration_seconds:.3f}",
        str(dst),
    ])

    res = run_safe_subprocess(cmd, timeout_seconds=120.0, check=False)
    if res.returncode != 0:
        raw_err = res.stderr.strip() or res.stdout.strip()
        lines = [line.strip() for line in raw_err.splitlines() if line.strip()]
        clean_err = " | ".join(lines[-4:]) if lines else f"exit code {res.returncode}"
        raise RuntimeError(f"FFmpeg audio mixing failed (code {res.returncode}, {res.elapsed_ms}ms):\n{clean_err}")


@dataclass(frozen=True)
class FastAssemblyReceipt:
    """Tamper-evident receipt of a deterministic Fast Assembly execution.

    Integrity is protected by canonical SHA-256 digest binding (`receipt_digest`
    covers all manifest metadata, and `output_sha256` covers output media bytes).
    """

    output_path: str
    output_sha256: str
    total_duration_seconds: float
    segment_count: int
    source_segments: tuple[dict[str, Any], ...]
    expected_media_contract: dict[str, Any]
    expected_media_contract_digest: str
    conformance_report: dict[str, Any]
    host_action: dict[str, Any]
    receipt_digest: str
    subtitle_path: str | None = None
    subtitle_sha256: str | None = None
    bgm_path: str | None = None
    bgm_sha256: str | None = None
    vo_path: str | None = None
    vo_sha256: str | None = None
    subtitle_policy: str = "none"
    subtitle_scheduling: str = "none"
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached deep copy of the receipt as a dictionary."""
        return {
            "schema_version": "fsf_assembly_receipt_v1",
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "total_duration_seconds": self.total_duration_seconds,
            "segment_count": self.segment_count,
            "source_segments": [copy.deepcopy(s) for s in self.source_segments],
            "expected_media_contract": copy.deepcopy(self.expected_media_contract),
            "expected_media_contract_digest": self.expected_media_contract_digest,
            "conformance_report": copy.deepcopy(self.conformance_report),
            "host_action": copy.deepcopy(self.host_action),
            "subtitle_path": self.subtitle_path,
            "subtitle_sha256": self.subtitle_sha256,
            "bgm_path": self.bgm_path,
            "bgm_sha256": self.bgm_sha256,
            "vo_path": self.vo_path,
            "vo_sha256": self.vo_sha256,
            "subtitle_policy": self.subtitle_policy,
            "subtitle_scheduling": self.subtitle_scheduling,
            "warning": self.warning,
            "receipt_digest": self.receipt_digest,
        }

    def verify_integrity(self, *, check_file_sha: bool = True) -> bool:
        """Verify receipt integrity against canonical digests."""
        return verify_assembly_receipt(self, check_file_sha=check_file_sha)


def build_assembly_receipt(
    *,
    output_path: str,
    output_sha256: str,
    total_duration_seconds: float,
    segment_count: int,
    source_segments: Sequence[dict[str, Any]],
    expected_media_contract: dict[str, Any],
    expected_media_contract_digest: str,
    conformance_report: dict[str, Any],
    host_action: dict[str, Any],
    subtitle_path: str | None = None,
    subtitle_sha256: str | None = None,
    bgm_path: str | None = None,
    bgm_sha256: str | None = None,
    vo_path: str | None = None,
    vo_sha256: str | None = None,
    subtitle_policy: str = "none",
    subtitle_scheduling: str = "none",
    warning: str | None = None,
) -> FastAssemblyReceipt:
    """Build a FastAssemblyReceipt with canonical whole-receipt digest binding."""
    deep_segments = tuple(copy.deepcopy(s) for s in source_segments)
    deep_expected = copy.deepcopy(expected_media_contract)
    deep_report = copy.deepcopy(conformance_report)
    deep_action = copy.deepcopy(host_action)

    payload_for_digest = {
        "schema_version": "fsf_assembly_receipt_v1",
        "output_path": str(output_path),
        "output_sha256": str(output_sha256),
        "total_duration_seconds": float(total_duration_seconds),
        "segment_count": int(segment_count),
        "source_segments": list(deep_segments),
        "expected_media_contract": deep_expected,
        "expected_media_contract_digest": str(expected_media_contract_digest),
        "conformance_report": deep_report,
        "host_action": deep_action,
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "subtitle_sha256": str(subtitle_sha256) if subtitle_sha256 else None,
        "bgm_path": str(bgm_path) if bgm_path else None,
        "bgm_sha256": str(bgm_sha256) if bgm_sha256 else None,
        "vo_path": str(vo_path) if vo_path else None,
        "vo_sha256": str(vo_sha256) if vo_sha256 else None,
        "subtitle_policy": str(subtitle_policy),
        "subtitle_scheduling": str(subtitle_scheduling),
        "warning": str(warning) if warning else None,
    }
    digest = canonical_contract_digest(payload_for_digest)

    return FastAssemblyReceipt(
        output_path=str(output_path),
        output_sha256=str(output_sha256),
        total_duration_seconds=float(total_duration_seconds),
        segment_count=int(segment_count),
        source_segments=deep_segments,
        expected_media_contract=deep_expected,
        expected_media_contract_digest=str(expected_media_contract_digest),
        conformance_report=deep_report,
        host_action=deep_action,
        receipt_digest=digest,
        subtitle_path=str(subtitle_path) if subtitle_path else None,
        subtitle_sha256=str(subtitle_sha256) if subtitle_sha256 else None,
        bgm_path=str(bgm_path) if bgm_path else None,
        bgm_sha256=str(bgm_sha256) if bgm_sha256 else None,
        vo_path=str(vo_path) if vo_path else None,
        vo_sha256=str(vo_sha256) if vo_sha256 else None,
        subtitle_policy=str(subtitle_policy),
        subtitle_scheduling=str(subtitle_scheduling),
        warning=str(warning) if warning else None,
    )


def verify_assembly_receipt(
    receipt: FastAssemblyReceipt | dict[str, Any],
    *,
    check_file_sha: bool = True,
) -> bool:
    """Verify that an assembly receipt has not been tampered with.

    Checks:
    1. Schema version and receipt_digest match canonical digest of all fields.
    2. Expected media contract matches expected_media_contract_digest.
    3. If check_file_sha is True, output, subtitle, bgm, vo files must exist and their SHA-256 match.
    """
    data = receipt.to_dict() if isinstance(receipt, FastAssemblyReceipt) else copy.deepcopy(receipt)
    if not isinstance(data, dict):
        return False
    if data.get("schema_version") != "fsf_assembly_receipt_v1":
        return False

    stored_digest = data.pop("receipt_digest", None)
    if not isinstance(stored_digest, str) or len(stored_digest) != 64:
        return False

    computed_digest = canonical_contract_digest(data)
    if stored_digest.casefold() != computed_digest.casefold():
        return False

    expected = data.get("expected_media_contract")
    if not isinstance(expected, dict):
        return False
    if canonical_contract_digest(expected) != data.get("expected_media_contract_digest"):
        return False

    if check_file_sha:
        p = Path(data.get("output_path", ""))
        if not p.is_file():
            return False
        try:
            current_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return False
        if current_sha.casefold() != str(data.get("output_sha256", "")).casefold():
            return False

        if data.get("subtitle_path") and data.get("subtitle_sha256"):
            sub_p = Path(data["subtitle_path"])
            if not sub_p.is_file():
                return False
            try:
                curr_sub_sha = hashlib.sha256(sub_p.read_bytes()).hexdigest()
            except OSError:
                return False
            if curr_sub_sha.casefold() != str(data["subtitle_sha256"]).casefold():
                return False

        if data.get("bgm_path") and data.get("bgm_sha256"):
            bgm_p = Path(data["bgm_path"])
            if not bgm_p.is_file():
                return False
            try:
                curr_bgm_sha = hashlib.sha256(bgm_p.read_bytes()).hexdigest()
            except OSError:
                return False
            if curr_bgm_sha.casefold() != str(data["bgm_sha256"]).casefold():
                return False

        if data.get("vo_path") and data.get("vo_sha256"):
            vo_p = Path(data["vo_path"])
            if not vo_p.is_file():
                return False
            try:
                curr_vo_sha = hashlib.sha256(vo_p.read_bytes()).hexdigest()
            except OSError:
                return False
            if curr_vo_sha.casefold() != str(data["vo_sha256"]).casefold():
                return False

    return True


def fast_assemble_timeline(
    timeline: Sequence[dict[str, Any]],
    output_path: Path | str,
    *,
    work_dir: Path | str | None = None,
    width: int = 1024,
    height: int = 576,
    fps: int = 24,
    audio_policy: str = "keep",
    expected_contract: dict[str, Any] | None = None,
    subtitles: Sequence[dict[str, Any]] | None = None,
    subtitle_policy: str = "none",  # "none" | "sidecar" | "burned"
    bgm_path: Path | str | None = None,
    bgm_volume: float = 0.3,
    vo_path: Path | str | None = None,
    vo_volume: float = 1.0,
    allow_burned_fallback: bool = False,
) -> FastAssemblyReceipt:
    """Execute deterministic Fast Assembly of a sequence of shot segments.

    Parameters:
        timeline: sequence of dicts, each specifying:
            - "shot_id": str
            - "video_path": Path | str
            - "duration_seconds": float
            - "audio_path": (optional) Path | str
        output_path: path to write final assembled MP4
        work_dir: optional scratch directory; cleaned up if not provided
        width, height, fps: target video specifications
        audio_policy: "forbidden" | "required"
        expected_contract: optional expected contract override; otherwise compiled
            from timeline total duration and video parameters.
        subtitles: sequence of dicts with {"text": str, "start_seconds": float, "end_seconds": float}
        subtitle_policy: "none", "sidecar" (.srt file), or "burned" (hardcoded to video)
        bgm_path: optional background music audio track to mix
        bgm_volume: BGM mix volume multiplier (default 0.3)
        vo_path: optional voiceover audio track to mix
        vo_volume: VO mix volume multiplier (default 1.0)

    Returns:
        FastAssemblyReceipt containing assembled output details and M5 Media report.
    """
    if not timeline:
        raise ValueError("Cannot assemble timeline with 0 segments")

    final_out = Path(output_path).resolve()
    final_out.parent.mkdir(parents=True, exist_ok=True)

    owns_work_dir = work_dir is None
    temp_dir = Path(tempfile.mkdtemp(prefix="fsf_assembly_")) if owns_work_dir else Path(work_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Fail-closed validation for explicitly requested audio inputs
    if vo_path is not None:
        vo_p = Path(vo_path).resolve()
        if not vo_p.is_file():
            raise FileNotFoundError(f"Specified VO audio track file not found: {vo_path}")
    else:
        vo_p = None

    if bgm_path is not None:
        bgm_p = Path(bgm_path).resolve()
        if not bgm_p.is_file():
            raise FileNotFoundError(f"Specified BGM audio track file not found: {bgm_path}")
    else:
        bgm_p = None

    prepared_segments: list[Path] = []
    segment_records: list[dict[str, Any]] = []
    total_duration = 0.0

    norm_audio = (audio_policy or "keep").lower()
    any_shot_has_audio = any(
        (item.get("audio_path") and Path(item["audio_path"]).is_file())
        or clip_has_audio(Path(item["video_path"]))
        for item in timeline
    )
    has_global_audio = (vo_p is not None or bgm_p is not None)

    if norm_audio in ("keep", "preserve"):
        effective_audio_policy = "required" if (any_shot_has_audio or has_global_audio) else "forbidden"
        force_segment_audio = (effective_audio_policy == "required")
    elif norm_audio in ("require", "required"):
        effective_audio_policy = "required"
        force_segment_audio = True
    elif norm_audio in ("strip", "forbidden"):
        effective_audio_policy = "required" if has_global_audio else "forbidden"
        force_segment_audio = False
    else:
        raise ValueError(f"Unknown audio policy '{audio_policy}'; expected keep, strip, or require")

    try:
        for idx, item in enumerate(timeline):
            shot_id = str(item.get("shot_id") or f"shot_{idx:03d}")
            src_video = Path(item["video_path"]).resolve()
            dur = float(item["duration_seconds"])
            audio_p = Path(item["audio_path"]).resolve() if item.get("audio_path") else None

            dst_seg = temp_dir / f"seg_{idx:03d}_{shot_id}.mp4"
            prepare_segment_video(
                src_video,
                dst_seg,
                target_duration=dur,
                width=width,
                height=height,
                fps=fps,
                audio_path=audio_p,
                audio_policy=norm_audio,
                force_audio=force_segment_audio,
            )
            prepared_segments.append(dst_seg)
            total_duration += dur

            segment_records.append(
                {
                    "shot_id": shot_id,
                    "source_video": str(src_video),
                    "duration_seconds": dur,
                    "audio_path": str(audio_p) if audio_p else None,
                }
            )

        raw_concat = temp_dir / "raw_concat.mp4"
        concat_segments(prepared_segments, raw_concat, fps=fps)

        # 2. Audio mixing if global vo_p or bgm_p provided
        if has_global_audio:
            mixed_video = temp_dir / "mixed_video.mp4"
            mix_audio_tracks(
                raw_concat,
                mixed_video,
                duration_seconds=total_duration,
                vo_path=vo_p,
                bgm_path=bgm_p,
                vo_volume=vo_volume,
                bgm_volume=bgm_volume,
            )
            stage_video = mixed_video
        else:
            stage_video = raw_concat

        # 3. Subtitles processing
        sub_path_str: str | None = None
        sub_sha_str: str | None = None
        effective_sub_policy = subtitle_policy
        scheduling_type = "none"
        warning_msg: str | None = None

        if subtitles:
            scheduling_type = "coarse"
            sidecar_file = final_out.with_suffix(".srt")
            generate_coarse_srt(subtitles, sidecar_file)
            sub_path_str = str(sidecar_file)
            sub_sha_str = hashlib.sha256(sidecar_file.read_bytes()).hexdigest()

            if subtitle_policy == "burned":
                burned_video = temp_dir / "burned_video.mp4"
                ok = burn_subtitles_to_video(stage_video, burned_video, sidecar_file, fps=fps)
                if ok:
                    stage_video = burned_video
                else:
                    if not allow_burned_fallback:
                        raise RuntimeError(
                            f"Failed to burn subtitles into video '{final_out}'. "
                            f"FFmpeg subtitle burning filter failed. Fail-closed: refusing silent downgrade to sidecar."
                        )
                    effective_sub_policy = "sidecar"
                    warning_msg = "Burned subtitles failed; explicitly allowed fallback to sidecar"

        # Atomically publish to final_out using same-directory stage + os.replace
        stage_in_dest = final_out.with_name(
            f".{final_out.name}.stage_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        )
        try:
            shutil.copy2(stage_video, stage_in_dest)
            os.replace(stage_in_dest, final_out)
        finally:
            if stage_in_dest.exists():
                stage_in_dest.unlink(missing_ok=True)

        # M5 Media Technical Conformance Check
        if expected_contract is None:
            expected = build_expected_contract(
                {"duration_seconds": total_duration},
                width=width,
                height=height,
                fps=fps,
                audio=effective_audio_policy,
            )
        else:
            expected = dict(expected_contract)

        expected_digest = canonical_contract_digest(expected)
        report, host_action = evaluate_media_file(final_out, expected)

        sha = hashlib.sha256(final_out.read_bytes()).hexdigest()

        bgm_sha = hashlib.sha256(bgm_p.read_bytes()).hexdigest() if bgm_p else None
        vo_sha = hashlib.sha256(vo_p.read_bytes()).hexdigest() if vo_p else None

        return build_assembly_receipt(
            output_path=str(final_out),
            output_sha256=sha,
            total_duration_seconds=total_duration,
            segment_count=len(timeline),
            source_segments=segment_records,
            expected_media_contract=expected,
            expected_media_contract_digest=expected_digest,
            conformance_report=report,
            host_action=host_action,
            subtitle_path=sub_path_str,
            subtitle_sha256=sub_sha_str,
            bgm_path=str(bgm_p) if bgm_p else None,
            bgm_sha256=bgm_sha,
            vo_path=str(vo_p) if vo_p else None,
            vo_sha256=vo_sha,
            subtitle_policy=effective_sub_policy,
            subtitle_scheduling=scheduling_type,
            warning=warning_msg,
        )

    finally:
        if owns_work_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
