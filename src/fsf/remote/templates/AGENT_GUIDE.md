# Remote jobs and model APIs

FSF1 is the parent and FSF2 is a worker running the same FSF version. The parent
owns its production journal, QC and review decisions. The worker executes one
approved recipe and returns named artifacts and its own journal. Workers cannot
dispatch another FSF worker. Neither a transport acknowledgement nor a model
acknowledgement proves visual quality.

## Select a method

The built-in `kaggle.*`, `api.*` and `local.*` shape adapters are not the full
set of production routes. Configured process executors can run a real backend;
remote recipe executors can send model work to an authorized local, SSH, HTTP
or Kaggle worker; typed API executors call an authorized provider. A recipe
declares its model, tools, dependencies, inputs and named outputs. A small parent
host does not need the worker's GPU or model installation.

Handoff is a workorder protocol, not a particular generation backend. An agent
fulfilling a workorder may use an already authorized backend or remote recipe.
Do not infer authorization from the absence of a new credential: still check
the approved recipe/profile and resource budget. Existing approval applies
within its scope; a new shot alone does not require a new recipe approval.
Report the selected host's configured routes separately from FSF's available
integration methods. A customization template is not a tested model backend.

| Method | Intended use | Requirements |
|---|---|---|
| Local | Separate worker process on this machine | FSF installed in the selected interpreter |
| HTTP | Async service implementing the reference job protocol | Operator endpoint, bearer environment variable |
| SSH | Existing Linux server or Runpod Pod | Python, pinned host key, operator SSH configuration |
| Kaggle | Private CPU jobs; authorized native v2 GPU jobs | Kaggle SDK 2.2.4, credentials, internet and notebook permission |
| Model API | Text, image, video, audio or upscale inference | A compatible, explicitly enabled provider profile |

SSH does not rent a Pod or start a billing account. Runpod Serverless is not the
SSH protocol. The v1 Kaggle route retains its CPU policy; native v2 GPU jobs
require an approved GPU recipe, profile and parent submission switch.
Resource declarations are budgets
and admission checks, not OS memory isolation or a hostile-code sandbox.

## Copy templates

Create an empty `config` directory first, then run:

```text
fsf remote templates --out config
fsf remote validate config/cpu.recipe.json
```

Profiles are disabled by default. An agent may prepare edits, but the operator
must approve the recipe digest, endpoint, dependencies, models, resource limits
and credential environment variable names before enabling them. Never place a
credential value in a profile, recipe, prompt or output artifact.

The CPU and FFmpeg recipes perform actual synthetic jobs. They do not invoke a
generative model. `model.recipe.json` is a customization starting point: its
`approved_model_backend` module is not supplied. Replace it with a real backend,
declare its setup and output contract, and test it before approving its digest.

## Prepare a local CPU job

This Python example prepares operator files only. Run it from a directory with
the installed FSF package. Place an audited wheel built from the same source at
`runtime/free_studio_flow-0.1.0rc8-py3-none-any.whl`. A released wheel lacking
these modules cannot be used as a worker. Check the version of your build.

```python
import json
import sys
from pathlib import Path
from fsf.remote.contracts import digest, seal

for name in ("state", "worker", "inputs"):
    Path(name).mkdir(exist_ok=True)
recipe = json.loads(Path("config/cpu.recipe.json").read_text())
recipe["environment"]["python"] = ".".join(map(str, sys.version_info[:2]))
recipe = seal(recipe, "recipe_digest")
Path("config/cpu.recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
profile = json.loads(Path("config/local.profile.json").read_text())
profile.update(authorized=True, approved_recipes=[recipe["recipe_digest"]])
profile["settings"] = {"worker_root": str(Path("worker").resolve()),
                       "python": sys.executable}
Path("config/local.profile.json").write_text(json.dumps(profile), encoding="utf-8")
definition = {
    "parent": {"job_id": "example", "run_id": "run1", "step_id": "cpu",
               "attempt_id": 1, "plan_digest": digest({"example": "plan"}),
               "inputs_digest": digest({"value": 7}),
               "expected_contract_digest": None, "requirements_digest": None},
    "parameters": {"value": 7}, "inputs": {}, "video_condition": None,
    "consistency_requirements": None, "expected_media_contract": None,
}
Path("definition.json").write_text(json.dumps(definition), encoding="utf-8")
```

