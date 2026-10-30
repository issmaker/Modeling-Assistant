from __future__ import annotations

import json
import bpy
import bmesh

from .numeric import parse_meter_text
from .dwos_log import event as log_event, exception as log_exception
from .weld_bake import collect_auxiliary, bake_auxiliary_mesh, remove_dwos_mesh_metadata, preview_rules

TOL = 1e-5


def _local(frame_obj, host_obj, co):
    return frame_obj.matrix_world.inverted() @ (host_obj.matrix_world @ co)


def _find_or_split_side_vertex(bm, frame_obj, host_obj, side, target, plane_z, width, height):
    hx = width * 0.5
    hy = height * 0.5

    def loc(v):
        return _local(frame_obj, host_obj, v.co)

    matches=[]
    for v in bm.verts:
        p = loc(v)
        if abs(p.z - plane_z) > TOL * 20:
            continue
        if side in {'bottom', 'top'}:
            sy = -hy if side == 'bottom' else hy
            if abs(p.y - sy) < TOL * 20 and abs(p.x - target) < TOL * 20:
                matches.append(v)
        else:
            sx = -hx if side == 'left' else hx
            if abs(p.x - sx) < TOL * 20 and abs(p.y - target) < TOL * 20:
                matches.append(v)
    if len(matches)>1:
        raise RuntimeError(f'Неоднозначный контакт стены: {len(matches)} вершин в одной позиции на {side}. Исправьте дубликаты перед Weld.')
    if matches:
        return matches[0]

    for edge in list(bm.edges):
        p0, p1 = loc(edge.verts[0]), loc(edge.verts[1])
        if abs(p0.z - plane_z) > TOL * 20 or abs(p1.z - plane_z) > TOL * 20:
            continue
        if side in {'bottom', 'top'}:
            sy = -hy if side == 'bottom' else hy
            if abs(p0.y - sy) > TOL * 20 or abs(p1.y - sy) > TOL * 20:
                continue
            a, b = p0.x, p1.x
        else:
            sx = -hx if side == 'left' else hx
            if abs(p0.x - sx) > TOL * 20 or abs(p1.x - sx) > TOL * 20:
                continue
            a, b = p0.y, p1.y
        if min(a, b) + TOL < target < max(a, b) - TOL:
            fac = (target - a) / (b - a)
            _, new_vert = bmesh.utils.edge_split(edge, edge.verts[0], fac)
            return new_vert
    raise RuntimeError(f'Не удалось создать вершину стены для {side} {target:.6f}')


def _side_sequence(bm, frame_obj, host_obj, side, plane_z, targets, width, height):
    vals = [p[0] if side in {'bottom', 'top'} else p[1] for p in targets]
    return [_find_or_split_side_vertex(bm, frame_obj, host_obj, side, value, plane_z, width, height) for value in vals]


def _delete_old_reveals(bm, frame_obj, host_obj, width, height, z0, z1):
    hx = width * 0.5
    hy = height * 0.5
    zmin, zmax = sorted((z0, z1))
    doomed = []
    for face in bm.faces:
        points = [_local(frame_obj, host_obj, v.co) for v in face.verts]
        if not points:
            continue
        if min(p.z for p in points) < zmin - TOL * 50 or max(p.z for p in points) > zmax + TOL * 50:
            continue
        same_side = False
        if all(abs(p.x + hx) < TOL * 50 and -hy - TOL <= p.y <= hy + TOL for p in points):
            same_side = True
        if all(abs(p.x - hx) < TOL * 50 and -hy - TOL <= p.y <= hy + TOL for p in points):
            same_side = True
        if all(abs(p.y + hy) < TOL * 50 and -hx - TOL <= p.x <= hx + TOL for p in points):
            same_side = True
        if all(abs(p.y - hy) < TOL * 50 and -hx - TOL <= p.x <= hx + TOL for p in points):
            same_side = True
        if same_side:
            doomed.append(face)
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='FACES')


