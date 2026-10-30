"""Single-purpose parameter dialogs (Blender native, no overlapped GPU dropdown).

Only one small context is shown. Parameters are snapshotted and committed
through the existing undo-aware production operator; never mutate live scene
settings from draw().
"""
import bpy
from .build_context import object_mode_error
from ..core.undo_transactions import commit_definition_patch
from ..core.definitions import (definition_for, snapshot_definition, restore_definition,
                                sync_scene_from_definition)

def resolve_selection(collection, name, label):
    """Resolve an operator-local datablock name without changing Blender data.

    An empty name explicitly clears an optional ID field. A stale/renamed
    datablock is never silently discarded: abort before the Undo transaction.
    """
    if not name:
        return None
    result = collection.get(name)
    if result is None:
        raise ValueError(f'{label}: объект «{name}» не найден. Выберите актуальный элемент.')
    return result

_KINDS=(
    ('WALL','Стена','Режим, сторона, направление и модульная сетка'),
    ('CAP','Верхняя крышка','Завершение стены и поворот сетки крышки'),
    ('PLANT','Клумба','Внутренняя поверхность и её отступы'),
    ('SURFACE','Поверхность','Модульная сетка, высота и нормали'),
    ('CORNICE','Карниз','Профиль и ориентация'),
)

class UVWALL_OT_context_editor(bpy.types.Operator):
    bl_idname='uvwall.context_editor'
    bl_label='Параметры UV Wall'
    bl_description='Редактирование только выбранной группы параметров'
    bl_options={'REGISTER'}
    kind:bpy.props.EnumProperty(items=_KINDS, options={'HIDDEN'})
    double:bpy.props.BoolProperty(name='Двойная стена')
    alignment:bpy.props.EnumProperty(name='Расположение',items=(('INSIDE','Внутрь','По внутренней стороне'),('CENTER','Центр',''),('OUTSIDE','Наружу','По наружной стороне')))
    original_alignment:bpy.props.StringProperty(options={'HIDDEN'},default='')
    direction:bpy.props.EnumProperty(name='Строить',items=(('DOWN','Вниз','От линии вниз'),('UP','Вверх','От линии вверх')))
    wall_rotation:bpy.props.FloatProperty(name='Поворот модулей стены',precision=3)
    wall_offset:bpy.props.FloatProperty(name='Смещение стены',precision=4)
    cap_enabled:bpy.props.BoolProperty(name='Верхняя крышка')
    cap_rotation:bpy.props.FloatProperty(name='Поворот модулей крышки',precision=3)
    cap_material_override:bpy.props.BoolProperty(name='Отдельный материал крышки')
    cap_material_name:bpy.props.StringProperty(name='Материал верхней крышки',default='')
    bottom_cap_enabled:bpy.props.BoolProperty(name='Нижняя крышка')
    bottom_cap_material_override:bpy.props.BoolProperty(name='Отдельный материал снизу')
    bottom_cap_material_name:bpy.props.StringProperty(name='Материал нижней крышки',default='')
    plant_enabled:bpy.props.BoolProperty(name='Создать поверхность грунта')
    plant_auto:bpy.props.BoolProperty(name='Автоматический отступ внутрь')
    plant_extra:bpy.props.FloatProperty(name='Дополнительный отступ внутрь',precision=4)
    plant_z:bpy.props.FloatProperty(name='Отступ по высоте от верха',precision=4)
    # Blender Operator RNA cannot store Material ID datablocks directly.
    # Use a name-based UI selector; resolve to a real datablock at commit time.
    plant_material_name:bpy.props.StringProperty(name='Материал грунта',default='')
    surface_rotation:bpy.props.FloatProperty(name='Поворот модулей поверхности',precision=3)
    surface_z:bpy.props.FloatProperty(name='Смещение поверхности Z',precision=4)
    surface_normal:bpy.props.EnumProperty(name='Нормали',items=(('UP','Вверх','+Z'),('DOWN','Вниз','-Z')))
    # Blender Operator RNA cannot store Object datablocks directly.
    profile_name:bpy.props.StringProperty(name='Профиль карниза',default='')
    profile_rotation:bpy.props.FloatProperty(name='Поворот профиля',precision=3)
    cornice_flip:bpy.props.BoolProperty(name='Отразить сторону')

    @staticmethod
    def _source(context):
        settings=getattr(context.scene,'uv_wall_settings',None)
        path=settings.path_mesh if settings is not None else None
        return settings,path,definition_for(path) if path is not None else None

    def invoke(self,context,event):
        settings,path,d=self._source(context)
        if d is None:
            self.report({'ERROR'},'Сначала выберите Path в UV Wall')
            return {'CANCELLED'}
        self.double=d.create_inner;self.direction=d.story_direction
        self.original_alignment=str(d.wall_alignment)
        # Old values are invisible in the active UI, but an unrelated edit
        # must NOT silently migrate the construction to a different geometry.
        self.alignment=(d.wall_alignment if d.wall_alignment in {'INSIDE','CENTER','OUTSIDE'}
                        else 'INSIDE')
        self.wall_rotation=d.wall_module_rotation;self.wall_offset=d.wall_offset
        self.cap_enabled=d.create_top_cap;self.cap_rotation=d.cap_rotation
        self.cap_material_override=getattr(d,'cap_material_override',False)
        self.cap_material_name=(d.cap_material.name if getattr(d,'cap_material',None) is not None else '')
        self.bottom_cap_enabled=getattr(d,'create_bottom_cap',False)
        self.bottom_cap_material_override=getattr(d,'bottom_cap_material_override',False)
        self.bottom_cap_material_name=(d.bottom_cap_material.name if getattr(d,'bottom_cap_material',None) is not None else '')
        self.plant_enabled=d.create_inner_surface;self.plant_auto=d.inner_surface_offset_auto
        self.plant_extra=d.inner_surface_offset;self.plant_z=d.inner_surface_height_offset
        self.plant_material_name=(d.inner_surface_material.name if d.inner_surface_material is not None else '')
        self.surface_rotation=d.surface_module_rotation;self.surface_z=d.surface_height_offset
        self.surface_normal=d.roof_normal
        self.profile_name=(d.cornice_profile.name if d.cornice_profile is not None else '')
        self.profile_rotation=d.cornice_profile_rotation
        self.cornice_flip=d.cornice_flip_side
        return context.window_manager.invoke_props_dialog(self,width=355)

    def draw(self,context):
        col=self.layout.column(align=True)
        title=dict((key,label) for key,label,_ in _KINDS)[self.kind]
        col.label(text=title,icon='SETTINGS')
        if self.kind=='WALL':
            col.prop(self,'double')
            col.prop(self,'alignment')
            col.label(text='Сторона второй стены вычисляется из расположения автоматически.',icon='INFO')
            col.prop(self,'direction');col.prop(self,'wall_rotation');col.prop(self,'wall_offset')
        elif self.kind=='CAP':
            col.prop(self,'cap_enabled')
            if self.cap_enabled:
                col.prop(self,'cap_rotation')
                col.prop(self,'cap_material_override')
                if self.cap_material_override:
                    col.prop_search(self,'cap_material_name',bpy.data,'materials',text='Материал верхней крышки')
            col.prop(self,'bottom_cap_enabled')
            if self.bottom_cap_enabled:
                col.prop(self,'bottom_cap_material_override')
                if self.bottom_cap_material_override:
                    col.prop_search(self,'bottom_cap_material_name',bpy.data,'materials',text='Материал нижней крышки')
            if self.cap_enabled or self.bottom_cap_enabled:
                col.label(text='Double: только полоса между стенками.',icon='INFO')
                col.label(text='Single: поверхность замкнутого контура.')
        elif self.kind=='PLANT':
            col.prop(self,'plant_enabled')
            if self.plant_enabled:
                col.prop(self,'plant_auto');col.prop(self,'plant_extra')
                col.prop(self,'plant_z');col.prop(self,'surface_rotation')
                col.prop_search(self,'plant_material_name',bpy.data,'materials',text='Материал грунта')
                col.label(text='Отступ задаётся числом',icon='INFO')
        elif self.kind=='SURFACE':
            col.prop(self,'surface_rotation');col.prop(self,'surface_z')
            col.prop(self,'surface_normal')
        elif self.kind=='CORNICE':
            col.prop_search(self,'profile_name',bpy.data,'objects',text='Профиль карниза')
            col.prop(self,'profile_rotation');col.prop(self,'cornice_flip')
            col.label(text='Угловые маркеры пока редактируются через панель.',icon='INFO')
        col.separator(factor=.5)
        col.label(text='OK — применить и построить · Cancel — без изменений')

    def execute(self,context):
        settings,path,d=self._source(context)
        if d is None:return {'CANCELLED'}
        mode_error = object_mode_error(context)
        if mode_error:
            self.report({'ERROR'}, mode_error)
            return {'CANCELLED'}
        patch={}
        if self.kind=='WALL':
            from ..production.wall_alignment import automatic_inset_side
            new_alignment=self.alignment
            if (self.original_alignment not in {'INSIDE','CENTER','OUTSIDE'}
                    and self.alignment=='INSIDE'):
                new_alignment=self.original_alignment
            patch.update(create_inner=self.double,
                         inset_side=automatic_inset_side(new_alignment,getattr(d,'inset_side','LEFT')),
                         story_direction=self.direction, wall_alignment=new_alignment,
                         wall_module_rotation=self.wall_rotation, wall_offset=self.wall_offset)
            if not self.double:patch['create_inner_surface']=False
        elif self.kind=='CAP':
            try:
                cap_material=resolve_selection(bpy.data.materials,self.cap_material_name,'Материал верхней крышки') if self.cap_material_override else None
                bottom_material=resolve_selection(bpy.data.materials,self.bottom_cap_material_name,'Материал нижней крышки') if self.bottom_cap_material_override else None
            except ValueError as exc:
                self.report({'ERROR'},str(exc));return {'CANCELLED'}
            patch.update(create_top_cap=self.cap_enabled, cap_rotation=self.cap_rotation,
                         cap_material_override=self.cap_material_override, cap_material=cap_material,
                         create_bottom_cap=self.bottom_cap_enabled,
                         bottom_cap_material_override=self.bottom_cap_material_override,
                         bottom_cap_material=bottom_material)
        elif self.kind=='PLANT':
            try:
                material=resolve_selection(bpy.data.materials,self.plant_material_name,'Материал грунта')
            except ValueError as exc:
                self.report({'ERROR'},str(exc))
                return {'CANCELLED'}
            patch.update(create_inner_surface=self.plant_enabled,
                         inner_surface_offset_auto=self.plant_auto,
                         inner_surface_offset=self.plant_extra,
                         inner_surface_height_offset=self.plant_z,
                         surface_module_rotation=self.surface_rotation,
                         inner_surface_material=material)
            if self.plant_enabled:patch['create_inner']=True
        elif self.kind=='SURFACE':
            patch.update(surface_module_rotation=self.surface_rotation,
                         surface_height_offset=self.surface_z, roof_normal=self.surface_normal)
            # Preserve hidden legacy TRIANGULATED mode for existing saved .blend;
            # new Definitions default to SMOOTH_LEGACY. No silent migration.
        elif self.kind=='CORNICE':
            try:
                profile=resolve_selection(bpy.data.objects,self.profile_name,'Профиль карниза')
            except ValueError as exc:
                self.report({'ERROR'},str(exc))
                return {'CANCELLED'}
            patch.update(cornice_profile=profile,
                         cornice_profile_rotation=self.profile_rotation,
                         cornice_flip_side=self.cornice_flip)
            if profile is None:
                self.report({'INFO'},'Назначьте профиль перед построением карниза')
                return {'CANCELLED'}
        try:
            commit_definition_patch(context,d,patch)
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

CLASSES=(UVWALL_OT_context_editor,)
