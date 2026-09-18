# FSF-S actual-work verification (Kaggle + API + local transport shape)

**Audience**: local self-test, Codex, Antigravity, and other agents. One command set; do not invent a second plan.

Offline shape tests are not FSF-S complete. At minimum:

1. **Kaggle work**: credentials work, Kaggle API can list this account’s kernels, intake writes a **private** job envelope, no secret leak. Do **not** submit a 12h GPU job by default.
2. **API**: public PyPI JSON confirms `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, and `pdx-adapter-media==0.2.0a3` are published. Hit a vendor endpoint only when `FSF_VENDOR_API_*` is set.
3. **Local transport**: `local.*` executors for all fourteen skills must exist. The live gate runs **generate shape** (deterministic artifact + `pdx_tool_result_v1`). When this machine has no useful Comfy / GPU, **skip the live model ping**; that is not a missing local feature.
4. **Plural API / local**: the catalog has one default transport executor per skill; operators may add more `api.*` / `local.*`. Real tokens live in **environment variables only**. See “How to add tokens” below.

Entry: [`AGENTS.md`](../AGENTS.md) and `.venv/Scripts/python scripts/agent_verify.py` (Unix: `.venv/bin/python`).

Kaggle credentials: `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json`. The live gate runs `kaggle kernels list --mine --page-size 1` and does **not** default to `kernels push`.

## How to add tokens

JSON / git / manifests / `AGENTS.local.md` **never store secrets**. FSF reads process environment variables and does **not** auto-load `.env`. `.venv/Scripts/python -m fsf.cli executors` shows which ids become `[Available]`. Reports and logs must not print keys.

### Built-in defaults (one API, one local)

| Use | Environment variable | Notes |
|---|---|---|
| Vendor / cloud API | `FSF_VENDOR_API_BASE` + `FSF_VENDOR_API_KEY` | `live-check` pings the vendor (Bearer) only when set. PyPI must still pass |
| Local Comfy / GPU | `COMFYUI_AVAILABLE=1` | Set only when the local backend is actually up. Local transport shape does not depend on this variable |
| Kaggle | `KAGGLE_CONFIG_DIR` or `~/.kaggle/kaggle.json` | Do not put this in extras JSON |
| Hugging Face (local downloads) | `HF_TOKEN` | OS environment only; do not write it into JSON |

PowerShell (current window):

```powershell
$env:FSF_VENDOR_API_BASE = "https://api.example.com"
$env:FSF_VENDOR_API_KEY  = "<token>"
$env:COMFYUI_AVAILABLE   = "1"
```

To persist: Windows Environment Variables → User variables, or:

```powershell
[System.Environment]::SetEnvironmentVariable("FSF_VENDOR_API_KEY", "<token>", "User")
[System.Environment]::SetEnvironmentVariable("FSF_VENDOR_API_BASE", "https://api.example.com", "User")
```

sh:

```text
export FSF_VENDOR_API_BASE="https://api.example.com"
export FSF_VENDOR_API_KEY="<token>"
export COMFYUI_AVAILABLE=1
```

### Adding more backends

1. Copy [`fsf-executors.example.json`](../fsf-executors.example.json) to gitignored `fsf-executors.json` (or set `FSF_EXECUTORS_FILE` / `FSF_EXTRA_EXECUTORS`).
2. JSON stores env **names** only (`key_env`, `endpoint_env`, `require_env`). Fields named `api_key` / `token` are refused.
3. Put the real token in that environment variable.
4. `.venv/Scripts/python -m fsf.cli flow run <skill> <sheet.docx> --transport api|local --executor <id> --shape`

```json
{
  "executor_id": "api.custom.video",
  "transport": "api",
  "tool_name": "studio.video.generate",
  "key_env": "FSF_VENDOR_API_KEY_CUSTOM",
  "endpoint_env": "FSF_VENDOR_API_BASE_CUSTOM"
}
```

```powershell
$env:FSF_VENDOR_API_KEY_CUSTOM  = "<token>"
$env:FSF_VENDOR_API_BASE_CUSTOM = "https://api.example.com"
```

A second local Comfy usually has no vendor token; use a flag plus an address:

```json
{
  "executor_id": "local.comfy.secondary",
  "transport": "local",
  "tool_name": "studio.video.generate",
  "require_env": "COMFYUI_SECONDARY_AVAILABLE",
  "endpoint": "http://127.0.0.1:8189"
}
```

```powershell
$env:COMFYUI_SECONDARY_AVAILABLE = "1"
```

Gitignored `AGENTS.local.md` may list which variable names and interpreter this machine uses. Still do not paste tokens.

Extras tokens currently mark “this backend is configured and selectable”. `flow run` still requires `--shape`; execute still writes deterministic `.shape.bin` and does not POST extras keys. Built-in `FSF_VENDOR_API_KEY` already does an optional ping in `live-check`. Do not treat shape artifacts as valid media.

## How agents run the same gate

From the repo root, on the venv interpreter (Unix: `.venv/bin/python`):

```text
.venv/Scripts/python scripts/agent_verify.py
```

Codex / Antigravity paste [`scripts/agent_verify_prompt.txt`](../scripts/agent_verify_prompt.txt). Codex `workspace-write` has no outbound network by default; live `kaggle kernels list --mine` fails until network is allowed.

Run Antigravity in an agent chat that **already has this repo open**. Headless `agentapi` needs the IDE CSRF token; an unauthenticated external call must not be treated as a pass.

## 2026-09-18 result

Cursor / local, Codex, and Antigravity all ran `.venv/Scripts/python scripts/agent_verify.py`: offline pytest, live Kaggle/API pytest, and `.venv/Scripts/python -m fsf.cli live-check` PASS. Local transport shape must pass; live local-model ping is skipped; no GPU submit.
