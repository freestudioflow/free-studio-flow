# FSF-S actual-work verification (Kaggle + API + local transport shape)

This guide covers backend configuration and contributor verification.

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

A local external CLI / script process executor can be defined using `kind: "process"` and `command_args`:

```json
{
  "executor_id": "local.process.generate",
  "transport": "local",
  "tool_name": "studio.video.generate",
  "kind": "process",
  "command_args": [
    "python",
    "/opt/fsf-tools/sample_worker.py",
    "--shot",
    "{shot_id}",
    "--out",
    "{output}"
  ],
  "timeout_seconds": 180.0
}
```

This is a configuration template: install your worker at the chosen absolute path and replace `python` with the intended interpreter. The worker is not included in this package. Process executors isolate output files in temporary scratch directories; the process inherits the caller working directory. Supported placeholders are `{shot_id}`, `{prompt}`, `{output}`, `{input}`, `{input_video}`, `{duration}`, and `{duration_int}`. Errors and timeouts redact known secrets, and successful artifacts are published atomically. This is output isolation, not an operating-system security sandbox.

Gitignored `AGENTS.local.md` may list which variable names and interpreter this machine uses. Still do not paste tokens.

API/local shape adapters mark a backend as configured and selectable; they do not perform real generation. `flow run` still requires `--shape`. For real media, compile with `plan --media` and use `execute` with a configured process executor whose worker produces the declared media artifact. Process workers run with the operator account permissions and may invoke external services; configure only trusted commands. Built-in `FSF_VENDOR_API_KEY` performs an optional ping in `live-check`. Do not treat shape artifacts as valid media.

## Run the verification suite

From the repo root, on the venv interpreter (Unix: `.venv/bin/python`):

```text
.venv/Scripts/python scripts/agent_verify.py
```

The live gate requires network access to Kaggle and PyPI. A network or
authentication failure is a failed gate, not a successful verification.
