import math
import time
import traceback

import bpy
from bpy.types import AddonPreferences, Gizmo, GizmoGroup, Menu, Operator, Panel
from mathutils import Matrix

from . import modeling_tools, runtime, view_tools, ui_layout, uv_feedback, viewport_feedback, weld_preview, module_visualization, log
from . import nav_hud_geometry, nav_hud_draw, tool_hud_draw, radial_geometry, radial_draw, tooltip_text, appearance_studio
from .core import registry, action_trace, active_tools, ui_layout_scaling, window_visibility, theme_engine, appearance_store, pattern_engine
from .tools.brick_hole import integration as brick_integration
from .tools.collision_inspector import integration as collision_integration
from .tools.uv_wall import integration as uv_wall_integration
from .tools.dwos_windows import integration as dwos_integration


COLOR_IDLE = (0.16, 0.18, 0.22)
COLOR_ORANGE = (0.95, 0.38, 0.04)
COLOR_RED = (0.88, 0.06, 0.12)
COLOR_PURPLE = (0.48, 0.12, 0.82)
COLOR_TEAL = (0.02, 0.58, 0.58)
COLOR_GREEN = (0.04, 0.68, 0.26)
_LAST_STRETCH_THRESHOLD = math.radians(20.0)
_HUD_DRAW_ERROR = None
_HUD_HIT_PROXY = {}  # last time Blender prepared each region
_TOOL_DRAW_HANDLER = None
_UV_TOOL_DRAW_HANDLER = None
_SHADER_TOOL_DRAW_HANDLER = None
_TOOL_HOVER = {}
_NAV_HOVER = {}
_ACTIVE_RADIAL = []
_NOTICE_ESC_WATCHERS = []

# Blender owns the main app theme; only restrained gizmo accents are customized.
def _tag_ui_redraw(_self, context):
    """Refresh every editor because settings affect GizmoGroup visibility."""
    wm = getattr(context, 'window_manager', None)
    if wm is None:
        return
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR'}:
                    area.tag_redraw()
    except (ReferenceError, RuntimeError, AttributeError):
        pass


# UI Pass 11A — a single persistent appearance profile for every HUD.
# No circular imports: core modules are pure and never import DWOS/UV Wall.
_APPEARANCE_READY = False
_APPEARANCE_LOADING = False
_APPEARANCE_DIRTY = False
_APPEARANCE_TIMER_REGISTERED = False
_APPEARANCE_ERROR = ''


def _appearance_directory():
    return bpy.utils.user_resource('CONFIG', path='issmaker', create=True)


def _appearance_write():
    global _APPEARANCE_DIRTY, _APPEARANCE_ERROR
    prefs = _preferences(bpy.context)
    if prefs is None:
        return False
    try:
        appearance_store.write(_appearance_directory(), appearance_store.serialize_preferences(prefs))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _APPEARANCE_ERROR = str(exc)
        return False
    _APPEARANCE_ERROR = ''
    _APPEARANCE_DIRTY = False
    return True


def _appearance_timer():
    global _APPEARANCE_TIMER_REGISTERED
    _APPEARANCE_TIMER_REGISTERED = False
    if _APPEARANCE_READY and _APPEARANCE_DIRTY:
        _appearance_write()
    return None


def _appearance_changed(self, context):
    global _APPEARANCE_DIRTY, _APPEARANCE_TIMER_REGISTERED
    _tag_ui_redraw(self, context)
    if not _APPEARANCE_READY or _APPEARANCE_LOADING:
        return
    _APPEARANCE_DIRTY = True
    try:
        if not bpy.app.timers.is_registered(_appearance_timer):
            bpy.app.timers.register(_appearance_timer, first_interval=.35)
        _APPEARANCE_TIMER_REGISTERED = True
    except (RuntimeError, ValueError, AttributeError):
        # Do not fail Blender actions if a timer cannot be queued.
        _appearance_write()


def initialize_appearance():
    """Load global settings after AddonPreferences registration, once per enable."""
    global _APPEARANCE_LOADING, _APPEARANCE_READY, _APPEARANCE_ERROR
    prefs = _preferences(bpy.context)
    if prefs is None:
        return False
    _APPEARANCE_LOADING = True
    try:
        try:
            saved, source = appearance_store.read(_appearance_directory())
        except (ValueError, OSError, TypeError) as exc:
            _APPEARANCE_ERROR = 'Не удалось прочитать профиль: ' + str(exc)
            # Do NOT overwrite invalid profiles automatically.
            _APPEARANCE_READY = True
            return False
        if saved is not None:
            appearance_store.restore_into_preferences(prefs, saved)
        else:
            for key,value in appearance_store.migrate_legacy(prefs).items():
                setattr(prefs,key,value)
        _APPEARANCE_READY = True
        if saved is None:
            _appearance_write()
        _tag_ui_redraw(prefs,bpy.context)
        return True
    finally:
        _APPEARANCE_LOADING = False


def _appearance_motion_tick():
    """Budgeted decorative refresh: no geometry writes, no invisible work."""
    if bpy.app.background:
        return None
    prefs=_preferences(bpy.context)
    if prefs is None or prefs.ui_motion_quality=='OFF' or prefs.ui_reduced_motion or prefs.ui_pattern=='NONE':
        return .65
    if appearance_studio.has_active():
        # Appearance Studio already owns its bounded redraw timer.
        return .25
    visible=window_visibility.snapshot_all(active_within=3.)
    scopes={scope for scope,entries in visible.items() if any(row['rect'] is not None and not row['hidden'] for row in entries)}
    if not scopes:
        return .45
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type!='VIEW_3D':continue
                region=next((r for r in area.regions if r.type=='WINDOW'),None)
                if region and window_visibility.viewport_key(window.as_pointer(),area.as_pointer(),region.as_pointer()) in scopes:
                    area.tag_redraw()
    except (AttributeError,RuntimeError,ReferenceError):
        pass
    return .055 if prefs.ui_motion_quality=='RICH' else .10


def flush_appearance():
    """Called on explicit add-on disable, including during Blender shutdown."""
    global _APPEARANCE_READY, _APPEARANCE_TIMER_REGISTERED
    try:
        if bpy.app.timers.is_registered(_appearance_timer):
            bpy.app.timers.unregister(_appearance_timer)
    except (RuntimeError, ValueError, AttributeError):
        pass
    if _APPEARANCE_READY and _APPEARANCE_DIRTY:
        _appearance_write()
    _APPEARANCE_READY = False
    _APPEARANCE_TIMER_REGISTERED = False



class IMA_AddonPreferences(AddonPreferences):
    bl_idname = __package__ or 'issmaker_modeling_assistant'

    accent: bpy.props.EnumProperty(name='Акцент', items=(
        ('MINT', 'Мятный', 'Сдержанный архитектурный стиль'),
        ('BLUE', 'Синий', 'Классический профессиональный стиль'),
        ('AMBER', 'Янтарный', 'Тёплый технический акцент'),
        ('PURPLE', 'Фиолетовый', 'Акцент для творческой работы'),
        ('CYAN', 'Бирюзовый', 'Холодная техническая палитра'),
        ('CORAL', 'Коралловый', 'Контрастный тёплый акцент'),
        ('LIME', 'Лаймовый', 'Нейтральный зелёный'),
        ('ROSE', 'Розовый', 'Мягкий контрастный акцент'),
        ('SILVER', 'Стальной', 'Монохромный технический акцент'),
        ('GOLD', 'Золотой', 'Тёплый приглушённый акцент'),
    ), default='MINT', update=_appearance_changed)
    density: bpy.props.EnumProperty(name='Плотность', items=(
        ('COMFORT', 'Удобная', 'Больше воздуха между кнопками'),
        ('COMPACT', 'Компактная', 'Меньше расстояние между кнопками'),
    ), default='COMFORT', update=_appearance_changed)
    button_scale: bpy.props.FloatProperty(
        name='Размер инструментов', default=1.0, min=0.8, max=1.3,
        description='Масштаб кнопок Viewport и UV Editor', update=_appearance_changed)
    hud_display_preset: bpy.props.EnumProperty(
        name='Читаемость дисплея',
        description='AUTO: адаптация по размеру окна с учётом DPI Blender; QHD/UHD: ручные минимумы. Без двойного масштабирования DPI',
        items=(('AUTO','Авто · адаптивно','Дополнительная читаемость по размеру окна без повторного умножения DPI Blender'),
               ('QHD','2K / 1440p','Минимальный масштаб HUD 118%'),
               ('UHD','4K / 2160p','Минимальный масштаб HUD 142%')),
        default='AUTO',update=_appearance_changed)
    font_scale: bpy.props.FloatProperty(
        name='Размер шрифта HUD', description='Масштаб текста плавающих инструментов; не меняет главное нижнее меню',
        default=1.0, min=0.75, max=2.0, precision=2, update=_appearance_changed)
    window_scale: bpy.props.FloatProperty(
        name='Размер окон HUD', description='Масштаб плавающих панелей; при большем шрифте размер повышается автоматически',
        default=1.0, min=0.75, max=2.0, precision=2, update=_appearance_changed)
    ui_debug_bounds: bpy.props.BoolProperty(
        name='UI Debug · границы', description='Показать границы поддерживаемых панелей для проверки компоновки',
        default=False, update=_appearance_changed)
    show_uv_issue_overlay: bpy.props.BoolProperty(
        name='Подсветка UV-проблем', default=True,
        description='Неразрушающие контуры в UV Editor: не изменяет выделение и UV',
        update=_appearance_changed)
    overlay_opacity: bpy.props.FloatProperty(
        name='Яркость подсветки', default=0.90, min=0.25, max=1.0,
        subtype='FACTOR', update=_appearance_changed)
    show_3d_issue_overlay: bpy.props.BoolProperty(
        name='3D · подсветка ошибок', default=True,
        description='Показывать проблемные UV-грани на модели без изменения выделения',
        update=_appearance_changed)
    overlay_3d_fill: bpy.props.FloatProperty(
        name='3D · насыщенность области', default=0.67, min=0.25, max=0.92,
        subtype='FACTOR', update=_appearance_changed)
    radial_center_cursor: bpy.props.BoolProperty(
        name='Центрировать курсор в радиальном меню', default=True,
        description='При открытии радиального меню перенести курсор к центру активного Viewport', update=_appearance_changed)
    radial_restore_cursor: bpy.props.BoolProperty(
        name='Возвращать курсор после закрытия (устарело)', default=False,
        description='Устаревший параметр. Курсор теперь всегда остаётся на месте после закрытия меню')
    overlay_3d_xray: bpy.props.BoolProperty(
        name='3D · видеть сквозь модель', default=False,
        description='X-Ray отображение проблемных граней через другие поверхности',
        update=_appearance_changed)


    # The only appearance selector for ALL tools, including future Material Studio.
    ui_scheme: bpy.props.EnumProperty(name='Цветовая схема', items=tuple(
        (x.upper(), x.title(), 'Единая цветовая схема HUD') for x in theme_engine.SCHEMES),
        default='OBSIDIAN', update=_appearance_changed)
    ui_surface: bpy.props.EnumProperty(name='Материал панели', items=tuple(
        (x.upper(), x.title(), 'Материал внешней оболочки HUD') for x in theme_engine.SURFACES),
        default='ARCHITECTURAL', update=_appearance_changed)
    ui_pattern: bpy.props.EnumProperty(name='Рисунок фона', items=tuple(
        (x.upper(), pattern_engine.NAMES.get(x,x.title()), 'Процедурный фон') for x in theme_engine.PATTERNS),
        default='CONTOURS', update=_appearance_changed)
    ui_render_quality: bpy.props.EnumProperty(name='Качество HUD', items=(
        ('ULTRA','Ultra','Максимальная детализация и сглаживание'),
        ('HIGH','High','Качественное сглаживание и умеренная нагрузка'),
        ('BALANCED','Balanced','Адаптивное качество'),
        ('PERFORMANCE','Performance','Минимальная стоимость декоративного слоя')),
        default='HIGH', update=_appearance_changed)
    ui_custom_accent: bpy.props.BoolProperty(name='Свой цвет акцента', default=False, update=_appearance_changed)
    ui_accent_color: bpy.props.FloatVectorProperty(name='Цвет акцента', subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(0.06,.62,.49), update=_appearance_changed)
    ui_secondary_color: bpy.props.FloatVectorProperty(name='Дополнительный цвет', subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(.26,.48,.50), update=_appearance_changed)
    ui_pattern_strength: bpy.props.FloatProperty(name='Яркость фона', default=.48, min=0., max=1., update=_appearance_changed)
    ui_pattern_scale: bpy.props.FloatProperty(name='Масштаб узора', default=1., min=.25, max=4., update=_appearance_changed)
    ui_pattern_amplitude: bpy.props.FloatProperty(name='Амплитуда', default=.5, min=0., max=2., update=_appearance_changed)
    ui_pattern_parallax: bpy.props.FloatProperty(name='Параллакс', default=.2, min=0., max=1., update=_appearance_changed)
    ui_pattern_relief: bpy.props.FloatProperty(name='Световой рельеф', default=.3, min=0., max=1., update=_appearance_changed)
    ui_pattern_speed: bpy.props.FloatProperty(name='Скорость', default=1., min=0., max=3., update=_appearance_changed)
    ui_pattern_density: bpy.props.FloatProperty(name='Плотность линий',default=1.,min=.25,max=2.5,update=_appearance_changed)
    ui_pattern_opacity: bpy.props.FloatProperty(name='Прозрачность рисунка',default=.48,min=0.,max=1.,update=_appearance_changed)
    ui_pattern_contrast: bpy.props.FloatProperty(name='Контраст узора',default=.65,min=.1,max=2.,update=_appearance_changed)
    ui_pattern_glow: bpy.props.FloatProperty(name='Свечение',default=.25,min=0.,max=1.,update=_appearance_changed)
    ui_pattern_softness: bpy.props.FloatProperty(name='Мягкость',default=.5,min=0.,max=1.,update=_appearance_changed)
    ui_wall_workspace_pos_x: bpy.props.FloatProperty(
        name='UV Wall Workspace X', default=1.,min=0.,max=1.,
        update=_appearance_changed,options={'HIDDEN'})
    ui_wall_workspace_pos_y: bpy.props.FloatProperty(
        name='UV Wall Workspace Y', default=.76,min=0.,max=1.,
        update=_appearance_changed,options={'HIDDEN'})
    ui_wall_workspace_user_moved: bpy.props.BoolProperty(
        name='UV Wall Workspace manual position', default=False,
        update=_appearance_changed,options={'HIDDEN'})
    # Pass 07A: DWOS floating Composer UI preferences; geometry is never modified.
    ui_dwos_hud_custom: bpy.props.BoolProperty(name='DWOS пользовательская позиция',default=False,
        options={'HIDDEN'},update=_appearance_changed)
    ui_dwos_hud_pinned: bpy.props.BoolProperty(name='DWOS закреплён',default=False,
        options={'HIDDEN'},update=_appearance_changed)
    ui_dwos_hud_x: bpy.props.FloatProperty(name='DWOS HUD X',default=.06,min=0.,max=1.,
        options={'HIDDEN'},update=_appearance_changed)
    ui_dwos_hud_y: bpy.props.FloatProperty(name='DWOS HUD Y',default=.55,min=0.,max=1.,
        options={'HIDDEN'},update=_appearance_changed)
    ui_studio_pos_x: bpy.props.FloatProperty(name='Позиция студии X',default=.50,min=0.,max=1.,update=_appearance_changed,options={'HIDDEN'})
    ui_studio_pos_y: bpy.props.FloatProperty(name='Позиция студии Y',default=.50,min=0.,max=1.,update=_appearance_changed,options={'HIDDEN'})
    ui_studio_tab: bpy.props.EnumProperty(name='Вкладка студии',items=(('GALLERY','Фоны',''),('DESIGN','Оформление',''),('TOOLS','Примеры','')),default='GALLERY',update=_appearance_changed,options={'HIDDEN'})
    ui_studio_collapsed: bpy.props.BoolProperty(name='Студия свёрнута',default=False,update=_appearance_changed,options={'HIDDEN'})
    ui_motion_quality: bpy.props.EnumProperty(name='Анимации', items=(
        ('OFF','Выкл.','Без декоративных анимаций'),
        ('BALANCED','Сбалансированно','Умеренная нагрузка'),
        ('RICH','Максимум','Полные анимации')), default='BALANCED',update=_appearance_changed)
    ui_reduced_motion: bpy.props.BoolProperty(name='Уменьшить движение', default=False, update=_appearance_changed)

    # UV Wall's original preference fields live in the ONE root addon.
    ui_mode: bpy.props.EnumProperty(name='UV Wall интерфейс',
        items=(('FLOATING_PRO','Floating Pro','Контекстные окна'),
               ('STUDIO_DOCK','Studio Dock','Закреплённый инспектор')), default='FLOATING_PRO',update=_appearance_changed)
    ui_theme: bpy.props.EnumProperty(name='UV Wall тема',
        items=(('OBSIDIAN','Obsidian Pro','Графит'),
               ('BLUEPRINT','Blueprint','Инженерный синий'),
               ('TITANIUM','Titanium','Металл'),
               ('EMBER','Carbon Ember','Медный акцент')), default='OBSIDIAN')
    dock_sizing: bpy.props.EnumProperty(name='Размер Studio Dock',
        items=(('AUTO','Авто','Адаптивный'),('MANUAL','Ручной','Заданный')),default='AUTO',update=_appearance_changed)
    dock_manual_width: bpy.props.IntProperty(default=350,min=280,max=520,update=_appearance_changed)
    dock_manual_height: bpy.props.IntProperty(default=620,min=344,max=1100,update=_appearance_changed)
    dock_animation: bpy.props.EnumProperty(name='Анимация Dock',
        items=(('OFF','Выключена',''),('ON','Плавная','')),default='ON')
    dock_favorites_json: bpy.props.StringProperty(default='{}',options={'HIDDEN'})
    dock_sections_json: bpy.props.StringProperty(default='{}',options={'HIDDEN'})
    dock_presets_json: bpy.props.StringProperty(default='{}',options={'HIDDEN'})

    def draw(self, context):
        _draw_visual_preferences(self.layout, self)


def _preferences(context):
    try:
        item = context.preferences.addons.get(__package__ or 'issmaker_modeling_assistant')
        return item.preferences if item else None
    except (AttributeError, TypeError, RuntimeError):
        return None


def _pref(context, key, fallback):
    prefs = _preferences(context)
    return getattr(prefs, key, fallback) if prefs else fallback


