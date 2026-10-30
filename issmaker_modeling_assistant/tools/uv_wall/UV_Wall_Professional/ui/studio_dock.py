"""Studio Dock: viewport-docked inspector with native Blender property popups.

Only the view layer differs from Floating Pro. The source Definition and the
existing production/Undo operators remain authoritative. All long editing is
explicitly committed by the existing Build operator; the advanced transactional
editor remains available for conflict-safe multi-field Apply.
"""
import bpy
from ..core.definitions import definition_for, sync_scene_from_definition
from ..viewport.resource_picker import validate_resource



class UVWALL_OT_studio_set_mode(bpy.types.Operator):
    """Choose generator without rebuilding production geometry."""
    bl_idname="uvwall.studio_set_mode"
    bl_label="Режим UV Wall"
    bl_options={"REGISTER","UNDO"}
    mode:bpy.props.EnumProperty(items=(("WALL","Стена",""),("SURFACE","Поверхность",""),("CORNICE","Карниз","")))

    def execute(self,context):
        settings=getattr(context.scene,"uv_wall_settings",None)
        path=getattr(settings,"path_mesh",None)
        d=definition_for(path) if path is not None else None
        if d is None:
            self.report({"WARNING"},"Сначала выберите Path")
            return {"CANCELLED"}
        if d.generator_type == self.mode:
            return {"FINISHED"}
        if self.mode=="SURFACE":
            from ..core.definitions import analyze_path
            analysis=analyze_path(path)
            if not analysis.get("closed_components") or analysis.get("intersections"):
                self.report({"WARNING"},"Поверхность требует замкнутого пути без пересечений")
                return {"CANCELLED"}
        d.generator_type=self.mode
        d.revision+=1
        sync_scene_from_definition(path,settings)
        from ..viewport.controller import _ACTIVE_SESSIONS
        for session in tuple(_ACTIVE_SESSIONS):
            try:
                if session._path()==path and session._engine is not None:
                    from ..preview.engine import Dirty
                    session._engine.invalidate(Dirty.ALL)
            except (AttributeError,ReferenceError,RuntimeError):
                continue
        return {"FINISHED"}


class UVWALL_OT_studio_build(bpy.types.Operator):
    """Synchronize the common Definition into existing, tested production Build."""
    bl_idname="uvwall.studio_build"
    bl_label="Применить / построить"
    bl_description="Применить те же параметры, что в Floating Pro, через штатный production-оператор"
    bl_options={"REGISTER"}

    @classmethod
    def poll(cls,context):
        settings=getattr(getattr(context,"scene",None),"uv_wall_settings",None)
        return bool(settings is not None and getattr(settings,"path_mesh",None) is not None
                    and getattr(context,"mode",None)=="OBJECT")

    def execute(self,context):
        s=context.scene.uv_wall_settings
        d=definition_for(s.path_mesh)
        if d is None:
            self.report({"ERROR"},"Нет текущего Path Definition")
            return {"CANCELLED"}
        sync_scene_from_definition(s.path_mesh,s)
        try:
            outcome=bpy.ops.object.build_uv_wall_along_path('EXEC_DEFAULT')
        except (RuntimeError,TypeError,ValueError) as exc:
            self.report({"ERROR"},str(exc))
            return {"CANCELLED"}
        return {"FINISHED"} if 'FINISHED' in outcome else {"CANCELLED"}


# The former N-panel renderer is intentionally removed: Studio Dock now
# has a dedicated viewport GPU view, with these operators as native inputs.


