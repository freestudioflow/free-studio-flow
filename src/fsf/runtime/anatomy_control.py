"""Run the pinned Anatomy spring model and register its sample as control video."""
from importlib import resources
import json
from pathlib import Path
import shutil
import hashlib
import tempfile

from fsf.executors.process_cmd import ProcessCommandExecutor
from fsf.executors.subprocess_safety import run_safe_subprocess
from fsf.runtime.control_import import import_control_video
from fsf.runtime.mechanism_control import sample_mechanism


ANATOMY_COMMIT = "1482e8469b0a9e0d8cc18ea2740a01423d29ae80"
# Complete bytes of the pinned license and the sampler's transitive modules.
_SOURCE_HASHES = {
    "LICENSE": "87c078ab9d90a3044f359e6b563d4e6afea7aa060897cf4f21618687b3856796",
    "kit/audit.mjs": "74e605b5d549c1f31c8a1104db7560057105cf86809bf6ced00e4ac79194c8e2",
    "kit/iso-kit.mjs": "6d3ecc725d30c3b0346da309cf61dfa5a65017c23620bbd4093009d4fb104018",
    "kit/lathe.mjs": "a4e7e974b1a37e603ad2b76363b6f9815cc101dfff0ad277a5d4e8f1dc56fccb",
    "kit/tube.mjs": "bcb3cc360f6d83e6a5753f4c464b4587ab7965028b05a5d58e285abce58667c9",
}


def _source_bytes(root):
    result = {}
    for name, expected in _SOURCE_HASHES.items():
        path = root / name
        code = "ANATOMY_LICENSE" if name == "LICENSE" else "ANATOMY_PIN"
        try:
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise AnatomyError(code)
            content = path.read_bytes()
        except OSError:
            raise AnatomyError(code) from None
        if hashlib.sha256(content).hexdigest() != expected:
            raise AnatomyError(code)
        result[name] = content
    return result


class AnatomyError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def sampler_script():
    packaged = Path(str(resources.files("fsf.tools").joinpath("anatomy_sample.mjs")))
    if packaged.is_file():
        return packaged
    fallback = Path(__file__).resolve().parents[1] / "tools" / "anatomy_sample.mjs"
    if fallback.is_file():
        return fallback
    raise AnatomyError("ANATOMY_SAMPLER")


def verify_anatomy_checkout(checkout):
    root = Path(checkout)
    _source_bytes(root)
    env = {name: __import__("os").environ[name] for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if name in __import__("os").environ}
    result = run_safe_subprocess(["git", "-C", str(root), "rev-parse", "HEAD"], timeout_seconds=20,
        inherit_env=False, env=env, output_limit_bytes=16384)
    if result.returncode or result.timed_out or result.stdout.strip() != ANATOMY_COMMIT:
        raise AnatomyError("ANATOMY_PIN")
    return root


def _job(checkout, job):
    if job.get("provenance", "model_sample") != "model_sample":
        raise AnatomyError("MECHANISM_PROVENANCE")
    return {**job, "provenance": "model_sample", "expected_commit": ANATOMY_COMMIT,
            "checkout": str(Path(checkout).resolve())}


def _accept(sample):
    if sample.get("motion_review") != "not_reviewed" or sample.get("anatomy_commit") != ANATOMY_COMMIT:
        raise AnatomyError("ANATOMY_PIN")
    frames = sample["frames"]
    checked = sample_mechanism(lambda frame: frames[frame]["pose"], lambda pose: {"shares": pose["shares"]},
        clock=sample["clock"], start_frame=0, end_frame=len(frames))
    if [row["readout"] for row in frames] != [row["readout"] for row in checked["frames"]]:
        raise AnatomyError("ANATOMY_READOUT")
    return sample


def _run_process(checkout, job_path, output_json):
    executor = ProcessCommandExecutor(
        executor_id="process.anatomy.sample",
        tool_name="process.anatomy.sample",
        command_args=["node", str(sampler_script()), str(checkout), "{input}", "{output}"],
        expected_suffix=".json",
        timeout_seconds=60,
    )
    executor.execute_shot({"id": "mechanism", "input_artifact": str(job_path),
        "artifact_filename": output_json.name}, output_json.parent)