The illustrative parent digests above identify this standalone example. For a
production plan use the actual parent identities and digests; the opt-in remote
executor supplies them directly from PlanExecutor.

```text
fsf remote prepare definition.json --recipe config/cpu.recipe.json --inputs inputs --wheel runtime/free_studio_flow-0.1.0rc8-py3-none-any.whl --profile config/local.profile.json --out state
fsf remote diagnose --profile config/local.profile.json --probe
```

Use `bundle` and `dispatch_id` from the prepare result in subsequent commands:

```text
fsf remote dispatch state/REQUEST_DIGEST.zip --profile config/local.profile.json --state state --wait
fsf remote status DISPATCH_ID --profile config/local.profile.json --state state
fsf remote fetch DISPATCH_ID --profile config/local.profile.json --state state
```

`REQUEST_DIGEST` and `DISPATCH_ID` are placeholders, not literal arguments.
The accepted directory contains result, environment and child journal evidence
plus verified artifacts. It does not contain an automatic human approval.

## Preflight before dispatch

From a source checkout supporting `preflight`, use a worker wheel built from
that same checkout:

```text
fsf remote preflight definition.json --recipe config/cpu.recipe.json --inputs inputs --wheel runtime/free_studio_flow-0.1.0rc8-py3-none-any.whl --profile config/local.profile.json
```

This read-only check validates declarations, authorization, QC contract digests
and local files without submitting work. Exit 3 means capability remains
unverified; exit 2 means a check failed. Neither means a completed job. Exit 0
means the requested checks passed, with `verified_execution` still false.

Add `--probe` to explicitly check provider connectivity. Local minimal CPU
recipes also check their configured interpreter. SSH, HTTP and Kaggle connection
checks do not prove installed tools, GPU capability or model inference; those
runtime checks remain unverified. Setup/model/tool requirements are not installed
or executed by this command. Without `--inputs` or `--wheel`, file readiness is
also unverified. Existing Kaggle CPU-only and Blender CPU limits remain in force.

Video outputs require `expected_media_contract` and its matching parent digest
before preparation. A control video is a distinct declared role; it is not
silently treated as a story video. Parent-side QC still runs on actual outputs.

## Recovery and cancellation

Intent is stored before submission. If a response is lost, the state is
`submission_unknown`. Run `reconcile` for the same dispatch; do not create another
request or change an attempt to bypass it. Repeated dispatch of the same bundle
does not submit again. Unsupported reconciliation remains unknown. This is not
a universal exactly-once guarantee from an external provider.

```text
fsf remote reconcile DISPATCH_ID --profile config/local.profile.json --state state
fsf remote cancel DISPATCH_ID --profile config/local.profile.json --state state
```

Local, HTTP reference and SSH cancellation request cooperative process-tree
cleanup. A request is not a confirmed cancellation until status confirms it.
Kaggle cancellation is unsupported here. Cancellation never proves zero cost.
Interrupted child steps with uncertain effects are not automatically repeated.

## Customize a recipe

Keep scheduling in the adapter, computation in the recipe. Declare typed
parameters, named input IDs, output IDs/paths/roles/media types, dependency order,
OS/Python version, tool version probes, setup source URLs and model revisions.
Each declared model has a relative local `path` and SHA-256 checked after setup.
Pin setup packages or immutable download revisions and verify downloaded hashes;
the worker records installed versions but does not create a hermetic lockfile.
Setup commands are trusted operator-approved programs, not a security sandbox.

