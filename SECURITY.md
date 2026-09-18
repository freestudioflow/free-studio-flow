# Security

This repository must stay free of secrets, local paths, and private account identifiers.
The bar matches the ProDocuX labs public-export gate and the B-roll generator:
source, tests, pins, and interface docs only.

## Never commit

- `kaggle.json`, `HF_TOKEN`, `hf_*`, cloud keys, `.env`
- Personal Kaggle usernames or filled kernel metadata
- Absolute workstation paths (Windows user profiles and private lab drive roots)
- Generated `output/`, live-check scratch, private Word sheets
- Customer documents (tests create synthetic DOCX at runtime)

## Credentials

`.venv/Scripts/python -m fsf.cli live-check` and `scripts/agent_verify.py` may report that a Kaggle username exists
and which config **folder** was used. They must never print the API key.
Kaggle jobs built by this tree stay `is_private: true`. Do not set `FSF_KAGGLE_SUBMIT=1`
unless an operator explicitly wants a private GPU kernel.

Look for `kaggle.json` only in:

- `KAGGLE_CONFIG_DIR` (if set), then
- `%USERPROFILE%\.kaggle` / `~/.kaggle`

`scripts/check_public_hygiene.py` scans **git-tracked** files for workstation paths,
secret-like tokens, denied names/suffixes, oversized files, and
symlinks/junctions. `scripts/agent_verify.py` runs that scan first.

Vendor API keys and extra backend tokens belong in the process environment
(`FSF_VENDOR_API_KEY`, or the `key_env` name registered in gitignored
`fsf-executors.json`). The JSON file may store environment **names** and public
endpoints only. Do not embed `api_key` / `token` fields. FSF does not auto-load
`.env`. Operator notes in `AGENTS.local.md` must not contain secret values.
See [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md).

## Scan before any push

From the repo root:

```text
.venv/Scripts/python scripts/check_public_hygiene.py
```

Unix: `.venv/bin/python`. Treat a hit as a release blocker. Do not commit or push until it is clean.

## Reporting

Report suspected vulnerabilities privately to FSF Team at info@prodocux.com before public
disclosure. Include the affected revision, reproduction steps, and impact.
Do not attach `kaggle.json`, tokens, or customer media.
