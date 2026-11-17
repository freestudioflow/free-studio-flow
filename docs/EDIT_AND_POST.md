# Edit Metadata and Verified Post-Production

*Introduced in rc7. For simple pipeline `--audio` flags, see [Audio Contract Behaviour](AUDIO.md).*

## Edit metadata commands

`fsf edit` manages a versioned edit description. The metadata commands below
do not read media or establish source QC/review authority. For verified source
import, previews and rendering, use the post-production workflow below.

Prepare `definition.json` using
[`fsf_edit_definition_v1`](../src/fsf/schemas/fsf_edit_definition_v1.schema.json):
set `schema_version` to `fsf_edit_definition_v1` and provide `edit_id`,
`clock_rate`, `sources`, and `clips`. `audio_tracks` and `subtitle_tracks`
are optional. Do not supply `revision`, `parent_digest`, `edit_digest`, or
`retired_clip_ids`; creation computes those fields. The destination directory
must already exist, and creation refuses to overwrite an existing file.

```powershell
fsf edit create definition.json --out edit.json
fsf edit inspect edit.json
fsf edit patch edit.json --patch patch.json
fsf edit diff before.json edit.json
```

Patches follow [`fsf_edit_patch_v1`](../src/fsf/schemas/fsf_edit_patch_v1.schema.json)
and must identify the current edit ID, base revision and base digest. Save a
separate revision snapshot if you need a later `diff`; patch updates the target
file in place. Concurrent cooperating writers cannot both publish from the
same base revision. A stale patch is rejected, not automatically rebased.

To locate a clip, copy the current revision and edit digest from `inspect`:

```text
fsf edit locate edit.json --revision <revision> --digest <edit_digest> --clip-id C01
```

Choose exactly one of `--clip-id`, `--position` (one-based), `--frame`
(zero-based), or `--label`. Quote labels containing spaces, for example
`--label "Officer wide"`. Label matching is case-insensitive substring matching;
multiple matches return candidates and a nonzero exit code, never an automatic
selection. Frame ranges exclude their end frame.

Except for help, commands write one JSON object to stdout. Success returns
`{"ok":true,"result":...}`. Errors return
`{"ok":false,"error":{"code":"EDIT_CONFLICT","message":...}}`.
Exit codes are 0 for success, 2 for usage, 3 for invalid data, 4 for a stale
base, 5 for missing/ambiguous clips, 6 for file IO, 7 for lock timeout,
8 for an existing create target, and 1 for an unexpected internal error.
Use `error.code` to distinguish errors sharing an exit code.

Edit JSON is limited to 8 MiB and nesting depth 64. Duplicate keys and
non-finite numbers are rejected. Persistence supports cooperating FSF writers
on local filesystems; it does not provide distributed locking or a guarantee
of zero data loss on power failure. Source declarations remain declarations,
not proof that a media file is valid or approved.

## Verified post-production

Edit existing real videos without dispatching new generation jobs. Source
imports require an exact production plan, execution journal, job manifest,
QC report and evaluation receipt. Required source reviews must still be
approved. Older executions without the recorded evaluation receipt are
rejected; FSF does not reconstruct missing execution evidence.

For new production plans, `--post-sources` also adds QC after remaster and VFX
when those skills are selected. It requires `--media` and does not itself run
generation. Use configured real-media executors or complete the Handoff workflow;
the default shape executors cannot produce importable video.

```powershell
fsf intake sheet.docx --out production
fsf plan sheet.docx --media --post-sources --out production/plan.json
fsf execute production/plan.json --out production
```

Approve any required source reviews before importing the outputs.

Create an operator-local `sources.json` using
[`fsf_source_executions_v1`](../src/fsf/schemas/fsf_source_executions_v1.schema.json):

```json
{
  "schema_version": "fsf_source_executions_v1",
  "executions": [{
    "execution_id": "production_A",
    "root": "production",
    "job": "job_manifest.json",
    "plan": "plan.json",
    "journal": "fsf_execution_journal.json"
  }]
}
```

`root` is explicitly trusted operator configuration, resolved from the process
working directory. Job, plan, journal and artifact paths must stay beneath it;
symlinks, reparse points and non-regular files are refused. Do not publish this
configuration. The post-production plan cannot supply or override these roots.

Create `selection.json` using
[`fsf_edit_import_v1`](../src/fsf/schemas/fsf_edit_import_v1.schema.json).
Copy `producer_step_id` and `artifact_ref` from the actual production plan;
the following identities illustrate the format:

