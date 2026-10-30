import math
import time

import bpy
import bmesh
from bpy.app.handlers import persistent
from bpy.types import Operator

from . import log, runtime, selection_core, uv_feedback, issue_nav_core, history_guard
from .core.uv_flip_detection import classify_uv_polygon, mesh_uv_states, bmesh_face_uv_state


_VIEW_STATES = {}
_OBJECT_COLOR_STATE = {}
_MODE_UCX_VISIBILITY = None
_UCX_VISIBILITY = None
_BORDER_STATE = None
_BORDER_EXPECTED = None
_BORDER_LAST_CHECK = 0.0
_ISSUES = []
_ISSUE_INDEX = -1
_ISSUE_KIND = None
_ISSUE_CURRENT = None
_ISSUE_THRESHOLD = None
_ISSUE_NAV_STATUS = 'NO_SESSION'
_ISSUE_NAV_REASON = ''
_ISSUE_SESSION_COUNTER = 0
_ISSUE_SESSION_ID = 0
_ISSUE_INITIAL_COUNT = 0
_ANALYSIS_STATE = None
_ANALYSIS_RUNNING = False
_ANALYSIS_TIMER = None
_ANALYSIS_WM = None
_ANALYSIS_SNAPSHOT_MESH = None
_ANALYSIS_EXPECTED_SELECTION = None  # last selection explicitly applied by the add-on
_ANALYSIS_SELECTION_TOUCHED = False
# Viewport display is UI state, not model data. None means that the user has
# not selected an add-on display mode in the current file/session yet.
_ACTIVE_VIEW_MODE = None


def _discard_analysis_snapshot():
    """Remove the detached mesh used by analysis without touching edit BMesh."""
    global _ANALYSIS_SNAPSHOT_MESH
    mesh = _ANALYSIS_SNAPSHOT_MESH
    _ANALYSIS_SNAPSHOT_MESH = None
    if mesh is None:
        return
    try:
        bpy.data.meshes.remove(mesh)
    except (ReferenceError, RuntimeError, TypeError, ValueError):
        # File loading, undo and add-on shutdown can invalidate RNA first.
        pass


def _stop_analysis_timer():
    global _ANALYSIS_TIMER, _ANALYSIS_WM, _ANALYSIS_RUNNING
    if _ANALYSIS_TIMER is not None and _ANALYSIS_WM is not None:
        try:
            _ANALYSIS_WM.event_timer_remove(_ANALYSIS_TIMER)
        except (ReferenceError, RuntimeError, ValueError, AttributeError):
            pass
    if _ANALYSIS_WM is not None:
        try:
            _ANALYSIS_WM.progress_end()
        except (ReferenceError, RuntimeError, ValueError, AttributeError):
            pass
    _ANALYSIS_TIMER = None
    _ANALYSIS_WM = None
    _ANALYSIS_RUNNING = False
    _discard_analysis_snapshot()


def _clear_stale_status_text():
    """One-shot cleanup for status text left by an interrupted older version."""
    try:
        workspace = getattr(bpy.context, "workspace", None)
        if workspace is not None:
            workspace.status_text_set(None)
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    return None


def _view_space(context):
    space = getattr(context, "space_data", None)
    return space if space and space.type == 'VIEW_3D' else None


def _read_attr(owner, name):
    return getattr(owner, name) if hasattr(owner, name) else None


def _write_attr(owner, name, value):
    if value is not None and hasattr(owner, name):
        try:
            setattr(owner, name, value)
        except (AttributeError, TypeError):
            pass


def _capture_view(space):
    key = space.as_pointer()
    if key not in _VIEW_STATES:
        shading = space.shading
        overlay = space.overlay
        _VIEW_STATES[key] = {
            "clip_start": space.clip_start,
            "lens": space.lens,
            "shading.type": shading.type,
            "shading.color_type": shading.color_type,
            "shading.use_scene_lights": _read_attr(shading, "use_scene_lights"),
            "shading.use_scene_world": _read_attr(shading, "use_scene_world"),
            "shading.show_backface_culling": _read_attr(shading, "show_backface_culling"),
            "shading.show_xray": _read_attr(shading, "show_xray"),
            "shading.xray_alpha": _read_attr(shading, "xray_alpha"),
            "overlay.show_face_orientation": overlay.show_face_orientation,
        }


def _restore_view(space):
    entry = _VIEW_STATES.get(space.as_pointer())
    if not entry:
        return
    values = entry
    _write_attr(space, "clip_start", values["clip_start"])
    _write_attr(space, "lens", values["lens"])
    for name, value in values.items():
        if name.startswith("shading."):
            _write_attr(space.shading, name.split(".", 1)[1], value)
        elif name.startswith("overlay."):
            _write_attr(space.overlay, name.split(".", 1)[1], value)


def _ucx_objects():
    return [obj for obj in bpy.data.objects if obj.name.upper().startswith("UCX_")]


def ucx_active():
    return _UCX_VISIBILITY is not None


def _discard_border_view(reason=None):
    global _BORDER_STATE, _BORDER_EXPECTED, _BORDER_LAST_CHECK
    _BORDER_STATE = None
    _BORDER_EXPECTED = None
    _BORDER_LAST_CHECK = 0.0
    if reason:
        log.write(f"Open borders view discarded: {reason}")


def borders_active(context=None):
    """Validate that the user still has exactly the shown border edges selected."""
    global _BORDER_LAST_CHECK
    if _BORDER_STATE is None or _BORDER_EXPECTED is None:
        return False
    if context is None:
        return True
    if context.mode != 'EDIT_MESH':
        _discard_border_view("left Edit Mode")
        return False
    now = time.monotonic()
    if now - _BORDER_LAST_CHECK < 0.20:
        return True
    _BORDER_LAST_CHECK = now
    try:
        for obj, expected_indexes, signature in _BORDER_EXPECTED:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.edges.ensure_lookup_table()
            if (len(bm.verts), len(bm.edges), len(bm.faces)) != signature:
                _discard_border_view("topology changed")
                return False
            # Keep indices only. Long-lived BMEdge references become invalid
            # when Blender rebuilds Edit Mesh and must never be retained.
            if any(index >= len(bm.edges) or not bm.edges[index].select
                   for index in expected_indexes):
                _discard_border_view("border selection changed")
                return False
            # Exact selection comparison is safe for ordinary meshes. On very
            # large meshes avoid a full Python scan on every viewport redraw.
            if len(bm.edges) <= 250000:
                selected = {edge.index for edge in bm.edges if edge.select}
                if selected != expected_indexes:
                    _discard_border_view("edge selection changed")
                    return False
    except (ReferenceError, RuntimeError, ValueError):
        _discard_border_view("edit mesh became unavailable")
        return False
    return True


def issues_active():
    return bool(_ISSUES)


def issues_context_matches(context):
    """Never restore an old object's selection into a different edit session."""
    if not issues_active() or _ANALYSIS_STATE is None:
        return False
    try:
        state = _ANALYSIS_STATE
        obj = context.active_object
        return bool(
            context.mode == 'EDIT_MESH' and
            obj is not None and obj == state["active"] and
            obj.type == 'MESH' and obj.data == state["mesh"] and
            selection_core.uv_layer_matches(obj.data, state["uv_key"])
        )
    except (ReferenceError, RuntimeError, AttributeError):
        return False


def discard_stale_issues(context):
    if issues_active() and not issues_context_matches(context):
        log.write("UV analysis discarded: active object/mesh/UV layer changed")
        _finish_analysis(context, restore_selection=False)
        return True
    return False


def clear_issue_session_for_tool(context, reason='other tool'):
    """Close read-only UV results before another diagnostic tool takes focus.

    No selection restoration: user might explicitly have chosen new vertices.
    """
    if _ANALYSIS_RUNNING:
        _stop_analysis_timer()
    if _ISSUES or _ANALYSIS_STATE is not None:
        log.write('UV session closed for tool: %s' % reason)
        _finish_analysis(context, restore_selection=False)


def issue_kind():
    return _ISSUE_KIND


def issue_navigation_status():
    """Diagnostic and UI status; unlike overlay counters, reflects verification."""
    return {
        'state': _ISSUE_NAV_STATUS,
        'reason': _ISSUE_NAV_REASON,
        'session_id': _ISSUE_SESSION_ID,
        'stored': len(_ISSUES),
        'initial_found': _ISSUE_INITIAL_COUNT,
        'current_face': (_ISSUE_CURRENT[1] if _ISSUE_CURRENT is not None else None),
        'position': (_ISSUE_INDEX + 1 if _ISSUE_INDEX >= 0 else 0),
    }


