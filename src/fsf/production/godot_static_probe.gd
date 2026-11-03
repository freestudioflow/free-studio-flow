extends SceneTree

func vec(value):
	return Vector3(value[0], value[1], value[2])

func arr(value):
	return [value.x, value.y, value.z]

func _initialize():
	call_deferred("probe")

func probe():
	var request = JSON.parse_string(FileAccess.get_file_as_string("res://probe.json"))
	var document = GLTFDocument.new()
	var state = GLTFState.new()
	state.create_animations = false
	if document.append_from_file("res://scene.glb", state) != OK:
		quit(2)
		return
	for index in range(state.get_nodes().size()):
		state.get_nodes()[index].set_meta("fsf_source_node_index", index)
	var scene = document.generate_scene(state)
	if scene == null:
		quit(2)
		return
	var matrix = request.spatial.root_transform_column_major
	scene.transform = Transform3D(Basis(Vector3(matrix[0],matrix[1],matrix[2]), Vector3(matrix[4],matrix[5],matrix[6]),
		Vector3(matrix[8],matrix[9],matrix[10])), Vector3(matrix[12],matrix[13],matrix[14])) * scene.transform
	root.add_child(scene)
	var mapped = {}
	var pending = [scene]
	while not pending.is_empty():
		var current = pending.pop_back()
		if current.has_meta("fsf_source_node_index"):
			var index = current.get_meta("fsf_source_node_index")
			if mapped.has(index):
				quit(6)
				return
			mapped[index] = current
		pending.append_array(current.get_children())
	var objects = []
	var materials = []
	var images = []
	var surfaces = 0
	var triangles = 0
	for index in range(state.get_nodes().size()):
		var node = mapped.get(index)
		if not node is MeshInstance3D:
			continue
		var bounds = node.global_transform * node.get_aabb()
		objects.append({"scene_index": state.json.get("scene", 0), "node_index": index,
			"bounds_m": {"minimum": arr(bounds.position), "maximum": arr(bounds.end)},
			"pivot_m": arr(node.global_position)})
		for surface in range(node.mesh.get_surface_count()):
			surfaces += 1
			var arrays = node.mesh.surface_get_arrays(surface)
			var indices = arrays[Mesh.ARRAY_INDEX]
			triangles += indices.size() / 3 if indices != null and indices.size() > 0 else arrays[Mesh.ARRAY_VERTEX].size() / 3
	for material in state.get_materials():
		if not material is StandardMaterial3D:
			quit(3)
			return
		materials.append({"metallic": material.metallic, "roughness": material.roughness,
			"albedo": [material.albedo_color.r, material.albedo_color.g, material.albedo_color.b, material.albedo_color.a],
			"has_albedo_texture": material.albedo_texture != null,
			"albedo_image_index": state.get_images().find(material.albedo_texture) if material.albedo_texture != null else null,
			"has_normal_texture": material.normal_texture != null,
			"normal_image_index": state.get_images().find(material.normal_texture) if material.normal_texture != null else null,
			"alpha_mode": "OPAQUE" if material.transparency == BaseMaterial3D.TRANSPARENCY_DISABLED else "MASK" if material.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR else "unsupported",
			"alpha_cutoff": material.alpha_scissor_threshold})
	for texture in state.get_images():
		var image = texture.get_image()
		if image == null or image.is_empty():
			quit(4)
			return
		var hashing = HashingContext.new()
		hashing.start(HashingContext.HASH_SHA256)
		hashing.update(image.get_data())
		images.append({"width": image.get_width(), "height": image.get_height(),
			"decoded_sha256": hashing.finish().hex_encode()})
	for selected in request.spatial.objects:
		if selected.collision_mode == "none":
			continue
		var node = mapped.get(int(selected.node_index))
		if not node is MeshInstance3D:
			print("collision node type: ", node.get_class() if node != null else "null")
			quit(5)
			return
		var shape = node.mesh.create_trimesh_shape() if selected.collision_mode == "static_trimesh" else node.mesh.create_convex_shape()
		if shape == null:
			print("collision shape unavailable")
			quit(5)
			return
		shape.margin = 0.0
		var body = StaticBody3D.new()
		body.set_meta("object_id", selected.object_id)
		root.add_child(body)
		body.global_transform = node.global_transform
		var collision = CollisionShape3D.new()
		collision.shape = shape
		body.add_child(collision)
	await physics_frame
	await physics_frame
	var rays = []
	for ray in request.rays:
		var origin = vec(ray.from_m)
		var query = PhysicsRayQueryParameters3D.create(origin, vec(ray.to_m))
		var hit = scene.get_world_3d().direct_space_state.intersect_ray(query)
		rays.append({"ray_id": ray.ray_id, "object_id": null if hit.is_empty() else hit.collider.get_meta("object_id", ""),
			"distance_m": null if hit.is_empty() else origin.distance_to(hit.position)})
	var version = Engine.get_version_info()
	var version_string = "%s.%s.%s.%s.%s.%s" % [version.major, version.minor, version.patch, version.status, version.build, version.hash.left(9)]
	var report = {"engine_version": version_string, "objects": objects,
		"materials": materials, "images": images, "surfaces": surfaces, "triangles": triangles, "rays": rays}
	var output = FileAccess.open("res://engine-report.json", FileAccess.WRITE)
	output.store_string(JSON.stringify(report))
	output.close()
	quit(0)
