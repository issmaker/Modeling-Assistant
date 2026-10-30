"""Thin integration adapter; production BMesh/UV engine is in __init__.py."""
import bpy
import bmesh
from . import register_integrated, unregister_integrated
from . import partition_brick_selection, set_action_sink
from ...core import action_trace


def can_open(context):
    return bool(getattr(context, 'area', None) and context.area.type == 'VIEW_3D')


def can_start(context):
    if not can_open(context):
        return False, 'Откройте инструмент в окне 3D Viewport'
    if getattr(context, 'mode', None) != 'EDIT_MESH':
        return False, 'Перейдите в Edit Mode (Tab)'
    obj = getattr(context, 'edit_object', None)
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return False, 'Выберите Mesh-объект в Edit Mode'
    try:
        flags = tuple(bool(x) for x in context.tool_settings.mesh_select_mode)
        if len(flags) != 3 or sum(flags) != 1 or flags[0]:
            return False, 'Нужен один режим: рёбра (2) или грани (3)'
        bm = bmesh.from_edit_mesh(obj.data)
        faces, edges, _ = partition_brick_selection(bm, flags)
        if not faces and not edges:
            return False, 'Выделите рёбра либо грани для создания проёма'
    except (ReferenceError, RuntimeError, ValueError, AttributeError) as exc:
        return False, 'Ошибка выделения: %s' % str(exc)[:120]
    return True, 'Готово к preflight · геометрия не изменена'


def is_preview_active():
    from . import _INTERACTIVE_SESSION
    return _INTERACTIVE_SESSION is not None


def status():
    from . import _INTERACTIVE_SESSION, _OWNED_CLASSES
    return {'registered_classes': len(_OWNED_CLASSES),
            'preview_active': _INTERACTIVE_SESSION is not None,
            'entry': 'view3d.brick_hole_interactive'}


def register():
    register_integrated()
    set_action_sink(lambda action, detail: action_trace.record(action, 'BRICK_HOLE', detail))
    action_trace.record('MODULE_REGISTER', 'BRICK_HOLE', '1.0.17')


def unregister():
    set_action_sink(None)
    unregister_integrated()
    action_trace.record('MODULE_UNREGISTER', 'BRICK_HOLE')