Commands use argument arrays with placeholders such as `{python}`, `{ffmpeg}`,
`{blender}`, `{param.prompt}`, `{input.scene}` and `{output.video}`. Do not insert
shell syntax or secrets. Recompute `recipe_digest` with `seal` after changes, then
explicitly update the operator's approved digest list. Do not edit a request
bundle in place. Tool installation and model downloads consume the shared budget.

For Blender, package a trusted scene and the controlled Blender request. A remote
recipe may install Blender or models if authorized. Merely disabling Blender
auto-execution is not a sandbox for untrusted scenes. Control movies retain
their own SHA/timebase/frame mapping; they are not story-video continuity.
Required conditions must be supported and acknowledged by the actual backend.
Use a backend-produced acknowledgement file as a named recipe output. Never
synthesize a claim that a model used a reference or followed a motion path.

## Model API profiles

`nebius-text`, `google-text` and `bedrock-text` profiles cover their named text
protocols. Bedrock uses the AWS SDK credential chain and requires the optional
`api` dependency extra. It does not send AWS keys as bearer tokens.

The image/video/audio/upscale profiles are generic JSON HTTP mapping templates,
not preconfigured accounts or universal vendor adapters. Set the actual model,
operator-approved base URL, parameter schema, routes, JSON pointers and response
mode. Supported result modes are text, base64, binary and allowlisted artifact
URL. Multipart, S3-native uploads and other proprietary protocols require a
separate approved adapter or recipe. Unsupported methods are reported, not
simulated. Async profiles must map the provider's job ID and status values.

```text
fsf api check --profile config/nebius-text.api-profile.json --probe
fsf api run api-request.json --profile config/image.api-profile.json --inputs inputs --state state
```

A request has `schema_version: fsf_model_api_request_v1`, a new immutable
`request_id`, `prompt`, typed `parameters`, relative-path `inputs`, and an
`expected_media_contract` object for video (otherwise null). The same request ID
cannot be repurposed after an uncertain POST. `status`, `fetch`, `reconcile` and
`cancel` take the same request and profile; support depends on declared routes.

Text must be nonempty UTF-8. Images are decoded and size checked. Video requires
parent Media QC. Audio is probed and fully decoded. Upscale checks actual source
and result dimensions. These checks do not establish semantic quality or model
motion adherence. Receipts keep review as `not_reviewed`.

For compiled Studio plans, extras support `kind: remote` and `kind: model_api`
with operator profile files. The model API executor requires `profile`,
`state_root`, optional `parameters` and `input_files`, in addition to the usual
executor ID, transport and tool name. Required visual conditions currently use
the remote recipe path; generic model APIs reject them rather than inventing an
acknowledgement. Existing shape executors remain shapes until explicitly changed.

Configuration validation, authentication, service reachability, synthetic
contract tests and real model inference are separate evidence categories.
Successful text inference does not establish image, video, audio or upscale
support for that provider. Install published FSF versions in managed agent
environments; use test wheels only in explicitly controlled development workers.

## Pinned environment preparation

The `environment-python311.json`, `environment-python312.json` and
`environment-python313.json` variants check only their declared Python prefix;
they do not install Blender or a model. Choose the observed worker variant,
not an assumed platform default. A variant's existence is not host verification.

Use a wheel built from the same checkout as the parent. An already published
wheel without these commands is not a compatible worker.

An `fsf_environment_plan_v1` declares the Python variant, OS, architecture,
exact tool versions, package SHA-256 and size, extraction limit, executable,
version check, smoke arguments and shared preparation deadline. Preinstalled
and supplied-archive tools are separate modes. A missing Blender installation
does not select another engine or silently skip rendering. Archives must be
supplied as declared input artifacts; the installer does not choose a download
site. Verify the package's origin before authorizing its recipe.

