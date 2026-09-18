# Data governance

This public tree is a labs-style **file allowlist**, not a folder dump. Tracked
paths must be listed in `scripts/check_public_hygiene.py` (`PUBLIC_FILES` and
`PUBLIC_DIR_PREFIXES`: `src/`, `tests/`, `scripts/`).

Allowed documentation is only:

- `README.md`, `AGENTS.md`, `AGENTS.local.example.md`, `SECURITY.md`
- `docs/RELEASE.md`, `docs/FSF_LIVE_VERIFY.md`, `docs/DATA_GOVERNANCE.md`

Allowed machine records are only:

- `compatibility/pdx_official_pin_rc7_a8_a3.json`
- `compatibility/pdx_conformance_freeze_v1.json`
- `compatibility/pdx_contract_seal_input_v1.json`

Construction notes, candidate reviews, and implementation diaries belong in
gitignored `internal/` (local only). They are not part of the public package.

The tree does **not** contain:

- operator Kaggle tokens or usernames
- customer prompt sheets or generated media
- unpublished lab checkouts, private venvs, or workstation layout
- hackathon strategy, pitch decks, or internal review prompts
- construction plans, implementation diaries, candidate-review notes, or CJK docs

Live artifacts belong under `output/` (gitignored). Tests build temporary DOCX in pytest
`tmp_path`. Kaggle credentials stay in the operator environment (`KAGGLE_CONFIG_DIR` or
`~/.kaggle`). Vendor API and extra local/API tokens stay in environment variables;
`fsf-executors.json` is gitignored and must list env **names** only.

GitHub destination: [github.com/freestudioflow/free-studio-flow](https://github.com/freestudioflow/free-studio-flow).
Public identity: **FSF Team** `<info@prodocux.com>`.
The first public package is pre-release `0.1.0rc1` while PDX pins remain rc/a.
GitHub Releases are the approval boundary; PyPI uses Trusted Publishing
(workflow `release.yml`, environment `pypi`). See [`docs/RELEASE.md`](RELEASE.md).
