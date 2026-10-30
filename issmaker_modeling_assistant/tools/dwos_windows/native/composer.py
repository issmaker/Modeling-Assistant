from __future__ import annotations

import math
from contextlib import contextmanager
import json
import time
import bpy
import blf
import gpu
from bpy.types import Operator
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty, PointerProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .numeric import parse_meter_text, format_meter
from .parameter_consistency_core import split_field_value, verify_unique_rows
from .interior_core import MAX_INTERIOR_DEPTH_M
from .window_builder import rebuild_window
from .frame_core import clear_rectangles, SplitSpec, interior_centerline_rectangle
from .zone_core import decode as decode_zones, layout as zone_layout, find as zone_find
from .dwos_log import event as log_event, exception as log_exception
from .window_assembly import assembly_action_ready
from .composer_layout import (floating_rect, declutter_handles, screen_safe_panel, screen_safe_multi_panel,
    scope_toolbar_rects, compact_panel_height, SCOPE_BUTTONS, SCOPE_HINTS,
    bottom_center_action_rect, overlap_area, ComposerPixelMap, popup_scale_for_viewport)
from .viewport_layout_qa import fit_inside as layout_fit_inside, fit_text as layout_fit_text, panel_height_for_state
from issmaker_modeling_assistant.core.window_manager import place_unobstructed
from issmaker_modeling_assistant.core.window_visibility import scope_from_context, is_hidden
from issmaker_modeling_assistant.core.ui_layout_scaling import (from_preferences as _base_ui_profile,
    select_font as _ui_select_font, inspect_layout as _ui_inspect_layout, record_layout as _ui_record_layout)
from .alignment_context import select_compatible, marquee_compatible, compact_align_axis, place_align_button
from .viewport_interaction import rectangle_selected_segments, preview_position
from .viewport_scissor_core import place_scissor_icons, projected_zone_label
from .mullion_snap_core import snap_position
from .health_blender import health_of, related_names
from .studio_ui_core import (health_short, health_extra_height, place_mullion_tag,
    mullion_position, focus_mullion_id)
from .numeric_input_core import edit as numeric_edit, caret_from_x
from .composer_presentation_core import (rounded_outline, constructor_guides,
    focus_card_rect, composer_hover_help)
from .dwos_history import push as history_push, undo as history_undo, redo as history_redo, depths as history_depths
from .mullion_core import parse_ids, encode_ids, targets, entries, apply_zone_targets, legacy_targets
from .zone_multi_core import toggle_zone
from .adaptive_opening_core import map_svd_local
from .corner_rules import corner_posts as calc_corner_posts, clamp_split_center, validate_split_rows
from .corner_post_policy_core import evaluate_corner_post, require_post_allowed
from .branch_core import (decode_state as decode_branch_state, layout_state as branch_layout_state,
    branch_for_s as branch_for_s_coord, rows_as_dicts as branch_rows_as_dicts,
    apply_positions as apply_branch_positions)
from .batch_edit import (
    resolve_window,
    target_windows,
    last_copy_result,
    apply_property_to_scope,
    apply_optimization_to_scope,
    apply_frame_material_to_scope,
)

_ACTIVE = None

# UI-only draw scope. Blender's draw callbacks are evaluated on the main UI
# thread; the scoped context also restores state if a draw function raises.
_PANEL_PIXELS = None
_PANEL_FONT_SCALE = 1.0

@contextmanager
def _composer_pixel_space(anchor, scale, font_scale):
    global _PANEL_PIXELS, _PANEL_FONT_SCALE
    previous=(_PANEL_PIXELS,_PANEL_FONT_SCALE)
    _PANEL_PIXELS=ComposerPixelMap(anchor[0],anchor[1],scale)
    _PANEL_FONT_SCALE=float(font_scale)
    try:
        yield
    finally:
        _PANEL_PIXELS,_PANEL_FONT_SCALE=previous


def _mapped_xyxy(x0,y0,x1,y1):
    return (_PANEL_PIXELS.xyxy(x0,y0,x1,y1) if _PANEL_PIXELS is not None
            else (x0,y0,x1,y1))


def _mapped_point(x,y):
    return _PANEL_PIXELS.point(x,y) if _PANEL_PIXELS is not None else (x,y)


from .viewport_theme import (PANEL_BG,PANEL_SURFACE,PANEL_HOVER,PANEL_BORDER,TEXT,MUTED,MINT,ORANGE,RED,BLUE,SHADOW)
from issmaker_modeling_assistant.core.design_tokens import component_style as _component_style
from issmaker_modeling_assistant.core.workspace_components import component_visual as _workspace_visual
from issmaker_modeling_assistant.core import viewport_skin as _viewport_skin
from issmaker_modeling_assistant.core import window_shell as _shell
from issmaker_modeling_assistant.core import theme_engine as _theme_engine


def _ui_profile(prefs):
    """Viewport-aware scale for both Composer paint and inverse click mapping."""
    return _base_ui_profile(prefs,context=bpy.context)


def _dwos_ui_snapshot():
    try:
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        prefs=addon.preferences if addon else None
    except (RuntimeError, ReferenceError, AttributeError, TypeError):
        prefs=None
    return _theme_engine.from_preferences(prefs)



def _rect(x0, y0, x1, y1, color):
    x0,y0,x1,y1=_mapped_xyxy(x0,y0,x1,y1)
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(sh, 'TRIS', {'pos': [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]}, indices=((0, 1, 2), (0, 2, 3)))
    sh.bind(); sh.uniform_float('color', color); batch.draw(sh)



def _soft_rect(x0, y0, x1, y1, color, radius=7.0):
    """DWOS + UV Wall: exact same Studio GPU primitive."""
    x0,y0,x1,y1=_mapped_xyxy(x0,y0,x1,y1)
    if _PANEL_PIXELS is not None:radius*= _PANEL_PIXELS.scale
    from issmaker_modeling_assistant.core.studio_gpu_components import fill_rect
    if not fill_rect(x0,y0,x1,y1,color,radius):
        _rect(x0,y0,x1,y1,color) if _PANEL_PIXELS is None else _draw_direct_rect(x0,y0,x1,y1,color)


def _draw_direct_rect(x0,y0,x1,y1,color):
    """Fallback expects physical coordinates, never remaps a second time."""
    sh=gpu.shader.from_builtin('UNIFORM_COLOR')
    verts=((x0,y0),(x1,y0),(x1,y1),(x0,y0),(x1,y1),(x0,y1))
    batch=batch_for_shader(sh,'TRIS',{'pos':verts})
    sh.bind();sh.uniform_float('color',color);batch.draw(sh)


def _soft_outline(x0,y0,x1,y1,color,radius=7.0,width=1.0):
    from issmaker_modeling_assistant.core.studio_gpu_components import outline_rect
    px0,py0,px1,py1=_mapped_xyxy(x0,y0,x1,y1)
    scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.
    if outline_rect(px0,py0,px1,py1,color,radius*scale,width*scale):return
    pts=rounded_outline(x0,y0,x1-x0,y1-y0,radius)
    if pts:_line(list(pts)+[pts[0]],color,width)


def _line(points, color, width=1.0):
    if _PANEL_PIXELS is not None:
        points=[_mapped_point(p[0],p[1]) for p in points]
        width*= _PANEL_PIXELS.scale
    if len(points) < 2:
        return
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.line_width_set(max(1.0, width))
    batch = batch_for_shader(sh, 'LINE_STRIP', {'pos': points})
    sh.bind(); sh.uniform_float('color', color); batch.draw(sh)
    gpu.state.line_width_set(1.0)


def _circle(cx, cy, radius, color, segments=24):
    pts = []
    for i in range(segments + 1):
        a = (i / segments) * math.tau
        pts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))
    _line(pts, color, 1.8)


def _corner_brackets(rect,color,length=14.0,width=1.2):
    if not rect:return
    x,y,w,h=rect;l=max(5.0,min(float(length),w*.22,h*.22))
    for pts in (
        [(x,y+l),(x,y),(x+l,y)],[(x+w-l,y),(x+w,y),(x+w,y+l)],
        [(x,y+h-l),(x,y+h),(x+l,y+h)],[(x+w-l,y+h),(x+w,y+h),(x+w,y+h-l)]):
        _line(pts,color,width)


def _text(x, y, text, size=11, color=TEXT, *, pixel_size=False):
    x,y=_mapped_point(x,y)
    if _PANEL_PIXELS is not None and not pixel_size:
        size=float(size)*_PANEL_FONT_SCALE
    from ....core.hud_readability import snap as _snap_text
    blf.size(0, max(7,int(round(size)))); blf.position(0, _snap_text(x), _snap_text(y), 0)
    try:
        blf.color(0, *color)
    except Exception:
        pass
    blf.draw(0, str(text))


def _text_fitted(x, y, text, max_width, size=11, color=TEXT, *, return_font=False):
    """Measure the exact BLF font in the ACTUAL physical space of the panel.

    Width, height and output text share the same transform. Never draw text
    through an adjacent field even when user font/window scales are different.
    """
    try:
        item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        profile=_ui_profile(item.preferences if item else None)
    except (AttributeError,RuntimeError,TypeError):
        profile=_ui_profile(None)
    scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.0
    available=max(0.,float(max_width)*scale)
    preferred=_PANEL_FONT_SCALE if _PANEL_PIXELS is not None else profile.font
    ceiling=max(10,int(round(float(size)*1.35*scale)))
    def width_at(value,font):
        blf.size(0,max(7,int(round(font))))
        return float(blf.dimensions(0,str(value))[0])
    chosen=_ui_select_font(str(text),size,available,width_at,preferred,
                            minimum_px=10,max_height=ceiling)
    if chosen is None:
        # If even a readable font won't fit, visibly elide rather than overlap.
        chosen=min(ceiling,max(10,int(round(size*min(preferred,scale)))))
        fitted=layout_fit_text(str(text),available,chosen,
                               width_fn=lambda value:width_at(value,chosen))
    else:
        fitted=str(text)
    _text(x,y,fitted,chosen,color,pixel_size=True)
    return (fitted, chosen) if return_font else fitted


def _glyph(kind, x, y, size=14.0, color=TEXT):
    """Small retained vector glyphs: deterministic, font-independent, no external PNG."""
    c=float(size);cx=float(x)+c*0.5;cy=float(y)+c*0.5
    if kind=='vertical':
        _line([(cx-4,cy-5),(cx-4,cy+5)],color,1.1)
        _line([(cx+4,cy-5),(cx+4,cy+5)],color,1.1)
        _rect(cx-1,cy-6,cx+1,cy+6,color)
    elif kind=='horizontal':
        _line([(cx-6,cy-4),(cx+6,cy-4)],color,1.1)
        _line([(cx-6,cy+4),(cx+6,cy+4)],color,1.1)
        _rect(cx-6,cy-1,cx+6,cy+1,color)
    elif kind=='copy':
        _line([(cx-5,cy-3),(cx-5,cy+5),(cx+3,cy+5)],color,1.1)
        _line([(cx-2,cy-6),(cx+6,cy-6),(cx+6,cy+2),(cx-2,cy+2),(cx-2,cy-6)],color,1.2)
    elif kind=='merge':
        _line([(cx-7,cy-5),(cx-7,cy+4),(cx,cy+4)],color,1.5)
        _line([(cx+7,cy+5),(cx+7,cy-4),(cx,cy-4)],color,1.5)
        _line([(cx-3,cy),(cx+3,cy)],color,1.5)
        _line([(cx,cy-3),(cx+3,cy),(cx,cy+3)],color,1.5)
    elif kind=='health':
        _line([(cx-6,cy),(cx-3,cy),(cx-1,cy+4),(cx+1,cy-4),(cx+3,cy),(cx+6,cy)],color,1.4)
    elif kind=='edit':
        _line([(cx-5,cy-5),(cx+3,cy+3),(cx+5,cy+1),(cx-3,cy-7)],color,1.45)
        _line([(cx-5,cy-5),(cx-3,cy-7)],color,1.2)
    elif kind=='check':
        _line([(cx-5,cy),(cx-1,cy-4),(cx+6,cy+5)],color,1.6)
    elif kind=='close':
        _line([(cx-4,cy-4),(cx+4,cy+4)],color,1.5)
        _line([(cx-4,cy+4),(cx+4,cy-4)],color,1.5)


def _distance_point_segment(p, a, b):
    px, py = p; ax, ay = a; bx, by = b
    vx = bx - ax; vy = by - ay; l2 = vx * vx + vy * vy
    if l2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    qx = ax + t * vx; qy = ay + t * vy
    return math.hypot(px - qx, py - qy)


class DWOS_OT_composer_set_value(Operator):
    bl_idname = 'dwos.composer_set_value'
    bl_label = 'Set Window Value'
    bl_options = {'REGISTER'}
    target: EnumProperty(items=[
        ('FRAME_WIDTH', 'Frame width', ''),
        ('INTERNAL_WIDTH', 'Internal width', ''),
        ('FRAME_DEPTH', 'Frame depth', ''),
        ('FRAME_OFFSET', 'Frame offset', ''),
        ('INTERIOR_OFFSET', 'Interior offset', ''),
        ('INTERIOR_DEPTH', 'Interior depth', ''),
        ('INTERIOR_CLEARANCE', 'Interior clearance', ''),
        ('GLASS_OFFSET', 'Glass offset', ''),
        ('GLASS_EDGE', 'Glass contour edge offset', ''),
        ('GLASS_GAP', 'Glass gap', ''),
    ])
    value_text: StringProperty(default='0')

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:
            return {'CANCELLED'}
        history_push(context,'SET_VALUE_'+str(self.target))
        prop = {
            'FRAME_WIDTH': 'frame_width_text',
            'INTERNAL_WIDTH': 'internal_width_text',
            'FRAME_DEPTH': 'frame_depth_text',
            'FRAME_OFFSET': 'frame_offset_text',
            'INTERIOR_OFFSET': 'interior_offset_text',
            'INTERIOR_DEPTH': 'interior_depth_text',
            'INTERIOR_CLEARANCE': 'interior_clearance_text',
            'GLASS_OFFSET': 'glass_offset_text',
            'GLASS_EDGE': 'glass_edge_offset_text',
            'GLASS_GAP': 'glass_gap_text',
        }[self.target]
        allow_negative = self.target in {'FRAME_OFFSET', 'INTERIOR_OFFSET', 'GLASS_OFFSET','GLASS_EDGE'}
        try:
            value = parse_meter_text(self.value_text, allow_negative=allow_negative)
            if self.target in {'FRAME_WIDTH', 'INTERNAL_WIDTH', 'FRAME_DEPTH', 'GLASS_GAP', 'INTERIOR_DEPTH'} and value <= 0:
                raise ValueError('Значение должно быть больше 0')
            if self.target=='INTERIOR_DEPTH' and value>MAX_INTERIOR_DEPTH_M:
                value=MAX_INTERIOR_DEPTH_M
                self.report({'INFO'}, 'Глубина Interior ограничена 1,5 м')
            before = [(target.name, target.dwos_window.glass_gap_text, target.dwos_window.glass_offset_text)
                      for target in target_windows(context, obj)] if self.target == 'FRAME_DEPTH' else []
            targets = apply_property_to_scope(context, obj, prop, format_meter(value))
            if before:
                old_values = {row[0]: row[1:] for row in before}
                adjusted = [target for target in targets
                            if old_values.get(target.name) != (target.dwos_window.glass_gap_text,
                                                               target.dwos_window.glass_offset_text)]
                if adjusted:
                    self.report({'INFO'}, f'Глубина изменена. Стекло подогнано для {len(adjusted)} окон')
        except Exception as exc:
            message=str(exc)
            if 'INTERIOR' in message.upper(): obj['DWOS_last_validation_error']=message
            log_exception('COMPOSER_SET_VALUE_FAILED', exc, target=self.target, value=self.value_text, window=obj.name)
            self.report({'ERROR'}, message); return {'CANCELLED'}
        if 'DWOS_last_validation_error' in obj: del obj['DWOS_last_validation_error']
        log_event('COMPOSER_SET_VALUE', window=obj.name, target=self.target, value=format_meter(value), scope=context.scene.dwos.batch_scope, targets=len(targets))
        return {'FINISHED'}


class DWOS_OT_composer_set_interior_mode(Operator):
    bl_idname = 'dwos.composer_set_interior_mode'
    bl_label = 'Interior Mode'
    bl_options = {'REGISTER'}
    mode: EnumProperty(items=[('OFF','OFF',''),('BOX','BOX','')])

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:
            return {'CANCELLED'}
        history_push(context,'INTERIOR_MODE')
        try:
            targets = apply_property_to_scope(context, obj, 'interior_mode', self.mode)
            log_event('INTERIOR_MODE', mode=self.mode, count=len(targets))
        except Exception as exc:
            log_exception('INTERIOR_MODE_FAILED', exc)
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_composer_set_optimization(Operator):
    bl_idname = 'dwos.composer_set_optimization'
    bl_label = 'Set Optimization'
    bl_options = {'REGISTER'}
    mode: EnumProperty(items=[('FULL', 'FULL', ''), ('OPTIMIZED', 'OPTIMIZED', '')], default='FULL')

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:
            return {'CANCELLED'}
        history_push(context,'OPTIMIZATION')
        try:
            targets = apply_optimization_to_scope(context, obj, self.mode)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        log_event('COMPOSER_OPTIMIZATION', window=obj.name, mode=self.mode, scope=context.scene.dwos.batch_scope, targets=len(targets))
        self.report({'INFO'},f'{self.mode}: применено к {len(targets)} окнам; CORNER/CURVED остаются FULL')
        return {'FINISHED'}


