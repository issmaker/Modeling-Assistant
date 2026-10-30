"""ISSMAKER graphite HUD 1.3.29: DPI-independent icon atlas + robust vector fallback.

Native Gizmos continue to own hit-testing. A shared POST_PIXEL pass renders the
visible controls. No modal event capture, no Mesh access, no perpetual idle timer.
"""
import math
import time
from . import icon_pack
from .core.design_tokens import PANEL_BG, PANEL_SURFACE, TEXT
from .core import viewport_skin as _viewport_skin
from .nav_hud_geometry import triangulate_fan, thick_segment

_HOVER_LEVELS={}
_SHADER=None
_STROKES={
 'MOD_PHYSICS':[(-.7,-.7,.7,-.7),(.7,-.7,.7,.7),(.7,.7,-.7,.7),(-.7,.7,-.7,-.7),(-.25,-.6,-.25,.6),(.25,-.6,.25,.6),(-.6,-.25,.6,-.25),(-.6,.25,.6,.25)],
 'SHADING_SOLID':[(0,-.85,.75,-.43),(.75,-.43,.75,.5),(.75,.5,0,.85),(0,.85,-.75,.5),(-.75,.5,-.75,-.43),(-.75,-.43,0,-.85)],
 'AUTOMERGE_ON':[(-.7,-.5,-.3,-.5),(-.3,-.5,-.3,-.1),(-.3,-.1,-.7,-.1),(-.7,-.1,-.7,-.5),(.3,.1,.7,.1),(.7,.1,.7,.5),(.7,.5,.3,.5),(.3,.5,.3,.1),(-.3,-.1,.4,.45)],
 'NORMALS_FACE':[(-.7,-.6,-.7,.6),(-.7,0,.65,0),(.2,-.3,.65,0),(.65,0,.2,.3)],
 'MOD_WIREFRAME':[(-.7,-.7,.7,-.7),(.7,-.7,.7,.7),(.7,.7,-.7,.7),(-.7,.7,-.7,-.7)],
 'MOD_SMOOTH':[(-.65,-.45,-.4,.2),(-.4,.2,.1,.65),(-.4,-.65,-.15,0),(-.15,0,.35,.45),(-.15,-.75,.05,-.25),(.05,-.25,.65,.25)],
 'UV_SYNC_SELECT':[(-.7,-.2,-.15,-.5),(-.15,-.5,.15,-.05),(.15,-.05,.7,-.35),(-.55,.35,0,.55),(0,.55,.55,.15),(-.05,.8,.15,.55),(.15,.55,.35,.8)],
 'LINKED':[(-.8,-.2,-.3,-.7),(-.3,-.7,.3,-.7),(.3,-.7,.8,-.2),(-.8,.2,-.3,.7),(-.3,.7,.3,.7),(.3,.7,.8,.2)],
 'UV_DATA':[(-.7,-.7,.1,-.7),(.1,-.7,.1,.1),(.1,.1,-.7,.1),(-.7,.1,-.7,-.7),(-.3,-.7,-.3,.1),(-.7,-.3,.1,-.3),(.35,-.7,.35,.7),(.65,-.7,.65,.7),(.2,-.45,.8,-.45),(.2,-.05,.8,-.05),(.2,.35,.8,.35)],
 'NODE_MATERIAL':[(-.8,.5,-.3,.5),(-.3,.5,-.3,-.4),(-.3,-.4,.7,-.4)],
 'PIVOT_BOUNDBOX':[(-.75,-.35,-.75,-.75),(-.75,-.75,-.35,-.75),(.75,-.35,.75,-.75),(.75,-.75,.35,-.75),(-.75,.35,-.75,.75),(-.75,.75,-.35,.75),(.75,.35,.75,.75),(.75,.75,.35,.75),(0,-.5,0,.5),(-.5,0,.5,0)],
 'MOD_UVPROJECT':[(-.55,-.65,.2,-.65),(.2,-.65,.2,.45),(.2,.45,-.55,.45),(-.55,.45,-.55,-.65),(-.15,-.25,.6,-.25),(.6,-.25,.6,.85),(.6,.85,-.15,.85),(-.15,.85,-.15,-.25),(.15,-.8,.35,-.6),(.35,-.8,.15,-.6)],
 'VIEWZOOM':[(-.7,.15,-.7,.7),(-.7,.7,-.1,.7),(.1,-.1,.7,-.7)],
 'TRASH':[(-.7,.5,.7,.5),(-.5,.3,-.4,-.75),(-.4,-.75,.4,-.75),(.4,-.75,.5,.3)],
 'X':[(-.7,-.7,.7,.7),(-.7,.7,.7,-.7)],
 'BACK':[(.25,-.75,-.5,0),(-.5,0,.25,.75)],
 'NEXT':[(-.25,-.75,.5,0),(.5,0,-.25,.75)],
 'CHECK':[(-.75,0,-.18,-.65),(-.18,-.65,.76,.75)],
 'RADIAL':[(-.75,-.25,-.25,.25),(-.25,.25,.5,.65),(.5,.65,.8,-.4)],
 'ISS_TOOLS':[(-.72,-.72,-.2,-.72),(-.2,-.72,-.2,-.2),(-.2,-.2,-.72,-.2),(-.72,-.2,-.72,-.72),(.2,-.72,.72,-.72),(.72,-.72,.72,-.2),(.72,-.2,.2,-.2),(.2,-.2,.2,-.72),(-.72,.2,-.2,.2),(-.2,.2,-.2,.72),(-.2,.72,-.72,.72),(-.72,.72,-.72,.2),(.2,.2,.72,.2),(.72,.2,.72,.72),(.72,.72,.2,.72),(.2,.72,.2,.2)],
 'ISS_SPLIT':[(-.72,-.65,.72,-.65),(.72,-.65,.72,.65),(.72,.65,-.72,.65),(-.72,.65,-.72,-.65),(0,-.65,0,.65),(-.56,0,-.15,0),(.15,.25,.54,.25)]}


