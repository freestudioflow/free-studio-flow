# Release policy

Free Studio Flow publishes the same way ProDocuX does: Git tags and GitHub
Releases are the approval boundary; PyPI receives the already-approved wheel
and source archive unchanged through Trusted Publishing. There is no
long-lived PyPI token.

The current published pre-release is **`0.1.0rc14`**, available on PyPI.
Existing PyPI versions are immutable and must not be rebuilt or re-uploaded.
Runtime pins
remain `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, and
`pdx-adapter-media==0.2.0a3`. Do not publish a stable `0.1.0` until those
pins leave rc/a.

## Release notes (0.1.0rc14)

Version `0.1.0rc14` is published from tag `v0.1.0rc14` at commit
`fa63ee91f55ddb5c0be1c9b215657497ba598be4`. GitHub and PyPI serve the same
verified files. Use matching parent and worker builds.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| free_studio_flow-0.1.0rc14-py3-none-any.whl | 685135 | `1ab2f9aaf1b503b69a7842f710b19dfcf5597c106bfc51613bb4311c1a2a709a` |
| free_studio_flow-0.1.0rc14.tar.gz | 919355 | `ca85d57ecac2f4eb4713261a6b8c6310629555e82081ed23a17d3fb2e90d4766` |

- The README starts with installation, a playable four-second synthetic MP4,
  playback, job status and job-contract verification. The standalone
  `verify <clip>` entry point remains available.
- `status --json` reports the last recorded local state, active workorders
  and attempts, blockers and the next action. It reads the job without
  writing or dispatching. Use the original plan for jobs stored separately
  from their plan, and rerun the original execute command to resume.
- `sheet new` starts a DOCX shot list. The synthetic Handoff tour demonstrates
  retry, continuity and review. Active workorder listing, status and fulfillment
  share canonical workorder validation. Fulfillment validates an exclusive
  temporary receipt before atomically publishing the canonical result.
- Accepted scope is local synthetic media on Windows and Linux. Demo playback
  needs working `ffmpeg` and `ffprobe`. Synthetic Handoff review covers the
  fixture. Production shots still require operator authorization and their
  applicable review. macOS and real model output remain unqualified.
- Receipt recovery covers process interruption before publication. Filesystem
  power-loss durability and simultaneous manual receipts under different
  filenames remain outside the verified scope. A completed status records
  execution state; media conformance is checked separately with `verify --job`.
- Earlier limits remain open, including real three-minute single-shot and
  120-minute audiovisual quality, semantic and human QC, designated GPU
  qualification, Kaggle Secret attachment, private-slot proof, dataset uploads
  above 64 MiB and missing historical evidence.

## Release notes (0.1.0rc13)

Version `0.1.0rc13` is published from tag `v0.1.0rc13` at commit
`6ae9419c1888ac4c5dac8a325582f397a8b808df`. GitHub and PyPI serve the same
verified files. Use matching parent and worker builds.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| free_studio_flow-0.1.0rc13-py3-none-any.whl | 667912 | `e50487b607527ef6e4014f7aae44ea9beee55eb61218b2af22d9cd4dd7a09c82` |
| free_studio_flow-0.1.0rc13.tar.gz | 890573 | `571b9e0663895a94fabb9003cebae34e4981ee06113b31196dbaadd8d6a20f05` |

- A single approved feature submission checks chapter and shot coverage,
  binds workorders to their inputs, and uses a durable attempt index to resume
  and retrieve results. Unknown submissions retain their budget reservation
  and require reconciliation. Returning a device lease leaves charge settlement
  to the production ledger.
- Local quality checks bind samples to the workorder reference and route.
  Advisory assessments cannot authorize acceptance. A/V checks use a rational
  frame-to-PCM mapping with an independent final sample bound. Voice receipts
  cover CPU PCM WAV with an operator-approved verifier.
- Windows and Linux completed a 172,800-frame, 24/1 synthetic feature with
  recovery and reuse. The video-only output was 64×64. A separate Windows
  run verified the same frame count at 24000/1001 for 7207.2 seconds.
  Process-tree figures apply to those synthetic workloads. Windows Job
  commit and Linux cgroup memory use different accounting, including cache
  in the Linux figure.
- The three-minute single-shot check covers analytic geometry and continuous
  timing over 4320 frames, plus local rejection tests for missing or stale
  evidence. Blender whitebox rendering, planning-model locks and held-out
  subjects, neural A/B and final neural delivery remain pending.
- Actual 120-minute audiovisual quality, cross-chapter playback review,
  calibrated semantic QC and designated GPU hardware remain pending.
  Official live evidence covers private Kaggle CPU and configured checks.
  Automatic Secret attachment, positive proof for new private Kaggle slots,
  dataset uploads above 64 MiB, and missing historical evidence remain pending.
  Local model inference was skipped in the live check.
- PDX pins and supported Blender versions carry forward. Earlier qualifications
  and open limitations remain documented in the release notes below.

## Release notes (0.1.0rc12)

Version `0.1.0rc12` is published from tag `v0.1.0rc12` at commit
`32517bd28237f23d4cbac9682e375ff52bfaa675`. GitHub and PyPI serve the same
verified files. Use matching parent and worker builds.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| free_studio_flow-0.1.0rc12-py3-none-any.whl | 638221 | `8d04da6e4982816bef195d979517a43ae32d64bb8141277e4c3a3773a22a0636` |
| free_studio_flow-0.1.0rc12.tar.gz | 849704 | `32173d60d2803958aae7d05f560f9392948e04aafad72be5d940611977e84179` |

- Versioned resource and streaming contracts add process-tree monitoring,
  adaptive backpressure and large-artifact paths. Existing profile limits
  and sealed documents retain their versioned behavior.
- Reference analysis provides time mapping, contact sheets and replaceable
  analyzers. Operators can organize cross-shot entities, revise production
  drafts and compare a reference with a verified delivery.
- Versioned edit patches support picture-in-picture composition. A local
  synthetic delivery exercise covered three rounds of subtitle, audio gain,
  order and overlay changes with no additional generation dispatch or charge.
- Private Kaggle CPU evidence covers the verified transfer exercised in
  testing. Dataset uploads above 64 MiB, Gemma/SA-3 and automatic Secret
  attachment remain pending.
- REAL24, ENT12, QC240, neural model A/B, human quality review, long-take
  runtime qualification and designated GPU hardware remain pending.
  The legacy 256 KiB write bound and latency stability across runs remain
  unqualified. The prior Docker sampler failure remains unresolved.

## Release notes (0.1.0rc11)

Version `0.1.0rc11` is published from tag `v0.1.0rc11` at commit
`e49d48d34992ceac4ab7c85ea48a4c7b48ca6e21`. GitHub and PyPI serve
the same verified files. Use matching parent and worker builds.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| free_studio_flow-0.1.0rc11-py3-none-any.whl | 593777 | `6d8f29c4c24b4451e9a1f69cb6914b0ee722f9f2ef4e5af567ac10556d61a16f` |
| free_studio_flow-0.1.0rc11.tar.gz | 803083 | `9d697a401709ef4d1394e215a1eee56cb9b401e7ab718f980ee680c8396646b7` |

 The README rc11
section lists additions and accepted limits, including synthetic media scope,
pending neural model A/B and hardware qualification, unqualified write-volume
bounds across runs and the unresolved initial Docker sampler failure.

## Release notes (0.1.0rc10)

Version `0.1.0rc10` is available on PyPI and GitHub Releases, published from
tag `v0.1.0rc10` at commit `e2c2d0471d5597b32057bb965dff6757edbddc2e`.
Use matching parent and worker builds for the additions below.

- Add an opt-in indexed production authority for task state, shared budget
  admission, durable observation progress, bounded batch continuation and
  cancellation/reconciliation. Unknown submissions retain their reservations.
- Preserve the requested duration when acquiring or renewing production leases
  across wall-clock second boundaries.
- Support quiescent migration and recovery while preserving execution bindings,
  request identities and authorization scope. Descriptor, database and retirement
  markers are not a single cross-file atomic transaction; old writers must stop.
- Stream verified chunks on mounted storage and safely import immutable asset
  packs with version and source/rights checks. Native archive limits and the lack
  of qualified provider byte-offset resume remain explicit.
- Connect operator ASR commands and an optional Whisper worker to dialogue
  coverage, and render ducking without replacing manual gain or human listening.
- Validate static glTF assets with pinned tools, probe a Godot import profile and
  bind spatial placement sidecars to asset hashes. This does not qualify rendered
  appearance, gameplay performance, tracking or physical XR devices.
- Establish Windows subprocess ownership at creation and reclaim owned
  descendants without enumerating unrelated processes on each normal callback.

The store uses SQLite DELETE journaling and FULL synchronization. Qualification
is specific to the tested runtime/filesystem; no WAL, network-filesystem or
arbitrary power-loss durability is implied. Machine checks do not attest human
review, semantic calibration or high-quality feature-film output. Required tools
and models remain operator-configured; examples are not capability whitelists.

## Release notes (0.1.0rc9)

Version `0.1.0rc9` is available on PyPI and GitHub Releases, published from
tag `v0.1.0rc9` at commit `bca390ff1a2cd47009521fd98b0b8b809013305d`.

- Import complete control sources through the production v2 contract and compile
  global core/context segments from declared backend capabilities. The legacy
  short Blender control path retains its original limits.
- Export and restore generated-frame checkpoints, run finite durable parent
  batches, and account for reservations, unknown submissions and bounded repair
  rounds. Resource-pool admission is cooperative accounting, not GPU isolation
  or a guarantee that an external host stops billing.
- Use authorized experimental recipes, provider-neutral SSH, trusted command
  adapters and custom text/image/video/audio/upscale HTTP profiles. The three
  native chat convenience protocols still implement text; they do not define
  the capabilities of those providers or of custom backends.
- Retain declared world state across chapters, check dialogue event/sample
  coverage, and aggregate segment/seam/chapter/film quality evidence. ASR and
  ducking controls require backend/rendering integration. These interfaces do
  not certify film quality, evaluator calibration or human review.
- Recover API v1/v2 credential interruptions without renewing the original
  submission budget, preserve known job identity, and recover a published
  receipt after a ledger-update interruption. Unknown submissions require
  reconciliation before another dispatch.
- Detect and redact classic and stateless GitHub App installation tokens in
  public-tree scanning and process/external-operation diagnostics.
- Stage oversized encoded Kaggle notebook inputs as private `payload.bin`
  datasets with byte-SHA checks. Transport metadata preserves existing file
  licenses instead of declaring the payload public domain.

Native ZIP archives remain bounded to 64 MiB; this route has no byte-offset
transfer qualification. Local chunked checkpoints and HTTP URL streaming are
separate mechanisms. Use short Windows working roots for deeply nested
checkpoint hashes. Unqualified capability probes remain explicitly probe-only.
Actual three-minute neural long takes, two-hour films, large-card hardware and
calibrated visual-quality statistics require separate qualification; they are
not claims made by this release. An optional enhancement backend needs its
own successful tool/model validation before use.

## Release notes (0.1.0rc8)

Version `0.1.0rc8` is available on PyPI and GitHub Releases, published from
tag `v0.1.0rc8` at commit `28eaf9f87e02affc64d1accdb3fbc1db301e4714`:

- Native private Kaggle API submission and output collection, plus SSH/SCP
  dispatch to existing worker disks. The v1 Kaggle route retains its CPU policy.
- Explicit Cycles/CUDA requirements and GPU UUID assignment, with configurable
  device queues, bounded retries, optional within-card sharing and memory
  reservations. GPU rendering and CPU video encoding remain separate.
- Global-frame Blender segments, verified checkpoints and pause/continuation.
  Provider cancellation does not guarantee preservation of uncollected outputs.
- Pinned worker tool preparation, early media-contract checks, scoped host
  credentials and sanitized capability diagnostics.
- Optional Anatomy spring-state control sampling through an external pinned
  checkout and Node runtime, without adding a Studio skill or Python dependency.
- Read-only edit patch-impact reporting and private source-change diagnostics.

Native archives remain limited to 64 MiB, with a separate configured Kaggle
notebook-size limit. Native transfer retries whole archives, not byte offsets.
Worker recipes and tools require host-specific verification; these changes do
not certify every GPU architecture, model or provider modality. Technical QC
does not grant motion or listening approval.

## Release notes (0.1.0rc7)

Version `0.1.0rc7` is available on PyPI and GitHub Releases:

- Locate and edit clips by stable IDs and revision-bound patches, with explicit
  subtitle timing and independent post-production plans for verified sources.
- Mix selected audio tracks and ranges, with optional loudness normalization,
  encoded-output peak checks and A/B previews. Numeric checks do not replace
  listening review.
- Render trusted small Blender scenes, export reference images and clay motion
  controls, and import existing control videos without implicit conversion.
- Register immutable asset versions and verify fixed-version lookups.
- Dispatch approved parent/worker recipes through local, HTTP, SSH and private
  CPU Kaggle transports, with verified result collection and bounded recovery.
- Configure typed text and media API profiles, host credential bindings and
  per-shot QC retries. Provider/model capabilities and manual review remain
  separate requirements; text support does not imply every media type.

## Release notes (0.1.0rc6)

- Add DOCX metadata for stable task IDs, asset references, continuity aspects and
  required consistency review. The existing intake and plan commands compile these
  declarations into consistency contexts without a separate conversion script.
- Preserve existing untagged-sheet behaviour and reject invalid declarations before
  execution. Visual consistency still requires review of real generated artifacts.

## Release notes (0.1.0rc5)

- **Consistency Review Gate** (`fsf review`): Assembly is blocked until all shots with
  `review_required: true` have an approved review record. The approval is bound to the
  artifact SHA at approval time; a changed file on disk invalidates the approval and
  blocks assembly again. The gate is re-evaluated on every resume.
  CLI: `review list`, `review status`, `review approve`, `review reject [--needs-rework]`.
- **Assembly input producer binding**: The plan compiler writes the producing step ID
  into each assembly input entry at compile time. The artifact path and SHA-256 are
  read from the execution journal after the step completes. Missing or ambiguous
  producers block assembly immediately.
- **Shape/mock alias gating**: `.shape.bin` ↔ `.mock.bin` alias expansion is restricted
  to confirmed shape execution kinds (`mock`, `shape`). Real-media executors
  must write the declared path exactly; a mismatched path blocks with a diagnostic message.
- **Consistency requirements digest**: Steps with `review_required: true` persist a
  requirements digest alongside the review record; a changed consistency context
  invalidates previously recorded approvals.
- **Resume gate coverage**: The assembly gate is re-evaluated on the resume path,
  preventing a previously withdrawn review from being silently reused.

## Release notes (0.1.0rc4)

- Add asynchronous agent Handoff through `fsf execute --handoff`, with
  digest-bound workorders and validated result ingestion.
- Allow rejected Handoff results to be retried with a new attempt ID.
- Improve Windows subprocess cleanup, including descendant processes when
  `taskkill` fails.
- Preserve explicit source audio contracts under `--audio keep`.
- Strengthen public-tree hygiene checks for sensitive paths and credentials.

## Release notes (0.1.0rc3)

- **Plan Execution Runtime (`fsf execute`)**: Orchestrates complete `pdx_execution_plan_v1`
  execution DAGs topologically. Evaluates steps, transforms (Fast Assembly), and QC gates
  with fail-closed conformance validation and authoritative resume invalidation.
- **Fast Assembly & Dual-Track Audio Mixing**: Extends fast assembly concatenation with
  background music (BGM) mixing and per-shot voice track layering (`amix` filter), along with
  sidecar and burned subtitle support.
- **Process Command Executor (`process_cmd`)**: Enables local external CLI tool execution
  via argument vectors, isolated scratch output files, atomic artifact publishing,
  and comprehensive secret redaction on command errors and timeouts.
- **Media-Aware Plan Compilation**: `fsf plan` now supports `--media` and `--shape` flags
  to configure real media output routing (`.mp4`, `.wav`) or contract stubs, as well as
  `--assembly`, `--subtitles`, and `--bgm` pipeline flags.
- **Configurable Executor Registry**: Supports loading process and extra executors from
  `fsf-executors.json` and the `FSF_EXECUTORS_FILE` environment variable.
- **Documentation & Release Governance Alignment**: Complete documentation of `fsf execute`,
  fast assembly options, process executor examples, and updated PyPI verified-release status tracking.

## Git pin vs published PyPI

Git commit pins on `main` are the authority until a tag is published.
PyPI versions are immutable: do not rebuild or re-upload an already-published
version.

```powershell
python -m pip install "free-studio-flow==0.1.0rc10"
```

The command above installs rc10. To develop against a checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Unix: `.venv/bin/python` instead of `.venv\Scripts\python.exe`.

## Change policy

- Security and correctness fixes may land without a new public API when they
  preserve CLI commands, plan schema (`pdx_execution_plan_v1`), and Studio
  skill names.
- Breaking CLI, executor IDs, transport contracts, or PDX pin changes need a
  new version. Coordinate pin bumps with the freeze records under
  `compatibility/`.
- This package is CLI/skill only. Do not add a Studio desk or other GUI here.

## Release gate

For maintainers publishing from a source checkout only. End users do not
need to run these checks or provide Kaggle credentials to install FSF.
The scripts and tests are included in the source archive, not the wheel.
Verification logs may contain account identifiers and local paths; keep
them private and exclude them from release attachments.

1. Run public hygiene: `.venv/Scripts/python scripts/check_public_hygiene.py`
2. Run offline tests: `.venv/Scripts/python -m pytest tests -m "not live" -v`
3. Run the live gate on an operator machine with Kaggle credentials:
   `.venv/Scripts/python scripts/agent_verify.py`
4. Run `.venv/Scripts/python scripts/verify_clean_install.py`
5. Build and check the wheel and source archive, then publish the same files
   to GitHub Releases and PyPI.

```powershell
.\.venv\Scripts\python.exe -m pip install build twine
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m twine check dist\*
```

Published distribution filenames for `0.1.0rc10`:

- `dist/free_studio_flow-0.1.0rc10-py3-none-any.whl`
- `dist/free_studio_flow-0.1.0rc10.tar.gz`

For future releases, build from the final commit and publish the approved
distributions unchanged to GitHub Releases and PyPI. PyPI publication requires
explicit manual dispatch of `.github/workflows/release.yml` and environment approval.

## PyPI trusted publication

The release workflow downloads the already-approved wheel and source archive,
checks package metadata and GitHub SHA-256 digests, installs the tagged
source in CI (`pytest -m "not live"` plus hygiene plus clean-install), and
promotes the unchanged files to PyPI. It never rebuilds a second set of files
for PyPI. Live Kaggle/API tests stay on operator machines; they are not a
GitHub Actions secret path.

Create a pending or existing-project Trusted Publisher with:

- PyPI project: `free-studio-flow`
- GitHub owner: `freestudioflow`
- Repository: `free-studio-flow`
- Workflow filename: `release.yml`
- Environment: `pypi`

Protect the `pypi` GitHub environment with a required reviewer
(Settings → Environments → `pypi`). No long-lived PyPI token belongs in
repository secrets. Creating a GitHub Release
does not trigger publication. To promote an approved release, run
**Publish release assets to PyPI** manually with its exact tag.

For each release, the operator live gate must pass on the release source
before creating its tag or GitHub Release. A separate manual workflow dispatch
uploads the approved distributions to PyPI after environment approval.

After PyPI exposes the version through its JSON API, update the verified-release
block locally, review the change, and commit it with the configured project identity:

```powershell
.\.venv\Scripts\python.exe scripts\update_readme_after_publish.py --version <version> --tag <tag> --commit <release-commit>
```
