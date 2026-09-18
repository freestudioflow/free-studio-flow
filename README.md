# free-studio-flow

AI-native film factory and Studio flows on [ProDocuX](https://github.com/prodocux/prodocux) contracts.

License: Apache-2.0. See [LICENSE](LICENSE). Public identity: **FSF Team** `<info@prodocux.com>`.

<!-- pypi-release-status:start -->
Source version in this branch: **`0.1.0rc1`**.

Latest verified PyPI release: **[`0.1.0rc1`](https://pypi.org/project/free-studio-flow/0.1.0rc1/)**,
published from tag **[`v0.1.0rc1`](https://github.com/freestudioflow/free-studio-flow/releases/tag/v0.1.0rc1)**
at commit `124b64aff89444f39086fe1297f9a073db7e5dcd`.

```powershell
python -m pip install "free-studio-flow==0.1.0rc1"
```
<!-- pypi-release-status:end -->

Repository: [github.com/freestudioflow/free-studio-flow](https://github.com/freestudioflow/free-studio-flow). This tree is the FSF product. Machine-readable PDX freeze and pin records live under `compatibility/`. It does **not** ship the B-roll generator; that remains [b-roll-library-generator](https://github.com/prodocux/b-roll-library-generator). Do not grow FSF features in B-roll. Construction plans and implementation diaries stay out of this public tree.

Runtime pins: `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, `pdx-adapter-media==0.2.0a3`.

## What this checkout contains

- **FSF-M1–M4**: Word intake, execution plan / resume, media conformance consume, multi-executor
- **FSF-S**: fourteen Studio skills as CLI/skill (kaggle / api / local transports). No Studio desk in this release
- **Live gate**: Kaggle API ping + private job envelope + published PyPI pins + local transport shape. Live local-model ping is skipped when this machine has no useful Comfy/GPU. GPU kernel submit is opt-in (`FSF_KAGGLE_SUBMIT=1`) and off by default. Extra API/local backends: `fsf-executors.example.json`

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

Same command for Cursor, Codex, Antigravity, and operators. See [`AGENTS.md`](AGENTS.md).

```text
.venv/Scripts/python scripts/agent_verify.py
```

That runs public hygiene, offline pytest, then live Kaggle/API pytest, then `.venv/Scripts/python -m fsf.cli live-check`.  
Kaggle credentials: `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json`. The key is never printed. Missing credentials **fail** the live gate; do not skip.

`.venv/Scripts/python -m fsf.cli run` defaults to **`mock.studio.video`** (writes `.mock.bin`, not media). `flow run` requires `--transport` and `--shape` and refuses mock executors. kaggle/api/local `execute_shot` writes `.shape.bin`. `verify` on those files is expected to fail.

Vendor API / extra backends: put tokens in process environment variables only (`FSF_VENDOR_API_KEY`, or the `key_env` name in `fsf-executors.json`). Do not put secrets in JSON, git, or `AGENTS.local.md`. FSF does not load `.env`. See [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md).

## CLI

Windows paths below; Unix: `.venv/bin/python`.

```text
.venv/Scripts/python -m fsf.cli check
.venv/Scripts/python -m fsf.cli flows
.venv/Scripts/python -m fsf.cli intake path/to/sheet.docx --out output/job
.venv/Scripts/python -m fsf.cli plan path/to/sheet.docx --pipeline p0
.venv/Scripts/python -m fsf.cli plan path/to/sheet.docx --pipeline custom --skills storyboard,generate,dub
.venv/Scripts/python -m fsf.cli flow run studio.video.generate path/to/sheet.docx --transport kaggle --shape
.venv/Scripts/python -m fsf.cli run path/to/sheet.docx --executor mock.studio.video
.venv/Scripts/python -m fsf.cli verify path/to/clip.mp4
.venv/Scripts/python -m fsf.cli live-check
```

`plan` writes the execution plan only. `run` executes generate only and defaults to mock (`.mock.bin`). Other skills use `flow run --transport … --shape`. Full sequence: [`AGENTS.md`](AGENTS.md).

## Docs

- Live verify: [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md)
- Release / PyPI: [`docs/RELEASE.md`](docs/RELEASE.md)
- Security: [`SECURITY.md`](SECURITY.md)
- Data governance: [`docs/DATA_GOVERNANCE.md`](docs/DATA_GOVERNANCE.md)
- Runtime pins: [`compatibility/pdx_official_pin_rc7_a8_a3.json`](compatibility/pdx_official_pin_rc7_a8_a3.json)
