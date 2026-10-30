import hashlib
import math
import struct

import bpy
import bmesh
from bpy.types import Operator
from mathutils import Vector

from . import log, runtime, selection_core


EPS = 1.0e-12


class UVSelectionError(RuntimeError):
    pass


class GeometryChangedError(RuntimeError):
    pass


def _edit_mesh_objects(context):
    objects = getattr(context, "objects_in_mode_unique_data", None)
    if objects is None:
        obj = context.active_object
        objects = [obj] if obj is not None else []
    return [obj for obj in objects if obj is not None and obj.type == 'MESH']


def _active_edit_mesh(context):
    obj = context.active_object
    if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return None
    return obj


def _validated_active_edit_mesh(context, purpose):
    obj = _active_edit_mesh(context)
    if obj is None:
        raise UVSelectionError(f"{purpose}: активный Mesh-объект должен быть в Edit Mode")
    edit_objects = _edit_mesh_objects(context)
    if len(edit_objects) != 1 or edit_objects[0] != obj:
        raise UVSelectionError(
            f"{purpose}: одновременно редактируется объектов: {len(edit_objects)}. "
            "Оставьте в Edit Mode только один объект")
    return obj


def _uv_editor_visible(context):
    """A visible UV editor makes its independent selection authoritative."""
    screen = getattr(context, "screen", None)
    areas = getattr(screen, "areas", ()) if screen is not None else ()
    for area in areas:
        if getattr(area, "type", None) == 'IMAGE_EDITOR':
            space = getattr(getattr(area, "spaces", None), "active", None)
            if getattr(space, "mode", None) == 'UV':
                return True
    return False


def _selection_source(context, requested='AUTO'):
    settings = context.tool_settings
    if requested == 'UV':
        if settings.use_uv_select_sync:
            raise UVSelectionError(
                "UV Sync Selection включён: используйте Mesh или отключите синхронизацию")
        return 'UV'
    if requested == 'MESH':
        return 'MESH'
    # With independent UV selection, never silently use broader 3D mesh selection.
    return ('UV' if not settings.use_uv_select_sync and _uv_editor_visible(context)
            else 'MESH')


def _target_faces(bm, purpose, context, uv_layer, requested='AUTO'):
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    source = _selection_source(context, requested)
    if source == 'MESH':
        faces = [face for face in bm.faces if face.select and not face.hide]
    else:
        if uv_layer is None:
            raise UVSelectionError(f"{purpose}: активная UV-карта отсутствует")
        mode = getattr(context.tool_settings, "uv_select_mode", 'VERTEX')
        faces, partial = selection_core.selected_uv_faces(bm, uv_layer, mode)
        if partial:
            raise UVSelectionError(
                f"{purpose}: частично выделено UV-граней: {partial}. "
                "Выделите острова целиком; операция не выполнялась")
    if not faces:
        hint = ("в UV Editor" if source == 'UV' else "в 3D Viewport")
        raise UVSelectionError(f"{purpose}: нет полностью выбранных полигонов {hint}")
    log.write(f"Selection guard: purpose={purpose}; source={source}; faces={len(faces)}")
    return faces


def _uv_equal(a, b):
    return (a - b).length_squared <= 1.0e-12


def _faces_uv_connected(face_a, face_b, edge, uv_layer):
    loops_a = [loop for loop in face_a.loops if loop.edge == edge]
    loops_b = [loop for loop in face_b.loops if loop.edge == edge]
    if not loops_a or not loops_b:
        return False
    la, lb = loops_a[0], loops_b[0]
    a0, a1 = la[uv_layer].uv, la.link_loop_next[uv_layer].uv
    b0, b1 = lb[uv_layer].uv, lb.link_loop_next[uv_layer].uv
    return ((_uv_equal(a0, b1) and _uv_equal(a1, b0)) or
            (_uv_equal(a0, b0) and _uv_equal(a1, b1)))


def _face_components(faces, connected):
    remaining = set(faces)
    components = []
    while remaining:
        seed = remaining.pop()
        component = [seed]
        stack = [seed]
        while stack:
            face = stack.pop()
            for edge in face.edges:
                for other in edge.link_faces:
                    if other in remaining and connected(face, other, edge):
                        remaining.remove(other)
                        component.append(other)
                        stack.append(other)
        components.append(component)
    return components


