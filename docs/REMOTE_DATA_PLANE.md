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

The native Kaggle path embeds a bounded compressed input bundle when its encoded
notebook fits `max_inline_notebook_bytes`. Larger encoded inputs can use a
private dataset containing `payload.bin`; the bootstrap checks the complete
bundle SHA before execution. Dataset metadata retains the files' original
ownership and licenses. Failure to establish a private dataset or verify the
payload blocks execution. The underlying 64 MiB input-bundle limit still
applies; dataset staging does not provide unlimited input or byte-offset resume.
These are implementation limits, not permanent Kaggle platform limits. Large tools
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
submission then creates its bounded input bundle; Kaggle embeds it or mounts its
private dataset payload as described above. `preflight-v2 --probe` distinguishes local declarations,
store/provider connection and runtime observations; unverified capability is
not reported as ready.

`dispatch-v2 --wait` submits once, reconciles uncertain responses and fetches
completed work. Repeating the same intent does not submit it again. Parent
collection independently verifies references, child journal, environment,
declared outputs, video QC and required acknowledgements. Human review remains
separate.

The optional Secret-backed Kaggle route requires a verified private CPU smoke
with the same profile and store binding before GPU submission. Native API jobs
do not use that Secret-binding gate, require no worker Secrets, and
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

## Neural-model execution contracts

Explicit `fsf_remote_recipe_v3` recipes produce `fsf_remote_request_v3` requests
through the same prepare and lifecycle entry points. The older recipe and
request contracts retain their validation rules. The model path declares a
CUDA device requirement and a fixed model lock instead of pretending to be a
Cycles render or requiring an unrelated video encoder.

A model lock binds an immutable repository revision, file SHA-256 values and
precision. The worker must return a model receipt containing those observed
file identities, selected device UUIDs, positive CUDA event duration, inference
interval, allocation peak and hashes of declared outputs. The parent checks
the receipt and actual returned bytes. This evidence records execution by an
approved worker; it is not hardware attestation or proof of visual correctness.

Model execution evidence remains separate from `gpu_render_verified`.
Multimodal calibration and motion/listening review also remain separate. The
optional model probe uses synthetic images for a small execution diagnostic;
it is not generated story footage or a calibrated consistency evaluator.

Provider-native transport does not require worker storage Secrets. The
separate secret-backed transport continues to require its saved-run binding
evidence. Native transport still requires an authorized profile, approved
recipe, parent GPU switch and bounded allocation before submission.


## Custom recipes, hosts, and APIs

Reference recipes are starting points, not a provider or model allowlist. An
execution permission, observed execution, media conformance, and human review
are separate facts. An untested model may run within the operator's authority;
it does not acquire qualification by running.

For production intake and planning, `--media-contracts FILE` accepts an operator
JSON object mapping shot IDs to full media conformance contracts. Use the same
file for `fsf intake` and `fsf plan --media`; its digest is recorded in the job.
Unknown shot IDs and invalid contracts are rejected. This allows an appropriate
model resolution instead of treating the default 1024x576 preset as universal.
Choose contracts before execution; changing them cannot reuse an in-flight plan.

## Recipes

A `fsf_remote_profile_v3` may set `allow_recipe_experiments: true` to allow
unlisted CPU/render v2 or MODEL v3 recipe digests within its existing GPU, network, credential,
time and allocation policy. Each actual recipe digest is still recorded and the
request is labelled `execution_mode: experimental`. Without that explicit
choice, `approved_recipes` remains an exact digest allowlist. Updating tools,
weights or parameters changes the execution identity. It does not reset the
budget or make an unresolved submission safe to repeat.

Model locks accept operator model identifiers, version identifiers and precision
labels, including quantized formats. Every declared file still has a SHA-256.
The optional Wan worker is one recipe implementation, not a universal model
runner and not a mandatory dependency.

Experimental non-Torch workers may emit `fsf_model_execution_receipt_v2` with
`runtime`, `runtime_metrics`, and `verification_level: observed` instead of
`cuda_event_ms`. Other binding fields remain required. Such a result retains
`model_execution_verified: false`; storage integrity and technical media QC
must not be described as measured GPU model execution or semantic quality.
The v1 measured receipt remains available and unchanged.

## SSH hosts

SSH uses an operator-owned host alias, known-hosts file, SSH configuration,
Python executable and remote working root. It does not select hosts by vendor.
An existing Vast, Runpod, EC2 or private Linux host can implement this contract.
Renting a machine through a vendor API is separate from connecting to that
machine. A successful test on one vendor is not a qualification of another.

## Command adapters

A v3 remote profile can use `transport: command`. Its `settings` contain:

- `command`: an argv array beginning with an absolute executable path.
- `files`: entries with absolute `path` and `sha256`, including the executable
  and the adapter script. Include all code files whose identity must be pinned.
- `cwd`: an absolute working directory.
- `timeout_seconds`: a finite call budget from 1 through 5400 seconds.
- `credential_env_names`: only the host environment names to pass to the adapter.

The operator installs and trusts this program. FSF does not sandbox it or its
dependencies. A workorder cannot choose a different host program. Credentials
belong in the host credential facility, not in argv or configuration values.

FSF appends two filenames to `command`: a request JSON file and the required
response JSON file. Requests use `fsf_command_adapter_call_v1`, with `operation`,
the exact remote `request`, `locator`, prior `remote` identity and local
`store_config` filename. Operations are `prepare`, `submit`, `status`,
`reconcile`, `fetch`, and `cancel`. Neither stdout nor stderr is a result channel.

