"""Safe integration for the ORIGINAL UV Wall 2.0.0 viewport engine.

The native release is loaded under its CANONICAL module name to preserve its
strict source/identity guard. It is not installed as a second Blender add-on.
Only the UI preference bridge has a modified digest, reflected in its manifest.
"""
import importlib.util
import sys
from pathlib import Path

import bpy
from ...core import action_trace

_CANONICAL = 'UV_Wall_Professional'
_WALL = None
_REGISTERED = False
_RELEASE = '2.0.0'


def can_open(context):
    return bool(getattr(context, 'area', None) and context.area.type == 'VIEW_3D')


def can_start(context):
    """Check ONLY read-only properties. Original invoke remains final authority."""
    if not can_open(context):
        return False, 'Откройте 3D Viewport для работы со стеной.'
    if getattr(context, 'mode', None) != 'OBJECT':
        return False, 'Стена: перейдите в Object Mode (Tab), затем выберите контур.'
    obj = getattr(context, 'active_object', None)
    if obj is None:
        return False, 'Стена: выделите Mesh-контур из рёбер без граней.'
    if getattr(obj, 'type', None) == 'CURVE':
        return False, 'Выделена Curve. Преобразуйте путь в Mesh (Object > Convert > Mesh).'
    if getattr(obj, 'type', None) != 'MESH':
        return False, 'Стена: нужен Mesh-контур, а не ' + str(obj.type) + '.'
    if callable(getattr(obj, 'select_get', None)) and not obj.select_get():
        return False, 'Стена: выделите объект Mesh-пути (сейчас он не выделен).'
    data = getattr(obj, 'data', None)
    if data is None:
        return False, 'Стена: у выбранного объекта нет Mesh-геометрии.'
    try:
        if len(data.polygons):
            return False, 'Стена: у пути не должно быть граней. Выберите контур только из рёбер.'
        if not len(data.edges):
            return False, 'Стена: у пути нет рёбер. Создайте линию хотя бы из двух точек.'
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False, 'Стена: Mesh недоступен. Выберите другой объект пути.'
    return True, 'Готово: Mesh-контур найден. Откройте Viewport Inspector.'


def status(context=None):
    context = context or bpy.context
    wm = getattr(context, 'window_manager', None)
    return dict(version=_RELEASE, registered=_REGISTERED,
                entry='uvwall.viewport_inspector',
                active=bool(getattr(wm, 'uvwall_viewport_active', False)),
                native_sessions=(_WALL._viewport_controller.lifecycle_snapshot()
                                 if _WALL is not None else None))


def _load_native():
    global _WALL
    if _WALL is not None:
        return _WALL
    native_dir = Path(__file__).resolve().parent / _CANONICAL
    root = str(native_dir.resolve())
    old = sys.modules.get(_CANONICAL)
    if old is not None:
        old_path = str(Path(getattr(old, '__file__', '')).resolve().parent)
        if old_path != root:
            raise RuntimeError('UV Wall Professional уже загружен отдельно. Отключите standalone и перезапустите Blender.')
        _WALL = old
        _WALL._ui_preferences.ADDON_KEY = __package__.rsplit('.tools.uv_wall', 1)[0]
        return old
    spec = importlib.util.spec_from_file_location(_CANONICAL, native_dir / '__init__.py',
                    submodule_search_locations=[root])
    if spec is None or spec.loader is None:
        raise RuntimeError('UV Wall: нет установочного пакета 2.0.0.')
    obj = importlib.util.module_from_spec(spec)
    sys.modules[_CANONICAL] = obj
    try:
        spec.loader.exec_module(obj)
    except Exception:
        for name in tuple(sys.modules):
            if name == _CANONICAL or name.startswith(_CANONICAL + '.'):
                sys.modules.pop(name, None)
        raise
    _WALL = obj
    # Blender Extensions import add-ons as bl_ext.<repository>.<addon>.
    # The preference bridge must target the actual enabled root module,
    # not assume the legacy no-prefix module ID.
    obj._ui_preferences.ADDON_KEY = __package__.rsplit('.tools.uv_wall', 1)[0]
    return obj


def register():
    global _REGISTERED
    if _REGISTERED:
        raise RuntimeError('UV Wall: повторная интеграционная регистрация')
    # Do not allow native self-healing to unregister other add-ons' RNA classes.
    if hasattr(bpy.types.Scene, 'uv_wall_settings') or hasattr(bpy.types.Object, 'uvwall_definition'):
        raise RuntimeError('UV Wall уже зарегистрирован отдельно. Отключите самостоятельный аддон и перезапустите Blender.')
    native = _load_native()
    from bpy.types import Panel, AddonPreferences
    # Independent Blender panel and addon prefs are replaced by ISSMAKER's UI.
    native.classes = tuple(cls for cls in native.classes
                           if not issubclass(cls, (Panel, AddonPreferences)))
    native_core = native._definitions
    guards = [native_core.UVWALL_PG_path_definition, *native.classes,
              *native._viewport_controller.CLASSES]
    for cls in guards:
        if getattr(bpy.types, cls.__name__, None) is not None:
            raise RuntimeError('UV Wall: конфликт Blender RNA ' + cls.__name__ + '. Отключите отдельно установленный UV Wall.')
    def _strict_register(cls):
        if getattr(bpy.types, cls.__name__, None) is not None:
            raise RuntimeError('UV Wall: запрещено вытеснять чужой класс ' + cls.__name__)
        bpy.utils.register_class(cls)
    native._register_class_replace_stale = _strict_register
    try:
        native.register()
    except Exception:
        # Native register() has its own transactional rollback.
        raise
    _REGISTERED = True
    action_trace.record('MODULE_REGISTER', 'UV_WALL', _RELEASE)


def unregister():
    global _REGISTERED, _WALL
    if _WALL is not None and _REGISTERED:
        try:
            _WALL.unregister()
        finally:
            _REGISTERED = False
    # Do not pop imported code while Blender is still drawing UV Wall UI.
    # Native module retains one source-checked identity for safe re-enable.
    action_trace.record('MODULE_UNREGISTER', 'UV_WALL', _RELEASE)


