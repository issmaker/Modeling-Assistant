"""Blender-native preferences for the single UV Wall Floating Pro workspace.

Old Blender configs may carry obsolete legacy RNA IDProperties; these
are intentionally ignored. No dynamic mode switch or dock layout is exposed.
"""
import bpy

ADDON_KEY = "issmaker_modeling_assistant"

def get_preferences(context=None):
    context = context or bpy.context
    preferences = getattr(context, "preferences", None)
    addons = getattr(preferences, "addons", None)
    addon = addons.get(ADDON_KEY) if addons is not None else None
    return getattr(addon, "preferences", None)

def active_ui_mode(context=None):
    """Public compatibility API for diagnostics, always Floating Pro."""
    return "FLOATING_PRO"

def _redraw_viewports(_self, context):
    wm = getattr(context, "window_manager", None)
    for window in tuple(getattr(wm, "windows", ())):
        try:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        except (AttributeError, ReferenceError, RuntimeError):
            continue

class UVWALL_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_KEY

    ui_theme: bpy.props.EnumProperty(
        name="Визуальный стиль",
        description="Профессиональная тема Floating Pro",
        items=(
            ("OBSIDIAN", "Obsidian Pro", "Графит и бирюзовый акцент"),
            ("BLUEPRINT", "Blueprint", "Инженерный синий"),
            ("TITANIUM", "Titanium", "Промышленный металл"),
            ("EMBER", "Carbon Ember", "Угольный фон с медным акцентом"),
        ),
        default="OBSIDIAN", update=_redraw_viewports,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="UV Wall · Floating Pro", icon="WINDOW")
        layout.label(text="Цвета и фон: ISSMAKER → Внешний вид", icon="INFO")
        layout.label(text="Настройки выполняются во Viewport.", icon="INFO")
