"""DWOS 3.0.1: minimal, read-only selection/placement helpers.

One picked mullion never opens a toolbar. A horizontal and a vertical
mullion cannot coexist in a multi-selection. Geometry always belongs to
separate split IDs; aligning never merges or renames them.
"""
from __future__ import annotations
import math


def select_compatible(old, ident, axis_by_id, *, extend=False):
    """Return ordered selection; last selected ID is the alignment anchor.

    Mixed-axis Shift+click is ignored without mutating the existing set.
    """
    if not ident or ident not in axis_by_id:
        return list(old)
    if not extend:
        return [ident]
    current = [x for x in dict.fromkeys(old) if x in axis_by_id]
    if ident in current:
        return [x for x in current if x != ident]
    if current and axis_by_id[current[-1]] != axis_by_id[ident]:
        return current
    return current + [ident]


def marquee_compatible(old, picked, axis_by_id):
    """Add hits of one orientation only, preserving selected reference order."""
    current=[x for x in dict.fromkeys(old) if x in axis_by_id]
    orientation=axis_by_id[current[-1]] if current else None
    if orientation is None:
        # A marquee across two H segments may also cross the central V split.
        # Prefer the most-represented axis, not the incidental traversal order.
        candidates=[axis_by_id.get(x) for x in picked if x not in current]
        valid=[x for x in candidates if x in ('H','V')]
        if valid:
            counts={axis:valid.count(axis) for axis in ('H','V')}
            orientation=max(valid, key=lambda axis:counts[axis])
    for sid in picked:
        axis=axis_by_id.get(sid)
        if axis not in ('H','V') or sid in current:
            continue
        if orientation is None:
            orientation=axis
        if axis == orientation:
            current.append(sid)
    return current


def compact_align_axis(rows, ids):
    """Which compact Align button is valid for the selected semantic mullions.

    Different CORNER branches may intentionally align to one H level. Inside one
    editing region we keep the legacy disjoint-span safeguard.
    """
    available={r['id']:r for r in rows}
    selected=[available[x] for x in ids if x in available]
    if len(selected)<2 or len(selected)!=len(ids):return None
    axes={r['axis'] for r in selected}
    if len(axes)!=1:return None
    axis=selected[0]['axis']
    if axis not in ('H','V'):return None
    branch_ids=[str(r.get('branch_id','')) for r in selected]
    if all(branch_ids) and len(set(branch_ids))>1:
        return axis
    spans=sorted((r['rect'][0],r['rect'][1]) if axis=='H' else
                 (r['rect'][2],r['rect'][3]) for r in selected)
    if any(b[0] < a[1] - 1e-7 for a,b in zip(spans,spans[1:])):
        return None
    return axis


def place_align_button(window_rect,viewport,panel_rect=None,*,size=(142,31),margin=12):
    """Place outside the projected WINDOW (not merely outside selected lines).

    None if there is no free spot: do not cover geometry to force a toolbar.
    Coordinates and rectangles are in View3D pixels (origin bottom-left).
    """
    if window_rect is None:
        return None
    x,y,w,h=window_rect;vw,vh=viewport;bw,bh=size
    if not all(math.isfinite(float(n)) for n in (x,y,w,h,vw,vh,bw,bh)):
        return None
    if min(w,h,vw,vh,bw,bh)<=0:
        return None
    # Outside the window and away from the main settings card.
    candidate=[(x+w+18,y+h*.5-bh*.5),
               (x-bw-18,y+h*.5-bh*.5),
               (x+w*.5-bw*.5,y+h+18),
               (x+w*.5-bw*.5,y-bh-18)]
    avoid=[(x-margin,y-margin,w+2*margin,h+2*margin)]
    if panel_rect:avoid.append(panel_rect)
    for cx,cy in candidate:
        if cx<margin or cy<margin or cx+bw>vw-margin or cy+bh>vh-margin:
            continue
        if all(cx+bw+4<=rx or rx+rw+4<=cx or cy+bh+4<=ry or ry+rh+4<=cy
               for rx,ry,rw,rh in avoid):
            return (round(cx),round(cy),bw,bh)
    return None
