"""Non-destructive staged DWOS winding fix for editable Frame/Glass/Interior."""
from __future__ import annotations

import json
import bpy
import bmesh

from .normal_winding_core import compute_face_flips
from .weld_attribute_policy import supported_without_custom_transfer
from .batch_edit import resolve_window, target_windows
from .dwos_log import event as log_event, exception as log_exception
from .adaptive_validation import validate_mesh as validate_adaptive_mesh

_FACE_ID = 'DWOS_Temp_NormalsFaceID'


def _get_roles(obj):
    key = 'DWOS_glass_roles' if obj.get('DWOS_role') == 'GLASS' else 'DWOS_face_roles'
    raw = obj.get(key)
    if not raw:
        raise RuntimeError(f'{obj.name}: отсутствуют служебные роли граней DWOS')
    try:
        roles = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{obj.name}: повреждены роли граней') from exc
    if not isinstance(roles, list) or len(roles) != len(obj.data.polygons):
        raise RuntimeError(f'{obj.name}: неправильное количество DWOS face roles')
    return key, roles


def _stage_object(obj, sign):
    source = obj.data
    if (obj.type != 'MESH' or source is None or source.users != 1 or source.library
            or obj.library or source.shape_keys or obj.modifiers):
        raise RuntimeError(f'{obj.name}: для исправления нормалей нужен локальный Single User Mesh без modifiers/shape keys')
    key, roles = _get_roles(obj)
    verts = [tuple(v.co) for v in source.vertices]
    polys = [list(poly.vertices) for poly in source.polygons]
    if obj.get('DWOS_adaptive_classification') in {'PLANAR','CURVED','CORNER'}:
        # Adaptive face normals rotate along S; the old ±local-Z oracle is invalid.
        # Generated adaptive parts are validated transactionally at build time.
        validate_adaptive_mesh({'verts':verts,'faces':polys,'roles':roles})
        return None
    flips = compute_face_flips(verts, polys, roles, sign)
    if not flips:
        return None
    staged = source.copy()
    bm = bmesh.new()
    try:
        bm.from_mesh(staged)
        bm.faces.ensure_lookup_table()
        if len(bm.faces) != len(roles):
            raise RuntimeError(f'{obj.name}: Blender изменил число граней при переносе')
        identity = bm.faces.layers.int.new(_FACE_ID)
        for i, face in enumerate(bm.faces):
            face[identity] = i + 1
        for i in sorted(flips):
            bm.faces[i].normal_flip()
        bm.to_mesh(staged)
        staged.update()
        layer = staged.attributes.get(_FACE_ID)
        if layer is None or len(layer.data) != len(roles):
            raise RuntimeError(f'{obj.name}: после исправления потеряна идентификация граней')
        old_indices = [int(v.value) - 1 for v in layer.data]
        if sorted(old_indices) != list(range(len(roles))):
            raise RuntimeError(f'{obj.name}: потеряна или дублирована грань')
        # Never lose UV/materials or user attributes. Scene remains unchanged.
        staged.attributes.remove(layer)
        old_attrs = {(a.name, a.domain, a.data_type) for a in source.attributes
                     if not supported_without_custom_transfer(a, {u.name for u in source.uv_layers})}
        new_attrs = {(a.name, a.domain, a.data_type) for a in staged.attributes
                     if not supported_without_custom_transfer(a, {u.name for u in staged.uv_layers})}
        if old_attrs != new_attrs or len(staged.uv_layers) != len(source.uv_layers):
            raise RuntimeError(f'{obj.name}: изменение пользовательских атрибутов/UV при фиксе нормалей')
        if len(staged.vertices) != len(source.vertices) or len(staged.polygons) != len(source.polygons):
            raise RuntimeError(f'{obj.name}: изменилось количество геометрии')
        if [tuple(v.co) for v in staged.vertices] != verts:
            raise RuntimeError(f'{obj.name}: изменились позиции вершин')
        if [mat for mat in staged.materials] != [mat for mat in source.materials]:
            raise RuntimeError(f'{obj.name}: потеряны материалы')
        # Map UV per face+vertex, robust to reversed loop order and BMesh face reordering.
        for uv in source.uv_layers:
            copied = staged.uv_layers.get(uv.name)
            if copied is None:
                raise RuntimeError(f'{obj.name}: исчез UV слой {uv.name}')
            for face_new, old_idx in enumerate(old_indices):
                old_poly, new_poly = source.polygons[old_idx], staged.polygons[face_new]
                old_uv = {int(source.loops[li].vertex_index): tuple(uv.data[li].uv)
                          for li in old_poly.loop_indices}
                new_uv = {int(staged.loops[li].vertex_index): tuple(copied.data[li].uv)
                          for li in new_poly.loop_indices}
                if old_uv.keys() != new_uv.keys() or any(
                        abs(old_uv[i][j]-new_uv[i][j]) > 1e-5
                        for i in old_uv for j in (0,1)):
                    raise RuntimeError(f'{obj.name}: UV координаты не перенесены без искажения')
        new_roles = [roles[i] for i in old_indices]
        return staged, key, json.dumps(new_roles), len(flips)
    except Exception:
        if staged.users == 0:
            bpy.data.meshes.remove(staged)
        raise
    finally:
        bm.free()


class DWOS_OT_repair_normals(bpy.types.Operator):
    bl_idname = 'dwos.repair_normals'
    bl_label = 'Исправить нормали рам и Interior'
    bl_description = ('Исправить направление граней DWOS без перестройки Split, '
                      'перемещения вершин и сброса UV; доступно для редактируемых окон')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = resolve_window(context.active_object)
        return (context.mode == 'OBJECT' and obj is not None
                and not obj.dwos_window.finalized and not obj.get('DWOS_archived_source'))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        windows = target_windows(context)
        if not windows:
            self.report({'WARNING'}, 'Нет редактируемых окон в выбранной области')
            return {'CANCELLED'}
        staged = []
        old = []
        changed = 0
        try:
            for window in windows:
                sign = 1 if window.dwos_window.flip_interior else -1
                parts = [window] + [c for c in window.children if c.get('DWOS_role') in {'GLASS','INTERIOR'}]
                for obj in parts:
                    result = _stage_object(obj, sign)
                    if result:
                        staged.append((obj, *result))
                        changed += result[3]
            for obj, prepared, key, value, _n in staged:
                old.append((obj, obj.data, key, obj.get(key)))
                obj.data = prepared
                obj[key] = value
            log_event('REPAIR_NORMALS_OK', windows=len(windows), parts=len(staged), faces_flipped=changed)
            self.report({'INFO'}, f'Нормали исправлены: окон {len(windows)}, граней {changed}')
            return {'FINISHED'}
        except Exception as exc:
            for obj, old_mesh, key, roles in reversed(old):
                obj.data = old_mesh
                obj[key] = roles
            log_exception('REPAIR_NORMALS_FAILED', exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            for _obj, prepared, _key, _roles, _n in staged:
                if prepared.users == 0:
                    bpy.data.meshes.remove(prepared)


CLASSES = (DWOS_OT_repair_normals,)
