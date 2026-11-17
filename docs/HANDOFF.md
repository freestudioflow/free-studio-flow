# Handoff Operations and Constraints

*Introduced in rc4; consistency acknowledgement added in rc5. For the step-by-step tutorial (`demo --handoff` and operator-approved workflow), see [Getting Started](GETTING_STARTED.md#5-handoff-tutorial).*

`fsf execute` can delegate media-generation tool steps to an asynchronous external agent.
Enable Handoff via any of:

```text
# Delegate all media-generation steps (Unix: .venv/bin/python)
.venv/Scripts/python -m fsf.cli execute plan.json --out output/execute --handoff

# Delegate via executor override
.venv/Scripts/python -m fsf.cli execute plan.json --out output/execute --executor handoff

# Per-step: set executor_id in the plan step inputs
inputs.executor_id = "handoff"
```

On the first run FSF emits workorders and exits with `status: "pending_handoff"` (exit 10).
Use `status <out_dir> --json` (and `workorder list` / `workorder input`) to inspect active workorders,
write the generated media to `write_artifact_to`, and run `workorder fulfill`
to validate outputs and write `<workorder_id>.result.json` into `<out_dir>/handoff/results/`, then
re-run the execute command (`next_action.argv` when present); FSF ingests the results and continues with QC
and assembly steps (those are always executed by FSF, never delegated).

**Workorder schema**: `fsf_workorder_v1`

Workorder fields used to prepare and validate a result:

| Field | Description |
|---|---|
| `workorder_id` | Unique per `job_id / run_id / step_id / attempt_id` |
| `target_artifact` | Exact output path the executor must write |
| `plan_digest` | SHA-256 of the compiled plan; must be echoed in the result |
| `inputs_digest` | SHA-256 of step input bindings; must be echoed in the result |
| `expected_contract_digest` | SHA-256 of the media QC contract; must be echoed if set |
| `workorder_digest` | SHA-256 for detecting workorder changes; not a signature |

**Acceptable result JSON** (`fsf_workorder_result_v1`):

```json
{
  "schema_version": "fsf_workorder_result_v1",
  "workorder_id":   "<copy from workorder>",
  "job_id":         "<copy from workorder>",
  "run_id":         "<copy from workorder>",
  "step_id":        "<copy from workorder>",
  "attempt_id":     "<copy from workorder>",
  "plan_digest":    "<copy from workorder>",
  "inputs_digest":  "<copy from workorder>",
  "expected_contract_digest": "<copy from workorder, or omit if null>",
  "status":         "completed",
  "artifact_path":  "<must equal workorder target_artifact exactly>",
  "artifact_sha256": "<sha256 of the written file>",
  "detail":         "optional free-text"
}
```

**Handoff cycle**:

1. **Emit workorder** — FSF writes a digest-bound `fsf_workorder_v1` to
   `<out_dir>/handoff/workorders/<workorder_id>.workorder.json`.
2. **Ingest result** — after the executor writes the output file and places
   `<workorder_id>.result.json` in `<out_dir>/handoff/results/`,
   `ingest_workorder_result()` verifies path confinement, zero-byte guard,
   SHA-256, and all mandatory digest fields.
   Violations raise `HandoffIntegrityError` or `HandoffSecurityError`.
3. **Retry on rejection** — re-run the same command to receive a new attempt.
   Use the new workorder's identifiers and digests when submitting a replacement result.

**Constraints**:

- Do **not** run `verify` on `.shape.bin` or `.mock.bin`; those are contract stubs, not media.
- `artifact_path` in the result must equal the workorder's `target_artifact` exactly; FSF
  does not accept substitutions. SHA-256 conflict detection rejects results whose digest
  differs from a previously accepted result for the same step.
- Path traversal in `target_artifact` is rejected unconditionally.
- Copy the result's identity and digest fields from the current workorder. Preserve the integer type of `attempt_id`; include `expected_contract_digest` when it is set.

## Consistency acknowledgement in Handoff results

When the workorder carries a `requirements_digest` field, the result **must** include
`consistency_acknowledgement`. Submitting a result without it, or with a mismatched
`acknowledged_digest`, causes ingest to raise `HandoffIntegrityError`.

**Full result example with consistency fields**:

```json
{
  "schema_version": "fsf_workorder_result_v1",
  "workorder_id":   "<copy from workorder>",
  "job_id":         "<copy from workorder>",
  "run_id":         "<copy from workorder>",
  "step_id":        "<copy from workorder>",
  "attempt_id":     1,
  "plan_digest":    "<copy from workorder>",
  "inputs_digest":  "<copy from workorder>",
  "expected_contract_digest": "<copy from workorder, or omit if null>",
  "status":         "completed",
  "artifact_path":  "<must equal workorder target_artifact exactly>",
  "artifact_sha256": "<sha256 of the written file>",
  "detail":         "optional free-text",
  "consistency_acknowledgement": {
    "acknowledged_digest": "<copy workorder.requirements_digest verbatim>",
    "ref_assets_used": [
      { "key": "<key from workorder.consistency_requirements.ref_image_shas>",
        "sha256": "<sha256 that matches the declared value>" }
    ],
    "continuity_method": "<method used — see accepted values below>"
  }
}
```

> **`attempt_id`**: copy the integer value from the current workorder unchanged.
> Do not hardcode `1`; the workorder may carry a higher attempt number on retry.

> **`continuity_method`**: only required when `workorder.consistency_requirements.resolved_sha256`
> is set. Use the value that describes how the continuity source was actually used.
> `"prompt_only"` is rejected when `workorder.consistency_requirements.prompt_only_allowed`
> is `false`.

**`consistency_acknowledgement` field rules**:

| Field | When required | Accepted values |
|---|---|---|
| `acknowledged_digest` | Always when `requirements_digest` is set | Exact copy of `workorder.requirements_digest` |
| `ref_assets_used` | When workorder declares `ref_image_shas` | Array of `{key, sha256}` — every declared key must appear; no duplicates; SHA must match |
| `continuity_method` | When workorder declares a `resolved_sha256` in continuity | One of: `first_frame_init`, `ip_adapter`, `reference_latent`, `reference_image`, `video_continuation`, `prompt_only` |
| `capability_gap` | Never on a `completed` result | A non-empty value with `status: completed` is rejected — report `status: failed` with the gap in `detail` instead |

**Placement rule**: `capability_gap`, `ref_assets_used`, and `continuity_method` must be
nested inside `consistency_acknowledgement`. Top-level placement is rejected outright.

**What acknowledgement proves**: that the agent received the requirements vector. It does
not prove visual compliance; human review via `fsf review approve` remains the
authoritative compliance gate. See [Consistency Review Gate](CONSISTENCY_REVIEW.md).