```json
{
  "schema_version": "fsf_edit_import_v1",
  "edit_id": "trailer",
  "clock_rate": {"num": 24, "den": 1},
  "selections": [{
    "source_execution_id": "production_A",
    "producer_step_id": "step_001_T01_generate",
    "artifact_ref": "artifact:T01.mp4",
    "label": "Opening shot"
  }]
}
```

Import assigns source IDs `S01`, `S02`, etc. and clip IDs `C01`, `C02`, etc.
in selection order. FSF captures and verifies the source digests and available
frame counts; do not manufacture completed journal entries or hand-edit hashes.
The edit destination directory must exist.

```powershell
fsf edit import selection.json --source-executions sources.json --out edit.json
fsf edit inspect edit.json
fsf edit patch edit.json --patch patch.json
fsf edit plan edit.json --out post-plan.json --width 1024 --height 576 --audio keep
fsf execute post-plan.json --source-executions sources.json --out post-output
```

An edit patch binds the current `edit_id`, `base_revision` and `base_digest`.
For example, these operations trim C01 to source frames `[12, 36)` and move
C02 before C01 (the reorder list must include every current clip exactly once):

```json
[
  {"op": "trim", "clip_id": "C01", "in_frame": 12, "out_frame": 36},
  {"op": "reorder", "clip_ids": ["C02", "C01"]}
]
```

Put that list in the patch's `operations` field; it is not a complete patch.
All frame values use the edit clock, including rational rates such as
`{"num":24000,"den":1001}`. A trim must remain inside the verified source range.
Use clip IDs plus revision/digest when discussing changes; labels may be ambiguous.

Subtitle patches support `upsert_subtitle_track`, `remove_subtitle_track`,
`upsert_cue` and `remove_cue`; see
[`fsf_edit_patch_v1`](../src/fsf/schemas/fsf_edit_patch_v1.schema.json).
Each track uses exactly one timebase. Clip-relative cues move with their clip;
absolute cues require explicit `confirm_timeline_anchor` after timing changes.
Removing or splitting clips requires repairing any dangling cues in the same
patch. Plan options `--subtitles sidecar|burned` select one track with
`--subtitle-track <track_id>` when needed. Required subtitle burning failures
block delivery instead of silently switching to sidecar.

To preview a clip, copy the current revision and digest from `inspect`:

```text
fsf edit preview edit.json --source-executions sources.json --out previews --revision <revision> --digest <edit_digest> --clip-id C01 --start-frame 0 --end-frame 12
```

The preview range is clip-relative and excludes its end frame. A stale revision
is rejected. Preview output binds the edit revision, clip, source SHA and range;
it is not a final conformance report or a visual approval.

Post-production uses an independent plan and journal. It rechecks source files,
QC evidence and current required reviews before execution, reuse and delivery.
Changing the pinned source journal requires a fresh import. Source production
files and journals remain unchanged. Real-media sources only are accepted;
shape/mock stubs, audio, subtitles and images cannot substitute for video.

Successful `execute` returns `artifact_directory` relative to `--out`. That
directory contains `movie.mp4`, `qc.json`, `source_manifest.json`, `receipt.json`
and optional `captions.srt`. Files are prepared privately and the complete
directory becomes visible by a same-filesystem rename. Revision/attempt paths
are isolated; `--no-resume` creates another attempt rather than overwriting a
prior movie. Revoking a source review blocks subsequent reuse/delivery but does
not delete historical output.

The receipt binds clip selections, clock, source manifest and delivered files.
`fsf.runtime.post_runtime.verify_post_receipt(folder)` checks local bundle
integrity; it does not re-check live source approvals. Digests are not signatures,
and source approval is not final-film visual approval. Concurrent hostile
filesystem mutation is outside the integrity guarantee.

`generation_dispatches` reports process/API/new-Handoff dispatches separately
from `post_transform_dispatches` and `media_process_counts.ffmpeg_calls`.
The latter counts post-renderer FFmpeg calls, including its version query;
Media QC's own probe/filter processes are separate. Resume still rechecks
sources and tool version even when all four post steps are reused.

Without `audio_mix`, audio options retain, remove or require the source audio,
padding missing audio with silence when needed for concatenation. An explicitly
disabled mix uses this same source-audio path. Post-production does not render
Blender scenes or register assets; see [Blender assets and motion controls](BLENDER.md)
and [Library asset validation](ASSETS.md) for those tasks.

## Audio mixing

An optional [`audio_mix`](../src/fsf/schemas/fsf_audio_mix_v1.schema.json) in an edit
selects exact sources, tracks, events and sample ranges. The existing
`audio_tracks` list supplies track roles and labels; settings refer to `track_id`.
Use clip IDs and event/region IDs when requesting changes, together with the edit
revision and digest. Audio inspection reports event positions in 48 kHz samples.