def _selected_uv_islands(faces, uv_layer):
    return _face_components(
        faces,
        lambda face, other, edge: _faces_uv_connected(
            face, other, edge, uv_layer),
    )


def _selected_geometry_components(faces):
    return _face_components(faces, lambda _face, _other, _edge: True)


def _is_partial_uv_selection(faces, uv_layer):
    selected = set(faces)
    for face in faces:
        for edge in face.edges:
            for other in edge.link_faces:
                if other in selected:
                    continue
                if _faces_uv_connected(face, other, edge, uv_layer):
                    return True, face.index, other.index
    return False, -1, -1


def _require_complete_uv_islands(faces, uv_layer, purpose):
    partial, selected_index, outside_index = _is_partial_uv_selection(
        faces, uv_layer)
    if partial:
        raise UVSelectionError(
            f"{purpose}: выбран только фрагмент UV-острова "
            f"(полигон {selected_index} соединён с невыбранным {outside_index}). "
            "Выделите остров целиком")


def _island_loops(island):
    return [loop for face in island for loop in face.loops]


def _bbox_center(loops, uv_layer):
    xs = [loop[uv_layer].uv.x for loop in loops]
    ys = [loop[uv_layer].uv.y for loop in loops]
    return Vector(((min(xs) + max(xs)) * 0.5,
                   (min(ys) + max(ys)) * 0.5))


def _geometry_digest(bm):
    """Fingerprint coordinates and topology; UV and selection are excluded."""
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    digest = hashlib.blake2b(digest_size=32)
    digest.update(struct.pack('<3Q', len(bm.verts), len(bm.edges), len(bm.faces)))
    for vert in bm.verts:
        digest.update(struct.pack('<3d', float(vert.co.x), float(vert.co.y), float(vert.co.z)))
    for edge in bm.edges:
        a, b = sorted((edge.verts[0].index, edge.verts[1].index))
        digest.update(struct.pack('<2Q', a, b))
    for face in bm.faces:
        digest.update(struct.pack('<Q', len(face.verts)))
        for vert in face.verts:
            digest.update(struct.pack('<Q', vert.index))
    return digest.digest()


def _verify_geometry_unchanged(bm, before, purpose):
    if _geometry_digest(bm) != before:
        raise GeometryChangedError(
            f"{purpose}: обнаружено изменение геометрии; операция отменена")


def _tri_area_3d(a, b, c):
    return ((b - a).cross(c - a)).length * 0.5


def _island_areas(obj, island, uv_layer):
    matrix = obj.matrix_world
    uv_area = 0.0
    world_area = 0.0
    for face in island:
        if len(face.loops) < 3:
            continue
        root = face.loops[0]
        for index in range(1, len(face.loops) - 1):
            a, b, c = root, face.loops[index], face.loops[index + 1]
            ua, ub, uc = a[uv_layer].uv, b[uv_layer].uv, c[uv_layer].uv
            uv_area += abs((ub - ua).cross(uc - ua)) * 0.5
            pa, pb, pc = matrix @ a.vert.co, matrix @ b.vert.co, matrix @ c.vert.co
            world_area += _tri_area_3d(pa, pb, pc)
    return uv_area, world_area


def _world_alignment_axes(obj, island):
    """Return world axes that represent right/up for this glass island."""
    normal_matrix = obj.matrix_world.to_3x3().inverted_safe().transposed()
    normal = Vector((0.0, 0.0, 0.0))
    for face in island:
        world_normal = normal_matrix @ face.normal
        if world_normal.length_squared > EPS:
            world_normal.normalize()
            normal += world_normal * max(face.calc_area(), EPS)
    if normal.length_squared <= EPS:
        raise UVSelectionError("Развёртка стекла: у выбранной области нет корректной нормали")
    normal.normalize()

    world_x = Vector((1.0, 0.0, 0.0))
    world_y = Vector((0.0, 1.0, 0.0))
    world_z = Vector((0.0, 0.0, 1.0))
    if abs(normal.z) < 0.95:
        # Façade/slanted/connecting glass: world Z must point up in UV.
        axis_v = world_z - normal * normal.dot(world_z)
        if axis_v.length_squared <= EPS:
            raise UVSelectionError("Развёртка стекла: не удалось построить вертикальную UV-ось")
        axis_v.normalize()
        axis_u = axis_v.cross(normal)
    else:
        # Floor/ceiling glass follows Top View: X to the right, Y upward.
        axis_u = world_x
        axis_v = world_y
    axis_u.normalize()
    axis_v.normalize()
    return axis_u, axis_v


