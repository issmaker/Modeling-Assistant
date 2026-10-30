"""Pure, Blender-free Composer 3.7.7 presentation geometry and state labels.

The functions are deliberately deterministic: rendering never mutates a mesh or
adds automatic timers.  Used by release UI regression tests.
"""
from __future__ import annotations
import math

HEALTH_SHORT = {
    'OK':'ГОТОВО', 'CONTACT':'СВЯЗЬ', 'PENDING':'ЖДЁТ',
    'ERROR':'ОШИБКА', 'FINALIZED':'ГОТОВО', 'NO_WINDOW':'—',
}


def health_short(code):
    return HEALTH_SHORT.get(str(code), 'ПРОВЕРКА')


def health_extra_height(open_details):
    return 80 if open_details else 0


def rect_intersection(a,b,pad=0.0):
    ax,ay,aw,ah=(float(q) for q in a)
    bx,by,bw,bh=(float(q) for q in b)
    p=float(pad)
    return max(0.,min(ax+aw+p,bx+bw+p)-max(ax-p,bx-p))*max(0.,min(ay+ah+p,by+bh+p)-max(ay-p,by-p))


def place_mullion_tag(anchor, viewport, avoid=(), size=(120.,30.), margin=8.):
    """Small read-only in-viewport tag; never covers the Composer or CTA."""
    if anchor is None or len(anchor)!=2:return None
    ax,ay=(float(v) for v in anchor)
    vw,vh=(float(v) for v in viewport)
    w,h=(float(v) for v in size)
    values=(ax,ay,vw,vh,w,h)
    if not all(math.isfinite(v) for v in values) or vw<w+2*margin or vh<h+2*margin:return None
    candidates=((ax+17,ay+15),(ax-w-17,ay+15),(ax+17,ay-h-15),(ax-w-17,ay-h-15))
    for x,y in candidates:
        box=(x,y,w,h)
        if x<margin or y<margin or x+w>vw-margin or y+h>vh-margin:continue
        if not any(rect_intersection(box,r,pad=4)>0 for r in avoid if r):
            return box
    return None


def mullion_position(*,axis,center,span):
    """Metric position from the same centered coordinate used by TREE_POSITION.

    Legacy split callers pass the current screen-preview center reconstructed
    from their existing 0..span position.  Never infer metric scale from pixels.
    """
    if axis not in {'V','H'}:raise ValueError('Unsupported mullion axis')
    c,limit=float(center),float(span)
    if not all(math.isfinite(v) for v in (c,limit)) or limit<=0:raise ValueError('Invalid metric position')
    return max(0.,min(limit,c+limit*.5))


def focus_mullion_id(selected_ids,hover_id,visible_ids):
    available=set(visible_ids)
    if hover_id in available:return hover_id
    for ident in reversed(tuple(selected_ids)):
        if ident in available:return ident
    return None