```powershell
.venv/Scripts/python -m fsf.cli edit audio inspect output/edit.json
.venv/Scripts/python -m fsf.cli edit audio import output/edit.json --definition audio-import.json --audio-resources audio-resources.json --out output/audio-declarations.json
.venv/Scripts/python -m fsf.cli edit patch output/edit.json --patch audio-patch.json
.venv/Scripts/python -m fsf.cli edit plan output/edit.json --out output/post-plan.json
.venv/Scripts/python -m fsf.cli edit audio analyze output/post-plan.json --source-executions sources.json --audio-resources audio-resources.json --out output/analysis
.venv/Scripts/python -m fsf.cli execute output/post-plan.json --source-executions sources.json --audio-resources audio-resources.json --out output/post
```

The import request follows
[`fsf_audio_import_request_v1`](../src/fsf/schemas/fsf_audio_import_request_v1.schema.json)
and binds the current edit ID, revision and digest. Its `selections` explicitly
choose `audio_source_id`, `resource_id`, `relative_path` and `stream_index`.
[`fsf_audio_resources_v1`](../src/fsf/schemas/fsf_audio_resources_v1.schema.json)
maps resource IDs to operator-controlled roots. Import returns measured source
declarations without changing the edit. It proves integrity and decodability,
not video conformance or generation provenance. No adjacent voice file is selected
automatically.

Apply `initialize_audio_mix` once through an edit patch, then use
`upsert_audio_track` (identity plus settings), `add_audio_source`,
`upsert_audio_event`, `upsert_audio_region`, their corresponding removal
operations, `set_audio_settings`, or `set_audio_enabled`. See the operation
shapes in [`fsf_edit_patch_v1`](../src/fsf/schemas/fsf_edit_patch_v1.schema.json).
Initialization must supply the complete settings and routing; original video
sound needs explicit `clip_original` sources and events in an enabled mix.
Removed IDs cannot be reused. Disabling a mix retains its settings and history;
it does not apply its external sources, gains or mute controls.

Events use canonical 48 kHz stereo sample trims and clip-relative or explicitly
anchored timeline placement. Events on one track cannot overlap. Regions are
event-relative sample intervals. Changes use the existing revision/digest
compare-and-publish protocol. `confirm_audio_timeline_anchor` explicitly confirms
an absolute event after a video timing change. Gain changes do not change the
video timing anchor. `--audio strip` conflicts with an enabled mix.

Normalization is off unless requested. Dialogue groups analyze selected event
contents in event declaration order, before placement gaps, manual gain and mute;
this is **not** whole-film loudness. BGM/SFX cannot be dialogue-group members.
Required normalization needs at least ten seconds of selected content and a
measurable loudness; silence or an unreachable target blocks delivery. Linear
normalization is capped at +12 dB. Manual gain remains after normalization, so a
master target of -16 LUFS followed by -3 dB has an effective target of -19 LUFS.
Group normalization imposes no -16 LUFS target on the final mixed film.

The renderer applies bounded ramps, peak limiting without automatic gain
compensation, and one final AAC encode. It measures the encoded MP4 again and
allows at most two additional attempts with tighter peak headroom. It never
changes manual gain to hide an unreachable target. Processing shares a fixed
300-second assemble budget; bounded process cleanup can take additional time.
Interrupting an audio/edit/post CLI operation returns a bounded cancellation
response with exit status 130, without a traceback.
Delivery includes `audio_report.json` and `audio_analysis.json`; numeric checks
and `listening_review: not_reviewed` remain separate. A missing peak reading is
not a passing measurement. Decoded all-zero audio can be delivered with
`numeric_quality: unmeasurable` and `peak_status: verified_digital_silence`;
other missing measurements block delivery. A required master LUFS target still
cannot pass on silence.

For A/B listening, preserve two edit snapshots differing only in audio processing:

```powershell
.venv/Scripts/python -m fsf.cli edit audio audition output/before.json output/after.json --request audition.json --source-executions sources.json --audio-resources audio-resources.json --out output/auditions
```

The request follows
[`fsf_audio_audition_request_v1`](../src/fsf/schemas/fsf_audio_audition_request_v1.schema.json),
including both revisions/digests and the `ab_projection_digest` returned by audio
inspection. It selects a clip-relative frame window and `mix` or `track_solo`.
Both sides process the full context before extracting the same window. They are
not independently volume-matched. Changed footage, source trims or sound positions
require an ordinary preview instead. A/B WAVs and video previews are previews,
not human listening approval. Numeric measurements cannot establish intelligibility
or whether background music masks speech.