def _accent(context):
    return theme_engine.from_preferences(_preferences(context)).accent


def _skin(gizmo, context, active=False, active_color=None):
    """Unify bounds of white-vector buttons and native Blender hit targets."""
    gizmo.pixel_size = 34.0 * _pref(context, 'button_scale', 1.0)
    gizmo.use_draw_hover = True
    # active state is presentation-only; custom gizmo never pushes undo.
    gizmo.weld_style = bool(active)
    try: gizmo.color_highlight = active_color or _accent(context)
    except (RuntimeError, TypeError): pass



def _track_tool_highlight(context, buttons):
    """The host handles tool hover; no additional modal mouse observers."""
    return


# A single source of truth for physical Gizmos and passive GPU artwork.
# Divergence here made the third (Tools) button invisible in v1.4.0.
VIEW_DOCK_ICONS = ('MOD_PHYSICS', 'SHADING_SOLID', 'ISS_TOOLS')

def _dock_count(context):
    return 6 if (context.mode=='EDIT_MESH' and
                 getattr(context.active_object,'type',None)=='MESH') else 0


def _viewport_positions(context, count):
    return ui_layout.viewport_layout(
        _left_edge(context), _right_edge(context), count,
        density=_pref(context, 'density', 'COMFORT'),
        scale=_pref(context, 'button_scale', 1.0),
        editor_dock=True,
        view_dock=True,
        height=getattr(context.region, 'height', None),
        mode='FULL')


def _strip_positions(context, count):
    return ui_layout.simple_strip(
        _left_edge(context), _right_edge(context), count,
        density=_pref(context, 'density', 'COMFORT'),
        scale=_pref(context, 'button_scale', 1.0),
        height=getattr(context.region, 'height', None))


def _draw_visual_preferences(layout, prefs):
    """Appearance editing has one home: bottom action of Tools radial menu."""
    col=layout.column(align=True)
    col.label(text='ISSMAKER · Appearance Studio', icon='SHADING_SOLID')
    col.label(text='Откройте меню инструментов → СТИЛЬ (снизу)')
    col.label(text='Цвета, фоны, шрифт и окна настраиваются в Studio')
    col.label(text='Автосохранение работает без кнопки «Сохранить»')
    if _APPEARANCE_ERROR:
        col.label(text='Ошибка профиля — проверьте диагностический журнал',icon='ERROR')


class IMA_OT_WindowVisibility(Operator):
    """User-facing, read-only-to-geometry floating HUD recovery control."""
    bl_idname = 'ima.window_visibility'
    bl_label = 'ISSMAKER · управление окнами'
    bl_description = 'Скрыть или восстановить активные HUD этого Viewport без изменения геометрии'
    bl_options = {'INTERNAL'}
    scope: bpy.props.StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D')

    def invoke(self, context, event):
        self.scope = window_visibility.scope_from_context(context) or ''
        if not self.scope:
            self.report({'WARNING'}, 'Не найден WINDOW-регион Viewport')
            return {'CANCELLED'}
        return context.window_manager.invoke_popup(self, width=340)

    def execute(self, context):
        return {'FINISHED'}

    def draw(self, context):
        layout=self.layout
        layout.label(text='ПЛАВАЮЩИЕ ОКНА · ТЕКУЩИЙ VIEWPORT',icon='WINDOW')
        rows=window_visibility.snapshot(self.scope)
        if not rows:
            layout.label(text='Активных зарегистрированных окон нет',icon='INFO')
            layout.label(text='Откройте DWOS или Brick Hole')
            return
        for item in rows:
            row=layout.row(align=True)
            row.label(text=item['title'],icon='HIDE_ON' if item['hidden'] else 'HIDE_OFF')
            row.label(text=('Скрыто' if item['hidden'] else
                            'Нет места' if item['status']=='NO_ROOM' else 'Открыто'))
            op=row.operator('ima.window_visibility_toggle',
                text='Показать' if item['hidden'] else 'Скрыть',
                icon='HIDE_OFF' if item['hidden'] else 'HIDE_ON')
            op.scope=self.scope;op.window_key=item['key'];op.hidden=not item['hidden']
        row=layout.row()
        op=row.operator('ima.window_visibility_restore',text='Показать все скрытые',icon='LOOP_BACK')
        op.scope=self.scope
        layout.label(text='Нет места: увеличьте Viewport или уменьшите HUD',icon='INFO')


class IMA_OT_WindowVisibilityToggle(Operator):
    bl_idname = 'ima.window_visibility_toggle'
    bl_label = 'Показать / скрыть окно'
    bl_options = {'INTERNAL'}
    scope: bpy.props.StringProperty(options={'HIDDEN'})
    window_key: bpy.props.StringProperty(options={'HIDDEN'})
    hidden: bpy.props.BoolProperty(options={'HIDDEN'})

    def execute(self, context):
        if not window_visibility.set_hidden(self.scope,self.window_key,self.hidden):
            self.report({'WARNING'},'Окно больше не зарегистрировано')
            return {'CANCELLED'}
        action_trace.record('WINDOW_VISIBILITY', 'UI',
                            '%s hidden=%s' % (self.window_key, self.hidden))
        _tag_ui_redraw(self,context)
        return {'FINISHED'}


class IMA_OT_WindowVisibilityRestore(Operator):
    bl_idname = 'ima.window_visibility_restore'
    bl_label = 'Восстановить скрытые окна'
    bl_options = {'INTERNAL'}
    scope: bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self, context):
        keys=window_visibility.restore_all(self.scope)
        action_trace.record('WINDOW_RESTORE_ALL','UI','count=%d keys=%s' % (len(keys),','.join(keys)))
        _tag_ui_redraw(self,context)
        self.report({'INFO'},'Восстановлено окон: %d' % len(keys))
        return {'FINISHED'}


def _right_edge(context):
    right = context.region.width
    window_x = getattr(context.region, "x", 0)
    for region in context.area.regions:
        if region.type != 'UI' or getattr(region, "width", 0) <= 1:
            continue
        sidebar_left = getattr(region, "x", 0) - window_x
        if 0 < sidebar_left < right:
            right = sidebar_left
    return right


def _left_edge(context):
    """Return a safe left coordinate after Blender's resizable toolbar."""
    left = 28
    window_x = getattr(context.region, "x", 0)
    for region in context.area.regions:
        if region.type != 'TOOLS' or getattr(region, "width", 0) <= 1:
            continue
        toolbar_right = getattr(region, "x", 0) + region.width - window_x
        if toolbar_right > 0:
            left = max(left, toolbar_right + 18)
    return left


def _button(group, operator, icon, color=COLOR_IDLE, **operator_properties):
    """Real Blender Gizmo target with a shared vector art / hit-area contract."""
    gizmo = group.gizmos.new(IMA_GT_ToolButton.bl_idname)
    gizmo.tool_icon = icon
    gizmo.pixel_size = 34.0
    gizmo.use_tooltip = True
    gizmo.use_event_handle_all = False
    target = gizmo.target_set_operator(operator)
    for name, value in operator_properties.items():
        setattr(target, name, value)
    return gizmo


class IMA_OT_SwitchEditor(Operator):
    """Corner-only switch inside the current editor; no screen split or Undo."""
    bl_idname='ima.switch_editor'
    bl_label='Переключить UV / Shading'
    bl_description='Переключить текущую область между UV Editor и Shader Editor'

    @classmethod
    def description(cls, context, properties):
        return tooltip_text.editor_switch(getattr(properties, 'editor', 'UV'))
    bl_options={'REGISTER','INTERNAL'}
    editor: bpy.props.EnumProperty(items=(('UV','UV Editor','Вернуться в UV Editor'),
                                           ('SHADER','Shader Editor','Перейти в Shader Editor')))

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type in {'IMAGE_EDITOR','NODE_EDITOR'})

    def execute(self,context):
        area=context.area
        if self.editor=='UV':
            area.type='IMAGE_EDITOR'
            try:area.ui_type='UV'
            except (RuntimeError,TypeError,AttributeError):pass
        else:
            area.type='NODE_EDITOR'
            try:area.ui_type='ShaderNodeTree'
            except (RuntimeError,TypeError,AttributeError):pass
            try:area.spaces.active.shader_type='OBJECT'
            except (RuntimeError,TypeError,AttributeError):pass
        area.tag_redraw()
        return {'FINISHED'}


def _editor_corner_position(context):
    left=_left_edge(context);right=_right_edge(context)
    height=getattr(context.region,'height',0)
    scale=_pref(context,'button_scale',1.0)
    if right-left<94*scale or height<120*scale:
        return None
    return (right-32*scale,height-34*scale)

class IMA_GGT_UVToShader(GizmoGroup):
    bl_idname='IMA_GGT_uv_to_shader'
    bl_label='UV → Shader Editor'
    bl_space_type='IMAGE_EDITOR'
    bl_region_type='WINDOW'
    bl_options={'PERSISTENT','SCALE'}

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type=='IMAGE_EDITOR' and
                    getattr(context.area,'ui_type','UV')=='UV')

    def setup(self,context):
        self.button=_button(self,'ima.switch_editor','NODE_MATERIAL',editor='SHADER')

    def draw_prepare(self,context):
        pos=_editor_corner_position(context)
        self.button.hide=pos is None
        if pos is not None:
            self.button.matrix_basis=Matrix.Translation((*pos,0))
            _skin(self.button,context)
            _track_tool_highlight(context,[self.button])


class IMA_GGT_ShaderToUV(GizmoGroup):
    bl_idname='IMA_GGT_shader_to_uv'
    bl_label='Shader Editor → UV'
    bl_space_type='NODE_EDITOR'
    bl_region_type='WINDOW'
    bl_options={'PERSISTENT','SCALE'}

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type=='NODE_EDITOR' and
                    getattr(context.space_data,'tree_type','')=='ShaderNodeTree')

    def setup(self,context):
        self.button=_button(self,'ima.switch_editor','UV_DATA',editor='UV')

    def draw_prepare(self,context):
        pos=_editor_corner_position(context)
        self.button.hide=pos is None
        if pos is not None:
            self.button.matrix_basis=Matrix.Translation((*pos,0))
            _skin(self.button,context)
            _track_tool_highlight(context,[self.button])


def draw_uv_switch_header(self, context):
    """Always-visible top-left UV -> Shader switch; no VIEW_3D context needed."""
    if getattr(context.area, 'type', None) != 'IMAGE_EDITOR':
        return
    row = self.layout.row(align=True)
    row.operator('ima.switch_editor', text='', icon='NODE_MATERIAL', emboss=True).editor = 'SHADER'


def draw_shader_switch_header(self, context):
    """Top-left Shader -> UV switch; works without a selected material/object."""
    if (getattr(context.area, 'type', None) != 'NODE_EDITOR' or
            getattr(context.space_data, 'tree_type', '') != 'ShaderNodeTree'):
        return
    row = self.layout.row(align=True)
    row.operator('ima.switch_editor', text='', icon='UV_DATA', emboss=True).editor = 'UV'


class IMA_MT_ViewModes(Menu):
    bl_idname = "IMA_MT_view_modes"
    bl_label = "View Modes"

    def draw(self, context):
        pie = self.layout.menu_pie()
        modes = (
            ('usual', "Solid", 'SHADING_SOLID'),
            ('normals', "Normals", 'NORMALS_FACE'),
            ('zfight', "Z-fighting", 'SELECT_INTERSECT'),
            ('night', "Night", 'LIGHT'),
            ('ucx', "UCX", 'MOD_PHYSICS'),
            ('ucx2', "UCX 2", 'MOD_PHYSICS'),
            ('textured', "Textures", 'SHADING_TEXTURE'),
            ('materials', "Materials", 'SHADING_RENDERED'),
        )
        for mode, label, icon in modes:
            op = pie.operator("ima.view_mode", text=label, icon='NONE')
            op.mode = mode


