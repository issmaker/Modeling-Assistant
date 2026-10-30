"""Non-destructive DWOS Finalize & Clean and explicit orientation repair.

The clean collection contains ordinary Mesh objects with original mesh materials,
UV layers and geometry, but no generator RNA state or DWOS-prefixed metadata.
The editable originals are ARCHIVED/HIDDEN, not destroyed by the default action.
"""
from __future__ import annotations
import bpy
from bpy.props import BoolProperty
from bpy.types import Operator

from .cleanup_core import owned_names, preview_counts
from .orientation_core import stabilize_up
from .batch_edit import resolve_window, target_windows
from .dwos_log import event as log_event, exception as log_exception


def _components(window):
    result=[window]
    for child in window.children:
        if child.get('DWOS_role') not in {'GLASS','INTERIOR'}:
            raise ValueError(f'{window.name}: чужеродный дочерний объект {child.name}. Сначала отделите его.')
        if child.children:
            raise ValueError(f'{child.name}: вложенная геометрия — требуется ручная проверка')
        result.append(child)
    return result


def _validate_components(window):
    if window.dwos_window.finalized or window.get('DWOS_archived_source'):
        raise ValueError(f'{window.name}: уже финализировано либо скрыто в архиве')
    components=_components(window)
    if not any(o.get('DWOS_role')=='GLASS' for o in components[1:]):
        raise ValueError(f'{window.name}: отсутствует стекло — финализация отменена')
    for obj in components:
        if obj.type!='MESH' or not obj.data or not len(obj.data.polygons):
            raise ValueError(f'{obj.name}: пустая/не-Mesh геометрия')
        if obj.modifiers:
            raise ValueError(f'{obj.name}: модификаторы требуют отдельного применения перед Finalize')
        if obj.constraints:
            raise ValueError(f'{obj.name}: ограничения нужно предварительно зафиксировать')
        if obj.data.shape_keys:
            raise ValueError(f'{obj.name}: Shape Keys требуют предварительной проверки')
        if obj.library or obj.data.library:
            raise ValueError(f'{obj.name}: linked asset. Сначала сделайте локальную копию.')
    return components


def _strip_dwos(mesh):
    for attr in list(mesh.attributes):
        if attr.name.startswith('DWOS_'):
            mesh.attributes.remove(attr)
    for key in owned_names(list(mesh.keys())):
        del mesh[key]


def _stage_clean_object(original, output_collection):
    mesh=original.data.copy()  # retains UV maps, materials and custom normals
    obj=None
    try:
        _strip_dwos(mesh)
        obj=bpy.data.objects.new(original.name+'_Clean',mesh)
        output_collection.objects.link(obj)
        obj.matrix_world=original.matrix_world.copy()  # world transform, no parent
        obj.hide_render=False
        if len(obj.data.uv_layers)!=len(original.data.uv_layers):
            raise RuntimeError(f'{original.name}: потеряны UV слои')
        if len(obj.data.materials)!=len(original.data.materials):
            raise RuntimeError(f'{original.name}: потеряны Material Slots')
        return obj
    except Exception:
        if obj is not None and obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj,do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise


def _backup_collection(scene):
    name='DWOS_EDITABLE_BACKUP'
    coll=bpy.data.collections.get(name)
    if coll is None:
        coll=bpy.data.collections.new(name)
    if coll.name not in scene.collection.children.keys():
        scene.collection.children.link(coll)
    return coll


def _hide_archived(objects, collection):
    """Move to one explicit archive collection; Undo restores prior membership."""
    for obj in objects:
        if collection.name not in {c.name for c in obj.users_collection}:
            collection.objects.link(obj)
        for other in list(obj.users_collection):
            if other is not collection:
                other.objects.unlink(obj)
        obj.hide_render=True
        obj.hide_set(True)
    # Mark only root window(s) separately after all objects staged.


