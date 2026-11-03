# Indexed production workflows

This page describes the current source checkout. Check the installed package's
CLI help before using these additions with an earlier published package.

The indexed production store is an opt-in, single-host authority. Existing JSON
workflows keep their existing commands. A store coordinates tasks, submission
intents and a shared authorization budget; it does not supply a model or certify
the semantic quality of a film.

Create a plan, an operator authorization and an operator command-driver
configuration using their packaged contracts. Then run:

```text
fsf production store-create plan.json --authorization authorization.json --out work/store
fsf production run-batch-v2 plan.json --store work/store --driver driver.json --seconds 300
fsf production store-inspect work/store
fsf production store-check work/store
```

The processing budget is cooperative. Initialization, an in-progress callback
and process cleanup can extend elapsed time. No new callback is admitted after
the batch budget expires. An admitted callback finishes within its configured
timeout; intent-commit latency does not shorten an admitted submission to a few
milliseconds. Billing observation can continue in the next batch while its
worst-case charge remains held. A lost submission reply requires reconciliation, not a
new request. Missing billing receipts retain the reserved worst-case amount.
The v2 command driver accepts an explicit settlement receipt; the v1 driver
continues to retain unknown charges.

`machine_accepted` describes the configured machine checks. Human review remains
separate. An observation sweep can continue across batches. `--delivery` instead
requires a fresh validation window and returns `needs_validation` if that window
cannot finish. Neither result is an atomic snapshot of external mutable files or
a promise that an external approval will never be revoked.

## Authority, cancellation and migration

Use a common `FSF_PRODUCTION_AUTHORITY_ROOT` for cooperating stores that share an
operator budget domain. The default registry is beside the store's parent
directory. Keep that registry with the execution environment. It prevents
accidental duplicate authorities within its scope; it is not a distributed
service, a signature, or protection against a privileged operator rewriting the
registry and database.

```text
fsf production store-pause work/store --execution-id film
fsf production store-resume work/store --execution-id film
fsf production store-cancel work/store --plan plan.json --driver driver.json --request-id REQUEST --epoch 1
fsf production store-backup work/store --out snapshots/store.sqlite3
fsf production store-export work/store --out snapshots/store.json
```

Replace `film` with the plan's production identity and `REQUEST` with the actual
attempt ID. Cancellation records intent before contacting the provider. An
unsupported or unknown cancellation is not confirmation of a stopped job.
Stopping a job does not release unknown charges. Backups and exports are data,
not independently executable copies of an authorization.

Migration requires a complete, quiescent authorization domain, immutable
execution bindings, original request IDs and verified source artifacts. Running
or unknown provider work must be reconciled first. Use the packaged migration
manifest contract; its relative source paths resolve beside the manifest file.

```text
fsf production store-migrate migration.json --out work/store
fsf production store-recover work/store --source-root legacy
```

Recovery finishes only a sealed cutover. The descriptor, database and retirement
markers are not one cross-file atomic transaction. Compatible JSON writers
respect retirement markers; an older binary cannot be physically fenced by a
marker it does not understand. Keep old writers stopped throughout migration.

The store uses SQLite DELETE journaling, FULL synchronization and bounded lock
waits. Qualification is specific to the tested local filesystem and runtime.
Do not infer network-filesystem, WAL or arbitrary power-loss guarantees.

## Large artifacts on mounted storage

The chunk interface verifies ordered chunk hashes, total size and final bytes.
Create the chunk directories before invoking these commands:

```text
fsf production split-artifact source.mp4 --out source-chunks
fsf production transfer-chunks manifest.json --source source-chunks --out received-chunks
fsf production assemble-artifact manifest.json --chunks received-chunks --out restored.mp4
```

Use the manifest emitted by `split-artifact`. Completed, verified chunks can be
reused while the same source bytes remain available. This is chunk recovery on
mounted storage, not an HTTP byte-offset resume guarantee. Native archive
transports retain their own limits; a large local transfer does not qualify a
provider's output route.

## Speech and derived ducking

`speech-recognize` runs an authorized operator command adapter. Expected words
are not provided to that backend as a prompt. The optional
`fsf.tools.faster_whisper_worker` example uses separately installed dependencies
and pinned local weights; other ASR implementations can use the same command
contract.

The packaged `fsf_asr_command_v1` schema declares the command, working directory,
processing timeout, credential environment-variable names, pinned executable and
support files, model-lock digest and canonical recognizer digest. Actual
credential values stay outside the document. This command contract does not
require the example model, a particular provider or a particular compute device.

```text
fsf production speech-recognize voice.wav --expected expected.json --recognizer recognizer.json --work recognition --out transcript.json --seconds 300
fsf production speech-check expected.json --transcript transcript.json --policy coverage-policy.json --artifact-sha256 SHA
fsf production ducking-envelope coverage.json --track-id BGM --attenuation-millidb 12000 --attack-samples 240 --release-samples 240 --out envelope.json
fsf production ducking-render music.wav --envelope envelope.json --out derived-audio
```

Save the successful `speech-check` result as `coverage.json` and replace `SHA`
with the actual audio digest. Time-window assignment in the example is not
speaker diarization. Unassigned or ambiguous speech is retained for coverage
checks. Ducking creates a separate PCM WAV and lineage receipt. Import that WAV
through the normal audio workflow. Recompute coverage and the derivation when
positions change; do not silently apply an old envelope to a new timeline.
Final encoded loudness, peak and padding checks remain necessary. Numeric QC
does not replace listening review.

## Static assets and spatial delivery

Pack imports publish a new immutable generation. `pack-delta` describes additions,
reuse and removal; it is not a minimal transmitted patch archive. A snapshot
selects one explicit version per asset identity. Same-path content conflicts do
not overwrite an existing generation.

The static-room reference profile uses a pinned Khronos Validator and a controlled
Godot importer. `delivery-profile`, `spatial-check` and `scene-inspect` separate
format, bounds, materials, collisions, appearance and device status. External
projects, addons and arbitrary scripts are not imported by this harness.
Headless import and collision probes do not certify runtime FPS, visual quality,
headset tracking or AR alignment. Other consumers require their own operator
adapter and qualification profile.
