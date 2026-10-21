# free-studio-flow

See [Provider setup](docs/FSF_LIVE_VERIFY.md#provider-setup-and-recoverable-generation) for API configuration, host secret
bindings, and bounded per-shot QC recovery. Credential checks and successful
model inference are verified separately.

AI-native film factory and Studio flows on [ProDocuX](https://github.com/prodocux/prodocux) contracts.

License: Apache-2.0. See [LICENSE](LICENSE).

<!-- pypi-release-status:start -->
Source version in this branch: **`0.1.0rc7`**.

Latest verified PyPI release: **[`0.1.0rc7`](https://pypi.org/project/free-studio-flow/0.1.0rc7/)**,
published from tag **[`v0.1.0rc7`](https://github.com/freestudioflow/free-studio-flow/releases/tag/v0.1.0rc7)**
at commit `aafe4328a5a30030c8178c0e16aa637c26e573e6`.

```powershell
python -m pip install "free-studio-flow==0.1.0rc7"
```
<!-- pypi-release-status:end -->

The rc7 features below are included in the published rc7 package.

Repository: [github.com/freestudioflow/free-studio-flow](https://github.com/freestudioflow/free-studio-flow). This tree is the FSF product. Machine-readable PDX freeze and pin records live under `compatibility/`. It does **not** ship the B-roll generator; that remains [b-roll-library-generator](https://github.com/prodocux/b-roll-library-generator).

### Remote workers and model APIs

This checkout includes opt-in `fsf remote` parent/worker dispatch and `fsf api`
profiles for text, image, video, audio and upscale outputs. Copy the disabled
templates with `fsf remote templates --out config` into an existing empty
directory. See the [operator and agent guide](src/fsf/remote/templates/AGENT_GUIDE.md)
for setup, recovery, provider protocol limits and recipe customization.
These workflows are included in the rc7 distributions.
Remote outputs retain parent-side QC and review requirements; successful transport
does not establish real model inference or visual consistency.

Runtime pins: `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, `pdx-adapter-media==0.2.0a3`.

## What this checkout contains

- **FSF-M1–M4 Spine**: Word intake, execution plan compiler, authoritative resume invalidation, media conformance verification, and multi-executor abstraction
- **Plan Execution Runtime (`fsf execute`)**: Topological DAG execution engine for `pdx_execution_plan_v1` plans, orchestrating tool steps, transform steps, and fail-closed QC conformance checks
- **Fast Assembly**: Multi-shot video concatenation with external background music (BGM) mixing, embedded voice audio track mixing (`amix`), and subtitle policies (none, sidecar, burned)
- **Process Command Executor (`process_cmd`)**: Process adapter executing local CLI and script commands with isolated temporary output files, secret sanitization and atomic publication
- **Agent Handoff** *(rc4)*: Delegate generation to external agents through validated workorders and results, with retry and resume support.
- **Windows Subprocess Cleanup** *(rc4)*: Clean up descendant processes on timeout or cancellation, including when `taskkill` fails.
- **Audio Policies** *(rc4)*: Preserve explicit source audio contracts while controlling assembly audio with `--audio keep|strip|require`.
- **Consistency Review Gate** *(rc5)*: Per-shot consistency management. Assembly is blocked until all `review_required` shots have an approved review record. The approval is bound to the artifact SHA at approval time; a changed file on disk invalidates the approval automatically. The plan compiler resolves which step produces each assembly input at compile time; the artifact path and SHA are read from the execution journal after the step completes. CLI: `fsf review list`, `approve`, `reject`, `status`.
- **FSF-S Skills**: Fourteen Studio skills as CLI/skill (kaggle / api / local transports). No Studio desk in this release
- **Live Gate**: Kaggle API ping + private job envelope + published PyPI pins + local transport shape. Live local-model ping is skipped when this machine has no useful Comfy/GPU. GPU kernel submit is opt-in (`FSF_KAGGLE_SUBMIT=1`) and off by default. Extra API/local/process backends: `fsf-executors.example.json`
- **Editing and audio** *(rc7)*: Revision-bound clip location, trims, subtitles,
  independent post-production plans, per-track gain/mute, optional loudness
  normalization, peak checks and A/B listening previews.
- **Blender and assets** *(rc7)*: Trusted small-scene rendering, static
  references, clay motion controls, external control-video import and immutable
  asset registration with fixed-version lookup.
- **Remote and API workflows** *(rc7)*: Parent/worker recipes, typed model
  profiles, host credential bindings and bounded per-shot QC recovery.

## Install

Python 3.11+.

```text
python -m venv .venv
.venv/Scripts/python -m pip install -U pip
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m fsf.cli check
```

Unix: `.venv/bin/python` instead of `.venv/Scripts/python`. Do not activate the venv and then use a bare `python` or `fsf` from PATH.

## Verify

Run the contributor verification suite from the checkout:

```text
.venv/Scripts/python scripts/agent_verify.py
```

That runs public hygiene, offline pytest, then live Kaggle/API pytest, then `.venv/Scripts/python -m fsf.cli live-check`.
Kaggle credentials: `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json`. The key is never printed. Missing credentials **fail** the live gate; do not skip.

`.venv/Scripts/python -m fsf.cli run` defaults to **`mock.studio.video`** (writes `.mock.bin`, not media). `flow run` requires `--transport` and `--shape` and refuses mock executors. Built-in kaggle/api/local shape adapters write `.shape.bin`; configured process adapters used by `fsf execute` can produce real media. `verify` on those files is expected to fail.

Vendor API / extra backends use process environment variables (`FSF_VENDOR_API_KEY`,
or the `key_env` name in `fsf-executors.json`). Typed model profiles can also use
an explicit host credential binding. Do not put secrets in JSON, git, or
`AGENTS.local.md`. FSF does not load `.env`. See
[`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md).

## CLI

Windows paths below; Unix: `.venv/bin/python`.

```text
.venv/Scripts/python -m fsf.cli check
.venv/Scripts/python -m fsf.cli flows
.venv/Scripts/python -m fsf.cli intake path/to/sheet.docx --out output/job
.venv/Scripts/python -m fsf.cli plan path/to/sheet.docx --pipeline p0
.venv/Scripts/python -m fsf.cli plan path/to/sheet.docx --pipeline custom --skills storyboard,generate,dub
.venv/Scripts/python -m fsf.cli plan path/to/sheet.docx --pipeline p1 --media --assembly --bgm path/to/bgm.wav --out output/plan.json
.venv/Scripts/python -m fsf.cli execute output/plan.json --out output/execute
.venv/Scripts/python -m fsf.cli flow run studio.video.generate path/to/sheet.docx --transport kaggle --shape
.venv/Scripts/python -m fsf.cli run path/to/sheet.docx --executor mock.studio.video
.venv/Scripts/python -m fsf.cli verify path/to/clip.mp4
.venv/Scripts/python -m fsf.cli live-check
```

- `plan`: Compiles the execution plan (`pdx_execution_plan_v1`). Supports `--media` (real media routing) or `--shape` (contract stubs), plus `--assembly`, `--subtitles`, and `--bgm`.
- `execute`: Orchestrates the compiled plan DAG end-to-end, executing steps, assembly transforms, and QC conformance gates with authoritative resume support.
- `review list <out_dir>`: Lists shots with `review_required` that are pending, rejected, or have an invalidated approval. *(rc5)*
- `review status <out_dir>`: Shows overall assembly gate status (pass / pending / blocked). *(rc5)*
- `review approve <out_dir> <step_id> [--reviewer NAME] [--notes TEXT]`: Approves a shot, binding the review to the current artifact SHA. *(rc5)*
- `review reject <out_dir> <step_id> [--reviewer NAME] [--notes TEXT] [--needs-rework]`: Rejects a shot. `--needs-rework` records verdict `needs_rework` instead of `rejected`. *(rc5)*
- `run`: Single-step video generation runner (defaults to `mock.studio.video` writing `.mock.bin`).
- `flow run`: Single Studio skill runner across kaggle/api/local transports (shape-only).
- Full sequence: [`AGENTS.md`](AGENTS.md).

## Handoff operations and constraints *(rc4)*

`fsf execute` can delegate media-generation tool steps to an asynchronous external agent.
Enable Handoff via any of:

```text
# Delegate all media-generation steps
fsf execute plan.json --out output/execute --handoff

# Delegate via executor override
fsf execute plan.json --out output/execute --executor handoff

# Per-step: set executor_id in the plan step inputs
inputs.executor_id = "handoff"
```

On the first run FSF emits workorders and exits with `status: "pending_handoff"`.
After the external agent writes result files into `<out_dir>/handoff/results/`,
re-run the **identical** command; FSF ingests the results and continues with QC
and assembly steps (those are always executed by FSF, never delegated).

**Workorder schema**: `fsf_workorder_v1`

Workorder fields used to prepare and validate a result:

| Field | Description |
|---|---|
| `workorder_id` | Unique per `job_id / run_id / step_id / attempt_id` |
| `target_artifact` | Exact output path the executor must write |
| `plan_digest` | SHA-256 of the compiled plan; must be echoed in the result |
| `inputs_digest` | SHA-256 of step input bindings; must be echoed in the result |
| `expected_contract_digest` | SHA-256 of the media QC contract; must be echoed if set |
| `workorder_digest` | SHA-256 for detecting workorder changes; not a signature |

**Acceptable result JSON** (`fsf_workorder_result_v1`):

```json
{
  "schema_version": "fsf_workorder_result_v1",
  "workorder_id":   "<copy from workorder>",
  "job_id":         "<copy from workorder>",
  "run_id":         "<copy from workorder>",
  "step_id":        "<copy from workorder>",
  "attempt_id":     "<copy from workorder>",
  "plan_digest":    "<copy from workorder>",
  "inputs_digest":  "<copy from workorder>",
  "expected_contract_digest": "<copy from workorder, or omit if null>",
  "status":         "completed",
  "artifact_path":  "<must equal workorder target_artifact exactly>",
  "artifact_sha256": "<sha256 of the written file>",
  "detail":         "optional free-text"
}
```

**Handoff cycle**:

1. **Emit workorder** — FSF writes a digest-bound `fsf_workorder_v1` to
   `<out_dir>/handoff/workorders/<workorder_id>.workorder.json`.
2. **Ingest result** — after the executor writes the output file and places
   `<workorder_id>.result.json` in `<out_dir>/handoff/results/`,
   `ingest_workorder_result()` verifies path confinement, zero-byte guard,
   SHA-256, and all mandatory digest fields.
   Violations raise `HandoffIntegrityError` or `HandoffSecurityError`.
3. **Retry on rejection** — re-run the same command to receive a new attempt.
   Use the new workorder's identifiers and digests when submitting a replacement result.

**Constraints**:

- Do **not** run `verify` on `.shape.bin` or `.mock.bin`; those are contract stubs, not media.
- `artifact_path` in the result must equal the workorder's `target_artifact` exactly; FSF
  does not accept substitutions. SHA-256 conflict detection rejects results whose digest
  differs from a previously accepted result for the same step.
- Path traversal in `target_artifact` is rejected unconditionally.
- Copy the result's identity and digest fields from the current workorder. Preserve the integer type of `attempt_id`; include `expected_contract_digest` when it is set.

### Consistency acknowledgement in Handoff results *(rc5)*

When the workorder carries a `requirements_digest` field, the result **must** include
`consistency_acknowledgement`. Submitting a result without it, or with a mismatched
`acknowledged_digest`, causes ingest to raise `HandoffIntegrityError`.

**Full result example with consistency fields**:

```json
{
  "schema_version": "fsf_workorder_result_v1",
  "workorder_id":   "<copy from workorder>",
  "job_id":         "<copy from workorder>",
  "run_id":         "<copy from workorder>",
  "step_id":        "<copy from workorder>",
  "attempt_id":     1,
  "plan_digest":    "<copy from workorder>",
  "inputs_digest":  "<copy from workorder>",
  "expected_contract_digest": "<copy from workorder, or omit if null>",
  "status":         "completed",
  "artifact_path":  "<must equal workorder target_artifact exactly>",
  "artifact_sha256": "<sha256 of the written file>",
  "detail":         "optional free-text",
  "consistency_acknowledgement": {
    "acknowledged_digest": "<copy workorder.requirements_digest verbatim>",
    "ref_assets_used": [
      { "key": "<key from workorder.consistency_requirements.ref_image_shas>",
        "sha256": "<sha256 that matches the declared value>" }
    ],
    "continuity_method": "<method used — see accepted values below>"
  }
}
```

> **`attempt_id`**: copy the integer value from the current workorder unchanged.
> Do not hardcode `1`; the workorder may carry a higher attempt number on retry.

> **`continuity_method`**: only required when `workorder.consistency_requirements.resolved_sha256`
> is set. Use the value that describes how the continuity source was actually used.
> `"prompt_only"` is rejected when `workorder.consistency_requirements.prompt_only_allowed`
> is `false`.

**`consistency_acknowledgement` field rules**:

| Field | When required | Accepted values |
|---|---|---|
| `acknowledged_digest` | Always when `requirements_digest` is set | Exact copy of `workorder.requirements_digest` |
| `ref_assets_used` | When workorder declares `ref_image_shas` | Array of `{key, sha256}` — every declared key must appear; no duplicates; SHA must match |
| `continuity_method` | When workorder declares a `resolved_sha256` in continuity | One of: `first_frame_init`, `ip_adapter`, `reference_latent`, `reference_image`, `video_continuation`, `prompt_only` |
| `capability_gap` | Never on a `completed` result | A non-empty value with `status: completed` is rejected — report `status: failed` with the gap in `detail` instead |

**Placement rule**: `capability_gap`, `ref_assets_used`, and `continuity_method` must be
nested inside `consistency_acknowledgement`. Top-level placement is rejected outright.

**What acknowledgement proves**: that the agent received the requirements vector. It does
not prove visual compliance; human review via `fsf review approve` remains the
authoritative compliance gate.

## Consistency review gate *(rc5)*

When a shot's `consistency_context` carries `review_required: true`, FSF blocks assembly
until an operator records an approval for that shot via `fsf review approve`.
The gate is re-evaluated on every resume: if the artifact on disk has changed since
approval (re-generated, deleted, or modified), the approval is invalidated and assembly
is blocked again.

### Consistency review workflow

```powershell
# 1. Execute the plan (generates shots and writes the execution journal)
.venv/Scripts/python -m fsf.cli execute output/plan.json --out output/execute

# 2. List shots awaiting review
.venv/Scripts/python -m fsf.cli review list output/execute

# 3. Check overall gate status (pass / pending / blocked)
.venv/Scripts/python -m fsf.cli review status output/execute

# 4. Approve a shot (records the SHA from the journal entry; disk is re-checked at assembly time)
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer operator

# 5. Approve with notes
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer operator --notes "Costume matches scene 2"

# 6. Reject (records verdict 'rejected'; re-approve is required before assembly proceeds)
.venv/Scripts/python -m fsf.cli review reject output/execute <step_id> --reviewer operator --notes "Wrong hair colour"

# 7. Reject and mark as needing rework (records verdict 'needs_rework')
.venv/Scripts/python -m fsf.cli review reject output/execute <step_id> --reviewer operator --needs-rework --notes "Re-generate with new prompt"

# 8. To regenerate: re-run execute with --no-resume (reruns ALL steps, not just rejected ones)
.venv/Scripts/python -m fsf.cli execute output/plan.json --out output/execute --no-resume

# 9. Then approve the new artifact and run execute again to reach assembly
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer operator
.venv/Scripts/python -m fsf.cli execute output/plan.json --out output/execute
```

**What the gate checks before each assembly step**:
- An `approved` review record exists for the step.
- The artifact file exists on disk at the path recorded in the journal.
- The artifact's current on-disk SHA-256 matches the digest recorded in the review.
  A mismatch means the file has changed since approval; the approval is invalidated.
- The consistency requirements digest in the review matches the current journal.

**How `approve` records the SHA**: `fsf review approve` reads the artifact SHA from the
execution journal (the value written by the executor when the step completed).
The assembly gate then re-checks the same path against the live file on disk at assembly time.
If the file has been modified or replaced between approval and assembly, the gate blocks.

**`reject` vs `reject --needs-rework`**: both record a non-approved verdict and block
assembly. The distinction is informational for human workflow — FSF treats them identically
at the gate. To regenerate the shot, use `fsf execute --no-resume`, which reruns all steps
(FSF has no selective per-shot re-run flag). Then approve the new artifact before re-running
execute to reach assembly.

### Consistency context fields (`fsf_consistency_context_v1`)

The DOCX intake compiler can construct `consistency_context` from explicit metadata
paragraphs. API callers can also supply it in each shot task of an `fsf_video_job_v1`
job manifest before calling `build_execution_plan()`. The plan compiler validates the
context and carries it into step inputs.

### DOCX consistency declarations (rc6)

Place each metadata line in its own paragraph (or line break) inside a block,
after the `SHOT:` or `ASSET:` prompt. Separate blocks with `-----` as usual:

```text
ASSET: Silver coat, short black hair, amber collar.
ID: hero
-----
SHOT: Wide view of the observation deck. 10 seconds
ID: T01
ASSET_REFS: hero
-----
SHOT: Close view of the same officer. 10 seconds
ID: T02
ASSET_REFS: hero
CONTINUITY: T01
CONTINUITY_ASPECTS: appearance,costume
REVIEW_REQUIRED: true
```

| Tag | Meaning |
| --- | --- |
| `ID` | Optional unique task ID, using letters, digits, `_` and `-`; starts with a letter or digit. Otherwise the existing `task_001` numbering applies. |
| `ASSET_REFS` | Ordered, comma-separated asset IDs from the same sheet. Only explicitly referenced asset descriptions are appended to the effective prompt. |
| `CONTINUITY` | ID of an earlier shot; uses `latest_completed` artifact selection. |
| `CONTINUITY_ASPECTS` | Comma-separated `appearance`, `costume`, `scene`, `composition`, `motion`; defaults to `appearance`. Requires `CONTINUITY`. |
| `REVIEW_REQUIRED` | `true` or `false`; defaults to `true` when any consistency tag is present. `ID` alone does not enable consistency review. |

Metadata is removed from the generation prompt. Asset references use version `1`
and a content hash computed from the asset specification; editing the description
changes the hash. Duplicate IDs/tags, empty values, unknown assets, invalid aspects,
and self/forward continuity references are rejected. Untagged sheets retain their
existing behaviour. Reference-image declarations and `specific_artifact` selection
remain available through the context API.

```powershell
python -m fsf.cli intake sheet.docx --out output/intake
python -m fsf.cli plan sheet.docx --media --assembly --out output/plan.json
python -m fsf.cli execute output/plan.json --out output/execute --handoff
```

A real-media backend must complete the Handoff workorders and consistency
acknowledgements. Review required shots with `fsf review` before assembly can run.
The declarations express requirements; they do not prove visual consistency.

**Correct flow when using a consistency context**:

```python
import json
from pathlib import Path
from fsf.runtime.plan import build_execution_plan

# This example demonstrates building a job manifest with a consistency_context
# and compiling it into a pdx_execution_plan_v1.  It does NOT demonstrate a
# working review→approve→assembly cycle; see the note below.

job_manifest = {
    "schema_version": "fsf_video_job_v1",
    "job_id": "job_example",
    "tasks": [
        {
            "id": "shot_001",
            "kind": "shot",
            "shot_spec": {"generation_prompt": "A hero walks through a neon city."},
            "consistency_context": {
                "schema_version": "fsf_consistency_context_v1",
                "asset_refs": [],       # no assets — empty list is valid
                "review_required": True,
            },
        }
    ],
}

# Compile into a pdx_execution_plan_v1.
# enable_assembly=True adds a transform step after generation.
# shape=True produces .shape.bin contract stubs (not real video files).
plan = build_execution_plan(
    job_manifest, pipeline="generate", shape=True, enable_assembly=True
)

# Save the plan (create output/ first to avoid FileNotFoundError)
out_path = Path("output/plan.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
print("Plan written to", out_path)
print("Steps:", [s["id"] for s in plan.get("steps", [])])
```

> **Shape mode limitation**: running `fsf execute` on this plan will exit with an error.
> Shape stubs (`.shape.bin`) fail Media QC, which blocks the assembly step with
> `QC dependency failed` — not a review gate block. Approving the shot review does not
> resolve a QC failure.
>
> This example is for illustrating **context compilation and plan structure only**.
> To exercise the full review → approve → assembly cycle, use a plan produced with
> a real-media executor and `shape=False`. The review gate blocks assembly independently
> of QC only when QC passes (i.e., when real media artifacts satisfy the conformance contract).

| Field | Required | Purpose |
|---|---|---|
| `schema_version` | yes | Always `"fsf_consistency_context_v1"` |
| `asset_refs` | yes | List of asset references that apply to this shot; empty list `[]` is valid when no assets are declared |
| `review_required` | yes | `true` to hold assembly until an operator approval is recorded |
| `ref_images` | no | Shot-level reference images (role + SHA-256 content identity; path is a location hint only) |
| `continues_from` | no | Continuity declaration: which upstream shot this shot continues from, how to select its artifact, and which visual aspects to match |
| `generation_intent` | no | Generation prompt components compiled by the plan (base_prompt, injection_rule_version, asset_injections) |

**Minimum valid context** (no assets, review required):

```python
{
    "schema_version": "fsf_consistency_context_v1",
    "asset_refs": [],
    "review_required": True,
}
```

**Context with an asset reference** — `content_hash` must be a real 64-character SHA-256
hex string computed from the canonical asset digest. Placeholder strings fail schema validation:

```python
{
    "schema_version": "fsf_consistency_context_v1",
    "asset_refs": [
        {
            "asset_id": "char_alice",
            "version": "v1",
            "content_hash": "a" * 64   # replace with actual digest
        }
    ],
    "review_required": True,
}
```

**`continues_from` selection rules**:
- `"latest_completed"` — use the most recently completed artifact from the upstream shot in the current run.
- `"specific_artifact"` — use the artifact identified by `artifact_ref` (requires `artifact_ref` to be set).

The `resolved_sha256` field inside `continues_from` is always null at compile time.
The executor fills it with the actual upstream artifact SHA-256 before computing the inputs digest.

**Compile-time vs execution-time**:
- The plan compiler resolves which step produces each assembly input at compile time.
- The artifact path and SHA-256 are read from the execution journal after the producing step completes.


## Audio contract behaviour under `--audio` flags *(rc4)*

CLI accepts `--audio keep` (default), `--audio strip`, or `--audio require`.
These flags govern two separate contract layers:

### Source QC contract (per-shot conformance gate)

Only intake-generated default contracts (`expected_media_contract_source: "intake_default"`)
are touched. Explicit operator contracts are never overridden.

| `--audio` value | Effect on intake-default source contract |
|---|---|
| `keep` *(default)* | `audio` relaxed to `"allowed"` |
| `strip` | No change to source contract |
| `require` | No change to source contract |

### Assembly output contract (concatenated output conformance)

Applies to the assembled output artifact regardless of contract source:

| `--audio` value | Assembly output audio expectation |
|---|---|
| `keep` *(default)* | `"required"` if BGM or dub track present; otherwise `"allowed"` |
| `strip` | `"forbidden"` (or `"required"` if BGM is attached) |
| `require` | `"required"` |

**Explicit operator contracts are never overridden.** If a shot carries an
explicit `audio: "forbidden"` or `audio: "required"` entry (no
`expected_media_contract_source: "intake_default"` marker), the source QC
contract is left untouched by any `--audio` flag.

## Edit metadata commands (rc7 release candidate)

`fsf edit` manages a versioned edit description. The metadata commands below
do not read media or establish source QC/review authority. For verified source
import, previews and rendering, use the post-production workflow below.

Prepare `definition.json` using
[`fsf_edit_definition_v1`](src/fsf/schemas/fsf_edit_definition_v1.schema.json):
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

Patches follow [`fsf_edit_patch_v1`](src/fsf/schemas/fsf_edit_patch_v1.schema.json)
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

## Verified post-production (rc7 release candidate)

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
[`fsf_source_executions_v1`](src/fsf/schemas/fsf_source_executions_v1.schema.json):

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
[`fsf_edit_import_v1`](src/fsf/schemas/fsf_edit_import_v1.schema.json).
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
[`fsf_edit_patch_v1`](src/fsf/schemas/fsf_edit_patch_v1.schema.json).
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
Blender scenes or register assets; use the separate commands below for those tasks.

### Audio mixing (rc7 release candidate)

An optional [`audio_mix`](src/fsf/schemas/fsf_audio_mix_v1.schema.json) in an edit
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
[`fsf_audio_import_request_v1`](src/fsf/schemas/fsf_audio_import_request_v1.schema.json)
and binds the current edit ID, revision and digest. Its `selections` explicitly
choose `audio_source_id`, `resource_id`, `relative_path` and `stream_index`.
[`fsf_audio_resources_v1`](src/fsf/schemas/fsf_audio_resources_v1.schema.json)
maps resource IDs to operator-controlled roots. Import returns measured source
declarations without changing the edit. It proves integrity and decodability,
not video conformance or generation provenance. No adjacent voice file is selected
automatically.

Apply `initialize_audio_mix` once through an edit patch, then use
`upsert_audio_track` (identity plus settings), `add_audio_source`,
`upsert_audio_event`, `upsert_audio_region`, their corresponding removal
operations, `set_audio_settings`, or `set_audio_enabled`. See the operation
shapes in [`fsf_edit_patch_v1`](src/fsf/schemas/fsf_edit_patch_v1.schema.json).
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
[`fsf_audio_audition_request_v1`](src/fsf/schemas/fsf_audio_audition_request_v1.schema.json),
including both revisions/digests and the `ab_projection_digest` returned by audio
inspection. It selects a clip-relative frame window and `mix` or `track_solo`.
Both sides process the full context before extracting the same window. They are
not independently volume-matched. Changed footage, source trims or sound positions
require an ordinary preview instead. A/B WAVs and video previews are previews,
not human listening approval. Numeric measurements cannot establish intelligibility
or whether background music masks speech.

## Library asset validation (rc7 release candidate)

The Python API `fsf.core.asset_library.validate_asset_record(record,
resource_root=...)` checks a
[`fsf_library_asset_record_v1`](src/fsf/schemas/fsf_library_asset_record_v1.schema.json)
against local files and returns an isolated record copy. This validation function
does not register assets or create an index. Use `fsf asset register` and
`fsf asset get` for immutable registration and fixed-version lookup, described
in the Blender section below. Both v1 records and v2 control-asset records are
accepted without changing their respective role definitions.

Validation checks the primary artifact reference, supported kind and role/MIME
pairs, reference-image bindings, portable relative paths, file sizes and SHA-256,
and both record digests. Every declared file must exist under the explicit
resource root. Symlinks, reparse points and non-regular files are rejected.

`ref_images[].role` describes the semantic category (`character`, `scene`,
`prop`, or `style`); `artifacts[].role` describes file usage. A reference-image
SHA must resolve to an artifact with usage `reference_image`. An image-only
asset may use that artifact as its primary. The content hash binds the asset
specification; the record digest also binds artifact declarations and order,
excluding only itself and location hints (`artifacts[].locator`). Moving a
locator does not change that digest, but the new location is still checked.

Successful validation proves the declared integrity checks at the time of
reading. It does not decode media, establish complete model dependencies,
certify rendering/Gaming/VR suitability, or protect against every concurrent
filesystem replacement. Consumers must re-check files when using them.

## Docs

- Live verify: [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md)
- Release / PyPI: [`docs/RELEASE.md`](docs/RELEASE.md)
- Security: [`SECURITY.md`](SECURITY.md)
- Data governance: [`docs/DATA_GOVERNANCE.md`](docs/DATA_GOVERNANCE.md)
- Runtime pins: [`compatibility/pdx_official_pin_rc7_a8_a3.json`](compatibility/pdx_official_pin_rc7_a8_a3.json)


## Blender assets and motion controls

These commands describe the rc7 release candidate; rc7 is not yet published.

`fsf blender` runs controlled, trusted small scenes with CPU Cycles. Supported
profiles are `reference` (one frame, up to two named cameras) and `clay_v1`
(explicit gray material override, camera/object transform keyframes). The tested
Blender families are 3.2 and 4.3. Blender is an operator-installed external tool.

Create a `fsf_blender_request_v1` JSON document using the packaged schema. It
binds a verified source asset record, relative source file, named cameras,
resolution, rational clock, half-open frame interval, and optional linear
position/Euler-rotation keyframes. The source root contains `record.json` and
its declared resources. Asset records verify bytes and declarations, not scene
safety. Only explicitly trusted scenes are accepted; disabling auto-execution
is not a sandbox for hostile files.

```powershell
fsf blender check --executable <blender-executable>
fsf blender plan request.json --out plan.json
fsf blender execute plan.json --source-root source --out renders --executable <blender-executable> --trusted-scene
fsf blender verify renders/<bundle-id>
fsf blender condition renders/<bundle-id> --view-id front --out condition.json
fsf blender record renders/<bundle-id> --asset-id prop_A --asset-version 1 --prompt "A fixed prop" --out record.json
fsf asset register record.json --resources renders/<bundle-id> --library library
fsf asset get prop_A 1 --library library
```

Create output/library directories before these commands. Executing the same
plan revalidates and reuses its bundle. `--no-resume` creates a separate attempt;
existing bundles and asset versions are not overwritten. JSON output files must
not already exist. A model, frame files, portable control MP4, manifest and
receipt are published together only after validation. Static references have
no control MP4. No credentials or operator scripts are copied into the bundle.

The small-scene profile limits rendering to 96 frames per view, two views,
512-by-512 pixels, 64 objects, 250,000 mesh vertices and 64 MiB of declared input
files. Frame rates are 24 and 24000/1001. The transform has a shared 120-second
processing deadline; cleanup may extend wall time. These limits are not a hard
OS memory sandbox. Linked libraries, pre-authored animation, constraints, modifiers and executable
drivers, compositing and sequencer overrides are unsupported. CPU profiles use four samples, seed zero and no denoising.
Tool-binary and worker-script digests participate in reuse identity. Reference images require available textures; clay is
an explicit override, never an automatic missing-texture fallback.

Dynamic artifacts use `fsf_library_asset_record_v2` control roles; v1 records
retain their original semantics. Control videos are conditioning inputs, not
approved story footage or video-continuity sources. They never enter static
`ref_images`. Condition metadata binds the actual video SHA, clock, frame count
and frame mapping. Only `preprocess="none"` is supported; implicit resampling,
cropping, looping and prompt-only fallback are refused.

Python callers can use `condition_request`, `validate_condition_capability`
and `validate_condition_ack` from `fsf.runtime.blender`. Handoff workorders can
carry `video_condition` and `condition_capability`; successful results must
return a matching `condition_acknowledgement`. This acknowledgement reports use,
not visual compliance. Render receipts keep `motion_review="not_reviewed"`.
Remote scheduling and model generation are separate capabilities; these commands
do not submit jobs or promise that a model will follow the control motion.

### Import a control video made in another tool

Blender is not required for this import. Create `controls/` first, then run:

```powershell
fsf blender import-control user-control.mp4 --out controls --control-id user_A
fsf blender verify controls/<bundle-id>
fsf blender condition controls/<bundle-id> --view-id imported --out condition.json
fsf blender record controls/<bundle-id> --asset-id control_A --asset-version 1 --prompt "User motion control" --out record.json
fsf asset register record.json --resources controls/<bundle-id> --library library
```

The import copies the original bytes, fully decodes the video and records its
SHA, actual frame timing, decoded RGB digest and identity frame mapping. Repeating
an unchanged import revalidates the existing bundle. The manifest identifies
`operator_control_video_import`; it does not claim Blender camera evidence or
certify a gray material profile. `clay_profile` is `unverified` and `motion_review`
remains `not_reviewed`. No Blender render receipt is fabricated.

This first import profile accepts a single video-only MP4 stream, 24 or
24000/1001 constant fps, zero-based timestamps, at most 96 frames, even dimensions
from 32 to 512 pixels, square pixels and at most 64 MiB. Audio, rotation, variable
timing and unsupported dimensions/rates are refused, not silently stripped or
converted. Prepare a conforming export in the source tool if necessary. Processing
shares a 120-second budget; cleanup may take longer. Existing bundles are never
overwritten. Use a short output directory on Windows to stay within filesystem
path limits.

The resulting condition uses the same capability and Handoff acknowledgement
checks as rendered controls. Registration and acknowledgement do not prove that
a model followed the motion. These commands do not run a generation model.
