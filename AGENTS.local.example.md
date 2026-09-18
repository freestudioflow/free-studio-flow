# Operator-only overlay (copy to AGENTS.local.md; that file is gitignored)

Use the interpreter for this checkout. Do not commit this file.
List **environment variable names** here if useful. Never paste tokens, Kaggle keys, or HF tokens.

How to set real secrets: [`docs/FSF_LIVE_VERIFY.md`](docs/FSF_LIVE_VERIFY.md) (section How to add tokens). FSF reads `os.environ` only; it does not load `.env`.

```text
# optional names (values live in the OS environment, not in this file)
# KAGGLE_CONFIG_DIR
# FSF_VENDOR_API_BASE
# FSF_VENDOR_API_KEY
# COMFYUI_AVAILABLE
# FSF_EXECUTORS_FILE
# extra key_env names from fsf-executors.json, e.g. RUNWAY_API_KEY
```

Kaggle credentials: `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json`. Extra API/local backends: gitignored `fsf-executors.json` (copy `fsf-executors.example.json`); JSON holds env **names** only.

Construction notes stay in gitignored `internal/`. Do not copy them back into `docs/` or `compatibility/`.
