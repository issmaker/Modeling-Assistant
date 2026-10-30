"""Scale- and representation-aware precision policy.

A tolerance may discover candidates or describe uncertainty; it is never, by
itself, proof of an intersection.  Hard geometric predicates should perform a
refined or exact test after the broad phase.
"""
from __future__ import annotations
from dataclasses import dataclass
import math


def _finite(values):
    return tuple(float(v) for v in values if math.isfinite(float(v)))

@dataclass(frozen=True)
class PrecisionPolicy:
    scale: float
    coordinate_floor: float
    linear: float
    plane: float
    area: float
    angular: float

    def witness(self):
        return {
            'scale': self.scale,
            'coordinate_floor': self.coordinate_floor,
            'linear_tolerance': self.linear,
            'plane_tolerance': self.plane,
            'area_tolerance': self.area,
            'angular_tolerance': self.angular,
        }


def policy_from_scale(scale, *, coordinate_floor=0.0,
                      rel_linear=1e-10, min_linear=1e-12,
                      ulp_multiplier=16.0, angular=1e-10):
    s = abs(float(scale))
    if not math.isfinite(s):
        raise ValueError('PRECISION_POLICY: scale must be finite')
    s = max(s, min_linear)
    floor = abs(float(coordinate_floor)) if math.isfinite(float(coordinate_floor)) else math.inf
    linear = max(float(min_linear), s*float(rel_linear), floor*float(ulp_multiplier))
    # A plane distance aggregates several operations; keep it conservative.
    plane = max(linear*4.0, s*1e-11)
    area = max(linear*linear*4.0, s*s*1e-12, 1e-24)
    return PrecisionPolicy(s, floor, linear, plane, area, float(angular))


def policy_from_points(points, *, rel_linear=1e-10, min_linear=1e-12,
                       ulp_multiplier=16.0, angular=1e-10):
    pts=[]
    for p in points:
        q=tuple(float(v) for v in p)
        if not q or not all(math.isfinite(v) for v in q):
            raise ValueError('PRECISION_POLICY: points must be finite')
        pts.append(q)
    if not pts:
        return policy_from_scale(1.0, rel_linear=rel_linear, min_linear=min_linear,
                                 ulp_multiplier=ulp_multiplier, angular=angular)
    dims=max(len(p) for p in pts)
    mins=[math.inf]*dims; maxs=[-math.inf]*dims
    floor=0.0
    for p in pts:
        for i,v in enumerate(p):
            mins[i]=min(mins[i],v);maxs[i]=max(maxs[i],v)
            floor=max(floor,math.ulp(v))
    span=math.sqrt(sum((maxs[i]-mins[i])**2 for i in range(dims) if mins[i] < math.inf))
    # If all points coincide, use absolute coordinate magnitude only as an ULP
    # source, not as the geometric scale itself.
    scale=max(span,min_linear)
    return policy_from_scale(scale,coordinate_floor=floor,rel_linear=rel_linear,
                             min_linear=min_linear,ulp_multiplier=ulp_multiplier,
                             angular=angular)
