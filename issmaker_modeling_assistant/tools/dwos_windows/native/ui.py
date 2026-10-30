from __future__ import annotations

import os
import json
import subprocess
import sys
import bpy
from bpy.props import BoolProperty, StringProperty

from .numeric import parse_meter_text, format_meter
from .opening_groups import group_sizes
from .dwos_log import event as log_event, log_dir
from .batch_edit import resolve_window, target_windows
from .health_blender import health_of, related_names
from .corner_post_policy_core import evaluate_corner_post


class DWOS_UL_openings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'enabled', text='')
        row.label(text=f'{item.width_text} × {item.height_text}')
        try:
            payload=json.loads(item.payload_json or '{}')
            cls=payload.get('adaptive_classification','PLANAR')
        except Exception:
            cls='PLANAR'
        if cls in {'CURVED','CORNER','COMPLEX'}:
            row.label(text=cls)


def _groups(state):
    sizes = []
    for i, item in enumerate(state.openings):
        try:
            sizes.append((i, parse_meter_text(item.width_text), parse_meter_text(item.height_text)))
        except Exception:
            continue
    return group_sizes(sizes)


class DWOS_OT_toggle_opening_group(bpy.types.Operator):
    bl_idname = 'dwos.toggle_opening_group'
    bl_label = 'Включить/выключить группу проёмов'
    bl_options = {'INTERNAL'}
    indices: StringProperty(default='')
    enable: BoolProperty(default=True)

    def execute(self, context):
        state = context.scene.dwos
        touched = 0
        for token in self.indices.split(','):
            try:
                idx = int(token)
            except ValueError:
                continue
            if 0 <= idx < len(state.openings):
                state.openings[idx].enabled = self.enable
                touched += 1
        log_event('OPENING_GROUP_TOGGLE', enable=self.enable, count=touched)
        return {'FINISHED'}


class DWOS_OT_open_log_folder(bpy.types.Operator):
    bl_idname = 'dwos.open_log_folder'
    bl_label = 'Открыть папку логов'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        folder = log_dir()
        try:
            if os.name == 'nt':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
            self.report({'INFO'}, 'Папка логов открыта')
        except Exception as exc:
            self.report({'WARNING'}, f'Логи: {folder}')
            log_event('OPEN_LOG_FOLDER_WARNING', error=repr(exc), folder=folder)
        return {'FINISHED'}


class DWOS_OT_health_locate(bpy.types.Operator):
    bl_idname = 'dwos.health_locate'
    bl_label = 'Найти связанные окна DWOS'
    bl_description = 'Выделить активное окно и известные связанные с ним окна в текущей сцене'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return resolve_window(getattr(context,'active_object',None)) is not None and context.mode == 'OBJECT'

    def execute(self, context):
        frame = resolve_window(context.active_object)
        names = set(related_names(frame)) | {frame.name}
        chosen = [o for o in context.scene.objects if o.name in names and
                  getattr(getattr(o,'dwos_window',None),'is_window',False)]
        if not chosen:
            return {'CANCELLED'}
        try:
            for o in context.selected_objects: o.select_set(False)
            for o in chosen: o.select_set(True)
            context.view_layer.objects.active = frame
        except (ReferenceError, RuntimeError) as exc:
            self.report({'WARNING'},f'Не удалось выделить связанные окна: {exc}')
            return {'CANCELLED'}
        self.report({'INFO'},f'Выделено связанных окон DWOS: {len(chosen)}')
        return {'FINISHED'}


