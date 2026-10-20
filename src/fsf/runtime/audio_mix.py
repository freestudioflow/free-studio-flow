"""Bounded sample-accurate audio processing; numeric QC is not human review."""
import copy
import json
import math
import os
import time
from contextvars import ContextVar
from pathlib import Path

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.audio_edit import AudioError, event_layout, frame_sample
from fsf.core.editing import _shape
from fsf.runtime.post_sources import file_sha, safe_file

ACTIVE_DEADLINE = ContextVar("audio_deadline", default=None)


class Deadline:
    def __init__(self, seconds=300):
        self.end = time.monotonic() + seconds

    def remaining(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise AudioError("AUDIO_DEADLINE_EXCEEDED")
        return remaining


def _run(args, folder):
    from fsf.runtime.post_render import run_ffmpeg
    return run_ffmpeg(args, folder)


def resource_map(config):
    if config is None:
        return {}
    try:
        _shape(config, "fsf_audio_resources_v1")
        result = {r["resource_id"]:Path(r["root"]) for r in config["resources"]}
        if len(result) != len(config["resources"]):
            raise AudioError("AUDIO_SOURCE_INVALID")
        return result
    except Exception:
        raise AudioError("AUDIO_SOURCE_INVALID") from None


def resolve_sources(edit, verified_videos, config):
    resources = resource_map(config)
    resolved = {}
    for item in edit["audio_mix"]["sources"]:
        binding = item["binding"]
        try:
            if binding["kind"] == "clip_original":
                video = verified_videos[binding["video_source_id"]]
                path = video["path"]
                sha = binding["video_sha256"]
                raw = video["raw"]
            else:
                path = safe_file(resources[binding["resource_id"]], binding["relative_path"])
                sha = binding["sha256"]
                if path.stat().st_size != binding["size_bytes"]:
                    raise AudioError("AUDIO_SOURCE_INVALID")
                from fsf.conformance.media import SafeFfprobeRunner
                raw = SafeFfprobeRunner(timeout_seconds=ACTIVE_DEADLINE.get().remaining() if ACTIVE_DEADLINE.get() else 30).probe(path)
            if file_sha(path) != sha:
                raise AudioError("AUDIO_SOURCE_INVALID")
            stream = next(s for s in raw["streams"] if s.get("index") == binding["stream_index"])
            if stream.get("codec_type") != "audio" or stream.get("channels") not in (1,2):
                raise AudioError("AUDIO_SOURCE_INVALID")
            resolved[item["audio_source_id"]] = {"path":Path(path),"sha256":sha,"binding":copy.deepcopy(binding)}
        except AudioError:
            raise
        except Exception:
            raise AudioError("AUDIO_SOURCE_INVALID") from None
    return resolved


def decode(path, stream, destination):
    _run(["-v","error","-i",path,"-map",f"0:{stream}","-vn","-ar","48000","-ac","2","-c:a","pcm_f32le","-f","f32le",destination], destination.parent)
    size = destination.stat().st_size
    if size == 0 or size % 8:
        raise AudioError("AUDIO_SOURCE_INVALID")
    return size // 8


def pcm_input(path):
    return ["-f","f32le","-ar","48000","-ac","2","-i",path]


def pcm_process(path, destination, filters):
    _run([*pcm_input(path),"-af",filters,"-c:a","pcm_f32le","-f","f32le",destination], destination.parent)
    return destination


def measure(path, folder, *, pcm=False):
    arguments = pcm_input(path) if pcm else ["-i",path]
    result = _run([*arguments,"-map","0:a:0","-af","loudnorm=I=-16:TP=-1:LRA=11:print_format=json","-f","null","-"],folder)
    try:
        text = result.stderr
        start = text.rfind('{')
        data = json.JSONDecoder().raw_decode(text[start:])[0]
        def number(key):
            value = float(data[key])
            if math.isnan(value) or value == math.inf:
                raise ValueError()
            return value if math.isfinite(value) else None
        return {"integrated_lufs":number("input_i"),"true_peak_dbtp":number("input_tp"),"lra_lu":number("input_lra")}
    except Exception:
        raise AudioError("AUDIO_ANALYSIS_FAILED") from None


def _concat(paths, destination):
    args = [value for p in paths for value in pcm_input(p)]
    labels = ''.join(f'[{i}:a]' for i in range(len(paths)))
    _run([*args,"-filter_complex",f"{labels}concat=n={len(paths)}:v=0:a=1[a]","-map","[a]","-c:a","pcm_f32le","-f","f32le",destination],destination.parent)


def normalization_gain(path, settings, folder, label, *, cache=None, identity=None):
    count = path.stat().st_size // 8
    key = digest({"input":identity,"pcm_sha256":file_sha(path),"measurement":"ffmpeg_loudnorm_input_v1"})
    measured = None
    hit = False
    if cache is not None:
        cache = Path(cache); cache.mkdir(parents=True,exist_ok=True)
        entry = cache/(key+".json")
        try:
            from fsf.core.edit_store import read_json
            cached = read_json(safe_file(cache,entry.name))
            if cached["key"] == key and cached["digest"] == digest({k:v for k,v in cached.items() if k != "digest"}):
                candidate = cached["measurement"]
                if set(candidate) == {"integrated_lufs","true_peak_dbtp","lra_lu"} and all(v is None or type(v) in (int,float) and math.isfinite(v) for v in candidate.values()):
                    measured, hit = candidate, True
        except Exception:
            pass
    if measured is None:
        measured = measure(path,folder,pcm=True)
        if cache is not None:
            from fsf.runtime.post_runtime import write_json
            cached = {"key":key,"measurement":measured};cached["digest"]=digest(cached)
            write_json(entry,cached)
    report = {"scope":label,"sample_count":count,"input":measured,"normalization_verified":False,"measurement_digest":digest(measured),"input_digest":key,"analysis_cache_hit":hit}
    if count < settings["minimum_seconds"] * 48000:
        raise AudioError("AUDIO_TOO_SHORT")
    if measured["integrated_lufs"] is None:
        raise AudioError("AUDIO_SILENT")
    gain = settings["target_lufs"] - measured["integrated_lufs"]
    if gain > settings["max_gain_db"]:
        raise AudioError("AUDIO_TARGET_UNREACHABLE")
    changed = pcm_process(path, folder / (label + "-normalized.f32"), f"volume={gain:.9f}dB")
    after = measure(changed, folder, pcm=True)
    if after["integrated_lufs"] is None or abs(after["integrated_lufs"] - settings["target_lufs"]) > settings["tolerance_lu"]:
        raise AudioError("AUDIO_TARGET_UNREACHABLE")
    report.update({"gain_db":gain,"after":after,"normalization_verified":True,"solution_digest":digest({"input_digest":key,"measurement":measured,"policy":settings,"gain_db":gain})})
    return gain, report


def _manual_filter(event, track, normal_gain):
    length = event["source_out_sample"] - event["source_in_sample"]
    gain = 0 if event["mute"] or track["mute"] else 10 ** ((event["gain_db"] + track["gain_db"] + normal_gain) / 20)
    factors = [f"{gain:.12g}"]
    for r in event["regions"]:
        start, end = r["start_sample"], r["end_sample"]
        ramp = min(240,(end-start)//2)
        multiplier = 0 if r["mute"] else 10 ** (r["gain_db"]/20)
        envelope = f"max(0,min(1,min((n-{start})/{ramp},({end}-n)/{ramp})))"
        factors.append(f"(1+({multiplier:.12g}-1)*{envelope})")
    expression = '*'.join(factors)
    ramp = min(240,length//2)
    return f"aeval=exprs='val(0)*({expression})|val(1)*({expression})',afade=t=in:ss=0:ns={ramp},afade=t=out:ss={length-ramp}:ns={ramp}"


def mix_pcm(edit, sources, folder, *, solo_track=None, cache=None):
    mix = edit["audio_mix"]
    folder.mkdir(parents=True,exist_ok=True)
    decoded, raw_events = {}, {}
    for index,(identifier, source) in enumerate(sources.items()):
        output = folder / f"decode_{index}.f32"
        samples = decode(source["path"],source.get("decode_stream_index", source["binding"]["stream_index"]),output)
        expected = source["binding"].get("canonical_samples")
        if expected is not None and samples != expected:
            raise AudioError("AUDIO_SOURCE_INVALID")
        if file_sha(source["path"]) != source["sha256"]:
            raise AudioError("AUDIO_SOURCE_INVALID")
        decoded[identifier] = (output,samples)
    for index,event in enumerate(mix["events"]):
        raw, samples = decoded[event["audio_source_id"]]
        if event["source_out_sample"] > samples:
            raise AudioError("AUDIO_SOURCE_INVALID")
        raw_events[event["event_id"]] = pcm_process(raw,folder/f"event_{index}.f32",f"atrim=start_sample={event['source_in_sample']}:end_sample={event['source_out_sample']},asetpts=PTS-STARTPTS")
    norm = mix["settings"]["normalization"]
    gains, reports = {}, []
    for index,group in enumerate(norm["groups"]):
        # Membership selector order never overrides event declaration order.
        selected = [e["event_id"] for e in mix["events"] if e["event_id"] in group["event_ids"]]
        path = folder / f"group_{index}.f32"
        _concat([raw_events[eid] for eid in selected],path)
        from fsf.runtime.post_render import tool_version
        identity = {"tool":tool_version(),"decoder":"pcm48_stereo_v1","events":[{
            "event_id":e["event_id"],"binding":sources[e["audio_source_id"]]["binding"],
            "source_in_sample":e["source_in_sample"],"source_out_sample":e["source_out_sample"]} for e in mix["events"] if e["event_id"] in selected]}
        gain, report = normalization_gain(path,norm,folder,f"group_{index}",cache=cache,identity=identity)
        gains.update({eid:gain for eid in selected})
        report.update({"group_id":group["group_id"],"ordered_event_ids":selected,"is_timeline_loudness":False,"scope":"event_sequence","inputs":identity["events"]})
        reports.append(report)
    tracks = {t["track_id"]:t for t in mix["track_settings"]}
    layout = event_layout(edit)
    total = frame_sample(sum(c["out_frame"]-c["in_frame"] for c in edit["clips"]),edit["clock_rate"])
    adjusted, selected_tracks = [], []
    for index,event in enumerate(mix["events"]):
        filters = _manual_filter(event,tracks[event["track_id"]],gains.get(event["event_id"],0))
        filters += f",adelay={layout[event['event_id']]['start_sample']}S:all=1"
        adjusted.append(pcm_process(raw_events[event["event_id"]],folder/f"adjusted_{index}.f32",filters))
        selected_tracks.append(event["track_id"])
    mixed = folder/"mixed.f32"
    if adjusted:
        args = [a for p in adjusted for a in pcm_input(p)]
        labels = ''.join(f'[{i}:a]' for i in range(len(adjusted)))
        filters = f"{labels}amix=inputs={len(adjusted)}:duration=longest:normalize=0,apad,atrim=end_sample={total}[a]"
        _run([*args,"-filter_complex",filters,"-map","[a]","-c:a","pcm_f32le","-f","f32le",mixed],folder)
    else:
        _run(["-f","lavfi","-i","anullsrc=r=48000:cl=stereo","-af",f"atrim=end_sample={total}","-c:a","pcm_f32le","-f","f32le",mixed],folder)
    if mixed.stat().st_size != total*8:
        raise AudioError("AUDIO_ANALYSIS_FAILED")
    master_normal_gain = 0
    if norm["mode"] == "master":
        master_normal_gain, report = normalization_gain(mixed,norm,folder,"master")
        reports.append(report)
    if solo_track is not None:
        selected = [p for p,t in zip(adjusted,selected_tracks) if t == solo_track]
        solo = folder/"solo.f32"
        if selected:
            args = [a for p in selected for a in pcm_input(p)]
            labels = ''.join(f'[{i}:a]' for i in range(len(selected)))
            _run([*args,"-filter_complex",f"{labels}amix=inputs={len(selected)}:normalize=0:duration=longest,apad,atrim=end_sample={total}[a]",
                "-map","[a]","-c:a","pcm_f32le","-f","f32le",solo],folder)
        else:
            _run(["-f","lavfi","-i","anullsrc=r=48000:cl=stereo","-af",f"atrim=end_sample={total}","-c:a","pcm_f32le","-f","f32le",solo],folder)
        mixed = solo
    gain = master_normal_gain + mix["settings"]["master_gain_db"]
    manual = pcm_process(mixed,folder/"master.f32",f"volume={0 if mix['settings']['master_mute'] else 10**(gain/20):.12g}")
    return manual, {"audio_track_identities":copy.deepcopy(edit["audio_tracks"]),"effective_audio_mix":copy.deepcopy(mix),"groups":reports,"samples":total,"settings_digest":digest(mix["settings"]),
        "source_digest":digest([s["binding"] for s in sources.values()]),"processing_digest":digest({"audio_mix":mix,"clock":edit["clock_rate"],"clips":edit["clips"]}),"manual_master_gain_db":mix["settings"]["master_gain_db"],
        "listening_review":"not_reviewed","normalization_mode":norm["mode"],"listen_scope":"mix" if solo_track is None else "track_solo"}


def _digital_silence(path):
    """Require actual decoded zero samples, not absence of a meter result."""
    import array
    count = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if len(chunk) % 4:
                return False
            values = array.array("f")
            values.frombytes(chunk)
            if any(value != 0.0 for value in values):
                return False
            count += len(values)
    return count > 0


def numeric_report_valid(report):
    """Check metric status explicitly, including older finite-peak reports."""
    try:
        settings = report["effective_audio_mix"]["settings"]
        mode = settings["normalization"]["mode"]
        if mode not in {"none", "dialogue_groups", "master"} or report.get("normalization_mode") != mode:
            return False
    except (KeyError, TypeError):
        return False
    final = report.get("final", {})
    peak = final.get("true_peak_dbtp")
    if type(peak) in (int, float) and math.isfinite(peak):
        ceiling = settings["peak_ceiling_dbtp"]
        return (report.get("numeric_quality") == "passed" and peak <= ceiling
                and final.get("peak_status", "measured") == "measured")
    return (peak is None and final.get("integrated_lufs") is None
            and report.get("numeric_quality") == "unmeasurable"
            and final.get("peak_status") == "verified_digital_silence"
            and report.get("digital_silence_verified") is True
            and type(report.get("decoded_samples")) is int and report["decoded_samples"] > 0
            and isinstance(report.get("decoded_pcm_sha256"), str) and len(report["decoded_pcm_sha256"]) == 64
            and (report.get("normalization_mode") != "master" or report.get("listen_scope") == "track_solo"))


def encode_verified(edit, pcm, video, folder, report):
    settings = edit["audio_mix"]["settings"]
    norm = settings["normalization"]
    attempts = []
    for attempt in range(settings["peak_extra_attempts"]+1):
        headroom = settings["encoding_headroom_db"] + attempt * 0.5
        ceiling = 10 ** ((settings["peak_ceiling_dbtp"]-headroom)/20)
        output = folder/f"encoded_{attempt}.mp4"
        filters = f"aresample=192000,alimiter=limit={ceiling:.12g}:level=false:latency=true,aresample=48000"
        _run(["-i",video,*pcm_input(pcm),"-map","0:v:0","-map","1:a:0","-c:v","copy","-af",filters,"-c:a","aac","-b:a","192k","-ar","48000","-ac","2",output],folder)
        final = measure(output,folder)
        attempts.append({"attempt":attempt+1,"encoding_headroom_db":headroom,**final})
        silence = False
        decoded_path = folder/f"final_decoded_{attempt}.f32"
        decoded_samples = None
        if final["true_peak_dbtp"] is None:
            if final["integrated_lufs"] is not None:
                raise AudioError("AUDIO_ANALYSIS_FAILED")
            decoded_samples = decode(output,1,decoded_path)
            if not _digital_silence(decoded_path):
                raise AudioError("AUDIO_ANALYSIS_FAILED")
            silence = True
        peak_ok = silence or final["true_peak_dbtp"] <= settings["peak_ceiling_dbtp"]
        if peak_ok:
            if norm["mode"] == "master" and report.get("listen_scope") != "track_solo":
                intended = norm["target_lufs"] + settings["master_gain_db"]
                if final["integrated_lufs"] is None or abs(final["integrated_lufs"]-intended) > norm["tolerance_lu"]:
                    raise AudioError("AUDIO_TARGET_UNREACHABLE")
            if decoded_samples is None:
                decoded_samples = decode(output,1,decoded_path)
            expected_samples = pcm.stat().st_size // 8
            if not 0 <= decoded_samples - expected_samples <= 1024:
                raise AudioError("AUDIO_TARGET_UNREACHABLE")
            from fsf.runtime.post_render import tool_version
            report.update({"tool_version":tool_version(),"canonical_samples":expected_samples,"decoded_samples":decoded_samples,
                "codec_padding_samples":decoded_samples-expected_samples,"attempts":attempts,
                "decoded_pcm_sha256":file_sha(decoded_path),"digital_silence_verified":silence,
                "final":{**final,"peak_status":"verified_digital_silence" if silence else "measured","scope":"final_encoded_timeline","is_timeline_loudness":True},
                "numeric_quality":"unmeasurable" if silence else "passed","artifact_sha256":file_sha(output)})
            report["report_digest"] = digest(report)
            return output, report
    raise AudioError("AUDIO_TARGET_UNREACHABLE")


def render_audio(edit, sources, video, folder, *, solo_track=None, cache=None):
    folder = Path(folder)
    pcm, report = mix_pcm(edit,sources,folder,solo_track=solo_track,cache=cache)
    output, report = encode_verified(edit,pcm,video,folder,report)
    return output, report
