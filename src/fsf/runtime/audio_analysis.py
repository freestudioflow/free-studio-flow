"""Measurement-only audio reports; never claim normalization or human approval."""
import copy
import os
from pathlib import Path
import uuid
from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.core.audio_edit import AudioError, enabled
from fsf.runtime.audio_mix import ACTIVE_DEADLINE, Deadline, resolve_sources, decode, pcm_process, measure, _concat
from fsf.runtime.post_runtime import PostContext, directory, write_json


def analyze_plan(plan, configurations, audio_resources, out):
    context = PostContext(plan,configurations,audio_resources)
    if not enabled(context.edit):
        raise AudioError()
    videos = context.check()
    sources = resolve_sources(context.edit,videos,audio_resources)
    base = directory(out)
    folder = directory(base/(".analysis-"+uuid.uuid4().hex))
    mix = context.edit["audio_mix"]
    token = ACTIVE_DEADLINE.set(Deadline())
    try:
        decoded, events, results = {}, {}, []
        for i,(identifier,source) in enumerate(sources.items()):
            path = folder/f"source_{i}.f32"
            samples = decode(source["path"],source["binding"]["stream_index"],path)
            if source["binding"].get("canonical_samples",samples) != samples:
                raise AudioError("AUDIO_SOURCE_INVALID")
            decoded[identifier] = (path,samples)
        for i,event in enumerate(mix["events"]):
            path,samples = decoded[event["audio_source_id"]]
            if event["source_out_sample"] > samples:
                raise AudioError("AUDIO_SOURCE_INVALID")
            events[event["event_id"]] = pcm_process(path,folder/f"event_{i}.f32",f"atrim=start_sample={event['source_in_sample']}:end_sample={event['source_out_sample']}")
        for i,group in enumerate(mix["settings"]["normalization"]["groups"]):
            selected = [e["event_id"] for e in mix["events"] if e["event_id"] in group["event_ids"]]
            output = folder/f"group_{i}.f32"
            _concat([events[eid] for eid in selected],output)
            measurements = measure(output,folder,pcm=True)
            samples = output.stat().st_size//8
            reason = "AUDIO_TOO_SHORT" if samples < 480000 else "AUDIO_SILENT" if measurements["integrated_lufs"] is None else None
            results.append({"group_id":group["group_id"],"scope":"event_sequence","ordered_event_ids":selected,"is_timeline_loudness":False,
                "status":"unmeasurable" if reason else "measured","reason":reason,"sample_count":samples,"measurements":measurements,
                "normalization_verified":False})
        # Without groups report individual trimmed inputs, never label them timeline LUFS.
        if not results:
            for identifier,path in events.items():
                measurements = measure(path,folder,pcm=True)
                samples = path.stat().st_size//8
                reason = "AUDIO_TOO_SHORT" if samples < 480000 else "AUDIO_SILENT" if measurements["integrated_lufs"] is None else None
                results.append({"event_id":identifier,"scope":"event_input","is_timeline_loudness":False,"status":"unmeasurable" if reason else "measured",
                    "reason":reason,"measurements":measurements,"sample_count":samples,"normalization_verified":False})
        context.check()
        report = {"schema_version":"fsf_audio_analysis_v1","edit_id":context.edit["edit_id"],"revision":context.edit["revision"],
            "edit_digest":context.edit["edit_digest"],"results":results,"listening_review":"not_reviewed"}
        report["report_digest"] = digest(report)
        write_json(folder/"analysis.json",report)
        destination = base/("analysis_"+uuid.uuid4().hex)
        os.rename(folder,destination)
        return {"directory":destination.name,"report":report}
    finally:
        ACTIVE_DEADLINE.reset(token)