class IMA_OT_ViewModePie(Operator):
    """Centered non-destructive modal radial menu; closed immediately on context loss."""
    bl_idname = 'ima.view_mode_pie'
    bl_label = 'Радиальное меню отображения'
    bl_description = 'Открыть радиальное меню отображения или Quick Select'

    @classmethod
    def description(cls, context, properties):
        return tooltip_text.radial(getattr(properties, 'kind', 'VIEW'))
    bl_options = {'REGISTER', 'INTERNAL'}

    kind: bpy.props.EnumProperty(
        name='Меню', items=(('VIEW','Вид','Режим отображения'),
                            ('LINKED','Быстрый выбор','Выбрать ограничение Select Linked'),
                            ('TOOLS','Инструменты','Открыть модульный инструмент')),
        default='VIEW')

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and
                    context.region and context.region.type == 'WINDOW')

    def execute(self, context):
        # Avoid leaving a modal handler when called without a mouse event.
        self.report({'INFO'}, 'Откройте радиальное меню кнопкой во Viewport')
        return {'CANCELLED'}

    def invoke(self, context, event):
        if not self.poll(context) or (self.kind=='LINKED' and context.mode!='EDIT_MESH'):
            return {'CANCELLED'}
        sc=radial_geometry.radial_scale(context.region.width, context.region.height,
                                        _pref(context,'button_scale',1.0))
        if sc is not None and self.kind=='TOOLS':
            sc=min(sc,max(.42,(context.region.height*.5-12.)/221.))
        if sc is None:
            self.report({'WARNING'}, 'Viewport слишком мал для радиального меню')
            return {'CANCELLED'}
        if _ACTIVE_RADIAL:
            self.report({'INFO'}, 'Радиальное меню уже открыто')
            return {'CANCELLED'}
        self._window=context.window
        self._area=context.area
        self._region=context.region
        self._region_ptr=context.region.as_pointer()
        self._area_ptr=context.area.as_pointer()
        self._mode=context.mode
        self._scale=sc
        self._center=(context.region.width*.5,context.region.height*.5)
        self._mouse_origin=(int(event.mouse_x),int(event.mouse_y))
        self._hover=-1
        self._opened_at=time.monotonic()
        self._hover_start=self._opened_at
        self._handler=None
        self._timer=None
        self._closed=False
        self._armed=False
        self._warp_done=False
        self._chosen=set()  # Quick Select supports multiple independent delimiters.
        self._selected=(getattr(context.scene,'ima_view_mode_state','RESTORE')
                        if self.kind=='VIEW' else set())
        try:
            self._handler=bpy.types.SpaceView3D.draw_handler_add(
                self._draw,(), 'WINDOW','POST_PIXEL')
            self._timer=context.window_manager.event_timer_add(.035,window=context.window)
            _ACTIVE_RADIAL.append(self)
            context.window_manager.modal_handler_add(self)
            if _pref(context,'radial_center_cursor',True):
                rx=int(context.region.x+self._center[0])
                ry=int(context.region.y+self._center[1])
                try:
                    context.window.cursor_warp(rx,ry)
                    self._warp_done=True
                except (RuntimeError,ReferenceError,AttributeError):
                    # Menu still works even if the host refuses a cursor warp.
                    self._warp_done=False
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self._cleanup(context)
            self._radial_failure(context, 'RADIAL_INVOKE', exc)
            return {'CANCELLED'}

    def _radial_failure(self, context, phase, exc):
        """Preserve the original traceback; never show the entire stack as a tooltip."""
        detail = traceback.format_exc()
        action_trace.record('RADIAL_ERROR', 'CORE',
                            'kind=%s phase=%s; %s: %s' %
                            (self.kind,phase,type(exc).__name__,str(exc)[:290]))
        log.write('RADIAL_ERROR kind=%s phase=%s\n%s' % (self.kind,phase,detail))
        reason = str(exc).strip().splitlines()[-1][:195] or type(exc).__name__
        try:
            _show_tool_notice(context,
                'Не удалось запустить %s: %s. Полная ошибка сохранена в логе ISSMAKER.' %
                ('инструмент' if self.kind == 'TOOLS' else 'режим',reason),
                'Ошибка меню')
        except Exception as notification_exc:
            log.write('RADIAL_NOTICE_FAILURE: %r' % notification_exc)
        try:
            self.report({'ERROR'}, 'Ошибка меню: '+reason[:145])
        except (ReferenceError,RuntimeError,AttributeError):pass

    def _valid(self,context):
        try:
            return (context.window == self._window and
                    self._area.type=='VIEW_3D' and
                    self._region.type=='WINDOW' and
                    self._region.as_pointer()==self._region_ptr and
                    self._area.as_pointer()==self._area_ptr and
                    context.mode==self._mode)
        except (ReferenceError,RuntimeError,AttributeError):
            return False

    def _draw(self):
        try:
            context=bpy.context
            if self._closed or not self._valid(context) or context.area!=self._area:
                return
            radial_draw.render(self._center,self._scale,self._opened_at,
                               self._hover,self._selected,_accent(context),
                               modes=(radial_geometry.MODES if self.kind=='VIEW' else
                                      radial_geometry.TOOL_MODES if self.kind=='TOOLS' else
                                      radial_geometry.QUICK_MODES),
                               center_title=('TOOLS' if self.kind=='TOOLS' else
                                             'VIEW' if self.kind=='VIEW' else 'QUICK'),
                               center_sub=('CANCEL' if self.kind=='TOOLS' else
                                           'RESET' if self.kind=='VIEW' else 'OFF'),
                               menu_kind=self.kind,
                               disabled=(radial_geometry.DISABLED_TOOL_IDS if self.kind=='TOOLS'
                                         else frozenset()))
            if self.kind=='TOOLS':
                appearance_studio.draw_radial_entry(self._center,self._scale,self._hover=='APPEARANCE',_accent(context))
                appearance_studio.draw_geometry_entry(self._center,self._scale,self._hover=='GEOMETRY',_accent(context))
        except Exception as exc:
            _hud_fault_once(exc)
            # Rendering exceptions are contained; modal closes on its next event.
            self._closed_due_to_draw=True

    def _cleanup(self,context):
        if getattr(self,'_closed',False):return
        self._closed=True
        if self in _ACTIVE_RADIAL:_ACTIVE_RADIAL.remove(self)
        handler=getattr(self,'_handler',None)
        self._handler=None
        if handler is not None:
            try:bpy.types.SpaceView3D.draw_handler_remove(handler,'WINDOW')
            except (RuntimeError,ReferenceError,ValueError):pass
        timer=getattr(self,'_timer',None)
        self._timer=None
        if timer is not None:
            try:context.window_manager.event_timer_remove(timer)
            except (RuntimeError,ReferenceError,ValueError,AttributeError):pass
        # PASS04: never warp the pointer BACK on close. Leave it at the user's position.
        try:self._area.tag_redraw()
        except (RuntimeError,ReferenceError,AttributeError):pass

    def modal(self,context,event):
        if getattr(self,'_closed_due_to_draw',False) or not self._valid(context):
            self._cleanup(context)
            return {'CANCELLED'}
        if event.type == 'ESC' and event.value == 'PRESS':
            # Esc is a single consistent OFF action for Quick Select, whether
            # the radial chooser is open alone or over a running modal worker.
            # The unrelated View radial must never disable Quick Select.
            self._cleanup(context)
            if self.kind == 'LINKED' and modeling_tools.linked_active():
                modeling_tools.stop_linked_runtime()
                log.write('Quick selection disabled: ESC in radial menu')
            return {'CANCELLED'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            # RMB cancels the chooser but does not silently disable the worker.
            self._cleanup(context)
            return {'CANCELLED'}
        if event.type in {'WINDOW_DEACTIVATE'}:
            self._cleanup(context)
            return {'CANCELLED'}
        if event.type=='TIMER':
            self._armed=True
            try:self._area.tag_redraw()
            except (ReferenceError,RuntimeError):pass
            return {'RUNNING_MODAL'}
        # Do not interfere with Blender while menu is closed; while open,
        # swallow only menu pointing, Escape, and mouse wheel to prevent camera drifts.
        if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
            dx=event.mouse_x-(self._region.x+self._center[0])
            dy=event.mouse_y-(self._region.y+self._center[1])
            hit=radial_geometry.polar_pick(dx,dy,self._scale, sectors=len(radial_geometry.TOOL_MODES) if self.kind=='TOOLS' else 8)
            if self.kind=='TOOLS' and radial_geometry.appearance_pick(dx,dy,self._scale):
                hit='APPEARANCE'
            elif self.kind=='TOOLS' and radial_geometry.geometry_pick(dx,dy,self._scale):
                hit='GEOMETRY'
            if hit!=self._hover:
                self._hover=hit
                self._hover_start=time.monotonic()
                self._area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS' and self._armed:
            dx=event.mouse_x-(self._region.x+self._center[0])
            dy=event.mouse_y-(self._region.y+self._center[1])
            hit=radial_geometry.polar_pick(dx,dy,self._scale, sectors=len(radial_geometry.TOOL_MODES) if self.kind=='TOOLS' else 8)
            if self.kind=='TOOLS' and radial_geometry.appearance_pick(dx,dy,self._scale):
                hit='APPEARANCE'
            elif self.kind=='TOOLS' and radial_geometry.geometry_pick(dx,dy,self._scale):
                hit='GEOMETRY'
            if self.kind == 'LINKED' and hit >= 0 and getattr(event,'shift',False):
                mode=radial_geometry.QUICK_MODES[hit][0]
                if mode!='OFF':
                    if mode in self._chosen:self._chosen.remove(mode)
                    else:self._chosen.add(mode)
                    self._selected=set(self._chosen)
                    self._area.tag_redraw()
                return {'RUNNING_MODAL'}
            self._cleanup(context)
            if hit==-1:return {'CANCELLED'}
            try:
                if self.kind == 'LINKED':
                    # modeling_tools is imported at module level. A second
                    # import here makes it local to modal(), breaking Esc.
                    choice=(radial_geometry.QUICK_MODES[hit][0] if hit >= 0 else None)
                    delimit=set(self._chosen)
                    if choice and choice!='OFF':
                        delimit.add(choice)
                    if choice=='OFF' or not delimit:
                        if modeling_tools.linked_active():
                            modeling_tools.stop_linked_runtime()
                        return {'FINISHED'}
                    if modeling_tools.linked_active():
                        modeling_tools.stop_linked_runtime()
                    image = modeling_tools.default_image_name(context.active_object) or ''
                    result = bpy.ops.ima.auto_select_linked_worker(
                        'INVOKE_DEFAULT', delimit=delimit, texture_image=image,
                        normal_angle=math.radians(35), color_tolerance=.05,
                        texture_wrap='REPEAT')
                    if 'RUNNING_MODAL' not in result:
                        self.report({'WARNING'}, 'Быстрое выделение не активировано; проверьте Face Select и UV-карту')
                        return {'CANCELLED'}
                elif self.kind == 'TOOLS':
                    if hit=='GEOMETRY':
                        with context.temp_override(window=self._window,area=self._area,region=self._region):
                            result=bpy.ops.ima.geometry_quality_popup('INVOKE_DEFAULT')
                        return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}
                    if hit=='APPEARANCE':
                        action_trace.record('APPEARANCE_STUDIO_OPEN', 'CORE','radial lower action')
                        with context.temp_override(window=self._window,area=self._area,region=self._region):
                            result=bpy.ops.ima.appearance_studio('INVOKE_DEFAULT')
                        return {'FINISHED'} if 'RUNNING_MODAL' in result or 'FINISHED' in result else {'CANCELLED'}
                    if hit == -2:
                        action_trace.record('RADIAL_CANCEL', 'CORE')
                        return {'CANCELLED'}
                    tool_id = radial_geometry.TOOL_MODES[hit][0]
                    if tool_id in radial_geometry.DISABLED_TOOL_IDS:
                        self.report({'INFO'}, 'Модуль появится в следующем обновлении')
                        _show_tool_notice(context,'Инструмент пока не подключён. Выберите доступный модуль.','Инструменты')
                        action_trace.record('TOOL_UNAVAILABLE', tool_id)
                        return {'CANCELLED'}
                    action_trace.record('TOOL_SELECTED', tool_id, 'radial')
                    # This operator has identical invoke/execute logic. EXEC_DEFAULT
                    # avoids re-entering a second modal invocation from a closing
                    # modal chooser; the native tool itself still uses INVOKE_DEFAULT.
                    with context.temp_override(window=self._window,area=self._area,region=self._region):
                        result = bpy.ops.ima.launch_tool('EXEC_DEFAULT', tool_id=tool_id)
                    return {'FINISHED'} if 'RUNNING_MODAL' in result or 'FINISHED' in result else {'CANCELLED'}
                elif hit == -2:
                    bpy.ops.ima.restore_view('EXEC_DEFAULT')
                else:
                    bpy.ops.ima.view_mode('EXEC_DEFAULT',mode=radial_geometry.MODES[hit][0])
                return {'FINISHED'}
            except Exception as exc:
                self._radial_failure(context, 'ACTION_DISPATCH', exc)
                return {'CANCELLED'}
        if event.type in {'LEFTMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE','MIDDLEMOUSE'}:
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and event.ctrl and not event.alt and event.type in {'Z','Y'}:
            # Radial chooser never owns Ctrl+Z. Native Blender and the active
            # editing modal decide which undo stack owns this shortcut.
            action_trace.record('RADIAL_UNDO_PASSTHROUGH','CORE',
                                'key='+event.type+' shift='+str(event.shift))
            self._cleanup(context)
            return {'PASS_THROUGH'}
        # Leave the keyboard to Blender, and close instead of leaving
        # an invisible modal menu behind during modeling shortcuts.
        if event.value=='PRESS':
            self._cleanup(context)
        return {'PASS_THROUGH'}


class IMA_OT_ToolRadial(Operator):
    """Dedicated Tools button; reuse the tested radial modal implementation."""
    bl_idname = 'ima.tool_radial'
    bl_label = 'Меню инструментов ISSMAKER'
    bl_description = 'Радиальное меню подключаемых инструментов'
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D')

    def invoke(self, context, event):
        window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if window_region is None:
            self.report({'WARNING'}, 'Не найден WINDOW-регион 3D Viewport')
            return {'CANCELLED'}
        action_trace.record('RADIAL_OPEN', 'CORE', 'tools')
        try:
            with context.temp_override(window=context.window, area=context.area,
                                       region=window_region):
                result = bpy.ops.ima.view_mode_pie('INVOKE_DEFAULT', kind='TOOLS')
        except Exception as exc:
            log.write_exception('Tools radial invoke failed', exc)
            action_trace.record('RADIAL_OPEN_FAILED', 'CORE',
                                '%s: %s' % (type(exc).__name__, str(exc)[:260]))
            _show_tool_notice(context, 'Не удалось открыть меню инструментов. '
                              'Подробности записаны в журнал ISSMAKER.',
                              'Меню инструментов')
            self.report({'ERROR'}, ('Меню инструментов: '+str(exc).splitlines()[-1])[:175])
            return {'CANCELLED'}
        return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}

    def execute(self, context):
        self.report({'INFO'}, 'Откройте меню кнопкой во Viewport')
        return {'CANCELLED'}


def _set_persistent_notice(context, message, tool_id='CORE', kind='WARNING'):
    scene = getattr(context, 'scene', None)
    if scene is None or not hasattr(scene, 'ima_notice_message'):
        return
    scene.ima_notice_message = str(message)[:400]
    scene.ima_notice_tool = str(tool_id)[:64]
    scene.ima_notice_kind = str(kind) if kind in ('WARNING','INFO','ERROR') else 'WARNING'
    _tag_ui_redraw(None, context)
    if message and not bpy.app.background and not _NOTICE_ESC_WATCHERS:
        wm=getattr(context,'window_manager',None)
        if wm is not None:
            try:
                bpy.ops.ima.notice_esc_watcher('INVOKE_DEFAULT')
            except (RuntimeError,AttributeError,ReferenceError) as exc:
                log.write('NOTICE_ESC_WATCHER_START_FAILED: %s' % exc)


def _clear_persistent_notice(context):
    _set_persistent_notice(context, '', kind='INFO')


def _show_tool_notice(context, message, title='Что требуется'):
    """A real popup, not a fleeting Blender status bar report."""
    message = str(message)
    _set_persistent_notice(context, message, title, 'WARNING')
    action_trace.record('CONTEXT_NOTICE', 'CORE', message)
    # Persistent Viewport + sidebar notice. No duplicate Blender popup:
    # a popup consumed Esc while the persistent copy remained open.
    # The passive watcher forwards Esc to the active tool, if any.



class IMA_OT_NoticeEscWatcher(Operator):
    """Dismiss a notice via Esc, never hijack Esc from a running tool.

    Unlike a global keymap this temporary modal exists only while a notice
    is visible; it always passes events through to native Blender/modals.
    """
    bl_idname='ima.notice_esc_watcher'
    bl_label='Закрытие сообщения через Esc'
    bl_options={'INTERNAL'}

    def invoke(self, context, event):
        if not getattr(getattr(context,'scene',None),'ima_notice_message',''):
            return {'CANCELLED'}
        if _NOTICE_ESC_WATCHERS:
            return {'CANCELLED'}
        self._done=False
        _NOTICE_ESC_WATCHERS.append(self)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if self._done or not getattr(getattr(context,'scene',None),'ima_notice_message',''):
            if self in _NOTICE_ESC_WATCHERS:
                _NOTICE_ESC_WATCHERS.remove(self)
            return {'FINISHED'}
        if event.type=='ESC' and event.value=='PRESS':
            _clear_persistent_notice(context)
            self._done=True
            action_trace.record('NOTICE_DISMISS_ESC','CORE','pass-through to active tool')
        return {'PASS_THROUGH'}


class IMA_OT_ClearNotice(Operator):
    bl_idname = 'ima.clear_notice'
    bl_label = 'Закрыть сообщение ISSMAKER'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        action_trace.record('NOTICE_DISMISS','CORE')
        _clear_persistent_notice(context)
        return {'FINISHED'}


