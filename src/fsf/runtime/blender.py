"""Bounded trusted-scene Blender transforms and portable conditioning bundles."""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import time

from jsonschema import Draft202012Validator
from fsf.assembly.fast_assembly import require_ffmpeg
from fsf.conformance.digest import canonical_contract_digest as digest
from fsf.conformance.media import SafeFfprobeRunner, ensure_media_tools_on_path
from fsf.core.asset_library import _root, _path_parts, _no_links, validate_asset_record
from fsf.core.edit_store import edit_lock, read_json, EditStoreError
from fsf.executors.subprocess_safety import run_safe_subprocess


def encoder_version_matches(text: str, expected: str) -> bool:
    """Accept the release tag's optional n prefix; retain exact build pins."""
    return re.search(r'(?m)^ffmpeg version n?'+re.escape(expected)+r'(?:\s|[-+])', text) is not None


def encoder_build_marker(text: str) -> str | None:
    """Return the reported x264 marker, without inventing stripped Git details."""
    found = re.findall(r'core [0-9]+(?: r[0-9]+ [0-9a-f]{7,40})?(?= - H\.264/)', text)
    return found[0] if len(found) == 1 else None


class BlenderError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def shape(value, name):
    schema = json.loads(resources.files("fsf.schemas").joinpath(name + ".schema.json").read_text("utf-8"))
    if not Draft202012Validator(schema).is_valid(value):
        raise BlenderError("BLENDER_SCHEMA")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            h.update(data)
    return h.hexdigest()


def artifact(folder, name, role, media):
    path = folder.joinpath(*_path_parts(name))
    _no_links(path)
    if not path.is_file() or path.stat().st_size < 1:
        raise BlenderError("BLENDER_OUTPUT")
    return {"path": name, "role": role, "media_type": media, "sha256": sha(path), "size_bytes": path.stat().st_size}


def validate_request(request):
    value = copy.deepcopy(request)
    version=value.get('schema_version')
    shape(value, "fsf_blender_request_v2" if version=='fsf_blender_request_v2' else "fsf_blender_request_v1")
    if version=='fsf_blender_request_v2':
        render=value['render']
        if render['device']=='cpu':
            if render['backend']!='none' or render['device_count']!=0 or render['min_vram_mb']!=0:raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
        elif render['backend']!='cuda' or not 1<=render['device_count']<=16 or render['min_vram_mb']<1:
            raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
        animation=value['animation_range']
        if not 0<=animation['start_frame']<=value['start_frame']<value['end_frame']<=animation['end_frame']:
            raise BlenderError('BLENDER_RANGE')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise BlenderError("BLENDER_SCHEMA") from None
    _path_parts(value["source_file"])
    if value["width"] % 2 or value["height"] % 2:
        raise BlenderError("BLENDER_DIMENSIONS")
    if value["end_frame"] <= value["start_frame"] or value["end_frame"]-value["start_frame"] > 96:
        raise BlenderError("BLENDER_RANGE")
    if Fraction(value["clock"]["num"], value["clock"]["den"]) not in {Fraction(24), Fraction(24000, 1001)}:
        raise BlenderError("BLENDER_CLOCK")
    if len({v["view_id"] for v in value["views"]}) != len(value["views"]):
        raise BlenderError("BLENDER_VIEW")
    if value["profile"] == "reference" and (value["motions"] or value["end_frame"]-value["start_frame"] != 1):
        raise BlenderError("BLENDER_PROFILE")
    seen = set()
    for move in value["motions"]:
        if move["object_id"] in seen:
            raise BlenderError("BLENDER_MOTION")
        seen.add(move["object_id"])
        frames = [k["frame"] for k in move["keys"]]
        bounds=value.get('animation_range',value)
        if frames != sorted(set(frames)) or frames[0] != bounds["start_frame"] or frames[-1] != bounds["end_frame"]-1:
            raise BlenderError("BLENDER_MOTION")
    return value


