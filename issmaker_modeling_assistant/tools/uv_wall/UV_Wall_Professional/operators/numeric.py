"""Discoverable numeric popup. Actual production commit remains Build's UNDO operator."""
import bpy
from .build_context import object_mode_error, execute_build
from ..ui.numeric_input import parse_value
from ..core.undo_transactions import commit_definition_patch
from ..core.definitions import definition_for, snapshot_definition, restore_definition, sync_scene_from_definition, set_total_wall_height

_PARAMETERS=(
 ('HEIGHT','Высота стены','Точная высота стены'),
 ('THICKNESS','Толщина','Толщина внутренней стены'),
 ('WALL_ROT','Поворот стены','Поворот сетки модулей стены'),
 ('CAP_ROT','Поворот крышки','Поворот сетки верхней крышки'),
 ('SURFACE_ROT','Поворот поверхности','Поворот модулей поверхности'),
 ('SURFACE_Z','Поверхность Z','Вертикальный сдвиг поверхности'),
 ('PLANT_Z','Отступ грунта по Z','Отступ внутренней поверхности относительно верха'),
 ('PLANT_OFFSET','Отступ клумбы','Дополнительное смещение внутренней поверхности'),
 ('PROFILE_ROT','Поворот профиля','Поворот профиля карниза'),
)

class UVWALL_OT_numeric_parameter(bpy.types.Operator):
    bl_idname='uvwall.numeric_parameter'
    bl_label='Точное значение'
    bl_description='Ввести точное число'
    bl_options={'REGISTER'}
    parameter: bpy.props.EnumProperty(items=_PARAMETERS,options={'HIDDEN'})
    expression: bpy.props.StringProperty(name='Размер / угол', default='4')

    def invoke(self,context,event):
        settings=getattr(context.scene,'uv_wall_settings',None)
        d=definition_for(settings.path_mesh) if settings and settings.path_mesh else None
        if d is not None:
            prop=self._target_prop()
            if self.parameter=='HEIGHT':
                from ..core.definitions import total_wall_height
                val=total_wall_height(d)
            else:
                val=float(getattr(d,prop,0))
            from ..ui.numeric_input import format_value
            self.expression=format_value(val)
        return context.window_manager.invoke_props_dialog(self,width=340)

    def draw(self,context):
        layout=self.layout
        title=next((label for key,label,_ in _PARAMETERS if key==self.parameter),'Параметр')
        layout.label(text=title,icon='DRIVER_DISTANCE' if not self._is_angle() else 'ORIENTATION_GIMBAL')
        layout.prop(self,'expression',text='Значение')

    def _is_angle(self):
        return self.parameter.endswith('_ROT')

    def _target_prop(self):
        return {'THICKNESS':'inset_distance','WALL_ROT':'wall_module_rotation',
                'CAP_ROT':'cap_rotation','SURFACE_ROT':'surface_module_rotation',
                'SURFACE_Z':'surface_height_offset','PLANT_Z':'inner_surface_height_offset',
                'PLANT_OFFSET':'inner_surface_offset','PROFILE_ROT':'cornice_profile_rotation'}.get(self.parameter)

    def execute(self,context):
        settings=context.scene.uv_wall_settings
        path=settings.path_mesh
        d=definition_for(path) if path is not None else None
        if d is None:
            self.report({'ERROR'},'Сначала выберите параметрический Path')
            return {'CANCELLED'}
        from ..core.definitions import total_wall_height
        prop=self._target_prop()
        current=total_wall_height(d) if self.parameter=='HEIGHT' else float(getattr(d,prop))
        try:
            value=parse_value(self.expression,current=current,angle=self._is_angle())
            if self.parameter=='HEIGHT':
                if value<=0:raise ValueError('Высота должна быть положительной')
            if self.parameter=='THICKNESS' and value<=0:
                raise ValueError('Толщина должна быть больше нуля')
            if self._is_angle() and not -180 <=value<=180:
                raise ValueError('Допустимый поворот: -180…180')
        except ValueError as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        mode_error = object_mode_error(context)
        if mode_error:
            self.report({'ERROR'}, mode_error)
            return {'CANCELLED'}
        try:
            changes = {} if self.parameter=='HEIGHT' else {prop: value}
            commit_definition_patch(context,d,changes,
                                    height=value if self.parameter=='HEIGHT' else None)
        except Exception as exc:
            self.report({'ERROR'},f'Не удалось применить параметр: {exc}')
            return {'CANCELLED'}
        return {'FINISHED'}

CLASSES=(UVWALL_OT_numeric_parameter,)
