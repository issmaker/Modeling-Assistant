import bpy
import bmesh
from math import floor, isfinite, radians
from bpy.app.handlers import persistent
from bpy.types import Operator

from . import log, runtime, selection_core, weld_preview
from .texture_selection import TextureSampler, colors_match, default_image_name


_LINKED_OPERATOR = None
_LINKED_TIMER = None
_LINKED_WM = None
_LINKED_WINDOW_PTR = None
_LINKED_AREA_PTR = None
_LINKED_REGION_PTR = None

# Fail before applying any new selection if a click would flood too much geometry.
MAX_QUICK_LINKED_VISITS = 750000


def linked_active():
    return _LINKED_OPERATOR is not None


def _find_linked_region():
    """Resolve current RNA objects from integer identities; never retain Area/Region."""
    if not (_LINKED_WINDOW_PTR and _LINKED_AREA_PTR and _LINKED_REGION_PTR):
        return None
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return None
    try:
        for window in wm.windows:
            if window.as_pointer() != _LINKED_WINDOW_PTR:
                continue
            screen = window.screen
            for area in screen.areas:
                if area.as_pointer() != _LINKED_AREA_PTR or area.type != 'VIEW_3D':
                    continue
                for region in area.regions:
                    if (region.as_pointer() == _LINKED_REGION_PTR and
                            region.type == 'WINDOW'):
                        return window, screen, area, region
    except (ReferenceError, RuntimeError):
        return None
    return None


def stop_linked_runtime():
    global _LINKED_OPERATOR, _LINKED_TIMER, _LINKED_WM
    global _LINKED_WINDOW_PTR, _LINKED_AREA_PTR, _LINKED_REGION_PTR
    resolved = _find_linked_region()
    if _LINKED_TIMER is not None and _LINKED_WM is not None:
        try:
            _LINKED_WM.event_timer_remove(_LINKED_TIMER)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    if _LINKED_OPERATOR is not None:
        # The next TIMER event must not apply an unfinished Shift-click after
        # the user has explicitly stopped Quick Select (Esc, Undo, context loss).
        _LINKED_OPERATOR._texture_sampler = None
        _LINKED_OPERATOR._before_faces = None
        _LINKED_OPERATOR._pending = False
        _LINKED_OPERATOR._after_click_signature = None
    _LINKED_OPERATOR = None
    _LINKED_TIMER = None
    _LINKED_WM = None
    _LINKED_WINDOW_PTR = None
    _LINKED_AREA_PTR = None
    _LINKED_REGION_PTR = None
    if resolved is not None:
        try:
            resolved[2].tag_redraw()
        except ReferenceError:
            pass


@persistent
def _linked_undo_pre(_dummy):
    stop_linked_runtime()


@persistent
def _linked_redo_pre(_dummy):
    # Ctrl+Shift+Z can rebuild Edit BMesh just like Ctrl+Z.
    stop_linked_runtime()


@persistent
def _linked_load_pre(_dummy):
    stop_linked_runtime()


def _active_edit_mesh(context):
    obj = context.active_object
    if (context.mode != 'EDIT_MESH' or obj is None or obj.type != 'MESH' or
            obj.library is not None or obj.data.library is not None):
        return None
    return obj


class IMA_OT_Merge002(Operator):
    bl_idname = "ima.merge_002"
    bl_label = "Weld · Проверить совпадения"
    bl_description = "Найти вершины для Weld: выделить только близкие пары и показать места сшивки. Геометрия пока не изменяется."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_edit_mesh(context) is not None

    def execute(self, context):
        if linked_active():
            stop_linked_runtime()
            log.write('Quick Linked stopped before Weld preflight')
        return weld_preview.begin(context, self)


