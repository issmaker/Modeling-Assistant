"""Collision Inspector 3.5.2 / RC12.2 integration; standalone engine unchanged.

Never register standalone N-panels or take ownership of another add-on's RNA.
"""
import bpy
from bpy.types import Panel
from bpy.props import PointerProperty
from . import (classes, COLLISIONINSPECTOR_Settings, _viewport_ui, _CI_KEYMAPS,
               _PICKER_CONFIRM_SCENES, _PICKER_REARM_TRIED, _PICKER_SESSIONS)
from ...core import action_trace

_OWNED_CLASSES = []
_OWNED_PROPERTY = False
_OWNED_VIEWPORT = False

def can_open(context):
    return bool(getattr(context, 'area', None) and context.area.type == 'VIEW_3D')

def can_start(context):
    if not can_open(context):
        return False, 'Откройте 3D Viewport'
    if getattr(context, 'mode', None) != 'OBJECT':
        return False, 'Коллизии: перейдите в Object Mode (Tab)'
    obj = getattr(context, 'active_object', None)
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return False, 'Коллизии: нужен активный Mesh-объект'
    if not getattr(obj, 'select_get', lambda: False)():
        return False, 'Выделите активный Mesh-объект'
    data = getattr(obj, 'data', None)
    if data is None or not len(data.vertices) and not len(data.edges) and not len(data.polygons):
        return False, 'Mesh пустой: добавьте геометрию'
    return True, 'Готов к проверке · исходный Mesh не меняется'

def status():
    settings = getattr(getattr(bpy.context, 'scene', None), 'collision_inspector', None)
    return {
        'version': '3.5.2', 'engine': 'RC12.2',
        'registered_classes': len(_OWNED_CLASSES),
        'viewport_handlers': len(getattr(_viewport_ui, '_HANDLERS', ())),
        'overlay_enabled': bool(getattr(settings, 'overlay_enabled', False)),
        'picker_active': bool(getattr(settings, 'overlay_pick_mode', False)),
        'results': len(settings.events) if settings is not None else 0,
        'entry': 'collision_inspector.check',
    }

def register():
    global _OWNED_PROPERTY, _OWNED_VIEWPORT
    if _OWNED_CLASSES or _OWNED_PROPERTY or _OWNED_VIEWPORT:
        raise RuntimeError('Collision Inspector: повторная регистрация модуля')
    if hasattr(bpy.types.Scene, 'collision_inspector'):
        raise RuntimeError('Collision Inspector уже установлен отдельно. Отключите standalone-аддон перед ISSMAKER.')
    # Own only native operator/property/UIList classes. Old Collision sidebar must never appear.
    for cls in (x for x in classes if not issubclass(x, Panel)):
        if hasattr(bpy.types, cls.__name__):
            raise RuntimeError('Collision Inspector: конфликт класса ' + cls.__name__)
    try:
        for cls in classes:
            if issubclass(cls, Panel):
                continue
            bpy.utils.register_class(cls)
            _OWNED_CLASSES.append(cls)
        bpy.types.Scene.collision_inspector = PointerProperty(type=COLLISIONINSPECTOR_Settings)
        _OWNED_PROPERTY = True
        # These two overlay handlers, depsgraph hooks, undo hooks and recovery timer
        # are wholly managed by the ORIGINAL viewport_ui implementation.
        _viewport_ui.install()
        _OWNED_VIEWPORT = True
        if not bpy.app.background:
            wm = bpy.context.window_manager
            config = getattr(getattr(wm, 'keyconfigs', None), 'addon', None)
            if config is not None:
                km = config.keymaps.new(name='3D View', space_type='VIEW_3D')
                kmi = km.keymap_items.new('collision_inspector.pick_markers', 'M', 'PRESS', ctrl=True, shift=True)
                _CI_KEYMAPS.append((km, kmi))
        action_trace.record('MODULE_REGISTER', 'COLLISION', '3.5.2')
    except Exception:
        unregister()
        raise

def unregister():
    global _OWNED_PROPERTY, _OWNED_VIEWPORT
    # Stop picker token first, so any in-flight modal instance exits safely.
    _PICKER_SESSIONS.clear()
    _PICKER_CONFIRM_SCENES.clear()
    _PICKER_REARM_TRIED.clear()
    for km, kmi in list(_CI_KEYMAPS):
        try: km.keymap_items.remove(kmi)
        except (RuntimeError, ReferenceError, ValueError): pass
    _CI_KEYMAPS.clear()
    if _OWNED_VIEWPORT or getattr(_viewport_ui, '_HANDLERS', None):
        try: _viewport_ui.uninstall()
        finally: _OWNED_VIEWPORT = False
    if _OWNED_PROPERTY:
        try: del bpy.types.Scene.collision_inspector
        except (AttributeError, ReferenceError, RuntimeError): pass
        _OWNED_PROPERTY = False
    for cls in reversed(_OWNED_CLASSES):
        try: bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError, ReferenceError): pass
    _OWNED_CLASSES.clear()
    action_trace.record('MODULE_UNREGISTER', 'COLLISION')