def _copy_frame_without_contact(bm, frame_obj, host_obj, frame_material_index=0, mark_layer=None):
    roles = json.loads(frame_obj.get('DWOS_face_roles', '[]') or '[]')
    transform = host_obj.matrix_world.inverted() @ frame_obj.matrix_world
    fmap = {}
    for vert in frame_obj.data.vertices:
        fmap[vert.index] = bm.verts.new(transform @ vert.co)
    bm.verts.index_update()
    for poly in frame_obj.data.polygons:
        role = roles[poly.index] if poly.index < len(roles) else ''
        # Contact side gets replaced by explicit wall↔frame topology.
        if role in {'OUTER_CONTACT', 'FRAME_CONTACT'}:
            continue
        verts = [fmap[i] for i in poly.vertices]
        try:
            face = bm.faces.new(verts)
            face.material_index = int(frame_material_index)
            if mark_layer is not None:
                face[mark_layer] = 1
        except ValueError as exc:
            raise RuntimeError('Дублирующаяся грань при копировании рамы') from exc
    return fmap


def _frame_side_vertices(frame_obj, fmap, side, z, width, height):
    hx = width * 0.5
    hy = height * 0.5
    arr = []
    for vert in frame_obj.data.vertices:
        p = vert.co
        if abs(p.z - z) > TOL * 20:
            continue
        if side == 'bottom' and abs(p.y + hy) < TOL * 20:
            arr.append((p.x, fmap[vert.index]))
        elif side == 'top' and abs(p.y - hy) < TOL * 20:
            arr.append((p.x, fmap[vert.index]))
        elif side == 'left' and abs(p.x + hx) < TOL * 20:
            arr.append((p.y, fmap[vert.index]))
        elif side == 'right' and abs(p.x - hx) < TOL * 20:
            arr.append((p.y, fmap[vert.index]))
    arr.sort(key=lambda q: q[0])
    out = []
    for coord, vert in arr:
        if not out or abs(coord - out[-1][0]) > TOL:
            out.append((coord, vert))
    return [vert for _, vert in out]


def _bridge(bm, wall_seq, frame_seq, reverse=False, material_index=0):
    if len(wall_seq) != len(frame_seq):
        raise RuntimeError('Wall/Frame topology count mismatch')
    for i in range(len(wall_seq) - 1):
        quad = (wall_seq[i], wall_seq[i + 1], frame_seq[i + 1], frame_seq[i])
        if reverse:
            quad = tuple(reversed(quad))
        try:
            face = bm.faces.new(quad)
            face.material_index = int(material_index)
        except ValueError as exc:
            raise RuntimeError('Дублирующаяся или вырожденная грань перехода Wall/Frame') from exc


def _merge_sequences(bm, wall_by_side, frame_by_side):
    verts = []
    for side in wall_by_side:
        if len(wall_by_side[side]) != len(frame_by_side[side]):
            raise RuntimeError(f'Wall/Frame topology count mismatch: {side}')
        verts.extend(wall_by_side[side])
        verts.extend(frame_by_side[side])
    # Restrict remove-doubles to contact-ring vertices only.
    unique = []
    seen = set()
    for v in verts:
        if v.is_valid and id(v) not in seen:
            seen.add(id(v)); unique.append(v)
    if unique:
        bmesh.ops.remove_doubles(bm, verts=unique, dist=TOL * 10)


