import math
import bpy
import json
import weakref
from time import perf_counter
from itertools import islice
from bpy.app.handlers import persistent
import blf
import gpu
from bpy.types import Operator
from issmaker_modeling_assistant.core.ui_layout_scaling import from_preferences as _base_ui_profile, record_layout as _record_ui_layout
from issmaker_modeling_assistant.core.window_manager import place_unobstructed


def _ui_profile(prefs):
    """Same adaptive scale for UV visible rectangles and their click targets."""
    return _base_ui_profile(prefs,context=bpy.context)

from issmaker_modeling_assistant.core.overlay_placement import StrictOverlayAllocator
from issmaker_modeling_assistant.core import window_visibility
from issmaker_modeling_assistant.core import universal_hud as _universal
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from ..core.definitions import (
    analyze_path, definition_for, initialize_from_scene, sync_scene_from_definition,
    total_wall_height, set_total_wall_height, ensure_source_uid, path_for_generated,
    snapshot_definition, restore_definition, has_committed_result,
    serialize_definition, apply_serialized_definition, path_from_source_uid, runtime_schema_status, ensure_runtime_schema,
    assign_virtual_asset_by_resolution, virtual_asset_for_module, sync_definition_from_scene,
)
from ..preview.engine import LivePreviewEngine, Dirty
from ..core.semantic_geometry import role_counts, face_role_codes, FACE_ROLE_CODES
from ..core.performance import SURFACE_GEOMETRY_CACHE, clear_performance_caches, PERFORMANCE_REGISTRY
from ..core.preview_budget import policy as preview_budget
from ..core.result_lifecycle import shutdown_result_lifecycle
from ..core.revisions import describe_revision
from ..core.problem_registry import active_problem, active_problem_count
from ..core.material_intelligence import inspect_material_asset
from ..core.build_guard import reset_build_guard
from ..core.surface_points import original_height_for_point, validate_height_edits
from .cornice_direct import corner_patch, sharp_indices, corner_popup_position, method_for_uid, method_patch, method_remove, MODES, MODE_IDS
from .surface_ui import draw_surface_annotations
from .ghost_geometry import (central_segment, surface_rotation_ghost, cornice_full_path_ghost, place_info_popup)
from .spatial_layout import layout_cards, interior_xy, stable_segment, camera_segment, floating_quick_rect, choose_semantic_anchor, semantic_edge_samples
from .spatial_layout import card_attention, tether_distance
from .parameter_windows import EditorStack, changed_patch, draft_from_definition, FIELDS, visible_fields
from .workspace_layout import WorkspaceLayoutManager, normalized_ui_scale
from .studio_dock_layout import (dock_rect, dock_row_visibility, visible_dock_rows,
                                 content_dock_rect, composed_rows, smoothing_step)
from ..ui.dock_workspace_state import read_map, write_map, safe_strings
from ..ui.preferences import active_ui_mode, get_preferences
from .resource_picker import (
    SmartResourcePicker, LABELS as RESOURCE_LABELS, assign_resource,
    current_resource, filter_rows, resources_for, validate_resource,
)
from ..ui.numeric_input import format_value
from ..operators.build_context import execute_build, object_mode_error
from ..ui.settings import sync_corner_uids, read_corner_uid_indices, validated_cornice_corner_points


# UV Wall 1.5.9 compatibility marker retained for legacy regression contracts.
MODE_INFO = (
    ("WALL", "WALL", "Стена"),
    ("SURFACE", "SURFACE", "Поверхность"),
    ("CORNICE", "CORNICE", "Карниз"),
)
HOVER_PALETTE_PRESETS=((1.0,.20,.17),(.98,.58,.16),(.20,.72,1.0),(.68,.38,1.0),(.20,.90,.62),(.93,.93,.96))
_ACTIVE_SESSIONS = weakref.WeakSet()


def ui_layout_snapshot():
    """Diagnostic map of real UI rectangles, ownership and reserved Blender chrome."""
    data={"sessions":[], "source":"UV Wall actual runtime; chrome exclusions are conservative estimates"}
    for session in tuple(_ACTIVE_SESSIONS):
        try:
            stack=session._window_stack
            size=(tuple(getattr(session,"_dock_region_size",(0,0)))
                  if getattr(session,"_ui_mode",None)=="STUDIO_DOCK"
                  else tuple(getattr(stack,"region_size",(0,0))) if stack is not None else (0,0))
            data["sessions"].append({
                "ui_mode":getattr(session,"_ui_mode","UNKNOWN"),
                "area_ptr":int(session._area_ptr),
                "region_ptr":int(session._region_ptr),
                "region_size":list(size),
                "chrome_obstacles":[list(r) for r in blender_chrome_obstacles(*size)] if all(size) else [],
                "floating_windows":[{
                    "kind":w.kind,"rect":list(w.rect()),
                    "pinned":bool(w.pinned),"collapsed":bool(w.collapsed),
                    "dirty_fields":int(w.dirty_count),"scroll":int(w.scroll),
                    "visible_in_active_mode":getattr(session,"_ui_mode","")=="FLOATING_PRO",
                } for w in getattr(stack,"windows",())] if stack is not None else [],
                "active_picker":str(getattr(session._resource_picker,"open_kind","") or ""),
                "picker_layout":getattr(session,"_picker_layout_snapshot",None),
                "picker_rect":list(session._picker_rect) if getattr(session,"_picker_rect",None) else None,
                "warning_rect":list(session._missing_resources_rect) if getattr(session,"_missing_resources_rect",None) else None,
                "problem_rect":list(session._problem_rect) if getattr(session,"_problem_rect",None) else None,
                "workspace_rect":list(session._workspace_rect) if session._workspace_rect else None,
                "studio_dock_rect":list(session._dock_rectangle) if getattr(session,"_dock_rectangle",None) else None,
                "studio_dock_scroll":int(getattr(session,"_dock_scroll",0)),
                "studio_dock_collapsed":bool(getattr(session,"_dock_collapsed",False)),
                "studio_dock_scroll_max":int(getattr(session,"_dock_scroll_max",0)),
                "studio_dock_list_bounds":list(session._dock_list_bounds) if getattr(session,"_dock_list_bounds",None) else None,
                "dock_drawn_region_size":list(session._dock_drawn_region_size) if getattr(session,"_dock_drawn_region_size",None) else None,
                "dock_visible_rows":len(getattr(session,"_dock_active_rows",()) or ()),
                "adaptive_editor_group_rect":list(session._window_stack.group_rect) if getattr(session,"_window_stack",None) and session._window_stack.group_rect else None,
                "adaptive_editor_group_count":int(getattr(getattr(session,"_window_stack",None),"group_count",0)),
                "workspace_anchor":list(session._window_stack.workspace_anchor) if getattr(session,"_window_stack",None) and session._window_stack.workspace_anchor else None,
                "viewport_focus_avoidance":str(getattr(getattr(session,"_window_stack",None),"last_focus_status","OFF")),
                "contextual_resource_buttons":{
                    w.kind:[label for label,_field in w.resource_actions]
                    for w in getattr(stack,"windows",())
                } if stack is not None else {},
                "compact_editor_rows":{
                    w.kind:bool(w.compact_rows)
                    for w in getattr(stack,"windows",())
                } if stack is not None else {},
                "dock_collapsed_sections":list(getattr(session,"_dock_collapsed_sections",()) or ()),
                "dock_favorites":list(getattr(session,"_dock_favorites",()) or ()),
                "dock_sizing_mode":str(getattr(get_preferences(),"dock_sizing","AUTO")),
                "dock_theme":str(getattr(get_preferences(),"ui_scheme","OBSIDIAN")),
                "dock_animation":str(getattr(get_preferences(),"ui_motion_quality","OFF")),
                "dock_preset_names_by_mode":{
                    mode:sorted(group)[:32]
                    for mode,group in read_map(getattr(get_preferences(),"dock_presets_json","{}") if get_preferences() else '{}').items()
                    if mode in {'WALL','SURFACE','CORNICE'} and isinstance(group,dict)
                },
                "display_rect":(list(display_panel_rect(*size))
                    if (getattr(session,"_ui_mode",None)=="FLOATING_PRO"
                        and all(size) and getattr(getattr(bpy.context,'window_manager',None),'uvwall_display_open',False))
                    else None),
                "last_draw_failure":str(getattr(session,"_draw_failure","") or ""),
            })
        except (AttributeError,ReferenceError,RuntimeError,TypeError,ValueError):
            continue
    return data


def lifecycle_snapshot():
    """JSON-safe runtime ownership snapshot for stress tests/support reports."""
    sessions=[]
    for session in tuple(_ACTIVE_SESSIONS):
        try:
            sessions.append({
                "area":int(session._area_ptr),"region":int(session._region_ptr),
                "pixel_handler":session._pixel_handle is not None,
                "view_handler":session._view_handle is not None,
                "timer":getattr(session,"_settle_timer",None) is not None,
                "draw_failed":bool(getattr(session,"_draw_failure","")),
            })
        except Exception:
            continue
    return {"active_sessions":len(sessions),"sessions":sessions}


def _remove_stale_handler(handlers, name):
    """Remove callbacks from older in-session module objects by module/name."""
    for fn in tuple(handlers):
        if (getattr(fn, "__module__", None) == __name__
                and getattr(fn, "__name__", None) == name):
            try:
                handlers.remove(fn)
            except Exception:
                pass


@persistent
def _close_sessions_before_load(_unused):
    for session in tuple(_ACTIVE_SESSIONS):
        try:
            session._finish(bpy.context)
        except Exception:
            # WeakSet prevents an unreachable modal instance from becoming a
            # permanent strong-reference leak after Area/Window destruction.
            _ACTIVE_SESSIONS.discard(session)


@persistent
def _reset_runtime_after_load(_unused):
    """File load is a hard runtime boundary: no cache/lock may cross it."""
    try:
        shutdown_result_lifecycle()
    except Exception:
        pass
    try:
        clear_performance_caches()
    except Exception:
        pass
    try:
        reset_build_guard(bpy.context)
    except Exception:
        pass
    wm=getattr(bpy.context, "window_manager", None)
    if wm is not None:
        for name,value in (("uvwall_viewport_active",False),
                           ("uvwall_viewport_commit_active",False),
                           ("uvwall_build_transaction_active",False),
                           ("uvwall_pending_definition_json",""),
                           ("uvwall_pending_surface_edits_json","")):
            try:
                setattr(wm,name,value)
            except Exception:
                pass


LENS_ITEMS = (
    ("BUILD", "Build", "Построение и direct manipulation"),
    ("LOOK", "Look", "Материал и UV reference"),
    ("OPT", "Opt", "Realtime/performance state"),
    ("INSPECT", "Inspect", "Пространственная диагностика"),
)

VISUAL_EFFECT_ITEMS = (
    ("GRAPHITE", "Graphite Depth", "Слоистый тёмный графит — основной production стиль"),
    ("AURORA", "Aurora Edge", "Очень слабое живое свечение активного окна"),
    ("TOPO", "Topo Flow", "Едва заметные топографические линии"),
    ("BLUEPRINT", "Blueprint Grid", "Техническая CAD-сетка на окнах"),
    ("CLEAN", "Clean", "Минимальный плоский режим без фоновых эффектов"),
)

PREVIEW_MODE_ITEMS = (
    ("MATERIAL", "Material", "Материальный production preview"),
    ("SHADED", "Shaded", "Полупрозрачная поверхность и контур"),
    ("WIREFRAME", "Wireframe", "Только рёбра preview"),
    ("TOPOLOGY", "Topology", "Рёбра и опорные точки"),
    ("NORMALS", "Normals", "Направление нормалей локальных preview-граней"),
    ("UV", "UV", "Технический UV tile preview без production Mesh"),
)


# Centralised color tokens, shared by Studio Dock and Floating Pro.
from issmaker_modeling_assistant.core import design_tokens as _iss_style
from issmaker_modeling_assistant.core.workspace_components import component_visual as _workspace_visual
from issmaker_modeling_assistant.core import viewport_skin as _viewport_skin
# Pass 11A: one theme source for every tool, not a local UV Wall palette.
from issmaker_modeling_assistant.core import theme_engine as _iss_theme
from issmaker_modeling_assistant.core import precision_markers as _precision
from issmaker_modeling_assistant.core.precision_feedback import precision_cue, place_cue
from issmaker_modeling_assistant.core import interaction_clarity as _clarity


def _ui_theme(context=None):
    return _iss_theme.from_preferences(get_preferences(context)).uv_palette()


def _theme_mix(base,accent,factor=.25):
    return tuple(base[i]*(1-factor)+accent[i]*factor for i in range(3))+(base[3],)

def _visual_palette(effect):
    effect=(effect or "GRAPHITE").upper()
    base = {
        "GRAPHITE": ((.025,.033,.041,.975),(.19,.28,.30,.98),(.35,.94,.79,1)),
        "AURORA": ((.019,.033,.043,.975),(.18,.37,.35,.98),(.36,.96,.78,1)),
        "TOPO": ((.027,.037,.039,.975),(.18,.30,.29,.98),(.38,.86,.71,1)),
        "BLUEPRINT": ((.022,.038,.057,.975),(.20,.40,.58,.98),(.47,.81,1.0,1)),
        "CLEAN": ((.034,.038,.042,.985),(.15,.19,.20,.98),(.60,.86,.77,1)),
    }.get(effect,((.025,.033,.041,.975),(.19,.28,.30,.98),(.35,.94,.79,1)))
    theme=_ui_theme()
    # Background effect and user theme are orthogonal: preserve Aurora/Topo
    # choices while applying the shared hue and border identities.
    return (_theme_mix(base[0],theme['panel'],.48),
            _theme_mix(base[1],theme['border'],.62),theme['accent'])


def _result_state(definition):
    """Compact live status without inspecting/creating production geometry."""
    if definition is None:
        return "NO DEF", (.60,.66,.68,1)
    has_result=bool(getattr(definition,"last_result_name", ""))
    last_error=str(getattr(definition,"last_error", "") or "")
    revision=int(getattr(definition,"revision",0))
    committed=int(getattr(definition,"last_committed_revision",-1))
    if last_error:
        return ("BLOCKED · STABLE" if has_result else "BLOCKED"), (1.0,.43,.35,1)
    if not has_result:
        return "READY", (.62,.72,.76,1)
    if revision != committed:
        return "DIRTY", (1.0,.70,.28,1)
    return "BUILT", (.35,.94,.79,1)


def hover_pulse(t):
    """Bounded red attention alpha: stable even without a functioning timer."""
    return 0.58 + 0.30 * (0.5 + 0.5 * math.sin(float(t) * 6.0))


def screen_safe_anchor(position, region_size, fallback, margin=23.):
    """Guarantee that a semantic control remains clickable after side changes.

    The rendered source position is authoritative when visible; a small edge
    proxy is used when Blender projection lies outside the current region.
    """
    width, height = map(float, region_size)
    if width <= 2*margin or height <= 2*margin:
        return None
    candidate = position if position is not None else fallback
    if candidate is None:
        candidate = (width * .5, height * .5)
    if not all(math.isfinite(float(v)) for v in candidate):
        candidate = (width * .5, height * .5)
    x, y = candidate
    return (max(margin, min(width-margin, float(x))),
            max(margin, min(height-margin, float(y))))


def semantic_hover_roles(key, mode="WALL"):
    """Identify the generated surface changed by a hovered parameter.

    This semantic relation is also shared with pure regression tests. The
    viewport overlay never edits Blender meshes, UVs or material assignments.
    """
    key=str(key or "")
    mode=str(mode or "").upper()
    if key.startswith('FIELD:'):
        name=key[6:]
        if mode=='WALL':
            if name in {'create_top_cap','cap_material_override','cap_material'}:
                return ('TOP_CAP',)
            if name in {'create_bottom_cap','bottom_cap_material_override','bottom_cap_material'}:
                return ('BOTTOM_CAP',)
            if name == 'cap_rotation':
                return ('TOP_CAP','BOTTOM_CAP')
            if name in {'close_ends','close_start_end','close_finish_end'}:
                return ('END_CAP',)
            if name in {'create_inner','inset_distance','inset_side','wall_alignment'}:
                return ('WALL_INNER','TOP_CAP')
            if name in {'height','story_direction','wall_offset','wall_module_rotation',
                        'wall_uv_origin_mode','module_standard'}:
                return ('WALL_OUTER','WALL_INNER')
        if mode=='SURFACE':
            return ('SURFACE',)
        if mode=='CORNICE':
            return ('CORNICE',)
    if mode=="WALL":
        if key in {"W_CAP", "W_TOP"}:
            return ("TOP_CAP","BOTTOM_CAP")
        if key.startswith(("FIELD:create_top_cap","FIELD:cap_material")):
            return ("TOP_CAP",)
        if key.startswith(("FIELD:create_bottom_cap","FIELD:bottom_cap_material")):
            return ("BOTTOM_CAP",)
        if key in {"W_THICKNESS","THICKNESS","W_PLANT"} or key.startswith("FIELD:inset_distance"):
            return ("WALL_INNER", "TOP_CAP")
        if key in {"W_WALL", "OFFSET", "W_SIDE", "W_DIR", "HEIGHT", "W_HEIGHT", "BOTTOM_LEVEL"}:
            return ("WALL_OUTER", "WALL_INNER")
        if key.startswith("PATH_Z:"):
            return ("WALL_OUTER", "WALL_INNER")
    if mode=="SURFACE" and (key.startswith("SURFACE_Z:") or key.startswith("S:RESET:") or key in {"S_Z","S_ROT","S_SETTINGS"}):
        return ("SURFACE",)
    if mode=="CORNICE" and (key.startswith("C_") or key.startswith("CORNER:") or key.startswith("FIELD:cornice_")):
        return ("CORNICE",)
    return ()


def blender_chrome_obstacles(region_width, region_height, ui_scale=1.0):
    """Reserved Blender-owned pixels which UV Wall must never cover.

    These lanes intentionally do not depend on any UV Wall window state.  The
    upper-right rectangle protects the navigation gizmo and viewport buttons;
    the left strip protects Blender's toolbar.
    """
    rw=float(region_width); rh=float(region_height)
    try:scale=max(.75,min(2.,float(ui_scale)))
    except (TypeError,ValueError):scale=1.0
    return (
        (0.0,0.0,min(rw,54.*scale),rh),
        (max(0.0,rw-132.*scale),max(0.0,rh-286.*scale),rw,rh),
        (0.0,max(0.0,rh-38.*scale),rw,rh),
    )


def problem_inspector_layout(region_width, region_height, display_open=False, obstacles=(), ui_scale=1.0):
    """Fixed lower-left diagnostics lane, independent from editors/settings."""
    rw=float(region_width); rh=float(region_height)
    try:scale=max(.75,min(2.,float(ui_scale)))
    except (TypeError,ValueError):scale=1.0
    w=min(348.*scale,max(90.*scale,rw-74.*scale))
    x=min(max(58.*scale,4.),max(4.,rw-w-8.*scale))
    y=max(46.*scale,min(72.*scale,rh-172.*scale))
    return (x,y,w)


def display_panel_rect(region_width, region_height, ui_scale=1.0):
    """Palette flyout anchored to the main bottom workspace, not the gizmo.

    Its toggle is a labelled button in the central action rail. The settings
    flyout never lives in the old upper-left corner or over Blender navigation.
    """
    rw=float(region_width);rh=float(region_height)
    try:scale=max(.75,min(1.6,float(ui_scale)))
    except (TypeError,ValueError):scale=1.0
    w=min(252.*scale,max(120.*scale,rw-18.*scale))
    h=min(370.*scale,max(90.*scale,rh-170.*scale))
    rail_w=min(480.*scale,rw-24.*scale)
    rail_right=(rw+rail_w)*.5
    desired_x=rail_right+12.*scale
    x=desired_x if desired_x+w+10.*scale <=rw else max(8.*scale,(rw-w)*.5)
    if rw<640.*scale:
        safe_right=rw-137.*scale
        safe_left=58.*scale
        if safe_right-safe_left>=128.*scale:
            w=min(w,safe_right-safe_left)
            x=safe_left
    y=min(max(116.*scale, 144.*scale),max(8.*scale,rh-h-55.*scale))
    return (x,y,x+w,y+h)


def viewport_ui_scale(context):
    """Read Blender DPI once and feed the same value to every custom panel."""
    preferences=getattr(context,'preferences',None)
    system=getattr(preferences,'system',None)
    prefs=get_preferences(context)
    ui_profile=_ui_profile(prefs)
    # Blender system scale is already the native HUD baseline; user multiplier
    # is applied only once to both visible UI and the shared hit geometry.
    baseline=normalized_ui_scale(getattr(system,'ui_scale',1.0))
    return normalized_ui_scale(baseline*ui_profile.safe_window)


def viewport_tooltip(key, definition=None):
    """One semantic source of hover help for card, handle and toolbar labels."""
    if not key:
        return ""
    if str(key).startswith('CORNER:'):
        return 'Вершина карниза · нажми для выбора Плавный / Острый · изменение отменяется Ctrl+Z'
    if str(key).startswith('PATH_Z:'):
        return 'Z точки пути · Shift — шаг 5 см · Ctrl — шаг 5 мм · Tab — точное значение · соседние секции обновляются вместе'
    if str(key).startswith(('Z:RESET:', 'S:RESET:')):
        return 'Вернуть исходную Z этой точки · Ctrl+Z отменяет сброс'
    if str(key).startswith('RP:'):
        kind = str(key).split(':')[1] if ':' in str(key) else ''
        return {'PATH':'Выбрать путь из сцены', 'MATERIAL':'Назначить материал 2K/4K',
                                'PROFILE':'Указать геометрию профиля карниза'}.get(kind,'Выбор ресурса')
    return {
        'W_HEIGHT':'Высота стены от линии пути · открыть точный ввод или потянуть маркер',
        'HEIGHT':'Высота стены от линии пути · тяни вверх/вниз · Tab — точное значение',
        'BOTTOM_LEVEL':'Смещение нижнего уровня · потяни по вертикали',
        'OFFSET':'Положение · потяни маркер · Tab — точное значение',
        'THICKNESS':'Толщина второй стены · потяни по нормали',
        'W_CAP':'Крышки и торцы · верх/низ наследуют материал стены или имеют override',
        'W_THICKNESS':'Настроить одинарную/двойную конструкцию; толщина только для двойной',
        'W_WALL':'Нормали одинарной стены или положение двойной относительно Path',
        'W_PLANT':'Настройки внутренней поверхности',
        'S_Z':'Высота поверхности', 'S_ROT':'Поворот поверхности',
        'C_PROFILE':'Выбрать профиль карниза из сцены или каталога',
        'C_ROT':'Изменить только угол поворота профиля',
        'C_SIDE':'Развернуть сторону или отразить профиль',
        'CORNER_SMOOTH':'Сделать выбранный угол плавным · применить и сохранить Undo',
        'CORNER_SHARP':'Сделать выбранный угол острым · применить и сохранить Undo',
        'PROB_HIDE':'Скрыть подробную карточку, оставив индикатор ошибок',
        'PROB_SHOW':'Переместить вид камеры к месту ошибки, если известна точная позиция',
        'PROB_FIX':'Отметить указанную вершину карниза как Sharp и перестроить',
        'PROB_PREV':'Предыдущая проблема', 'PROB_NEXT':'Следующая проблема',
        'PROB_SHOW_PANEL':'Раскрыть подробную карточку найденной проблемы',
        'WS:HIDE':'Скрыть нижнюю панель, не выключая инструменты',
        'WS:SHOW':'Вернуть нижнюю панель',
        'WS:TOGGLE':'Открыть или закрыть назначения ресурсов',
        'BUILD':'Перестроить итоговую геометрию · R',
        'PROBLEMS_REFRESH':'Повторно проверить ошибки геометрии',
        'PREVIEW_REFRESH':'Обновить предпросмотр без изменения исходного пути',
        'MODE_WALL':'Стена: модульное построение по ребрам',
        'MODE_SURFACE':'Поверхность из замкнутого контура',
        'MODE_CORNICE':'Карниз вдоль пути из выбранного профиля',
    }.get(str(key), 'Нажмите, чтобы открыть действие или параметр')


def _rect_hit(x, y, rect):
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def _distance(a, b):
    return math.hypot(float(a[0]-b[0]), float(a[1]-b[1]))


def _text(x, y, text, size=12, color=(0.9, 0.94, 0.97, 1.0), align="LEFT"):
    font = 0
    prefs=get_preferences(bpy.context)
    font_px=max(7,int(round(float(size)*_ui_profile(prefs).font)))
    blf.size(font, font_px)
    try: blf.color(font, *color)
    except Exception: pass
    width, height = blf.dimensions(font, text)
    if align == "CENTER": x -= width * 0.5
    elif align == "RIGHT": x -= width
    from issmaker_modeling_assistant.core.hud_readability import snap as _snap_text
    blf.position(font, _snap_text(x), _snap_text(y), 0)
    blf.draw(font, text)
    return width, height


def _short_label(label, available_px, size=11):
    """Keep the interactive hitbox and visible label within the same card."""
    if available_px <= 0:
        return ""
    prefs=get_preferences(bpy.context)
    blf.size(0, max(7,int(round(float(size)*_ui_profile(prefs).font))))
    from issmaker_modeling_assistant.core.hud_readability import fit_caption
    return fit_caption(label,available_px,lambda sample:blf.dimensions(0,sample)[0])


def _draw_rect(x0, y0, x1, y1, color, border=None):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": [(x0,y0),(x1,y0),(x1,y1),(x0,y0),(x1,y1),(x0,y1)]})
    gpu.state.blend_set("ALPHA")
    shader.bind(); shader.uniform_float("color", color); batch.draw(shader)
    if border:
        # Purely decorative inward AA; keep old LINE_STRIP as GPU fallback.
        from issmaker_modeling_assistant.hud_chrome_gpu import draw_aa_rect
        if not draw_aa_rect(x0,y0,x1,y1,border):
            batch = batch_for_shader(shader, "LINE_STRIP", {"pos": [(x0,y0),(x1,y0),(x1,y1),(x0,y1),(x0,y0)]})
            shader.bind(); shader.uniform_float("color", border); gpu.state.line_width_set(1.0); batch.draw(shader)


def _draw_editor_rounded(x0, y0, x1, y1, color, border=None):
    """Single shared drawing code for wall editors and DWOS Composer."""
    from issmaker_modeling_assistant.core.studio_gpu_components import fill_rect,outline_rect
    if not fill_rect(x0,y0,x1,y1,color,radius=7.0):
        _draw_rect(x0,y0,x1,y1,color,border)
        return
    if border and not outline_rect(x0,y0,x1,y1,border,radius=7.0):
        _draw_rect(x0,y0,x1,y1,(0,0,0,0),border)


