"""Stable generated-result lifecycle for UV Wall NEXTGEN.

Production rebuilds should preserve Blender Object identity.  Replacing the
Object on every commit causes unnecessary depsgraph/material churn and breaks
selection/context references.  This module swaps only the Mesh datablock and
cleans orphaned previous meshes later, after the current depsgraph event.
"""
from __future__ import annotations

import bpy
import uuid
from mathutils import Matrix

# Queue entries are committed ONLY inside the successful production UNDO step.
# Keep original visibility for rollback and RNA pointer identity for name reuse.
_DEFERRED_MESH_NAMES = {}  # mesh name -> RNA pointer
_DEFERRED_OBJECT_NAMES = {}  # object name -> (pointer, owner, hidden, render, pending)
_TIMER_ARMED = False
_COMMIT_CLEANUP_ALLOWED = False


def _cleanup_deferred_datablocks():
    global _TIMER_ARMED
    if not _COMMIT_CLEANUP_ALLOWED:
        # A stale timer must never mutate geometry outside Blender Undo.
        return None
    object_jobs = tuple(_DEFERRED_OBJECT_NAMES.items())
    _DEFERRED_OBJECT_NAMES.clear()
    for name, (expected_pointer, owner_uid, _hidden, _render, _pending) in object_jobs:
        obj = _lookup_datablock_by_pointer(bpy.data.objects, name, expected_pointer)
        if obj is None:
            continue
        # Undo/Redo or a rapid disable-enable may replace/resurrect this name.
        # Never remove a new Object, a reactivated child, or foreign ownership.
        try:
            if obj.as_pointer() != expected_pointer:
                continue
            if owner_uid and obj.get('uvwall_child_parent') != owner_uid and obj.get('uvwall_owner_uid') != owner_uid:
                continue
            if not obj.get('uvwall_pending_delete'):
                continue
            if obj.get('uvwall_detached') or obj.get('uvwall_baked'):
                continue
            if not (obj.get('uvwall_generated') or obj.get('uvwall_child_surface') or obj.get('uvwall_guide')):
                continue
        except ReferenceError:
            continue
        mesh = obj.data if getattr(obj, "type", None) == "MESH" else None
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            continue
        _queue_orphan_mesh(mesh)

    pending = tuple(_DEFERRED_MESH_NAMES.items())
    _DEFERRED_MESH_NAMES.clear()
    for name, expected_pointer in pending:
        mesh = _lookup_datablock_by_pointer(bpy.data.meshes, name, expected_pointer)
        if mesh is not None and mesh.users == 0 and _safe_pointer(mesh) == expected_pointer:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                # Keep it orphaned; Blender can purge it safely later.
                pass
    _TIMER_ARMED = False
    return None


def finalize_pending_cleanup():
    """Call only from a successful Blender UNDO production operator."""
    global _COMMIT_CLEANUP_ALLOWED
    _COMMIT_CLEANUP_ALLOWED = True
    try:
        _cleanup_deferred_datablocks()
    finally:
        _COMMIT_CLEANUP_ALLOWED = False


def _arm_cleanup(delay=0.5):
    # Compatibility no-op: NEVER register a background timer for Undo-owned IDs.
    return None

def _safe_pointer(datablock):
    """RNA address is stable across wrapper instances; 0/removed IDs are rejected."""
    try:
        ptr = datablock.as_pointer()
        return ptr if isinstance(ptr, int) and ptr > 0 else None
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return None


def _lookup_datablock_by_pointer(collection, name, expected_pointer):
    """Resolve a queued Blender ID even if the user renamed it before cleanup.

    Name-only deferred queues can leak hidden ``uvwall_pending_delete`` objects
    when a rename happens between scheduling and commit.  Pointer matching is
    runtime-only and is never persisted as semantic ownership.
    """
    try:
        direct = collection.get(name)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        direct = None
    if direct is not None and _safe_pointer(direct) == expected_pointer:
        return direct
    try:
        for candidate in collection:
            if _safe_pointer(candidate) == expected_pointer:
                return candidate
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    return None


def _queue_orphan_mesh(mesh):
    if mesh is None:
        return
    try:
        pointer = _safe_pointer(mesh)
        if mesh.users == 0 and pointer is not None:
            _DEFERRED_MESH_NAMES[mesh.name] = pointer
    except (ReferenceError, AttributeError, RuntimeError, TypeError):
        return


def defer_mesh_cleanup(mesh, delay=0.5):
    """Defer orphan deletion until Build succeeds; guard against name reuse."""
    _queue_orphan_mesh(mesh)


def result_owner_uid(obj):
    if obj is None:
        return ""
    try:
        return str(obj.get("uvwall_definition_uid") or obj.get("uvwall_owner_uid") or obj.get("uvwall_child_parent") or "")
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return ""


