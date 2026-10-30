"""Safe AA border renderer for ISSMAKER. No GPU objects at import-time.

Treat a border as a PURELY DECORATIVE inner stroke; it never changes hitboxes.
The core generates bounded feather triangles; BLENDER GPU errors fall back to
original UNIFORM_COLOR line drawing in the caller. No redraw timer is created.
"""
from __future__ import annotations
from .core import hud_quality

_LAST_ERROR = None
_DISABLE_AA = False

def draw_aa_border(points,rgba,rect,*,width=.85,fringe=.85,closed=False):
    """Return False if unavailable. The caller retains its legacy fallback."""
    global _LAST_ERROR, _DISABLE_AA
    if len(points)<2 or _DISABLE_AA:return False
    # Respect a user's PERFORMANCE preset. Do not force expensive AA strokes.
    try:
        import bpy
        item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        if item is not None and getattr(item.preferences,'ui_render_quality','HIGH')=='PERFORMANCE':
            return False
    except (ImportError,AttributeError,RuntimeError,ReferenceError):pass
    old_blend=None
    gpu=None
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        paths=tuple((points[i],points[(i+1)%len(points)]) for i in range(len(points) if closed else len(points)-1))
        segs=[(float(a[0]),float(a[1]),float(b[0]),float(b[1]),1.) for a,b in paths]
        from . import hud_gpu_runtime
        shared=hud_gpu_runtime.cache()
        shader=shared.smooth_shader if shared is not None else None
        if shader is None:
            shader=gpu.shader.from_builtin('SMOOTH_COLOR')
            if shared is not None:shared.smooth_shader=shader
        c=tuple(float(a) for a in rgba)
        def prepare():
            verts,coverage=hud_quality.smooth_stroke_mesh(segs,rect,width=width,fringe=fringe)
            if not verts:return None
            colors=tuple((c[0],c[1],c[2],c[3]*v[3]) for v in coverage)
            return batch_for_shader(shader,'TRIS',{'pos':verts,'color':colors})
        if shared is not None:
            # Cap at 160 separate borders for one viewport, including colors.
            # RGB/alpha are part of the key; hover never shows stale colors.
            key=(tuple((round(float(a),3),round(float(b),3)) for a,b in points),
                 tuple(round(float(n),3) for n in rect),
                 tuple(round(float(n),4) for n in c),round(width,3),round(fringe,3),bool(closed))
            batch=shared.fetch(shared.chrome,key,prepare,160)
        else:batch=prepare()
        if batch is None:return False
        try:
            old_blend=gpu.state.blend_get()
        except (AttributeError,RuntimeError):pass
        gpu.state.blend_set('ALPHA')
        shader.bind()
        batch.draw(shader)
        return True
    except Exception as exc:
        _LAST_ERROR=f'{type(exc).__name__}: {exc}'[:180]
        # GPU shader incompatibility is process-wide for this session; do not
        # retry 40 times per frame. Blender restart resets this safe fallback.
        if gpu is not None:_DISABLE_AA=True
        return False
    finally:
        if old_blend is not None and gpu is not None:
            try:gpu.state.blend_set(old_blend)
            except (AttributeError,RuntimeError):pass

def draw_aa_rect(x0,y0,x1,y1,color):
    """Inward antialiased 1px hairline, strictly bounded by rectangle."""
    x0,y0,x1,y1=map(float,(x0,y0,x1,y1))
    if x1-x0<5 or y1-y0<5:return False
    inset=1.75
    pts=((x0+inset,y0+inset),(x1-inset,y0+inset),
         (x1-inset,y1-inset),(x0+inset,y1-inset))
    return draw_aa_border(pts,color,(x0,y0,x1-x0,y1-y0),closed=True)
