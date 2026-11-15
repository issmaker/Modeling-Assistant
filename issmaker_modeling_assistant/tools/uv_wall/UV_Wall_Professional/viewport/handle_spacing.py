"""Screen-space safety for WALL thickness/position handles.

The authored 3D anchors, normal, parameter and drag distance are never moved.
Only the displayed thickness marker and its mouse hitbox may move.
If safe placement is impossible, publish NO overlapping thickness hitbox;
the controller preserves the editable thickness card instead.
"""
import math


def _point(value):
    try:
        x, y = (float(value[0]), float(value[1]))
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return (x, y)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def separate_handle_pair(offset, thickness, width, height, min_gap=40.,
                         pad=18., obstacles=(), offset_radius=16.,
                         thickness_radius=15., clearance=8.):
    """Return (safe thickness xy, moved) without overlapping other drag zones.

    Obstacles are an iterable of (center_xy, hit_radius).
    The fixed OFFSET handle is never displaced. A failed placement returns
    None rather than retaining an ambiguous overlapping drag target.
    """
    if offset is None or thickness is None:
        return thickness, False
    anchor, original = _point(offset), _point(thickness)
    if anchor is None or original is None:
        return None, False
    try:
        width, height = float(width), float(height)
        pad, min_gap = float(pad), float(min_gap)
        offset_radius, thickness_radius = float(offset_radius), float(thickness_radius)
        clearance = float(clearance)
    except (ValueError, TypeError, OverflowError):
        return None, False
    numeric = (width, height, pad, min_gap, offset_radius, thickness_radius, clearance)
    if not all(map(math.isfinite, numeric)) or min(numeric[2:]) < 0:
        return None, False
    pad = max(pad, thickness_radius + 4.)
    min_gap = max(33., min_gap, offset_radius + thickness_radius + clearance)
    if width <= pad*2 or height <= pad*2:
        return None, False
    blockers = []
    try:
        for center, radius in obstacles:
            pt = _point(center)
            r = float(radius)
            if pt is not None and math.isfinite(r) and r >= 0:
                blockers.append((pt, r))
    except (TypeError, ValueError):
        return None, False

    def safe(pos):
        if not (pad <= pos[0] <= width-pad and pad <= pos[1] <= height-pad):
            return False
        if math.dist(pos, anchor) + 1.e-7 < min_gap:
            return False
        return all(math.dist(pos, center) + 1.e-7 >= thickness_radius + radius + clearance
                   for center, radius in blockers)

    if safe(original):
        return original, False
    dx, dy = original[0]-anchor[0], original[1]-anchor[1]
    angle = math.atan2(dy,dx) if math.hypot(dx,dy) > 1.e-7 else math.pi/2.
    # Dense search supports constrained viewports and avoids neighboring gizmos.
    candidates = []
    for step in range(1,17):
        radius = min_gap + (step-1)*8.
        for index in range(48):
            theta = angle + index*math.pi/24.
            candidate = (anchor[0]+radius*math.cos(theta),
                         anchor[1]+radius*math.sin(theta))
            if safe(candidate):
                deviation = math.dist(candidate, original)
                candidates.append((round(deviation,6),radius,index,candidate))
        if candidates and step>=3:
            break
    if not candidates:
        return None, False
    best = min(candidates,key=lambda item:item[:3])[3]
    return best, True
