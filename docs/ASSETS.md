# Library Asset Validation

*Introduced in rc7. For immutable CLI registration (`fsf asset register` / `fsf asset get`), see [Blender Assets and Motion Controls](BLENDER.md).*

The Python API `fsf.core.asset_library.validate_asset_record(record,
resource_root=...)` checks a
[`fsf_library_asset_record_v1`](../src/fsf/schemas/fsf_library_asset_record_v1.schema.json)
against local files and returns an isolated record copy. This validation function
does not register assets or create an index. Use `fsf asset register` and
`fsf asset get` for immutable registration and fixed-version lookup, described
in [Blender Assets and Motion Controls](BLENDER.md). Both v1 records and v2 control-asset records are
accepted without changing their respective role definitions.

Validation checks the primary artifact reference, supported kind and role/MIME
pairs, reference-image bindings, portable relative paths, file sizes and SHA-256,
and both record digests. Every declared file must exist under the explicit
resource root. Symlinks, reparse points and non-regular files are rejected.

`ref_images[].role` describes the semantic category (`character`, `scene`,
`prop`, or `style`); `artifacts[].role` describes file usage. A reference-image
SHA must resolve to an artifact with usage `reference_image`. An image-only
asset may use that artifact as its primary. The content hash binds the asset
specification; the record digest also binds artifact declarations and order,
excluding only itself and location hints (`artifacts[].locator`). Moving a
locator does not change that digest, but the new location is still checked.

Successful validation proves the declared integrity checks at the time of
reading. It does not decode media, establish complete model dependencies,
certify rendering/Gaming/VR suitability, or protect against every concurrent
filesystem replacement. Consumers must re-check files when using them.