class IMA_OT_FlipNormals(Operator):
    bl_idname = "ima.flip_normals"
    bl_label = "Flip Normals"
    bl_description = "Развернуть нормали выбранных полигонов (Flip Normals). Изменение можно отменить Ctrl+Z."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_edit_mesh(context) is not None

    def execute(self, context):
        key = "flip_normals"
        if not runtime.acquire(key):
            self.report({'WARNING'}, "Операция уже выполняется")
            return {'CANCELLED'}
        transaction = None
        try:
            obj = _active_edit_mesh(context)
            if obj is None:
                self.report({'ERROR'}, "Объект больше не находится в Edit Mode")
                return {'CANCELLED'}
            bm = bmesh.from_edit_mesh(obj.data)
            faces = [face for face in bm.faces if face.select]
            if not faces:
                self.report({'WARNING'}, "Выделите полигоны")
                return {'CANCELLED'}
            transaction = runtime.EditMeshTransaction(context, obj, "Flip Normals")
            try:
                bmesh.ops.reverse_faces(bm, faces=faces, flip_multires=True)
            except TypeError:
                # Compatibility fallback for Blender versions without this flag.
                bmesh.ops.reverse_faces(bm, faces=faces)
            bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
            transaction.commit()
            log.write(f"Flip normals: object={obj.name}; faces={len(faces)}")
            self.report({'INFO'}, f"Нормали развёрнуты: {len(faces)}")
            return {'FINISHED'}
        except Exception as exc:
            log.write_exception("Flip normals failed", exc)
            restored = transaction.rollback() if transaction is not None else False
            suffix = "; исходная сетка восстановлена" if restored else ""
            if transaction is not None and transaction.rollback_error is not None:
                log.write_exception("Flip normals rollback failed", transaction.rollback_error)
                suffix = "; АВАРИЯ ОТКАТА — сохраните файл под новым именем"
            self.report({'ERROR'}, f"Flip Normals остановлен: {exc}{suffix}")
            return {'CANCELLED'}
        finally:
            runtime.release(key)


def _face_udim(face, uv_layer):
    """Use the UV vertex mean; boundary-spanning faces belong to one tile."""
    count = len(face.loops)
    if not count:
        return None
    u = sum(loop[uv_layer].uv.x for loop in face.loops) / count
    v = sum(loop[uv_layer].uv.y for loop in face.loops) / count
    if not (isfinite(u) and isfinite(v)):
        return None
    column, row = floor(u), floor(v)
    if not (0 <= column < 10 and row >= 0):
        return None
    return 1001 + column + 10 * row


def _uv_edge_matches(face, other, edge, uv_layer):
    for vert in edge.verts:
        a = next(loop[uv_layer].uv for loop in face.loops if loop.vert == vert)
        b = next(loop[uv_layer].uv for loop in other.loops if loop.vert == vert)
        if a != b:
            return False
    return True


def _expand_udim_selection(bm, before, delimit, sampler=None, tolerance=0.05,
                           normal_angle=radians(35.0)):
    """Seed-locked selection: only newly clicked faces may start expansion."""
    use_uv = bool({'UV', 'UDIM', 'TEXTURE_COLOR'} & delimit)
    uv_layer = bm.loops.layers.uv.active if use_uv else None
    if use_uv and uv_layer is None:
        raise RuntimeError("Для выбранных ограничений нужна активная UV-карта")
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    seeds = [face for face in bm.faces
             if face.select and not face.hide and face.index not in before]
    tiles = {}

    def tile(face):
        if face.index not in tiles:
            tiles[face.index] = _face_udim(face, uv_layer)
        return tiles[face.index]

    colors = {}

    def color(face):
        if face.index not in colors:
            colors[face.index] = sampler.face_color(face, uv_layer)
        return colors[face.index]

    use_udim = 'UDIM' in delimit
    use_color = 'TEXTURE_COLOR' in delimit
    if use_color and sampler is None:
        raise RuntimeError("Не выбран цветовой атлас")
    selected = set()
    visited_count = 0
    for seed in seeds:
        target = tile(seed) if use_udim else None
        reference = color(seed) if use_color else None
        if (use_udim and target is None) or (use_color and reference is None):
            continue
        visited = {seed.index}
        stack = [seed]
        while stack:
            face = stack.pop()
            visited_count += 1
            if visited_count > MAX_QUICK_LINKED_VISITS:
                raise RuntimeError(
                    "Быстрое выделение отменено: слишком большая связанная область")
            selected.add(face.index)
            for edge in face.edges:
                if edge.hide:
                    continue
                if 'SEAM' in delimit and edge.seam:
                    continue
                if 'SHARP' in delimit and not edge.smooth:
                    continue
                for other in edge.link_faces:
                    if other.hide or other.index in visited:
                        continue
                    if 'NORMAL' in delimit:
                        if (face.normal.length_squared < 1e-20 or
                                other.normal.length_squared < 1e-20 or
                                face.normal.angle(other.normal) > normal_angle):
                            continue
                    if use_udim and tile(other) != target:
                        continue
                    # Compare to the clicked face, preventing drift along gradients.
                    if use_color and not colors_match(color(other), reference, tolerance):
                        continue
                    if 'MATERIAL' in delimit and face.material_index != other.material_index:
                        continue
                    if 'UV' in delimit and not _uv_edge_matches(face, other, edge, uv_layer):
                        continue
                    visited.add(other.index)
                    stack.append(other)
    for index in selected:
        bm.faces[index].select_set(True)
    bm.select_flush_mode()


