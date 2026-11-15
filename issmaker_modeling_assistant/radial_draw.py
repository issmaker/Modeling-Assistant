"""Passive GPU radial view-mode drawing. No mesh, selection, or mouse writes."""
from .extension_compat import addon_key
from .radial_geometry import MODES, sector_polygon, point_for, ease_out, favorite_point
from .nav_hud_geometry import triangulate_fan, rounded_rect, thick_segment
from . import icon_pack
from .core.design_tokens import PANEL_BG, PANEL_SURFACE, TEXT, MUTED, fit_label
from .core import viewport_skin as _viewport_skin


def render(center, scale, opened_at, hovered, selected, accent, *, modes=MODES, center_title='ВИД', center_sub='СБРОС', disabled=frozenset(), menu_kind='VIEW', favorites=()):
    import time
    import gpu
    import blf
    from gpu_extras.batch import batch_for_shader
    from math import sin, cos, pi
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    elapsed = time.monotonic()-opened_at
    progress = ease_out(elapsed/.24)
    if progress <= 0:
        return
    cx, cy=center
    try:
        import bpy
        from .core import theme_engine
        addon=bpy.context.preferences.addons.get(addon_key())
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
            tool_wedges = menu_kind == 'TOOLS' and len(modes) in (2,3,4,5)
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
            dx,dy=point_for(i,112 if tool_wedges else 112,scale*progress,sectors=segs)
            if tool_wedges:
                # Custom monochrome pictograms for the current tool families;
                # no font sprites or fragile external image resources.
                from .core.quick_favorites import ICONS
                from .tool_hud_draw import _fallback_icon
                _fallback_icon(ICONS.get(mode,'FAV_COLLISION'),
                               cx+dx,cy+dy+29*scale*progress,
                               23*scale*progress,
                               (*ACCENT_LABEL[:3],.92*progress))
            lines=label.split('\n')
            for row,line in enumerate(lines):
                size=max(9,round((13 if tool_wedges or quick else _viewport_skin.text_px('caption'))*scale))
                min_size=max(9,round(_viewport_skin.text_px('caption')*scale)) if quick else 9
                width_limit=(96 if tool_wedges else 72 if quick else 60)*scale
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
        if menu_kind=='TOOLS' and len(modes) in (2, 3, 4, 5):
            # Favourite hearts live OUTSIDE the current tool sectors.
            # The same dynamically counted centers are used by favorite_pick().
            # Reuse module-scope triangulate_fan/thick_segment: importing
            # here shadows them as locals of render(), leaving the earlier
            # nested circle() closure unbound and crashing ALL radial HUDs.
            favorite_ids=set(favorites)
            for i,(identifier,caption,_symbol) in enumerate(modes):
                px,py=favorite_point(i,scale*progress)
                marked=identifier in favorite_ids
                focused=hovered==('FAVORITE:'+identifier)
                rr=(14.5 if focused else 13.5)*scale*progress
                circle_color=(*accent,(.43 if marked else .24)*progress)
                shadow=[(px+rr*cos(2*pi*j/32),py-1*scale+rr*sin(2*pi*j/32))
                        for j in range(32)]
                tris(triangulate_fan(shadow),
                     (*PANEL_BG[:3],.94*progress))
                outline=[(px+rr*cos(2*pi*j/32),py+rr*sin(2*pi*j/32))
                         for j in range(33)]
                for a,b in zip(outline,outline[1:]):
                    tris(thick_segment(a,b,1.5*scale*progress),
                         circle_color if not focused else (*accent,.94*progress))
                # Filled when pinned, open outline when not pinned.
                heart=[]
                for j in range(48):
                    t=2*pi*j/48
                    hx=(16*sin(t)**3)/17
                    hy=(13*cos(t)-5*cos(2*t)-2*cos(3*t)-cos(4*t))/19
                    heart.append((px+hx*8.5*scale*progress,
                                  py+hy*8.5*scale*progress))
                fill_color=(*accent,.96*progress) if marked else (*TEXT[:3],.92*progress)
                if marked or focused:
                    tris(triangulate_fan(heart),fill_color)
                else:
                    for a,b in zip(heart,heart[1:]+heart[:1]):
                        tris(thick_segment(a,b,1.4*scale*progress),fill_color)
            # One unobtrusive affordance, not an extra action or hit zone.
            blf.size(0,max(9,round(10*scale)))
            label='♥  ДОБАВИТЬ В ИЗБРАННОЕ'
            label_w,_=blf.dimensions(0,label)
            blf.position(0,cx-label_w/2,cy+203*scale,0)
            blf.color(0,*MUTED[:3],.92*progress)
            blf.draw(0,label)
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
