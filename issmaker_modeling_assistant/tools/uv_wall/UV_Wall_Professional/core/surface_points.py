"""Semantic surface height transactions.

Source Mesh coordinates remain the geometric truth. During pointer dragging the
viewport only holds temporary world-Z overrides; Blender datablocks are edited
by the production UNDO operator, and are restored on any generator failure.
"""
import json
import math
import uuid

from mathutils import Vector

UID_ATTRIBUTE = "uvwall_surface_point_uid"
ORIGINAL_LOCAL_KEY = "__uvwall_original_local_by_uid__"


def _positive_uid(used):
    for _ in range(128):
        uid = 1 + (uuid.uuid4().int % 2147483646)
        if uid not in used:
            return uid
    raise RuntimeError("Could not allocate a unique surface point UID")


def ensure_surface_point_uids(mesh):
    """Allocate persistent point-domain IDs without changing coordinates."""
    attribute = mesh.attributes.get(UID_ATTRIBUTE)
    created = attribute is None
    if created:
        attribute = mesh.attributes.new(name=UID_ATTRIBUTE, type="INT", domain="POINT")
    if attribute.data_type != "INT" or attribute.domain != "POINT":
        raise ValueError("Surface UID attribute has incompatible type/domain")
    used = set()
    for item in attribute.data:
        value = int(item.value)
        if value > 0 and value not in used:
            used.add(value)
        else:
            value = _positive_uid(used)
            item.value = value
            used.add(value)
    return created


def validate_height_edits(path, edits):
    """Return normalized {index: world_z} after all preflight checks."""
    if not isinstance(edits, dict):
        raise ValueError("Surface edit payload must be a dictionary")
    if path is None or path.type != "MESH":
        raise ValueError("Surface source must be a Mesh path")
    if path.data.users > 1:
        raise ValueError("Surface path has shared mesh data: make it Single User before height editing")
    if path.data.shape_keys is not None:
        raise ValueError("Surface authored height editing does not support shape-key Mesh sources")
    if abs(path.matrix_world.determinant()) < 1.e-10:
        raise ValueError("Surface path transform is singular; cannot set world-space height")
    result = {}
    for key, val in edits.items():
        try:
            index = int(key)
            height = float(val)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Invalid surface point index/height")
        if str(index) != str(key) or index < 0 or index >= len(path.data.vertices):
            raise ValueError("Surface height edit points to an invalid vertex")
        if not math.isfinite(height) or abs(height) > 1.e7:
            raise ValueError("Surface height is not finite or outside the allowed range")
        result[index] = height
    return result


def original_height_for_point(path, definition, index):
    """Return the authored pre-first-edit world Z, or None when unknowable.

    Each source point is keyed by a persistent UID rather than its mutable mesh
    index. Storing the local 3D position makes the baseline follow Object
    transforms. Older edits without this metadata are deliberately NOT guessed.
    """
    if path is None or definition is None or path.type != "MESH":
        return None
    try:
        i = int(index)
        if i < 0 or i >= len(path.data.vertices):
            return None
        attribute = path.data.attributes.get(UID_ATTRIBUTE)
        if attribute is None or len(attribute.data) != len(path.data.vertices):
            return None
        uid = int(attribute.data[i].value)
        if uid <= 0:
            return None
        payload = json.loads(definition.surface_heights_json or "{}")
        coordinates = payload.get(ORIGINAL_LOCAL_KEY, {}).get(str(uid))
        if not isinstance(coordinates, list) or len(coordinates) != 3:
            return None
        local = Vector(coordinates)
        world_z = float((path.matrix_world @ local).z)
        return world_z if math.isfinite(world_z) else None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, ReferenceError):
        return None


def apply_surface_heights(path, definition, edits):
    """Apply edits in current Blender UNDO transaction; return rollback state.

    Do not call outside the production operator. The caller must invoke
    rollback_surface_heights() on any generator error.
    """
    edits = validate_height_edits(path, edits)
    previous = {i: path.data.vertices[i].co.copy() for i in edits}
    uid_was_created = False
    existing_attribute = path.data.attributes.get(UID_ATTRIBUTE)
    uid_values_before = [int(e.value) for e in existing_attribute.data] if existing_attribute else None
    revision_before = int(definition.revision) if definition else 0
    metadata_before = definition.surface_heights_json if definition else "{}"
    try:
        if edits:
            uid_was_created = ensure_surface_point_uids(path.data)
            inv = path.matrix_world.inverted_safe()
            attr = path.data.attributes[UID_ATTRIBUTE]
            saved = json.loads(metadata_before or "{}")
            if not isinstance(saved, dict):
                saved = {}
            baselines = saved.get(ORIGINAL_LOCAL_KEY, {})
            if not isinstance(baselines, dict):
                baselines = {}
            for index, world_z in edits.items():
                uid = str(int(attr.data[index].value))
                if uid not in baselines:
                    # Capture the *pre-edit* local XYZ. This is the first
                    # recoverable baseline for paths already edited on <=1.7.1.
                    local = path.data.vertices[index].co
                    baselines[uid] = [float(local.x), float(local.y), float(local.z)]
                world_pos = path.matrix_world @ path.data.vertices[index].co
                world_pos.z = world_z
                path.data.vertices[index].co = inv @ world_pos
                saved[str(int(attr.data[index].value))] = world_z
            saved[ORIGINAL_LOCAL_KEY] = baselines
            if definition is not None:
                definition.surface_heights_json = json.dumps(saved, separators=(",", ":"), sort_keys=True)
                definition.revision += 1
            path.data.update()
    except Exception:
        rollback_surface_heights(path, definition, (previous, uid_was_created, metadata_before, uid_values_before, revision_before))
        raise
    return (previous, uid_was_created, metadata_before, uid_values_before, revision_before)


def rollback_surface_heights(path, definition, rollback):
    previous, uid_was_created, metadata_before, uid_values_before, revision_before = rollback
    for index, local_co in previous.items():
        if index < len(path.data.vertices):
            path.data.vertices[index].co = local_co
    if uid_was_created:
        attr = path.data.attributes.get(UID_ATTRIBUTE)
        if attr is not None:
            path.data.attributes.remove(attr)
    if uid_values_before is not None:
        attr = path.data.attributes.get(UID_ATTRIBUTE)
        if attr is not None:
            for datum, old_value in zip(attr.data, uid_values_before):
                datum.value = old_value
    if definition is not None:
        definition.surface_heights_json = metadata_before
        definition.revision = revision_before
    path.data.update()