def probe_blender(executable):
    try:
        result = run_safe_subprocess([str(executable), "--version"], timeout_seconds=10, inherit_env=False,
                                    env=_environment())
        match = re.search(r"Blender (\d+)\.(\d+)\.(\d+)", result.stdout)
        if result.returncode or result.timed_out or not match:
            raise BlenderError("BLENDER_UNAVAILABLE")
        version = tuple(map(int, match.groups()))
        if version[:2] not in {(3, 2), (4, 3)}:
            raise BlenderError("BLENDER_VERSION")
        binary = shutil.which(str(executable)) or str(executable)
        return {"version": ".".join(map(str, version)), "engine": "CYCLES", "device": "CPU",
                "executable_sha256": sha(Path(binary).resolve(strict=True)), "runtime_sha256": sha(Path(__file__))}
    except OSError:
        raise BlenderError("BLENDER_UNAVAILABLE") from None


def _environment():
    # Minimal OS runtime, never model/API credentials or operator Python paths.
    from fsf.remote.gpu import gpu_runtime_environment
    return {**{k: os.environ[k] for k in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if k in os.environ}, **gpu_runtime_environment()}


def validate_glb(path):
    data = Path(path).read_bytes()
    if len(data) < 20 or len(data) > 64 * 1024 * 1024:
        raise BlenderError("BLENDER_GLB")
    magic, version, length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise BlenderError("BLENDER_GLB")
    offset, chunks = 12, []
    while offset < len(data):
        if offset+8 > len(data):
            raise BlenderError("BLENDER_GLB")
        count, kind = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if count % 4 or offset+count > len(data):
            raise BlenderError("BLENDER_GLB")
        chunks.append((kind, data[offset:offset+count]))
        offset += count
    if not chunks or chunks[0][0] != b"JSON":
        raise BlenderError("BLENDER_GLB")
    from fsf.core.edit_store import decode_json
    doc = decode_json(chunks[0][1])
    if not doc.get("meshes") or not doc.get("scenes"):
        raise BlenderError("BLENDER_GLB")
    if any("uri" in item for item in doc.get("buffers", []) + doc.get("images", [])):
        raise BlenderError("BLENDER_GLB_EXTERNAL")
    binary = [data for kind, data in chunks if kind == b"BIN\x00"]
    buffers = doc.get("buffers", [])
    if len(binary) != 1 or len(buffers) != 1 or not 0 <= len(binary[0])-buffers[0].get("byteLength", -100) <= 3:
        raise BlenderError("BLENDER_GLB")
    for view in doc.get("bufferViews", []):
        start, count = view.get("byteOffset", 0), view.get("byteLength", -1)
        if view.get("buffer") != 0 or start < 0 or count < 1 or start+count > buffers[0]["byteLength"]:
            raise BlenderError("BLENDER_GLB")
    positions = []
    for mesh in doc["meshes"]:
        for primitive in mesh.get("primitives", []):
            index = primitive.get("attributes", {}).get("POSITION")
            if type(index) is not int or not 0 <= index < len(doc.get("accessors", [])):
                raise BlenderError("BLENDER_GLB")
            positions.append(doc["accessors"][index])
    if not positions or sum(a.get("count", 0) for a in positions) > 250000:
        raise BlenderError("BLENDER_GLB")
    for accessor in positions:
        bounds = accessor.get("min", []) + accessor.get("max", [])
        if accessor.get("count", 0) < 1 or accessor.get("type") != "VEC3" or len(bounds) != 6 or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 10000 for v in bounds):
            raise BlenderError("BLENDER_GLB")
        view_index = accessor.get("bufferView")
        if type(view_index) is not int or not 0 <= view_index < len(doc.get("bufferViews", [])) or accessor.get("componentType") != 5126 or "sparse" in accessor:
            raise BlenderError("BLENDER_GLB")
        view = doc["bufferViews"][view_index]
        stride = view.get("byteStride", 12)
        local = accessor.get("byteOffset", 0)
        if type(stride) is not int or stride < 12 or stride % 4 or type(local) is not int or local < 0 or local+(accessor["count"]-1)*stride+12 > view["byteLength"]:
            raise BlenderError("BLENDER_GLB")
        for index in range(accessor["count"]):
            xyz = struct.unpack_from("<3f", binary[0], view.get("byteOffset", 0)+local+index*stride)
            if any(not math.isfinite(v) or abs(v) > 10000 for v in xyz):
                raise BlenderError("BLENDER_GLB")
    return doc