```text
fsf remote recipe-environment blender.recipe.json environment.json --out prepared.recipe.json
fsf remote recipe-diff blender.recipe.json prepared.recipe.json
```

The output is a new, unapproved recipe. Add its exact digest to an authorized
operator profile only after reviewing the changes and budgets. The environment
plan is a declared `environment_plan` JSON input. An archive tool uses a declared
input with the same ID as the tool; its plan path must match the worker mapping
`inputs/<tool_id><last-extension>`. The preparation source
`fsf:bundled-tools` must be explicitly allowed by the profile. Large packages
use the versioned data plane rather than increasing the v1 bundle limit.

Do not copy the parent's complete environment to a worker. A host Secret Manager
may supply `FSF_REMOTE_INSTALL_HTTP_PROXY`, `FSF_REMOTE_INSTALL_HTTPS_PROXY`,
`FSF_REMOTE_INSTALL_NO_PROXY` or `FSF_REMOTE_INSTALL_CA`. The corresponding names
must be explicitly allowed in the profile. They are mapped only for installation
commands and are not copied into ordinary job environments or evidence.

## Versioned data plane and explicit GPU requests

The v1 Kaggle path remains CPU-only. For v2 Kaggle jobs, use the native API
route described in [Versioned remote jobs](../../../../docs/REMOTE_DATA_PLANE.md).
It submits a private notebook per request and downloads its native outputs.
It requires neither a browser nor worker Secret bindings. SSH workers can use
their existing disk with native bundle upload/download instead. The local
`fsf_kaggle_native_store_v1` configuration stages immutable bytes; it does not
provision a storage service. Remote recipe/tool requirements remain explicit.

An optional alternative v2 data plane uses a private,
version-enabled S3/SigV4 store, exact object versions and independent SHA/size
checks. Install the `remote` dependency extra on development workers. Store
configuration and credential bindings belong to the operator's private host,
not to a job-supplied endpoint. `artifact-probe` checks connection and bucket
versioning; it does not prove a successful job or render.

```text
fsf remote artifact-probe --store private-store.json
fsf remote encoder-pin --ffmpeg path-to-approved-ffmpeg --out encoder.json
fsf remote recipe-v2 blender.recipe.json --requirements requirements.json --out blender-v2.recipe.json
```

`encoder-pin` performs one small CPU encoding to record the exact FFmpeg 8.0.1
binary, build and libx264 core. Requirements distinguish Cycles CPU/CUDA
rendering from CPU libx264 encoding. Copy pins measured on the intended worker;
pins from another platform are not interchangeable. Recipes remain editable,
but changed executable content, tool packages, models, sources or requirements
need a new digest approval. Existing v1 inputs are not automatically upgraded.

GPU submission requires profile permission, an approved GPU recipe and the
parent process switch `FSF_REMOTE_GPU=1`. A CPU request does not enable GPU just
because the profile allows it. The certified Blender path selects one visible
CUDA device, checks required memory, explicitly selects Cycles and rejects CPU
or Eevee fallback. This does not impose a single-GPU limit on unrelated custom
recipes, or prove model motion adherence.

V2 profiles also provide `group_limits` and a private operator-owned `allocation`
ledger. Creating another client state or restarting the agent does not reset its
submission/deadline reservations. A changed allocation ceiling requires an
explicitly new allocation rather than overwriting the prior ledger.

Kaggle v2 requires an accepted private CPU smoke using the same data binding
before GPU submission. Native API jobs do not require worker Secrets. Only the
optional object-store route requires its saved-run Secret bindings.
The bootstrap uses fixed host Secret Manager labels
`FSF_ARTIFACT_STORE_CONFIG`, `FSF_STORE_ACCESS_KEY_ID`,
`FSF_STORE_SECRET_ACCESS_KEY` and optional `FSF_STORE_SESSION_TOKEN`.
For an SSH object-store job, configure `FSF_ARTIFACT_STORE_CONFIG` and the AWS SDK credential chain
on that host. Native SSH jobs upload their bundle to the configured remote disk
and retrieve the result over SCP. Bootstrap requires host Python and pip. No credential values
or presigned URLs belong in notebook source, metadata, recipes or receipts.