class IMA_OT_LaunchTool(Operator):
    """Safe bounded handoff into the original module's actual viewport operator."""
    bl_idname = 'ima.launch_tool'
    bl_label = 'Запустить инструмент ISSMAKER'
    bl_options = {'INTERNAL'}
    tool_id: bpy.props.StringProperty(default='BRICK_HOLE')

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D')

    def execute(self, context):
        return self._safe_launch(context)

    def invoke(self, context, event):
        return self._safe_launch(context)

    def _safe_launch(self, context):
        """Preserve full Python exceptions, even in adapter preflight or handoff."""
        try:
            return self._launch(context)
        except Exception as exc:
            log.write_exception('ISSMAKER launch_tool: '+str(self.tool_id), exc)
            action_trace.record('LAUNCH_UNHANDLED_EXCEPTION',self.tool_id,
                                '%s: %s'%(type(exc).__name__,str(exc)[:250]))
            message='Ошибка запуска %s: %s. Подробности в журнале.' % (
                self.tool_id, (str(exc).splitlines()[-1] if str(exc) else type(exc).__name__)[:200])
            try:_show_tool_notice(context,message,'Ошибка запуска')
            except Exception:pass
            self.report({'ERROR'},message[:180])
            return {'CANCELLED'}

    def _exclusive_handoff(self, context, tool_id):
        ok, reason = active_tools.switch_to(tool_id, context)
        if not ok:
            self.report({'WARNING'}, reason[:175])
            _show_tool_notice(context, reason, 'Переключение')
        return ok

    def _launch_dwos(self, context, tool):
        ready, reason, action = dwos_integration.can_start(context)
        action_trace.record('CONTEXT_CHECK', 'DWOS', reason)
        if not ready:
            self.report({'WARNING'},reason)
            _show_tool_notice(context, reason, 'Окна')
            return {'CANCELLED'}
        region = next((r for r in context.area.regions if r.type=='WINDOW'),None)
        if region is None:
            _show_tool_notice(context,'Окна: не найден WINDOW-регион 3D Viewport','Окна')
            return {'CANCELLED'}
        try:
            if action == 'AUTO':
                action_trace.record('AUTO_BUILD_REQUEST','DWOS','dwos.auto_windows')
                with context.temp_override(window=context.window, area=context.area, region=region):
                    outcome=bpy.ops.dwos.auto_windows('INVOKE_DEFAULT')
                if 'FINISHED' in outcome:
                    _clear_persistent_notice(context)
                    action_trace.record('AUTO_BUILD_FINISHED','DWOS')
                    return {'FINISHED'}
                _show_tool_notice(context,'DWOS: автоматическое построение отклонено; проверьте причину в статусе Blender.','Окна')
                return {'CANCELLED'}
            if action == 'COMPOSER':
                action_trace.record('PREVIEW_REQUEST','DWOS','dwos.viewport_composer')
                with context.temp_override(window=context.window, area=context.area, region=region):
                    result=bpy.ops.dwos.viewport_composer('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' in result:
                    _clear_persistent_notice(context)
                    action_trace.record('PREVIEW_START','DWOS')
                    return {'FINISHED'}
                _show_tool_notice(context,'DWOS Composer не открылся. Проверьте окно и журнал DWOS.','Окна')
                return {'CANCELLED'}
            action_trace.record('DETECTION_REQUEST','DWOS','dwos.find_openings')
            with context.temp_override(window=context.window, area=context.area, region=region):
                result=bpy.ops.dwos.find_openings('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                _show_tool_notice(context,'Не удалось найти проёмы. Проверьте топологию стены и журнал DWOS.','Окна')
                return {'CANCELLED'}
            state=getattr(context.scene,'dwos',None)
            count=len(state.openings) if state is not None else 0
            context.scene.ima_suite_tab='DWOS'
            context.scene.ima_dwos_tab='MAIN'
            if count:
                notice=f'Найдено проёмов: {count}. Нажмите «СОЗДАТЬ ОКНА» во Viewport.'
                _set_persistent_notice(context, notice,'DWOS','INFO')
                # Esc cancels the floating DWOS create workflow. This modal
                # immediately stops when candidates disappear or area changes.
                try:
                    with context.temp_override(window=context.window, area=context.area, region=region):
                        bpy.ops.ima.dwos_create_session('INVOKE_DEFAULT')
                except (RuntimeError, ValueError, AttributeError) as exc:
                    log.write_exception('DWOS create Esc watcher',exc)
            else:
                notice='Проёмов не найдено. Проверьте Mesh-стену или выберите готовое окно DWOS.'
                _show_tool_notice(context, notice,'Окна')
            self.report({'INFO'},notice)
            action_trace.record('DETECTION_FINISHED','DWOS',notice)
            return {'FINISHED'}
        except (RuntimeError,ReferenceError,ValueError,TypeError,AttributeError) as exc:
            action_trace.record('DWOS_LAUNCH_ERROR','DWOS',str(exc))
            log.write_exception('DWOS launch failed',exc)
            _show_tool_notice(context,'Ошибка DWOS: '+str(exc)[:160],'Окна')
            return {'CANCELLED'}

    def _launch(self, context):
        # Pass 06.5: user selecting a tool means leaving the previous tool,
        # regardless of whether the target context can pass its preflight.
        # Close first; never resurrect the earlier session after a target failure.
        if not self._exclusive_handoff(context, self.tool_id):
            return {'CANCELLED'}
        action_trace.record('HANDOFF_PREVIOUS_CLOSED', self.tool_id,
                            'target preflight follows; no auto-restore')
        tool = registry.find(self.tool_id)
        if tool is None or not tool.enabled:
            self.report({'WARNING'}, 'Инструмент пока недоступен')
            _show_tool_notice(context,'Этот модуль ещё не подключён к ISSMAKER.')
            action_trace.record('TOOL_UNAVAILABLE', self.tool_id)
            return {'CANCELLED'}
        # Each independent module owns its own context contract.
        if tool.tool_id == 'DWOS':
            return self._launch_dwos(context, tool)
        if tool.tool_id == 'COLLISION':
            ready, reason = collision_integration.can_start(context)
            action_trace.record('CONTEXT_CHECK', tool.tool_id, reason)
            if not ready:
                self.report({'WARNING'}, reason)
                _show_tool_notice(context, reason)
                return {'CANCELLED'}
            window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if window_region is None:
                self.report({'WARNING'}, '3D Viewport: отсутствует WINDOW-регион')
                return {'CANCELLED'}
            try:
                action_trace.record('ANALYSIS_REQUEST', 'COLLISION', tool.entry_operator)
                # The original check runs its own engine and starts the persistent
                # Viewport inspector; never call pick_markers before a valid check.
                with context.temp_override(window=context.window, area=context.area,
                                           region=window_region):
                    result = bpy.ops.collision_inspector.check('EXEC_DEFAULT')
                if 'FINISHED' in result:
                    _clear_persistent_notice(context)
                    action_trace.record('ANALYSIS_STARTED', 'COLLISION', 'Finished, inspector managed by Collision core')
                    return {'FINISHED'}
                action_trace.record('ANALYSIS_BLOCKED', 'COLLISION')
                self.report({'WARNING'}, 'Collision Inspector: проверка не выполнена')
                _show_tool_notice(context,'Коллизии: анализ не выполнен. Проверьте Mesh и журнал.')
                return {'CANCELLED'}
            except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
                action_trace.record('ANALYSIS_ERROR', 'COLLISION', str(exc))
                log.write_exception('Collision Inspector launch failed', exc)
                self.report({'ERROR'}, 'Коллизии: %s' % str(exc)[:170])
                _show_tool_notice(context,'Коллизии: '+str(exc)[:150])
                return {'CANCELLED'}
        if tool.tool_id == 'UV_WALL':
            ready, reason = uv_wall_integration.can_start(context)
            action_trace.record('CONTEXT_CHECK', tool.tool_id, reason)
            if not ready:
                self.report({'WARNING'}, reason)
                _show_tool_notice(context, reason, 'Стены')
                return {'CANCELLED'}
            if uv_wall_integration.status(context)['active']:
                message = 'Инспектор стены уже открыт. Закройте его клавишей Esc.'
                self.report({'INFO'}, message)
                _show_tool_notice(context, message, 'Стены')
                return {'CANCELLED'}
            region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if region is None:
                self.report({'WARNING'}, 'Стена: не найдено окно Viewport')
                return {'CANCELLED'}
            try:
                action_trace.record('PREVIEW_REQUEST', 'UV_WALL', tool.entry_operator)
                with context.temp_override(window=context.window, area=context.area,
                                           region=region):
                    result = bpy.ops.uvwall.viewport_inspector('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' in result:
                    _clear_persistent_notice(context)
                    action_trace.record('PREVIEW_START', 'UV_WALL')
                    return {'FINISHED'}
                self.report({'WARNING'}, 'UV Wall: инспектор не запущен. Проверьте путь.')
                _show_tool_notice(context, 'Не удалось открыть инструмент. Проверьте Mesh-контур и журнал UV Wall.')
                return {'CANCELLED'}
            except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
                action_trace.record('PREVIEW_ERROR', 'UV_WALL', str(exc))
                log.write_exception('UV Wall launch failed', exc)
                self.report({'ERROR'}, str(exc)[:180])
                _show_tool_notice(context, 'Ошибка запуска стены: ' + str(exc)[:190])
                return {'CANCELLED'}
        if tool.tool_id != 'BRICK_HOLE':
            self.report({'WARNING'}, 'Нет адаптера запуска: ' + tool.tool_id)
            return {'CANCELLED'}
        ready, reason = brick_integration.can_start(context)
        action_trace.record('CONTEXT_CHECK', tool.tool_id, reason)
        if not ready:
            self.report({'WARNING'}, reason)
            _show_tool_notice(context, reason, 'Проёмы')
            return {'CANCELLED'}
        if brick_integration.is_preview_active():
            self.report({'WARNING'}, 'Предпросмотр проёмов уже активен')
            _show_tool_notice(context,'Проёмы: предпросмотр уже активен. Завершите или отмените текущий Preview.')
            action_trace.record('TOOL_BUSY', tool.tool_id)
            return {'CANCELLED'}
        window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if window_region is None:
            self.report({'WARNING'}, '3D Viewport: регион WINDOW не найден')
            return {'CANCELLED'}
        try:
            # N-panel buttons have region=UI. The native modal operator MUST
            # start in VIEW_3D/WINDOW with its original invoke/preflight.
            action_trace.record('PREVIEW_REQUEST', tool.tool_id, tool.entry_operator)
            with context.temp_override(window=context.window, area=context.area,
                                       region=window_region):
                result = bpy.ops.view3d.brick_hole_interactive('INVOKE_DEFAULT')
            if 'RUNNING_MODAL' in result:
                _clear_persistent_notice(context)
                action_trace.record('PREVIEW_START', tool.tool_id)
                return {'FINISHED'}
            state = str(getattr(context.scene, 'brick_hole_interactive_state', ''))
            action_trace.record('PREVIEW_BLOCKED', tool.tool_id, state)
            self.report({'WARNING'}, (state or 'Viewport Preview не запущен')[:180])
            _show_tool_notice(context,state or 'Проёмы: Preview не запущен. Проверьте режим выделения.')
            return {'CANCELLED'}
        except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
            action_trace.record('PREVIEW_ERROR', tool.tool_id, exc)
            log.write_exception('Integrated Brick Hole launch failed', exc)
            self.report({'ERROR'}, 'Проёмы: %s' % str(exc)[:175])
            _show_tool_notice(context,'Проёмы: '+str(exc)[:150])
            return {'CANCELLED'}


class IMA_OT_AuditWallSeams(Operator):
    bl_idname='ima.audit_wall_seams'
    bl_label='Проверить стыки модулей стены'
    bl_description='Только проверка открытых граничных рёбер, не меняет Mesh'
    bl_options={'REGISTER','INTERNAL'}

    def execute(self, context):
        from .core.wall_seam_audit import suspect_open_seam_pairs
        from collections import Counter
        from mathutils import Vector
        obj=context.active_object
        if obj is None or obj.type!='MESH' or context.mode!='OBJECT':
            _show_tool_notice(context,'Выделите готовый Mesh стены в Object Mode')
            return {'CANCELLED'}
        verts=[tuple(obj.matrix_world @ v.co) for v in obj.data.vertices]
        uses=Counter()
        for face in obj.data.polygons:
            ring=list(face.vertices)
            for a,b in zip(ring,ring[1:]+ring[:1]):
                uses[tuple(sorted((a,b)))]+=1
        edges=[tuple(e.vertices) for e in obj.data.edges]
        warnings=suspect_open_seam_pairs(verts,edges,uses)
        for item in warnings:
            action_trace.record('UV_WALL_SEAM_SUSPECT','UV_WALL',str(item))
        if warnings:
            self.report({'WARNING'},f'Возможных незакрытых стыков: {len(warnings)}. Соберите лог для анализа')
            _show_tool_notice(context,f'Найдены потенциальные щели: {len(warnings)}. Проверьте стыки; автоматическая сварка не выполнялась.')
        else:
            self.report({'INFO'},'Пар граничных рёбер с зазором 2.5–25 мм не найдено')
        return {'FINISHED'}


class IMA_OT_CollisionVisibility(Operator):
    bl_idname = 'ima.collision_visibility'
    bl_label = 'Показать или скрыть инспектор коллизий'
    bl_options = {'INTERNAL'}
    show: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D'
                    and hasattr(context.scene, 'collision_inspector'))

    def execute(self, context):
        from .tools.collision_inspector import viewport_ui
        settings = context.scene.collision_inspector
        if not self.show:
            settings.overlay_enabled = False
            settings.overlay_pick_mode = False
            viewport_ui.close_marker_menu()
            viewport_ui.close_footer_group_menu()
            viewport_ui.redraw()
            action_trace.record('HIDE_INSPECTOR', 'COLLISION')
            return {'FINISHED'}
        if not settings.analysis_signature:
            self.report({'WARNING'}, 'Сначала проверьте Mesh')
            return {'CANCELLED'}
        obj = context.active_object
        if obj is None or obj.name != settings.object_name or context.mode != 'OBJECT':
            self.report({'WARNING'}, 'Выберите проверенный Mesh в Object Mode')
            return {'CANCELLED'}
        settings.overlay_enabled = True
        settings.overlay_markers = True
        if not settings.overlay_pick_mode:
            region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if region is None:
                return {'CANCELLED'}
            try:
                with context.temp_override(window=context.window, area=context.area, region=region):
                    result = bpy.ops.collision_inspector.pick_markers('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' not in result:
                    self.report({'WARNING'}, 'Инспектор не удалось открыть')
                    return {'CANCELLED'}
            except (RuntimeError, ReferenceError, ValueError) as exc:
                self.report({'WARNING'}, str(exc)[:170])
                return {'CANCELLED'}
        viewport_ui.redraw()
        action_trace.record('SHOW_INSPECTOR', 'COLLISION')
        return {'FINISHED'}


class IMA_OT_UVAnalysisAction(Operator):
    bl_idname = "ima.uv_analysis_action"
    bl_label = "Найти UV-проблемы"
    bl_description = "Найти Flip UV или растяжения UV-развёртки без изменения выделения"

    @classmethod
    def description(cls, context, properties):
        # Both toolbar buttons call the same operator with different `kind`.
        # Blender evaluates description using that button's actual properties.
        kind = getattr(properties, 'kind', 'STRETCH')
        active = False
        try:
            active = bool(view_tools.issues_active() and
                          view_tools.issues_context_matches(context))
        except (AttributeError, ReferenceError, RuntimeError, ValueError):
            pass
        return tooltip_text.uv_analysis(kind, session_active=active)

    kind: bpy.props.EnumProperty(items=(
        ('STRETCH', "Потяги", "Найти UV-искажения"),
        ('FLIPPED', "Flip UV", "Найти перевёрнутые UV-полигоны"),
    ), default='STRETCH')

    threshold: bpy.props.FloatProperty(
        name="Допустимое отклонение",
        description="Максимальная разница между углом полигона в 3D и UV",
        default=math.radians(20.0),
        min=math.radians(0.1),
        max=math.radians(90.0),
        subtype='ANGLE',
        unit='ROTATION',
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(context.mode == 'EDIT_MESH' and obj and obj.type == 'MESH')

    def invoke(self, context, event):
        global _LAST_STRETCH_THRESHOLD
        if view_tools.analysis_running():
            self.report({'INFO'}, "UV-анализ выполняется; Esc отменяет")
            return {'CANCELLED'}
        if view_tools.issues_active():
            if view_tools.issues_context_matches(context):
                return bpy.ops.ima.exit_analysis()
            view_tools.discard_stale_issues(context)
        if weld_preview.active():
            weld_preview.clear('UV analysis started')
        if self.kind == 'FLIPPED':
            result = bpy.ops.ima.select_flipped('INVOKE_DEFAULT')
            return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}
        self.threshold = _LAST_STRETCH_THRESHOLD
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Поиск UV-потягов", icon='MOD_SMOOTH')
        layout.label(text="Укажите допустимую разницу углов:")
        layout.prop(self, "threshold")

    def execute(self, context):
        global _LAST_STRETCH_THRESHOLD
        if view_tools.analysis_running():
            self.report({'WARNING'}, "UV-анализ уже выполняется")
            return {'CANCELLED'}
        if view_tools.issues_active():
            if view_tools.issues_context_matches(context):
                return bpy.ops.ima.exit_analysis()
            view_tools.discard_stale_issues(context)
        if weld_preview.active():
            weld_preview.clear('UV analysis started')
        if self.kind == 'FLIPPED':
            result = bpy.ops.ima.select_flipped('INVOKE_DEFAULT')
            return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}
        _LAST_STRETCH_THRESHOLD = self.threshold
        result = bpy.ops.ima.select_stretch(
            'INVOKE_DEFAULT', threshold=self.threshold)
        return {'FINISHED'} if 'RUNNING_MODAL' in result else {'CANCELLED'}


class IMA_OT_ToggleUVFeedback(Operator):
    bl_idname = 'ima.toggle_uv_feedback'
    bl_label = 'Показать или скрыть UV-подсветку'
    bl_description = 'Показать или скрыть цветные контуры найденных проблем в UV Editor. Выделение и UV не изменяются.'
    bl_options = {'REGISTER'}

    def execute(self, context):
        prefs = _preferences(context)
        if prefs is None:
            self.report({'WARNING'}, 'Не удалось получить настройки ISSMAKER')
            return {'CANCELLED'}
        prefs.show_uv_issue_overlay = not prefs.show_uv_issue_overlay
        self.report({'INFO'}, 'Подсветка UV: %s' %
                    ('включена' if prefs.show_uv_issue_overlay else 'скрыта'))
        return {'FINISHED'}


class IMA_OT_Toggle3DFeedback(Operator):
    bl_idname = 'ima.toggle_3d_feedback'
    bl_label = 'Показать или скрыть 3D-подсветку'
    bl_description = 'Показать или скрыть заливку найденных UV-проблем на 3D-модели. Выделение не изменяется.'
    bl_options = {'REGISTER'}

    def execute(self, context):
        prefs = _preferences(context)
        if prefs is None:
            self.report({'WARNING'}, 'Не удалось получить настройки ISSMAKER')
            return {'CANCELLED'}
        prefs.show_3d_issue_overlay = not prefs.show_3d_issue_overlay
        self.report({'INFO'}, 'Подсветка 3D: %s' %
                    ('включена' if prefs.show_3d_issue_overlay else 'скрыта'))
        return {'FINISHED'}


class IMA_GT_ToolButton(Gizmo):
    """Independent Gizmo with a true Blender hit proxy and consistent vector art.

    Do not subclass a previously registered custom Gizmo RNA class: Blender
    registration may reject registered RNA subclass inheritance.
    """
    bl_idname = 'IMA_GT_tool_button'
    bl_target_properties = ()
    tool_icon: bpy.props.StringProperty(default='')
    pixel_size: bpy.props.FloatProperty(default=34., min=18., max=90.)
    weld_style: bpy.props.BoolProperty(default=False)

    def setup(self):
        self.use_event_handle_all = False
        self.use_undo = False
        self.use_tooltip = True
        self.use_draw_hover = True
        from math import cos, sin, pi
        triangles = []
        for i in range(32):
            a = 2*pi*i/32; b = 2*pi*(i+1)/32
            triangles.extend(((0.,0.,0.),(.5*cos(a),.5*sin(a),0.),
                              (.5*cos(b),.5*sin(b),0.)))
        self._hit_shape = self.new_custom_shape('TRIS', tuple(triangles))
        self.color = (0,0,0)
        self.color_highlight = (0,0,0)
        self.alpha = 0.0
        self.alpha_highlight = 0.0

    def _pick_matrix(self):
        return self.matrix_basis @ Matrix.Scale(self.pixel_size,4)

    def draw(self, context):
        if not self.hide:
            self.draw_custom_shape(self._hit_shape, matrix=self._pick_matrix())

    def draw_select(self, context, select_id):
        if not self.hide:
            self.draw_custom_shape(self._hit_shape,
                                   matrix=self._pick_matrix(), select_id=select_id)

    def test_select(self, context, location):
        if self.hide or getattr(context.region,'type',None)!='WINDOW':
            return -1
        x,y=location
        pos=self.matrix_basis.translation
        r=self.pixel_size*.5
        hit=(x-pos.x)**2+(y-pos.y)**2 <= r*r
        if hit:
            try:
                key=(context.area.as_pointer(),context.region.as_pointer(),self.tool_icon)
                _TOOL_HOVER[key]=time.monotonic()
                if not bpy.app.timers.is_registered(_ui_redraw_timer):
                    bpy.app.timers.register(_ui_redraw_timer,first_interval=.035)
                context.area.tag_redraw()
            except (ReferenceError,RuntimeError,AttributeError,ValueError): pass
        return 0 if hit else -1


from .styled_panels import IMA_OT_GeometryQualityPopup


class IMA_GGT_Viewport(GizmoGroup):
    bl_idname = "IMA_GGT_viewport"
    bl_label = "Быстрые инструменты 3D Viewport"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and
                    context.mode in {'OBJECT', 'EDIT_MESH'})

    def setup(self, context):
        # Hard-code menu kind on the View gizmo: never inherit the last
        # executed enum value (TOOLS) from Blender's operator RNA memory.
        self.buttons = [
            _button(self, 'ima.toggle_ucx', VIEW_DOCK_ICONS[0]),
            _button(self, 'ima.view_mode_pie', VIEW_DOCK_ICONS[1], kind='VIEW'),
            _button(self, 'ima.tool_radial', VIEW_DOCK_ICONS[2]),
        ]

    def draw_prepare(self, context):
        points = _viewport_positions(context, _dock_count(context))['view']
        for button in self.buttons:
            button.hide = True
        for button, xy in zip(self.buttons, points):
            button.hide = False
            button.matrix_basis = Matrix.Translation((*xy, 0))
            _skin(button, context)
        if len(points) > 0 and view_tools.ucx_active():
            _skin(self.buttons[0], context, active=True)
        if len(points) > 1 and getattr(context.scene, 'ima_view_mode_state', 'RESTORE') != 'RESTORE':
            _skin(self.buttons[1], context, active=True, active_color=COLOR_PURPLE)
        if len(points) > 2 and brick_integration.is_preview_active():
            _skin(self.buttons[2], context, active=True, active_color=COLOR_GREEN)
        _track_tool_highlight(context, self.buttons)



# PASS 04.1: DWOS candidates are actionable directly in the Viewport.
# Use one condition and one rectangle calculation for the GPU paint and Gizmo hit-test.
def _dwos_suspect(candidate):
    """Non-destructive advisory, not a hard dimensional limit for real windows."""
    try:
        width=float(str(candidate.width_text).replace(',','.'))
        height=float(str(candidate.height_text).replace(',','.'))
        payload=__import__('json').loads(getattr(candidate,'payload_json','{}') or '{}')
        kind=str(payload.get('adaptive_classification',payload.get('classification','')))
        return bool(kind == 'CURVED' and height > 0 and width > 15. and width/height > 7.)
    except (ValueError,TypeError,AttributeError):
        return False


class IMA_OT_DWOSExcludeSuspicious(Operator):
    bl_idname='ima.dwos_exclude_suspicious'
    bl_label='Исключить подозрительные проёмы'
    bl_description='Снять галочки с аномально длинных CURVED-кандидатов, оставив их в списке'
    bl_options={'REGISTER','INTERNAL'}

    def execute(self, context):
        state=getattr(getattr(context,'scene',None),'dwos',None)
        if state is None:
            self.report({'WARNING'},'DWOS не зарегистрирован')
            return {'CANCELLED'}
        changed=0
        for candidate in state.openings:
            if candidate.enabled and _dwos_suspect(candidate):
                candidate.enabled=False
                changed+=1
        action_trace.record('DWOS_SUSPECT_EXCLUDED','DWOS',f'count={changed}')
        self.report({'INFO'},f'Подозрительных кандидатов отключено: {changed}. Их можно включить вручную.')
        if context.area: context.area.tag_redraw()
        return {'FINISHED'}


def _dwos_creation_state(context):
    if (not getattr(context, 'scene', None) or
            getattr(context, 'mode', None) != 'OBJECT'):
        return False, 'Для создания окон перейдите в Object Mode.', 0
    state = getattr(context.scene, 'dwos', None)
    if state is None:
        return False, 'Модуль DWOS не зарегистрирован.', 0
    wall_name = getattr(state, 'wall_name', '')
    host = bpy.data.objects.get(wall_name) if wall_name else None
    if not host or host.type != 'MESH':
        return False, 'Исходная Mesh-стена не найдена. Снова выполните поиск проёмов.', 0
    active = getattr(context, 'active_object', None)
    if active is None or active != host:
        return False, 'Выделите стену, на которой были найдены проёмы.', 0
    if not getattr(state, 'show_overlay', False):
        return False, 'После создания повторно выполните поиск, чтобы создать новую группу окон.', 0
    count = sum(bool(c.enabled) for c in state.openings)
    if not count:
        return False, 'Нет выбранных проёмов. Сначала выполните поиск.', 0
    return True, f'Найдено {count} проёмов, можно создать окна.', count


def _dwos_create_rect(context):
    ready, _reason, count = _dwos_creation_state(context)
    if not ready or not context.area or context.area.type != 'VIEW_3D':
        return None
    region = getattr(context, 'region', None)
    if not region or region.type != 'WINDOW' or region.width < 360 or region.height < 270:
        return None
    width = min(270.0, float(region.width) - 72.0)
    height = 56.0
    x = (float(region.width) - width) * .5
    y = min(103.0, max(62.0, float(region.height) * .14))
    return x, y, width, height, count


class IMA_OT_DWOSCreateWindows(Operator):
    bl_idname = 'ima.dwos_create_windows'
    bl_label = 'Создать окна DWOS'
    bl_description = 'Создать окна в найденных проёмах выбранной стены'
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        ok, reason, count = _dwos_creation_state(context)
        if not ok:
            self.report({'WARNING'}, reason)
            _show_tool_notice(context, reason, 'Окна')
            return {'CANCELLED'}
        window_region = next((r for r in context.area.regions if r.type == 'WINDOW'), None) if context.area else None
        if context.area is None or context.area.type != 'VIEW_3D' or window_region is None:
            reason = 'Откройте 3D Viewport для создания окон DWOS.'
            self.report({'WARNING'}, reason)
            _show_tool_notice(context, reason, 'Окна')
            return {'CANCELLED'}
        action_trace.record('DWOS_CREATE_REQUEST', 'DWOS', f'candidates={count}')
        try:
            with context.temp_override(window=context.window, area=context.area, region=window_region):
                result = bpy.ops.dwos.create_windows('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                action_trace.record('DWOS_CREATE_CANCELLED', 'DWOS', f'candidates={count}')
                msg = 'DWOS не создал окна. Причина указана в журнале DWOS и строке состояния Blender.'
                _show_tool_notice(context, msg, 'Окна')
                return {'CANCELLED'}
            action_trace.record('DWOS_CREATE_FINISHED', 'DWOS', f'candidates={count}')
            context.scene.ima_suite_tab = 'DWOS'
            context.scene.ima_dwos_tab = 'MAIN'
            try:
                with context.temp_override(window=context.window, area=context.area,
                                           region=window_region):
                    composer_result=bpy.ops.dwos.viewport_composer('INVOKE_DEFAULT')
                if 'RUNNING_MODAL' in composer_result or 'FINISHED' in composer_result:
                    _clear_persistent_notice(context)
                    action_trace.record('DWOS_AUTO_COMPOSER_OPENED','DWOS',
                                        getattr(context.active_object,'name',''))
                else:
                    raise RuntimeError('Composer вернул '+str(composer_result))
            except (RuntimeError, ValueError, TypeError, AttributeError, ReferenceError) as composer_exc:
                # Geometry was already successfully committed: do NOT roll it
                # back merely because opening a UI session failed.
                log.write_exception('DWOS auto Composer unavailable after successful create',composer_exc)
                action_trace.record('DWOS_AUTO_COMPOSER_FAILED','DWOS',str(composer_exc)[:240])
                _set_persistent_notice(context,
                    'Окна созданы. Composer не открылся: выберите окно и откройте Composer вручную.',
                    'DWOS','WARNING')
                self.report({'WARNING'},'Окна созданы, но Composer не открылся')
            context.area.tag_redraw()
            return {'FINISHED'}
        except (RuntimeError, ValueError, TypeError, AttributeError, ReferenceError) as exc:
            action_trace.record('DWOS_CREATE_ERROR', 'DWOS', str(exc))
            log.write_exception('DWOS viewport creation failed', exc)
            _show_tool_notice(context, 'Не удалось создать окна: ' + str(exc)[:150], 'Окна')
            return {'CANCELLED'}


class IMA_OT_DWOSCreateSession(Operator):
    """Esc dismisses the candidate-creation overlay, not the whole Blender tool."""
    bl_idname='ima.dwos_create_session'
    bl_label='Контекст создания окон DWOS'
    bl_options={'INTERNAL'}

    def invoke(self, context, event):
        ok, _, _ = _dwos_creation_state(context)
        if not ok:
            return {'CANCELLED'}
        self._area_ptr=context.area.as_pointer() if context.area else 0
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if (not context.area or context.area.as_pointer()!=self._area_ptr
                or not _dwos_creation_state(context)[0]):
            return {'FINISHED'}
        if event.type=='ESC' and event.value=='PRESS':
            state=context.scene.dwos
            state.show_overlay=False
            _clear_persistent_notice(context)
            action_trace.record('DWOS_CREATE_ESC_CANCELLED','DWOS','candidates kept; create overlay hidden')
            context.area.tag_redraw()
            return {'FINISHED'}
        return {'PASS_THROUGH'}


class IMA_GT_DWOSCreateButton(Gizmo):
    bl_idname = 'IMA_GT_dwos_create_button'
    bl_label = 'Создать окна'
    bl_target_properties = ()

    def setup(self):
        self.use_event_handle_all = False
        self.use_draw_hover = True
        self.use_undo = False
        self.use_tooltip = True
        vertices = ((-.5,-.5,0.),(.5,-.5,0.),(.5,.5,0.),
                    (-.5,-.5,0.),(.5,.5,0.),(-.5,.5,0.))
        self._shape = self.new_custom_shape('TRIS', vertices)
        self.color = (0, 0, 0)
        self.color_highlight = (0, 0, 0)
        self.alpha = 0.0
        self.alpha_highlight = 0.0
        self._hit_w = 0.0
        self._hit_h = 0.0

    def _matrix(self):
        return self.matrix_basis @ Matrix.Diagonal((self._hit_w, self._hit_h, 1., 1.))

    def draw(self, context):
        if not self.hide:
            self.draw_custom_shape(self._shape, matrix=self._matrix())

    def draw_select(self, context, select_id):
        if not self.hide:
            self.draw_custom_shape(self._shape, matrix=self._matrix(), select_id=select_id)

    def test_select(self, context, location):
        if self.hide or _dwos_create_rect(context) is None:
            return -1
        x, y = location
        cx, cy, _ = self.matrix_basis.translation
        return 0 if abs(x-cx) <= self._hit_w * .5 and abs(y-cy) <= self._hit_h * .5 else -1


class IMA_GGT_DWOSCreate(GizmoGroup):
    bl_idname = 'IMA_GGT_dwos_create'
    bl_label = 'Создание окон DWOS во Viewport'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and context.mode == 'OBJECT')

    def setup(self, context):
        self.button = self.gizmos.new('IMA_GT_dwos_create_button')
        self.button.target_set_operator('ima.dwos_create_windows')

    def draw_prepare(self, context):
        rect = _dwos_create_rect(context)
        if rect is None:
            self.button.hide = True
            return
        x, y, w, h, _count = rect
        self.button.hide = False
        self.button._hit_w = w
        self.button._hit_h = h
        self.button.matrix_basis = Matrix.Translation((x + w * .5, y + h * .5, 0.))


class IMA_GGT_EditTools(GizmoGroup):
    bl_idname = "IMA_GGT_edit_tools"
    bl_label = "Инструменты Edit Mode"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(context.mode == 'EDIT_MESH' and obj and obj.type == 'MESH')

    def setup(self, context):
        self.buttons = [
            _button(self, "ima.merge_002", 'AUTOMERGE_ON'),
            _button(self, "ima.flip_normals", 'NORMALS_FACE'),
            _button(self, "ima.toggle_borders", 'MOD_WIREFRAME'),
            _button(self, "ima.uv_analysis_action", 'MOD_SMOOTH', kind='STRETCH'),
            _button(self, "ima.uv_analysis_action", 'UV_SYNC_SELECT', kind='FLIPPED'),
            _button(self, "ima.view_mode_pie", 'LINKED', kind="LINKED"),
        ]

    def draw_prepare(self, context):
        have_issues = view_tools.issues_active()
        chosen = [0, 1, 2, 3, 4, 5]  # FULL always: no launcher or mode switching.
        points = _viewport_positions(context, len(chosen))['edit']
        for button in self.buttons:
            button.hide = True
            _skin(button, context)
        for index, xy in zip(chosen, points):
            button = self.buttons[index]
            button.hide = False
            button.matrix_basis = Matrix.Translation((*xy, 0))
        if view_tools.borders_active(context):
            _skin(self.buttons[2], context, active=True, active_color=COLOR_ORANGE)
        if modeling_tools.linked_active():
            _skin(self.buttons[5], context, active=True)
        self.buttons[3].icon = 'MOD_SMOOTH'
        self.buttons[4].icon = 'UV_SYNC_SELECT'
        if have_issues:
            issue_color = COLOR_RED if view_tools.issue_kind() == 'STRETCH' else COLOR_ORANGE
            active_index = 3 if view_tools.issue_kind() == 'STRETCH' else 4
            self.buttons[active_index].icon = 'X'
            _skin(self.buttons[active_index], context, active=True,
                  active_color=issue_color)
        elif view_tools.analysis_running():
            for index in (3, 4):
                _skin(self.buttons[index], context, active=True,
                      active_color=COLOR_ORANGE)
        _track_tool_highlight(context, self.buttons)


def _navigator_y(context):
    """Place the navigator ABOVE the full dock; never overlap its geometry."""
    width = _right_edge(context) - _left_edge(context)
    scale = _pref(context, 'button_scale', 1.0)
    # Rounded panel width 222 px + clear space for sidebar margins.
    if width < 232.0 * scale:
        return None
    if context.area.type == 'IMAGE_EDITOR':
        return 138.0 * scale if context.region.height > 200.0 * scale else None
    center = (_left_edge(context) + _right_edge(context)) * .5
    target = 138.0 * scale
    positions = _viewport_positions(context, 6)['edit']
    # Full tool strip can wrap; reserve the entire HUD panel footprint.
    for _ in range(14):
        conflicting = [py for px, py in positions
                       if abs(px-center) < 150.0*scale and
                       abs(py-target) < 81.0*scale]
        if not conflicting:
            break
        target = max(conflicting) + 83.0*scale
    return target if target+60.0*scale < context.region.height else None


def _nav_layout(context):
    y = _navigator_y(context)
    if y is None:
        return None
    return nav_hud_geometry.layout(
        (_left_edge(context)+_right_edge(context))*.5,
        y,
        _pref(context,'button_scale',1.0))


def _hud_fault_once(exc):
    global _HUD_DRAW_ERROR
    if _HUD_DRAW_ERROR is None:
        _HUD_DRAW_ERROR = '%s: %s' % (type(exc).__name__, str(exc))[:1200]
        try:
            from . import log
            log.write_exception('Unified Navigator HUD rendering failed', exc)
        except Exception:
            pass


def radial_hud_diagnostics(context):
    return {'active':len(_ACTIVE_RADIAL),'style':'CENTERED_RADIAL_1328',
            'warp_enabled':_pref(context,'radial_center_cursor',True),
            'restore_cursor':False,
            'gpu_error':_HUD_DRAW_ERROR}


def tool_hud_diagnostics(context):
    try:
        rect=_viewport_positions(context,6) if context.area.type=='VIEW_3D' else None
        return {'style':'SPRITE_AA_GRAPHITE_1328','active_groups':
                [key for key in ('editor','view','edit') if rect and rect.get(key)],
                'gpu_error':_HUD_DRAW_ERROR,'native_blender_icons':False,
                'hover':'GPU_EASE_180MS',
                'handler_active':_TOOL_DRAW_HANDLER is not None,
                'header_switches':'LEFT_HEADER_UV_SHADING',
                'recent_hover_events':len(_TOOL_HOVER),
                'icon_pack':tool_hud_draw.icon_pack.status()}
    except (AttributeError,ReferenceError,RuntimeError,ValueError):
        return {'style':'SPRITE_AA_GRAPHITE_1328','status':'CONTEXT_UNAVAILABLE'}


def navigator_hud_diagnostics(context):
    """Snapshot only, never a draw or mesh operation."""
    data={'style':'ROUND_ISSUE_NAV_1328','gpu_error':_HUD_DRAW_ERROR,
          'status':'NO_SESSION','editor':getattr(getattr(context,'area',None),'type',None)}
    try:
        if getattr(context,'mode',None)!='EDIT_MESH':
            return data
        if (getattr(context.area,'type',None)=='VIEW_3D' and
                weld_preview.context_matches(context)):
            data['status']='WELD'
        elif view_tools.issues_active() and view_tools.issues_context_matches(context):
            data['status']=str(view_tools.issue_kind())
        else:
            return data
        if getattr(context.region,'type',None)=='WINDOW':
            layout=_nav_layout(context)
            if layout:
                data['panel']=tuple(round(v,1) for v in layout['panel'])
                data['buttons']=[{'x':round(x,1),'y':round(y,1),'size':round(size,1)}
                                 for x,y,size in layout['buttons']]
                data['status']+=':VISIBLE'
                last=_HUD_HIT_PROXY.get((context.area.as_pointer(), context.region.as_pointer()))
                data['hit_proxy']='PREPARED' if last is not None and time.monotonic()-last < 2 else 'NOT_PREPARED'
            else:
                data['status']+=':TOO_SMALL'
        else:
            data['status']+=':NON_WINDOW_REGION'
    except (ReferenceError,RuntimeError,AttributeError,ValueError,TypeError) as exc:
        data['status']='UNKNOWN'
        data['reason']=str(exc)[:300]
    return data


class IMA_GT_NavButton(Gizmo):
    """Invisible native Blender hit proxy for a GPU-rendered round button.

    The complete visible HUD is painted by _draw_issue_nav_counter. This
    gizmo contributes only picking and operator dispatch, so it can never
    show a mismatched or late button relative to the panel.
    """
    bl_idname = 'IMA_GT_nav_button'
    bl_target_properties = ()

    slot: bpy.props.IntProperty(default=0, min=0, max=3)
    tool_icon: bpy.props.StringProperty(default="")
    pixel_size: bpy.props.FloatProperty(default=36.0, min=18.0, max=80.0)
    weld_style: bpy.props.BoolProperty(default=False)

    def setup(self):
        self.use_draw_hover = True
        self.use_event_handle_all = False  # Never swallow Blender undo/hotkeys.
        self.use_undo = False  # Navigation must not push an Undo step.
        self.use_tooltip = True
        # Explicit GPU selection shape is supported by Blender 4.5 and 5.x.
        # Local square [-.5,+.5] is scaled to pixel_size by _pick_matrix().
        from math import cos, sin, pi
        n=32
        vertices=[]
        for i in range(n):
            a=2*pi*i/n; b=2*pi*(i+1)/n
            vertices.extend(((0.,0.,0.), (.5*cos(a),.5*sin(a),0.),
                             (.5*cos(b),.5*sin(b),0.)))
        self._hit_shape=self.new_custom_shape('TRIS',tuple(vertices))
        self.color = (0.0, 0.0, 0.0)
        self.color_highlight = (0.0, 0.0, 0.0)
        self.alpha = 0.0
        self.alpha_highlight = 0.0

    def _pick_matrix(self):
        return self.matrix_basis @ Matrix.Scale(self.pixel_size, 4)

    def draw(self, context):
        if not self.hide:
            # Keep a selectable native gizmo, but all visible geometry is
            # emitted by the same passive HUD draw handler as the panel.
            self.draw_custom_shape(self._hit_shape, matrix=self._pick_matrix())

    def draw_select(self, context, select_id):
        if not self.hide:
            self.draw_custom_shape(self._hit_shape,
                                   matrix=self._pick_matrix(), select_id=select_id)

    def test_select(self, context, location):
        if self.hide or getattr(context.region,'type',None)!='WINDOW':
            return -1
        pos=self.matrix_basis.translation
        half=self.pixel_size*.5
        x,y=location
        found = (x-pos.x)**2 + (y-pos.y)**2 <= half**2
        if found:
            try:
                key=(context.area.as_pointer(),context.region.as_pointer())
                _NAV_HOVER[key]=(self.slot,time.monotonic())
                if not bpy.app.timers.is_registered(_ui_redraw_timer):
                    bpy.app.timers.register(_ui_redraw_timer,first_interval=.035)
                context.area.tag_redraw()
            except (ReferenceError,AttributeError,RuntimeError,ValueError):pass
        return 0 if found else -1


def _nav_button(group, operator, slot, weld=False, **properties):
    gizmo=group.gizmos.new(IMA_GT_NavButton.bl_idname)
    gizmo.slot=slot
    gizmo.weld_style=weld
    gizmo.pixel_size=36.0
    target=gizmo.target_set_operator(operator)
    for name,value in properties.items():
        setattr(target,name,value)
    return gizmo


class IMA_GGT_IssueNavigator(GizmoGroup):
    """True rounded-square navigator, in the bottom-center of the 3D Viewport."""
    bl_idname = 'IMA_GGT_issue_navigator'
    bl_label = 'Проблемы · навигация'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        if not (context.mode == 'EDIT_MESH' and context.area and
                context.area.type == 'VIEW_3D'):
            return False
        return bool(weld_preview.context_matches(context) or
                    (view_tools.issues_active() and view_tools.issues_context_matches(context)))

    def setup(self, context):
        # Independent Weld and UV operators preserve Undo ownership.
        self.buttons = [
            _nav_button(self,'ima.issue_navigate',0,direction=-1),
            _nav_button(self,'ima.issue_navigate',1,direction=1),
            _nav_button(self,'ima.select_all_issues',2),
            _nav_button(self,'ima.exit_analysis',3),
            _nav_button(self,'ima.weld_step',0,True,direction=-1),
            _nav_button(self,'ima.weld_step',1,True,direction=1),
            _nav_button(self,'ima.weld_commit',2,True),
            _nav_button(self,'ima.weld_exit',3,True),
        ]

    def draw_prepare(self, context):
        layout=_nav_layout(context)
        start=4 if weld_preview.context_matches(context) else 0
        for button in self.buttons:
            button.hide=True
        if layout is None:
            return
        _HUD_HIT_PROXY[(context.area.as_pointer(), context.region.as_pointer())] = time.monotonic()
        for i,button in enumerate(self.buttons[start:start+4]):
            x,y,size=layout['buttons'][i]
            button.hide=False
            button.pixel_size=size
            button.matrix_basis=Matrix.Translation((x,y,0))


class IMA_GGT_UVIssueNavigator(GizmoGroup):
    """Identical HUD semantics for UV Editor, no duplicate header arrows."""
    bl_idname = 'IMA_GGT_uv_issue_navigator'
    bl_label = 'Проблемы · UV-навигация'
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type=='IMAGE_EDITOR' and
                    context.mode=='EDIT_MESH' and view_tools.issues_active() and
                    view_tools.issues_context_matches(context))

    def setup(self, context):
        self.buttons = [
            _nav_button(self,'ima.issue_navigate',0,direction=-1),
            _nav_button(self,'ima.issue_navigate',1,direction=1),
            _nav_button(self,'ima.select_all_issues',2),
            _nav_button(self,'ima.exit_analysis',3),
        ]

    def draw_prepare(self, context):
        layout=_nav_layout(context)
        if layout is not None:
            _HUD_HIT_PROXY[(context.area.as_pointer(), context.region.as_pointer())] = time.monotonic()
        for i,button in enumerate(self.buttons):
            button.hide=layout is None
            if layout is not None:
                x,y,size=layout['buttons'][i]
                button.pixel_size=size
                button.matrix_basis=Matrix.Translation((x,y,0))


