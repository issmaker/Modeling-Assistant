"""Persistent, Blender-native interface preferences for both UV Wall views."""
import bpy

ADDON_KEY = "issmaker_modeling_assistant"  # Integrated: preferences belong to the single root addon
UI_MODES = (
    ("FLOATING_PRO", "Floating Pro", "Контекстные плавающие окна поверх Viewport"),
    ("STUDIO_DOCK", "Studio Dock", "Отдельный закреплённый инспектор внутри Viewport"),
)


def get_preferences(context=None):
    context = context or bpy.context
    prefs = getattr(context, "preferences", None)
    addons = getattr(prefs, "addons", None)
    addon = addons.get(ADDON_KEY) if addons is not None else None
    return getattr(addon, "preferences", None)


def active_ui_mode(context=None):
    preferences = get_preferences(context)
    mode = getattr(preferences, "ui_mode", "FLOATING_PRO")
    return mode if mode in {row[0] for row in UI_MODES} else "FLOATING_PRO"


def _redraw_viewports(_self, context):
    # RNA preference callbacks must not create/close modal operators or edit a
    # mesh. Existing sessions read the mode on the next draw/event tick.
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

    ui_mode: bpy.props.EnumProperty(
        name="Режим интерфейса", items=UI_MODES,
        default="FLOATING_PRO", update=_redraw_viewports,
    )

    ui_theme: bpy.props.EnumProperty(
        name="Визуальный стиль",
        description="Профессиональная тема обоих интерфейсов UV Wall",
        items=(
            ("OBSIDIAN", "Obsidian Pro", "Нейтральный графит и бирюзовый акцент"),
            ("BLUEPRINT", "Blueprint", "Холодный инженерный синий"),
            ("TITANIUM", "Titanium", "Тёплый промышленный металл"),
            ("EMBER", "Carbon Ember", "Угольный фон с медным акцентом"),
        ),
        default="OBSIDIAN", update=_redraw_viewports,
    )

    dock_sizing: bpy.props.EnumProperty(
        name="Размер Studio Dock", items=(('AUTO','Авто','Высота по содержимому'),
                                           ('MANUAL','Ручной','Заданный размер')),
        default='AUTO',update=_redraw_viewports)
    dock_manual_width: bpy.props.IntProperty(name='Ширина Dock',default=350,min=280,max=520,update=_redraw_viewports)
    dock_manual_height: bpy.props.IntProperty(name='Высота Dock',default=620,min=344,max=1100,update=_redraw_viewports)
    dock_animation: bpy.props.EnumProperty(name='Анимация Dock',items=(('OFF','Без анимации',''),('ON','Плавная','')),default='ON',update=_redraw_viewports)
    dock_favorites_json: bpy.props.StringProperty(name='Избранное',default='{}',options={'HIDDEN'})
    dock_sections_json: bpy.props.StringProperty(name='Группы',default='{}',options={'HIDDEN'})
    dock_presets_json: bpy.props.StringProperty(name='Пресеты',default='{}',options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.label(text="UV Wall Professional · 2.0.0", icon="PREFERENCES")
        layout.prop(self, "ui_mode", expand=True)
        layout.label(text="Тема, цвет и фон: ISSMAKER → Внешний вид", icon="INFO")
        row=layout.row(align=True)
        row.prop(self,'dock_sizing',expand=True)
        if self.dock_sizing=='MANUAL':
            row=layout.row(align=True)
            row.prop(self,'dock_manual_width');row.prop(self,'dock_manual_height')
        # Motion quality is controlled globally by ISSMAKER.
        box = layout.box()
        if self.ui_mode == "STUDIO_DOCK":
            box.label(text="Studio Dock: компактный графитовый инспектор во Viewport", icon="MENU_PANEL")
            box.label(text="N-панель теперь только запускает инструмент и диагностику.")
        else:
            box.label(text="Floating Pro: контекстные окна во Viewport", icon="WINDOW")
            box.label(text="Позиции окон и черновики сохраняются в активной сессии.")
        box.label(text="Смена UI не перестраивает геометрию и не создаёт Undo.", icon="INFO")