def analysis_running():
    return _ANALYSIS_RUNNING


def _capture_colors():
    for obj in bpy.data.objects:
        if obj.type == 'MESH' and obj.as_pointer() not in _OBJECT_COLOR_STATE:
            _OBJECT_COLOR_STATE[obj.as_pointer()] = tuple(obj.color)


def _restore_colors():
    global _OBJECT_COLOR_STATE
    try:
        live = {obj.as_pointer(): obj for obj in bpy.data.objects}
        for pointer, color in _OBJECT_COLOR_STATE.items():
            obj = live.get(pointer)
            if obj is not None:
                obj.color = color
    except (ReferenceError, RuntimeError):
        pass
    _OBJECT_COLOR_STATE = {}


def _show_ucx_for_mode():
    global _MODE_UCX_VISIBILITY
    if _MODE_UCX_VISIBILITY is None:
        _MODE_UCX_VISIBILITY = [(obj, obj.hide_get()) for obj in _ucx_objects()]
    for obj in _ucx_objects():
        obj.hide_set(False)


def _restore_mode_ucx_visibility():
    global _MODE_UCX_VISIBILITY
    if _MODE_UCX_VISIBILITY is None:
        return
    for obj, hidden in _MODE_UCX_VISIBILITY:
        try:
            obj.hide_set(hidden)
        except ReferenceError:
            pass
    _MODE_UCX_VISIBILITY = None


def _apply_view_mode(space, mode):
    _capture_view(space)
    _restore_view(space)
    _restore_colors()
    _restore_mode_ucx_visibility()
    shading, overlay = space.shading, space.overlay
    overlay.show_face_orientation = False
    _write_attr(shading, "show_xray", False)
    _write_attr(shading, "show_backface_culling", False)

    if mode == 'usual':
        shading.type = 'SOLID'
        shading.color_type = 'MATERIAL'
    elif mode == 'textured':
        shading.type = 'SOLID'
        shading.color_type = 'TEXTURE'
    elif mode == 'materials':
        shading.type = 'MATERIAL'
        _write_attr(shading, "use_scene_lights", False)
        _write_attr(shading, "use_scene_world", False)
    elif mode == 'night':
        shading.type = 'MATERIAL'
        _write_attr(shading, "use_scene_lights", True)
        _write_attr(shading, "use_scene_world", True)
    elif mode == 'normals':
        shading.type = 'SOLID'
        shading.color_type = 'MATERIAL'
        overlay.show_face_orientation = True
        space.lens = 20.0
    elif mode == 'zfight':
        shading.type = 'SOLID'
        shading.color_type = 'MATERIAL'
        space.clip_start = 0.01
        _write_attr(shading, "show_backface_culling", True)
    elif mode in {'ucx', 'ucx2'}:
        shading.type = 'SOLID'
        shading.color_type = 'OBJECT'
        _capture_colors()
        _show_ucx_for_mode()
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            is_ucx = obj.name.upper().startswith("UCX_")
            if mode == 'ucx':
                obj.color = (0.02, 0.20, 0.95, 1.0) if is_ucx else (0.35, 0.35, 0.35, 1.0)
            else:
                obj.color = (0.55, 0.55, 0.55, 1.0) if is_ucx else (0.02, 0.20, 0.95, 1.0)


@persistent
def _history_pre(_dummy, kind):
    """Stop volatile modal/GPU readers before native Blender history rebuilds BMesh.

    Never modify a Mesh, select geometry, intercept Ctrl+Z or push Undo.
    """
    try:
        history_guard.begin(kind)
        _stop_analysis_timer()
        uv_feedback.clear('%s in progress: BMesh snapshot invalidated' % kind)
        _set_navigation_status('HISTORY_PENDING', '%s: ожидание Blender' % kind)
        log.write('UV HISTORY %s_PRE: modal stopped, overlay cleared' % kind)
    except Exception as exc:
        # A buggy add-on handler must not block Blender's native Undo/Redo.
        log.write('UV HISTORY %s_PRE cleanup failed: %s: %s' %
                  (kind, type(exc).__name__, exc))


@persistent
def _undo_pre(_dummy):
    _history_pre(_dummy, 'UNDO')


@persistent
def _redo_pre(_dummy):
    _history_pre(_dummy, 'REDO')


def _history_post(_dummy, kind):
    """Invalidate scene-derived ephemeral state after Blender restored history.

    Handles may reference freed/recreated IDs after either Ctrl+Z or Ctrl+Shift+Z.
    Never restore old selection, alter scene shading, or rebuild geometry here.
    """
    global _UCX_VISIBILITY, _BORDER_STATE, _BORDER_EXPECTED
    global _ISSUES, _ISSUE_INDEX, _ISSUE_KIND
    global _ISSUE_CURRENT, _ISSUE_THRESHOLD, _ISSUE_INITIAL_COUNT
    global _ANALYSIS_STATE, _ANALYSIS_RUNNING
    global _ANALYSIS_EXPECTED_SELECTION, _ANALYSIS_SELECTION_TOUCHED
    failure = ''
    try:
        _ANALYSIS_EXPECTED_SELECTION = None
        _ANALYSIS_SELECTION_TOUCHED = False
        _stop_analysis_timer()
        _UCX_VISIBILITY = None
        _BORDER_STATE = None
        _BORDER_EXPECTED = None
        _ISSUES = []
        _ISSUE_INDEX = -1
        _ISSUE_KIND = None
        _ISSUE_CURRENT = None
        _ISSUE_THRESHOLD = None
        _ISSUE_INITIAL_COUNT = 0
        _ANALYSIS_STATE = None
        _ANALYSIS_RUNNING = False
        uv_feedback.clear('%s: анализ устарел, запустите заново' % kind)
        _set_navigation_status('%s_INVALIDATED' % kind,
                               'Blender %s: требуется новый UV-анализ' % kind)
        runtime.reset()
    except Exception as exc:
        failure = '%s: %s' % (type(exc).__name__, exc)
        log.write('UV HISTORY %s_POST cleanup failed: %s' % (kind, failure))
    finally:
        history_guard.complete(kind, not bool(failure), failure)
        log.write('UV HISTORY %s_POST: generation=%s result=%s' %
                  (kind, history_guard.status()['generation'],
                   'ERROR' if failure else 'INVALIDATED'))
        # Blender history changed. Refresh UI but never mutate scene data.
        try:
            for window in tuple(bpy.context.window_manager.windows):
                for area in tuple(window.screen.areas):
                    if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                        area.tag_redraw()
        except (AttributeError, ReferenceError, RuntimeError, ValueError):
            pass


@persistent
def _undo_post(_dummy):
    _history_post(_dummy, 'UNDO')


@persistent
def _redo_post(_dummy):
    _history_post(_dummy, 'REDO')


@persistent
def _load_pre(_dummy):
    """Stop modal work before Blender invalidates objects during file loading."""
    global _ANALYSIS_STATE, _ISSUES, _ISSUE_INDEX, _ISSUE_KIND
    global _ISSUE_CURRENT, _ISSUE_THRESHOLD, _ISSUE_INITIAL_COUNT
    global _VIEW_STATES, _OBJECT_COLOR_STATE, _MODE_UCX_VISIBILITY
    global _UCX_VISIBILITY, _BORDER_STATE, _BORDER_EXPECTED
    global _ACTIVE_VIEW_MODE
    _stop_analysis_timer()
    global _ANALYSIS_EXPECTED_SELECTION, _ANALYSIS_SELECTION_TOUCHED
    _ANALYSIS_EXPECTED_SELECTION = None
    _ANALYSIS_SELECTION_TOUCHED = False
    _ANALYSIS_STATE = None
    _ISSUES = []
    _ISSUE_INDEX = -1
    _ISSUE_KIND = None
    _ISSUE_CURRENT = None
    _ISSUE_THRESHOLD = None
    _ISSUE_INITIAL_COUNT = 0
    # Do not dereference old-file RNA from a load handler. Simply forget it.
    _VIEW_STATES = {}
    _OBJECT_COLOR_STATE = {}
    _MODE_UCX_VISIBILITY = None
    _UCX_VISIBILITY = None
    _BORDER_STATE = None
    _BORDER_EXPECTED = None
    _ACTIVE_VIEW_MODE = None
    uv_feedback.clear('file load')
    history_guard.reset()
    runtime.reset()


