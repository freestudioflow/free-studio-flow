"""Operator audio imports and version-bound listening previews."""
import copy
import os
from pathlib import Path
import tempfile
import uuid

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.audio_edit import AudioError, ab_projection, check_ab, enabled, frame_sample
from fsf.core.editing import _shape, inspect_edit, validate_edit
from fsf.runtime.audio_mix import ACTIVE_DEADLINE, Deadline, decode, resource_map, resolve_sources, render_audio, measure, pcm_input
from fsf.runtime.post_sources import safe_file, file_sha


def import_audio(edit, request, config):
    validate_edit(edit)
    _shape(request,"fsf_audio_import_request_v1")
    if (request["edit_id"],request["base_revision"],request["base_digest"]) != (edit["edit_id"],edit["revision"],edit["edit_digest"]):
        raise AudioError("AUDIO_STALE")
    resources = resource_map(config)
    ids = [s["audio_source_id"] for s in request["selections"]]
    if len(ids) != len(set(ids)):
        raise AudioError()
    existing = edit.get("audio_mix",{})
    if set(ids) & ({s["audio_source_id"] for s in existing.get("sources",[])} | set(existing.get("retired_ids",{}).get("source",[]))):
        raise AudioError("AUDIO_ID_RETIRED")
    result = []
    token = ACTIVE_DEADLINE.set(Deadline())
    try:
        with tempfile.TemporaryDirectory(prefix="fsf-audio-") as temp:
            for i,selection in enumerate(request["selections"]):
                try:
                    path = safe_file(resources[selection["resource_id"]],selection["relative_path"])
                    from fsf.conformance.media import SafeFfprobeRunner
                    raw = SafeFfprobeRunner(timeout_seconds=ACTIVE_DEADLINE.get().remaining()).probe(path)
                    stream = next(s for s in raw["streams"] if s.get("index") == selection["stream_index"])
                    if stream.get("codec_type") != "audio" or stream.get("channels") not in (1,2):
                        raise AudioError("AUDIO_SOURCE_INVALID")
                    sha = file_sha(path)
                    size = path.stat().st_size
                    samples = decode(path,selection["stream_index"],Path(temp)/f"{i}.f32")
                    if file_sha(path) != sha or path.stat().st_size != size:
                        raise AudioError("AUDIO_SOURCE_INVALID")
                    result.append({"audio_source_id":selection["audio_source_id"],"binding":{
                        "kind":"operator_audio_import","resource_id":selection["resource_id"],"relative_path":selection["relative_path"],
                        "sha256":sha,"size_bytes":size,"stream_index":selection["stream_index"],"canonical_samples":samples,"decoder_policy":"pcm48_stereo_v1"}})
                except AudioError:
                    raise
                except Exception:
                    raise AudioError("AUDIO_SOURCE_INVALID") from None
    finally:
        ACTIVE_DEADLINE.reset(token)
    return {"schema_version":"fsf_audio_import_declarations_v1","edit_id":edit["edit_id"],"base_revision":edit["revision"],"base_digest":edit["edit_digest"],
        "sources":result,"authority":"operator_import_integrity_and_decodability_only"}


def inspect_audio(edit):
    from fsf.core.audio_edit import event_layout
    validate_edit(edit)
    return {"edit_id":edit["edit_id"],"revision":edit["revision"],"edit_digest":edit["edit_digest"],
        "audio_tracks":copy.deepcopy(edit["audio_tracks"]),"clock_rate":copy.deepcopy(edit["clock_rate"]),
        "clips":inspect_edit(edit)["clips"],"audio_mix":copy.deepcopy(edit.get("audio_mix")),"event_positions":event_layout(edit) if "audio_mix" in edit else {},
        "ab_projection_digest":digest(ab_projection(edit)) if enabled(edit) else None}


def audition(before,after,request,configurations,audio_resources,out):
    from fsf.runtime.post_runtime import PostContext, directory, write_json
    from fsf.runtime.post_plan import compile_post_plan
    from fsf.runtime.post_render import render, run_ffmpeg
    validate_edit(before); validate_edit(after)
    _shape(request,"fsf_audio_audition_request_v1")
    check_ab(before,after)
    if (request["edit_id"],request["before_revision"],request["before_digest"],request["after_revision"],request["after_digest"]) != (before["edit_id"],before["revision"],before["edit_digest"],after["revision"],after["edit_digest"]):
        raise AudioError("AUDIO_STALE")
    projection = digest(ab_projection(before))
    if request["projection_digest"] != projection:
        raise AudioError("AUDIO_AB_MISMATCH")
    clip = next((c for c in inspect_edit(before)["clips"] if c["clip_id"] == request["clip_id"]),None)
    if clip is None or not 0 <= request["start_frame"] < request["end_frame"] <= clip["out_frame"]-clip["in_frame"]:
        raise AudioError()
    solo = request["track_id"] if request["listen_scope"] == "track_solo" else None
    if (request["listen_scope"] == "mix" and request["track_id"] is not None or solo is not None and solo not in {t["track_id"] for t in before["audio_tracks"]}):
        raise AudioError()
    base = directory(out)
    folder = directory(base / (".audition-"+uuid.uuid4().hex))
    start = frame_sample(clip["timeline_start_frame"]+request["start_frame"],before["clock_rate"])
    end = frame_sample(clip["timeline_start_frame"]+request["end_frame"],before["clock_rate"])
    reports = []
    for name,edit in (("A",before),("B",after)):
        context = PostContext(compile_post_plan(edit,width=320,height=180),configurations,audio_resources)
        videos = context.check()
        part = directory(folder/name)
        token = ACTIVE_DEADLINE.set(Deadline())
        try:
            video,_ = render(edit,{**context.context,"audio":"strip"},videos,part)
            audio_sources = resolve_sources(edit,videos,audio_resources)
            output,report = render_audio(edit,audio_sources,video,part/"processing",solo_track=solo)
            run_ffmpeg(["-i",output,"-map","0:a:0","-af",f"atrim=start_sample={start}:end_sample={end},asetpts=PTS-STARTPTS","-c:a","pcm_s16le",folder/f"{name}.wav"],folder)
            context.check()
            reports.append({"side":name,"revision":edit["revision"],"edit_digest":edit["edit_digest"],"analysis":report,
                "wav_sha256":file_sha(folder/f"{name}.wav")})
        finally:
            ACTIVE_DEADLINE.reset(token)
    manifest = {"schema_version":"fsf_audio_audition_v1","kind":"preview","projection_digest":projection,"request":copy.deepcopy(request),
        "start_sample":start,"end_sample":end,"sides":reports,"listening_review":"not_reviewed",
        "input_projection":ab_projection(before),"clock_rate":copy.deepcopy(before["clock_rate"]),"unity_playback_intent":True}
    manifest["manifest_digest"] = digest(manifest)
    write_json(folder/"audition.json",manifest)
    destination = base/("audition_"+uuid.uuid4().hex)
    os.rename(folder,destination)
    return {"directory":destination.name,"manifest":manifest}
