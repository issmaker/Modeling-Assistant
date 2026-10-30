"""ISSMAKER Pass 12 quality policy and analytic 2D antialias geometry.

Pure Python by design: usable in tests and before Blender registration.
Triangle strips use a solid inner core plus transparent fringe. Blender's
SMOOTH_COLOR vertex shader interpolates alpha per-pixel; no bitmap upscaling.
All generated vertices stay strictly inside the caller's paint rectangle.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import hypot, isfinite

PRESETS = {
    'ULTRA': (2.0, 1.55, 160),
    'HIGH': (1.6, 1.20, 112),
    'BALANCED': (1.15, 1.0, 80),
    'PERFORMANCE': (1.0, .85, 56),
}
DEFAULT = 'HIGH'

@dataclass(frozen=True)
class QualityPolicy:
    name: str
    detail: float
    aa_fringe_px: float
    max_cached_patterns: int


def resolve(value):
    key=str(value or DEFAULT).upper()
    if key not in PRESETS:key=DEFAULT
    detail,fringe,limit=PRESETS[key]
    return QualityPolicy(key,detail,fringe,limit)


def _clip(x0,y0,x1,y1,rect):
    x,y,w,h=rect
    lo,hi=0.,1.;dx=x1-x0;dy=y1-y0
    for p,q in ((-dx,x0-x),(dx,x+w-x0),(-dy,y0-y), (dy,y+h-y0)):
        if abs(p)<1e-10:
            if q<0:return None
            continue
        t=q/p
        if p<0:lo=max(lo,t)
        else:hi=min(hi,t)
        if lo>hi:return None
    return x0+lo*dx,y0+lo*dy,x0+hi*dx,y0+hi*dy


def smooth_stroke_mesh(segments, rect, *, width=1.0, fringe=1.1):
    """Return (pos,color) for a triangle batch; RGB white, alpha per vertex.

    Segment tuples: (x0,y0,x1,y1,opacity). Clip to an inset rectangle so
    anti-aliased fringes cannot paint outside the panel. No NaN/Inf accepted.
    """
    x,y,w,h=(float(v) for v in rect)
    width=float(width);fringe=float(fringe)
    if not all(isfinite(v) for v in (x,y,w,h,width,fringe)) or w<4 or h<4 or not .2<=width<=5 or not .25<=fringe<=3:
        raise ValueError('Invalid anti-alias geometry bounds')
    outer=width*.5+fringe
    safe=(x+outer,y+outer,w-2*outer,h-2*outer)
    if safe[2]<=0 or safe[3]<=0:return (),()
    verts=[];colors=[]
    # Six vertices per band, three bands; alpha coverage falls to zero at both edges.
    for item in segments:
        x0,y0,x1,y1,opacity=item
        if not all(isfinite(float(v)) for v in item):continue
        cut=_clip(float(x0),float(y0),float(x1),float(y1),safe)
        if cut is None:continue
        a,b,c,d=cut;dx=c-a;dy=d-b;length=hypot(dx,dy)
        if length<.1:continue
        nx=-dy/length;ny=dx/length
        alpha=max(0.,min(1.,float(opacity)))
        bands=((-outer,0.),(-width*.5,alpha),(width*.5,alpha),(outer,0.))
        for (offset0,opacity0),(offset1,opacity1) in zip(bands,bands[1:]):
            points=((a+nx*offset0,b+ny*offset0),(c+nx*offset0,d+ny*offset0),
                    (c+nx*offset1,d+ny*offset1),(a+nx*offset1,b+ny*offset1))
            for idx in (0,1,2,0,2,3):
                verts.append(points[idx]);colors.append((1.,1.,1.,opacity0 if idx in (0,1) else opacity1))
    return tuple(verts),tuple(colors)
