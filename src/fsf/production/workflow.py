"""Durable finite leases and fenced checkpoints. Provider calls stay in adapters."""
from __future__ import annotations

import copy
import math
import re
import time

from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, integer, key, publish, seal
from fsf.production.segments import affected_tasks


class Workflow:
    def __init__(self, path):
        self.path = path

    def create(self, *, production_id, binding_digest, dependencies):
        key(production_id)
        if not isinstance(binding_digest,str) or not re.fullmatch("[0-9a-f]{64}",binding_digest):
            raise ProductionError("PRODUCTION_INVALID")
        for task_id, deps in dependencies.items():
            key(task_id)
            if len(deps) != len(set(deps)):
                raise ProductionError("PRODUCTION_DEPENDENCY_INVALID")
        affected_tasks(dependencies,[])
        value = {"schema_version":"fsf_production_workflow_v1","production_id":production_id,
                 "binding_digest":binding_digest,"dependencies":copy.deepcopy(dependencies),
                 "paused":False,"revision":1,"tasks":{k:{"epoch":0,"attempts":[],"accepted":None} for k in dependencies}}
        with edit_lock(self.path) as path:
            if path.exists():
                prior = self.inspect()
                if any(prior[k] != value[k] for k in ("production_id","binding_digest","dependencies")):
                    raise ProductionError("PRODUCTION_WORKFLOW_CONFLICT")
                return prior
            publish(path,seal(value,"state_digest"))
        return seal(value,"state_digest")

    def inspect(self):
        value = read_json(self.path)
        if value.get("schema_version") != "fsf_production_workflow_v1" or seal(value,"state_digest") != value:
            raise ProductionError("PRODUCTION_INTEGRITY")
        return value

    def pause(self, enabled=True):
        if type(enabled) is not bool:
            raise ProductionError("PRODUCTION_INVALID")
        with edit_lock(self.path) as path:
            state = self.inspect()
            state["paused"] = enabled
            self._save(path,state)
        return state

    def claim(self, task_id, *, owner, seconds=300, now=None):
        key(owner)
        integer(seconds,1,5400)
        now = time.time() if now is None else now
        if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now) or now < 0:
            raise ProductionError("PRODUCTION_INVALID")
        with edit_lock(self.path) as path:
            state = self.inspect()
            if task_id not in state["tasks"]:
                raise ProductionError("PRODUCTION_NOT_FOUND")
            item = state["tasks"][task_id]
            if state["paused"] or item["accepted"] or any(not state["tasks"][k]["accepted"] for k in state["dependencies"][task_id]):
                raise ProductionError("PRODUCTION_NOT_READY")
            if item["attempts"]:
                last = item["attempts"][-1]
                if last["status"] not in {"rejected","failed","cancelled"} and not last.get("invalidated",False):
                    # Expiration is not proof a remote request stopped billing.
                    raise ProductionError("PRODUCTION_RECONCILE_REQUIRED" if last["expires_at"] <= now else "PRODUCTION_LEASE_HELD")
            if len(item["attempts"]) >= 3:
                raise ProductionError("PRODUCTION_ATTEMPTS_EXCEEDED")
            item["epoch"] += 1
            attempt = {"epoch":item["epoch"],"owner":owner,"expires_at":now+seconds,
                       "request_id":digest([state["binding_digest"],task_id,item["epoch"]]),
                       "status":"reserved","evidence":None}
            item["attempts"].append(attempt)
            self._save(path,state)
            return copy.deepcopy(attempt)

    def record(self, task_id, *, epoch, request_id, status, evidence=None, artifact_path=None):
        allowed = {"reserved":{"submitted","unknown","failed"},
                   "submitted":{"running","produced","unknown","failed","cancelled"},
                   "running":{"produced","unknown","failed","cancelled"},
                   "unknown":{"submitted","running","produced","failed","cancelled"},
                   "produced":{"verified","rejected"},"verified":{"machine_accepted","rejected"}}
        with edit_lock(self.path) as path:
            state = self.inspect()
            item = state["tasks"].get(task_id)
            if not item or not item["attempts"]:
                raise ProductionError("PRODUCTION_NOT_FOUND")
            attempt = item["attempts"][-1]
            if type(epoch) is not int or epoch != item["epoch"] or request_id != attempt["request_id"]:
                raise ProductionError("PRODUCTION_FENCED")
            if status not in allowed.get(attempt["status"],set()):
                raise ProductionError("PRODUCTION_STATE_CONFLICT")
            if status in {"verified","machine_accepted"}:
                from fsf.production.control import file_identity
                if (not isinstance(evidence,dict) or artifact_path is None
                        or any(not isinstance(evidence.get(k),str) or not re.fullmatch("[0-9a-f]{64}",evidence[k])
                               for k in ("artifact_sha256","policy_digest","report_digest"))
                        or evidence.get("verdict") != "accept"
                        or seal(evidence,"report_digest") != evidence
                        or file_identity(artifact_path)["sha256"] != evidence["artifact_sha256"]):
                    raise ProductionError("PRODUCTION_EVIDENCE_REQUIRED")
                if status == "machine_accepted" and evidence != attempt["evidence"]:
                    raise ProductionError("PRODUCTION_EVIDENCE_REQUIRED")
            attempt["status"] = status
            if evidence is not None:
                attempt["evidence"] = copy.deepcopy(evidence)
            if status == "machine_accepted":
                item["accepted"] = copy.deepcopy(evidence)
            self._save(path,state)
            return copy.deepcopy(attempt)

    def invalidate(self, changed):
        with edit_lock(self.path) as path:
            state = self.inspect()
            affected = affected_tasks(state["dependencies"],changed)
            for task_id in affected:
                item = state["tasks"][task_id]
                item["epoch"] += 1
                item["accepted"] = None
                if item["attempts"] and item["attempts"][-1]["status"] not in {"failed","cancelled","rejected","machine_accepted"}:
                    raise ProductionError("PRODUCTION_RECONCILE_REQUIRED")
                if item["attempts"]:
                    item["attempts"][-1]["invalidated"] = True
            self._save(path,state)
            return affected

    @staticmethod
    def _save(path,state):
        state["revision"] += 1
        publish(path,seal(state,"state_digest"))
