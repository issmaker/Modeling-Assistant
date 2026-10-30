"""Compatibility facade for the shared ISSMAKER Reliability Foundation.

BH0472 introduced these predicates locally. RF1 keeps the public Brick Hole
imports stable while moving the implementation into ``core.reliability`` so
Collision Inspector, DWOS and UV Wall can use the same mathematical contract.
"""
from __future__ import annotations
import math
from ...core.reliability.predicates import (
    exact_segment_contact_3d, polygon_area_vector, localize_points,
)
from ...core.reliability.decision import VALID, PROVEN_INVALID as INVALID, UNCERTAIN


def coordinate_resolution(points):
    values=[]
    for p in points:
        for value in p:
            v=float(value)
            if math.isfinite(v):values.append(abs(v))
    if not values:return math.inf
    return max(math.ulp(v) for v in values)

__all__=['VALID','INVALID','UNCERTAIN','exact_segment_contact_3d',
         'polygon_area_vector','localize_points','coordinate_resolution']