def prepare_weld_mesh(frame_obj, host_obj, source_mesh=None):
    """Construct validated weld on temporary datablock; NEVER mutate host/controller."""
    source_mesh = source_mesh or host_obj.data
    settings = frame_obj.dwos_window
    width = parse_meter_text(settings.width_text)
    height = parse_meter_text(settings.height_text)
    frame_depth = parse_meter_text(settings.frame_depth_text)
    wall_depth = parse_meter_text(settings.wall_depth_text)
    frame_offset = parse_meter_text(settings.frame_offset_text, allow_negative=True)
    payload = json.loads(settings.opening_payload_json or '{}')
    is_surface = payload.get('wall_mode') == 'SURFACE' or wall_depth <= TOL
    log_event('WELD_PREFLIGHT', host=host_obj.name, window=frame_obj.name, mode='SURFACE' if is_surface else 'SOLID')

    # 2.1.3 removes the outer lateral contact faces from BOTH frame modes.
    # FULL still has rear sheet/edges for a double-sided SOLID seam, while
    # OPTIMIZED has neither. Never try to join an absent back perimeter.
    if not is_surface and settings.optimization == 'OPTIMIZED':
        raise RuntimeError('Для двухстороннего Weld с SOLID-стеной выберите FULL: '
                           'у OPTIMIZED нет заднего контура рамы для сшивки.')

    interior_sign = 1 if settings.flip_interior else -1
    wall_front = 0.0
    wall_back = interior_sign * wall_depth
    frame_front = frame_offset
    frame_back = frame_offset + interior_sign * frame_depth
    frame_back_depth = frame_back * interior_sign
    if not is_surface and frame_back_depth > wall_depth + TOL:
        raise RuntimeError('Задняя сторона рамы выходит за внутреннюю поверхность стены')

    xs = json.loads(frame_obj.get('DWOS_grid_xs', '[]') or '[]')
    ys = json.loads(frame_obj.get('DWOS_grid_ys', '[]') or '[]')
    if len(xs) < 2 or len(ys) < 2:
        raise RuntimeError('Нет topology grid рамы')

    bm = bmesh.new()
    bm.from_mesh(source_mesh)
    bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
    frame_layer = bm.faces.layers.int.get('DWOS_baked_frame') or bm.faces.layers.int.new('DWOS_baked_frame')
    for face in bm.faces:
        face[frame_layer] = 0
    temp_mesh = None
    try:
        targets = {
            'bottom': [(x, -height * 0.5) for x in xs],
            'top': [(x, height * 0.5) for x in xs],
            'left': [(-width * 0.5, y) for y in ys],
            'right': [(width * 0.5, y) for y in ys],
        }
        wall_front_seq = {side: _side_sequence(bm, frame_obj, host_obj, side, wall_front, pts, width, height) for side, pts in targets.items()}
        wall_back_seq = {}
        if not is_surface:
            wall_back_seq = {side: _side_sequence(bm, frame_obj, host_obj, side, wall_back, pts, width, height) for side, pts in targets.items()}
            _delete_old_reveals(bm, frame_obj, host_obj, width, height, wall_front, wall_back)

        frame_mat = frame_obj.data.materials[0] if len(frame_obj.data.materials) else None
        names = [m.name if m else '' for m in source_mesh.materials]
        append_material = bool(frame_mat and frame_mat.name not in names)
        frame_mat_index = (len(names) if append_material else names.index(frame_mat.name) if frame_mat else 0)
        fmap = _copy_frame_without_contact(bm, frame_obj, host_obj, frame_mat_index, frame_layer)
        frame_front_seq = {side: _frame_side_vertices(frame_obj, fmap, side, frame_front, width, height) for side in targets}
        frame_back_seq = {side: _frame_side_vertices(frame_obj, fmap, side, frame_back, width, height) for side in targets}

        if abs(frame_front - wall_front) <= TOL * 10:
            _merge_sequences(bm, wall_front_seq, frame_front_seq)
        else:
            for side in targets:
                _bridge(bm, wall_front_seq[side], frame_front_seq[side], material_index=frame_mat_index)

        if not is_surface:
            if abs(frame_back - wall_back) <= TOL * 10:
                _merge_sequences(bm, wall_back_seq, frame_back_seq)
            else:
                for side in targets:
                    _bridge(bm, frame_back_seq[side], wall_back_seq[side], material_index=frame_mat_index)

        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bad = [edge for edge in bm.edges if len(edge.link_faces) > 2]
        if bad:
            raise RuntimeError(f'Weld создал non-manifold edges: {len(bad)}')
        zero = [face for face in bm.faces if face.calc_area() < 1e-12]
        if zero:
            raise RuntimeError(f'Weld создал zero-area faces: {len(zero)}')
        # True weld proof: at least four shared edges with an original wall face
        # and a generated frame face, rather than merely coincident coordinates.
        shared = sum(1 for edge in bm.edges if len(edge.link_faces) == 2 and
                     {int(f[frame_layer]) for f in edge.link_faces} == {0, 1})
        if shared < 4:
            raise RuntimeError(f'Weld не создал общий контур Wall/Frame (shared edges={shared})')
        # All destructive work was done in BMesh. Do not modify the host datablock
        # or material slots until every validation check succeeds.
        pre_stats=(len(source_mesh.vertices),len(source_mesh.edges),len(source_mesh.polygons))
        temp_mesh = source_mesh.copy()
        if append_material:
            temp_mesh.materials.append(frame_mat)
        bm.to_mesh(temp_mesh)
        temp_mesh.update()
        result = temp_mesh
        temp_mesh = None  # Ownership transfers to caller; finally must not delete.
        return result, shared, ('SURFACE' if is_surface else 'SOLID'), pre_stats
    finally:
        bm.free()
        if temp_mesh is not None and temp_mesh.users == 0:
            bpy.data.meshes.remove(temp_mesh)