def _tris(points,rgba):
    if not points:return
    import gpu
    from gpu_extras.batch import batch_for_shader
    global _SHADER
    if _SHADER is None:_SHADER=gpu.shader.from_builtin('UNIFORM_COLOR')
    _SHADER.bind();_SHADER.uniform_float('color',rgba)
    batch_for_shader(_SHADER,'TRIS',{'pos':points}).draw(_SHADER)


def _circle(cx,cy,radius,rgba,segments=72):
    if radius<=0:return
    pts=[(cx+radius*math.cos(2*math.pi*i/segments),
          cy+radius*math.sin(2*math.pi*i/segments)) for i in range(segments)]
    _tris(triangulate_fan(pts),rgba)


def _ring(cx,cy,r,width,rgba,segments=72):
    if width<=0:return
    pts=[]
    for i in range(segments):
        a=math.tau*i/segments;b=math.tau*(i+1)/segments
        def pt(rad,ang):return(cx+rad*math.cos(ang),cy+rad*math.sin(ang))
        o0,o1=pt(r,a),pt(r,b)
        i0,i1=pt(r-width,a),pt(r-width,b)
        pts.extend((o0,o1,i1,o0,i1,i0))
    _tris(pts,rgba)


def _fallback_icon(symbol,x,y,size,ink,rotation=0):
    segments=_STROKES.get(symbol)
    if not segments:return
    icon_pack.note_fallback()
    cs=math.cos(rotation);ss=math.sin(rotation)
    for a,b,c,d in segments:
        def xy(vx,vy):
            vx*=size*.28;vy*=size*.28
            return x+vx*cs-vy*ss,y+vx*ss+vy*cs
        _tris(thick_segment(xy(a,b),xy(c,d),max(1.7,size*.057)),ink)


def _lerp(a,b,t):return a+(b-a)*t


def _smooth(t):
    t=max(0.,min(1.,float(t)))
    return t*t*(3.-2.*t)