def _run_docker(checkout, job_path, output_json):
    if not shutil.which("docker"):
        raise AnatomyError("ANATOMY_DOCKER")
    script = sampler_script()
    work = output_json.parent.resolve()
    args = ["docker", "run", "--rm",
        "-v", f"{Path(checkout).resolve()}:/anatomy:ro",
        "-v", f"{script.parent.resolve()}:/sampler:ro",
        "-v", f"{work}:/work",
        "node:20-bookworm-slim",
        "node", "/sampler/anatomy_sample.mjs", "/anatomy", f"/work/{job_path.name}", f"/work/{output_json.name}"]
    result = run_safe_subprocess(args, timeout_seconds=180, output_limit_bytes=65536)
    if result.returncode or result.timed_out:
        raise AnatomyError("ANATOMY_SAMPLER")


def sample_anatomy(checkout, job, output_json, *, runner="process"):
    if job.get("provenance", "model_sample") != "model_sample":
        raise AnatomyError("MECHANISM_PROVENANCE")
    root = verify_anatomy_checkout(checkout)
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    job_path = output_json.parent / "anatomy-job.json"
    job_path.write_text(json.dumps(_job(root, job)), encoding="utf-8")
    # Execute the exact checked bytes, not a mutable checkout after validation.
    with tempfile.TemporaryDirectory(prefix=".anatomy-", dir=output_json.parent) as temp:
        snapshot = Path(temp)
        for name, content in _source_bytes(root).items():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if runner == "docker":
            _run_docker(snapshot, job_path, output_json)
        elif runner == "process":
            _run_process(snapshot, job_path, output_json)
        else:
            raise AnatomyError("ANATOMY_RUNNER")
    sample = _accept(json.loads(output_json.read_text(encoding="utf-8")))
    return sample


def encode_control_mp4(sample, path):
    from PIL import Image, ImageDraw
    from fsf.assembly.fast_assembly import require_ffmpeg
    path = Path(path)
    frames = path.parent / "anatomy-frames"
    frames.mkdir(exist_ok=True)
    for row in sample["frames"]:
        render_mechanism_frame(row).save(frames / f"{row['frame']:06d}.png")
    rate = f"{sample['clock']['num']}/{sample['clock']['den']}"
    result = run_safe_subprocess([require_ffmpeg(), "-nostdin", "-v", "error", "-y", "-framerate", rate,
        "-i", str(frames / "%06d.png"), "-frames:v", str(len(sample["frames"])),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], timeout_seconds=60, output_limit_bytes=65536)
    if result.returncode or result.timed_out or not path.is_file():
        raise AnatomyError("ANATOMY_ENCODE")
    return path


def render_mechanism_frame(row):
    """Orthographic exploded assembly. Pose and gauge use the same state.

    This is an FSF mechanism visualization driven by Anatomy spring samples,
    not a claim to have rendered an upstream scene or audited its geometry.
    """
    import math
    from PIL import Image, ImageDraw
    shares = row["pose"]["shares"]
    if not shares or len(shares) > 8 or row["readout"]["shares"] != shares:
        raise AnatomyError("ANATOMY_READOUT")
    image = Image.new("RGB", (512, 288), (22, 26, 32))
    draw = ImageDraw.Draw(image)
    draw.text((12, 8), "MODEL SAMPLE / ORTHOGRAPHIC ASSEMBLY", fill="white")
    for index, (name, share) in enumerate(shares.items()):
        if type(share) not in (int, float) or not math.isfinite(share) or not -0.25 <= share <= 1.25:
            raise AnatomyError("ANATOMY_READOUT")
        y = 40 + index * (224 // len(shares))
        x = 98 + round(share * 156)
        draw.line((46, y + 12, 290, y + 12), fill=(100, 110, 120), width=3)
        draw.rectangle((46, y, 74, y + 24), fill=(130, 145, 165))
        draw.rectangle((x, y - 4, x + 38, y + 28), fill=(230, 165, 66), outline="white", width=2)
        draw.text((320, y), f"{name}: {share:.4f}", fill="white")
    return image


def import_anatomy_control(checkout, job, output_root, *, control_id, runner="process"):
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    sample_path = root / "sample.json"
    sample = sample_anatomy(checkout, job, sample_path, runner=runner)
    movie = encode_control_mp4(sample, root / "control-source.mp4")
    controls = root / "controls"
    controls.mkdir(exist_ok=True)
    imported = import_control_video(movie, output_root=controls, control_id=control_id)
    return {"sample": sample_path.name, "frames": len(sample["frames"]), "motion_review": "not_reviewed",
            "control": imported, "runner": runner}
