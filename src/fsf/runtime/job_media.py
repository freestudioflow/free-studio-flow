"""Local job-media verification against a plan and execution journal."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil

from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.conformance.media import evaluate_media_file
from fsf.core.asset_library import _no_links, _path_parts
from fsf.core.edit_store import read_json
from fsf.runtime.blender import sha


class JobMediaError(ValueError):
    pass


@contextmanager
def local_media_tools():
    """Use installed system tools or an existing static-ffmpeg cache offline."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        yield ffmpeg
        return
    try:
        from static_ffmpeg.run import get_platform_dir
        directory = get_platform_dir()
    except (ImportError, OSError):
        raise JobMediaError("FFMPEG_REQUIRED") from None
    ffmpeg, ffprobe = (shutil.which(name, path=directory) for name in ("ffmpeg","ffprobe"))
    if not ffmpeg or not ffprobe:
        raise JobMediaError("FFMPEG_REQUIRED")
    old_path = os.environ.get("PATH")
    os.environ["PATH"] = directory + os.pathsep + (old_path or "")
    try:
        yield ffmpeg
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


def _job_file(root, relative):
    try:
        path = root.joinpath(*_path_parts(relative))
        _no_links(path)
        if not path.is_file():
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise JobMediaError("JOB_FILE_INVALID") from None
    return path


def verify_job(root, *, artifact=None, plan=None):
    """Verify a journal-bound artifact, selecting assembly by default."""
    from pdx_artifact_core import validate_execution_plan

    root = Path(root).resolve(strict=True)
    plan_path = Path(plan).resolve(strict=True) if plan else _job_file(root, "plan.json")
    journal_path = _job_file(root, "fsf_execution_journal.json")
    before = {path: sha(path) for path in (plan_path,journal_path)}
    source = read_json(plan_path)
    journal = read_json(journal_path)
    plan_digest = digest(source)
    if (validate_execution_plan(source) or journal.get("schema_version") != "fsf_plan_execution_journal_v1"
            or journal.get("plan_digest") != plan_digest or not journal.get("job_id") or not journal.get("run_id")):
        raise JobMediaError("JOB_PLAN_MISMATCH")
    steps = source["steps"]
    if len({step["id"] for step in steps}) != len(steps):
        raise JobMediaError("JOB_PLAN_MISMATCH")
    source_sha = next((str(step["inputs"]["source_sha256"]) for step in steps
                       if step.get("inputs", {}).get("source_sha256")), "")
    if journal.get("source_sha256") != source_sha:
        raise JobMediaError("JOB_PLAN_MISMATCH")
    if artifact is None:
        assembled = [ref.removeprefix("artifact:") for step in steps
                     if step.get("transform") == "fsf.assembly.fast_concat"
                     for ref in step.get("outputs", []) if ref.startswith("artifact:") and ref.lower().endswith(".mp4")]
        if not assembled:
            assembled = [ref.removeprefix("artifact:") for step in steps
                         for ref in step.get("outputs", [])
                         if ref.startswith("artifact:") and ref.lower().endswith(".mp4")]
        if len(assembled) != 1:
            raise JobMediaError("JOB_ARTIFACT_SELECTION_REQUIRED")
        artifact = assembled[0]
    media = _job_file(root, artifact)
    reference = "artifact:" + artifact
    producers = [step for step in steps if reference in step.get("outputs", [])]
    if len(producers) != 1:
        raise JobMediaError("JOB_ARTIFACT_UNBOUND")
    producer = producers[0]
    completed = journal["steps"].get(producer["id"], {})
    rows = [row for row in completed.get("output_files", []) if row.get("path") == artifact]
    if completed.get("status") not in {"completed","reused"} or len(rows) != 1:
        raise JobMediaError("JOB_ARTIFACT_UNBOUND")
    expected_sha = rows[0].get("sha256")
    if sha(media) != expected_sha:
        raise JobMediaError("JOB_ARTIFACT_CHANGED")
    contracts = [producer["inputs"]] if producer.get("inputs", {}).get("expected_media_contract") else [
        step["inputs"] for step in steps
        if step.get("tool") == "pdx.media.technical_conform"
        and step.get("inputs", {}).get("target_artifact") == reference
        and producer["id"] in step.get("depends_on", [])]
    if len(contracts) != 1:
        raise JobMediaError("JOB_CONTRACT_UNBOUND")
    expected = contracts[0].get("expected_media_contract")
    contract_digest = contracts[0].get("expected_media_contract_digest")
    if not isinstance(expected, dict) or not expected or digest(expected) != contract_digest:
        raise JobMediaError("JOB_CONTRACT_UNBOUND")
    with local_media_tools():
        report, host_action = evaluate_media_file(media, expected)
    if sha(media) != expected_sha or any(sha(path) != value for path,value in before.items()):
        raise JobMediaError("JOB_ARTIFACT_CHANGED")
    return {"job_id":journal["job_id"],"run_id":journal["run_id"],"plan_digest":plan_digest,
            "artifact":artifact,"sha256":expected_sha,"contract_digest":contract_digest,
            "evaluation_status":report.get("evaluation_status","does_not_conform"),
            "report":report,"host_action":host_action}