class UVWALL_OT_hover_palette(bpy.types.Operator):
    """Full Blender HSV/RGB color wheel; confirms atomically, Esc cancels."""
    bl_idname = "uvwall.hover_palette"
    bl_label = "Палитра цвета наведения"
    bl_description = "Выбрать произвольный цвет наведения с помощью цветового круга Blender"
    bl_options = {"REGISTER", "UNDO"}

    color: bpy.props.FloatVectorProperty(
        name="Цвет", size=3, subtype='COLOR', min=0.0, max=1.0,
        default=(1.0, .20, .17),
    )

    def invoke(self, context, event):
        self.color = tuple(context.scene.uv_wall_settings.viewport_hover_color)
        return context.window_manager.invoke_props_dialog(self, width=330)

    def draw(self, context):
        layout=self.layout
        layout.label(text="Цвет маркеров и подсветки во Viewport", icon="COLOR")
        layout.template_color_picker(self, "color", value_slider=True)
        layout.prop(self, "color", text="RGB / HEX")
        layout.label(text="ОК — применить, Esc — отменить", icon="INFO")

    def execute(self,context):
        settings=context.scene.uv_wall_settings
        settings.viewport_hover_color=tuple(self.color)
        for area in tuple(getattr(getattr(context,"screen",None),"areas",())):
            if area.type=='VIEW_3D':area.tag_redraw()
        return {"FINISHED"}


_DOCK_EDITABLE = {
    'stories', 'inset_distance','wall_offset','wall_module_rotation',
    'surface_height_offset','surface_module_rotation','cap_rotation',
    'inner_surface_height_offset','cornice_profile_rotation',
    'first_story_height','cornice_linear_step',
}
_DOCK_CHOICES = {
    'module_standard':(('2','2 × 2 м'),('4','4 × 4 м')),
    'wall_alignment':(('INSIDE','Внутрь'),('CENTER','Центр'),('OUTSIDE','Наружу')),
    'roof_normal':(('UP','Вверх'),('DOWN','Вниз')),
    'story_direction':(('UP','Вверх'),('DOWN','Вниз')),
    'inset_side':(('LEFT','Слева'),('RIGHT','Справа')),
    'surface_method':(('SMOOTH_LEGACY','Плавная'),('TRIANGULATED','Треугольная')),
    'cornice_uv_mode':(('MAX_REAL_WORLD','Непрерывная'),('MAX_CLIPPED','Острый угол')),
}
_DOCK_TOGGLES = {
    'create_inner','create_top_cap','cap_material_override','close_start_end','close_finish_end',
    'create_bottom_cap','bottom_cap_material_override',
    'create_inner_surface','custom_first_story','close_ends','cornice_flip_side','cornice_profile_flip_x',
    'cornice_profile_flip_y','cornice_reverse_path',
}


def _dock_context(context):
    settings=context.scene.uv_wall_settings
    path=getattr(settings,'path_mesh',None)
    definition=definition_for(path) if path is not None else None
    return settings,path,definition


def _dock_refresh(context, path, settings):
    sync_scene_from_definition(path,settings)
    from ..viewport.controller import _ACTIVE_SESSIONS
    from ..preview.engine import Dirty
    for session in tuple(_ACTIVE_SESSIONS):
        try:
            if session._path()==path and session._engine is not None:
                session._engine.invalidate(Dirty.ALL)
                session._engine.update()
        except (AttributeError,ReferenceError,RuntimeError,ValueError):
            continue


