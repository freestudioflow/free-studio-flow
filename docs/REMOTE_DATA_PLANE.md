# Versioned remote jobs and global Blender segments

These commands describe this source checkout. Build the parent and worker from
the same source; do not substitute a published wheel that lacks this protocol.
The existing v1 transport remains compatible, including its Kaggle CPU policy.

## Configure the host

### Native Kaggle API and existing SSH disks

Use an authorized `fsf_kaggle_native_store_v1` configuration for provider-native
transfer. This staging format supports both
Kaggle API jobs and SSH disk jobs. It stages content-addressed bytes on the
parent host; it does not create a cloud storage service or require cloud keys.

```json
{
  "schema_version": "fsf_kaggle_native_store_v1",
  "store_id": "native_artifacts",
  "authorized": true,
  "root": "./remote-native-objects",
  "read_prefixes": ["jobs/"],
  "write_prefixes": ["jobs/"],
  "max_artifact_bytes": 67108864,
  "max_total_bytes": 67108864,
  "deadline_seconds": 900,
  "max_inline_notebook_bytes": 1048576
}
```

The `root` is host-local and excluded from portable data authority. Prepare
input and wheel references with `artifact-put --store native.json`, then use
the same `prepare-v2`, `dispatch-v2`, `status-v2` and `fetch-v2` commands below.
Kaggle profiles use `activation_mode: "api_push"` or omit that setting; omit
`worker_notebook`. Requests normally use a unique private notebook. To reuse an
operator-selected private notebook, set `native_notebook` to its slug. FSF
checks that the previous version is terminal, binds the next version and exact
source, and refuses ambiguous results. There is no browser step or per-notebook
Secret grant. A pinned `docker_image` must match the recipe's Python version.

The current native Kaggle path embeds a bounded compressed input bundle in
the submitted notebook. The complete encoded notebook must fit
`max_inline_notebook_bytes`; oversized inputs are rejected before submission.
This is a product limit, not a permanent Kaggle platform limit. Large tools
can be downloaded by an explicitly approved recipe from fixed vendor URLs,
with SHA verification. The normal 64 MiB bundle limit is not increased.

Result bytes are downloaded from native outputs, never reconstructed from
stdout. Request, child identity, SHA, frame mapping and QC checks still apply.
Provider cancellation can make native outputs unavailable; an absent result
is not successful recovery or a durable checkpoint. Completed segments already
accepted by the parent remain available for continuation.

SSH profiles use their existing `remote_root`, which may be on an attached
volume. Bundles are uploaded and results retrieved through the configured SSH
connection with strict host-key verification. No additional storage account is
required. Native content versions are SHA-256 identities, not cloud VersionIds.

### Multiple GPUs inside one worker

An approved recipe can run:

```text
python -m fsf.remote.gpu_schedule schedule.json --out schedule --archive gpu-batch.zip
```

This schedules tasks inside the existing worker; it does not rent machines or
submit nested cloud jobs. By default the task queue binds exclusive CUDA device
UUIDs, fills free devices as tasks finish, checks dependencies and limits retries.
The authorized count is configurable up to 16
and must be available on the worker. It is not inferred from a machine name.

For a Blender batch, declare `gpu_schedule` as a data input and bind its canonical
digest in `execution_requirements.schedule_digest`. The render device count is
the batch's authorized count; each task declares its own count. Name the batch
archive output `gpu_batch`. The worker and parent verify every Blender bundle,
device assignment, clock and complete frame coverage before acceptance. Command
success alone does not prove GPU rendering, model adherence or human approval.
Completed tasks are reused only after checking the same plan, selected devices
and output hashes. An unresolved running claim is rejected rather than repeated.

To share cards inside an approved recipe, add a `sharing` object to the schedule:

```json
{"max_tasks_per_device": 2, "memory_budget_mb": 12000, "headroom_mb": 2048}
```

Every task must then declare `vram_mb_per_device`, covering its model and working
memory. Both task slots and memory reservations must fit before it starts. A task
requiring several cards reserves all of them together. This combines card-level
dispatch with within-card concurrency, without changing the default exclusive
policy. The schedule digest and recipe approval bind these settings.

Shared mode also samples physical GPU memory and resident compute processes.
Missing measurements or exceeding the memory budget stops the batch. Sampled
peaks are not an allocator hard limit and may miss shorter spikes. Concurrency
does not guarantee a throughput increase. Model caching or serving several
models in one process remains the recipe's responsibility.

Creating `pause` in the schedule output directory drains running tasks and
retains verified completed outputs. Remove it before running the same schedule
again to continue pending work. Creating `cancel` requests process cancellation.
These files operate in the worker's existing filesystem; they do not guarantee
that a cancelled provider session will preserve its native outputs.

