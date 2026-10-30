"""Explicit, undoable recovery of the most recently accepted WALL mesh."""
import bpy

from ..core.build_guard import transaction_is_active
from ..core.mesh_recovery import recovery_status, restore_accepted_mesh, RecoveryError


class OBJECT_OT_uvwall_restore_mesh_snapshot(bpy.types.Operator):
    bl_idname = 'object.uvwall_restore_mesh_snapshot'
    bl_label = 'Восстановить сохранённый Mesh стены'
    bl_description = ('Восстановить последнюю принятую геометрию WALL из независимого Mesh-снимка. '
                      'Исходный Path и история параметров не изменяются; Blender Undo доступен')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if getattr(context, 'mode', None) != 'OBJECT':
            return False
        if transaction_is_active(context):
            return False
        if getattr(getattr(context, 'window_manager', None), 'uvwall_viewport_active', False):
            return False
        obj = getattr(context, 'active_object', None)
        return bool(obj is not None and recovery_status(obj, bpy.data)['available'])

    def execute(self, context):
        if (context.mode != 'OBJECT' or transaction_is_active(context)
                or getattr(getattr(context, 'window_manager', None), 'uvwall_viewport_active', False)):
            self.report({'WARNING'}, 'Закройте текущий Build и перейдите в Object Mode')
            return {'CANCELLED'}
        target = context.active_object
        try:
            info = restore_accepted_mesh(target, bpy.data)
        except RecoveryError as exc:
            self.report({'ERROR'}, f'Восстановление отменено; Mesh не изменён: {exc.code}')
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f'Внутренняя ошибка восстановления; выполнен откат: {type(exc).__name__}')
            return {'CANCELLED'}
        try:
            from ..production.validation import log_event
            log_event(f"RECOVERY_RESTORE_OK owner={info['owner_uid']}; "
                      f"result_uid={info['result_uid']}; revision={info['revision']}", 'RECOVERY')
        except Exception:
            pass
        self.report({'INFO'}, 'Последний принятый Mesh восстановлен; для возврата используйте Ctrl+Z')
        return {'FINISHED'}