def ensure_result_uid(obj):
    """Persistent semantic Result UID independent of Blender object name/pointer."""
    if obj is None:
        return ""
    try:
        uid = str(obj.get("uvwall_result_uid") or "")
        if not uid:
            uid = uuid.uuid4().hex
            obj["uvwall_result_uid"] = uid
        return uid
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return ""


def ownership_guard(obj, requester_uid, operation="MUTATE", allow_unowned_legacy=False):
    """Reject cross-Definition mutation before touching Blender datablocks."""
    if obj is None or not requester_uid:
        return False, "missing target/requester"
    owner = result_owner_uid(obj)
    if owner == requester_uid:
        return True, "OK"
    if allow_unowned_legacy and not owner:
        return True, "LEGACY_UNOWNED"
    result_uid = ""
    try:
        result_uid = str(obj.get("uvwall_result_uid") or "")
    except Exception:
        pass
    return False, (f"OWNERSHIP_GUARD_DENIED operation={operation}; requester={requester_uid}; "
                   f"target={getattr(obj, 'name', '<removed>')}; result_uid={result_uid}; owner={owner}")


def assign_result_identity(obj, owner_uid, owner_path, mode, revision):
    """Stamp the common Definition→Result contract after successful geometry validation."""
    if obj is None or not owner_uid:
        raise ValueError("Result identity requires owner UID")
    current = result_owner_uid(obj)
    if current and current != owner_uid:
        raise ValueError(
            f"OWNERSHIP_GUARD_DENIED operation=ASSIGN_IDENTITY; requester={owner_uid}; "
            f"target={getattr(obj, 'name', '<removed>')}; owner={current}"
        )
    obj["uvwall_owner_uid"] = owner_uid
    obj["uvwall_definition_uid"] = owner_uid
    obj["uvwall_owner_path"] = str(owner_path or "")
    obj["uvwall_result_type"] = str(mode or "")
    obj["uvwall_build_revision"] = int(revision)
    obj["uvwall_generated"] = True
    return ensure_result_uid(obj)


def defer_object_cleanup(obj, delay=0.2, requester_uid=""):
    """Snapshot state before marking an owned generated object for deletion.

    Return False for authored, detached, baked, or stale Blender objects.
    Scheduling twice keeps the original visibility snapshot for correct rollback.
    """
    if obj is None:
        return False
    if requester_uid:
        allowed, _reason = ownership_guard(obj, requester_uid, operation="DELETE")
        if not allowed:
            return False
    try:
        pointer = _safe_pointer(obj)
        if pointer is None or obj.get('uvwall_detached') or obj.get('uvwall_baked'):
            return False
        if not (obj.get('uvwall_generated') or obj.get('uvwall_child_surface') or obj.get('uvwall_guide')):
            return False
        name = obj.name
        previous = _DEFERRED_OBJECT_NAMES.get(name)
        if previous and previous[0] == pointer:
            return True
        try:
            hidden = bool(obj.hide_get())
        except AttributeError:
            hidden = getattr(obj, 'hidden', None)  # Blender API test doubles only
        rendered = bool(obj.hide_render)
        old_pending = obj.get('uvwall_pending_delete', None)
        # Write the marker only after the snapshot has succeeded.
        obj['uvwall_pending_delete'] = True
        owner_uid = obj.get('uvwall_child_parent') or obj.get('uvwall_owner_uid') or ''
        _DEFERRED_OBJECT_NAMES[name] = (pointer, owner_uid, hidden, rendered, old_pending)
        return True
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def _hide_for_cleanup(obj):
    # Capture visibility BEFORE touching the viewport state; failed Builds
    # reverse this mutation rather than leaving orphaned invisible children.
    if defer_object_cleanup(obj):
        obj.hide_set(True)
        obj.hide_render = True


def schedule_duplicate_children(owner_uid,keep=None):
    if not owner_uid:
        return 0
    obsolete=[obj for obj in tuple(bpy.data.objects)
              if obj is not keep and obj.get('uvwall_child_surface')
              and obj.get('uvwall_child_parent')==owner_uid
              and not obj.get('uvwall_detached') and not obj.get('uvwall_baked')]
    for obj in obsolete:
        if defer_object_cleanup(obj, requester_uid=owner_uid):
            obj.hide_set(True)
            obj.hide_render = True
    return len([obj for obj in obsolete if obj.get("uvwall_pending_delete")])


def schedule_obsolete_children(owner_uid, last_name=''):
    """Find children by stable UID, not by the possibly out-of-date name.

    Baked/detached objects are deliberately excluded. Hide instantly; remove
    safely on the next depsgraph event. A subsequent rebuild can reactivate it.
    """
    if not owner_uid:
        return 0
    stale = [obj for obj in tuple(bpy.data.objects)
             if obj.get('uvwall_child_surface') and obj.get('uvwall_child_parent')==owner_uid
             and not obj.get('uvwall_detached') and not obj.get('uvwall_baked')]
    scheduled = 0
    for obj in stale:
        if defer_object_cleanup(obj, requester_uid=owner_uid):
            obj.hide_set(True)
            obj.hide_render = True
            scheduled += 1
    return scheduled