def button_paint(x,y,size,symbol,active,hover,accent,radial_opener=False, *, theme=None):
    """Draw one complete circular control; no interception outside native hit circle."""
    hover=_smooth(hover)
    panel_ink=_viewport_skin.ink(theme)
    control_surface=_viewport_skin.component('button',theme=theme).background
    # Hover has a strong silhouette change, without an infinite idle pulse.
    cx,cy=round(x)+.0,round(y)+.0
    r=size*.5
    if hover>.01:
        _circle(cx,cy,r+2.6,(accent[0],accent[1],accent[2],.16*hover))
        _circle(cx,cy,r+1.2,(accent[0],accent[1],accent[2],.18*hover))
    _circle(cx,cy-1.2,r+.7,(.0,.0,.0,.24))
    _circle(cx,cy,r,tuple(min(1.,control_surface[i]+.055*hover) for i in range(3))+(.96,))
    _ring(cx,cy,r-.5,1.15,(*accent,(.6 if active else .48*hover)))
    if radial_opener:
        # Persistent double ring + top-right notch mark a mode-opening action.
        _ring(cx,cy,r-2.25,.85,(*accent,.72 if active else .4+.42*hover))
        ax=cx+r*.69;ay=cy+r*.69
        _circle(ax,ay,max(1.4,size*.058),(*accent,.85 if hover else .62))
    if active:
        _circle(cx,cy,r-4.6,(*accent,.13))
    color=panel_ink[:3]
    # The raw accent still defines the decorative ring. Glyphs instead use a
    # contrast-corrected accent, so dark user accents never hide an icon.
    goal=_viewport_skin.ink(theme,kind='accent')[:3]
    tint=min(1.,hover*.94 + (.74 if active else 0.))
    ink=tuple(_lerp(color[i],goal[i],tint) for i in range(3))
    # Small meaningful icon movement rather than bouncing the clickable button.
    # Tilt only modes, slide directional symbols; positions remain within hit circle.
    rotate=((.075 if symbol in ('SHADING_SOLID','LINKED','MOD_UVPROJECT') else 0.)*hover)
    dx=(1.1*hover if symbol in ('LINKED','AUTOMERGE_ON') else 0.)
    dy=(.8*hover if symbol in ('MOD_SMOOTH','UV_SYNC_SELECT') else 0.)
    icon_size=size*(.89+.025*hover)
    # ISSMAKER 1.8.2 field appearance: use the original geometry glyphs,
    # independently of GPUTexture support. The later FLOAT sprite fix made
    # different bitmap pictograms appear on the same buttons, which was an
    # unrequested visual redesign. Keep hit-testing and hover unchanged.
    _fallback_icon(symbol,cx+dx,cy+dy,size,(*ink,.99),rotate)


def render(groups,accent,now=None):
    """All visual geometry in one pass; clear state even after a GPU failure."""
    import gpu
    now=time.monotonic() if now is None else now
    try:
        import bpy
        from .core import theme_engine
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        shared_theme=theme_engine.from_preferences(addon.preferences if addon else None)
    except (RuntimeError,ReferenceError,AttributeError,ImportError,TypeError):
        shared_theme=None
    gpu.state.blend_set('ALPHA')
    try:
        for group in groups:
            for x,y,size,symbol,active,requested_hover in group['buttons']:
                key=(group['key'],round(x,1),round(y,1),symbol)
                prev,last=_HOVER_LEVELS.get(key,(0.,now))
                target=max(0.,min(1.,requested_hover))
                # Independent ease for incoming/outgoing states, bounded at 180ms.
                factor=_smooth(min(1.,max(0.,(now-last)/.18)))
                hover=_lerp(prev,target,factor)
                if abs(target-hover)<.008:hover=target
                _HOVER_LEVELS[key]=(hover,now)
                radial=(symbol=='LINKED' or
                       (group['key']=='view' and symbol in {'SHADING_SOLID','ISS_TOOLS'}) or
                       (group['key']=='shader' and symbol=='NODE_MATERIAL'))
                button_paint(x,y,size,symbol,active,hover,accent,radial,theme=shared_theme)
    finally:
        gpu.state.blend_set('NONE')
    if len(_HOVER_LEVELS)>180:
        for k,(_,last) in list(_HOVER_LEVELS.items()):
            if now-last>3.:_HOVER_LEVELS.pop(k,None)


def reset():
    global _SHADER
    _HOVER_LEVELS.clear();_SHADER=None
    icon_pack.reset()