class UVWALL_OT_inner_height_point(bpy.types.Operator):
    """Inert legacy entry point; all secondary geometry comes from Offset Edges."""
    bl_idname='uvwall.inner_height_point'
    bl_label='Устаревший редактор дополнительной стены'
    bl_options={'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return False

    def execute(self, context):
        self.report({'WARNING'}, 'Точки дополнительной стены создаются автоматически через Offset Edges')
        return {'CANCELLED'}


class UVWALL_OT_dock_field(bpy.types.Operator):
    """Native input dialog for a dock row, independent of floating editors."""
    bl_idname='uvwall.dock_field'
    bl_label='Изменить параметр'
    bl_options={'REGISTER','UNDO'}

    field:bpy.props.StringProperty(options={'HIDDEN'})
    value_float:bpy.props.FloatProperty(name='Значение',precision=4)
    value_int:bpy.props.IntProperty(name='Значение')
    value_distance:bpy.props.FloatProperty(name='Расстояние',unit='LENGTH',subtype='DISTANCE',precision=4)
    value_angle:bpy.props.FloatProperty(name='Угол',unit='ROTATION',subtype='ANGLE',precision=2)

    def invoke(self,context,event):
        _settings,_path,d=_dock_context(context)
        if d is None or self.field not in _DOCK_EDITABLE:return {'CANCELLED'}
        v=getattr(d,self.field)
        if self.field=='stories':self.value_int=int(v)
        elif self.field in {'inset_distance','wall_offset','surface_height_offset','inner_surface_height_offset','first_story_height','cornice_linear_step'}:
            self.value_distance=float(v)
        elif self.field in {'wall_module_rotation','surface_module_rotation','cap_rotation','cornice_profile_rotation'}:
            import math
            self.value_angle=math.radians(float(v))
        else:self.value_float=float(v)
        return context.window_manager.invoke_props_dialog(self,width=300)

    def draw(self,context):
        field=('value_int' if self.field=='stories'
               else 'value_distance' if self.field in {'inset_distance','wall_offset','surface_height_offset','inner_surface_height_offset','first_story_height','cornice_linear_step'}
               else 'value_angle' if self.field in {'wall_module_rotation','surface_module_rotation','cap_rotation','cornice_profile_rotation'}
               else 'value_float')
        self.layout.label(text='Параметр · '+self.field)
        self.layout.prop(self,field,text='')
        self.layout.label(text='Изменение не применяется до подтверждения',icon='INFO')

    def execute(self,context):
        s,path,d=_dock_context(context)
        if d is None or self.field not in _DOCK_EDITABLE:return {'CANCELLED'}
        if self.field=='stories':value=self.value_int
        elif self.field in {'inset_distance','wall_offset','surface_height_offset','inner_surface_height_offset','first_story_height','cornice_linear_step'}:
            value=self.value_distance
        elif self.field in {'wall_module_rotation','surface_module_rotation','cap_rotation','cornice_profile_rotation'}:
            import math
            value=math.degrees(self.value_angle)
        else:value=self.value_float
        if self.field=='stories' and not 1<=value<=256:
            self.report({'WARNING'},'Количество секций: 1…500');return {'CANCELLED'}
        if self.field in {'inset_distance','cornice_linear_step'} and value<=0:
            self.report({'WARNING'},'Толщина должна быть положительной');return {'CANCELLED'}
        if self.field.endswith('rotation') or self.field=='cap_rotation':
            if not -180<=value<=180:
                self.report({'WARNING'},'Поворот: −180…180°');return {'CANCELLED'}
        setattr(d,self.field,value)
        d.revision+=1
        _dock_refresh(context,path,s)
        return {'FINISHED'}


class UVWALL_OT_dock_switch(bpy.types.Operator):
    """Atomic checkbox/enum changes, without rebuilding the Result."""
    bl_idname='uvwall.dock_switch'
    bl_label='Переключить параметр'
    bl_options={'REGISTER','UNDO'}
    field:bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self,context):
        s,path,d=_dock_context(context)
        if d is None:return {'CANCELLED'}
        if self.field in _DOCK_TOGGLES:
            setattr(d,self.field,not bool(getattr(d,self.field)))
        elif self.field in _DOCK_CHOICES:
            choices=[key for key,_ in _DOCK_CHOICES[self.field]]
            v=getattr(d,self.field)
            setattr(d,self.field,choices[(choices.index(v)+1)%len(choices)] if v in choices else choices[0])
        else:return {'CANCELLED'}
        d.revision+=1
        _dock_refresh(context,path,s)
        return {'FINISHED'}