def _align_island_to_world_up(obj, island, uv_layer):
    """Rotate a Smart UV island so it reads upright like the 3D world view."""
    axis_u, axis_v = _world_alignment_axes(obj, island)
    matrix = obj.matrix_world
    sine_sum = 0.0
    cosine_sum = 0.0
    seen_edges = set()
    for face in island:
        for loop in face.loops:
            edge_key = loop.edge.index
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            next_loop = loop.link_loop_next
            world_delta = matrix @ next_loop.vert.co - matrix @ loop.vert.co
            target = Vector((world_delta.dot(axis_u), world_delta.dot(axis_v)))
            uv_delta = next_loop[uv_layer].uv - loop[uv_layer].uv
            if target.length_squared <= EPS or uv_delta.length_squared <= EPS:
                continue
            difference = math.atan2(target.y, target.x) - math.atan2(uv_delta.y, uv_delta.x)
            weight = target.length
            sine_sum += math.sin(difference) * weight
            cosine_sum += math.cos(difference) * weight
    if abs(sine_sum) <= EPS and abs(cosine_sum) <= EPS:
        raise UVSelectionError("Развёртка стекла: не удалось определить направление острова")

    rotation = math.atan2(sine_sum, cosine_sum)
    loops = _island_loops(island)
    center = sum(
        (loop[uv_layer].uv.copy() for loop in loops),
        Vector((0.0, 0.0)),
    ) / len(loops)
    cosine = math.cos(rotation)
    sine = math.sin(rotation)
    for loop in loops:
        point = loop[uv_layer].uv - center
        loop[uv_layer].uv = center + Vector((
            cosine * point.x - sine * point.y,
            sine * point.x + cosine * point.y,
        ))


def _scale_island_to_safe_bounds(island, uv_layer, margin):
    """Scale one island independently and center it inside the safe 0-1 area."""
    loops = _island_loops(island)
    xs = [loop[uv_layer].uv.x for loop in loops]
    ys = [loop[uv_layer].uv.y for loop in loops]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width <= EPS or height <= EPS:
        raise UVSelectionError("Развёртка стекла: UV-остров вырожден в линию или точку")

    safe_size = 1.0 - 2.0 * margin
    if safe_size <= EPS:
        raise UVSelectionError("Развёртка стекла: UV-отступ слишком большой")
    scale = min(safe_size / width, safe_size / height)
    center = Vector(((min(xs) + max(xs)) * 0.5,
                     (min(ys) + max(ys)) * 0.5))
    target = Vector((0.5, 0.5))
    for loop in loops:
        loop[uv_layer].uv = target + (loop[uv_layer].uv - center) * scale


def _selection_snapshot(bm, uv_layer):
    """No BMesh element references survive Blender operator boundaries."""
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    history = []
    for element in bm.select_history:
        if isinstance(element, bmesh.types.BMVert):
            history.append(('VERT', element.index))
        elif isinstance(element, bmesh.types.BMEdge):
            history.append(('EDGE', element.index))
        elif isinstance(element, bmesh.types.BMFace):
            history.append(('FACE', element.index))
    return {
        "vertices": frozenset(v.index for v in bm.verts if v.select),
        "edges": frozenset(e.index for e in bm.edges if e.select),
        "faces": frozenset(f.index for f in bm.faces if f.select),
        "uv": tuple(tuple((bool(l[uv_layer].select),
                           bool(getattr(l[uv_layer], 'select_edge', False)))
                          for l in f.loops) for f in bm.faces),
        "active_face": bm.faces.active.index if bm.faces.active else None,
        "history": history,
    }


def _prepare_smart_uv_selection(bm, target_indexes, uv_layer):
    """Expose exactly the requested faces to Blender's Smart UV operator."""
    for vert in bm.verts:
        vert.select = False
    for edge in bm.edges:
        edge.select = False
    for face in bm.faces:
        face.select = False
        for loop in face.loops:
            loop[uv_layer].select = False
            if hasattr(loop[uv_layer], "select_edge"):
                loop[uv_layer].select_edge = False
    for index in target_indexes:
        face = bm.faces[index]
        face.select_set(True)
        for loop in face.loops:
            loop[uv_layer].select = True
            if hasattr(loop[uv_layer], "select_edge"):
                loop[uv_layer].select_edge = True
    bm.select_flush_mode()


