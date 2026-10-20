# Remote jobs and model APIs

FSF1 is the parent and FSF2 is a worker running the same FSF version. The parent
owns its production journal, QC and review decisions. The worker executes one
approved recipe and returns named artifacts and its own journal. Workers cannot
dispatch another FSF worker. Neither a transport acknowledgement nor a model
acknowledgement proves visual quality.

## Select a method

| Method | Intended use | Requirements |
|---|---|---|
| Local | Separate worker process on this machine | FSF installed in the selected interpreter |
| HTTP | Async service implementing the reference job protocol | Operator endpoint, bearer environment variable |
| SSH | Existing Linux server or Runpod Pod | Python, pinned host key, operator SSH configuration |
| Kaggle | Private CPU notebook | Kaggle SDK 2.2.4, credentials, internet and notebook permission |
| Model API | Text, image, video, audio or upscale inference | A compatible, explicitly enabled provider profile |

SSH does not rent a Pod or start a billing account. Runpod Serverless is not the
SSH protocol. Kaggle support here is CPU only. Resource declarations are budgets
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
`runtime/free_studio_flow-0.1.0rc7-py3-none-any.whl`. A released wheel lacking
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
fsf remote prepare definition.json --recipe config/cpu.recipe.json --inputs inputs --wheel runtime/free_studio_flow-0.1.0rc7-py3-none-any.whl --profile config/local.profile.json --out state
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