def find_owned_guides(owner_uid):
    if not owner_uid:
        return []
    return [obj for obj in bpy.data.objects
            if obj.get('uvwall_guide') and obj.get('uvwall_child_parent') == owner_uid
            and not obj.get('uvwall_detached') and not obj.get('uvwall_baked')]


def schedule_obsolete_guides(owner_uid, keep_guide=False):
    guides=find_owned_guides(owner_uid)
    scheduled = 0
    if not keep_guide:
        for obj in guides:
            if defer_object_cleanup(obj, requester_uid=owner_uid):
                obj.hide_set(True); obj.hide_render = True; scheduled += 1
    elif len(guides)>1:
        for obj in guides[1:]:
            if defer_object_cleanup(obj, requester_uid=owner_uid):
                obj.hide_set(True); obj.hide_render = True; scheduled += 1
    return 0 if keep_guide else scheduled


def discard_pending_cleanup():
    """Rollback any visibility/marker changes made while queuing deletion.

    Match both name and RNA pointer. Never unhide a replacement object or a
    user-owned object that inherited the name after undo/redo.
    """
    for name, (pointer, _owner, hidden, rendered, pending) in tuple(_DEFERRED_OBJECT_NAMES.items()):
        try:
            obj = _lookup_datablock_by_pointer(bpy.data.objects, name, pointer)
            if obj is None:
                continue
            if hidden is not None:
                obj.hide_set(hidden)
            obj.hide_render = rendered
            if pending is None:
                if 'uvwall_pending_delete' in obj:
                    del obj['uvwall_pending_delete']
            else:
                obj['uvwall_pending_delete'] = pending
        except (AttributeError, ReferenceError, RuntimeError, TypeError, KeyError):
            continue
    _DEFERRED_OBJECT_NAMES.clear()
    _DEFERRED_MESH_NAMES.clear()


def shutdown_result_lifecycle():
    """Cancel runtime cleanup without leaving a half-hidden pending Result.

    Undo/Redo, file load and add-on reload are lifecycle boundaries.  Pending
    visibility/markers are rolled back before queues are forgotten; otherwise a
    failed/reloaded transaction could leave an invisible Result indefinitely.
    """
    global _TIMER_ARMED
    try:
        if bpy.app.timers.is_registered(_cleanup_deferred_datablocks):
            bpy.app.timers.unregister(_cleanup_deferred_datablocks)
    except Exception:
        pass
    _TIMER_ARMED = False
    discard_pending_cleanup()

def find_owned_results(path_obj, definition, exclude=()):
    if path_obj is None or definition is None:
        return []
    excluded = {obj for obj in exclude if obj is not None}
    owner_uid = getattr(definition, "source_uid", "")
    owner_name = path_obj.name
    results = []
    for obj in bpy.data.objects:
        if obj in excluded or not obj.get("uvwall_generated"):
            continue
        if owner_uid and obj.get("uvwall_owner_uid") == owner_uid:
            results.append(obj)
        elif not obj.get("uvwall_owner_uid") and obj.get("uvwall_owner_path") == owner_name:
            results.append(obj)
    return results


def active_owned_results(path_obj, definition):
    """Return live generated Results that are candidates for one Definition.

    Multiple active candidates are an ownership ambiguity, not a cleanup hint.
    Production code must fail closed rather than deleting a user-duplicated or
    resurrected Result merely because it carries the same copied UID.
    """
    active = []
    for obj in find_owned_results(path_obj, definition):
        try:
            if obj.get("uvwall_pending_delete") or obj.get("uvwall_detached") or obj.get("uvwall_baked"):
                continue
            active.append(obj)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue
    return active


def preferred_stable_result(path_obj, definition):
    results = active_owned_results(path_obj, definition)
    last_name = getattr(definition, "last_result_name", "") if definition is not None else ""
    if last_name:
        obj = bpy.data.objects.get(last_name)
        if obj in results and obj.type == "MESH":
            return obj
        # Migration path for pre-ownership NEXTGEN results: reuse the named
        # derived object when it still declares the same authored Path.
        if (
            obj is not None and obj.type == "MESH"
            and (obj.get("path_object") or obj.get("uvwall_owner_path")) == getattr(path_obj, "name", None)
            and (not obj.get("uvwall_owner_uid") or obj.get("uvwall_owner_uid") == getattr(definition, "source_uid", ""))
            and obj.get("uv_wall_mode") in {"WALL", "SURFACE", "CORNICE"}
        ):
            return obj
    for obj in results:
        if obj.type == "MESH":
            return obj
    return None


