"""Conservative 2D broadphase for wall-path validation (Blender independent).

Only a true intersection test can reject a path. We never conclude that two
segments intersect here; we only skip pairs whose XY bounding boxes provably
do not overlap. If the index cannot be constructed within fixed budgets, the
caller must run the original exhaustive validator.
"""
from math import floor, hypot, isfinite, sqrt


def candidate_pairs_xy(points, *, min_segments=96, max_index_entries=900_000,
                       max_pairs=900_000, max_cells_per_segment=2048):
    """Return ordered (i,j) candidate pairs, or None for exact-scan fallback.

    Bounding boxes include both endpoints, so every *proper* crossing appears
    in at least one common cell. Closed-ring adjacency is filtered by caller.
    Return value is sorted lexicographically, preserving legacy error order.
    No Blender API, side effects, or changes to source coordinates.
    """
    n = len(points) - 1
    if n < min_segments:
        return None
    try:
        xy = [(float(p.x), float(p.y)) if hasattr(p, 'x')
              else (float(p[0]), float(p[1])) for p in points]
        if not all(isfinite(x) and isfinite(y) for x, y in xy):
            return None
        xs, ys = zip(*xy)
        extent_x = max(xs) - min(xs)
        extent_y = max(ys) - min(ys)
        total_length = sum(hypot(x1-x0, y1-y0)
                           for (x0,y0),(x1,y1) in zip(xy,xy[1:]))
        average_length = total_length / n
        if not isfinite(average_length) or average_length <= 0.0:
            return None
        # Grid size adapts to linear, nearly collinear, and 2D contours.
        # It stays positive even for a path with zero XY bounding area.
        cell = max(average_length * .25,
                   sqrt((extent_x+average_length)*(extent_y+average_length)/n))
        if not isfinite(cell) or cell <= 0.0:
            return None
        origin_x = min(xs)
        origin_y = min(ys)
        buckets = {}
        pairs = set()
        inserted = 0
        for j, ((x0,y0),(x1,y1)) in enumerate(zip(xy,xy[1:])):
            left = floor((min(x0,x1)-origin_x)/cell)
            right = floor((max(x0,x1)-origin_x)/cell)
            bottom = floor((min(y0,y1)-origin_y)/cell)
            top = floor((max(y0,y1)-origin_y)/cell)
            cells = (right-left+1)*(top-bottom+1)
            if cells > max_cells_per_segment or inserted+cells > max_index_entries:
                return None
            for gx in range(left,right+1):
                for gy in range(bottom,top+1):
                    key = (gx,gy)
                    earlier = buckets.get(key)
                    if earlier:
                        for i in earlier:
                            # AABB is only a broadphase, final exact test is
                            # still the original _segments_intersect_2d.
                            pairs.add((i,j))
                            if len(pairs)>max_pairs:
                                return None
                        earlier.append(j)
                    else:
                        buckets[key] = [j]
            inserted += cells
        return sorted(pairs)
    except (OverflowError, MemoryError, ValueError, TypeError, ZeroDivisionError):
        # Conservative fallback: never interpret failure as 'no crossings'.
        return None
