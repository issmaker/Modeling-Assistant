"""Floating Pro viewport operators; no alternate WALL interface.

Shared palette input remains available for the WALL workspace; the inert
legacy inner-height entry point is preserved for old Blender operator calls.
"""
import bpy

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


