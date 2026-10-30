"""Same premium graphite skin as the primary toolbar for Flip/Stretch/Weld.

Passive draw only: independent Blender Gizmos retain operator dispatch.
"""
import time
from . import tool_hud_draw
from .core.design_tokens import MINT, RED, TEXT, MUTED
from .core import viewport_skin as _viewport_skin
from .core import motion_engine
_MOTION = motion_engine.MotionBank(limit=96)

def draw_complete_hud(layout_data,kind,index,count,*,highlighted_slot=-1, symbols=None, motion_enabled=True, grip_enabled=False, grip_active=False, grip_hovered=False):
    import blf, gpu
    now=time.monotonic()
    try:
        import bpy
        from .core import theme_engine
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        shared_theme=theme_engine.from_preferences(addon.preferences if addon else None)
    except (RuntimeError,ReferenceError,AttributeError,ImportError,TypeError):
        shared_theme=None
    spec = motion_engine.from_theme(shared_theme) if motion_enabled else motion_engine.MotionSpec(mode='OFF')
    gpu.state.blend_set('ALPHA')
    try:
        for slot,(x,y,size) in enumerate(layout_data['buttons']):
            key=(round(x,1),round(y,1),slot)
            goal = slot == highlighted_slot
            level = _MOTION.sample(key,goal,now,spec.duration('hover'))
            symbol=(symbols or ('BACK','NEXT','CHECK','X'))[slot]
            # Semantic green/red remain obvious even without hover.
            accent=(MINT[:3] if slot==2 else RED[:3] if slot==3
                    else tuple(getattr(shared_theme,'accent',MINT[:3])))
            tool_hud_draw.button_paint(x,y,size,symbol,slot in (2,3),level,accent,theme=shared_theme)
        if layout_data.get('grip'):
            try:
                gx, gy, gw, gh = layout_data['grip']
                # Purely cosmetic: size is bounded by the same native grip box.
                grip_text = '•••  ДВИГАТЬ' if grip_enabled else '•••'
                grip_level = _MOTION.sample(('grip',), bool(grip_active or grip_hovered), now, spec.duration('hover'))
                grip_size = max(8, min(12, round(10*layout_data['scale'])))
                blf.size(0, grip_size)
                text_width, _ = blf.dimensions(0, grip_text)
                if text_width <= gw - 4:
                    ink = _viewport_skin.ink(shared_theme)
                    blf.color(0, ink[0], ink[1], ink[2], (.28 if not grip_enabled else .65 + .3*grip_level))
                    blf.position(0, gx-text_width*.5, gy-grip_size*.35, 0)
                    blf.draw(0, grip_text)
            except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
                pass
        label='%s · %d / %d'%(kind,max(0,index),max(0,count))
        try:
            size=max(9,round(_viewport_skin.text_px('control')*layout_data['scale']))
            blf.size(0,size)
            w,h=blf.dimensions(0,label)
            x,y=layout_data['label_anchor']
            blf.color(0,*_viewport_skin.ink(shared_theme)[:3],.94)
            blf.position(0,x-w/2,y,0)
            blf.draw(0,label)
        except (RuntimeError,AttributeError,ValueError):pass
    finally:
        gpu.state.blend_set('NONE')

def reset():
    _MOTION.clear()