def restore_all_runtime_state():
    global _VIEW_STATES, _UCX_VISIBILITY, _BORDER_STATE, _BORDER_EXPECTED
    _stop_analysis_timer()
    uv_feedback.clear()
    # Add-on disable must not strand an explicitly applied issue selection.
    # Context and live selection are validated before any restoration.
    try:
        if _ANALYSIS_STATE is not None:
            _finish_analysis(bpy.context, restore_selection=True)
    except (ReferenceError, RuntimeError, ValueError) as exc:
        log.write(f"Analysis cleanup during unregister skipped safely: {exc}")
    _BORDER_STATE = None
    _BORDER_EXPECTED = None
    try:
        wm = getattr(bpy.context, "window_manager", None)
        if wm is not None:
            for window in wm.windows:
                for area in window.screen.areas:
                    if area.type != 'VIEW_3D':
                        continue
                    space = area.spaces.active
                    if space.as_pointer() in _VIEW_STATES:
                        _restore_view(space)
    except (ReferenceError, RuntimeError):
        pass
    _VIEW_STATES = {}
    _restore_colors()
    _restore_mode_ucx_visibility()
    if _UCX_VISIBILITY is not None:
        for obj, hidden in _UCX_VISIBILITY:
            try:
                obj.hide_set(hidden)
            except ReferenceError:
                pass
        _UCX_VISIBILITY = None


class IMA_OT_ToggleUCX(Operator):
    bl_idname = "ima.toggle_ucx"
    bl_label = "Показать/скрыть UCX"
    bl_description = "Показать или скрыть коллизионные объекты с префиксом UCX_. Геометрия не изменяется."
    bl_options = {'REGISTER', 'UNDO'}

    @runtime.protected("toggle_ucx")
    def execute(self, context):
        global _UCX_VISIBILITY
        objects = _ucx_objects()
        if not objects:
            self.report({'WARNING'}, "Объекты UCX_ не найдены")
            return {'CANCELLED'}
        if _UCX_VISIBILITY is None:
            _UCX_VISIBILITY = [(obj, obj.hide_get()) for obj in objects]
            show = not any(not obj.hide_get() for obj in objects)
            # If at least one UCX is already visible, the first click hides all.
            for obj in objects:
                obj.hide_set(not show)
            state = "показаны" if show else "скрыты"
        else:
            for obj, hidden in _UCX_VISIBILITY:
                try:
                    obj.hide_set(hidden)
                except ReferenceError:
                    pass
            _UCX_VISIBILITY = None
            state = "исходная видимость восстановлена"
        log.write(f"UCX: {state}; objects={len(objects)}")
        self.report({'INFO'}, f"UCX: {state}")
        return {'FINISHED'}


class IMA_OT_ViewMode(Operator):
    bl_idname = "ima.view_mode"
    bl_label = "Режим отображения"
    bl_description = "Переключить безопасный режим отображения текущего 3D Viewport"
    # Display changes must never consume or be restored by geometry Ctrl+Z.
    bl_options = {'REGISTER'}

    mode: bpy.props.EnumProperty(items=(
        ('usual', "Обычный", "Solid с цветами материалов"),
        ('normals', "Нормали", "Ориентация граней"),
        ('zfight', "Z-fighting", "Режим поиска совпадающих поверхностей"),
        ('night', "Ночь", "Материалы со светом и миром сцены"),
        ('ucx', "Коллизии", "UCX синие, остальная модель нейтральная"),
        ('ucx2', "Коллизии 2", "UCX нейтральные, остальная модель синяя"),
        ('textured', "Текстуры", "Solid с текстурами"),
        ('materials', "Материалы", "Material Preview со студийным светом"),
    ))

    @runtime.protected("view_mode", cooldown=0.15)
    def execute(self, context):
        global _ACTIVE_VIEW_MODE
        space = _view_space(context)
        if not space:
            return {'CANCELLED'}
        context.scene.ima_view_mode_state = self.mode
        _ACTIVE_VIEW_MODE = self.mode
        _apply_view_mode(space, self.mode)
        context.area.tag_redraw()
        log.write(f"View mode: {self.mode}")
        return {'FINISHED'}


class IMA_OT_RestoreView(Operator):
    bl_idname = "ima.restore_view"
    bl_label = "Восстановить отображение"
    bl_description = "Вернуть настройки отображения, которые были до использования режимов аддона"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _ACTIVE_VIEW_MODE
        space = _view_space(context)
        if space:
            _restore_view(space)
            _VIEW_STATES.pop(space.as_pointer(), None)
        context.scene.ima_view_mode_state = 'RESTORE'
        _ACTIVE_VIEW_MODE = 'RESTORE'
        _restore_colors()
        _restore_mode_ucx_visibility()
        context.area.tag_redraw()
        log.write("Viewport state restored")
        return {'FINISHED'}


def _mesh_selection_snapshot(context):
    objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
    active = context.view_layer.objects.active
    mode = context.mode
    if mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')
    data = []
    for obj in objects:
        data.append((obj,
                     {v.index for v in obj.data.vertices if v.select},
                     {e.index for e in obj.data.edges if e.select},
                     {p.index for p in obj.data.polygons if p.select}))
    return mode, active, objects, data


def _restore_mesh_selection(context, state):
    mode, active, objects, data = state
    if context.mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    for obj, verts, edges, faces in data:
        try:
            obj.select_set(True)
            for item in obj.data.vertices:
                item.select = item.index in verts
            for item in obj.data.edges:
                item.select = item.index in edges
            for item in obj.data.polygons:
                item.select = item.index in faces
        except ReferenceError:
            continue
    if active and active.name in context.view_layer.objects:
        context.view_layer.objects.active = active
    if mode == 'EDIT_MESH' and active and active.name in context.view_layer.objects:
        bpy.ops.object.mode_set(mode='EDIT')


class IMA_OT_ToggleBorders(Operator):
    bl_idname = "ima.toggle_borders"
    bl_label = "Граничные рёбра"
    bl_description = "Найти и выделить открытые граничные рёбра сетки. Повторное нажатие возвращает предыдущее выделение."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(context.mode == 'EDIT_MESH' and obj and obj.type == 'MESH')

    @runtime.protected("toggle_borders")
    def execute(self, context):
        global _BORDER_STATE, _BORDER_EXPECTED, _BORDER_LAST_CHECK
        borders_active(context)
        if _BORDER_STATE is not None:
            _restore_mesh_selection(context, _BORDER_STATE)
            _discard_border_view()
            self.report({'INFO'}, "Исходное выделение восстановлено")
            return {'FINISHED'}
        objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not objects and context.active_object and context.active_object.type == 'MESH':
            objects = [context.active_object]
        if not objects:
            self.report({'WARNING'}, "Выберите Mesh-объект")
            return {'CANCELLED'}
        _BORDER_STATE = _mesh_selection_snapshot(context)
        bpy.ops.object.select_all(action='DESELECT')
        total = 0
        affected = []
        for obj in objects:
            bm = bmesh.new()
            try:
                bm.from_mesh(obj.data)
                indexes = [edge.index for edge in bm.edges if len(edge.link_faces) == 1]
            finally:
                bm.free()
            if not indexes:
                continue
            affected.append((obj, set(indexes)))
            total += len(indexes)
            obj.select_set(True)
        if not affected:
            state = _BORDER_STATE
            _discard_border_view()
            _restore_mesh_selection(context, state)
            self.report({'INFO'}, "Открытых граничных рёбер нет")
            return {'FINISHED'}
        context.view_layer.objects.active = affected[0][0]
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='EDGE')
        bpy.ops.mesh.select_all(action='DESELECT')
        for obj, indexes in affected:
            bm = bmesh.from_edit_mesh(obj.data)
            for edge in bm.edges:
                edge.select = edge.index in indexes
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        _BORDER_EXPECTED = []
        for obj, indexes in affected:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.edges.ensure_lookup_table()
            _BORDER_EXPECTED.append((
                obj, frozenset(indexes),
                (len(bm.verts), len(bm.edges), len(bm.faces)),
            ))
        _BORDER_LAST_CHECK = 0.0
        log.write(f"Open borders shown: {total}")
        self.report({'INFO'}, f"Граничных рёбер: {total}")
        return {'FINISHED'}


