"""DWOS 2.1.3: outer frame belt is an OPEN seam, irrespective of FULL mode.

Migration does not rebuild splittings, glass, interior, UVs or materials. Stages
new Mesh datablocks and only switches them after every window has passed QA.
"""
from __future__ import annotations

import json

import bpy
import bmesh

from .dwos_log import event as log_event, exception as log_exception
from .batch_edit import resolve_window, target_windows

CONTACT_ROLES = frozenset({'FRAME_CONTACT', 'OUTER_CONTACT'})
_INDEX_LAYER_NAME = 'DWOS_Temp_OriginalFaceIndex'


def _stage_contact_removal(window):
    source = window.data
    if window.type != 'MESH' or source is None:
        raise ValueError(f'{window.name}: ожидается Mesh-рама')
    if window.dwos_window.finalized or window.get('DWOS_archived_source'):
        raise ValueError(f'{window.name}: завершённая или архивная рама')
    if source.library or window.library or source.shape_keys:
        raise ValueError(f'{window.name}: linked mesh / shape keys; нужен отдельный безопасный workflow')
    if window.modifiers:
        raise ValueError(f'{window.name}: рама содержит модификаторы. Примените отдельно перед очисткой контакта')
    if source.users > 1:
        raise ValueError(f'{window.name}: общий Mesh Data нескольких объектов; сделайте Single User')
    try:
        roles = json.loads(window.get('DWOS_face_roles', '') or '[]')
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{window.name}: повреждены DWOS_face_roles') from exc
    if len(roles) != len(source.polygons):
        raise ValueError(f'{window.name}: семантические роли не соответствуют полигонам ({len(roles)} / {len(source.polygons)})')
    if 'FRAME_EXTERIOR' not in roles:
        raise ValueError(f'{window.name}: не найден лицевой пояс FRAME_EXTERIOR')
    indices = [i for i, role in enumerate(roles) if role in CONTACT_ROLES]
    if not indices:
        return None
    prepared = source.copy()
    bm = bmesh.new()
    try:
        bm.from_mesh(prepared)
        bm.faces.ensure_lookup_table()
        if len(bm.faces) != len(roles):
            raise RuntimeError('Не совпадает число полигонов BMesh и DWOS roles')
        index_layer = bm.faces.layers.int.new(_INDEX_LAYER_NAME)
        for i, face in enumerate(bm.faces):
            face[index_layer] = i + 1
        faces_to_remove = [bm.faces[i] for i in indices]
        # FACES also cleans newly orphaned back-ring edges/verts in OPTIMIZED.
        # Front (and FULL rear) perimeter edges belong to surviving sheet faces.
        bmesh.ops.delete(bm, geom=faces_to_remove, context='FACES')
        if any(len(e.link_faces) > 2 for e in bm.edges):
            raise RuntimeError('Некорректная геометрия рамы: ребро с тремя и более полигонами')
        bm.to_mesh(prepared)
        prepared.update()
        layer = prepared.attributes.get(_INDEX_LAYER_NAME)
        if layer is None or layer.domain != 'FACE' or layer.data_type != 'INT':
            raise RuntimeError('Потеряна идентификация граней при переносе BMesh')
        new_roles = []
        for item in layer.data:
            old_idx = item.value - 1
            if old_idx < 0 or old_idx >= len(roles) or roles[old_idx] in CONTACT_ROLES:
                raise RuntimeError('Невозможно восстановить DWOS роли после открытия контура')
            new_roles.append(roles[old_idx])
        prepared.attributes.remove(layer)
        if len(new_roles) != len(source.polygons) - len(indices):
            raise RuntimeError('Потеряны другие грани рамы при удалении внешнего контакта')
        if len(prepared.uv_layers) != len(source.uv_layers) or len(prepared.materials) != len(source.materials):
            raise RuntimeError('Изменились UV или материалы. Транзакция отменена')
        return prepared, new_roles, len(indices)
    except Exception:
        if prepared.users == 0:
            bpy.data.meshes.remove(prepared)
        raise
    finally:
        bm.free()


class DWOS_OT_open_outer_contact(bpy.types.Operator):
    bl_idname = 'dwos.open_outer_contact'
    bl_label = 'Открыть наружный контур рамы'
    bl_description = ('Убрать наружные боковые полигоны FRAME_CONTACT у старых рам в выбранной области. '
                      'Контурные рёбра, стекло, Interior, UV и материалы остаются')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        frame = resolve_window(context.active_object)
        return (context.mode == 'OBJECT' and frame is not None
                and not frame.dwos_window.finalized and not frame.get('DWOS_archived_source'))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        frames = target_windows(context)
        if not frames:
            self.report({'WARNING'}, 'В выбранной области нет редактируемых рам DWOS')
            return {'CANCELLED'}
        staged = []
        originals = []
        try:
            for frame in frames:
                result = _stage_contact_removal(frame)
                if result is not None:
                    new_mesh, roles, deleted = result
                    staged.append((frame, new_mesh, roles, deleted))
            # Single publish point after all preflight and mesh staging.
            for frame, new_mesh, roles, deleted in staged:
                originals.append((frame, frame.data, frame.get('DWOS_face_roles'),
                                  frame.get('DWOS_outer_contact_policy')))
                frame.data = new_mesh
                frame['DWOS_face_roles'] = json.dumps(roles)
                frame['DWOS_outer_contact_policy'] = 'OPEN_FOR_WELD'
        except Exception as exc:
            for frame, old_mesh, old_roles, old_policy in reversed(originals):
                frame.data = old_mesh
                if old_roles is None:
                    if 'DWOS_face_roles' in frame: del frame['DWOS_face_roles']
                else: frame['DWOS_face_roles'] = old_roles
                if old_policy is None:
                    if 'DWOS_outer_contact_policy' in frame: del frame['DWOS_outer_contact_policy']
                else: frame['DWOS_outer_contact_policy'] = old_policy
            for _, mesh, _, _ in staged:
                if mesh.users == 0: bpy.data.meshes.remove(mesh)
            log_exception('OUTER_CONTACT_OPEN_FAILED', exc, windows=len(frames))
            self.report({'ERROR'}, f'Открытие наружного контура отменено: {exc}')
            return {'CANCELLED'}
        removed = sum(row[3] for row in staged)
        log_event('OUTER_CONTACT_OPEN_OK', windows=len(staged), removed_faces=removed,
                  scope=context.scene.dwos.batch_scope)
        self.report({'INFO'}, f'Рам обновлено: {len(staged)}; внешних полигонов удалено: {removed}')
        return {'FINISHED'}


CLASSES = (DWOS_OT_open_outer_contact,)
