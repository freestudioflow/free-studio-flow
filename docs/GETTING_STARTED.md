# FSF Onboarding, Architecture, and Workflow Guide

This guide covers the dataflow, terminology, path requirements, DOCX syntax,
Handoff tutorial (both synthetic demo and human-approved workflow), and current
qualification boundaries.

## 1. Dataflow and Architecture

```text
DOCX shot sheet (fsf sheet new / python-docx)
  |
  v
fsf intake / fsf plan --media --assembly   -->  pdx_execution_plan_v1
  |
  v
fsf execute (DAG orchestrator + fsf status <out_dir> --json)
  |
  +---> local process executor (fsf-executors.json, kind="process")
  +---> Handoff workorders (--handoff: fsf_workorder_v1 / workorder input / fulfill)
  +---> remote parent/worker recipes (fsf remote / fsf api)
  +---> Studio flow shape stubs (fsf flow run --transport ... --shape)
  |
  v
pdx.media.technical_conform (per-shot QC) + fsf review gate (human approval)
  |
  v
fsf.assembly.fast_concat  -->  fsf verify --job <out_dir> [--plan <plan.json>]
```

## 2. Glossary

- **ProDocuX contract / PDX**: Schema- and digest-bound contracts (`prodocux`,
  `pdx-artifact-engine`, `pdx-adapter-media`) that FSF consumes without modifying.
- **`pdx_execution_plan_v1`**: Compiled DAG JSON produced by `fsf plan`, binding
  inputs, steps, contracts, and assembly transforms.
- **Executor**: Registered backend in FSF (`mock`, `shape`, `process`, `api`, `handoff`).
- **Transport**: Delivery channel used by Studio skills (`kaggle`, `api`, `local`).
- **Adapter**: Translation layer connecting an executor/transport to an external CLI, HTTP API, or Kaggle notebook.
- **Recipe / Profile**: Pinned remote execution recipe and typed hardware/environment/API capability profile (`fsf remote`, `fsf api`).
- **Shape**: Contract-stub mode (`--shape`) writing `.shape.bin` files to test interfaces without real media generation.
- **Media**: Real encoded A/V artifacts (`.mp4`, `.wav`) checked by `pdx.media.technical_conform` and `fsf verify`.
- **Handoff / Workorder**: Asynchronous delegation (`--handoff`) emitting digest-bound `fsf_workorder_v1` JSON files and ingesting `fsf_workorder_result_v1` receipts.
- **Parent / Worker**: Split remote dispatch architecture where the parent verifies receipts/budgets locally and isolated remote workers run inference.
- **FSF-M1-M4**: Core production spine (M1 Word intake, M2 plan/resume, M3 media conformance, M4 multi-executor).
- **FSF-S**: Fourteen Studio skill interfaces across P0-P3 phases (CLI/skill only; no desktop UI).

## 3. Execution Paths Matrix

| Path | OS Tested | Required Dependencies | Cost / Credentials | Key Config / Env Vars |
|---|---|---|---|---|
| Local synthetic demo (`fsf demo`) | Windows, Linux | Python 3.11+, `ffmpeg`, `ffprobe` | Free, offline, no account | None |
| Local process worker (`execute`) | Windows, Linux | Python 3.11+, `ffmpeg`, operator worker CLI | Depends on worker | `FSF_EXECUTORS_FILE` or `fsf-executors.json` |
| Agent Handoff (`execute --handoff`) | Windows, Linux | Python 3.11+, `ffmpeg`/`ffprobe` | Depends on agent/tool | `fsf workorder`, `fsf review`, `fsf status` |
| Studio shape (`flow run --shape`) | Windows, Linux | Python 3.11+ | Free, offline stubs | `--transport kaggle\|api\|local --shape` |
| Remote / Kaggle worker (`fsf remote`) | Windows, Linux | `kaggle` CLI / API deps | Kaggle quota or API billing | `KAGGLE_API_TOKEN`, `FSF_KAGGLE_SUBMIT=1`, `FSF_VENDOR_API_KEY` |
| Indexed production (`fsf production`) | Windows, Linux | SQLite + optional Blender/Whisper/Godot | Depends on backend | `FSF_PRODUCTION_AUTHORITY_ROOT` |

FSF never reads `.env` files automatically; credentials live in environment
variables or authorized host credential bindings only.

## 4. DOCX Shot Sheet Reference

Create a starter sheet directly with:

```powershell
.venv/Scripts/python -m fsf.cli sheet new --out my-shot-list.docx --shots 2 --handoff
```

Unix/macOS: replace `.venv/Scripts/python` with `.venv/bin/python`.

Supported DOCX syntax:

