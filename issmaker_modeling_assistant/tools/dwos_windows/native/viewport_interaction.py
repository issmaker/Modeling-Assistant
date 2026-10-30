"""Pure 2D hit/selection math for DWOS viewport editor.

The actual UI owns the Blender event loop.  This module contains only
reproducible, testable geometry and never touches bpy or live BMesh.
"""
from __future__ import annotations
import math


def segment_intersects_box(a, b, corner_a, corner_b, *, margin=0.0):
    """Liang–Barsky segment clipping, including touching box edge."""
    x0, y0 = a; x1, y1 = b
    lo_x = min(corner_a[0],corner_b[0])-margin
    hi_x = max(corner_a[0],corner_b[0])+margin
    lo_y = min(corner_a[1],corner_b[1])-margin
    hi_y = max(corner_a[1],corner_b[1])+margin
    if not all(math.isfinite(v) for v in (x0,y0,x1,y1,lo_x,hi_x,lo_y,hi_y)):
        return False
    dx=x1-x0;dy=y1-y0
    tmin=0.; tmax=1.
    for direction,offset in ((-dx,x0-lo_x),(dx,hi_x-x0),(-dy,y0-lo_y),(dy,hi_y-y0)):
        if abs(direction)<1e-12:
            if offset<0:return False
        else:
            t=offset/direction
            if direction<0:tmin=max(tmin,t)
            else:tmax=min(tmax,t)
            if tmin>tmax:return False
    return True


def segment_in_box(a, b, corner_a, corner_b):
    x0=min(corner_a[0],corner_b[0]);x1=max(corner_a[0],corner_b[0])
    y0=min(corner_a[1],corner_b[1]);y1=max(corner_a[1],corner_b[1])
    return all(x0<=p[0]<=x1 and y0<=p[1]<=y1 for p in (a,b))


def rectangle_selected_segments(lines, a, b, *, containment=False):
    """Return IDs of viewport split segments crossing the marquee box.

    lines: (id, axis, screen_point_a, screen_point_b) tuples.
    A right-to-left drag uses crossing selection, left-to-right containment.
    """
    hit_fn = segment_in_box if containment else segment_intersects_box
    return list(dict.fromkeys(sid for sid,_axis,start,end in lines if hit_fn(start,end,a,b)))


def toolbar_anchor(lines, ids):
    """Stable median screen position of selected logical mullion segments."""
    sel=set(ids)
    centers=[((a[0]+b[0])*0.5,(a[1]+b[1])*0.5)
             for sid,_axis,a,b in lines if sid in sel]
    if not centers:return None
    xs=sorted(q[0] for q in centers);ys=sorted(q[1] for q in centers)
    mid=len(xs)//2
    return ((xs[mid],ys[mid]) if len(xs)%2 else
            ((xs[mid-1]+xs[mid])/2,(ys[mid-1]+ys[mid])/2))


def axis_from_ids(lines, ids):
    axes={axis for sid,axis,_a,_b in lines if sid in set(ids)}
    if len(axes)==1:return next(iter(axes))
    return 'MIXED' if axes else 'NONE'


def preview_position(current_id, current_value, active_id, proposed, original, selected_ids):
    """One shared ghost transform for all lines, with no mutation."""
    if proposed is None or original is None:
        return current_value
    if current_id == active_id:
        return proposed
    selected=set(selected_ids)
    return current_value + (proposed-original) if active_id in selected and current_id in selected else current_value
