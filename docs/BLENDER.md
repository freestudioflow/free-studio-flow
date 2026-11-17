# Blender Assets and Motion Controls

*Introduced in rc7 and extended in rc8–rc9. See [Library Asset Validation](ASSETS.md) and [Production Workflows](PRODUCTION_WORKFLOWS.md).*

`fsf blender` runs controlled, trusted small scenes with CPU Cycles. Supported
profiles are `reference` (one frame, up to two named cameras) and `clay_v1`
(explicit gray material override, camera/object transform keyframes). The tested
Blender families are 3.2 and 4.3. Blender is an operator-installed external tool.

Create a `fsf_blender_request_v1` JSON document using the packaged schema. It
binds a verified source asset record, relative source file, named cameras,
resolution, rational clock, half-open frame interval, and optional linear
position/Euler-rotation keyframes. The source root contains `record.json` and
its declared resources. Asset records verify bytes and declarations, not scene
safety. Only explicitly trusted scenes are accepted; disabling auto-execution
is not a sandbox for hostile files.

```powershell
fsf blender check --executable <blender-executable>
fsf blender plan request.json --out plan.json
fsf blender execute plan.json --source-root source --out renders --executable <blender-executable> --trusted-scene
fsf blender verify renders/<bundle-id>
fsf blender condition renders/<bundle-id> --view-id front --out condition.json
fsf blender record renders/<bundle-id> --asset-id prop_A --asset-version 1 --prompt "A fixed prop" --out record.json
fsf asset register record.json --resources renders/<bundle-id> --library library
fsf asset get prop_A 1 --library library
```

Create output/library directories before these commands. Executing the same
plan revalidates and reuses its bundle. `--no-resume` creates a separate attempt;
existing bundles and asset versions are not overwritten. JSON output files must
not already exist. A model, frame files, portable control MP4, manifest and
receipt are published together only after validation. Static references have
no control MP4. No credentials or operator scripts are copied into the bundle.

The small-scene profile limits rendering to 96 frames per view, two views,
512-by-512 pixels, 64 objects, 250,000 mesh vertices and 64 MiB of declared input
files. Frame rates are 24 and 24000/1001. The transform has a shared 120-second
processing deadline; cleanup may extend wall time. These limits are not a hard
OS memory sandbox. Linked libraries, pre-authored animation, constraints, modifiers and executable
drivers, compositing and sequencer overrides are unsupported. CPU profiles use four samples, seed zero and no denoising.
Tool-binary and worker-script digests participate in reuse identity. Reference images require available textures; clay is
an explicit override, never an automatic missing-texture fallback.

Dynamic artifacts use `fsf_library_asset_record_v2` control roles; v1 records
retain their original semantics. Control videos are conditioning inputs, not
approved story footage or video-continuity sources. They never enter static
`ref_images`. Condition metadata binds the actual video SHA, clock, frame count
and frame mapping. Only `preprocess="none"` is supported; implicit resampling,
cropping, looping and prompt-only fallback are refused.

Python callers can use `condition_request`, `validate_condition_capability`
and `validate_condition_ack` from `fsf.runtime.blender`. Handoff workorders can
carry `video_condition` and `condition_capability`; successful results must
return a matching `condition_acknowledgement`. This acknowledgement reports use,
not visual compliance. Render receipts keep `motion_review="not_reviewed"`.
Remote scheduling and model generation are separate capabilities; these commands
do not submit jobs or promise that a model will follow the control motion.

## Import a control video made in another tool

Blender is not required for this import. Create `controls/` first, then run:

```powershell
fsf blender import-control user-control.mp4 --out controls --control-id user_A
fsf blender verify controls/<bundle-id>
fsf blender condition controls/<bundle-id> --view-id imported --out condition.json
fsf blender record controls/<bundle-id> --asset-id control_A --asset-version 1 --prompt "User motion control" --out record.json
fsf asset register record.json --resources controls/<bundle-id> --library library
```