### Optional version-enabled object store

Install the `remote` extra to use the S3/SigV4 data plane. Each host has its own
private `fsf_artifact_store_v1` configuration. Configure a private, version-enabled
bucket and narrowly scoped credentials before submitting work. No bucket or Pod
is created by these commands.

| Store setting | Meaning |
|---|---|
| `store_id`, `endpoint`, `region`, `bucket` | Operator-selected storage authority; HTTPS is required |
| `authorized` | Explicit permission to use this store |
| `read_prefixes`, `write_prefixes` | Allowed namespaces, each ending in `/` |
| `max_artifact_bytes`, `max_total_bytes` | Object and manifest limits |
| `deadline_seconds` | Transfer budget, retained in incomplete transfer checkpoints |
| `credential_mode` | `aws_default_chain` or private credential `bindings` |
| `credential_bindings` | Host-local binding files for access key, secret key and optional session token; values are never stored here |

Default-chain hosts may use their existing AWS profile, role or native secret
manager bridge. Explicit bindings use `fsf_credential_binding_v1`, with profile
IDs `<store_id>.access_key`, `<store_id>.secret_key` and optional
`<store_id>.session_token`. Environment, OS credential store and host Secret
Manager resolution use the same binding protocol as other FSF APIs. Keep these
files and resolver settings private. A worker never receives an endpoint chosen
by an untrusted job.

```text
fsf remote artifact-probe --store private-store.json
```

The probe verifies connection and versioning. A successful probe is not a
successful transfer, GPU test or model inference.

## Prepare tools and recipes

The environment plan schema is
[`fsf_environment_plan_v1`](../src/fsf/schemas/fsf_environment_plan_v1.schema.json).
Declare the intended Python variant and exact OS/architecture. Each tool is
either preinstalled or supplied as a pinned archive. Archive SHA, size,
extraction limit, executable, version check and smoke command are explicit.
The installer rejects links, escaping paths, oversized data and version changes.

```text
fsf remote recipe-environment blender.recipe.json environment.json --out prepared.recipe.json
fsf remote recipe-diff blender.recipe.json prepared.recipe.json
```

The new recipe is not automatically approved. Review its changed paths and exact
digest before authorizing it. The environment plan is a declared JSON input;
tool archives are declared data inputs. Native input bundles retain the 64 MiB
limit and Kaggle's configured encoded-notebook limit. Larger tools may be
downloaded during setup from explicitly approved, SHA-verified vendor sources.
For Blender jobs, both Blender and media tools must
be present or supplied explicitly. The v2 render path does not silently install
missing media tools.

Host installation network settings use the explicit names listed in
[the agent guide](../src/fsf/remote/templates/AGENT_GUIDE.md). They are restricted
to installation commands. Ordinary job environments do not inherit proxy
credentials or the store's AWS credential environment.

## Use explicit v2 requirements

Measure the encoder on the intended worker platform:

```text
fsf remote encoder-pin --ffmpeg path-to-approved-ffmpeg --out encoder.json
```

This performs a one-frame CPU encode and records the exact FFmpeg 8.0.1 binary,
build and libx264 core. The control profile uses libx264, medium, CRF 16,
yuv420p, two threads, no B frames, GOP 24, CFR, a 24000 track timescale and no
audio. GPU rendering does not imply GPU encoding.

Create an `execution_requirements` object with `render` and `encode` matching the
[`v2 recipe schema`](../src/fsf/schemas/fsf_remote_recipe_v2.schema.json):

```text
fsf remote recipe-v2 prepared.recipe.json --requirements requirements.json --out render-v2.recipe.json
```

Cycles CPU uses `device=cpu`, `backend=none`, zero devices and zero minimum GPU
memory. CUDA uses `device=gpu`, `backend=cuda` and an explicit device count and
minimum memory requirement. The single-device segment profile selects one
device; a batch declares its authorized count and each task its own count.
Samples, seed and threads are bound
to the request. The v1 Blender request is unchanged; the v2 Blender request also
declares the full `animation_range`, allowing each segment to render global
frames from the same motion definition.

A [`v2 operator profile`](../src/fsf/schemas/fsf_remote_profile_v2.schema.json)
adds:

- `gpu_limits`: allowed backend, device count and memory ceiling.
- `store_binding`: the store ID, public configuration digest and input/output
  namespaces. Compute the digest with `store_public_identity`; host-local
  credential paths and secret values are excluded.
