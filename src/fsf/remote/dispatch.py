"""Parent-side durable intent, reconciliation and verified atomic collection."""
import copy
import os
from pathlib import Path
import shutil
import tempfile
import time

from fsf.core.asset_library import _root
from fsf.core.edit_store import edit_lock, read_json
from fsf.remote.contracts import (RemoteError, digest, sha, seal, shape, check_digest, write_json,
    safe_path, file_item, validate_profile, check_authorization)
from fsf.remote.bundle import unpack, read_bundle, bundle_identity, copy_bounded
from fsf.remote.worker import child_identity


def _ledger(path):
    value = read_json(path)
    shape(value, "ledger")
    check_digest(value, "ledger_digest")
    return value


def _save(path, value):
    value = seal(value, "ledger_digest")
    shape(value, "ledger")
    write_json(path, value)
    return value


def summary(ledger):
    """No provider account, host, URL, credentials or raw logs in diagnostics."""
    return {k: ledger[k] for k in ("dispatch_id", "request_digest", "status", "submitted_count", "accepted_result_digest")}


def submit_bundle(bundle, *, profile, state_root, adapter=None):
    if os.environ.get("FSF_REMOTE_DEPTH"):
        raise RemoteError("REMOTE_NESTING_FORBIDDEN")
    profile = validate_profile(profile)
    root = _root(state_root)
    scratch = Path(tempfile.mkdtemp(prefix=".dispatch-open-", dir=root))
    try:
        names = unpack(bundle, scratch)
        request = read_bundle(scratch, names)
        check_authorization(request, profile)
        key = "dispatch_"+request["request_digest"][:40]
        with edit_lock(root/"dispatch-scope"), edit_lock(root/(key+".lock-target")):
            directory = root/key
            if directory.exists():
                ledger = _ledger(directory/"ledger.json")
                if ledger["request_digest"] != request["request_digest"] or ledger["bundle_sha256"] != sha(bundle):
                    raise RemoteError("REMOTE_INTENT_CONFLICT")
                if ledger["status"] != "prepared":
                    return summary(ledger)
            else:
                for prior_path in root.glob("dispatch_*/ledger.json"):
                    prior = _ledger(prior_path)
                    if prior["status"] == "submission_unknown":
                        old = read_json(prior_path.parent/"request.json")
                        if all(old["parent"][k] == request["parent"][k] for k in ("job_id", "step_id")):
                            raise RemoteError("REMOTE_PREVIOUS_SUBMISSION_UNKNOWN")
                directory.mkdir()
                copy_bounded(bundle, directory/"request.zip")
                write_json(directory/"request.json", request)
                ledger = {"schema_version": "fsf_remote_ledger_v1", "dispatch_id": key,
                    "request_digest": request["request_digest"], "bundle_digest": bundle_identity(request),
                    "bundle_sha256": sha(directory/"request.zip"), "profile_digest": digest(profile),
                    "status": "prepared", "created_at": time.time(), "remote": {}, "submitted_count": 0,
                    "accepted_result_digest": None}
                _save(directory/"ledger.json", ledger)
            if adapter is None:
                from fsf.remote.transports import adapter_for
                adapter = adapter_for(profile)
            adapter.prepare(request, directory/"request.zip", directory)
            # Durable before the first irreversible submit. A thrown/ambiguous
            # response can only be reconciled, never blindly submitted again.
            ledger["status"] = "submission_unknown"
            ledger = _save(directory/"ledger.json", ledger)
            try:
                ledger["remote"] = adapter.submit(request, directory/"request.zip", directory)
                ledger["submitted_count"] = 1
                ledger["status"] = "submitted"
            except KeyboardInterrupt:
                _save(directory/"ledger.json", ledger)
                raise RemoteError("REMOTE_CANCELLED") from None
            except Exception:
                return summary(_save(directory/"ledger.json", ledger))
            return summary(_save(directory/"ledger.json", ledger))
    finally:
        shutil.rmtree(scratch)


