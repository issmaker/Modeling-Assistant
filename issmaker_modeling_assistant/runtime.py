import time
from functools import wraps

import bpy


_ACTIVE = set()
_LAST_START = {}

# Detached Blender mesh copies are safe, but an unbounded copy can exhaust RAM.
MAX_SAFETY_COPY_BYTES = 512 * 1024 * 1024


class MeshSafetyError(RuntimeError):
    pass


def estimate_mesh_copy_bytes(mesh):
    """Conservative preflight estimate including topology, UV and attributes."""
    return (
        len(mesh.vertices) * 128 +
        len(mesh.edges) * 96 +
        len(mesh.loops) * 192 +
        len(mesh.polygons) * 160 +
        4 * 1024 * 1024
    )


def checked_mesh_copy(mesh, purpose="операции"):
    estimate = estimate_mesh_copy_bytes(mesh)
    if estimate > MAX_SAFETY_COPY_BYTES:
        mib = estimate / (1024 * 1024)
        raise MeshSafetyError(
            f"{purpose}: безопасная копия потребует примерно {mib:.0f} МБ; "
            "операция отменена до изменения объекта")
    try:
        return mesh.copy()
    except (MemoryError, RuntimeError) as exc:
        raise MeshSafetyError(
            f"{purpose}: не удалось создать резервную копию; объект не изменён") from exc


class EditMeshTransaction:
    """Explicit rollback for operators that can fail after changing edit geometry."""

    def __init__(self, context, obj, purpose="операции"):
        if obj is None or context.mode != 'EDIT_MESH' or obj.type != 'MESH':
            raise MeshSafetyError(f"{purpose}: объект больше не находится в Edit Mode")
        if obj.data.users != 1:
            raise MeshSafetyError(
                f"{purpose}: сетка используется несколькими объектами; "
                "сначала сделайте данные Single User")
        self.context = context
        self.obj = obj
        self.original = obj.data
        self.original_name = obj.data.name
        self.rollback_error = None
        self.mutation_started = False
        obj.update_from_editmode()
        self.backup = checked_mesh_copy(obj.data, purpose)
        self.backup.name = ".IMA_Safety_Backup"

    def commit(self):
        backup = self.backup
        self.backup = None
        if backup is not None:
            try:
                bpy.data.meshes.remove(backup)
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                pass

    def rollback(self):
        backup = self.backup
        self.backup = None
        if backup is None:
            return False
        try:
            obj = self.obj
            if self.context.mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='OBJECT')
            damaged = obj.data
            backup.name = self.original_name
            obj.data = backup
            if damaged.users == 0:
                try:
                    bpy.data.meshes.remove(damaged)
                except (ReferenceError, RuntimeError, TypeError, ValueError):
                    pass
            self.context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            return True
        except Exception as exc:
            self.rollback_error = exc
            return False


def acquire(key, cooldown=0.35):
    """Reject concurrent and accidental double starts of the same operation."""
    now = time.monotonic()
    if key in _ACTIVE or now - _LAST_START.get(key, -1000.0) < cooldown:
        return False
    _ACTIVE.add(key)
    _LAST_START[key] = now
    return True


def release(key):
    _ACTIVE.discard(key)


def reset():
    _ACTIVE.clear()
    _LAST_START.clear()


def protected(key, cooldown=0.35):
    """Decorator for short Blender operators with shared double-click protection."""
    def decorate(function):
        @wraps(function)
        def wrapped(operator, context):
            if not acquire(key, cooldown=cooldown):
                operator.report({'WARNING'}, "Операция уже выполняется")
                return {'CANCELLED'}
            try:
                return function(operator, context)
            finally:
                release(key)
        return wrapped
    return decorate


def register_classes_atomic(classes):
    """Never leave half-registered Python operator types behind."""
    registered = []
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        raise


def unregister_classes_safe(classes):
    """Attempt every class even if one was already removed by Blender."""
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