class DWOS_OT_repair_orientation(Operator):
    bl_idname='dwos.repair_orientation'
    bl_label='Исправить верх/низ окон'
    bl_description='Выровнять локальную вертикаль существующих DWOS окон без изменения Split-параметров'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        w=resolve_window(context.active_object)
        return context.mode=='OBJECT' and w is not None and not w.dwos_window.finalized and not w.get('DWOS_archived_source')

    def execute(self,context):
        from .window_builder import _reanchor_to_facade
        windows=target_windows(context)
        if not windows:
            self.report({'ERROR'},'Нет редактируемых окон в выбранной области');return {'CANCELLED'}
        import json
        for w in windows:
            settings=w.dwos_window
            try:
                payload=json.loads(settings.opening_payload_json)
                for axis in ('center_local','axis_x_local','axis_y_local','axis_z_local'):
                    if len(payload[axis])!=3: raise ValueError(axis)
                host=bpy.data.objects.get(settings.host_name)
                if host is None or host.type!='MESH': raise ValueError('Host не найден')
            except Exception as exc:
                self.report({'ERROR'},f'{w.name}: нет валидного opening payload / Host: {exc}')
                return {'CANCELLED'}
        old={w:w.matrix_world.copy() for w in windows}
        try:
            for w in windows:
                _reanchor_to_facade(w)
        except Exception as exc:
            for w,mat in old.items(): w.matrix_world=mat
            log_exception('ORIENTATION_REPAIR_FAILED',exc,count=len(windows))
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        changed=sum(any(abs(w.matrix_world[r][c]-old[w][r][c])>1e-6 for r in range(4) for c in range(4)) for w in windows)
        log_event('ORIENTATION_REPAIR_OK',windows=len(windows),changed=changed)
        self.report({'INFO'},f'Ось вверх исправлена: {changed} из {len(windows)}')
        return {'FINISHED'}


class DWOS_OT_finalize_clean(Operator):
    bl_idname='dwos.finalize_clean'
    bl_label='Финализировать и очистить DWOS'
    bl_description='Создать обычные чистые Mesh, сохранить UV/материалы, скрыть исходники в DWOS_EDITABLE_BACKUP'
    bl_options={'REGISTER','UNDO'}

    confirm: BoolProperty(name='Создать чистые Mesh и архивировать редактируемые оригиналы',default=True)

    @classmethod
    def poll(cls,context):
        w=resolve_window(context.active_object)
        return context.mode=='OBJECT' and w is not None and not w.dwos_window.finalized and not w.get('DWOS_archived_source')

    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self,width=450)

    def draw(self,context):
        layout=self.layout
        targets=target_windows(context)
        row=layout.row()
        row.label(text=f'Область: {context.scene.dwos.batch_scope} · окон: {len(targets)}',icon='INFO')
        layout.label(text='Создадим независимые Mesh без DWOS metadata.')
        layout.label(text='Материалы, UV, нормали и геометрия сохраняются.')
        layout.label(text='Исходники останутся скрыты в DWOS_EDITABLE_BACKUP.')
        layout.prop(self,'confirm',text='Я проверил область применения')

    def execute(self,context):
        if not self.confirm:
            self.report({'WARNING'},'Операция не подтверждена');return {'CANCELLED'}
        windows=target_windows(context)
        if not windows:
            self.report({'ERROR'},'Нет редактируемых окон');return {'CANCELLED'}
        try:
            # FULL PREFLIGHT: do not modify any Blender mesh/visibility until all pass.
            source={w:_validate_components(w) for w in windows}
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        clean_collection=bpy.data.collections.new('DWOS_FINAL_CLEAN')
        staged=[]
        source_state={obj: (tuple(obj.users_collection),obj.hide_get(),obj.hide_render)
                      for components in source.values() for obj in components}
        try:
            for w, components in source.items():
                for original in components:
                    staged.append(_stage_clean_object(original,clean_collection))
            # Publish only after all copies exist and have been cleaned.
            context.scene.collection.children.link(clean_collection)
            archive=_backup_collection(context.scene)
            for w, components in source.items():
                _hide_archived(components,archive)
                w['DWOS_archived_source']=True
            # Selection is cosmetic; failure must not invalidate a successful commit.
            try:
                for obj in context.selected_objects:
                    obj.select_set(False)
                for obj in staged:
                    if context.view_layer.objects.get(obj.name) is obj:
                        obj.select_set(True)
                if staged:
                    context.view_layer.objects.active=staged[0]
            except Exception as select_exc:
                log_event('FINALIZE_SELECTION_WARNING',error=repr(select_exc))
        except Exception as exc:
            # Restore scene memberships and visibility before deleting staged copies.
            for obj,(collections,hidden,hidden_render) in source_state.items():
                try:
                    for coll in collections:
                        if coll.name not in {c.name for c in obj.users_collection}:
                            coll.objects.link(obj)
                    for coll in list(obj.users_collection):
                        if coll not in collections: coll.objects.unlink(obj)
                    obj.hide_render=hidden_render
                    obj.hide_set(hidden)
                    if 'DWOS_archived_source' in obj:del obj['DWOS_archived_source']
                except Exception as restore_exc:
                    log_exception('FINALIZE_ROLLBACK_WARNING',restore_exc,object=obj.name)
            for obj in staged:
                mesh=obj.data
                bpy.data.objects.remove(obj,do_unlink=True)
                if mesh is not None and mesh.users==0:bpy.data.meshes.remove(mesh)
            if clean_collection.users==0:bpy.data.collections.remove(clean_collection)
            log_exception('FINALIZE_CLEAN_FAILED',exc,windows=len(windows))
            self.report({'ERROR'},f'Финализация отменена: {exc}')
            return {'CANCELLED'}
        log_event('FINALIZE_CLEAN_OK',windows=len(windows),meshes=len(staged),
                  archive='DWOS_EDITABLE_BACKUP',clean_collection=clean_collection.name)
        self.report({'INFO'},f'Чистых Mesh: {len(staged)} · окон: {len(windows)}. Источники сохранены в архиве.')
        return {'FINISHED'}