class DWOS_PT_main(bpy.types.Panel):
    bl_label = 'DWOS Legacy · Совместимость'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 90
    bl_idname = 'DWOS_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'DWOS'

    def draw(self, context):
        layout=self.layout
        state=context.scene.dwos
        window=resolve_window(context.active_object)
        layout.prop(state,'show_studio_section',text='РАБОЧАЯ СТУДИЯ',toggle=True,
                    icon='DOWNARROW_HLT' if state.show_studio_section else 'RIGHTARROW')
        if state.show_studio_section:
            studio=layout.box()
            lod=studio.row(align=True)
            lod.label(text='LOD:')
            for mode,title in (('HP','HP'),('LP','LP'),('BOTH','BOTH'),('NONE','HIDE')):
                op=lod.operator('dwos.set_lod_view',text=title,depress=state.lod_view_mode==mode)
                op.mode=mode
            if window is not None:
                s=window.dwos_window
                studio.operator('dwos.viewport_composer',text='ОТКРЫТЬ DWOS STUDIO',icon='ORIENTATION_VIEW')
                cls=str(window.get('DWOS_adaptive_classification','PLANAR')).upper()
                studio.label(text=f'{cls} · {s.width_text} × {s.height_text} м')
                row=studio.row(align=True)
                a=row.operator('dwos.composer_set_optimization',text='FULL',depress=s.optimization=='FULL')
                a.mode='FULL'
                b=row.operator('dwos.composer_set_optimization',text='OPTIMIZED',depress=s.optimization=='OPTIMIZED')
                b.mode='OPTIMIZED'
                if cls in {'CORNER','CURVED'}:
                    studio.label(text='OPTIMIZED: только для PLANAR',icon='INFO')
                    studio.label(text='CORNER/CURVED сохраняют FULL + 2 стекла')
                if cls!='CORNER' and s.interior_mode!='OFF':
                    box=studio.box()
                    box.label(text='INTERIOR · РУЧНЫЕ ГРАНИЦЫ',icon='MOD_ARRAY')
                    box.label(text='Изначально 1 · каждый V отмечает конец')
                    box.prop(s,'interior_partition_mode',text='Режим')
                    if s.interior_partition_mode=='COUNT':
                        box.prop(s,'interior_groups_count',text='Секций')
                    box.prop(s,'interior_joint_gap_text',text='Мин. зазор у V, м')
                    box.operator('dwos.interior_partition_apply',text='Применить Interior',icon='CHECKMARK')
                    row=box.row(align=True)
                    row.operator('dwos.interior_cuts_bulk',text='Все V').enable=True
                    row.operator('dwos.interior_cuts_bulk',text='Сбросить до 1').enable=False
                    try:
                        from .window_builder import _splits_from_settings
                        from .interior_sections_core import full_height_bands
                        from .interior_authored_sections_core import effective_cut_ids
                        from .zone_core import decode,layout as zone_layout
                        w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
                        fw=parse_meter_text(s.frame_width_text)
                        z=decode(s.zone_tree_json) if s.zone_tree_json else None
                        eligible=full_height_bands(_splits_from_settings(s,w,h),
                            zone_layout(z,w,h,fw)[1] if z else [],w,h,fw)
                        mode=str(s.interior_partition_mode)
                        selected=set(effective_cut_ids(eligible,(-w/2+fw,w/2-fw,-h/2+fw,h/2-fw),
                            mode,int(s.interior_groups_count),s.interior_dividers_json))
                        box.label(text=f'Interior: {len(selected)+1} групп · границ: {len(selected)}')
                        box.label(text=f'Связных Interior: {len(selected)+1} · V позади профиля')
                        box.label(text='Здесь конец Interior: ножницы возле V',icon='INFO')
                        if len(eligible)>9:
                            box.label(text=f'Видимость по наведению: {len(eligible)} V',icon='VIEWZOOM')
                    except (ValueError,TypeError,KeyError) as exc:
                        line=box.row();line.alert=True
                        line.label(text='V: '+str(exc)[:75],icon='ERROR')
                row=studio.row(align=True)
                for scope,title in (('ACTIVE','ONE'),('SELECTED','SEL'),('SAME_SIZE','SAME'),('ALL_HOST','ALL')):
                    op=row.operator('dwos.batch_set_scope',text=title,depress=state.batch_scope==scope)
                    op.scope=scope
                studio.operator('dwos.batch_copy_splits',text='COPY раскладки',icon='MOD_ARRAY')
            else:
                studio.label(text='Выбери Mesh стены или окно DWOS',icon='INFO')
                studio.operator('dwos.find_openings',text='НАЙТИ ПРОЁМЫ',icon='VIEWZOOM')
                if state.status:studio.label(text=state.status[:90])
                for group in _groups(state):
                    row=studio.row(align=True)
                    ids=','.join(map(str,group.indices))
                    enabled=all(state.openings[i].enabled for i in group.indices)
                    op=row.operator('dwos.toggle_opening_group',text='',icon='CHECKBOX_HLT' if enabled else 'CHECKBOX_DEHLT')
                    op.indices=ids;op.enable=not enabled
                    row.label(text=f'{format_meter(group.width)} × {format_meter(group.height)} · {group.count}')
                count=sum(bool(c.enabled) for c in state.openings)
                if count:studio.operator('dwos.create_windows',text=f'СОЗДАТЬ ОКНА · {count}',icon='MESH_GRID')
        layout.prop(state,'show_diagnostics_section',text='ДИАГНОСТИКА',toggle=True,
                    icon='DOWNARROW_HLT' if state.show_diagnostics_section else 'RIGHTARROW')
        if state.show_diagnostics_section:
            diag=layout.box()
            diag.operator('dwos.audit_mesh_consistency',text='Аудит рам и стекла',icon='CHECKMARK')
            diag.operator('dwos.analyze_meshes',text='Полный Mesh Analysis',icon='VIEWZOOM').reason='MANUAL_RELEASE'
            diag.operator('dwos.validate_scene_interiors',text='Аудит Interior',icon='CHECKMARK')
            diag.operator('dwos.export_field_bundle',text='Один клик · ZIP диагностики',icon='PACKAGE')
            from .surface_steps import draw_diagnostic
            draw_diagnostic(diag, context)
            diag.label(text='ТЕСТОВЫЕ ПРОЁМЫ',icon='MESH_GRID')
            row=diag.row(align=True);row.enabled=context.mode=='OBJECT'
            row.operator('dwos.create_fixture_gallery',text='15 обычных').variant='STANDARD'
            row.operator('dwos.create_fixture_gallery',text='15 длинных').variant='LONG'
            diag.operator('dwos.create_interior_fixture_gallery',text='5 стен Interior',icon='ADD')
            obj=context.active_object
            if obj and obj.get('DWOS_fixture_schema')=='DWOS_fixture_interior_contact_v1':
                row=diag.row(align=True)
                row.operator('dwos.check_interior_fixture_gallery',text='Проверить I01–I05')
                row.operator('dwos.run_interior_fixture_auto',text='AUTO 5 стен')
                row=diag.row(align=True)
                for i in range(1,6):row.operator('dwos.focus_fixture_case',text=f'I{i:02d}').case_id=f'I{i:02d}'
            if obj and obj.get('DWOS_fixture_schema') in {'DWOS_fixture_gallery_v1','DWOS_fixture_gallery_long_v1'}:
                diag.operator('dwos.check_fixture_gallery',text='Проверить 15 проёмов')
                for kind in ('P','R','A'):
                    row=diag.row(align=True)
                    for i in range(1,6):row.operator('dwos.focus_fixture_case',text=f'{kind}{i:02d}').case_id=f'{kind}{i:02d}'
        layout.prop(state,'show_service_section',text='СЕРВИС',toggle=True,
                    icon='DOWNARROW_HLT' if state.show_service_section else 'RIGHTARROW')
        if state.show_service_section:
            service=layout.box()
            # Smart Interior is proven in the field; keep AUTO on but hide its knobs.
            service.label(text='SMART INTERIOR · фоновый режим',icon='MOD_BOOLEAN')
            service.prop(state,'smart_interior_enabled',text='Автоматическое согласование')
            if not state.smart_interior_enabled:
                service.label(text='Авто сейчас выключено',icon='INFO')
            service.prop(state,'smart_interior_gap',text='Зазор, м')
            service.operator('dwos.set_interior_gap_003',text='Зазор 0,03 м')
            service.prop(state,'smart_interior_mode',text='Алгоритм')
            service.prop(state,'manual_interior_strategy',text='Ручной метод')
            service.operator('dwos.manual_interior_repair',text='Ручное исправление',icon='TOOL_SETTINGS')
            if window is not None and window.get('DWOS_smart_interior_pending',False):
                service.operator('dwos.rebuild_smart_interiors',text='Повторить согласование',icon='FILE_REFRESH')
            service.operator('dwos.open_log_folder',text='Открыть логи',icon='FILE_FOLDER')
            if window is not None:
                s=window.dwos_window
                if not s.finalized:
                    service.prop(s,'frame_material',text='Материал рамы')
                    service.operator('dwos.finalize_clean',text='Финализировать',icon='CHECKMARK')
                else:
                    service.label(text='Окно финализировано',icon='CHECKMARK')
            service.operator('dwos.clean_host_attributes',text='Очистить атрибуты',icon='BRUSH_DATA')
            col=service.column(align=True)
            col.enabled=bool(context.active_object and context.active_object.type=='MESH' and window is None)
            col.operator('dwos.inspect_wall_mesh',text='Анализ Mesh стены',icon='MESH_DATA')
            manual=col.column(align=True)
            manual.enabled=bool(context.active_object and context.active_object.mode=='EDIT')
            manual.operator('dwos.inspect_manual_openings',text='Анализ выделения',icon='EDGESEL')
            manual.operator('dwos.use_selected_openings',text='Принять ручные контуры',icon='CHECKMARK')