class UVWALL_OT_dock_resource(bpy.types.Operator):
    """Searchable, exact-field resource picker; no unreliable ID-pointer dialog."""
    bl_idname='uvwall.dock_resource'
    bl_label='Ресурс · UV Wall'
    bl_description='Найти ресурс по имени, выбрать из сцены или очистить поле'
    bl_options={'REGISTER','UNDO'}
    field:bpy.props.StringProperty(options={'HIDDEN'})
    resource_name:bpy.props.StringProperty(name='Ресурс',description='Имя ресурса Blender Data')

    _FIELDS={
        'cap_material':('Материал верхней крышки','materials','MATERIAL'),
        'bottom_cap_material':('Материал нижней крышки','materials','MATERIAL'),
        'inner_surface_material':('Материал грунта','materials','MATERIAL'),
        'cornice_profile':('Профиль карниза','objects','PROFILE'),
    }

    def invoke(self,context,event):
        _s,_p,d=_dock_context(context)
        if d is None or self.field not in self._FIELDS:
            self.report({'WARNING'},'Для выбора ресурса сначала назначьте Path')
            return {'CANCELLED'}
        existing=getattr(d,self.field,None)
        self.resource_name=existing.name if existing is not None else ''
        return context.window_manager.invoke_props_dialog(self,width=440)

    def draw(self,context):
        layout=self.layout
        definition=_dock_context(context)[2]
        label,collection,kind=self._FIELDS.get(self.field,('Ресурс','objects','PROFILE'))
        layout.label(text=label,icon='OUTLINER_OB_GROUP_INSTANCE' if kind=='PROFILE' else 'MATERIAL')
        # Blender's native searchable collection field is visible even on the
        # initial None selection.  PointerProperty ID-fields were blank in the
        # actual Studio Dock dialog on Blender 4.5 (reported field failure).
        layout.prop_search(self,'resource_name',bpy.data,collection,text='Поиск')
        actions=layout.row(align=True)
        op=actions.operator('uvwall.dock_resource_action',text='Из выделенного',icon='EYEDROPPER')
        op.field=self.field;op.action='ACTIVE'
        op=actions.operator('uvwall.dock_resource_action',text='Очистить',icon='X')
        op.field=self.field;op.action='CLEAR'
        if kind=='PROFILE':
            layout.label(text='Mesh · одна цепь ребер · локальная плоскость XZ',icon='INFO')
            layout.label(text='Origin рядом с вершиной профиля (2 мм)')
        current=getattr(definition,self.field,None) if definition is not None else None
        layout.label(text='Текущий: '+(current.name if current is not None else 'Не выбран'))
        layout.label(text='OK применяет только это поле, Cancel оставляет старое')

    def execute(self,context):
        s,path,d=_dock_context(context)
        if d is None or self.field not in self._FIELDS:return {'CANCELLED'}
        _label,collection,kind=self._FIELDS[self.field]
        entered=self.resource_name.strip()
        value=getattr(bpy.data,collection).get(entered) if entered else None
        if entered and value is None:
            self.report({'WARNING'},'Ресурс не найден: '+entered)
            return {'CANCELLED'}
        if value is not None:
            valid,reason,_meta=validate_resource(kind,value,path=path,definition=d)
            if not valid:
                self.report({'WARNING'},reason)
                return {'CANCELLED'}
        if getattr(d,self.field,None)==value:return {'FINISHED'}
        setattr(d,self.field,value)
        d.revision+=1
        _dock_refresh(context,path,s)
        return {'FINISHED'}


class UVWALL_OT_dock_resource_action(bpy.types.Operator):
    """Use scene selection or clear exact field; commits via shared Definition."""
    bl_idname='uvwall.dock_resource_action'
    bl_label='Ресурс из выделенного / очистить'
    bl_options={'REGISTER','UNDO'}
    field:bpy.props.StringProperty(options={'HIDDEN'})
    action:bpy.props.EnumProperty(items=(('ACTIVE','Из выделенного',''),('CLEAR','Очистить','')))

    def execute(self,context):
        s,path,d=_dock_context(context)
        meta=UVWALL_OT_dock_resource._FIELDS.get(self.field)
        if d is None or meta is None:return {'CANCELLED'}
        _label,_collection,kind=meta
        if self.action=='CLEAR':
            value=None
        elif kind=='PROFILE':
            value=getattr(context,'active_object',None)
        else:
            active=getattr(context,'active_object',None)
            value=getattr(active,'active_material',None) if active else None
        if self.action!='CLEAR' and value is None:
            self.report({'WARNING'},'Нет подходящего активного объекта/материала')
            return {'CANCELLED'}
        if value is not None:
            valid,reason,_meta=validate_resource(kind,value,path=path,definition=d)
            if not valid:
                self.report({'WARNING'},reason)
                return {'CANCELLED'}
        if getattr(d,self.field,None)==value:return {'FINISHED'}
        setattr(d,self.field,value)
        d.revision+=1
        _dock_refresh(context,path,s)
        self.report({'INFO'},'Ресурс: '+(value.name if value is not None else 'очищен'))
        return {'FINISHED'}