def _restore_mesh_selection(bm, snapshot, uv_layer):
    if len(bm.faces) != len(snapshot["uv"]):
        raise GeometryChangedError("Сетка изменилась: нельзя восстановить UV-выделение")
    for vert in bm.verts:
        vert.select = vert.index in snapshot["vertices"]
    for edge in bm.edges:
        edge.select = edge.index in snapshot["edges"]
    for face in bm.faces:
        face.select = face.index in snapshot["faces"]
        saved = snapshot["uv"][face.index]
        if len(face.loops) != len(saved):
            raise GeometryChangedError("UV-топология изменилась при развёртке")
        for loop, (select, select_edge) in zip(face.loops, saved):
            loop[uv_layer].select = select
            if hasattr(loop[uv_layer], 'select_edge'):
                loop[uv_layer].select_edge = select_edge
    bm.faces.active = (bm.faces[snapshot["active_face"]]
                       if snapshot["active_face"] is not None else None)
    try:
        bm.select_history.clear()
        seq = {'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}
        for kind, index in snapshot["history"]:
            element = seq[kind][index]
            if element.select:
                bm.select_history.add(element)
    except (IndexError, ValueError, RuntimeError):
        # Mesh + UV element selection is authoritative; history is advisory.
        pass


def _outside_uv_digest(bm, uv_layer, target_indexes):
    """Prove a native UV operator did not alter any non-target coordinates."""
    digest = hashlib.blake2b(digest_size=32)
    for face in bm.faces:
        if face.index not in target_indexes:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                digest.update(struct.pack('<2d', float(uv.x), float(uv.y)))
    return digest.digest()


def _begin_uv_operation(context, purpose, require_uv=True, selection_source='AUTO',
                        require_complete_islands=False):
    """Read-only validation FIRST; acquire the expensive rollback copy only later.

    A normal partial-island refusal must not swap the mesh datablock, switch
    Object/Edit Mode or cause a selection reset.  Reacquire Edit BMesh after
    update_from_editmode() in the transaction constructor.
    """
    obj = _validated_active_edit_mesh(context, purpose)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.normal_update()
    uv = bm.loops.layers.uv.active
    if uv is None and require_uv:
        raise UVSelectionError(f"{purpose}: у объекта нет активной UV-развёртки")
    faces = _target_faces(bm, purpose, context, uv, selection_source)
    if require_complete_islands:
        _require_complete_uv_islands(faces, uv, purpose)
    target_indexes = tuple(face.index for face in faces)

    # No mutation has occurred above. If the safety copy fails, the original
    # Edit Mesh, object and selection stay exactly where the user left them.
    transaction = runtime.EditMeshTransaction(context, obj, purpose)
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        uv = bm.loops.layers.uv.active
        if uv is None and require_uv:
            raise UVSelectionError(f"{purpose}: UV-карта исчезла при проверке")
        faces = [bm.faces[index] for index in target_indexes]
        before = _geometry_digest(bm)
        transaction.scope_faces = frozenset(target_indexes)
        transaction.outside_uv = (_outside_uv_digest(bm, uv, transaction.scope_faces)
                                  if uv is not None else None)
        transaction.selection_guard = (selection_core.uv_and_mesh_fingerprint(
            bm, uv, context.tool_settings) if uv is not None else None)
        return obj, transaction, bm, faces, uv, before
    except Exception:
        # Still pre-mutation; never invoke the destructive mesh-swap rollback.
        transaction.commit()
        raise


def _finish_uv_operation(obj, transaction, bm, before, purpose):
    _verify_geometry_unchanged(bm, before, purpose)
    uv = bm.loops.layers.uv.active
    if transaction.outside_uv is not None:
        if uv is None or _outside_uv_digest(bm, uv, transaction.scope_faces) != transaction.outside_uv:
            raise UVSelectionError(f"{purpose}: изменились UV вне выделенной области — откат")
    if transaction.selection_guard is not None:
        actual = selection_core.uv_and_mesh_fingerprint(
            bm, uv, transaction.context.tool_settings)
        if actual != transaction.selection_guard:
            raise UVSelectionError(f"{purpose}: изменилось пользовательское выделение — откат")
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    transaction.commit()


def _cancel_transaction(transaction, _exc):
    if transaction is None:
        return False
    if not transaction.mutation_started:
        # Pure validation failures must not leave Edit Mode or replace Mesh data.
        transaction.commit()
        return False
    return transaction.rollback()


class IMA_OT_SelectionScope(Operator):
    bl_idname = "ima.selection_scope"
    bl_label = "Проверить область UV-операции"
    bl_description = "Показать точную область UV-выделения до изменения координат"
    bl_options = {'REGISTER'}

    selection_source: bpy.props.EnumProperty(
        name="Источник выделения",
        items=(("AUTO", "Авто", "UV приоритет при выключенном Sync"),
               ("UV", "Только UV", "Строго самостоятельное UV-выделение"),
               ("MESH", "Только Mesh", "Строго выделенные Mesh-полигоны")),
        default='AUTO',
    )

    @classmethod
    def poll(cls, context):
        return bool(_active_edit_mesh(context))

    def _inspect(self, context):
        obj = _validated_active_edit_mesh(context, "Предпросмотр выделения")
        bm = bmesh.from_edit_mesh(obj.data)
        uv = bm.loops.layers.uv.active
        if uv is None:
            raise UVSelectionError("Нет активной UV-карты")
        faces = _target_faces(bm, "Предпросмотр выделения", context,
                              uv, self.selection_source)
        _require_complete_uv_islands(faces, uv, "Предпросмотр выделения")
        return obj.name, len(faces), len(_selected_uv_islands(faces, uv)),                _selection_source(context, self.selection_source)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=365)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'selection_source')
        try:
            name, faces, islands, source = self._inspect(context)
            layout.label(text=f"Объект: {name}")
            layout.label(text=f"Источник: {source}")
            layout.label(text=f"В области: {faces} граней / {islands} UV-островов")
            layout.label(text="Остальные UV останутся без изменений", icon='LOCKED')
        except (ReferenceError, RuntimeError, ValueError) as exc:
            layout.label(text=f"Небезопасный выбор: {str(exc)[:86]}", icon='ERROR')

    def execute(self, context):
        try:
            obj, faces, islands, source = self._inspect(context)
            log.write(f"Selection preview: object={obj}; source={source}; faces={faces}; islands={islands}")
            self.report({'INFO'}, f"Проверено: {faces} граней, {islands} островов. UV не изменены")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}