- **Block separator**: a paragraph containing three or more hyphens (`-----`).
- **Unit prefixes**: `SHOT:` or `ASSET:` (`:` required in body paragraphs; optional on Word `Heading` styles; unlabeled blocks default to `shot`).
- **Trailing duration**: optional trailing duration such as `2 seconds` or `3.5s` (defaults to `5.0` seconds). Note: `normalized_prompt` strips trailing duration phrases while `shot_spec.source_prompt` preserves the operator text verbatim.
- **Metadata tags** (one `TAG: value` per line inside the block; unique per block):
  - `ID: T01` (alphanumeric, `_`, `-`)
  - `ASSET_REFS: A01, A02` (comma-separated declared `ASSET` IDs)
  - `CONTINUITY: T01` (upstream source shot ID; only on `SHOT` blocks)
  - `CONTINUITY_ASPECTS: appearance, wardrobe` (requires `CONTINUITY`)
  - `REVIEW_REQUIRED: true|false` (defaults to `true` when consistency tags are set)

Markdown shot-sheet input is a future design candidate and is not supported by the current CLI.

## 5. Handoff Tutorial

### A. Closed synthetic tour (`demo --handoff`)

Run the self-contained two-shot synthetic demonstration (`T01 -> T02`):

```powershell
.venv/Scripts/python -m fsf.cli demo --handoff --out demo-handoff
.venv/Scripts/python -m fsf.cli verify --job demo-handoff
```

Notice that `demo --handoff` uses **`synthetic_worker`** and **`synthetic_demo_reviewer`**
inside a closed synthetic fixture solely to demonstrate dependency blocking, rejection/retry
(`att1 -> att2`), continuity chaining, pre-review assembly blocking, assembly, and tamper
invalidation. Real jobs require an authorized human reviewer.

### B. Human-approved Handoff workflow

1. Create and edit a Handoff sheet, compile the plan, and start execution:

```powershell
.venv/Scripts/python -m fsf.cli sheet new --out my-shot-list.docx --handoff
.venv/Scripts/python -m fsf.cli plan my-shot-list.docx --pipeline generate --media --assembly --out my-plan.json
.venv/Scripts/python -m fsf.cli execute my-plan.json --out my-job --handoff
```

2. Check status and inspect the active workorder (resolution, fps, duration, codec, audio, continuity):

```powershell
.venv/Scripts/python -m fsf.cli status my-job --json
.venv/Scripts/python -m fsf.cli workorder list my-job
```

3. For a continuity shot (`T02`), verify its workspace-relative input file and SHA-256:

```powershell
.venv/Scripts/python -m fsf.cli workorder input my-job <workorder_id>
```

4. Produce the matching MP4 at `target_artifact` (or report a failure with `--status failed --capability-gap "..."`), then seal the result with `workorder fulfill`. You must declare `--continuity-method` explicitly when continuity is required; FSF will not guess it for you:

```powershell
.venv/Scripts/python -m fsf.cli workorder fulfill my-job <workorder_id> --artifact T01.mp4
.venv/Scripts/python -m fsf.cli execute my-plan.json --out my-job --handoff
```

5. Record authorized human review before final assembly, then resume and verify:

```powershell
.venv/Scripts/python -m fsf.cli review list my-job
.venv/Scripts/python -m fsf.cli review approve my-job <step_id> --reviewer operator
.venv/Scripts/python -m fsf.cli execute my-plan.json --out my-job --handoff
.venv/Scripts/python -m fsf.cli verify --job my-job --plan my-plan.json
```

## 6. Repair, `--no-resume`, and Budgets

- **`execute --no-resume`** intentionally re-runs all steps in a standard JSON job for deterministic replay.
- **Per-shot retry / closure**: failed Handoff attempts automatically advance `attempt_id` on the next `execute --handoff` run. For bounded batch recovery and closed authorization budgets, use the indexed store (`fsf production store-create`, `run-batch-v2`, `store-resume`) documented in [Production Workflows](PRODUCTION_WORKFLOWS.md). Partial single-shot regeneration outside indexed recovery or Handoff retry remains a confirmed gap.

## 7. Capability Status Matrix

| Area | Status | Notes |
|---|---|---|
| DOCX intake, `plan --media`, `execute` resume, `verify --job`, `status --json` | Verified (local synthetic) | Covered on Windows and Linux with `ffmpeg`/`ffprobe` |
| `demo`, `demo --handoff`, `workorder input/fulfill`, `review` gate | Verified (local synthetic) | Synthetic FFmpeg test patterns only |
| `fsf flows` / `fsf flow run --shape` | Contract / shape tested | Transports write `.shape.bin` stubs, not live inference |
| Private Kaggle CPU & live gate (`live-check`) | Interface & CPU tested | GPU submit opt-in (`FSF_KAGGLE_SUBMIT=1`); Secrets auto-mount & >64 MiB GPU limits pending |
| Neural long takes, L40S/B300 hardware, formal 120m A/V, human perceptual QC | Pending | Not certified by synthetic demo or technical conformance |
