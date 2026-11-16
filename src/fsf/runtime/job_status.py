"""Read the last recorded local execution state without resuming it."""
from pathlib import Path

from fsf.conformance.digest import canonical_contract_digest
from fsf.core.asset_library import _no_links, _path_parts
from fsf.core.edit_store import read_json
from fsf.runtime.blender import sha
from fsf.runtime.consistency_review import REVIEW_STORE_FILENAME, is_review_valid
from fsf.runtime.handoff import load_active_workorder


def _file(root, relative):
    path = root.joinpath(*_path_parts(relative))
    _no_links(path)
    if not path.is_file():
        raise ValueError("Expected a regular file")
    return path


def _record(root, relative):
    try:
        return read_json(_file(root, relative))
    except FileNotFoundError:
        return None


def _action(kind, instruction, *argv):
    return {"kind": kind, "argv": list(argv) or None, "instruction": instruction}


def _identity(step_id, step):
    return {"step_id": step_id, "workorder_id": step.get("workorder_id"),
            "attempt_id": step.get("attempt_id")}


def _active_order(root, journal, step_id, step):
    identity = _identity(step_id, step)
    identifier = identity["workorder_id"]
    order = load_active_workorder(root, journal, step_id)
    path = f"handoff/workorders/{identifier}.workorder.json"
    scope = {key: journal[key] for key in ("job_id", "run_id", "plan_digest")}
    binding = {**scope, **identity}
    result_path = f"handoff/results/{identifier}.result.json"
    try:
        result = _record(root, result_path)
        if result is None:
            alternate = f"handoff/results/{identifier}.json"
            result = _record(root, alternate)
            if result is not None:
                result_path = alternate
        if result is not None and (
            result.get("schema_version") != "fsf_workorder_result_v1"
            or any(result.get(key) != value for key, value in binding.items())
            or result.get("inputs_digest") != order.get("inputs_digest")
            or result.get("expected_contract_digest") != order.get("expected_contract_digest")
        ):
            raise ValueError("Result identity differs from the journal")
    except (ValueError, OSError, AttributeError):
        result_state = "unverified"
    else:
        result_state = "ready_for_ingest" if result is not None else "absent"
    return {**identity, "path": path, "target_artifact": order["target_artifact"],
            "result_path": result_path, "result_state": result_state}


def _review_blocker(root, journal, step_id, step, review):
    if step["status"] != "completed" or not step.get("review_required"):
        return None
    identity = _identity(step_id, step)
    try:
        artifact = step["output_files"][0]
        disk_sha = sha(_file(root, artifact["path"]))
    except (IndexError, KeyError, OSError, ValueError, TypeError):
        return {**identity, "code": "REVIEW_ARTIFACT_UNAVAILABLE"}
    if disk_sha != artifact["sha256"]:
        return {**identity, "code": "REVIEW_ARTIFACT_CHANGED"}
    if review is None:
        return {**identity, "code": "REVIEW_REQUIRED"}
    valid, _ = is_review_valid(
        review, current_artifact_sha=disk_sha,
        current_requirements_digest=step.get("requirements_digest"),
        current_workorder_id=step.get("workorder_id") or "",
        expected_step_id=step_id, expected_job_id=journal["job_id"],
        expected_run_id=journal["run_id"], current_step=step,
    )
    return None if valid else {**identity, "code": "REVIEW_NOT_APPROVED", "verdict": str(review.get("verdict") or "").lower()}