def compile_blender_plan(request):
    value = validate_request(request)
    control = value["profile"] == "clay_v1"
    outputs = ["artifact:model.glb", "artifact:manifest.json", "artifact:receipt.json"]
    outputs += [f"artifact:control_{view['view_id']}.mp4" if control else f"artifact:frames/{view['view_id']}/000000.png" for view in value["views"]]
    plan = {"schema_version": "pdx_execution_plan_v1", "request_id": "blender_" + digest(value)[:16],
            "producer": {"type": "fsf_planner", "name": "fsf.runtime.blender"},
            "steps": [{"id": "blender_transform", "name": "Render controlled Blender assets", "kind": "transform",
            "transform": "fsf.blender.render_control" if control else "fsf.blender.export_reference",
            "depends_on": [], "inputs": {"request": value}, "outputs": outputs}]}
    from pdx_artifact_core import validate_execution_plan
    if validate_execution_plan(plan):
        raise BlenderError("BLENDER_PLAN")
    return plan


def verify_bundle(folder):
    folder = _root(folder)
    value = read_json(folder / "manifest.json")
    if value.get("schema_version") == "fsf_control_import_v1":
        from fsf.runtime.control_import import verify_import
        return verify_import(folder)
    if value.get('schema_version')=='fsf_segment_delivery_v1':
        from fsf.remote.segment_delivery import verify_delivery
        return verify_delivery(folder)
    if value.get("schema_version") not in {"fsf_blender_manifest_v1","fsf_blender_manifest_v2"}:
        raise BlenderError("BLENDER_MANIFEST")
    receipt = read_json(folder / "receipt.json")
    version='v2' if value['schema_version']=='fsf_blender_manifest_v2' else 'v1'
    shape(value, 'fsf_blender_manifest_'+version)
    shape(receipt, 'fsf_blender_receipt_'+version)
    if version=='v2':
        evidence=value['execution_evidence'];render=value['render_profile']
        expected='GPU' if render['device']=='gpu' else 'CPU'
        if evidence['device']!=expected or value['tools']['device']!=expected or evidence['backend']!=render['backend']:
            raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
        if expected=='GPU':
            selected=evidence['selection']
            items=selected if isinstance(selected,list) else [selected]
            if (len(evidence['devices'])!=render['device_count'] or len(items)!=render['device_count']
                    or any(not item or item.get('memory_mb',0)<render['min_vram_mb'] for item in items)):
                raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
        if expected=='CPU' and (evidence['devices'] or evidence['selection'] is not None):raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
    payload = {k: v for k, v in value.items() if k != "manifest_digest"}
    if digest(payload) != value.get("manifest_digest") or receipt.get("manifest_digest") != value["manifest_digest"]:
        raise BlenderError("BLENDER_INTEGRITY")
    if receipt.get("identity") != value["identity"]:
        raise BlenderError("BLENDER_INTEGRITY")
    if digest({k: v for k, v in receipt.items() if k != "receipt_digest"}) != receipt.get("receipt_digest"):
        raise BlenderError("BLENDER_INTEGRITY")
    for item in value["artifacts"]:
        actual = artifact(folder, item["path"], item["role"], item["media_type"])
        if actual != item:
            raise BlenderError("BLENDER_INTEGRITY")
    paths = [a["path"] for a in value["artifacts"]]
    if len(paths) != len(set(paths)) or digest(value["frame_mapping"]) != value["frame_mapping_digest"]:
        raise BlenderError("BLENDER_INTEGRITY")
    return value


def _cuda_visible(selection):
    if not selection:return {}
    items=selection if isinstance(selection,list) else [selection]
    return {'CUDA_VISIBLE_DEVICES':','.join(item['uuid'] for item in items)}


