"""Pure geometry for profile-sweep normal orientation; usable without Blender.

A closed section has an intrinsic interior. An open section does not: closing
its endpoints is a documented convention, not a mathematically unique solid.
"""
from __future__ import annotations


def section_signed_area_xz(points):
    coords = [(float(p[0]), float(p[1])) for p in points]
    if len(coords) < 3:
        return 0.0
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1]
                     for a, b in zip(coords, coords[1:] + coords[:1]))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def quad_outward_decision(quad, frame_xy, section_edge_xz, signed_area, tolerance=1.e-9):
    """Should a path-first quad reverse winding for a closed section?

    The outward reference is the *exterior of the authored cross-section*, not
    the side on which the cornice happens to be mirrored. Returns +1, -1, or 0
    for indeterminate near-degenerate quads (handled by later validation).
    """
    if abs(signed_area) <= tolerance:
        return 0
    dx, dz = map(float, section_edge_xz)
    fx, fy = map(float, frame_xy)
    sign = 1.0 if signed_area > 0 else -1.0
    reference = (sign * fx * dz, sign * fy * dz, -sign * dx)
    a, b, _c, d = quad
    side0 = tuple(float(b[i]) - float(a[i]) for i in range(3))
    side1 = tuple(float(d[i]) - float(a[i]) for i in range(3))
    scalar = dot(cross(side0, side1), reference)
    magnitude2 = dot(reference, reference) * dot(cross(side0, side1), cross(side0, side1))
    if magnitude2 <= tolerance ** 4 or abs(scalar) <= (magnitude2 ** .5) * 1.e-7:
        return 0
    return 1 if scalar > 0 else -1
