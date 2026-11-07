"""Durable quality decisions for the local indexed production runner."""

from copy import deepcopy
import math
import re
import json
import tempfile
import time

from fsf.production.contracts import ProductionError, digest, seal
from fsf.production.control import file_identity
from fsf.production.store import _load
from fsf.executors.subprocess_safety import run_safe_subprocess

CHECKS = {"intra_segment", "seam", "speed", "path", "semantic"}


def motion_checks(metrics):
    """Apply the frozen metric profile to independently observed measurements."""
    limits = {"speed_relative_error": .15, "step_relative_error": .15,
              "cross_track_p95_m": .25, "endpoint_error_m": .30, "event_error_s": .25}
    result = {"speed": "unknown", "path": "unknown"}
    if metrics.get("metric_scale") is not True:
        return result

    def finite(name):
        value = metrics.get(name)
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    if not all(finite(name) for name in ("coverage", "unknown_span_s")):
        return result
    if not .9 <= metrics["coverage"] <= 1 or metrics["unknown_span_s"] > .5:
        return result
    for check, fields in (("speed", ("speed_relative_error", "step_relative_error")),
                          ("path", ("cross_track_p95_m", "endpoint_error_m", "event_error_s"))):
        if all(finite(name) for name in fields):
            result[check] = "reject" if any(metrics[name] > limits[name] for name in fields) else "accept"
    return result


def reference_checks(observed, reference, frames, expected_frames):
    """Fixed fixture comparison against a time-aligned approved reference.

    Reference means include intentional lighting changes. Frame identities
    carry the independently observed mapping, not image similarity.
    """
    result = {"intra_segment": "unknown", "seam": "unknown"}
    if frames is not None and expected_frames is not None:
        result["seam"] = "accept" if frames == expected_frames else "reject"
    if (observed and reference and len(observed) == len(reference)
            and all(len(row) == 3 and all(type(v) in (int, float) and 0 <= v <= 255 for v in row)
                    for row in (*observed, *reference))):
        result["intra_segment"] = "reject" if any(
            abs(a - b) >= 10 for left, right in zip(observed, reference)
            for a, b in zip(left, right)) else "accept"
    return result


class LocalSegmentGate:
    """Trusted, cooperative local evaluator. Evaluations have no provider cost."""

    def __init__(self, *, policy_digest, evaluator_digest, required, evaluate=None,
                 scope="fixture"):
        if (scope not in {"fixture", "uncalibrated"}
                or not required or not set(required) <= CHECKS
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                       for v in (policy_digest, evaluator_digest))):
            raise ProductionError("PRODUCTION_QUALITY_CONFIG")
        self._config = {"policy_digest": policy_digest, "evaluator_digest": evaluator_digest,
                        "required": sorted(set(required)), "scope": scope,
                        "max_evaluations": 3, "cost_units": 0}
        self.evaluate = evaluate

    @property
    def config(self):
        return deepcopy(self._config)

    @classmethod
    def command(cls, argv, *, policy_digest, evaluator_digest, required, cancel_check=None):
        """Run a configured local fixture evaluator with process ownership and limits."""
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ProductionError("PRODUCTION_QUALITY_CONFIG")
        argv = tuple(argv)

        def evaluate(observation, artifact, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            with tempfile.TemporaryFile() as source:
                source.write(json.dumps({"observation": observation, "artifact": str(artifact)}).encode())
                source.seek(0)
                result = run_safe_subprocess(argv, stdin=source, timeout_seconds=min(remaining, 60),
                                             output_limit_bytes=65536, inherit_env=False,
                                             cancel_check=cancel_check)
            if not result.ok:
                return {}
            reply = json.loads(result.stdout)
            if reply.get("observation_digest") != digest(observation):
                return {}
            return reply["checks"]

        gate = cls(policy_digest=policy_digest, evaluator_digest=evaluator_digest,
                   required=required, evaluate=evaluate)
        gate._config["command_digest"] = digest(list(argv))
        return gate

    def assess(self, store, task, request, epoch, artifact, technical, deadline):
        config = self.config
        sha = file_identity(artifact)["sha256"]
        if (technical.get("artifact_sha256") != sha
                or technical.get("policy_digest") != config["policy_digest"]
                or technical != seal(technical, "report_digest")):
            raise ProductionError("PRODUCTION_EVIDENCE_REQUIRED")
        attempt = store.attempt(request)
        parents = {}
        for parent in task["depends_on"]:
            source = store.task(attempt["execution"], parent)
            if not source["accepted"]:
                raise ProductionError("PRODUCTION_NOT_READY")
            parents[parent] = digest(_load(source["accepted"]))
        observation = {"request_id": request, "task_id": task["task_id"],
                       "input_digest": task["input_digest"], "artifact_sha256": sha,
                       "technical_digest": technical["report_digest"], "parents": parents}
        observation_digest = digest(observation)
        binding = digest(config)
        with store.transaction():
            store._fence(request, epoch)
            previous = store.db.execute("""SELECT payload FROM events WHERE request=?
                AND kind='segment_quality_result' ORDER BY seq DESC LIMIT 1""", (request,)).fetchone()
            if previous:
                result = _load(previous[0])
                if result["observation_digest"] != observation_digest or result["binding_digest"] != binding:
                    raise ProductionError("PRODUCTION_SOURCE_CHANGED")
                if result["verdict"] != "unknown":
                    return result
            count = store.db.execute("""SELECT count(*) FROM events WHERE request=?
                AND kind='segment_quality_started'""", (request,)).fetchone()[0]
            if count >= config["max_evaluations"] or time.monotonic() >= deadline:
                return {"verdict": "unknown"}
            store._event("segment_quality_started", attempt["execution"], task["task_id"], request,
                         {"number": count + 1, "binding_digest": binding,
                          "observation_digest": observation_digest})
        checks = {}
        if config["scope"] == "fixture" and callable(self.evaluate):
            try:
                checks = self.evaluate(deepcopy(observation), artifact, deadline)
            except Exception:
                checks = {}
        if (time.monotonic() >= deadline or not isinstance(checks, dict)
                or any(k not in CHECKS or v not in ("accept", "reject", "unknown")
                       for k, v in checks.items())):
            checks = {}
        if file_identity(artifact)["sha256"] != sha:
            raise ProductionError("PRODUCTION_SOURCE_CHANGED")
        required = [checks.get(check, "unknown") for check in config["required"]]
        verdict = "reject" if "reject" in required else "unknown" if "unknown" in required else "accept"
        result = seal({**observation, "observation_digest": observation_digest, "binding_digest": binding,
                       "checks": checks, "verdict": verdict, "evaluation_number": count + 1,
                       "scope": config["scope"], "motion_review": "not_reviewed",
                       "listening_review": "not_reviewed"}, "quality_digest")
        with store.transaction():
            store._fence(request, epoch)
            store._event("segment_quality_result", attempt["execution"], task["task_id"], request, result)
        return result