class IMA_OT_UVAlign(Operator):
    bl_idname = "ima.uv_align"
    bl_label = "Центрировать UV"
    bl_description = "Центрировать полностью выбранные UV-острова в их плитках. Координаты UV меняются, геометрия — нет."
    bl_options = {'REGISTER', 'UNDO'}

    selection_source: bpy.props.EnumProperty(
        name="Область выделения",
        description="AUTO: при открытом UV Editor без Sync Selection работать только по UV",
        items=(('AUTO', "Авто (UV приоритет)", "UV-выделение важнее Mesh при отключённой синхронизации"),
               ('UV', "Только UV", "Не использовать выделение Mesh"),
               ('MESH', "Только Mesh", "Использовать только выделенные полигоны 3D")),
        default='AUTO',
    )


    @classmethod
    def poll(cls, context):
        return bool(_active_edit_mesh(context))

    @runtime.protected("uv_align")
    def execute(self, context):
        purpose = "Центрирование UV"
        transaction = None
        try:
            obj, transaction, bm, faces, uv, before = _begin_uv_operation(
                context, purpose, selection_source=self.selection_source,
                require_complete_islands=True)
            islands = _selected_uv_islands(faces, uv)
            for island in islands:
                loops = _island_loops(island)
                center = _bbox_center(loops, uv)
                target = Vector((math.floor(center.x) + 0.5,
                                 math.floor(center.y) + 0.5))
                delta = target - center
                transaction.mutation_started = True
                for loop in loops:
                    loop[uv].uv += delta
            _finish_uv_operation(obj, transaction, bm, before, purpose)
            log.write(f"UV center SAFE: object={obj.name}; faces={len(faces)}; islands={len(islands)}; geometry_unchanged=true")
            self.report({'INFO'}, f"Центрировано UV-островов: {len(islands)}")
            return {'FINISHED'}
        except UVSelectionError as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write(f"UV center selection guard: {exc}; rollback={restored}")
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'WARNING'}, f"{exc}{suffix}")
            return {'CANCELLED'}
        except Exception as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write_exception("UV center failed", exc)
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'ERROR'}, f"{exc}{suffix}")
            return {'CANCELLED'}