# 1.16.0: a native Blender dialog for editing preset names and choosing one of
# the active mode's saved presets. Only validated primitive Definition values
# are saved; scene pointers, user geometry and Result are never serialized.
from .dock_workspace_state import (preset_entries, store_preset, remove_preset,
                                   apply_transaction)
from .preferences import get_preferences
_PRESET_CHOICES=[]


def _dock_preset_names(self,context):
    global _PRESET_CHOICES
    context=context or bpy.context
    prefs=get_preferences(context)
    _s,_p,d=_dock_context(context)
    mode=getattr(d,'generator_type','WALL')
    keys=sorted(preset_entries(getattr(prefs,'dock_presets_json','{}'),mode))
    _PRESET_CHOICES[:]=[(n,n,'Сохранённые параметры') for n in keys]
    if not _PRESET_CHOICES:_PRESET_CHOICES[:]=[('__EMPTY__','Нет пресетов','Создайте первый пресет')]
    return _PRESET_CHOICES


class UVWALL_OT_dock_presets(bpy.types.Operator):
    """Save, restore or delete a validated mode-specific generator preset."""
    bl_idname='uvwall.dock_presets'
    bl_label='Пресеты UV Wall'
    bl_description='Сохранить, применить или удалить настройки активного режима'
    bl_options={'REGISTER','UNDO'}
    action:bpy.props.EnumProperty(name='Действие',items=(('SAVE','Сохранить',''),
                               ('APPLY','Применить',''),('DELETE','Удалить','')),default='SAVE')
    new_name:bpy.props.StringProperty(name='Имя',default='Мой пресет',maxlen=56)
    selected:bpy.props.EnumProperty(name='Пресет',items=_dock_preset_names)

    def invoke(self,context,event):
        _s,_p,d=_dock_context(context)
        if d is None:
            self.report({'WARNING'},'Сначала выберите Path')
            return {'CANCELLED'}
        prefs=get_preferences(context)
        names=sorted(preset_entries(getattr(prefs,'dock_presets_json','{}'),d.generator_type))
        if names:self.selected=names[0]
        return context.window_manager.invoke_props_dialog(self,width=420)

    def draw(self,context):
        layout=self.layout
        _s,_p,d=_dock_context(context)
        if d is not None:layout.label(text='Режим · '+str(d.generator_type),icon='PRESET')
        layout.prop(self,'action',expand=True)
        if self.action=='SAVE':layout.prop(self,'new_name')
        else:layout.prop(self,'selected')
        layout.label(text='Не сохраняются Path, Result, объекты и ссылки на материалы',icon='INFO')

    def execute(self,context):
        prefs=get_preferences(context)
        s,path,d=_dock_context(context)
        if prefs is None or d is None:return {'CANCELLED'}
        mode=d.generator_type
        raw=prefs.dock_presets_json
        try:
            if self.action=='SAVE':
                prefs.dock_presets_json=store_preset(raw,mode,self.new_name,d)
                self.report({'INFO'},'Пресет сохранён · '+self.new_name)
            elif self.action=='DELETE':
                prefs.dock_presets_json=remove_preset(raw,mode,self.selected)
                self.report({'INFO'},'Пресет удалён · '+self.selected)
            elif self.action=='APPLY':
                data=preset_entries(raw,mode)
                if self.selected not in data:raise ValueError('Пресет отсутствует')
                values=data[self.selected]
                previous={k:getattr(d,k) for k in values if hasattr(d,k)}
                previous_revision=d.revision
                try:
                    count=apply_transaction(d,values,mode)
                    _dock_refresh(context,path,s)
                except Exception:
                    for k,v in previous.items():
                        try:setattr(d,k,v)
                        except Exception:pass
                    d.revision=previous_revision
                    raise
                self.report({'INFO'},'Применён пресет · '+str(count)+' параметров')
            else:return {'CANCELLED'}
        except (ValueError,TypeError,RuntimeError,AttributeError) as exc:
            self.report({'WARNING'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}