def _angle(v1, v2):
    if v1.length_squared <= 1.0e-16 or v2.length_squared <= 1.0e-16:
        return None
    return v1.angle(v2)


def _distorted_face_indexes(obj, threshold_degrees):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv = bm.loops.layers.uv.active
        if uv is None:
            return None
        result = []
        matrix = obj.matrix_world
        threshold = math.radians(threshold_degrees)
        for face in bm.faces:
            distorted = False
            for loop in face.loops:
                previous = loop.link_loop_prev
                following = loop.link_loop_next
                center_3d = matrix @ loop.vert.co
                a3 = matrix @ previous.vert.co - center_3d
                b3 = matrix @ following.vert.co - center_3d
                center_uv = loop[uv].uv
                auv = previous[uv].uv - center_uv
                buv = following[uv].uv - center_uv
                angle_3d = _angle(a3, b3)
                angle_uv = _angle(auv, buv)
                if angle_3d is None or angle_uv is None:
                    distorted = True
                    break
                if abs(angle_3d - angle_uv) > threshold:
                    distorted = True
                    break
            if distorted:
                result.append(face.index)
        return result
    finally:
        bm.free()


def _flipped_face_indexes(obj):
    """Read-only clockwise UV polygons (triangles, quads, N-gons).

    Folded/invalid shapes are not promoted to a valid positive/negative sign.
    The modal operator reports these separately as UNVERIFIED.
    """
    mesh = obj.data
    layer = mesh.uv_layers.active
    if layer is None:
        return None
    uv_data = layer.data
    return [index for index,state in mesh_uv_states(mesh).items() if state=='FLIPPED']


def _select_face_sets(context, affected, select_one=None):
    if context.mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    for obj, _indexes in affected:
        obj.hide_set(False)
        obj.select_set(True)
    target_obj = select_one[0] if select_one else affected[0][0]
    context.view_layer.objects.active = target_obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='DESELECT')
    for obj, indexes in affected:
        bm = bmesh.from_edit_mesh(obj.data)
        for face in bm.faces:
            face.select = ((obj == select_one[0] and face.index == select_one[1])
                           if select_one else face.index in indexes)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def _analysis_selection_snapshot(context, obj):
    """Compact, lossless mesh+UV flags, not selected faces only."""
    if context.mode != 'EDIT_MESH':
        raise RuntimeError("Анализ требует Edit Mode")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    layer = bm.loops.layers.uv.active
    if layer is None:
        raise RuntimeError("Активная UV-карта отсутствует")
    # Failing before any selection mutation is better than partial snapshots.
    approximate_bytes = (len(bm.verts) + len(bm.edges) + len(bm.faces) +
                         2 * sum(len(f.loops) for f in bm.faces) +
                         64 * len(bm.faces))
    if approximate_bytes > 192 * 1024 * 1024:
        raise RuntimeError("Слишком большое выделение для безопасной копии состояния")
    return {
        "mode": context.mode,
        "active": context.view_layer.objects.active,
        "objects": list(context.selected_objects),
        "mesh": obj.data,
        "uv_key": selection_core.uv_layer_key(obj.data),
        "counts": (len(bm.verts), len(bm.edges), len(bm.faces)),
        "vert_flags": bytes(int(v.select) for v in bm.verts),
        "edge_flags": bytes(int(e.select) for e in bm.edges),
        "face_flags": bytes(int(f.select) for f in bm.faces),
        "uv_flags": tuple(bytes((int(l[layer].select) |
                                 (int(getattr(l[layer], 'select_edge', False)) << 1))
                                for l in f.loops) for f in bm.faces),
        "active_face": bm.faces.active.index if bm.faces.active else None,
        "select_mode": tuple(context.tool_settings.mesh_select_mode),
        "topology": selection_core.topology_fingerprint(bm),
        "history": [
            ('VERT', item.index) if isinstance(item, bmesh.types.BMVert)
            else ('EDGE', item.index) if isinstance(item, bmesh.types.BMEdge)
            else ('FACE', item.index)
            for item in bm.select_history
            if isinstance(item, (bmesh.types.BMVert, bmesh.types.BMEdge,
                                 bmesh.types.BMFace))
        ],
        "selection_before": selection_core.uv_and_mesh_fingerprint(
            bm, layer, context.tool_settings),
    }


def _analysis_live_selection_digest(context):
    obj = context.active_object
    bm = bmesh.from_edit_mesh(obj.data)
    return selection_core.uv_and_mesh_fingerprint(
        bm, bm.loops.layers.uv.active, context.tool_settings)


def _select_analysis_faces(context, obj, indexes):
    """Explicit opt-in: select issue faces ONLY on the current edit BMesh.

    Never run object.select_all, mode_set or change scene object selection.
    This also prevents switching the active object when changing UV issues.
    """
    if context.mode != 'EDIT_MESH' or context.active_object != obj:
        raise RuntimeError("Просмотр UV: выбран другой объект; выделение не изменено")
    if not issues_context_matches(context):
        raise RuntimeError("Просмотр UV: исходная UV-карта изменена; выделение не изменено")
    editing = getattr(context, 'objects_in_mode_unique_data', None)
    if editing is not None and len(editing) != 1:
        raise RuntimeError("Просмотр UV: Multi-Object Edit Mode не поддерживается")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    wanted = frozenset(indexes)
    if any(index < 0 or index >= len(bm.faces) for index in wanted):
        raise RuntimeError("Просмотр UV: список проблем устарел; выделение не изменено")
    # Face mode is intentional, but is not changed until all checks pass.
    context.tool_settings.mesh_select_mode = (False, False, True)
    for face in bm.faces:
        face.select_set(False)
    for vert in bm.verts:
        vert.select_set(False)
    for edge in bm.edges:
        edge.select_set(False)
    for index in wanted:
        bm.faces[index].select_set(True)
    bm.select_flush_mode()
    layer = bm.loops.layers.uv.active
    if layer is not None and not context.tool_settings.use_uv_select_sync:
        for face in bm.faces:
            is_issue = face.index in wanted
            for loop in face.loops:
                loop[layer].select = is_issue
                if hasattr(loop[layer], 'select_edge'):
                    loop[layer].select_edge = is_issue
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def _apply_issue_selection(context, obj, indexes):
    """Do not overwrite a user's manual selection made since last preview."""
    global _ANALYSIS_EXPECTED_SELECTION, _ANALYSIS_SELECTION_TOUCHED
    if not issues_context_matches(context):
        raise RuntimeError("Контекст анализа изменился")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.index_update()
    bm.edges.index_update()
    if (selection_core.topology_fingerprint(bm) !=
            _ANALYSIS_STATE['topology']):
        raise RuntimeError("Топология изменилась; выполните новый поиск")
    actual = _analysis_live_selection_digest(context)
    expected = (_ANALYSIS_EXPECTED_SELECTION if _ANALYSIS_SELECTION_TOUCHED
                else _ANALYSIS_STATE['selection_before'])
    if expected != actual:
        raise RuntimeError(
            "Выделение изменено вручную после анализа: команда отменена; "
            "запустите поиск заново, чтобы не потерять выделение")
    # A broken Blender selection operator must not leave a half-selected mesh.
    before_apply = _analysis_selection_snapshot(context, obj)
    try:
        _select_analysis_faces(context, obj, indexes)
        expected_after = _analysis_live_selection_digest(context)
    except Exception:
        try:
            _restore_analysis_selection(context, before_apply)
            log.write('Issue selection failure: pre-action selection restored')
        except Exception as recovery_exc:
            log.write_exception('Issue selection rollback failed', recovery_exc)
        raise
    _ANALYSIS_SELECTION_TOUCHED = True
    _ANALYSIS_EXPECTED_SELECTION = expected_after