class IMA_OT_SetTexelDensity(Operator):
    bl_idname = "ima.set_texel_density"
    bl_label = "Тексельная плотность 1000 px/m"
    bl_description = "Установить плотность UV 1000 пикселей/метр для полностью выбранных островов. Геометрия не изменяется."
    bl_options = {'REGISTER', 'UNDO'}

    selection_source: bpy.props.EnumProperty(
        name="Область выделения",
        description="AUTO: при открытом UV Editor без Sync Selection работать только по UV",
        items=(('AUTO', "Авто (UV приоритет)", "UV-выделение важнее Mesh при отключённой синхронизации"),
               ('UV', "Только UV", "Не использовать выделение Mesh"),
               ('MESH', "Только Mesh", "Использовать только выделенные полигоны 3D")),
        default='AUTO',
    )


    resolution: bpy.props.EnumProperty(
        name="Размер текстуры",
        items=(('2048', "2K — 2048 px", "Использовать текстуру 2048×2048"),
               ('4096', "4K — 4096 px", "Использовать текстуру 4096×4096")),
        default='4096',
    )

    @classmethod
    def poll(cls, context):
        return bool(_active_edit_mesh(context))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        self.layout.label(text="Только выбранные полигоны", icon='RESTRICT_SELECT_OFF')
        self.layout.prop(self, "selection_source")
        self.layout.prop(self, "resolution", expand=True)
        self.layout.label(text="Только полные UV-острова", icon='INFO')
        self.layout.label(text="Неполный остров: отказ без изменения выделения")

    @runtime.protected("texel_density", cooldown=0.5)
    def execute(self, context):
        purpose = "Texel Density"
        transaction = None
        skipped = 0
        try:
            obj, transaction, bm, faces, uv, before = _begin_uv_operation(
                context, purpose, selection_source=self.selection_source,
                require_complete_islands=True)
            islands = _selected_uv_islands(faces, uv)
            resolution = float(self.resolution)
            unit_scale = max(context.scene.unit_settings.scale_length, EPS)
            changed = 0
            for island in islands:
                loops = _island_loops(island)
                uv_area, world_area = _island_areas(obj, island, uv)
                if uv_area <= EPS or world_area <= EPS:
                    skipped += 1
                    continue
                world_area_m2 = world_area * unit_scale * unit_scale
                current = resolution * math.sqrt(uv_area / world_area_m2)
                if current <= EPS or not math.isfinite(current):
                    skipped += 1
                    continue
                scale = 1000.0 / current
                center = sum((loop[uv].uv.copy() for loop in loops), Vector((0.0, 0.0))) / len(loops)
                transaction.mutation_started = True
                for loop in loops:
                    loop[uv].uv = center + (loop[uv].uv - center) * scale
                changed += 1
            if not changed:
                raise UVSelectionError("Texel Density: выбранные острова имеют нулевую площадь")
            _finish_uv_operation(obj, transaction, bm, before, purpose)
            log.write(f"Texel density SAFE: object={obj.name}; faces={len(faces)}; resolution={self.resolution}; islands={changed}; skipped={skipped}; geometry_unchanged=true")
            text = f"1000 px/m: островов {changed}"
            if skipped:
                text += f"; пропущено {skipped}"
            self.report({'INFO'}, text)
            return {'FINISHED'}
        except UVSelectionError as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write(f"Texel density selection guard: {exc}; rollback={restored}")
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'WARNING'}, f"{exc}{suffix}")
            return {'CANCELLED'}
        except Exception as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write_exception("Texel density failed", exc)
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'ERROR'}, f"{exc}{suffix}")
            return {'CANCELLED'}


