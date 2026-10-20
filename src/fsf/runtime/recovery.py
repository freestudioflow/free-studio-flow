"""Durable bounded video retries over the existing plan executor and QC gates."""
import copy
from importlib import resources
import json
import time
from pathlib import Path
from jsonschema import Draft202012Validator

from fsf.core.edit_store import edit_lock, read_json
from fsf.core.asset_library import _root
from fsf.remote.bundle import copy_bounded
from fsf.remote.contracts import RemoteError, check_digest, digest, seal, sha, safe_path, write_json

VIDEO_TOOLS = {"studio.video.generate", "studio.video.remaster"}
AUTH_CODES = {"API_AUTH_FAILED", "API_AUTH_MISSING", "API_CONNECTION_PAUSED", "API_SECRET_UNAVAILABLE",
    "API_SECRET_EXPIRED", "API_SECRET_REVISION", "API_BEDROCK_ACCESS_DENIED", "API_BEDROCK_EXPIRED",
    "API_REQUEST_REJECTED", "REMOTE_NOT_AUTHORIZED"}


def validate_policy(policy):
    schema=json.loads(resources.files("fsf.schemas").joinpath("fsf_generation_recovery_policy_v1.schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(policy):
        raise RemoteError("RECOVERY_POLICY_INVALID")
    fields = {"schema_version", "authorized", "max_attempts", "max_dispatches", "max_new_dispatches_per_run", "deadline_seconds"}
    if (not isinstance(policy, dict) or set(policy) != fields
            or policy["schema_version"] != "fsf_generation_recovery_policy_v1"
            or policy["authorized"] is not True):
        raise RemoteError("RECOVERY_POLICY_INVALID")
    for key, upper in (("max_attempts", 10), ("max_dispatches", 1000),
                       ("max_new_dispatches_per_run", 100), ("deadline_seconds", 86400)):
        if type(policy[key]) is not int or not 1 <= policy[key] <= upper:
            raise RemoteError("RECOVERY_POLICY_INVALID")
    return copy.deepcopy(policy)


class Recovery:
    def __init__(self, executor, plan):
        self.executor = executor
        self.plan = plan
        self.policy = validate_policy(executor.recovery_policy)
        self.root = executor.output_dir
        self.path = self.root / "fsf_generation_recovery.json"
        self.steps = {s["id"]: s for s in plan["steps"]}
        self.producers = {sid for sid, s in self.steps.items() if s.get("tool") in VIDEO_TOOLS}
        self.qc_map = {}
        for sid, step in self.steps.items():
            if step.get("tool") != "pdx.media.technical_conform":
                continue
            ref = step.get("inputs", {}).get("target_artifact")
            producers = [p for p in self.producers if ref in self.steps[p].get("outputs", [])]
            if len(producers) > 1:
                raise RemoteError("RECOVERY_AMBIGUOUS_PRODUCER")
            if producers:
                p = producers[0]
                if p not in step.get("depends_on", []):
                    raise RemoteError("RECOVERY_QC_BINDING")
                expected = step.get("inputs", {}).get("expected_media_contract", {})
                limit = expected.get("max_freeze_duration_seconds")
                if type(limit) not in (int, float) or not 0 <= limit <= .5:
                    raise RemoteError("RECOVERY_FREEZE_GATE_REQUIRED")
                self.qc_map[sid] = p
        if self.producers - set(self.qc_map.values()):
            raise RemoteError("RECOVERY_QC_REQUIRED")
        self.identity = digest({"plan": plan, "policy": self.policy,
            "job_id": executor.job_id, "run_id": executor.run_id, "executor": executor.executor_override,
            "handoff": executor.handoff})
        self.new_count = 0
        self.state = {"identity": self.identity, "status": "ready", "created_at": time.time(),
            "dispatches": 0, "steps": {}, "force": [], "events": []}
        if self.path.exists():
            self.state = read_json(self.path)
            check_digest(self.state, "recovery_digest")
            if self.state.get("identity") != self.identity:
                raise RemoteError("RECOVERY_IDENTITY_CHANGED")
        elif (self.root / "fsf_execution_journal.json").exists():
            raise RemoteError("RECOVERY_REQUIRES_FRESH_STATE")

    def save(self):
        write_json(self.path, seal(self.state, "recovery_digest"))

    def closure(self, producers):
        result = set(producers)
        while True:
            more = {sid for sid, s in self.steps.items() if set(s.get("depends_on", [])) & result}
            if more <= result:
                return result
            result |= more

    def before(self, step):
        sid = step["id"]
        if sid not in self.producers:
            return
        if self.state["status"] == "paused_auth":
            raise RemoteError("API_CONNECTION_PAUSED")
        if self.state["status"] == "paused_quota":
            raise RemoteError("API_RATE_LIMITED")
        if self.state["status"] == "paused_unknown":
            raise RemoteError("RECOVERY_SUBMISSION_UNKNOWN")
        if time.time() - self.state["created_at"] >= self.policy["deadline_seconds"]:
            raise RemoteError("RECOVERY_BUDGET_EXHAUSTED")
        record = self.state["steps"].get(sid)
        if record and record["status"] in {"waiting_external", "inflight"}:
            self.executor._recovery_attempt = record["attempt"]
            if record["status"] == "inflight" and not self.executor._is_handoff_tool_step(step):
                raise RemoteError("RECOVERY_SUBMISSION_UNKNOWN")
            return
        if self.new_count >= self.policy["max_new_dispatches_per_run"]:
            raise RemoteError("RECOVERY_BATCH_PAUSED")
        if (time.time() - self.state["created_at"] >= self.policy["deadline_seconds"]
                or self.state["dispatches"] >= self.policy["max_dispatches"]):
            raise RemoteError("RECOVERY_BUDGET_EXHAUSTED")
        attempt = (record["attempt"] if record else 0) + 1
        if attempt > self.policy["max_attempts"]:
            raise RemoteError("RECOVERY_ATTEMPTS_EXHAUSTED")
        self.new_count += 1
        self.state["dispatches"] += 1
        self.state["steps"][sid] = {"attempt": attempt, "status": "inflight"}
        self.executor._recovery_attempt = attempt
        self.save()  # durable intent before a side effect; no blind replay

    def after(self, step, result):
        sid = step["id"]
        if sid not in self.producers:
            return
        record = self.state["steps"].get(sid)
        if record:
            record["status"] = "waiting_external" if result.get("status") in {"pending_handoff","pending_external"} else result.get("status", "failed")
            if result.get("resume_identity"):
                record["resume_identity"] = copy.deepcopy(result["resume_identity"])
            self.save()

    def checkpoint(self, results):
        # Persist the observed step before marking its durable intent complete.
        # On abrupt exit the next run can reuse verified bytes, not re-dispatch.
        steps = copy.deepcopy(results)
        for value in steps.values():
            if value.get("status") == "reused":
                value["status"] = "completed"
        source_sha = next((s.get("inputs",{}).get("source_sha256") for s in self.plan["steps"]
                           if s.get("inputs",{}).get("source_sha256")), "")
        write_json(self.root/"fsf_execution_journal.json", {
            "schema_version":"fsf_plan_execution_journal_v1", "job_id":self.executor.current_job_id,
            "run_id":self.executor.current_run_id, "plan_digest":digest(self.plan),
            "source_sha256":source_sha, "steps":steps})
        for sid,result in results.items():
            if sid in self.producers and result.get("status") not in {"blocked"}:
                self.after(self.steps[sid],result)
        codes={r.get("error_code") or r.get("error") for r in results.values() if r.get("status")=="failed"}
        if codes & AUTH_CODES:
            self.state["status"]="paused_auth"
        elif "API_RATE_LIMITED" in codes:
            self.state["status"]="paused_quota"
        elif codes & {"RECOVERY_SUBMISSION_UNKNOWN","REMOTE_SUBMISSION_UNKNOWN","API_SUBMISSION_UNKNOWN","API_CONNECTION_UNKNOWN","API_NOT_COMPLETED"}:
            self.state["status"]="paused_unknown"
        self.save()

    def archive(self, result, producers):
        for sid in producers:
            entry = result["steps"].get(sid, {})
            attempt = self.state["steps"][sid]["attempt"]
            history=self.root/"attempts"
            history.mkdir(exist_ok=True)
            _root(history)
            target = history / digest({"step": sid})[:20] / str(attempt)
            target.mkdir(parents=True, exist_ok=True)
            _root(target)
            files = []
            selected = self.closure({sid})
            for selected_id in selected:
                for item in result["steps"].get(selected_id, {}).get("output_files", []):
                    source = safe_path(self.root, item["path"])
                    if sha(source) != item["sha256"]:
                        raise RemoteError("RECOVERY_SOURCE_CHANGED")
                    name = digest({"step": selected_id, "path": item["path"]}) + source.suffix
                    dest = target / name
                    if not dest.exists():
                        copy_bounded(source, dest)
                    if sha(dest) != item["sha256"]:
                        raise RemoteError("RECOVERY_ARCHIVE_CHANGED")
                    files.append({"step_id": selected_id, "original_path": item["path"],
                        "archive_path": name, "sha256": item["sha256"]})
            write_json(target / "record.json", seal({"producer_step_id": sid, "attempt": attempt,
                "files": files, "steps": {s: result["steps"][s] for s in selected if s in result["steps"]}}, "attempt_digest"))

    def execute(self):
        if self.executor.no_resume:
            raise RemoteError("RECOVERY_NO_RESUME_FORBIDDEN")
        if self.state["status"] in {"paused_auth", "paused_quota", "paused_unknown", "exhausted"}:
            if not self.executor.resume_paused or self.state["status"] in {"paused_unknown", "exhausted"}:
                return self.summary()
            for sid, record in self.state["steps"].items():
                if record["status"] == "failed":
                    record["status"] = "retry_pending"
                    self.state["force"] = sorted(set(self.state["force"]) | self.closure({sid}))
        self.state["status"] = "ready"
        self.save()
        try:
            while True:
                self.executor._recovery_force = set(self.state["force"])
                result = self.executor.execute_plan(self.plan)
                # Preserve cumulative dispatch counters independently of the base
                # executor's per-pass counters, and retain each attempted journal.
                write_json(self.root / "recovery-last-result.json", result)
                failures = [r for r in result["steps"].values() if r.get("status") == "failed"]
                codes = {r.get("error_code") or r.get("error") for r in failures}
                if codes & AUTH_CODES:
                    self.state["status"] = "paused_auth"
                elif "API_RATE_LIMITED" in codes:
                    self.state["status"] = "paused_quota"
                elif codes & {"RECOVERY_SUBMISSION_UNKNOWN", "REMOTE_SUBMISSION_UNKNOWN", "API_SUBMISSION_UNKNOWN", "API_CONNECTION_UNKNOWN", "API_NOT_COMPLETED"}:
                    self.state["status"] = "paused_unknown"
                elif codes & {"RECOVERY_BUDGET_EXHAUSTED", "RECOVERY_ATTEMPTS_EXHAUSTED"}:
                    self.state["status"] = "exhausted"
                elif "RECOVERY_BATCH_PAUSED" in codes:
                    self.state["status"] = "paused_batch"
                else:
                    bad = {self.qc_map[sid] for sid, r in result["steps"].items()
                           if sid in self.qc_map and r.get("evaluation_status") == "does_not_conform"}
                    if bad:
                        self.archive(result, bad)
                        if any(self.state["steps"][sid]["attempt"] >= self.policy["max_attempts"] for sid in bad):
                            self.state["status"] = "exhausted"
                        else:
                            for sid in bad:
                                self.state["steps"][sid]["status"] = "retry_pending"
                            self.state["force"] = sorted(self.closure(bad))
                            self.state["events"].append({"code": "QC_RETRY", "producers": sorted(bad)})
                            self.save()
                            continue
                    elif any(r.get("status") in {"pending_handoff","pending_external"} for r in result["steps"].values()):
                        self.state["status"] = "waiting_external"
                    else:
                        self.state["status"] = "completed" if result.get("success") else "blocked"
                        self.state["force"] = []
                self.save()
                result.update(recovery=self.summary())
                if self.state["status"] not in {"completed", "blocked"}:
                    result.update(status=self.state["status"], success=False)
                return result
        except BaseException:
            self.state["status"] = "paused_unknown"
            self.save()
            raise

    def summary(self):
        return {"status": self.state["status"], "success": self.state["status"]=="completed",
            "video_dispatches": self.state["dispatches"], "attempts": {
                sid: r["attempt"] for sid, r in self.state["steps"].items()}}


def execute_recoverable(executor, plan):
    with edit_lock(executor.output_dir / "generation-recovery"):
        controller = Recovery(executor, plan)
        executor._recovery_controller = controller
        try:
            return controller.execute()
        finally:
            executor._recovery_controller = None
            executor._recovery_force = set()
