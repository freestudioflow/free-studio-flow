# FSF-S actual-work verification (Kaggle + API + local transport shape)

This guide covers backend configuration and contributor verification.

Offline shape tests are not FSF-S complete. At minimum:

1. **Kaggle work**: credentials work, Kaggle API can list this account’s kernels, intake writes a **private** job envelope, no secret leak. Do **not** submit a 12h GPU job by default.
2. **API**: public PyPI JSON confirms `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, and `pdx-adapter-media==0.2.0a3` are published. Hit a vendor endpoint only when `FSF_VENDOR_API_*` is set.
3. **Local transport**: `local.*` executors for all fourteen skills must exist. The live gate runs **generate shape** (deterministic artifact + `pdx_tool_result_v1`). When this machine has no useful Comfy / GPU, **skip the live model ping**; that is not a missing local feature.
4. **Plural API / local**: the catalog has one default transport executor per skill; operators may add more `api.*` / `local.*`. Legacy extras use process environment variables. Typed model profiles can also use host credential bindings. See “How to add tokens” below.

Entry: [`AGENTS.md`](../AGENTS.md) and `.venv/Scripts/python scripts/agent_verify.py` (Unix: `.venv/bin/python`).

Kaggle credentials: `KAGGLE_API_TOKEN`, `~/.kaggle/access_token` (or
`access_token.txt`), legacy `KAGGLE_USERNAME` + `KAGGLE_KEY`, or
`KAGGLE_CONFIG_DIR` / `~/.kaggle/kaggle.json`. Legacy credentials remain
supported. Configuration presence is reported separately from authentication;
the live gate runs `kaggle kernels list --mine --page-size 1` and does **not**
default to `kernels push`. Credential reports omit account names and directories.

## How to add tokens

JSON / git / manifests / `AGENTS.local.md` **never store secrets**. FSF reads process environment variables and does **not** auto-load `.env`. `.venv/Scripts/python -m fsf.cli executors` shows which ids become `[Available]`. Reports and logs must not print keys.

### Built-in defaults (one API, one local)

| Use | Environment variable | Notes |
|---|---|---|
| Vendor / cloud API | `FSF_VENDOR_API_BASE` + `FSF_VENDOR_API_KEY` | `live-check` pings the vendor (Bearer) only when set. PyPI must still pass |
| Local Comfy / GPU | `COMFYUI_AVAILABLE=1` | Set only when the local backend is actually up. Local transport shape does not depend on this variable |
| Kaggle | `KAGGLE_API_TOKEN`, token file, or legacy config | Do not put credential values in extras JSON |
| Hugging Face (local downloads) | `HF_TOKEN` | OS environment only; do not write it into JSON |

For Windows, open **Edit environment variables for your account**, add the
credential under **User variables**, and confirm the dialogs. Open a new terminal,
or load the named value into the current PowerShell process without typing it
into command history:

```powershell
$env:FSF_VENDOR_API_BASE = "https://api.example.com"
$env:FSF_VENDOR_API_KEY = [Environment]::GetEnvironmentVariable("FSF_VENDOR_API_KEY", "User")
$env:COMFYUI_AVAILABLE   = "1"
```

On Unix, populate the token through the host credential facility before
launching FSF. Non-secret flags and URLs can be exported normally:

```text
export FSF_VENDOR_API_BASE="https://api.example.com"
export COMFYUI_AVAILABLE=1
```

### Adding more backends

#### Kaggle host credential binding

For a host Secret Manager or OS credential store, set
`FSF_KAGGLE_CREDENTIAL_BINDING_FILE` to a private binding file using the
existing `fsf_credential_binding_v1` format. Its `profile_id` must be `kaggle`.
Supported sources are `environment`, `os_credential_store`, and
`host_secret_manager`; authorization must be explicit. The binding contains
only a secret reference, revision and approved resolver command, never a token.
See the credential binding format in the provider setup section below.

FSF resolves the token for one child call. It does not modify the parent
environment or fall back to another account's legacy/OAuth files when an
explicit binding is used. Resolver errors and expired bindings fail closed.
Inspecting configuration does not invoke the resolver or prove authentication.

#### Executor configuration

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
$env:FSF_VENDOR_API_KEY_CUSTOM = [Environment]::GetEnvironmentVariable("FSF_VENDOR_API_KEY_CUSTOM", "User")
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

This is a configuration template: install your worker at the chosen absolute path and replace `python` with the intended interpreter. The worker is not included in this package. Process executors isolate output files in temporary scratch directories; the process inherits the caller working directory. Supported placeholders are `{shot_id}`, `{prompt}`, `{output}`, `{input}`, `{input_video}`, `{duration}`, `{duration_int}`, and `{generation_attempt}`. Errors and timeouts redact known secrets, and successful artifacts are published atomically. This is output isolation, not an operating-system security sandbox.

Gitignored `AGENTS.local.md` may list which variable names and interpreter this machine uses. Still do not paste tokens.

API/local shape adapters mark a backend as configured and selectable; they do not perform real generation. `flow run` still requires `--shape`. For real media, compile with `plan --media` and use `execute` with a configured process executor whose worker produces the declared media artifact. Process workers run with the operator account permissions and may invoke external services; configure only trusted commands. Built-in `FSF_VENDOR_API_KEY` performs an optional ping in `live-check`. Do not treat shape artifacts as valid media.

## Run the verification suite

From the repo root, on the venv interpreter (Unix: `.venv/bin/python`):

```text
.venv/Scripts/python scripts/agent_verify.py
```

The live gate requires network access to Kaggle and PyPI. A network or
authentication failure is a failed gate, not a successful verification.

## Provider setup and recoverable generation

Use the host's credential facility. Never paste keys into conversations, recipes,
workorders, notebooks or command arguments. A managed agent may have its own
secret manager; it need not export keys to a workstation.

### Offline setup

```text
fsf api setup --provider nebius --model MODEL_ID --out config/nebius
fsf api setup --provider google --model MODEL_ID --out config/google
fsf api setup --provider bedrock --model MODEL_ID --region REGION --reference AWS_PROFILE_NAME --out config/bedrock
```

These commands create unauthorized `profile.json`, `binding.json` and
`guidance.json` without connecting. Existing directories are never overwritten.
Native templates cover text. For media use `--provider custom` and
`--capability image|video|audio|upscale`, then configure actual endpoints,
parameters and result mapping. Text compatibility does not imply media support.

Environment bindings reference a variable name. Bedrock bindings reference an
AWS profile, or `default` for the SDK default chain; configure role/SSO, model
access, region, quotas and billing separately. A model may require an inference
profile. Enable both files' `authorized` fields after operator approval.
The optional `credentials` extra enables `os_credential_store` via keyring, using
service `fsf/PROFILE_ID` and the binding reference as its account entry.

### Local credential names

| Provider | Credential source | Additional configuration |
| --- | --- | --- |
| Nebius Token Factory | `NEBIUS_API_KEY` | Accessible text model ID and available account credit |
| Google Gemini API | `GEMINI_API_KEY` | Accessible model and project API/billing permissions |
| AWS Bedrock | `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, or an existing SDK profile/role/SSO session | Region and model/inference-profile ID |