class IMA_OT_UnwrapGlass(Operator):
    bl_idname = "ima.unwrap_glass"
    bl_label = "Развернуть стекло"
    bl_description = "Создать UV-развёртку выбранных полигонов стекла. Геометрия не изменяется."
    bl_options = {'REGISTER', 'UNDO'}

    selection_source: bpy.props.EnumProperty(
        name="Область выделения",
        description="AUTO: при открытом UV Editor без Sync Selection работать только по UV",
        items=(('AUTO', "Авто (UV приоритет)", "UV-выделение важнее Mesh при отключённой синхронизации"),
               ('UV', "Только UV", "Не использовать выделение Mesh"),
               ('MESH', "Только Mesh", "Использовать только выделенные полигоны 3D")),
        default='AUTO',
    )


    resolution: bpy.props.EnumProperty(
        name="Размер текстуры",
        items=(('2048', "2K — отступ 24 px", "Безопасный UV-отступ 24 пикселя"),
               ('4096', "4K — отступ 48 px", "Безопасный UV-отступ 48 пикселей")),
        default='4096',
    )

    @classmethod
    def poll(cls, context):
        return bool(_active_edit_mesh(context))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Только выбранные полигоны", icon='RESTRICT_SELECT_OFF')
        layout.prop(self, "selection_source")
        layout.label(text="Геометрия и диагонали не изменяются", icon='LOCKED')
        layout.label(text="Smart UV Project: угол 0°")
        layout.label(text="World Up → каждый остров отдельно в UV 0–1")
        layout.prop(self, "resolution", expand=True)

    def execute(self, context):
        key = "unwrap_glass"
        if not runtime.acquire(key, cooldown=0.75):
            self.report({'WARNING'}, "Развёртка уже выполняется")
            return {'CANCELLED'}
        purpose = "Развёртка стекла"
        transaction = None
        try:
            obj, transaction, bm, faces, uv, before = _begin_uv_operation(context, purpose, require_uv=False, selection_source=self.selection_source)
            if uv is None:
                uv = bm.loops.layers.uv.verify()
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            target_indexes = {face.index for face in faces}
            selection_state = _selection_snapshot(bm, uv)
            outside_before = _outside_uv_digest(bm, uv, target_indexes)
            transaction.mutation_started = True
            _prepare_smart_uv_selection(bm, target_indexes, uv)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

            try:
                result = bpy.ops.uv.smart_project(
                    angle_limit=0.0,
                    island_margin=0.0,
                    area_weight=0.0,
                    correct_aspect=True,
                    scale_to_bounds=False,
                )
            except TypeError:
                # Compatibility with Blender builds exposing the shorter API.
                result = bpy.ops.uv.smart_project(angle_limit=0.0)
            if 'FINISHED' not in result:
                raise RuntimeError("Smart UV Project не выполнил развёртку")

            # Smart UV changes only loop data, but reacquire Edit BMesh so no
            # stale references survive an operator boundary.
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            bm.verts.index_update()
            bm.edges.index_update()
            bm.faces.index_update()
            uv = bm.loops.layers.uv.active
            if uv is None:
                raise RuntimeError("Smart UV Project не создал активный UV-слой")
            faces = [bm.faces[index] for index in sorted(target_indexes)]
            islands = _selected_uv_islands(faces, uv)
            resolution = int(self.resolution)
            margin_pixels = 24 if resolution == 2048 else 48
            margin = margin_pixels / resolution
            for island in islands:
                _align_island_to_world_up(obj, island, uv)
                _scale_island_to_safe_bounds(island, uv, margin)
            if _outside_uv_digest(bm, uv, target_indexes) != outside_before:
                raise UVSelectionError(
                    "Smart UV изменил невыбранные UV: выполнен безопасный откат")
            _restore_mesh_selection(bm, selection_state, uv)
            _finish_uv_operation(obj, transaction, bm, before, purpose)
            log.write(
                f"Glass unwrap SMART_0 WORLD_UP: object={obj.name}; "
                f"faces={len(faces)}; islands={len(islands)}; "
                f"resolution={resolution}; margin={margin_pixels}px; "
                "geometry_unchanged=true; selection_preserved=true")
            self.report({'INFO'}, f"Стекло: островов {len(islands)}; отступ {margin_pixels} px")
            return {'FINISHED'}
        except UVSelectionError as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write(f"Glass unwrap selection guard: {exc}; rollback={restored}")
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'WARNING'}, f"{exc}{suffix}")
            return {'CANCELLED'}
        except Exception as exc:
            restored = _cancel_transaction(transaction, exc)
            log.write_exception("Glass unwrap failed", exc)
            suffix = "; исходная сетка восстановлена" if restored else ""
            self.report({'ERROR'}, f"{exc}{suffix}")
            return {'CANCELLED'}
        finally:
            runtime.release(key)


CLASSES = (IMA_OT_SelectionScope, IMA_OT_UVAlign, IMA_OT_SetTexelDensity,
           IMA_OT_UnwrapGlass)


def register():
    runtime.register_classes_atomic(CLASSES)


def unregister():
    runtime.unregister_classes_safe(CLASSES)