class DWOS_OT_clean_host_attributes(Operator):
    bl_idname='dwos.clean_host_attributes'
    bl_label='Очистить служебные атрибуты стены'
    bl_description='Удалить только DWOS_* из Host, если ни одно редактируемое окно больше не зависит от стены'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        obj=context.active_object
        return context.mode=='OBJECT' and obj is not None and obj.type=='MESH' and (not obj.dwos_window.is_window or obj.dwos_window.finalized)

    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)

    def execute(self,context):
        obj=context.active_object
        host=bpy.data.objects.get(obj.dwos_window.host_name) if obj.dwos_window.is_window else obj
        if host is None or host.type!='MESH':
            self.report({'ERROR'},'Исходный Host не найден');return {'CANCELLED'}
        dependents=[item for item in context.scene.objects if item.dwos_window.is_window
                    and item.dwos_window.host_name==host.name and not item.dwos_window.finalized]
        if dependents:
            self.report({'ERROR'},f'Очистка заблокирована: {len(dependents)} окон/архивных источников связаны со стеной')
            return {'CANCELLED'}
        if host.data.library or host.library:
            self.report({'ERROR'},'Linked Host: создайте локальную копию')
            return {'CANCELLED'}
        if host.data.users>1:
            self.report({'ERROR'},'Mesh используется несколькими объектами: сначала сделайте Single User')
            return {'CANCELLED'}
        stats=preview_counts([host])
        _strip_dwos(host.data)
        for key in owned_names(list(host.keys())):
            del host[key]
        log_event('HOST_DWOS_ATTRIBUTES_CLEANED',host=host.name,summary=stats)
        self.report({'INFO'},f'Удалены только DWOS metadata: {stats}. UV и материалы сохранены.')
        return {'FINISHED'}


CLASSES=(DWOS_OT_repair_orientation,DWOS_OT_finalize_clean,DWOS_OT_clean_host_attributes)
