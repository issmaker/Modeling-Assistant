"""RC31 viewport-only HUD layout logic. No bpy or GPU dependencies.

The returned controls are anchored to actual FULL-height V split IDs. In crowded
views a focus-based level of detail prevents a screen full of tiny buttons.
Never move a button over a blocked HUD or over another button; hide it instead.
"""
from __future__ import annotations
import math

W,H=30.0,30.0

def overlap(a,b,pad=0.0):
    ax,ay,aw,ah=a;bx,by,bw,bh=b
    return min(ax+aw,bx+bw+pad)>max(ax,bx-pad) and min(ay+ah,by+bh+pad)>max(ay,by-pad)

def eligible_hud_ids(candidates, focus=None, active=(), page='INTERIOR', dense_limit=9):
    """Return ordered stable IDs. No automatic controls for short splitters."""
    ids=[]
    for row in candidates:
        sid=str(row[0])
        if sid and sid not in ids:ids.append(sid)
    active_set=set(str(x) for x in active)
    focus=str(focus) if focus is not None else ''
    if page=='INTERIOR' and len(ids)<=dense_limit:
        return ids
    # Focused/selected V only, up to six buttons, never blanket 30+.
    ordered=([focus] if focus in ids else [])+[x for x in ids if x in active_set and x!=focus]
    return ordered[:6]

def place_scissor_icons(candidates, viewport, avoid=(), focus=None, active=(),page='INTERIOR',dense_limit=9):
    """candidates=(ID, anchor_x, anchor_y); outputs (ID,(x,y,30,30))."""
    vw,vh=map(float,viewport)
    if not math.isfinite(vw+vh) or min(vw,vh)<48:return []
    candidates=list(candidates)
    ordered=eligible_hud_ids(candidates,focus,active,page,dense_limit)
    mapping={str(sid):(float(x),float(y)) for sid,x,y in candidates}
    blocking=[tuple(map(float,b)) for b in avoid if b and len(b)==4]
    placed=[]
    for sid in ordered:
        if sid not in mapping:continue
        cx,cy=mapping[sid]
        if not all(math.isfinite(q) for q in (cx,cy)):continue
        # Only nearby choices: never detach an icon from its physical post.
        for dx,dy in ((18,4),(-W-18,4),(18,-H-5),(-W-18,-H-5),(18,H+8),(-W-18,H+8)):
            rect=(cx+dx,cy+dy,W,H)
            x,y,w,h=rect
            if x<8 or y<8 or x+w>vw-8 or y+h>vh-8:continue
            if any(overlap(rect,b,4) for b in blocking):continue
            if any(overlap(rect,p[1],6) for p in placed):continue
            placed.append((sid,rect))
            break
    return placed

def projected_zone_label(poly, index, selected=False, hovered=False, min_width=72,min_height=38):
    """One compact label per focused zone; no label flood in a dense split tree."""
    if not (selected or hovered) or len(poly)<3:return None
    xs=[p[0] for p in poly];ys=[p[1] for p in poly]
    x0,x1=min(xs),max(xs);y0,y1=min(ys),max(ys)
    if x1-x0<min_width or y1-y0<min_height:return None
    return (f'Z{index+1:02d}',((x0+x1)/2,(y0+y1)/2))