The import copies the original bytes, fully decodes the video and records its
SHA, actual frame timing, decoded RGB digest and identity frame mapping. Repeating
an unchanged import revalidates the existing bundle. The manifest identifies
`operator_control_video_import`; it does not claim Blender camera evidence or
certify a gray material profile. `clay_profile` is `unverified` and `motion_review`
remains `not_reviewed`. No Blender render receipt is fabricated.

This first import profile accepts a single video-only MP4 stream, 24 or
24000/1001 constant fps, zero-based timestamps, at most 96 frames, even dimensions
from 32 to 512 pixels, square pixels and at most 64 MiB. Audio, rotation, variable
timing and unsupported dimensions/rates are refused, not silently stripped or
converted. Prepare a conforming export in the source tool if necessary. Processing
shares a 120-second budget; cleanup may take longer. Existing bundles are never
overwritten. Use a short output directory on Windows to stay within filesystem
path limits.

The resulting condition uses the same capability and Handoff acknowledgement
checks as rendered controls. Registration and acknowledgement do not prove that
a model followed the motion. These commands do not run a generation model.

## Long control preparation in this source checkout

The separate production importer retains a complete control movie rather than
requiring the operator to split it into short assets. The original Blender
import profile above keeps its existing limits.

```powershell
fsf production import-control full-control.mp4 --out controls --control-id control_A
fsf production verify-control controls/<bundle-id>
fsf production segments controls/<bundle-id> --capability capability.json --shot-id shot_A --policy-digest <sha256> --out segments.json
```

Create `controls/` first. The production input profile accepts video-only CFR
MP4, square pixels, no rotation, 24/1 or 24000/1001 fps, even dimensions from
32 through 3840, up to 120 minutes and 64 GiB, subject to decoding and disk
budgets. It refuses implicit conversion. Verified decode checkpoints survive
interruption; incomplete directories are not delivery sources.

Segmentation uses an explicit model capability document. The default path
requires a verified capability; `--qualification-probe` permits an explicitly
probe-only unqualified capability. The sequence runner likewise requires
`allow_probe=True` for that plan. Authorized experiments do not acquire model
qualification from a flag. Global core intervals remain separate from overlapping
context and declared tail padding; padding is not extra delivered story time.
The current sequence runner transfers actual generated frames for
`previous_frame` continuation. A backend's latent conversion needs its own
implementation and bindings; a continuity declaration alone does not supply it.

`production split-artifact` and `assemble-artifact` provide content-checked
chunks on mounted disks. A transfer adapter must transport those chunks; these
commands do not make the old native archive limit disappear.

The Python production parent supports finite leases, bounded attempts, exact
budget reservations and provider reconciliation. Its remote driver reuses
existing native dispatch and collection. A current-source and quality verifier
is mandatory: provider completion does not approve story footage. These
components can coordinate operator-supplied generation and evaluation backends
across finite batches. Building a long-take or feature-film workflow is separate
from demonstrating its visual quality. World-state records preserve declared
identity, spatial and prop facts across chapters; they do not force model pixels
to obey. ASR callbacks and ducking envelopes require explicit backend/rendering
integration. Required missing, rejected or unknown quality checks block machine
acceptance, which does not grant human approval or evaluator calibration.

Use short working roots on Windows: deeply nested hash-based checkpoint paths
can exceed platform path limits. Native ZIP transfer remains limited to 64 MiB;
local checkpoint chunks and streamed HTTP outputs have separate limits.
Large-card hardware qualification, full-length neural works and calibrated
visual-quality statistics are not established by these software interfaces.

`production calibration-corpus` checks an operator-supplied set of double
annotations, balanced development and held-out windows, and split isolation.
`production calibration-check` scores exactly the frozen held-out predictions
bound to their media, evaluator and policy digests. Unknown results do not count
as correct decisions. The tools do not supply human labels or qualify a model
without real labeled examples; they do not change human review states.

`production workorder-definition` binds a remote definition to a workorder
already emitted by production execution. `production fulfill-remote` rechecks
the corresponding remote result, video QC and bytes before publishing its
Handoff result. It refuses mismatched parent IDs, attempts and contracts; it
does not re-label an unrelated remote clip as a new production job. Resume the
original production execution to obtain its own journal and QC evidence.
The bridge never writes production completion entries or approves reviews.