def _restore_analysis_selection(context, state):
    active = state["active"]
    if (context.mode != 'EDIT_MESH' or context.active_object != active or
            active.data != state["mesh"] or
            not selection_core.uv_layer_matches(active.data, state["uv_key"])):
        raise RuntimeError("Сеанс объекта/UV изменился; старое выделение не восстановлено")
    bm = bmesh.from_edit_mesh(active.data)
    if (len(bm.verts), len(bm.edges), len(bm.faces)) != state["counts"]:
        raise RuntimeError("Топология изменилась; старое выделение не восстановлено")
    layer = bm.loops.layers.uv.active
    if layer is None:
        raise RuntimeError("UV-карта исчезла; восстановление отменено")
    bm.verts.index_update()
    bm.edges.index_update()
    if selection_core.topology_fingerprint(bm) != state['topology']:
        raise RuntimeError("Топология изменилась при просмотре; восстановление отменено")
    for face, saved in zip(bm.faces, state["uv_flags"]):
        if len(face.loops) != len(saved):
            raise RuntimeError("UV-топология изменилась; восстановление отменено")
    # Apply after validation, without an Edit/Object mode round-trip.
    context.tool_settings.mesh_select_mode = state["select_mode"]
    for vert, flag in zip(bm.verts, state["vert_flags"]):
        vert.select = bool(flag)
    for edge, flag in zip(bm.edges, state["edge_flags"]):
        edge.select = bool(flag)
    for face, flag, saved in zip(bm.faces, state["face_flags"], state["uv_flags"]):
        face.select = bool(flag)
        for loop, uv_flag in zip(face.loops, saved):
            loop[layer].select = bool(uv_flag & 1)
            if hasattr(loop[layer], 'select_edge'):
                loop[layer].select_edge = bool(uv_flag & 2)
    bm.faces.active = (bm.faces[state["active_face"]]
                       if state["active_face"] is not None else None)
    bm.select_history.clear()
    arrays = {'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}
    for kind, index in state['history']:
        item = arrays[kind][index]
        if item.select:
            bm.select_history.add(item)
    bmesh.update_edit_mesh(active.data, loop_triangles=False, destructive=False)


def _set_navigation_status(status, reason=''):
    global _ISSUE_NAV_STATUS, _ISSUE_NAV_REASON
    _ISSUE_NAV_STATUS = status
    _ISSUE_NAV_REASON = str(reason)[:512]


def _store_issues(kind, affected, threshold=None):
    global _ISSUES, _ISSUE_INDEX, _ISSUE_KIND
    global _ISSUE_CURRENT, _ISSUE_THRESHOLD, _ISSUE_SESSION_COUNTER, _ISSUE_SESSION_ID, _ISSUE_INITIAL_COUNT
    _ISSUE_SESSION_COUNTER += 1
    _ISSUE_SESSION_ID = _ISSUE_SESSION_COUNTER
    _set_navigation_status('READY')
    _ISSUES = [(obj, index) for obj, indexes in affected for index in sorted(indexes)]
    # No single issue is active while the initial result shows all problems.
    _ISSUE_INITIAL_COUNT = len(_ISSUES)
    _ISSUE_INDEX = -1
    _ISSUE_KIND = kind if _ISSUES else None
    _ISSUE_CURRENT = None
    _ISSUE_THRESHOLD = threshold if _ISSUES and kind == 'STRETCH' else None


def _finish_analysis(context, restore_selection=True):
    global _ISSUES, _ISSUE_INDEX, _ISSUE_KIND, _ANALYSIS_STATE
    global _ISSUE_CURRENT, _ISSUE_THRESHOLD, _ISSUE_INITIAL_COUNT
    global _ANALYSIS_EXPECTED_SELECTION, _ANALYSIS_SELECTION_TOUCHED
    _set_navigation_status('NO_SESSION')
    uv_feedback.clear()
    try:
        # Scanning has zero selection side effects. Only undo selections that
        # THIS session explicitly applied, and only if unchanged by the user.
        if (restore_selection and _ANALYSIS_SELECTION_TOUCHED and
                _ANALYSIS_STATE is not None):
            if issues_context_matches(context):
                try:
                    if (_analysis_live_selection_digest(context) ==
                            _ANALYSIS_EXPECTED_SELECTION):
                        _restore_analysis_selection(context, _ANALYSIS_STATE)
                    else:
                        log.write("UV restore skipped: user changed selection after preview")
                except (ReferenceError, RuntimeError, ValueError) as exc:
                    log.write(f"UV selection restore skipped safely: {exc}")
            else:
                log.write("UV selection restore skipped: stale analysis context")
    finally:
        _ANALYSIS_STATE = None
        _ANALYSIS_EXPECTED_SELECTION = None
        _ANALYSIS_SELECTION_TOUCHED = False
        _ISSUES = []
        _ISSUE_INDEX = -1
        _ISSUE_KIND = None
        _ISSUE_CURRENT = None
        _ISSUE_THRESHOLD = None
        _ISSUE_INITIAL_COUNT = 0


class _UVAnalysisModal:
    """Small timed batches keep Blender responsive on heavy production meshes."""
    analysis_kind = None
    batch_size = 250

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(context.mode == 'EDIT_MESH' and obj and obj.type == 'MESH')

    def _begin(self, context):
        global _ANALYSIS_STATE, _ANALYSIS_RUNNING, _ANALYSIS_TIMER, _ANALYSIS_WM
        global _ANALYSIS_SNAPSHOT_MESH
        if _ANALYSIS_RUNNING:
            self.report({'WARNING'}, "Другой UV-анализ уже выполняется")
            return {'CANCELLED'}
        if issues_active():
            _finish_analysis(context, restore_selection=issues_context_matches(context))
        obj = context.active_object
        editing = getattr(context, 'objects_in_mode_unique_data', None)
        if editing is not None and len(editing) != 1:
            self.report({'ERROR'}, "UV-анализ: выйдите из Multi-Object Edit Mode")
            return {'CANCELLED'}
        if not obj or obj.type != 'MESH':
            self.report({'WARNING'}, "Выберите активный Mesh-объект")
            return {'CANCELLED'}
        if obj.data.uv_layers.active is None:
            self.report({'WARNING'}, "У активного объекта нет UV")
            return {'CANCELLED'}
        try:
            # Synchronize once, then analyse a detached copy. Holding BMFace,
            # BMLoop or the edit BMesh between TIMER events can crash Blender
            # after an undo, mode switch, topology edit or operator redo.
            obj.update_from_editmode()
            source_bm = bmesh.from_edit_mesh(obj.data)
            self._source_signature = (
                len(source_bm.verts), len(source_bm.edges), len(source_bm.faces)
            )
            source_bm.verts.index_update()
            source_bm.edges.index_update()
            source_bm.faces.index_update()
            self._source_content = selection_core.content_fingerprint(
                source_bm, source_bm.loops.layers.uv.active) if source_bm.loops.layers.uv.active else None
            # Independent selection in a visible UV Editor is authoritative.
            # Never silently turn a selected-island analysis into an all-mesh scan.
            from . import uv_tools
            if (not context.tool_settings.use_uv_select_sync and
                    uv_tools._uv_editor_visible(context)):
                layer = source_bm.loops.layers.uv.active
                selected_faces = uv_tools._target_faces(
                    source_bm, "UV-анализ", context, layer, 'UV')
                self._scan_indices = sorted(face.index for face in selected_faces)
                self._selection_scope = 'UV'
            else:
                self._scan_indices = None
                self._selection_scope = 'ALL_MESH'
            snapshot = runtime.checked_mesh_copy(obj.data, "UV-анализ")
            snapshot.name = ".IMA_Analysis_Snapshot"
            if snapshot.uv_layers.active is None:
                bpy.data.meshes.remove(snapshot)
                self.report({'WARNING'}, "У активного объекта нет UV")
                return {'CANCELLED'}
        except (ReferenceError, RuntimeError, ValueError, MemoryError) as exc:
            self.report({'ERROR'}, f"Анализ не запущен: {exc}")
            return {'CANCELLED'}

        try:
            selection_state = _analysis_selection_snapshot(context, obj)
        except (ReferenceError, RuntimeError, ValueError) as exc:
            try:
                bpy.data.meshes.remove(snapshot)
            except (ReferenceError, RuntimeError, TypeError):
                pass
            self.report({'ERROR'}, f"Анализ не запущен: состояние объекта изменилось ({exc})")
            return {'CANCELLED'}

        self._object = obj
        self._source_mesh = obj.data
        self._source_uv_key = selection_core.uv_layer_key(obj.data)
        self._source_uv_layer = obj.data.uv_layers.active.name
        self._mesh = snapshot
        self._uv_data = snapshot.uv_layers.active.data
        self._flip_states = None
        self._cursor = 0
        self._found = []
        self._unverified_faces = 0
        self._total = (len(snapshot.polygons) if self._scan_indices is None
                       else len(self._scan_indices))
        if self._total == 0:
            try:
                bpy.data.meshes.remove(snapshot)
            except (ReferenceError, RuntimeError, TypeError):
                pass
            self.report({'INFO'}, "В объекте нет полигонов")
            return {'CANCELLED'}

        _ANALYSIS_STATE = selection_state
        _ANALYSIS_RUNNING = True
        _ANALYSIS_SNAPSHOT_MESH = snapshot
        try:
            self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
            _ANALYSIS_TIMER = self._timer
            _ANALYSIS_WM = context.window_manager
            context.window_manager.modal_handler_add(self)
            context.window_manager.progress_begin(0, self._total)
        except (ReferenceError, RuntimeError, ValueError) as exc:
            _stop_analysis_timer()
            _finish_analysis(context, restore_selection=False)
            self.report({'ERROR'}, f"Анализ не запущен: не удалось создать безопасный таймер ({exc})")
            return {'CANCELLED'}
        log.write(
            f"UV {self.analysis_kind.lower()} started: object={obj.name}; "
            f"scope={self._selection_scope}; faces={self._total}; "
            f"uv_identity={self._source_uv_key!r}; "
            f"uv_editor_visible={uv_tools._uv_editor_visible(context)}; "
            f"uv_sync={bool(context.tool_settings.use_uv_select_sync)}")
        return {'RUNNING_MODAL'}

    def _source_is_safe(self, context):
        """Use fresh, short-lived BMesh access only to validate the source."""
        if not _ANALYSIS_RUNNING or _ANALYSIS_TIMER is not self._timer:
            return False, "анализ уже остановлен"
        try:
            obj = self._object
            if context.mode != 'EDIT_MESH':
                return False, "пользователь вышел из Edit Mode"
            if context.active_object != obj or obj.type != 'MESH':
                return False, "изменён активный объект"
            if obj.data != self._source_mesh:
                return False, "сетка объекта была заменена"
            if not selection_core.uv_layer_matches(obj.data, self._source_uv_key):
                current_uv_key = selection_core.uv_layer_key(obj.data)
                return False, ("активная UV-карта изменилась: "
                               f"expected={self._source_uv_key!r}, "
                               f"actual={current_uv_key!r}")
            bm = bmesh.from_edit_mesh(obj.data)
            live_edit_uv = bm.loops.layers.uv.active
            if live_edit_uv is None or live_edit_uv.name != self._source_uv_layer:
                return False, "активная UV-карта Edit Mesh изменилась"
            signature = (len(bm.verts), len(bm.edges), len(bm.faces))
            if signature != self._source_signature:
                return False, "топология была изменена"
            return True, ""
        except (ReferenceError, RuntimeError, ValueError):
            return False, "редактируемая геометрия больше недоступна"

    def invoke(self, context, event):
        return self._begin(context)

    def execute(self, context):
        return self._begin(context)

    def _owns_runtime(self):
        return (_ANALYSIS_RUNNING and
                _ANALYSIS_TIMER is getattr(self, "_timer", None))

    def _cleanup_timer(self, context):
        # A delayed modal callback from an older run must not stop a newer run.
        if self._owns_runtime():
            _stop_analysis_timer()
        self._timer = None

    def cancel(self, context):
        self._cleanup_timer(context)
        _finish_analysis(context, restore_selection=False)
        log.write(f"UV {self.analysis_kind.lower()} cancelled")

    def modal(self, context, event):
        # Undo/load/unregister can remove the timer before the modal handler
        # receives its final event. Leave immediately without touching a
        # possible newer analysis run.
        if not self._owns_runtime():
            self._timer = None
            return {'CANCELLED'}
        if event.type == 'ESC':
            self.cancel(context)
            self.report({'INFO'}, "Анализ отменён")
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}
        try:
            safe, reason = self._source_is_safe(context)
            if not safe:
                self._cleanup_timer(context)
                _finish_analysis(context, restore_selection=False)
                log.write(f"UV {self.analysis_kind.lower()} safely stopped: {reason}")
                self.report({'ERROR'}, f"Анализ безопасно остановлен: {reason}")
                return {'CANCELLED'}
            stop = min(self._cursor + self.batch_size, self._total)
            for index in range(self._cursor, stop):
                polygon_index = (index if self._scan_indices is None
                                 else self._scan_indices[index])
                polygon = self._mesh.polygons[polygon_index]
                if self._matches(polygon):
                    self._found.append(polygon.index)
            self._cursor = stop
            context.window_manager.progress_update(self._cursor)
            if self._cursor < self._total:
                return {'RUNNING_MODAL'}
            current_bm = bmesh.from_edit_mesh(self._object.data)
            current_bm.verts.index_update()
            current_bm.edges.index_update()
            current_bm.faces.index_update()
            active_layer = current_bm.loops.layers.uv.active
            if (active_layer is None or
                    selection_core.content_fingerprint(current_bm, active_layer)
                    != self._source_content):
                raise RuntimeError(
                    "Геометрия или UV изменились во время поиска — результаты отменены")
            self._cleanup_timer(context)
            if not self._found:
                _finish_analysis(context, restore_selection=False)
                log.write(f"UV {self.analysis_kind.lower()}: none; faces={self._total}")
                message = "Перевёрнутых UV-граней не найдено" if self.analysis_kind == 'FLIPPED' else "Проблемных граней не найдено"
                if self.analysis_kind == 'FLIPPED' and self._unverified_faces:
                    message += f"; НЕ ПРОВЕРЕНО вырожденных/перекрученных UV-контуров: {self._unverified_faces}"
                self.report({'WARNING'} if self.analysis_kind == 'FLIPPED' and self._unverified_faces
                            else {'INFO'}, message)
                return {'FINISHED'}
            affected = [(self._object, set(self._found))]
            threshold = self.threshold if self.analysis_kind == 'STRETCH' else None
            _store_issues(self.analysis_kind, affected, threshold=threshold)
            # Shared immutable UV + 3D feedback. No selection changes.
            uv_feedback.publish(context, self._object, self.analysis_kind, self._found)
            log.write(f"UV {self.analysis_kind.lower()} preview ready: faces={len(self._found)}; selection_unchanged=true")
            self.report({'INFO'},
                        f"Найдено {len(self._found)} проблем; "
                        + (f"НЕ ПРОВЕРЕНО вырожденных/перекрученных UV-контуров: {self._unverified_faces}. "
                           if self.analysis_kind == 'FLIPPED' and self._unverified_faces else '')
                        + "Исходное выделение сохранено. Используйте стрелки для просмотра")
            return {'FINISHED'}
        except Exception as exc:
            self._cleanup_timer(context)
            _finish_analysis(context, restore_selection=False)
            log.write_exception(f"UV {self.analysis_kind.lower()} failed", exc)
            self.report({'ERROR'}, f"Анализ остановлен: {exc}")
            return {'CANCELLED'}


