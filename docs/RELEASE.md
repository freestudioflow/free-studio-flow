# Release policy

Free Studio Flow publishes the same way ProDocuX does: Git tags and GitHub
Releases are the approval boundary; PyPI receives the already-approved wheel
and source archive unchanged through Trusted Publishing. There is no
long-lived PyPI token.

The first public package is pre-release **`0.1.0rc1`** while runtime pins
remain `prodocux==0.3.0rc7`, `pdx-artifact-engine==0.3.0a8`, and
`pdx-adapter-media==0.2.0a3`. Do not publish a stable `0.1.0` until those
pins leave rc/a.

## Git pin vs published PyPI

Git commit pins on `main` are the authority until a tag is published.
PyPI versions are immutable: do not rebuild or re-upload an already-published
version.

```powershell
python -m pip install "free-studio-flow==0.1.0rc1"
```

Use that command only after the README verified-release block records a PyPI
JSON hit. Until then, install from the checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Unix: `.venv/bin/python` instead of `.venv/Scripts/python.exe`.

## Change policy

- Security and correctness fixes may land without a new public API when they
  preserve CLI commands, plan schema (`pdx_execution_plan_v1`), and Studio
  skill names.
- Breaking CLI, executor IDs, transport contracts, or PDX pin changes need a
  new version. Coordinate pin bumps with the freeze records under
  `compatibility/`.
- Studio desk / Farpals UI, B-roll generator features, and PDX Kernel/Engine
  work stay outside this repository.

## Release gate

1. Run public hygiene: `.venv/Scripts/python scripts/check_public_hygiene.py`
2. Run offline tests: `.venv/Scripts/python -m pytest tests -m "not live" -v`
3. Run the live gate on an operator machine with Kaggle credentials:
   `.venv/Scripts/python scripts/agent_verify.py`
4. Run `.venv/Scripts/python scripts/verify_clean_install.py`
5. Build once, check metadata, and attach **those** files to the GitHub
   Release. Do not rebuild a second set for PyPI.

```powershell
.\.venv\Scripts\python.exe -m pip install build twine
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m twine check dist\*
```

Expected artifacts for `0.1.0rc1`:

- `dist/free_studio_flow-0.1.0rc1-py3-none-any.whl`
- `dist/free_studio_flow-0.1.0rc1.tar.gz` (or the PEP 625 hyphenated sdist
  name `free-studio-flow-0.1.0rc1.tar.gz`)

Tag `v0.1.0rc1` only after maintainers approve those files. Create a GitHub
Release on that tag and attach the two distributions. Publishing the Release
starts `.github/workflows/release.yml`.

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
long-lived PyPI token belongs in repository secrets. Future GitHub Releases
start the workflow automatically. To promote an existing release, run
**Publish release assets to PyPI** manually with its exact tag.

Do not create tag `v0.1.0rc1` or a GitHub Release until the operator live
gate has passed on this tree. Publishing the Release is what uploads to
PyPI.

After PyPI exposes the version through its JSON API, the workflow opens a
README-only PR that updates the verified-release block.