def _unpublish_welded_windows(frames):
    """Reversibly unlink old controllers/parts, keeping datablocks for Undo.

    Only one ordinary Host Mesh remains linked to the scene. Unlinked original
    datablocks are temporary undo-compatible orphans, not scene objects.
    """
    all_objects=[]
    for frame in frames:
        all_objects.extend((frame, *collect_auxiliary(frame)))
    states={obj: tuple(obj.users_collection) for obj in all_objects}
    try:
        for obj in all_objects:
            for collection in list(obj.users_collection):
                collection.objects.unlink(obj)
        return states
    except Exception:
        _restore_welded_windows(states)
        raise


def _restore_welded_windows(states):
    for obj,collections in states.items():
        for collection in collections:
            if obj.name not in collection.objects:
                collection.objects.link(obj)


def _welded_controllers(scene,host):
    return [obj for obj in scene.objects if getattr(obj,'dwos_window',None)
            and obj.dwos_window.is_window and obj.dwos_window.finalized
            and obj.dwos_window.host_name==host.name and obj.get('DWOS_welded_to_host')]


def consolidate_legacy_weld(scene,host):
    """Migrate old 2.1.3 Weld: frame already belongs to Host; children don't."""
    controllers=_welded_controllers(scene,host)
    if not controllers:
        raise RuntimeError('На этой стене нет старых Weld-контроллеров')
    for frame in controllers:
        collect_auxiliary(frame)
    mesh=host.data.copy()
    try:
        parts=[obj for frame in controllers for obj in collect_auxiliary(frame)]
        bake_auxiliary_mesh(host,mesh,parts)
        if preview_rules(scene,host,[]):
            remove_dwos_mesh_metadata(mesh)
        old=host.data
        host.data=mesh
        try:
            _unpublish_welded_windows(controllers)
        except Exception:
            host.data=old
            raise
        log_event('WELD_LEGACY_CONSOLIDATE_OK',host=host.name,windows=len(controllers),baked_parts=len(parts))
        return len(controllers),len(parts)
    finally:
        if mesh.users==0:
            bpy.data.meshes.remove(mesh)


def weld_window_to_wall(frame_obj, host_obj):
    """Single window goes through exactly the same safety gates as batch."""
    return weld_scope_to_wall([frame_obj],host_obj)