class IMA_OT_SelectStretch(_UVAnalysisModal, Operator):
    bl_idname = "ima.select_stretch"
    bl_label = "Найти потяги"
    bl_description = "Найти растяжения UV-развёртки (Stretch). Найденные области подсвечиваются, исходное выделение не меняется."
    # Read-only analysis must NOT push Blender's history; no redo invocation.
    bl_options = set()
    analysis_kind = 'STRETCH'

    threshold: bpy.props.FloatProperty(
        name="Допустимое отклонение",
        description="Максимальная разница между углом полигона в 3D и UV",
        default=math.radians(20.0), min=math.radians(0.1),
        max=math.radians(90.0), subtype='ANGLE', unit='ROTATION',
    )

    def _matches(self, polygon):
        # MSI checks triangles and quads. N-gons are skipped because a single
        # corner comparison is unreliable for concave topology.
        count = polygon.loop_total
        if count not in {3, 4}:
            return False
        loops = self._mesh.loops
        vertices = self._mesh.vertices
        start = polygon.loop_start
        count = polygon.loop_total
        threshold = self.threshold
        for offset in range(count):
            prev_loop = loops[start + ((offset - 1) % count)]
            loop = loops[start + offset]
            next_loop = loops[start + ((offset + 1) % count)]
            center = vertices[loop.vertex_index].co
            a3 = vertices[prev_loop.vertex_index].co - center
            b3 = vertices[next_loop.vertex_index].co - center
            center_uv = self._uv_data[start + offset].uv
            auv = self._uv_data[start + ((offset - 1) % count)].uv - center_uv
            buv = self._uv_data[start + ((offset + 1) % count)].uv - center_uv
            angle_3d = _angle(a3, b3)
            angle_uv = _angle(auv, buv)
            if angle_3d is None or angle_uv is None or abs(angle_3d - angle_uv) > threshold:
                return True
        return False


