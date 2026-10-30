"""Slope-safe wall station seams; XY modular metrics never use spatial arc length.

A near-coincident authored corner and nominal module cut used to create a
sub-millimetre wall sliver. The 2mm production weld subsequently destroyed it,
causing intermittent holes/invalid faces on complex elevated paths. Preserve
authored corners exactly and snap the *UV seam* to a nearby authored station.
No source point is moved, and no offset/geometry topology is approximated.
"""
from bisect import bisect_left
from math import isfinite, floor

WELD_CLEARANCE = 0.002


def modular_stations(authored, tile_length, clearance=WELD_CLEARANCE):
    if not isfinite(tile_length) or tile_length <= 0:
        raise ValueError("Wall module length must be finite and positive")
    if not authored or not all(isfinite(float(d)) for d in authored):
        raise ValueError("Wall stations must be finite")
    if any(b <= a for a, b in zip(authored, authored[1:])):
        raise ValueError("Wall XY stations must be strictly increasing")
    total = authored[-1]
    if total <= 0:
        raise ValueError("Wall XY length must be positive")
    stations = list(authored)
    for index in range(1, int(floor((total - 1.e-9) / tile_length)) + 1):
        pos = index * tile_length
        j = bisect_left(authored, pos)
        neighbors = authored[max(0,j-1):min(len(authored),j+1)]
        if any(abs(pos - corner) < clearance for corner in neighbors):
            continue
        stations.append(pos)
    return sorted(stations)


def uv_tile_origin(distance, tile_length, clearance=WELD_CLEARANCE):
    """Anchor a snapped corner as a UV seam; avoid wraparound at ~k*tile."""
    near_index = int(round(distance / tile_length))
    if near_index >= 0 and abs(distance - near_index*tile_length) < clearance:
        return near_index * tile_length
    return int(floor((distance + 1.e-6)/tile_length)) * tile_length


def slope_diagnostics(points, xy_min=1.e-6):
    """Numerical diagnostics; steepness itself is never a rejection criterion."""
    if len(points) < 2:
        raise ValueError("Wall path needs at least two points")
    from math import hypot
    total=0.0; max_grade=0.0; max_jump=0.0
    for index,(a,b) in enumerate(zip(points,points[1:]),1):
        values=(float(a.x),float(a.y),float(a.z),float(b.x),float(b.y),float(b.z))
        if not all(isfinite(v) for v in values):
            raise ValueError(f"Wall path segment {index}: non-finite XYZ")
        dx=hypot(b.x-a.x,b.y-a.y)
        if dx<=xy_min:
            raise ValueError(f"Wall path segment {index}: coincident XY, cannot construct a vertical facade")
        delta=abs(b.z-a.z)
        total+=dx;max_grade=max(max_grade,delta/dx);max_jump=max(max_jump,delta)
    return {"horizontal_m":total,"max_grade":max_grade,"max_z_jump_m":max_jump,
            "z_min":min(p.z for p in points),"z_max":max(p.z for p in points)}
