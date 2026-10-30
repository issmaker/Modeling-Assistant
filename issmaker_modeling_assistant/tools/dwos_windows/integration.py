"""DWOS Window Core 3.7.37 RC38 integration into the single ISSMAKER sidebar.

Native DWOS geometry, operators and viewport composer are reused.
Only Smart Interior 3D Lab / Conflict Navigator have their own ISSMAKER
sidebar panels. All unrelated original DWOS standalone panels remain disabled.
"""
import bpy
from bpy.types import Panel, AddonPreferences
from . import native
from ...core import action_trace

_OWNED_CLASSES = []
_OWNED_PROPERTIES = False
_OWNED_PROPERTY_CLASSES = []
_OWNED_SCENE_PTR = False
_OWNED_OBJECT_PTR = False
_OWNED_SMART = False
_OWNED_OVERLAY = False
_OWNED_SURFACE = False


def _window_from_object(obj):
    if obj is None:
        return None
    try:
        return native.composer.resolve_window(obj)
    except (AttributeError, RuntimeError, ReferenceError, TypeError):
        return None


def can_start(context):
    if not getattr(context, 'area', None) or context.area.type != 'VIEW_3D':
        return False, 'Откройте 3D Viewport.', None
    if getattr(context, 'mode', None) not in {'OBJECT', 'EDIT_MESH'}:
        return False, 'Окна: нужен Object Mode или Edit Mode выбранной Mesh-стены.', None
    from .native import surface_composer_bridge
    if getattr(context, 'mode', None) == 'EDIT_MESH':
        from .native import auto_window
        try:
            host, groups=auto_window.selected_patches(context)
            return True, f'Создать {len(groups)} окон из выделенных полигонов и открыть Composer.', 'AUTO'
        except (ValueError, TypeError, RuntimeError) as exc:
            return False, 'Окна: '+str(exc)[:155], None
    obj = getattr(context, 'active_object', None)
    if obj is None:
        return False, 'Окна: ничего не выделено. Выделите Mesh-стену с проёмами либо готовое окно DWOS.', None
    window = _window_from_object(obj)
    if window is None:
        host = surface_composer_bridge.resolve_host(context)
        if host is not None:
            return True, 'Открыть существующий DWOS Composer для LP/HP и импостов.', 'COMPOSER'
    if window is not None:
        if getattr(window.dwos_window, 'finalized', False):
            return False, 'Окно DWOS финализировано. Выберите редактируемое окно.', None
        return True, 'Открыть Composer выбранного окна DWOS.', 'COMPOSER'
    if obj.type != 'MESH':
        return False, 'Окна: нужен Mesh-объект стены с проёмами. Выбран другой тип объекта.', None
    if not obj.select_get():
        return False, 'Окна: выделите Mesh-стену (Object Mode).', None
    if not getattr(obj, 'data', None) or not len(obj.data.polygons):
        return False, 'Окна: у стены должны быть грани и реальные проёмы. Выбран пустой контур.', None
    return True, 'Найти проёмы в выбранной Mesh-стене.', 'FIND'


def status(context=None):
    settings = getattr(getattr(context, 'scene', None), 'dwos', None)
    return dict(version='3.7.37 RC38', registered_classes=len(_OWNED_CLASSES),
                registered=bool(_OWNED_PROPERTIES),
                composer_active=bool(getattr(native.composer, '_ACTIVE', None)),
                candidate_count=len(settings.openings) if settings else 0,
                native_entry_points=['dwos.viewport_composer', 'dwos.find_openings'],
                standalone_panel=False)


