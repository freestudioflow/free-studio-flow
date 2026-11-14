"""Operator-reviewed reference identities and non-executable production drafts."""
import re

from fsf.core.edit_store import edit_lock, read_json
from fsf.production.contracts import ProductionError, digest, key, publish, seal


def require(condition, code="REFERENCE_DRAFT_INVALID"):
    if not condition:
        raise ProductionError(code)


def fields(value, names):
    require(isinstance(value, dict) and value.keys() == set(names.split()))


def sha(value):
    require(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value))
    return value


def analysis_binding(analysis):
    require(isinstance(analysis, dict) and analysis.get("schema_version") == "reference_analysis_v1"
            and seal(analysis, "analysis_digest") == analysis, "REFERENCE_ANALYSIS_INVALID")
    shots = {}
    end = 0
    for index, shot in enumerate(analysis["shots"]):
        start, stop = shot["range"]
        require(type(start) is int and type(stop) is int and start == end and stop > start)
        shots[f"shot-{index + 1}"] = (start, stop)
        end = stop
    require(shots and end == len(analysis["timeline"]["pts"]))
    return {"analysis_digest": analysis["analysis_digest"],
            "source_sha256": sha(analysis["source"]["sha256"])}, shots


def organize(binding, shots, entities, previous=None):
    require(isinstance(entities, list))
    rows = {}
    observations = set()
    for entity in entities:
        fields(entity, "id kind appearances transfers")
        name = key(entity["id"])
        kind = entity["kind"]
        require(name not in rows and kind in ("character", "prop", "scene"))
        spans = entity["appearances"]
        require(isinstance(spans, list) and spans)
        for span in spans:
            fields(span, "id shot range")
            observation = key(span["id"])
            start, end = span["range"]
            lo, hi = shots[span["shot"]]
            require(observation not in observations and type(start) is int
                    and type(end) is int and lo <= start < end <= hi)
            observations.add(observation)
        require(isinstance(entity["transfers"], list) and (kind == "prop" or not entity["transfers"]))
        rows[name] = entity
    for entity in rows.values():
        holder = None
        last = -1
        for change in entity["transfers"]:
            fields(change, "frame from to")
            frame, before, after = change["frame"], change["from"], change["to"]
            require(type(frame) is int and last < frame < max(end for _, end in shots.values())
                    and before == holder and before != after)
            require(after is None or after in rows and rows[after]["kind"] == "character")
            holder, last = after, frame
    fingerprints = {name: digest(row) for name, row in rows.items()}
    older = previous["entity_digests"] if previous else {}
    changed = sorted(name for name in older.keys() | fingerprints.keys()
                     if older.get(name) != fingerprints.get(name))
    return seal({"schema_version": "reference_entities_v1", **binding,
                 "revision": previous["revision"] + 1 if previous else 1,
                 "previous_digest": previous["entity_revision_digest"] if previous else None,
                 "entities": entities, "entity_digests": fingerprints,
                 "changed_entity_ids": changed}, "entity_revision_digest")


def draft(binding, shots, registry, spec, previous=None):
    fields(spec, "shots preferences")
    require(isinstance(spec["preferences"], dict)
            and all(isinstance(value, str) for value in spec["preferences"].values()))
    require(isinstance(spec["shots"], list) and spec["shots"])
    rows = {}
    holders = {entity["id"]: {change["to"] for change in entity["transfers"]
                              if change["to"] is not None}
               for entity in registry["entities"]}
    for row in spec["shots"]:
        fields(row, "id reference_shot entities depends_on route assets rights")
        name = key(row["id"])
        require(name not in rows and row["reference_shot"] in shots)
        require(isinstance(row["depends_on"], list) and all(dep in rows for dep in row["depends_on"]))
        require(isinstance(row["entities"], list) and
                all(entity in registry["entity_digests"] for entity in row["entities"]))
        route = row["route"]
        fields(route, "kind profile_digest required_channels provided_channels ceiling_units worst_units")
        require(route["kind"] in ("reuse", "recipe", "adapter", "api", "edit"))
        if route["profile_digest"] is not None:
            sha(route["profile_digest"])
        for field in ("required_channels", "provided_channels"):
            require(isinstance(route[field], list))
            for channel in route[field]:
                key(channel)
        for field in ("ceiling_units", "worst_units"):
            require(type(route[field]) is int and route[field] >= 0)
        require(isinstance(row["assets"], list) and row["assets"])
        blockers = []
        for asset in row["assets"]:
            if asset is None:
                blockers.append("missing_asset")
            else:
                sha(asset)
        fields(row["rights"], "reference original_audio fonts delivery")
        require(all(type(value) is bool for value in row["rights"].values()))
        if not all(row["rights"].values()):
            blockers.append("rights_unconfirmed")
        if route["profile_digest"] is None:
            blockers.append("missing_profile")
        if set(route["required_channels"]) - set(route["provided_channels"]):
            blockers.append("missing_required_channel")
        if route["worst_units"] > route["ceiling_units"]:
            blockers.append("budget_exceeded")
        parents = {dep: rows[dep]["input_digest"] for dep in row["depends_on"]}
        if any(rows[dep]["blockers"] for dep in parents):
            blockers.append("dependency_blocked")
        referenced = set(row["entities"])
        for entity in row["entities"]:
            referenced.update(holders[entity])
        inputs = {entity: registry["entity_digests"][entity] for entity in sorted(referenced)}
        rows[name] = {**row, "blockers": sorted(set(blockers)),
                      "input_digest": digest({"row": row, "analysis": binding,
                                              "entities": inputs, "parents": parents})}
    older = {row["id"]: row for row in previous["shots"]} if previous else {}
    changed = sorted(name for name in older.keys() | rows.keys()
                     if older.get(name, {}).get("input_digest") != rows.get(name, {}).get("input_digest"))
    return seal({"schema_version": "reference_draft_v1", **binding,
                 "revision": previous["revision"] + 1 if previous else 1,
                 "previous_digest": previous["draft_digest"] if previous else None,
                 "entity_revision_digest": registry["entity_revision_digest"],
                 "preferences": spec["preferences"], "shots": list(rows.values()),
                 "invalidated_shots": changed, "generation_authorized": False,
                 "dispatches": 0}, "draft_digest")


def update(operation, target, analysis, spec, *, expected=None, registry=None):
    require(operation in ("organize", "draft"))
    binding, shots = analysis_binding(analysis)
    field = "entity_revision_digest" if operation == "organize" else "draft_digest"
    with edit_lock(target):
        previous = read_json(target) if target.exists() else None
        if previous is not None:
            require(previous.get(field) == expected and seal(previous, field) == previous
                    and all(previous.get(k) == v for k, v in binding.items()), "REFERENCE_CAS_CONFLICT")
        else:
            require(expected is None, "REFERENCE_CAS_CONFLICT")
        if operation == "organize":
            value = organize(binding, shots, spec, previous)
        else:
            require(isinstance(registry, dict) and registry.get("schema_version") == "reference_entities_v1"
                    and seal(registry, "entity_revision_digest") == registry
                    and all(registry.get(k) == v for k, v in binding.items()), "REFERENCE_ENTITIES_INVALID")
            checked = organize(binding, shots, registry["entities"])
            require(checked["entity_digests"] == registry["entity_digests"], "REFERENCE_ENTITIES_INVALID")
            value = draft(binding, shots, registry, spec, previous)
        publish(target, value)
    return value[field]
