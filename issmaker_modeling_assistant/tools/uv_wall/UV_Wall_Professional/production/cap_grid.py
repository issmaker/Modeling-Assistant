"""Bounded quadrilateral remeshing for the cap ribbon, independent of Blender.

Every output cell is a bilinear restriction of the original authored four-sided
patch. The outer/inner borders are preserved exactly, including true corners.
UV module clipping happens AFTER refinement: no additional texture scaling.
"""
from math import ceil, isfinite


def refine_ribbon_patch(patch, tile_length, target_fraction=0.50, max_cells=20000):
    """Return locally quad-dominant ribbon cells; refuse unbounded subdivision.

    In particular, an entire long miter strip must never become one giant ngon
    after face-local T-junction stitching.  Truly concave authored patches are
    left unsplit for the existing coverage and topology validators to handle.
    """
    if len(patch) != 4 or not isfinite(float(tile_length)) or tile_length <= 0:
        raise ValueError('Cap ribbon: invalid patch or tile length')
    a, b, c, d = patch  # outer 0..1, inner 1..0
    xy = lambda point: (float(point.x), float(point.y))
    def xy_dist(first, second):
        x1, y1 = xy(first)
        x2, y2 = xy(second)
        return ((x2-x1)**2+(y2-y1)**2)**0.5
    def cross(first, middle, last):
        ax, ay = xy(first); bx, by = xy(middle); cx, cy = xy(last)
        return (bx-ax)*(cy-by)-(by-ay)*(cx-bx)
    turns = [cross(a,b,c), cross(b,c,d), cross(c,d,a), cross(d,a,b)]
    # Conservative: do not fan or bilinearly fold nonconvex corner fragments.
    if min(turns) < -1e-9 and max(turns) > 1e-9:
        return [list(patch)]
    target = max(tile_length * target_fraction, 1e-7)
    along = max(1, ceil(max(xy_dist(a,b), xy_dist(d,c)) / target))
    across = max(1, ceil(max(xy_dist(a,d), xy_dist(b,c)) / target))
    if along * across > max_cells:
        raise ValueError('Cap ribbon: subdivision safety limit exceeded')
    def point(t, s):
        return a.lerp(b, t).lerp(d.lerp(c, t), s)
    return [
        [point(t0, s0), point(t1, s0), point(t1, s1), point(t0, s1)]
        for i in range(along) for j in range(across)
        for t0, t1 in [(i/along, (i+1)/along)]
        for s0, s1 in [(j/across, (j+1)/across)]
    ]