def weld_scope_to_wall(frames, host_obj):
    """Preflight and stage the *entire* wall+frames+glass+Interior Mesh.

    Commit only once, and reversibly unlink obsolete controllers so the scene
    has one standard host Object (no orphan meshes in Outliner scene). Undo may
    restore the originals; never destructively remove linked mesh IDs here.
    """
    if not frames:
        raise RuntimeError('Не выбраны окна для Weld')
    if len({f.as_pointer() for f in frames}) != len(frames):
        raise RuntimeError('Повторяющееся окно в Weld batch')
    if any(f.dwos_window.finalized or f.dwos_window.host_name != host_obj.name for f in frames):
        raise RuntimeError('Weld scope содержит завершённое окно или другую стену')
    if getattr(host_obj.data,'shape_keys',None) or host_obj.mode!='OBJECT':
        raise RuntimeError('Host должен быть в Object Mode без Shape Keys')
    if host_obj.library or host_obj.data.library:
        raise RuntimeError('Linked Host нельзя изменять через Weld')
    components={frame:collect_auxiliary(frame) for frame in frames}
    source=host_obj.data
    working=source
    created=[]
    prepared=[]
    try:
        for frame in frames:
            new_mesh, shared, mode, original_stats = prepare_weld_mesh(frame,host_obj,working)
            created.append(new_mesh)
            bake_auxiliary_mesh(host_obj,new_mesh,components[frame])
            if working is not source and working.users==0:
                bpy.data.meshes.remove(working)
                created.remove(working)
            working=new_mesh
            prepared.append((frame,shared,mode,original_stats))
        clean=preview_rules(bpy.context.scene,host_obj,frames)
        if clean:
            remove_dwos_mesh_metadata(working)
        original_host_mesh=host_obj.data
        host_obj.data = working
        try:
            _unpublish_welded_windows(frames)
        except Exception:
            host_obj.data = original_host_mesh
            raise
        created.remove(working)
        # Selection is cosmetic; do not undo successful mesh commit for UI failure.
        try:
            bpy.context.scene.dwos.show_overlay=False
            for obj in bpy.context.selected_objects:
                if obj.select_get():obj.select_set(False)
            host_obj.select_set(True)
            bpy.context.view_layer.objects.active=host_obj
        except Exception as select_exc:
            log_event('WELD_SELECT_HOST_WARNING',error=repr(select_exc))
        for frame,shared,mode,_ in prepared:
            log_event('WELD_SCOPE_ITEM',host=host_obj.name,window=frame.name,
                      shared_edges=shared,mode=mode,baked_to_host=True)
        log_event('WELD_SCOPE_COMMIT',host=host_obj.name,windows=len(frames),
                  shared_edges=sum(row[1] for row in prepared),
                  baked_parts=sum(len(parts) for parts in components.values()),
                  one_mesh=True,clean_metadata=clean)
        return prepared
    finally:
        for mesh in created:
            if mesh.users==0:
                bpy.data.meshes.remove(mesh)


def check_weld_scope(frames, host_obj):
    """Run the entire Weld pipeline on disposable Mesh copies, NO scene changes."""
    if not frames:
        raise RuntimeError('Нет окон для проверки Weld')
    if host_obj.mode != 'OBJECT':
        raise RuntimeError('Проверка Weld доступна в Object Mode')
    if getattr(host_obj.data,'shape_keys',None):
        raise RuntimeError('Weld для стены с Shape Keys пока не поддерживается')
    if len(set(f.as_pointer() for f in frames)) != len(frames):
        raise RuntimeError('Повторяющееся окно в Weld batch')
    components={frame:collect_auxiliary(frame) for frame in frames}
    source=host_obj.data
    created=[]
    results=[]
    try:
        for frame in frames:
            if frame.dwos_window.host_name != host_obj.name or frame.dwos_window.finalized:
                raise RuntimeError('В проверке есть завершённое окно или другая стена')
            new_mesh,shared,mode,before=prepare_weld_mesh(frame,host_obj,source)
            created.append(new_mesh)
            bake_auxiliary_mesh(host_obj,new_mesh,components[frame])
            results.append((frame.name, shared, mode))
            source=new_mesh
        log_event('WELD_SCOPE_PREFLIGHT_OK',host=host_obj.name,windows=len(results),
                  shared_edges=sum(r[1] for r in results))
        return results
    finally:
        # No mesh was assigned to a real Object, so every intermediate is disposable.
        for mesh in created:
            if mesh.users==0:
                bpy.data.meshes.remove(mesh)


class DWOS_OT_weld_check(bpy.types.Operator):
    bl_idname = 'dwos.weld_check'
    bl_label = 'Проверить Production Weld'
    bl_description = 'Тест всех выбранных окон на временных Mesh без изменения стены'
    bl_options = {'REGISTER'}

    def execute(self, context):
        from .batch_edit import resolve_window,target_windows
        frame=resolve_window(context.active_object)
        if frame is None:
            self.report({'ERROR'},'Выберите окно DWOS');return {'CANCELLED'}
        host=bpy.data.objects.get(frame.dwos_window.host_name)
        if host is None or host.type!='MESH':
            self.report({'ERROR'},'Стена не найдена');return {'CANCELLED'}
        targets=target_windows(context,frame)
        try:
            rows=check_weld_scope(targets,host)
        except Exception as exc:
            log_exception('WELD_SCOPE_PREFLIGHT_FAILED',exc,host=host.name,targets=len(targets))
            self.report({'ERROR'},f'Weld нельзя применить: {exc}')
            return {'CANCELLED'}
        self.report({'INFO'},f'Проверка Weld: {len(rows)} окон, общих рёбер: {sum(x[1] for x in rows)}')
        return {'FINISHED'}


