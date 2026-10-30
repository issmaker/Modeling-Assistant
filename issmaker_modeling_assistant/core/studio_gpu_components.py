"""One real GPU-component renderer shared by DWOS and UV Wall.

The math layer is importable without Blender. GPU resources are created lazily
inside draw calls. Caller retains ownership of hitboxes and semantic actions.
"""
from __future__ import annotations
from math import pi, cos, sin, isfinite


def rounded_outline(x,y,w,h,radius=7.,steps=5):
    vals=tuple(map(float,(x,y,w,h,radius)))
    if not all(isfinite(v) for v in vals) or w<=0 or h<=0:return ()
    r=max(0.,min(radius,w*.5,h*.5));n=max(1,min(16,int(steps)))
    if r<.1:return ((x,y),(x+w,y),(x+w,y+h),(x,y+h))
    corners=((x+w-r,y+r,-pi/2,0.),(x+w-r,y+h-r,0.,pi/2),
             (x+r,y+h-r,pi/2,pi),(x+r,y+r,pi,3*pi/2))
    return tuple((cx+r*cos(a+(b-a)*i/n),cy+r*sin(a+(b-a)*i/n))
                 for cx,cy,a,b in corners for i in range(n+1))


def fill_rect(x0,y0,x1,y1,color,radius=7.,steps=5):
    """Return False on GPU failure. Never modify current hitboxes."""
    pts=rounded_outline(x0,y0,x1-x0,y1-y0,radius,steps)
    if not pts:return False
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        from issmaker_modeling_assistant import hud_gpu_runtime
        shared=hud_gpu_runtime.cache()
        sh=(shared.shader if shared else None) or gpu.shader.from_builtin('UNIFORM_COLOR')
        if shared is not None:shared.shader=sh
        cx=(x0+x1)*.5;cy=(y0+y1)*.5
        def make():
            vertices=((cx,cy),)+pts
            triangles=tuple((0,i,i+1) for i in range(1,len(pts)))+((0,len(pts),1),)
            return batch_for_shader(sh,'TRIS',{'pos':vertices},indices=triangles)
        if shared is not None:
            key=('STUDIO_GPU_RECT',round(x0,3),round(y0,3),round(x1,3),round(y1,3),round(radius,2),int(steps))
            batch=shared.fetch(shared.boxes,key,make,shared.max_boxes)
        else:batch=make()
        gpu.state.blend_set('ALPHA')
        sh.bind();sh.uniform_float('color',color);batch.draw(sh)
        return True
    except (ImportError,RuntimeError,ValueError,TypeError,AttributeError):return False


def outline_rect(x0,y0,x1,y1,color,radius=7.,width=1.,steps=5):
    """Inward antialiased border; fail closed so caller can use legacy stroke."""
    pts=rounded_outline(x0+1.8,y0+1.8,x1-x0-3.6,y1-y0-3.6,max(.0,radius-1.8),steps)
    if not pts:return False
    try:
        from issmaker_modeling_assistant.hud_chrome_gpu import draw_aa_border
        return bool(draw_aa_border(pts,color,(x0,y0,x1-x0,y1-y0),width=width,closed=True))
    except (ImportError,RuntimeError,ValueError,TypeError,AttributeError):return False


def shell_background(theme,rect):
    """Studia/ DWOS panel material in matching physical pixels (decorative)."""
    if theme is None:return False
    try:
        from issmaker_modeling_assistant.appearance_gpu import draw_shared_background
        x0,y0,x1,y1=rect
        draw_shared_background(theme,(x0,y0,x1-x0,y1-y0))
        return True
    except (ImportError,RuntimeError,ValueError,TypeError,AttributeError):return False


def draw_component(rect, *, part='button', state='normal', role='neutral',
                   theme=None, scale=1., accent_line=True):
    """Shared matte GPU component: one visual state, no event or hitbox ownership.

    Legacy controllers provide the exact original xyxy bounds for paint *and*
    hit testing. If the GPU path is unavailable return False and let that
    controller draw its existing fallback without changing click behavior.
    """
    from .workspace_components import component_visual
    try:
        a,b,c,d=map(float,rect)
        scale=float(scale)
        if not all(isfinite(v) for v in (a,b,c,d,scale)) or c<=a or d<=b or scale<=0:
            return False
        visual=component_visual(part,state,role,theme)
    except (TypeError,ValueError,OverflowError):
        return False
    radius={'button':6.,'field':5.,'tab':6.,'header':8.,'card':9.}[part]*scale
    radius=min(radius,(c-a)/2,(d-b)/2)
    if not fill_rect(a,b,c,d,visual.background,radius):
        return False
    # The outline is strictly decorative; a failed AA shader must not remove
    # the already-painted button or its pre-existing input rectangle.
    outline_rect(a,b,c,d,visual.border,radius,1.*scale)
    if accent_line and state in {'active','focused'} and role in {'primary','warning','danger'}:
        inset=min(max(4.*scale,radius),max(0.,(c-a)/4))
        if c-a>2*inset+2 and d-b>=8*scale:
            # One quiet, short status stroke, never a full-width neon border.
            width=min(34.*scale,(c-a)-2*inset)
            fill_rect(a+inset,d-2.4*scale,a+inset+width,d-1.1*scale,
                      visual.accent,0.)
    return True