`prepare-v2` consumes a definition, approved recipe/profile, fixed input and
worker-wheel reference JSON, and explicit segment identity. `dispatch-v2`,
`status-v2`, `reconcile-v2` and `fetch-v2` keep scheduling observations separate
from worker completion and artifact verification. After an uncertain submit,
reconcile; do not invent another attempt. Terminal failed/cancelled provider
jobs require explicit `fetch-v2 --recover` and complete independent validation.
This preserves the original provider state and is not ordinary completion.

### Optional browser activation for host-bound object-store jobs

This compatibility route is not required for native API jobs. A dedicated
private worker notebook can use the explicit profile setting
`settings.activation_mode: "agent_browser"` together with
`settings.worker_notebook: "fsf-worker-example"`. The operator grants that
notebook its required Secrets once. An authenticated host browser and an agent
capable of operating it are prerequisites; the CLI does not include a browser
executor. This is a separate activation route, not a claim that API submissions
inherit saved-run Secret access. The default remains `api_push`.

In browser mode, `dispatch-v2` prepares `worker.ipynb` and the sealed
`browser-activation.json` in the request state directory. Its
`awaiting_activation` status means no provider submission has been confirmed;
it is neither running nor completed. The browser agent must:

1. Check the activation digest, request/profile binding, dedicated private
   notebook, previous version and approved resource policy.
2. Reconcile first. If a newer version already contains the exact generated
   source, observe it rather than clicking Save again.
3. Otherwise import the complete generated `worker.ipynb` into that same
   notebook. Do not copy partially visible editor text or create another
   notebook. Import can reset notebook accelerator metadata. For a GPU request,
   explicitly select the approved GPU type in notebook Settings after import;
   a Save dialog GPU override alone is not hardware evidence. Confirm the
   accelerator matches the request, then use Save & Run All. Do not change
   Secret values or their notebook authorization.
4. Use `status-v2` or `reconcile-v2` to bind the newer version and exact source,
   then `fetch-v2` to validate the normal versioned artifacts and result. A
   browser success message alone is not completion evidence.

Repeated dispatch of the same request retains its state rather than requesting
another activation. A parent restart must reuse that state and allocation.
An unavailable browser, expired login or uncertain activation leaves the job
pending for reconciliation; do not silently switch routes, reset keys or ask
the operator to reauthorize the same Secrets for each job. Host login renewal
is distinct from notebook Secret authorization. This CPU activation path does
not by itself establish GPU readiness, model execution or cancellation support.

## Global segments and editing communication

`segments-plan` binds a template, generation identity, rational clock and
contiguous half-open global frame intervals. `segments-run` checkpoints accepted
segments and retains attempt counts across process restarts. `segments-pause`
stops new segments while allowing in-flight work to finish and checkpoint;
`segments-resume` clears that pause request. `segments-assemble` revalidates all
accepted frames, checks global ordering and encodes once with the pinned CPU
profile. Technical checks leave motion/listening review `not_reviewed`.

For editing, `duplicate` inserts the copy immediately after its source clip.
Time changes can invalidate absolute subtitle anchors. `fsf edit patch-impact`
reports potentially affected cue/track IDs without applying a patch or granting
anchor confirmation; normal patch validation is still required. Include an
explicit `confirm_timeline_anchor` when approving a new absolute timing anchor.
Audio events must fit their host clip. Output directories and immutable-output
rules still apply. Supported Blender versions remain the explicitly tested
3.2/4.3 families; Empty or parented motion is not silently rewritten.

Connection, local CPU contract tests, real remote GPU rendering and human review
are different evidence. Remote readiness must be demonstrated on the intended
host; a template or a local result cannot establish it.
