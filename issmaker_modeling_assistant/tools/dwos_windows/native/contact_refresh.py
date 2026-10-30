"""DWOS 3.1: rebuild editable Host-contact topology without changing the Host.

Uses existing window settings; does not remesh previously finalized ordinary meshes.
The original Mesh datablocks remain untouched until all targets were validated.
"""
from __future__ import annotations

import copy
import bpy

from .batch_edit import resolve_window, target_windows
from .window_builder import rebuild_window
from .window_assembly import contact_ring_stats
from .dwos_log import event as log_event, exception as log_exception


def _dwos_properties(obj):
    return {key: copy.deepcopy(obj[key]) for key in obj.keys()
            if isinstance(key, str) and key.startswith('DWOS_')}


def _restore_properties(obj, saved):
    for key in tuple(obj.keys()):
        if isinstance(key, str) and key.startswith('DWOS_') and key not in saved:
            del obj[key]
    for key, value in saved.items():
        obj[key] = value


def _controlled_meshes(frame):
    return [frame] + [child for child in frame.children
                      if child.type == 'MESH' and child.get('DWOS_role') in {'GLASS', 'INTERIOR'}]


def _preflight(frame):
    if (frame is None or frame.type != 'MESH' or not frame.dwos_window.is_window or
            frame.dwos_window.finalized or frame.get('DWOS_archived_source')):
        raise ValueError('Можно перестраивать только редактируемые окна DWOS')
    if not frame.users_collection:
        raise ValueError(f'{frame.name}: отсутствует рабочая коллекция')
    for obj in _controlled_meshes(frame):
        if (obj.library or obj.data.library or obj.data.users != 1 or
                obj.data.shape_keys or obj.modifiers or obj.constraints):
            raise ValueError(f'{obj.name}: необходим локальный Single User без модификаторов')


def rebuild_contact_scope(frames):
    """Rebuild selected editable frames with reversible Mesh swaps.

    Real Blender Undo/Redo still requires on-device verification. Do not claim a
    new Mesh to be geometrically welded with the wall: Host is read-only here.
    """
    if not frames or len({f.as_pointer() for f in frames}) != len(frames):
        raise ValueError('Некорректный или пустой набор окон DWOS')
    for frame in frames:
        _preflight(frame)
    saved = []
    staging = []
    try:
        for frame in frames:
            controlled = _controlled_meshes(frame)
            record = {
                'frame': frame,
                'children': {child.as_pointer() for child in frame.children},
                'matrix': frame.matrix_world.copy(),
                'interior_mode': frame.dwos_window.interior_mode,
                'objects': [(obj, obj.data, _dwos_properties(obj)) for obj in controlled],
            }
            saved.append(record)
            # Swap to a copy BEFORE rebuilding; the prior Mesh is never edited.
            for obj, old_mesh, _props in record['objects']:
                new_mesh = old_mesh.copy()
                staging.append(new_mesh)
                obj.data = new_mesh
            rebuild_window(frame)
            stats = contact_ring_stats(frame)
            if stats['extra_junctions']:
                raise RuntimeError(f'{frame.name}: после перестроения остались лишние контактные точки')
        # A single success for the whole batch. No Host data touched.
        for record in saved:
            for _obj, old_mesh, _props in record['objects']:
                if old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
        log_event('CONTACT_REFRESH_OK', windows=len(frames), host_topology_preserved=True,
                  host_untouched=True)
        return len(frames)
    except Exception:
        # Restore ALL windows (including ones already rebuilt in this batch).
        for record in reversed(saved):
            frame = record['frame']
            for child in tuple(frame.children):
                if child.as_pointer() not in record['children']:
                    if child.get('DWOS_role') in {'GLASS', 'INTERIOR'}:
                        data = child.data if child.type == 'MESH' else None
                        bpy.data.objects.remove(child, do_unlink=True)
                        if data and data.users == 0:
                            bpy.data.meshes.remove(data)
            frame.matrix_world = record['matrix']
            frame.dwos_window.interior_mode = record['interior_mode']
            for obj, old_mesh, properties in record['objects']:
                obj.data = old_mesh
                _restore_properties(obj, properties)
        for mesh in staging:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        raise


class DWOS_OT_refresh_contact_scope(bpy.types.Operator):
    bl_idname = 'dwos.refresh_contact_scope'
    bl_label = 'Перестроить контактный контур рам'
    bl_description = ('Перестроить редактируемые рамы по их текущим настройкам в выбранной области. '
                      'Синхронизировать наружный контур с Host, сохранив Split, стекло, Interior и стену')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        frame = resolve_window(context.active_object)
        return (context.mode == 'OBJECT' and frame is not None and
                not frame.dwos_window.finalized and not frame.get('DWOS_archived_source'))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        frames = target_windows(context)
        try:
            n = rebuild_contact_scope(frames)
            self.report({'INFO'}, f'Перестроено рам: {n}; контактный контур синхронизирован с Host')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('CONTACT_REFRESH_FAILED', exc, scope=context.scene.dwos.batch_scope)
            self.report({'ERROR'}, f'Перестроение отменено: {exc}')
            return {'CANCELLED'}


CLASSES = (DWOS_OT_refresh_contact_scope,)