class IMA_GGT_EditorSplit(GizmoGroup):
    bl_idname = "IMA_GGT_editor_split"
    bl_label = "Добавить рабочее окно"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and
                    context.mode in {'OBJECT', 'EDIT_MESH'})

    def setup(self, context):
        self.buttons = [
            _button(self, "ima.split_editor", 'UV_DATA', editor='UV'),
            _button(self, "ima.split_editor", 'NODE_MATERIAL', editor='SHADER'),
        ]

    def draw_prepare(self, context):
        positions = _viewport_positions(context, _dock_count(context))['editor']
        for gizmo, xy in zip(self.buttons, positions):
            gizmo.hide = False
            gizmo.matrix_basis = Matrix.Translation((*xy, 0))
            _skin(gizmo, context)
        for gizmo in self.buttons[len(positions):]:
            gizmo.hide = True
        _track_tool_highlight(context, self.buttons)


class IMA_GGT_UV(GizmoGroup):
    bl_idname = "IMA_GGT_uv"
    bl_label = "Быстрые инструменты UV Editor"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "active_object", None)
        return bool(context.area and context.area.type == 'IMAGE_EDITOR' and
                    context.mode == 'EDIT_MESH' and obj and obj.type == 'MESH')

    def setup(self, context):
        self.buttons = [
            _button(self, "ima.uv_align", 'PIVOT_BOUNDBOX'),
            _button(self, "ima.set_texel_density", 'UV_DATA'),
            _button(self, "ima.unwrap_glass", 'MOD_UVPROJECT'),
        ]

    def draw_prepare(self, context):
        visible = True
        for gizmo in self.buttons:
            gizmo.hide = not visible
        if not visible:
            return
        for gizmo, xy in zip(self.buttons, _strip_positions(context, len(self.buttons))):
            gizmo.matrix_basis = Matrix.Translation((*xy, 0))
            _skin(gizmo, context)
        _track_tool_highlight(context, self.buttons)


