# Release policy

Free Studio Flow publishes the same way ProDocuX does: Git tags and GitHub
Releases are the approval boundary; PyPI receives the already-approved wheel
and source archive unchanged through Trusted Publishing. There is no
long-lived PyPI token.

The current published pre-release is **`0.1.0rc6`**, available on PyPI.
Existing PyPI versions are immutable and must not be rebuilt or re-uploaded.
Runtime pins
remain `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, and
`pdx-adapter-media==0.2.0a3`. Do not publish a stable `0.1.0` until those
pins leave rc/a.

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
python -m pip install "free-studio-flow==0.1.0rc6"
```

The command above installs the published rc6 release. To use unreleased
changes, install from the checkout:

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

Published artifacts for `0.1.0rc6`:

- `dist/free_studio_flow-0.1.0rc6-py3-none-any.whl`
- `dist/free_studio_flow-0.1.0rc6.tar.gz`

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
(Settings → Environments → `pypi`). Enable “Allow GitHub Actions to create
and approve pull requests” so the README follow-up PR can open. No
long-lived PyPI token belongs in repository secrets. Creating a GitHub Release
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
