# Consistency Review Gate

*Introduced in rc5; DOCX consistency tags added in rc6. See [Handoff Operations](HANDOFF.md) for Handoff consistency acknowledgement rules.*

When a shot's `consistency_context` carries `review_required: true`, FSF blocks assembly
until an operator records an approval for that shot via `fsf review approve`.
The gate is re-evaluated on every resume: if the artifact on disk has changed since
approval (re-generated, deleted, or modified), the approval is invalidated and assembly
is blocked again.

## Consistency review workflow

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

## Consistency context fields (`fsf_consistency_context_v1`)

The DOCX intake compiler can construct `consistency_context` from explicit metadata
paragraphs. API callers can also supply it in each shot task of an `fsf_video_job_v1`
job manifest before calling `build_execution_plan()`. The plan compiler validates the
context and carries it into step inputs.

## DOCX consistency declarations

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
> To exercise the full review -> approve -> assembly cycle, use a plan produced with
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