class IMA_GGT_Shader(GizmoGroup):
    bl_idname = "IMA_GGT_shader"
    bl_label = "Быстрые инструменты Shader Editor"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        return bool(space and getattr(space, "tree_type", '') == 'ShaderNodeTree')

    def setup(self, context):
        self.buttons = [
            _button(self, "ima.clean_material", 'TRASH'),
            _button(self, "ima.material_choice", 'NODE_MATERIAL'),
        ]

    def draw_prepare(self, context):
        visible = True
        for gizmo in self.buttons:
            gizmo.hide = not visible
        if not visible:
            return
        for gizmo, xy in zip(self.buttons, _strip_positions(context, len(self.buttons))):
            gizmo.matrix_basis = Matrix.Translation((*xy, 0))
            _skin(gizmo, context)
        _track_tool_highlight(context, self.buttons)


def draw_uv_header(self, context):
    obj = getattr(context, 'active_object', None)
    if not obj or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return
    row = self.layout.row(align=True)
    row.separator()
    if context.area.width < 680:
        return
    sync = context.tool_settings.use_uv_select_sync
    row.label(text='Mesh' if sync else 'UV',
              icon='RESTRICT_SELECT_OFF' if sync else 'UV_DATA')
    if view_tools.issues_active() and view_tools.issues_context_matches(context):
        status = uv_feedback.status()
        navigation = view_tools.issue_navigation_status()
        if context.area.width >= 850:
            row.label(text='%s %d' % ('Flip' if view_tools.issue_kind() == 'FLIPPED'
                                      else 'Потяги', navigation['stored']), icon='ERROR')
        row.operator('ima.toggle_uv_feedback', text='',
                     icon='HIDE_OFF' if _pref(context, 'show_uv_issue_overlay', True) else 'HIDE_ON')
    if context.area.width >= 910:
        row.operator('ima.uv_align', text='', icon='PIVOT_BOUNDBOX')
        row.operator('ima.set_texel_density', text='1000', icon='UV_DATA')
        row.operator('ima.unwrap_glass', text='Стекло', icon='MOD_UVPROJECT')


