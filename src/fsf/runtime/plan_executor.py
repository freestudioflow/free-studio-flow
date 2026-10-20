"""FSF Plan Executor (Deterministic DAG Orchestration & Strict QC Closed Loop).

Orchestrates deterministic execution of pdx_execution_plan_v1 plans:
- Builds topological DAG of steps from depends_on.
- Dispatches tool steps (using registry or ProcessCommandExecutor with isolated scratch).
- Routes explicit upstream artifacts ({input}, {input_video}) and target filenames.
- Dispatches transform steps (fsf.assembly.fast_concat) with compile-time artifact routing
  and verifies conformance report before reporting completed.
- Evaluates Media QC steps (pdx.media.technical_conform) and enforces strict QC gates:
  any status other than 'conforms' immediately blocks all downstream dependent steps.
- Enforces authoritative resume bound to:
  (plan_digest, source_sha256, step_inputs_digest, step_executor_config_digest, output_files_shas).
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pdx_artifact_core import validate_execution_plan

from fsf.assembly.fast_assembly import FastAssemblyReceipt, fast_assemble_timeline
from fsf.conformance.digest import canonical_contract_digest
from fsf.conformance.media import evaluate_media_file
from fsf.core.consistency import ConsistencyResolutionError, resolve_execution_context
from fsf.runtime.consistency_review import check_assembly_gate, load_reviews
from fsf.executors.process_cmd import sanitize_process_text
from fsf.executors.registry import DEFAULT_REGISTRY, ExecutorRegistry
from fsf.runtime.external_op import scrub_dict_secrets, SHAPE_EXECUTION_KINDS
from fsf.runtime.handoff import (
    HandoffIntegrityError,
    HandoffSecurityError,
    build_consistency_requirements,
    build_workorder,
    find_result_file,
    ingest_workorder_result,
    write_workorder,
)


class PlanExecutionError(RuntimeError):
    """Raised when plan execution halts due to blocked or failed steps."""


class PlanExecutor:
    """Deterministic orchestrator for pdx_execution_plan_v1."""

    def __init__(
        self,
        output_dir: Path | str,
        *,
        registry: ExecutorRegistry | None = None,
        no_resume: bool = False,
        executor_override: str | dict[str, str] | None = None,
        handoff: bool = False,
        job_id: str | None = None,
        run_id: str | None = None,
        source_executions: dict | None = None,
        audio_resources: dict | None = None,
        recovery_policy: dict | None = None,
        resume_paused: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry or DEFAULT_REGISTRY
        self.no_resume = bool(no_resume)
        self.executor_override = executor_override
        self.handoff = bool(handoff)
        self.job_id = str(job_id) if job_id else None
        self.run_id = str(run_id) if run_id else None
        self.audio_resources = audio_resources
        self.source_executions = source_executions
        self.recovery_policy = recovery_policy
        self.resume_paused = resume_paused
        self._recovery_controller = None
        self._recovery_force = set()
        self._recovery_attempt = 1
        self._post_context = None
        self.generation_dispatches = {"process": 0, "api": 0, "handoff": 0}
        self.current_job_id: str = ""
        self.current_run_id: str = ""
        self.current_plan_digest: str = ""
        self._artifact_registry: dict[str, Path] = {}
        self._steps_by_id: dict[str, dict[str, Any]] = {}

    def _is_handoff_tool_step(self, step: dict[str, Any]) -> bool:
        """True if the tool step delegates media generation to asynchronous Handoff."""
        if step.get("kind") != "tool":
            return False
        tool = step.get("tool", "")
        if tool == "pdx.media.technical_conform":
            return False
        if self.handoff:
            return True
        if self.executor_override == "handoff":
            return True
        if isinstance(self.executor_override, dict) and self.executor_override.get(tool) == "handoff":
            return True
        if step.get("inputs", {}).get("executor_id") == "handoff":
            return True
        return False

    def resolve_artifact_path(self, artifact_ref: str | None) -> Path | None:
        """Resolve an artifact URI/filename to a local Path."""
        if not artifact_ref:
            return None
        if artifact_ref in self._artifact_registry:
            return self._artifact_registry[artifact_ref]
        clean_name = artifact_ref.replace("artifact:", "").replace("report:", "")
        candidate = Path(clean_name)
        if candidate.is_absolute():
            return candidate
        return self.output_dir / clean_name

    # Positive allowlist of video / shape-class artifact suffixes.
    # Using a whitelist (rather than a blacklist) prevents PNG, SRT, VTT,
    # and other non-video types from being mistakenly selected as a
    # continuity video source.
    _VIDEO_SUFFIXES: frozenset[str] = frozenset(
        {".mp4", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m2ts"}
    )
    # Compound name-suffixes valid ONLY in shape/test mode.
    # .dub.json is a JSON configuration for dubbing — never a video source.
    _SHAPE_NAME_SUFFIXES: tuple[str, ...] = (".shape.bin", ".mock.bin")

    @classmethod
    def _is_video_artifact(cls, fpath: str, *, shape_mode: bool = False) -> bool:
        """Return True iff *fpath* is a video or shape-class (generated media) artifact.

        shape_mode (default False):
          - False (real-media execution): only real video extensions qualify.
            ``.shape.bin`` and ``.mock.bin`` are explicitly excluded because
            they are test stubs, not real video media.
          - True (shape / offline-test execution): ``.shape.bin`` and
            ``.mock.bin`` are also accepted as they ARE the generated artifact
            in test mode.

        ``.dub.json`` is intentionally excluded: it is a JSON configuration
        artefact produced by the Dub skill, not a video continuity source.

        Checks compound name-suffixes first (e.g. ``.shape.bin``), then
        single-extension video types.  Audio, JSON, image, subtitle, and
        other non-video types return False.
        """
        name = Path(fpath).name.lower()
        if shape_mode:
            for compound in cls._SHAPE_NAME_SUFFIXES:
                if name.endswith(compound):
                    return True
        return Path(fpath).suffix.lower() in cls._VIDEO_SUFFIXES

    def _verify_source_file_sha(
        self,
        file_path: Path,
        journal_sha: str,
        label: str,
    ) -> str:
        """Read *file_path*, compute SHA-256, verify against *journal_sha*.

        Returns the verified SHA-256.
        Raises ConsistencyResolutionError on mismatch, missing file, or I/O error.
        """
        from fsf.core.consistency import ConsistencyResolutionError

        if not file_path.is_file():
            raise ConsistencyResolutionError(
                f"Source artifact not found on disk: {file_path}  [{label}]"
            )
        try:
            actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ConsistencyResolutionError(
                f"Cannot read source artifact {file_path}: {exc}  [{label}]"
            ) from exc
        if actual != journal_sha:
            raise ConsistencyResolutionError(
                f"Source artifact SHA-256 mismatch [{label}]: "
                f"journal={journal_sha[:16]}… disk={actual[:16]}….  "
                f"The file has changed since it was recorded."
            )
        return actual

    def _find_video_sha_in_step_result(
        self,
        step_id: str,
        step_res: dict[str, Any],
        step_def: dict[str, Any],
        label: str,
    ) -> str | None:
        """Return verified on-disk SHA of the primary video/shape artifact of *step_id*.

        Uses a positive allowlist (_is_video_artifact) — only video and shape
        class files qualify.  Whether ``.shape.bin`` / ``.mock.bin`` are valid
        is determined by ``step_res["execution_kind"]``:

        - ``"shape"`` execution: shape/mock artifacts ARE the generated media;
          they are accepted.
        - All other execution kinds (``"media"``, ``None``, unknown): only real
          video extensions (``.mp4``, ``.mov``, etc.) are accepted.  Shape stubs
          produced by an upstream shape-mode step must never become continuity
          sources for a real-media execution.

        ``.dub.json`` is always excluded: it is a JSON config, not a video.

        A qualifying entry MUST have a journal record (output_files entry with
        a non-empty sha256).  Files that exist on disk but lack a journal entry
        are NEVER trusted as continuity sources.

        Raises ConsistencyResolutionError when a qualifying journal entry exists
        but the disk copy is missing or its SHA does not match.
        """
        is_shape_mode = step_res.get("execution_kind") in SHAPE_EXECUTION_KINDS

        # Only look in journal output_files — no unauthenticated disk fallback.
        for f in step_res.get("output_files", []):
            fpath = f.get("path", "")
            if not self._is_video_artifact(fpath, shape_mode=is_shape_mode):
                continue
            journal_sha = str(f.get("sha256", ""))
            if not journal_sha:
                continue
            art_path = (
                self.output_dir / fpath
                if not Path(fpath).is_absolute()
                else Path(fpath)
            )
            return self._verify_source_file_sha(
                art_path, journal_sha, label=f"{label}::journal/{fpath}"
            )
        # No qualifying journal entry — source not ready or step produced no video
        return None

    def _resolve_continues_from_sha(
        self,
        continues_from: dict[str, Any],
        step_results: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Resolve {source_shot_id -> sha256} for a continues_from declaration.

        Implements the two selection_rule modes with strict declaration compliance:

        - ``specific_artifact``: Locates the exact step whose ``outputs`` list
          contains the declared ``artifact_ref``.  Verifies the artifact exists
          on disk and its SHA matches the journal.  Raises if not found or if
          multiple steps claim the same output ref.

        - ``latest_completed`` (default): Scans ALL completed steps for the
          source shot_id (not just direct depends_on) to find the most recently
          completed step that produced a video/shape-class artifact (positive
          allowlist via _is_video_artifact).  If the primary media-producing
          step is followed by QC or Dub terminal steps (which produce only JSON
          or audio), the scan naturally finds the correct step by iterating
          backwards through topo order.  Verifies the file on disk against the
          journal SHA; no unauthenticated disk-read fallback.

        Returns an empty dict when the source shot has not yet completed.
        Raises ConsistencyResolutionError when:
        - selection_rule is 'specific_artifact' but artifact_ref is absent
        - the declared artifact_ref cannot be found among completed steps
        - the located file is missing, unreadable, or its SHA mismatches
        - the result is ambiguous (multiple steps produce the same artifact_ref)
        """
        from fsf.core.consistency import ConsistencyResolutionError

        src_shot_id = continues_from.get("shot_id", "")
        selection_rule = continues_from.get("selection_rule", "latest_completed")
        artifact_ref = continues_from.get("artifact_ref")

        if not src_shot_id:
            return {}

        # Collect all completed steps for the source shot (in topo order)
        src_step_ids = [
            sid
            for sid in self._topo_order  # type: ignore[attr-defined]
            if self._steps_by_id.get(sid, {}).get("inputs", {}).get("shot_id") == src_shot_id
            and step_results.get(sid, {}).get("status") in ("completed", "reused")
        ]

        if not src_step_ids:
            # Source shot not yet completed — caller treats absence as "not ready"
            return {}

        if selection_rule == "specific_artifact":
            if not artifact_ref:
                raise ConsistencyResolutionError(
                    f"continues_from has selection_rule='specific_artifact' for shot "
                    f"'{src_shot_id}' but no artifact_ref was declared."
                )
            matches = [
                sid
                for sid in src_step_ids
                if artifact_ref in self._steps_by_id[sid].get("outputs", [])
            ]
            if not matches:
                raise ConsistencyResolutionError(
                    f"continues_from artifact_ref='{artifact_ref}' not found in any "
                    f"completed step for source shot '{src_shot_id}'."
                )
            if len(matches) > 1:
                raise ConsistencyResolutionError(
                    f"Ambiguous source: artifact_ref='{artifact_ref}' is claimed by "
                    f"multiple completed steps: {matches}.  "
                    f"Each output artifact must be produced by exactly one step."
                )
            step_id = matches[0]
            step_res = step_results[step_id]

            # Resolve the canonical absolute path for the declared artifact_ref.
            # This is the authoritative identity: two refs with the same filename
            # but different directory components (e.g. "generate/clip.shape.bin"
            # vs "remaster/clip.shape.bin") must resolve to different paths and
            # must never be conflated.
            canonical_art_path = self.resolve_artifact_path(artifact_ref)

            # Find the journal entry whose resolved path matches the canonical path.
            # Journal entries store relative paths from output_dir (e.g.
            # "generate/clip.shape.bin"); matching by full resolved path prevents
            # same-filename-different-directory collisions.
            # A file that exists on disk but has NO journal record is NEVER
            # accepted — absence of a journal entry means the step did not
            # officially produce that artifact.
            for f in step_res.get("output_files", []):
                fpath = f.get("path", "")
                journal_sha = str(f.get("sha256", ""))
                if not journal_sha:
                    continue
                f_full = (
                    self.output_dir / fpath
                    if not Path(fpath).is_absolute()
                    else Path(fpath)
                )
                # Full-path comparison (resolves symlinks & normalises separators)
                if canonical_art_path and f_full.resolve() == canonical_art_path.resolve():
                    # Validate artifact type BEFORE verifying SHA.
                    # Both selection rules must enforce the same type + mode rules:
                    # - .dub.json is never a valid video source
                    # - .shape.bin/.mock.bin require execution_kind in SHAPE_EXECUTION_KINDS
                    is_shape = step_res.get("execution_kind") in SHAPE_EXECUTION_KINDS
                    if not self._is_video_artifact(fpath, shape_mode=is_shape):
                        raise ConsistencyResolutionError(
                            f"continues_from artifact_ref='{artifact_ref}' resolves "
                            f"to '{fpath}' which is not a valid video/media artifact "
                            f"(execution_kind={step_res.get('execution_kind')!r}).  "
                            f"Shape stubs (.shape.bin/.mock.bin) are only valid as "
                            f"continuity sources when execution_kind is a shape kind "
                            f"{sorted(SHAPE_EXECUTION_KINDS)!r}.  "
                            f".dub.json is never a valid continuity source."
                        )
                    verified = self._verify_source_file_sha(
                        f_full, journal_sha,
                        label=f"specific_artifact={artifact_ref}"
                    )
                    return {src_shot_id: verified}

            # No matching journal entry — reject.
            # Note: a fallback that reads the file from disk without a journal
            # record is intentionally absent; it would bypass the integrity
            # guarantee that the SHA was recorded at step-completion time.
            raise ConsistencyResolutionError(
                f"continues_from artifact_ref='{artifact_ref}' is declared in step "
                f"'{step_id}' outputs but has no matching journal record.  "
                f"The step must record this artifact in output_files (with sha256) "
                f"before it can be used as a continuity source."
            )

        else:  # latest_completed — scan backwards (last completed step first)
            for step_id in reversed(src_step_ids):
                step_res = step_results[step_id]
                step_def = self._steps_by_id[step_id]
                sha = self._find_video_sha_in_step_result(
                    step_id, step_res, step_def,
                    label=f"latest_completed/{src_shot_id}/{step_id}"
                )
                if sha is not None:
                    return {src_shot_id: sha}

            # No video-class artifact found; source not ready
            return {}

    def _build_ref_image_files(self, ctx: dict[str, Any]) -> "dict[str, Path] | None":
        """Collect all ref_image path entries keyed by their declared sha256.

        Covers both shot-level ``ctx["ref_images"]`` and asset-level
        ``ctx["asset_refs"][i]["ref_images"]``.  Entries without a ``path``
        field are omitted (they must be supplied externally or rejected at
        verification time).  Keying by sha256 supports multiple images that
        share the same role.
        """
        result: dict[str, Path] = {}

        def _collect(ri_list: list[dict]) -> None:
            for ri in ri_list:
                sha = ri.get("sha256", "")
                path_str = ri.get("path")
                if sha and path_str:
                    result[sha] = Path(path_str)

        _collect(ctx.get("ref_images", []))
        for asset_ref in ctx.get("asset_refs", []):
            _collect(asset_ref.get("ref_images", []))

        return result if result else None

    def _compute_step_digests(
        self,
        step: dict[str, Any],
        execution_context_digest: str | None = None,
    ) -> tuple[str, str, Any]:
        """Compute (inputs_digest, executor_config_digest, executor_instance) for a step.

        execution_context_digest: when provided (C1 resolved context), it is
        included in inputs_payload so that any change to effective_prompt,
        resolved_sha256, or ref image SHAs invalidates the resume binding.
        """
        kind = step.get("kind")
        inputs = step.get("inputs", {})

        if self._post_context is not None:
            return (canonical_contract_digest({"step": step, "bindings": self._post_context.bindings}),
                    canonical_contract_digest({"engine": "fsf_edit_post_v1", "tool": self._post_context.version}), None)

        if kind == "tool":
            tool = step.get("tool", "")
            if tool == "pdx.media.technical_conform":
                target_ref = inputs.get("target_artifact", "")
                target_path = self.resolve_artifact_path(target_ref)
                target_sha = ""
                if target_path and target_path.is_file():
                    target_sha = hashlib.sha256(target_path.read_bytes()).hexdigest()
                expected = inputs.get("expected_media_contract", {})
                expected_digest = canonical_contract_digest(expected)
                inputs_payload = {
                    "tool": tool,
                    "target_artifact": target_ref,
                    "target_sha256": target_sha,
                    "expected_media_contract_digest": expected_digest,
                }
                exec_payload = {
                    "executor_id": "pdx.media.technical_conform",
                    "execution_kind": "qc",
                }
                return canonical_contract_digest(inputs_payload), canonical_contract_digest(exec_payload), None
            elif self._is_handoff_tool_step(step):
                dep_shas: dict[str, str] = {}
                for dep_id in step.get("depends_on", []):
                    dep_step = self._steps_by_id.get(dep_id, {})
                    if dep_step.get("tool") == "pdx.media.technical_conform":
                        target_ref = dep_step.get("inputs", {}).get("target_artifact")
                        cand = self.resolve_artifact_path(target_ref)
                        if cand and cand.is_file():
                            dep_shas[target_ref] = hashlib.sha256(cand.read_bytes()).hexdigest()
                    for out_ref in dep_step.get("outputs", []):
                        out_path = self.resolve_artifact_path(out_ref)
                        if out_path and out_path.is_file():
                            dep_shas[out_ref] = hashlib.sha256(out_path.read_bytes()).hexdigest()

                inputs_payload = {
                    "tool": tool,
                    "inputs": copy.deepcopy(inputs),
                    "dependency_artifact_shas": dep_shas,
                }
                if execution_context_digest:
                    inputs_payload["execution_context_digest"] = execution_context_digest
                exec_payload = {
                    "executor_id": "fsf.handoff.agent",
                    "execution_kind": "handoff",
                }
                return canonical_contract_digest(inputs_payload), canonical_contract_digest(exec_payload), None
            else:
                executor = None
                if self.executor_override:
                    if isinstance(self.executor_override, dict):
                        cand_id = self.executor_override.get(tool)
                        if cand_id:
                            executor = self.registry.get(cand_id)
                    elif isinstance(self.executor_override, str):
                        cand = self.registry.get(self.executor_override)
                        if cand.tool_name == tool:
                            executor = cand
                if executor is None:
                    executor_id = inputs.get("executor_id")
                    if executor_id:
                        executor = self.registry.get(executor_id)
                    else:
                        matches = [
                            exc
                            for exc in reversed(list(self.registry._executors.values()))
                            if exc.tool_name == tool
                        ]
                        if matches:
                            executor = matches[0]
                        else:
                            executor = self.registry.get(tool)

                dep_shas: dict[str, str] = {}
                for dep_id in step.get("depends_on", []):
                    dep_step = self._steps_by_id.get(dep_id, {})
                    if dep_step.get("tool") == "pdx.media.technical_conform":
                        target_ref = dep_step.get("inputs", {}).get("target_artifact")
                        cand = self.resolve_artifact_path(target_ref)
                        if cand and cand.is_file():
                            dep_shas[target_ref] = hashlib.sha256(cand.read_bytes()).hexdigest()
                    for out_ref in dep_step.get("outputs", []):
                        out_path = self.resolve_artifact_path(out_ref)
                        if out_path and out_path.is_file():
                            dep_shas[out_ref] = hashlib.sha256(out_path.read_bytes()).hexdigest()

                inputs_payload = {
                    "tool": tool,
                    "inputs": copy.deepcopy(inputs),
                    "dependency_artifact_shas": dep_shas,
                }
                if execution_context_digest:
                    inputs_payload["execution_context_digest"] = execution_context_digest

                if hasattr(executor, "config_digest"):
                    exec_digest = executor.config_digest
                else:
                    exec_payload = {
                        "executor_id": executor.executor_id,
                        "tool_name": executor.tool_name,
                        "execution_kind": executor.execution_kind,
                    }
                    exec_digest = canonical_contract_digest(exec_payload)

                return canonical_contract_digest(inputs_payload), exec_digest, executor

        elif kind == "transform":
            artifact_inputs = inputs.get("artifact_inputs", [])
            in_shas: dict[str, str] = {}
            for art_in in artifact_inputs:
                ref = art_in.get("artifact", "")
                p = self.resolve_artifact_path(ref)
                if p and p.is_file():
                    in_shas[ref] = hashlib.sha256(p.read_bytes()).hexdigest()

                audio_ref = art_in.get("audio_artifact")
                if audio_ref:
                    ap = self.resolve_artifact_path(audio_ref)
                    if ap and ap.is_file():
                        in_shas[audio_ref] = hashlib.sha256(ap.read_bytes()).hexdigest()

            bgm_ref = inputs.get("bgm_artifact")
            bgm_sha = ""
            if bgm_ref:
                bgm_p = self.resolve_artifact_path(bgm_ref)
                if bgm_p and bgm_p.is_file():
                    bgm_sha = hashlib.sha256(bgm_p.read_bytes()).hexdigest()

            vo_ref = inputs.get("vo_artifact")
            vo_sha = ""
            if vo_ref:
                vo_p = self.resolve_artifact_path(vo_ref)
                if vo_p and vo_p.is_file():
                    vo_sha = hashlib.sha256(vo_p.read_bytes()).hexdigest()

            transform_inputs_payload = {
                "transform": step.get("transform"),
                "inputs": copy.deepcopy(inputs),
                "input_artifact_shas": in_shas,
                "bgm_sha256": bgm_sha,
                "vo_sha256": vo_sha,
            }
            exec_payload = {
                "transform": step.get("transform"),
                "engine": "fast_assembly",
            }
            return canonical_contract_digest(transform_inputs_payload), canonical_contract_digest(exec_payload), None

        raise ValueError(f"Unknown step kind '{kind}'")

    def execute_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute all steps in a validated pdx_execution_plan_v1 according to DAG order."""
        if self.recovery_policy is not None and self._recovery_controller is None:
            if validate_execution_plan(plan):
                raise ValueError("Invalid execution plan")
            if self.source_executions is not None or plan.get("producer", {}).get("name") == "fsf.runtime.post_plan":
                raise ValueError("Recovery policy is only supported for production plans")
            from fsf.runtime.recovery import execute_recoverable
            return execute_recoverable(self, plan)
        if self._post_context is None and (self.source_executions is not None or plan.get("producer", {}).get("name") == "fsf.runtime.post_plan"
                or any(s.get("transform", "").startswith("fsf.edit.") for s in plan.get("steps", []))):
            from fsf.runtime.post_runtime import execute_post
            return execute_post(self, plan)
        errors = validate_execution_plan(plan)
        if errors:
            raise ValueError(f"Invalid pdx_execution_plan_v1:\n" + "\n".join(errors))

        self.generation_dispatches = {"process": 0, "api": 0, "handoff": 0}

        plan_digest = canonical_contract_digest(plan)
        self.current_plan_digest = plan_digest

        source_sha = ""
        for s in plan.get("steps", []):
            if s.get("inputs", {}).get("source_sha256"):
                source_sha = str(s["inputs"]["source_sha256"])
                break

        steps: list[dict[str, Any]] = plan.get("steps", [])
        self._steps_by_id = {s["id"]: s for s in steps}

        # Build topological graph
        in_degree: dict[str, int] = {s["id"]: 0 for s in steps}
        dependents: dict[str, list[str]] = {s["id"]: [] for s in steps}

        for s in steps:
            for dep_id in s.get("depends_on", []):
                if dep_id not in self._steps_by_id:
                    raise ValueError(f"Step '{s['id']}' depends on unknown step '{dep_id}'")
                dependents[dep_id].append(s["id"])
                in_degree[s["id"]] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        topo_order: list[str] = []
        while queue:
            curr = queue.pop(0)
            topo_order.append(curr)
            for nxt in dependents[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        if len(topo_order) != len(steps):
            raise ValueError("Dependency cycle detected in pdx_execution_plan_v1")

        # Store for use by _resolve_continues_from_sha
        self._topo_order = topo_order

        # Load authoritative journal
        journal_path = self.output_dir / "fsf_execution_journal.json"
        prior_journal: dict[str, Any] | None = None
        if not self.no_resume and journal_path.is_file():
            try:
                loaded = json.loads(journal_path.read_text(encoding="utf-8"))
                if (
                    loaded.get("schema_version") == "fsf_plan_execution_journal_v1"
                    and loaded.get("plan_digest") == plan_digest
                    and (not source_sha or loaded.get("source_sha256") == source_sha)
                ):
                    prior_journal = loaded
            except Exception:
                prior_journal = None

        persisted_job = prior_journal.get("job_id") if prior_journal else None
        persisted_run = prior_journal.get("run_id") if prior_journal else None

        self.current_job_id = self.job_id or persisted_job or plan.get("request_id") or f"job_{plan_digest[:12]}"
        if self.run_id:
            self.current_run_id = self.run_id
        elif persisted_run and (not persisted_job or persisted_job == self.current_job_id):
            self.current_run_id = persisted_run
        else:
            self.current_run_id = f"run_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}"

        step_results: dict[str, dict[str, Any]] = {}

        for step_id in topo_order:
            step = self._steps_by_id[step_id]

            # 1. Dependency check
            blocked_by = None
            for dep_id in step.get("depends_on", []):
                dep_res = step_results.get(dep_id, {})
                dep_status = dep_res.get("status")
                if dep_status == "pending_handoff":
                    blocked_by = f"Dependency '{dep_id}' is waiting for handoff result"
                    break
                elif dep_status not in ("completed", "reused"):
                    blocked_by = f"Dependency '{dep_id}' status is '{dep_status}'"
                    break

            if blocked_by:
                step_results[step_id] = {
                    "id": step_id,
                    "kind": step.get("kind"),
                    "status": "blocked",
                    "error": blocked_by,
                }
                continue

            if self._post_context is not None:
                try:
                    self._post_context.check()
                except Exception:
                    step_results[step_id] = {"id": step_id, "kind": step.get("kind"), "status": "blocked", "error": "POST_LIVE_GATE"}
                    continue

            # ── C1: Resolve execution context BEFORE digest / resume ──────────
            # Resolution must precede _compute_step_digests() so that
            # execution_context_digest (which covers effective_prompt,
            # resolved_sha256, and ref-image SHAs) participates in
            # inputs_digest and therefore in the authoritative resume binding.
            resolved_execution_context: dict[str, Any] | None = None
            resolution_error: str | None = None
            raw_ctx = step.get("inputs", {}).get("consistency_context")
            if raw_ctx is not None and isinstance(raw_ctx, dict) and raw_ctx:
                shot_id_for_ctx = step.get("inputs", {}).get("shot_id", step_id)
                try:
                    # Resolve continues_from source using declared selection_rule
                    # (specific_artifact / latest_completed), scanning ALL steps for
                    # the source shot (not just direct depends_on) so QC/Dub terminal
                    # steps don't prevent finding the actual video artifact.
                    cf_decl = raw_ctx.get("continues_from")
                    resolved_shas: dict[str, str] = {}
                    if cf_decl is not None:
                        resolved_shas = self._resolve_continues_from_sha(cf_decl, step_results)

                    # Build ref_image_files keyed by sha256 (not role) covering
                    # both shot-level ref_images and asset_refs[i].ref_images.
                    ref_image_files = self._build_ref_image_files(raw_ctx)

                    resolved_execution_context = resolve_execution_context(
                        raw_ctx,
                        shot_id=shot_id_for_ctx,
                        resolved_artifact_shas=resolved_shas if cf_decl is not None else None,
                        ref_image_files=ref_image_files,
                    )
                except ConsistencyResolutionError as exc:
                    resolution_error = str(exc)

            if resolution_error:
                step_results[step_id] = {
                    "id": step_id,
                    "kind": step.get("kind"),
                    "status": "failed",
                    "error": f"Consistency context resolution failed: {resolution_error}",
                }
                continue

            ec_digest = (
                resolved_execution_context.get("execution_context_digest")
                if resolved_execution_context
                else None
            )

            # Compute current step digests for authoritative binding
            # (includes ec_digest so resume detects any context change)
            curr_inputs_digest, curr_exec_digest, resolved_executor = self._compute_step_digests(
                step, execution_context_digest=ec_digest
            )

            # 2. Authoritative Resume check
            # Invariant: If any dependency was re-executed (status != 'reused'), this step must re-run
            deps_all_reused = all(
                step_results.get(dep_id, {}).get("status") == "reused"
                for dep_id in step.get("depends_on", [])
            )
            # P1-2: For assembly (transform) steps, run the review gate BEFORE
            # allowing the step to be reused.  A prior assembly run that was
            # approved may have had its reviews withdrawn; reusing the assembled
            # output without re-checking would silently deliver an unapproved product.
            reused = False
            if prior_journal and deps_all_reused and step.get("kind") == "transform" and self._post_context is None:
                _art_inputs = step.get("inputs", {}).get("artifact_inputs", [])
                _resolved_resume, _res_errors = self._resolve_assembly_inputs(
                    _art_inputs, step_results
                )
                if _res_errors:
                    err_str = "; ".join(_res_errors)
                    step_results[step_id] = {
                        "id": step_id,
                        "kind": "transform",
                        "status": "blocked",
                        "error": (
                            f"Assembly gate blocked (resume path): cannot resolve all "
                            f"declared artifact inputs.  Details: {err_str}"
                        ),
                    }
                    continue
                _live_snap = self._build_gate_journal_snapshot(step_results)
                _revs = load_reviews(self.output_dir)
                _gate_ok, _gate_reasons = check_assembly_gate(
                    _live_snap, _revs, self.output_dir,
                    assembled_artifacts=_resolved_resume if _resolved_resume else None,
                )
                if not _gate_ok:
                    reasons_str = "; ".join(_gate_reasons)
                    step_results[step_id] = {
                        "id": step_id,
                        "kind": "transform",
                        "status": "blocked",
                        "error": (
                            f"Assembly gate blocked (resume path): consistency review "
                            f"incomplete for one or more shots.  "
                            f"Approve all shots via 'fsf review approve' before assembling.  "
                            f"Details: {reasons_str}"
                        ),
                    }
                    continue
            if prior_journal and deps_all_reused:
                prior_step = prior_journal.get("steps", {}).get(step_id)
                if prior_step and prior_step.get("status") == "completed" and step_id not in self._recovery_force:
                    # Check executor config & inputs digest match exactly
                    inputs_match = (
                        prior_step.get("inputs_digest") == curr_inputs_digest
                    )
                    exec_match = (
                        prior_step.get("executor_config_digest") == curr_exec_digest
                    )
                    prior_out_files = prior_step.get("output_files", [])

                    # Invariant: a completed step that produces outputs must have non-empty output_files
                    declared_outputs = step.get("outputs", [])
                    has_required_outputs = not (declared_outputs and not prior_out_files)

                    if inputs_match and exec_match and has_required_outputs:
                        outputs_valid = True
                        for out_info in prior_out_files:
                            f_path = self.output_dir / out_info["path"]
                            if not f_path.is_file():
                                outputs_valid = False
                                break
                            curr_sha = hashlib.sha256(f_path.read_bytes()).hexdigest()
                            if curr_sha.casefold() != str(out_info["sha256"]).casefold():
                                outputs_valid = False
                                break

                        # Extra check for QC steps
                        if outputs_valid and step.get("kind") == "tool" and step.get("tool") == "pdx.media.technical_conform":
                            rep_file = self.output_dir / declared_outputs[0].replace("report:", "")
                            if rep_file.is_file():
                                try:
                                    rep_data = json.loads(rep_file.read_text(encoding="utf-8"))
                                    if rep_data.get("evaluation_status") != "conforms":
                                        outputs_valid = False
                                except Exception:
                                    outputs_valid = False
                            else:
                                outputs_valid = False

                        if outputs_valid:
                            if hasattr(resolved_executor, "validate_resume") and not resolved_executor.validate_resume(prior_step):
                                outputs_valid = False
                        if outputs_valid:
                            reused = True
                            output_artifacts = [str(self.output_dir / f["path"]) for f in prior_out_files]
                            step_results[step_id] = {
                                "id": step_id,
                                "kind": step.get("kind"),
                                "status": "reused",
                                "executor_id": prior_step.get("executor_id"),
                                # Carry execution_kind forward so downstream
                                # continuity source resolution can determine
                                # whether .shape.bin/.mock.bin are valid.
                                "execution_kind": prior_step.get("execution_kind"),
                                "executor_config_digest": curr_exec_digest,
                                "inputs_digest": curr_inputs_digest,
                                "output_files": prior_out_files,
                                "output_artifacts": output_artifacts,
                            }
                            if prior_step.get("remote_provenance"):
                                step_results[step_id]["remote_provenance"] = copy.deepcopy(prior_step["remote_provenance"])
                            # Re-carry verified consistency acknowledgement from
                            # prior journal so review tooling can access it on resume.
                            if prior_step.get("consistency_acknowledgement"):
                                step_results[step_id]["consistency_acknowledgement"] = (
                                    prior_step["consistency_acknowledgement"]
                                )
                            if prior_step.get("requirements_digest"):
                                step_results[step_id]["requirements_digest"] = (
                                    prior_step["requirements_digest"]
                                )
                            if prior_step.get("review_required"):
                                step_results[step_id]["review_required"] = True
                            for out_ref in declared_outputs:
                                clean = out_ref.replace("artifact:", "").replace("report:", "")
                                self._artifact_registry[out_ref] = self.output_dir / clean

            if reused:
                continue

            # 3. Fresh execution
            try:
                res = self._execute_step(
                    step,
                    executor=resolved_executor,
                    inputs_digest=curr_inputs_digest,
                    prior_journal=prior_journal,
                    step_results=step_results,
                    resolved_execution_context=resolved_execution_context,
                )
                res["inputs_digest"] = curr_inputs_digest
                if self._post_context is not None and res.get("error"):
                    res["error"] = "POST_STEP_FAILED"
                res["executor_config_digest"] = curr_exec_digest
                if resolved_executor:
                    res["executor_id"] = resolved_executor.executor_id
                    res["execution_kind"] = getattr(resolved_executor, "execution_kind", None)
                # Carry review_required from the step's consistency_context into the
                # step result so _build_gate_journal_snapshot and journal build can
                # use it for review-gate scoping without re-reading the plan.
                if raw_ctx is not None:
                    res["review_required"] = bool(raw_ctx.get("review_required", False))
                # P1-F2: For every review_required=True step, compute requirements_digest
                # from the resolved execution context, regardless of whether the step is
                # a Handoff.  Without this, non-handoff steps have requirements_digest=None
                # and the review is only bound to the artifact SHA — changes in asset refs,
                # continuity source, or generation intent would not invalidate the review.
                if res.get("review_required") and resolved_execution_context:
                    try:
                        reqs = build_consistency_requirements(resolved_execution_context)
                        if reqs is not None:
                            computed_req_dig = reqs.get("requirements_digest")
                            if computed_req_dig and not res.get("requirements_digest"):
                                res["requirements_digest"] = computed_req_dig
                    except Exception:
                        pass  # non-fatal: handoff path may already have set it
                # Eagerly compute output_files from output_artifacts so that
                # downstream steps in the SAME run can read execution_kind and
                # verify SHA via _resolve_continues_from_sha without waiting
                # for the journal-build pass at the end of execute_plan().
                if "output_files" not in res or not res.get("output_files"):
                    eager_out_files: list[dict[str, Any]] = []
                    for out_path in res.get("output_artifacts", []):
                        p = Path(out_path)
                        if p.is_file():
                            sha = hashlib.sha256(p.read_bytes()).hexdigest()
                            try:
                                rel = p.relative_to(self.output_dir)
                                path_str = str(rel).replace("\\", "/")
                            except ValueError:
                                path_str = p.name
                            eager_out_files.append({"path": path_str, "sha256": sha})
                    if eager_out_files:
                        res["output_files"] = eager_out_files
                step_results[step_id] = res
            except Exception as exc:
                clean_err = (getattr(exc, "code", "POST_STEP_FAILED") if self._post_context is not None
                             else sanitize_process_text(str(exc)))
                failed_entry: dict[str, Any] = {
                    "id": step_id,
                    "kind": step.get("kind"),
                    "status": "failed",
                    "error": clean_err,
                    "inputs_digest": curr_inputs_digest,
                    "executor_config_digest": curr_exec_digest,
                }
                if getattr(exc, "code", None):
                    failed_entry["error_code"] = exc.code
                # Preserve attempt/workorder identity so rejected results can be re-dispatched.
                attempt_hint = getattr(exc, "attempt_id", None)
                workorder_hint = getattr(exc, "workorder_id", None)
                if attempt_hint is None and self.handoff:
                    import re

                    clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.current_job_id))
                    clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.current_run_id))
                    clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))
                    wo_prefix = f"wo_{clean_job}_{clean_run}_{clean_step}_att"
                    wo_dir = self.output_dir / "handoff" / "workorders"
                    attempts: list[int] = []
                    if wo_dir.is_dir():
                        for f in wo_dir.glob(f"{wo_prefix}*.workorder.json"):
                            m = re.search(r"_att(\d+)\.workorder\.json$", f.name)
                            if m:
                                attempts.append(int(m.group(1)))
                    if attempts:
                        attempt_hint = max(attempts)
                if attempt_hint is not None:
                    failed_entry["attempt_id"] = int(attempt_hint)
                if workorder_hint:
                    failed_entry["workorder_id"] = str(workorder_hint)
                step_results[step_id] = failed_entry

            if self._recovery_controller:
                self._recovery_controller.checkpoint(step_results)

        # 4. Compile overall result & save journal
        completed_count = sum(1 for s in step_results.values() if s.get("status") == "completed")
        reused_count = sum(1 for s in step_results.values() if s.get("status") == "reused")
        failed_count = sum(1 for s in step_results.values() if s.get("status") == "failed")
        blocked_count = sum(1 for s in step_results.values() if s.get("status") == "blocked")
        pending_handoff_count = sum(1 for s in step_results.values() if s.get("status") == "pending_handoff")

        pending_external_count = sum(1 for s in step_results.values() if s.get("status") == "pending_external")
        if failed_count == 0 and pending_external_count > 0:
            overall_status = "waiting_external"
            all_ok = False
        elif failed_count == 0 and pending_handoff_count > 0:
            overall_status = "pending_handoff"
            all_ok = False
        elif failed_count == 0 and blocked_count == 0:
            overall_status = "completed"
            all_ok = True
        else:
            overall_status = "failed"
            all_ok = False

        # Build journal entries preserving verified output_files
        journal_steps: dict[str, Any] = {}
        for sid, sdata in step_results.items():
            if sdata.get("status") in ("completed", "reused"):
                out_files = sdata.get("output_files")
                if not out_files:
                    out_files = []
                    for out_path in sdata.get("output_artifacts", []):
                        p = Path(out_path)
                        if p.is_file():
                            sha = hashlib.sha256(p.read_bytes()).hexdigest()
                            # Store relative path (from output_dir) so that
                            # artifacts in subdirectories can be unambiguously
                            # matched against artifact_ref declarations.
                            try:
                                rel = p.relative_to(self.output_dir)
                                path_str = str(rel).replace("\\", "/")
                            except ValueError:
                                path_str = p.name
                            out_files.append({"path": path_str, "sha256": sha})

                declared_outs = self._steps_by_id[sid].get("outputs", [])
                if declared_outs and not out_files:
                    # Invariant: step cannot be written as completed without its declared outputs!
                    continue

                journal_steps[sid] = {
                    "status": "completed",
                    "kind": sdata.get("kind"),
                    "executor_id": sdata.get("executor_id"),
                    "execution_kind": sdata.get("execution_kind"),
                    "executor_config_digest": sdata.get("executor_config_digest"),
                    "inputs_digest": sdata.get("inputs_digest"),
                    "output_files": out_files,
                }
                if sdata.get("attempt_id") is not None:
                    journal_steps[sid]["attempt_id"] = sdata["attempt_id"]
                if sdata.get("remote_provenance"):
                    journal_steps[sid]["remote_provenance"] = copy.deepcopy(sdata["remote_provenance"])
                if sdata.get("workorder_id"):
                    journal_steps[sid]["workorder_id"] = sdata["workorder_id"]
                # Persist verified consistency acknowledgement for downstream review.
                # Stored as-reported; visual compliance is NOT implied.
                if sdata.get("consistency_acknowledgement"):
                    journal_steps[sid]["consistency_acknowledgement"] = sdata["consistency_acknowledgement"]
                if sdata.get("requirements_digest"):
                    journal_steps[sid]["requirements_digest"] = sdata["requirements_digest"]
                # Record review_required so the gate can determine scope on resume
                # without re-reading the original plan.
                if sdata.get("review_required"):
                    journal_steps[sid]["review_required"] = True
            elif sdata.get("status") == "failed":
                journal_steps[sid] = {
                    "status": "failed",
                    "kind": sdata.get("kind"),
                    "error": sdata.get("error"),
                }
                if sdata.get("attempt_id") is not None:
                    journal_steps[sid]["attempt_id"] = sdata["attempt_id"]
                if sdata.get("remote_provenance"):
                    journal_steps[sid]["remote_provenance"] = copy.deepcopy(sdata["remote_provenance"])
                if sdata.get("workorder_id"):
                    journal_steps[sid]["workorder_id"] = sdata["workorder_id"]
            elif sdata.get("status") in {"pending_handoff", "pending_external"}:
                journal_steps[sid] = {
                    "status": sdata["status"],
                    "kind": sdata.get("kind"),
                }
                if sdata.get("resume_identity"):
                    journal_steps[sid]["resume_identity"] = copy.deepcopy(sdata["resume_identity"])
                if sdata.get("attempt_id") is not None:
                    journal_steps[sid]["attempt_id"] = sdata["attempt_id"]
                if sdata.get("workorder_id"):
                    journal_steps[sid]["workorder_id"] = sdata["workorder_id"]

        journal_payload = {
            "schema_version": "fsf_plan_execution_journal_v1",
            "job_id": self.current_job_id,
            "run_id": self.current_run_id,
            "plan_request_id": plan.get("request_id"),
            "plan_digest": plan_digest,
            "source_sha256": source_sha,
            "steps": journal_steps,
        }
        from fsf.remote.contracts import write_json
        write_json(journal_path,journal_payload)

        return {
            "schema_version": "fsf_plan_execution_result_v1",
            "status": overall_status,
            "success": all_ok,
            "plan_request_id": plan.get("request_id"),
            "plan_digest": plan_digest,
            "job_id": self.current_job_id,
            "run_id": self.current_run_id,
            "completed_count": completed_count,
            "reused_count": reused_count,
            "failed_count": failed_count,
            "blocked_count": blocked_count,
            "pending_handoff_count": pending_handoff_count,
            "steps": step_results,
        }

    def _execute_step(
        self,
        step: dict[str, Any],
        executor: Any = None,
        inputs_digest: str = "",
        prior_journal: dict[str, Any] | None = None,
        step_results: dict[str, dict[str, Any]] | None = None,
        resolved_execution_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a single step by kind."""
        kind = step.get("kind")
        if self._recovery_controller:
            self._recovery_controller.before(step)
        if kind == "tool":
            return self._execute_tool_step(
                step,
                executor=executor,
                inputs_digest=inputs_digest,
                prior_journal=prior_journal,
                step_results=step_results or {},
                resolved_execution_context=resolved_execution_context,
            )
        if kind == "transform":
            return self._execute_transform_step(step, step_results=step_results or {})
        raise ValueError(f"Unsupported step kind '{kind}' in step '{step.get('id')}'")

    def _execute_tool_step(
        self,
        step: dict[str, Any],
        executor: Any = None,
        inputs_digest: str = "",
        prior_journal: dict[str, Any] | None = None,
        step_results: dict[str, dict[str, Any]] | None = None,
        resolved_execution_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a tool step, Handoff generation step, or Media Conformance QC step.

        resolved_execution_context: pre-resolved context from execute_plan() main loop.
        It is already deep-copied, has effective_prompt assembled, resolved_sha256 filled,
        and carries execution_context_digest.  Must NOT be re-resolved here.
        """
        tool = step.get("tool", "")
        step_id = step["id"]
        inputs = step.get("inputs", {})

        # Use the pre-resolved execution context (resolved in execute_plan() BEFORE digest/resume)
        execution_context = resolved_execution_context

        # A. Media Technical Conformance QC step
        if tool == "pdx.media.technical_conform":
            target_ref = inputs.get("target_artifact", "")
            target_path = self.resolve_artifact_path(target_ref)
            if not target_path or not target_path.is_file():
                return {
                    "id": step_id,
                    "kind": "tool",
                    "tool": tool,
                    "status": "failed",
                    "error": f"Target media artifact not found: {target_path}",
                }

            expected = inputs.get("expected_media_contract")
            from fsf.conformance.media import evaluate_media_with_receipt
            report, _host_action, media_receipt = evaluate_media_with_receipt(target_path, expected)

            report_out = step.get("outputs", [f"report:{step_id}.json"])[0]
            clean_rep_name = report_out.replace("report:", "")
            rep_path = self.output_dir / clean_rep_name
            rep_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            self._artifact_registry[report_out] = rep_path
            evidence_outputs = [str(rep_path)]
            if media_receipt is not None:
                evidence_path = rep_path.with_name(rep_path.name + ".evidence.json")
                evidence_path.write_text(json.dumps(media_receipt, indent=2), encoding="utf-8")
                evidence_outputs.append(str(evidence_path))

            eval_status = report.get("evaluation_status")
            schema_ver = report.get("schema_version")

            if schema_ver != "pdx_media_conformance_result_v1":
                return {
                    "id": step_id,
                    "kind": "tool",
                    "tool": tool,
                    "status": "failed",
                    "error": "Host policy refusal: artifact did not produce a Media conformance report",
                    "report_path": str(rep_path),
                    "output_artifacts": [str(rep_path)],
                }

            if eval_status != "conforms":
                return {
                    "id": step_id,
                    "kind": "tool",
                    "tool": tool,
                    "status": "failed",
                    "error": f"Media technical conformance check failed with status: {eval_status}",
                    "evaluation_status": eval_status,
                    "report_path": str(rep_path),
                    "output_artifacts": [str(rep_path)],
                }

            return {
                "id": step_id,
                "kind": "tool",
                "tool": tool,
                "status": "completed",
                "report_path": str(rep_path),
                "output_artifacts": evidence_outputs,
            }

        # B. Asynchronous External Handoff Tool Step
        if self._is_handoff_tool_step(step):
            declared_outputs = step.get("outputs", [])
            artifact_filename = (
                declared_outputs[0].replace("artifact:", "").replace("report:", "")
                if declared_outputs
                else f"{step_id}.artifact"
            )

            # Route upstream input artifact if step has dependencies
            input_artifact_path: Path | None = None
            for dep_id in step.get("depends_on", []):
                dep_step = self._steps_by_id.get(dep_id, {})
                if dep_step.get("tool") == "pdx.media.technical_conform":
                    target_ref = dep_step.get("inputs", {}).get("target_artifact")
                    cand = self.resolve_artifact_path(target_ref)
                    if cand and cand.is_file() and not cand.name.endswith(".json"):
                        input_artifact_path = cand
                        break
                for out_ref in dep_step.get("outputs", []):
                    if out_ref.endswith(".json"):
                        continue
                    cand = self.resolve_artifact_path(out_ref)
                    if cand and cand.is_file():
                        input_artifact_path = cand
                        break
                if input_artifact_path:
                    break

            task = {
                "id": inputs.get("shot_id", step_id),
                # C1 (P4): use assembled effective_prompt as the authoritative generation
                # input when available; fall back to source_prompt for traceability.
                "prompt": (
                    (
                        execution_context.get("generation_intent", {}).get("effective_prompt")
                        if execution_context
                        else None
                    )
                    or inputs.get("shot_spec", {}).get("source_prompt")
                    or inputs.get("asset_spec", {}).get("source_prompt")
                    or ""
                ),
                "source_prompt": (
                    inputs.get("shot_spec", {}).get("source_prompt")
                    or inputs.get("asset_spec", {}).get("source_prompt")
                    or ""
                ),
                "duration_seconds": float(
                    inputs.get("shot_spec", {}).get("duration_seconds")
                    or inputs.get("duration_seconds")
                    or 5.0
                ),
                "shot_spec": inputs.get("shot_spec", {}),
                "asset_spec": inputs.get("asset_spec", {}),
                "target_artifact": artifact_filename,
                "input_artifact": str(input_artifact_path) if input_artifact_path else None,
                # C1: resolved execution context (effective_prompt assembled, SHA filled)
                "execution_context": execution_context,
            }

            expected_contract = inputs.get("expected_media_contract")
            expected_digest = inputs.get("expected_media_contract_digest") or (
                canonical_contract_digest(expected_contract) if expected_contract else None
            )

            # Determine attempt_id:
            # Check existing workorders on disk for this job/run/step
            import re
            clean_job = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.current_job_id))
            clean_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.current_run_id))
            clean_step = re.sub(r"[^A-Za-z0-9_-]", "_", str(step_id))
            wo_prefix = f"wo_{clean_job}_{clean_run}_{clean_step}_att"

            wo_dir = self.output_dir / "handoff" / "workorders"
            existing_attempts = []
            if wo_dir.is_dir():
                for f in wo_dir.glob(f"{wo_prefix}*.workorder.json"):
                    m = re.search(r"_att(\d+)\.workorder\.json$", f.name)
                    if m:
                        existing_attempts.append(int(m.group(1)))

            latest_existing = max(existing_attempts) if existing_attempts else 1

            # Check if this step already failed on latest_existing attempt:
            prior_step_info = prior_journal.get("steps", {}).get(step_id, {}) if prior_journal else {}
            prior_failed = (
                prior_step_info.get("status") == "failed"
                and int(prior_step_info.get("attempt_id", -1)) >= latest_existing
            )

            current_attempt = (latest_existing + 1) if prior_failed else latest_existing
            if self._recovery_controller:
                current_attempt = self._recovery_attempt

            wo = build_workorder(
                job_id=self.current_job_id,
                run_id=self.current_run_id,
                step_id=step_id,
                attempt_id=current_attempt,
                tool=tool,
                plan_digest=self.current_plan_digest,
                inputs_digest=inputs_digest,
                target_artifact=artifact_filename,
                task_payload=task,
                expected_media_contract=expected_contract,
                expected_contract_digest=expected_digest,
                consistency_requirements=build_consistency_requirements(execution_context)
                if execution_context
                else None,
            )

            res_file = find_result_file(
                self.output_dir,
                job_id=self.current_job_id,
                run_id=self.current_run_id,
                step_id=step_id,
                attempt_id=current_attempt,
            )

            if res_file:
                prior_sha = None
                if prior_journal:
                    accepted_prior = prior_journal.get("steps", {}).get(step_id, {})
                    p_info = accepted_prior.get("output_files", []) if accepted_prior.get("attempt_id") == current_attempt else []
                    if p_info:
                        prior_sha = p_info[0].get("sha256")

                try:
                    ingest_res = ingest_workorder_result(
                        res_file,
                        wo,
                        self.output_dir,
                        previous_accepted_sha=prior_sha,
                    )
                except (HandoffIntegrityError, HandoffSecurityError, FileNotFoundError, ValueError) as exc:
                    # Integrity/SHA/path rejection must still bind attempt_id so the next
                    # run can increment and re-dispatch instead of re-ingesting attN forever.
                    return {
                        "id": step_id,
                        "kind": "tool",
                        "tool": tool,
                        "status": "failed",
                        "attempt_id": current_attempt,
                        "workorder_id": wo["workorder_id"],
                        "error": sanitize_process_text(str(exc)),
                        "output_artifacts": [],
                    }

                if ingest_res.get("status") == "completed":
                    art_path = Path(ingest_res["artifact_path"])
                    for out_ref in declared_outputs:
                        self._artifact_registry[out_ref] = art_path
                    step_result: dict[str, Any] = {
                        "id": step_id,
                        "kind": "tool",
                        "tool": tool,
                        "status": "completed",
                        "attempt_id": current_attempt,
                        "workorder_id": wo["workorder_id"],
                        "handoff_result": ingest_res,
                        "output_artifacts": [str(art_path)],
                    }
                    # Preserve verified consistency acknowledgement for journal
                    # and downstream review.  Stored as-reported; visual compliance
                    # is NOT implied — this is the structural audit record only.
                    ack_record = ingest_res.get("consistency_acknowledgement")
                    if ack_record:
                        step_result["consistency_acknowledgement"] = ack_record
                        step_result["requirements_digest"] = wo.get("requirements_digest")
                    return step_result
                else:
                    return {
                        "id": step_id,
                        "kind": "tool",
                        "tool": tool,
                        "status": "failed",
                        "attempt_id": current_attempt,
                        "workorder_id": wo["workorder_id"],
                        "error": ingest_res.get("error", "Handoff reported failure"),
                        "output_artifacts": [],
                    }

            # Emit workorder and return pending_handoff
            existing_workorder = self.output_dir / "handoff" / "workorders" / f"{wo['workorder_id']}.workorder.json"
            if not existing_workorder.exists():
                self.generation_dispatches["handoff"] += 1
            wo_path = write_workorder(wo, self.output_dir)
            return {
                "id": step_id,
                "kind": "tool",
                "tool": tool,
                "status": "pending_handoff",
                "attempt_id": current_attempt,
                "workorder_id": wo["workorder_id"],
                "workorder_path": str(wo_path),
                "output_artifacts": [],
            }

        # C. General Tool Step
        if executor is None:
            if self.executor_override:
                if isinstance(self.executor_override, dict):
                    cand_id = self.executor_override.get(tool)
                    if cand_id:
                        executor = self.registry.get(cand_id)
                elif isinstance(self.executor_override, str):
                    cand = self.registry.get(self.executor_override)
                    if cand.tool_name == tool:
                        executor = cand
            if executor is None:
                executor_id = inputs.get("executor_id")
                if executor_id:
                    executor = self.registry.get(executor_id)
                else:
                    matches = [
                        exc
                        for exc in reversed(list(self.registry._executors.values()))
                        if exc.tool_name == tool
                    ]
                    if matches:
                        executor = matches[0]
                    else:
                        executor = self.registry.get(tool)

        # Route explicit upstream media artifact if step has dependencies
        input_artifact_path: Path | None = None
        for dep_id in step.get("depends_on", []):
            dep_step = self._steps_by_id.get(dep_id, {})
            # If dependency was a QC step, route its inspected media artifact
            if dep_step.get("tool") == "pdx.media.technical_conform":
                target_ref = dep_step.get("inputs", {}).get("target_artifact")
                cand = self.resolve_artifact_path(target_ref)
                if cand and cand.is_file() and not cand.name.endswith(".json"):
                    input_artifact_path = cand
                    break
            for out_ref in dep_step.get("outputs", []):
                if out_ref.endswith(".json"):
                    continue
                cand = self.resolve_artifact_path(out_ref)
                if cand and cand.is_file():
                    input_artifact_path = cand
                    break
            if input_artifact_path:
                break

        # Output artifact filename from declared step outputs
        declared_outputs = step.get("outputs", [])
        artifact_filename = None
        if declared_outputs:
            artifact_filename = declared_outputs[0].replace("artifact:", "").replace("report:", "")

        task = {
            "id": inputs.get("shot_id", step_id),
            # C1 (P4): use assembled effective_prompt as the authoritative generation
            # input when available; fall back to source_prompt for traceability.
            "prompt": (
                (
                    execution_context.get("generation_intent", {}).get("effective_prompt")
                    if execution_context
                    else None
                )
                or inputs.get("shot_spec", {}).get("source_prompt")
                or inputs.get("asset_spec", {}).get("source_prompt")
                or ""
            ),
            "source_prompt": (
                inputs.get("shot_spec", {}).get("source_prompt")
                or inputs.get("asset_spec", {}).get("source_prompt")
                or ""
            ),
            "duration_seconds": float(
                inputs.get("shot_spec", {}).get("duration_seconds")
                or inputs.get("duration_seconds")
                or 5.0
            ),
            "shot_spec": inputs.get("shot_spec", {}),
            "asset_spec": inputs.get("asset_spec", {}),
            "request_id": f"req:{step_id}",
            "artifact_filename": artifact_filename,
            "input_artifact": str(input_artifact_path) if input_artifact_path else None,
            "input_video": str(input_artifact_path) if input_artifact_path else None,
            # C1: resolved execution context (effective_prompt assembled, SHA filled)
            "execution_context": execution_context,
        }

        execution_kind = getattr(executor, "execution_kind", None)
        if self._recovery_controller:
            task["request_id"] += f":att{self._recovery_attempt}"
            task["generation_attempt"] = self._recovery_attempt
            task["resume_paused"] = self.resume_paused
            task["cooperative_wait"] = True
        if execution_kind == "remote" or getattr(executor,"requires_model_contract",False):
            expected = None
            for qc in self._steps_by_id.values():
                if qc.get("tool") == "pdx.media.technical_conform" and qc.get("inputs", {}).get("target_artifact") in declared_outputs:
                    expected = qc["inputs"].get("expected_media_contract")
                    break
            requirements = build_consistency_requirements(execution_context) if execution_context else None
            task["remote_parent"] = {"job_id": self.current_job_id, "run_id": self.current_run_id, "step_id": step_id,
                "attempt_id": self._recovery_attempt if self._recovery_controller else 1,
                "plan_digest": self.current_plan_digest, "inputs_digest": inputs_digest,
                "expected_contract_digest": canonical_contract_digest(expected) if expected is not None else None,
                "requirements_digest": requirements["requirements_digest"] if requirements else None}
            task["remote_expected_media_contract"] = expected
            task["remote_consistency_requirements"] = requirements
            task["video_condition"] = inputs.get("video_condition")
        if tool.startswith("studio.") and execution_kind in ("process", "api") and not getattr(executor,"requires_model_contract",False):
            self.generation_dispatches[execution_kind] += 1
        tool_result = executor.execute_shot(task, self.output_dir)
        if getattr(executor,"requires_model_contract",False):
            self.generation_dispatches["api"] += getattr(executor,"last_submitted_delta",0)
        remote_provenance = copy.deepcopy(getattr(executor, "last_provenance", None))
        if execution_kind == "remote" and remote_provenance:
            transport = remote_provenance["provider"]
            self.generation_dispatches[transport] = self.generation_dispatches.get(transport, 0) + executor.last_submitted_delta
        art_file = tool_result.get("outputs", {}).get("artifact_file")
        out_path = self.output_dir / art_file if art_file else None

        for out_ref in step.get("outputs", []):
            if out_path:
                self._artifact_registry[out_ref] = out_path

        return {
            "id": step_id,
            "kind": "tool",
            "tool": tool,
            "status": tool_result["status"] if tool_result.get("status") in {"completed","pending_external"} else "failed",
            **({"resume_identity":tool_result["resume_identity"]} if tool_result.get("resume_identity") else {}),
            "tool_result": tool_result,
            **({"error":tool_result.get("error","Executor did not complete")} if tool_result.get("status")!="completed" else {}),
            "output_artifacts": [str(out_path)] if out_path and out_path.is_file() else [],
            **({"remote_provenance": remote_provenance} if remote_provenance else {}),
            **({"consistency_acknowledgement": remote_provenance["consistency_acknowledgement"]}
               if remote_provenance and remote_provenance.get("consistency_acknowledgement") else {}),
        }

    def _resolve_assembly_inputs(
        self,
        artifact_inputs: list[dict[str, Any]],
        step_results: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve each declared assembly input to a per-artifact record.

        Returns ``(resolved, errors)``.

        ``resolved`` is a list of dicts with:
            step_id       – the step that produced this artifact
            artifact_ref  – the original artifact ref from the plan
            canonical_path – normalised relative path from output_dir
            sha256        – SHA-256 of the artifact being assembled

        ``errors`` is a list of human-readable blocking reasons.  When
        non-empty the caller MUST block assembly — a non-empty error list
        means at least one declared input could not be unambiguously resolved
        to a producer / path / SHA.  Silently dropping inputs would reduce the
        gate scope and allow unreviewed shots through.

        Invariants enforced:
        • Every declared input must have an artifact_ref.
        • Every artifact_ref must resolve to a known path.
        • Every path must be produced by exactly one step in step_results.
        • That step must have a non-empty SHA for this file.
        Missing or ambiguous inputs → blocking error, NOT a silent skip.
        """
        resolved: list[dict[str, Any]] = []
        errors: list[str] = []

        # Canonical shape/mock suffix alias table.
        # When the plan declares e.g. "S01.shape.bin" but the mock executor writes
        # "S01.mock.bin", both names identify the same logical artifact.
        # CRITICAL: these aliases are ONLY valid when the producer ran with
        # execution_kind == "shape".  In real-media mode a .mock.bin output is
        # a test stub, not the actual media — accepting it would mean the gate
        # verified a stub while assembly received the wrong (or nonexistent) file.
        _SHAPE_ALIAS_PAIRS: tuple[tuple[str, str], ...] = (
            (".shape.bin",             ".mock.bin"),
            (".remaster.shape.bin",    ".remaster.mock.bin"),
            (".first_frame.shape.bin", ".first_frame.mock.bin"),
            (".design.shape.bin",      ".design.mock.bin"),
        )

        def _alias_path(declared_rel: str) -> str | None:
            """Return the shape/mock alias for *declared_rel*, or None."""
            declared_base = Path(declared_rel).name
            for suf_a, suf_b in _SHAPE_ALIAS_PAIRS:
                if declared_base.endswith(suf_a):
                    alias = declared_base[: -len(suf_a)] + suf_b
                    return str(Path(declared_rel).parent / alias).replace("\\", "/")
                if declared_base.endswith(suf_b):
                    alias = declared_base[: -len(suf_b)] + suf_a
                    return str(Path(declared_rel).parent / alias).replace("\\", "/")
            return None

        for idx, art_in in enumerate(artifact_inputs):
            art_ref = art_in.get("artifact", "")
            shot_id = art_in.get("shot_id", f"input[{idx}]")

            if not art_ref:
                errors.append(
                    f"artifact_inputs[{idx}] (shot_id={shot_id!r}) has no artifact ref.  "
                    f"Cannot verify review for unnamed input."
                )
                continue

            art_path = self.resolve_artifact_path(art_ref)
            if art_path is None:
                errors.append(
                    f"Cannot resolve artifact ref '{art_ref}' for shot '{shot_id}'.  "
                    f"Artifact is not registered and path cannot be constructed."
                )
                continue

            try:
                declared_rel = str(art_path.relative_to(self.output_dir)).replace("\\", "/")
            except ValueError:
                declared_rel = art_path.name

            # ── Producer step resolution ────────────────────────────────────────
            # Strategy A (preferred): use the compile-time ``producer_step_id``
            #   written by build_execution_plan().  Exact lookup — no guessing.
            #   Alias paths (.shape.bin ↔ .mock.bin) are added ONLY when the
            #   producer's execution_kind is "shape"; real-media producers must
            #   match the declared path exactly.
            # Strategy B (fallback, legacy plans): scan all step_results.  Per-step
            #   execution_kind gate: alias matching is restricted to steps whose
            #   execution_kind == "shape".
            # ────────────────────────────────────────────────────────────────────
            producer_step_id = art_in.get("producer_step_id", "").strip()

            matches: list[tuple[str, str, str]] = []  # (step_id, sha256, actual_path)

            if producer_step_id:
                # Strategy A: direct lookup by producer_step_id
                sdata = step_results.get(producer_step_id)
                if sdata is None:
                    errors.append(
                        f"Declared producer step '{producer_step_id}' for artifact "
                        f"'{art_ref}' (shot '{shot_id}') not found in step_results.  "
                        f"The producer step must complete before assembly can proceed."
                    )
                    continue

                # Gate: determine accepted paths based on execution_kind
                producer_exec_kind = str(sdata.get("execution_kind") or "").strip()
                is_shape = producer_exec_kind in SHAPE_EXECUTION_KINDS
                accepted_paths: set[str] = {declared_rel}
                if is_shape:
                    alias = _alias_path(declared_rel)
                    if alias:
                        accepted_paths.add(alias)
                # In real-media mode accepted_paths == {declared_rel} only.

                # Find the matching output_file within the producer step
                for f in sdata.get("output_files") or []:
                    f_path = f.get("path", "").replace("\\", "/")
                    sha = str(f.get("sha256", "")).strip()
                    if f_path in accepted_paths and sha:
                        matches.append((producer_step_id, sha, f_path))

                if not matches:
                    alias = _alias_path(declared_rel)
                    if not is_shape and alias:
                        recorded = [f.get("path") for f in sdata.get("output_files") or []]
                        if alias in recorded or any(
                            (f.get("path", "")).replace("\\", "/") == alias
                            for f in sdata.get("output_files") or []
                        ):
                            # The producer recorded the alias path but not in shape/mock mode
                            errors.append(
                                f"Producer step '{producer_step_id}' for artifact '{art_ref}' "
                                f"(shot '{shot_id}') recorded alias path '{alias}' but "
                                f"execution_kind={producer_exec_kind!r} is not a shape "
                                f"execution kind {sorted(SHAPE_EXECUTION_KINDS)!r}.  "
                                f"Shape/mock aliases are only valid in shape test mode.  "
                                f"A real-media producer must write the declared path "
                                f"'{declared_rel}' exactly."
                            )
                            continue
                    errors.append(
                        f"Producer step '{producer_step_id}' for artifact '{art_ref}' "
                        f"(shot '{shot_id}') completed but recorded no output_file "
                        f"matching accepted paths {sorted(accepted_paths)!r}.  "
                        f"Recorded output_files: "
                        f"{[f.get('path') for f in sdata.get('output_files') or []]!r}."
                    )
                    continue
            else:
                # Strategy B: scan all steps (legacy plans without producer_step_id)
                for sid, sdata in step_results.items():
                    exec_kind = str(sdata.get("execution_kind") or "").strip()
                    is_shape = exec_kind in SHAPE_EXECUTION_KINDS
                    # Build per-step accepted_paths gated by execution_kind
                    step_accepted: set[str] = {declared_rel}
                    if is_shape:
                        alias = _alias_path(declared_rel)
                        if alias:
                            step_accepted.add(alias)
                    for f in sdata.get("output_files") or []:
                        f_path = f.get("path", "").replace("\\", "/")
                        sha = str(f.get("sha256", "")).strip()
                        if f_path in step_accepted and sha:
                            matches.append((sid, sha, f_path))

            if len(matches) == 0:
                errors.append(
                    f"No producer found in step_results for artifact '{art_ref}' "
                    f"(declared='{declared_rel}') from shot '{shot_id}'.  "
                    f"Cannot verify review for unresolvable artifact."
                )
                continue

            if len(matches) > 1:
                producers = sorted({m[0] for m in matches})
                errors.append(
                    f"Ambiguous producer for artifact '{art_ref}' "
                    f"(declared='{declared_rel}') "
                    f"from shot '{shot_id}': matched {producers!r}.  "
                    f"Assembly input must be produced by exactly one step."
                )
                continue

            matched_step_id, sha, actual_path = matches[0]
            if not sha:
                errors.append(
                    f"Artifact '{art_ref}' from shot '{shot_id}' produced by "
                    f"'{matched_step_id}' has no SHA256 recorded.  "
                    f"Cannot verify review gate without a SHA."
                )
                continue

            resolved.append({
                "step_id": matched_step_id,
                "artifact_ref": art_ref,
                # canonical_path is the actual path from output_files — the ONLY
                # path used by (a) gate disk-verification and (b) timeline assembly.
                # It is never independently re-resolved from art_ref.
                "canonical_path": actual_path,
                "sha256": sha,
            })

        return resolved, errors

    def _build_gate_journal_snapshot(
        self,
        step_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Project in-memory step_results into the journal-dict shape expected by
        check_assembly_gate().

        This is used instead of reading the on-disk journal so that the first run
        (whose journal hasn't been written yet) and the resume path (where the on-disk
        journal may be stale) both get an accurate, up-to-date gate check.

        Only completed/reused steps with output_files are included; failed/blocked/
        pending_handoff steps are omitted (the gate ignores them anyway).
        """
        steps_snapshot: dict[str, Any] = {}
        for sid, sdata in step_results.items():
            if sdata.get("status") not in ("completed", "reused"):
                continue
            # Resolve output_files: prefer explicit list, fall back to output_artifacts
            out_files: list[dict[str, Any]] = list(sdata.get("output_files") or [])
            if not out_files:
                for art_path_str in sdata.get("output_artifacts", []):
                    p = Path(art_path_str)
                    if p.is_file():
                        sha = hashlib.sha256(p.read_bytes()).hexdigest()
                        try:
                            rel = p.relative_to(self.output_dir)
                            path_str = str(rel).replace("\\", "/")
                        except ValueError:
                            path_str = p.name
                        out_files.append({"path": path_str, "sha256": sha})
            steps_snapshot[sid] = {
                "status": sdata["status"],
                "output_files": out_files,
                # Carry review-gate fields from the live step result
                "review_required": sdata.get("review_required", False),
                "requirements_digest": sdata.get("requirements_digest"),
                "workorder_id": sdata.get("workorder_id"),
                "consistency_acknowledgement": sdata.get("consistency_acknowledgement"),
            }
        return {
            "schema_version": "fsf_plan_execution_journal_v1",
            "job_id": self.current_job_id,
            "run_id": self.current_run_id,
            "steps": steps_snapshot,
        }

    def _execute_transform_step(
        self,
        step: dict[str, Any],
        step_results: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute a transform step (e.g. fsf.assembly.fast_concat)."""
        transform = step.get("transform", "")
        step_id = step["id"]
        inputs = step.get("inputs", {})

        if self._post_context is not None:
            from fsf.runtime.post_sources import PostError
            try:
                return self._post_context.execute(step, self.output_dir)
            except (PostError, __import__("fsf.core.audio_edit", fromlist=["AudioError"]).AudioError):
                raise
            except Exception:
                raise PostError("POST_OPERATION_FAILED") from None

        if transform != "fsf.assembly.fast_concat":
            raise ValueError(f"Unsupported transform '{transform}' in step '{step_id}'")

        # ── Assembly review gate ────────────────────────────────────────── #
        # Evaluate the gate using the in-memory step_results from the        #
        # current run — NOT the on-disk journal.  The journal is written     #
        # AFTER all steps complete, so a first-run check against the journal  #
        # would see an empty or stale file and would incorrectly pass.       #
        #                                                                    #
        # _resolve_assembly_inputs() maps each declared input to a           #
        # (step_id, artifact_ref, canonical_path, sha256) record and is      #
        # fail-closed: any unresolvable / ambiguous input is an error and    #
        # blocks assembly immediately.  Silently dropping inputs would       #
        # shrink the gate scope and allow unreviewed shots through.          #
        # ─────────────────────────────────────────────────────────────────  #
        artifact_inputs = inputs.get("artifact_inputs", [])
        _resolved, _resolve_errors = self._resolve_assembly_inputs(
            artifact_inputs, step_results or {}
        )
        if _resolve_errors:
            err_str = "; ".join(_resolve_errors)
            return {
                "id": step_id,
                "kind": "transform",
                "status": "blocked",
                "error": (
                    f"Assembly gate blocked: cannot resolve all declared artifact inputs.  "
                    f"Details: {err_str}"
                ),
            }

        _live_snapshot = self._build_gate_journal_snapshot(step_results or {})
        _reviews = load_reviews(self.output_dir)
        _gate_ok, _gate_reasons = check_assembly_gate(
            _live_snapshot, _reviews, self.output_dir,
            assembled_artifacts=_resolved if _resolved else None,
        )
        if not _gate_ok:
            reasons_str = "; ".join(_gate_reasons)
            return {
                "id": step_id,
                "kind": "transform",
                "status": "blocked",
                "error": (
                    f"Assembly gate blocked: consistency review incomplete for one or more shots.  "
                    f"Approve all shots via 'fsf review approve' before assembling.  "
                    f"Details: {reasons_str}"
                ),
            }

        shot_ids = inputs.get("shot_ids", [])
        total_dur = float(inputs.get("target_duration_seconds", 0.0))

        # Build a lookup from artifact_ref → resolved item so the timeline loop
        # uses the gate-verified canonical_path (never re-resolves art_ref independently).
        _resolved_by_ref: dict[str, dict[str, Any]] = {
            item["artifact_ref"]: item for item in _resolved
        }

        timeline: list[dict[str, Any]] = []

        if artifact_inputs:
            for art_in in artifact_inputs:
                sid = art_in["shot_id"]
                role = art_in.get("role", "video")
                art_ref = art_in["artifact"]

                # Use the gate-verified canonical_path from _resolved — NEVER re-resolve
                # art_ref with resolve_artifact_path().  The gate verified this exact file;
                # assembling a different path would break the verified → assembled chain.
                resolved_item = _resolved_by_ref.get(art_ref)
                if resolved_item is None:
                    # This should only happen if the gate was bypassed — fail hard.
                    raise RuntimeError(
                        f"Transform step '{step_id}': artifact '{art_ref}' for shot "
                        f"'{sid}' passed the gate but has no resolved canonical_path.  "
                        f"This indicates an internal state inconsistency."
                    )
                art_path = self.output_dir / resolved_item["canonical_path"]
                if not art_path.is_file():
                    raise FileNotFoundError(
                        f"Transform step '{step_id}' requires artifact '{art_ref}' "
                        f"for shot '{sid}' ({role}), but the gate-verified file is "
                        f"not found: {art_path}"
                    )
                if art_path.stat().st_size == 0:
                    raise ValueError(
                        f"Transform step '{step_id}' required artifact '{art_ref}' "
                        f"for shot '{sid}' is 0 bytes: {art_path}"
                    )
                shot_dur = float(
                    art_in.get("duration_seconds")
                    or inputs.get("shot_durations", {}).get(sid)
                    or (total_dur / max(1, len(artifact_inputs)))
                )

                # Explicit per-shot audio artifact from plan routing (fail-closed, no guessing)
                per_shot_audio: Path | None = None
                audio_ref = art_in.get("audio_artifact")
                if audio_ref:
                    audio_cand = self.resolve_artifact_path(audio_ref)
                    if not audio_cand or not audio_cand.is_file():
                        raise FileNotFoundError(
                            f"Transform step '{step_id}' requires audio artifact '{audio_ref}' for shot '{sid}', but file not found: {audio_cand}"
                        )
                    if audio_cand.stat().st_size == 0:
                        raise ValueError(
                            f"Transform step '{step_id}' required audio artifact '{audio_ref}' for shot '{sid}' is 0 bytes"
                        )
                    per_shot_audio = audio_cand

                timeline.append({
                    "shot_id": sid,
                    "video_path": art_path,
                    "duration_seconds": shot_dur,
                    "audio_path": per_shot_audio,
                })
        else:
            shot_durations_map = inputs.get("shot_durations", {})
            for sid in shot_ids:
                cand = self.resolve_artifact_path(f"artifact:{sid}.mp4")
                if not cand or not cand.is_file():
                    cand = self.resolve_artifact_path(f"artifact:{sid}.shape.bin")
                if not cand or not cand.is_file():
                    raise FileNotFoundError(f"Segment video for shot '{sid}' not found: {cand}")
                shot_dur = float(shot_durations_map.get(sid) or (total_dur / max(1, len(shot_ids))))
                timeline.append({
                    "shot_id": sid,
                    "video_path": cand,
                    "duration_seconds": shot_dur,
                })

        out_ref = step.get("outputs", ["artifact:assembled_timeline.mp4"])[0]
        clean_out_name = out_ref.replace("artifact:", "")
        out_path = self.output_dir / clean_out_name

        subtitles = inputs.get("subtitles")
        subtitle_policy = inputs.get("subtitle_policy", "none")
        bgm_ref = inputs.get("bgm_artifact")
        bgm_path = self.resolve_artifact_path(bgm_ref) if bgm_ref else None
        vo_ref = inputs.get("vo_artifact")
        vo_path = self.resolve_artifact_path(vo_ref) if vo_ref else None

        expected_contract = inputs.get("expected_media_contract")
        if expected_contract:
            computed_dig = canonical_contract_digest(expected_contract)
            expected_dig = inputs.get("expected_media_contract_digest")
            if expected_dig and expected_dig != computed_dig:
                raise ValueError(
                    f"Transform step expected_media_contract_digest mismatch: {expected_dig} != {computed_dig}"
                )

        width = int(inputs.get("width") or (expected_contract.get("width") if expected_contract else 1024))
        height = int(inputs.get("height") or (expected_contract.get("height") if expected_contract else 576))
        fps = int(inputs.get("fps") or (expected_contract.get("fps") if expected_contract else 24))
        audio_policy = inputs.get("audio_policy", "keep")

        try:
            receipt: FastAssemblyReceipt = fast_assemble_timeline(
                timeline=timeline,
                output_path=out_path,
                width=width,
                height=height,
                fps=fps,
                audio_policy=audio_policy,
                expected_contract=expected_contract,
                subtitles=subtitles,
                subtitle_policy=subtitle_policy,
                bgm_path=bgm_path,
                vo_path=vo_path,
                bgm_volume=float(inputs.get("bgm_volume", 0.3)),
                vo_volume=float(inputs.get("vo_volume", 1.0)),
            )
        except Exception as exc:
            return {
                "id": step_id,
                "kind": "transform",
                "transform": transform,
                "status": "failed",
                "error": f"Assembly timeline failed with error: {exc}",
                "output_artifacts": [],
            }

        receipt_path = self.output_dir / "assembly_receipt.json"
        receipt_path.write_text(json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        self._artifact_registry[out_ref] = out_path

        out_artifacts = [str(out_path), str(receipt_path)]
        if receipt.subtitle_path and Path(receipt.subtitle_path).is_file():
            out_artifacts.append(receipt.subtitle_path)

        eval_status = receipt.conformance_report.get("evaluation_status")
        if eval_status != "conforms":
            return {
                "id": step_id,
                "kind": "transform",
                "transform": transform,
                "status": "failed",
                "error": (
                    f"Assembled timeline failed media technical conformance with status '{eval_status}': "
                    f"{receipt.conformance_report.get('issues', [])}"
                ),
                "receipt": receipt.to_dict(),
                "output_artifacts": out_artifacts,
            }

        return {
            "id": step_id,
            "kind": "transform",
            "transform": transform,
            "status": "completed",
            "receipt": receipt.to_dict(),
            "output_artifacts": out_artifacts,
        }