class DWOS_OT_interior_partition_apply(bpy.types.Operator):
    bl_idname='dwos.interior_partition_apply'
    bl_label='Применить физические Interior'
    bl_description='Перестроить Interior по выбранным границам; безопасный отказ без изменения Mesh'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        obj=context.active_object
        if obj is not None and obj.get('DWOS_role')=='INTERIOR':obj=obj.parent
        if obj is None or not getattr(obj.dwos_window,'is_window',False):
            self.report({'ERROR'},'Сначала выберите окно DWOS');return {'CANCELLED'}
        try:
            if str(obj.get('DWOS_adaptive_classification'))=='CORNER':
                raise ValueError('CORNER: разделение Interior по V недоступно')
            from .window_builder import rebuild_window
            rebuild_window(obj)
            st=obj.dwos_window
            self.report({'INFO'},f'Interior: применено {st.interior_partition_mode}; смотрите реальные секции')
            return {'FINISHED'}
        except Exception as exc:
            log_event('INTERIOR_PARTITION_APPLY_REJECTED',window=obj.name,error=repr(exc))
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class DWOS_OT_interior_cuts_bulk(bpy.types.Operator):
    bl_idname='dwos.interior_cuts_bulk'
    bl_label='Выбрать все или очистить границы Interior'
    bl_options={'REGISTER','UNDO'}
    enable: BoolProperty(default=True)
    def execute(self,context):
        obj=context.active_object
        if obj is not None and obj.get('DWOS_role')=='INTERIOR':obj=obj.parent
        if obj is None or not getattr(obj.dwos_window,'is_window',False):return {'CANCELLED'}
        s=obj.dwos_window
        try:
            if str(obj.get('DWOS_adaptive_classification',''))=='CORNER':
                raise ValueError('CORNER: разделение Interior по V отключено')
            from .window_builder import _splits_from_settings,rebuild_window
            from .interior_sections_core import full_height_bands
            from .zone_core import decode,layout
            from . import dwos_history
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            z=decode(s.zone_tree_json) if s.zone_tree_json else None
            bands=full_height_bands(_splits_from_settings(s,w,h),layout(z,w,h,fw)[1] if z else [],w,h,fw)
            old_mode=s.interior_partition_mode;old_ids=s.interior_dividers_json
            new_ids=[b[0] for b in bands] if self.enable else []
            dwos_history.push(context,'INTERIOR_DIVIDERS_ALL' if self.enable else 'INTERIOR_DIVIDERS_CLEAR')
            s.interior_partition_mode='CUSTOM';s.interior_dividers_json=json.dumps(new_ids)
            try:rebuild_window(obj)
            except Exception:
                s.interior_partition_mode=old_mode;s.interior_dividers_json=old_ids
                raise
            self.report({'INFO'},f'Interior: {len(new_ids)+1} физических секций')
            return {'FINISHED'}
        except Exception as exc:
            log_event('INTERIOR_DIVIDERS_BULK_REJECTED',window=obj.name,error=repr(exc))
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


