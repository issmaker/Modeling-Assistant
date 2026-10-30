"""Pass 07A: Screen-local, DPI-independent normalized HUD positioning.

Pure functions; do not import bpy or modify modeling data.
"""
from __future__ import annotations


def clamp_to_viewport(viewport, size, preferred, margin=12.):
    """Return bottom-left xy, or None if panel physically cannot fit."""
    vw,vh=map(float,viewport)
    w,h=map(float,size)
    x,y=map(float,preferred)
    margin=max(0.,float(margin))
    if not all(map(lambda n: n == n and abs(n) < 1.e12,(vw,vh,w,h,x,y,margin))):
        return None
    if min(vw,vh,w,h)<=0 or w+2*margin>vw or h+2*margin>vh:
        return None
    return max(margin,min(x,vw-w-margin)), max(margin,min(y,vh-h-margin))