class DWOS_OT_weld_scope(bpy.types.Operator):
    bl_idname = 'dwos.weld_scope'
    bl_label = 'Production Weld для выбранной области'
    bl_description = 'Один preflight и атомарный commit для всех выбранных DWOS окон одной стены'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from .batch_edit import resolve_window, target_windows
        frame=resolve_window(context.active_object)
        if frame is None:
            self.report({'ERROR'}, 'Выберите окно DWOS')
            return {'CANCELLED'}
        host=bpy.data.objects.get(frame.dwos_window.host_name)
        if host is None or host.type!='MESH':
            self.report({'ERROR'}, 'Стена не найдена')
            return {'CANCELLED'}
        targets=target_windows(context,frame)
        try:
            rows=weld_scope_to_wall(targets,host)
        except Exception as exc:
            log_exception('WELD_SCOPE_FAILED',exc,host=host.name,targets=len(targets))
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},f'Сшито окон: {len(rows)}')
        return {'FINISHED'}


class DWOS_OT_weld_to_wall(bpy.types.Operator):
    bl_idname = 'dwos.weld_to_wall'
    bl_label = 'Сшить с стеной'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        frame = context.active_object
        while frame is not None and not (getattr(frame, 'dwos_window', None) and frame.dwos_window.is_window):
            frame = frame.parent
        if not frame or not frame.dwos_window.is_window or frame.dwos_window.finalized:
            self.report({'ERROR'}, 'Выберите незавершённое окно DWOS')
            return {'CANCELLED'}
        host = bpy.data.objects.get(frame.dwos_window.host_name)
        if not host or host.type != 'MESH':
            self.report({'ERROR'}, 'Стена не найдена')
            return {'CANCELLED'}
        log_event('WELD_BEGIN', window=frame.name, host=host.name)
        try:
            weld_window_to_wall(frame, host)
        except Exception as exc:
            log_exception('WELD_FAILED', exc, window=frame.name, host=host.name)
            self.report({'ERROR'}, f'Weld failed: {exc}')
            return {'CANCELLED'}
        log_event('WELD_OK', window=frame.name, host=host.name)
        try:
            from .mesh_diagnostics import write_report
            write_report(context.scene, host, 'WELD_OK')
        except Exception as analysis_exc:
            log_exception('AUTO_MESH_ANALYSIS_WARNING', analysis_exc, reason='WELD_OK')
        self.report({'INFO'}, 'Рама сшита со стеной')
        return {'FINISHED'}


class DWOS_OT_consolidate_weld(bpy.types.Operator):
    bl_idname='dwos.consolidate_weld'
    bl_label='Собрать Weld в один Mesh'
    bl_description='Собрать стекло/Interior из Weld 2.1.3 в Host; убрать пустые контроллеры'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        o=context.active_object
        return context.mode=='OBJECT' and o is not None and o.type=='MESH' and (
            not o.dwos_window.is_window or (o.dwos_window.finalized and o.get('DWOS_welded_to_host')))

    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)

    def execute(self,context):
        selected=context.active_object
        host=(bpy.data.objects.get(selected.dwos_window.host_name)
              if selected.dwos_window.is_window else selected)
        try:
            if host is None:
                raise RuntimeError('Host не найден')
            if host.data.users>1 or host.library or host.data.library or host.mode!='OBJECT':
                raise RuntimeError('Host должен быть локальным Single User Mesh в Object Mode')
            windows,parts=consolidate_legacy_weld(context.scene,host)
            context.scene.dwos.show_overlay=False
            try:
                for obj in context.selected_objects:obj.select_set(False)
                host.select_set(True)
                context.view_layer.objects.active=host
            except Exception as select_exc:
                log_event('WELD_LEGACY_SELECT_WARNING',error=repr(select_exc))
            self.report({'INFO'},f'Один обычный Mesh: {windows} Weld окон, добавлено компонентов: {parts}')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('CONSOLIDATE_WELD_FAILED',exc,host=host.name)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
