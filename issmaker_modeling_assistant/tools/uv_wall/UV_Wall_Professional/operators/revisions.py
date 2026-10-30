"""Controlled restoration of previously accepted UV Wall parameters.

The accepted Mesh is NOT swapped here. Rebuild must use the existing
transactional operator; this is an explicit, Undo-able parameter load.
"""
import bpy
from ..core.definitions import (
    definition_for, path_for_generated, apply_serialized_definition,
    sync_scene_from_definition, has_committed_result,
)
from ..core.revisions import PROPERTY, parse_ledger, revision_payload
from ..core.revision_recovery import atomic_restore_definition


def revision_path_and_ledger(context):
    active = getattr(context, 'active_object', None)
    settings = getattr(getattr(context, 'scene', None), 'uv_wall_settings', None)
    path = None
    # Fast path for an already bound authored Path: avoid an O(scene.objects)
    # UID search on every N-panel redraw on a huge production scene.
    current_path = getattr(settings, 'path_mesh', None) if settings is not None else None
    if active is not None and active is current_path:
        path = current_path
    elif (active is not None and current_path is not None and
          bool(active.get('uvwall_generated'))):
        current_definition = definition_for(current_path)
        if (current_definition is not None and
                str(active.get('uvwall_owner_uid', '')) == str(getattr(current_definition, 'source_uid', '')) and
                str(getattr(current_definition, 'last_result_name', '')) == str(active.name)):
            path = current_path
    if path is None and active is not None and bool(active.get('uvwall_generated')):
        path = path_for_generated(active)
    if path is None and active is not None and getattr(active, 'type', None) == 'MESH':
        candidate = definition_for(active)
        if candidate is not None and getattr(candidate, 'initialized', False):
            path = active
    # Never load the history of a different selected object via stale settings.
    if path is None and active is None and settings is not None:
        path = getattr(settings, 'path_mesh', None)
    definition = definition_for(path) if path is not None else None
    if path is None or definition is None or not has_committed_result(definition, path):
        return None, None, None
    owner_uid = str(getattr(definition, 'source_uid', '') or '')
    if not owner_uid:
        return None, None, None
    # Only the authored Path is authoritative for revisions; never trust a
    # user-duplicated result object's copied custom properties.
    raw = path.get(PROPERTY, '')
    try:
        ledger = parse_ledger(raw, owner_uid)
    except ValueError:
        return path, definition, None
    return path, definition, ledger


class OBJECT_OT_uvwall_load_revision(bpy.types.Operator):
    bl_idname = 'object.uvwall_load_revision'
    bl_label = 'Загрузить параметры ревизии'
    bl_description = 'Загрузить сохранённые параметры без изменения принятого Mesh; затем вручную перестроить стену'
    bl_options = {'REGISTER', 'UNDO'}
    revision_id: bpy.props.IntProperty(name='Ревизия', min=1)

    @classmethod
    def poll(cls, context):
        if getattr(getattr(context, 'window_manager', None), 'uvwall_viewport_active', False):
            return False
        path, definition, ledger = revision_path_and_ledger(context)
        return bool(path is not None and definition is not None and ledger and ledger['entries'])

    def execute(self, context):
        path, definition, ledger = revision_path_and_ledger(context)
        if path is None or definition is None or not ledger:
            self.report({'WARNING'}, 'Выберите принятую стену или её исходный Path')
            return {'CANCELLED'}
        try:
            payload = revision_payload(path.get(PROPERTY, ''), definition.source_uid, self.revision_id)
        except (ValueError, TypeError) as exc:
            self.report({'ERROR'}, 'Ревизия повреждена или не принадлежит этой стене: '+str(exc))
            return {'CANCELLED'}
        try:
            atomic_restore_definition(
                definition, payload, bpy.data,
                sync=lambda: sync_scene_from_definition(path, context.scene.uv_wall_settings),
            )
        except (ValueError, RuntimeError, TypeError, AttributeError) as exc:
            self.report({'ERROR'}, 'Параметры не изменены: '+str(exc)[:155])
            return {'CANCELLED'}
        context.scene.uv_wall_settings.path_mesh = path
        path['uvwall_history_restore_parent_v1'] = int(self.revision_id)
        self.report({'INFO'}, 'Параметры ревизии загружены. Mesh не изменён — выполните Build для применения.')
        return {'FINISHED'}