class IMA_OT_AutoSelectLinkedWorker(Operator):
    """Internal modal worker; never used as a viewport gizmo target."""
    bl_idname = "ima.auto_select_linked_worker"
    bl_label = "Select Linked Worker"

    normal_angle: bpy.props.FloatProperty(
        name="Угол Normal", description="Максимальный угол перехода между гранями",
        default=radians(35), min=0.0, max=radians(180), subtype='ANGLE', unit='ROTATION')

    texture_image: bpy.props.StringProperty(name="Атлас", description="Изображение для сравнения цвета по активной UV-карте")
    color_tolerance: bpy.props.FloatProperty(
        name="Допуск цвета", description="Максимальная разница RGB с цветом нажатого полигона; 0 — точное совпадение",
        default=0.05, min=0.0, max=1.0, precision=3, step=1)
    texture_wrap: bpy.props.EnumProperty(
        name="За пределами UV 0–1",
        items=(('REPEAT', "Repeat", "Повторять атлас"),
               ('EXTEND', "Extend", "Использовать край изображения")), default='REPEAT')

    delimit: bpy.props.EnumProperty(
        name="Delimit",
        description="Границы, через которые Select Linked не должен переходить",
        items=(
            ('NORMAL', "Normal", "Ограничивать по направлению нормалей"),
            ('MATERIAL', "Material", "Ограничивать по материалам"),
            ('SEAM', "Seam", "Ограничивать по UV-швам"),
            ('SHARP', "Sharp", "Ограничивать по Sharp-рёбрам"),
            ('UV', "UV", "Ограничивать по UV-островам"),
            ('UDIM', "UDIM", "Останавливать Select Linked на границе UDIM"),
            ('TEXTURE_COLOR', "Texture Color", "Ограничивать по цвету атласа в центре UV-полигона"),
        ),
        options={'ENUM_FLAG'},
        default={'SEAM'},
    )

    @classmethod
    def poll(cls, context):
        return _active_edit_mesh(context) is not None

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        global _LINKED_OPERATOR, _LINKED_TIMER, _LINKED_WM
        global _LINKED_WINDOW_PTR, _LINKED_AREA_PTR, _LINKED_REGION_PTR
        # All entry points (radial, N panel, direct operator) share the same
        # stop-before-preflight contract. Import lazily to avoid a module cycle.
        from .core import active_tools
        handoff_ok, handoff_reason = active_tools.switch_to('QUICK_SELECT', context)
        if not handoff_ok:
            self.report({'WARNING'}, 'Быстрое выделение: ' + handoff_reason[:170])
            return {'CANCELLED'}
        if linked_active():
            self.report({'WARNING'}, "Режим уже включён")
            return {'CANCELLED'}
        obj = _active_edit_mesh(context)
        if obj is None:
            return {'CANCELLED'}
        if not context.window or not context.area or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Не найден активный 3D Viewport")
            return {'CANCELLED'}
        # Popup/dialog invocations often execute from a UI region rather than
        # WINDOW. Resolve WINDOW explicitly; otherwise the modal worker looks
        # active but cannot ever observe viewport clicks.
        window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if window_region is None:
            self.report({'ERROR'}, "Не найдено окно 3D Viewport")
            return {'CANCELLED'}
        if not context.tool_settings.mesh_select_mode[2]:
            self.report({'WARNING'}, "Быстрое выделение: сначала включите Face Select")
            return {'CANCELLED'}
        self._texture_sampler = None
        if bool({'UV', 'UDIM', 'TEXTURE_COLOR'} & set(self.delimit)):
            bm = bmesh.from_edit_mesh(obj.data)
            if bm.loops.layers.uv.active is None:
                self.report({'WARNING'}, "Для UV / UDIM / Texture Color нужна активная UV-карта")
                return {'CANCELLED'}
        if 'TEXTURE_COLOR' in self.delimit:
            try:
                self._texture_sampler = TextureSampler(
                    bpy.data.images.get(self.texture_image), self.texture_wrap)
            except (RuntimeError, ValueError, MemoryError) as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
        self._before_faces = None
        self._session = selection_core.snapshot_edit_session(
            context, obj, require_uv=bool({'UV', 'UDIM', 'TEXTURE_COLOR'} & set(self.delimit)))
        self._object_name = obj.name_full
        self._mesh_ptr = obj.data.as_pointer()
        self._pending = False
        self._after_click_signature = None
        try:
            self._timer = context.window_manager.event_timer_add(
                0.08, window=context.window)
        except (ReferenceError, RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, f"Не удалось включить режим: {exc}")
            return {'CANCELLED'}
        _LINKED_OPERATOR = self
        _LINKED_TIMER = self._timer
        _LINKED_WM = context.window_manager
        _LINKED_WINDOW_PTR = context.window.as_pointer()
        _LINKED_AREA_PTR = context.area.as_pointer()
        _LINKED_REGION_PTR = window_region.as_pointer()
        try:
            context.window_manager.modal_handler_add(self)
            context.area.tag_redraw()
        except (ReferenceError, RuntimeError, ValueError) as exc:
            stop_linked_runtime()
            self.report({'ERROR'}, f"Не удалось включить режим: {exc}")
            return {'CANCELLED'}
        log.write(
            f"Quick selection enabled: object={obj.name}; delimit={sorted(self.delimit)}; "
            f"area={_LINKED_AREA_PTR}; region_window={_LINKED_REGION_PTR}")
        self.report({'INFO'}, "Быстрое выделение включено — Shift + клик")
        return {'RUNNING_MODAL'}

    def _owns_runtime(self):
        return (_LINKED_OPERATOR is self and
                _LINKED_TIMER is getattr(self, "_timer", None))

    def _finish(self, message):
        if self._owns_runtime():
            stop_linked_runtime()
        self._timer = None
        log.write(message)

    def modal(self, context, event):
        if not self._owns_runtime():
            self._timer = None
            return {'CANCELLED'}
        resolved = _find_linked_region()
        obj = context.active_object
        if event.type == 'ESC' and event.value == 'PRESS':
            self._finish('Quick selection disabled: ESC in active worker')
            return {'CANCELLED'}
        if (context.mode != 'EDIT_MESH' or
                obj is None or obj.type != 'MESH' or
                obj.name_full != self._object_name or
                not selection_core.edit_session_matches(
                    context, self._session,
                    require_uv=bool({'UV', 'UDIM', 'TEXTURE_COLOR'} & set(self.delimit))) or
                resolved is None):
            self._finish("Auto Select Linked disabled")
            return {'CANCELLED'}
        window, screen, area, region = resolved
        inside_region = False
        try:
            inside_region = (
                region.x <= event.mouse_x < region.x + region.width and
                region.y <= event.mouse_y < region.y + region.height)
        except ReferenceError:
            self._finish("Auto Select Linked disabled: viewport closed")
            return {'CANCELLED'}
        if not context.tool_settings.mesh_select_mode[2]:
            self._finish("Quick selection disabled: face select mode left")
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # If a second click arrives before the timer, discard the old seed.
            # Never mix pending clicks from different interaction cycles.
            self._pending = False
            self._after_click_signature = None
            self._before_faces = None
            if event.shift and not event.ctrl and not event.alt and inside_region:
                bm = bmesh.from_edit_mesh(obj.data)
                bm.faces.index_update()
                self._before_faces = {face.index for face in bm.faces if face.select}
                log.write('Quick Linked CLICK armed: selected_before=%d' % len(self._before_faces))
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            # Blender may apply the native release-selection AFTER our modal
            # handler. Do not record a fingerprint until the next TIMER event.
            self._pending = bool(inside_region and self._before_faces is not None)
            if self._pending:
                log.write('Quick Linked CLICK release: waiting for Blender selection')
            if not self._pending:
                self._before_faces = None
            return {'PASS_THROUGH'}
        if (event.type in {'RIGHTMOUSE', 'MIDDLEMOUSE'} and
                event.value == 'PRESS'):
            self._pending = False
            self._before_faces = None
        if event.type == 'TIMER' and self._pending:
            self._pending = False
            try:
                with context.temp_override(
                        window=window, screen=screen, area=area, region=region,
                        active_object=obj, object=obj, edit_object=obj):
                    bm = bmesh.from_edit_mesh(obj.data)
                    # The native Shift-click has finished by this TIMER.
                    # Pending cycles are explicitly invalidated by any next
                    # mouse click, preventing expansion from a different click.
                    bm.faces.ensure_lookup_table()
                    bm.faces.index_update()
                    new_faces = [face.index for face in bm.faces if face.select and
                                 face.index not in self._before_faces]
                    if new_faces:
                        log.write('Quick Linked CLICK ready: seeds=%d' % len(new_faces))
                    if not new_faces:
                        log.write('Quick Linked click skipped: no new face selected; '
                                  'use Shift+LEFTMOUSE in Face Select and click an unselected face')
                        self._before_faces = None
                        return {'RUNNING_MODAL'}
                    before_flags = selection_core.capture_mesh_flags(bm)
                    try:
                        _expand_udim_selection(
                            bm, self._before_faces, set(self.delimit),
                            self._texture_sampler, self.color_tolerance,
                            self.normal_angle)
                        newly_selected = sum(
                            f.select and not before_flags[2][f.index] for f in bm.faces)
                        log.write(f"Quick Linked completed: object={obj.name}; added={newly_selected}")
                    except Exception:
                        selection_core.restore_mesh_flags(bm, before_flags)
                        bmesh.update_edit_mesh(
                            obj.data, loop_triangles=False, destructive=False)
                        raise
                    self._before_faces = None
                    self._after_click_signature = None
                bmesh.update_edit_mesh(
                    obj.data, loop_triangles=False, destructive=False)
            except Exception as exc:
                log.write_exception("Auto Select Linked stopped", exc)
                self.report({'ERROR'}, f"Быстрое выделение остановлено: {exc}")
                self._finish("Auto Select Linked disabled after error")
                return {'CANCELLED'}
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        if self._owns_runtime():
            self._finish("Auto Select Linked cancelled")