class DWOS_OT_interior_cut_toggle(bpy.types.Operator):
    bl_idname='dwos.interior_cut_toggle'
    bl_label='Выбрать V как разделитель Interior'
    bl_description='Переключить физическую границу Interior по данному сквозному импосту'
    bl_options={'REGISTER','UNDO'}
    cut_id: StringProperty(default='')
    def execute(self,context):
        obj=context.active_object
        if obj is not None and obj.get('DWOS_role')=='INTERIOR':obj=obj.parent
        if obj is None or not getattr(obj.dwos_window,'is_window',False):return {'CANCELLED'}
        s=obj.dwos_window
        try:
            from .interior_authored_sections_core import toggled_cut_ids
            from .interior_sections_core import full_height_bands
            from .window_builder import _splits_from_settings,rebuild_window
            from .zone_core import decode,layout
            from . import dwos_history
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            z=decode(s.zone_tree_json) if s.zone_tree_json else None
            eligible=full_height_bands(_splits_from_settings(s,w,h),layout(z,w,h,fw)[1] if z else [],w,h,fw)
            old_mode=s.interior_partition_mode;old_ids=s.interior_dividers_json
            before,chosen=toggled_cut_ids(eligible,(-w/2+fw,w/2-fw,-h/2+fw,h/2-fw),
                old_mode,int(s.interior_groups_count),old_ids,self.cut_id)
            enabled=str(self.cut_id) in chosen
            log_event('INTERIOR_CUT_TOGGLE_BEGIN',window=obj.name,cut_id=self.cut_id,
                before_mode=old_mode,before=before,after=chosen,enabled=enabled)
            dwos_history.push(context,'INTERIOR_CUT_TOGGLE')
            s.interior_partition_mode='CUSTOM';s.interior_dividers_json=json.dumps(chosen)
            try:rebuild_window(obj)
            except Exception:
                s.interior_partition_mode=old_mode;s.interior_dividers_json=old_ids
                raise
            log_event('INTERIOR_CUT_TOGGLE_APPLIED',window=obj.name,cut_id=self.cut_id,
                enabled=enabled,groups=len(chosen)+1,physical_shells=len(chosen)+1,
                note='Interior follows actual V width; unselected V has no white returns')
            self.report({'INFO'},f'Interior: {len(chosen)+1} связных оболочек; '+
                                  ('граница добавлена' if enabled else 'граница удалена'))
            return {'FINISHED'}
        except Exception as exc:
            log_event('INTERIOR_CUT_TOGGLE_REJECTED',window=obj.name,error=repr(exc))
            self.report({'ERROR'},str(exc)[:220]);return {'CANCELLED'}


CLASSES = (
    DWOS_UL_openings,
    DWOS_OT_toggle_opening_group,
    DWOS_OT_open_log_folder,
    DWOS_OT_interior_partition_apply,
    DWOS_OT_interior_cut_toggle,
    DWOS_OT_interior_cuts_bulk,
    DWOS_OT_health_locate,
    DWOS_PT_main,
)