def update_dispatch(dispatch_id, *, profile, state_root, action="status", adapter=None):
    from fsf.remote.contracts import _path_parts
    if len(_path_parts(dispatch_id)) != 1 or not dispatch_id.startswith("dispatch_"):
        raise RemoteError("REMOTE_DISPATCH_ID")
    root = _root(state_root)
    directory = root/dispatch_id
    with edit_lock(root/(dispatch_id+".lock-target")):
        ledger = _ledger(directory/"ledger.json")
        request = read_json(directory/"request.json")
        check_authorization(request, profile)
        if digest(profile) != ledger["profile_digest"] or request["request_digest"] != ledger["request_digest"]:
            raise RemoteError("REMOTE_PROFILE_CHANGED")
        if adapter is None:
            from fsf.remote.transports import adapter_for
            adapter = adapter_for(profile)
        if time.time()-ledger["created_at"] > request["policy"]["deadline_seconds"]:
            ledger["status"] = "expired"
            return summary(_save(directory/"ledger.json", ledger))
        if action == "cancel":
            ledger["status"] = "cancel_requested"
            _save(directory/"ledger.json", ledger)
            state = adapter.cancel(request, ledger["remote"], directory)
            ledger["status"] = {"confirmed":"cancelled","requested":"cancel_requested","already_finished":"remote_completed"}.get(state,"cancel_unsupported")
        elif action == "reconcile" or ledger["status"] == "submission_unknown":
            remote = adapter.reconcile(request, directory)
            if remote is not None:
                ledger["remote"] = remote
                ledger["submitted_count"] = 1
                ledger["status"] = "submitted"
        elif action == "status":
            if ledger["status"] not in {"verified", "remote_failed", "cancelled", "expired"}:
                state = adapter.status(request, ledger["remote"], directory)
                if state not in {"queued", "running", "completed", "failed", "cancelled", "unknown"}:
                    raise RemoteError("REMOTE_PROVIDER_STATUS")
                if state == "failed":
                    ledger["status"] = "remote_failed"
                elif state == "cancelled":
                    ledger["status"] = "cancelled"
                elif state in {"queued", "running"}:
                    ledger["status"] = state
                elif state == "completed":
                    ledger["status"] = "remote_completed"
        elif action == "fetch":
            if ledger["status"] in {"submission_unknown", "cancelled", "expired"}:
                raise RemoteError("REMOTE_NOT_READY")
            # Re-fetch and revalidate even an already accepted result; no
            # permanent conformance or review approval is inferred from reuse.
            temp = Path(tempfile.mkdtemp(prefix=".remote-fetch-", dir=directory))
            try:
                archive = temp/"download.zip"
                adapter.fetch(request, ledger["remote"], archive, directory)
                ledger["status"] = "downloaded"
                _save(directory/"ledger.json", ledger)
                manifest, staged = validate_result(archive, request, temp)
                if ledger["accepted_result_digest"] not in (None, manifest["result_digest"]):
                    raise RemoteError("REMOTE_RESULT_CONFLICT")
                accepted = directory/"accepted"
                if accepted.exists():
                    prior_manifest=_verify_accepted(accepted, request)
                    if prior_manifest["result_digest"]!=manifest["result_digest"]:
                        raise RemoteError("REMOTE_RESULT_CONFLICT")
                else:
                    os.replace(staged, accepted)
                ledger["accepted_result_digest"] = manifest["result_digest"]
                ledger["status"] = "verified"
            finally:
                shutil.rmtree(temp)
        else:
            raise RemoteError("REMOTE_ACTION")
        return summary(_save(directory/"ledger.json", ledger))


