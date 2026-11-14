"""Bounded, observation-only reference video analysis."""
from fractions import Fraction
import json
from pathlib import Path
import statistics
import tempfile
import time
import sys

from fsf.conformance.media import ensure_media_tools_on_path
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.production.contracts import ProductionError, seal
from fsf.production.control import file_identity
from fsf.production import reference_events

MAX_FRAMES = 10000
SIDE = 32
STRIDE = SIDE * SIDE * 3


def _run(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProductionError("REFERENCE_DEADLINE")
    result = run_safe_subprocess(args, timeout_seconds=remaining, output_limit_bytes=8*1024**2)
    if result.timed_out:
        raise ProductionError("REFERENCE_DEADLINE")
    if not result.ok:
        raise ProductionError("REFERENCE_DECODE")
    return result.raw_stdout or result.stdout


def _timeline(frames, stream):
    ticks = [frame.get("pts") for frame in frames]
    if any(type(value) is not int for value in ticks) or any(a >= b for a,b in zip(ticks,ticks[1:])):
        raise ProductionError("REFERENCE_PTS_UNAVAILABLE")
    timebase = Fraction(stream["time_base"])
    clock = Fraction(stream.get("r_frame_rate","0/1"))
    exact_cfr = len(ticks)>1 and clock>0 and all(
        abs((tick-ticks[0])*timebase-index/clock)<=timebase/2 for index,tick in enumerate(ticks))
    durations = [frame.get("duration",frame.get("pkt_duration")) for frame in frames]
    terminal = durations[-1]
    return {"pts":ticks,"time_base":[timebase.numerator,timebase.denominator],
            "timestamp_kind":"cfr" if exact_cfr else "vfr" if len(ticks)>1 else "unknown",
            "clock":[clock.numerator,clock.denominator] if exact_cfr else None,
            "end_pts":ticks[-1]+terminal if type(terminal) is int and terminal>0 else None,
            "end_basis":"declared_frame_duration" if type(terminal) is int and terminal>0 else "unknown",
            "frame_durations":durations,
            "display_intervals_pts":[[left,right] for left,right in zip(ticks,ticks[1:])]+
                [[ticks[-1],ticks[-1]+terminal if type(terminal) is int and terminal>0 else None]]}


def _changes(stream):
    prior = stream.read(STRIDE)
    cuts = []
    fades = []
    gradual = []
    def finish():
        if len(gradual)>=6 and sum(value for _,value in gradual)>=32:
            fades.append([gradual[0][0]-1, gradual[-1][0]])
        gradual.clear()
    index = 1
    while frame := stream.read(STRIDE):
        if len(frame)!=STRIDE:
            raise ProductionError("REFERENCE_DECODE")
        differences = [abs(a-b) for a,b in zip(frame,prior)]
        coverage = sum(max(differences[i:i+3])>=2 for i in range(0,STRIDE,3))/(SIDE*SIDE)
        magnitude = statistics.fmean(differences)
        if coverage>=.75 and magnitude>=20:
            cuts.append(index)
            finish()
        elif coverage>=.75 and magnitude>=.75:
            gradual.append((index,magnitude))
        else:
            finish()
        prior = frame
        index += 1
    finish()
    return cuts, fades, index


def analyze(source, scratch, *, seconds=120, audio_source=None, classifier=None, detector=None, contacts=None):
    """Analyze a finite source and bind observations to its original bytes."""
    source = Path(source).absolute()
    identity = file_identity(source)
    audio_identity = file_identity(audio_source) if audio_source is not None else None
    ffmpeg,ffprobe = ensure_media_tools_on_path()
    if not ffmpeg or not ffprobe:
        raise ProductionError("REFERENCE_TOOLS")
    deadline = time.monotonic()+seconds
    detector_lock = None
    if detector is not None:
        detector=Path(detector).absolute()
        detector_lock=file_identity(detector)
    common = ["-v","error","-protocol_whitelist","file",
              "-format_whitelist","mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,nut,avi,mpegts,mpeg"]
    # A first-video stream is explicit. Metadata for audio/subtitles is
    # inventoried separately and is never interpreted as instructions.
    probe = json.loads(_run([ffprobe,*common,"-select_streams","v:0",
        "-read_intervals",f"%+#{MAX_FRAMES+1}","-show_frames","-show_streams",
        "-show_entries","frame=pts,pkt_duration,duration:stream=time_base,r_frame_rate,width,height",
        "-of","json",str(source)],deadline))
    frames = probe.get("frames",[])
    if not frames or len(frames)>MAX_FRAMES:
        raise ProductionError("REFERENCE_FRAME_LIMIT")
    video = probe["streams"][0]
    timeline = _timeline(frames,video)
    streams=json.loads(_run([ffprobe,*common,"-show_entries",
        "stream=index,codec_type,codec_name,channels,sample_rate","-of","json",str(source)],deadline)).get("streams",[])
    layer_events=None
    audio=None
    with tempfile.TemporaryDirectory(prefix=".reference-",dir=scratch) as directory:
        pixels = Path(directory)/"pixels.rgb"
        _run([ffmpeg,"-nostdin",*common,"-i",str(source),"-map","0:v:0",
              "-vf",f"scale={SIDE}:{SIDE}:flags=area,format=rgb24","-fps_mode","passthrough",
              "-threads","1","-frames:v",str(MAX_FRAMES+1),"-f","rawvideo",str(pixels)],deadline)
        with pixels.open("rb") as stream:
            cuts,fades,count = _changes(stream)
        if count!=len(frames) or count>MAX_FRAMES:
            raise ProductionError("REFERENCE_FRAME_MAPPING")
        if detector is not None:
            raw=_run([sys.executable,str(detector),str(source),identity["sha256"]],deadline)
            cuts,fades=reference_events.transitions(raw,identity,count)
            if file_identity(detector)!=detector_lock:
                raise ProductionError("REFERENCE_SOURCE_CHANGED")
        if max(video["width"],video["height"]) <= 128:
            exact=Path(directory)/"exact.rgb"
            _run([ffmpeg,"-nostdin",*common,"-i",str(source),"-map","0:v:0",
                  "-fps_mode","passthrough","-threads","1","-frames:v",str(count),
                  "-pix_fmt","rgb24","-f","rawvideo",str(exact)],deadline)
            layer_events=reference_events.layers(exact,video["width"],video["height"],count)
        if audio_source is not None:
            audio=reference_events.audio_events(audio_source,audio_identity)
            audio["clock_origin"]="explicit_sidecar_zero"
        elif any(stream["codec_type"]=="audio" for stream in streams):
            pcm=Path(directory)/"audio.wav"
            _run([ffmpeg,"-nostdin",*common,"-i",str(source),"-map","0:a:0",
                  "-t","101","-ac","1","-ar","48000","-c:a","pcm_s16le",str(pcm)],deadline)
            audio=reference_events.audio_events(pcm)
            audio["clock_origin"]="decoded_audio_start"
        contact = _contact_sheet(pixels,[0,*cuts,count]) if contacts is not None else None
    if file_identity(source)!=identity:
        raise ProductionError("REFERENCE_SOURCE_CHANGED")
    lock={"detector":"rgb-pair-v1","sample_size":[SIDE,SIDE],
          "ffmpeg":file_identity(ffmpeg),"ffprobe":file_identity(ffprobe),
          "audio":"pcm-240-window-v1","layers":"static-insert-bitmap-v1",
          "builtin":file_identity(__file__),"events":file_identity(reference_events.__file__)}
    if detector_lock is not None:
        lock.update(detector="operator-transition-v1",operator_detector=detector_lock)
    hypothesis={"status":"unknown","reason":"source_classification_unavailable"}
    if classifier is not None:
        classifier=Path(classifier).absolute()
        lock["classifier"]=file_identity(classifier)
        result=_run([sys.executable,str(classifier),str(source),identity["sha256"]],deadline)
        hypothesis=reference_events.classification(result,identity)
        if file_identity(classifier)!=lock["classifier"] or file_identity(source)!=identity:
            raise ProductionError("REFERENCE_SOURCE_CHANGED")
    if contact is not None:
        sheet,frame_indices=contact
        import io
        buffer=io.BytesIO()
        sheet.save(buffer,format="PNG")
        with Path(contacts).open("xb") as stream:
            stream.write(buffer.getvalue())
        contact={"frames":frame_indices,"artifact":file_identity(contacts)}
    extracted=layer_events or []
    return seal({"schema_version":"reference_analysis_v1","source":identity,
        "timeline":timeline,"shots":[{"range":[start,end],"kind":"observation"}
            for start,end in zip([0,*cuts],[*cuts,count])],
        "events":[{"type":"hard_cut","frame":n,"kind":"observation"} for n in cuts]+
                 [{"type":"gradual_transition","range":span,"kind":"observation"} for span in fades],
        "detector_lock":lock,"streams":streams,"entity_references":[],
        "audio":audio if audio is not None else {"status":"unknown","reason":"audio_source_unavailable"},
        "layers":{"status":"observed" if extracted else "unknown","events":extracted},
        "text":{"status":"observed" if any(event["text"] for event in extracted) else "not_available",
                "scope":"exact_default_bitmap","events":[event for event in extracted if event["text"]]},
        "classification":hypothesis,"contact_sheet":contact,
        "provenance":{"status":"unknown","reason":"source_classification_unavailable"},
        "confidence":None},"analysis_digest")


def _contact_sheet(pixels, boundaries):
    from PIL import Image, ImageDraw
    indices=sorted({index for left,right in zip(boundaries,boundaries[1:])
                    for index in (left, (left+right-1)//2, right-1)})
    if len(indices)>300:
        raise ProductionError("REFERENCE_CONTACT_LIMIT")
    sheet=Image.new("RGB",(6*96,((len(indices)+5)//6)*80))
    draw=ImageDraw.Draw(sheet)
    with pixels.open("rb") as stream:
        for slot,index in enumerate(indices):
            stream.seek(index*STRIDE)
            image=Image.frombytes("RGB",(SIDE,SIDE),stream.read(STRIDE))
            x,y=slot%6*96,slot//6*80
            sheet.paste(image.resize((96,64)),(x,y))
            draw.text((x+2,y+64),str(index),fill="white")
    return sheet,indices
