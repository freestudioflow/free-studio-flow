# free-studio-flow

Free Studio Flow (FSF) turns a Word shot list into a production plan, tracks
generation and review, and assembles and verifies the finished video.

## Try a video locally

Use Python 3.11+ and put `ffmpeg` and `ffprobe` on your PATH. From a source
checkout, you can use `pip install .` (or install the RC15 candidate wheel) in
place of the PyPI package. The local synthetic demo needs no account, API key, or
GPU.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install "free-studio-flow==0.1.0rc15"
.venv/Scripts/python -m fsf.cli --version
.venv/Scripts/python -m fsf.cli demo --out demo-output
```

**Linux / macOS (shell)**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "free-studio-flow==0.1.0rc15"
.venv/bin/python -m fsf.cli --version
.venv/bin/python -m fsf.cli demo --out demo-output
```

Expect `--version` to print `fsf 0.1.0rc15` for this branch. Open
`demo-output/assembled_timeline.mp4` in your video player. An agent should give
the user the full path so they can play it. Expect a four-second, silent clip
with moving test patterns.

Verify the clip against the job's recorded contract. A successful result reports
`evaluation_status: "conforms"`.

**Windows**

```powershell
.venv/Scripts/python -m fsf.cli status demo-output --json
.venv/Scripts/python -m fsf.cli verify --job demo-output
```

**Linux / macOS**

```bash
.venv/bin/python -m fsf.cli status demo-output --json
.venv/bin/python -m fsf.cli verify --job demo-output
```

Let the demo create its output directory. If `demo-output` already contains
other files, choose a new name and use that same name when verifying. If FSF
reports `FFMPEG_REQUIRED`, check that both executables run in the same shell.
The demo does not download them.

## Start your own job

1. Create or copy a shot sheet (`sample.docx` or `sheet new`), keeping the `-----`
   separator between shots. Ask the user to confirm the shot list and what will
   produce the clips.
2. Create the plan and emit the first generation workorders. On Linux/macOS
   replace `.venv/Scripts/python` with `.venv/bin/python`.

   ```powershell
   .venv/Scripts/python -m fsf.cli sheet new --out my-shot-list.docx --handoff
   .venv/Scripts/python -m fsf.cli plan my-shot-list.docx --pipeline generate --media --assembly --out my-plan.json
   .venv/Scripts/python -m fsf.cli execute my-plan.json --out my-job --handoff
   ```

3. When execute pauses, run `.venv/Scripts/python -m fsf.cli status my-job --json`.
   Read `state`, `blockers`, and `next_action`. For `waiting_handoff`, inspect
   `workorders` (or run `workorder list my-job`), verify any continuity input
   with `workorder input my-job <workorder_id>`, write the produced MP4 to
   `write_artifact_to`, and submit the sealed receipt with
   `workorder fulfill my-job <workorder_id> --artifact <write_artifact_to>` (or
   run the suggested `fulfill_argv`).
   For `waiting_review`, present the shot for
   [operator review](docs/CONSISTENCY_REVIEW.md#consistency-review-gate).
   Re-run the original execute command (`next_action.argv` when available) after
   submitting results or recording approval.
4. After assembly, verify with `.venv/Scripts/python -m fsf.cli verify --job my-job`.

## Docs

- **Start & Handoff**: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) (dataflow diagram, glossary, DOCX reference, `demo --handoff`, capability matrix)
- **Handoff & Workorder Contract**: [`docs/HANDOFF.md`](docs/HANDOFF.md)
- **Consistency Review Gate**: [`docs/CONSISTENCY_REVIEW.md`](docs/CONSISTENCY_REVIEW.md)
- **Audio Contract Flags**: [`docs/AUDIO.md`](docs/AUDIO.md)
- **Edit & Verified Post-Production**: [`docs/EDIT_AND_POST.md`](docs/EDIT_AND_POST.md)
- **Library Asset Validation**: [`docs/ASSETS.md`](docs/ASSETS.md)
- **Blender Assets & Motion Controls**: [`docs/BLENDER.md`](docs/BLENDER.md)
- **Indexed Production Workflows**: [`docs/PRODUCTION_WORKFLOWS.md`](docs/PRODUCTION_WORKFLOWS.md)
- **Versioned Remote Jobs & API Guide**: [`docs/REMOTE_DATA_PLANE.md`](docs/REMOTE_DATA_PLANE.md), [`src/fsf/remote/templates/AGENT_GUIDE.md`](src/fsf/remote/templates/AGENT_GUIDE.md)
- **Live Verification & Provider Setup**: [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md)
- **Release Notes & Changelogs**: [`docs/RELEASE.md`](docs/RELEASE.md)
- **Governance, Security & Agent Rules**: [`docs/DATA_GOVERNANCE.md`](docs/DATA_GOVERNANCE.md), [`SECURITY.md`](SECURITY.md), [`AGENTS.md`](AGENTS.md)
- **Runtime Pins**: [`compatibility/pdx_official_pin_rc7_a8_a3.json`](compatibility/pdx_official_pin_rc7_a8_a3.json)

## Installation and releases