class IMA_OT_AutoSelectLinked(Operator):
    """Short-lived button/dialog operator kept separate from the modal worker."""
    bl_idname = "ima.auto_select_linked"
    bl_label = "Быстрое Select Linked"
    bl_description = "Открыть настройки Quick Select для Shift+ЛКМ. Ограничения UV, UDIM, Seam, Sharp, Material, Normal и Texture Color; Esc выключает режим."

    normal_angle: bpy.props.FloatProperty(
        name="Угол Normal", description="Максимальный угол перехода между гранями",
        default=radians(35), min=0.0, max=radians(180), subtype='ANGLE', unit='ROTATION')

    texture_image: bpy.props.StringProperty(name="Атлас", description="Изображение для сравнения цвета по активной UV-карте")
    color_tolerance: bpy.props.FloatProperty(
        name="Допуск цвета", description="Максимальная разница RGB с цветом нажатого полигона; 0 — точное совпадение",
        default=0.05, min=0.0, max=1.0, precision=3, step=1)
    texture_wrap: bpy.props.EnumProperty(
        name="За пределами UV 0–1",
        items=(('REPEAT', "Repeat", "Повторять атлас"),
               ('EXTEND', "Extend", "Использовать край изображения")), default='REPEAT')

    delimit: bpy.props.EnumProperty(
        name="Delimit",
        description="Границы, через которые Select Linked не должен переходить",
        items=(
            ('NORMAL', "Normal", "Ограничивать по направлению нормалей"),
            ('MATERIAL', "Material", "Ограничивать по материалам"),
            ('SEAM', "Seam", "Ограничивать по UV-швам"),
            ('SHARP', "Sharp", "Ограничивать по Sharp-рёбрам"),
            ('UV', "UV", "Ограничивать по UV-островам"),
            ('UDIM', "UDIM", "Останавливать Select Linked на границе UDIM"),
            ('TEXTURE_COLOR', "Texture Color", "Ограничивать по цвету атласа в центре UV-полигона"),
        ),
        options={'ENUM_FLAG'},
        default={'SEAM'},
    )

    @classmethod
    def poll(cls, context):
        return _active_edit_mesh(context) is not None

    def invoke(self, context, event):
        """Interactive entry uses ISSMAKER's centered radial chooser, not a Blender dialog.

        execute() remains available to callers that specify their delimiters
        programmatically. No mesh changes occur until the worker receives clicks.
        """
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, 'Откройте быстрое выделение в 3D Viewport')
            return {'CANCELLED'}
        region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if region is None:
            self.report({'WARNING'}, 'Область 3D Viewport недоступна')
            return {'CANCELLED'}
        try:
            with context.temp_override(region=region):
                return bpy.ops.ima.view_mode_pie('INVOKE_DEFAULT', kind='LINKED')
        except (RuntimeError, ReferenceError, AttributeError) as exc:
            log.write_exception('Quick Linked radial menu failed', exc)
            self.report({'ERROR'}, 'Не удалось открыть меню выделения')
            return {'CANCELLED'}

    def draw(self, context):
        layout = self.layout
        layout.label(text="Shift + клик → Select Linked", icon='INFO')
        layout.label(text="Ограничения выделения:")
        layout.prop(self, "delimit")
        if 'NORMAL' in self.delimit:
            layout.prop(self, "normal_angle")
        if 'UDIM' in self.delimit:
            layout.label(text="Связанная часть в пределах UDIM")
            layout.label(text="Тайл по центру UV; активная UV-карта")
        if 'TEXTURE_COLOR' in self.delimit:
            layout.prop_search(self, "texture_image", bpy.data, "images")
            layout.prop(self, "color_tolerance")
            layout.prop(self, "texture_wrap")
            layout.label(text="Цвет в центре UV; сравнение с нажатым полигоном")

    def execute(self, context):
        if linked_active():
            self.report({'WARNING'}, "Режим уже включён")
            return {'CANCELLED'}
        result = bpy.ops.ima.auto_select_linked_worker(
            'INVOKE_DEFAULT', delimit=set(self.delimit),
            texture_image=self.texture_image, color_tolerance=self.color_tolerance,
            texture_wrap=self.texture_wrap, normal_angle=self.normal_angle)
        if 'RUNNING_MODAL' not in result:
            self.report({'ERROR'}, "Не удалось включить быстрое выделение")
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (
    IMA_OT_Merge002, IMA_OT_FlipNormals,
    IMA_OT_AutoSelectLinkedWorker, IMA_OT_AutoSelectLinked,
)


def register():
    runtime.register_classes_atomic(CLASSES)
    try:
        for handler_name, callback in (
                ('undo_pre', _linked_undo_pre),
                ('redo_pre', _linked_redo_pre),
                ('load_pre', _linked_load_pre)):
            handlers = getattr(bpy.app.handlers, handler_name, None)
            if handlers is None:
                continue
            for old in tuple(handlers):
                if (getattr(old, '__module__', None) == __name__
                        and getattr(old, '__name__', None) == callback.__name__):
                    handlers.remove(old)
            handlers.append(callback)
    except Exception:
        unregister()
        raise


def unregister():
    stop_linked_runtime()
    for handler_name, callback in (
            ('undo_pre', _linked_undo_pre),
            ('redo_pre', _linked_redo_pre),
            ('load_pre', _linked_load_pre)):
        handlers = getattr(bpy.app.handlers, handler_name, None)
        if handlers is not None:
            for old in tuple(handlers):
                if (getattr(old, '__module__', None) == __name__
                        and getattr(old, '__name__', None) == callback.__name__):
                    handlers.remove(old)
    runtime.unregister_classes_safe(CLASSES)