def register():
    global _OWNED_PROPERTIES, _OWNED_SCENE_PTR, _OWNED_OBJECT_PTR, _OWNED_SMART, _OWNED_OVERLAY, _OWNED_SURFACE
    if _OWNED_CLASSES or _OWNED_PROPERTIES or _OWNED_PROPERTY_CLASSES:
        raise RuntimeError('DWOS: повторная регистрация ISSMAKER')
    if hasattr(bpy.types.Scene,'dwos') or hasattr(bpy.types.Object,'dwos_window'):
        raise RuntimeError('DWOS уже установлен отдельно. Отключите отдельный аддон перед запуском ISSMAKER.')
    # W58.9: retain Unified Suite ownership of the main sidebar, but permit
    # exactly the two user-requested Smart Interior issue/preview panels.
    # The original bulk DWOS panels remain suppressed in this integration.
    _approved_panels=(
        native.smart_interior_conflict_ui.DWOS_PT_smart_interior_conflict_navigator,
        native.smart_interior_3d_modes.DWOS_PT_smart_interior_3d_lab,
        native.window_region_cutter.DWOS_PT_W5811_architectural_qa,
    )
    classes = tuple(
        x for x in native.CLASSES
        if not issubclass(x,AddonPreferences) and
        (not issubclass(x,Panel) or x in _approved_panels))
    if len({x.__name__ for x in classes}) != len(classes):
        raise RuntimeError('DWOS: повторяющиеся классы RNA в исходном релизе')
    for cls in (*native.properties.CLASSES, *classes):
        if getattr(bpy.types, cls.__name__, None) is not None:
            raise RuntimeError('DWOS: конфликт класса ' + cls.__name__ + '. Отключите самостоятельный DWOS.')
    try:
        native.setup_logging()
        native.dwos_history.clear()
        for cls in native.properties.CLASSES:
            bpy.utils.register_class(cls)
            _OWNED_PROPERTY_CLASSES.append(cls)
        bpy.types.Object.dwos_window = bpy.props.PointerProperty(type=native.properties.DWOSWindowSettings)
        _OWNED_OBJECT_PTR=True
        bpy.types.Scene.dwos = bpy.props.PointerProperty(type=native.properties.DWOSSceneSettings)
        _OWNED_SCENE_PTR=True
        _OWNED_PROPERTIES=True
        for cls in classes:
            bpy.utils.register_class(cls)
            _OWNED_CLASSES.append(cls)
        native.surface_steps.register()
        _OWNED_SURFACE=True
        native.smart_interior_auto.register()
        _OWNED_SMART=True
        if not bpy.app.background:
            native.viewport.ensure_overlay()
            _OWNED_OVERLAY=True
        action_trace.record('MODULE_REGISTER', 'DWOS', '3.7.37 RC38')
    except Exception:
        unregister()
        raise


def unregister():
    global _OWNED_PROPERTIES, _OWNED_SCENE_PTR, _OWNED_OBJECT_PTR, _OWNED_SMART, _OWNED_OVERLAY, _OWNED_SURFACE
    # Composer contains a modal handler; original cleanup invalidates that handle.
    try: native.composer.remove_active_draw_handler()
    except (RuntimeError,ReferenceError,AttributeError): pass
    try:
        if _OWNED_OVERLAY: native.viewport.remove_overlay()
    finally: _OWNED_OVERLAY=False
    try:
        if _OWNED_SMART: native.smart_interior_auto.unregister()
    finally: _OWNED_SMART=False
    if _OWNED_SURFACE:
        try:native.surface_steps.unregister()
        finally:_OWNED_SURFACE=False
    for cls in reversed(_OWNED_CLASSES):
        try: bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError, ReferenceError): pass
    _OWNED_CLASSES.clear()
    if _OWNED_SCENE_PTR:
        try: del bpy.types.Scene.dwos
        except (AttributeError,ReferenceError,RuntimeError): pass
        _OWNED_SCENE_PTR=False
    if _OWNED_OBJECT_PTR:
        try: del bpy.types.Object.dwos_window
        except (AttributeError,ReferenceError,RuntimeError): pass
        _OWNED_OBJECT_PTR=False
    for cls in reversed(_OWNED_PROPERTY_CLASSES):
        try: bpy.utils.unregister_class(cls)
        except (RuntimeError,ValueError,ReferenceError): pass
    _OWNED_PROPERTY_CLASSES.clear()
    _OWNED_PROPERTIES=False
    action_trace.record('MODULE_UNREGISTER','DWOS')