RC15 is a candidate awaiting PyPI promotion; the PyPI block below still
selects the latest verified PyPI release until promotion completes. For RC15
changes and accepted limits, see [release notes](docs/RELEASE.md#release-notes-010rc15).

<!-- pypi-release-status:start -->
Source version in this branch: **`0.1.0rc15`**.

Latest verified PyPI release: **[`0.1.0rc14`](https://pypi.org/project/free-studio-flow/0.1.0rc14/)**,
published from tag **[`v0.1.0rc14`](https://github.com/freestudioflow/free-studio-flow/releases/tag/v0.1.0rc14)**
at commit `fa63ee91f55ddb5c0be1c9b215657497ba598be4`.

```powershell
python -m pip install "free-studio-flow==0.1.0rc14"
```
<!-- pypi-release-status:end -->

For editable contributor installs from a source checkout (Unix: `.venv/bin/python`;
do not activate the venv and rely on a bare `python` or `fsf` from PATH):

```text
python -m venv .venv
.venv/Scripts/python -m pip install -U pip
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m fsf.cli check
```

Repository: [github.com/freestudioflow/free-studio-flow](https://github.com/freestudioflow/free-studio-flow).
Runtime pins: `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, `pdx-adapter-media==0.2.0a3`.
This tree is the FSF product; it does **not** ship the B-roll generator
([b-roll-library-generator](https://github.com/pdx-experimental/b-roll-library-generator)).

## Verify

Run the contributor verification suite from the checkout:

```text
.venv/Scripts/python scripts/agent_verify.py
```

This runs public hygiene, offline pytest, live Kaggle/API pytest, and
`.venv/Scripts/python -m fsf.cli live-check`. Kaggle authentication uses
`KAGGLE_API_TOKEN`, an authorized host credential binding, or legacy credentials
(`KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json`). Missing usable credentials
**fail** the live gate; never skip it.

Vendor API / extra backends use process environment variables (`FSF_VENDOR_API_KEY`,
or `key_env` in `fsf-executors.json`) or host credential bindings; FSF never
loads `.env` files, and secrets must never enter JSON, git, or `AGENTS.local.md`.
See [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md).

## CLI

Windows paths use `.venv/Scripts/python -m fsf.cli ...` (Unix/macOS: `.venv/bin/python -m fsf.cli ...`).
Full operational sequence: [`AGENTS.md`](AGENTS.md).

| Command | Purpose | Details |
|---|---|---|
| `demo [--handoff] --out <dir>` | Generate playable 4s synthetic MP4 (`conforms`) | [Getting Started](docs/GETTING_STARTED.md) |
| `status <out_dir> [--json]` | Read-only state, blockers, and suggested `next_action` (`argv` is advisory) | Exit 0 ok / 1 unreadable |
| `verify --job <out_dir>` / `verify <clip>` | Verify job contract (`--artifact` / `--plan` optional) or standalone clip | [Getting Started](docs/GETTING_STARTED.md) |
| `sheet new` / `intake` / `plan` / `execute` | Author DOCX, compile `pdx_execution_plan_v1`, run DAG with resume | [Consistency Review](docs/CONSISTENCY_REVIEW.md) |
| `workorder list\|input\|fulfill` | Inspect active workorders, check continuity input, publish receipt | [Handoff](docs/HANDOFF.md) |
| `review list\|status\|approve\|reject` | Manage operator shot approvals (`--needs-rework` supported) | [Consistency Review](docs/CONSISTENCY_REVIEW.md) |
| `edit` / `edit audio` | Versioned edits, verified post-production, 48 kHz audio mix, A/B | [Edit & Post](docs/EDIT_AND_POST.md) |
| `blender` / `asset` | Controlled Blender scenes, imported controls, immutable assets | [Blender](docs/BLENDER.md), [Assets](docs/ASSETS.md) |
| `production` | Indexed SQLite production stores, chunk transfer, long controls | [Production Workflows](docs/PRODUCTION_WORKFLOWS.md) |
| `remote` / `api` | Opt-in parent/worker recipes (`remote templates --out config`) and profiles | [Remote Data Plane](docs/REMOTE_DATA_PLANE.md) |
| `flows` / `flow run --shape` / `run` / `live-check` | Single-skill shape stubs (`.shape.bin`), `mock.studio.video` (`.mock.bin`), live gate | [Live Verify](docs/FSF_LIVE_VERIFY.md) |

## Capabilities and boundaries

- Local synthetic demo, DOCX planning, `execute` resume, `status --json`, `verify --job`, Handoff workorders, and review gating are tested on Windows and Linux with local `ffmpeg`/`ffprobe`.
- `fsf run` defaults to `mock.studio.video` (`.mock.bin`) and `fsf flow run --shape` runs shape-only contract stubs (`.shape.bin`); do not run media `verify` on stub outputs.
- Remote recipes (`fsf remote`), GPU slots, API profiles (`fsf api`), and Blender/imported controls enforce declared limits (including the 64 MiB native archive limit) and keep human quality/listening/motion checks separate (`not_reviewed`). Successful transport or digest checks do not establish real model inference or visual consistency.
- Historical release additions (`rc3` through `rc14`) and qualification status live in [`docs/RELEASE.md`](docs/RELEASE.md).

License: Apache-2.0. See [LICENSE](LICENSE).
