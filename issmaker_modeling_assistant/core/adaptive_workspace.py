"""Pass 15.11 PREVIEW: deterministic visual workspace layout without Blender.

Only a measured placement proposal: modal/UI controllers own all hitboxes and
must never activate compact mode unless its essential actions are reachable.
No bpy, gpu, window references or persistent state.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite

from .window_manager import ViewportWindowManager
from .overlay_placement import rect_xyxy

DENSITIES = frozenset({'AUTO', 'COMPACT', 'STANDARD', 'DETAILED'})


@dataclass(frozen=True)
class PanelProposal:
    key: str
    standard: tuple
    preferred: tuple
    compact: tuple | None = None
    detailed: tuple | None = None
    priority: int = 0
    pinned: bool = False
    # Compact is only legal if caller certifies a route to every vital action.
    compact_safe: bool = False


@dataclass(frozen=True)
class PanelPlacement:
    key: str
    bounds: tuple | None
    density: str
    status: str


def sanitize_scale(value):
    try:
        number = float(value)
        return min(2., max(.75, number)) if isfinite(number) else 1.
    except (TypeError, ValueError, OverflowError):
        return 1.


def resolved_density(viewport, *, policy='AUTO', scale=1.):
    """Fallback to compact only in narrow/tall constrained viewports.

    DETAILED is never chosen automatically; preserve explicit user intent.
    """
    if policy not in DENSITIES:
        raise ValueError('Unknown workspace density')
    w,h = tuple(float(v) for v in viewport)
    if not all(isfinite(x) and x > 0 for x in (w,h)):
        raise ValueError('Invalid viewport')
    k=sanitize_scale(scale)
    if policy != 'AUTO':
        return policy
    return 'COMPACT' if (w < 790*k or h < 475*k) else 'STANDARD'


def magnetize(preferred, size, viewport, *, scale=1., distance=14., margin=8.):
    """Snap to viewport edge only inside a small magnet zone, clamp to screen.

    Not a persistent drag preference; the caller may store a normalized anchor
    using its existing workspace system after user drag ends.
    """
    try:
        x,y=(float(v) for v in preferred)
        w,h=(float(v) for v in size)
        vw,vh=(float(v) for v in viewport)
        distance=float(distance);margin=float(margin)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Invalid magnet placement') from exc
    if (not all(isfinite(v) for v in (x,y,w,h,vw,vh,distance,margin)) or
            min(w,h,vw,vh)<=0 or min(distance,margin)<0):
        raise ValueError('Invalid magnet placement')
    k=sanitize_scale(scale)
    m=margin*k
    x_max=max(m,vw-w-m);y_max=max(m,vh-h-m)
    x=min(max(m,x),x_max);y=min(max(m,y),y_max)
    if abs(x-m)<=distance*k:x=m
    elif abs(x-x_max)<=distance*k:x=x_max
    if abs(y-m)<=distance*k:y=m
    elif abs(y-y_max)<=distance*k:y=y_max
    return x,y


def plan_workspace(viewport, proposals, *, blockers=(), density='AUTO',
                   scale=1., margin=8., gap=6.):
    """Plan visible panels; fallback to NO_ROOM without ghost hit regions.

    Caller passes truly safe compact sizes, after testing its own text/actions;
    layout does not silently hide controls or alter text buffers.
    """
    profile=resolved_density(viewport,policy=density,scale=scale)
    manager=ViewportWindowManager(*viewport,margin=margin*sanitize_scale(scale),
                                  gap=gap*sanitize_scale(scale))
    for i,box in enumerate(blockers):
        if box is not None:
            manager.reserve(f'blocker:{i}',rect_xyxy(box))
    requests=tuple(proposals)
    seen=set()
    for p in requests:
        if not isinstance(p,PanelProposal) or not p.key or p.key in seen:
            raise ValueError('Bad or duplicate panel proposal')
        seen.add(p.key)
        allow_compact=bool(p.compact_safe and p.compact is not None)
        if profile=='COMPACT' and allow_compact:
            size=p.compact
            alternate=None
        elif profile=='DETAILED' and p.detailed is not None:
            size=p.detailed
            alternate=p.standard  # A detailed panel may return to standard.
        else:
            size=p.standard
            alternate=p.compact if allow_compact else None
        origin=magnetize(p.preferred,size,viewport,scale=scale,margin=margin)
        manager.register(p.key,size,origin,priority=p.priority,
                         compact_size=alternate,pinned=p.pinned)
    result=manager.layout()
    placements=[]
    for p in requests:
        state=manager.windows[p.key]
        key=p.key
        if state.rect is None:
            final='NO_ROOM'
        elif state.status in {'AUTO_COMPACT','COLLAPSED'}:
            final=('STANDARD' if profile=='DETAILED' else 'COMPACT') if state.status=='AUTO_COMPACT' else profile
        elif profile=='DETAILED' and p.detailed is not None:
            final='DETAILED'
        elif profile=='COMPACT' and p.compact_safe and p.compact is not None:
            final='COMPACT'
        else:
            final='STANDARD'
        placements.append(PanelPlacement(key,result.get(key),final,state.status))
    return tuple(placements)


def diagnostics():
    return {'contract':'ADAPTIVE_WORKSPACE_PREVIEW_1511',
            'modes':sorted(DENSITIES),'policy':'fail_closed',
            'compact_requires':'explicit_compact_safe',
            'paint_hitbox':'same_committed_rect'}