class IMA_OT_SelectFlipped(_UVAnalysisModal, Operator):
    bl_idname = "ima.select_flipped"
    bl_label = "Найти Flip-полигоны"
    bl_description = ("Найти UV-полигоны с обратным направлением обхода — "
                      "включая квады. Не изменяет геометрию и UV.")
    bl_options = set()
    analysis_kind = 'FLIPPED'

    def _matches(self, polygon):
        start = polygon.loop_start
        coords = [self._uv_data[start + offset].uv
                  for offset in range(polygon.loop_total)]
        if self._flip_states is None:self._flip_states=mesh_uv_states(self._mesh)
        state = self._flip_states.get(polygon.index,'UNVERIFIED')
        if state in {'DEGENERATE', 'UNVERIFIED'}:
            self._unverified_faces += 1
        return state == 'FLIPPED'


def _live_issue_matches(face, layer, kind, threshold):
    """Read only the current Edit BMesh; never silently turn read errors into False."""
    if kind == 'FLIPPED':
        state = bmesh_face_uv_state(face,layer)
        if state in {'DEGENERATE', 'UNVERIFIED'}:
            raise ValueError('UV-контур вырожден или перекрещен; Flip не подтверждён')
        return state == 'FLIPPED'
    if kind != 'STRETCH':
        raise ValueError('неизвестный тип UV-анализа: %r' % kind)
    # The scanner deliberately skips N-gons. Its live validator must agree.
    if len(face.loops) not in (3, 4):
        return False
    limit = threshold if threshold is not None else math.radians(20.0)
    for loop in face.loops:
        previous = loop.link_loop_prev
        following = loop.link_loop_next
        a3 = previous.vert.co - loop.vert.co
        b3 = following.vert.co - loop.vert.co
        uv = loop[layer].uv
        auv = previous[layer].uv - uv
        buv = following[layer].uv - uv
        a = _angle(a3, b3)
        b = _angle(auv, buv)
        if a is None or b is None or abs(a - b) > limit:
            return True
    return False


def _verify_live_issues(context):
    """One BMesh read and one topology check for ALL stored issues.

    Returns status, current issue list, examined count, reason, bm, uv_layer.
    On an error, callers must retain all old issues and the user's selection.
    """
    if not _ISSUES:
        return ('NO_SESSION', [], 0, 'Сначала выполните анализ UV', None, None)
    if not issues_context_matches(context):
        return ('STALE', list(_ISSUES), 0, 'Объект, Mesh или UV-карта изменились', None, None)
    try:
        obj = context.active_object
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        layer = bm.loops.layers.uv.active
        if layer is None:
            return ('STALE', list(_ISSUES), 0, 'Активная UV-карта недоступна', None, None)
        if selection_core.topology_fingerprint(bm) != _ANALYSIS_STATE['topology']:
            return ('STALE', list(_ISSUES), 0, 'Топология изменилась; повторите анализ', None, None)
        def verify(item):
            item_obj, index = item
            if item_obj != obj:
                raise ValueError('полигон принадлежит другому объекту')
            if index < 0 or index >= len(bm.faces):
                raise IndexError('индекс полигона вне актуальной топологии')
            return _live_issue_matches(bm.faces[index], layer, _ISSUE_KIND, _ISSUE_THRESHOLD)
        status, survivors, checked, reason = issue_nav_core.verify_issues(
            _ISSUES, verify)
        return status, survivors, checked, reason, bm, layer
    except Exception as exc:
        return ('UNREADABLE', list(_ISSUES), 0,
                '%s: %s' % (type(exc).__name__, str(exc)[:240]), None, None)


def _focus_uv_issue(context, obj, face, uv_layer):
    """Change only editor view/cursor (cursor restored), never selection."""
    face_uvs = [loop[uv_layer].uv.copy() for loop in face.loops]
    if not face_uvs:
        return 'NONE'
    uv_center = (sum(p.x for p in face_uvs) / len(face_uvs),
                 sum(p.y for p in face_uvs) / len(face_uvs))
    # Prefer the calling editor, then a visible UV editor in the same window.
    areas = [context.area] if context.area else []
    try:
        if context.window is not None:
            areas.extend(area for area in context.window.screen.areas
                         if area != context.area and area.type == 'IMAGE_EDITOR')
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    for area in areas:
        if area is None:
            continue
        if area.type == 'IMAGE_EDITOR':
            space = area.spaces.active
            if getattr(space, 'ui_mode', 'UV') != 'UV':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            # Blender's image.view_center_cursor centers without touching UV
            # selection. Restore user's UV cursor even when Blender rejects op.
            original_cursor = tuple(space.cursor_location)
            try:
                space.cursor_location = uv_center
                with bpy.context.temp_override(window=context.window, area=area, region=region):
                    result = bpy.ops.image.view_center_cursor()
                return 'UV' if 'FINISHED' in result else 'UV_UNAVAILABLE'
            except (AttributeError, RuntimeError, ValueError, TypeError) as exc:
                log.write('UV focus unavailable: %s' % exc)
                return 'UV_UNAVAILABLE'
            finally:
                try:
                    space.cursor_location = original_cursor
                except (ReferenceError, RuntimeError, AttributeError):
                    pass
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            if getattr(space, 'region_3d', None) is not None:
                space.region_3d.view_location = obj.matrix_world @ face.calc_center_median()
                area.tag_redraw()
                return '3D'
    return 'NO_EDITOR'


class IMA_OT_IssueNavigate(Operator):
    bl_idname = 'ima.issue_navigate'
    bl_label = 'Проблемный полигон'
    bl_description = 'Перейти к найденной UV-проблеме, не меняя выделение'

    @classmethod
    def description(cls, context, properties):
        from . import tooltip_text
        return tooltip_text.issue_navigation(getattr(properties, 'direction', 1))
    # View/cursor changes should not push an Undo step that destroys _ISSUES.
    bl_options = {'REGISTER'}

    direction: bpy.props.IntProperty(default=1, min=-1, max=1)

    def execute(self, context):
        global _ISSUES, _ISSUE_INDEX, _ISSUE_CURRENT
        status, remaining, checked, reason, bm, layer = _verify_live_issues(context)
        sid = _ISSUE_SESSION_ID
        if status in {'NO_SESSION', 'STALE', 'UNREADABLE'}:
            _set_navigation_status(status, reason)
            log.write('UV NAV sid=%s action=ARROW result=%s checked=%d stored=%d reason=%s'
                      % (sid, status, checked, len(_ISSUES), reason))
            self.report({'WARNING'}, reason or 'Проверка UV недоступна; повторите анализ')
            return {'CANCELLED'}
        if status == 'RESOLVED':
            originally_found = _ISSUE_INITIAL_COUNT
            log.write('UV NAV sid=%s action=ARROW result=RESOLVED checked=%d stored=%d initial=%d'
                      % (sid, checked, len(_ISSUES), originally_found))
            _finish_analysis(context, restore_selection=True)
            _set_navigation_status('RESOLVED', 'Все %d найденных проблем проверены и устранены' % originally_found)
            self.report({'INFO'}, 'Проверено: все %d найденных UV-проблем устранены' % originally_found)
            return {'FINISHED'}
        old_issues = list(_ISSUES)
        issue = issue_nav_core.next_issue(old_issues, remaining, _ISSUE_CURRENT,
                                          self.direction)
        if issue is None:
            _set_navigation_status('UNREADABLE', 'Невозможно выбрать найденную UV-проблему')
            return {'CANCELLED'}
        try:
            focus = _focus_uv_issue(context, issue[0], bm.faces[issue[1]], layer)
        except Exception as exc:
            # A focus failure is an informational UI limitation, never UV repair.
            focus = 'ERROR'
            log.write('UV NAV focus failed: %s: %s' % (type(exc).__name__, exc))
        _ISSUES = remaining
        _ISSUE_CURRENT = issue
        _ISSUE_INDEX = remaining.index(issue)
        # Republish updated contours after verified UV edits. An overlay is not
        # authoritative: even if GPU is absent, navigation must still work.
        try:
            if (uv_feedback.status()['state'] in {'STALE', 'EMPTY'} or
                    uv_feedback.status()['total'] != len(remaining)):
                uv_feedback.publish(context, issue[0], _ISSUE_KIND,
                                    [idx for _, idx in remaining])
            uv_feedback.focus_face(
                issue[1], [(l[layer].uv.x, l[layer].uv.y)
                           for l in bm.faces[issue[1]].loops],
                [tuple(l.vert.co) for l in bm.faces[issue[1]].loops])
        except Exception as exc:
            log.write('UV NAV overlay unavailable: %s' % exc)
        _set_navigation_status('VALID')
        log.write('UV NAV sid=%s action=ARROW result=VALID kind=%s face=%d position=%d/%d checked=%d focus=%s selection_changed=false'
                  % (sid, _ISSUE_KIND, issue[1], _ISSUE_INDEX + 1,
                     len(remaining), checked, focus))
        self.report({'INFO'}, 'Проблема %d из %d (полигон %d)'
                    % (_ISSUE_INDEX + 1, len(remaining), issue[1]))
        return {'FINISHED'}