Responses use `fsf_command_adapter_reply_v1`, `request_digest`, `operation`, and
`result`. Return an object for prepare/submit/fetch; submit returns the durable
provider identity. Status is queued/running/completed/failed/cancelled/unknown.
Reconcile returns that same provider identity, or null when it cannot establish
one. It must never create another job. Cancel is requested/cancelled/unsupported/
unknown; requested does not prove that billing or compute stopped.

Fetch imports results into the configured artifact store. Normal FSF result,
input/output SHA, child journal and media checks still run after fetch. A
provider's completed status alone cannot accept an artifact. Preserve
idempotency using the request digest, retain unknown submissions, implement
bounded transfer retries, and test cancellation before using a paid backend.
The submit-hold marker is checked before the adapter's submit process starts.

## Model APIs

`json_http_v1` profiles support custom HTTPS endpoints for text, image, video,
audio and upscale results, including async status/fetch/reconcile/cancel routes.
Model names are operator data. Configure request/response mappings and actual
output contracts; changing only a capability string cannot implement a new
provider protocol. The OpenAI chat, Gemini content and Bedrock Converse
convenience protocols currently implement text only. A provider's other
modalities use an appropriate HTTP profile or a trusted custom adapter.
Provider-specific signing can be implemented in an operator adapter or a
host-owned proxy; never put signing keys in a notebook or workorder.

`fsf_model_api_profile_v2` keeps the same HTTP mappings and adds explicit
`input_max_bytes` and `request_max_bytes`. Positive token caps, output dimensions
and output duration are operator choices; timeout is at most 5400 seconds per
call lifecycle. This does not assert that a model supports the declared values.
The existing inline HTTP implementation still limits outputs to 64 MiB and
request/input buffers to their schema limits. V2 URL results stream to disk in
64 KiB chunks with an operator cap of at most 64 GiB, followed by the same media
QC and atomic acceptance. Artifact hosts must be explicitly allowed; provider
credentials are not forwarded to a different origin. Failed partial downloads
are removed. Native byte-offset network recovery is not implied by streaming.
Profiles using the old v1 schema retain their previous limits.

Use independent live tests for required modalities. Protocol fixtures do not
prove vendor access, quota, GPU execution, visual consistency or human approval.

## Durable production parents

`fsf production run-batch PLAN --driver DRIVER --ledger LEDGER --state STATE`
executes a finite batch (at most 5400 seconds). Run it again with the same
documents and state to observe or continue existing work. The state directory
pins one driver configuration, plan and quality policy; use a distinct state
directory for a different plan. A lost reply requires reconciliation, never an
automatic second provider submission. An expired authorization can still be
inspected and reconciled; it cannot authorize new work.

The operator command driver uses `fsf_production_command_driver_v1`, `settings`
from the command adapter contract, a trusted existing `artifact_root`, and a
pinned `evaluator_digest`. Submit/reconcile return the same durable provider
identity. Fetch returns an `artifact` path relative to the request's artifact
directory and a bound content `report`. It must not create new billable work.
This integration is operator-trusted code, not an isolation boundary. Known
passed credential values in the response file are rejected before persistence.

Budget v2 adds an authorization expiry, exact plan binding, per-shot segment
IDs, repair-round caps and completion forecasts. Unknown provider costs remain
reserved. Cleanup/delivery reserves cannot fund generation. At the configured
reforecast threshold, optional work stops and essential work needs an affordable
remaining-work estimate; `production budget-forecast` records it explicitly.

## World memory, dialogue and quality coverage

`production world-create`, `world-state` and `world-window` freeze and inspect
declared assets, entities, routes and chronological events on a global frame
clock. Ownership and carrying are separate; chapter changes do not reset facts.
The generated requirements describe conditions a backend must implement. A
matching acknowledgement does not prove that the resulting pixels obey them.

The `fsf.production.speech` API accepts an operator ASR callback and binds its
transcript to actual PCM bytes. `production speech-check` checks explicit event,
track, clip and sample-time coverage. `production ducking-envelope` produces an
additive gain envelope for a named target track, preserving the user's gain.
An envelope is control data; rendering it is an explicit recipe operation.
Neither transcript equality nor numeric loudness is a listening approval.

`production quality-rollup MANIFEST --reports REPORTS --out OUTPUT` checks
complete segment/chapter/film partitions and every internal segment or chapter
join. Reports bind each full check definition, artifact, evaluator, plan and
policy. Any missing, rejected or unknown check prevents machine acceptance.
The result does not attest evaluator calibration or human review. Required
checks must be pinned before evaluation; this aggregator cannot decide whether
the operator selected sufficient artistic checks.

These components support composing long-take and feature-film workflows with
operator-selected backends. They do not guarantee pixel continuity or a
finished high-quality film. Actual-frame continuation is implemented by the
sequence runner; latent extraction/conversion is backend-specific. Declared
world state and condition acknowledgements are inputs/evidence, not proof of
visual adherence. External lease/watchdog deployment and confirmation that
compute stopped remain host responsibilities.

## Portable generated-frame checkpoints

`production checkpoint-export SEQUENCE_ROOT --plan PLAN --out OUTPUT` exports
verified completed segments as bounded index pages and 8 MiB chunks.
`checkpoint-verify` and `checkpoint-restore` require `--plan` and an independently
pinned `--digest`. Restore into a fresh worker root, then continue the same
sequence plan. Existing accepted frames and initialization images are verified
and reused; an earlier prefix cannot overwrite conflicting later progress.
These are integrity records, not signatures or semantic-quality approvals.
Transporting the directory is separate from rendering and does not authorize a
new model call.
