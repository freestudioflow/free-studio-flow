"""Blender-only entrypoint, invoked with a validated request and trusted scene."""
import json
import math
from pathlib import Path
import sys


def main():
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    request = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text("utf-8"))
    out = Path.cwd()
    scene = bpy.context.scene
    if request["source_type"] == "glb":
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.import_scene.gltf(filepath=str(out / request["source_file"]))
    else:
        bpy.ops.wm.open_mainfile(filepath=str(out / request["source_file"]), load_ui=False, use_scripts=False)
    # Reject executable drivers, linked libraries and unsupported simulation.
    for obj in bpy.data.objects:
        if obj.library or obj.constraints or obj.modifiers:
            raise ValueError("Unsupported scene behavior")
        if obj.animation_data and (obj.animation_data.action or obj.animation_data.drivers or obj.animation_data.nla_tracks):
            raise ValueError("Unsupported animation behavior")
        obj.animation_data_clear()
    if bpy.data.libraries:
        raise ValueError("External libraries are unsupported")
    if len(bpy.data.objects) > 64 or sum(len(m.vertices) for m in bpy.data.meshes) > 250000:
        raise ValueError("Scene exceeds the small-scene profile")
    if any(not math.isfinite(v) or abs(v) > 10000 for mesh in bpy.data.meshes for vertex in mesh.vertices for v in vertex.co):
        raise ValueError("Invalid mesh position")
    if any(not math.isfinite(v) or abs(v) > 10000 for obj in bpy.data.objects for v in obj.location):
        raise ValueError("Invalid scene transform")
    scene = bpy.context.scene
    if scene.use_nodes or (scene.sequence_editor and len(scene.sequence_editor.sequences_all)):
        raise ValueError("Compositing and sequencing are unsupported")
    for collection in (bpy.data.materials, bpy.data.cameras, bpy.data.lights, bpy.data.worlds):
        for block in collection:
            if getattr(block, "animation_data", None) or (getattr(block, "node_tree", None) and block.node_tree.animation_data):
                raise ValueError("Animated data blocks are unsupported")
    scene.animation_data_clear()
    # Profiles are explicit; missing resources do not select a different profile.
    if request["profile"] == "reference":
        for image in bpy.data.images:
            if image.source == "FILE" and not image.packed_file:
                path = Path(bpy.path.abspath(image.filepath)).resolve()
                if not path.is_file() or not path.is_relative_to(out.resolve()):
                    raise ValueError("Missing image resource")
    else:
        scene.world = bpy.data.worlds.new("FSF_Clay_World_v1")
        scene.world.color = (0.1, 0.1, 0.1)
        clay = bpy.data.materials.new("FSF_Clay_v1")
        clay.diffuse_color = (0.65, 0.65, 0.65, 1)
        clay.use_nodes = True
        bsdf = clay.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = (0.65, 0.65, 0.65, 1)
        bsdf.inputs["Roughness"].default_value = 0.8
        for obj in scene.objects:
            if obj.type == "MESH":
                obj.data.materials.clear()
                obj.data.materials.append(clay)
    # CPU Cycles and explicit settings avoid version-dependent viewport engines.
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.seed = 0
    scene.cycles.use_animated_seed = False
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.resolution_x = request["width"]
    scene.render.resolution_y = request["height"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.fps = request["clock"]["num"]
    scene.render.fps_base = request["clock"]["den"]
    scene.view_settings.view_transform = "Standard"
    scene.render.film_transparent = False
    for move in request["motions"]:
        obj = bpy.data.objects.get(move["object_id"])
        if obj is None or obj.type not in {"CAMERA", "MESH"} or obj.parent:
            raise ValueError("Unknown or unsupported animation target")
        obj.rotation_mode = "XYZ"
        for key in move["keys"]:
            obj.location = key["location"]
            obj.rotation_euler = key["rotation_euler"]
            for prop in ("location", "rotation_euler"):
                obj.keyframe_insert(data_path=prop, frame=key["frame"])
        for curve in obj.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"
    frames = []
    for view in request["views"]:
        camera = bpy.data.objects.get(view["camera_id"])
        if camera is None or camera.type != "CAMERA" or camera.data.type != "PERSP":
            raise ValueError("Unknown perspective camera")
        scene.camera = camera
        for frame in range(request["start_frame"], request["end_frame"]):
            scene.frame_set(frame)
            name = "frames/" + view["view_id"] + "/" + f"{frame-request['start_frame']:06d}.png"
            scene.render.filepath = str(out / name)
            bpy.ops.render.render(write_still=True)
            projection = {}
            for obj in scene.objects:
                if obj.type == "MESH":
                    v = world_to_camera_view(scene, camera, obj.matrix_world.translation)
                    projection[obj.name] = [float(v.x), float(v.y), float(v.z)]
            frames.append({"view_id": view["view_id"], "scene_frame": frame,
                           "frame": frame-request["start_frame"], "path": name, "projection": projection})
    bpy.ops.export_scene.gltf(filepath=str(out / "model.glb"), export_format="GLB")
    (out / "worker.json").write_text(json.dumps({"blender_version": bpy.app.version_string, "frames": frames}, allow_nan=False), "utf-8")


if __name__ == "__main__":
    main()
