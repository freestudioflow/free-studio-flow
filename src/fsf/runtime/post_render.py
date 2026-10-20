"""Bounded FFmpeg editing with rational clocks, no looping or freeze padding."""
from fractions import Fraction
from contextvars import ContextVar
from pathlib import Path

from fsf.assembly.fast_assembly import require_ffmpeg
from fsf.core.editing import inspect_edit
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.runtime.post_sources import PostError

FFMPEG_CALLS = ContextVar("post_ffmpeg_calls", default=None)


def run_ffmpeg(arguments, cwd):
    counter = FFMPEG_CALLS.get()
    if counter is not None:
        counter["ffmpeg_calls"] += 1
    from fsf.runtime.audio_mix import ACTIVE_DEADLINE
    from fsf.core.audio_edit import AudioError
    deadline = ACTIVE_DEADLINE.get()
    try:
        result = run_safe_subprocess([require_ffmpeg(), "-nostdin", "-y", *map(str, arguments)],
                                     cwd=cwd, timeout_seconds=deadline.remaining() if deadline else 120)
        if getattr(result, "timed_out", False):
            raise AudioError("AUDIO_DEADLINE_EXCEEDED") if deadline else PostError("POST_RENDER")
        if result.returncode != 0:
            if deadline and ("no such filter" in result.stderr.casefold() or "filter not found" in result.stderr.casefold()):
                raise AudioError("AUDIO_FILTER_UNAVAILABLE")
            raise AudioError("AUDIO_ANALYSIS_FAILED") if deadline else PostError("POST_RENDER")
        font_log = result.stderr.casefold()
        if ("glyph" in font_log and "not found" in font_log or "failed to find any fallback" in font_log):
            raise PostError("POST_SUBTITLE_FONT")
        return result
    except (PostError, AudioError):
        raise
    except TimeoutError:
        raise AudioError("AUDIO_DEADLINE_EXCEEDED") if deadline else PostError("POST_RENDER") from None
    except Exception:
        raise AudioError("AUDIO_ANALYSIS_FAILED") if deadline else PostError("POST_RENDER") from None


def tool_version():
    return run_ffmpeg(["-version"], None).stdout.splitlines()[0]


def _stamp(frame, clock):
    ms = round(Fraction(frame * clock["den"] * 1000, clock["num"]))
    seconds, milli = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{sec:02},{milli:03}"


def render(edit, settings, sources, folder, *, preview_range=None):
    folder.mkdir(parents=True, exist_ok=True)
    clock = edit["clock_rate"]
    fps = Fraction(clock["num"], clock["den"])
    rate = f"{clock['num']}/{clock['den']}"
    view = inspect_edit(edit)
    clips = edit["clips"]
    preview_start = None
    if preview_range:
        clip_id, start, end = preview_range
        selected = [c for c in view["clips"] if c["clip_id"] == clip_id]
        if len(selected) != 1 or type(start) is not int or type(end) is not int or not 0 <= start < end <= selected[0]["out_frame"] - selected[0]["in_frame"]:
            raise PostError("POST_PREVIEW_RANGE")
        preview_start = selected[0]["timeline_start_frame"] + start
        clips = [{**selected[0], "in_frame": selected[0]["in_frame"] + start, "out_frame": selected[0]["in_frame"] + end}]
    has_audio = lambda source_id: any(s.get("codec_type") == "audio" for s in sources[source_id]["raw"].get("streams", []))
    audio = settings["audio"] != "strip" and (settings["audio"] == "require" or any(has_audio(c["source_id"]) for c in clips))
    segments, frame_counts = [], []
    for index, clip in enumerate(clips):
        source = sources[clip["source_id"]]
        count = clip["out_frame"] - clip["in_frame"]
        duration = float(count / fps)
        destination = folder / f"clip_{index:04}.mp4"
        args = ["-i", source["path"]]
        present = has_audio(clip["source_id"])
        if audio and not present:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        vf = f"fps={rate},trim=start_frame={clip['in_frame']}:end_frame={clip['out_frame']},setpts=PTS-STARTPTS,scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease,pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        args += ["-map", "0:v:0", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", rate, "-t", f"{duration:.9f}"]
        if audio:
            start_seconds = float(clip["in_frame"] / fps) if present else 0
            args += ["-map", "0:a:0" if present else "1:a:0", "-af", f"atrim=start={start_seconds:.9f}:end={start_seconds + duration:.9f},asetpts=PTS-STARTPTS,apad=whole_dur={duration:.9f}", "-ar", "48000", "-ac", "2", "-c:a", "aac"]
        else:
            args += ["-an"]
        args += [destination]
        run_ffmpeg(args, folder)
        segments.append(destination)
        frame_counts.append(count)
    captions = None
    if settings["subtitles"] != "none":
        selected_cues = [cue for cue in view["cues"] if cue["track_id"] == settings["subtitle_track"]]
        lines, number = [], 1
        for cue in selected_cues:
            start, end = cue["timeline_start_frame"], cue["timeline_end_frame"]
            if preview_start is not None:
                start, end = max(start, preview_start) - preview_start, min(end, preview_start + sum(frame_counts)) - preview_start
            if end <= start:
                continue
            text = "\n".join(line for line in cue["text"].splitlines() if line.strip())
            if not text or "\0" in text:
                raise PostError("POST_SUBTITLE_TEXT")
            lines += [str(number), f"{_stamp(start, clock)} --> {_stamp(end, clock)}", text, ""]
            number += 1
        captions = folder / "captions.srt"
        captions.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args, filters, inputs = [], [], []
    for index, (segment, count) in enumerate(zip(segments, frame_counts)):
        args += ["-i", segment]
        filters.append(f"[{index}:v]fps={rate},trim=end_frame={count},setpts=PTS-STARTPTS[v{index}]")
        inputs.append(f"[v{index}]")
        if audio:
            duration = float(count / fps)
            filters.append(f"[{index}:a]apad=whole_dur={duration:.9f},atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a{index}]")
            inputs.append(f"[a{index}]")
    filters.append("".join(inputs) + f"concat=n={len(segments)}:v=1:a={int(audio)}[vc]" + ("[ac]" if audio else ""))
    video_label = "[vc]"
    if settings["subtitles"] == "burned" and captions is not None:
        filters.append("[vc]subtitles=filename=captions.srt[burned]")
        video_label = "[burned]"
    destination = folder / "movie.mp4"
    args += ["-filter_complex", ";".join(filters), "-map", video_label, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", rate,
             "-t", f"{float(sum(frame_counts) / fps):.9f}"]
    args += ["-map", "[ac]", "-c:a", "aac", "-ar", "48000", "-ac", "2"] if audio else ["-an"]
    run_ffmpeg([*args, destination], folder)
    from fsf.conformance.media import SafeFfprobeRunner
    raw = SafeFfprobeRunner().probe(destination)
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    if int(video.get("nb_frames", -1)) != sum(frame_counts):
        raise PostError("POST_FRAME_COUNT")
    return destination, captions