def _verify_accepted(folder, request):
    manifest = read_json(folder/"result.json")
    shape(manifest, "result")
    check_digest(manifest, "result_digest")
    if (manifest["parent"] != request["parent"] or manifest["child"] != child_identity(request)
            or manifest["request_digest"] != request["request_digest"] or manifest["bundle_digest"] != bundle_identity(request)
            or manifest["fsf_version"] != request["fsf_version"] or manifest["status"] != "completed" or manifest["error_code"] is not None):
        raise RemoteError("REMOTE_RESULT_IDENTITY")
    journal = read_json(folder/"child-journal.json")
    if (sha(folder/"child-journal.json") != manifest["child_journal_sha256"] or journal.get("child") != child_identity(request)
            or journal.get("request_digest") != request["request_digest"] or journal.get("status") != "completed"
            or journal.get("plan_digest") != digest(request["child"]["plan"])
            or set(journal.get("steps",{})) != {s["step_id"] for s in request["recipe"]["steps"]}):
        raise RemoteError("REMOTE_CHILD_JOURNAL")
    environment = read_json(folder/"environment.json")
    if digest(environment) != manifest["environment_digest"]:
        raise RemoteError("REMOTE_ENVIRONMENT_INTEGRITY")
    expected = [a for s in request["recipe"]["steps"] for a in s["outputs"]]
    if len(manifest["artifacts"]) != len(expected) or len({a["artifact_id"] for a in manifest["artifacts"]}) != len(expected):
        raise RemoteError("REMOTE_OUTPUTS")
    actual_by_id = {a["artifact_id"]: a for a in manifest["artifacts"]}
    for step in request["recipe"]["steps"]:
        record = journal.get("steps", {}).get(step["step_id"], {})
        if record.get("status") != "completed" or record.get("recipe_step_digest") != digest(step):
            raise RemoteError("REMOTE_CHILD_JOURNAL")
        if record.get("artifacts") != [actual_by_id.get(a["artifact_id"]) for a in step["outputs"]]:
            raise RemoteError("REMOTE_CHILD_JOURNAL")
    for declared in expected:
        actual = actual_by_id.get(declared["artifact_id"])
        if actual is None or any(actual[k] != declared[k] for k in declared):
            raise RemoteError("REMOTE_OUTPUTS")
        path = safe_path(folder/"outputs", actual["path"])
        if file_item(path, actual["artifact_id"], actual["path"], actual["role"], actual["media_type"]) != actual:
            raise RemoteError("REMOTE_OUTPUT_INTEGRITY")
        if actual["role"] == "video":
            if not request["expected_media_contract"]:
                raise RemoteError("REMOTE_QC_CONTRACT_REQUIRED")
            from fsf.conformance.media import evaluate_media_file
            report, _action = evaluate_media_file(path, expected_contract=request["expected_media_contract"])
            if report.get("evaluation_status") != "conforms":
                raise RemoteError("REMOTE_QC_FAILED")
    if request["video_condition"]:
        from fsf.runtime.blender import validate_condition_ack
        validate_condition_ack(request["video_condition"], request["recipe"]["condition_capability"], manifest["condition_acknowledgement"])
    if request["consistency_requirements"]:
        # Reuse the existing Handoff verifier on a truthful protocol projection
        # of this remote result; this creates no conformance/review approval.
        from fsf.runtime.handoff import build_workorder, ingest_workorder_result
        parent = request["parent"]
        item = manifest["artifacts"][0]
        work = build_workorder(job_id=parent["job_id"], run_id=parent["run_id"], step_id=parent["step_id"],
            attempt_id=parent["attempt_id"], tool=request["recipe"]["steps"][0]["tool"],
            plan_digest=parent["plan_digest"], inputs_digest=parent["inputs_digest"], target_artifact="outputs/"+item["path"],
            task_payload={}, consistency_requirements=request["consistency_requirements"])
        projection = {k: work[k] for k in ("job_id", "run_id", "step_id", "attempt_id", "workorder_id", "plan_digest", "inputs_digest")}
        projection.update(schema_version="fsf_workorder_result_v1", status="completed", artifact_path="outputs/"+item["path"],
            artifact_sha256=item["sha256"], consistency_acknowledgement=manifest["consistency_acknowledgement"])
        temp = folder/".consistency-result.json"
        try:
            write_json(temp, projection)
            ingest_workorder_result(temp, work, folder)
        finally:
            temp.unlink(missing_ok=True)
    return manifest


def validate_result(archive, request, staging):
    folder = Path(staging)/"verified"
    folder.mkdir()
    names = unpack(archive, folder)
    manifest = _verify_accepted(folder, request)
    expected = {"result.json", "child-journal.json", "environment.json", *("outputs/"+a["path"] for a in manifest["artifacts"])}
    if names != expected:
        raise RemoteError("REMOTE_UNDECLARED_FILE")
    return manifest, folder


def accepted_result(dispatch_id, *, profile, state_root):
    root = _root(state_root)
    directory = safe_path(root, dispatch_id+"/request.json").parent
    request = read_json(directory/"request.json")
    check_authorization(request, profile)
    ledger = _ledger(directory/"ledger.json")
    manifest = _verify_accepted(directory/"accepted", request)
    if ledger["status"] != "verified" or ledger["accepted_result_digest"] != manifest["result_digest"]:
        raise RemoteError("REMOTE_NOT_VERIFIED")
    return manifest, directory/"accepted"
