# Agent verification gate (Cursor / Codex / Antigravity)

Read this file before changing the tree. Before pushing the FSF core, FSF-S must pass **actual-work verification**: Kaggle work, API, and **local transport shape**. When this machine has no useful local model, **skip live local inference**, but the local capability must exist; do not treat it as unimplemented.

Put workstation absolute paths and the private venv in gitignored `AGENTS.local.md`. Do not write them here.

Always use the **venv** interpreter. Do not `activate` and then call a bare `python` / `fsf` from PATH:

- Windows: `.venv/Scripts/python`
- Unix: `.venv/bin/python`

Commands below use Windows paths; on Unix replace `Scripts` with `bin`.

## Film-assembly cheat sheet

When assembling a job from operator intent, **do not invent CLI flags and do not edit Kernel / Word XML**. Standard sequence:

1. **Confirm input**: use the operator prompt sheet `.docx` when present; otherwise use a synthetic sheet (`live-check` builds one, or write one shot per paragraph with `python-docx`, separated by `-----`).
2. **Compile the job** (single source of truth): `.venv/Scripts/python -m fsf.cli intake <sheet.docx> --out <out_dir>`
3. **Plan** (writes `pdx_execution_plan_v1` only; does not execute):
   - Ladder: `.venv/Scripts/python -m fsf.cli plan <sheet.docx> --pipeline generate|p0|p1|p2|p3`
   - Chosen skills (skip VFX, generate + dub only, etc.): `.venv/Scripts/python -m fsf.cli plan <sheet.docx> --pipeline custom --skills storyboard,generate,dub`
   - `--skills` accepts canonical names (`studio.voice.dub`) or aliases (`storyboard`, `generate`, `dub`, `music`, `sfx`, `vfx`, `remaster`, `lora`, …). Setting `--skills` overrides the ladder.
4. **Execute**:
   - `.venv/Scripts/python -m fsf.cli run <sheet.docx>` **runs generate only**. Default executor is **`mock.studio.video` (`.mock.bin`, not media)**. Switching to kaggle / api / local requires `--shape`. There is **no** `fsf run --pipeline p1`.
   - Each skill in the plan: `.venv/Scripts/python -m fsf.cli flow run <skill> <sheet.docx> --transport kaggle|api|local --shape`
   - `--transport` is **required**. `--shape` is **required** (including with `--executor`). `flow run` **refuses** `mock.studio.video`. Media-class artifacts are `.shape.bin`, not valid mp4 / png. `--live` is refused.
   - `--executor` may override the catalog default (including operator-added plural `api.*` / `local.*`) and must not override to mock.
   - Example: `.venv/Scripts/python -m fsf.cli flow run studio.storyboard.first_frame <sheet.docx> --transport local --shape`
5. **Quality check**: `.venv/Scripts/python -m fsf.cli verify <output.mp4>` (consume PDX media conformance; do not write ffprobe). **Do not** run verify on `.shape.bin` / `.mock.bin` and treat that as a successful generate.
6. **Catalog**: `.venv/Scripts/python -m fsf.cli flows` lists fourteen skills; transport status is `shape`, not live `implemented`.

Do not write a standalone SDK script that bypasses the CLI unless the operator explicitly asks. Do not guess `--pipeline` values that do not exist.

## Forbidden

- Do not edit the PDX Kernel / Engine worktrees
- Do not push B-roll; do not grow FSF features in B-roll
- Do not build Studio desk / Farpals UI
- Do not write tokens, Kaggle keys, or HF tokens into manifests, logs, or commits
- Do not commit `internal/` (gitignored construction notes, candidate reviews, implementation diaries)
- Do not publish a public Kaggle kernel (`is_private` must be true)
- Do not publish a public Kaggle kernel (`is_private` must be true)
- Do not submit a 12-hour GPU kernel by default (unless the operator explicitly sets `FSF_KAGGLE_SUBMIT=1`)

## Required gate (same suite for every agent)

Working directory: this repository root.  
Python: the interpreter for this checkout. Create it with `python -m venv .venv`, then use **that same** `.venv` interpreter (Windows: `.venv/Scripts/python`; Unix: `.venv/bin/python`) for `pip install -e ".[dev]"` and the commands below. Do not use a global `python`.

```text
.venv/Scripts/python scripts/agent_verify.py
```

Codex / Antigravity use the same prompt: `scripts/agent_verify_prompt.txt`. Do not invent a second test plan.

Equivalent split:

```text
.venv/Scripts/python -m pytest tests -m "not live" -v
.venv/Scripts/python -m pytest tests/test_live_kaggle_api.py -m live -v
.venv/Scripts/python -m fsf.cli live-check
.venv/Scripts/python scripts/check_public_hygiene.py
```

| Gate | Proves | Does not prove |
|---|---|---|
| `scripts/check_public_hygiene.py` | Tracked tree: absolute paths, secret patterns, denied names / suffixes, large files, symlinks, top-level allowlist | gitignored operator files |
| `pytest -m "not live"` | M1–M4 spine, fourteen skill **shapes** (including local), plural extras, CLI refusal of fake success, private Kaggle job envelope | Real network, real GPU |
| `pytest -m live` and `.venv/Scripts/python -m fsf.cli live-check` | Kaggle API `kernels list --mine`, private job envelope, PyPI rc7 / a8 / a3, local transport shape | Real Comfy HTTP / real local GPU inference |

A failure means FSF-S is not verified. Missing `kaggle.json` or unreachable Kaggle / PyPI must **fail the live gate**; do not skip and pretend pass.

## Environment

Credential details (including plural extras): [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md) section “How to add tokens”. Summary:

- `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json` (`%USERPROFILE%\.kaggle\kaggle.json`)
- Optional vendor API: `FSF_VENDOR_API_BASE` + `FSF_VENDOR_API_KEY` (vendor ping only when set; PyPI must still pass)
- Optional local Comfy: `COMFYUI_AVAILABLE=1` (set only when the backend is actually up)
- Optional plural API / local: copy `fsf-executors.example.json` to gitignored `fsf-executors.json`, or set `FSF_EXECUTORS_FILE` / `FSF_EXTRA_EXECUTORS`. JSON stores env **names** only. Optional: `.venv/Scripts/python -m fsf.cli flow run … --transport api --executor api.custom.video --shape`
- `AGENTS.local.md` lists variable names and the interpreter only; never paste tokens
- Required package: `kaggle` CLI (`pip install kaggle`, already in `pyproject.toml`)

## Pass look

- Offline pytest all green
- Live: `kaggle.ok == true`, `api.ok == true`, `local.shape_ok == true`, `local.live_model.skipped == true`
- `.venv/Scripts/python -m fsf.cli flows` transports are `shape`; `flow run` without `--shape` (including a mock `--executor`) must fail
- Job `is_private == true`; reports contain no API key