class IMA_PT_VisualSettings(Panel):
    bl_idname = 'IMA_PT_visual_settings'
    bl_label = 'ISSMAKER · Modeling Suite'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ISSMAKER'

    @staticmethod
    def _tabs(layout, scene, field, entries):
        # 2 columns, never 4 blank expanding RNA tabs squeezed to <50px.
        for offset in range(0, len(entries), 2):
            row = layout.row(align=True)
            row.scale_y = 1.23
            for key, text, icon in entries[offset:offset + 2]:
                row.prop_enum(scene, field, key, text=text, icon=icon)

    @staticmethod
    def _opening_status(context, layout):
        from .tools.brick_hole import integration as integration
        if integration.is_preview_active():
            layout.label(text='Preview уже открыт', icon='INFO')
            return False
        if context.mode != 'EDIT_MESH':
            layout.label(text='Сначала: Tab → Edit Mode', icon='INFO')
            return False
        obj = context.edit_object
        if not obj or obj.type != 'MESH':
            layout.label(text='Нужен Mesh в Edit Mode', icon='ERROR')
            return False
        selection = tuple(bool(x) for x in context.tool_settings.mesh_select_mode)
        if selection not in ((False, True, False), (False, False, True)):
            layout.label(text='Выбери режим: 2 или 3', icon='INFO')
            layout.label(text='2 — рёбра, 3 — грани')
            return False
        label = 'Рёбра (2)' if selection[1] else 'Грани (3)'
        layout.label(text='Режим: ' + label, icon='CHECKMARK')
        layout.label(text='Выдели контур проёма', icon='INFO')
        return True

    @staticmethod
    def _openings_main(context, layout):
        scene = context.scene
        box = layout.box()
        box.label(text='Создание проёма', icon='MOD_SOLIDIFY')
        can_invoke = IMA_PT_VisualSettings._opening_status(context, box)
        settings = layout.box()
        settings.label(text='Параметры', icon='SETTINGS')
        col = settings.column(align=True)
        col.prop(scene, 'brick_hole_depth', text='Глубина, м')
        col.prop(scene, 'brick_hole_direction', text='Направление')
        if context.mode == 'EDIT_MESH' and context.edit_object is not None:
            flags = tuple(bool(x) for x in context.tool_settings.mesh_select_mode)
            if flags == (False, True, False):
                col.prop(scene, 'brick_hole_direction_source', text='Источник оси')
                if scene.brick_hole_direction_source in {'GLOBAL','FACE','MANUAL'}:
                    col.prop(scene, 'brick_hole_manual_normal', text='Ось XYZ')
                    row = col.row(align=True)
                    for axis in ('X', 'Y', 'Z'):
                        row.operator('mesh.brick_hole_set_manual_axis', text=axis).axis = axis
        # BH-03.2C · On-demand safe Edge candidate; no automatic modification.
        if (context.mode == 'EDIT_MESH' and context.edit_object is not None
                and tuple(bool(v) for v in context.tool_settings.mesh_select_mode)==(False,True,False)):
            safe_box=layout.box()
            safe_box.label(text='SAFE DEPTH · Предел для рёбер',icon='LOCKED')
            safe_buttons=safe_box.row(align=True)
            safe_buttons.operator('mesh.brick_hole_safe_depth',
                text='Найти допустимую',icon='VIEWZOOM').action='FIND'
            safe_buttons.operator('mesh.brick_hole_safe_depth',
                text='Применить',icon='CHECKMARK').action='USE'
            safe_box.label(text=str(getattr(scene,'brick_hole_safe_depth_status',
                'Проверка ещё не проводилась'))[:115],icon='INFO')
            safe_box.label(text='Не максимальная глубина · пересчёт перед применением',icon='INFO')
        actions = layout.column(align=True)
        actions.scale_y = 1.18
        launch = actions.row()
        launch.enabled = can_invoke
        launch.operator('ima.launch_tool', text='НАСТРОИТЬ В VIEWPORT', icon='GIZMO').tool_id = 'BRICK_HOLE'
        # Pass through to the original production operators for Mesh operations.
        row = actions.row(align=True)
        row.enabled = can_invoke
        row.operator('mesh.brick_hole_extrude_unified', text='Создать', icon='MOD_SOLIDIFY')
        row.operator('mesh.brick_hole_preview_unified', text='Проверить', icon='VIEWZOOM')
        # PASS05: transaction failures remain visible after the fading Blender
        # report; use the same persistent notice mechanism as other tools.
        recent_error = str(getattr(scene, 'brick_hole_preview_text', ''))
        if recent_error.startswith('ERROR:'):
            error_box = layout.box()
            error_box.alert = True
            error_box.label(text='Последняя ошибка построения', icon='ERROR')
            import textwrap
            for piece in textwrap.wrap(recent_error[6:].strip(), width=40,
                                       break_long_words=False)[:6]:
                error_box.label(text=piece)
            error_box.label(text='Подробнее: BrickHole_Log', icon='CONSOLE')
        state = str(scene.brick_hole_interactive_state)
        if state.startswith(('ERROR', 'STALE', 'BLOCKED')):
            error = layout.box()
            error.alert = True
            error.label(text='Ошибка запуска Preview', icon='ERROR')
            # Full diagnostics remain available; do not dump an 80-char
            # traceback label that gets clipped by a narrow N-panel.
            error.label(text=state[:28])
        if not can_invoke:
            hint = layout.box()
            hint.label(text='Когда режим будет готов,', icon='INFO')
            hint.label(text='кнопка станет доступна.')

    @staticmethod
    def _openings_subsection(context, layout, section):
        from types import SimpleNamespace
        from .tools import brick_hole as bh
        # Reuse the existing property handlers but never reuse their own
        # N-panel registration; keep the package's data model unchanged.
        panels = {
            'GEOMETRY': bh.VIEW3D_PT_brick_hole_geometry,
            'APPEARANCE': bh.VIEW3D_PT_brick_hole_texture,
            'TESTS': bh.VIEW3D_PT_brick_hole_advanced,
        }
        panel = panels.get(section)
        if panel is None:
            return
        box = layout.box()
        box.label(text={'GEOMETRY':'UV и материалы',
                        'APPEARANCE':'Внешний вид',
                        'TESTS':'Тесты и диагностика'}[section],
                  icon={'GEOMETRY':'UV_DATA','APPEARANCE':'HIDE_OFF',
                        'TESTS':'CHECKMARK'}[section])
        panel.draw(SimpleNamespace(layout=box), context)

    @staticmethod
    def _collision_main(context, layout):
        from .tools.collision_inspector import _viewport_ui
        settings = context.scene.collision_inspector
        ready, reason = collision_integration.can_start(context)
        box = layout.box()
        box.label(text='Collision Inspector · 3.5.2', icon='MOD_PHYSICS')
        obj = getattr(context, 'active_object', None)
        box.label(text=('Mesh: ' + obj.name[:20]) if obj and obj.type == 'MESH' else 'Нет активного Mesh')
        if not ready:
            box.label(text=reason[:38], icon='INFO')
        run = layout.column(align=True)
        run.scale_y = 1.35
        row = run.row()
        row.operator('ima.launch_tool', text='ПРОВЕРИТЬ МОДЕЛЬ', icon='VIEWZOOM').tool_id = 'COLLISION'
        info = layout.box()
        status = settings.status or 'Проверка ещё не запускалась'
        info.label(text=status[:40], icon='ERROR' if settings.last_error else 'INFO')
        if settings.analysis_signature:
            info.label(text='Оболочек: %d · проблем: %d' % (settings.shell_count, len(settings.events)))
            controls = layout.row(align=True)
            controls.operator('ima.collision_visibility', text='Показать', icon='HIDE_OFF').show = True
            controls.operator('ima.collision_visibility', text='Скрыть', icon='HIDE_ON').show = False
            controls = layout.row(align=True)
            controls.operator('collision_inspector.previous', text='Назад', icon='TRIA_LEFT')
            controls.operator('collision_inspector.next', text='Далее', icon='TRIA_RIGHT')
            if settings.events:
                layout.operator('collision_inspector.frame', text='К проблеме', icon='VIEWZOOM')
        if _viewport_ui.draw_error():
            layout.label(text='Ошибка HUD — см. диагностику', icon='ERROR')
        layout.label(text='Esc скрывает активный инспектор', icon='INFO')

    @staticmethod
    def _collision_subsection(context, layout, section):
        from types import SimpleNamespace
        from .tools import collision_inspector as ci
        panels = {
            'ISSUES': ci.COLLISIONINSPECTOR_PT_findings,
            'VIEW': ci.COLLISIONINSPECTOR_PT_viewport,
            'REPAIR': ci.COLLISIONINSPECTOR_PT_repair,
            'SETTINGS': ci.COLLISIONINSPECTOR_PT_settings,
            'EXAMPLES': ci.COLLISIONINSPECTOR_PT_examples,
            'LOG': ci.COLLISIONINSPECTOR_PT_diagnostics,
        }
        panel = panels.get(section)
        if panel is None:
            return
        box = layout.box()
        box.label(text={
            'ISSUES':'Проблемы', 'VIEW':'Вид и маркеры',
            'REPAIR':'Исправление', 'SETTINGS':'Настройки проверки',
            'EXAMPLES':'Тестовые модели · 55', 'LOG':'Диагностика'
        }[section], icon='MOD_PHYSICS')
        if section == 'ISSUES' and not context.scene.collision_inspector.events:
            box.label(text='Сначала проверьте объект', icon='INFO')
        else:
            panel.draw(SimpleNamespace(layout=box), context)
        if section == 'LOG':
            box.operator('collision_inspector.diagnostics_zip', text='Collision ZIP', icon='FILE_FOLDER')
            row = box.row(align=True)
            row.operator('collision_inspector.copy_log', text='Лог', icon='COPYDOWN')
            row.operator('collision_inspector.copy_error', text='Ошибки', icon='ERROR')

    @staticmethod
    def _dwos_main(context, layout):
        ready, reason, action = dwos_integration.can_start(context)
        obj=getattr(context,'active_object',None)
        if (obj is not None and getattr(obj,'type',None)=='MESH'
                and getattr(context,'mode',None)=='EDIT_MESH'):
            manual=layout.box()
            manual.label(text='РУЧНОЙ КОНТУР ОКНА',icon='EDITMODE_HLT')
            manual.label(text='Грани дна проёма или его рёбра')
            manual.operator('dwos.use_selected_cap_faces',
                            text='Использовать выделенные грани',icon='FACESEL')
            manual.operator('dwos.use_selected_openings',
                            text='Использовать выделенные контуры',icon='EDGESEL')
            manual.label(text='Затем Tab: Object Mode для создания')
        box=layout.box()
        box.label(text='DWOS Window Core · 3.7.37 RC38',icon='SHADING_SOLID')
        obj=getattr(context,'active_object',None)
        box.label(text='Выбрано: '+obj.name[:26] if obj else 'Объект не выделен')
        if ready:
            box.label(text='Composer готов' if action=='COMPOSER' else 'Готов к поиску проёмов',icon='CHECKMARK')
        else:
            box.alert=True
            import textwrap
            for line in textwrap.wrap(reason,35,break_long_words=False):
                box.label(text=line,icon='INFO' if line==reason else 'NONE')
        row=layout.column(align=True);row.scale_y=1.4
        row.operator('ima.launch_tool',text=('ОТКРЫТЬ COMPOSER' if action=='COMPOSER' else 'НАЙТИ ПРОЁМЫ / ОТКРЫТЬ ОКНА'),icon='PLAY').tool_id='DWOS'
        state=getattr(context.scene,'dwos',None)
        if state is None:
            layout.label(text='DWOS не зарегистрирован',icon='ERROR');return
        if state.status:
            box=layout.box()
            import textwrap
            for line in textwrap.wrap(state.status,36,break_long_words=False)[:4]: box.label(text=line)
        windows=len(state.openings)
        if windows:
            suspect=[c for c in state.openings if c.enabled and _dwos_suspect(c)]
            if suspect:
                warning=layout.box()
                warning.alert=True
                warning.label(text=f'Проверьте CURVED: {len(suspect)}',icon='ERROR')
                warning.label(text='Возможен ложный контур стены')
                warning.operator('ima.dwos_exclude_suspicious',text='Исключить подозрительные',icon='REMOVE')
            # Show explicit candidate selection: one bad shape must not hide a valid 4m window.
            candidates=layout.box()
            candidates.label(text='Выбор проёмов',icon='MESH_GRID')
            for i,c in enumerate(state.openings):
                line=candidates.row(align=True)
                line.prop(c,'enabled',text='')
                line.label(text=f'{i+1}: {c.width_text} × {c.height_text} м'[:35])
                if _dwos_suspect(c):line.label(text='',icon='ERROR')

            box=layout.box()
            box.label(text=f'Найдено проёмов: {windows}',icon='CHECKMARK')
            if _dwos_creation_state(context)[0]:
                box.operator('ima.dwos_create_windows',text='СОЗДАТЬ ОКНА',icon='MESH_GRID')
            else:
                box.label(text='Для новой группы повторите поиск',icon='INFO')
        layout.label(text='Настройки, COPY и диагностика — вкладки ниже',icon='INFO')

    @staticmethod
    def _dwos_subsection(context, layout, section):
        from types import SimpleNamespace
        from .tools.dwos_windows.native import ui as dwos_ui
        if section == 'STUDIO':
            box=layout.box()
            box.label(text='Оригинальные настройки DWOS',icon='TOOL_SETTINGS')
            # Delegates to native draw without registering its standalone N-panel.
            dwos_ui.DWOS_PT_main.draw(SimpleNamespace(layout=box),context)
        elif section == 'DIAG':
            box=layout.box()
            box.label(text='DWOS · Контроль качества',icon='CHECKMARK')
            box.operator('dwos.audit_mesh_consistency',text='Аудит рам и стекла',icon='CHECKMARK')
            box.operator('dwos.analyze_meshes',text='Полный Mesh Analysis',icon='VIEWZOOM').reason='MANUAL_RELEASE'
            box.operator('dwos.export_field_bundle',text='DWOS ZIP · диагностика',icon='PACKAGE')
            from .tools.dwos_windows.native.surface_steps import draw_diagnostic
            draw_diagnostic(box,context)
            box.label(text='Генерация тестов — в Настройках DWOS')
        else:
            layout.label(text='Раздел пока недоступен',icon='INFO')

    @staticmethod
    def _wall_main(context, layout):
        settings = getattr(context.scene, 'uv_wall_settings', None)
        if settings is None:
            layout.label(text='UV Wall не зарегистрирован', icon='ERROR')
            return
        ready, reason = uv_wall_integration.can_start(context)
        info = layout.box()
        info.label(text='UV Wall Professional · 1.18.17', icon='MOD_WIREFRAME')
        path = getattr(context, 'active_object', None)
        info.label(text=('Активный: ' + path.name[:25]) if path else 'Активный объект не выбран')
        if ready:
            info.label(text='Контур готов · только рёбра', icon='CHECKMARK')
        else:
            info.alert = True
            from textwrap import wrap
            for line in wrap(reason, 36, break_long_words=False):
                info.label(text=line, icon='INFO' if line == reason else 'NONE')
        action = layout.column(align=True)
        action.scale_y = 1.35
        active = uv_wall_integration.status(context)['active']
        if active:
            action.operator('uvwall.viewport_inspector_exit', text='ЗАКРЫТЬ ИНСПЕКТОР', icon='X')
            action.operator('uvwall.finalize_result', text='ПРИНЯТЬ СТЕНУ · ENTER', icon='CHECKMARK')
        else:
            # Never disable; click shows a meaningful context popup.
            action.operator('ima.launch_tool', text='ОТКРЫТЬ СТЕНУ В VIEWPORT', icon='PLAY').tool_id = 'UV_WALL'
        box = layout.box()
        box.label(text='Путь и проект', icon='OUTLINER_OB_MESH')
        box.prop(settings, 'build_mode', text='Инструмент')
        row = box.row(align=True)
        row.prop(settings, 'path_mesh', text='Путь')
        row.operator('object.uv_wall_use_active_as_path', text='', icon='EYEDROPPER')
        box.label(text='Для Preview нужен активный контур', icon='INFO')
        box.label(text='Режим: Object · Mesh без граней')
        if len(settings.problems):
            row = layout.row()
            row.alert = True
            row.label(text='Проблем: %d' % len(settings.problems), icon='ERROR')
            row.operator('object.uv_wall_problem_navigate', text='Показать', icon='VIEWZOOM').direction = 0

    @staticmethod
    def _wall_subsection(context, layout, section):
        settings = getattr(context.scene, 'uv_wall_settings', None)
        if settings is None:
            layout.label(text='Модуль стены не загружен', icon='ERROR')
            return
        if section == 'PARAMETERS':
            box = layout.box()
            box.label(text='Параметры проекта', icon='MOD_SOLIDIFY')
            box.prop(settings, 'module_standard', text='Модуль')
            if settings.build_mode == 'WALL':
                box.prop(settings, 'stories', text='Этажей')
                box.prop(settings, 'create_inner', text='Двойная стена')
                box.prop(settings, 'create_top_cap', text='Верхняя крышка')
                box.prop(settings, 'create_bottom_cap', text='Нижняя крышка')
            elif settings.build_mode == 'CORNICE':
                box.prop(settings, 'cornice_profile', text='Профиль')
                box.operator('object.uv_wall_use_active_as_profile', text='Профиль из выделения', icon='EYEDROPPER')
            else:
                box.prop(settings, 'surface_height_offset', text='Смещение Z')
            box.label(text='Полное редактирование — во Viewport', icon='INFO')
        elif section == 'VIEW':
            prefs = uv_wall_integration._load_native()._ui_preferences.get_preferences(context)
            box = layout.box()
            box.label(text='Интерфейс UV Wall', icon='WINDOW')
            if prefs is not None:
                box.prop(prefs, 'ui_mode', text='Режим')
                box.label(text='Цвета и фон: ISSMAKER → Внешний вид', icon='INFO')
                if prefs.ui_mode == 'STUDIO_DOCK':
                    box.prop(prefs, 'dock_sizing', text='Размер Dock')
            else:
                box.label(text='Настройки доступны в Preferences ISSMAKER')
            box = layout.box()
            box.label(text='Визуализация и UV-контроль — в нижней панели',icon='INFO')
            box.operator('ima.geometry_quality_popup',text='Открыть меню геометрии',icon='MOD_WIREFRAME')
        elif section == 'LOG':
            box = layout.box()
            box.label(text='Диагностика стены', icon='CONSOLE')
            row = box.row(align=True)
            row.operator('object.uv_wall_copy_log', text='Копировать лог', icon='COPYDOWN')
            row.operator('object.uv_wall_collect_support', text='ZIP', icon='EXPORT')
            box.operator('object.uv_wall_quick_check', text='Быстрая проверка', icon='CHECKMARK')
            box.operator('ima.audit_wall_seams', text='Найти зазоры между модулями', icon='VIEWZOOM')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        # Support is visible regardless of active tool/sidebar section.
        support=layout.box()
        support.label(text='Поддержка', icon='QUESTION')
        row=support.row(align=True)
        row.operator('ima.open_support_link', text='Telegram: @isssmaker', icon='URL').target='AUTHOR'
        row=support.row(align=True)
        row.operator('ima.open_support_link', text='Техлид: @mishashkonda', icon='URL').target='TECHLEAD'
        support.operator('ima.copy_full_support_log', text='СОБРАТЬ ЛОГИ И ОТКРЫТЬ TELEGRAM', icon='COPYDOWN')
        support.label(text='ZIP в буфере (Desktop) или прикрепить файл', icon='INFO')
        if not hasattr(scene, 'ima_suite_tab'):
            layout.label(text='Не загружены настройки', icon='ERROR')
            return
        message = getattr(scene, 'ima_notice_message', '')
        if message:
            box=layout.box()
            box.alert=(getattr(scene,'ima_notice_kind','WARNING')!='INFO')
            row=box.row(align=True)
            row.label(text='Требуется действие' if box.alert else 'Информация', icon='ERROR' if box.alert else 'INFO')
            row.operator('ima.clear_notice', text='', icon='X')
            import textwrap
            for line in textwrap.wrap(message, 35, break_long_words=False, break_on_hyphens=False):
                box.label(text=line)
        # Pass09: standalone control in N-panel, not the bottom icon/main radial menu.
        row=layout.row(align=True)
        row.operator('ima.window_visibility',text='Окна HUD · показать / скрыть',icon='WINDOW')
        layout.label(text='РАБОЧАЯ ОБЛАСТЬ', icon='TOOL_SETTINGS')
        self._tabs(layout, scene, 'ima_suite_tab', (
            ('ASSISTANT', 'Ассистент', 'MODIFIER'),
            ('OPENINGS', 'Проёмы', 'MOD_SOLIDIFY'),
            ('COLLISION', 'Коллизии', 'MOD_PHYSICS'),
            ('UV_WALL', 'Стены', 'MOD_WIREFRAME'),
            ('DWOS', 'Окна', 'WINDOW'),
            ('SETTINGS', 'Настройки', 'PREFERENCES'),
            ('DIAGNOSTICS', 'Диагностика', 'CONSOLE'),
        ))
        layout.separator(factor=.75)
        section = scene.ima_suite_tab
        if section == 'ASSISTANT':
            layout.label(text='Инструменты Viewport', icon='VIEW3D')
            box = layout.box()
            box.label(text='Справа внизу 3D-окна:')
            box.label(text='UCX  |  Вид  |  Инструменты')
            action = layout.column(align=True)
            action.scale_y = 1.2
            action.operator('ima.tool_radial', text='ОТКРЫТЬ МЕНЮ ИНСТРУМЕНТОВ', icon='MODIFIER')
            layout.label(text='UV · Weld · Quick Select', icon='INFO')
        elif section == 'OPENINGS':
            layout.label(text='ПРОЁМЫ · Brick Hole 1.0.17', icon='MOD_SOLIDIFY')
            self._tabs(layout, scene, 'ima_openings_tab', (
                ('MAIN', 'Основное', 'GIZMO'),
                ('GEOMETRY', 'UV / Мат', 'UV_DATA'),
                ('APPEARANCE', 'Вид', 'HIDE_OFF'),
                ('TESTS', 'Проверки', 'CHECKMARK'),
            ))
            layout.separator(factor=.5)
            if scene.ima_openings_tab == 'MAIN':
                self._openings_main(context, layout)
            else:
                self._openings_subsection(context, layout, scene.ima_openings_tab)
        elif section == 'COLLISION':
            layout.label(text='КОЛЛИЗИИ · Inspector 3.5.2', icon='MOD_PHYSICS')
            self._tabs(layout, scene, 'ima_collision_tab', (
                ('MAIN', 'Основное', 'VIEWZOOM'),
                ('ISSUES', 'Проблемы', 'ERROR'),
                ('VIEW', 'Вид', 'HIDE_OFF'),
                ('REPAIR', 'Исправление', 'MOD_TRIANGULATE'),
                ('SETTINGS', 'Параметры', 'PREFERENCES'),
                ('EXAMPLES', 'Тесты 55', 'CHECKMARK'),
                ('LOG', 'Логи', 'CONSOLE'),
            ))
            layout.separator(factor=.5)
            if scene.ima_collision_tab == 'MAIN':
                self._collision_main(context, layout)
            else:
                self._collision_subsection(context, layout, scene.ima_collision_tab)
        elif section == 'UV_WALL':
            layout.label(text='СТЕНЫ · UV Wall 1.18.17', icon='MOD_WIREFRAME')
            self._tabs(layout, scene, 'ima_wall_tab', (
                ('MAIN', 'Основное', 'PLAY'),
                ('PARAMETERS', 'Параметры', 'MOD_SOLIDIFY'),
                ('VIEW', 'Интерфейс', 'WINDOW'),
                ('LOG', 'Диагностика', 'CONSOLE'),
            ))
            layout.separator(factor=.5)
            if scene.ima_wall_tab == 'MAIN':
                self._wall_main(context, layout)
            else:
                self._wall_subsection(context, layout, scene.ima_wall_tab)
        elif section == 'DWOS':
            layout.label(text='ОКНА И ДВЕРИ · DWOS 3.7.37',icon='WINDOW')
            self._tabs(layout,scene,'ima_dwos_tab', (
                ('MAIN','Основное','VIEW3D'),('STUDIO','DWOS Studio','PREFERENCES'),
                ('DIAG','Диагностика','CONSOLE'),))
            if scene.ima_dwos_tab=='MAIN': self._dwos_main(context,layout)
            else: self._dwos_subsection(context,layout,scene.ima_dwos_tab)
        elif section == 'SETTINGS':
            prefs = _preferences(context)
            if prefs is not None:
                _draw_visual_preferences(layout, prefs)
            else:
                layout.label(text='Настройки в Preferences', icon='INFO')
        elif section == 'DIAGNOSTICS':
            state = brick_integration.status()
            box = layout.box()
            box.label(text='Brick Hole · 1.0.17', icon='FILE_FOLDER')
            box.label(text='Классы: %s' % state['registered_classes'])
            box.label(text='Preview: %s' % ('Активен' if state['preview_active'] else 'Выключен'))
            box.label(text='DWOS 3.7.37 · модуль подключён', icon='INFO')
            box.label(text='Reliability Foundation RF1 · общий Geometry Kernel', icon='SHIELD')
            layout.operator('ima.diagnostics_zip', text='Собрать диагностику ZIP', icon='FILE_FOLDER')
            deep_box = layout.box()
            deep_box.label(text='PRODUCTION · DEEP AUTOLAB', icon='MOD_BUILD')
            from . import deep_autolab
            deep_state = deep_autolab.state()
            control = deep_box.row(align=True)
            control.enabled = deep_state.get('status') != 'RUNNING'
            control.operator('ima.deep_autolab', text='ОДНА КНОПКА · СЛОЖНЫЕ ТЕСТЫ → ZIP', icon='PLAY')
            if deep_state.get('status') == 'RUNNING':
                deep_box.label(text='Выполняется: '+deep_state.get('case','')[:44],icon='TIME')
                deep_box.label(text='Сценариев завершено: '+str(deep_state.get('cases',0)))
            elif deep_state.get('status') != 'NOT_RUN':
                deep_box.label(text='DEEP LAB: '+str(deep_state.get('status')),icon='INFO')
                deep_box.label(text='ZIP: '+str(deep_state.get('path',''))[-57:])
                deep_box.label(text='Путь скопирован в буфер',icon='COPYDOWN')
            deep_box.label(text='Отдельный Blender · текущая сцена не изменяется')
            deep_box.label(text='GUI-клики и Ctrl+Z — НЕ проверены', icon='INFO')
            layout.operator('ima.reliability_lab', text='RELIABILITY LAB · МАТЕМАТИКА ВСЕХ ИНСТРУМЕНТОВ', icon='CHECKMARK')
            row = layout.row(align=True)
            row.operator('wm.brick_hole_diagnostics_zip', text='Brick Hole ZIP', icon='FILE_FOLDER')
            row.operator('wm.brick_hole_copy_log', text='Лог', icon='COPYDOWN')
            row = layout.row(align=True)
            row.operator('collision_inspector.diagnostics_zip', text='Collision ZIP', icon='FILE_FOLDER')
            row.operator('collision_inspector.copy_log', text='Лог коллизий', icon='COPYDOWN')
            layout.label(text='ZIP может содержать пути', icon='INFO')
            # HUD Pass 06: interactive Blender-side evidence, not simulated clicks.
            from . import hud_evidence_lab
            lab_box = layout.box()
            lab_box.label(text='HUD EVIDENCE LAB · AUTO TEST ALL', icon='VIEW3D')
            auto_status = hud_evidence_lab.auto_state()
            auto_row = lab_box.row(align=True)
            auto_row.enabled = not (auto_status and auto_status.get('status') == 'RUNNING')
            auto_row.operator('ima.hud_lab_auto_all', text='AUTO TEST ALL · ОДНА КНОПКА → ZIP', icon='PLAY')
            if auto_status:
                if auto_status['status'] == 'RUNNING':
                    lab_box.label(text=f"Выполняется: {auto_status['progress']}/{auto_status['total']}", icon='TIME')
                    lab_box.label(text=str(auto_status.get('detail',''))[:56])
                else:
                    lab_box.label(text='AUTO: '+str(auto_status['status'])[:40], icon='INFO')
                    lab_box.label(text='ZIP: '+str(auto_status.get('path',''))[-52:])
                    lab_box.label(text='Путь к ZIP скопирован, файл в Загрузках',icon='COPYDOWN')
            lab_box.label(text='Автоматические проверки + снимки Blender')
            lab_box.label(text='Клики и оценка стиля = НЕ ПРОВЕРЕНО',icon='INFO')
            lab_state = hud_evidence_lab.state(context)
            if lab_state is None:
                lab_box.label(text='Отдельная тестовая сцена', icon='SCENE_DATA')
                lab_box.label(text='Рабочая геометрия не изменяется')
                if hud_evidence_lab.pending_session():
                    lab_box.operator('ima.hud_lab_resume',text='Продолжить существующий HUD Lab',icon='LOOP_BACK')
                else:
                    lab_box.operator('ima.hud_lab_new', text='NEW TEST · создать всё для HUD', icon='ADD')
                lab_box.label(text='Реальные клики проверяются вручную', icon='INFO')
            else:
                step = lab_state['current']
                counts = lab_state['summary']['counts']
                lab_box.label(text=f"Шаг {lab_state['index']+1}/{lab_state['summary']['total']} · {step['id']}")
                lab_box.label(text='Оценка по текущему шагу: '+str(hud_evidence_lab.step_result(context)))
                from textwrap import wrap as _hud_wrap
                for line in _hud_wrap(step['name'], 35, break_long_words=False)[:3]:
                    lab_box.label(text=line)
                info = lab_box.column(align=True)
                for line in _hud_wrap(step['instruction'], 38, break_long_words=False)[:7]:
                    info.label(text=line)
                row = lab_box.row(align=True)
                row.operator('ima.hud_lab_step',text='Назад',icon='BACK').direction=-1
                row.operator('ima.hud_lab_step',text='Далее',icon='FORWARD').direction=1
                lab_box.operator('ima.hud_lab_fixture',text='Подготовить объект теста',icon='OUTLINER_OB_MESH')
                lab_box.operator('ima.hud_lab_capture',text='Снимок через 4 секунды',icon='RENDER_STILL')
                if lab_state['pending_capture']:
                    lab_box.label(text='Снимок запланирован · откройте HUD',icon='INFO')
                lab_box.prop(scene,'ima_hud_lab_note',text='Комментарий')
                row=lab_box.row(align=True)
                row.operator('ima.hud_lab_mark',text='PASS',icon='CHECKMARK').status='PASS'
                row.operator('ima.hud_lab_mark',text='FAIL',icon='ERROR').status='FAIL'
                row=lab_box.row(align=True)
                row.operator('ima.hud_lab_mark',text='BLOCKED').status='BLOCKED'
                row.operator('ima.hud_lab_mark',text='Не проверено').status='NOT_TESTED'
                lab_box.label(text=f"PASS {counts['PASS']}  FAIL {counts['FAIL']}  НЕ ПРОВ. {counts['NOT_TESTED']}")
                lab_box.label(text=f"BLOCKED {counts['BLOCKED']} · итог не автоподтверждён")
                if lab_state['last_error']:
                    for line in _hud_wrap(lab_state['last_error'],35,break_long_words=False)[:3]:
                        lab_box.label(text=line,icon='ERROR')
                lab_box.operator('ima.hud_lab_audit',text='Проверить размещение HUD',icon='VIEWZOOM')
                lab_box.operator('ima.hud_lab_export',text='СОБРАТЬ EVIDENCE ZIP',icon='PACKAGE')
                lab_box.operator('ima.hud_lab_exit',text='Вернуться в рабочую сцену',icon='LOOP_BACK')