def execute_blender_plan(plan, *, source_root, output_root, executable, trusted_scene=False, no_resume=False, media_tools=None):
    """Trusted inputs only. CLI flags are not a sandbox for hostile .blend files."""
    if trusted_scene is not True:
        raise BlenderError("BLENDER_TRUST_REQUIRED")
    try:
        request = plan["steps"][0]["inputs"]["request"]
        if plan != compile_blender_plan(request):
            raise BlenderError("BLENDER_PLAN")
        value = validate_request(request)
        source = _root(source_root)
        output = _root(output_root)
        record = validate_asset_record(read_json(source / "record.json"), resource_root=source)
        if sum(a["size_bytes"] for a in record["artifacts"]) > 64*1024*1024:
            raise BlenderError("BLENDER_SOURCE_LIMIT")
        if record["record_digest"] != value["source_record_digest"]:
            raise BlenderError("BLENDER_SOURCE")
        matches = [a for a in record["artifacts"] if a["logical_path"] == value["source_file"] and a["role"] in {"scene", "model"}]
        if len(matches) != 1 or matches[0]["media_type"] != {"blend": "application/x-blender", "glb": "model/gltf-binary"}[value["source_type"]]:
            raise BlenderError("BLENDER_SOURCE")
        tools = probe_blender(executable)
        v2=value['schema_version']=='fsf_blender_request_v2'
        selection=None
        if v2 and value['render']['device']=='gpu':
            from fsf.remote.gpu import cuda_inventory,select_devices
            from fsf.remote.contracts import RemoteError
            count=value['render']['device_count']
            try:
                chosen=select_devices(cuda_inventory(min_vram_mb=value['render']['min_vram_mb']),authorized=count,requested=count)
            except RemoteError as exc:
                raise BlenderError(exc.code) from None
            selection=chosen[0] if count==1 else chosen
            tools['device']='GPU'
        worker = resources.files("fsf.tools").joinpath("blender_worker.py")
        worker_bytes = worker.read_bytes()
        identity = digest({"plan": plan, "record_digest": record["record_digest"], "tools": tools,
                           "worker_sha256": hashlib.sha256(worker_bytes).hexdigest()})
        with edit_lock(output / "blender.entry"):
            destination = output / identity
            if destination.exists() and not no_resume:
                manifest = verify_bundle(destination)
                if manifest["identity"] != identity:
                    raise BlenderError("BLENDER_INTEGRITY")
                return {"status": "reused", "bundle": destination.name, "manifest_digest": manifest["manifest_digest"]}
            if no_resume:
                destination = output / (identity + "_" + __import__("uuid").uuid4().hex)
            scratch = Path(tempfile.mkdtemp(prefix=".blender-", dir=output))
            deadline = time.monotonic() + 120
            def run(args):
                left = deadline-time.monotonic()
                if left <= 0:
                    raise BlenderError("BLENDER_DEADLINE")
                result = run_safe_subprocess(args, cwd=scratch, timeout_seconds=left, inherit_env=False,
                    output_limit_bytes=1024*1024,
                    env={**_environment(), **_cuda_visible(selection), "HOME": str(scratch), "USERPROFILE": str(scratch),
                         "BLENDER_USER_CONFIG": str(scratch / "config"), "OMP_NUM_THREADS": "2"})
                if result.timed_out:
                    raise BlenderError("BLENDER_DEADLINE")
                if result.returncode:
                    # Return only bounded categories, never raw tool stderr.
                    categories={
                        'out of memory':'BLENDER_OUT_OF_MEMORY', 'cuda':'BLENDER_CUDA_FAILED',
                        'no such file':'BLENDER_FILE_MISSING', 'unrecognized option':'BLENDER_TOOL_OPTION',
                    }
                    text=(getattr(result,'stderr','')+getattr(result,'stdout','')).lower()
                    for marker,code in categories.items():
                        if marker in text:raise BlenderError(code)
                    raise BlenderError("BLENDER_TOOL_FAILED")
                return result
            try:
                for item in record["artifacts"]:
                    target = scratch / "inputs" / item["logical_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / item["locator"], target)
                    if sha(target) != item["sha256"]:
                        raise BlenderError("BLENDER_SOURCE")
                if value["source_type"] == "glb":
                    validate_glb(scratch / "inputs" / value["source_file"])
                worker_request = {**value, "source_file": "inputs/" + value["source_file"]}
                if selection:worker_request['_gpu_selection']=selection
                (scratch / "request.json").write_text(json.dumps(worker_request), "utf-8")
                (scratch / "worker.py").write_bytes(worker_bytes)
                run([str(executable), "--background", "--factory-startup", "--disable-autoexec",
                     "--python-exit-code", "17", "--python", "worker.py", "--", "request.json"])
                validate_asset_record(read_json(source / "record.json"), resource_root=source)
                validate_glb(scratch / "model.glb")
                from PIL import Image, ImageChops, ImageStat
                worker_result = read_json(scratch / "worker.json")
                if v2:
                    evidence=worker_result.get('execution_evidence',{})
                    if evidence.get('device')!=('GPU' if value['render']['device']=='gpu' else 'CPU') or evidence.get('selection')!=selection:
                        raise BlenderError('REMOTE_GPU_DEVICE_MISMATCH')
                    tools['device']=evidence['device']
                count = value["end_frame"]-value["start_frame"]
                frames = worker_result["frames"]
                if len(frames) != count*len(value["views"]):
                    raise BlenderError("BLENDER_FRAMES")
                artifacts = [artifact(scratch, "model.glb", "model", "model/gltf-binary")]
                for view in value["views"]:
                    selected = [f for f in frames if f["view_id"] == view["view_id"]]
                    if [f["frame"] for f in selected] != list(range(count)):
                        raise BlenderError("BLENDER_FRAMES")
                    for frame in selected:
                        expected = f"frames/{view['view_id']}/{frame['frame']:06d}.png"
                        if frame["path"] != expected:
                            raise BlenderError("BLENDER_FRAMES")
                        with Image.open(scratch / expected) as image:
                            image.load()
                            if image.size != (value["width"], value["height"]) or image.format != "PNG":
                                raise BlenderError("BLENDER_IMAGE")
                        artifacts.append(artifact(scratch, expected, "control_frame" if value["profile"] == "clay_v1" else "reference_image", "image/png"))
                    if value["profile"] == "clay_v1":
                        if v2:
                            ffmpeg,ffprobe=media_tools if media_tools is not None else (shutil.which('ffmpeg'),shutil.which('ffprobe'))
                        else:
                            ffmpeg, ffprobe = ensure_media_tools_on_path()
                        if not ffmpeg or not ffprobe:
                            raise BlenderError("BLENDER_MEDIA_TOOLS")
                        name = f"control_{view['view_id']}.mp4"
                        rate = f"{value['clock']['num']}/{value['clock']['den']}"
                        encoder_options=[]
                        if v2:
                            encoded_version=run([ffmpeg,'-version']).stdout
                            pin=value['encode']
                            if not encoder_version_matches(encoded_version,pin['ffmpeg_version']) or sha(ffmpeg)!=pin['ffmpeg_sha256'] or digest(encoded_version)!=pin['ffmpeg_build_digest']:
                                raise BlenderError('BLENDER_ENCODER_VERSION')
                            encoder_options=['-preset','medium','-crf','16','-threads','2','-bf','0','-g','24','-fps_mode','cfr','-video_track_timescale','24000']
                        encoded=run([ffmpeg, "-nostdin", "-y", "-framerate", rate, "-i",
                             f"frames/{view['view_id']}/%06d.png", "-frames:v", str(count),
                             "-an", "-c:v", "libx264", *encoder_options, "-pix_fmt", "yuv420p", name])
                        if v2 and encoder_build_marker(encoded.stderr)!=value['encode']['libx264_build']:raise BlenderError('BLENDER_ENCODER_VERSION')
                        raw = SafeFfprobeRunner(ffprobe).probe(scratch / name)
                        stream = next(s for s in raw["streams"] if s["codec_type"] == "video")
                        if int(stream.get("nb_frames", 0)) != count or Fraction(stream["avg_frame_rate"]) != Fraction(rate):
                            raise BlenderError("BLENDER_VIDEO")
                        decoded = scratch / "decoded" / view["view_id"]
                        decoded.mkdir(parents=True)
                        run([ffmpeg, "-nostdin", "-y", "-i", name, "-vsync", "0", str(decoded / "%06d.png")])
                        images = sorted(decoded.glob("*.png"))
                        if len(images) != count:
                            raise BlenderError("BLENDER_VIDEO")
                        if v2:
                            score=run([ffmpeg,'-nostdin','-i',name,'-framerate',rate,'-i',f"frames/{view['view_id']}/%06d.png",
                                '-filter_complex','[0:v]format=yuv420p,setpts=PTS-STARTPTS[a];[1:v]format=yuv420p,setpts=PTS-STARTPTS[b];[a][b]ssim',
                                '-an','-f','null','-'])
                            matches=re.findall(r'All:([0-9.]+)',score.stderr)
                            if not matches or float(matches[-1])<0.98:raise BlenderError('BLENDER_SSIM')
                        for frame, image_path in zip(selected, images):
                            with Image.open(scratch/frame["path"]) as original, Image.open(image_path) as encoded:
                                if encoded.size != original.size:
                                    raise BlenderError("BLENDER_VIDEO")
                                difference = ImageChops.difference(original.convert("RGB"), encoded.convert("RGB"))
                                if max(ImageStat.Stat(difference).rms) > 20:
                                    raise BlenderError("BLENDER_VIDEO")
                        artifacts.append(artifact(scratch, name, "control_video", "video/mp4"))
                manifest = {"schema_version": "fsf_blender_manifest_v1", "identity": identity,
                    "control_id": value["control_id"], "revision": value["revision"], "request_digest": digest(value),
                    "source_record_digest": record["record_digest"], "clock": value["clock"], "frame_count": count,
                    "width": value["width"], "height": value["height"], "profile": value["profile"],
                    "tools": tools, "render_profile": {"version": "fsf_cpu_cycles_v1", "samples": 4,
                        "seed": 0, "denoising": False, "threads": 2},
                    "frame_mapping": frames, "frame_mapping_digest": digest(frames), "artifacts": artifacts,
                    "motion_review": "not_reviewed", "generation_dispatches": 0}
                manifest["manifest_digest"] = digest(manifest)
                if v2:
                    manifest.update(schema_version='fsf_blender_manifest_v2',render_profile=value['render'],
                        execution_evidence=evidence,encode_profile=value['encode'])
                    manifest.pop('manifest_digest');manifest['manifest_digest']=digest(manifest)
                receipt = {"schema_version": "fsf_blender_receipt_v1", "identity": identity,
                           "manifest_digest": manifest["manifest_digest"], "execution": "completed",
                           "motion_review": "not_reviewed"}
                if v2:receipt['schema_version']='fsf_blender_receipt_v2'
                receipt["receipt_digest"] = digest(receipt)
                for name, obj in [("manifest.json", manifest), ("receipt.json", receipt)]:
                    with (scratch / name).open("w", encoding="utf-8") as stream:
                        json.dump(obj, stream, allow_nan=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                # Only declared results, never local inputs/config/scripts, become public bundle files.
                keep = {a["path"] for a in artifacts} | {"manifest.json", "receipt.json"}
                for item in list(scratch.rglob("*")):
                    if item.is_file() and item.relative_to(scratch).as_posix() not in keep:
                        item.unlink()
                verify_bundle(scratch)
                os.rename(scratch, destination)
                return {"status": "completed", "bundle": destination.name, "manifest_digest": manifest["manifest_digest"]}
            finally:
                if scratch.exists():
                    shutil.rmtree(scratch)
    except BlenderError:
        raise
    except KeyboardInterrupt:
        raise BlenderError("BLENDER_CANCELLED") from None
    except EditStoreError as exc:
        raise BlenderError("BLENDER_LOCK_TIMEOUT" if exc.code == "EDIT_LOCK_TIMEOUT" else "BLENDER_INVALID") from None
    except RuntimeError as exc:
        raise BlenderError("BLENDER_OUTPUT_LIMIT" if str(exc) == "PROCESS_OUTPUT_LIMIT" else "BLENDER_INVALID") from None
    except Exception:
        raise BlenderError("BLENDER_INVALID") from None


def condition_request(folder, view_id, *, required=True):
    """Portable video condition, not a static reference or a story video output."""
    value = verify_bundle(folder)
    if value["schema_version"] in {"fsf_control_import_v1","fsf_segment_delivery_v1"}:
        if view_id != ('assembled' if value['schema_version']=='fsf_segment_delivery_v1' else 'imported'):
            raise BlenderError("CONDITION_SOURCE")
        matches = [value["artifact"]]
    else:
        matches = [a for a in value["artifacts"] if a["path"] == f"control_{view_id}.mp4" and a["role"] == "control_video"]
    if len(matches) != 1:
        raise BlenderError("CONDITION_SOURCE")
    result = {"schema_version": "fsf_video_condition_v1", "control_id": value["control_id"],
        "revision": value["revision"], "manifest_digest": value['delivery_digest'] if value['schema_version']=='fsf_segment_delivery_v1' else value["manifest_digest"], "artifact": matches[0],
        "clock": value["clock"], "frame_count": value["frame_count"], "width": value["width"], "height": value["height"],
        "frame_mapping_digest": value["frame_mapping_digest"], "required": required, "preprocess": "none"}
    result["condition_digest"] = digest(result)
    shape(result, "fsf_video_condition_v1")
    return result


def validate_condition_capability(request, capability):
    shape(request, "fsf_video_condition_v1")
    if digest({k: v for k, v in request.items() if k != "condition_digest"}) != request["condition_digest"]:
        raise BlenderError("CONDITION_INTEGRITY")
    if not isinstance(capability, dict) or set(capability) != {"video_conditioning", "max_frames", "max_width", "max_height", "clocks"}:
        raise BlenderError("CONDITION_UNSUPPORTED")
    if not isinstance(capability["clocks"], list):
        raise BlenderError("CONDITION_UNSUPPORTED")
    if any(type(capability[k]) is not int or capability[k] < 1 for k in ("max_frames", "max_width", "max_height")):
        raise BlenderError("CONDITION_UNSUPPORTED")
    if (capability.get("video_conditioning") is not True or request["clock"] not in capability["clocks"]
            or request["frame_count"] > capability.get("max_frames", 0)
            or request["width"] > capability.get("max_width", 0) or request["height"] > capability.get("max_height", 0)):
        raise BlenderError("CONDITION_UNSUPPORTED")


def validate_condition_ack(request, capability, acknowledgement):
    validate_condition_capability(request, capability)
    expected = {"condition_digest": request["condition_digest"], "artifact_sha256": request["artifact"]["sha256"],
                "frame_mapping_digest": request["frame_mapping_digest"], "preprocess": "none", "capability_gap": None}
    if acknowledgement != expected:
        raise BlenderError("CONDITION_ACK")
    return {"acknowledged": True, "motion_review": "not_reviewed"}


def bundle_asset_record(folder, *, asset_id, version, source_prompt):
    """Create a v2 record; dynamic controls never become static references."""
    from fsf.core.asset_library import canonical_library_record_digest
    from fsf.core.consistency import canonical_asset_digest
    manifest = verify_bundle(folder)
    imported = manifest["schema_version"] in {"fsf_control_import_v1","fsf_segment_delivery_v1"}
    record = {"schema_version": "fsf_library_asset_record_v2", "digest_version": "fsf_library_asset_record_digest_v2",
        "asset_id": asset_id, "asset_version": version, "source_prompt": source_prompt, "kind": "prop",
        "constraints": {"preserve": [], "forbid": [], "unresolved": []}, "ref_images": [],
        "artifacts": [], "primary_artifact_id": "control" if imported else "model"}
    for index, a in enumerate([manifest["artifact"]] if imported else manifest["artifacts"]):
        record["artifacts"].append({"artifact_id": "control" if imported else "model" if a["role"] == "model" else f"item_{index:04}",
            "role": a["role"], "logical_path": a["path"], "locator": a["path"], "media_type": a["media_type"],
            "sha256": a["sha256"], "size_bytes": a["size_bytes"]})
        if a["role"] == "reference_image":
            record["ref_images"].append({"role": "prop", "sha256": a["sha256"]})
    # Provenance remains a separately bound artifact, not an unknown schema field.
    for name in (("manifest.json",) if manifest['schema_version']=='fsf_control_import_v1' else ("manifest.json", "receipt.json")):
        a = artifact(Path(folder), name, "other", "application/json")
        record["artifacts"].append({"artifact_id": name.split(".")[0], "role": "other", "logical_path": name,
            "locator": name, "media_type": "application/json", "sha256": a["sha256"], "size_bytes": a["size_bytes"]})
    record["content_hash"] = canonical_asset_digest(asset_id, version, source_prompt, "prop", record["constraints"], record["ref_images"])
    record["record_digest"] = canonical_library_record_digest(record)
    return validate_asset_record(record, resource_root=folder)