class IMA_OT_SelectAllIssues(Operator):
    bl_idname = 'ima.select_all_issues'
    bl_label = 'Выделить найденные UV-проблемы'
    bl_description = 'Выделить найденные UV-проблемы. В отличие от стрелок, эта команда изменяет выделение.'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _ISSUES
        status, remaining, checked, reason, _bm, _layer = _verify_live_issues(context)
        if status in {'NO_SESSION', 'STALE', 'UNREADABLE'}:
            _set_navigation_status(status, reason)
            log.write('UV NAV sid=%s action=SELECT_ALL result=%s checked=%d stored=%d reason=%s'
                      % (_ISSUE_SESSION_ID, status, checked, len(_ISSUES), reason))
            self.report({'WARNING'}, reason or 'Проверка недоступна')
            return {'CANCELLED'}
        if status == 'RESOLVED':
            log.write('UV NAV sid=%s action=SELECT_ALL result=RESOLVED checked=%d'
                      % (_ISSUE_SESSION_ID, checked))
            _finish_analysis(context, restore_selection=True)
            _set_navigation_status('RESOLVED', 'Проверено: все найденные проблемы устранены')
            self.report({'INFO'}, 'Все найденные UV-проблемы действительно исправлены')
            return {'FINISHED'}
        try:
            _apply_issue_selection(context, remaining[0][0], {i for _, i in remaining})
        except (ReferenceError, RuntimeError, ValueError) as exc:
            _set_navigation_status('SELECTION_BLOCKED', str(exc))
            log.write('UV NAV sid=%s action=SELECT_ALL result=BLOCKED reason=%s'
                      % (_ISSUE_SESSION_ID, exc))
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        _ISSUES = remaining
        _set_navigation_status('VALID')
        log.write('UV NAV sid=%s action=SELECT_ALL result=VALID count=%d'
                  % (_ISSUE_SESSION_ID, len(remaining)))
        self.report({'INFO'}, 'Выделено по команде: %d проблем' % len(remaining))
        return {'FINISHED'}


def _issue_exit_requires_undo(context):
    """A read-only review must not become an Undo step.

    Only an explicit selection action that can safely be restored needs the
    separate undoable operator. No reads/writes are performed in a GPU callback.
    """
    if not _ANALYSIS_SELECTION_TOUCHED or _ANALYSIS_STATE is None:
        return False
    if not issues_context_matches(context):
        return False
    try:
        return _analysis_live_selection_digest(context) == _ANALYSIS_EXPECTED_SELECTION
    except (ReferenceError, RuntimeError, ValueError, AttributeError):
        return False


class IMA_OT_ExitAnalysisRestore(Operator):
    bl_idname = 'ima.exit_analysis_restore'
    bl_label = 'Восстановить выделение после UV-анализа'
    bl_description = 'Undoable restoration only after explicit Select Issues'
    bl_options = {'INTERNAL', 'UNDO'}

    def execute(self, context):
        if not issues_active() or not _issue_exit_requires_undo(context):
            return {'CANCELLED'}
        _finish_analysis(context, restore_selection=True)
        log.write('UV analysis selection restored using one Undo step')
        return {'FINISHED'}


class IMA_OT_ExitAnalysis(Operator):
    bl_idname = "ima.exit_analysis"
    bl_label = "Выйти из просмотра"
    bl_description = "Закрыть просмотр UV-проблем и убрать подсветку. Если ранее выбирались найденные полигоны, восстановить выделение безопасно."
    # Closing a read-only preview must not consume the user's next Ctrl+Z.
    bl_options = {'REGISTER'}

    def execute(self, context):
        if not issues_active():
            return {'CANCELLED'}
        if discard_stale_issues(context):
            self.report({'INFO'}, "Старый анализ другого объекта закрыт без изменения выделения")
            return {'FINISHED'}
        if _issue_exit_requires_undo(context):
            try:
                result = bpy.ops.ima.exit_analysis_restore()
                if 'FINISHED' not in result:
                    self.report({'WARNING'}, 'Восстановление выделения не выполнено')
                    return {'CANCELLED'}
            except (ReferenceError, RuntimeError, ValueError) as exc:
                self.report({'ERROR'}, 'Восстановление выделения недоступно: %s' % exc)
                return {'CANCELLED'}
        else:
            _finish_analysis(context, restore_selection=False)
        log.write("UV issue viewing finished")
        self.report({'INFO'}, "Просмотр завершён")
        return {'FINISHED'}


CLASSES = (
    IMA_OT_ToggleUCX, IMA_OT_ViewMode, IMA_OT_RestoreView,
    IMA_OT_ToggleBorders, IMA_OT_SelectStretch, IMA_OT_SelectFlipped,
    IMA_OT_IssueNavigate, IMA_OT_SelectAllIssues, IMA_OT_ExitAnalysisRestore, IMA_OT_ExitAnalysis,
)


def register():
    try:
        bpy.types.Scene.ima_view_mode_state = bpy.props.EnumProperty(
            name="ISSMAKER View Mode",
            items=(
                ('RESTORE', "Исходный", ""), ('usual', "Обычный", ""),
                ('normals', "Нормали", ""), ('zfight', "Z-fighting", ""),
                ('night', "Ночь", ""), ('ucx', "Коллизии", ""),
                ('ucx2', "Коллизии 2", ""), ('textured', "Текстуры", ""),
                ('materials', "Материалы", ""),
            ),
            default='RESTORE',
        )
        runtime.register_classes_atomic(CLASSES)
        for handler_name, callback in (
                ('undo_pre', _undo_pre), ('undo_post', _undo_post),
                ('redo_pre', _redo_pre), ('redo_post', _redo_post),
                ('load_pre', _load_pre)):
            handlers = getattr(bpy.app.handlers, handler_name, None)
            if handlers is None:
                continue
            # Blender hot reloading can leave an old function in persistent lists.
            for old in tuple(handlers):
                if (getattr(old, '__module__', None) == __name__
                        and getattr(old, '__name__', None) == callback.__name__):
                    handlers.remove(old)
            handlers.append(callback)
        if not bpy.app.timers.is_registered(_clear_stale_status_text):
            bpy.app.timers.register(_clear_stale_status_text, first_interval=0.1)
    except Exception:
        unregister()
        raise


def unregister():
    try:
        if bpy.app.timers.is_registered(_clear_stale_status_text):
            bpy.app.timers.unregister(_clear_stale_status_text)
    except (ReferenceError, RuntimeError, ValueError):
        pass
    _clear_stale_status_text()
    for handler_name, callback in (
            ('undo_pre', _undo_pre), ('undo_post', _undo_post),
            ('redo_pre', _redo_pre), ('redo_post', _redo_post),
            ('load_pre', _load_pre)):
        handlers = getattr(bpy.app.handlers, handler_name, None)
        if handlers is not None:
            for old in tuple(handlers):
                if (getattr(old, '__module__', None) == __name__
                        and getattr(old, '__name__', None) == callback.__name__):
                    handlers.remove(old)
    history_guard.reset()
    runtime.unregister_classes_safe(CLASSES)
    if hasattr(bpy.types.Scene, "ima_view_mode_state"):
        del bpy.types.Scene.ima_view_mode_state