class DWOS_OT_composer_choose_material(Operator):
    bl_idname = 'dwos.composer_choose_material'
    bl_label = 'Выбрать материал рамы'
    bl_options = {'REGISTER'}
    material_name: StringProperty(name='Материал',default='')

    def invoke(self, context, event):
        obj = resolve_window(context.active_object)
        if not obj:
            return {'CANCELLED'}
        self.material_name = obj.dwos_window.frame_material.name if obj.dwos_window.frame_material else ''
        return context.window_manager.invoke_props_dialog(self, width=370)

    def draw(self, context):
        self.layout.prop_search(self,'material_name',bpy.data,'materials',text='Материал рамы')
        self.layout.label(text='Пусто — стандартный DWOS_Frame_Default')
        self.layout.label(text='Применяется к выбранной области (ONE / SEL / SIZE / ALL)')

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj:
            return {'CANCELLED'}
        history_push(context,'FRAME_MATERIAL')
        try:
            material = bpy.data.materials.get(self.material_name) if self.material_name else None
            if self.material_name and material is None:
                raise ValueError('Материал не найден в сцене')
            targets = apply_frame_material_to_scope(context, obj, material)
        except Exception as exc:
            log_exception('FRAME_MATERIAL_FAILED', exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Материал применён: {len(targets)}')
        return {'FINISHED'}


class DWOS_OT_apply_frame_material(Operator):
    bl_idname = 'dwos.apply_frame_material'
    bl_label = 'Применить материал рамы'
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj:
            return {'CANCELLED'}
        history_push(context,'FRAME_MATERIAL_APPLY')
        try:
            apply_frame_material_to_scope(context, obj, obj.dwos_window.frame_material)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_default_frame_material(Operator):
    bl_idname = 'dwos.default_frame_material'
    bl_label = 'Стандартный материал рамы'
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj:
            return {'CANCELLED'}
        history_push(context,'FRAME_MATERIAL_DEFAULT')
        try:
            apply_frame_material_to_scope(context, obj, None)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class DWOS_OT_composer_set_split_value(Operator):
    bl_idname = 'dwos.composer_set_split_value'
    bl_label = 'Set Split Value'
    bl_options = {'REGISTER'}
    index: IntProperty(default=-1)
    target: EnumProperty(items=[('POSITION', 'Position', ''), ('THICKNESS', 'Thickness', '')])
    value_text: StringProperty(default='0')

    def execute(self, context):
        obj = resolve_window(context.active_object)
        if not obj or obj.dwos_window.finalized:
            return {'CANCELLED'}
        history_push(context,'SPLIT_VALUE_'+str(self.target))
        settings = obj.dwos_window
        idx = self.index
        if idx < 0 or idx >= len(settings.splits):
            return {'CANCELLED'}
        split = settings.splits[idx]
        original=(split.position_text, split.thickness_text, bool(split.override_thickness))
        try:
            value = parse_meter_text(self.value_text)
            if self.target == 'POSITION':
                limit = parse_meter_text(settings.width_text if split.axis == 'V' else settings.height_text)
                if value < 0 or value > limit:
                    raise ValueError(f'Позиция Split должна быть от 0 до {format_meter(limit)}')
                split.position_text = format_meter(value)
            else:
                if value <= 0:
                    raise ValueError('Толщина Split должна быть больше 0')
                split.thickness_text = format_meter(value)
                split.override_thickness = True
            rebuild_window(obj)
        except Exception as exc:
            # RC38: a rejected thickness cannot leave stale width settings
            # behind an unchanged Mesh. Restore the previous source values.
            split.position_text,split.thickness_text,split.override_thickness=original
            log_exception('COMPOSER_SPLIT_VALUE_REJECTED',exc,window=obj.name,index=idx,target=self.target)
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        settings.active_split_index = idx
        log_event('COMPOSER_SPLIT_VALUE', window=obj.name, index=idx, target=self.target, value=self.value_text)
        return {'FINISHED'}


class DWOS_OT_viewport_composer(Operator):
    bl_idname = 'dwos.viewport_composer'
    bl_label = 'Открыть Composer во Viewport'
    bl_options = {'BLOCKING'}

    _handle = None
    _obj_name = ''
    _hits = None
    _split_lines = None
    _depth_handle = None
    _zones = None
    _selected_zone = None
    _selected_zone_ids = None
    _interior_handles = None
    _interior_drag = None
    _interior_ghost = None
    _scissor_controls = None
    _page = 'FRAME'
    _corner_post_selected = False
    _editing = None
    _buffer = ''
    _edit_fresh = False
    _drag_idx = -1
    _drag_value = None
    _depth_drag = False
    _depth_start_mouse = None
    _depth_start_value = None
    _depth_axis_screen = None
    _depth_mpp = None
    _ghost_depth = None

    @property
    def obj(self):
        return bpy.data.objects.get(self._obj_name)

    def invoke(self, context, event):
        global _ACTIVE
        obj = resolve_window(context.active_object)
        self._surface_mode = False
        if obj is None:
            from . import surface_composer_bridge as _surface_bridge
            obj = _surface_bridge.resolve_host(context)
            self._surface_mode = obj is not None
        if obj is None or (not self._surface_mode and obj.dwos_window.finalized):
            self.report({'ERROR'}, 'Выберите окно DWOS или фасад с выделенными гранями'); return {'CANCELLED'}
        if context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Composer запускается из 3D View'); return {'CANCELLED'}
        if _ACTIVE is not None:
            self.report({'WARNING'}, 'Composer уже открыт'); return {'CANCELLED'}
        self._obj_name = obj.name
        self._surface_page = 'BUILD'
        self._surface_zone_index = 0
        self._surface_input = None
        self._surface_buffer = ''
        self._surface_message = ''
        self._disabled=False
        self._hits = []
        self._split_lines = []
        self._zones = []
        self._selected_zone = None
        self._selected_zone_ids = []
        self._last_mullion_click = None
        self._zone_ids = []
        self._zone_menu_anchor = None
        self._tree_drag_id = ''
        self._tree_drag_value = None
        self._page = 'FRAME'
        self._compact = True
        self._history_message=''
        self._frame_more = False
        self._health_open = False
        self._constructor_mode = False  # viewport annotations only; never saved to mesh
        self._corner_post_selected=False
        self._corner_marker_popup=None
        self._corner_marker_hitboxes=[]
        self._mullion_focus_at = 0.0
        self._mullion_pulse_timer_pending = False
        self._split_popup_id = None
        self._split_popup_anchor = None
        self._active_handle = None
        self._hover_handle = None
        self._hover_zone = -1
        self._hover_split = -1
        self._panel_rect = None
        self._footer_rect = None
        self._panel_side = None
        self._hud_drag=None
        self._hud_fallback=False
        try:
            prefs=context.preferences.addons.get('issmaker_modeling_assistant')
            prefs=prefs.preferences if prefs else None
            self._hud_custom=bool(prefs.ui_dwos_hud_custom) if prefs else False
            self._hud_pinned=bool(prefs.ui_dwos_hud_pinned) if prefs else False
            self._hud_custom_position=(float(prefs.ui_dwos_hud_x),float(prefs.ui_dwos_hud_y)) if prefs else (.06,.55)
        except (AttributeError,ValueError,RuntimeError):
            self._hud_custom=False;self._hud_pinned=False;self._hud_custom_position=(.06,.55)
        self._layout_container = None
        self._layout_skips = []
        self._layout_issues = []
        self._hud_pixel_scale = 1.0
        self._hud_font_scale = 1.0
        self._floating_rect = None
        self._floating_scale = 1.0
        self._hover_window_name = ''
        self._show_orientation_debug = False
        self._hover_ui_key = ''
        self._interior_handles = {}
        self._interior_drag = None
        self._snap_feedback = ''
        self._interior_ghost = None
        self._scissor_controls = []
        self._editing = None
        self._buffer = ''
        self._edit_fresh = False
        self._field_focus_at=0.0
        self._focus_timer_pending=False
        self._drag_idx = -1
        self._drag_value = None
        self._group_drag_start = None
        self._box_armed = False
        self._box_start = None
        self._box_end = None
        self._area_pointer = context.area.as_pointer()
        self._depth_drag = False
        self._ghost_depth = None
        try:
            context.view_layer.objects.active = obj
            obj.select_set(True)
        except Exception:
            pass
        from ....core import lifecycle_lease as _lease
        self._pulse_token, _previous = _lease.claim('dwos.composer')
        if _previous:
            for orphan in _previous.get('handles', ()):
                try:
                    bpy.types.SpaceView3D.draw_handler_remove(orphan, 'WINDOW')
                except (RuntimeError, ValueError, ReferenceError) as exc:
                    log_event('DWOS_OLD_COMPOSER_DRAW_CLEANUP_FAILED', error=repr(exc))
        try:
            self._handle = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), 'WINDOW', 'POST_PIXEL')
            _lease.owned('dwos.composer', self._pulse_token)['handles'].append(self._handle)
        except Exception:
            _lease.retire('dwos.composer', self._pulse_token)
            raise
        _ACTIVE = self
        try:
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            # Modal ownership must be transactional just like GPU ownership.
            # This is a runtime failure, not invalid window geometry.
            try:
                self._finish(context)
            except Exception as cleanup_exc:
                log_event('COMPOSER_FAILED_MODAL_CLEANUP', error=repr(cleanup_exc))
            log_event('COMPOSER_FAILED_MODAL_REGISTRATION', error=repr(exc))
            self.report({'ERROR'}, 'DWOS: не удалось запустить редактор (Modal).')
            return {'CANCELLED'}
        context.area.tag_redraw()
        log_event('VIEWPORT_COMPOSER_OPEN', window=obj.name)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        global _ACTIVE
        from ....core import lifecycle_lease as _lease
        _lease.retire('dwos.composer', getattr(self, '_pulse_token', None))
        self._disabled = True
        # Keep the last active hit geometry for support ZIPs gathered after close.
        try:
            from .diagnostic_bundle import cache_composer_ui_snapshot
            cache_composer_ui_snapshot(self)
        except (ValueError,TypeError,AttributeError,ReferenceError,RuntimeError) as exc:
            log_event('COMPOSER_UI_SNAPSHOT_WARNING',window=self._obj_name,error=repr(exc))
        if self._handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            except Exception:
                pass
            self._handle = None
        if _ACTIVE is self:
            _ACTIVE = None
        self._footer_rect=None
        if context.area:
            context.area.tag_redraw()
        log_event('VIEWPORT_COMPOSER_CLOSE', window=self._obj_name)
        return {'FINISHED'}

    def _hit(self, key, x, y, w, h, payload=None):
        if _PANEL_PIXELS is not None:
            x,y,w,h=_PANEL_PIXELS.xywh(x,y,w,h)
        self._hits.append((key, x, y, x + w, y + h, payload))

    def _button(self, key, x, y, w, h, label, active=False, color=MINT, payload=None):
        theme=getattr(self,'_frame_theme',None) or _dwos_ui_snapshot()
        # The MINT token means UI accent here. Explicit warning/error colours
        # and geometry diagnostics are not remapped.
        if color==MINT:
            color=(*theme.accent,1.)
        if self._layout_container is not None:
            r=layout_fit_inside(self._layout_container,(x,y,w,h),pad=10,min_w=18,min_h=18)
            if (abs(r.x-x)>1 or abs(r.y-y)>1 or abs(r.w-w)>1 or abs(r.h-h)>1):
                self._layout_skips.append(key)
                return
            x,y,w,h=r.x,r.y,r.w,r.h
        hover=(key==getattr(self,'_hover_ui_key',''))
        glyph={
            'ADD_V':'vertical','ZONE_ADD_V':'vertical',
            'ADD_H':'horizontal','ZONE_ADD_H':'horizontal',
            'APPLY_SPLITS':'copy','ASSEMBLE_MESH':'merge','CLOSE':'close',
            'HEALTH_TOGGLE':'health','MULLION_EDIT':'edit',
            'SPLIT_DONE':'check',
        }.get(key)
        action=(key in {'APPLY_SPLITS','ASSEMBLE_MESH'})
        # Shared surface language (also used by UV Wall, Brick Hole and
        # Collision).  Keep all existing Composer geometry/hit registration.
        visual=_workspace_visual('button',
            'active' if active else ('hover' if hover else 'normal'),
            'primary' if action else 'neutral',theme)
        bg,border=visual.background,visual.border
        # Identical common rounded control as the other Viewport tools.
        # ComposerPixelMap converts logical coordinates to physical paint
        # pixels; _hit below retains the exact preexisting logical bounds.
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.
        rect=_mapped_xyxy(x,y,x+w,y+h)
        if not draw_component(rect,part='button',state=(
                'active' if active else 'hover' if hover else 'normal'),
                role='primary' if action else 'neutral',theme=theme,scale=scale):
            _soft_rect(x,y,x+w,y+h,bg,5.5)
            _soft_outline(x,y,x+w,y+h,border,5.5,1.2 if active or hover else 0.8)
        if active:
            # Restrained top accent instead of a bright rectangular outline.
            _line([(x+7,y+h-1.5),(x+min(w-7,35),y+h-1.5)],color,1.6)
        quiet_ink=_viewport_skin.ink(theme,kind='muted')
        label_ink=_viewport_skin.control_ink(theme,part='button',state=(
            'active' if active else 'hover' if hover else 'normal'),
            role='primary' if action else 'neutral')
        control_font=_viewport_skin.text_px('control')
        if glyph and w>=44:
            _glyph(glyph,x+7,y+(h-14)*0.5,14,color if active or action else label_ink)
            _text_fitted(x+28,y+(h-10)*.5,label,max(0,w-35),control_font,label_ink)
        elif key=='CLOSE':
            _glyph('close',x+5,y+(h-14)*.5,14,quiet_ink)
        else:
            _text_fitted(x+8,y+(h-10)*.5,label,max(0,w-15),control_font,label_ink)
        self._hit(key,x,y,w,h,payload)

    def _assembly_footer(self,context,obj):
        # Independent bottom-center action: never parent it to the Composer panel.
        self._footer_rect=None
        if self._editing or self._drag_idx>=0 or self._tree_drag_id or self._depth_drag or self._interior_drag:
            return
        try:
            frames=target_windows(context,obj)
            if not assembly_action_ready(context,frames):
                return
            protected=[r for r in (self._projected_frame_rect(context,w) for w in self._selected_windows(context)) if r]
            pos=bottom_center_action_rect((context.region.width,context.region.height),protected)
            if pos is None:
                return
        except (ValueError,TypeError,KeyError,AttributeError):
            return
        x,y,w,h=pos
        self._footer_rect=pos
        theme=_dwos_ui_snapshot()
        base=_viewport_skin.component('card',theme=theme)
        _soft_rect(x+3,y-3,x+w+3,y+h-3,(0,0,0,0.22),_viewport_skin.control_radius('panel'))
        _soft_rect(x,y,x+w,y+h,base.background,_viewport_skin.control_radius('panel'))
        _soft_outline(x,y,x+w,y+h,base.border,_viewport_skin.control_radius('panel'),1.1)
        # Narrow accent indicates a ready-to-validate action, not validation itself.
        _rect(x+2,y+2,x+5,y+h-2,(*theme.accent,.92))
        self._button('ASSEMBLE_MESH',x+7,y+5,w-14,h-10,'СОБРАТЬ В ОДИН MESH',False,MINT)

    def _scope_toolbar(self,context,obj,x,row_y,panel_w,hint_y):
        """Always-visible five-action toolbar; actions are the original batch operators.

        One shared scope lives in scene.dwos.batch_scope. No duplicate modal
        selection state is introduced, even when active DWOS window changes.
        """
        scope=str(context.scene.dwos.batch_scope)
        try:
            count=len(target_windows(context,obj))
        except (ValueError,KeyError,TypeError):
            count=0
        rects=scope_toolbar_rects(x,row_y,panel_w)
        # Subtle graphite rail separates the scope state from the editing page.
        _rect(x+10,row_y-4,x+panel_w-10,row_y+32,_viewport_skin.component('header',theme=_dwos_ui_snapshot()).background)
        for scope_id,label in SCOPE_BUTTONS:
            bx,by,bw,bh=rects[scope_id]
            self._button('SCOPE_'+scope_id,bx,by,bw,bh,label,scope==scope_id,MINT)
        bx,by,bw,bh=rects['COPY']
        self._button('APPLY_SPLITS',bx,by,bw,bh,'COPY',False,BLUE)
        hovered=getattr(self,'_hover_ui_key','')
        if hovered=='CONSTRUCTOR_TOGGLE':
            msg='СХЕМА · '+('выключить' if self._constructor_mode else 'показать')+' номинальные размеры во Viewport'
        elif hovered=='APPLY_SPLITS':
            msg=SCOPE_HINTS['COPY']+' · '+str(count)+' целей'
        elif hovered.startswith('STEP:'):
            msg='ЧИСЛОВОЙ ШАГ · '+('увеличить' if hovered.endswith(':plus') else 'уменьшить')+' на 1 мм'
        elif hovered.startswith('SCOPE_'):
            msg=SCOPE_HINTS.get(hovered[6:],SCOPE_HINTS['ACTIVE'])
        elif composer_hover_help(hovered):
            msg=composer_hover_help(hovered)
        elif getattr(self,'_history_message','').startswith('COPY:'):
            msg=self._history_message
        else:
            suffix='окно' if count==1 else ('окна' if count in (2,3,4) else 'окон')
            msg=f'ОБЛАСТЬ · {count} {suffix}  |  COPY — раскладка'
        _text_fitted(x+12,hint_y,msg,panel_w-24,10,_viewport_skin.ink(_dwos_ui_snapshot(),kind='muted'))

    def _field(self, key, label, value, x, y, w=105, payload=None):
        field_height = 28.0
        fx = x + 145
        if self._layout_container is not None:
            content=layout_fit_inside(self._layout_container,(x,y,max(20,145+w),field_height),pad=10,min_w=40,min_h=25)
            if abs(content.x-x)>1 or abs(content.y-y)>1 or abs(content.h-field_height)>1:
                self._layout_skips.append('FIELD:'+key)
                return
            x,y=content.x,content.y
            available=content.w
            field_w=min(float(w),max(64.0,available*0.40))
            fx=x+available-field_w
            w=field_w
        _text_fitted(x, y + 10, label, max(24.0,fx-x-10), _viewport_skin.text_px('control'), _viewport_skin.ink(_dwos_ui_snapshot(),kind='muted'))
        active = self._editing and self._editing[0] == key and self._editing[1] == payload
        hover=('FIELD:'+key)==getattr(self,'_hover_ui_key','')
        shared_theme=_dwos_ui_snapshot()
        field_state='focused' if active else ('hover' if hover else 'normal')
        field_visual=_viewport_skin.component('field',field_state,theme=shared_theme)
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component(_mapped_xyxy(fx,y,fx+w,y+field_height),
                              part='field',state=field_state,theme=shared_theme,
                              scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.):
            _soft_rect(fx, y, fx + w, y + field_height, field_visual.background,5.0)
            _soft_outline(fx, y, fx + w, y + field_height, field_visual.border,5.0,1.5 if active else 1.0)
        if active:
            elapsed=time.monotonic()-getattr(self,'_field_focus_at',0.0)
            if elapsed<0.34:
                strength=(1.0-elapsed/.34)**2
                _rect(fx+1,y+1,fx+3,y+field_height-1,(*shared_theme.accent,strength*0.7))
        caret = ('|' if int((time.monotonic()-getattr(self,'_field_focus_at',0.0))*2.0)%2==0 else '')
        index=max(0,min(len(self._buffer),getattr(self,'_caret_pos',len(self._buffer))))
        txt = (self._buffer[:index]+caret+self._buffer[index:]) if active else str(value)
        field_ink=_viewport_skin.control_ink(shared_theme,part='field',state=field_state)
        if active:
            _fitted,self._editing_render_font_px=_text_fitted(
                fx + 8, y + 9, txt, max(0,w-16), _viewport_skin.text_px('field'), field_ink, return_font=True)
        else:
            _text_fitted(fx + 8, y + 9, txt, max(0,w-16), _viewport_skin.text_px('field'), field_ink)
        self._hit('FIELD:' + key, fx, y, w, field_height, payload)
        # ISS-010: the field remains full width; +/- are *adjacent* controls.
        # No new hitbox may exist unless its glyph is actually painted.
        if self._layout_container is not None and key in {
                'FRAME_WIDTH','INTERNAL_WIDTH','FRAME_DEPTH','FRAME_OFFSET',
                'POST_WIDTH','INTERIOR_OFFSET','INTERIOR_DEPTH','INTERIOR_CLEARANCE',
                'GLASS_OFFSET','GLASS_GAP','GLASS_EDGE'}:
            from ....core.numeric_spinner import adjacent_step_buttons
            for direction,rect in adjacent_step_buttons(
                    (fx,y,w,field_height),self._layout_container).items():
                sx,sy,sw,sh=rect
                state='hover' if getattr(self,'_hover_ui_key','')==f'STEP:{key}:{direction}' else 'normal'
                visual=_viewport_skin.component('button',state,'neutral',shared_theme)
                if not draw_component(_mapped_xyxy(sx,sy,sx+sw,sy+sh),
                                      part='button',state=state,theme=shared_theme,
                                      scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.):
                    _soft_rect(sx,sy,sx+sw,sy+sh,visual.background,4.0)
                    _soft_outline(sx,sy,sx+sw,sy+sh,visual.border,4.0,1.0)
                step_ink=_viewport_skin.control_ink(shared_theme,part='button',state=state)
                center=(sx+sw*.5,sy+sh*.5)
                _line([(center[0]-4,center[1]),(center[0]+4,center[1])],step_ink,1.5)
                if direction=='plus':
                    _line([(center[0],center[1]-4),(center[0],center[1]+4)],step_ink,1.5)
                self._hit(f'STEP:{key}:{direction}',sx,sy,sw,sh,payload)

    def _health_card(self, context, obj, status, x, y, w, top, extra):
        """Optional Composer-only card, occupying measured EXTRA bottom space.

        The original controls remain anchored to TOP, and are shifted UP by the
        exact same extra height. All Health hitboxes are inside the panel.
        """
        if extra<=0:return
        cx,cy,cw,ch=x+11,y+9,w-22,69
        card_theme=_dwos_ui_snapshot()
        _soft_rect(cx,cy,cx+cw,cy+ch,_viewport_skin.component('card',theme=card_theme).background,
                   _viewport_skin.control_radius('panel'))
        accent=RED if status['code']=='ERROR' else (ORANGE if status['needs_attention'] else MINT)
        _rect(cx,cy,cx+3,cy+ch,(accent[0],accent[1],accent[2],0.82))
        _text_fitted(cx+11,cy+49,status['label'].upper(),cw-23,10,_viewport_skin.ink(card_theme))
        _text_fitted(cx+11,cy+32,status['detail'] or 'Нарушения не зарегистрированы',cw-23,10,
                     _viewport_skin.ink(card_theme,kind='muted'))
        links=related_names(obj)
        if status['code']=='PENDING':
            self._button('HEALTH_RETRY',cx+10,cy+5,116,22,'ПОВТОРИТЬ',False,MINT)
        if links:
            self._button('HEALTH_LOCATE',cx+135,cy+5,120,22,'СВЯЗАННЫЕ',False,BLUE)
        elif status['code'] not in {'PENDING'}:
            _text(cx+11,cy+12,'Данные DWOS · без тяжёлой проверки',10,_viewport_skin.ink(card_theme,kind='muted'))

    def _mullion_dimension(self, obj, sid, axis):
        """True parametric V/H coordinate; never estimate meters from the screen."""
        try:
            s=obj.dwos_window
            limit=parse_meter_text(s.width_text if axis=='V' else s.height_text)
            if isinstance(sid,str):
                r=self._split_row(obj,sid)
                if not r:return None
                center=float(r['position'])
                if self._tree_drag_id==sid and self._tree_drag_value is not None:
                    center=float(self._tree_drag_value)
                return mullion_position(axis=axis,center=center,span=limit)
            if isinstance(sid,int) and 0<=sid<len(s.splits):
                pos=(float(self._drag_value) if self._drag_idx==sid and self._drag_value is not None
                     else parse_meter_text(s.splits[sid].position_text))
                return mullion_position(axis=axis,center=pos-limit*.5,span=limit)
        except (ValueError,TypeError,KeyError,IndexError):
            return None
        return None

    def _mullion_focus_tag(self,context,obj):
        """Read-only micro-inspector positioned outside model and UI geometry.

        Uses real parametric position, not pixel-derived lengths; NEVER changes
        object selection, starts a modal operation or adds a hitbox of its own.
        """
        s=obj.dwos_window
        selected=self._current_ids(obj)
        lines=self._split_lines or []
        visible={sid if isinstance(sid,str) else s.splits[sid].split_id
                 for sid,axis,a,b in lines
                 if isinstance(sid,str) or (isinstance(sid,int) and 0<=sid<len(s.splits))}
        hovered=(self._hover_split if self._hover_split in {sid for sid,_,_,_ in lines} else None)
        if isinstance(hovered,int) and 0<=hovered<len(s.splits):
            hovered=s.splits[hovered].split_id
        focus=focus_mullion_id(selected,hovered,visible)
        if focus is None:return
        # CURVED/H can project to several segments; choose the longest visible
        # segment for a stable screen anchor without changing parametric data.
        candidates=[]
        for sid,axis,a,b in lines:
            ident=sid if isinstance(sid,str) else (s.splits[sid].split_id if 0<=sid<len(s.splits) else '')
            if ident==focus:
                candidates.append(((a[0]-b[0])**2+(a[1]-b[1])**2,sid,axis,a,b))
        if not candidates:return
        _length,sid,axis,a,b=max(candidates,key=lambda r:r[0])
        position=self._mullion_dimension(obj,sid,axis)
        if position is None:return
        mid=((a[0]+b[0])*.5,(a[1]+b[1])*.5)
        protected=[self._panel_rect,self._floating_rect,self._footer_rect,
                   self._projected_frame_rect(context,obj)]
        protected.extend(self._projected_frame_rect(context,other)
                         for other in self._selected_windows(context) if other is not obj)
        is_selected=(focus in selected)
        box=focus_card_rect(mid,(context.region.width,context.region.height),
                            protected,selected=is_selected)
        if box is None:return
        bx,by,bw,bh=box
        color=MINT if is_selected else BLUE
        _soft_rect(bx+2,by-2,bx+bw+2,by+bh-2,(0.0,0.0,0.0,0.19),8.0)
        _soft_rect(bx,by,bx+bw,by+bh,(0.017,0.033,0.039,0.965),7.0)
        _soft_outline(bx,by,bx+bw,by+bh,(0.12,0.25,0.25,0.95),7.0,1.0)
        _soft_rect(bx+3,by+7,bx+5,by+bh-7,color,1.0)
        _glyph('vertical' if axis=='V' else 'horizontal',bx+10,by+bh-23,13,color)
        from .numeric import format_meter
        label=f'{axis}  ·  {format_meter(position)}'
        _text_fitted(bx+30,by+bh-19,label,bw-39,11,TEXT)
        if is_selected:
            _text_fitted(bx+30,by+8,'DRAG · СДВИГ   2× · ВВОД',bw-39,8,MUTED)

    def _adaptive_topology(self, obj):
        try:
            payload=json.loads(obj.dwos_window.opening_payload_json or '{}')
            topo=payload.get('adaptive_topology')
            if isinstance(topo,dict) and topo.get('coordinate_system')=='SVD_RIBBON':
                return topo
        except Exception:
            pass
        return None

    def _local_point(self,obj,s,v,d):
        topo=self._adaptive_topology(obj)
        return Vector(map_svd_local(topo,s,v,d)) if topo else Vector((s,v,d))

    def _adaptive_s_samples(self,topo,s0,s1):
        values=[float(s0),float(s1)]
        if topo:
            half=float(topo.get('width',0.0))*.5
            values.extend(float(st.get('s',0.0))-half for st in topo.get('stations',[])
                          if min(s0,s1)+1e-9 < float(st.get('s',0.0))-half < max(s0,s1)-1e-9)
        values=sorted(set(round(v,10) for v in values))
        return values if s0<=s1 else list(reversed(values))

    def _project_sv_polyline(self,context,obj,s_values,v,d):
        out=[]
        for ss in s_values:
            world=obj.matrix_world @ self._local_point(obj,ss,v,d)
            p=view3d_utils.location_3d_to_region_2d(context.region,context.region_data,world)
            if p is None:return []
            out.append((float(p.x),float(p.y)))
        return out

    def _safe_aux_window(self, context, key, proposed):
        """Pass 07 shared strict placement for all DWOS contextual UI.

        Return *physical* xywh.  A None result means no drawing and no hits.
        This method never mutates window geometry or modal ownership.
        """
        if proposed is None:
            return None
        x,y,w,h=proposed
        peers=(r for r in (self._panel_rect,self._footer_rect,self._floating_rect)
               if r is not None)
        blockers=[(r[0],r[1],r[0]+r[2],r[1]+r[3]) for r in peers]
        vw,vh=context.region.width,context.region.height
        blockers.append((max(0,vw-96),max(0,vh-96),vw,vh))
        result=place_unobstructed((vw,vh),key,(w,h),(x,y),
                                  blockers=blockers,margin=10,gap=6,scope=scope_from_context(context))
        if result is None:
            return None
        return result[0],result[1],result[2]-result[0],result[3]-result[1]

    def _draw_hud_micro(self,context):
        """Minimal recovery chip when even compact Composer cannot fit."""
        vw,vh=context.region.width,context.region.height
        if vw<132 or vh<42:return
        x,y=12,max(9,vh-45);w=min(230,vw-24);h=34
        self._panel_rect=(x,y,w,h)
        theme=self._frame_theme
        _soft_rect(x,y,x+w,y+h,_viewport_skin.component('card',theme=theme).background,7.)
        _soft_outline(x,y,x+w,y+h,(*theme.accent,.62),7.,1.)
        _text_fitted(x+8,y+12,'DWOS · HUD',max(0,w-91),11,_viewport_skin.ink(theme))
        self._button('HUD_RESET',x+w-76,y+5,42,24,'RST')
        self._button('CLOSE',x+w-31,y+5,27,24,'X')

    def _save_hud_position(self,context):
        """Persist only preference coordinates, never mutate DWOS mesh data."""
        try:
            addon=context.preferences.addons.get('issmaker_modeling_assistant')
            prefs=addon.preferences if addon else None
            if prefs:
                prefs.ui_dwos_hud_custom=bool(self._hud_custom)
                prefs.ui_dwos_hud_pinned=bool(self._hud_pinned)
                prefs.ui_dwos_hud_x=max(0.,min(1.,self._hud_custom_position[0]))
                prefs.ui_dwos_hud_y=max(0.,min(1.,self._hud_custom_position[1]))
        except (AttributeError,ReferenceError,RuntimeError):pass

    def _hud_position(self,context,width,height):
        """Floating position is normalized to the Viewport, independent of Mesh."""
        from ....core.hud_floating_contract import clamp_to_viewport
        vw,vh=context.region.width,context.region.height
        return clamp_to_viewport((vw,vh),(width,height),
            (vw*self._hud_custom_position[0],vh*self._hud_custom_position[1]),12)

    def _panel_origin(self, context, obj, width, height):
        if getattr(self,'_hud_custom',False):
            return self._hud_position(context,width,height)
        frame=self._projected_frame_rect(context,obj)
        vw,vh=context.region.width,context.region.height
        # Reserve Blender's navigation gizmo area.  Region coordinates already
        # exclude the N-panel/tool shelf themselves.
        avoid=((max(0,vw-96),max(0,vh-96),96,96),)
        if self._footer_rect is not None:
            avoid=avoid+(self._footer_rect,)
        selected=self._selected_windows(context)
        if len(selected)>1:
            # Include EVERY selected DWOS window, not just the active one.
            protected=tuple(r for win in selected
                            if (r:=self._projected_frame_rect(context,win)) is not None)
            placement=screen_safe_multi_panel(frame,(width,height),(vw,vh),protected,
                avoid=avoid,previous_side=getattr(self,'_panel_side',None),margin=12,gap=18)
            if placement is None:
                return None  # do not cover the geometry; sidebar remains available
            px,py,_w,_h,side=placement
        else:
            px,py,_w,_h,side=screen_safe_panel(frame,(width,height),(vw,vh),
                avoid=avoid,previous_side=getattr(self,'_panel_side',None),margin=12,gap=18)
        # Pass 07: all Composer panels undergo the same strict viewport registry
        # check, including geometry silhouettes and Blender navigation chrome.
        # The legacy side selection is a preference, never permission to overlap.
        peers=list(avoid)
        peers.extend(r for win in selected
                     if (r:=self._projected_frame_rect(context,win)) is not None)
        if frame is not None and frame not in peers:
            peers.append(frame)
        bounds=place_unobstructed((vw,vh),'DWOS_COMPOSER',(width,height),
                                  (px,py),blockers=(
                                  (r[0],r[1],r[0]+r[2],r[1]+r[3]) for r in peers),
                                  margin=12,gap=8,scope=scope_from_context(context))
        if bounds is None:
            return None
        self._panel_side=side
        return bounds[0],bounds[1]

    def _window_rect(self, context, obj, margin=0.0):
        rect=self._projected_frame_rect(context,obj)
        if rect is None:return None
        x,y,w,h=rect;m=float(margin)
        return (x-m,y-m,w+2*m,h+2*m)

    def _pick_window(self, context, x, y):
        """Screen-space whole-window hit target with view-depth priority."""
        candidates=[]
        for win in context.scene.objects:
            st=getattr(win,'dwos_window',None)
            if not st or not st.is_window or st.finalized or win.get('DWOS_archived_source'):
                continue
            rect=self._window_rect(context,win,8.0)
            if rect is None:continue
            rx,ry,rw,rh=rect
            if not (rx<=x<=rx+rw and ry<=y<=ry+rh):continue
            try:
                center=win.matrix_world @ Vector((0,0,0))
                view=context.region_data.view_matrix @ center
                depth=abs(float(view.z))
            except Exception:
                depth=1e30
            candidates.append((depth,win))
        return min(candidates,key=lambda q:q[0])[1] if candidates else None

    def _switch_window(self, context, target, preserve_selection=False):
        if target is None or target.name==self._obj_name:
            if target is not None and preserve_selection:
                try:target.select_set(True);context.view_layer.objects.active=target
                except Exception:pass
            return False
        old=self.obj
        self._obj_name=target.name
        self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None;self._split_popup_id=None
        self._split_popup_anchor=None;self._last_mullion_click=None
        self._health_open=False
        self._active_handle=None;self._hover_handle=None;self._hover_zone=-1;self._hover_split=-1
        self._tree_drag_id='';self._tree_drag_value=None;self._drag_idx=-1;self._drag_value=None
        self._group_drag_start=None;self._editing=None;self._buffer='';self._compact=True
        try:
            if not preserve_selection:
                for o in list(context.selected_objects):o.select_set(False)
            target.select_set(True);context.view_layer.objects.active=target
        except Exception:
            pass
        log_event('VIEWPORT_COMPOSER_SWITCH_WINDOW',old=(old.name if old else ''),window=target.name)
        context.area.tag_redraw()
        return True

    def _selected_windows(self,context):
        out=[]
        for o in context.selected_objects:
            st=getattr(o,'dwos_window',None)
            if st and st.is_window and not st.finalized and not o.get('DWOS_archived_source'):
                out.append(o)
        return out

    def _shift_toggle_window(self,context,target):
        """Blender-like multi-window selection without stealing Mullion Shift."""
        current=self.obj
        try:selected=bool(target.select_get())
        except Exception:selected=False
        if not selected:
            target.select_set(True)
            self._switch_window(context,target,preserve_selection=True)
            log_event('VIEWPORT_WINDOW_MULTISELECT',window=target.name,count=len(self._selected_windows(context)),active=target.name,action='ADD')
            return True
        # Toggle an already selected window off. Composer must still have an active
        # editable window, so if this is active choose another selected DWOS window.
        remaining=[o for o in self._selected_windows(context) if o.name!=target.name]
        if not remaining:
            return True  # keep the sole active window selected
        target.select_set(False)
        if current and current.name==target.name:
            replacement=remaining[-1]
            self._switch_window(context,replacement,preserve_selection=True)
        log_event('VIEWPORT_WINDOW_MULTISELECT',window=target.name,count=len(remaining),active=self._obj_name,action='REMOVE')
        return True

    def _corner_post_enabled(self,obj):
        topo=self._adaptive_topology(obj)
        return bool(topo and topo.get('classification')=='CORNER' and
                    evaluate_corner_post(topo,getattr(obj.dwos_window,'corner_post_mode','AUTO'))['effective_mode']!='OFF')

    def _corner_posts_for_obj(self,obj):
        topo=self._adaptive_topology(obj)
        if not topo:return []
        try:
            t=parse_meter_text(obj.dwos_window.internal_width_text)
            fd=parse_meter_text(obj.dwos_window.frame_depth_text)
        except Exception:
            t=0.06;fd=0.10
        return calc_corner_posts(topo,getattr(obj.dwos_window,'corner_post_mode','AUTO'),t,fd)

    def _branch_layout_for_obj(self,obj):
        topo=self._adaptive_topology(obj);s=obj.dwos_window
        if not topo or topo.get('classification')!='CORNER' or s.zone_tree_json:
            return None,None,None
        try:
            h=parse_meter_text(s.height_text);fw=parse_meter_text(s.frame_width_text)
            posts=self._corner_posts_for_obj(obj)
            state=decode_branch_state(getattr(s,'branch_state_json',''),topo)
            leaves,bands=branch_layout_state(state,topo,h,fw,posts)
            return state,leaves,bands
        except Exception as exc:
            log_exception('BRANCH_LAYOUT_REJECTED',exc,window=obj.name)
            return None,None,None

    def _split_row(self,obj,sid):
        _state,_leaves,bands=self._branch_layout_for_obj(obj)
        if bands is not None:
            r=next((r for r in bands if r[0]==sid),None)
            if r:return {'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4],'band':r[5],'branch_id':r[6]}
        s=obj.dwos_window
        if s.zone_tree_json:
            try:
                _,rows=zone_layout(decode_zones(s.zone_tree_json),parse_meter_text(s.width_text),parse_meter_text(s.height_text),parse_meter_text(s.frame_width_text))
                r=next((r for r in rows if r[0]==sid),None)
                if r:return {'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4],'band':r[5]}
            except Exception:pass
        return None

    def _assert_split_consistency(self,obj,selected_id):
        """Only on explicit field focus; no expensive audit in GPU draw loop."""
        settings=obj.dwos_window
        topo=self._adaptive_topology(obj)
        branch_mode=bool(topo and topo.get('classification')=='CORNER' and not settings.zone_tree_json)
        if branch_mode:
            _state,_leaves,bands=self._branch_layout_for_obj(obj)
            if bands is None:
                raise ValueError('CORNER Branch недоступен, редактирование остановлено')
            rows=[dict(id=r[0],axis=r[1],position=r[2],thickness=r[3],branch_id=r[6]) for r in bands]
        elif settings.zone_tree_json:
            w=parse_meter_text(settings.width_text);h=parse_meter_text(settings.height_text)
            fw=parse_meter_text(settings.frame_width_text)
            _leaves,bands=zone_layout(decode_zones(settings.zone_tree_json),w,h,fw)
            rows=[dict(id=r[0],axis=r[1],position=r[2],thickness=r[3]) for r in bands]
        else:
            raise ValueError('Дерево локальных импостов недоступно')
        problems=verify_unique_rows(rows,branch_mode=branch_mode)
        if problems:
            log_event('DWOS_SPLIT_CONSISTENCY_ALERT',window=obj.name,
                      source='CORNER_BRANCH' if branch_mode else 'ZONE',issues=str(problems[:6]))
            raise ValueError('Несогласованные данные импостов: требуется диагностика')
        if selected_id not in {r['id'] for r in rows}:
            log_event('DWOS_STALE_SPLIT_SELECTION',window=obj.name,split=str(selected_id))
            raise ValueError('Выбранный импост больше не существует')

    def _constrain_split_center(self,obj,axis,proposed,thickness,rect,previous=None):
        if axis!='V':
            lo,hi=(rect[0],rect[1]) if axis=='V' else (rect[2],rect[3])
            return clamp_split_center(proposed,axis,thickness,lo,hi,(),0.003,previous)
        posts=self._corner_posts_for_obj(obj)
        lo,hi=rect[0],rect[1]
        return clamp_split_center(proposed,axis,thickness,lo,hi,posts,0.003,previous)

    def _project_corner_lines(self,context,obj):
        topo=self._adaptive_topology(obj)
        if not topo or topo.get('classification')!='CORNER':return []
        try:
            h=parse_meter_text(obj.dwos_window.height_text)
            d=parse_meter_text(obj.dwos_window.frame_offset_text,allow_negative=True)
            half=float(topo.get('width',0.0))*.5
            out=[]
            for j in topo.get('corner_junctions',[]):
                ss=float(j['s'])-half
                pts=[self._local_point(obj,ss,-h*.5,d),self._local_point(obj,ss,h*.5,d)]
                pp=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@q) for q in pts]
                if all(q is not None for q in pp):out.append(((float(pp[0].x),float(pp[0].y)),(float(pp[1].x),float(pp[1].y)),j))
            return out
        except Exception:return []

    def _corner_post_hit(self,context,obj,x,y):
        if not self._corner_post_enabled(obj):return False
        for a,b,_ in self._project_corner_lines(context,obj):
            dx=b[0]-a[0];dy=b[1]-a[1];den=dx*dx+dy*dy
            if den<1e-7:continue
            t=max(0.0,min(1.0,((x-a[0])*dx+(y-a[1])*dy)/den))
            if (x-a[0]-t*dx)**2+(y-a[1]-t*dy)**2<=10.0**2:
                return True
        return False

    def _project_splits(self, context, obj):
        out=[]
        s=obj.dwos_window
        topo=self._adaptive_topology(obj)
        try:
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            d=parse_meter_text(s.frame_offset_text,allow_negative=True)
            selected_ids=set(parse_ids(s.mullion_selection_json))
            _branch_state,_branch_leaves,_branch_bands=self._branch_layout_for_obj(obj)
            if _branch_bands is not None:
                bands=[r[:6] for r in _branch_bands]
                for split_id,axis,center,thickness,rect,band in bands:
                    if self._tree_drag_id and self._tree_drag_value is not None:
                        center=preview_position(split_id,center,self._tree_drag_id,
                           self._tree_drag_value,self._group_drag_start,selected_ids)
                    x0,x1,y0,y1=rect
                    if axis=='V':
                        coords=[self._local_point(obj,center,y0,d),self._local_point(obj,center,y1,d)]
                        pts=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@pt) for pt in coords]
                        if all(p is not None for p in pts):
                            out.append((split_id,axis,(float(pts[0].x),float(pts[0].y)),(float(pts[1].x),float(pts[1].y))))
                    else:
                        samples=self._adaptive_s_samples(topo,x0,x1)
                        poly=self._project_sv_polyline(context,obj,samples,center,d)
                        for a,b in zip(poly,poly[1:]):out.append((split_id,axis,a,b))
            elif s.zone_tree_json:
                _,bands=zone_layout(decode_zones(s.zone_tree_json),w,h,
                                    parse_meter_text(s.frame_width_text))
                for split_id,axis,center,thickness,rect,band in bands:
                    if self._tree_drag_id and self._tree_drag_value is not None:
                        center=preview_position(split_id,center,self._tree_drag_id,
                           self._tree_drag_value,self._group_drag_start,selected_ids)
                    x0,x1,y0,y1=rect
                    if axis=='V':
                        coords=[self._local_point(obj,center,y0,d),self._local_point(obj,center,y1,d)]
                        pts=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@pt) for pt in coords]
                        if all(p is not None for p in pts):
                            out.append((split_id,axis,(float(pts[0].x),float(pts[0].y)),(float(pts[1].x),float(pts[1].y))))
                    else:
                        samples=self._adaptive_s_samples(topo,x0,x1)
                        poly=self._project_sv_polyline(context,obj,samples,center,d)
                        for a,b in zip(poly,poly[1:]):out.append((split_id,axis,a,b))
            else:
                for i,split in enumerate(s.splits):
                    pos=parse_meter_text(split.position_text)
                    if self._drag_idx>=0 and self._drag_value is not None:
                        pos=preview_position(split.split_id,pos,
                             s.splits[self._drag_idx].split_id,self._drag_value,
                             self._group_drag_start,selected_ids)
                    center=(pos-w*.5) if split.axis=='V' else (pos-h*.5)
                    if split.axis=='V':
                        coords=[self._local_point(obj,center,-h*.5,d),self._local_point(obj,center,h*.5,d)]
                        pts=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@pt) for pt in coords]
                        if all(p is not None for p in pts):out.append((i,split.axis,(float(pts[0].x),float(pts[0].y)),(float(pts[1].x),float(pts[1].y))))
                    else:
                        poly=self._project_sv_polyline(context,obj,self._adaptive_s_samples(topo,-w*.5,w*.5),center,d)
                        for a,b in zip(poly,poly[1:]):out.append((i,split.axis,a,b))
        except (ValueError,TypeError,ZeroDivisionError) as exc:
            log_exception('SPLIT_PROJECTION_REJECTED',exc,window=obj.name)
        return out

    def _project_zones(self,context,obj):
        s=obj.dwos_window;result=[];self._zone_ids=[]
        topo=self._adaptive_topology(obj)
        try:
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            d=parse_meter_text(s.frame_offset_text,allow_negative=True)+0.001
            _branch_state,_branch_leaves,_branch_bands=self._branch_layout_for_obj(obj)
            if _branch_leaves is not None:
                entries2=[(zid,rect) for zid,rect,_bid in _branch_leaves]
            elif s.zone_tree_json:
                entries2=zone_layout(decode_zones(s.zone_tree_json),w,h,fw)[0]
            else:
                splits=[]
                for sp in s.splits:
                    pos=parse_meter_text(sp.position_text);centered=pos-(w if sp.axis=='V' else h)*.5
                    thick=parse_meter_text(sp.thickness_text if sp.override_thickness else s.internal_width_text)
                    splits.append(SplitSpec(sp.axis,centered,thick,sp.split_id))
                entries2=[('',rect) for rect in clear_rectangles(w,h,fw,splits)]
            for zid,rect in entries2:
                x0,x1,y0,y1=rect;samples=self._adaptive_s_samples(topo,x0,x1)
                coords=[self._local_point(obj,ss,y0,d) for ss in samples]
                coords += [self._local_point(obj,ss,y1,d) for ss in reversed(samples)]
                points=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@pt) for pt in coords]
                if len(points)>=4 and all(p is not None for p in points):
                    self._zone_ids.append(zid);result.append((rect,[(float(q.x),float(q.y)) for q in points]))
        except (ValueError,ZeroDivisionError,TypeError) as exc:
            log_exception('ZONE_PROJECTION_REJECTED',exc,window=obj.name)
        return result

    def _zone_hit(self,x,y):
        for i,(rect,poly) in enumerate(self._zones or []):
            inside=False;n=len(poly)
            for j in range(n):
                ax,ay=poly[j];bx,by=poly[(j+1)%n]
                if (ay>y)!=(by>y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:inside=not inside
            if inside:return i
        return -1

    def _scissor_candidates(self,context,obj):
        """Exactly the full-window V candidates supported by the real Interior solver."""
        state=obj.dwos_window
        if state.interior_mode=='OFF' or str(obj.get('DWOS_adaptive_classification',''))=='CORNER':
            return [],set()
        from .interior_sections_core import full_height_bands
        from .interior_authored_sections_core import effective_cut_ids
        from .window_builder import _splits_from_settings
        w=parse_meter_text(state.width_text);h=parse_meter_text(state.height_text)
        fw=parse_meter_text(state.frame_width_text)
        _b,_l,rows=self._branch_layout_for_obj(obj)
        if rows is None:
            rows=(zone_layout(decode_zones(state.zone_tree_json),w,h,fw)[1]
                  if state.zone_tree_json else [])
        bands=full_height_bands(_splits_from_settings(state,w,h),rows,w,h,fw)
        active=set(effective_cut_ids(bands,(-w/2+fw,w/2-fw,-h/2+fw,h/2-fw),
            str(state.interior_partition_mode),int(state.interior_groups_count),state.interior_dividers_json))
        d=parse_meter_text(state.frame_offset_text,allow_negative=True)
        candidates=[]
        for sid,c,t in bands:
            screen=view3d_utils.location_3d_to_region_2d(
                context.region,context.region_data,obj.matrix_world@self._local_point(obj,c,h*.13,d))
            if screen is not None:candidates.append((str(sid),float(screen.x),float(screen.y)))
        return candidates,active

    def _draw_interior_scissors(self,context,obj):
        """Viewport-scoped cut toggles; no second geometry/split editing engine."""
        self._scissor_controls=[]
        try:candidates,active=self._scissor_candidates(context,obj)
        except (ValueError,TypeError,KeyError) as exc:
            log_exception('VIEWPORT_INTERIOR_SCISSORS_REJECTED',exc,window=obj.name)
            return
        if not candidates:return
        focus=self._hover_split
        if isinstance(focus,int) and 0<=focus<len(obj.dwos_window.splits):
            focus=str(obj.dwos_window.splits[focus].split_id)
        focus=str(focus) if focus not in (None,-1) else ''
        selected=set(self._current_ids(obj))
        important=selected|active
        avoid=[r for r in (self._panel_rect,self._floating_rect,self._footer_rect) if r]
        avoid.extend((x0,y0,x1-x0,y1-y0) for key,x0,y0,x1,y1,_payload in self._hits
                     if key!='INTERIOR_SCISSOR')
        positions=place_scissor_icons(candidates,(context.region.width,context.region.height),
                    avoid,focus,important,page=('INTERIOR' if self._compact else self._page))
        self._scissor_controls=positions
        for sid,(x,y,w,h) in positions:
            on=sid in active
            hovered=getattr(self,'_hover_scissor_id','')==sid
            theme=_dwos_ui_snapshot()
            state='active' if on else ('hover' if hovered else 'normal')
            visual=_viewport_skin.component('button',state,theme=theme)
            col=(*theme.accent,1.) if on else (_viewport_skin.ink(theme) if hovered else _viewport_skin.ink(theme,kind='muted'))
            from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
            if not draw_component(_mapped_xyxy(x,y,x+w,y+h),part='button',state=state,theme=theme,
                                  scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.):
                _soft_rect(x,y,x+w,y+h,visual.background,_viewport_skin.control_radius('button'))
                _soft_outline(x,y,x+w,y+h,visual.border,_viewport_skin.control_radius('button'),1.6 if on or hovered else 1.0)
            # Scissors formed from screen-space line strokes; no font dependency.
            _line([(x+9,y+9),(x+22,y+21)],col,1.9)
            _line([(x+9,y+21),(x+22,y+9)],col,1.9)
            _circle(x+9,y+8,3.0,col)
            _circle(x+9,y+22,3.0,col)
            if on:_circle(x+24,y+24,3.0,MINT)
            if hovered:
                from .viewport_scissor_core import overlap
                # RC32: show the actual logical Interior group next to this
                # post. Unselected V still excludes metal physically, but it
                # does not create another user Interior group.
                ordered=[row[0] for row in candidates]
                earlier=ordered[:ordered.index(sid)] if sid in ordered else ()
                left_group=1+sum(other in active for other in earlier)
                if on:
                    msg=f'I{left_group} / I{left_group+1} · убрать границу'
                else:
                    msg=f'I{left_group} · единый Interior за V'
                label_w=189
                px=x+w+7 if x+w+label_w+12<context.region.width else x-label_w-7
                py=y+9
                tip=(px,py-3,label_w,20)
                other_buttons=[r for other,r in positions if other!=sid]
                if (px>=6 and px+label_w<=context.region.width-6 and
                        not any(overlap(tip,r,3) for r in (*avoid,*other_buttons))):
                    tip_style=_viewport_skin.component('card',theme=theme)
                    _soft_rect(px,py-3,px+label_w,py+17,tip_style.background,_viewport_skin.control_radius('field'))
                    _text_fitted(px+7,py+2,msg,label_w-13,_viewport_skin.text_px('caption'),_viewport_skin.ink(theme))
            self._hit('INTERIOR_SCISSOR',x,y,w,h,sid)

    def _project_interior_handles(self,context,obj):
        s=obj.dwos_window
        if s.interior_mode=='OFF':
            return {}
        try:
            w=parse_meter_text(s.width_text)
            fw=parse_meter_text(s.frame_width_text)
            fd=parse_meter_text(s.frame_depth_text)
            front=parse_meter_text(s.frame_offset_text,allow_negative=True)
            offset=parse_meter_text(s.interior_offset_text,allow_negative=True)
            depth=(self._interior_ghost if self._interior_drag and self._interior_drag[0]=='INTERIOR_DEPTH'
                   and self._interior_ghost is not None else parse_meter_text(s.interior_depth_text))
            sign=1 if s.flip_interior else -1
            if self._interior_drag and self._interior_drag[0]=='INTERIOR_OFFSET' and self._interior_ghost is not None:
                offset=self._interior_ghost
            start=front+sign*(fd*0.25+offset)
            end=start+sign*depth
            clearance=(self._interior_ghost if self._interior_drag and self._interior_drag[0]=='INTERIOR_CLEARANCE'
                       and self._interior_ghost is not None else parse_meter_text(s.interior_clearance_text))
            xx=-w*0.5+fw*0.5
            points=[obj.matrix_world@self._local_point(obj,xx,0,z) for z in (start,end,start+sign*0.1)]
            points.extend([obj.matrix_world@self._local_point(obj,xx+clearance,0,start),
                           obj.matrix_world@self._local_point(obj,xx+clearance+0.1,0,start)])
            proj=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,p) for p in points]
            if any(q is None for q in proj): return {}
            a,b,c,d,e=[(float(q.x),float(q.y)) for q in proj]
            handles={'INTERIOR_OFFSET':a,'INTERIOR_DEPTH':b, 'INTERIOR_CLEARANCE':d,
                     'reference':c,'clearance_reference':e,'start':a,'end':b}
            handles.update(declutter_handles({key:handles[key] for key in
                ('INTERIOR_OFFSET','INTERIOR_DEPTH','INTERIOR_CLEARANCE') if key in handles},minimum=21))
            return handles
        except (ValueError, ZeroDivisionError):
            return {}

    def _project_glass_handles(self, context, obj):
        """Spatial glass edge/depth controls. Ghost-only preview during drag."""
        v=obj.dwos_window
        try:
            w=parse_meter_text(v.width_text)
            h=parse_meter_text(v.height_text)
            fw=parse_meter_text(v.frame_width_text)
            fd=parse_meter_text(v.frame_depth_text)
            front=parse_meter_text(v.frame_offset_text,allow_negative=True)
            sign=1 if v.flip_interior else -1
            edge=(self._interior_ghost if self._interior_drag and
                  self._interior_drag[0]=='GLASS_EDGE' and self._interior_ghost is not None
                  else parse_meter_text(v.glass_edge_offset_text,allow_negative=True))
            offset=(self._interior_ghost if self._interior_drag and
                    self._interior_drag[0]=='GLASS_OFFSET' and self._interior_ghost is not None
                    else parse_meter_text(v.glass_offset_text,allow_negative=True))
            z=front+sign*(fd*.5+offset)
            from .assembly_core import glass_overlap
            x=-w*.5+fw*.5+edge-glass_overlap(fw)
            y=-h*.5+fw*.5+edge-glass_overlap(fw)
            raw={
                'GLASS_EDGE':((x,0,z),(x+.1,0,z)),
                'GLASS_OFFSET':((0,y,z),(0,y,z+sign*.1)),
            }
            out={}
            for name,(pt,reference) in raw.items():
                a,b=[view3d_utils.location_3d_to_region_2d(context.region,context.region_data,
                     obj.matrix_world@self._local_point(obj,*co)) for co in (pt,reference)]
                if a is not None and b is not None:
                    out[name]=(float(a.x),float(a.y))
                    out[name+'_reference']=(float(b.x),float(b.y))
            return out
        except (ValueError,TypeError,ZeroDivisionError):
            return {}

    def _project_depth_handle(self, context, obj):
        s = obj.dwos_window
        try:
            width = parse_meter_text(s.width_text)
            depth = self._ghost_depth if self._ghost_depth is not None else parse_meter_text(s.frame_depth_text)
            front = parse_meter_text(s.frame_offset_text, allow_negative=True)
            interior_sign = 1 if s.flip_interior else -1
        except Exception:
            return None
        p0 = obj.matrix_world @ self._local_point(obj,width*.5,0.0,front)
        p1 = obj.matrix_world @ self._local_point(obj,width*.5,0.0,front+interior_sign*depth)
        a = view3d_utils.location_3d_to_region_2d(context.region, context.region_data, p0)
        b = view3d_utils.location_3d_to_region_2d(context.region, context.region_data, p1)
        if a is None or b is None:
            return None
        return ((float(a.x), float(a.y)), (float(b.x), float(b.y)), depth)

    def _draw(self):
        context = bpy.context
        if getattr(self, '_surface_mode', False):
            from . import surface_composer_bridge as _surface_bridge
            return _surface_bridge.draw(self, context)
        obj = self.obj
        if (obj is None or context.area is None or context.area.type != 'VIEW_3D' or
                context.area.as_pointer()!=getattr(self,'_area_pointer',None) or
                getattr(self,'_disabled',False)):
            return
        self._frame_theme = _dwos_ui_snapshot()
        self._hits = []
        self._layout_container = None  # bottom CTA must never be clamped to prior HUD panel
        self._layout_skips = []
        self._layout_issues = []
        self._split_lines = self._project_splits(context,obj)
        self._zones = self._project_zones(context,obj)
        self._depth_handle = self._project_depth_handle(context,obj)
        self._interior_handles = (self._project_interior_handles(context,obj) if self._page=='INTERIOR'
                                  else self._project_glass_handles(context,obj) if self._page=='GLASS' else {})
        s=obj.dwos_window
        gpu.state.blend_set('ALPHA')
        self._floating_rect=None
        self._floating_scale=1.0
        self._hud_fallback=False

        # Window-level selection feedback: the whole projected window is a hit
        # target, so feedback must not depend on aiming at a thin frame edge.
        active_rect=self._projected_frame_rect(context,obj)
        if active_rect:
            _corner_brackets(active_rect,(*self._frame_theme.accent,.72),16.0,1.35)
        hover_obj=bpy.data.objects.get(getattr(self,'_hover_window_name',''))
        if hover_obj is not None and hover_obj is not obj:
            hr=self._projected_frame_rect(context,hover_obj)
            if hr:_corner_brackets(hr,(*self._frame_theme.accent,.60),13.0,1.5)

        # Secondary brackets show the multi-window selection set without turning
        # the viewport into a large overlay. Active Window remains the strongest.
        for win in self._selected_windows(context):
            if win is obj:continue
            rr=self._projected_frame_rect(context,win)
            if rr:_corner_brackets(rr,(0.32,0.53,0.50,0.50),11.0,1.0)
        # Diagnostic focus is purely visual. No solver invocation from redraw.
        status=health_of(obj, context.scene)
        if status['needs_attention']:
            for neighbor_name in related_names(obj):
                peer=context.scene.objects.get(neighbor_name)
                if peer is not None and peer is not obj:
                    rect=self._projected_frame_rect(context,peer)
                    if rect:_corner_brackets(rect,RED,18.0,1.7)

        # Selected central post is a semantic target within the SAME mesh object;
        # no geometry or duplicated Blender object is created by this highlight.
        # Fixed architectural corner posts are not ordinary draggable Split nodes.
        # W58.29: visible viewport marker even when optional corner post is OFF.
        self._corner_marker_hitboxes=[]
        for a,b,j in self._project_corner_lines(context,obj):
            col=(0.32,0.86,0.76,0.95) if self._corner_post_enabled(obj) else (1.0,0.75,0.35,0.91)
            _line([a,b],col,2.2)
            mx,my=(a[0]+b[0])*.5,(a[1]+b[1])*.5
            _circle(mx,my,11.0,(.012,.033,.047,.94))
            _circle(mx,my,8.4,col)
            _text(mx-3.5,my-4.7,'+',13,(.022,.030,.041,1.))
            self._corner_marker_hitboxes.append((mx,my))
        # The popup is contained in the SAME Composer modal; it does not
        # spawn a Blender popup, capture or additional draw handler.
        if self._corner_marker_popup is not None:
            px,py=self._corner_marker_popup
            px=min(max(144,px),context.region.width-144)
            py=min(max(12,py),context.region.height-110)
            self._corner_marker_popup=(px,py)
            _soft_rect(px-135,py+17,px+135,py+101,(.022,.035,.051,.97),9)
            _text(px-121,py+77,'DWOS  /  CORNER POST',12,MINT)
            _text(px-121,py+56,'Optional junction · LP first',9,MUTED)
            _soft_rect(px-120,py+25,px-7,py+50,(.12,.49,.42,.96),5)
            _soft_rect(px+7,py+25,px+120,py+50,(.38,.32,.23,.96),5)
            _text(px-101,py+32,'ADD POST',10,WHITE)
            _text(px+28,py+32,'NO POST',10,WHITE)

        if getattr(self,'_show_orientation_debug',False):
            topo=self._adaptive_topology(obj)
            if topo:
                half=float(topo.get('width',0.0))*.5
                for st in topo.get('stations',[]):
                    ss=float(st.get('s',0.0))-half
                    try:
                        p0=self._project_sv_polyline(context,obj,[ss],0.0,0.0)
                        pe=self._project_sv_polyline(context,obj,[ss],0.0,0.28)
                        pi=self._project_sv_polyline(context,obj,[ss],0.0,-0.28)
                        ps=self._project_sv_polyline(context,obj,[max(-half,ss-.20),min(half,ss+.20)],0.0,0.0)
                        if p0 and pe:_line([p0[0],pe[0]],(0.26,0.95,0.48,0.95),2.0)
                        if p0 and pi:_line([p0[0],pi[0]],(1.0,0.55,0.22,0.95),2.0)
                        if len(ps)==2:_line(ps,(0.30,0.82,1.0,0.9),1.5)
                    except Exception:pass

        # Legacy semantic retained: if not (selected or hovered):continue
        # Zones are not painted until hovered or selected. For CORNER Branches,
        # hovering one zone softly identifies the whole facade branch so +H/+V
        # ownership is obvious before the click.
        hover_branch=''
        if isinstance(self._hover_zone,int) and 0<=self._hover_zone<len(self._zone_ids):
            hz=self._zone_ids[self._hover_zone]
            if isinstance(hz,str) and '|' in hz:hover_branch=hz.split('|',1)[0]
        for i,(_rect2,poly) in enumerate(self._zones):
            zid=self._zone_ids[i] if i<len(self._zone_ids) else ''
            selected=((zid if zid else i) in (self._selected_zone_ids or []))
            hovered=(i==self._hover_zone)
            branch_hover=bool(hover_branch and isinstance(zid,str) and zid.startswith(hover_branch+'|'))
            if not (selected or hovered or branch_hover):continue
            tint=(0.20,0.79,0.60,0.072 if selected else (0.042 if hovered else 0.018))
            sh=gpu.shader.from_builtin('UNIFORM_COLOR')
            sh.bind();sh.uniform_float('color',tint)
            tris=[(0,j,j+1) for j in range(1,len(poly)-1)]
            if tris:batch_for_shader(sh,'TRIS',{'pos':poly},indices=tris).draw(sh)
            _line(poly+[poly[0]], MINT if selected else (0.39,0.72,0.67,0.53),1.5 if selected else 1.0)
            label=projected_zone_label(poly,i,selected,hovered)
            if label:
                name,(cx,cy)=label
                _soft_rect(cx-18,cy-10,cx+18,cy+10,(0.025,0.085,0.09,0.85),5.0)
                _text(cx-12,cy-4,name,10,MINT if selected else TEXT)

        group_ids=set(self._current_ids(obj))
        for sid,axis,a,b in self._split_lines:
            grouped=(sid if isinstance(sid,str) else s.splits[sid].split_id) in group_ids if (isinstance(sid,str) or (isinstance(sid,int) and 0<=sid<len(s.splits))) else False
            selected=(sid==s.active_zone_split_id if isinstance(sid,str) else sid==s.active_split_index)
            hovered=(sid==self._hover_split)
            dragging=(sid==self._tree_drag_id or sid==self._drag_idx)
            col=MINT if dragging or grouped else (ORANGE if selected else (0.34,0.75,0.66,0.95 if hovered else 0.38))
            if grouped or selected or hovered:
                decay=max(0.0,1.0-(time.monotonic()-getattr(self,'_mullion_focus_at',0.0))/.24) if grouped else 0.0
                _line([a,b],(col[0],col[1],col[2],0.15+0.14*decay),
                      (6.4 if dragging else 5.0)+1.8*decay)
            _line([a,b],col,3.4 if dragging else (3.0 if grouped else (2.6 if selected else (2.0 if hovered else 1.0))))
            if selected or hovered:
                mx=(a[0]+b[0])*.5;my=(a[1]+b[1])*.5
                _circle(mx,my,4.2,col)
                if hovered:_text(mx+8,my+7,('V  /  ВЕРТИКАЛЬ' if axis=='V' else 'H  /  ГОРИЗОНТАЛЬ'),9,col)
                if (self._tree_drag_id==sid or self._drag_idx==sid) and getattr(self,'_snap_feedback',''):
                    _text(mx+9,my+21,'ПРИВЯЗКА · '+self._snap_feedback,9,MINT)

        # Small delete is retained as a keyboard action; the contextual Studio
        # toolbar is rendered OUTSIDE the projected model after panel layout.

        # Show only the handle family of the selected editing task.
        if self._page=='FRAME' and self._active_handle=='FRAME_DEPTH' and self._depth_handle:
            a,b,depth=self._depth_handle
            _line([a,b],BLUE,1.8);_circle(b[0],b[1],5.0,BLUE)
            _text(b[0]+10,b[1]+5,format_meter(depth),10,BLUE)
        if self._page in {'INTERIOR','GLASS'} and self._active_handle:
            ih=self._interior_handles or {}
            families={
                'INTERIOR_DEPTH':('INTERIOR_DEPTH',),
                'INTERIOR_OFFSET':('INTERIOR_OFFSET',),
                'INTERIOR_CLEARANCE':('INTERIOR_CLEARANCE',),
                'GLASS_EDGE':('GLASS_EDGE',),
                'GLASS_OFFSET':('GLASS_OFFSET',),
            }
            visible=families.get(self._active_handle,())
            for key in visible:
                if key not in ih:continue
                color=ORANGE if key=='INTERIOR_DEPTH' else BLUE
                px,py=ih[key];_circle(px,py,5.5,color)
                if key==self._hover_handle or (self._interior_drag and self._interior_drag[0]==key):
                    prop={'INTERIOR_DEPTH':'interior_depth_text','INTERIOR_OFFSET':'interior_offset_text',
                          'INTERIOR_CLEARANCE':'interior_clearance_text',
                          'GLASS_EDGE':'glass_edge_offset_text',
                          'GLASS_OFFSET':'glass_offset_text'}[key]
                    _text(px+9,py+8,key.removeprefix('INTERIOR_')+'  '+getattr(s,prop),10,color)

        # 3.3: one calm HUD, not an N-panel recreated inside the viewport.
        # 3.5.6 measured retained HUD width.  The old 252 px panel forced
        # bottom controls through fit_inside(), creating overlaps/empty squares.
        self._assembly_footer(context,obj)
        w=292  # logical Composer width; physical placement uses profile scale
        try:
            item=context.preferences.addons.get('issmaker_modeling_assistant')
            prof=_ui_profile(item.preferences if item else None)
        except (AttributeError,RuntimeError,TypeError):
            prof=_ui_profile(None)
        hud_scale=prof.safe_window
        self._hud_pixel_scale=hud_scale
        self._hud_font_scale=prof.font
        topo=self._adaptive_topology(obj)
        is_corner=bool(topo and topo.get('classification')=='CORNER')
        post_policy=evaluate_corner_post(topo,s.corner_post_mode) if is_corner else None
        last_validation=str(obj.get('DWOS_smart_interior_reason','') or obj.get('DWOS_last_validation_error','') or '')
        health_extra=health_extra_height(self._health_open)
        expanded_h=panel_height_for_state(self._page,is_corner=is_corner,frame_more=self._frame_more,error=bool(last_validation))+44
        if self._page=='FRAME' and post_policy and not post_policy['available']:
            expanded_h+=26  # warning below post switch, without overlapping next controls
        # RC30: no central-post editor; only ON/OFF is exposed.
        if not self._compact and context.region.height < (expanded_h+health_extra)*hud_scale + 24:
            self._compact=True
        h=((compact_panel_height(is_corner,bool(last_validation))+52) if self._compact else expanded_h)+health_extra
        if context.region.height < h*hud_scale+24 or context.region.width < w*hud_scale+24:
            # A too-small viewport is NOT permission to clamp/stack hit boxes.
            self._panel_rect=None;self._layout_container=None;self._floating_rect=None
            self._draw_hud_micro(context)
            self._draw_panel_free_hints(context,obj)
            self._draw_interior_scissors(context,obj)
            gpu.state.blend_set('NONE')
            return
        panel_origin=self._panel_origin(context,obj,w*hud_scale,h*hud_scale)
        if panel_origin is None and len(self._selected_windows(context))>1 and not self._compact:
            # Preserve user expansion preference when space returns; only
            # temporarily collapse during selected-window crowding.
            self._compact=True
            h=compact_panel_height(is_corner,bool(last_validation))+52+health_extra
            if context.region.height>=h*hud_scale+24:
                panel_origin=self._panel_origin(context,obj,w*hud_scale,h*hud_scale)
        if panel_origin is None:
            # The projected frame may fill the entire Viewport at close zoom.
            # A contextual overlay is preferable to an invisible Composer.
            # The normal non-occluding placement remains the default.
            self._hud_fallback=True
            vw,vh=context.region.width,context.region.height
            # Exclude the model silhouette, but still respect other HUD windows.
            bounds=place_unobstructed((vw,vh),'DWOS_COMPOSER',
                (w*hud_scale,h*hud_scale),(12,vh-h*hud_scale-12),
                blockers=((max(0,vw-96),max(0,vh-96),vw,vh),),
                margin=12,gap=8,scope=scope_from_context(context))
            panel_origin=(bounds[0],bounds[1]) if bounds is not None else None
        if panel_origin is None:
            # No non-occluding place exists. Retained mullion lines still draw,
            # but no panel covers chosen geometry. N-panel controls remain usable.
            self._panel_rect=None
            self._layout_container=None
            self._floating_rect=None
            self._draw_hud_micro(context)
            self._draw_panel_free_hints(context,obj)
            gpu.state.blend_set('NONE')
            return
        x,y=panel_origin
        self._panel_rect=(x,y,w*hud_scale,h*hud_scale)
        self._layout_container=(x,y,w,h)  # logical coordinates while painting
        top=y+h
        hit_start=len(self._hits)
        with _composer_pixel_space((x,y),hud_scale,prof.font):
            # Layered graphite material: one soft shadow and a restrained top accent.
            _soft_rect(x+4,y-5,x+w+4,top-5,SHADOW,12.0)
            shell_palette=_shell.surface(self._frame_theme)
            _soft_rect(x,y,x+w,top,shell_palette['panel'],11.0)
            # Global Studia-inspired panel material, decorative only; hitboxes untouched.
            from issmaker_modeling_assistant.appearance_gpu import draw_shared_background
            px0,py0,px1,py1=_mapped_xyxy(x,y,x+w,top)
            draw_shared_background(self._frame_theme,(px0,py0,px1-px0,py1-py0))
            _soft_outline(x,y,x+w,top,shell_palette['border'],11.0,1.0)
            shell_header=_shell.zones((x,y,x+w,top),kind='composer')
            if shell_header is not None:
                _rect(x+9,shell_header.header[1],x+w-9,top-34,
                      (*shell_palette['header'][:3],.75))
            _rect(x+12,top-36,x+w-12,top-35,(*shell_palette['accent'][:3],.26))
            _line([(x+10,top-1),(x+44,top-1)],(*self._frame_theme.accent,0.52),1.0)
            _text(x+12,top-23,'DWOS / STUDIO',_viewport_skin.text_px('control'),_viewport_skin.ink(self._frame_theme,kind='accent'))
            selected_windows=self._selected_windows(context)
            header_dims=f'{s.width_text} × {s.height_text}'
            if len(selected_windows)>1:header_dims=f'{len(selected_windows)} WINDOWS'
            # Grip and state actions are inside the same mapped pixel/hitbox space.
            _line([(x+100,top-22),(x+100,top-11)],(*self._frame_theme.accent,.55),1.)
            self._hit('HUD_DRAG',x+9,top-32,99,31)
            self._button('HUD_PIN',x+113,top-30,29,20,'P' if self._hud_pinned else 'F',self._hud_pinned,MINT)
            self._button('HUD_RESET',x+145,top-30,31,20,'RST',False,MINT)
            health_color=RED if status['code']=='ERROR' else (ORANGE if status['needs_attention'] else MINT)
            self._button('HEALTH_TOGGLE',x+w-113,top-30,71,20,
                         health_short(status['code']),self._health_open,health_color)
            self._button('CLOSE',x+w-36,top-30,24,20,'X',False,MUTED)
            if self._hud_fallback:
                _text_fitted(x+11,y+3,'HUD · SCREEN SAFE',w-22,8,(*self._frame_theme.accent,.8))

            if self._compact:
                self._button('CONSTRUCTOR_TOGGLE',x+w-73,top-56,61,20,'СХЕМА',self._constructor_mode,BLUE)
                _text(x+12,top-55,'ОБЛАСТЬ ПРИМЕНЕНИЯ',8,MUTED)
                self._scope_toolbar(context,obj,x,top-86,w,top-104)
                if is_corner:
                    _text(x+12,top-123,'CORNER · FULL · 2 GLASS · '+('MITRE' if post_policy['effective_mode']=='OFF' else 'POST'),8,MINT)
                    if not post_policy['available']:
                        _text_fitted(x+12,top-142,'! ЦЕНТР НЕДОСТУПЕН · < 70°',w-24,8,ORANGE)
                if last_validation:
                    _text_fitted(x+12,top-144,'! '+last_validation,w-24,8,RED)
                self._optimization_toolbar(context,obj,x,top-(184 if last_validation else 160),is_corner,topo)
                self._button('EXPAND',x+12,y+10+health_extra,118,23,'ПАРАМЕТРЫ',False,MINT)
                if obj.get("DWOS_W58_auto_uid"):
                    self._button('W58_VIEW_HP',x+145,y+10+health_extra,59,23,'HP',not obj.hide_get(),MINT)
                    self._button('W58_VIEW_LP',x+211,y+10+health_extra,59,23,'LP',obj.hide_get(),BLUE)
                if topo and not obj.get("DWOS_W58_auto_uid"):
                    cls=str(topo.get('classification',''))
                    conf=str(topo.get('orientation_confidence','LOW'))
                    _text_fitted(x+140,y+17+health_extra,cls+' · '+conf,132,8,MUTED)
            else:
                # Production shell: pages and always-visible scoped batch actions.
                self._button('COLLAPSE',x+12,top-61,24,22,'<',False,MUTED)
                self._button('PAGE_FRAME',x+42,top-61,62,22,'FRAME',self._page=='FRAME',MINT)
                self._button('PAGE_INTERIOR',x+109,top-61,58,22,'BOX',self._page=='INTERIOR',MINT)
                self._button('PAGE_GLASS',x+172,top-61,62,22,'GLASS',self._page=='GLASS',MINT)
                self._button('CONSTRUCTOR_TOGGLE',x+w-51,top-61,39,22,'CAD',self._constructor_mode,BLUE)
                _text(x+12,top-80,'ОБЛАСТЬ ПРИМЕНЕНИЯ',8,MUTED)
                self._scope_toolbar(context,obj,x,top-109,w,top-126)
                self._optimization_toolbar(context,obj,x,top-164,is_corner,topo)
                cy=top-198
                if self._page=='FRAME':
                    _text(x+12,cy,'FRAME',9,MINT);cy-=27
                    for key,label,value in (
                        ('FRAME_WIDTH','Основная рама',s.frame_width_text),
                        ('INTERNAL_WIDTH','Рамы / новые',s.internal_width_text),
                        ('FRAME_DEPTH','Глубина рамы',s.frame_depth_text),
                        ('FRAME_OFFSET','Посадка',s.frame_offset_text)):
                        self._field(key,label,value,x+12,cy,w=82);cy-=29
                    self._button('HANDLE_FRAME_DEPTH',x+12,cy,126,22,
                                 'РУЧКА ГЛУБИНЫ',self._active_handle=='FRAME_DEPTH',BLUE)
                    if topo:
                        conf=topo.get('orientation_confidence','LOW')
                        _text_fitted(x+146,cy+7,'SIDE '+str(conf),78,8,MUTED)
                        self._button('FLIP_SIDE',x+w-58,cy,46,22,'FLIP',False,BLUE);cy-=31
                        if topo.get('classification') in {'CORNER','CURVED'}:
                            _text(x+12,cy+6,('CORNER · FULL · 2 GLASS' if topo.get('classification')=='CORNER' else 'CURVED · FULL · 2 GLASS'),8,MINT)
                            cy-=18
                            if is_corner:
                                _text(x+12,cy+5,'ЦЕНТРАЛЬНЫЙ РАЗДЕЛИТЕЛЬ',8,MUTED);cy-=26
                                if post_policy['available']:
                                    self._button('CORNER_ON',x+12,cy,102,22,'С ИМПОСТОМ',post_policy['effective_mode']=='ON',MINT)
                                else:
                                    # Read-only disabled surface: never add an ON hitbox.
                                    _soft_rect(x+12,cy,x+114,cy+22,PANEL_SURFACE,5.5)
                                    _soft_outline(x+12,cy,x+114,cy+22,PANEL_BORDER,5.5)
                                    _text_fitted(x+19,cy+7,'! С ИМПОСТОМ',90,9,ORANGE)
                                self._button('CORNER_OFF',x+120,cy,102,22,'БЕЗ ИМПОСТА',post_policy['effective_mode']=='OFF',BLUE)
                                cy-=30
                                if not post_policy['available']:
                                    angle=post_policy['angle_deg']
                                    warning=(f'! УГОЛ {angle:.1f}° < 70° · ТОЛЬКО БЕЗ ИМПОСТА'
                                             if angle is not None else '! НЕИЗВЕСТНЫЙ УГОЛ · БЕЗ ИМПОСТА')
                                    _text_fitted(x+12,cy+8,warning,w-24,8,ORANGE)
                                    cy-=26
                        else:
                            _text_fitted(x+12,cy+14,'STANDARD FACADE' ,200,8,MUTED);cy-=16
                    _text(x+12,cy,'РАЗДЕЛИТЬ ЗОНУ',8,MUTED);cy-=24
                    self._button('ADD_V',x+12,cy,102,22,'+ V')
                    self._button('ADD_H',x+120,cy,102,22,'+ H');cy-=31
                    self._button('MORE_FRAME',x+12,cy,210,22,
                                 '- ДОПОЛНИТЕЛЬНО' if self._frame_more else '+ ДОПОЛНИТЕЛЬНО',self._frame_more,MUTED)
                    if self._frame_more:
                        cy-=32
                        _text(x+12,cy,'РЕЖИМ ГЕОМЕТРИИ — ВСЕГДА ВВЕРХУ',8,MUTED);cy-=23
                        _text(x+12,cy,(s.frame_material.name if s.frame_material else 'DWOS_Frame_Default')[:28],9,TEXT);cy-=25
                        self._button('MAT_CHOOSE',x+12,cy,132,22,'МАТЕРИАЛ',False,BLUE)
                        self._button('MAT_DEFAULT',x+150,cy,72,22,'DEFAULT',False,MUTED);cy-=31
                        _text_fitted(x+12,cy+7,'COPY раскладки — в верхней панели области',w-24,9,MUTED)
                elif self._page=='INTERIOR':
                    _text(x+12,cy,'INTERIOR',9,MINT);cy-=29
                    self._button('INT_OFF',x+12,cy,102,24,'OFF',s.interior_mode=='OFF',MINT)
                    self._button('INT_BOX',x+120,cy,102,24,'BOX',s.interior_mode!='OFF',MINT);cy-=37
                    if s.interior_mode!='OFF':
                        self._button('INT_MANUAL_FIX',x+12,cy,210,22,'РУЧНОЕ · ИСПРАВИТЬ',False,MINT);cy-=29
                        if not is_corner:
                            try:groups_requested=int(s.interior_groups_count)
                            except Exception:groups_requested=0
                            mode=str(getattr(s,'interior_partition_mode','AUTO'))
                            self._button('INT_GROUPS',x+12,cy,210,22,
                                ('СЕКЦИИ: ВСЕ V' if mode=='AUTO' else f'СЕКЦИИ: {groups_requested}' if mode=='COUNT' else 'СЕКЦИИ: 1 + ВЫБРАННЫЕ V'),False,BLUE)
                            cy-=30
                            self._button('INT_TOGGLE_DIVIDER',x+12,cy,210,22,'ГРАНИЦА ПО ВЫБРАННОМУ V',False,MINT);cy-=30
                        if is_corner:
                            if self._active_handle=='INTERIOR_DEPTH': self._active_handle=None
                            _text(x+12,cy+7,'Глубина CORNER',8,MUTED)
                            _text(x+118,cy+7,'AUTO',9,MINT);cy-=29
                            for key,label,value in (
                                ('INTERIOR_OFFSET','Сдвиг',s.interior_offset_text),
                                ('INTERIOR_CLEARANCE','Контур',s.interior_clearance_text)):
                                self._field(key,label,value,x+12,cy,w=82);cy-=29
                            self._button('HANDLE_INTERIOR_OFFSET',x+12,cy,100,22,'OFFSET',self._active_handle=='INTERIOR_OFFSET',BLUE)
                            self._button('HANDLE_INTERIOR_CLEARANCE',x+118,cy,104,22,'CONTOUR',self._active_handle=='INTERIOR_CLEARANCE',BLUE)
                            cy-=31
                            _text(x+12,cy+7,'CORNER INTERIOR · LOCKED',8,MINT)
                            _text(x+12,cy-8,'2 прямые границы · 1 внутренний угол',8,MUTED)
                        else:
                            for key,label,value in (
                                ('INTERIOR_DEPTH','Глубина',s.interior_depth_text),
                                ('INTERIOR_OFFSET','Сдвиг',s.interior_offset_text),
                                ('INTERIOR_CLEARANCE','Контур',s.interior_clearance_text)):
                                self._field(key,label,value,x+12,cy,w=82);cy-=29
                            self._button('HANDLE_INTERIOR_DEPTH',x+12,cy,65,22,'DEPTH',self._active_handle=='INTERIOR_DEPTH',BLUE)
                            self._button('HANDLE_INTERIOR_OFFSET',x+82,cy,65,22,'OFFSET',self._active_handle=='INTERIOR_OFFSET',BLUE)
                            self._button('HANDLE_INTERIOR_CLEARANCE',x+152,cy,70,22,'CONTOUR',self._active_handle=='INTERIOR_CLEARANCE',BLUE)
                else:
                    _text(x+12,cy,('GLASS · CORNER 2 PANELS · FULL' if is_corner else 'GLASS · '+('DOUBLE' if s.optimization=='FULL' else 'SINGLE')),9,MINT);cy-=28
                    self._field('GLASS_OFFSET','Глубина',s.glass_offset_text,x+12,cy,w=82);cy-=29
                    self._field('GLASS_EDGE','Контур',s.glass_edge_offset_text,x+12,cy,w=82);cy-=29
                    if s.optimization=='FULL':
                        self._field('GLASS_GAP','Между листами',s.glass_gap_text,x+12,cy,w=82);cy-=29
                    self._button('HANDLE_GLASS_EDGE',x+12,cy,100,22,'КРАЙ',self._active_handle=='GLASS_EDGE',BLUE)
                    self._button('HANDLE_GLASS_OFFSET',x+118,cy,104,22,'ГЛУБИНА',self._active_handle=='GLASS_OFFSET',BLUE)

            if (not self._compact) and last_validation:
                _text_fitted(x+12,y+10+health_extra,'! '+last_validation,w-24,8,RED)
            elif (not self._compact) and getattr(self,'_history_message',''):
                _text_fitted(x+12,y+10+health_extra,self._history_message,w-24,8,MUTED)
            self._health_card(context,obj,status,x,y,w,top,health_extra)

        self._layout_container=None
        if prof.debug:
            panel_bounds=(x,y,x+w*hud_scale,y+h*hud_scale)
            panel_controls=[(str(item[0]),tuple(item[1:5])) for item in self._hits[hit_start:]]
            issues=_ui_inspect_layout(panel_bounds,panel_controls,margin=0.)
            if self._layout_skips:
                issues.extend('HIDDEN_OVERFLOW:'+key for key in self._layout_skips)
            self._layout_issues=issues[:100]
            _ui_record_layout('DWOS_COMPOSER',context.region.width,context.region.height,
                              panel_controls)
            _soft_outline(x,y,x+w*hud_scale,y+h*hud_scale,
                          (1.,.34,.23,.98) if issues else (0.28,.78,.67,.62),
                          11.0,1.3)
        else:
            self._layout_issues=[]
        if self._layout_skips:
            _text_fitted(x+8,y+h*hud_scale+6,
                         f'! HUD: скрыты элементы ({len(self._layout_skips)}). Уменьшите масштаб.',
                         max(20,w*hud_scale-16),9,ORANGE)
        # Contextual overlay comes after the panel so hit zones match visual stacking.
        panel=self._panel_rect
        if self._selected_zone is not None and self._zone_menu_anchor:
            zone_scale=popup_scale_for_viewport((200,88),
                (context.region.width,context.region.height),hud_scale)
            if zone_scale is not None:
                suggested=floating_rect(self._zone_menu_anchor,
                    (200*zone_scale,88*zone_scale),
                    (context.region.width,context.region.height),avoid=(panel,),margin=14)
                rect=place_unobstructed((context.region.width,context.region.height),
                    'DWOS_ZONE_POPUP',(200*zone_scale,88*zone_scale),
                    suggested[:2],blockers=((r[0],r[1],r[0]+r[2],r[1]+r[3])
                                             for r in (panel,self._footer_rect) if r),
                    margin=14,gap=8,scope=scope_from_context(context))
                if rect is not None:
                    zx,zy,_,_=rect
                    self._floating_rect=(zx,zy,200*zone_scale,88*zone_scale)
                    self._floating_scale=zone_scale
                    with _composer_pixel_space((zx,zy),zone_scale,prof.font):
                        _soft_rect(zx,zy,zx+200,zy+88,(0.026,0.034,0.040,0.98),9.0)
                        _soft_outline(zx,zy,zx+200,zy+88,PANEL_BORDER,9.0,1.0)
                        _text_fitted(zx+10,zy+64,'РАЗДЕЛИТЬ ЗОНЫ · '+str(len(self._selected_zone_ids or [self._selected_zone])),159,10,MINT)
                        if getattr(self,'_history_message','').startswith('ГРУППОВОЙ SPLIT:'):
                            _text_fitted(zx+10,zy+46,self._history_message[18:],174,8,RED)
                        self._button('ZONE_CLOSE',zx+169,zy+60,22,22,'×',False,MUTED)
                        self._button('ZONE_ADD_V',zx+8,zy+12,86,29,'Вертикально')
                        self._button('ZONE_ADD_H',zx+100,zy+12,92,29,'Горизонт.')
                else:
                    self._history_message="Недостаточно места для редактора зоны · увеличьте Viewport"
            else:
                self._history_message='Недостаточно места для редактора зоны · уменьшите масштаб HUD'
        else:
            sid=s.active_zone_split_id if (s.zone_tree_json or getattr(s,'branch_state_json','')) else s.active_split_index
            if sid not in ('',-1,None) and self._compact and sid==self._split_popup_id and not self._current_ids(obj):
                segment=next((r for r in self._split_lines if r[0]==sid),None)
                if segment:
                    _id,axis,a,b=segment
                    mid=((a[0]+b[0])*.5,(a[1]+b[1])*.5)
                    anchor=getattr(self,'_split_popup_anchor',None) or mid
                    split_scale=popup_scale_for_viewport((274,184),
                        (context.region.width,context.region.height),hud_scale)
                    rect=(place_mullion_tag(anchor,(context.region.width,context.region.height),
                            avoid=tuple(r for r in (panel,self._footer_rect) if r),
                            size=(274*split_scale,184*split_scale)) if split_scale is not None else None)
                    rect=self._safe_aux_window(context,'DWOS_SPLIT_POPUP',rect)
                    if rect is None:
                        self._history_message='SPLIT: увеличьте Viewport или уменьшите масштаб HUD'
                    else:
                        zx,zy,zw,zh=rect
                        self._floating_rect=rect
                        self._floating_scale=split_scale
                        with _composer_pixel_space((zx,zy),split_scale,prof.font):
                            _soft_rect(zx,zy,zx+274,zy+184,(0.026,0.034,0.040,0.98),9.0)
                            _soft_outline(zx,zy,zx+274,zy+184,PANEL_BORDER,9.0,1.0)
                            _text_fitted(zx+10,zy+160,'SPLIT · '+axis,213,11,ORANGE)
                            self._button('SPLIT_CLOSE',zx+243,zy+152,22,23,'×',False,MUTED)
                            if isinstance(sid,str) and (s.zone_tree_json or getattr(s,'branch_state_json','')):
                                try:
                                    match=self._split_row(obj,sid)
                                    if not match:raise ValueError('Split not found')
                                    extent=parse_meter_text(s.width_text if axis=='V' else s.height_text)
                                    self._field('TREE_POSITION','Позиция',format_meter(match['position']+extent*.5),zx+9,zy+122,w=94,payload=sid)
                                    self._field('TREE_THICKNESS','Толщина',format_meter(match['thickness']),zx+9,zy+89,w=94,payload=sid)
                                    self._button('SPLIT_GLOBAL',zx+10,zy+51,254,23,'СБРОСИТЬ К ОБЩЕЙ ТОЛЩИНЕ',False,BLUE)
                                    self._button('TREE_DELETE',zx+10,zy+13,100,24,'УДАЛИТЬ',False,RED)
                                    self._button('SPLIT_DONE',zx+156,zy+13,108,24,'ГОТОВО',False,MINT)
                                except (ValueError,TypeError,StopIteration):pass
                            elif isinstance(sid,int) and 0<=sid<len(s.splits):
                                sp=s.splits[sid]
                                self._field('SPLIT_POSITION','Позиция',sp.position_text,zx+9,zy+122,w=94,payload=sid)
                                self._field('SPLIT_THICKNESS','Толщина',sp.thickness_text if sp.override_thickness else s.internal_width_text,zx+9,zy+89,w=94,payload=sid)
                                self._button('SPLIT_GLOBAL',zx+10,zy+51,254,23,'СБРОСИТЬ К ОБЩЕЙ ТОЛЩИНЕ',False,BLUE)
                                self._button('SPLIT_DELETE',zx+10,zy+13,100,24,'УДАЛИТЬ',False,RED)
                                self._button('SPLIT_DONE',zx+156,zy+13,108,24,'ГОТОВО',False,MINT)
        self._draw_mullion_hud(context,obj)
        self._draw_interior_scissors(context,obj)
        self._mullion_focus_tag(context,obj)
        if self._constructor_mode:
            self._draw_constructor_guides(context,obj)
        if self._box_start is not None and self._box_end is not None:
            a=self._box_start;b=self._box_end
            _rect(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]),
                  (0.21,0.92,0.69,0.09))
            corners=[(a[0],a[1]),(b[0],a[1]),(b[0],b[1]),(a[0],b[1]),a]
            _line(corners,MINT,1.5)
        gpu.state.blend_set('NONE')

    def _find_hit(self, x, y):
        for hit in reversed(self._hits or []):
            key, x0, y0, x1, y1, payload = hit
            if x0 <= x <= x1 and y0 <= y <= y1:
                return key, payload
        return None, None

    def _inside_ui(self,x,y):
        for rect in (self._panel_rect,self._floating_rect,self._footer_rect):
            if rect:
                rx,ry,rw,rh=rect
                if rx<=x<=rx+rw and ry<=y<=ry+rh:return True
        return False

    def _split_hit(self, x, y):
        best = (-1, 999.0)
        for i, axis, a, b in self._split_lines or []:
            d = _distance_point_segment((x, y), a, b)
            if d < best[1]:
                best = (i, d)
        return best[0] if best[1] <= 10.0 else -1

    def _interior_handle_hit(self,x,y):
        for key in ('INTERIOR_CLEARANCE','INTERIOR_DEPTH','INTERIOR_OFFSET',
                    'GLASS_EDGE','GLASS_OFFSET'):
            p=(self._interior_handles or {}).get(key)
            if p and math.hypot(x-p[0],y-p[1])<13:
                return key
        return None

    def _depth_hit(self, x, y):
        if not self._depth_handle:
            return False
        _, b, _ = self._depth_handle
        return math.hypot(x - b[0], y - b[1]) <= 12.0

    def _local_hit(self, context, x, y):
        obj=self.obj;topo=self._adaptive_topology(obj)
        if topo is None:
            origin=view3d_utils.region_2d_to_origin_3d(context.region,context.region_data,(x,y))
            direction=view3d_utils.region_2d_to_vector_3d(context.region,context.region_data,(x,y))
            inv=obj.matrix_world.inverted();o=inv@origin;d=inv.to_3x3()@direction
            plane_z=parse_meter_text(obj.dwos_window.frame_offset_text,allow_negative=True)
            if abs(d.z)<1e-9:return None
            t=(plane_z-o.z)/d.z
            return None if t<0 else o+d*t

        # Invert the adaptive ribbon in screen space.  First recover S from the
        # projected centreline, then V from the projected local vertical at S.
        # This keeps direct dragging intuitive on polygonal curves and hard corners
        # without flattening the Host into one ray/plane intersection.
        try:
            w=float(topo['width']);h=float(topo['height'])
            depth=parse_meter_text(obj.dwos_window.frame_offset_text,allow_negative=True)
            ss=self._adaptive_s_samples(topo,-w*.5,w*.5)
            centre=self._project_sv_polyline(context,obj,ss,0.0,depth)
            if len(centre)<2:return None
            best=None
            for i,(a,b) in enumerate(zip(centre,centre[1:])):
                ax,ay=a;bx,by=b;vx=bx-ax;vy=by-ay;l2=vx*vx+vy*vy
                t=0.0 if l2<=1e-12 else max(0.0,min(1.0,((x-ax)*vx+(y-ay)*vy)/l2))
                q=(ax+t*vx,ay+t*vy);dist=(x-q[0])**2+(y-q[1])**2
                if best is None or dist<best[0]:best=(dist,i,t)
            _dist,i,t=best;scoord=ss[i]*(1-t)+ss[i+1]*t
            ends=self._project_sv_polyline(context,obj,[scoord],-h*.5,depth)+self._project_sv_polyline(context,obj,[scoord],h*.5,depth)
            if len(ends)!=2:return Vector((scoord,0.0,depth))
            a,b=ends;vx=b[0]-a[0];vy=b[1]-a[1];l2=vx*vx+vy*vy
            tv=0.5 if l2<=1e-12 else max(0.0,min(1.0,((x-a[0])*vx+(y-a[1])*vy)/l2))
            vcoord=-h*.5+tv*h
            return Vector((scoord,vcoord,depth))
        except (ValueError,TypeError,KeyError,ZeroDivisionError):
            return None

    def _current_ids(self, obj):
        s=obj.dwos_window
        available={sid if isinstance(sid,str) else s.splits[sid].split_id
                   for sid,axis,a,b in self._split_lines or []
                   if isinstance(sid,str) or (isinstance(sid,int) and 0<=sid<len(s.splits))}
        selected=[sid for sid in parse_ids(s.mullion_selection_json) if sid in available]
        return marquee_compatible([],selected,self._mullion_axes(obj))

    def _preview_group_allowed(self, obj, active_id, target_pos, start_pos):
        """Validate every moved line before drawing the ghost.

        In 3.3 this includes the physical Corner Post envelope, so a preview can
        never enter a position that the staged Mesh would later reject.
        """
        s=obj.dwos_window
        ids=self._current_ids(obj)
        if active_id not in ids or len(ids)<2:return True
        delta=target_pos-start_pos
        w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
        fw=parse_meter_text(s.frame_width_text)
        posts=self._corner_posts_for_obj(obj)
        try:
            bstate,bleaves,bbands=self._branch_layout_for_obj(obj)
            if bbands is not None:
                rows=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4],'branch_id':r[6]} for r in bbands]
                requested=targets(rows,ids,'MOVE',value=delta)
                topo=self._adaptive_topology(obj)
                staged=apply_branch_positions(bstate,topo,h,fw,requested,posts)
                check=branch_rows_as_dicts(staged,topo,h,fw,posts)
                if validate_split_rows(check,posts,0.003):return False
            elif s.zone_tree_json:
                requested=targets(entries(s.zone_tree_json,w,h,fw),ids,'MOVE',value=delta)
                staged=apply_zone_targets(s.zone_tree_json,w,h,fw,requested)
                if validate_split_rows(entries(staged,w,h,fw),posts,0.003):
                    return False
            else:
                rows=[{'id':sp.split_id,'axis':sp.axis,
                       'position':parse_meter_text(sp.position_text),
                       'thickness':parse_meter_text(sp.thickness_text if sp.override_thickness else s.internal_width_text)}
                      for sp in s.splits]
                requested=legacy_targets(rows,ids,'MOVE',delta,width=w,height=h,frame_width=fw)
                centred=[]
                for row in rows:
                    pos=float(requested.get(row['id'],row['position']))
                    centred.append(dict(row,position=pos-(w*.5 if row['axis']=='V' else h*.5),
                        rect=(-w*.5+fw,w*.5-fw,-h*.5+fw,h*.5-fw)))
                if validate_split_rows(centred,posts,0.003):
                    return False
            return True
        except (ValueError,TypeError,KeyError,ZeroDivisionError):
            return False


    def _snap_position(self,obj,axis,rect,position,active_id):
        """Snap on the selected CORNER branch or local zone, never another branch."""
        peers=[]
        branch=self._split_row(obj,active_id)
        _bs,_leaves,bands=self._branch_layout_for_obj(obj)
        if bands is not None:
            for r in bands:
                if r[0]!=active_id and r[1]==axis and branch and r[6]==branch.get('branch_id'):
                    peers.append(r[2])
        elif obj.dwos_window.zone_tree_json:
            try:
                w=parse_meter_text(obj.dwos_window.width_text)
                h=parse_meter_text(obj.dwos_window.height_text)
                fw=parse_meter_text(obj.dwos_window.frame_width_text)
                for row in zone_layout(decode_zones(obj.dwos_window.zone_tree_json),w,h,fw)[1]:
                    if row[0]!=active_id and row[1]==axis and row[4]==rect:
                        peers.append(row[2])
            except (ValueError,KeyError,TypeError):pass
        else:
            w=parse_meter_text(obj.dwos_window.width_text)
            h=parse_meter_text(obj.dwos_window.height_text)
            for sp in obj.dwos_window.splits:
                if sp.split_id!=active_id and sp.axis==axis:
                    peers.append(parse_meter_text(sp.position_text)-(w*.5 if axis=='V' else h*.5))
        result,label=snap_position(position,axis,rect,peers)
        return result,label

    def _micro_field(self,key,label,value,x,y,width=65,payload=None):
        """HUD input: short label + unit-aware number, without Blender RNA popups."""
        theme=_dwos_ui_snapshot()
        _text(x,y+8,label,_viewport_skin.text_px('caption'),_viewport_skin.ink(theme,kind='muted'))
        fx=x+51
        active=self._editing and self._editing[0]==key
        state='focused' if active else 'normal'
        style=_viewport_skin.component('field',state,theme=theme)
        from issmaker_modeling_assistant.core.studio_gpu_components import draw_component
        if not draw_component(_mapped_xyxy(fx,y,fx+width,y+24),part='field',state=state,theme=theme,
                              scale=_PANEL_PIXELS.scale if _PANEL_PIXELS is not None else 1.):
            _soft_rect(fx,y,fx+width,y+24,style.background,_viewport_skin.control_radius('field'))
            _soft_outline(fx,y,fx+width,y+24,style.border,_viewport_skin.control_radius('field'),1.2)
        caret=('|' if int((time.monotonic()-getattr(self,'_field_focus_at',0.0))*2)%2==0 else '')
        value=((self._buffer[:getattr(self,'_caret_pos',len(self._buffer))]+caret+self._buffer[getattr(self,'_caret_pos',len(self._buffer)):]) if active else str(value))
        _text(fx+6,y+6,str(value)[:14],_viewport_skin.text_px('caption'),
              _viewport_skin.control_ink(theme,part='field',state=state))
        self._hit('FIELD:'+key,fx,y,width,24,payload)

    def _projected_frame_rect(self, context, obj):
        """Screen bounds of the real generated frame, including adaptive curves."""
        pts=[]
        for corner in obj.bound_box:
            p=view3d_utils.location_3d_to_region_2d(context.region,context.region_data,obj.matrix_world@Vector(corner))
            if p is not None:pts.append((float(p.x),float(p.y)))
        if not pts:return None
        xx=[p[0] for p in pts];yy=[p[1] for p in pts]
        return min(xx),min(yy),max(xx)-min(xx),max(yy)-min(yy)

    def _draw_panel_free_hints(self,context,obj):
        # Retain non-interactive cues if a full panel cannot fit the viewport.
        self._mullion_focus_tag(context,obj)
        if self._constructor_mode:
            self._draw_constructor_guides(context,obj)

    def _optimization_toolbar(self,context,obj,x,y,is_corner,topo):
        """Permanent compact geometry switch, shared by all Composer pages.

        CORNER/CURVED retain the mandated FULL geometry and two glazing layers.
        OPTIMIZED is shown but guarded by the action handler for these types.
        """
        s=obj.dwos_window
        locked=bool(is_corner or (topo and topo.get('classification')=='CURVED'))
        self._button('OPT_FULL',x+12,y,96,23,'FULL',locked or s.optimization=='FULL',MINT)
        self._button('OPT_OPT',x+114,y,108,23,'OPTIMIZED',not locked and s.optimization=='OPTIMIZED',MUTED if locked else BLUE)
        if locked:
            _text_fitted(x+230,y+7,'FULL!',50,8,ORANGE)
        else:
            _text_fitted(x+230,y+7,'FRAME',50,8,MUTED)

    def _draw_constructor_guides(self,context,obj):
        """Optional screen-space architectural dimensions, never changes geometry.

        Bounding boxes are merely projected extents, especially for curved and
        corner facades. Labels always use the stored nominal DWOS dimensions.
        """
        frame=self._projected_frame_rect(context,obj)
        if frame is None:return
        s=obj.dwos_window
        blocked=[self._panel_rect,self._floating_rect,self._footer_rect]
        # Other selected windows remain visually unobscured.
        blocked.extend(self._projected_frame_rect(context,other)
                       for other in self._selected_windows(context) if other is not obj)
        rails=constructor_guides(frame,(context.region.width,context.region.height),blocked)
        topo=self._adaptive_topology(obj)
        complex_facade=bool(topo and topo.get('classification') in {'CURVED','CORNER'})
        # Non-interactive annotations; no hitboxes, mesh writes or draw timers.
        dims={'horizontal':str(s.width_text),'vertical':str(s.height_text)}
        for axis,rail in rails.items():
            for i,(p,q) in enumerate(rail['segments']):
                _line([p,q],(BLUE[0],BLUE[1],BLUE[2],0.72 if i==0 else 0.45),1.15)
            lx,ly,lw,lh=rail['label']
            _soft_rect(lx-3,ly-4,lx+lw+3,ly+lh+3,(0.015,0.027,0.034,0.95),5)
            title=('W' if axis=='horizontal' else 'H')+' · '+dims[axis]
            _text_fitted(lx+4,ly+2,title,lw-8,10,BLUE)
        if complex_facade and rails:
            # Do not misrepresent a projected CORNER/CURVED bbox as measured 2D geometry.
            rail=rails.get('horizontal') or rails.get('vertical')
            lx,ly,lw,lh=rail['label']
            x=lx; y=ly-20 if ly>36 else ly+20
            if x>=9 and y>=9 and x+lw<context.region.width-9 and y+12<context.region.height-9:
                note=(x,y,lw,12)
                if not any(r and overlap_area(note,r,pad=4)>0 for r in blocked):
                    _text_fitted(x,y,'НОМИНАЛ · НЕ ПРОЕКЦИЯ',lw,8,MUTED)

        # Optional architectural turn note: label the true topology, not the
        # perspective-projected angle. Never create a hit target or alter mesh.
        if self._constructor_mode and topo and topo.get('classification')=='CORNER':
            half=float(topo.get('width',0.0))*.5
            for junction in (topo.get('corner_junctions') or ())[:3]:
                try:
                    turn=float(junction.get('angle_deg'))
                    if not (0.0<turn<180.0):continue
                    points=self._project_sv_polyline(context,obj,[float(junction['s'])-half],0.0,0.0)
                    if not points:continue
                    px,py=points[0]
                    label=(min(context.region.width-104,max(8,px+18)),min(context.region.height-20,max(8,py+22)),99,17)
                    if any(r and overlap_area(label,r,pad=5)>0 for r in blocked):continue
                    if label[0]-px>115 or label[1]-py>58:continue
                    _line([(px,py),(label[0],label[1]+8)],BLUE,1.0)
                    _soft_rect(*((label[0],label[1],label[0]+label[2],label[1]+label[3])),(0.015,0.027,0.034,0.92),5)
                    _text_fitted(label[0]+5,label[1]+4,'ПОВОРОТ %.1f°'%turn,90,9,BLUE)
                except (ValueError,TypeError,KeyError,IndexError):
                    continue

    def _draw_mullion_hud(self,context,obj):
        """One tiny action for 2+ compatible segments. Never a second settings card."""
        s=obj.dwos_window
        ids=self._current_ids(obj)
        if not ids or self._selected_zone is not None:
            return
        if len(ids)==1:
            sid=ids[0]
            segments=[r for r in self._split_lines
                      if (r[0] if isinstance(r[0],str) else s.splits[r[0]].split_id)==sid]
            pointer=getattr(self,'_last_mullion_click',None)
            if not segments:return
            points=[((r[2][0]+r[3][0])*.5,(r[2][1]+r[3][1])*.5) for r in segments]
            anchor=(min(points,key=lambda p:(p[0]-pointer[0])**2+(p[1]-pointer[1])**2)
                    if pointer else points[len(points)//2])
            blocked=[self._panel_rect,self._footer_rect]
            rect=place_mullion_tag(anchor,(context.region.width,context.region.height),
                                   avoid=tuple(r for r in blocked if r),size=(176,34))
            # No remote frame-wide fallback: hide rather than place a distant card.
            rect=self._safe_aux_window(context,'DWOS_MULLION_EDIT',rect)
            if rect is None:return
            x,y,bw,bh=rect
            self._floating_rect=rect
            theme=_dwos_ui_snapshot()
            card=_viewport_skin.component('card',theme=theme)
            _soft_rect(x,y,x+bw,y+bh,card.background,_viewport_skin.control_radius('panel'))
            _soft_outline(x,y,x+bw,y+bh,card.border,_viewport_skin.control_radius('panel'),1.2)
            self._button('MULLION_EDIT',x+4,y+3,bw-8,bh-6,'ПАРАМЕТРЫ ИМПОСТА',False,MINT)
            return
        try:
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            _bs,_bl,_bb=self._branch_layout_for_obj(obj)
            if _bb is not None:
                rows=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],
                       'rect':r[4],'branch_id':r[6]} for r in _bb]
                axis=compact_align_axis(rows,ids)
                if axis:
                    proposed=targets(rows,ids,'ALIGN')
                    topo=self._adaptive_topology(obj)
                    posts=self._corner_posts_for_obj(obj)
                    staged=apply_branch_positions(_bs,topo,h,fw,proposed,posts)
                    check=branch_rows_as_dicts(staged,topo,h,fw,posts)
                    if validate_split_rows(check,posts,0.003): axis=None
            elif s.zone_tree_json:
                rows=entries(s.zone_tree_json,w,h,fw)
                axis=compact_align_axis(rows,ids)
                if axis:
                    proposed=targets(rows,ids,'ALIGN')
                    apply_zone_targets(s.zone_tree_json,w,h,fw,proposed)
            else:
                # Legacy global splitters cross the same cell and cannot align
                # without collapsing geometry. The tool stays hidden.
                axis=None
            if axis is None:return
        except (ValueError,TypeError,KeyError,ZeroDivisionError):
            return
        segments=[r for r in self._split_lines
                  if (r[0] if isinstance(r[0],str) else s.splits[r[0]].split_id) in ids]
        if not segments:return
        points=[((r[2][0]+r[3][0])*.5,(r[2][1]+r[3][1])*.5) for r in segments]
        pointer=getattr(self,'_last_mullion_click',None)
        anchor=(min(points,key=lambda p:(p[0]-pointer[0])**2+(p[1]-pointer[1])**2)
                if pointer else points[len(points)//2])
        rect=place_mullion_tag(anchor,(context.region.width,context.region.height),
                               avoid=tuple(r for r in (self._panel_rect,self._footer_rect) if r),size=(160,34))
        rect=self._safe_aux_window(context,'DWOS_ALIGN_SPLITS',rect)
        if rect is None:return
        x,y,bw,bh=rect
        self._floating_rect=rect
        theme=_dwos_ui_snapshot()
        card=_viewport_skin.component('card',theme=theme)
        _soft_rect(x,y,x+bw,y+bh,card.background,_viewport_skin.control_radius('panel'))
        _soft_outline(x,y,x+bw,y+bh,card.border,_viewport_skin.control_radius('panel'),1.2)
        self._button('ALIGN_SEGMENTS',x+4,y+3,bw-8,bh-6,
                     'Выровнять H' if axis=='H' else 'Выровнять V',False,MINT)

    def _start_mullion_focus_pulse(self):
        """Bounded 240ms accent on selection; never a permanent Viewport timer."""
        self._mullion_focus_at=time.monotonic()
        if getattr(self,'_mullion_pulse_timer_pending',False):return
        self._mullion_pulse_timer_pending=True
        target=self
        def tick():
            from ....core import lifecycle_lease as _lease
            if (_ACTIVE is not target or getattr(target, '_disabled', False)
                    or not _lease.alive('dwos.composer', getattr(target, '_pulse_token', None))):
                target._mullion_pulse_timer_pending=False
                return None
            if time.monotonic()-target._mullion_focus_at>0.24:
                target._mullion_pulse_timer_pending=False
                return None
            try:
                for screen in bpy.data.screens:
                    for area in screen.areas:
                        if area.type=='VIEW_3D' and area.as_pointer()==target._area_pointer:
                            area.tag_redraw()
            except (ReferenceError,RuntimeError,AttributeError):pass
            return 0.045
        bpy.app.timers.register(tick,first_interval=0.045)

    def _select_mullion_click(self, s, identifier):
        """Keep the selection when dragging a picked line; ordinary new click replaces."""
        chosen=parse_ids(s.mullion_selection_json)
        if identifier not in chosen:
            s.mullion_selection_json=encode_ids([identifier])
        self._start_mullion_focus_pulse()

    def _mullion_axes(self, obj):
        s=obj.dwos_window
        return {(sid if isinstance(sid,str) else s.splits[sid].split_id):axis
                for sid,axis,_a,_b in (self._split_lines or [])
                if isinstance(sid,str) or (isinstance(sid,int) and 0<=sid<len(s.splits))}

    def _begin_field(self, key, payload):
        s = self.obj.dwos_window
        if key in {'HUD_OFFSET','HUD_STEP'}:
            value=s.mullion_offset_text if key=='HUD_OFFSET' else s.mullion_step_text
        elif key in {'TREE_POSITION','TREE_THICKNESS'}:
            # Authoritative UI read: CORNER Branch or Zone as selected by _split_row.
            # Never silently substitute 0 for a stale/invalid split reference.
            self._assert_split_consistency(self.obj,str(payload))
            row=self._split_row(self.obj,str(payload))
            value=split_field_value(row,key,parse_meter_text(s.width_text),parse_meter_text(s.height_text))
            log_event('COMPOSER_SPLIT_FIELD_OPEN',window=self.obj.name,split=str(payload),
                      field=key,source=('CORNER_BRANCH' if row.get('branch_id') else 'ZONE'),value=value)
        elif key == 'SPLIT_POSITION':
            value = s.splits[payload].position_text
        elif key == 'SPLIT_THICKNESS':
            value = s.splits[payload].thickness_text if s.splits[payload].override_thickness else s.internal_width_text
        else:
            prop = {
                'FRAME_WIDTH': 'frame_width_text', 'INTERNAL_WIDTH': 'internal_width_text',
                'FRAME_DEPTH': 'frame_depth_text', 'FRAME_OFFSET': 'frame_offset_text',
                'POST_WIDTH':'corner_post_width_text',
                'INTERIOR_OFFSET': 'interior_offset_text',
            'INTERIOR_DEPTH': 'interior_depth_text',
            'INTERIOR_CLEARANCE': 'interior_clearance_text',
            'GLASS_OFFSET': 'glass_offset_text',
                'GLASS_GAP': 'glass_gap_text',
                'GLASS_EDGE': 'glass_edge_offset_text',
            }[key]
            value = getattr(s, prop)
        self._editing = (key, payload); self._buffer = str(value); self._edit_fresh = False
        self._caret_pos=len(self._buffer);self._select_all=False
        self._field_focus_at=time.monotonic()
        self._editing_render_font_px=max(8,int(round(11*getattr(self,'_hud_font_scale',1.0))))
        self._start_focus_redraw()

    def _start_focus_redraw(self):
        if getattr(self,'_focus_timer_pending',False):
            return
        self._focus_timer_pending=True
        target=self
        def pulse():
            from ....core import lifecycle_lease as _lease
            if (_ACTIVE is not target or getattr(target, '_disabled', False)
                    or not _lease.alive('dwos.composer', getattr(target, '_pulse_token', None))
                    or not getattr(target,'_editing',None)):
                target._focus_timer_pending=False
                return None
            area=bpy.context.area
            if area and area.type=='VIEW_3D':
                area.tag_redraw()
            else:
                try:
                    for screen in bpy.data.screens:
                        for area in screen.areas:
                            if area.type=='VIEW_3D' and area.as_pointer()==target._area_pointer:
                                area.tag_redraw()
                except Exception:
                    pass
            return 0.50
        bpy.app.timers.register(pulse,first_interval=0.12)

    def _ensure_active(self, context):
        obj = self.obj
        if obj is not None:
            try:
                context.view_layer.objects.active = obj
                obj.select_set(True)
            except Exception:
                pass

    def _history_checkpoint(self, context, label):
        history_push(context,label)

    def _history_apply(self, context, redo=False):
        # Ctrl+Z inside Composer is intentionally NOT Blender scene Undo.
        # It restores only DWOS controller settings / split trees and rebuilds
        # the generated window geometry from that logical state.
        try:
            result=history_redo(context) if redo else history_undo(context)
            if result.get('ok'):
                self._history_message=('REDO · ' if redo else 'UNDO · ')+str(result.get('label') or 'DWOS')
                log_event('COMPOSER_HISTORY_APPLIED',window=self._obj_name,redo=bool(redo),label=result.get('label',''),changed=len(result.get('changed') or []))
            else:
                self._history_message='ИСТОРИЯ DWOS ПУСТА'
            if context.area:context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception as exc:
            log_exception('COMPOSER_HISTORY_FAILED',exc,window=self._obj_name,redo=bool(redo))
            self.report({'WARNING'},str(exc))
            return {'RUNNING_MODAL'}

    def _commit_field(self, context):
        if not self._editing:
            return
        self._ensure_active(context)
        key, payload = self._editing; text = self._buffer
        self._history_checkpoint(context,'FIELD_'+str(key))
        accepted=False
        try:
            if key in {'HUD_OFFSET','HUD_STEP'}:
                value=parse_meter_text(text,allow_negative=(key=='HUD_OFFSET'))
                if key=='HUD_STEP' and value<=0:
                    raise ValueError('Шаг должен быть больше нуля')
                setattr(self.obj.dwos_window,
                        'mullion_offset_text' if key=='HUD_OFFSET' else 'mullion_step_text',
                        format_meter(value))
                result={'FINISHED'}
            elif key in {'TREE_POSITION','TREE_THICKNESS'}:
                result=bpy.ops.dwos.zone_edit('EXEC_DEFAULT',split_id=str(payload),target=('POSITION' if key=='TREE_POSITION' else 'THICKNESS'),value_text=text)
            elif key == 'SPLIT_POSITION':
                result=bpy.ops.dwos.composer_set_split_value('EXEC_DEFAULT', index=int(payload), target='POSITION', value_text=text)
            elif key == 'SPLIT_THICKNESS':
                result=bpy.ops.dwos.composer_set_split_value('EXEC_DEFAULT', index=int(payload), target='THICKNESS', value_text=text)
            elif key=='POST_WIDTH':
                value=parse_meter_text(text)
                if not (0.001<=value<=1.0):raise ValueError('Ширина центрального импоста: 0,001–1 м')
                settings=self.obj.dwos_window
                old=settings.corner_post_width_text
                settings.corner_post_width_text=format_meter(value)
                try:
                    rebuild_window(self.obj)
                except Exception:
                    settings.corner_post_width_text=old
                    raise
                log_event('CORNER_POST_WIDTH_CHANGED',window=self.obj.name,requested=value,
                    effective=float(self.obj.get('DWOS_corner_post_width_effective_m',value)))
                result={'FINISHED'}
            else:
                result=bpy.ops.dwos.composer_set_value('EXEC_DEFAULT', target=key, value_text=text)
            if 'FINISHED' not in result:
                self.report({'WARNING'},'Значение отклонено: Split должен оставлять место соседним зонам')
            else:
                accepted=True
        except (RuntimeError, ValueError) as exc:
            log_exception('COMPOSER_FIELD_REJECTED', exc, target=key, value=text)
            self.report({'WARNING'}, str(exc))
        finally:
            self._editing = None; self._buffer = ''; self._edit_fresh = False
            context.area.tag_redraw()
        return accepted

    def _field_focus_next(self, context):
        fields=[(key[6:],payload) for key,*_,payload in (self._hits or []) if key.startswith('FIELD:')]
        if not fields:
            return
        if self._editing in fields:
            nxt=fields[(fields.index(self._editing)+1)%len(fields)]
        else:
            nxt=fields[0]
        self._commit_field(context)
        self._begin_field(*nxt)
        context.area.tag_redraw()

    def _add_zone_split(self,context,axis):
        from .zone_tool import _tree
        s=self.obj.dwos_window
        w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
        fw=parse_meter_text(s.frame_width_text)
        _branch_state,_branch_leaves,_branch_bands=self._branch_layout_for_obj(self.obj)
        if _branch_leaves is not None:
            leaves=[(zid,rect) for zid,rect,_bid in _branch_leaves]
            zid=s.active_zone_id if s.active_zone_id and any(z[0]==s.active_zone_id for z in leaves) else None
            if not zid and isinstance(self._selected_zone,str) and any(z[0]==self._selected_zone for z in leaves):
                zid=self._selected_zone
            if not zid and isinstance(self._selected_zone,int) and 0<=self._selected_zone<len(self._zones):
                r=self._zones[self._selected_zone][0];cx=(r[0]+r[1])/2;cy=(r[2]+r[3])/2
                zid=next((id_ for id_,(x0,x1,y0,y1) in leaves if x0<=cx<=x1 and y0<=cy<=y1),None)
            if not zid and len(leaves)==1:zid=leaves[0][0]
            if zid is None:raise ValueError('Выберите сторону/зону CORNER окна для Split')
            if '|' in zid:s.active_branch_id=zid.split('|',1)[0]
        else:
            tree=_tree(s)
            leaves,_=zone_layout(tree,w,h,fw)
            zid=s.active_zone_id if s.zone_tree_json and s.active_zone_id else None
            if not zid and self._selected_zone is not None:
                if s.zone_tree_json and isinstance(self._selected_zone,str):
                    zid=self._selected_zone
                elif isinstance(self._selected_zone,int) and 0<=self._selected_zone<len(self._zones):
                    r=self._zones[self._selected_zone][0]
                    cx=(r[0]+r[1])/2;cy=(r[2]+r[3])/2
                    zid=next((id_ for id_,(x0,x1,y0,y1) in leaves if x0<=cx<=x1 and y0<=cy<=y1),None)
            if not zid:
                zid=leaves[0][0] if len(leaves)==1 else None
            if zid is None:raise ValueError('Выберите зону для локального Split')
        # Before a legacy Zone Tree has been migrated, on-screen hit IDs
        # are integer rectangles. Resolve EACH against the SAME staged tree.
        # Never silently omit a selected zone from a bulk operation.
        picked=[]
        for selected in (self._selected_zone_ids or []):
            if isinstance(selected,str):
                picked.append(selected)
            elif isinstance(selected,int) and 0<=selected<len(self._zones):
                r=self._zones[selected][0]
                cx=(r[0]+r[1])*.5;cy=(r[2]+r[3])*.5
                found=next((name for name,(x0,x1,y0,y1) in leaves
                            if x0-1e-7<=cx<=x1+1e-7 and y0-1e-7<=cy<=y1+1e-7),None)
                if found is None:raise ValueError('Выбранная область не найдена в дереве зон')
                picked.append(found)
            else:
                raise ValueError('Выбранная зона устарела; повторите выбор')
        if len(set(picked))!=len(picked):
            raise ValueError('Несколько экранных зон относятся к одному Split; уточните выбор')
        if len(picked)>1:
            # A single transactional rebuild; no N partial window mutations.
            result=bpy.ops.dwos.zone_add_many('EXEC_DEFAULT',zone_ids_json=json.dumps(picked),axis=axis)
            if 'FINISHED' not in result:
                reason=str(self.obj.get('DWOS_last_group_split_error','') or 'Неизвестная ошибка геометрии')
                self._history_message='ГРУППОВОЙ SPLIT: '+reason
                raise ValueError('Групповой Split отклонён: '+reason+'; исходная модель сохранена')
        else:
            result=bpy.ops.dwos.zone_add('EXEC_DEFAULT',zone_id=zid,axis=axis)
            if 'FINISHED' not in result:raise ValueError('Локальный Split отклонён')
        self._history_message=f'SPLIT · {len(picked) if picked else 1} зон'
        self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
        self._compact=True;self._page='FRAME'

    def _optimization_has_eligible_target(self, context):
        """Conservative RC33 UX preflight; underlying batch operator stays authoritative."""
        try:
            scope_targets=target_windows(context,self.obj)
        except (ValueError,AttributeError,TypeError):
            # Let the operator report an unusual or invalid batch scope.
            return True
        if not scope_targets:
            return False
        return any(str(o.get('DWOS_adaptive_classification','PLANAR')).upper() not in
                   {'CORNER','CURVED'} and not bool(o.dwos_window.finalized)
                   for o in scope_targets)

    def _action(self, context, key, payload):
        if key=='HUD_PIN':
            if not self._hud_pinned and self._panel_rect:
                # Pin always freezes the current screen location, including AUTO.
                self._hud_custom=True
                self._hud_custom_position=(self._panel_rect[0]/max(1,context.region.width),
                                           self._panel_rect[1]/max(1,context.region.height))
            self._hud_pinned=not self._hud_pinned
            self._save_hud_position(context)
            log_event('COMPOSER_HUD_PIN',pinned=self._hud_pinned)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key in {'W58_VIEW_HP','W58_VIEW_LP'}:
            self._ensure_active(context)
            result=bpy.ops.dwos.auto_view('EXEC_DEFAULT',mode=('LP' if key=='W58_VIEW_LP' else 'HP'),
                                           window_name=self._obj_name)
            if 'FINISHED' not in result:
                raise ValueError('DWOS: LOD-переключение отклонено')
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='HUD_RESET':
            self._hud_custom=False;self._hud_pinned=False;self._hud_drag=None
            self._save_hud_position(context)
            log_event('COMPOSER_HUD_RESET')
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        self._ensure_active(context)
        s = self.obj.dwos_window
        # OPT_FULL / OPT_OPT are history-managed by composer_set_optimization itself.
        # Avoid double Undo checkpoints when invoked through the Viewport.
        mutation_keys={'ALIGN_SEGMENTS','FLIP_SIDE','CORNER_AUTO','CORNER_ON','CORNER_OFF',
                       'INT_OFF','INT_BOX','ZONE_ADD_V','ZONE_ADD_H',
                       'TREE_DELETE','ADD_V','ADD_H','SPLIT_GLOBAL','SPLIT_DELETE',
                       'DELETE_SELECTED','MAT_DEFAULT'}
        # Validate OPTIMIZED before the operator's own Undo checkpoint.
        if key=='OPT_OPT' and not self._optimization_has_eligible_target(context):
            self._history_message='OPTIMIZED: в области нет совместимых PLANAR · CORNER/CURVED остаются FULL'
            log_event('COMPOSER_OPTIMIZATION_UNAVAILABLE', window=self._obj_name,
                      scope=str(getattr(context.scene.dwos,'batch_scope','ACTIVE')))
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key in mutation_keys:
            self._history_checkpoint(context,'ACTION_'+str(key))
        if key.startswith('STEP:'):
            from ....core.numeric_spinner import step_metre_text
            parts=key.split(':')
            if len(parts)!=3 or parts[1] not in {
                    'FRAME_WIDTH','INTERNAL_WIDTH','FRAME_DEPTH','FRAME_OFFSET',
                    'POST_WIDTH','INTERIOR_OFFSET','INTERIOR_DEPTH','INTERIOR_CLEARANCE',
                    'GLASS_OFFSET','GLASS_GAP','GLASS_EDGE'} or parts[2] not in {'minus','plus'}:
                raise ValueError('Недопустимое числовое управление')
            field,action=parts[1],parts[2]
            self._begin_field(field,payload)
            try:
                self._buffer=step_metre_text(self._buffer, 1 if action=='plus' else -1,
                    signed=field in {'FRAME_OFFSET','INTERIOR_OFFSET','GLASS_OFFSET'},
                    minimum=('0.001' if field in {
                        'FRAME_WIDTH','INTERNAL_WIDTH','FRAME_DEPTH','POST_WIDTH','INTERIOR_DEPTH'} else None))
            except ValueError:
                self._editing=None;self._buffer='';self._edit_fresh=False
                raise
            accepted=self._commit_field(context)  # original validated operator + local history
            log_event('COMPOSER_NUMERIC_STEP' if accepted else 'COMPOSER_NUMERIC_STEP_REJECTED',
                      window=self._obj_name,field=field,direction=action)
            return {'RUNNING_MODAL'}
        if key=='CONSTRUCTOR_TOGGLE':
            self._constructor_mode=not self._constructor_mode
            self._history_message='Схема: '+('включена' if self._constructor_mode else 'выключена')
            log_event('COMPOSER_CONSTRUCTOR_TOGGLE', window=self._obj_name, enabled=self._constructor_mode)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='HEALTH_TOGGLE':
            self._health_open=not self._health_open
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='INT_MANUAL_FIX':
            outcome=bpy.ops.dwos.manual_interior_repair('EXEC_DEFAULT')
            self._history_message=('Ручной Interior: выполнено' if 'FINISHED' in outcome else
                                   'Ручной Interior: отклонено; смотрите лог/панель')
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='INTERIOR_SCISSOR':
            sid=str(payload or '')
            if not sid:raise ValueError('Не удалось определить сквозной импост')
            # The Blender operator uses context.active_object, which can lag
            # behind the Composer target after selecting another window.
            self._ensure_active(context)
            if context.view_layer.objects.active != self.obj:
                self._history_message='Сначала активируйте окно DWOS'
                log_event('VIEWPORT_INTERIOR_SCISSOR_REJECTED',window=self._obj_name,split=sid,reason='ACTIVE_WINDOW_MISMATCH')
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            before_mode=str(self.obj.dwos_window.interior_partition_mode)
            before_ids=str(self.obj.dwos_window.interior_dividers_json)
            log_event('VIEWPORT_INTERIOR_SCISSOR_CLICK',window=self._obj_name,split=sid,mode=before_mode,ids=before_ids)
            result=bpy.ops.dwos.interior_cut_toggle('EXEC_DEFAULT',cut_id=sid)
            if 'FINISHED' not in result:
                self._history_message='Не удалось изменить границу Interior · подробности в диагностике'
                log_event('VIEWPORT_INTERIOR_SCISSOR_REJECTED',window=self._obj_name,split=sid)
            else:
                from .interior_authored_sections_core import read_ids
                current=set(read_ids(self.obj.dwos_window.interior_dividers_json))
                is_cut=sid in current
                self._history_message=('Граница создана' if is_cut else 'Граница снята')+' · '+sid
                log_event('VIEWPORT_INTERIOR_SCISSOR_APPLIED',window=self._obj_name,split=sid,cut_enabled=is_cut,
                          mode=str(self.obj.dwos_window.interior_partition_mode),ids=str(self.obj.dwos_window.interior_dividers_json))
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='INT_TOGGLE_DIVIDER':
            from .interior_authored_sections_core import toggled_cut_ids
            from .window_builder import _splits_from_settings
            from .interior_sections_core import full_height_bands
            from .zone_core import decode as zone_decode,layout as zone_layout
            if str(self.obj.get('DWOS_adaptive_classification',''))=='CORNER':
                raise ValueError('CORNER: разделение по V отключено')
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            z=zone_decode(s.zone_tree_json) if s.zone_tree_json else None
            bands=zone_layout(z,w,h,fw)[1] if z else []
            eligible=full_height_bands(_splits_from_settings(s,w,h),bands,w,h,fw)
            valid={x[0] for x in eligible}
            ids=self._current_ids(self.obj)
            if not ids or not all(str(x) in valid for x in ids):
                self._history_message='Сначала выбери сквозной V или нажми ножницы возле него'
                log_event('INTERIOR_DIVIDER_NO_VALID_SELECTION',window=self._obj_name,
                          selected=[str(x) for x in ids],eligible_count=len(valid))
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            old_json=s.interior_dividers_json;old_mode=s.interior_partition_mode
            from .interior_authored_sections_core import effective_cut_ids
            rect=(-w/2+fw,w/2-fw,-h/2+fw,h/2-fw)
            before=effective_cut_ids(eligible,rect,old_mode,int(s.interior_groups_count),old_json)
            current=set(before)
            for sid in ids:
                sid=str(sid)
                if sid in current:current.remove(sid)
                else:current.add(sid)
            from . import dwos_history
            dwos_history.push(context,'INTERIOR_DIVIDER_SELECTION')
            s.interior_partition_mode='CUSTOM'
            s.interior_dividers_json=json.dumps(sorted(current))
            try:rebuild_window(self.obj)
            except Exception:
                s.interior_partition_mode=old_mode;s.interior_dividers_json=old_json
                raise
            self._history_message=f'Границ Interior: {len(current)} · связных оболочек: {len(current)+1}'
            self._compact=False;self._page='INTERIOR'
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key=='INT_GROUPS':
            if self.obj.get('DWOS_adaptive_classification')=='CORNER':
                raise ValueError('Для CORNER секции Interior не переключаются')
            from .window_builder import _splits_from_settings
            from .interior_sections_core import full_height_bands
            from .zone_core import decode as zone_decode,layout as zone_layout
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            z=zone_decode(s.zone_tree_json) if s.zone_tree_json else None
            bands=zone_layout(z,w,h,fw)[1] if z else []
            eligible=full_height_bands(_splits_from_settings(s,w,h),bands,w,h,fw)
            max_count=len(eligible)+1
            old=int(s.interior_groups_count)
            old_mode=str(s.interior_partition_mode)
            new=(old+1 if old_mode=='COUNT' and old<max_count else 1)
            from . import dwos_history
            dwos_history.push(context,'INTERIOR_GROUPS')
            s.interior_groups_count=new
            s.interior_partition_mode='COUNT'
            try:rebuild_window(self.obj)
            except Exception:
                s.interior_groups_count=old
                s.interior_partition_mode=old_mode
                raise
            self._history_message=f'Физических Interior: {new}'
            self._compact=False;self._page='INTERIOR'
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='HEALTH_RETRY':
            result=bpy.ops.dwos.rebuild_smart_interiors('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                self._history_message='Interior: повторный расчёт отклонён'
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='HEALTH_LOCATE':
            bpy.ops.dwos.health_locate('EXEC_DEFAULT')
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='MULLION_EDIT':
            ids=self._current_ids(self.obj)
            if len(ids)!=1:
                return {'RUNNING_MODAL'}
            sid=ids[0]
            for index,sp in enumerate(s.splits):
                if sp.split_id==sid:
                    sid=index
                    s.active_split_index=index
                    break
            if isinstance(sid,str):s.active_zone_split_id=sid
            s.mullion_selection_json='[]'
            self._split_popup_id=sid
            self._split_popup_anchor=getattr(self,'_last_mullion_click',None)
            self._compact=True
            self._selected_zone=None
            self._zone_menu_anchor=None
            self._tree_drag_id='';self._tree_drag_value=None;self._drag_idx=-1;self._drag_value=None
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='ALIGN_SEGMENTS':
            ids=self._current_ids(self.obj)
            w=parse_meter_text(s.width_text);h=parse_meter_text(s.height_text)
            fw=parse_meter_text(s.frame_width_text)
            _bs,_bl,_bb=self._branch_layout_for_obj(self.obj)
            if _bb is not None:
                rows=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4],'branch_id':r[6]} for r in _bb]
            else:
                rows=entries(s.zone_tree_json,w,h,fw) if s.zone_tree_json else []
            if not compact_align_axis(rows,ids):
                raise ValueError('Выберите два независимых импоста одного направления')
            result=bpy.ops.dwos.mullion_group_edit('EXEC_DEFAULT',action='ALIGN')
            if 'FINISHED' not in result:
                raise ValueError('Выравнивание отклонено; геометрия не изменена')
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if key=='ASSEMBLE_MESH':
            # Close this modal before Blender's confirm dialog takes control.
            # `execute()` stages/validates ALL targets, then publishes atomically.
            self._finish(context)
            bpy.ops.dwos.assemble_window_mesh('INVOKE_DEFAULT')
            return {'FINISHED'}
        if key=='FLIP_SIDE':
            s.flip_interior=not bool(s.flip_interior)
            rebuild_window(self.obj)
            log_event('WINDOW_SIDE_FLIPPED',window=self.obj.name,flip_interior=bool(s.flip_interior))
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key=='DEBUG_ORIENTATION':
            self._show_orientation_debug=not bool(getattr(self,'_show_orientation_debug',False))
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key in {'CORNER_AUTO','CORNER_ON','CORNER_OFF'}:
            topo=self._adaptive_topology(self.obj)
            if not topo or topo.get('classification')!='CORNER':
                raise ValueError('Режим стыка доступен только для углового окна')
            previous=str(s.corner_post_mode)
            target={'CORNER_AUTO':'AUTO','CORNER_ON':'ON','CORNER_OFF':'OFF'}[key]
            require_post_allowed(topo,target)
            if target==previous:return {'RUNNING_MODAL'}
            from .corner_mode_core import plan_corner_mode_change
            old_branch=str(s.branch_state_json or '')
            staged_branch=plan_corner_mode_change(
                topo,old_branch,previous,target,height=parse_meter_text(s.height_text),
                frame_width=parse_meter_text(s.frame_width_text),
                internal_width=parse_meter_text(s.internal_width_text),
                frame_depth=parse_meter_text(s.frame_depth_text),
                legacy_zone_tree=s.zone_tree_json,legacy_splits=list(s.splits),
                old_published_post_count=int(self.obj.get('DWOS_corner_post_count',0)))
            s.corner_post_mode=target
            s.branch_state_json=staged_branch
            try:
                rebuild_window(self.obj)
            except Exception:
                s.corner_post_mode=previous
                s.branch_state_json=old_branch
                # No new frame/glass is published after a rejected staging pass.
                raise
            log_event('CORNER_JUNCTION_MODE_CHANGED',window=self.obj.name,before=previous,after=target)
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key == 'REF_USER':
            result=bpy.ops.dwos.show_user_frame('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                raise ValueError('Не удалось записать «Мою раму»')
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key == 'REF_DWOS':
            result=bpy.ops.dwos.show_generated_frame('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                raise ValueError('Не удалось записать «Твою раму»')
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key == 'CLOSE':
            return self._finish(context)
        if key in {'SPLIT_CLOSE','SPLIT_DONE'}:
            self._split_popup_id=None
            self._editing=None
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key == 'ZONE_CLOSE':
            self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if key.startswith('FIELD:'):
            self._begin_field(key.split(':', 1)[1], payload); context.area.tag_redraw(); return {'RUNNING_MODAL'}
        if key.startswith('SCOPE_'):
            bpy.ops.dwos.batch_set_scope('EXEC_DEFAULT', scope=key.split('_', 1)[1])
            self._history_message=''
        elif key == 'PAGE_FRAME':
            self._page='FRAME';self._active_handle=None
        elif key == 'PAGE_INTERIOR':
            self._page='INTERIOR';self._active_handle=None
        elif key == 'PAGE_GLASS':
            self._page='GLASS';self._active_handle=None
        elif key == 'EXPAND':
            self._compact=False;self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
        elif key == 'COLLAPSE':
            self._compact=True;self._active_handle=None
        elif key == 'MORE_FRAME':
            self._frame_more=not self._frame_more
        elif key.startswith('HANDLE_'):
            name=key.removeprefix('HANDLE_')
            self._active_handle=None if self._active_handle==name else name
        elif key == 'OPT_FULL':
            bpy.ops.dwos.composer_set_optimization('EXEC_DEFAULT', mode='FULL')
        elif key == 'OPT_OPT':
            # RC33: the selected object is self.obj; no undefined local `obj`.
            outcome=bpy.ops.dwos.composer_set_optimization('EXEC_DEFAULT', mode='OPTIMIZED')
            if 'CANCELLED' in outcome:
                self._history_message='Нет совместимых PLANAR в области · CORNER/CURVED всегда FULL'
        elif key in {'INT_OFF','INT_BOX'}:
            mode={'INT_OFF':'OFF','INT_BOX':'BOX'}[key]
            bpy.ops.dwos.composer_set_interior_mode('EXEC_DEFAULT',mode=mode)
        elif key == 'ZONE_ADD_V':
            self._add_zone_split(context,'V')
        elif key == 'ZONE_ADD_H':
            self._add_zone_split(context,'H')
        elif key == 'TREE_DELETE' and s.active_zone_split_id:
            bpy.ops.dwos.zone_delete('EXEC_DEFAULT',split_id=s.active_zone_split_id)
        elif key == 'ADD_V':
            self._add_zone_split(context,'V')
        elif key == 'ADD_H':
            self._add_zone_split(context,'H')
        elif key == 'SPLIT_GLOBAL':
            result=bpy.ops.dwos.reset_split_thickness('EXEC_DEFAULT', index=s.active_split_index)
            if 'FINISHED' not in result:
                raise ValueError('Не удалось вернуть общую толщину для выбранного импоста')
        elif key == 'SPLIT_DELETE' and 0 <= s.active_split_index < len(s.splits):
            bpy.ops.dwos.remove_split('EXEC_DEFAULT', index=s.active_split_index)
        elif key == 'DELETE_SELECTED':
            bpy.ops.dwos.delete_selected_mullions('EXEC_DEFAULT')
            self._split_popup_id=None;self._tree_drag_id='';self._drag_idx=-1
        elif key == 'APPLY_SPLITS':
            # Same legacy semantics: COPY only the mullion/Split layout.
            # The underlying operator already handles DWOS History + rollback.
            count=len(target_windows(context,self.obj))
            if count<=1:
                self._history_message='COPY: выберите SEL, SAME или ALL (сейчас 1 окно)'
            else:
                result=bpy.ops.dwos.batch_copy_splits('EXEC_DEFAULT')
                if 'FINISHED' not in result:
                    raise ValueError('COPY отклонён: исходная раскладка не изменена')
                outcome=last_copy_result()
                self._history_message=(f"COPY: применено {outcome['applied']} · пропущено {outcome['skipped']}")
        elif key == 'MAT_CHOOSE':
            bpy.ops.dwos.composer_choose_material('INVOKE_DEFAULT')
        elif key == 'MAT_DEFAULT':
            bpy.ops.dwos.default_frame_material('EXEC_DEFAULT')
        context.area.tag_redraw(); return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            result=self._modal_impl(context, event)
            self._modal_error_streak=0
            return result
        except Exception as exc:
            self._modal_error_streak=getattr(self,'_modal_error_streak',0)+1
            self._interior_drag=None
            self._interior_ghost=None
            self._depth_drag=False
            self._ghost_depth=None
            self._drag_idx=-1
            self._drag_value=None
            key=(type(exc).__name__,str(exc))
            if key!=getattr(self,'_last_modal_error',None):
                log_exception('VIEWPORT_MODAL_GUARD',exc,window=self._obj_name)
                self.report({'WARNING'},f'Composer: {str(exc)[:120]}')
                self._last_modal_error=key
            if getattr(self,'_modal_error_streak',0)>=3:
                # A broken operator must not hold an invisible modal handler.
                log_event('COMPOSER_MODAL_FAIL_CLOSED',window=self._obj_name,count=self._modal_error_streak)
                return self._finish(context)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

    def _modal_impl(self, context, event):
        if getattr(self, '_surface_mode', False):
            from . import surface_composer_bridge as _surface_bridge
            return _surface_bridge.modal(self, context, event)
        obj = self.obj
        if getattr(self,'_disabled',False):
            return self._finish(context)
        if (context.area is None or context.area.type!='VIEW_3D' or
                context.area.as_pointer()!=getattr(self,'_area_pointer',None)):
            return self._finish(context)
        if obj is None or obj.dwos_window.finalized:
            return self._finish(context)
        # Pass09: hide action must invalidate even cached Composer hitboxes
        # before any subsequent modal event, not merely on the next GPU draw.
        if is_hidden(scope_from_context(context), 'DWOS_COMPOSER'):
            self._panel_rect=None
            self._layout_container=None
            self._hits=[]
            if self._editing:
                self._editing=None
                self._buffer=''
                self._edit_fresh=False
        # A drag owns only its mouse gesture, never geometry or field history.
        if self._hud_drag is not None:
            if event.type=='ESC' and event.value=='PRESS':
                self._hud_custom,self._hud_custom_position=self._hud_drag[4],self._hud_drag[5]
                self._hud_drag=None;context.area.tag_redraw();return {'RUNNING_MODAL'}
            if event.type=='MOUSEMOVE':
                pos=self._hud_drag
                width,height=context.region.width,context.region.height
                px=pos[2]+event.mouse_region_x-pos[0]
                py=pos[3]+event.mouse_region_y-pos[1]
                from ....core.hud_floating_contract import clamp_to_viewport
                rect=self._panel_rect
                if rect:
                    at=clamp_to_viewport((width,height),(rect[2],rect[3]),(px,py),12)
                    if at is not None:
                        self._hud_custom=True
                        self._hud_custom_position=(at[0]/width,at[1]/height)
                        context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value=='RELEASE':
                self._hud_drag=None
                if self._hud_custom:self._save_hud_position(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        # The focused editor owns the first Esc. A second Esc exits Composer.
        # The prior unconditional handler terminated the entire tool and could
        # discard the user's context while cancelling a single number.
        if event.type == 'ESC' and event.value == 'PRESS' and self._editing:
            self._editing=None
            self._buffer=''
            self._edit_fresh=False
            self._select_all=False
            self._caret_pos=0
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'ESC' and event.value == 'PRESS':
            self._editing=None
            self._buffer=''
            self._edit_fresh=False
            self._interior_drag=None
            self._interior_ghost=None
            self._depth_drag=False
            self._ghost_depth=None
            self._drag_idx=-1
            self._drag_value=None
            self._tree_drag_id=''
            self._tree_drag_value=None
            self._box_armed=False
            self._box_start=None
            return self._finish(context)

        # DWOS-local history: never pass Ctrl+Z from an active Composer to
        # Blender's global scene Undo. Ctrl+Shift+Z / Ctrl+Y redo DWOS state.
        if event.value=='PRESS' and event.ctrl and event.type in {'Z','Y'}:
            redo=bool(event.type=='Y' or (event.type=='Z' and event.shift))
            if self._editing:
                self._editing=None;self._buffer='';self._edit_fresh=False
            return self._history_apply(context,redo=redo)

        if self._editing:
            if event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
                key,payload=self._find_hit(event.mouse_region_x,event.mouse_region_y)
                if key and key.startswith('FIELD:'):
                    selected=(key.split(':',1)[1],payload)
                    if selected != self._editing:
                        self._begin_field(*selected)
                    self._caret_pos=len(self._buffer)
                    self._select_all=True
                    self._edit_fresh=True
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            # Modal focus contract: a single outside click commits and gives
            # Blender the SAME mouse event. Never trap the user in a field.
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                x,y=event.mouse_region_x,event.mouse_region_y
                key,payload=self._find_hit(x,y)
                current=self._editing
                if key and key.startswith('FIELD:') and (key.split(':',1)[1],payload)==current:
                    for hit_key,x0,_y0,_x1,_y1,hit_payload in reversed(self._hits or []):
                        if hit_key==key and hit_payload==payload:
                            font_size=max(8,int(getattr(self,'_editing_render_font_px',round(11*getattr(self,'_hud_font_scale',1.0)))))
                            blf.size(0,font_size)
                            panel_scale=getattr(self,'_hud_pixel_scale',1.0)
                            popup=self._floating_rect
                            if popup and popup[0]<=x<=popup[0]+popup[2] and popup[1]<=y<=popup[1]+popup[3]:
                                panel_scale=getattr(self,'_floating_scale',1.0)
                            input_padding=8*panel_scale
                            self._caret_pos=caret_from_x(self._buffer,x,x0+input_padding,
                                lambda prefix:blf.dimensions(0,prefix)[0])
                            self._select_all=False;self._edit_fresh=False
                            context.area.tag_redraw()
                            break
                    return {'RUNNING_MODAL'}
                self._commit_field(context)
                if key and key.startswith('FIELD:'):
                    other=(key.split(':',1)[1],payload)
                    if other!=current:self._begin_field(*other)
                    return {'RUNNING_MODAL'}
                if key:
                    try:return self._action(context,key,payload)
                    except (RuntimeError,ValueError) as exc:
                        log_exception('COMPOSER_ACTION_REJECTED',exc,action=key,window=self._obj_name)
                        self.report({'WARNING'},str(exc))
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                picked=self._pick_window(context,x,y)
                if picked is not None:
                    self._switch_window(context,picked);return {'RUNNING_MODAL'}
                return {'RUNNING_MODAL'} if self._inside_ui(x,y) else {'PASS_THROUGH'}
            if event.value == 'PRESS':
                if event.type in {'RET', 'NUMPAD_ENTER'}:
                    self._commit_field(context); return {'RUNNING_MODAL'}
                if event.type == 'TAB':
                    self._field_focus_next(context);return {'RUNNING_MODAL'}
                if event.type == 'ESC':
                    self._editing = None; self._buffer = ''; self._edit_fresh = False
                    context.area.tag_redraw(); return {'RUNNING_MODAL'}
                if event.ctrl and event.type in {'A','C','X','V'}:
                    if event.type=='A':
                        self._buffer,self._caret_pos,self._select_all=numeric_edit(
                            self._buffer,self._caret_pos,'SELECT_ALL')
                        self._edit_fresh=True
                    elif event.type in {'C','X'}:
                        context.window_manager.clipboard=self._buffer if self._select_all else self._buffer
                        if event.type=='X' and self._select_all:
                            self._buffer,self._caret_pos,self._select_all=numeric_edit(
                                self._buffer,self._caret_pos,'DELETE',selected=True)
                            self._edit_fresh=False
                    else:
                        inserted=str(context.window_manager.clipboard or '').strip()
                        if inserted and all(ch in '0123456789.,-+' for ch in inserted):
                            self._buffer,self._caret_pos,self._select_all=numeric_edit(
                                self._buffer,self._caret_pos,'INSERT',insert=inserted,
                                selected=(self._select_all or self._edit_fresh))
                            self._edit_fresh=False
                    context.area.tag_redraw();return {'RUNNING_MODAL'}
                if event.type in {'HOME','END','LEFT_ARROW','RIGHT_ARROW','BACK_SPACE','DEL'}:
                    command={'LEFT_ARROW':'LEFT','RIGHT_ARROW':'RIGHT',
                             'BACK_SPACE':'BACKSPACE','DEL':'DELETE'}.get(event.type,event.type)
                    self._buffer,self._caret_pos,self._select_all=numeric_edit(
                        self._buffer,self._caret_pos,command,
                        selected=(self._select_all or self._edit_fresh) if command in {'BACKSPACE','DELETE'} else False)
                    self._edit_fresh=False
                    context.area.tag_redraw();return {'RUNNING_MODAL'}
                if event.type == 'RIGHTMOUSE':
                    self._editing=None;self._buffer='';self._edit_fresh=False
                    context.area.tag_redraw();return {'PASS_THROUGH'}
                ch=event.unicode
                if ch and ch in '0123456789.,-+':
                    self._buffer,self._caret_pos,self._select_all=numeric_edit(
                        self._buffer,self._caret_pos,'INSERT',insert=ch,
                        selected=(self._select_all or self._edit_fresh))
                    self._edit_fresh=False;context.area.tag_redraw();return {'RUNNING_MODAL'}
                # Other Viewport navigation is never swallowed by a text input.
                if event.type in {'MIDDLEMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE','NUMPAD_1','NUMPAD_3','NUMPAD_7'}:
                    return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}

        # Marquee mode is explicit (B) and never leaks an LMB drag to Blender.
        if event.type == 'ESC' and event.value == 'PRESS' and (self._box_armed or self._box_start is not None):
            self._box_armed=False;self._box_start=None;self._box_end=None
            context.area.tag_redraw();return {'RUNNING_MODAL'}
        if self._box_armed:
            if event.type=='LEFTMOUSE' and event.value=='PRESS':
                x,y=event.mouse_region_x,event.mouse_region_y
                if self._inside_ui(x,y):
                    self._box_armed=False;return {'RUNNING_MODAL'}
                self._box_start=(x,y);self._box_end=(x,y)
                return {'RUNNING_MODAL'}
            if event.type=='MOUSEMOVE' and self._box_start is not None:
                self._box_end=(event.mouse_region_x,event.mouse_region_y)
                context.area.tag_redraw();return {'RUNNING_MODAL'}
            if event.type=='LEFTMOUSE' and event.value=='RELEASE' and self._box_start is not None:
                a=self._box_start;b=(event.mouse_region_x,event.mouse_region_y)
                self._box_start=None;self._box_end=None;self._box_armed=False
                obj=self.obj;s=obj.dwos_window
                lines=[(sid if isinstance(sid,str) else s.splits[sid].split_id,axis,p,q)
                       for sid,axis,p,q in (self._split_lines or [])]
                picked=rectangle_selected_segments(lines,a,b,containment=(b[0]>=a[0]))
                old=self._current_ids(obj) if event.shift else []
                s.mullion_selection_json=encode_ids(marquee_compatible(
                    old,picked,self._mullion_axes(obj)))
                context.area.tag_redraw();return {'RUNNING_MODAL'}
            if event.type in {'MIDDLEMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE'}:
                return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}
        if event.type=='B' and event.value=='PRESS' and not event.ctrl and not event.alt:
            self._box_armed=True
            context.area.tag_redraw();return {'RUNNING_MODAL'}

        if event.type in {'DEL','X'} and event.value=='PRESS' and not event.ctrl and not event.alt:
            if self._current_ids(obj):
                self._ensure_active(context)
                self._history_checkpoint(context,'DELETE_MULLION')
                try:
                    bpy.ops.dwos.delete_selected_mullions('EXEC_DEFAULT')
                    self._split_popup_id=None;self._tree_drag_id='';self._drag_idx=-1
                except Exception as exc:
                    log_exception('DELETE_MULLION_REJECTED',exc,window=obj.name)
                    self.report({'WARNING'},str(exc))
                context.area.tag_redraw();return {'RUNNING_MODAL'}

        if event.type in {'MIDDLEMOUSE', 'NUMPAD_1', 'NUMPAD_3', 'NUMPAD_7'}:
            return {'PASS_THROUGH'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            pos=(event.mouse_region_x,event.mouse_region_y)
            return {'RUNNING_MODAL'} if self._inside_ui(*pos) else {'PASS_THROUGH'}

        x = event.mouse_region_x; y = event.mouse_region_y

        if event.type=='LEFTMOUSE' and event.value=='DOUBLE_CLICK':
            key,payload=self._find_hit(x,y)
            if key and key.startswith('FIELD:'):
                self._begin_field(key.split(':',1)[1],payload)
                self._caret_pos=len(self._buffer)
                self._select_all=True
                self._edit_fresh=True
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if not self._inside_ui(x,y):
                sid=self._split_hit(x,y)
                if sid not in (-1,None,''):
                    obj.dwos_window.mullion_selection_json='[]'
                    self._split_popup_id=sid
                    if isinstance(sid,str):obj.dwos_window.active_zone_split_id=sid
                    else:obj.dwos_window.active_split_index=sid
                    self._selected_zone=None;self._compact=True
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE' and self._interior_drag:
            name,initial,mouse0,ux,uy,metres_per_pixel=self._interior_drag
            pixels=(x-mouse0[0])*ux+(y-mouse0[1])*uy
            raw=initial+pixels*metres_per_pixel
            if name=='INTERIOR_DEPTH':
                raw=max(0.001,min(MAX_INTERIOR_DEPTH_M,raw))
            elif name=='GLASS_EDGE':
                ss=obj.dwos_window
                w=parse_meter_text(ss.width_text);h=parse_meter_text(ss.height_text)
                fw=parse_meter_text(ss.frame_width_text)
                raw=max(-fw*.375+1e-6,min(min(w,h)*.5-fw*.5-.001,raw))
            elif name=='GLASS_OFFSET':
                ss=obj.dwos_window
                d=parse_meter_text(ss.frame_depth_text)
                gap=parse_meter_text(ss.glass_gap_text) if ss.optimization=='FULL' else 0.0
                allowed=max(0.0,d*.5-.001-gap*.5)
                raw=max(-allowed,min(allowed,raw))
            elif name=='INTERIOR_CLEARANCE':
                settings=obj.dwos_window
                w=parse_meter_text(settings.width_text)
                h=parse_meter_text(settings.height_text)
                fw=parse_meter_text(settings.frame_width_text)
                raw=max(0.0,min(max(0.0,(min(w,h)-2*fw)*0.5-0.002),raw))
            else:
                d=parse_meter_text(obj.dwos_window.frame_depth_text)
                raw=max(-d*.25,min(d*.75,raw))
            self._interior_ghost=raw
            context.area.tag_redraw();return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._depth_drag:
            ux, uy = self._depth_axis_screen
            dx = x - self._depth_start_mouse[0]; dy = y - self._depth_start_mouse[1]
            delta_px = dx * ux + dy * uy
            self._ghost_depth = max(0.005, self._depth_start_value + delta_px * self._depth_mpp)
            context.area.tag_redraw(); return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._tree_drag_id:
            hit=self._local_hit(context,x,y)
            if hit is not None:
                entry=self._split_row(obj,self._tree_drag_id)
                if entry:
                    axis=entry['axis'];thickness=entry['thickness'];rect=entry['rect']
                    proposed=hit.x if axis=='V' else hit.y
                    proposed=self._constrain_split_center(obj,axis,proposed,thickness,rect,
                                                         self._tree_drag_value if self._tree_drag_value is not None else self._group_drag_start)
                    raw_proposed=proposed
                    candidate,snap_label=self._snap_position(obj,axis,rect,proposed,self._tree_drag_id)
                    candidate=self._constrain_split_center(obj,axis,candidate,thickness,rect,proposed)
                    if self._preview_group_allowed(obj,self._tree_drag_id,candidate,self._group_drag_start):
                        self._tree_drag_value=candidate
                        self._snap_feedback=(snap_label if abs(candidate-raw_proposed)>1e-9 else '')
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._drag_idx >= 0:
            hit = self._local_hit(context, x, y)
            if hit is not None:
                s = obj.dwos_window; sp = s.splits[self._drag_idx]
                w = parse_meter_text(s.width_text); h = parse_meter_text(s.height_text)
                raw = (hit.x + w * 0.5) if sp.axis == 'V' else (hit.y + h * 0.5)
                limit = w if sp.axis == 'V' else h
                proposed=max(0.0,min(limit,raw))
                fw=parse_meter_text(s.frame_width_text)
                thickness=parse_meter_text(sp.thickness_text if sp.override_thickness else s.internal_width_text)
                centered=proposed-(w*.5 if sp.axis=='V' else h*.5)
                prev=(self._drag_value if self._drag_value is not None else self._group_drag_start)
                prev_centered=(prev-(w*.5 if sp.axis=='V' else h*.5)) if prev is not None else centered
                rect=(-w*.5+fw,w*.5-fw,-h*.5+fw,h*.5-fw)
                centered=self._constrain_split_center(obj,sp.axis,centered,thickness,rect,prev_centered)
                raw_centered=centered
                snapped,snap_label=self._snap_position(obj,sp.axis,rect,centered,sp.split_id)
                centered=self._constrain_split_center(obj,sp.axis,snapped,thickness,rect,centered)
                proposed=centered+(w*.5 if sp.axis=='V' else h*.5)
                if self._preview_group_allowed(obj,sp.split_id,proposed,self._group_drag_start):
                    self._drag_value=proposed
                    self._snap_feedback=(snap_label if abs(centered-raw_centered)>1e-9 else '')
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and not (self._interior_drag or self._depth_drag or self._tree_drag_id or self._drag_idx>=0):
            hover_key,_hover_payload=self._find_hit(x,y)
            hover_key=hover_key or ''
            self._hover_scissor_id=str(_hover_payload) if hover_key=='INTERIOR_SCISSOR' else ''
            ui_changed=(hover_key!=getattr(self,'_hover_ui_key',''))
            self._hover_ui_key=hover_key
            in_ui=self._inside_ui(x,y)
            new_zone=-1 if in_ui else self._zone_hit(x,y)
            new_split=(-1 if in_ui else self._split_hit(x,y))
            if hover_key=='INTERIOR_SCISSOR':
                # Preserve actual legacy integer indices and Zone/Branch IDs.
                sid=str(_hover_payload)
                new_split=next((i for i,sp in enumerate(obj.dwos_window.splits)
                                if str(sp.split_id)==sid),sid)
                new_zone=-1
            new_handle=(self._interior_handle_hit(x,y) if self._active_handle in
                        {'INTERIOR_DEPTH','INTERIOR_OFFSET','INTERIOR_CLEARANCE','GLASS_EDGE','GLASS_OFFSET'}
                        and not in_ui else None)
            picked=None if in_ui or new_zone>=0 or new_split not in (-1,None,'') else self._pick_window(context,x,y)
            new_window=picked.name if picked is not None else ''
            if ui_changed or (new_zone,new_split,new_handle,new_window)!=(self._hover_zone,self._hover_split,self._hover_handle,getattr(self,'_hover_window_name','')):
                self._hover_zone,self._hover_split,self._hover_handle=new_zone,new_split,new_handle
                self._hover_window_name=new_window
                context.area.tag_redraw()
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # W58.29 optional in-viewport corner marker mini HUD.
            if getattr(self,'_corner_marker_popup',None) is not None:
                px,py=self._corner_marker_popup
                if py+25<=y<=py+50 and px-120<=x<=px-7:
                    self._corner_marker_popup=None
                    return self._action(context,'CORNER_ON',None)
                if py+25<=y<=py+50 and px+7<=x<=px+120:
                    self._corner_marker_popup=None
                    return self._action(context,'CORNER_OFF',None)
                self._corner_marker_popup=None
            if not self._inside_ui(x,y):
                for mx,my in getattr(self,'_corner_marker_hitboxes',()):
                    if (x-mx)**2+(y-my)**2<=13**2:
                        self._corner_marker_popup=(x,y)
                        self._corner_post_selected=True
                        context.area.tag_redraw()
                        return {'RUNNING_MODAL'}
            key, payload = self._find_hit(x, y)
            if key=='HUD_DRAG':
                if not self._hud_pinned and self._panel_rect:
                    self._hud_drag=(x,y,self._panel_rect[0],self._panel_rect[1],
                                    self._hud_custom,self._hud_custom_position)
                else:
                    self._history_message='HUD закреплён · нажмите P для открепления'
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if key:
                try:
                    return self._action(context, key, payload)
                except (RuntimeError, ValueError) as exc:
                    log_exception('COMPOSER_ACTION_REJECTED', exc, action=key, window=obj.name)
                    self.report({'WARNING'}, str(exc))
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            key=(self._interior_handle_hit(x,y) if self._page in {'INTERIOR','GLASS'} and
                 self._active_handle in {'INTERIOR_DEPTH','INTERIOR_OFFSET','INTERIOR_CLEARANCE','GLASS_EDGE','GLASS_OFFSET'} else None)
            if key and key==self._active_handle:
                # Preserve the current page for independent glass handles.
                hh=self._interior_handles
                if key in ('GLASS_EDGE','GLASS_OFFSET'):
                    anchor=hh[key+'_reference'];start=hh[key]
                else:
                    anchor=hh['clearance_reference'] if key=='INTERIOR_CLEARANCE' else hh['reference']
                    start=hh['INTERIOR_CLEARANCE'] if key=='INTERIOR_CLEARANCE' else hh['start']
                dx=anchor[0]-start[0];dy=anchor[1]-start[1]
                size=math.hypot(dx,dy)
                if size>3:
                    s=obj.dwos_window
                    prop={'INTERIOR_DEPTH':'interior_depth_text','INTERIOR_OFFSET':'interior_offset_text',
                          'INTERIOR_CLEARANCE':'interior_clearance_text',
                          'GLASS_EDGE':'glass_edge_offset_text',
                          'GLASS_OFFSET':'glass_offset_text'}[key]
                    value=parse_meter_text(getattr(s,prop),allow_negative=(key in {'INTERIOR_OFFSET','GLASS_EDGE','GLASS_OFFSET'}))
                    self._interior_drag=(key,value,(x,y),dx/size,dy/size,0.1/size)
                    self._interior_ghost=value
                    return {'RUNNING_MODAL'}
            if self._page=='FRAME' and self._active_handle=='FRAME_DEPTH' and self._depth_hit(x, y):
                a, b, depth = self._depth_handle
                vx = b[0] - a[0]; vy = b[1] - a[1]; length = max(1.0, math.hypot(vx, vy))
                self._depth_drag = True
                self._depth_start_mouse = (x, y)
                self._depth_start_value = parse_meter_text(obj.dwos_window.frame_depth_text)
                self._depth_axis_screen = (vx / length, vy / length)
                self._depth_mpp = self._depth_start_value / length
                self._ghost_depth = self._depth_start_value
                return {'RUNNING_MODAL'}
            idx = self._split_hit(x, y)
            if isinstance(idx,str) and idx:
                ids=self._current_ids(obj)
                if event.shift:
                    self._last_mullion_click=(x,y)
                    obj.dwos_window.mullion_selection_json=encode_ids(
                        select_compatible(ids,idx,self._mullion_axes(obj),extend=True))
                    self._start_mullion_focus_pulse()
                    context.area.tag_redraw();return {'RUNNING_MODAL'}
                self._select_mullion_click(obj.dwos_window,idx)
                self._group_drag_start=None
                self._split_popup_id=None
                obj.dwos_window.active_zone_split_id=idx
                match=self._split_row(obj,idx)
                if match:
                    if match.get('branch_id'):obj.dwos_window.active_branch_id=match['branch_id']
                    self._tree_drag_id=idx
                    self._tree_drag_value=match['position']
                    self._group_drag_start=match['position']
                self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
                self._last_mullion_click=(x,y)
                self._compact=True
                context.area.tag_redraw();return {'RUNNING_MODAL'}
            if isinstance(idx,int) and idx >= 0:
                ident=obj.dwos_window.splits[idx].split_id
                ids=self._current_ids(obj)
                if event.shift:
                    self._last_mullion_click=(x,y)
                    obj.dwos_window.mullion_selection_json=encode_ids(
                        select_compatible(ids,ident,self._mullion_axes(obj),extend=True))
                    self._start_mullion_focus_pulse()
                    context.area.tag_redraw();return {'RUNNING_MODAL'}
                self._select_mullion_click(obj.dwos_window,ident)
                self._group_drag_start=None
                self._split_popup_id=None
                obj.dwos_window.active_split_index = idx
                self._compact=True;self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
                self._last_mullion_click=(x,y)
                self._drag_idx = idx
                self._drag_value = parse_meter_text(obj.dwos_window.splits[idx].position_text)
                self._group_drag_start=self._drag_value
                context.area.tag_redraw(); return {'RUNNING_MODAL'}
            zone=self._zone_hit(x,y)
            if zone>=0:
                ident=self._zone_ids[zone] if self._zone_ids[zone] else zone
                self._selected_zone_ids=toggle_zone(self._selected_zone_ids,ident,extend=bool(event.shift))
                self._selected_zone=(self._selected_zone_ids[-1] if self._selected_zone_ids else None)
                obj.dwos_window.active_zone_id=self._selected_zone if isinstance(self._selected_zone,str) else ''
                self._zone_menu_anchor=(x,y) if self._selected_zone is not None else None
                obj.dwos_window.mullion_selection_json='[]'
                self._compact=True;self._active_handle=None
                log_event('COMPOSER_ZONES_SELECTED',window=obj.name,count=len(self._selected_zone_ids),shift=bool(event.shift))
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if self._inside_ui(x,y):
                return {'RUNNING_MODAL'}
            picked=self._pick_window(context,x,y)
            if picked is not None:
                if event.shift:
                    self._shift_toggle_window(context,picked)
                    # Window Shift applies only after semantic target resolution;
                    # Shift on a Mullion was handled above and never reaches here.
                    self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None;self._split_popup_id=None
                    context.area.tag_redraw();return {'RUNNING_MODAL'}
                if picked is not obj:self._switch_window(context,picked,preserve_selection=False)
                else:
                    # Plain click follows Blender: one active/selected window.
                    try:
                        for other in list(context.selected_objects):
                            if other is not picked:other.select_set(False)
                        picked.select_set(True);context.view_layer.objects.active=picked
                    except Exception:pass
                    self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None;self._split_popup_id=None
                    obj.dwos_window.mullion_selection_json='[]';self._compact=True
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            # Blank click deselects without opening a second floating panel.
            self._selected_zone=None;self._selected_zone_ids=[];self._zone_menu_anchor=None
            self._split_popup_id=None
            obj.dwos_window.mullion_selection_json='[]'
            self._compact=True
            context.area.tag_redraw()
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._interior_drag:
            name=self._interior_drag[0]
            value=self._interior_ghost
            self._interior_drag=None;self._interior_ghost=None
            if value is not None:
                self._ensure_active(context)
                self._history_checkpoint(context,'DRAG_'+str(name))
                try:
                    result=bpy.ops.dwos.composer_set_value('EXEC_DEFAULT',target=name,value_text=format_meter(value))
                except Exception as exc:
                    log_exception('INTERIOR_HANDLE_REJECTED',exc,window=obj.name,target=name)
                    self.report({'WARNING'},str(exc))
            context.area.tag_redraw();return {'RUNNING_MODAL'}


        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._depth_drag:
            value = self._ghost_depth
            self._depth_drag = False; self._ghost_depth = None
            if value is not None:
                self._ensure_active(context)
                self._history_checkpoint(context,'DRAG_FRAME_DEPTH')
                try:
                    result=bpy.ops.dwos.composer_set_value('EXEC_DEFAULT', target='FRAME_DEPTH', value_text=format_meter(value))
                except (RuntimeError, ValueError) as exc:
                    log_exception('DEPTH_DRAG_REJECTED', exc, value=format_meter(value), window=obj.name)
                    self.report({'WARNING'}, 'Глубина не применена. Проверьте параметры стекла.')
            context.area.tag_redraw(); return {'RUNNING_MODAL'}

        if event.type=='LEFTMOUSE' and event.value=='RELEASE' and self._tree_drag_id:
            sid=self._tree_drag_id;value=self._tree_drag_value
            self._tree_drag_id='';self._tree_drag_value=None;self._snap_feedback=''
            if value is not None and abs(value-self._group_drag_start)>1e-7:
                self._ensure_active(context)
                self._history_checkpoint(context,'DRAG_ZONE_MULLION')
                s=obj.dwos_window
                row=self._split_row(obj,sid)
                if row:
                    extent=parse_meter_text(s.width_text if row['axis']=='V' else s.height_text)
                    try:
                        ids=parse_ids(s.mullion_selection_json)
                        if sid in ids and len(ids)>1:
                            bpy.ops.dwos.mullion_group_edit('EXEC_DEFAULT',action='MOVE',
                                delta_text=format_meter(value-self._group_drag_start))
                        else:
                            bpy.ops.dwos.zone_edit('EXEC_DEFAULT',split_id=sid,target='POSITION',
                                                   value_text=format_meter(value+extent/2))
                    except (RuntimeError,ValueError) as exc:
                        log_exception('ZONE_DRAG_REJECTED',exc,window=obj.name)
                        self.report({'WARNING'},str(exc))
            context.area.tag_redraw();return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._drag_idx >= 0:
            idx = self._drag_idx; value = self._drag_value
            self._drag_idx = -1; self._drag_value = None;self._snap_feedback=''
            if value is not None and abs(value-self._group_drag_start)>1e-7:
                self._ensure_active(context)
                self._history_checkpoint(context,'DRAG_SPLIT')
                try:
                    s=obj.dwos_window
                    ident=s.splits[idx].split_id
                    ids=parse_ids(s.mullion_selection_json)
                    if ident in ids and len(ids)>1:
                        bpy.ops.dwos.mullion_group_edit('EXEC_DEFAULT',action='MOVE',
                            delta_text=format_meter(value-self._group_drag_start))
                    else:
                        bpy.ops.dwos.composer_set_split_value('EXEC_DEFAULT', index=idx, target='POSITION', value_text=format_meter(value))
                except (RuntimeError, ValueError) as exc:
                    log_exception('SPLIT_DRAG_REJECTED', exc, value=format_meter(value), window=obj.name)
                    self.report({'WARNING'}, 'Положение Split не применено')
            context.area.tag_redraw(); return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


CLASSES = (
    DWOS_OT_composer_set_value,
    DWOS_OT_composer_set_optimization,
    DWOS_OT_composer_set_interior_mode,
    DWOS_OT_composer_choose_material,
    DWOS_OT_apply_frame_material,
    DWOS_OT_default_frame_material,
    DWOS_OT_composer_set_split_value,
    DWOS_OT_viewport_composer,
)


def remove_active_draw_handler():
    """Release DRAW handlers on disable/reload, even after a modal exception."""
    global _ACTIVE
    active=_ACTIVE
    if active is not None:
        try:
            from .diagnostic_bundle import cache_composer_ui_snapshot
            cache_composer_ui_snapshot(active)
        except (ValueError,TypeError,AttributeError,ReferenceError,RuntimeError):
            pass
        active._disabled=True
        from ....core import lifecycle_lease as _lease
        _lease.retire('dwos.composer', getattr(active, '_pulse_token', None))
        try:
            if active._handle is not None:
                bpy.types.SpaceView3D.draw_handler_remove(active._handle,'WINDOW')
                active._handle=None
        except (ReferenceError,RuntimeError,ValueError):
            pass
        _ACTIVE=None


def lifecycle_snapshot():
    """Read-only Composer lifetime evidence for Support ZIP."""
    from ....core import lifecycle_lease as _lease
    active = _ACTIVE
    return {'active': active is not None and not getattr(active, '_disabled', False),
            'has_draw_handler': bool(active is not None and getattr(active, '_handle', None) is not None),
            'lease': _lease.snapshot('dwos.composer')}