def install_mesh_on_result(context, mesh, name, target_obj=None):
    """Create the first result Object or atomically reuse an existing one."""
    if target_obj is not None and target_obj.name in bpy.data.objects and target_obj.type == "MESH":
        old_mesh = target_obj.data
        target_obj.data = mesh
        # Generated meshes are authored in world coordinates. Rebuilds must not
        # inherit an accidental manual transform from the previous derived result.
        try:
            target_obj.matrix_world = Matrix.Identity(4)
        except Exception:
            pass
        # Keep object identity, collections, selection and custom ownership.
        if name:
            try:
                target_obj.name = name
            except Exception:
                pass
        defer_mesh_cleanup(old_mesh)
        return target_obj, True

    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)
    return obj, False



_MODE_SPECIFIC_KEYS = {
    "roof_only", "chain_index", "source_object", "path_object", "profile_object",
    "uv_tile_length", "stories", "first_story_height", "align_bottom",
    "bottom_extend", "stories_down", "profile_length", "profile_perimeter",
    "profile_closed", "side_flipped", "side_auto_flipped", "side_requested_flip",
    "cornice_uv_distortion_warnings", "cornice_uv_max_predicted_distortion",
    "cornice_auto_corner_fallbacks", "cornice_frame_max_up_tilt_deg",
    # WALL-only forensic evidence must not survive a Stable Result conversion
    # WALL -> CORNICE/SURFACE. Previous diagnostic archives misleadingly
    # displayed prior WALL optimizer and CAP contact counters on a CORNICE Mesh.
    "uvwall_topology_optimization", "uvwall_topology_first",
    "uvwall_geometry_truth_contract",
    "uvwall_boundary_first", "uvwall_normal_integrity",
    "inner_wall", "inner_wall_height_delta", "inner_wall_height_overrides_json",
    "secondary_points_json", "closed_start_end", "closed_finish_end",
    "wall_normal_mode", "top_cap", "bottom_cap", "top_cap_mode",
}

_ROLLBACK_METADATA_KEYS = _MODE_SPECIFIC_KEYS | {
    "uv_wall_mode", "uvwall_pending_delete", "uvwall_owner_uid", "uvwall_owner_path",
    "uvwall_definition_revision", "uvwall_definition_json", "uvwall_generated",
    "uvwall_definition_uid", "uvwall_result_uid", "uvwall_result_type", "uvwall_build_revision",
}


def snapshot_result_metadata(obj):
    """Keep identity and key generator metadata for a failed Stable Result swap.

    We intentionally snapshot a bounded set of generated keys rather than
    cloning arbitrary third-party IDProperties or Blender image datablocks.
    """
    if obj is None:
        return None
    try:
        return (obj.name, {key: obj[key] for key in _ROLLBACK_METADATA_KEYS if key in obj})
    except (AttributeError, ReferenceError, RuntimeError, TypeError, KeyError):
        return None


def restore_result_metadata(obj, snapshot):
    """Undo temporary mode/ownership tags and an accidental result rename."""
    if obj is None or snapshot is None:
        return
    name, props = snapshot
    try:
        for key in _ROLLBACK_METADATA_KEYS:
            try:
                if key in props:
                    obj[key] = props[key]
                elif key in obj:
                    del obj[key]
            except (AttributeError, ReferenceError, RuntimeError, TypeError, KeyError):
                pass
        obj.name = name
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass


def prepare_result_metadata(obj, mode):
    """Clear stale generator-specific metadata when one stable Object changes intent."""
    if obj is None:
        return
    for key in _MODE_SPECIFIC_KEYS:
        try:
            if key in obj:
                del obj[key]
        except Exception:
            pass
    try:
        obj["uv_wall_mode"] = str(mode)
    except Exception:
        pass

def copy_custom_properties(source_obj, target_obj):
    if source_obj is None or target_obj is None:
        return
    for key in source_obj.keys():
        if key == "_RNA_UI":
            continue
        try:
            target_obj[key] = source_obj[key]
        except Exception:
            pass


def cleanup_duplicate_results(path_obj, definition, keep_objects):
    """Remove duplicate result Objects but defer their Mesh datablock cleanup."""
    if isinstance(keep_objects, (list, tuple, set)):
        keep = tuple(obj for obj in keep_objects if obj is not None)
    else:
        keep = (keep_objects,) if keep_objects is not None else ()
    owner_uid = getattr(definition, "source_uid", "") if definition is not None else ""
    for obj in list(find_owned_results(path_obj, definition, exclude=keep)):
        try:
            defer_object_cleanup(obj, requester_uid=owner_uid)
        except Exception:
            continue
    # Commit-time cleanup follows after the production operator succeeds.