_NAV_DRAW_HANDLER = None
_UV_NAV_DRAW_HANDLER = None


def _draw_issue_nav_counter():
    """Purely visual HUD panel; buttons are REAL native operator targets."""
    context=bpy.context
    if (not context.area or context.area.type not in {'VIEW_3D','IMAGE_EDITOR'} or
            not context.region or context.region.type!='WINDOW'):
        return
    try:
        if context.area.type=='VIEW_3D' and weld_preview.context_matches(context):
            st=weld_preview.state()
            kind,count,index='WELD',int(st['pairs']),int(st['cursor'])+1
        elif view_tools.issues_active() and view_tools.issues_context_matches(context):
            nav=view_tools.issue_navigation_status()
            kind='FLIP' if view_tools.issue_kind()=='FLIPPED' else 'STRETCH'
            count=int(nav.get('stored',0))
            index=int(nav.get('position',0))
        else:
            return
        layout=_nav_layout(context)
        if count<=0 or layout is None:
            return
        key=(context.area.as_pointer(),context.region.as_pointer())
        selected=_NAV_HOVER.get(key)
        highlight=(selected[0] if selected and time.monotonic()-selected[1]<.38 else -1)
        nav_hud_draw.draw_complete_hud(layout,kind,index,count,highlighted_slot=highlight)
    except Exception as exc:
        # Rendering never alters analysis, selection, or BMesh.
        _hud_fault_once(exc)



def _draw_context_notice(ctx):
    """Passive, styled 3D Viewport notice: no timer or mouse capture."""
    if not getattr(ctx, 'scene', None) or not hasattr(ctx.scene, 'ima_notice_message'):
        return
    msg=ctx.scene.ima_notice_message
    if not msg or ctx.area.type!='VIEW_3D' or ctx.region.type!='WINDOW':
        return
    try:
        import textwrap
        from .appearance_gpu import Painter
        from .core import viewport_skin, theme_engine
        width, height=ctx.region.width, ctx.region.height
        if width<320 or height<240:return
        lines=textwrap.wrap(msg,58,break_long_words=False,break_on_hyphens=False)[:3]
        box_w=min(530,width-28);box_h=39+len(lines)*22
        x=(width-box_w)*.5;y=height-box_h-70
        addon=ctx.preferences.addons.get('issmaker_modeling_assistant')
        theme=theme_engine.from_preferences(addon.preferences if addon else None)
        is_info=ctx.scene.ima_notice_kind=='INFO'
        role='primary' if is_info else 'warning'
        card=viewport_skin.component('card',role=role,theme=theme)
        title_color=viewport_skin.ink(theme,kind='accent') if is_info else viewport_skin.semantic_ink(theme,role='warning')
        painter=Painter()
        try:
            painter.box(x,y,box_w,box_h,card.background,card.border,rad=viewport_skin.control_radius('panel'))
            painter.box(x+11,y+box_h-25,3,15,(*theme.accent,1.) if is_info else title_color,None,rad=1.5)
            painter.text('ИНФОРМАЦИЯ' if is_info else 'ТРЕБУЕТСЯ ДЕЙСТВИЕ',
                         x+25,y+box_h-22,viewport_skin.text_px('title'),title_color,width=box_w-38)
            for i,line in enumerate(lines):
                painter.text(line,x+15,y+box_h-45-i*22,
                             viewport_skin.text_px('body'),viewport_skin.ink(theme),width=box_w-30)
        finally:painter.close()
    except (AttributeError,RuntimeError,ValueError,ReferenceError):
        # An overlay failure is non-fatal, as in the original renderer.
        return


def _draw_dwos_create_action(context):
    """Shared visual shell; native IMA_GGT_DWOSCreate still owns exact hit rect."""
    rect=_dwos_create_rect(context)
    if rect is None:return
    from .appearance_gpu import Painter
    from .core import viewport_skin, theme_engine
    x,y,width,height,count=rect
    item=context.preferences.addons.get('issmaker_modeling_assistant')
    theme=theme_engine.from_preferences(item.preferences if item else None)
    primary=viewport_skin.component('button','active','primary',theme)
    painter=Painter()
    try:
        # Painted control bounds match the native Gizmo hit rectangle.
        painter.box(x,y,width,height,primary.background,primary.border,
                    rad=viewport_skin.control_radius('button'))
        painter.box(x+3,y+6,3,max(0.,height-12),(*theme.accent,.95),None,rad=1.5)
        painter.text(f'СОЗДАТЬ ОКНА  ·  {count}',x+width*.5,y+29,
                     viewport_skin.text_px('title'),viewport_skin.control_ink(theme,part='button',state='active',role='primary'),
                     width=width-30,align='center')
        painter.text('DWOS · из найденных проёмов',x+width*.5,y+10,
                     viewport_skin.text_px('caption'),viewport_skin.ink(theme,kind='muted'),
                     width=width-30,align='center')
    finally:painter.close()


def _draw_tool_hud():
    """Single render pass for *all* vector controls, never reads or writes a Mesh."""
    ctx = bpy.context
    if not ctx.area or not ctx.region or ctx.region.type != 'WINDOW':
        return
    editor = ctx.area.type
    groups = []
    scale = _pref(ctx, 'button_scale', 1.0)
    now = time.monotonic()

    def group(name, icons, positions, active_indices=()):
        if not positions:
            return
        try: area_id = ctx.area.as_pointer(); region_id = ctx.region.as_pointer()
        except (ReferenceError, RuntimeError): return
        buttons = []
        for index, (xy, icon) in enumerate(zip(positions, icons)):
            key = (area_id, region_id, icon)
            # A lightweight hover impulse driven by native Gizmo hit testing.
            age = now - _TOOL_HOVER.get(key, -100)
            hovered = max(0.0, min(1.0, 1.0 - age / .32))
            buttons.append((xy[0], xy[1], 34.0*scale, icon,
                            index in active_indices, hovered))
        groups.append({'key': name, 'buttons': buttons})

    try:
        if editor == 'VIEW_3D':
            positions = _viewport_positions(ctx, _dock_count(ctx))
            group('editor', ('UV_DATA','NODE_MATERIAL'), positions['editor'])
            group('view', VIEW_DOCK_ICONS, positions['view'],
                  ((0,) if view_tools.ucx_active() else ()))
            if _dock_count(ctx):
                icons = ('AUTOMERGE_ON','NORMALS_FACE','MOD_WIREFRAME',
                         'MOD_SMOOTH','UV_SYNC_SELECT','LINKED')
                active = tuple(i for i, yes in enumerate((False,False,
                    view_tools.borders_active(ctx),
                    view_tools.issues_active() and view_tools.issue_kind()=='STRETCH',
                    view_tools.issues_active() and view_tools.issue_kind()=='FLIPPED',
                    modeling_tools.linked_active())) if yes)
                group('edit', icons, positions['edit'], active)
        elif editor == 'IMAGE_EDITOR' and ctx.mode == 'EDIT_MESH':
            group('uv', ('PIVOT_BOUNDBOX','UV_DATA','MOD_UVPROJECT'),
                  _strip_positions(ctx, 3))
        elif editor == 'NODE_EDITOR' and getattr(ctx.space_data,'tree_type','')=='ShaderNodeTree':
            group('shader', ('TRASH','NODE_MATERIAL'), _strip_positions(ctx, 2))
        if groups:
            tool_hud_draw.render(groups, _accent(ctx), now=now)
        if editor == 'VIEW_3D':
            _draw_context_notice(ctx)
            _draw_dwos_create_action(ctx)
    except Exception as exc:
        _hud_fault_once(exc)


def _ui_redraw_timer():
    """Render only while animated. No perpetual heavy-scene redraw."""
    now=time.monotonic()
    recent_hover=(any(now-t<.90 for t in _TOOL_HOVER.values()) or
                  any(now-t<.90 for _,t in _NAV_HOVER.values()))
    if _ACTIVE_RADIAL or recent_hover:
        try:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type in {'VIEW_3D','IMAGE_EDITOR','NODE_EDITOR'}:
                        area.tag_redraw()
        except (RuntimeError,ReferenceError,AttributeError):pass
    if _TOOL_HOVER:
        for key,t in list(_TOOL_HOVER.items()):
            if now-t>2.0:_TOOL_HOVER.pop(key,None)
    for key,(_,stamp) in list(_NAV_HOVER.items()):
        if now-stamp>2.0:_NAV_HOVER.pop(key,None)
    # Stop the timer completely once no active hover animation remains.
    return .055 if _ACTIVE_RADIAL or recent_hover else None


def _issue_context_watch():
    """Close stale UI sessions when active object/mode changes; no Mesh writes."""
    try:
        context = bpy.context
        if weld_preview.active() and not weld_preview.context_matches(context):
            weld_preview.clear('automatic context change')
        if view_tools.issues_active() and not view_tools.issues_context_matches(context):
            view_tools.discard_stale_issues(context)
        if weld_preview.active():
            # Redraw also keeps the subtle marker pulse alive.
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError) as exc:
        pass
    return .2


CLASSES = (
    IMA_AddonPreferences, IMA_PT_VisualSettings,
    appearance_studio.IMA_OT_AppearanceStudio, appearance_studio.IMA_OT_AppearanceAdvanced,
    IMA_OT_WindowVisibility, IMA_OT_WindowVisibilityToggle, IMA_OT_WindowVisibilityRestore,
    IMA_OT_ToggleUVFeedback, IMA_OT_Toggle3DFeedback,
    IMA_MT_ViewModes, IMA_OT_ViewModePie, IMA_OT_ToolRadial, IMA_OT_NoticeEscWatcher, IMA_OT_ClearNotice, IMA_OT_LaunchTool, IMA_OT_AuditWallSeams, IMA_OT_CollisionVisibility, IMA_OT_DWOSCreateWindows, IMA_OT_DWOSCreateSession, IMA_OT_DWOSExcludeSuspicious,
    IMA_OT_UVAnalysisAction, IMA_OT_SwitchEditor, IMA_OT_GeometryQualityPopup,
    IMA_GT_NavButton, IMA_GT_ToolButton, IMA_GT_DWOSCreateButton, IMA_GGT_Viewport, IMA_GGT_DWOSCreate, IMA_GGT_EditTools, IMA_GGT_IssueNavigator, IMA_GGT_EditorSplit,
    IMA_GGT_UV, IMA_GGT_UVIssueNavigator, IMA_GGT_Shader,
    # Header buttons replace unreliable image/node Gizmo groups.
)


def register():
    global _NAV_DRAW_HANDLER, _UV_NAV_DRAW_HANDLER, _TOOL_DRAW_HANDLER, _UV_TOOL_DRAW_HANDLER, _SHADER_TOOL_DRAW_HANDLER, _HUD_DRAW_ERROR
    _HUD_DRAW_ERROR = None
    for prop in ('ima_suite_tab', 'ima_openings_tab', 'ima_collision_tab', 'ima_wall_tab', 'ima_dwos_tab',
                  'ima_notice_message','ima_notice_tool','ima_notice_kind'):
        if hasattr(bpy.types.Scene, prop):
            raise RuntimeError('ISSMAKER: конфликт Scene.' + prop)
    runtime.register_classes_atomic(CLASSES)
    try:
        bpy.types.Scene.ima_suite_tab = bpy.props.EnumProperty(
            name='Раздел', items=(("ASSISTANT","Ассистент","Существующие инструменты"),
                                 ("OPENINGS","Проёмы","Brick Hole"),
                                 ("COLLISION","Коллизии","Collision Inspector"),
                                 ("UV_WALL","Стены","UV Wall Professional"),
                                 ("DWOS","Окна","DWOS Window Core"),
                                 ("SETTINGS","Настройки","Внешний вид"),
                                 ("DIAGNOSTICS","Диагностика","Журналы и диагностика")),
            default='OPENINGS')
        bpy.types.Scene.ima_openings_tab = bpy.props.EnumProperty(
            name='Проёмы', items=(("MAIN","Основное","Viewport Preview"),
                                  ("GEOMETRY","UV","Материалы и UV"),
                                  ("APPEARANCE","Вид","Overlay и HUD"),
                                  ("TESTS","Тесты","Валидация и диагностика")),
            default='MAIN')
        bpy.types.Scene.ima_collision_tab = bpy.props.EnumProperty(
            name='Коллизии', items=(('MAIN','Основное','Проверка и Viewport'),
                ('ISSUES','Проблемы','Навигация по результатам'),
                ('VIEW','Вид','Маркеры, overlay'),
                ('REPAIR','Исправление','Безопасные исправления'),
                ('SETTINGS','Параметры','Параметры анализа'),
                ('EXAMPLES','Тесты 55','Галерея тестов'),
                ('LOG','Логи','Журнал и диагностика')),
            default='MAIN')

        bpy.types.Scene.ima_wall_tab = bpy.props.EnumProperty(
            name='Стены', items=(('MAIN','Основное','Viewport Inspector'),
                ('PARAMETERS','Параметры','Модули и геометрия'),
                ('VIEW','Интерфейс','Studio Dock / Floating Pro'),
                ('LOG','Диагностика','Логи и ZIP')), default='MAIN')

        bpy.types.Scene.ima_dwos_tab = bpy.props.EnumProperty(name='Окна',items=(
            ('MAIN','Основное','Поиск и запуск'),('STUDIO','DWOS Studio','Все оригинальные настройки'),
            ('DIAG','Диагностика','Логи и проверки')),default='MAIN')
        bpy.types.Scene.ima_notice_message = bpy.props.StringProperty(name='ISSMAKER Notice',default='',options={'SKIP_SAVE'})
        bpy.types.Scene.ima_notice_tool = bpy.props.StringProperty(name='ISSMAKER Tool',default='',options={'SKIP_SAVE'})
        bpy.types.Scene.ima_notice_kind = bpy.props.EnumProperty(name='Notice Type',items=(
            ('WARNING','Предупреждение',''),('INFO','Информация',''),('ERROR','Ошибка','')),default='INFO',options={'SKIP_SAVE'})

        bpy.types.IMAGE_HT_header.append(draw_uv_header)
        bpy.types.IMAGE_HT_header.prepend(draw_uv_switch_header)
        bpy.types.NODE_HT_header.prepend(draw_shader_switch_header)
        if not bpy.app.background:
            _NAV_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                _draw_issue_nav_counter, (), 'WINDOW', 'POST_PIXEL')
            _UV_NAV_DRAW_HANDLER = bpy.types.SpaceImageEditor.draw_handler_add(
                _draw_issue_nav_counter, (), 'WINDOW', 'POST_PIXEL')
            _TOOL_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                _draw_tool_hud, (), 'WINDOW', 'POST_PIXEL')
            _UV_TOOL_DRAW_HANDLER = bpy.types.SpaceImageEditor.draw_handler_add(
                _draw_tool_hud, (), 'WINDOW', 'POST_PIXEL')
            _SHADER_TOOL_DRAW_HANDLER = bpy.types.SpaceNodeEditor.draw_handler_add(
                _draw_tool_hud, (), 'WINDOW', 'POST_PIXEL')
            if not bpy.app.timers.is_registered(_issue_context_watch):
                bpy.app.timers.register(_issue_context_watch, first_interval=.2)
            if not bpy.app.timers.is_registered(_appearance_motion_tick):
                bpy.app.timers.register(_appearance_motion_tick,first_interval=.25)
    except Exception:
        unregister()
        raise


def unregister():
    global _NAV_DRAW_HANDLER, _UV_NAV_DRAW_HANDLER, _TOOL_DRAW_HANDLER, _UV_TOOL_DRAW_HANDLER, _SHADER_TOOL_DRAW_HANDLER
    for session in tuple(_ACTIVE_RADIAL):
        try:session._cleanup(bpy.context)
        except Exception:pass
    _ACTIVE_RADIAL.clear()
    for notice_watcher in tuple(_NOTICE_ESC_WATCHERS):
        notice_watcher._done=True
    _NOTICE_ESC_WATCHERS.clear()
    appearance_studio.close_all()
    window_visibility.reset()
    _HUD_HIT_PROXY.clear()
    _TOOL_HOVER.clear()
    _NAV_HOVER.clear()
    tool_hud_draw.reset()
    nav_hud_draw.reset()
    if bpy.app.timers.is_registered(_ui_redraw_timer):
        bpy.app.timers.unregister(_ui_redraw_timer)
    if bpy.app.timers.is_registered(_issue_context_watch):
        bpy.app.timers.unregister(_issue_context_watch)
    if bpy.app.timers.is_registered(_appearance_motion_tick):
        bpy.app.timers.unregister(_appearance_motion_tick)
    if _NAV_DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_NAV_DRAW_HANDLER, 'WINDOW')
        except (RuntimeError, ValueError, ReferenceError):
            pass
        _NAV_DRAW_HANDLER = None
    if _UV_NAV_DRAW_HANDLER is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(_UV_NAV_DRAW_HANDLER, 'WINDOW')
        except (RuntimeError, ValueError, ReferenceError):
            pass
        _UV_NAV_DRAW_HANDLER = None
    for header, function in ((bpy.types.IMAGE_HT_header, draw_uv_header),
                             (bpy.types.IMAGE_HT_header, draw_uv_switch_header),
                             (bpy.types.NODE_HT_header, draw_shader_switch_header)):
        try: header.remove(function)
        except (RuntimeError, ValueError): pass
    for space,handler_name in ((bpy.types.SpaceView3D,'_TOOL_DRAW_HANDLER'),
                               (bpy.types.SpaceImageEditor,'_UV_TOOL_DRAW_HANDLER'),
                               (bpy.types.SpaceNodeEditor,'_SHADER_TOOL_DRAW_HANDLER')):
        handler=globals()[handler_name]
        if handler is not None:
            try:space.draw_handler_remove(handler,'WINDOW')
            except (RuntimeError,ValueError,ReferenceError):pass
            globals()[handler_name]=None
    for prop in ('ima_notice_kind','ima_notice_tool','ima_notice_message',
                 'ima_dwos_tab','ima_wall_tab','ima_collision_tab','ima_openings_tab','ima_suite_tab'):
        if hasattr(bpy.types.Scene,prop):
            delattr(bpy.types.Scene,prop)
    runtime.unregister_classes_safe(CLASSES)
