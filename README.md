# free-studio-flow

AI-native film factory and Studio flows on [ProDocuX](https://github.com/prodocux/prodocux) contracts.

License: Apache-2.0. See [LICENSE](LICENSE).

<!-- pypi-release-status:start -->
Source version in this branch: **`0.1.0rc6`**.

Latest verified PyPI release: **[`0.1.0rc6`](https://pypi.org/project/free-studio-flow/0.1.0rc6/)**.
The published distributions remain available on PyPI.

```powershell
python -m pip install "free-studio-flow==0.1.0rc6"
```
<!-- pypi-release-status:end -->

Repository: [github.com/freestudioflow/free-studio-flow](https://github.com/freestudioflow/free-studio-flow). This tree is the FSF product. Machine-readable PDX freeze and pin records live under `compatibility/`. It does **not** ship the B-roll generator; that remains [b-roll-library-generator](https://github.com/prodocux/b-roll-library-generator).

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

Vendor API / extra backends: put tokens in process environment variables only (`FSF_VENDOR_API_KEY`, or the `key_env` name in `fsf-executors.json`). Do not put secrets in JSON, git, or `AGENTS.local.md`. FSF does not load `.env`. See [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md).

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
| `workorder_digest` | SHA-256 of the workorder itself (tamper-evidence) |

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
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer alice

# 5. Approve with notes
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer alice --notes "Costume matches scene 2"

# 6. Reject (records verdict 'rejected'; re-approve is required before assembly proceeds)
.venv/Scripts/python -m fsf.cli review reject output/execute <step_id> --reviewer alice --notes "Wrong hair colour"

# 7. Reject and mark as needing rework (records verdict 'needs_rework')
.venv/Scripts/python -m fsf.cli review reject output/execute <step_id> --reviewer alice --needs-rework --notes "Re-generate with new prompt"

# 8. To regenerate: re-run execute with --no-resume (reruns ALL steps, not just rejected ones)
.venv/Scripts/python -m fsf.cli execute output/plan.json --out output/execute --no-resume

# 9. Then approve the new artifact and run execute again to reach assembly
.venv/Scripts/python -m fsf.cli review approve output/execute <step_id> --reviewer alice
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

## Docs

- Live verify: [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md)
- Release / PyPI: [`docs/RELEASE.md`](docs/RELEASE.md)
- Security: [`SECURITY.md`](SECURITY.md)
- Data governance: [`docs/DATA_GOVERNANCE.md`](docs/DATA_GOVERNANCE.md)
- Runtime pins: [`compatibility/pdx_official_pin_rc7_a8_a3.json`](compatibility/pdx_official_pin_rc7_a8_a3.json)