Temporary AWS credentials also require `AWS_SESSION_TOKEN`. Do not combine an
old session token with a new long-term key. With binding reference `default`,
the SDK can use process credentials; a named reference selects that AWS profile.
`AWS_DEFAULT_REGION` can configure the host SDK, but the FSF profile's `region`
must still be explicitly set. Install the optional `api` extra for Bedrock.
Credentials stored only inside another editor are not automatically available
to FSF. FSF does not automatically import Windows User variables into an
already-running process; start a new terminal or explicitly reload the intended
names there. Never print their values to confirm setup.

For a managed agent, use its own Secret Manager or approved proxy as described
below; do not assume it can read a workstation's environment or AWS files.

### Host secret managers

No universal Muse secret command is assumed. Configure the host's actual
integration with `--source host_secret_manager --reference SECRET_NAME
--resolver-config argv.json`. This JSON array contains a trusted executable and
fixed arguments, provided by the operator, never by a workorder.

FSF appends `--reference SECRET_NAME`. Within ten seconds the resolver must
return bounded stdout JSON with `value`, opaque `revision`, and `expires_at`
(Unix timestamp). Revision must match the binding and expiry must be in the
future. Do not save a real resolver response: its value stays inside the runtime
credential scope, without changing global environment variables.

A host prohibiting key export can use an authorized `json_http_v1` proxy profile
with `auth_header: none` and a `host_proxy` binding. Its endpoint and security are
operator responsibilities. This is not native provider authentication. A child
worker uses its own credential facility, not secrets copied in a parent bundle.