def inspect_status(out_dir):
    directory = str(out_dir)
    root = Path(out_dir).resolve()
    report = {"schema_version": "fsf_job_status_v1", "out_dir": directory,
              "job_id": None, "run_id": None, "workorders": [], "blockers": []}

    def reply(state, action):
        return {**report, "state": state, "next_action": action}

    journal = _record(root, "fsf_execution_journal.json")
    if journal is None:
        return reply("not_started", _action("start", "Create a plan and execute it in this output directory."))
    if (journal.get("schema_version") != "fsf_plan_execution_journal_v1"
            or not all(isinstance(journal.get(key), str) and journal[key]
                       for key in ("job_id", "run_id", "plan_digest"))
            or not isinstance(journal.get("steps"), dict)):
        raise ValueError("Invalid execution journal")
    report.update(job_id=journal["job_id"], run_id=journal["run_id"])
    steps = journal["steps"]
    recorded = journal.get("execution_status")
    if recorded not in {None, "completed", "failed", "blocked", "waiting_external", "pending_handoff"}:
        raise ValueError("Invalid recorded state")
    if any(step.get("status") not in {"completed", "failed", "blocked", "pending_external", "pending_handoff"}
           for step in steps.values()):
        raise ValueError("Invalid journal step status")
    reviews = _record(root, REVIEW_STORE_FILENAME)
    reviews = {} if reviews is None else reviews["reviews"]
    for step_id, step in steps.items():
        if step["status"] == "pending_handoff":
            try:
                order = _active_order(root, journal, step_id, step)
            except (KeyError, ValueError, OSError, TypeError, AttributeError):
                report["blockers"].append({"step_id": step_id, "code": "WORKORDER_UNVERIFIED"})
            else:
                report["workorders"].append(order)
                code = {"absent": "HANDOFF_RESULT_PENDING", "ready_for_ingest": "HANDOFF_RESULT_NOT_INGESTED",
                        "unverified": "HANDOFF_RESULT_UNVERIFIED"}[order["result_state"]]
                report["blockers"].append({**_identity(step_id, step), "code": code})
        review = _review_blocker(root, journal, step_id, step, reviews.get(step_id))
        if review:
            report["blockers"].append(review)
        if step["status"] == "failed":
            report["blockers"].append({**_identity(step_id, step), "code": "STEP_FAILED"})
        if step["status"] == "pending_external":
            report["blockers"].append({"step_id": step_id, "code": "EXTERNAL_RESULT_PENDING"})
        if step["status"] == "blocked" and step.get("blocker") not in {"DEPENDENCY_PENDING", "REVIEW_REQUIRED"}:
            report["blockers"].append({"step_id": step_id, "code": "STEP_BLOCKED"})

    codes = {item["code"] for item in report["blockers"]}
    if "STEP_FAILED" in codes or recorded == "failed":
        return reply("failed", _action("inspect_failure", "Inspect the failed step and original execute output before retrying."))
    if codes & {"STEP_BLOCKED", "WORKORDER_UNVERIFIED", "REVIEW_ARTIFACT_UNAVAILABLE", "REVIEW_ARTIFACT_CHANGED"}:
        return reply("blocked", _action("inspect_blocker", "Resolve the recorded blockers before resuming."))
    if "REVIEW_NOT_APPROVED" in codes:
        return reply("waiting_review", _action(
            "inspect_rejected_review",
            "Review was rejected or invalidated; inspect the review details before regenerating (--no-resume) or re-approving.",
            "fsf", "review", "list", directory, "--json"))
    if "REVIEW_REQUIRED" in codes:
        return reply("waiting_review", _action(
            "inspect_review", "Review the listed artifacts and record the operator's decision.",
            "fsf", "review", "list", directory, "--json"))
    if report["workorders"]:
        if any(row["result_state"] == "unverified" for row in report["workorders"]):
            return reply("blocked", _action("inspect_result", "Check the submitted result against its listed workorder."))
        if any(row["result_state"] == "ready_for_ingest" for row in report["workorders"]):
            return reply("waiting_handoff", _action(
                "resume", "Re-run the original execute command to validate and ingest the submitted result."))
        return reply("waiting_handoff", _action(
            "fulfill_workorder", "Open a workorder listed in this report. Submit its result, then re-run the original execute command."))
    if "EXTERNAL_RESULT_PENDING" in codes or recorded == "waiting_external":
        return reply("waiting_external", _action("reconcile", "Check the existing external request before resuming."))
    if recorded == "blocked" and any(step.get("blocker") == "REVIEW_REQUIRED" for step in steps.values()):
        return reply("ready_to_resume", _action("resume", "Re-run the original execute command with its original options."))
    if recorded == "blocked" or any(step["status"] == "blocked" for step in steps.values()):
        return reply("blocked", _action("inspect_blocker", "Check the original execute output before resuming."))
    plan = _record(root, "plan.json")
    bound_plan = plan is not None and canonical_contract_digest(plan) == journal["plan_digest"]
    legacy_complete = bound_plan and {step["id"] for step in plan["steps"]} == set(steps)
    if (recorded == "completed" or (recorded is None and legacy_complete)) and steps and all(
            step["status"] == "completed" for step in steps.values()):
        if not any(row["path"].lower().endswith(".mp4")
                   for step in steps.values() for row in step.get("output_files", [])):
            return reply("completed", _action("inspect_outputs", "Open the outputs recorded for the completed job."))
        if bound_plan:
            return reply("completed", _action("verify", "Verify the delivered video against its job contract.",
                                              "fsf", "verify", "--job", directory))
        return reply("completed", _action("verify_with_plan",
                                          f"For a video job, run 'fsf verify --job {directory} --plan <original-plan.json>'."))
    return reply("unknown", _action("inspect_execution", "Obtain the original plan and execute output to establish the state."))