def _draw_circle(center, radius, color, width=2.0, fill=False):
    cx, cy = center
    steps = 32
    pts = [(cx + math.cos(i*math.tau/steps)*radius, cy + math.sin(i*math.tau/steps)*radius) for i in range(steps)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    if fill:
        tris=[]
        for i in range(steps):
            tris.extend([(cx,cy), pts[i], pts[(i+1)%steps]])
        batch=batch_for_shader(shader,"TRIS",{"pos":tris})
    else:
        batch=batch_for_shader(shader,"LINE_STRIP",{"pos":pts+[pts[0]]})
        gpu.state.line_width_set(width)
    shader.bind(); shader.uniform_float("color",color); batch.draw(shader)


class UVWALL_OT_viewport_inspector_exit(Operator):
    bl_idname = "uvwall.viewport_inspector_exit"
    bl_label = "Закрыть UV Wall Inspector"

    def execute(self, context):
        context.window_manager.uvwall_viewport_active = False
        return {"FINISHED"}


from issmaker_modeling_assistant.styled_panels import StyledPanel


class UVWALL_OT_finalize_result(StyledPanel, Operator):
    """Bake ownership only: keep existing Mesh, preserve authored Path.

    The next Build cannot reuse this frozen Result because the source ownership
    and Stable Result pointer are detached in one Blender UNDO operation.
    """
    bl_idname = "uvwall.finalize_result"
    bl_label = "Принять стену · сохранить путь"
    bl_description = "Зафиксировать результат отдельно и оставить Path для новой стены"
    bl_options = {"REGISTER", "UNDO"}

    smooth_angle: bpy.props.FloatProperty(
        name="Угол сглаживания", subtype='ANGLE', default=math.radians(35.0),
        min=0.0, max=math.pi)

    exclusive = True
    title = 'ISSMAKER  /  СГЛАЖИВАНИЕ'
    panel_height = 330

    def invoke(self, context, event):
        return self.open_panel(context)

    def execute(self, context):
        scene = getattr(context, 'scene', None)
        settings = getattr(scene, 'uv_wall_settings', None)
        path = getattr(settings, 'path_mesh', None)
        if path is None or path.type != 'MESH':
            self.report({'WARNING'}, 'Нет исходного Path: выберите контур стены')
            return {'CANCELLED'}
        d = definition_for(path)
        if d is None:
            self.report({'WARNING'}, 'Для Path нет сохранённой Definition')
            return {'CANCELLED'}
        from ..core.result_lifecycle import active_owned_results
        results = active_owned_results(path, d)
        if len(results) != 1 or results[0].type != 'MESH':
            self.report({'WARNING'}, 'Для принятия нужен ровно один готовый Result. Сначала постройте стену.')
            return {'CANCELLED'}
        result = results[0]
        if not len(result.data.polygons):
            self.report({'WARNING'}, 'Готовый Result пуст: нельзя фиксировать пустую стену')
            return {'CANCELLED'}
        from issmaker_modeling_assistant.geometry_shading import apply_angle_smoothing
        try:
            apply_angle_smoothing(context, result, self.smooth_angle)
        except Exception as exc:
            self.report({'ERROR'}, 'Сглаживание: ' + str(exc)[:180])
            return {'CANCELLED'}
        # Never change the Mesh or create an overlapping duplicate. Remove
        # *generator ownership*, not render materials, UV or physical geometry.
        for name in ('uvwall_owner_uid', 'uvwall_owner_path', 'path_object',
                     'uvwall_result_uid', 'uvwall_definition_revision',
                     'uvwall_definition_json', 'uvwall_generated'):
            if name in result:
                del result[name]
        result['uvwall_baked'] = True
        result['uvwall_detached'] = True
        result['issmaker_finalized_wall'] = True
        result['issmaker_source_path_name'] = path.name  # informative only
        result.name = 'ISSMAKER_Wall_Final'
        # Break last-result migration fallback; the saved Path Definition and
        # its authored vertices/edges remain untouched for the NEXT wall.
        d.last_result_name = ''
        d.last_committed_revision = -1
        try:
            context.window_manager.uvwall_viewport_active = False
        except (AttributeError, ReferenceError):
            pass
        try:
            from ..production.validation import log_event
            log_event('WALL_FINALIZED result=%s path=%s polygons=%d independent=True' %
                      (result.name, path.name, len(result.data.polygons)), 'LIFECYCLE')
        except (ImportError, RuntimeError, AttributeError):
            pass
        self.report({'INFO'}, 'Стена принята. Path сохранён; следующий Build создаст новый объект.')
        return {'FINISHED'}


class UVWALL_OT_viewport_inspector(Operator):
    """Viewport-first parametric editor for UV Wall source paths."""
    bl_idname = "uvwall.viewport_inspector"
    bl_label = "UV Wall Viewport Inspector"
    bl_description = "Параметрическое построение и редактирование прямо во Viewport"
    bl_options = {"REGISTER"}  # Non-blocking: native Blender history owns Ctrl+Z / Redo

    _pixel_handle = None
    _view_handle = None
    _area_ptr = 0
    _region_ptr = 0
    _path_name = ""
    _engine = None
    _buttons = None
    _handles = None
    _hover = None
    _drag_kind = None
    _drag_start_mouse = None
    _drag_start_value = None
    _drag_snapshot = None
    _drag_touched = False
    _drag_ppm = 100.0
    _drag_screen_axis = (0.0, 1.0)
    _numeric = False
    _numeric_buffer = ""
    _numeric_kind = None
    _radial = False
    _radial_center = None
    _radial_choice = None
    _last_mouse = (0.0, 0.0)
    _last_status = ""
    _source_uid = ""
    _path_ref = None
    _workspace_ref = None
    _drag_index = None
    _surface_drag_z = None
    _window_stack = None
    _hud_rect = (20, 20, 440, 172)
    _spatial_anchors = None
    _spatial_rects = None
    _spatial_previous = None
    _inside_analysis_ref = None
    _inside_cache_xy = None
    _composition_analysis_ref = None
    _composition_world_mids = None
    _smart_segment_key = None
    _quick_rect = None
    _history_generation = -1
    _resource_picker = None
    _picker_editor = None
    _corner_edit_active = False
    _corner_selected = -1
    _corner_selected_uid = 0
    _wall_point_selected = -1
    _surface_point_selected = -1
    _workspace_rect = None
    _workspace_details = False
    _workspace_visible = True
    _selection_mismatch = False
    _draw_failure = ""
    _workspace_undo = None
    _workspace_redo = None
    _history_replay = False
    _toast_text = ""
    _toast_level = "INFO"
    _toast_until = 0.0
    _last_path_fingerprint = None

    def _path(self):
        # Object rename must never detach an active viewport session.
        # Undo may invalidate a Python bpy Object wrapper: recover by UID.
        try:
            obj = self._path_ref
            if obj is not None and obj.name in bpy.data.objects and bpy.data.objects.get(obj.name) == obj:
                return obj if obj.type == "MESH" else None
        except ReferenceError:
            pass
        obj = path_from_source_uid(self._source_uid) if self._source_uid else None
        # Name fallback is valid only for a legacy session with no source UID.
        # After deletion/Undo a different mesh can inherit the same name.
        if obj is None and not self._source_uid:
            obj = bpy.data.objects.get(self._path_name)
        if obj is not None and obj.type == "MESH":
            self._path_ref = obj
            self._path_name = obj.name
            return obj
        return None

    def _definition(self):
        return definition_for(self._path())

    def _selection_path(self, context):
        active=getattr(context,"active_object",None)
        if active is None:return None
        owner=path_for_generated(active)
        if owner is not None:return owner
        try:
            definition=definition_for(active)
            if active.type=="MESH" and definition is not None and getattr(definition,"initialized",False):
                return active
        except (AttributeError,ReferenceError,RuntimeError,TypeError):
            pass
        return None

    def _sync_follow_selection(self,context,force=False):
        candidate=self._selection_path(context);current=self._path()
        self._selection_mismatch=bool(candidate is not None and candidate is not current)
        mode=getattr(context.window_manager,"uvwall_selection_mode","FOLLOW")
        if candidate is None or candidate is current or (mode!="FOLLOW" and not force):return False
        ok,_reason=self._rebind_path(context,candidate)
        self._selection_mismatch=not ok
        return ok

    def _region(self, area):
        if area is None: return None
        for region in area.regions:
            if region.type == "WINDOW" and region.as_pointer() == self._region_ptr:
                return region
        return next((r for r in area.regions if r.type == "WINDOW"), None)

    def _mouse_xy(self, context, event):
        """Window event coordinates -> actual 3D WINDOW region coordinates.

        Blender Region.x/y and Event.mouse_x/y both use window coordinates.
        Region offsets already include Area location, even in split workspaces.
        """
        region = self._region(context.area)
        if region is None: return float(event.mouse_region_x), float(event.mouse_region_y)
        return (float(event.mouse_x - region.x),
                float(event.mouse_y - region.y))

    def _screen(self, context, world):
        region = self._region(context.area)
        rv3d = getattr(context.space_data, "region_3d", None)
        if region is None or rv3d is None: return None
        p = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
        return None if p is None else (float(p.x), float(p.y))

    def _analysis(self):
        return self._engine.analysis if self._engine and self._engine.analysis else analyze_path(self._path())

    def _centroid(self):
        analysis = self._analysis()
        cache=getattr(self,"_centroid_cache",None)
        if cache is not None and cache[0] is analysis:
            return cache[1].copy()
        pts = [p for chain in analysis.get("simple_chains", []) for p in chain["points"][:-1 if chain["closed"] else None]]
        if not pts:
            path = self._path()
            if not path or not path.data.vertices: return Vector()
            pts = [path.matrix_world @ v.co for v in path.data.vertices]
        centroid=sum((p for p in pts), Vector()) / max(1, len(pts))
        self._centroid_cache=(analysis,centroid.copy())
        return centroid

    def _visual_object_center(self, definition, analysis=None):
        """Bounding-box XY center of the actual result (or authored path).

        Unlike vertex averaging, this is stable when one side of the Path is
        densely subdivided. Never mutate or rebuild a mesh in this method.
        """
        from .ghost_geometry import bbox_center
        result_name=str(getattr(definition, "last_result_name", "") or "")
        result=bpy.data.objects.get(result_name) if result_name else None
        if result is not None and getattr(result,"type",None)=="MESH":
            try:
                corners=[result.matrix_world @ Vector(corner) for corner in result.bound_box]
                if len(corners)==8:
                    center=bbox_center([tuple(v) for v in corners])
                    if center is not None:
                        return Vector(center)
            except (ReferenceError,TypeError,ValueError,AttributeError):
                pass
        analysis=analysis or self._analysis()
        coords=[p for chain in analysis.get("simple_chains",()) for p in chain.get("points",())]
        if coords:
            center=bbox_center([tuple(v) for v in coords])
            if center is not None:
                return Vector(center)
        return self._centroid()

    def _first_segment_basis(self):
        # Camera-aware *source edge* selection. Stable across mesh rebuilds and
        # cyclic shifts of the initial vertex. Lock while a handle is dragged.
        pair=None
        ctx=bpy.context
        rv3d=getattr(getattr(ctx,'space_data',None),'region_3d',None)
        region=self._region(getattr(ctx,'area',None))
        analysis=self._analysis()
        camera_key=(id(analysis),tuple(round(float(x),7) for row in rv3d.view_matrix for x in row),
                    bool(self._drag_kind),self._smart_segment_key,
                    (region.width,region.height)) if rv3d is not None and region is not None else None
        cached=getattr(self,"_basis_cache",None)
        if cached is not None and cached[0]==camera_key:
            return cached[1]
        if rv3d is not None and region is not None:
            try:
                camera=rv3d.view_matrix.inverted().translation
                picked=camera_segment(
                    analysis.get('simple_chains',()),
                    lambda world:self._screen(ctx,world),camera,
                    (region.width*.5,region.height*.5),
                    current_key=self._smart_segment_key,
                    lock=self._drag_kind is not None,
                    max_candidates=preview_budget(len(self._path().data.vertices))["marker_candidates"])
                if picked is not None:
                    self._smart_segment_key=picked[0]
                    pair=picked[1:3]
            except (RuntimeError,ValueError,TypeError):
                pass
        if pair is None:
            pair=stable_segment(analysis.get('simple_chains',()))
        if pair is None:
            self._basis_cache=(camera_key,(None,None,None))
            return None,None,None
        a,b=pair
        d=Vector((b.x-a.x,b.y-a.y,0.0))
        if d.length<1.e-8:return None,None,None
        d.normalize()
        result=(a.lerp(b,0.5),d,Vector((-d.y,d.x,0.0)))
        # Key after selection: selection may update its stable segment id.
        if camera_key is not None:
            self._basis_cache=((camera_key[0],camera_key[1],camera_key[2],
                                self._smart_segment_key,camera_key[4]),result)
        return result

    def _mode_available(self, mode):
        a = self._analysis()
        if not a.get("simple_chains"): return False
        if mode == "SURFACE": return bool(a.get("closed_components")) and not bool(a.get("intersections"))
        return True

    def invoke(self, context, _event):
        schema_ok, schema_message, schema_repaired = ensure_runtime_schema()
        if not schema_ok:
            self.report({"ERROR"}, "UV Wall не смог автоматически восстановить RNA: " + schema_message)
            return {"CANCELLED"}
        if schema_repaired:
            self.report({"INFO"}, "UV Wall: Path Definition RNA восстановлена автоматически")
        if getattr(context.window_manager, "uvwall_viewport_active", False):
            self.report({"INFO"}, "UV Wall Viewport Inspector уже активен")
            return {"CANCELLED"}
        if not context.area or context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "Viewport Inspector запускается из 3D View")
            return {"CANCELLED"}
        region = next((r for r in context.area.regions if r.type == "WINDOW"), None)
        if region is None or getattr(context.space_data, "region_3d", None) is None:
            self.report({"ERROR"}, "Не найдено рабочее окно 3D View")
            return {"CANCELLED"}
        active = context.active_object
        settings = context.scene.uv_wall_settings
        # Re-enter an accepted result through its *proven* authored Path.
        # Result selection must not be mistaken for a new polygon-only Path,
        # or adopt another object's copied/ambiguous Definition UID.
        path = active
        if active is not None and getattr(active, 'type', None) == 'MESH' and active.get('uvwall_generated'):
            owner = path_for_generated(active)
            owner_definition = definition_for(owner) if owner is not None else None
            if (owner is None or owner_definition is None or
                    not has_committed_result(owner_definition, owner) or
                    str(active.get('uvwall_owner_uid', '')) != str(owner_definition.source_uid) or
                    str(owner_definition.last_result_name) != str(active.name)):
                self.report({'ERROR'}, 'Невозможно доказать владельца принятой стены. Выберите исходный Path.')
                return {'CANCELLED'}
            path = owner
        valid_path = bool(path is not None and getattr(path,"type",None)=="MESH"
                          and getattr(path.data,"edges",None) and len(path.data.edges)>0
                          and len(getattr(path.data,"polygons",()))==0)
        if not valid_path:
            self.report({"ERROR"}, "UV Wall: выделите Mesh-путь из рёбер (без граней), затем откройте инспектор")
            return {"CANCELLED"}
        settings.path_mesh = path
        definition = initialize_from_scene(path, settings)
        if definition is None:
            self.report({"ERROR"}, "Не удалось создать Path Definition")
            return {"CANCELLED"}
        # New paths start with the clean virtual reference workflow. Legacy scene
        # assets are migrated only when their image resolution identifies the
        # correct 2K/4K module; never copy one shared asset blindly to both.
        legacy_material = getattr(settings, "reference_material", None)
        legacy_image = getattr(settings, "reference_image", None)
        if legacy_material is not None or legacy_image is not None:
            assign_virtual_asset_by_resolution(definition, legacy_material, legacy_image)
        definition.use_virtual_reference = True if getattr(settings, "viewport_prefer_virtual", True) else definition.use_virtual_reference

        self._path_name = path.name; self._path_ref = path; self._source_uid = ensure_source_uid(path, definition)
        self._workspace_ref = context.workspace
        self._ui_mode = active_ui_mode(context)
        self._area_ptr = context.area.as_pointer(); self._region_ptr = region.as_pointer()
        self._engine = LivePreviewEngine(); self._engine.bind(path, definition); self._engine.update()
        self._preview_batch_state = None
        self._basis_cache = None
        self._centroid_cache = None
        self._preview_batches_built = 0
        self._preview_batches_reused = 0
        self._buttons=[]; self._handles=[]; self._hover=None; self._drag_kind=None; self._drag_snapshot=None
        self._drag_index=None; self._surface_drag_z=None
        self._window_stack=EditorStack(); self._window_stack.set_source(self._source_uid)
        self._resource_picker=SmartResourcePicker()
        self._picker_rect=None
        self._picker_layout_snapshot=None
        self._missing_resources_rect=None
        self._problem_rect=None
        self._corner_popup_rect=None
        self._picker_editor=None
        self._selection_mismatch=False;self._draw_failure=""
        self._workspace_undo=[];self._workspace_redo=[];self._history_replay=False
        self._toast_text="";self._toast_level="INFO";self._toast_until=0.0
        self._last_path_fingerprint=self._path_fingerprint(path)
        self._corner_edit_active=True; self._corner_selected=-1; self._corner_selected_uid=0; self._wall_point_selected=-1; self._surface_point_selected=-1
        self._workspace_rect=None
        self._workspace_details=False; self._workspace_visible=True
        self._workspace_history_open=False
        self._dock_scroll=0;self._dock_collapsed=False;self._dock_rectangle=None
        self._dock_drawn_region_size=None;self._dock_list_bounds=None;self._dock_scroll_max=0
        self._dock_active_rows=();self._dock_collapsed_sections=();self._dock_favorites=()
        self._dock_height_previous=None
        self._spatial_anchors={}; self._spatial_rects={}; self._spatial_previous={}
        self._smart_segment_key=None; self._quick_rect=None
        self._inside_analysis_ref=None; self._inside_cache_xy=None
        self._composition_analysis_ref=None; self._composition_world_mids=None
        self._numeric=False; self._numeric_buffer=""; self._numeric_kind=None; self._inline_marker=None
        self._marker_choice=None; self._marker_choice_rect=None; self._marker_choice_rows=[]
        self._numeric_editor=None; self._spin_state=None; self._preview_visible=True
        self._precision_modifiers=(False,False)  # presentation only; no geometry or hitbox writes
        self._clean_preview=False;self._clean_button=None
        self._rotation_phase="";self._rotation_message=""
        self._radial=False; self._radial_center=None; self._radial_choice=None
        # Partial registration rollback: if the second draw handler or timer
        # fails, NEVER leave the first GPU handle active in Blender.
        self._pixel_handle = None
        self._view_handle = None
        self._settle_timer = None
        try:
            self._pixel_handle = bpy.types.SpaceView3D.draw_handler_add(self._safe_draw_pixel, (), "WINDOW", "POST_PIXEL")
            self._view_handle = bpy.types.SpaceView3D.draw_handler_add(self._safe_draw_view, (), "WINDOW", "POST_VIEW")
            # Low-frequency timer drives only SETTLED preview; no Mesh commits.
            self._settle_timer = context.window_manager.event_timer_add(0.12, window=context.window)
            context.window_manager.uvwall_viewport_active = True
            _ACTIVE_SESSIONS.add(self)
            if not context.window_manager.uvwall_viewport_lens:
                context.window_manager.uvwall_viewport_lens = "BUILD"
            context.window_manager.modal_handler_add(self)
            context.workspace.status_text_set("ISSMAKER 2.0 · Enter — принять Mesh · Ctrl+Z — история · Esc — закрыть")
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        except Exception as exc:
            # Draw/WindowManager APIs may fail during workspace close or reload.
            # This is our runtime setup failure, NOT invalid user geometry.
            try:
                self._finish(context)
            except Exception:
                pass
            self.report({"ERROR"}, f"UV Wall: не удалось открыть Inspector ({type(exc).__name__})")
            return {"CANCELLED"}

    def _finish(self, context):
        _ACTIVE_SESSIONS.discard(self)
        try:
            if self._window_stack is not None:self._window_stack.clear()
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        self._spatial_previous={};self._spatial_rects={};self._spatial_anchors={}
        self._inside_analysis_ref=None;self._inside_cache_xy=None
        if self._pixel_handle is not None:
            try: bpy.types.SpaceView3D.draw_handler_remove(self._pixel_handle, "WINDOW")
            except Exception: pass
            self._pixel_handle=None
        if self._view_handle is not None:
            try: bpy.types.SpaceView3D.draw_handler_remove(self._view_handle, "WINDOW")
            except Exception: pass
            self._view_handle=None
        settle_timer = getattr(self, "_settle_timer", None)
        if settle_timer is not None:
            try: context.window_manager.event_timer_remove(settle_timer)
            except Exception: pass
            self._settle_timer = None
        try: context.window_manager.uvwall_viewport_active=False
        except Exception: pass
        try:
            if self._workspace_ref is not None:
                self._workspace_ref.status_text_set(None)
        except (ReferenceError, RuntimeError, AttributeError):
            pass
        try: context.area.tag_redraw()
        except Exception: pass
        # Drop strong RNA/runtime references after handlers/timers are detached.
        # A finished modal session must never keep an Object/Workspace alive or
        # reuse a wrapper resurrected by Undo/Load.
        self._preview_batch_state=None
        self._basis_cache=None
        self._centroid_cache=None
        self._engine=None
        self._path_ref=None
        if self._resource_picker is not None:
            self._resource_picker.reset()
        self._resource_picker=None
        self._picker_editor=None
        self._corner_edit_active=False;self._corner_selected=-1
        self._workspace_rect=None
        self._workspace_ref=None
        self._window_stack=None
        self._source_uid=""

    def _record_draw_failure(self,kind,error):
        if self._draw_failure:return
        self._draw_failure=f"{kind}: {type(error).__name__}: {error}"
        # Preserve the traceback rather than only recording a NoneType error.
        # Missing resources must never silently leave a blank Viewport.
        import traceback
        try:
            from ..production.validation import log_event
            log_event(f"VIEWPORT_DRAW_DISABLED kind={kind}; reason={self._draw_failure}"
                      + "\n" + traceback.format_exc(), "ERROR")
        except Exception:
            pass

    def _draw_failure_banner(self):
        try:
            context=bpy.context
            if not context.area or context.area.as_pointer()!=self._area_ptr:return
            region=self._region(context.area)
            if region is None:return
            x=max(8., (float(region.width)-420.)*.5)
            y=max(16.,float(region.height)-140.)
            _draw_rect(x,y,x+min(420.,region.width-16.),y+74.,
                       (.055,.043,.038,.97),(.94,.47,.27,.98))
            _text(x+12,y+50,'Интерфейс: ошибка отображения',11,(1.,.79,.56,1.))
            _text(x+12,y+31,'Полный Traceback записан в диагностику.',9,(.94,.92,.88,1.))
            _text(x+12,y+12,'Esc — закрыть, затем открыть инструмент повторно.',9,(.88,.85,.82,1.))
        except Exception:
            pass

    def _cached_preview_batch(self, geo, name, primitive, shader, coords_fn):
        """Build a GPU batch once per immutable PreviewGeometry revision.

        Cache belongs to the modal inspector; no bpy data are retained.
        A changed geometry reference invalidates all old batches, including
        cached normals and cap geometry.
        """
        state=self._preview_batch_state
        if state is None or state[0] is not geo:
            state=(geo,{})
            self._preview_batch_state=state
        batch=state[1].get(name)
        if batch is not None:
            self._preview_batches_reused += 1
            return batch
        coords=coords_fn()
        if not coords:return None
        batch=batch_for_shader(shader,primitive,{"pos":coords})
        state[1][name]=batch
        self._preview_batches_built += 1
        return batch

    def _safe_draw_view(self):
        if self._draw_failure:return
        # Editing uses a BMesh that may replace the object-mode Mesh on exit.
        # Never dereference authored Mesh RNA from a draw callback in Edit Mode.
        if getattr(bpy.context, "mode", "") != "OBJECT":return
        started=perf_counter()
        try:self._draw_view()
        except Exception as error:
            self._preview_batch_state=None
            self._record_draw_failure("POST_VIEW",error)
        finally:PERFORMANCE_REGISTRY.add("HUD_DRAW_VIEW",perf_counter()-started)

    def _safe_draw_pixel(self):
        if self._draw_failure:
            self._draw_failure_banner()
            return
        if getattr(bpy.context, "mode", "") != "OBJECT":return
        started=perf_counter()
        try:self._draw_pixel()
        except Exception as error:self._record_draw_failure("POST_PIXEL",error)
        finally:PERFORMANCE_REGISTRY.add("HUD_DRAW_PIXEL",perf_counter()-started)

    def cancel(self, context):
        if self._drag_kind is not None: self._restore_drag()
        self._finish(context)

    def _draw_view(self):
        context=bpy.context
        if not context.area or context.area.as_pointer() != self._area_ptr: return
        path=self._path(); d=self._definition()
        if path is None or d is None or not self._engine: return
        if not getattr(self,"_preview_visible",True) or getattr(self,"_clean_preview",False): return
        try: geo=self._engine.update()
        except Exception: return
        shader=gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA"); gpu.state.depth_test_set("LESS_EQUAL")
        preview_mode=getattr(context.window_manager,"uvwall_preview_mode","MATERIAL")
        effect=getattr(context.window_manager,"uvwall_visual_effect","GRAPHITE")
        _bg,_border,accent=_visual_palette(effect)
        if geo.triangles and preview_mode not in {"WIREFRAME","TOPOLOGY","UV"}:
            batch=self._cached_preview_batch(geo,"WALL_TRIS","TRIS",shader,
                        lambda:[tuple(p) for tri in geo.triangles for p in tri])
            alpha=.34 if preview_mode=="MATERIAL" else .22
            shader.bind(); shader.uniform_float("color",(accent[0],accent[1],accent[2],alpha)); batch.draw(shader)
        if geo.lines:
            batch=self._cached_preview_batch(geo,"PATH_LINES","LINES",shader,
                        lambda:[tuple(p) for seg in geo.lines for p in seg])
            gpu.state.depth_test_set("NONE")  # visible contour; does not change world geometry
            gpu.state.line_width_set(2.7 if preview_mode!="TOPOLOGY" else 1.8)
            shader.bind(); shader.uniform_float("color",(accent[0],accent[1],accent[2],.96)); batch.draw(shader)
            gpu.state.depth_test_set("LESS_EQUAL")
        if preview_mode=="TOPOLOGY" and geo.points:
            batch=self._cached_preview_batch(geo,"PATH_POINTS","POINTS",shader,
                        lambda:[tuple(p) for p in geo.points])
            try: gpu.state.point_size_set(5.0)
            except Exception: pass
            shader.bind(); shader.uniform_float("color",(1.0,.72,.30,.95)); batch.draw(shader)
            shader.bind(); shader.uniform_float("color",(0.28,0.94,0.80,0.88)); batch.draw(shader)
        # Rotation LIVE: always-visible geometric orientation guide. This is a
        # transient GPU overlay; it does not claim to be the final production mesh.
        spin=getattr(self,'_spin_state',None)
        if (not spin and getattr(self,'_numeric',False)
                and self._numeric_kind in {'SURFACE_ROT','CORNICE_ROT'}):
            spin={'kind':self._numeric_kind,
                  'value':(d.surface_module_rotation if self._numeric_kind=='SURFACE_ROT'
                           else d.cornice_profile_rotation)}
        if spin and spin.get('kind') in {'SURFACE_ROT','CORNICE_ROT'}:
            # The entire authored contour participates in the GPU preview.
            # All segments are temporary lines; neither a source Path nor a
            # confirmed Result mesh is modified until production validation.
            try:
                chains=self._analysis().get('simple_chains',())
                angle=float(spin.get('value',0.))
                if spin['kind']=='SURFACE_ROT':
                    guide=surface_rotation_ghost(chains, angle,
                        spacing=max(.2,float(d.module_standard)),max_lines=220)
                else:
                    # Direct numeric rotation and spinner rotation share the
                    # SAME full-path guide; a numeric editor has no spin cache.
                    current_profile=spin.get('profile_cache',())
                    if not current_profile and getattr(d,'cornice_profile',None) is not None:
                        profile_obj=d.cornice_profile
                        profile_key=profile_obj.as_pointer()
                        cached=getattr(self,'_cornice_profile_xy_cache',None)
                        if cached is None or cached[0]!=profile_key:
                            from ..production.cornice import cornice_profile_data
                            points,_closed=cornice_profile_data(profile_obj)
                            cached=(profile_key,tuple((float(p.x),float(p.z)) for p in points[:512]))
                            self._cornice_profile_xy_cache=cached
                        current_profile=cached[1]
                    guide=cornice_full_path_ghost(chains,current_profile,
                        angle,flip=bool(d.cornice_flip_side),
                        flip_x=bool(d.cornice_profile_flip_x),flip_z=bool(d.cornice_profile_flip_y),
                        reverse_path=bool(d.cornice_reverse_path),max_edges=3600)
                if guide:
                    coordinates=[pt for segment in guide for pt in segment]
                    overlay=batch_for_shader(shader,'LINES',{'pos':coordinates})
                    gpu.state.depth_test_set('NONE')
                    shader.bind();shader.uniform_float('color',(.96,.68,.27,.85))
                    gpu.state.line_width_set(1.7);overlay.draw(shader)
                    gpu.state.depth_test_set('LESS_EQUAL')
            except (ValueError,RuntimeError,AttributeError,TypeError,ArithmeticError) as exc:
                self._rotation_message='Не удалось показать полный контур: '+str(exc)[:110]
            finally:
                try:
                    gpu.state.depth_test_set('LESS_EQUAL')
                    gpu.state.line_width_set(1.0)
                except (RuntimeError,AttributeError):pass
        # Disabled CAP and PLANT have faint geometric previews so the user
        # knows what can be enabled. This remains visual-only and read-only.
        if d.generator_type=='WALL' and (not d.create_top_cap or not d.create_inner_surface):
            try:
                faded=[]
                height=(-1. if d.story_direction=='DOWN' else 1.)*total_wall_height(d)
                for chain in self._analysis().get('simple_chains',()):
                    pts=list(chain.get('points',()))
                    if not d.create_top_cap:
                        if chain.get('closed') and len(pts)>=3 and (pts[0]-pts[-1]).length>1e-7:
                            pts.append(pts[0])
                        faded.extend((tuple(Vector(a)+Vector((0,0,height))),
                                      tuple(Vector(b)+Vector((0,0,height))))
                                     for a,b in islice(zip(pts,pts[1:]),800))
                    if (not d.create_inner_surface and d.create_inner and chain.get('closed')):
                        faded.extend((tuple(a),tuple(b)) for a,b in zip(pts,pts[1:]))
                if faded:
                    overlay=batch_for_shader(shader,'LINES',{'pos':[point for seg in faded[:800] for point in seg]})
                    gpu.state.depth_test_set('NONE')
                    shader.bind();shader.uniform_float('color',(.41,.86,.74,.57))
                    gpu.state.line_width_set(2.0);overlay.draw(shader)
                    gpu.state.depth_test_set('LESS_EQUAL')
            except (ValueError,RuntimeError,AttributeError,TypeError,ArithmeticError):
                pass  # A ghost cannot disable normal production rendering.
            finally:
                try:gpu.state.depth_test_set('LESS_EQUAL');gpu.state.line_width_set(1.)
                except (RuntimeError,AttributeError):pass
        # Idle cornice ghosts are intentionally DISABLED. The profile is
        # visualised only during an explicit edit/rotation gesture. This also
        # prevents stale side/flip caches from misleading the user.
        if preview_mode=="NORMALS" and geo.triangles:
            def normal_coordinates():
                result=[]
                for tri in geo.triangles:
                    a,b,c=(Vector(p) for p in tri)
                    normal=(b-a).cross(c-a)
                    if normal.length <= 1.0e-9:
                        continue
                    edge_scale=max((b-a).length,(c-b).length,(a-c).length)
                    normal.normalize()
                    length=max(.08,min(.8,edge_scale*.18))
                    center=(a+b+c)/3.0
                    result.extend((tuple(center),tuple(center+normal*length)))
                return result
            batch=self._cached_preview_batch(geo,"NORMAL_LINES","LINES",shader,normal_coordinates)
            if batch is not None:
                gpu.state.line_width_set(2.0)
                shader.bind(); shader.uniform_float("color",(1.0,.68,.24,.95)); batch.draw(shader)
        if geo.points:
            batch=self._cached_preview_batch(geo,"PATH_POINTS","POINTS",shader,
                        lambda:[tuple(p) for p in geo.points])
            gpu.state.point_size_set(6.0)
            shader.bind(); shader.uniform_float("color",(0.92,0.96,0.98,0.95)); batch.draw(shader)
        # Passive UV-origin/seam indicator, always drawn after ordinary path
        # markers and never registered as an interactive handle. One red point
        # per independent WALL chain; closed loops therefore expose exactly
        # one controlled seam at their authored start.
        if getattr(d, 'generator_type', '') == 'WALL' and getattr(geo, 'seam_points', None):
            seam_batch = self._cached_preview_batch(
                geo, 'AUTHORED_UV_SEAM', 'POINTS', shader,
                lambda: [tuple(point) for point in geo.seam_points])
            if seam_batch is not None:
                gpu.state.depth_test_set('NONE')
                gpu.state.point_size_set(13.0)
                shader.bind()
                shader.uniform_float('color', (1.0, 0.10, 0.13, 1.0))
                seam_batch.draw(shader)
                gpu.state.depth_test_set('LESS_EQUAL')
                gpu.state.point_size_set(1.0)
        # Red attention belongs to the GEOMETRY, not to its circular handle.
        # Draw real committed face roles for the current Definition only.
        focus=self._hover or ""
        window=self._window_stack.top_at(*self._last_mouse) if self._window_stack else None
        if window is not None and window.hover and window.hover[0]=="FIELD":
            focus="FIELD:"+str(window.hover[1])
        role_names=semantic_hover_roles(focus,d.generator_type)
        if 'TOP_CAP' in role_names and getattr(geo,'cap_triangles',None):
            overlay=self._cached_preview_batch(geo,"CAP_HOVER","TRIS",shader,
                          lambda:[tuple(vertex) for tri in geo.cap_triangles[:6000] for vertex in tri])
            if overlay is not None:
                shader.bind()
                hover_rgb=tuple(getattr(context.scene.uv_wall_settings,'viewport_hover_color',(1.,.20,.17))); shader.uniform_float("color",(*hover_rgb,.20+.20*hover_pulse(perf_counter())))
                gpu.state.blend_set("ALPHA");gpu.state.depth_test_set("LESS_EQUAL")
                overlay.draw(shader)
        if role_names:
            try:
                result=bpy.data.objects.get(d.last_result_name) if d.last_result_name else None
                if result is not None and result.type=="MESH" and result.get("uvwall_definition_uid","")==d.source_uid:
                    mesh=result.data
                    codes=set(FACE_ROLE_CODES[name] for name in role_names)
                    if getattr(geo,'cap_triangles',None):
                        codes.discard(FACE_ROLE_CODES['TOP_CAP'])
                    role_list=face_role_codes(mesh)
                    marked={i for i,code in enumerate(role_list) if code in codes}
                    if marked and len(marked)<=12000:
                        calc=getattr(mesh,"calc_loop_triangles",None)
                        if callable(calc):calc()
                        coords=[]
                        matrix=result.matrix_world
                        for tri in mesh.loop_triangles:
                            if tri.polygon_index in marked:
                                coords.extend(tuple(matrix @ mesh.vertices[i].co) for i in tri.vertices)
                                if len(coords)>=36000:break
                        if coords:
                            overlay=batch_for_shader(shader,"TRIS",{"pos":coords})
                            shader.bind()
                            hover_rgb=tuple(getattr(context.scene.uv_wall_settings,'viewport_hover_color',(1.,.20,.17))); shader.uniform_float("color",(*hover_rgb,.16+.16*hover_pulse(perf_counter())))
                            gpu.state.blend_set("ALPHA");gpu.state.depth_test_set("LESS_EQUAL")
                            overlay.draw(shader)
            except (ReferenceError,RuntimeError,ValueError,TypeError,AttributeError):
                pass  # Transient Undo/rebuild never interrupts normal viewport.
        gpu.state.depth_test_set("NONE"); gpu.state.blend_set("NONE")

    def _button(self, key, x, y, w, h, label, active=False, enabled=True, visual_state=None):
        self._buttons.append((key,(x,y,x+w,y+h),enabled))
        # The workspace component contract determines all interactive surface
        # colours; contextual marker/corner warnings keep their own palette.
        if not enabled: component_state='disabled'
        elif visual_state in {'focused','hover','open','quiet'}:
            component_state=visual_state
        elif active: component_state='active'
        elif self._hover==key: component_state='hover'
        else: component_state='normal'
        part='tab' if key.startswith(('MODE_', 'DOCK:MODE:')) else 'button'
        shared_theme=_iss_theme.from_preferences(get_preferences())
        visual=_viewport_skin.component(part,component_state,'neutral',shared_theme)
        bg,border=visual.background,visual.border
        fg=_viewport_skin.control_ink(shared_theme,part=part,state=component_state)
        # Common rounded control renderer shared with DWOS, Brick Hole,
        # and Collision. Paint only: existing _buttons is sole hit authority.
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component((x,y,x+w,y+h),part=part,state=component_state,
                              role='neutral',theme=shared_theme,scale=1.):
            _draw_rect(x,y,x+w,y+h,bg,border)
        if visual_state is not None:
            # Distinguish an editing panel from a mere mouse hover without
            # changing the hitbox or adding intrusive always-on text.
            if visual_state=='focused':
                _draw_rect(x+2,y+3,x+5,y+h-3,visual.accent)
            elif visual_state=='open':
                _draw_rect(x+2,y+7,x+4,y+h-7,(*shared_theme.accent,.85))
            _text(x+w-12,y+8,'›',12,fg,'CENTER')
            _text(x+(w-9)*.5,y+8,_short_label(label,w-31.,11),11,fg,"CENTER")
        elif key.startswith('MODE_') and key[5:] in {'WALL','SURFACE','CORNICE'}:
            # Pure vector icons: mode meanings remain available through the
            # existing contextual tooltip, with no new event-handling code.
            mode_icon={'WALL':'wall','SURFACE':'surface','CORNICE':'cornice'}[key[5:]]
            lines=_universal.icon_segments(mode_icon,(x+w*.5-16,y+3,x+w*.5+16,y+h-3))
            try:
                shader=gpu.shader.from_builtin('UNIFORM_COLOR')
                positions=[pt for a,b in lines for pt in (a,b)]
                if positions:
                    batch=batch_for_shader(shader,'LINES',{'pos':positions})
                    shader.bind();shader.uniform_float('color',fg)
                    gpu.state.line_width_set(1.7)
                    batch.draw(shader)
                    gpu.state.line_width_set(1.)
            except (RuntimeError,ValueError,TypeError):
                _text(x+w*.5,y+8,_short_label(label,w-14.,11),11,fg,'CENTER')
        else:
            _text(x+w*0.5,y+8,_short_label(label,w-14.,11),11,fg,"CENTER")

    @staticmethod
    def _ui_stroke(a,b,color,width=1.5):
        try:
            shader=gpu.shader.from_builtin('UNIFORM_COLOR')
            line=batch_for_shader(shader,'LINES',{'pos':[a,b]})
            shader.bind();shader.uniform_float('color',color)
            gpu.state.line_width_set(max(1.,float(width)))
            line.draw(shader)
        except (RuntimeError,ValueError,TypeError,AttributeError):
            pass  # decorative icon failure never interrupts modeling
        finally:
            try:gpu.state.line_width_set(1.)
            except (RuntimeError,AttributeError):pass

    def _register_handle(self, key, pos, radius, **meta):
        if pos is not None: self._handles.append((key,pos,float(radius),meta))

    def _safe_handle_screen(self, context, projected, fallback, dx=0., dy=0.):
        region=self._region(context.area)
        if region is None:return None
        pos=projected if projected is not None else fallback
        if pos is not None:
            pos=(pos[0]+dx,pos[1]+dy)
        return screen_safe_anchor(pos,(region.width,region.height),
                                  (region.width*.5+dx,region.height*.5+dy))

    def _draw_hover_tooltip(self, region, definition):
        if not getattr(bpy.context.window_manager,'uvwall_show_tooltips',True):return
        # Drag precision overlay owns the hint while an interaction is active.
        if self._drag_kind is not None or self._spin_state is not None:return
        if self._radial or self._numeric:
            return
        if self._resource_picker and self._resource_picker.open_kind:
            return
        mx,my=self._last_mouse
        # Floating editor owns its own hitboxes. Hovering it must not show a
        # stale tooltip for a different spatial marker underneath.
        editor=self._window_stack.top_at(mx,my) if self._window_stack else None
        # Group header has an independent measured hitbox contract.
        group_action=getattr(self._window_stack,'group_hover','') if self._window_stack else ''
        if group_action and self._window_stack:
            hint=next((h for key,h,_rect in self._window_stack.group_hitboxes if key==group_action),'')
        elif editor is not None:
            action,field_name=editor.hover
            label=next((f[1] for f in FIELDS.get(editor.kind,()) if f[0]==field_name),field_name)
            if action=='FIELD':hint=f'{label} · нажмите для точной настройки'
            else:
                hint={
                    'APPLY':'Применить черновик: одна отменяемая операция Blender',
                    'CANCEL':'Закрыть окно без изменения геометрии',
                    'RESET':'Сбросить черновик параметров этого окна',
                    'PIN':'Сохранить секцию в группе',
                    'HIDE':'Скрыть окно; его иконка останется в Workspace',
                    'COLLAPSE':'Свернуть параметры до заголовка',
                    'LIST':'Открыть список подходящих ресурсов',
                    'PICK':'Назначить подходящий ресурс из выделения',
                    'CLOSE':'Закрыть настройки без применения изменений',
                }.get(action,'')
            if not hint:return
        else:
            split={
                'W_WALL:LEFT':'Список положений прямо у маркера',
                'W_WALL:RIGHT':'Точно ввести смещение от Path',
                'W_THICKNESS:LEFT':'Список конструкций прямо у маркера',
                'W_THICKNESS:RIGHT':'Толщина · точное числовое поле, можно дописывать число',
                'W_HEIGHT:LEFT':'Редактор высоты · ручка автоматически определяет верх и низ',
                'W_HEIGHT:RIGHT':'Ввести точную высоту стены',
                'W_CAP:LEFT':'Включить или выключить верхнюю крышку',
                'W_CAP:RIGHT':'Редактор крышек, торцов и материалов',
                'W_PLANT:LEFT':'Включить или выключить внутреннюю поверхность',
                'W_PLANT:RIGHT':'Параметры внутренней поверхности',
                'S_Z:RIGHT':'Ввести точное смещение поверхности по Z',
                'S_ROT:RIGHT':'Ввести точный угол поворота',
                'C_ROT:RIGHT':'Ввести точный угол профиля',
                'C_SIDE:RIGHT':'Отражение и стороны карниза',
            }
            hint=split.get(self._hover,viewport_tooltip(self._hover,definition))
        if not hint:return
        # Measured tooltip: no ghost hitbox and no change to original wall
        # markers, their positions or their input handlers. 2K/4K readability
        # is user-configurable; never multiply Blender OS DPI a second time.
        from issmaker_modeling_assistant.core.tooltip_layout import tooltip_layout
        profile=_ui_profile(get_preferences(bpy.context))
        font_px=max(11,profile.font_px(11))
        blf.size(0,font_px)
        layout=tooltip_layout(hint,(region.width,region.height),(mx,my),
                              lambda sample:blf.dimensions(0,sample)[0],
                              font_px=font_px,max_width=420.*profile.safe_window,
                              pad_x=12.*profile.safe_window,
                              pad_y=9.*profile.safe_window)
        if layout is None:return
        tx,ty,w,h=layout.rect
        theme=_iss_theme.from_preferences(get_preferences(context=bpy.context))
        tooltip=_viewport_skin.component('card',theme=theme)
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component((tx,ty,tx+w,ty+h),part='card',theme=theme):
            _draw_rect(tx,ty,tx+w,ty+h,tooltip.background,tooltip.border)
        for i,line in enumerate(layout.lines):
            baseline=ty+h-layout.pad_y-(i+1)*layout.line_height+2.
            _text(tx+layout.pad_x,baseline,line,
                  float(layout.font_px)/max(.01,_ui_profile(get_preferences(bpy.context)).font),
                  _viewport_skin.ink(theme))

    def _draw_precision_feedback(self, context, region):
        """Purely painted precision state near pointer; never a hit target."""
        if (self._numeric or self._radial or getattr(self,'_clean_preview',False)
                or not getattr(context.window_manager,'uvwall_show_tooltips',True)):
            return
        if self._resource_picker and self._resource_picker.open_kind:return
        spin=getattr(self,'_spin_state',None)
        kind=(spin.get('kind') if spin else self._drag_kind)
        if kind is None:return
        shift,ctrl=(spin.get('modifiers',(False,False)) if spin else
                    getattr(self,'_precision_modifiers',(False,False)))
        cue=precision_cue(kind,shift=shift,ctrl=ctrl,spin=bool(spin))
        if cue is None:return
        profile=_ui_profile(get_preferences(context))
        scale=viewport_ui_scale(context)
        head_size=max(10,profile.font_px(11))
        desc_size=max(9,profile.font_px(10))
        try:
            blf.size(0,head_size)
            head_w=blf.dimensions(0,cue.heading)[0]
            blf.size(0,desc_size)
            desc_w=blf.dimensions(0,cue.description)[0]
        except (RuntimeError,ValueError,AttributeError):return
        width=max(head_w,desc_w)+24.*scale
        height=max(57.*scale,head_size+desc_size+23.*scale)
        # Never abbreviate the step values: if the label does not fit, do not
        # draw a misleading partially cropped precision cue.
        obstacles=[self._hud_rect,self._workspace_rect,self._problem_rect,
                   getattr(self,'_missing_resources_rect',None)]
        obstacles.extend(getattr(self,'_point_card_rects',()))
        obstacles.extend(getattr(self,'_spatial_rects',{}).values())
        stack=self._window_stack
        if stack is not None:
            obstacles.extend(w.rect() for w in stack.windows if not w.layout_hidden)
            if stack.group_rect:obstacles.append(stack.group_rect)
        if getattr(context.window_manager,'uvwall_display_open',False):
            obstacles.append(display_panel_rect(region.width,region.height,scale))
        place=place_cue(self._last_mouse,(width,height),(region.width,region.height),obstacles,
                        margin=8.,offset=18.*scale)
        if place is None:return
        x0,y0,x1,y1=place
        stroke=(.35,.86,.73,.96) if cue.role=='snapped' else (
               (.40,.75,.98,.96) if cue.role=='precise' else (.35,.46,.52,.90))
        _draw_editor_rounded(x0,y0,x1,y1,(.025,.041,.048,.975),stroke)
        _draw_rect(x0+7*scale,y0+9*scale,x0+10*scale,y1-9*scale,stroke)
        _text(x0+16*scale,y1-(head_size+9*scale),cue.heading,11,(.93,.99,.96,1.))
        _text(x0+16*scale,y0+10*scale,cue.description,10,(.75,.86,.86,1.))

    def _resource_rows(self, kind):
        picker=self._resource_picker
        editor = self._picker_editor
        if kind == 'MATERIAL' and editor is not None and editor[2] in {
                'cap_material', 'bottom_cap_material', 'inner_surface_material'}:
            # Overrides decorate geometry; they do not define UV module size.
            rows = [(mat, True, 'Готов', 'Материал поверхности')
                    for mat in sorted(bpy.data.materials, key=lambda m:m.name.casefold())]
            return filter_rows(rows, picker.query if picker else '')
        base=(picker.rows(kind,bpy,path=self._path(),definition=self._definition()) if picker else
              resources_for(kind,bpy,path=self._path(),definition=self._definition()))
        return filter_rows(base,
                           picker.query if picker else "")

    def _refresh_problems(self, context):
        try:
            from ..operators.problems import refresh_scene_problems
            refresh_scene_problems(context)
        except Exception:
            pass

    def _rebind_path(self, context, path):
        """Move this modal session to another authored Path without leaking handlers."""
        valid,reason,_meta=validate_resource("PATH",path,path=self._path(),definition=self._definition())
        if not valid:
            return False,reason
        settings=context.scene.uv_wall_settings
        settings.path_mesh=path
        definition=initialize_from_scene(path,settings)
        if definition is None:
            return False,"Не удалось создать Path Definition"
        self._path_ref=path;self._path_name=path.name
        self._source_uid=ensure_source_uid(path,definition)
        self._engine.bind(path,definition);self._engine.invalidate(Dirty.ALL);self._engine.update()
        if self._window_stack is not None:
            self._window_stack.clear();self._window_stack.set_source(self._source_uid)
        self._smart_segment_key=None;self._corner_selected=-1;self._wall_point_selected=-1;self._surface_point_selected=-1
        return True,"Готов"

    def _apply_resource(self, kind, item, context):
        picker=self._resource_picker
        path=self._path();definition=self._definition()
        valid,reason,_meta=validate_resource(kind,item,path=path,definition=definition)
        if (kind == 'MATERIAL' and self._picker_editor is not None
                and self._picker_editor[2] in {'cap_material', 'bottom_cap_material', 'inner_surface_material'}):
            valid = item is not None and isinstance(item, bpy.types.Material)
        if not valid:
            if picker:
                picker.message=reason;picker.message_level="ERROR"
            return False
        editor=self._picker_editor
        if editor is not None:
            win,expected_kind,field=editor
            if (kind!=expected_kind or self._window_stack is None
                    or win not in self._window_stack.windows or win.uid!=self._source_uid):
                self._picker_editor=None
                if picker:
                    picker.message="Окно изменилось — откройте список заново"
                    picker.message_level="ERROR"
                return False
            # A dropdown selection is a *draft*, not an immediate build or
            # mutation of an unrelated property shared by another editor.
            win.draft[field]=item.name
            win.edit_field='';win.edit_buffer='';win.error=''
            self._picker_editor=None
            picker.remember(kind,item);picker.invalidate();picker.close_dropdown()
            picker.message=f"{RESOURCE_LABELS[kind]} · черновик · нажмите Применить"
            picker.message_level="INFO"
            return True
        if kind=="PATH":
            ok,reason=self._rebind_path(context,item)
        else:
            snapshot=snapshot_definition(definition)
            ok=assign_resource(kind,item,definition);reason="Готов" if ok else "Не удалось назначить ресурс"
            if ok:
                if kind=="PROFILE" and definition.generator_type=="CORNICE":
                    # Choosing a section is STEP 2, not a production build.
                    # Do not discard the chosen profile when the prior result
                    # exists, or when an authored Sharp corner is still missing.
                    # A new profile without authored corners starts in Smooth.
                    try:
                        has_sharp=bool(json.loads(definition.corner_uid_json or "[]"))
                    except (ValueError, TypeError):
                        has_sharp=True  # Never override corrupt corner state.
                    if snapshot.get('cornice_profile') is None and not has_sharp:
                        definition.cornice_uv_mode="MAX_REAL_WORLD"
                    definition.last_error=""
                    sync_scene_from_definition(path,context.scene.uv_wall_settings)
                    self._engine.invalidate(Dirty.ALL);self._engine.update()
                    ready,_readiness=self._cornice_readiness(definition)
                    if ready:
                        ok=self._commit_or_restore(context,snapshot,quiet=True,
                                                  history_label='назначение профиля карниза')
                        reason=('Профиль применён · предпросмотр обновлён' if ok
                                else (definition.last_error or 'Профиль не удалось применить'))
                    else:
                        reason='Профиль назначен · '+_readiness
                else:
                    sync_scene_from_definition(path,context.scene.uv_wall_settings)
                    self._engine.invalidate(Dirty.ALL);self._engine.update()
                    ready,_readiness=self._cornice_readiness(definition) if definition.generator_type=="CORNICE" else (True,"Ready")
                    if ready:
                        # Other complete resource edits still take a single
                        # protected production transaction.
                        ok=self._commit_or_restore(context,snapshot,quiet=True)
                        reason="Готов · AUTO" if ok else (definition.last_error or "Автообновление не завершено")
                    elif has_committed_result(definition,path):
                        restore_definition(definition,snapshot)
                        sync_scene_from_definition(path,context.scene.uv_wall_settings)
                        self._engine.invalidate(Dirty.ALL);self._engine.update()
                        ok=False
                        reason="Результат не изменён: " + _readiness
                    else:
                        reason="Настройка сохранена · " + _readiness
        if picker:
            picker.message=(f"{RESOURCE_LABELS[kind]} · {item.name} · {reason}" if ok else reason)
            picker.message_level="SUCCESS" if ok else "ERROR"
            if ok:
                picker.remember(kind,item);picker.invalidate();picker.close_dropdown();picker.viewport_kind="";picker.pick_state="VALID"
        if ok:
            self._refresh_problems(context)
        return ok

    def _clear_resource(self, kind, context):
        """Clear as an atomic viewport edit, never leave a stale generated Mesh.

        Unsetting a required Cornice profile is allowed before the first build;
        after a successful build it must be replaced rather than silently
        disconnecting an existing production result from its Definition.
        """
        d=self._definition();path=self._path()
        if d is None or kind=="PATH":return False
        editor=self._picker_editor
        if editor is not None:
            win,expected_kind,field=editor
            if kind==expected_kind and self._window_stack is not None and win in self._window_stack.windows:
                win.draft[field]='';win.edit_field='';win.edit_buffer='';win.error=''
                self._picker_editor=None
                self._resource_picker.message=f"{RESOURCE_LABELS[kind]} · черновик очищен"
                self._resource_picker.message_level="INFO"
                return True
            self._picker_editor=None
            return False
        if kind not in {"PROFILE","MATERIAL"}:return False
        if kind=="MATERIAL":
            if current_resource(kind,path,d) is None:
                return True
        elif current_resource(kind,path,d) is None:
            return True
        snap=snapshot_definition(d)
        if kind=="PROFILE":
            d.cornice_profile=None
        elif kind=="MATERIAL":
            setattr(d,"virtual_material_2k" if str(d.module_standard)=="2" else "virtual_material_4k",None)
        d.revision+=1
        sync_scene_from_definition(path,context.scene.uv_wall_settings)
        self._engine.invalidate(Dirty.ALL);self._engine.update()
        ready,reason=self._cornice_readiness(d) if d.generator_type=="CORNICE" else (True,"Ready")
        if ready:
            ok=self._commit_or_restore(context,snap,quiet=True)
            reason="Очищено · AUTO" if ok else (d.last_error or "Ошибка перестройки")
        elif has_committed_result(d,path):
            restore_definition(d,snap)
            sync_scene_from_definition(path,context.scene.uv_wall_settings)
            self._engine.invalidate(Dirty.ALL);self._engine.update()
            ok=False
            reason="Нельзя очистить обязательный ресурс готового результата; выберите замену. " + reason
        else:
            ok=True
            reason="Очищено · требуется завершить настройки"
        picker=self._resource_picker
        if picker is not None:
            picker.message=reason;picker.message_level="INFO" if ok else "ERROR"
            picker.invalidate(kind)
        self._refresh_problems(context)
        return ok

    def _active_for_resource(self, kind, context):
        active=getattr(context,"active_object",None)
        if kind in {"PATH","PROFILE"}:return active
        if kind=="MATERIAL":return getattr(active,"active_material",None) if active else None
        return None

    def _show_resource(self, item, context):
        if item is None or not hasattr(item,"select_set"):return False
        try:
            for obj in context.selected_objects:obj.select_set(False)
            item.hide_set(False);item.select_set(True);context.view_layer.objects.active=item
            bpy.ops.view3d.view_selected(use_all_regions=False)
            return True
        except Exception:
            return False

    def _raycast_object(self, context, x, y):
        region=self._region(context.area);rv3d=getattr(context.space_data,"region_3d",None)
        if region is None or rv3d is None:return None
        try:
            coord=(float(x),float(y))
            origin=view3d_utils.region_2d_to_origin_3d(region,rv3d,coord)
            direction=view3d_utils.region_2d_to_vector_3d(region,rv3d,coord)
            hit,location,normal,index,obj,matrix=context.scene.ray_cast(
                context.evaluated_depsgraph_get(),origin,direction,distance=1.0e7)
            return obj if hit else None
        except Exception:
            return None

    def _set_corner_sharp(self,index,sharp,context):
        """Per-vertex Smooth/Sharp: one Definition transaction + one Blender Undo.

        UI never edits Scene.cornice_corners directly; the production bridge
        reconstructs that collection from the persistent UID list on commit.
        """
        path=self._path();d=self._definition()
        if path is None or d is None or index<0 or index>=len(path.data.vertices):return False
        # Never stamp corner IDs into a BMesh before validating object mode.
        # Multiple mesh users must not receive unrequested UID mutations.
        error=object_mode_error(context)
        if error:
            self.report({'WARNING'},error)
            return False
        if getattr(path.data,'users',1)>1:
            self.report({'WARNING'},'Карниз: исходный Mesh общий для нескольких объектов. Сделайте Single User.')
            return False
        uid=next((int(uid) for uid,item in sync_corner_uids(path).items()
                  if int(item[0])==int(index)),None)
        if uid is None:return False
        try:
            payload,mode=corner_patch(d.corner_uid_json,uid,sharp)
        except ValueError as exc:
            self.report({'WARNING'},str(exc));return False
        if payload==d.corner_uid_json and mode==d.cornice_uv_mode:
            return True
        snapshot=snapshot_definition(d)
        d.corner_uid_json=payload
        if not sharp:
            d.corner_methods_json=method_remove(getattr(d,'corner_methods_json','{}'),uid)
        d.cornice_uv_mode=mode
        d.revision+=1
        sync_scene_from_definition(path,context.scene.uv_wall_settings)
        self._engine.invalidate(Dirty.ALL);self._engine.update()
        ready,reason=self._cornice_readiness(d)
        if not ready:
            restore_definition(d,snapshot)
            sync_scene_from_definition(path,context.scene.uv_wall_settings)
            d.last_error=reason
            self._register_build_problem(context)
            self._refresh_problems(context)
            return False
        ok=self._commit_or_restore(context,snapshot,quiet=False)
        self._refresh_problems(context)
        return ok

    def _set_corner_method(self, mode, context):
        """Undoable per-UID geometry experiment with atomic Result swap."""
        d=self._definition(); path=self._path();uid=int(self._corner_selected_uid or 0)
        if d is None or path is None or uid<=0 or mode not in MODE_IDS:return False
        error=object_mode_error(context)
        if error:
            self.report({'WARNING'},error);return False
        if getattr(path.data,'users',1)>1:
            self.report({'WARNING'},'Выберите Single User Mesh для исходного пути');return False
        try:
            sharp_uids={int(x) for x in json.loads(d.corner_uid_json or '[]')}
            payload=method_patch(getattr(d,'corner_methods_json','{}'),uid,mode)
        except (ValueError,TypeError,OverflowError) as exc:
            self.report({'WARNING'},f'Повреждены настройки угла: {exc}');return False
        if uid not in sharp_uids:
            self.report({'WARNING'},'Способ доступен только для Sharp угла');return False
        if payload==d.corner_methods_json:return True
        snapshot=snapshot_definition(d)
        d.corner_methods_json=payload;d.revision+=1
        sync_scene_from_definition(path,context.scene.uv_wall_settings)
        self._engine.invalidate(Dirty.ALL);self._engine.update()
        ready,reason=self._cornice_readiness(d)
        if not ready:
            restore_definition(d,snapshot)
            sync_scene_from_definition(path,context.scene.uv_wall_settings)
            d.last_error=reason
            self._register_build_problem(context)
            self._refresh_problems(context)
            return False
        ok=self._commit_or_restore(context,snapshot,quiet=False)
        self._refresh_problems(context)
        return ok

    def _apply_workspace_button(self,key,context):
        picker=self._resource_picker;d=self._definition()
        if not key:return False
        if key=='CLEAN:EXIT':
            self._clean_preview=False;self._clean_button=None;return True
        if key=='RP:SEARCH':return True
        if key=='PROB_HIDE':
            context.scene.uv_wall_settings.problem_inspector_visible=False
            return True
        if key=='PROB_SHOW_PANEL':
            context.scene.uv_wall_settings.problem_inspector_visible=True
            return True
        if key=='WS:HIDE':
            self._workspace_visible=False
            self._workspace_details=False
            self._workspace_history_open=False
            return True
        if key=='WS:HISTORY':
            self._workspace_history_open=not self._workspace_history_open
            if self._workspace_history_open:
                self._workspace_details=False
                picker.close_dropdown()
            return True
        if key.startswith('WS:HIST:'):
            try:
                revision_id=int(key.rsplit(':',1)[1])
                from ..core.revisions import PROPERTY, revision_payload
                path=self._path()
                definition=self._definition()
                if path is None or definition is None or not has_committed_result(definition,path):
                    raise ValueError('Нет принятой стены')
                payload=revision_payload(str(path.get(PROPERTY,'')), str(definition.source_uid), revision_id)
                snapshot=snapshot_definition(definition)
                from ..core.revision_recovery import atomic_restore_definition
                old_parent = path.get('uvwall_history_restore_parent_v1', None)
                try:
                    atomic_restore_definition(
                        definition,payload,bpy.data,
                        sync=lambda:sync_scene_from_definition(path,context.scene.uv_wall_settings),
                    )
                    path['uvwall_history_restore_parent_v1']=revision_id
                    ok=self._commit_or_restore(context,snapshot,quiet=False,
                                               history_label=f'восстановление ревизии {revision_id}')
                except Exception:
                    # An exception between parameter load and the transactional
                    # Build must not leave a half-restored Definition or branch.
                    restore_definition(definition,snapshot)
                    if old_parent is None:
                        if 'uvwall_history_restore_parent_v1' in path:
                            del path['uvwall_history_restore_parent_v1']
                    else:
                        path['uvwall_history_restore_parent_v1']=old_parent
                    sync_scene_from_definition(path,context.scene.uv_wall_settings)
                    raise
                if not ok:
                    if old_parent is None:
                        if 'uvwall_history_restore_parent_v1' in path: del path['uvwall_history_restore_parent_v1']
                    else:
                        path['uvwall_history_restore_parent_v1']=old_parent
                    self._toast('Ревизия не восстановлена · прежняя стена сохранена','ERROR',4.)
                else:
                    self._workspace_history_open=False
                    self._toast(f'Восстановлена ревизия {revision_id}','SUCCESS')
            except (ValueError,AttributeError,ReferenceError,RuntimeError,TypeError) as exc:
                self._toast('Ошибка истории: '+str(exc)[:100],'ERROR',4.)
            return True
        if key=='WS:SHOW':
            self._workspace_visible=True
            return True
        if key=='WS:PREVIEW':
            self._clean_preview=not getattr(self,'_clean_preview',False)
            self._clean_button=None
            self._hover=None;self._marker_choice=None;self._marker_choice_rows=[]
            if context.area:context.area.tag_redraw()
            return True
        if key=='WS:TOGGLE':
            self._workspace_visible=True
            self._workspace_history_open=False
            self._workspace_details=not self._workspace_details
            return True
        if key=="RP:CLOSE":picker.close_dropdown();self._picker_editor=None;return True
        if key=="RP:CLEAR":
            kind=picker.open_kind
            ok=self._clear_resource(kind,context)
            if ok:picker.close_dropdown()
            return True
        if key.startswith("RP:ROW:"):
            rows=self._resource_rows(picker.open_kind)
            try:index=int(key.rsplit(":",1)[1])
            except Exception:return True
            if 0<=index<len(rows):self._apply_resource(picker.open_kind,rows[index][0],context)
            return True
        if key=="RP:INVALID":
            picker.include_invalid=not picker.include_invalid;picker.invalidate();return True
        if key.startswith("RP:"):
            parts=key.split(":")
            if len(parts)!=3:return False
            _prefix,kind,action=parts
            if action=="OPEN":
                window_visibility.set_hidden(window_visibility.scope_from_context(context), 'UV_WALL_RESOURCE_PICKER', False)
                self._picker_editor=None;picker.open(kind)
            elif action=="VIEW":self._picker_editor=None;picker.begin_viewport(kind)
            elif action=="ACTIVE":
                item=self._active_for_resource(kind,context)
                if item is None:
                    picker.message=f"Для «{RESOURCE_LABELS[kind]}» в выделенном ничего не найдено"
                    picker.message_level="ERROR"
                else:self._apply_resource(kind,item,context)
            elif action=="MORE":
                item=current_resource(kind,self._path(),d)
                if kind in {"PATH","PROFILE"}:self._show_resource(item,context)
                else:
                    window_visibility.set_hidden(window_visibility.scope_from_context(context), 'UV_WALL_RESOURCE_PICKER', False)
                    self._picker_editor=None;picker.open(kind)
            return True
        if key=='DISPLAY:TOGGLE':
            wm=context.window_manager
            wm.uvwall_display_open=not wm.uvwall_display_open
            if wm.uvwall_display_open:self._workspace_details=False
            return True
        if key=='DISPLAY:ARROWS':
            context.window_manager.uvwall_show_arrows=not context.window_manager.uvwall_show_arrows;return True
        if key=='DISPLAY:TOOLTIPS':
            context.window_manager.uvwall_show_tooltips=not context.window_manager.uvwall_show_tooltips;return True
        if key=='DISPLAY:LABELS':
            context.window_manager.uvwall_show_labels=not context.window_manager.uvwall_show_labels;return True
        if key.startswith('DISPLAY:THEME:') or key=='DISPLAY:THEME_NEXT':
            # Legacy action compatibility; old controls are no longer drawn.
            # Actual themes are only set from the root ISSMAKER Preferences.
            self._toast('Темы теперь в ISSMAKER · Внешний вид','INFO',1.4)
            return True
        if key=='DISPLAY:EFFECT':
            self._toast('Анимированный фон · ISSMAKER → Внешний вид','INFO',1.4)
            return True
        if key=='DISPLAY:COLOR':
            bpy.ops.uvwall.hover_palette('INVOKE_DEFAULT')
            return True
        if key.startswith('DISPLAY:SWATCH:'):
            try:index=int(key.rsplit(':',1)[1])
            except ValueError:return True
            if 0<=index<len(HOVER_PALETTE_PRESETS):
                context.scene.uv_wall_settings.viewport_hover_color=HOVER_PALETTE_PRESETS[index]
                self._toast('Цвет наведения обновлён','SUCCESS',1.3)
            return True
        if key=="CORNERS_EDIT":
            # Compatibility with existing buttons: corner handles are now
            # automatic in CORNICE mode; this only focuses a corner overlay.
            self._corner_edit_active=True
            self._corner_selected=-1
            return True
        if key in {"CORNICE_SMOOTH","CORNICE_SHARP"} and d is not None:
            snapshot=snapshot_definition(d)
            d.cornice_uv_mode="MAX_REAL_WORLD" if key=="CORNICE_SMOOTH" else "MAX_CLIPPED"
            d.revision+=1;sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            self._engine.invalidate(Dirty.ALL);self._engine.update()
            ready,reason=self._cornice_readiness(d)
            if ready:self._commit_or_restore(context,snapshot,quiet=True)
            else:
                d.last_error=reason
                self._register_build_problem(context)
            self._refresh_problems(context);return True
        if key=="CORNER_SMOOTH":return self._set_corner_sharp(self._corner_selected,False,context)
        if key=="CORNER_SHARP":return self._set_corner_sharp(self._corner_selected,True,context)
        if key=="PROBLEMS_REFRESH":
            self._refresh_problems(context)
            count=active_problem_count(context.scene.uv_wall_settings)
            self._toast("Проверка завершена · проблем не найдено" if not count else f"Проверка завершена · найдено проблем: {count}",
                        "SUCCESS" if not count else "WARNING",3.2)
            return True
        if key=="SELECTION_MODE":
            wm=context.window_manager
            wm.uvwall_selection_mode="LOCK" if wm.uvwall_selection_mode=="FOLLOW" else "FOLLOW"
            self._selection_mismatch=False;return True
        if key=="SELECTION_REBIND":
            return self._sync_follow_selection(context,force=True)
        if key=="RESTORE_SUCCESS":
            try:bpy.ops.object.uv_wall_restore_last_successful()
            except Exception:return False
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality="SETTLED");return True
        if key=="PREVIEW_REFRESH":
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality="SETTLED")
            self._toast("Предпросмотр обновлён","SUCCESS")
            return True
        if key=="BUILD":
            ready,reason=self._cornice_readiness(d) if d is not None and d.generator_type=="CORNICE" else (True,"Ready")
            if ready:
                before=int(getattr(d,'last_committed_revision',-1)) if d is not None else -1
                requested=int(getattr(d,'revision',-1)) if d is not None else -1
                had_changes=(before!=requested)
                started=perf_counter();ok=self._commit_production(context)
                elapsed=perf_counter()-started
                after=int(getattr(d,'last_committed_revision',-1)) if d is not None else -1
                if ok and not had_changes:
                    message=f"Изменений нет · результат перепроверен · {elapsed:.2f} s"
                elif ok:
                    message=f"Обновлено · revision {before} → {after} · {elapsed:.2f} s"
                else:
                    message="Обновление не завершено"
                self._toast(message,"SUCCESS" if ok else "ERROR",3.2)
            else:
                if d is not None:d.last_error=reason
                self._register_build_problem(context)
                self._toast(reason,"WARNING",4.)
                self.report({"WARNING"},reason)
            return True
        return False

    def _draw_resource_row(self, kind, x, y, width, context, compact=False):
        d=self._definition();item=current_resource(kind,self._path(),d)
        valid,reason,meta=validate_resource(kind,item,path=self._path(),definition=d)
        label=RESOURCE_LABELS[kind]
        if kind=="MATERIAL":
            expected="2K" if str(getattr(d,"module_standard","4"))=="2" else "4K"
            label=f"{label} · {expected}"
        _text(x,y+34,label.upper(),8,(.49,.61,.62,1))
        field_w=width-72
        self._button(f"RP:{kind}:OPEN",x,y,field_w,29,(item.name if item else "Выбрать…"),False,True)
        if kind in {"PATH","PROFILE"}:
            self._button(f"RP:{kind}:VIEW",x+field_w+5,y,31,29,"◎",False,True)
        else:
            self._button(f"RP:{kind}:ACTIVE",x+field_w+5,y,31,29,"↙",False,True)
        self._button(f"RP:{kind}:MORE",x+field_w+41,y,31,29,"⋮",False,item is not None)
        if not compact:
            color=(.37,.91,.74,1) if valid else ((1.,.65,.27,1) if item else (.55,.63,.65,1))
            _text(x,y-14,(meta or reason)[:52],8,color)

    def _setup_requirement(self, definition=None):
        """Return the missing mandatory source, not a build error.

        Optional materials/images are deliberately non-blocking because the
        virtual module/texture workflow supports valid first builds without
        a user-authored plane. Advanced cornice geometry is not offered before
        both its path and section profile are valid.
        """
        d=definition or self._definition()
        if d is None:
            return ('PATH', 'Не выбран исходный объект')
        path=self._path()
        valid,reason,_=validate_resource('PATH',path,definition=d)
        if not valid:
            return ('PATH',reason)
        if d.generator_type=='CORNICE':
            valid,reason,_=validate_resource('PROFILE',d.cornice_profile,
                                             path=path,definition=d)
            if not valid:
                return ('PROFILE',reason)
        return None

    def _draw_setup_gate(self, context, region, d, y):
        """Only required setup actions remain until a valid source is chosen."""
        missing=self._setup_requirement(d)
        if missing is None:
            return False
        kind,reason=missing
        width=min(414.,float(region.width)-18.)
        x=(region.width-width)*.5
        height=min(166.,max(0.,float(region.height)-(y+61.)-9.))
        if height<45.:
            return False
        bottom=y+58.
        top=bottom+height
        self._workspace_rect=(x,y,x+width,top)
        _draw_rect(x-3,bottom-3,x+width+3,top+3,(.006,.010,.014,.48))
        _draw_rect(x,bottom,x+width,top,(.027,.043,.048,.992),(.55,.51,.27,1))
        _draw_rect(x,top-3,x+width,top,(.92,.67,.29,1))
        narrow=width<300.
        title='КАРНИЗ · ПОДГОТОВКА' if d.generator_type=='CORNICE' else 'ПОДГОТОВКА · '+('СТЕНА' if d.generator_type=='WALL' else 'ПОВЕРХНОСТЬ')
        if height>=113.:
            _text(x+12,top-22,title,10,(.98,.93,.78,1))
            path=self._path()
            path_name=path.name if path else 'не указан'
            _text(x+12,top-42,_short_label(('✓  1. Путь: ' if kind=='PROFILE' else '!  1. Путь: ')+path_name,width-24,9),9,
                  (.52,.90,.73,1) if kind=='PROFILE' else (1.,.69,.38,1))
            if kind=='PROFILE':
                _text(x+12,top-60,_short_label('!  2. Профиль: '+reason,width-24,9),9,(1.,.75,.45,1))
                if height>=139.:
                    _text(x+12,top-79,'3. Углы, поворот и сторона — после профиля',8,(.63,.73,.73,1))
            else:
                _text(x+12,top-60,_short_label(reason,width-24,9),9,(1.,.75,.45,1))
                if height>=139.:
                    _text(x+12,top-79,'Настройки появятся после выбора правильного пути',8,(.63,.73,.73,1))
        elif height>=77.:
            _text(x+12,top-22,'Сначала назначьте '+('профиль' if kind=='PROFILE' else 'путь'),9,(1.,.78,.49,1))
        margin=10.;gap=5.;usable=width-2*margin-2*gap
        cx=x+margin
        for key,caption,fraction in (
            (f'RP:{kind}:OPEN','Выбрать '+('профиль' if kind=='PROFILE' else 'путь') if not narrow else 'Выбрать',.46),
            (f'RP:{kind}:ACTIVE','Из выделенного' if not narrow else 'Выдел.',.29),
            (f'RP:{kind}:VIEW','В сцене' if not narrow else 'Сцена',.25)):
            cw=usable*fraction
            self._button(key,cx,bottom+10.,cw,29.,caption,key.endswith(':OPEN'),True)
            cx+=cw+gap
        return True

    def _draw_workspace(self, context, region, d, geo):
        """1.7.2: centered, collapsible bottom rail with a separate resource tray.

        Unlike the old lower-left panel this never occupies a permanent canvas
        column. Even in compact Blender windows the hide/show control survives.
        """
        if region.width<185 or region.height<120:
            self._workspace_rect=None
            return
        scale=viewport_ui_scale(context)
        width=min(520.*scale,float(region.width)-24.*scale)
        x=(float(region.width)-width)*.5
        # Raise the rail away from the Blender timeline/footer and cursor.
        y=(74. if region.height>=250.*scale else (43. if region.height>=190.*scale else 18.))*scale
        if not self._workspace_visible:
            width=100.;x=(float(region.width)-width)*.5
            self._workspace_rect=(x,y,x+width,y+34.)
            _draw_rect(x,y,x+width,y+34.,(.032,.045,.051,.96),(.25,.43,.40,.96))
            self._button('WS:SHOW',x+5,y+4,width-10,26,'▴ Показать',False,True)
            return
        # Five actions keep readable labels; narrow windows wrap into rows.
        count=5
        cols=5 if width>=450*scale else (4 if width>=390*scale else (3 if width>=318*scale else (2 if width>=208*scale else 1)))
        rows=(count+cols-1)//cols
        rail_height=(14.+rows*35.)*scale
        if y+rail_height>region.height-8*scale:
            # Tiny Viewports keep one deliberate accessible control.
            rail_height=45.*scale
            self._workspace_rect=(x,y,x+width,y+rail_height)
            _draw_editor_rounded(x,y,x+width,y+rail_height,(.031,.047,.052,.976),(.24,.39,.39,.98))
            self._button('WS:PREVIEW',x+6*scale,y+7*scale,width-12*scale,29*scale,'Просмотр',False,True)
            return
        self._workspace_rect=(x,y,x+width,y+rail_height)
        _draw_rect(x-3,y-3,x+width+3,y+rail_height+3,(.005,.009,.013,.37))
        _draw_editor_rounded(x,y,x+width,y+rail_height,(.031,.047,.052,.976),(.24,.39,.39,.98))
        from issmaker_modeling_assistant.core.studio_gpu_components import shell_background
        from issmaker_modeling_assistant.core import theme_engine
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        shell_background(theme_engine.from_preferences(addon.preferences if addon else None),(x,y,x+width,y+rail_height))
        _draw_rect(x+8,y+rail_height-2,x+width-8,y+rail_height,(.35,.84,.70,.70))
        needed=self._setup_requirement(d)
        captions=('Ресурсы','Проверка',
                  ('Профиль' if needed and needed[0]=='PROFILE' else 'Путь') if needed else
                  ('Построить' if d.generator_type=='CORNICE' and
                   (not getattr(d,'last_result_name','') or int(getattr(d,'revision',0))!=int(getattr(d,'last_committed_revision',-1)))
                   else 'Обновить'),
                  'История','Просмотр')
        kinds=('WS:TOGGLE','PROBLEMS_REFRESH',('RP:'+needed[0]+':OPEN') if needed else 'BUILD',
               'WS:HISTORY','WS:PREVIEW')
        cell_w=(width-12.*scale-(cols-1)*5.*scale)/cols
        for i,(key,caption) in enumerate(zip(kinds,captions)):
            col=i%cols;row=i//cols
            self._button(key,x+6*scale+col*(cell_w+5.*scale),y+7*scale+row*35*scale,cell_w,29*scale,caption,
                         key=='BUILD' or (key=='WS:PREVIEW' and getattr(self,'_clean_preview',False)),True)
        # Resource/setup panels start above the ENTIRE rail, not in its cells.
        tray_anchor_y=y+rail_height+7.
        if getattr(self,'_workspace_history_open',False):
            from ..core.revisions import PROPERTY, parse_ledger
            path=self._path();owner_uid=str(getattr(d,'source_uid','') or '')
            try:
                ledger=parse_ledger(str(path.get(PROPERTY,'')) if path is not None else '',owner_uid)
                entries=list(reversed(ledger['entries'][-6:]))
                history_message='' if entries else 'История появится после первого успешного Build'
            except (ValueError,TypeError,ReferenceError) as exc:
                entries=[];history_message='История недоступна: '+str(exc)[:50]
            history_h=(53.+35.*len(entries))*scale
            if tray_anchor_y+history_h>float(region.height)-8*scale:
                available=max(30.,float(region.height)-tray_anchor_y-8*scale)
                max_rows=max(0,int((available-53*scale)//(35*scale)))
                entries=entries[:max_rows]
                history_h=min(history_h,available)
                if not entries: history_message='Увеличьте высоту Viewport для просмотра ревизий'
            hx=x;hy=tray_anchor_y;hw=width
            self._workspace_rect=(min(x,hx),y,max(x+width,hx+hw),hy+history_h)
            _draw_editor_rounded(hx,hy,hx+hw,hy+history_h,
                                 (.031,.047,.052,.976),(.28,.53,.47,.98))
            _text(hx+13.,hy+history_h-20.*scale,'ИСТОРИЯ · ПРИНЯТЫЕ ПАРАМЕТРЫ',10,(.82,.98,.92,1))
            if history_message:
                _text(hx+13.,hy+history_h-43.*scale,_short_label(history_message,hw-25,10),10,(.71,.79,.79,1))
            for index,entry in enumerate(entries):
                row_y=hy+history_h-(58.+index*35.)*scale
                detail=describe_revision(entry,ledger,max_fields=1)
                label=f"#{entry['id']} · {detail}"
                self._button(f"WS:HIST:{entry['id']}",hx+9.*scale,row_y,
                             hw-18.*scale,28.*scale,label,False,True)
            return
        # The setup gate replaces the drawer; anchor follows the full rail.
        if self._draw_setup_gate(context,region,d,tray_anchor_y):
            return
        # Resource dropdown is modal.  Temporarily hide its parent resource
        # drawer while it is open; restore it automatically after closing.
        # This frees real space without changing the user's saved preference.
        if not self._workspace_details or (self._resource_picker and self._resource_picker.open_kind):
            return
        y=tray_anchor_y
        is_cornice=d.generator_type=='CORNICE'
        tray_width=min(362.*scale,float(region.width)-26.*scale)
        tray_x=(float(region.width)-tray_width)*.5
        # The drawer is above, not behind, the persistent action rail.
        available_height=max(0.,float(region.height)-(y+58.)-12.)
        # One header, exactly the fields in the active mode and one helper
        # row; do not reserve a large blank resource box for three controls.
        tray_height=min((212. if is_cornice else 195.)*scale,available_height)
        if tray_height<76.*scale:return
        tray_y=y+58.
        top=tray_y+tray_height
        self._workspace_rect=(tray_x,tray_y if y>tray_y else y,tray_x+tray_width,top)
        _draw_rect(tray_x,tray_y,tray_x+tray_width,top,(.032,.048,.051,.985),(.24,.46,.41,.95))
        _draw_rect(tray_x,top-34,tray_x+tray_width,top,(.058,.094,.087,.99))
        tray_title='РЕСУРСЫ · '+{'WALL':'СТЕНА','SURFACE':'ПОВЕРХНОСТЬ','CORNICE':'КАРНИЗ'}.get(d.generator_type,d.generator_type)
        _text(tray_x+13,top-24,_short_label(tray_title,tray_width-66,10),10,(.91,.97,.95,1))
        # Drawer collapses back into Resources; no duplicate Hide action.
        self._button('WS:TOGGLE',tray_x+tray_width-42,top-29,29,23,'−',False,True)
        if tray_height<165.*scale:
            labels=(('MATERIAL','Материал'),('PROFILE','Профиль')) if is_cornice else (('PATH','Путь'),('MATERIAL','Материал'))
            visible=list(labels)
            cell=(tray_width-24.-4.*(len(visible)-1))/len(visible)
            for i,(kind,label) in enumerate(visible):
                self._button(f'RP:{kind}:OPEN',tray_x+12+i*(cell+4),tray_y+10,cell,29,label,False,True)
        else:
            # Each selector is single-purpose. The source and output are never
            # altered until the selected resource has passed validation.
            kinds=('PROFILE','MATERIAL') if is_cornice else ('PATH','MATERIAL')
            spacing=min(52.,max(34.,(tray_height-54.)/len(kinds)))
            for i,kind in enumerate(kinds):
                row_y=top-76-i*spacing
                self._draw_resource_row(kind,tray_x+13,row_y,tray_width-26,context,compact=True)
            if is_cornice and tray_height>=222.*scale:
                _text(tray_x+13,tray_y+35,'УГЛЫ НАСТРАИВАЮТСЯ НА ТОЧКАХ ПУТИ',8,(.55,.73,.70,1))
                _text(tray_x+13,tray_y+18,'Нажмите маркер вершины → Плавный / Острый',8,(.68,.80,.79,1))
            elif not is_cornice and tray_height>=190.*scale:
                self._button('PREVIEW_REFRESH',tray_x+13,tray_y+10,tray_width-26,27,
                             'Обновить предпросмотр',False,True)

    def _picker_allocator(self, context, region):
        """Only one frame-local placement authority; NEVER accept peer overlap.

        The current mode HUD and missing-resource warning are immovable;
        resource dropdowns must go around them rather than paint on top.
        """
        layout=StrictOverlayAllocator(region.width,region.height,margin=8.,gap=7.)
        for i,rect in enumerate(blender_chrome_obstacles(
                region.width,region.height,viewport_ui_scale(context))):
            layout.reserve(f"BLENDER_CHROME:{i}",rect)
        if getattr(self,"_hud_rect",None):layout.reserve('MODE_HUD',self._hud_rect)
        if getattr(self,"_missing_resources_rect",None):
            layout.reserve('RESOURCE_WARNING',self._missing_resources_rect)
        if getattr(self,"_workspace_rect",None):layout.reserve('WORKSPACE',self._workspace_rect)
        if getattr(self,"_problem_rect",None):layout.reserve('PROBLEM',self._problem_rect)
        if getattr(self,"_corner_popup_rect",None):layout.reserve('CORNICE_EDITOR',self._corner_popup_rect)
        if getattr(self,"_dock_rectangle",None) and active_ui_mode(context)=='STUDIO_DOCK':
            layout.reserve('STUDIO_DOCK',self._dock_rectangle)
        if self._window_stack is not None and active_ui_mode(context)!='STUDIO_DOCK':
            group=self._window_stack.group_rect
            if group is not None:
                layout.reserve('JOINED_EDITOR_WORKSPACE',group)
            for i,window in enumerate(self._window_stack.windows):
                if not getattr(window,'layout_hidden',False) and not window.grouped:
                    layout.reserve(f'EDITOR:{i}',window.rect())
        if getattr(context.window_manager,'uvwall_display_open',False):
            layout.reserve('DISPLAY',display_panel_rect(
                region.width,region.height,viewport_ui_scale(context)))
        return layout

    def _draw_picker_dropdown(self, context, region):
        picker=self._resource_picker
        if picker is None:return
        # No button can outlive the frame/area where it was drawn. This is
        # important when the user resizes a high-DPI Viewport mid-selection.
        self._picker_rendered_region=None
        self._buttons=[b for b in self._buttons
                       if not (str(b[0]).startswith('RP:ROW:') or b[0] in
                               {'RP:SEARCH','RP:INVALID','RP:CLEAR','RP:CLOSE'})]
        picker.hitboxes=[]
        self._picker_rect=None
        if picker.viewport_kind:
            w=min(430.,region.width-28.)
            layout=self._picker_allocator(context,region)
            placed=layout.place('RESOURCE_VIEWPORT_PROMPT',(w,52.),
                                ((region.width-w)*.5,22.))
            if placed is not None:
                x,y=placed[0],placed[1]
                self._picker_rect=placed
                theme=_iss_theme.from_preferences(get_preferences())
                role='danger' if picker.message_level=='ERROR' else 'neutral'
                status_style=_viewport_skin.component('card','active' if role=='danger' else 'normal',role,theme)
                _draw_rect(x,y,x+w,y+52,status_style.background,status_style.border)
                _text(x+15,y+30,_short_label(picker.message,w-29,11),11,_viewport_skin.ink(theme))
            self._picker_layout_snapshot=layout.snapshot()
        kind=picker.open_kind
        visibility_scope=window_visibility.scope_from_context(context,region=region)
        picker.hitboxes=[]
        self._picker_rect=None
        if not kind:return
        if window_visibility.is_hidden(visibility_scope,'UV_WALL_RESOURCE_PICKER'):
            window_visibility.observe(visibility_scope,'UV_WALL_RESOURCE_PICKER',None)
            return
        if region.height<175 or region.width<220:
            self._picker_layout_snapshot={"failure":{"reason":"VIEWPORT_TOO_SMALL"}}
            window_visibility.observe(visibility_scope,'UV_WALL_RESOURCE_PICKER',None)
            return
        rows=self._resource_rows(kind)
        width=min(456.,region.width-26.)
        # A modal picker must respect every registered independent surface.
        # Unlike the legacy "best overlap" selection, no colliding placement
        # is ever rendered or exposed to pointer events.
        picker.hitboxes=[]
        self._picker_rect=None
        editor=self._picker_editor[0] if self._picker_editor else None
        anchor=(editor.x if editor is not None else
                (self._workspace_rect[0] if self._workspace_rect else 30.))
        desired_top=float(region.height)-47.
        if editor is not None:
            desired_top=min(desired_top,editor.y+editor.height)
        status_extra=(49 if region.height>=260 else 15) if picker.message_level=='ERROR' else 0
        empty_extra=64 if not rows else 0
        layout=self._picker_allocator(context,region)
        placed=None
        # Try the full width first, then a narrower responsive card.  The
        # title/actions move to a stacked header below 330px: no collisions.
        # Don't shrink below 225px: controls must remain usable.
        width_candidates=sorted(set([width]+[min(width,w) for w in (400.,350.,320.,280.,225.)]),reverse=True)
        for test_width in width_candidates:
            if test_width<225.:continue
            test_stacked=test_width<330 and region.height>=205
            test_extra_header=32 if test_stacked else 0
            test_max_rows=max(1,min(9,int((region.height-122-test_extra_header-status_extra)//46)))
            for test_capacity in range(min(len(rows),test_max_rows),-1,-1):
                if rows and test_capacity==0:continue
                test_height=108+test_extra_header+test_capacity*46+status_extra+empty_extra
                candidate=layout.place('RESOURCE_PICKER',(test_width,test_height),
                                        (float(anchor),desired_top-test_height))
                if candidate is not None:
                    placed=candidate
                    width=test_width;capacity=test_capacity
                    stacked_header=test_stacked;extra_header=test_extra_header
                    break
            if placed is not None:break
        if placed is None:
            self._picker_layout_snapshot=layout.snapshot()
            # Next frame can retry after resize.  If a full list cannot fit,
            # expose a tiny readable, closable notice in a guaranteed free
            # zone, rather than silent disappearance or invisible controls.
            notice_width=min(292.,max(175.,region.width-24.))
            notice=layout.place('RESOURCE_COMPACT_NOTICE',(notice_width,38.),
                                ((region.width-notice_width)*.5,16.))
            if notice is not None:
                nx,ny,nx1,ny1=notice
                self._picker_rect=window_visibility.observe(visibility_scope,'UV_WALL_RESOURCE_PICKER',notice)
                if self._picker_rect is not None:
                    self._picker_rendered_region=(int(region.width),int(region.height))
                if self._picker_rect is None:
                    picker.hitboxes.clear()
                    return
                self._picker_layout_snapshot=layout.snapshot()
                theme=_iss_theme.from_preferences(get_preferences())
                notice_style=_viewport_skin.component('card','active','warning',theme)
                _draw_rect(nx,ny,nx1,ny1,notice_style.background,notice_style.border)
                _text(nx+9,ny+14,_short_label('Увеличьте 3D View для списка',notice_width-48.,9),9,
                      _viewport_skin.ink(theme))
                self._button('RP:CLOSE',nx1-33.,ny+5.,26.,27.,'×',False,True)
            if notice is None:
                window_visibility.observe(visibility_scope,'UV_WALL_RESOURCE_PICKER',None)
            # Don't keep stale interactive row/search buttons from an older
            # viewport size; only the explicit compact close action survives.
            self._buttons=[b for b in self._buttons if not str(b[0]).startswith('RP:ROW:')
                           and b[0] not in {'RP:SEARCH','RP:INVALID','RP:CLEAR'}]
            return
        placed=window_visibility.observe(visibility_scope,'UV_WALL_RESOURCE_PICKER',placed)
        if placed is None:
            picker.hitboxes.clear()
            return
        x,y,x1,top=placed
        self._picker_rect=placed
        self._picker_rendered_region=(int(region.width),int(region.height))
        self._picker_layout_snapshot=layout.snapshot()
        if rows:
            start=max(0,min(max(0,len(rows)-capacity),picker.active_index-capacity//2))
            visible=rows[start:start+capacity]
        else:
            start=0;visible=[]
        theme=_iss_theme.from_preferences(get_preferences())
        shell=_viewport_skin.shell(theme)
        _draw_rect(x-4,y-5,x+width+4,top+4,(.005,.009,.013,.75))
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component((x,y,x+width,top),part='card',theme=theme):
            _draw_rect(x,y,x+width,top,shell['panel'],shell['border'])
        header_height=42+extra_header
        _draw_rect(x,top-header_height,x+width,top,shell['header'])
        _draw_rect(x,top-2,x+width,top,(*theme.accent,.88))
        has_current=kind!='PATH' and current_resource(kind,self._path(),self._definition()) is not None
        if stacked_header:
            title_px=width-64
            _text(x+14,top-26,_short_label(f'{RESOURCE_LABELS[kind]}  ·  {len(rows)}',title_px,12),12,_viewport_skin.ink(theme))
            self._button('RP:INVALID',x+12,top-67,102,25,
                         'Все' if picker.include_invalid else 'Валидные',False,True)
            if has_current:
                self._button('RP:CLEAR',x+width-112,top-67,99,25,'Очистить',False,True)
        else:
            title_px=max(35,width-211)
            _text(x+14,top-26,_short_label(f'{RESOURCE_LABELS[kind]}  ·  {len(rows)}',title_px,12),12,_viewport_skin.ink(theme))
            toggle_width=70 if width>=300 else 50
            self._button('RP:INVALID',x+width-195,top-34,toggle_width,25,
                         ('Все' if picker.include_invalid else ('Валидные' if width>=300 else 'OK')),False,True)
            if has_current:
                self._button('RP:CLEAR',x+width-115,top-34,71,25,'Очистить',False,True)
        self._button('RP:CLOSE',x+width-38,top-34,27,25,'×',False,True)
        sy=top-77-extra_header
        search_box=(x+12,sy,x+width-12,sy+29)
        search_style=_viewport_skin.component('field','focused' if picker.query else 'normal',theme=theme)
        if not draw_component(search_box,part='field',state='focused' if picker.query else 'normal',theme=theme):
            _draw_rect(*search_box,search_style.background,search_style.border)
        prompt=picker.query+'│' if picker.query else 'Поиск по названию и свойствам…'
        _text(x+23,sy+9,_short_label(prompt,width-55,10),10,
              _viewport_skin.control_ink(theme,part='field',state='focused') if picker.query else _viewport_skin.ink(theme,kind='muted'))
        picker.hitboxes=[('SEARCH',search_box)]
        self._buttons.append(('RP:SEARCH',search_box,True))
        if not rows:
            _text(x+16,y+46,'Ничего не найдено',11,(.96,.76,.58,1))
            _text(x+16,y+29,'Измените запрос или покажите все объекты',8,(.63,.75,.74,1))
        elif picker.message_level=='ERROR':
            # Explain failures in 2–3 readable lines instead of silently
            # truncating "Нельзя менять обязательный ресурс...".
            from textwrap import wrap
            line_chars=max(18,int((width-43.)/5.9))
            lines=wrap(str(picker.message or "Ошибка назначения"),width=line_chars,
                       break_long_words=False,break_on_hyphens=False)
            available=3 if status_extra>=40 else 1
            for i,line in enumerate(lines[:available]):
                _text(x+14,y+10+(available-1-i)*15,
                      _short_label(line,width-29,9),9,(1.,.69,.54,1))
        else:
            _text(x+14,y+10,'↑↓ / колесо · Enter выбрать · Esc закрыть',8,(.56,.73,.70,1))
        row_y=sy-44
        for offset,(item,valid,reason,meta) in enumerate(visible):
            index=start+offset
            selected=index==min(picker.active_index,max(0,len(rows)-1))
            bounds=(x+12,row_y,x+width-12,row_y+40)
            row_state='disabled' if not valid else ('active' if selected else 'normal')
            row_style=_viewport_skin.component('button',row_state,theme=theme)
            if not draw_component(bounds,part='button',state=row_state,theme=theme):
                _draw_rect(*bounds,row_style.background,row_style.border)
            _text(x+23,row_y+23,_short_label(item.name,width-50,11),11,
                  _viewport_skin.control_ink(theme,part='button',state=row_state))
            _text(x+23,row_y+8,_short_label(meta or reason,width-51,8),8,
                  _viewport_skin.ink(theme,kind='muted') if valid else (1.,.70,.51,1))
            self._buttons.append((f'RP:ROW:{index}',bounds,valid))
            row_y-=46

    def _physical_alignment(self,definition):
        """Same physical INSIDE/OUTSIDE as production, never mutate source."""
        mode=getattr(definition,'wall_alignment','LEGACY')
        if mode not in {'INSIDE','OUTSIDE'}:
            return mode
        from ..production.wall_alignment import resolve_placement
        from ..production.path import ordered_path_vertices
        try:
            return resolve_placement(mode,ordered_path_vertices(self._path()),definition.inset_side)
        except (ValueError,TypeError,AttributeError,ReferenceError):
            return 'LEGACY'  # invalid authored path: never infer a side in the UI

    def _draw_direction_arrows(self,context,d,mid,direction,normal):
        if not getattr(context.window_manager,'uvwall_show_arrows',True):return
        if mid is None:return
        key=self._hover or ''
        vectors=[]
        if d.generator_type=='WALL' and not d.create_inner and key in {'','W_WALL','W_SIDE'}:
            from ..production.wall_alignment import preview_normal_side
            chains=self._analysis().get('simple_chains',())
            chosen=next((c for c in chains if len(c.get('points',()))>=2),None)
            if chosen is not None:
                try:
                    normal_side=preview_normal_side(chosen['points'],closed=bool(chosen.get('closed')),
                                                    normal_mode=d.wall_normal_mode)
                    vectors=[normal if normal_side=='LEFT' else -normal]
                except ValueError:
                    vectors=[]
        elif key in {'W_WALL','W_SIDE'}:
            mode=self._physical_alignment(d)
            if mode=='LEFT':vectors=[normal]
            elif mode=='RIGHT':vectors=[-normal]
            elif mode=='CENTER':vectors=[normal,-normal]
            else:vectors=[normal]
        elif key=='W_THICKNESS' and d.create_inner:
            from ..production.wall_alignment import automatic_inset_side
            side=automatic_inset_side(self._physical_alignment(d),d.inset_side)
            vectors=[normal*(1. if side=='LEFT' else -1.)]
        elif key in {'C_SIDE'}:
            vectors=[normal*(-1. if getattr(d,'cornice_flip_side',False) else 1.)]
        if not vectors:return
        shader=gpu.shader.from_builtin('UNIFORM_COLOR');coords=[]
        for vec in vectors:
            a=self._screen(context,mid);b=self._screen(context,mid+vec*1.35)
            if a is None or b is None:continue
            dx=b[0]-a[0];dy=b[1]-a[1];length=max(1.e-6,(dx*dx+dy*dy)**.5);ux=dx/length;uy=dy/length
            tip=(b[0],b[1]);base=(a[0],a[1]);left=(b[0]-ux*13-uy*7,b[1]-uy*13+ux*7);right=(b[0]-ux*13+uy*7,b[1]-uy*13-ux*7)
            coords.extend([base,tip,tip,left,tip,right])
        if coords:
            batch=batch_for_shader(shader,'LINES',{'pos':coords});shader.bind();rgb=tuple(getattr(context.scene.uv_wall_settings,'viewport_hover_color',(1.,.20,.17)));shader.uniform_float('color',(*rgb,.94));gpu.state.line_width_set(3.0);batch.draw(shader);gpu.state.line_width_set(1.0)

    def _draw_corner_tools(self,context,path,d):
        if d.generator_type!="CORNICE" or self._setup_requirement(d) is not None:return
        settings=context.scene.uv_wall_settings
        uid_to_index=read_corner_uid_indices(path)
        sharp=sharp_indices(d.corner_uid_json,uid_to_index)
        if self._corner_selected_uid:
            self._corner_selected=uid_to_index.get(self._corner_selected_uid,-1)
        degrees={index:0 for index in range(len(path.data.vertices))}
        for edge in path.data.edges:
            a,b=map(int,edge.vertices);degrees[a]=degrees.get(a,0)+1;degrees[b]=degrees.get(b,0)+1
        seen=set()
        for chain in self._analysis().get("simple_chains",()):
            for index,world in zip(chain.get("indices",()),chain.get("points",())):
                index=int(index)
                if index in seen or index>=len(path.data.vertices):continue
                seen.add(index)
                if degrees.get(index)!=2:continue
                p=self._screen(context,world)
                if p is None:continue
                active=index==self._corner_selected;is_sharp=index in sharp
                color=(.98,.68,.24,1) if is_sharp else (.29,.91,.75,1)
                if is_sharp:
                    r=8 if active else 6
                    shader=gpu.shader.from_builtin("UNIFORM_COLOR")
                    pts=[(p[0],p[1]+r),(p[0]+r,p[1]),(p[0],p[1]-r),(p[0]-r,p[1]),(p[0],p[1]+r)]
                    batch=batch_for_shader(shader,"LINE_STRIP",{"pos":pts});shader.bind();shader.uniform_float("color",color);batch.draw(shader)
                else:_draw_circle(p,8 if active else 6,color,2,active)
                self._register_handle(f"CORNER:{index}",p,13.,vertex_index=index)
                if active:
                    px,py,pw,ph=corner_popup_position(
                        p,(context.region.width,context.region.height),
                        size=(252,136))
                    # Corner tools use exactly the same strict policy as the
                    # material picker; on a crowded viewport only the 3D
                    # marker remains available, never a colliding panel.
                    placed=self._picker_allocator(context,context.region).place(
                        'CORNER_EDITOR',(pw,ph),(px,py))
                    if placed is None:continue
                    px,py=placed[0],placed[1]
                    self._corner_popup_rect=placed
                    _draw_rect(px,py,px+pw,py+ph,(.024,.037,.043,.99),(.27,.67,.57,1))
                    _text(px+12,py+ph-22,f"УГОЛ {index:02d}",11,(.95,.98,.97,1))
                    _text(px+12,py+ph-41,"Острый · Sharp" if is_sharp else "Плавный · Smooth",9,color)
                    button_w=(pw-28.)*.5
                    top_buttons_y=py+ph-81
                    self._button("CORNER_SMOOTH",px+10,top_buttons_y,button_w,30,"Плавный",not is_sharp,True)
                    self._button("CORNER_SHARP",px+18+button_w,top_buttons_y,button_w,30,"Острый",is_sharp,True)
                    # Pass57: no 5-way experimental joining UI. The original
                    # Classic/Miter algorithm remains the default for new corners;
                    # old per-UID metadata is retained for saved-project fidelity.
                    if is_sharp:
                        _text(px+12,py+ph-99,"Соединение: старый метод",8,(.6,.83,.78,1))
                    self._button(f"FOCUS:C:{index}",px+10,py+8,pw-20,23,"◎ Показать точку",False,True)

    def _draw_marker_glyph(self,key,x,y,focus=False):
        """Vector-only, no font glyph fallback gaps, kept outside edit hitboxes."""
        col=(.55,.95,.82,.96) if focus else (.43,.71,.67,.83)
        def line(x1,y1,x2,y2,w=1.35):
            self._ui_stroke((x+x1,y+y1),(x+x2,y+y2),col,w)
        if key=='W_THICKNESS':
            line(2,3,2,17,2);line(10,3,10,17,2);line(4,5,8,5);line(4,15,8,15)
        elif key=='W_WALL':
            line(1,10,13,10);line(9,6,13,10);line(9,14,13,10)
        elif key=='W_HEIGHT':
            line(7,2,7,18);line(3,6,7,2);line(11,6,7,2);line(3,14,7,18);line(11,14,7,18)
        elif key=='W_CAP':
            line(1,6,13,6,2);line(2,7,2,17);line(12,7,12,17)
        elif key=='W_PLANT':
            line(7,18,7,8);line(7,8,2,5);line(7,8,12,5);line(2,5,1,2);line(12,5,13,2)
        elif key=='C_PROFILE':
            line(1,16,4,16);line(4,16,4,11);line(4,11,9,11);line(9,11,9,5);line(9,5,13,5)
        elif key in {'S_DIR','S_ROT','C_ROT'}:
            line(7,2,7,18);line(3,6,7,2);line(11,6,7,2)
        elif key=='S_Z':
            line(1,13,5,9);line(5,9,9,11);line(9,11,13,4)

    def _draw_numeric_marker_field(self, rect):
        x0,y0,x1,y1=rect
        if x1-x0<25 or y1-y0<14:return
        theme=_iss_theme.from_preferences(get_preferences())
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component(rect,part='field',state='focused',theme=theme):
            style=_viewport_skin.component('field','focused',theme=theme)
            _draw_editor_rounded(x0,y0,x1,y1,style.background,style.border)
        # Pencil icon signals actual text editing, not a passive status or menu.
        color=_viewport_skin.control_ink(theme,part='field',state='focused')
        self._ui_stroke((x1-17,y0+8),(x1-10,y0+15),color,2.1)
        self._ui_stroke((x1-19,y0+5),(x1-17,y0+8),color,1.5)
        self._ui_stroke((x1-19,y0+5),(x1-15,y0+7),color,1.0)
        self._ui_stroke((x1-15,y0+9),(x1-8,y0+16),color,1.2)

    def _draw_marker_choice_dropdown(self, region):
        """Styled marker-anchored list. Hit boxes are created by this draw only."""
        self._marker_choice_rows=[]
        marker=getattr(self,'_marker_choice',None)
        rect=self._spatial_rects.get(marker) if marker else None
        d=self._definition()
        if rect is None or d is None:
            self._marker_choice_rect=None
            return
        options=_precision.choices_for(marker,is_double=bool(d.create_inner))
        outer=_precision.popup_rect(rect,(region.width,region.height),len(options))
        self._marker_choice_rect=outer
        if outer is None:
            self._marker_choice=None
            return
        x0,y0,x1,y1=outer
        theme=_iss_theme.from_preferences(get_preferences())
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component(outer,part='card',theme=theme):
            style=_viewport_skin.component('card',theme=theme)
            _draw_editor_rounded(x0,y0,x1,y1,style.background,style.border)
        field='create_inner' if marker=='W_THICKNESS' else ('wall_alignment' if d.create_inner else 'wall_normal_mode')
        selected=('DOUBLE' if d.create_inner else 'SINGLE') if marker=='W_THICKNESS' else str(getattr(d,field,''))
        for idx,(value,label) in enumerate(options):
            top=y1-6-idx*29
            row=(x0+6,top-27,x1-6,top)
            focused=value==selected
            state='active' if focused else 'normal'
            style=_viewport_skin.component('button',state,theme=theme)
            if not draw_component(row,part='button',state=state,theme=theme):
                _draw_editor_rounded(*row,style.background,style.border)
            _text(row[0]+12,row[1]+8,_short_label(('✓ ' if focused else '   ')+label,row[2]-row[0]-24,11),11,
                  _viewport_skin.control_ink(theme,part='button',state=state))
            self._marker_choice_rows.append((value,row))
            self._buttons.append((f'MARKER_CHOICE:{idx}',row,True))

    def _draw_pixel(self):
        context=bpy.context
        if not context.area or context.area.as_pointer() != self._area_ptr: return
        path=self._path(); d=self._definition()
        if path is None or d is None or not self._engine: return
        self._buttons=[]; self._handles=[]
        # Blender may invalidate an Operator RNA after Undo or modal exit.
        # Resolve popup ownership through a guarded registry, never by directly
        # dereferencing an untrusted/stale Operator stored in _ACTIVE.
        from issmaker_modeling_assistant.styled_panels import exclusive_panel_active
        if exclusive_panel_active(context.region):
            return
        if getattr(self, '_clean_preview', False):
            region=self._region(context.area)
            self._spatial_rects={};self._clean_button=None
            if region is not None:
                bounds=_clarity.clean_preview_button(region.width,region.height)
                if bounds is not None:
                    self._clean_button=bounds
                    ax,ay,bx,by=bounds
                    _draw_editor_rounded(ax,ay,bx,by,(.018,.052,.054,.985),(.31,.88,.72,.97))
                    _text((ax+bx)*.5,ay+9,'◉ ВЕРНУТЬ HUD',11,(.89,.99,.95,1.),'CENTER')
                    self._buttons=[('CLEAN:EXIT',bounds,True)]
            return
        self._picker_rect=None; self._missing_resources_rect=None
        self._picker_layout_snapshot=None; self._problem_rect=None
        self._corner_popup_rect=None
        if active_ui_mode(context) == "STUDIO_DOCK":
            self._spatial_rects={}
            self._hud_rect=None
            self._workspace_rect=None
            region=self._region(context.area)
            if region is not None:
                self._draw_studio_dock(context,region,d)
                if d is not None and self._path() is not None:
                    self._draw_corner_tools(context,self._path(),d)
            return  # Studio controls remain in one responsive inspector.
        geo=self._engine.update(); analysis=self._analysis(); centroid=self._centroid()
        region=self._region(context.area)
        if region is None or region.width<105 or region.height<100:return
        self._spatial_anchors={}
        self._point_card_rects=[]
        # Context transition cannot leave old CAP / PLANT drafts floating over
        # an ineligible open/single wall. Closing discards their drafts only.
        if d.generator_type=='WALL' and self._window_stack is not None:
            closed=any(bool(c.get('closed')) for c in analysis.get('simple_chains',()))
            if not d.create_inner and not closed:self._window_stack.close('CAP')
            if not d.create_inner or not closed:self._window_stack.close('PLANT')
        # The only fixed control is the tiny mode switcher; ALL model
        # parameters live at geometric anchors, not in a centroid HUD.
        mode_scale=viewport_ui_scale(context)
        hud_w=min(334.0*mode_scale,region.width-28.0*mode_scale)
        # 3D region-local coords exclude the Blender header but NOT the left
        # toolbar. Top-centre keeps the switcher clear of tools and axes.
        x=max(8.0*mode_scale,(float(region.width)-hud_w)*.5)
        y=max(8.*mode_scale,float(region.height)-113.0*mode_scale)
        self._hud_rect=(x,y,x+hud_w,y+66.*mode_scale)
        effect=getattr(context.window_manager,"uvwall_visual_effect","GRAPHITE")
        hud_bg,hud_border,accent=_visual_palette(effect)
        _draw_rect(x,y,x+hud_w,y+66.*mode_scale,hud_bg,hud_border)
        if effect=="BLUEPRINT":
            for gx in range(int(x)+12,int(x+hud_w)-8,24):
                _draw_rect(gx,y+2*mode_scale,gx+1,y+64*mode_scale,(.34,.65,.88,.045))
        elif effect=="TOPO":
            for i,gy in enumerate(range(int(y)+10,int(y)+60,14)):
                inset=7+(i%3)*8
                _draw_rect(x+inset,gy,x+hud_w-inset,gy+1,(.30,.68,.58,.035))
        _text(x+12*mode_scale,y+45*mode_scale,(f"UV WALL · {path.name[:24]}" if hud_w>235 else "UV WALL"),11,(.92,.96,.97,1))
        if hud_w>200:
            result_state,result_color=_result_state(d)
            if geo.status.startswith("БЫСТРЫЙ КОНТУР"):
                _text(x+hud_w-10*mode_scale,y+45*mode_scale,"FAST · КОНТУР",9,(1.,.73,.36,1.),"RIGHT")
            else:
                _text(x+hud_w-10*mode_scale,y+45*mode_scale,result_state,9,result_color,"RIGHT")
        gap=4.*mode_scale; margin=8.*mode_scale; tab=(hud_w-2*margin-2*gap)/3
        for i,(mode,label,_) in enumerate(MODE_INFO):
            names=({'WALL':'СТЕНА','SURFACE':'ПОВЕРХНОСТЬ','CORNICE':'КАРНИЗ'}
                   if hud_w>250 else {'WALL':'СТЕНА','SURFACE':'ПОВ.','CORNICE':'КАРН.'})
            self._button('MODE_'+mode,x+margin+i*(tab+gap),y+7*mode_scale,tab,29*mode_scale,
                         names[mode],d.generator_type==mode,self._mode_available(mode))
        # Optional resources are explicit, not fatal. A valid geometry build
        # can use virtual 2K/4K texel references without assigned assets.
        missing_resources=[]
        for kind,caption in (("MATERIAL","Материал"),):
            asset=current_resource(kind,self._path(),d)
            valid,reason,_=validate_resource(kind,asset,path=self._path(),definition=d)
            if not valid:
                missing_resources.append((kind,caption,reason))
        if missing_resources and getattr(context.window_manager,"uvwall_preview_mode","MATERIAL")=="MATERIAL":
            ay=y-81.*mode_scale
            self._missing_resources_rect=(x,ay,x+hud_w,ay+73.*mode_scale)
            _draw_rect(x,ay,x+hud_w,ay+73.*mode_scale,(.045,.055,.060,.97),(.79,.51,.25,.90))
            message=" · ".join(caption+": "+reason for _,caption,reason in missing_resources)
            _text(x+9*mode_scale,ay+55*mode_scale,_short_label(message,hud_w-18*mode_scale,9),9,(1.,.82,.59,1.))
            _text(x+9*mode_scale,ay+41*mode_scale,"Назначьте материал с Image Texture 2K/4K",8,(.74,.81,.82,1.))
            gap=6.
            count=len(missing_resources)
            width=(hud_w-18*mode_scale-gap*(count-1))/count
            for index,(kind,caption,_reason) in enumerate(missing_resources):
                self._button(f'RP:{kind}:OPEN',x+9*mode_scale+index*(width+gap),ay+7*mode_scale,width,29*mode_scale,
                             'Выбрать: '+caption.lower(),False,True)
        lens=context.window_manager.uvwall_viewport_lens

        # Spatial wall handles. Blue vertex marks: drag to modify source Z;
        # Shift: 5 cm snap; Ctrl: 5 mm snap; Tab: type exact absolute Z.
        if d.generator_type=="WALL" and analysis.get("simple_chains"):
            h=total_wall_height(d); sign=-1.0 if d.story_direction=="DOWN" else 1.0
            local_basis=self._first_segment_basis()[0]
            height_origin=local_basis if local_basis is not None else centroid
            # Pick a visible LOCAL segment for the height gizmo; do not use
            # global minimum Z on non-planar paths. Avoid far-left/outside
            # endpoints when a central segment is easier to interact with.
            from .ghost_geometry import center_height_anchor
            # Object-centric projected anchor, but Z interpolated from the
            # *nearest local authored Path segment*, never a global Z plane.
            visual_center=self._visual_object_center(d,analysis)
            centered=center_height_anchor(analysis.get('simple_chains',()),visual_center)
            if centered is not None:
                candidate=Vector((visual_center.x,visual_center.y,float(centered[2])))
                candidate_screen=self._screen(context,candidate+Vector((0,0,sign*h)))
                if candidate_screen is not None and 26<candidate_screen[0]<region.width-26 and 26<candidate_screen[1]<region.height-26:
                    height_origin=candidate
            base=self._screen(context,height_origin)
            if d.align_bottom and d.story_direction=="DOWN":
                analysis_points=[p for chain in analysis.get("simple_chains",[]) for p in chain["points"]]
                bottom_z=(min((p.z for p in analysis_points),default=centroid.z)-max(0.0,float(d.bottom_extend)))
                target=Vector((centroid.x,centroid.y,bottom_z)); hp=self._screen(context,target)
                if hp and base:
                    _draw_circle(hp,10 if self._hover in ('HEIGHT','BOTTOM_LEVEL') or self._drag_kind in ('HEIGHT','BOTTOM_LEVEL') else 7,(0.22,0.95,0.80,0.95),2.0,True)
                    shader=gpu.shader.from_builtin("UNIFORM_COLOR")
                    batch=batch_for_shader(shader,"LINES",{"pos":[base,hp]}); shader.bind(); shader.uniform_float("color",(0.22,0.95,0.80,0.75)); gpu.state.line_width_set(2); batch.draw(shader)
                    _text(hp[0]+12,hp[1]-5,f"Низ {format_value(-d.bottom_extend)}",10,(0.88,0.98,0.95,1))
                    self._register_handle("BOTTOM_LEVEL",hp,16.0,anchor=centroid)
            else:
                hp=self._screen(context,height_origin+Vector((0,0,sign*h)))
                if hp and base:
                    _draw_circle(hp,10 if self._hover in ('HEIGHT','BOTTOM_LEVEL') or self._drag_kind in ('HEIGHT','BOTTOM_LEVEL') else 7,(0.22,0.95,0.80,0.95),2.0,True)
                    shader=gpu.shader.from_builtin("UNIFORM_COLOR")
                    batch=batch_for_shader(shader,"LINES",{"pos":[base,hp]}); shader.bind(); shader.uniform_float("color",(0.22,0.95,0.80,0.75)); gpu.state.line_width_set(2); batch.draw(shader)
                    self._register_handle("HEIGHT",hp,16.0,anchor=height_origin,sign=sign)
                    self._spatial_anchors["W_HEIGHT"]=hp
            if "W_HEIGHT" not in self._spatial_anchors:
                height_world=height_origin+Vector((0,0,sign*h))
                height_screen=self._screen(context,height_world)
                if height_screen:
                    _draw_circle(height_screen,8,(.22,.95,.80,.95),2,True)
                    self._register_handle("HEIGHT",height_screen,16.,anchor=height_origin,sign=sign)
                    self._spatial_anchors["W_HEIGHT"]=height_screen
            mid,_dir,norm=self._first_segment_basis()
            if mid is not None and norm is not None:
                from ..production.wall_alignment import left_signed_shift, automatic_inset_side
                visual_side=automatic_inset_side(self._physical_alignment(d),d.inset_side)
                # Match production: single wall follows authored Path; its
                # stored (inactive) double-wall width cannot move the preview.
                align_shift=(left_signed_shift(self._physical_alignment(d),
                    d.inset_distance,visual_side,True) if d.create_inner else 0.0)
                wall_mid=mid+norm*(d.wall_offset+align_shift)
                mp=self._screen(context,mid); offp=self._screen(context,wall_mid)
                offp=self._safe_handle_screen(context,offp,self._screen(context,mid),dx=27.,dy=-18.) if offp is None or not (22.<offp[0]<region.width-22. and 22.<offp[1]<region.height-22.) else offp
                if offp:
                    _draw_circle(offp,10 if self._hover=='OFFSET' or self._drag_kind=='OFFSET' else 7,(0.38,0.74,1.0,0.94),2,True)
                    self._register_handle("OFFSET",offp,16.0,mid=mid,norm=norm)
                    self._spatial_anchors["W_WALL"]=offp
                if d.create_inner:
                    from ..production.wall_alignment import thickness_gizmo
                    control_shift,direction,sensitivity=thickness_gizmo(
                        self._physical_alignment(d),d.inset_distance,visual_side,True)
                    tp=mid+norm*(d.wall_offset+control_shift)
                    thp=self._screen(context,tp)
                    thp=self._safe_handle_screen(context,thp,self._screen(context,mid),dx=-27.,dy=24.) if thp is None or not (22.<thp[0]<region.width-22. and 22.<thp[1]<region.height-22.) else thp
                    if thp:
                        _draw_circle(thp,10 if self._hover=='THICKNESS' or self._drag_kind=='THICKNESS' else 7,(0.96,0.72,0.28,0.96),2.0,True)
                        self._register_handle("THICKNESS",thp,15.0,mid=tp,norm=norm*direction,
                                              sensitivity=sensitivity)
                        self._spatial_anchors["W_THICKNESS"]=thp
        # WALL: direct editing of the actual authored path vertex elevation.
        # No source Mesh mutation occurs during drag: LivePreview uses a
        # transient Z override, and the production operator owns the Undo step.
        if d.generator_type == "WALL" and lens == "BUILD":
            seen=set()
            for chain in analysis.get("simple_chains", ()):
                point_ids=chain.get("indices", ())
                world_points=chain.get("points", ())
                n=len(point_ids)
                # Preserve interactivity on imported high-density paths.
                stride=max(1, (n+127)//128)
                for count,(index,world) in enumerate(zip(point_ids,world_points)):
                    index=int(index)
                    if index in seen or (count % stride and index != self._wall_point_selected):
                        continue
                    seen.add(index)
                    if self._drag_kind == "PATH_Z" and self._drag_index == index and self._surface_drag_z is not None:
                        world=Vector((world.x,world.y,self._surface_drag_z))
                    pos=self._screen(context,world)
                    if pos is None:continue
                    selected=(index==self._wall_point_selected)
                    active=(self._hover==f"PATH_Z:{index}" or
                            (self._drag_kind=="PATH_Z" and self._drag_index==index))
                    color=(1.0,.73,.30,.99) if selected or active else (.36,.81,1.0,.88)
                    _draw_circle(pos,8 if selected or active else 5,color,2,True)
                    self._register_handle(f"PATH_Z:{index}",pos,12.0 if not selected else 15.0,
                                          anchor=world,vertex_index=index)
                    if selected or active:
                        original_z=original_height_for_point(path,d,index)
                        label_z=self._surface_drag_z if self._drag_kind=="PATH_Z" and self._drag_index==index else world.z
                        # Measured overlay: top line is absolute Z, second is
                        # a short interaction cue; tooltip never exits region.
                        scale=viewport_ui_scale(context)
                        popup_w=min(340.*scale,float(region.width)-16.*scale)
                        from .card_text_layout import wrap_lines
                        blf.size(0,max(8,int(9*scale)))
                        desc=('Исходная Z: нет сохранённой базы' if original_z is None else
                              f'Исходная {format_value(original_z)} · Δ {format_value(float(label_z)-original_z)} м')
                        lines=wrap_lines(desc,popup_w-20.,lambda v:blf.dimensions(0,v)[0])
                        popup_h=(87.+16.*len(lines))*scale
                        point_obstacles=[self._hud_rect,self._workspace_rect]
                        point_obstacles.extend(self._point_card_rects)
                        place=place_info_popup(pos,(popup_w,popup_h),(region.width,region.height),point_obstacles)
                        if place is None:
                            continue  # Point and its gizmo stay interactive; no overlapping popup.
                        tx,ty=place[0]+8*scale,place[1]+8*scale
                        self._point_card_rects.append(place)
                        _draw_editor_rounded(*place,(.023,.035,.043,.96),(.27,.57,.69,.96))
                        top=place[3]-12.*scale
                        _text(tx,top-4.*scale,_short_label(f"ТОЧКА {index:03d} · Z {format_value(label_z)} м",popup_w-20.*scale,10),10,(.94,.97,.99,1))
                        for line_id,line in enumerate(lines):
                            _text(tx,top-(25.+line_id*16.)*scale,line,9,(.67,.85,.86,1))
                        _text(tx,top-(26.+len(lines)*16.)*scale,
                              _short_label('Тяни · Shift 5 см · Ctrl 5 мм · Tab',popup_w-20.*scale,8),8,(.62,.81,.88,1))
                        self._button(f'FOCUS:W:{index}',tx,ty+30*scale,max(28.,popup_w-16*scale),22*scale,'Показать точку',False,True)
                        if original_z is not None and not math.isclose(float(label_z),original_z,abs_tol=1.e-7):
                            self._button(f'Z:RESET:{index}',tx,ty+2*scale,max(28.,popup_w-16*scale),22*scale,'Исходная высота',False,True)
        # Every card refers to a stable semantic location, never a polygon index.
        # Keep controls available when optional geometry is switched off.
        first_mid,first_dir,first_norm=self._first_segment_basis()
        if d.generator_type=="WALL":
            from .tool_eligibility import wall_tool_eligibility
            is_closed=any(bool(chain.get('closed')) for chain in analysis.get('simple_chains',()))
            eligibility=wall_tool_eligibility(closed=is_closed,double=d.create_inner,has_interior=True)
            if first_mid is not None and first_norm is not None:
                top_z=max(0.,(-1. if d.story_direction=="DOWN" else 1.)*total_wall_height(d))
                if eligibility['W_CAP']:
                    self._spatial_anchors["W_CAP"]=self._screen(context,first_mid+Vector((0,0,top_z)))
                self._spatial_anchors.setdefault("W_WALL",self._screen(context,first_mid))
                if not d.create_inner:
                    # SINGLE has no thickness gizmo. Keep a configuration card
                    # so the user can still switch the structure to DOUBLE.
                    self._spatial_anchors["W_THICKNESS"]=self._screen(
                        context,first_mid+Vector((0.0,0.0,0.15*total_wall_height(d))))
            if self._inside_analysis_ref is analysis:
                inside=self._inside_cache_xy
            else:
                inside=None
                for chain in analysis.get("simple_chains",[]):
                    if chain.get("closed"):
                        points=chain["points"][:-1]
                        if len(points)>=3:
                            inside=interior_xy([(p.x,p.y) for p in points])
                            if inside is not None:break
                self._inside_analysis_ref=analysis
                self._inside_cache_xy=inside
            eligibility=wall_tool_eligibility(closed=is_closed,double=d.create_inner,
                                               has_interior=inside is not None)
            if eligibility['W_PLANT']:
                top_z=max(0.,(-1. if d.story_direction=="DOWN" else 1.)*total_wall_height(d))
                self._spatial_anchors["W_PLANT"]=self._screen(context,Vector((inside[0],inside[1],centroid.z+top_z)))
            # Composition pass: semantic wall cards must NOT share the same
            # authored first-edge midpoint. Choose separated on-screen wall
            # segments, while preserving true handle anchors for drag controls.
            # This is presentation-only and never touches Definition or Mesh.
            top_shift=max(0.,(-1. if d.story_direction=="DOWN" else 1.)*total_wall_height(d))
            if self._composition_analysis_ref is not analysis:
                self._composition_analysis_ref=analysis
                self._composition_world_mids=semantic_edge_samples(
                    analysis.get("simple_chains",()),max_samples=40)
            world_mids=self._composition_world_mids or ()
            cap_candidates=[self._screen(context,mid+Vector((0,0,top_shift)))
                            for mid in world_mids]
            position_candidates=[self._screen(context,mid) for mid in world_mids]
            screen_size=(region.width,region.height)
            reserved=[self._spatial_anchors.get('W_HEIGHT'),
                      self._spatial_anchors.get('W_THICKNESS'),
                      self._spatial_anchors.get('W_PLANT')]
            old_cap=self._spatial_anchors.get('W_CAP')
            chosen_cap=choose_semantic_anchor(cap_candidates,reserved,screen_size,
                    previous=self._spatial_previous.get('W_CAP',(None,old_cap))[1])
            if chosen_cap is not None and eligibility['W_CAP']:
                self._spatial_anchors['W_CAP']=chosen_cap
            reserved.append(self._spatial_anchors.get('W_CAP'))
            old_wall=self._spatial_anchors.get('W_WALL')
            chosen_wall=choose_semantic_anchor(position_candidates,reserved,screen_size,
                    previous=self._spatial_previous.get('W_WALL',(None,old_wall))[1])
            if chosen_wall is not None:
                self._spatial_anchors['W_WALL']=chosen_wall
            labels={
                "W_HEIGHT":f"Высота  {format_value(total_wall_height(d))}",
                "W_THICKNESS":(f"Толщина  {format_value(d.inset_distance)}" if d.create_inner
                               else "Конструкция · Одинарная"),
                "W_WALL":("Положение · "+{'INSIDE':'Внутрь','CENTER':'Центр','OUTSIDE':'Наружу'}.get(d.wall_alignment,'Выберите')
                          if d.create_inner else
                          "Нормали · "+{'OUTWARD':'Наружу','INWARD':'Внутрь',
                                        'PATH_LEFT':'Влево','PATH_RIGHT':'Вправо'}.get(d.wall_normal_mode,'Выберите')),
                "W_CAP":"Крышка · Вкл" if d.create_top_cap else "Крышка · Выкл",
                "W_PLANT":"Клумба · Вкл" if d.create_inner_surface else "Клумба · Выкл",
            }
            if not eligibility['W_CAP']:
                labels.pop("W_CAP",None)
                self._spatial_anchors.pop('W_CAP',None)
            if not eligibility['W_PLANT']:
                labels.pop("W_PLANT",None)
                self._spatial_anchors.pop('W_PLANT',None)
        elif d.generator_type=="SURFACE":
            center_screen=self._screen(context,centroid)
            # Three separate, meaningful controls; the old generic
            # "Поверхность" card was redundant and hid useful contour space.
            candidates=[self._screen(context,mid) for mid in
                        semantic_edge_samples(analysis.get('simple_chains',()),max_samples=24)]
            candidates=[p for p in candidates if p is not None]
            self._spatial_anchors["S_Z"]=center_screen
            self._spatial_anchors["S_ROT"]=candidates[len(candidates)//4] if candidates else center_screen
            self._spatial_anchors["S_DIR"]=candidates[(len(candidates)*3)//4] if candidates else center_screen
            labels={"S_Z":f"Z  {format_value(d.surface_height_offset)}",
                    "S_ROT":f"Поворот  {format_value(d.surface_module_rotation)}",
                    "S_DIR":"Направление: вверх" if d.roof_normal=="UP" else "Направление: вниз"}
        else:
            center_screen=self._screen(context,centroid)
            seg_screen=self._screen(context,first_mid) if first_mid is not None else center_screen
            self._spatial_anchors.update(C_PROFILE=seg_screen, C_ROT=center_screen,
                                         C_SIDE=seg_screen)
            if self._setup_requirement(d) is None:
                labels={"C_PROFILE":"Профиль  "+getattr(d.cornice_profile, "name", "не назначен"),
                        "C_ROT":f"Поворот  {format_value(d.cornice_profile_rotation)}°",
                        "C_SIDE":"Сторона  "+("Обратная" if d.cornice_flip_side else "Прямая")}
            else:
                # Profile selection is intentionally exclusive to the guided card.
                labels={}
        self._draw_direction_arrows(context,d,first_mid,first_dir,first_norm)
        draw_surface_annotations(self, context, path, d, analysis, region, x, y,
                                 lens, _draw_circle, _text)
        self._draw_workspace(context,region,d,geo)
        # The field editors share the same authoritative Definition. A card
        # opens the existing 1.5.4 conflict-safe, undo-safe editor stack.
        # The same control is never shown as both an open editor and a
        # duplicate floating action card. Mode handles remain on the geometry.
        if self._window_stack is not None:
            editor_for_card={'W_HEIGHT':'HEIGHT','W_THICKNESS':'THICKNESS',
                'W_WALL':'PLACEMENT' if d.create_inner else 'NORMALS',
                'W_CAP':'CAP','W_PLANT':'PLANT','S_Z':'SURFACE',
                'S_ROT':'SURFACE','S_SETTINGS':'SURFACE',
                'C_PROFILE':'CORNICE','C_ROT':'CORNICE_ROT','C_SIDE':'CORNICE_SIDE'}
            active={w.kind for w in self._window_stack.windows if not w.layout_hidden}
            labels={k:v for k,v in labels.items() if editor_for_card.get(k) not in active}
        card_definitions=[]
        card_order={"W_HEIGHT":5,"W_THICKNESS":4,"W_CAP":3,"W_PLANT":2,
                    "W_WALL":1,"S_Z":5,"S_ROT":4,"S_DIR":3,"S_SETTINGS":2,
                    "C_PROFILE":6,"C_ROT":5,"C_SIDE":4,"C_SETTINGS":2}
        card_offsets={"W_HEIGHT":(22,20),"W_THICKNESS":(22,-22),
                      "W_CAP":(22,28),"W_PLANT":(22,-20),"W_WALL":(22,-58),
                      "S_Z":(22,22),"S_ROT":(22,-30),"S_DIR":(22,-58),"S_SETTINGS":(22,-90),
                      "C_PROFILE":(22,22),"C_ROT":(22,-26),"C_SIDE":(22,-64),
                      "C_SETTINGS":(22,-102)}
        for key,label in labels.items():
            anchor=self._spatial_anchors.get(key)
            if anchor is not None:
                card_definitions.append((key,anchor,(max(244 if key in {'W_CAP','W_PLANT'} else
                                                 226 if key in {'W_WALL','W_THICKNESS','C_SIDE','C_PROFILE'} else 148,
                                                min(256,20+len(label)*7.2)),32.),
                                         card_order[key],card_offsets[key]))
        occupied=[self._hud_rect]
        occupied.extend(getattr(self,'_point_card_rects',()))
        if self._workspace_rect is not None:
            occupied.append(self._workspace_rect)
        occupied.extend((pos[0]-12,pos[1]-12,pos[0]+12,pos[1]+12)
                        for _,pos,_,_ in self._handles)
        if self._window_stack is not None:
            occupied.extend(w.rect() for w in self._window_stack.windows if not w.layout_hidden)
            if self._window_stack.group_rect is not None:
                occupied.append(self._window_stack.group_rect)
        self._spatial_rects=layout_cards(card_definitions,(region.width,region.height),
                                        obstacles=occupied,previous=self._spatial_previous)
        self._spatial_previous={key:(rect,self._spatial_anchors[key])
                                for key,rect in self._spatial_rects.items()}
        for key,rect in self._spatial_rects.items():
            if self._radial:continue
            sx,sy,ex,ey=rect
            anchor=self._spatial_anchors[key]
            # Use a discreet connector only when collision avoidance pushes
            # a card unusually far from its geometric anchor.
            nx=max(sx,min(anchor[0],ex));ny=max(sy,min(anchor[1],ey))
            open_editors=tuple(w.kind for w in self._window_stack.windows) if self._window_stack else ()
            focused_editor=open_editors[-1] if open_editors else None
            state=card_attention(key, self._hover, open_editors, focused_editor,self._drag_kind)
            focus_marker=str(self._hover or '').split(':')[0]
            if focus_marker in labels and focus_marker!=key and not self._drag_kind:
                state='quiet'
            # A connector is explanatory, not a second visual focus: subdued
            # while another parameter is being edited, vivid on focus.
            distance=tether_distance(anchor,rect)
            if 34. < distance <= 165.:
                shader=gpu.shader.from_builtin("UNIFORM_COLOR")
                batch=batch_for_shader(shader,"LINES",{"pos":[anchor,(nx,ny)]})
                shader.bind();shader.uniform_float("color",
                    (.33,.84,.72,.65) if state in ('focused','hover') else (.28,.48,.44,.26))
                batch.draw(shader)
            split_hover=(self._hover==key or str(self._hover or '').startswith(key+':'))
            enabled_second=(d.create_top_cap if key=='W_CAP' else
                            d.create_inner_surface if key=='W_PLANT' else
                            d.create_inner if key=='W_THICKNESS' else True)
            zones=([] if key=='C_PROFILE' else
                   _universal.marker_zones(key,rect,split_hover,secondary_enabled=enabled_second))
            if zones:
                # Two semantic halves. Base rect + its original behaviour are
                # preserved while not hovered. Popup callbacks stay unchanged.
                self._button(key,sx,sy,ex-sx,ey-sy,'',False,True,visual_state=state)
                mx=zones[0][1][2] if len(zones)>1 else ex
                active_half=(':LEFT' if self._hover==key+':LEFT' else
                             ':RIGHT' if self._hover==key+':RIGHT' else '')
                if active_half:
                    hl=zones[0][1] if active_half==':LEFT' or len(zones)==1 else zones[1][1]
                    _draw_rect(hl[0]+2,sy+2,hl[2]-2,ey-2,(.22,.53,.44,.16))
                if len(zones)>1:
                    _draw_rect(mx-.85,sy+3,mx+.85,ey-3,(.38,.80,.69,.78))
                    # A constant split signature; do not depend on cursor hover.
                    _draw_rect(sx+3,sy+2,mx-3,sy+4,(.28,.54,.48,.26))
                left_caption,right_caption,_numeric_role=_precision.marker_presentation(
                    key,hovered=True,double=bool(d.create_inner),
                    cap=bool(d.create_top_cap),plant=bool(d.create_inner_surface))
                if key=='C_SIDE':left_caption,right_caption='Сторона','Опции'
                if len(zones)>1:
                    # Field background must be drawn BEFORE its label, never above it.
                    if _numeric_role:
                        self._draw_numeric_marker_field((mx+5,sy+4,ex-5,ey-4))
                    elif key in {'W_WALL','W_THICKNESS'}:
                        # Menu source is a visibly different surface from a numeric field.
                        _draw_editor_rounded(sx+4,sy+4,mx-5,ey-4,(.063,.096,.099,.94),(.20,.44,.39,.71))
                    # Sufficient horizontal spacing, well-defined menu vs numeric.
                    _text((sx+mx)*.5-4,sy+10,
                          _short_label(left_caption,(mx-sx)-26,10),10,(.84,.96,.91,1),'CENTER')
                    _text((mx+ex)*.5,sy+10,
                          _short_label(right_caption or 'Настройки',(ex-mx)-23,10),10,(.95,.99,.97,1),'CENTER')
                    if key in {'W_WALL','W_THICKNESS'}:
                        self._ui_stroke((mx-14,sy+10),(mx-11,sy+7),(.48,.87,.77,.9),1.4)
                        self._ui_stroke((mx-8,sy+10),(mx-11,sy+7),(.48,.87,.77,.9),1.4)
                else:
                    inactive=key in {'W_CAP','W_PLANT'} and not enabled_second
                    if inactive:
                        _draw_editor_rounded(sx+3,sy+3,ex-3,ey-3,(.035,.078,.074,.62),(.28,.63,.55,.40))
                    _text((sx+ex)*.5,sy+10,
                          _short_label(left_caption,ex-sx-20,10),10,
                          (.55,.79,.72,.79) if inactive else (.84,.96,.91,1),'CENTER')
                for sub_key,sub_rect in zones:
                    self._buttons.append((sub_key,sub_rect,True))
            else:
                caption=('Профиль · '+getattr(d.cornice_profile,'name','не назначен')
                         if key=='C_PROFILE' else labels[key])
                if key=='S_DIR':
                    # Vector arrow renders independently of font glyph support.
                    # Hit test is still the full unchanged card rectangle.
                    caption='Вверх' if d.roof_normal=='UP' else 'Вниз'
                self._button(key,sx,sy,ex-sx,ey-sy,caption,False,True,visual_state=state)
                if key=='S_DIR':
                    ax=sx+15.;ay=(sy+ey)*.5
                    delta=8. if d.roof_normal=='UP' else -8.
                    self._ui_stroke((ax,ay-delta),(ax,ay+delta),(.33,.91,.73,1.),2.2)
                    self._ui_stroke((ax-5,ay+delta*.45),(ax,ay+delta),(.33,.91,.73,1.),2.2)
                    self._ui_stroke((ax+5,ay+delta*.45),(ax,ay+delta),(.33,.91,.73,1.),2.2)
            # Blender-like spinner: a separate, visually evident drag stripe.
            # Height uses vertical dragging; both rotations use horizontal.
            if key in {'W_HEIGHT','S_Z','S_ROT','C_ROT'} and ex-sx>75:
                spin=(ex-23,sy+3,ex-3,ey-3)
                _draw_editor_rounded(*spin,(.11,.20,.21,.98),(.36,.76,.67,.88))
                if key in {'W_HEIGHT','S_Z'}:
                    self._ui_stroke((ex-13,sy+7),(ex-13,ey-7),(.8,.94,.90,1),1.6)
                    self._ui_stroke((ex-17,ey-11),(ex-13,ey-7),(.8,.94,.90,1),1.5)
                    self._ui_stroke((ex-9,ey-11),(ex-13,ey-7),(.8,.94,.90,1),1.5)
                    self._ui_stroke((ex-17,sy+11),(ex-13,sy+7),(.8,.94,.90,1),1.5)
                    self._ui_stroke((ex-9,sy+11),(ex-13,sy+7),(.8,.94,.90,1),1.5)
                else:
                    self._ui_stroke((ex-19,(sy+ey)*.5),(ex-7,(sy+ey)*.5),(.8,.94,.90,1),1.6)
                self._buttons.append(('SPIN:'+key,spin,True))
            self._draw_marker_glyph(key,sx+7,sy+6,focus=split_hover or self._inline_marker==key)
            # Focus guides the eye, without changing any clickable rect.
            if (self._numeric and getattr(self,'_inline_marker',None) != key or
                getattr(self,'_marker_choice',None) and self._marker_choice!=key):
                _draw_rect(sx+2,sy+2,ex-2,ey-2,(.0,.012,.016,.43))
        lens=context.window_manager.uvwall_viewport_lens
        panel_w=min(360.0,region.width-28.0)
        if lens=="LOOK":
            active_material, _legacy_image = virtual_asset_for_module(d)
            asset_state = inspect_material_asset(active_material, None, d.module_standard)
            active_image = asset_state.get("image")
            mat=active_material.name if active_material else "—"
            img=active_image.name if active_image else "НЕ НАЗНАЧЕНА"
            _draw_rect(x,y-126,x+panel_w,y-54,(0.03,0.038,0.045,0.90),(0.16,0.21,0.23,0.9))
            _text(x+14,y-78,f"Модуль {d.module_standard}×{d.module_standard}",11)
            _text(x+14,y-98,f"Material: {mat}",10,(0.65,0.72,0.76,1)); _text(x+14,y-114,f"Texture: {img}",10,(0.65,0.72,0.76,1))
            # Read-only derived resolution. Module selection never overrides Material.
        elif lens=="OPT":
            _draw_rect(x,y-112,x+panel_w,y-54,(0.03,0.038,0.045,0.90),(0.16,0.21,0.23,0.9))
            _text(x+14,y-77,"LIVE PREVIEW · object-free GPU geometry",10,(0.63,0.85,0.79,1))
            _text(x+14,y-96,f"Preview {geo.build_ms:.2f} ms · {geo.quality} · coalesced {self._engine.coalesced_updates} · rev {d.revision}",9,(0.65,0.72,0.76,1))
            cache_stats=SURFACE_GEOMETRY_CACHE.snapshot()
            _text(x+14,y-111,f"Surface cache {cache_stats['entries']} · hit {cache_stats['hits']} / miss {cache_stats['misses']}",8,(0.50,0.57,0.61,1))
        elif lens=="INSPECT":
            inspect_bottom=y-178 if getattr(d,"last_error","") else y-158
            _draw_rect(x,inspect_bottom,x+panel_w,y-54,(0.03,0.038,0.045,0.92),(0.16,0.21,0.23,0.9))
            status="OK" if analysis.get("simple_chains") else "INVALID"
            branch=len(analysis.get("branches",[])); iso=len(analysis.get("isolated",[])); crossings=len(analysis.get("intersections",[]))
            _text(x+14,y-76,f"PATH {status}",11,(0.35,0.94,0.79,1) if status=="OK" else (1.0,0.45,0.35,1))
            _text(x+14,y-95,f"Components {analysis.get('components',0)} · closed {analysis.get('closed_components',0)} · open {analysis.get('open_components',0)}",10,(0.65,0.72,0.76,1))
            _text(x+14,y-114,f"Branches {branch} · isolated {iso} · crossings {crossings}",10,(1.0,0.72,0.35,1) if branch or iso or crossings else (0.65,0.72,0.76,1))
            result=bpy.data.objects.get(d.last_result_name) if d.last_result_name else None
            if result is not None and result.type=="MESH":
                roles=role_counts(result.data); unknown=roles.get("UNKNOWN",0)
                role_summary=" · ".join(f"{name}:{count}" for name,count in roles.items() if count)
                _text(x+14,y-133,f"RESULT {len(result.data.polygons)}F · semantic {'OK' if unknown==0 else 'CHECK'}",9,(0.35,0.94,0.79,1) if unknown==0 else (1.0,0.65,0.34,1))
                _text(x+14,y-149,role_summary[:54],8,(0.50,0.57,0.61,1))
            else:
                _text(x+14,y-133,"RESULT not committed",9,(0.50,0.57,0.61,1))
            if getattr(d,"last_error",""):
                _text(x+14,y-169,"LAST FAILURE · "+str(d.last_error).replace("\n"," ")[:48],8,(1.0,.52,.42,1))

        if getattr(context.window_manager,"uvwall_preview_mode","MATERIAL")=="UV":
            size=min(112.0,max(72.0,region.width*.105))
            ux=max(8.0,min(region.width-size-8.0,x+hud_w+10.0))
            uy=max(8.0,y+66.0-size)
            _draw_rect(ux,uy,ux+size,uy+size,(.018,.027,.038,.94),(.24,.48,.62,.88))
            margin=10.0
            inner=size-2*margin
            step=inner/4.0
            for i in range(5):
                px=ux+margin+i*step; py=uy+margin+i*step
                _draw_rect(px,uy+margin,px+1,uy+size-margin,(.34,.70,.90,.16))
                _draw_rect(ux+margin,py,ux+size-margin,py+1,(.34,.70,.90,.16))
            _text(ux+8,uy+size-16,f"UV · {d.module_standard}×{d.module_standard} m",9,(.68,.86,.96,1))

        # Universal active Problem marker. It is a pure GPU overlay: no helper
        # objects, meshes or persistent datablocks are created.
        problem = active_problem(context.scene.uv_wall_settings)
        if problem is not None:
            # A problem may have no screen-projectable 3D location. The panel
            # still needs a defined color (the 1.8.1 draw failure was an
            # UnboundLocalError here, disabling ALL POST_PIXEL overlays).
            severity = str(getattr(problem, "severity", "WARNING"))
            color = (1.0,.30,.24,.96) if severity == "ERROR" else (1.0,.68,.20,.95)
            has_position=str(getattr(problem,'element_kind',''))!='BUILD'
            p2d = self._screen(context, Vector(problem.world_position)) if has_position else None
            if p2d is not None:
                pulse = 1.0 + 0.10 * math.sin(perf_counter()*5.0)
                _draw_circle(p2d, 12.0*pulse, color, 2.2, False)
                _draw_circle(p2d, 4.0, color, 1.0, True)
                _text(p2d[0]+17,p2d[1]+8,str(problem.title)[:42],10,color)
                _text(p2d[0]+17,p2d[1]-7,f"{context.scene.uv_wall_settings.active_problem_index+1}/{len(context.scene.uv_wall_settings.problems)}",8,(.82,.86,.87,1))
            nav_x,nav_y,nav_w=problem_inspector_layout(
                region.width,region.height,
                getattr(context.window_manager,'uvwall_display_open',False),(),viewport_ui_scale(context))
            from .card_text_layout import wrap_lines
            size=viewport_ui_scale(context)
            def _measurer(font):
                blf.size(0,max(8,int(font)))
                return lambda value:blf.dimensions(0,str(value))[0]
            wrap_w=max(35.,nav_w-23.*size)
            count=max(1,int(getattr(problem,'occurrence_count',1)))
            suffix=f' · ×{count}' if count>1 else ''
            identity=str(getattr(problem,'element_kind','') or '')
            indices=str(getattr(problem,'element_indices','') or '')
            location=f'{identity} · {indices}' if indices else ('Место точно не определено' if identity=='BUILD' else 'Расположение: см. маркер')
            sections=(
                (str(problem.title)+suffix,11,(.94,.97,.97,1),2),
                (location,9,(.65,.73,.75,1),2),
                (str(getattr(problem,'description','')),9,(.80,.85,.84,1),4),
                ('ДЕЙСТВИЕ: '+str(getattr(problem,'suggested_action','')),8,(.65,.86,.77,1),4),
            )
            lines=[]
            for message,sz,clr,max_lines in sections:
                if message.strip():
                    lines.append((wrap_lines(message,wrap_w,_measurer(sz),max_lines),sz,clr))
            row_step=14.*size
            full_rows=sum(len(part) for part,*_ in lines)
            wanted=75.*size+full_rows*row_step+6.*size*len(lines)
            available=float(region.height)-nav_y-9.
            nav_h=min(max(165.*size,wanted),available)
            problem_rect=(nav_x,nav_y,nav_x+nav_w,nav_y+nav_h)
            overlay_rect=display_panel_rect(region.width,region.height,viewport_ui_scale(context))
            display_collision=(getattr(context.window_manager,'uvwall_display_open',False)
                and self._window_stack._intersection_area(problem_rect,overlay_rect)>0)
            if (not context.scene.uv_wall_settings.problem_inspector_visible
                    or region.width<260 or nav_h<148.*size or display_collision):
                # A small visible badge is always recoverable; no old buttons survive.
                badge_w=min(155.*size,nav_w-8.)
                badge_y=min(nav_y+86.*size,float(region.height)-37.*size)
                self._problem_rect=(nav_x+nav_w-badge_w,badge_y,
                                    nav_x+nav_w,badge_y+27.*size)
                self._button('PROB_SHOW_PANEL',nav_x+nav_w-badge_w,badge_y,badge_w,27.*size,
                             f'Ошибки · {active_problem_count(context.scene.uv_wall_settings)}',False,True)
            else:
                self._problem_rect=problem_rect
                _draw_editor_rounded(nav_x,nav_y,nav_x+nav_w,nav_y+nav_h,(.025,.032,.038,.98),(.32,.39,.40,.96))
                _draw_rect(nav_x+8,nav_y+nav_h-3,nav_x+nav_w-8,nav_y+nav_h,color)
                name={'WALL':'СТЕНА','SURFACE':'ПОВЕРХНОСТЬ','CORNICE':'КАРНИЗ',
                      'PATH':'ПУТЬ','MATERIAL':'МАТЕРИАЛ','GEOMETRY':'ГЕОМЕТРИЯ'}.get(str(problem.subsystem).upper(),str(problem.subsystem))
                _text(nav_x+10.*size,nav_y+nav_h-22.*size,
                      _short_label(f'{name} · {context.scene.uv_wall_settings.active_problem_index+1}/{len(context.scene.uv_wall_settings.problems)}',nav_w-55.*size,9),9,color)
                self._button('PROB_HIDE',nav_x+nav_w-32.*size,nav_y+nav_h-30.*size,25.*size,23.*size,'×',False,True)
                line_y=nav_y+nav_h-44.*size
                min_y=nav_y+49.*size
                for part,sz,clr in lines:
                    for content in part:
                        if line_y<min_y:break
                        _text(nav_x+11.*size,line_y,content,sz,clr)
                        line_y-=row_step
                    line_y-=5.*size
                    if line_y<min_y:break
                # Footer navigation remains fixed even when description grows.
                can_fix=str(getattr(problem,'element_kind','')).startswith('CORNICE_VERTEX')
                gap=4.*size; bw=(nav_w-16.*size-3*gap)/4.
                self._button('PROB_PREV',nav_x+8.*size,nav_y+8.*size,bw,26.*size,'←',True,True)
                self._button('PROB_SHOW',nav_x+8.*size+bw+gap,nav_y+8.*size,bw,26.*size,'К месту',True,has_position)
                self._button('PROB_FIX',nav_x+8.*size+(bw+gap)*2,nav_y+8.*size,bw,26.*size,'Sharp',False,can_fix)
                self._button('PROB_NEXT',nav_x+8.*size+(bw+gap)*3,nav_y+8.*size,bw,26.*size,'→',True,True)

        if self._numeric and getattr(self,'_inline_marker',None) in self._spatial_rects:
            rect=self._spatial_rects[self._inline_marker]
            nx,ny,ex,ey=rect
            _draw_editor_rounded(nx,ny,ex,ey,(.028,.077,.073,.99),(.27,.90,.74,1.))
            edit=getattr(self,'_numeric_editor',None)
            if edit is not None and edit.selected:
                try:
                    blf.size(0,12)
                    a,b=edit.span
                    left=nx+11+blf.dimensions(0,edit.text[:a])[0]
                    right=nx+11+blf.dimensions(0,edit.text[:b])[0]
                    _draw_rect(max(nx+8,left),ny+5,min(ex-8,right),ey-5,(.16,.43,.38,.62)) if right>left else None
                except (ValueError,RuntimeError,AttributeError):pass
            _text(nx+11,ny+11,_short_label(self._numeric_buffer or '—',ex-nx-25,12),12,(.97,1.,.98,1))
            if edit is not None and not edit.selected:
                try:
                    blf.size(0,12)
                    cx=min(ex-11,nx+11+blf.dimensions(0,edit.text[:edit.caret])[0])
                    self._ui_stroke((cx,ny+6),(cx,ey-6),(.97,.99,.99,.96),1.2)
                except (ValueError,RuntimeError):pass
        elif self._numeric:
            nx,ny=self._last_mouse
            popup_width=min(172.,region.width-16.)
            nx=max(8.,min(nx+18.,region.width-popup_width-8.))
            ny=max(8.,min(ny+18.,region.height-60.))
            _draw_rect(nx,ny,nx+popup_width,ny+52,(0.02,0.028,0.034,0.97),(0.30,0.90,0.76,0.95))
            label={"HEIGHT":"Высота", "BOTTOM_LEVEL":"Низ", "THICKNESS":"Толщина", "OFFSET":"Положение", "SURFACE_Z":"Z"}.get(self._numeric_kind,"Значение")
            _text(nx+10,ny+36,_short_label(label,popup_width-18.,9),9,(0.55,0.66,0.70,1))
            _text(nx+10,ny+14,_short_label((self._numeric_buffer or "0"),popup_width-18.,14),14,(0.96,0.99,0.99,1))
        if (getattr(self,'_rotation_phase','') and getattr(self,'_spin_state',None)) or (
                self._numeric and self._numeric_kind in {'SURFACE_ROT','CORNICE_ROT'}):
            phase=self._rotation_phase or 'PREVIEW'
            state=getattr(self,'_spin_state',{}) or {}
            if not state and self._numeric:
                state={'kind':self._numeric_kind,
                    'value':getattr(d,'surface_module_rotation' if self._numeric_kind=='SURFACE_ROT'
                                    else 'cornice_profile_rotation',0.0)}
            kind=state.get('kind','')
            if kind in {'SURFACE_ROT','CORNICE_ROT'}:
                degree=state.get('value',state.get('initial',0.))
                text=f'ПРЕДПРОСМОТР · {degree:.2f}° · Проверка после отпускания'
                panel_w=min(340.,region.width-18.)
                px=max(9.,(region.width-panel_w)*.5);py=region.height-102.
                _draw_editor_rounded(px,py,px+panel_w,py+30,(.025,.066,.062,.98),(.31,.86,.71,.95))
                _text(px+11,py+10,_short_label(text,panel_w-20,10),10,(.88,.99,.95,1.))
        # A hovered handle pulses red but remains fully selectable with its
        # existing stable hitbox. Draw at the actual/projected visible point.
        if self._hover and self._handles:
            for key,position,radius,_metadata in self._handles:
                if key==self._hover:
                    pulse=hover_pulse(perf_counter())
                    hover_rgb=tuple(getattr(context.scene.uv_wall_settings,'viewport_hover_color',(1.,.20,.17)))
                    _draw_circle(position,max(10.,radius*.88),(*hover_rgb,pulse),2.4,False)
                    _draw_circle(position,max(4.,radius*.45),(*hover_rgb,pulse*.37),1.5,True)
                    break
        self._draw_corner_tools(context,path,d)
        # Floating editors are below Q; Q is a single opaque modal interaction.
        if self._window_stack is not None and not self._radial:
            window_obstacles=list(blender_chrome_obstacles(region.width,region.height,viewport_ui_scale(context)))
            if self._workspace_rect is not None:window_obstacles.append(self._workspace_rect)
            if self._hud_rect is not None:window_obstacles.append(self._hud_rect)
            if self._missing_resources_rect is not None:window_obstacles.append(self._missing_resources_rect)
            if self._problem_rect is not None:window_obstacles.append(self._problem_rect)
            if getattr(context.window_manager,'uvwall_display_open',False):
                window_obstacles.append(display_panel_rect(region.width,region.height,viewport_ui_scale(context)))
            # Viewport Awareness: protect the active projected handle (and
            # currently edited semantic marker) from the right dock whenever
            # the viewport has room. This is a SOFT preference; UI obstacles
            # remain strict. We never mutate model geometry or pointer routes.
            from issmaker_modeling_assistant.core.context_workspace import project_focus_rects
            focus_points=[]
            if self._hover:
                focus_points.extend(p for key,p,*_ in self._handles if key==self._hover and p is not None)
            if self._window_stack.windows and not focus_points:
                active_kind=self._window_stack.windows[-1].kind
                semantic={'HEIGHT':'W_HEIGHT','THICKNESS':'W_THICKNESS',
                          'PLACEMENT':'W_WALL','NORMALS':'W_WALL','CAP':'W_CAP',
                          'PLANT':'W_PLANT','SURFACE':'S_Z','CORNICE':'C_PROFILE',
                          'CORNICE_ROT':'C_ROT','CORNICE_SIDE':'C_SIDE'}.get(active_kind)
                marker=self._spatial_anchors.get(semantic) if semantic else None
                if marker is not None:focus_points.append(marker)
            protected=project_focus_rects((region.width,region.height),focus_points,
                                           radius=90*viewport_ui_scale(context),max_regions=1)
            self._window_stack.draw((region.width,region.height),_draw_rect,_text,
                                    getattr(context.window_manager,"uvwall_visual_effect","GRAPHITE"),
                                    obstacles=window_obstacles,ui_scale=viewport_ui_scale(context),
                                    scope=window_visibility.scope_from_context(context,region=region),
                                    focus_areas=protected,soft_rect=_draw_editor_rounded,
                                    stroke_line=self._ui_stroke)
        if self._radial and self._radial_center:
            rx,ry,rw,rh=floating_quick_rect(self._radial_center,(region.width,region.height))
            self._quick_rect=(rx,ry,rw,rh)
            _draw_rect(rx,ry,rx+rw,ry+rh,(.025,.035,.044,.995),(.28,.66,.56,1.))
            _text(rx+12,ry+rh-24,'Быстрый выбор · Q',11,(.94,.98,.97,1))
            gap=6.; margin=10.; tab=(rw-2*margin-2*gap)/3.
            for idx,(mode,label) in enumerate((('WALL','Стена'),('SURFACE','Поверхность'),('CORNICE','Карниз'))):
                bx=rx+margin+idx*(tab+gap)
                selected=(self._radial_choice==idx or (self._radial_choice is None and d.generator_type==mode))
                _draw_rect(bx,ry+12,bx+tab,ry+55,
                           (.04,.24,.20,1.) if selected else (.10,.13,.15,1.),
                           (.28,.92,.73,1.) if selected else (.24,.32,.33,1.))
                _text(bx+tab*.5,ry+29,_short_label(label,tab-8,10),10,(.96,.99,.99,1),'CENTER')
        if not self._radial:
            self._draw_picker_dropdown(context,region)
            self._draw_marker_choice_dropdown(region)
            self._draw_hover_tooltip(region,d)
            self._draw_precision_feedback(context,region)
        self._draw_display_controls(context,region)
        self._draw_toast(region)
        prefs=get_preferences(context)
        if _ui_profile(prefs).debug:
            # The favorite star is a deliberate child inside a dock row.
            peer=[(key,rect) for key,rect,enabled in self._buttons
                  if enabled and not str(key).startswith('DOCK:FAVORITE:')]
            _record_ui_layout('UV_WALL',region.width,region.height,peer)
            for key,rect in peer[:250]:
                try:
                    a,b,c,d=rect
                    bad=a<0 or b<0 or c>region.width or d>region.height
                    _draw_rect(a,b,c,d,(0.,0.,0.,0.),
                               (1.,.27,.27,.84) if bad else (.46,.83,.73,.37))
                except (TypeError,ValueError,RuntimeError):
                    continue

    def _dock_field_row(self, key, x, y, w, h, label, raw, kind):
        """Draw a clearly-addressed, two-column inspector control.

        Draw and hit testing share the same bounds.  Separate key identity
        prevents cap/top and bottom-cap resource fields being interchanged.
        """
        bounds=(x,y,x+w,y+h)
        mouse=getattr(self,'_last_mouse',(-10000.,-10000.))
        hovered=_rect_hit(mouse[0],mouse[1],bounds)
        if kind=='TOGGLE':
            value='ВКЛ' if bool(raw) else 'ВЫКЛ'
        elif kind=='RESOURCE':
            value=getattr(raw,'name','Не выбран') if raw is not None else 'Не выбран'
        elif isinstance(raw,float):
            value=('%0.3f' % raw).rstrip('0').rstrip('.')
        elif isinstance(raw,bool):
            value='ВКЛ' if raw else 'ВЫКЛ'
        else:
            value=str(raw) if raw is not None else '—'
        theme=_iss_theme.from_preferences(get_preferences())
        field_state='hover' if hovered else 'normal'
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component(bounds,part='field',state=field_state,theme=theme):
            visual=_viewport_skin.component('field',field_state,theme=theme)
            _draw_rect(x,y,x+w,y+h,visual.background,visual.border)
        left_width=w*.55
        _text(x+12,y+(h-10)*.5,
              _short_label(label,left_width-18.,_viewport_skin.text_px('control')),
              _viewport_skin.text_px('control'),_viewport_skin.ink(theme))
        tag_w=max(44.,min(w*.36,w-left_width-38.))
        tag_x=x+w-tag_w-31.
        if kind=='TOGGLE':
            tag_state='active' if bool(raw) else 'quiet'
            tag_role='primary' if bool(raw) else 'neutral'
        elif kind=='RESOURCE' and raw is None:
            tag_state='normal'; tag_role='warning'
        else:
            tag_state='normal'; tag_role='neutral'
        tag_rect=(tag_x,y+4.,x+w-28.,y+h-4.)
        tag_visual=_viewport_skin.component('tab',tag_state,tag_role,theme)
        if not draw_component(tag_rect,part='tab',state=tag_state,
                              role=tag_role,theme=theme):
            _draw_rect(*tag_rect,tag_visual.background,tag_visual.border)
        tag_fg=_viewport_skin.control_ink(theme,part='tab',state=tag_state,role=tag_role)
        tag_font=_viewport_skin.text_px('caption')
        _text(tag_x+(tag_w-3)*.5,y+(h-tag_font)*.5,
              _short_label(value,tag_w-10.,tag_font),tag_font,tag_fg,'CENTER')
        self._buttons.append((key,bounds,True))
        # One small star hitbox per field, after full-row target for priority.
        fav_key=key[5:] if key.startswith('DOCK:') else key
        star=(x+w-27.,y+4.,x+w-3.,y+h-4.)
        is_fav=fav_key in (getattr(self,'_dock_favorites',()) or ())
        _text(x+w-15.,y+(h-10)*.5,'★' if is_fav else '☆',12,
              (.96,.76,.32,1.) if is_fav else (.50,.61,.63,1.),'CENTER')
        self._buttons.append(('DOCK:FAVORITE:'+fav_key,star,True))

    def _is_closed_path(self):
        try:
            return any(bool(c.get('closed')) for c in self._analysis().get('simple_chains',()))
        except (AttributeError,ReferenceError,RuntimeError,ValueError,TypeError):
            return False

    def _draw_studio_dock(self,context,region,definition):
        """Viewport-native inspector: one owned visual + interaction surface."""
        self._dock_region_size=(int(region.width),int(region.height))
        self._dock_drawn_region_size=self._dock_region_size
        self._dock_list_bounds=None;self._dock_scroll_max=0
        prefs=get_preferences(context)
        mode=definition.generator_type
        collapsed=safe_strings(read_map(getattr(prefs,'dock_sections_json','{}')),mode)
        favorites=safe_strings(read_map(getattr(prefs,'dock_favorites_json','{}')),mode,
                               {k for k,_ in dock_row_visibility(mode,definition,path_closed=self._is_closed_path()) if k!='SECTION'})
        rows=composed_rows(mode,definition,collapsed,favorites,path_closed=self._is_closed_path())
        self._dock_active_rows=rows
        self._dock_collapsed_sections=collapsed
        self._dock_favorites=favorites
        scale=viewport_ui_scale(context)
        rect=content_dock_rect(region.width,region.height,scale,rows,
                               getattr(prefs,'dock_sizing','AUTO'),
                               getattr(prefs,'dock_manual_width',350),
                               getattr(prefs,'dock_manual_height',620))
        if rect is None:
            window_visibility.observe(window_visibility.scope_from_context(context,region=region),
                                      'UV_WALL_STUDIO_DOCK',None)
            self._dock_rectangle=None
            self._workspace_rect=None
            return
        x,y,w,h=rect
        # Smooth resizing remains bounded and anchored to same upper edge.
        if _iss_theme.from_preferences(prefs).motion!='OFF' and h>=344*scale:
            previous=getattr(self,'_dock_height_previous',None)
            animated=smoothing_step(previous,h,.42)
            if abs(h-animated)>.8 and context.area is not None:
                context.area.tag_redraw()
            animated=min(animated,content_dock_rect(region.width,region.height,scale,rows,
                               'MANUAL',getattr(prefs,'dock_manual_width',350),1100)[3])
            y+=h-animated
            h=animated
        # Strict placement shared with DWOS, Brick Hole and Collision HUD.
        # The source Dock rectangle is a preference, never permission to cover
        # another committed viewport window or Blender's navigation chrome.
        dock_scope=window_visibility.scope_from_context(context,region=region)
        dock_key='UV_WALL_STUDIO_DOCK'
        badge_only=bool(self._dock_collapsed or h<344*scale or w<280*scale)
        occupied_height=min(h,48.*scale) if badge_only else h
        placed=place_unobstructed(
            (region.width,region.height),dock_key,(w,occupied_height),(x,y+h-occupied_height),
            blockers=blender_chrome_obstacles(region.width,region.height,scale),
            margin=8.,gap=6.,scope=dock_scope)
        auto_compact=False
        if placed is None and not badge_only:
            # Graceful non-interactive badge when other windows occupy the Dock.
            # No full-window hitboxes are created for a panel we did not draw.
            occupied_height=min(h,48.*scale)
            placed=place_unobstructed(
                (region.width,region.height),dock_key,(w,occupied_height),
                (x,y+h-occupied_height),
                blockers=blender_chrome_obstacles(region.width,region.height,scale),
                margin=8.,gap=6.,scope=dock_scope)
            auto_compact=placed is not None
        if placed is None:
            # Fail closed: hidden windows cannot retain clickable old rows.
            self._dock_rectangle=None
            self._dock_list_bounds=None
            self._workspace_rect=None
            self._dock_scroll_max=0
            return
        badge_only=badge_only or auto_compact
        x,y=placed[0],placed[1]
        self._dock_height_previous=h
        self._dock_rectangle=(x,y,placed[2],placed[3])
        theme=_ui_theme(context)
        panel,border,header,accent=theme['panel'],theme['border'],theme['header'],theme['accent']
        if badge_only:
            badge_h=occupied_height
            self._dock_rectangle=(x,y,x+w,y+badge_h)
            _draw_editor_rounded(*self._dock_rectangle,panel,border)
            _draw_rect(x,y+badge_h-3.,x+w,y+badge_h,accent)
            too_short=auto_compact or h<344*scale or w<280*scale
            if too_short:
                # No fake Expand hitbox: an expanded inspector could never fit
                # into this viewport. It will recover automatically on resize.
                message=_short_label('UV WALL · Нужна свободная область Viewport',max(0.,w-18.),11)
                _text(x+9.,y+max(12.,(badge_h-8.)*.5),message,11,theme['subtitle'])
            else:
                self._button('DOCK:EXPAND',x+8.,y+7.,w-16.,max(20.,badge_h-14.),
                             'UV WALL  ·  Развернуть',False,True)
            return
        _draw_rect(x-4,y-4,x+w+4,y+h+4,(.006,.012,.016,.45))
        _draw_editor_rounded(x,y,x+w,y+h,panel,border)
        from issmaker_modeling_assistant.core import theme_engine
        from issmaker_modeling_assistant.core.studio_gpu_components import shell_background
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        shared=theme_engine.from_preferences(addon.preferences if addon else None)
        shell_background(shared,(x,y,x+w,y+h))
        top=y+h
        header_h=45.*scale
        _draw_rect(x+1,top-header_h,x+w-1,top-1,header)
        _draw_rect(x+1,top-3,x+w-1,top,accent)
        _text(x+14.,top-27.*scale,'UV WALL',_viewport_skin.text_px('title'),_viewport_skin.ink(shared))
        _text(x+100.*scale,top-26.*scale,'STUDIO',_viewport_skin.text_px('caption'),_viewport_skin.ink(shared,kind='muted'))
        self._button('DOCK:PRESETS',x+w-141.*scale,top-36.*scale,90.*scale,26.*scale,'Пресеты',False,True)
        self._button('DOCK:COLLAPSE',x+w-42.*scale,top-36.*scale,29.*scale,26.*scale,'−',False,True)
        # Consistent segmented control; selected mode is readable at a glance.
        modes=(('WALL','Стена'),('SURFACE','Плоскость'),('CORNICE','Карниз'))
        gap=5.*scale;mw=(w-28.*scale-2*gap)/3.
        for i,(kind,label) in enumerate(modes):
            self._button('DOCK:MODE:'+kind,x+14.*scale+i*(mw+gap),top-87.*scale,mw,30.*scale,
                         label,definition.generator_type==kind,True)
        _text(x+14.,top-111.*scale,'ИСТОЧНИК',10,_viewport_skin.ink(shared,kind='muted'))
        path=self._path()
        path_name=getattr(path,'name','Не выбран')
        _text(x+14.,top-129.*scale,_short_label(path_name,w-108.*scale,11),11,_viewport_skin.ink(shared))
        self._button('RP:PATH:OPEN',x+w-94.*scale,top-135.*scale,79.*scale,28.*scale,'Сменить',False,True)
        state,state_color=_result_state(definition)
        _draw_rect(x+13,top-168.*scale,x+w-13,top-143.*scale,
                   _viewport_skin.component('card',theme=shared).background)
        _draw_rect(x+15,top-166.*scale,x+18,top-145.*scale,state_color)
        _text(x+26.,top-161.*scale,'RESULT · '+_short_label(state,w-62.,10),10,_viewport_skin.ink(shared))
        # A compact asset toolbar, fixed above scrolling fields.
        gap=7.*scale
        half=(w-28.*scale-gap)*.5
        no_material=current_resource('MATERIAL',self._path(),definition) is None
        self._button('RP:MATERIAL:OPEN',x+14.*scale,top-205.*scale,w-28.*scale,29.*scale,
                     'Материал: нет' if no_material else 'Материал',False,True)
        list_top=top-219.*scale
        footer_h=83.*scale
        footer_top=y+footer_h
        line_h=36.*scale
        available=max(0.,list_top-footer_top)
        capacity=max(0,int(available//line_h))
        visible,scroll,max_scroll=visible_dock_rows(rows,self._dock_scroll,capacity)
        self._dock_scroll=scroll;self._dock_scroll_max=max_scroll
        self._dock_list_bounds=(x+11*scale,footer_top,x+w-11*scale,list_top)
        if capacity<1:
            _text(x+13,footer_top+14.,'Увеличьте высоту области 3D View',10,_viewport_skin.ink(shared,kind='muted'))
        for i,(key,label) in enumerate(visible):
            row_top=list_top-i*line_h
            row_y=row_top-line_h+4.*scale
            if key=='SECTION':
                _draw_rect(x+11*scale,row_y+2*scale,x+w-11*scale,row_y+line_h-5*scale,
                           _viewport_skin.component('header',theme=shared).background)
                _draw_rect(x+11*scale,row_y+2*scale,x+14*scale,row_y+line_h-5*scale,accent)
                marker='▸' if label in collapsed else '▾'
                _text(x+19.*scale,row_y+11*scale,_short_label(marker+'  '+label,w-42.*scale,10),10,_viewport_skin.ink(shared,kind='muted'))
                if label!='ИЗБРАННОЕ':
                    self._buttons.append(('DOCK:SECTION:'+label,
                                          (x+11*scale,row_y+2*scale,x+w-11*scale,row_y+line_h-5*scale),True))
                continue
            field=key.split(':',1)[1]
            raw=getattr(definition,field,None)
            self._dock_field_row('DOCK:'+key,x+13*scale,row_y,w-26*scale,line_h-6*scale,label,raw,key.split(':',1)[0])
        if max_scroll>0 and available>=line_h:
            # Thin progress track — passive, cannot swallow pointer events.
            track_x=x+w-8*scale;track_top=list_top-4*scale
            track_bottom=footer_top+5*scale;track_height=track_top-track_bottom
            if track_height>12*scale:
                _draw_rect(track_x,track_bottom,track_x+2*scale,track_top,(.12,.18,.19,.68))
                thumb_h=max(12*scale,track_height*(capacity/max(1,len(rows))))
                thumb_y=track_top-thumb_h-(scroll/max_scroll)*(track_height-thumb_h)
                _draw_rect(track_x-1*scale,thumb_y,track_x+3*scale,thumb_y+thumb_h,accent)
        # Never move/scroll the primary actions; always use same bounds.
        _draw_rect(x+8*scale,y+7*scale,x+w-8*scale,footer_top-4*scale,
                   _viewport_skin.component('card',theme=shared).background)
        self._button('DOCK:BUILD',x+13*scale,y+43*scale,w-26*scale,29*scale,
                     '✓  ПОСТРОИТЬ / ПРИМЕНИТЬ',True,True)
        left=(w-32*scale)*.55
        self._button('DOCK:COLOR',x+13*scale,y+13*scale,left,25*scale,'Палитра цвета',False,True)
        count=active_problem_count(context.scene.uv_wall_settings)
        self._button('DOCK:PROBLEMS',x+18*scale+left,y+13*scale,w-left-31*scale,25*scale,
                     'Проверка'+(' · '+str(count) if count else ''),False,True)
        # Color preview shows the actual selected hue; still opens native HSV wheel.
        rgb=tuple(float(v) for v in context.scene.uv_wall_settings.viewport_hover_color)
        _draw_rect(x+17*scale,y+19*scale,x+27*scale,y+32*scale,(*rgb,1.),(.85,.93,.90,.94))
        self._workspace_rect=self._dock_rectangle
        self._draw_toast(region)
        if self._resource_picker is not None and (self._resource_picker.open_kind or self._resource_picker.viewport_kind):
            self._draw_picker_dropdown(context,region)

    def _handle_studio_dock_event(self,context,event,x,y,region):
        """Hitboxes only belong to the most recently drawn valid dock geometry."""
        current_size=(int(region.width),int(region.height))
        if self._dock_drawn_region_size != current_size:
            # Do not apply the old pixel hitbox to a newly resized viewport.
            # Redraw and allow Blender navigation while layout catches up.
            if context.area is not None:context.area.tag_redraw()
            return None
        picker=self._resource_picker
        if (picker is not None and picker.open_kind
                and not window_visibility.is_hidden(window_visibility.scope_from_context(context,region=region),
                                                     'UV_WALL_RESOURCE_PICKER')):
            rows=self._resource_rows(picker.open_kind)
            if event.value=='PRESS':
                if event.type in {'ESC','RIGHTMOUSE'}:
                    picker.close_dropdown();return {'RUNNING_MODAL'}
                if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:
                    delta=-1 if event.type=='WHEELUPMOUSE' else 1
                    picker.active_index=max(0,min(max(0,len(rows)-1),picker.active_index+delta))
                    return {'RUNNING_MODAL'}
                if event.type in {'UP_ARROW','DOWN_ARROW'}:
                    delta=-1 if event.type=='UP_ARROW' else 1
                    picker.active_index=max(0,min(max(0,len(rows)-1),picker.active_index+delta))
                    return {'RUNNING_MODAL'}
                if event.type in {'RET','NUMPAD_ENTER'}:
                    if rows and 0<=picker.active_index<len(rows):
                        self._apply_resource(picker.open_kind,rows[picker.active_index][0],context)
                    return {'RUNNING_MODAL'}
                if event.type=='BACK_SPACE':
                    picker.query=picker.query[:-1];return {'RUNNING_MODAL'}
                char=getattr(event,'unicode','') or ''
                if char and char.isprintable() and not event.ctrl and not event.alt:
                    picker.query+=char;picker.active_index=0;return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value=='PRESS':
                key=self._hit_button(x,y)
                if key and key.startswith('RP:'):
                    self._apply_workspace_button(key,context)
                else:picker.close_dropdown()
                return {'RUNNING_MODAL'}
            return None
        rect=self._dock_rectangle
        if rect is None or not _rect_hit(x,y,rect):
            if event.type=='LEFTMOUSE' and event.value=='PRESS':
                handle=self._hit_handle(x,y)
            return None
        if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'} and event.value=='PRESS':
            # Scrolling the inspector must never zoom the underlying geometry.
            # But rows only change if there are actually hidden rows.
            bounds=self._dock_list_bounds
            if (bounds is not None and _rect_hit(x,y,bounds) and self._dock_scroll_max>0):
                delta=-2 if event.type=='WHEELUPMOUSE' else 2
                self._dock_scroll=max(0,min(self._dock_scroll_max,self._dock_scroll+delta))
            return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            key=self._hit_button(x,y)
            if key:self._execute_studio_dock_button(key,context)
            return {'RUNNING_MODAL'}
        # Never let clicking empty inspector space select hidden geometry.
        if event.type in {'LEFTMOUSE','RIGHTMOUSE'}:return {'RUNNING_MODAL'}
        return None

    def _execute_studio_dock_button(self,key,context):
        if key=='DOCK:COLLAPSE':self._dock_collapsed=True;return
        if key=='DOCK:EXPAND':self._dock_collapsed=False;return
        if key=='DOCK:PRESETS':
            bpy.ops.uvwall.dock_presets('INVOKE_DEFAULT');return
        if key.startswith('DOCK:SECTION:'):
            prefs=get_preferences(context)
            if prefs is not None:
                mode=definition_for(self._path()).generator_type
                content=read_map(getattr(prefs,'dock_sections_json','{}'))
                values=set(safe_strings(content,mode))
                name=key[len('DOCK:SECTION:'):]
                if name in values:values.remove(name)
                else:values.add(name)
                content[mode]=sorted(values)
                prefs.dock_sections_json=write_map(content)
                self._dock_scroll=0;self._dock_drawn_region_size=None
            return
        if key.startswith('DOCK:FAVORITE:'):
            prefs=get_preferences(context)
            if prefs is not None:
                mode=definition_for(self._path()).generator_type
                valid={k for k,_ in dock_row_visibility(mode,definition_for(self._path()),path_closed=self._is_closed_path()) if k!='SECTION'}
                field=key[len('DOCK:FAVORITE:'):]
                if field in valid:
                    content=read_map(getattr(prefs,'dock_favorites_json','{}'))
                    values=list(safe_strings(content,mode,valid))
                    if field in values:values.remove(field)
                    else:values.append(field)
                    content[mode]=values[:48]
                    prefs.dock_favorites_json=write_map(content)
                    self._dock_scroll=0;self._dock_drawn_region_size=None
            return
        if key.startswith('DOCK:MODE:'):
            target=key.rsplit(':',1)[1]
            try:bpy.ops.uvwall.studio_set_mode('EXEC_DEFAULT',mode=target)
            except (RuntimeError,ValueError) as exc:self._toast(str(exc),'ERROR')
            self._dock_scroll=0
            return
        if key=='DOCK:COLOR':
            bpy.ops.uvwall.hover_palette('INVOKE_DEFAULT');return
        if key=='DOCK:BUILD':
            self._apply_workspace_button('BUILD',context);return
        if key=='DOCK:PROBLEMS':
            self._apply_workspace_button('PROBLEMS_REFRESH',context);return
        if key.startswith('DOCK:TOGGLE:'):
            bpy.ops.uvwall.dock_switch('EXEC_DEFAULT',field=key.split(':',2)[2]);return
        if key.startswith('DOCK:EDIT:'):
            field=key.split(':',2)[2]
            if field in {'module_standard','wall_alignment','roof_normal','story_direction','inset_side','surface_method','cornice_uv_mode'}:
                bpy.ops.uvwall.dock_switch('EXEC_DEFAULT',field=field)
            else:bpy.ops.uvwall.dock_field('INVOKE_DEFAULT',field=field)
            return
        if key.startswith('DOCK:RESOURCE:'):
            bpy.ops.uvwall.dock_resource('INVOKE_DEFAULT',field=key.split(':',2)[2]);return
        if key.startswith('RP:'):
            self._apply_workspace_button(key,context)

    def _draw_toast(self,region):
        if not self._toast_text or perf_counter()>self._toast_until:return
        width=min(420.,max(180.,region.width-30.));x=(region.width-width)*.5
        # Notifications use a collision-aware safe zone. Start near the top,
        # then move above/below the workspace if its expanded drawer owns that
        # area. This prevents success/error messages from hiding the bottom HUD.
        y=max(8.,float(region.height)-176.)
        manager=self._picker_allocator(bpy.context,region)
        if self._picker_rect is not None:manager.reserve('RESOURCE_PICKER',self._picker_rect)
        placed=manager.place('TOAST',(width,38.),(x,y))
        if placed is None:return
        x,y=placed[0],placed[1]
        colors={'SUCCESS':(.22,.82,.63,1),'WARNING':(.98,.67,.24,1),'ERROR':(1.,.27,.22,1),'INFO':(.40,.72,.83,1)}
        border=colors.get(self._toast_level,colors['INFO'])
        _draw_rect(x,y,x+width,y+38,(.018,.028,.033,.96),border)
        _text(x+width*.5,y+13,_short_label(self._toast_text,width-24,10),10,(.94,.98,.97,1),'CENTER')

    def _draw_display_controls(self,context,region):
        """Only functional per-tool toggles; the theme is global ISSMAKER.

        Do not create legacy theme/effect hitboxes. Position rows from the panel
        bounds so compact layouts cannot overlap search, palette or close.
        """
        wm=context.window_manager
        if not getattr(wm,'uvwall_display_open',False):return
        x,y,x1,y1=display_panel_rect(region.width,region.height,viewport_ui_scale(context))
        w=x1-x;h=y1-y
        theme=_ui_theme(context)
        _draw_rect(x-3,y-3,x+w+3,y+h+3,(.010,.015,.018,.5))
        _draw_rect(x,y,x+w,y+h,theme['panel'],theme['border'])
        _draw_rect(x,y+h-3,x+w,y+h,theme['accent'])
        _text(x+13,y+h-26,'ВИД · ISSMAKER',12,(.92,.98,.96,1))
        self._button('DISPLAY:TOGGLE',x+w-37,y+h-36,26,26,'×',False,True)
        if h>=85.:
            _text(x+12,y+h-55,_short_label('Тема и фон: настройки ISSMAKER',w-24,9),9,theme['subtitle'])
        controls=(
            ('DISPLAY:ARROWS','Стрелки · '+('Вкл' if wm.uvwall_show_arrows else 'Выкл')),
            ('DISPLAY:TOOLTIPS','Подсказки · '+('Вкл' if wm.uvwall_show_tooltips else 'Выкл')),
            ('DISPLAY:LABELS','Подписи · '+('Вкл' if wm.uvwall_show_labels else 'Выкл')),
        )
        available=h-94.
        n=min(len(controls),max(0,int(available//31.)))
        for index,(key,caption) in enumerate(controls[:n]):
            by=y+h-88-index*31.
            if by>=y+43.:
                self._button(key,x+10,by,w-20,26,caption,False,True)
        # The hover-colour swatches are geometry diagnostics, NOT a UI theme.
        if h>=190.:
            sw_y=y+47.
            space=(w-30.)/len(HOVER_PALETTE_PRESETS)
            current_rgb=tuple(float(v) for v in context.scene.uv_wall_settings.viewport_hover_color)
            for i,rgb in enumerate(HOVER_PALETTE_PRESETS):
                px=x+12.+i*space
                selected=sum((current_rgb[j]-rgb[j])**2 for j in range(3))<.005
                _draw_rect(px,sw_y,px+space-5.,sw_y+18.,(*rgb,1.),
                    (.96,1.,.95,1.) if selected else (.18,.23,.25,1.))
                self._buttons.append((f'DISPLAY:SWATCH:{i}',(px,sw_y,px+space-5.,sw_y+18.),True))
        if h>=85.:
            self._button('DISPLAY:COLOR',x+10,y+9,w-20,27,'Цвет маркеров · геометрия',False,True)

    def _hit_button(self,x,y):
        # Visibility may change from Blender UI between redraw frames. Never
        # accept a previously cached resource-picker hitbox after Hide.
        scope=window_visibility.scope_from_context(bpy.context)
        hidden_picker=window_visibility.is_hidden(scope,"UV_WALL_RESOURCE_PICKER")
        for key,rect,enabled in reversed(self._buttons or []):
            if hidden_picker and (str(key).startswith('RP:ROW:') or key in {'RP:SEARCH','RP:INVALID','RP:CLEAR','RP:CLOSE'}):
                continue
            if enabled and _rect_hit(x,y,rect): return key
        return None

    def _hit_handle(self,x,y):
        best=None; bd=1e9
        for item in self._handles or []:
            key,pos,radius,meta=item; dist=_distance((x,y),pos)
            if dist<=radius and dist<bd: best=item; bd=dist
        return best

    def _toast(self, text, level="INFO", seconds=2.6):
        self._toast_text=str(text or "")
        self._toast_level=str(level or "INFO")
        self._toast_until=perf_counter()+max(.5,float(seconds))

    def _path_fingerprint(self, path=None):
        """Native, bounded-memory Path signature for the settlement timer.

        Avoid materializing Python tuples for every vertex/edge 8x a second;
        the Preview engine already implements foreach_get + Blake2b hashing.
        """
        path=path or self._path()
        if path is None or getattr(path,"type",None)!="MESH":
            return None
        try:
            engine=self._engine
            if engine is not None and engine.path is path:
                return engine._path_signature()
            return None
        except (ReferenceError,RuntimeError,AttributeError,TypeError):
            return None

    def _refresh_open_editors(self):
        if self._window_stack is None:return
        d=self._definition()
        if d is None:
            self._window_stack.clear();return
        for win in self._window_stack.windows:
            fresh=draft_from_definition(win.kind,d,total_wall_height(d))
            win.draft=fresh.copy();win.baseline=fresh.copy()
            win.edit_field='';win.edit_buffer='';win.choice_field='';win.error='';win.select_all=False

    @staticmethod
    def _payload_keep_surface_metadata(base_payload, authoritative_payload):
        try:
            base=json.loads(base_payload); authoritative=json.loads(authoritative_payload)
            meta=authoritative.get('values',{}).get('surface_heights_json')
            if meta is not None:
                base.setdefault('values',{})['surface_heights_json']=meta
            return json.dumps(base,ensure_ascii=False,separators=(',',':'))
        except Exception:
            return base_payload

    def _push_workspace_history(self, before_payload, after_payload, before_surface=None, after_surface=None, label="изменение"):
        if self._history_replay or not before_payload or not after_payload:
            return
        before_surface=dict(before_surface or {});after_surface=dict(after_surface or {})
        if before_surface or after_surface:
            # Baseline/original-Z metadata is authored on first successful edit.
            # Keep it available even when the coordinate itself is locally undone.
            before_payload=self._payload_keep_surface_metadata(before_payload,after_payload)
        if before_payload==after_payload and before_surface==after_surface:
            return
        self._workspace_undo.append({
            'before':before_payload,'after':after_payload,
            'before_surface':before_surface,'after_surface':after_surface,
            'label':str(label or 'изменение'),
        })
        if len(self._workspace_undo)>64:
            del self._workspace_undo[:-64]
        self._workspace_redo.clear()

    def _apply_workspace_history(self, context, redo=False):
        source=self._workspace_redo if redo else self._workspace_undo
        target=self._workspace_undo if redo else self._workspace_redo
        if not source:
            self._toast('Нет изменений UV Wall для '+('повтора' if redo else 'отмены'),'INFO')
            return True
        entry=source[-1]
        d=self._definition();path=self._path()
        if d is None or path is None:return False
        payload=entry['after' if redo else 'before']
        edits=entry['after_surface' if redo else 'before_surface']
        snap=snapshot_definition(d)
        if not apply_serialized_definition(d,payload):
            self._toast('История UV Wall повреждена — действие не выполнено','ERROR',4.)
            return False
        sync_scene_from_definition(path,context.scene.uv_wall_settings)
        self._history_replay=True
        try:
            ok=self._commit_or_restore(context,snap,quiet=True,surface_edits=edits,
                                       record_history=False,history_label=entry.get('label','изменение'))
        finally:
            self._history_replay=False
        if not ok:
            self._toast('Не удалось '+('повторить' if redo else 'отменить')+' изменение','ERROR',4.)
            return False
        source.pop();target.append(entry)
        self._refresh_open_editors()
        self._smart_segment_key=None;self._spatial_previous.clear()
        self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality='SETTLED')
        self._toast(('Повторено: ' if redo else 'Отменено: ')+entry.get('label','изменение'),'SUCCESS')
        return True

    def _focus_vertex(self, context, index, kind='POINT'):
        path=self._path();region=self._region(context.area);rv3d=getattr(context.space_data,'region_3d',None)
        if path is None or region is None or rv3d is None:return False
        try:
            i=int(index)
            if i<0 or i>=len(path.data.vertices):return False
            world=path.matrix_world @ path.data.vertices[i].co
            rv3d.view_location=world
            rv3d.view_distance=max(.35,min(float(rv3d.view_distance),3.0))
            self._toast(f'Показано: {kind} {i}','INFO')
            context.area.tag_redraw();return True
        except (ReferenceError,RuntimeError,AttributeError,ValueError,TypeError):
            return False

    def _dismiss_canvas_context(self):
        """A blank canvas click dismisses transient selection, not the tool.

        Persistent/pinned parameter editors survive, and dirty drafts are not
        silently discarded. This is UI-only: no Definition/Mesh/Undo mutation.
        """
        changed=(self._corner_selected!=-1 or self._corner_selected_uid!=0
                 or self._wall_point_selected!=-1 or self._surface_point_selected!=-1)
        self._corner_selected=-1
        self._corner_selected_uid=0
        self._wall_point_selected=-1
        self._surface_point_selected=-1
        self._hover=None
        if self._window_stack is not None:
            changed=self._window_stack.dismiss_on_canvas() or changed
        return changed

    def _commit_or_restore(self, context, snapshot, quiet=False, surface_edits=None, record_history=True, history_label="изменение"):
        """Commit one viewport edit as an Undo-safe atomic production transaction.

        Preview mutates the persistent Definition for immediate feedback. Before
        invoking the UNDO production operator we restore the pre-edit snapshot and
        pass the target Definition through a transient payload. Therefore Blender
        sees the real pre-edit state at operator start and one Ctrl+Z can restore
        Definition + generated Mesh together.
        """
        d=self._definition()
        if d is None:
            return False
        # Reject before creating an undo payload or retaining first-build edits.
        # Blender stores authored mesh changes in Edit Mode's BMesh, which a
        # production build must never silently read as an old object-mode mesh.
        mode_error=object_mode_error(context)
        if mode_error:
            if snapshot is not None:
                restore_definition(d,snapshot)
                sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            d.last_error=mode_error
            self._register_build_problem(context)
            if not quiet:
                self.report({'ERROR'},mode_error)
            return False
        # legacy contract marker: had_committed=has_committed_result(d)
        had_committed=has_committed_result(d,self._path())
        target_payload=serialize_definition(d)
        previous_error=d.last_error
        before_surface={}
        if surface_edits:
            path=self._path()
            try:
                for key in surface_edits:
                    index=int(key)
                    before_surface[str(index)]=float((path.matrix_world @ path.data.vertices[index].co).z)
            except (ReferenceError,RuntimeError,AttributeError,IndexError,ValueError,TypeError):
                before_surface={}
        if snapshot is not None:
            restore_definition(d,snapshot)
            sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
        before_payload=serialize_definition(d)
        ok=self._commit_production(context,quiet=quiet,target_payload=target_payload,surface_edits=surface_edits)
        if ok:
            after_payload=serialize_definition(d)
            if record_history:
                self._push_workspace_history(before_payload,after_payload,before_surface,surface_edits,history_label)
            self._engine.clear_surface_override()
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True, quality="SETTLED")
            return True
        error=d.last_error or previous_error or "Production commit failed"
        if not had_committed:
            # First-time setup may remain configured while prerequisites are added.
            apply_serialized_definition(d,target_payload)
            d.last_error=error
            sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
        else:
            # A failed live edit is not an undo step.  Restore the exact
            # pre-edit Definition and Scene so Ctrl+Z cannot jump over a hidden
            # invalid thickness and remove an older, valid wall transaction.
            # The already committed Result mesh remains untouched.
            apply_serialized_definition(d,before_payload)
            d.last_error=error
            sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            self._toast('Изменение отменено · стена сохранена', 'WARNING', 4.0)
        self._engine.clear_surface_override()
        self._engine.invalidate(Dirty.ALL);self._engine.update(force=True, quality="SETTLED")
        self._register_build_problem(context)
        if not quiet:
            self.report({"ERROR"},error)
        return False

    def _cornice_readiness(self, definition=None):
        d=definition or self._definition()
        if d is None:
            return False, 'Не найдено описание карниза'
        required=self._setup_requirement(d)
        if required is not None:
            kind,reason=required
            return False, ('Шаг 1 · Выберите путь: ' if kind=='PATH' else 'Шаг 2 · Выберите профиль: ')+reason
        # Smooth/Sharp is a local authored-corner property, never a global
        # resource gate. Legacy MAX_CLIPPED without authored corners is
        # normalized by the production builder to Smooth.
        return True,'Готов к построению'

    def _set_mode(self, mode, context, commit=False):
        d=self._definition()
        if d is None or not self._mode_available(mode): return False
        snapshot=snapshot_definition(d)
        if mode!=d.generator_type and self._window_stack is not None:
            self._window_stack.clear();self._window_stack.set_source(self._source_uid)
        d.generator_type=mode; d.revision+=1
        sync_scene_from_definition(self._path(), context.scene.uv_wall_settings)
        self._engine.invalidate(Dirty.ALL); self._engine.update()
        if commit:
            ready, reason = self._cornice_readiness(d) if mode=="CORNICE" else (True, "Ready")
            if ready:
                self._commit_or_restore(context,snapshot,quiet=True)
            else:
                d.last_error=reason
                self._engine.invalidate(Dirty.ALL); self._engine.update()
        return True

    def _reset_wall_height(self, context, index):
        """Original Z reset for WALL and SURFACE via one atomic Undo build."""
        d=self._definition();path=self._path()
        if d is None or path is None or d.generator_type not in {'WALL','SURFACE'}:return False
        original=original_height_for_point(path,d,index)
        if original is None:
            self.report({'WARNING'},'Исходная Z неизвестна: изменение до 1.7.2 не имеет базовой записи')
            return False
        try:
            validate_height_edits(path,{str(int(index)):original})
        except (ValueError,ReferenceError,TypeError) as exc:
            self.report({'WARNING'},str(exc));return False
        current=float((path.matrix_world @ path.data.vertices[int(index)].co).z)
        if math.isclose(original,current,abs_tol=1.e-7):return True
        previous=snapshot_definition(d)
        ok=self._commit_or_restore(context,previous,quiet=False,
                                   surface_edits={str(int(index)):original},history_label="сброс исходной Z")
        if ok:self._wall_point_selected=int(index)
        return ok

    def _apply_context_button(self, key, context):
        if key in {'PROB_HIDE','PROB_SHOW_PANEL'}:
            return self._apply_workspace_button(key,context)
        if key=="PROB_FIX":
            try:bpy.ops.object.uv_wall_problem_make_sharp()
            except Exception:return False
            self._engine.invalidate(Dirty.ALL);self._engine.update();self._refresh_problems(context)
            return True
        if key in {"PROB_PREV", "PROB_SHOW", "PROB_NEXT"}:
            direction = -1 if key == "PROB_PREV" else (1 if key == "PROB_NEXT" else 0)
            try:
                bpy.ops.object.uv_wall_problem_navigate(direction=direction)
            except Exception:
                return False
            return True
        d=self._definition()
        if d is None:return False
        snapshot=snapshot_definition(d)
        if key=="W_INNER":d.create_inner=not d.create_inner
        elif key=="W_SIDE":
            if not d.create_inner:
                # A single shell's direction is its face normal, not inset
                # alignment: no second shell exists to position.
                d.wall_normal_mode={
                    'OUTWARD':'INWARD','INWARD':'OUTWARD',
                    'PATH_LEFT':'PATH_RIGHT','PATH_RIGHT':'PATH_LEFT',
                }.get(d.wall_normal_mode,'OUTWARD')
            else:
                physical=self._physical_alignment(d)
                d.wall_alignment="OUTSIDE" if physical!="OUTSIDE" else "INSIDE"
                from ..production.wall_alignment import automatic_inset_side
                d.inset_side=automatic_inset_side(d.wall_alignment,d.inset_side)
            self._smart_segment_key=None;self._spatial_previous.clear()
        elif key=="W_TOP":d.create_top_cap=not d.create_top_cap
        elif key=="W_DIR":
            d.story_direction="UP" if d.story_direction=="DOWN" else "DOWN"
            self._smart_segment_key=None;self._spatial_previous.clear()
        elif key=="S_NORMAL":d.roof_normal="DOWN" if d.roof_normal=="UP" else "UP"
        elif key=="C_UV":d.cornice_uv_mode="MAX_REAL_WORLD" if d.cornice_uv_mode=="MAX_CLIPPED" else "MAX_CLIPPED"
        elif key=="C_FLIP":d.cornice_flip_side=not d.cornice_flip_side
        elif key=="W_PLANT":d.create_inner_surface=not d.create_inner_surface
        elif key=="MODULE":
            self._toast("Модуль задаётся текстурой материала · Ресурсы → Материал", "INFO", 2.5)
            return True
        else:return False
        d.revision+=1;sync_scene_from_definition(self._path(),context.scene.uv_wall_settings);self._engine.invalidate(Dirty.ALL);self._engine.update()
        # A completed click is an atomic edit: GPU preview changes immediately,
        # then production follows in the same Undo-safe transaction. BUILD is a
        # manual refresh/recovery action, never a required second step.
        ready, reason = self._cornice_readiness(d) if d.generator_type=="CORNICE" else (True, "Ready")
        if ready:
            self._commit_or_restore(context,snapshot,quiet=True)
        else:
            d.last_error=reason
            self._engine.invalidate(Dirty.ALL); self._engine.update()
        return True

    def _begin_drag(self, kind, x, y, meta):
        d=self._definition()
        if kind.startswith("PATH_Z:"):
            # Shared/shape-key/singular source paths are not editable safely.
            # Report before the user drags instead of rejecting only on release.
            from ..core.surface_points import validate_height_edits
            try:
                validate_height_edits(self._path(), {str(int(meta["vertex_index"])):float(meta["anchor"].z)})
            except (ValueError, TypeError, ReferenceError) as exc:
                self.report({"WARNING"},str(exc))
                return False
        self._drag_kind=("SURFACE_Z" if kind.startswith("SURFACE_Z:") else
                         "PATH_Z" if kind.startswith("PATH_Z:") else kind)
        self._drag_index=int(meta["vertex_index"]) if self._drag_kind in {"SURFACE_Z","PATH_Z"} else None
        if self._drag_kind=="PATH_Z": self._wall_point_selected=self._drag_index
        self._drag_start_mouse=(x,y); self._drag_snapshot=snapshot_definition(d)
        self._drag_touched=False
        self._precision_modifiers=(False,False)
        if self._drag_kind in {"SURFACE_Z","PATH_Z"}:
            self._drag_start_value=float(meta["anchor"].z);self._surface_drag_z=self._drag_start_value
        elif kind=="HEIGHT":
            self._drag_start_value=total_wall_height(d)
            self._drag_start_direction=d.story_direction
        elif kind=="BOTTOM_LEVEL": self._drag_start_value=float(d.bottom_extend)
        elif kind=="OFFSET": self._drag_start_value=float(d.wall_offset)
        else: self._drag_start_value=float(d.inset_distance)
        context=bpy.context; anchor=meta.get("anchor") if meta else None
        if self._drag_kind in {"HEIGHT","BOTTOM_LEVEL","SURFACE_Z","PATH_Z"} and anchor is not None:
            a=self._screen(context,anchor); b=self._screen(context,anchor+Vector((0,0,1)))
        else:
            mid=meta.get("mid") if meta else None; norm=meta.get("norm") if meta else None
            a=self._screen(context,mid) if mid is not None else None; b=self._screen(context,mid+norm) if mid is not None and norm is not None else None
        if a and b:
            vx, vy = b[0]-a[0], b[1]-a[1]
            length=max(1.0e-6, math.hypot(vx,vy))
            sensitivity=float(meta.get('sensitivity',1.)) if meta else 1.
            self._drag_ppm=max(8.0,length*max(.01,sensitivity))
            self._drag_screen_axis=(vx/length,vy/length)
        else:
            self._drag_ppm=100.0; self._drag_screen_axis=(0.0,1.0)

    def _drag_has_change(self):
        """Compare semantic parameter values, not revision or mouse pixels."""
        d=self._definition()
        if d is None or self._drag_kind is None or self._drag_start_value is None:
            return False
        kind=self._drag_kind
        current={
            "HEIGHT":lambda:total_wall_height(d),
            "THICKNESS":lambda:float(d.inset_distance),
            "OFFSET":lambda:float(d.wall_offset),
            "BOTTOM_LEVEL":lambda:float(d.bottom_extend),
            "SURFACE_Z":lambda:self._surface_drag_z,
            "PATH_Z":lambda:self._surface_drag_z,
            "SURFACE_OFFSET":lambda:float(d.surface_height_offset),
            "SURFACE_ROT":lambda:float(d.surface_module_rotation),
            "CORNICE_ROT":lambda:float(d.cornice_profile_rotation),
        }.get(kind)
        if current is None:return False
        if kind=='HEIGHT' and d.story_direction!=self._drag_start_direction:return True
        value=current()
        return value is not None and math.isfinite(float(value)) and not math.isclose(
            float(value),float(self._drag_start_value),rel_tol=0.0,abs_tol=1.e-9)

    def _restore_drag(self):
        # A click-without-movement must not invalidate/rebuild settled geometry.
        if not self._drag_touched:
            self._drag_index=None;self._surface_drag_z=None
            self._drag_kind=None;self._drag_start_value=None
            self._drag_start_mouse=None;self._drag_snapshot=None
            self._numeric=False
            return
        self._engine.clear_surface_override()
        self._drag_index=None;self._surface_drag_z=None
        d=self._definition()
        if d is not None and self._drag_snapshot is not None:
            restore_definition(d,self._drag_snapshot)
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True, quality="SETTLED")
        elif d is not None and self._drag_start_value is not None:
            if self._drag_kind=="HEIGHT": set_total_wall_height(d,self._drag_start_value)
            elif self._drag_kind=="BOTTOM_LEVEL": d.bottom_extend=self._drag_start_value; d.revision+=1
            elif self._drag_kind=="THICKNESS": d.inset_distance=self._drag_start_value; d.revision+=1
            elif self._drag_kind=="OFFSET": d.wall_offset=self._drag_start_value; d.revision+=1
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True, quality="SETTLED")
        self._drag_kind=None; self._drag_start_value=None; self._drag_start_mouse=None; self._drag_snapshot=None
        self._drag_touched=False

    def _apply_drag(self, x, y, shift=False, ctrl=False):
        self._precision_modifiers=(bool(shift),bool(ctrl))
        d=self._definition()
        if d is None or self._drag_kind is None: return
        sx,sy=self._drag_start_mouse; dx=x-sx; dy=y-sy
        projected=dx*self._drag_screen_axis[0]+dy*self._drag_screen_axis[1]
        if self._drag_kind=="HEIGHT":
            value,direction=_universal.height_from_signed_drag(
                self._drag_start_value,self._drag_start_direction,
                projected/self._drag_ppm)
            if shift:value=max(.01,round(value/.05)*.05)
            if d.story_direction!=direction:
                d.story_direction=direction
                d.revision+=1
                self._smart_segment_key=None
                self._spatial_previous.clear()
            set_total_wall_height(d,value)
            self._engine.invalidate(Dirty.HEIGHT)
        elif self._drag_kind=="BOTTOM_LEVEL":
            value=max(0.0,self._drag_start_value - projected/self._drag_ppm)
            if shift:value=round(value/0.05)*0.05
            d.bottom_extend=value;d.revision+=1;self._engine.invalidate(Dirty.HEIGHT)
        elif self._drag_kind in {"SURFACE_Z","PATH_Z"}:
            value=self._drag_start_value+projected/self._drag_ppm
            if shift: value=round(value/0.05)*0.05
            elif ctrl: value=round(value/0.005)*0.005
            self._surface_drag_z=value
            self._engine.set_surface_override(self._drag_index,value)
        elif self._drag_kind=="OFFSET":
            value=self._drag_start_value+projected/self._drag_ppm
            if shift: value=round(value/0.05)*0.05
            elif ctrl: value=round(value/0.005)*0.005
            d.wall_offset=value; d.revision+=1; self._engine.invalidate(Dirty.OFFSET)
        elif self._drag_kind=="THICKNESS":
            # screen projection already follows the chosen side; horizontal/vertical view is irrelevant.
            value=max(0.001,self._drag_start_value + projected/self._drag_ppm)
            if shift: value=round(value/0.01)*0.01
            d.inset_distance=value; d.revision+=1; self._engine.invalidate(Dirty.THICKNESS)
        self._drag_touched=True
        self._engine.update()

    def _start_marker_spin(self, context, key, x, y):
        d=self._definition()
        if d is None or self._numeric:return False
        kind={'W_HEIGHT':'HEIGHT','S_Z':'SURFACE_OFFSET','S_ROT':'SURFACE_ROT','C_ROT':'CORNICE_ROT'}.get(key)
        if kind is None:return False
        value=(total_wall_height(d) if kind=='HEIGHT' else
               d.surface_height_offset if kind=='SURFACE_OFFSET' else
               d.surface_module_rotation if kind=='SURFACE_ROT' else d.cornice_profile_rotation)
        self._spin_state={'marker':key,'kind':kind,'initial':float(value),
                         'xy':(float(x),float(y)),'snapshot':snapshot_definition(d),'changed':False,'value':float(value),
                         'modifiers':(False,False)}
        self._rotation_phase='PREVIEW' if kind in {'SURFACE_ROT','CORNICE_ROT'} else ''
        self._rotation_message=''
        if kind=='CORNICE_ROT' and getattr(d,'cornice_profile',None) is not None:
            # Use the real scaled XZ profile only for temporary GPU preview;
            # no Mesh object or authored geometry is created on MOUSEMOVE.
            try:
                from ..production.cornice import cornice_profile_data
                coords,_closed=cornice_profile_data(d.cornice_profile)
                if len(coords)<=512:
                    self._spin_state['profile_cache']=tuple((float(p.x),float(p.z)) for p in coords)
            except (ValueError,TypeError,RuntimeError,ReferenceError) as exc:
                self._spin_state['preview_error']='Профиль карниза: '+str(exc)[:140]
        return True

    def _update_marker_spin(self, context, x, y, *, shift=False, ctrl=False):
        state=self._spin_state
        if not state:return
        state['modifiers']=(bool(shift),bool(ctrl))
        d=self._definition()
        if d is None:return
        start_x,start_y=state['xy']
        pixels=(float(y)-start_y) if state['kind'] in {'HEIGHT','SURFACE_OFFSET'} else (float(x)-start_x)
        if abs(pixels)<2 and not state['changed']:return
        value=_precision.numeric_spin(state['initial'],pixels,
                                kind=state['kind'],shift=shift,ctrl=ctrl)
        if state['kind']=='HEIGHT':set_total_wall_height(d,value);self._engine.invalidate(Dirty.HEIGHT)
        elif state['kind']=='SURFACE_OFFSET':d.surface_height_offset=value;d.revision+=1;self._engine.invalidate(Dirty.ALL)
        elif state['kind']=='SURFACE_ROT':d.surface_module_rotation=value;d.revision+=1;self._engine.invalidate(Dirty.ALL)
        else:d.cornice_profile_rotation=value;d.revision+=1;self._engine.invalidate(Dirty.ALL)
        state['changed']=True;state['value']=float(value)
        # LivePreviewEngine never mutates the built Result Mesh, only GPU buffers.
        # Even its cornice path-only representation receives a separate live
        # orientation guide, rendered by _draw_view from the Definition.
        try:
            self._engine.update()
        except (RuntimeError,ValueError,ArithmeticError) as exc:
            state['preview_error']=str(exc)[:180]
            self._rotation_message=state['preview_error']
            self._toast('Предпросмотр: '+state['preview_error'],'WARNING',3.)
        if context.area:context.area.tag_redraw()

    def _finish_marker_spin(self, context, *, cancel=False):
        state=self._spin_state
        self._spin_state=None
        if not state:return
        d=self._definition()
        if d is None:return
        kind=state['kind']
        if cancel or not state['changed']:
            self._rotation_phase=''
            if state['changed']:
                restore_definition(d,state['snapshot'])
                self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality='SETTLED')
            if context.area:context.area.tag_redraw()
            return
        self._rotation_phase='VALIDATING' if kind in {'SURFACE_ROT','CORNICE_ROT'} else ''
        valid,reason=_clarity.rotation_candidate(kind,state.get('value'),
            source_valid=(self._path() is not None and
              (kind!='SURFACE_ROT' or self._mode_available('SURFACE'))),
            required_resource=(kind!='CORNICE_ROT' or self._cornice_readiness(d)[0])) if kind in _clarity.ROTATION_KINDS else (True,'')
        if state.get('preview_error'):
            valid=False;reason='Ошибка предварительной геометрии: '+str(state['preview_error'])
        if not valid:
            restore_definition(d,state['snapshot'])
            sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality='SETTLED')
            self._rotation_phase='ROLLBACK';self._rotation_message=reason
            self._toast('Поворот отклонён: '+reason,'ERROR',5.)
        else:
            ok=self._commit_or_restore(context,state['snapshot'],quiet=False,
                                 history_label='точное изменение маркера')
            if not ok:
                reason=str(getattr(d,'last_error','') or 'Не удалось построить сетку')
                restore_definition(d,state['snapshot'])
                sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
                self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality='SETTLED')
                self._rotation_phase='ROLLBACK';self._rotation_message=reason
                self._toast('Поворот отменён · '+reason,'ERROR',5.)
            else:
                self._rotation_phase='APPLIED' if kind in _clarity.ROTATION_KINDS else ''
                self._rotation_message=''
                if kind in _clarity.ROTATION_KINDS:
                    self._toast('Поворот применён · геометрия проверена','SUCCESS',2.)
        if context.area:context.area.tag_redraw()

    def _apply_marker_choice(self, context, marker, value):
        d=self._definition()
        if d is None:return False
        options=dict(_precision.choices_for(marker,is_double=bool(d.create_inner)))
        if value not in options:return False
        if marker=='W_THICKNESS':
            if bool(d.create_inner)==(value=='DOUBLE'):return True
            return self._apply_context_button('W_INNER',context)
        if marker!='W_WALL':return False
        field='wall_alignment' if d.create_inner else 'wall_normal_mode'
        if str(getattr(d,field))==value:return True
        snapshot=snapshot_definition(d)
        setattr(d,field,value)
        if d.create_inner:
            from ..production.wall_alignment import automatic_inset_side
            d.inset_side=automatic_inset_side(d.wall_alignment,d.inset_side)
        d.revision+=1
        self._spatial_previous.clear()
        sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
        self._engine.invalidate(Dirty.ALL);self._engine.update()
        return self._commit_or_restore(context,snapshot,quiet=False,history_label='положение стены')

    def _open_marker_choice(self, context, marker):
        if marker not in self._spatial_rects:return False
        self._marker_choice=None if self._marker_choice==marker else marker
        self._marker_choice_rows=[]
        if context.area:context.area.tag_redraw()
        return True

    def _start_marker_numeric(self, context, marker, kind):
        """Inline-only number entry; reuse existing preview + atomic Undo commit.

        No parameter editor is created. The committed definition is restored
        on Esc, and a successful Enter uses the same production transaction
        as dragging the 3D gizmo. No second modeling implementation.
        """
        d=self._definition()
        if d is None or marker not in self._spatial_rects:return False
        fields={'HEIGHT':total_wall_height(d), 'THICKNESS':float(d.inset_distance),
                'OFFSET':float(d.wall_offset),'SURFACE_OFFSET':float(d.surface_height_offset),
                'SURFACE_ROT':float(d.surface_module_rotation),
                'CORNICE_ROT':float(d.cornice_profile_rotation)}
        if kind not in fields:return False
        self._drag_kind=kind
        self._drag_snapshot=snapshot_definition(d)
        self._drag_start_value=float(fields[kind])
        self._drag_start_direction=getattr(d,'story_direction','UP')
        self._drag_start_mouse=None
        self._drag_index=None
        self._drag_touched=False
        self._numeric=True;self._numeric_kind=kind
        self._numeric_mouse_selecting=False
        self._numeric_buffer=format_value(fields[kind])
        self._numeric_caret=len(self._numeric_buffer)
        self._numeric_selected=True
        self._inline_marker=marker
        self._numeric_editor=_precision.InlineNumericBuffer.selected_all(self._numeric_buffer)
        if context.area:context.area.tag_redraw()
        return True

    def _begin_numeric(self):
        if self._drag_kind not in {"HEIGHT","THICKNESS","BOTTOM_LEVEL","SURFACE_Z","PATH_Z","OFFSET"}: return
        self._numeric=True; self._numeric_kind=self._drag_kind
        self._numeric_mouse_selecting=False
        # Typing a number REPLACES the drag value, not appends to it.
        self._numeric_buffer=""

    def _apply_numeric(self):
        if not self._numeric or not self._numeric_buffer: return
        value=_precision.parse_numeric_expression(self._numeric_buffer)
        if value is None:return
        if not math.isfinite(value): return
        d=self._definition()
        if self._numeric_kind in {"SURFACE_Z","PATH_Z"}:
            self._surface_drag_z=value; self._engine.set_surface_override(self._drag_index,value)
        elif self._numeric_kind=="HEIGHT": set_total_wall_height(d,max(0.001,value)); self._engine.invalidate(Dirty.HEIGHT)
        elif self._numeric_kind=="OFFSET": d.wall_offset=value; d.revision+=1; self._engine.invalidate(Dirty.OFFSET)
        elif self._numeric_kind=="BOTTOM_LEVEL": d.bottom_extend=max(0.0,value); d.revision+=1; self._engine.invalidate(Dirty.HEIGHT)
        elif self._numeric_kind=='SURFACE_OFFSET': d.surface_height_offset=value; d.revision+=1; self._engine.invalidate(Dirty.ALL)
        elif self._numeric_kind=='SURFACE_ROT': d.surface_module_rotation=value; d.revision+=1; self._engine.invalidate(Dirty.ALL)
        elif self._numeric_kind=='CORNICE_ROT': d.cornice_profile_rotation=value; d.revision+=1; self._engine.invalidate(Dirty.ALL)
        else: d.inset_distance=max(0.001,value); d.revision+=1; self._engine.invalidate(Dirty.THICKNESS)
        self._drag_touched=True
        self._engine.update()

    def _open_spatial_editor(self,context,key,field_override=None):
        # Central routing for both spatial cards and handle click-to-type.
        if key=='W_HEIGHT':
            return self._start_marker_numeric(context,key,'HEIGHT')
        if key=='C_PROFILE':
            if self._resource_picker is None:return False
            self._picker_editor=None
            self._resource_picker.open('PROFILE')
            return True
        d=self._definition()
        if key in {'C_ROT','C_SIDE','C_SETTINGS'} and d is not None and self._setup_requirement(d) is not None:
            self._resource_picker.open('PROFILE')
            return True
        editor_kind={
            'W_THICKNESS':'THICKNESS',
            'W_WALL':('PLACEMENT' if self._definition().create_inner else 'NORMALS'),
            'W_CAP':'CAP','W_PLANT':'PLANT','S_Z':'SURFACE',
            'S_ROT':'SURFACE','S_SETTINGS':'SURFACE',
            'C_ROT':'CORNICE_ROT','C_SIDE':'CORNICE_SIDE','C_SETTINGS':'CORNICE',
        }.get(key)
        if editor_kind is None or self._window_stack is None:return False
        if key in {'W_CAP','W_PLANT'}:
            analysis=self._analysis()
            closed=any(bool(c.get('closed')) for c in analysis.get('simple_chains',()))
            from .tool_eligibility import wall_tool_eligibility
            eligible=wall_tool_eligibility(closed=closed,double=self._definition().create_inner,
                                            has_interior=self._inside_cache_xy is not None)
            if not eligible[key]:return False
        d=self._definition()
        region=self._region(context.area)
        if d is None or region is None:return False
        w=self._window_stack.open(editor_kind,self._source_uid,d,
                                  total_wall_height(d),
                                  self._spatial_rects.get(key,self._hud_rect),
                                  (region.width,region.height),
                                  path_closed=any(bool(c.get('closed')) for c in self._analysis().get('simple_chains',())))
        primary={'W_HEIGHT':'height',
                 'W_THICKNESS':('inset_distance' if d.create_inner else None),
                 'S_Z':'surface_height_offset',
                 'S_ROT':'surface_module_rotation',
                 'C_ROT':'cornice_profile_rotation'}.get(key)
        primary=field_override or primary
        if (field_override and w.edit_field and w.edit_field!=field_override):
            # Preserve an unsaved valid number in the editor draft before
            # navigating to the opposite half of its source marker.
            if not self._window_stack._commit_field(w):
                return True
        if primary and primary in w.draft and not w.edit_field:
            target=next((f for f in FIELDS[w.kind] if f[0]==primary),None)
            if target and target[2]=='choice':
                # Choice fields must never enter the numeric text editor.
                w.choice_field=primary
                visible=[f[0] for f in visible_fields(w.kind,w.draft,w.path_closed)]
                if primary in visible and w.row_capacity>0:
                    w.scroll=max(0,min(visible.index(primary),len(visible)-w.row_capacity))
            elif target and target[2]=='number':
                w.edit_field=primary
                w.edit_buffer=format_value(w.draft[primary])
                w.caret=len(w.edit_buffer)
                w.select_all=False
        return True

    def _apply_editor_window(self,context,w):
        """One editor Apply -> exactly one existing UNDO production transaction.

        Other floating editor drafts stay untouched. No geometry mutation on
        typing or window activation; snapshot/rollback precedes production.
        """
        d=self._definition()
        if d is None or w.uid!=self._source_uid:
            w.error='Объект изменился — откройте окно заново';return False
        try:
            current_draft=draft_from_definition(w.kind,d,total_wall_height(d))
            values=changed_patch(w.kind,w.draft,w.baseline,current_draft)
            if not values:
                self._window_stack.accept(w,d,total_wall_height(d))
                return True
            # Resolve ONLY changed external references before mutating Definition.
            for key,collection_name in (('inner_surface_material','materials'),('cap_material','materials'),('bottom_cap_material','materials'),('cornice_profile','objects')):
                if key in values:
                    name=values[key].strip()
                    target=getattr(bpy.data,collection_name).get(name) if name else None
                    if name and target is None:raise ValueError('Не найден: '+name)
                    values[key]=target
            snap=snapshot_definition(d)
            for key,value in values.items():
                if key=='height':set_total_wall_height(d,value)
                else:setattr(d,key,value)
            d.revision+=1
            if 'cornice_profile' in values and d.generator_type=='CORNICE':
                # Applying a profile in a floating editor is also a setup
                # action, not an implicit full production transaction.
                # Don't erase it just because Sharp corners are still pending.
                if snap.get('cornice_profile') is None:
                    try:
                        authored=bool(json.loads(d.corner_uid_json or '[]'))
                    except (TypeError, ValueError):
                        authored=True
                    if not authored:d.cornice_uv_mode='MAX_REAL_WORLD'
                d.last_error=''
                sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
                self._engine.invalidate(Dirty.ALL);self._engine.update()
                self._window_stack.accept(w,d,total_wall_height(d))
                return True
            if any(k in values for k in ('inset_side','wall_alignment','story_direction','create_inner')):
                if d.generator_type=='WALL':
                    from ..production.wall_alignment import automatic_inset_side
                    d.inset_side=automatic_inset_side(self._physical_alignment(d),d.inset_side)
                self._smart_segment_key=None
                self._spatial_previous.clear()
            sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            ready,reason=self._cornice_readiness(d) if d.generator_type=='CORNICE' else (True,'')
            if not ready:
                restore_definition(d,snap)
                sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
                w.error=reason
                return False
            if self._commit_or_restore(context,snap,quiet=False,history_label="параметры"):
                # Only this window is accepted; sibling dirty drafts are preserved.
                self._window_stack.accept(w,d,total_wall_height(d))
                return True
            w.error=d.last_error or 'Построение не завершено'
        except Exception as exc:
            # A validation failure never changes Definition or generated Mesh.
            if 'snap' in locals():
                restore_definition(d,snap)
                sync_scene_from_definition(self._path(),context.scene.uv_wall_settings)
            w.error=str(exc)
        return False

    def _register_build_problem(self, context):
        """Expose cancelled builds without wiping precise geometry records."""
        d=self._definition();path=self._path()
        if d is None or path is None:return
        detail=str(getattr(d,'last_error','') or '').strip()
        if not detail:return
        from ..core.problem_registry import add_problem, explain_build_failure
        name,description,action=explain_build_failure(d.generator_type,detail)
        add_problem(context.scene.uv_wall_settings,subsystem=d.generator_type,
                    severity='ERROR',title=f'{name}: ошибка построения',
                    description=description,object_name=path.name,
                    world_position=path.matrix_world.translation,
                    code='LAST_BUILD_FAILURE',element_kind='BUILD',suggested_action=action)

    def _commit_production(self, context, quiet=False, target_payload="", surface_edits=None):
        path=self._path(); d=self._definition(); settings=context.scene.uv_wall_settings
        if path is None or d is None:
            return False
        # When target_payload is supplied the production operator applies the target
        # after its UNDO snapshot has been taken. Poll is relaxed by the commit flag.
        wm=context.window_manager
        try:
            wm.uvwall_viewport_commit_active=True
            wm.uvwall_pending_definition_json=target_payload or ""
            wm.uvwall_pending_surface_edits_json=json.dumps(surface_edits or {})
            result=execute_build(context)
        except Exception as exc:
            d.last_error=str(exc)
            self._register_build_problem(context)
            if not quiet:self.report({"ERROR"},str(exc))
            return False
        finally:
            try:
                wm.uvwall_viewport_commit_active=False
                wm.uvwall_pending_definition_json=""
                wm.uvwall_pending_surface_edits_json=""
            except Exception:
                pass
        if "FINISHED" not in result:
            if not d.last_error:
                d.last_error="Production build cancelled"
            self._register_build_problem(context)
            return False
        return True

    def modal(self, context, event):
        # Global ISSMAKER tool-lifecycle contract: one Esc exits this tool,
        # even if Studio Dock, a resource dropdown or a drag owns the input.
        if event.type == 'ESC' and event.value == 'PRESS':
            if getattr(self,'_clean_preview',False):
                self._clean_preview=False;self._clean_button=None
                if context.area:context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if getattr(self,'_spin_state',None) is not None:
                self._finish_marker_spin(context,cancel=True)
                return {'RUNNING_MODAL'}
            if self._numeric:
                self._numeric=False;self._numeric_editor=None;self._inline_marker=None
                self._restore_drag()
                return {'RUNNING_MODAL'}
            if getattr(self,'_marker_choice',None):
                self._marker_choice=None;self._marker_choice_rows=[]
                return {'RUNNING_MODAL'}
            if self._drag_kind is not None:
                self._restore_drag()
            if self._resource_picker is not None:
                self._resource_picker.close_dropdown()
                self._resource_picker.cancel_viewport()
            self._numeric=False
            self._radial=False
            self._finish(context)
            return {'FINISHED'}
        # Enter is explicit finalization ONLY outside text editors/popovers.
        # Numeric fields and popup editors must receive their own Enter first.
        if (event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS'
                and not (event.ctrl or event.alt or event.shift)
                and getattr(context, 'mode', '') == 'OBJECT'
                and getattr(context, 'area', None) is not None
                and context.area.as_pointer() == self._area_ptr
                and not self._drag_kind and not self._numeric and not self._radial
                and not (self._resource_picker and self._resource_picker.open_kind)
                and not (self._window_stack and any(
                    bool(getattr(win, 'edit_field', '') or getattr(win, 'choice_field', ''))
                    for win in self._window_stack.windows))):
            try:
                result=bpy.ops.uvwall.finalize_result('INVOKE_DEFAULT')
            except Exception as exc:
                self.report({'ERROR'}, 'Фиксация стены: ' + str(exc)[:150])
                return {'RUNNING_MODAL'}
            if 'FINISHED' in result:
                self._finish(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}
        new_mode=active_ui_mode(context)
        if new_mode != getattr(self, "_ui_mode", "FLOATING_PRO"):
            if self._drag_kind is not None:
                self._restore_drag()
            self._ui_mode=new_mode
            self._hover=None; self._radial=False; self._numeric=False
            self._buttons=[]; self._handles=[]; self._spatial_rects={}
            self._hud_rect=None
            self._workspace_rect=None; self._picker_editor=None
            self._dock_rectangle=None
            self._dock_drawn_region_size=None;self._dock_list_bounds=None;self._dock_scroll_max=0
            if self._resource_picker is not None:
                self._resource_picker.close_dropdown()
                self._resource_picker.cancel_viewport()
            if context.area is not None:context.area.tag_redraw()
            # Floater drafts persist in EditorStack until returning to Floating.
        # Keep the modal session and its windows, but suspend ALL source-Mesh
        # reads while Blender is in Edit Mode. An active BMesh is not a safe
        # substitute for the object-mode geometry; build remains prohibited.
        if getattr(context, "mode", "") != "OBJECT":
            self._hover=None
            if self._drag_kind is not None:
                self._restore_drag()
            return {"PASS_THROUGH"}
        from ..core.undo_handlers import history_generation
        if self._history_generation != history_generation:
            self._history_generation = history_generation
            # Native Blender history (menu, another editor, script) is a new
            # history branch. Never replay UV Wall's in-memory snapshots across
            # it: they may reference a pre-Undo Definition state.
            if self._workspace_undo is not None: self._workspace_undo.clear()
            if self._workspace_redo is not None: self._workspace_redo.clear()
            self._toast("История Blender изменилась · локальная история UV Wall очищена", "INFO", 2.0)
            # Blender can replace Object/mesh wrappers on Undo; avoid replaying
            # old GPU/UI drafts into the restored scene and invalidate preview.
            self._drag_kind = None
            self._drag_snapshot = None
            self._numeric = False
            self._smart_segment_key = None
            if self._window_stack is not None:
                restored=self._definition()
                if restored is not None:
                    for win in self._window_stack.windows:
                        fresh=draft_from_definition(win.kind,restored,total_wall_height(restored))
                        win.draft=fresh.copy();win.baseline=fresh.copy()
                        win.edit_field='';win.edit_buffer='';win.choice_field=''
                        win.error='';win.select_all=False
                else:
                    self._window_stack.clear()
            if self._engine is not None:
                self._engine.invalidate(Dirty.ALL)
        path=self._path()
        if not context.window_manager.uvwall_viewport_active:
            if self._drag_kind is not None:self._restore_drag()
            self._finish(context); return {"FINISHED"}
        if path is None:
            if self._drag_kind is not None:self._restore_drag()
            self._finish(context); return {"CANCELLED"}
        if context.area and context.area.as_pointer()==self._area_ptr: context.area.tag_redraw()
        x,y=self._mouse_xy(context,event); self._last_mouse=(x,y)
        region=self._region(context.area)
        inside=bool(region and 0 <= x < region.width and 0 <= y < region.height)
        if getattr(self,'_clean_preview',False):
            if (event.type=='LEFTMOUSE' and event.value=='PRESS'
                    and _clarity.in_rect(x,y,getattr(self,'_clean_button',None))):
                self._clean_preview=False;self._clean_button=None
                if context.area:context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            # Preview Clean must not leave invisible controls active.
            if event.type in {'MOUSEMOVE','LEFTMOUSE','RIGHTMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE','MIDDLEMOUSE','NDOF_MOTION'}:
                return {'PASS_THROUGH'}
            if event.type=='TIMER':
                return {'PASS_THROUGH'}
            return {'PASS_THROUGH'}

        if new_mode == "STUDIO_DOCK":
            if inside and region is not None:
                outcome=self._handle_studio_dock_event(context,event,x,y,region)
                if outcome is not None:return outcome
            if event.type=="TIMER":
                try:
                    pointers={area.as_pointer() for area in context.window.screen.areas}
                    if self._area_ptr not in pointers or context.window.workspace != self._workspace_ref:
                        self._finish(context)
                        return {"FINISHED"}
                    self._sync_follow_selection(context)
                    self._engine.update()
                except (AttributeError,ReferenceError,RuntimeError):
                    self._finish(context)
                    return {"FINISHED"}
            return {"PASS_THROUGH"}

        # While the workspace is active Ctrl+Z belongs to UV Wall's atomic
        # edit history. Passing it to Blender undoes the long-running modal
        # operator itself and destroys every window/handler. We therefore
        # consume it and replay exactly one successful production transaction.
        if event.ctrl and not event.alt and event.type=='Z' and event.value=='PRESS':
            if self._numeric and self._numeric_editor is not None:
                if self._numeric_editor.undo(redo=bool(event.shift)):
                    self._numeric_buffer=self._numeric_editor.text
                    self._apply_numeric()
                return {'RUNNING_MODAL'}
            if self._drag_kind is not None:
                self._restore_drag()
                if self._engine is not None:self._engine.invalidate(Dirty.ALL)
                self._toast('Отменён незавершённый жест','INFO')
                return {"RUNNING_MODAL"}
            if self._numeric:
                self._numeric=False
                self._toast('Числовой ввод отменён','INFO')
                return {"RUNNING_MODAL"}
            if self._window_stack is not None:
                editing=[w for w in self._window_stack.windows if w.edit_field]
                if editing:
                    for editor in editing:
                        editor.edit_field='';editor.edit_buffer='';editor.select_all=False
                    self._toast('Редактирование поля отменено','INFO')
                    return {"RUNNING_MODAL"}
            self._apply_workspace_history(context,redo=bool(event.shift))
            return {"RUNNING_MODAL"}

        picker=self._resource_picker
        if picker is not None and picker.open_kind and region is not None and (region.width<220 or region.height<175):
            picker.close_dropdown()
            self._picker_editor=None
        if picker is not None and picker.viewport_kind:
            if event.type in {"ESC","RIGHTMOUSE"} and event.value=="PRESS":
                picker.cancel_viewport();return {"RUNNING_MODAL"}
            if event.type=="MOUSEMOVE":
                candidate=self._raycast_object(context,x,y)
                picker.hovered_name=getattr(candidate,"name","") if candidate else ""
                if candidate is None:
                    picker.message=f"{RESOURCE_LABELS[picker.viewport_kind]} · наведите на объект · Esc отмена"
                    picker.message_level="INFO"
                else:
                    valid,reason,_meta=validate_resource(picker.viewport_kind,candidate,
                        path=self._path(),definition=self._definition())
                    picker.message=("✓ " if valid else "✕ ")+candidate.name+("" if valid else " · "+reason)
                    picker.message_level="SUCCESS" if valid else "ERROR"
                return {"RUNNING_MODAL"}
            if event.type=="LEFTMOUSE" and event.value=="PRESS":
                candidate=self._raycast_object(context,x,y)
                if candidate is None:
                    # Curves do not participate in Scene.ray_cast. Blender's
                    # own selector remains a safe fallback, but validation is
                    # still performed before any Definition pointer changes.
                    try:
                        bpy.ops.view3d.select(location=(int(x),int(y)),extend=False)
                        candidate=context.view_layer.objects.active
                    except Exception:
                        candidate=None
                if candidate is None:
                    picker.message="Объект под курсором не найден";picker.message_level="ERROR"
                else:self._apply_resource(picker.viewport_kind,candidate,context)
                return {"RUNNING_MODAL"}
            if event.type in {"MIDDLEMOUSE","WHEELUPMOUSE","WHEELDOWNMOUSE","NDOF_MOTION"}:
                return {"PASS_THROUGH"}
            return {"RUNNING_MODAL"}

        if (picker is not None and picker.open_kind
                and not window_visibility.is_hidden(window_visibility.scope_from_context(context,region=region),
                                                     "UV_WALL_RESOURCE_PICKER")
                and event.value=="PRESS"):
            rows=self._resource_rows(picker.open_kind)
            if event.type=="ESC":picker.close_dropdown();self._picker_editor=None;return {"RUNNING_MODAL"}
            if event.type in {"WHEELUPMOUSE","WHEELDOWNMOUSE"}:
                delta=-1 if event.type=="WHEELUPMOUSE" else 1
                picker.active_index=max(0,min(max(0,len(rows)-1),picker.active_index+delta))
                # Never leak wheel events to Blender viewport zoom while a
                # resource catalogue is open over the scene.
                return {"RUNNING_MODAL"}
            if event.type in {"UP_ARROW","DOWN_ARROW","PAGE_UP","PAGE_DOWN","HOME","END"}:
                if event.type=="HOME": picker.active_index=0
                elif event.type=="END": picker.active_index=max(0,len(rows)-1)
                else:
                    delta={"UP_ARROW":-1,"DOWN_ARROW":1,"PAGE_UP":-8,"PAGE_DOWN":8}[event.type]
                    picker.active_index=max(0,min(max(0,len(rows)-1),picker.active_index+delta))
                return {"RUNNING_MODAL"}
            if event.type in {"RET","NUMPAD_ENTER"}:
                if rows:
                    picker.active_index=min(max(0,picker.active_index),len(rows)-1)
                    self._apply_resource(picker.open_kind,rows[picker.active_index][0],context)
                return {"RUNNING_MODAL"}
            if event.type=="BACK_SPACE":
                picker.query=picker.query[:-1];picker.active_index=0;return {"RUNNING_MODAL"}
            char=getattr(event,"unicode","") or ""
            if char and char.isprintable() and not event.ctrl and not event.alt:
                picker.query+=char;picker.active_index=0;return {"RUNNING_MODAL"}
        if (picker is not None and picker.open_kind
                and not window_visibility.is_hidden(window_visibility.scope_from_context(context,region=region),
                                                     "UV_WALL_RESOURCE_PICKER")
                and event.type=="LEFTMOUSE" and event.value=="PRESS"):
            # Modal callbacks can precede the first redraw on DPI/region
            # changes. Never accept a stale hitbox from another pixel map.
            pixel_map=(int(region.width),int(region.height)) if region else None
            if (self._picker_rect is None or
                    getattr(self,'_picker_rendered_region',None)!=pixel_map):
                self._buttons=[b for b in self._buttons if not str(b[0]).startswith('RP:')]
                if context.area is not None:context.area.tag_redraw()
                return {"RUNNING_MODAL"}
            picker_key=self._hit_button(x,y)
            if picker_key and picker_key.startswith("RP:"):
                self._apply_workspace_button(picker_key,context)
            else:
                picker.close_dropdown();self._picker_editor=None
            return {"RUNNING_MODAL"}

        if self._window_stack is not None and region is not None:
            # Layout is authoritative only at draw time (with ALL obstacles).
            # A modal-only reflow used to move editor rectangles between draw
            # and click while leaving old hitboxes live. On resize fail closed.
            if self._window_stack.last_drawn_region != (region.width,region.height):
                for win in self._window_stack.windows:
                    win.hitboxes.clear()
                if context.area is not None:context.area.tag_redraw()
            definition=self._definition()
            if definition is not None:
                self._window_stack.refresh(definition,total_wall_height(definition))

        if self._window_stack is not None and not self._radial and not self._numeric and event.type != 'TIMER':
            if event.type in {'C','X','V'} and event.value=='PRESS' and event.ctrl:
                from types import SimpleNamespace
                event_to_edit=SimpleNamespace(
                    type=event.type,value='PRESS',ctrl=True,alt=False,
                    unicode='',clipboard=context.window_manager.clipboard,
                    clipboard_set=lambda text: setattr(context.window_manager,'clipboard',text))
            else:
                event_to_edit=event
            blf.size(0,11)
            action,window=self._window_stack.event(
                event_to_edit,x,y,measure_text=lambda value:blf.dimensions(0,str(value))[0])
            if action=='APPLY':
                self._apply_editor_window(context,window)
                return {"RUNNING_MODAL"}
            if action=='LIST':
                if window.kind=='CORNICE':
                    resource_kind,field='PROFILE','cornice_profile'
                elif window.kind=='CAP':
                    field=window.list_field if window.list_field in {'cap_material','bottom_cap_material'} else 'cap_material'
                    resource_kind='MATERIAL'
                else:
                    resource_kind,field='MATERIAL','inner_surface_material'
                self._picker_editor=(window,resource_kind,field)
                window_visibility.set_hidden(window_visibility.scope_from_context(context), 'UV_WALL_RESOURCE_PICKER', False)
                self._resource_picker.open(resource_kind)
                return {"RUNNING_MODAL"}
            if action=='PICK':
                active=context.view_layer.objects.active
                if window.kind=='CORNICE':
                    valid,reason,_meta=validate_resource('PROFILE',active,
                        path=self._path(),definition=self._definition())
                    if not valid:
                        window.error=reason
                    else:
                        window.draft['cornice_profile']=active.name;window.error=''
                        self._apply_editor_window(context,window)
                elif window.kind in {'PLANT','CAP'}:
                    material=getattr(active,'active_material',None) if active else None
                    valid,reason,_meta=validate_resource('MATERIAL',material,
                        path=self._path(),definition=self._definition())
                    valid = material is not None
                    if not valid:window.error=reason
                    else:
                        if window.kind=='CAP':
                            field=window.list_field if window.list_field in {'cap_material','bottom_cap_material'} else 'cap_material'
                        else: field='inner_surface_material'
                        window.draft[field]=material.name
                        window.error=''
                return {"RUNNING_MODAL"}
            if action in ('CONSUMED','CLOSED'):
                return {"RUNNING_MODAL"}
            if event.type=='ESC' and event.value=='PRESS' and self._window_stack.windows and not self._drag_kind:
                self._window_stack.close(self._window_stack.windows[-1].kind)
                return {"RUNNING_MODAL"}
        if event.type == "TIMER" and getattr(self, "_settle_timer", None) is not None:
            try:
                area_pointers={area.as_pointer() for area in context.window.screen.areas}
                if self._area_ptr not in area_pointers or context.window.workspace != self._workspace_ref:
                    self._finish(context)
                    return {"FINISHED"}
            except (AttributeError, ReferenceError, RuntimeError):
                self._finish(context)
                return {"FINISHED"}
            self._sync_follow_selection(context)
            if getattr(context,'mode','OBJECT')=='OBJECT' and self._drag_kind is None:
                fingerprint=self._path_fingerprint()
                if self._last_path_fingerprint is not None and fingerprint is not None and fingerprint!=self._last_path_fingerprint:
                    self._last_path_fingerprint=fingerprint
                    self._smart_segment_key=None;self._spatial_previous.clear()
                    self._engine.invalidate(Dirty.ALL);self._engine.update(force=True,quality='SETTLED')
                    self._toast('Путь изменён · предпросмотр обновлён','INFO')
                elif fingerprint is not None:
                    self._last_path_fingerprint=fingerprint
            if self._hover is not None or self._drag_kind is not None:
                if context.area and context.area.as_pointer()==self._area_ptr:
                    context.area.tag_redraw()  # bounded by 0.12s session timer
            if self._engine is not None and self._engine.settled_due():
                try:
                    self._engine.update(force=True, quality="SETTLED")
                    if context.area and context.area.as_pointer()==self._area_ptr:
                        context.area.tag_redraw()
                except Exception:
                    pass
            return {"RUNNING_MODAL"}

        if self._numeric:
            # Mouse press is handled BEFORE the general PRESS/keyboard branch.
            # Otherwise Blender sends LEFTMOUSE as PRESS and the general
            # branch consumes it, making outside-click cancellation unreachable.
            if event.type=='MOUSEMOVE' and getattr(self,'_numeric_mouse_selecting',False):
                rect=self._spatial_rects.get(getattr(self,'_inline_marker',None))
                edit=getattr(self,'_numeric_editor',None)
                if rect is not None and edit is not None:
                    from issmaker_modeling_assistant.core.text_input_core import caret_from_x
                    blf.size(0,12)
                    caret=caret_from_x(edit.text,x,rect[0]+11,
                                      lambda value:blf.dimensions(0,str(value))[0])
                    edit.click(caret,extend=True)
                    self._numeric_caret=edit.caret;self._numeric_selected=edit.selected
                return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value=='RELEASE' and getattr(self,'_numeric_mouse_selecting',False):
                self._numeric_mouse_selecting=False
                return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value in {'PRESS','DOUBLE_CLICK'}:
                rect=self._spatial_rects.get(getattr(self,'_inline_marker',None))
                if rect is None or not _rect_hit(x,y,rect):
                    self._numeric_mouse_selecting=False
                    self._restore_drag();self._numeric=False;self._numeric_editor=None
                    self._inline_marker=None
                    self._toast('Ввод отменён · Enter применяет значение', 'INFO', 2.0)
                    return {'PASS_THROUGH'}
                edit=self._numeric_editor
                if edit is not None:
                    if event.value=='DOUBLE_CLICK':
                        edit.select_all()
                        self._numeric_mouse_selecting=False
                        self._numeric_selected=True
                        self._numeric_caret=edit.caret
                        return {'RUNNING_MODAL'}
                    try:
                        from issmaker_modeling_assistant.core.text_input_core import caret_from_x
                        blf.size(0,12)
                        caret=caret_from_x(edit.text,x,rect[0]+11,
                                          lambda value:blf.dimensions(0,str(value))[0])
                        edit.click(caret,extend=bool(event.shift))
                        self._numeric_mouse_selecting=(event.value=='PRESS')
                        self._numeric_caret=edit.caret
                        self._numeric_selected=edit.selected
                    except (ValueError,RuntimeError):pass
                return {'RUNNING_MODAL'}
            if event.value=='PRESS':
                if event.type=='ESC':
                    self._numeric=False;self._inline_marker=None
                    self._numeric_mouse_selecting=False;self._restore_drag()
                    return {'RUNNING_MODAL'}
                if event.type in {'RET','NUMPAD_ENTER'}:
                    if not self._numeric_buffer or self._numeric_buffer in {'-','+','.','-.','+.'}:
                        self._toast('Введите число перед подтверждением','WARNING')
                        return {'RUNNING_MODAL'}
                    if (self._numeric_editor is not None and
                            self._numeric_editor.valid_number() is None):
                        self._toast('Некорректное число','WARNING')
                        return {'RUNNING_MODAL'}
                    self._apply_numeric();self._numeric=False;self._inline_marker=None
                    self._numeric_mouse_selecting=False
                    self._numeric_editor=None
                    if not self._drag_has_change():
                        self._restore_drag();return {'RUNNING_MODAL'}
                    snapshot=self._drag_snapshot
                    surface_edits={str(self._drag_index):self._surface_drag_z} if self._drag_kind in {'SURFACE_Z','PATH_Z'} and self._drag_index is not None else None
                    labels={'HEIGHT':'высота стены','THICKNESS':'толщина стены','OFFSET':'положение стены',
                            'SURFACE_OFFSET':'высота поверхности','SURFACE_Z':'высота точки','SURFACE_ROT':'поворот поверхности',
                            'CORNICE_ROT':'поворот карниза','BOTTOM_LEVEL':'уровень низа'}
                    self._commit_or_restore(context,snapshot,quiet=False,surface_edits=surface_edits,
                                            history_label=labels.get(self._drag_kind,'изменение'))
                    self._drag_kind=None;self._drag_start_value=None;self._drag_start_mouse=None
                    self._drag_snapshot=None;self._drag_index=None;self._surface_drag_z=None
                    self._drag_touched=False
                    return {'RUNNING_MODAL'}
                edit=self._numeric_editor
                if edit is None:
                    edit=_precision.InlineNumericBuffer(self._numeric_buffer,len(self._numeric_buffer),len(self._numeric_buffer))
                    self._numeric_editor=edit
                cmd=None
                char=getattr(event,'unicode','') or ''
                if event.ctrl and not event.alt:
                    if event.type=='A':edit.select_all()
                    elif event.type=='C':context.window_manager.clipboard=edit.copy()
                    elif event.type=='X':context.window_manager.clipboard=edit.cut()
                    elif event.type=='V':edit.replace(str(context.window_manager.clipboard))
                    elif event.type=='Y':edit.undo(redo=True)
                elif event.type in {'LEFT_ARROW','RIGHT_ARROW','HOME','END'}:
                    direction={'LEFT_ARROW':'LEFT','RIGHT_ARROW':'RIGHT','HOME':'HOME','END':'END'}[event.type]
                    edit.move(direction,extend=bool(event.shift))
                elif event.type in {'BACK_SPACE','DEL','DELETE'}:
                    edit.erase(back=event.type=='BACK_SPACE')
                elif event.type in {'MINUS','NUMPAD_MINUS'}:edit.replace('-')
                elif char and all(c.lower() in '0123456789.,-+*/() cm' for c in char):edit.replace(char)
                else:
                    digits={'ZERO':'0','ONE':'1','TWO':'2','THREE':'3','FOUR':'4',
                            'FIVE':'5','SIX':'6','SEVEN':'7','EIGHT':'8','NINE':'9',
                            **{'NUMPAD_'+str(i):str(i) for i in range(10)},
                            'PERIOD':'.','NUMPAD_PERIOD':'.','COMMA':',',
                            'NUMPAD_PLUS':'+','NUMPAD_ASTERIX':'*','NUMPAD_SLASH':'/'}
                    if event.type in digits:edit.replace(digits[event.type])
                if edit.text!=self._numeric_buffer:
                    self._numeric_buffer=edit.text
                    self._apply_numeric()
                self._numeric_caret=edit.caret
                self._numeric_selected=edit.selected
                if context.area:context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if self._radial:
            if event.type=='Q' and event.value=='RELEASE':return {'RUNNING_MODAL'}
            if event.type in {'ESC','RIGHTMOUSE','Q'} and event.value=='PRESS':
                self._radial=False;self._quick_rect=None;return {'RUNNING_MODAL'}
            if event.type in {'MOUSEMOVE','LEFTMOUSE'}:
                q=self._quick_rect
                if q is None and region is not None:
                    q=floating_quick_rect(self._radial_center,(region.width,region.height))
                choice=None
                if q:
                    rx,ry,rw,rh=q
                    if rx<=x<=rx+rw and ry+12<=y<=ry+55:
                        idx=int((x-(rx+10.))//((rw-20.-12.)/3.+6.))
                        if 0<=idx<3:choice=idx
                self._radial_choice=choice
                if event.type=='LEFTMOUSE' and event.value=='PRESS':
                    self._radial=False;self._quick_rect=None
                    if choice is not None:
                        self._set_mode(('WALL','SURFACE','CORNICE')[choice],context,commit=True)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if self._spin_state is not None:
            if event.type in {'LEFT_SHIFT','RIGHT_SHIFT','LEFT_CTRL','RIGHT_CTRL'}:
                self._spin_state['modifiers']=(bool(event.shift),bool(event.ctrl))
                if context.area:context.area.tag_redraw()
            if event.type=='ESC' and event.value=='PRESS':
                self._finish_marker_spin(context,cancel=True);return {'RUNNING_MODAL'}
            if event.type=='MOUSEMOVE':
                self._update_marker_spin(context,x,y,shift=event.shift,ctrl=event.ctrl)
                return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value=='RELEASE':
                self._finish_marker_spin(context);return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        active_interaction=self._drag_kind is not None
        if active_interaction and event.type in {'LEFT_SHIFT','RIGHT_SHIFT','LEFT_CTRL','RIGHT_CTRL'}:
            self._precision_modifiers=(bool(event.shift),bool(event.ctrl))
            if context.area:context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {"LEFTMOUSE","RIGHTMOUSE","MOUSEMOVE"} and not inside and not active_interaction:
            return {"PASS_THROUGH"}
        if event.type in {"MIDDLEMOUSE","WHEELUPMOUSE","WHEELDOWNMOUSE","NDOF_MOTION","NDOF_BUTTON_MENU"}: return {"PASS_THROUGH"}
        # Undo/Redo is handled before popup/picker routing above.

        # Native number/NumPad keys remain owned by Blender.
        # Q/R are native Blender hotkeys. Build and mode switching have
        # visible controls; no global override without an explicit keymap.
        if event.type=="TAB" and event.value=="PRESS" and self._drag_kind:
            self._begin_numeric();return {"RUNNING_MODAL"}

        if event.type in {"ESC","RIGHTMOUSE"} and event.value=="PRESS" and self._drag_kind:
            self._restore_drag();return {"RUNNING_MODAL"}

        if event.type=="ESC" and event.value=="PRESS":
            if self._drag_kind:
                self._restore_drag();return {"RUNNING_MODAL"}
            self._finish(context);return {"FINISHED"}

        if event.type=="MOUSEMOVE":
            if self._drag_kind:
                if self._drag_start_mouse and _distance((x,y),self._drag_start_mouse)<4.0:
                    return {"RUNNING_MODAL"}
                self._apply_drag(x,y,event.shift,event.ctrl);return {"RUNNING_MODAL"}
            self._hover=None
            self._hover=self._hit_button(x,y)
            if self._hover is None:
                h=self._hit_handle(x,y)
                if h:self._hover=h[0]
            return {"RUNNING_MODAL"} if self._hover else {"PASS_THROUGH"}

        # Blender can report a DOUBLE_CLICK on the first active editor event.
        # Treat a standalone double-click on an inline numeric half as a valid
        # start+Select All, not a button click that opens the old popup.
        if event.type=="LEFTMOUSE" and event.value=="DOUBLE_CLICK" and not self._numeric:
            dbl_key=self._hit_button(x,y)
            dbl_kind=None; dbl_marker=None
            if dbl_key and dbl_key.endswith(':RIGHT'):
                dbl_marker=dbl_key.rsplit(':',1)[0]
                d=self._definition()
                action=_universal.marker_action(dbl_marker,'RIGHT',
                    double=bool(d.create_inner) if d is not None else False,
                    top_cap=bool(d.create_top_cap) if d is not None else False,
                    plant=bool(d.create_inner_surface) if d is not None else False)
                dbl_kind={'INLINE_OFFSET':'OFFSET','INLINE_THICKNESS':'THICKNESS',
                          'INLINE_HEIGHT':'HEIGHT','INLINE_SURFACE_Z':'SURFACE_OFFSET',
                          'INLINE_SURFACE_ROT':'SURFACE_ROT',
                          'INLINE_CORNICE_ROT':'CORNICE_ROT'}.get(action)
            elif dbl_key in {'W_HEIGHT','S_Z','S_ROT','C_ROT'}:
                dbl_marker=dbl_key
                dbl_kind={'W_HEIGHT':'HEIGHT','S_Z':'SURFACE_OFFSET',
                          'S_ROT':'SURFACE_ROT','C_ROT':'CORNICE_ROT'}[dbl_key]
            if dbl_kind and self._start_marker_numeric(context,dbl_marker,dbl_kind):
                if self._numeric_editor is not None:
                    self._numeric_editor.select_all()
                    self._numeric_selected=True
                return {'RUNNING_MODAL'}

        if event.type=="LEFTMOUSE" and event.value=="PRESS" and getattr(self,'_marker_choice',None):
            matched=next((value for value,rect in self._marker_choice_rows if _rect_hit(x,y,rect)),None)
            marker=self._marker_choice
            self._marker_choice=None;self._marker_choice_rows=[]
            if matched is not None:self._apply_marker_choice(context,marker,matched)
            return {'RUNNING_MODAL'}
        if event.type=="LEFTMOUSE" and event.value=="PRESS":
            # Popovers are topmost. Their buttons must not be swallowed by a
            # neighbouring Z/corner handle's larger touch target.
            control=self._hit_button(x,y)
            if control in {"CORNER_SMOOTH","CORNER_SHARP"}:
                self._apply_workspace_button(control,context)
                return {"RUNNING_MODAL"}
            if control and control.startswith(("Z:RESET:","S:RESET:")):
                index=int(control.rsplit(':',1)[1])
                if control.startswith('S:RESET:'):
                    self._surface_point_selected=index
                self._reset_wall_height(context,index)
                return {"RUNNING_MODAL"}
            if control and control.startswith("FOCUS:"):
                try:
                    _prefix,kind,index=control.split(':',2)
                    self._focus_vertex(context,int(index),{'W':'точка стены','S':'точка поверхности','C':'угол карниза'}.get(kind,'точка'))
                except (ValueError,IndexError):pass
                return {"RUNNING_MODAL"}
            if control and control.startswith('SPIN:'):
                self._start_marker_spin(context,control[5:],x,y)
                return {'RUNNING_MODAL'}
            h=self._hit_handle(x,y)
            if h:
                key,pos,radius,meta=h
                if key.startswith("SURFACE_Z:"):
                    self._surface_point_selected=int(meta["vertex_index"])
                if key.startswith("CORNER:"):
                    try:
                        self._corner_selected=int(key.split(":",1)[1])
                        uid_map=sync_corner_uids(self._path())
                        self._corner_selected_uid=next((int(uid) for uid,meta in uid_map.items()
                            if int(meta[0])==self._corner_selected),0)
                    except (ValueError,TypeError,IndexError,ReferenceError):
                        self._corner_selected=-1;self._corner_selected_uid=0
                    return {"RUNNING_MODAL"}
                self._begin_drag(key,x,y,meta);return {"RUNNING_MODAL"}
            key=self._hit_button(x,y)
            if self._apply_workspace_button(key,context):
                return {"RUNNING_MODAL"}
            if key and key.startswith("MODE_"):
                self._set_mode(key[5:],context,commit=True);return {"RUNNING_MODAL"}
            if key in {'W_CAP','W_PLANT'}:
                self._apply_context_button({'W_CAP':'W_TOP','W_PLANT':'W_PLANT'}[key],context)
                return {'RUNNING_MODAL'}
            if key=='S_DIR':
                self._apply_context_button('S_NORMAL',context)
                return {'RUNNING_MODAL'}
            if key and key.endswith((':LEFT',':RIGHT')):
                parent,part=key.rsplit(':',1)
                d=self._definition()
                action=_universal.marker_action(parent,part,double=bool(d.create_inner),
                          top_cap=bool(d.create_top_cap),plant=bool(d.create_inner_surface))
                if action is None:return {'RUNNING_MODAL'}
                toggles={'TOGGLE_CAP':'W_TOP','TOGGLE_PLANT':'W_PLANT',
                         'TOGGLE_CORNICE_SIDE':'C_FLIP','TOGGLE_SURFACE_DIRECTION':'S_NORMAL'}
                if action in toggles:
                    self._apply_context_button(toggles[action],context)
                    return {'RUNNING_MODAL'}
                numbers={'INLINE_HEIGHT':'HEIGHT','INLINE_OFFSET':'OFFSET',
                         'INLINE_THICKNESS':'THICKNESS',
                         'INLINE_SURFACE_Z':'SURFACE_OFFSET',
                         'INLINE_SURFACE_ROT':'SURFACE_ROT',
                         'INLINE_CORNICE_ROT':'CORNICE_ROT'}
                if action in numbers:
                    self._start_marker_numeric(context,parent,numbers[action]);return {'RUNNING_MODAL'}
                if action in {'DETAIL_CONSTRUCTION','DETAIL_POSITION'}:
                    self._open_marker_choice(context,parent);return {'RUNNING_MODAL'}
                if action=='PICK_PROFILE':
                    self._open_spatial_editor(context,'C_PROFILE');return {'RUNNING_MODAL'}
                self._open_spatial_editor(context,parent)
                return {'RUNNING_MODAL'}
            direct={'W_HEIGHT':'HEIGHT','S_Z':'SURFACE_OFFSET',
                    'S_ROT':'SURFACE_ROT','C_ROT':'CORNICE_ROT'}
            if key in direct and self._start_marker_numeric(context,key,direct[key]):
                return {'RUNNING_MODAL'}
            if self._open_spatial_editor(context,key):
                return {'RUNNING_MODAL'}
            if key=="C_PICK":
                try:bpy.ops.object.uv_wall_use_active_as_profile('INVOKE_DEFAULT')
                except Exception as exc:self.report({"WARNING"},str(exc))
                return {"RUNNING_MODAL"}
            if key and key.startswith('FOCUS:'):
                try:
                    _prefix,kind,index=key.split(':',2);self._focus_vertex(context,int(index),kind)
                except (ValueError,IndexError):pass
                return {"RUNNING_MODAL"}
            if key and key.startswith(('Z:RESET:','S:RESET:')):
                try:
                    if key.startswith('S:RESET:'):
                        self._surface_point_selected=int(key.rsplit(':',1)[1])
                    self._reset_wall_height(context,int(key.rsplit(':',1)[1]))
                except (ValueError,IndexError):pass
                return {"RUNNING_MODAL"}
            if key and self._apply_context_button(key,context):
                return {"RUNNING_MODAL"}
            # Clicking empty scene space must dismiss the selected point/corner
            # popover. Let Blender process its normal canvas selection too.
            self._dismiss_canvas_context()
            return {"PASS_THROUGH"}
        if event.type=="LEFTMOUSE" and event.value=="RELEASE" and self._drag_kind:
            if self._drag_start_mouse and _distance((x,y),self._drag_start_mouse)<4.0:
                target={"HEIGHT":"W_HEIGHT","THICKNESS":"W_THICKNESS",
                        "OFFSET":"W_WALL"}.get(self._drag_kind)
                if self._drag_kind == "PATH_Z": self._wall_point_selected=self._drag_index
                self._restore_drag()
                if target:self._start_marker_numeric(context,target,{'W_HEIGHT':'HEIGHT','W_THICKNESS':'THICKNESS','W_WALL':'OFFSET'}[target])
                return {"RUNNING_MODAL"}
            if not self._drag_has_change():
                self._restore_drag()
                return {"RUNNING_MODAL"}
            snapshot=self._drag_snapshot
            surface_edits={str(self._drag_index):self._surface_drag_z} if self._drag_kind in {"SURFACE_Z","PATH_Z"} else None
            label={"HEIGHT":"высота стены","THICKNESS":"толщина стены","OFFSET":"положение стены",
                   "SURFACE_Z":"высота поверхности","PATH_Z":"высота точки стены",
                   "BOTTOM_LEVEL":"уровень низа"}.get(self._drag_kind,"изменение")
            self._commit_or_restore(context,snapshot,quiet=False,surface_edits=surface_edits,history_label=label)
            self._drag_kind=None;self._drag_start_value=None;self._drag_start_mouse=None;self._drag_snapshot=None
            self._drag_index=None;self._surface_drag_z=None;self._drag_touched=False
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}


CLASSES=(UVWALL_OT_viewport_inspector_exit,UVWALL_OT_finalize_result,UVWALL_OT_viewport_inspector)


def register():
    # Same hot-reload rule as the Definition RNA: modal operator classes from a
    # previous submodule object must not remain registered after an in-place ZIP
    # update.
    for cls in CLASSES:
        stale = getattr(bpy.types, cls.__name__, None)
        if stale is not None and stale is not cls:
            try: bpy.utils.unregister_class(stale)
            except Exception: pass
        if getattr(bpy.types, cls.__name__, None) is not cls:
            bpy.utils.register_class(cls)
    bpy.types.WindowManager.uvwall_viewport_active=bpy.props.BoolProperty(default=False,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_viewport_lens=bpy.props.EnumProperty(name="UV Wall Lens",items=LENS_ITEMS,default="BUILD",options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_visual_effect=bpy.props.EnumProperty(name="Эффект окон",items=VISUAL_EFFECT_ITEMS,default="GRAPHITE",options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_preview_mode=bpy.props.EnumProperty(name="Preview",items=PREVIEW_MODE_ITEMS,default="MATERIAL",options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_selection_mode=bpy.props.EnumProperty(name="Selection",items=(("FOLLOW","Follow Selection","Follow the active UV Wall path"),("LOCK","Lock Selection","Keep the current UV Wall path")),default="FOLLOW",options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_display_open=bpy.props.BoolProperty(default=False,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_show_arrows=bpy.props.BoolProperty(default=True,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_show_tooltips=bpy.props.BoolProperty(default=True,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_show_labels=bpy.props.BoolProperty(default=True,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_viewport_commit_active=bpy.props.BoolProperty(default=False,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_build_transaction_active=bpy.props.BoolProperty(default=False,options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_pending_definition_json=bpy.props.StringProperty(default="",options={"SKIP_SAVE"})
    bpy.types.WindowManager.uvwall_pending_surface_edits_json=bpy.props.StringProperty(default="",options={"SKIP_SAVE"})
    _remove_stale_handler(bpy.app.handlers.load_pre,"_close_sessions_before_load")
    _remove_stale_handler(bpy.app.handlers.load_post,"_reset_runtime_after_load")
    bpy.app.handlers.load_pre.append(_close_sessions_before_load)
    bpy.app.handlers.load_post.append(_reset_runtime_after_load)


def unregister():
    _close_sessions_before_load(None)
    _remove_stale_handler(bpy.app.handlers.load_pre,"_close_sessions_before_load")
    _remove_stale_handler(bpy.app.handlers.load_post,"_reset_runtime_after_load")
    reset_build_guard(bpy.context)
    for name in ("uvwall_viewport_active","uvwall_viewport_lens","uvwall_visual_effect","uvwall_preview_mode","uvwall_selection_mode","uvwall_display_open","uvwall_show_arrows","uvwall_show_tooltips","uvwall_show_labels","uvwall_viewport_commit_active","uvwall_build_transaction_active","uvwall_pending_definition_json","uvwall_pending_surface_edits_json"):
        try:
            if hasattr(bpy.types.WindowManager,name):delattr(bpy.types.WindowManager,name)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    for cls in reversed(CLASSES):
        try:
            if getattr(cls, 'is_registered', False):
                bpy.utils.unregister_class(cls)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