### Deliberate verification

Create the `inputs` and `state` directories before the commands below. For a
small native text request, save this as `request.json`:

```json
{
  "schema_version": "fsf_model_api_request_v1",
  "request_id": "text_smoke_001",
  "prompt": "Reply only with OK.",
  "parameters": {"max_tokens": 32, "temperature": 0},
  "inputs": {},
  "expected_media_contract": null
}
```

```text
fsf api check --profile config/nebius/profile.json --binding config/nebius/binding.json
fsf api check --profile config/nebius/profile.json --binding config/nebius/binding.json --state state --probe
fsf api run request.json --profile config/nebius/profile.json --binding config/nebius/binding.json --inputs inputs --state state
fsf api verify state/<bundle>
```

Copy `<bundle>` from the run result. Verify takes the bundle root, not its
`accepted` subdirectory. Use a new request ID for a genuinely new job; do not
change an ID to bypass an uncertain submission.

Checking without `--probe` does not retrieve a secret or authenticate. A probe
does not prove inference. Bedrock Converse has no read-only probe here: use an
approved bounded inference. Limit text smoke tests to 32 output tokens; select
the smallest valid model settings and a separate budget for each media type.

Rejected connections are persisted in the selected state root. Calls do not
automatically repeat after authorization, quota or unknown failures. After
correcting configuration, `--recheck` explicitly reopens a check; a new opaque
binding revision establishes a distinct check. An uncertain submission still
requires status/reconciliation: recheck never repurposes an immutable request ID.
Only a validated collected artifact proves inference for that capability.

### Per-shot recovery

Save this explicitly authorized policy as `recovery.json`:

```json
{"schema_version":"fsf_generation_recovery_policy_v1","authorized":true,"max_attempts":2,"max_dispatches":12,"max_new_dispatches_per_run":3,"deadline_seconds":900}
```

```text
fsf execute plan.json --out output --recovery-policy recovery.json
fsf execute plan.json --out output --recovery-policy recovery.json --resume-paused
```

Start a recovery job in a fresh output directory. Policy, plan and execution
identity are bound; `--no-resume` is forbidden. Limits count video generation or
remaster attempts and durable dispatch intents, not currency or provider bills.
The deadline includes paused time and does not terminate a remote machine.

Each video producer needs directly bound technical QC with a freeze limit of at
most 0.5 seconds. Content QC failure archives the old attempt and starts a new
producer attempt, invalidating dependent QC, continuity, reviews and assembly.
Independent successful shots reuse their files. Contracts never relax; images
never substitute for video. Process arguments may use `{generation_attempt}`.

Batch boundaries return `paused_batch`. Handoff and asynchronous remote/API jobs
return `waiting_external`; rerunning the same command collects the same attempt.
Auth/quota pauses require correction and explicit `--resume-paused`. Unknown
submissions require investigation/reconciliation and cannot be blindly replayed.
Exhausted limits block delivery and preserve prior attempt records.

Freeze QC does not establish semantic motion: a panned still can look active.
Use provenance and consistency review for that requirement. Unperformed motion
or listening review remains `not_reviewed`. Work performed outside FSF's jobs is
outside this gate's authority.
