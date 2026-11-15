"""Shared, pure adaptive CONSTRUCTION contour for shared modeling tools.

The authored source/UV path is immutable. Only *offset directions* are
interpolated from selected anchor stations. No source Blender Mesh is edited.
All generated geometry must still pass each host tool's existing collision,
UV and non-manifold validation. This module never silently merges vertices.
"""
from __future__ import annotations

import math

SCHEMA = 'ISSMAKER_CONSTRUCTION_CONTOUR_V1'


def _point(p):
    out = tuple(float(v) for v in p)
    if len(out) != 3 or not all(math.isfinite(v) for v in out):
        raise ValueError('CONSTRUCTION_NONFINITE_POINT')
    return out


def _dist(a, b):
    return math.sqrt(sum((a[j]-b[j])**2 for j in range(3)))


def _segment_deviation(p, a, b):
    v = tuple(b[k]-a[k] for k in range(3))
    den = sum(x*x for x in v)
    if den <= 1.e-18:
        return float('inf')
    t = max(0., min(1., sum((p[j]-a[j])*v[j] for j in range(3))/den))
    q = tuple(a[j]+t*v[j] for j in range(3))
    return _dist(p, q)


def _turn_angle(a, b, c):
    x = tuple(b[k]-a[k] for k in range(3))
    y = tuple(c[k]-b[k] for k in range(3))
    nx = math.sqrt(sum(v*v for v in x))
    ny = math.sqrt(sum(v*v for v in y))
    if min(nx, ny) <= 1.e-9:
        return 180.
    return math.degrees(math.acos(max(-1., min(1.,
        sum(x[k]*y[k] for k in range(3))/(nx*ny)))))


def plan(points, *, min_edge_m=.04, max_error_m=.004,
         max_turn_deg=20., protect=()):
    """Conservative simplification of an OPEN ordered 3D construction chain.

    Short edges alone are insufficient: deletion additionally requires a
    globally bounded point-to-segment deviation, a gentle local turn, and
    no protected architectural/UV anchor. Endpoints are always protected.
    Returns deterministic indices and bounded measured error.
    """
    pts = tuple(_point(p) for p in points)
    n = len(pts)
    for key, value in [('min_edge_m',min_edge_m), ('max_error_m',max_error_m),
                       ('max_turn_deg',max_turn_deg)]:
        if not math.isfinite(float(value)) or float(value) <= 0.:
            raise ValueError('CONSTRUCTION_INVALID_'+key.upper())
    if n < 3:
        return {'schema':SCHEMA,'source_count':n,'kept_indices':list(range(n)),
                'removed_indices':[],'max_error_m':0.,'candidate_count':0,
                'status':'UNMODIFIED'}
    locked = {0,n-1}
    locked.update(int(i) for i in protect if 0 <= int(i) < n)
    # Prevent true CAD kinks from being simplified even if extremely short.
    for j in range(1,n-1):
        if _turn_angle(pts[j-1],pts[j],pts[j+1]) >= float(max_turn_deg):
            locked.add(j)
    kept = list(range(n))
    candidates=0
    # Recheck original points after each removal; never allow accumulated
    # errors across a chain of individually safe deletions to exceed tolerance.
    while True:
        accepted=None
        for pos in range(1,len(kept)-1):
            idx=kept[pos]
            if idx in locked:continue
            lo,hi=kept[pos-1],kept[pos+1]
            if min(_dist(pts[lo],pts[idx]),_dist(pts[idx],pts[hi])) >= min_edge_m:
                continue
            if _turn_angle(pts[lo],pts[idx],pts[hi]) >= max_turn_deg:
                continue
            candidates+=1
            err=max(_segment_deviation(pts[k],pts[lo],pts[hi])
                    for k in range(lo+1,hi))
            if err <= max_error_m+1.e-10:
                accepted=pos
                break
        if accepted is None:break
        kept.pop(accepted)
    removed=sorted(set(range(n))-set(kept))
    maximum=0.
    for a,b in zip(kept,kept[1:]):
        for i in range(a+1,b):
            maximum=max(maximum,_segment_deviation(pts[i],pts[a],pts[b]))
    if maximum>max_error_m+1.e-8:
        raise ValueError('CONSTRUCTION_ERROR_BUDGET_EXCEEDED')
    return {'schema':SCHEMA,'source_count':n,'kept_indices':kept,
            'removed_indices':removed,'max_error_m':float(maximum),
            'min_edge_m':float(min_edge_m),'budget_m':float(max_error_m),
            'candidate_count':candidates,
            'status':'SIMPLIFIED' if removed else 'UNMODIFIED'}


def interpolate_vectors(points, original_vectors, result):
    """Preserve anchor directions; ignore intermediate micro-kink directions.

    The returned directions correspond to EVERY original source vertex; no
    topology is removed or welded. Endpoints of the original wall are fixed.
    """
    pts=tuple(_point(p) for p in points)
    vecs=tuple(_point(v) for v in original_vectors)
    if len(pts)!=len(vecs) or int(result['source_count'])!=len(pts):
        raise ValueError('CONSTRUCTION_DIRECTION_SIZE_MISMATCH')
    values=list(vecs)
    for a,b in zip(result['kept_indices'],result['kept_indices'][1:]):
        length=_dist(pts[a],pts[b])
        if length<=1.e-9:continue
        segment=tuple(pts[j] for j in range(a,b+1))
        for i in range(a+1,b):
            # cumulative parameter respects original authored arc spacing,
            # not an artificial vertex index or floating point ordinal.
            cumulative=sum(_dist(segment[j],segment[j+1]) for j in range(i-a))
            total=sum(_dist(segment[j],segment[j+1]) for j in range(b-a))
            t=min(1.,max(0.,cumulative/total)) if total>1.e-12 else (i-a)/(b-a)
            values[i]=tuple(vecs[a][k]*(1.-t)+vecs[b][k]*t for k in range(3))
    return [list(v) for v in values]


def bounded_offset_vectors(original_vectors, proposed_vectors, depth_m, max_adjust_m=.005):
    """Limit displacement of a generated rear profile from its legacy offset.

    Contract: max(abs(depth)*length(new-old)) <= max_adjust_m for every
    source station, including unusually deep 1-2 metre extrusions.
    """
    depth=abs(float(depth_m))
    if not math.isfinite(depth) or not math.isfinite(max_adjust_m) or max_adjust_m <= 0.:
        raise ValueError('CONSTRUCTION_INVALID_DEPTH_OR_ADJUSTMENT')
    if len(original_vectors)!=len(proposed_vectors):
        raise ValueError('CONSTRUCTION_OFFSET_SIZE_MISMATCH')
    out=[];maximum=0.
    for a,b in zip(original_vectors,proposed_vectors):
        u=_point(a);v=_point(b)
        difference=tuple(v[k]-u[k] for k in range(3))
        full=math.sqrt(sum(d*d for d in difference))*depth
        factor=min(1.0,max_adjust_m/full) if full>0. else 1.0
        changed=tuple(u[k]+difference[k]*factor for k in range(3))
        final=_dist(changed,u)*depth
        maximum=max(maximum,final)
        out.append(list(changed))
    if maximum>max_adjust_m+1.e-8:
        raise ValueError('CONSTRUCTION_DISPLACEMENT_LIMIT_VIOLATED')
    return out,maximum
