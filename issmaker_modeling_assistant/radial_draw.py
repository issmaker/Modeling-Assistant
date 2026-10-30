"""Passive GPU radial view-mode drawing. No mesh, selection, or mouse writes."""
from .radial_geometry import MODES, sector_polygon, point_for, ease_out
from .nav_hud_geometry import triangulate_fan, rounded_rect, thick_segment
from . import icon_pack
from .core.design_tokens import PANEL_BG, PANEL_SURFACE, TEXT, MUTED, fit_label
from .core import viewport_skin as _viewport_skin


def render(center, scale, opened_at, hovered, selected, accent, *, modes=MODES, center_title='ВИД', center_sub='СБРОС', disabled=frozenset(), menu_kind='VIEW'):
    import time
    import gpu
    import blf
    from gpu_extras.batch import batch_for_shader
    from math import sin, pi
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    elapsed = time.monotonic()-opened_at
    progress = ease_out(elapsed/.24)
    if progress <= 0:
        return
    cx, cy=center
    try:
        import bpy
        from .core import theme_engine
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        shared_theme=theme_engine.from_preferences(addon.preferences if addon else None)
    except (RuntimeError,ReferenceError,AttributeError,ImportError,TypeError):
        shared_theme=None
    hud=_viewport_skin.shell(shared_theme)
    PANEL_BG=hud['panel']
    PANEL_SURFACE=_viewport_skin.component('button',theme=shared_theme).background
    TEXT=hud['title']
    MUTED=hud['muted']
    ACCENT_LABEL=_viewport_skin.ink(shared_theme,kind='accent')
    # Draw all geometry from local coordinates with a shared origin.
    def tris(coords, rgba):
        if not coords: return
        shader.bind(); shader.uniform_float('color',rgba)
        batch_for_shader(shader,'TRIS',{'pos':[(x+cx,y+cy) for x,y in coords]}).draw(shader)
    def ring(poly,rgba):
        # Concave annular polygon needs explicit quad strip, not a fan.
        mid=len(poly)//2
        coords=[]
        for j in range(mid-1):
            a,b,c,d=poly[j],poly[j+1],poly[-j-2],poly[-j-1]
            coords.extend((a,b,c,a,c,d))
        tris(coords,rgba)
    def circle(radius,rgba,segments=48):
        from math import cos,sin
        poly=[(radius*cos(2*pi*i/segments),radius*sin(2*pi*i/segments)) for i in range(segments)]
        tris(triangulate_fan(poly),rgba)
    gpu.state.blend_set('ALPHA')
    try:
        circle(164*scale*progress,(0.0,.0,.0,.23*progress))
        for i,(mode,label,icon) in enumerate(modes):
            four_tools = menu_kind == 'TOOLS' and len(modes) in (4,5)
            segs = len(modes) if menu_kind == 'TOOLS' else 8
            inactive=mode in disabled
            active=i==hovered and not inactive
            applied=(mode in selected if isinstance(selected,(set,frozenset,tuple,list)) else mode==selected)
            glow=1. if active else .55 if applied else .0
            if active:
                # Soft two-frame halo, bounded and GPU-only.
                pulse=.5+.5*sin(elapsed*7.0)
                ring(sector_polygon(i,64,164,scale*progress,sectors=segs),
                     (*accent,(.10+.085*pulse)*progress))
            ring(sector_polygon(i,67,158,scale*progress,sectors=segs),
                 (*accent,(.24+.2*glow)*progress) if active or applied else (*PANEL_BG[:3],.94*progress))
            ring(sector_polygon(i,72,153,scale*progress,sectors=segs),
                 tuple(min(1.,PANEL_SURFACE[j]+.14*glow) for j in range(3))+(.94*progress,))
            # All radial wheels are text-only. Do not draw a sprite or a
            # fallback square glyph: visible labels are already informative.
            # Command IDs and sector hit testing remain fully unchanged.
            quick = menu_kind == 'QUICK'
            dx,dy=point_for(i,112 if four_tools else 112,scale*progress,sectors=segs)
            lines=label.split('\n')
            for row,line in enumerate(lines):
                size=max(9,round((13 if four_tools or quick else _viewport_skin.text_px('caption'))*scale))
                min_size=max(9,round(_viewport_skin.text_px('caption')*scale)) if quick else 9
                width_limit=(96 if four_tools else 72 if quick else 60)*scale
                while size>min_size:
                    blf.size(0,size)
                    if blf.dimensions(0,line)[0] <= width_limit:break
                    size-=1
                blf.size(0,size)
                w,h=blf.dimensions(0,line)
                if w>width_limit:
                    # Never make a valid TOOL invisible because its Russian
                    # caption is wider than this sector's optical budget.
                    line,_=fit_label(line,width_limit,lambda t:blf.dimensions(0,t)[0])
                    if not line:continue
                    w,h=blf.dimensions(0,line)
                line_height=max(h+2*scale,13*scale)
                offset=(row-(len(lines)-1)/2)*line_height
                blf.position(0,cx+dx-w/2,cy+dy+offset-h/2,0)
                blf.color(0,*( (*MUTED[:3],.86*progress) if inactive else
                                (*ACCENT_LABEL[:3],.99*progress) if active else (*TEXT[:3],.98*progress)))
                blf.draw(0,line)
        circle(45*scale*progress,(*PANEL_SURFACE[:3],.86*progress))
        circle(42*scale*progress,(*PANEL_BG[:3],.98*progress))
        for label,shift,sz,col in [(center_title,8,_viewport_skin.text_px('title'),(*TEXT[:3],.99)),
                                   (center_sub,-11,_viewport_skin.text_px('caption'),(*MUTED[:3],.96))]:
            blf.size(0,max(8,round(sz*scale)))
            w,h=blf.dimensions(0,label)
            blf.position(0,cx-w/2,cy+shift*scale-h/2,0)
            blf.color(0,*col[:3],col[3]*progress)
            blf.draw(0,label)
    finally:
        gpu.state.blend_set('NONE')
