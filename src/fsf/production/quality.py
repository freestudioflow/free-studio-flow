"""Bounded evaluator evidence. No digest or model self-report proves correctness."""
from __future__ import annotations

from fsf.production.contracts import ProductionError, digest, seal, key
import re

CATEGORIES = frozenset({"identity","costume_prop","action","seam","static_substitute","intended_still"})


def content_verdict(value, *, artifact_sha256, evaluator_digest, policy_digest, sample_hashes):
    required = {"artifact_sha256","evaluator_digest","policy_digest","sample_hashes","verdict","defects"}
    if not isinstance(value,dict) or set(value) != required:
        raise ProductionError("PRODUCTION_ANALYSIS_FAILED")
    if (value["artifact_sha256"] != artifact_sha256 or value["evaluator_digest"] != evaluator_digest
            or value["policy_digest"] != policy_digest or value["sample_hashes"] != sample_hashes
            or not sample_hashes or value["verdict"] not in {"accept","reject","unknown"}
            or not isinstance(value["defects"],list) or len(value["defects"]) > 256):
        raise ProductionError("PRODUCTION_ANALYSIS_FAILED")
    for item in value["defects"]:
        if (not isinstance(item,dict) or set(item) != {"category","start_frame","end_frame"}
                or item["category"] not in CATEGORIES or type(item["start_frame"]) is not int
                or type(item["end_frame"]) is not int or not 0 <= item["start_frame"] < item["end_frame"]):
            raise ProductionError("PRODUCTION_ANALYSIS_FAILED")
    if value["verdict"] == "accept" and value["defects"]:
        raise ProductionError("PRODUCTION_ANALYSIS_FAILED")
    return seal({"schema_version":"fsf_production_verdict_v1",**value,
                 "motion_review":"not_reviewed","listening_review":"not_reviewed"},"report_digest")


def calibration_report(examples):
    """A locked holdout: six categories, ten positive and ten negative per category."""
    if not isinstance(examples,list) or len(examples) != 120:
        raise ProductionError("PRODUCTION_CALIBRATION_INCOMPLETE")
    seen,cells = set(),{k:{"good":0,"bad":0,"good_accept":0,"bad_accept":0,"bad_reject":0} for k in CATEGORIES}
    for item in examples:
        if (set(item) != {"sample_id","source_group","category","ground_truth","verdict"}
                or item["sample_id"] in seen or item["category"] not in cells
                or type(item["ground_truth"]) is not bool or item["verdict"] not in {"accept","reject","unknown"}):
            raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
        seen.add(item["sample_id"])
        cell = cells[item["category"]]
        cell["good" if item["ground_truth"] else "bad"] += 1
        if item["verdict"] == "accept":
            cell["good_accept" if item["ground_truth"] else "bad_accept"] += 1
        if not item["ground_truth"] and item["verdict"] == "reject":
            cell["bad_reject"] += 1
    if any(c["good"] != 10 or c["bad"] != 10 for c in cells.values()):
        raise ProductionError("PRODUCTION_CALIBRATION_INCOMPLETE")
    passed = all(c["good_accept"] >= 9 and c["bad_reject"] >= 9 for c in cells.values()) and sum(c["bad_accept"] for c in cells.values()) <= 3
    return {"passed":passed,"holdout_digest":digest(examples),"confusion":cells,
            "scope":"finite_holdout_not_full_film_guarantee"}


def validate_corpus(samples):
    """Check declared double annotations and split isolation, not human identity.

    The operator supplies real reviewed windows. This does not generate labels,
    attest that annotators are people, or inspect media from a declared hash.
    """
    if not isinstance(samples,list) or len(samples) != 240:
        raise ProductionError("PRODUCTION_CALIBRATION_INCOMPLETE")
    seen,groups,hashes=set(),{},{}
    counts={(split,cat,truth):0 for split in ("development","holdout") for cat in CATEGORIES for truth in (True,False)}
    for item in samples:
        if (not isinstance(item,dict) or set(item) != {"sample_id","source_group","category","subset","artifact_sha256","annotations"}
                or item["category"] not in CATEGORIES or item["subset"] not in {"development","holdout"}
                or not isinstance(item["artifact_sha256"],str) or not re.fullmatch("[0-9a-f]{64}",item["artifact_sha256"])
                or not isinstance(item["annotations"],list) or len(item["annotations"]) != 2):
            raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
        key(item["sample_id"]);key(item["source_group"])
        if item["sample_id"] in seen:raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
        seen.add(item["sample_id"])
        annotators=[];labels=[]
        for label in item["annotations"]:
            if not isinstance(label,dict) or set(label) != {"annotator_id","ground_truth"} or type(label["ground_truth"]) is not bool:
                raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
            annotators.append(key(label["annotator_id"]));labels.append(label["ground_truth"])
        if annotators[0]==annotators[1] or labels[0] != labels[1]:
            raise ProductionError("PRODUCTION_ANNOTATION_UNRESOLVED")
        for index,value in ((groups,item["source_group"]),(hashes,item["artifact_sha256"])):
            if value in index and index[value] != item["subset"]:
                raise ProductionError("PRODUCTION_HOLDOUT_LEAKAGE")
            index[value]=item["subset"]
        counts[item["subset"],item["category"],labels[0]]+=1
    if any(v != 10 for v in counts.values()):raise ProductionError("PRODUCTION_CALIBRATION_INCOMPLETE")
    return seal({"schema_version":"fsf_calibration_corpus_v1","samples":samples,
                 "annotation_status":"operator_declared_double_labels"},"corpus_digest")


def evaluate_holdout(corpus, predictions, *, evaluator_digest, policy_digest):
    """Require exact held-out IDs and bindings; unknown is never a correct reject."""
    if (not isinstance(corpus,dict) or set(corpus) != {"schema_version","samples","annotation_status","corpus_digest"}
            or validate_corpus(corpus["samples"]) != corpus
            or not isinstance(predictions,dict) or set(predictions) != {"corpus_digest","evaluator_digest","policy_digest","results"}
            or predictions["corpus_digest"] != corpus["corpus_digest"]
            or predictions["evaluator_digest"] != evaluator_digest or predictions["policy_digest"] != policy_digest
            or not all(isinstance(x,str) and re.fullmatch("[0-9a-f]{64}",x) for x in (evaluator_digest,policy_digest))
            or not isinstance(predictions["results"],list)):
        raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
    windows={s["sample_id"]:s for s in corpus["samples"] if s["subset"]=="holdout"}
    rows=[];seen=set()
    for item in predictions["results"]:
        if (not isinstance(item,dict) or set(item) != {"sample_id","artifact_sha256","verdict"}
                or item["sample_id"] not in windows or item["sample_id"] in seen
                or item["artifact_sha256"] != windows[item["sample_id"]]["artifact_sha256"]):
            raise ProductionError("PRODUCTION_CALIBRATION_INVALID")
        sample=windows[item["sample_id"]];seen.add(item["sample_id"])
        rows.append({"sample_id":item["sample_id"],"source_group":sample["source_group"],"category":sample["category"],
                     "ground_truth":sample["annotations"][0]["ground_truth"],"verdict":item["verdict"]})
    report=calibration_report(rows)
    return seal({**report,"corpus_digest":corpus["corpus_digest"],"evaluator_digest":evaluator_digest,
                 "policy_digest":policy_digest,"human_review":"not_reviewed"},"calibration_digest")