- `group_limits`: frame, duration and attempt ceilings.
- `allocation`: an operator-owned ledger directory and allocation ID, submission
  ceiling and execution-seconds ceiling. The full deadline is reserved before
  each irreversible submission. A new client state cannot reset this ledger or
  duplicate an existing intent. A changed allocation policy needs a new explicit
  allocation rather than silently discarding old charges.

GPU submission additionally requires `FSF_REMOTE_GPU=1` in the parent process.
CPU jobs stay CPU even when the profile permits GPU. Reading authorized existing
results does not require the submission switch.

## Upload, submit and collect

Upload each declared input and the matching worker wheel with `artifact-put`.
Save the returned reference JSON; it binds content version, SHA, size and role.
`artifact-get` accepts that exact reference and verifies downloaded bytes.
The optional object-store route supports multipart upload and range download
with 8 MiB chunks and private checkpoints. Native SSH/SCP and Kaggle output
downloads retry the complete bounded archive; they do not resume a byte offset.
Never replace a version with `latest` or use an ETag as a content SHA.

`prepare-v2` requires a definition, recipe, profile, store, input-reference map,
worker-wheel reference and segment identity. It writes a sealed request. Native
submission then creates its bounded input bundle; Kaggle embeds that bundle in
the notebook. `preflight-v2 --probe` distinguishes local declarations,
store/provider connection and runtime observations; unverified capability is
not reported as ready.

`dispatch-v2 --wait` submits once, reconciles uncertain responses and fetches
completed work. Repeating the same intent does not submit it again. Parent
collection independently verifies references, child journal, environment,
declared outputs, video QC and required acknowledgements. Human review remains
separate.

Kaggle requires a verified private CPU smoke with the same profile and store
binding before GPU submission. Native API jobs require no worker Secrets and
use `settings.native_notebook` when reusing an authorized notebook.

Only the optional object-store route requires the saved-run Secret labels
documented in the agent guide; that route's notebook source contains bootstrap
code and a non-secret manifest locator. Its optional `settings.worker_notebook`
selects an operator-created
private notebook whose slug starts with `fsf-worker-`. Saved versions are
checked against the exact request source and version; a newer version cannot
stand in for an older job. Editor access to Secrets, or a manually saved probe,
does not prove that an API-submitted version has the same binding. Only a
verified end-to-end CPU dispatch establishes readiness. If its bootstrap cannot
read the host Secrets, keep the adapter unready; do not embed credentials or
signed URLs in notebook source to work around the missing binding.

Native SSH requires host Python and pip, an authorized remote disk directory,
and the pinned SSH configuration. The optional object-store route additionally
requires its SDK, private store configuration and host credential chain.
FSF does not rent a machine or increase its budget.

A terminal cancelled/failed provider job is not ordinary completion. Use
`fetch-v2 --recover` only after deciding to inspect its complete worker evidence.
Recovery preserves the original provider status. Logs/base64 are not the formal
artifact channel, and a running/unknown job cannot be treated as completed.

## Render global segments

A `fsf_segment_template_v1` contains `definition`, `recipe`, fixed `inputs`,
`worker_wheel`, `blender_input_id` and the canonical digest of the full `motions`
array as `animation_digest`. The Blender input is an archive containing its
request, asset record and declared scene files. The driver changes only the
selected global interval and bound render/encode requirements for each attempt.

`segments-plan` creates contiguous half-open ranges, explicit attempt limits and
a shared processing budget. Both `24/1` and `24000/1001` are supported; certified
control delivery is bounded to the profile's frame limit, at most 96 frames.
`segments-run --wait` continues unfinished work. After a rejected segment, a new
attempt consumes the original limits; successful segments are revalidated and
reused. Unknown submissions are reconciled before another attempt is allowed.

`segments-pause` stops new submissions and allows in-flight work to finish and
checkpoint. `segments-resume` clears the request. Idle pause time does not create
new render charges. In-flight attempts retain their reserved deadline and
attempt count across parent restarts.

`segments-assemble` revalidates every accepted frame, encodes the full PNG
sequence once, requires SSIM at least 0.98 and verifies actual decoded frame
count, rational timestamps and stream structure. It publishes an immutable
directory containing the control MP4, manifest and receipt. Encoder-only changes
may reuse verified PNG provenance while producing a separate encoding intent;
an invalid new encoder pin blocks encoding without regenerating frames.

The assembled control can be used through `fsf blender condition` with view
`assembled`, or registered as a v2 control asset. It remains a control video,
not a story-video continuity source. Motion and listening review stay
`not_reviewed` until a person reviews them. Technical validation is not proof
that a downstream model follows the camera motion.
