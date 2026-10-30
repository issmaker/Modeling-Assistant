"""XY coverage invariants and deterministic triangle/UV-grid repair.

No Blender dependency: caller owns triangulation and result installation.
All coordinates remain in the same (possibly rotated) XY frame.
"""


def projected_area(points):
    """Unsigned polygon area in XY; Z is intentionally ignored."""
    if len(points) < 3:
        return 0.0
    return abs(sum(a.x * b.y - b.x * a.y for a, b in zip(points, points[1:] + points[:1]))) * 0.5


def coverage_tolerance(area, module):
    # Allow micrometre-level BMesh welding noise, not missing visible patches.
    return max(1.0e-7, module * module * 1.0e-6, area * 2.0e-5)


def coverage_delta(expected, polygons):
    return sum(projected_area(p) for p in polygons) - expected


def clip_convex_to_cell(polygon, x0, x1, y0, y1, epsilon=1.0e-10):
    """Sutherland-Hodgman clipping of a triangle/convex polygon to one tile.

    Vertex.lerp is supplied by mathutils.Vector in Blender.  No bounding-box
    intersection is guessed: each edge is clipped to the actual cell borders.
    """
    result = list(polygon)
    planes = ((0, x0, 1), (0, x1, -1), (1, y0, 1), (1, y1, -1))
    for axis, boundary, side in planes:
        if not result:
            break
        output = []
        start = result[-1]
        start_value = (start.x if axis == 0 else start.y) - boundary
        start_inside = side * start_value >= -epsilon
        for end in result:
            end_value = (end.x if axis == 0 else end.y) - boundary
            end_inside = side * end_value >= -epsilon
            if end_inside != start_inside:
                denom = start_value - end_value
                if abs(denom) > epsilon * 1.0e-3:
                    t = max(0.0, min(1.0, start_value / denom))
                    output.append(start.lerp(end, t))
            if end_inside:
                output.append(end)
            start, start_value, start_inside = end, end_value, end_inside
        result = output
    return result


def triangulated_cell_fragments(triangles, u0, v0, tile_length, epsilon=1.0e-9):
    """Exact covered fragments for one tile, including concave contour cuts."""
    x1, y1 = u0 + tile_length, v0 + tile_length
    fragments = []
    for triangle in triangles:
        if max(p.x for p in triangle) < u0 - epsilon or min(p.x for p in triangle) > x1 + epsilon:
            continue
        if max(p.y for p in triangle) < v0 - epsilon or min(p.y for p in triangle) > y1 + epsilon:
            continue
        fragment = clip_convex_to_cell(triangle, u0, x1, v0, y1, epsilon)
        if projected_area(fragment) > max(epsilon * epsilon, tile_length * tile_length * 1.0e-14):
            fragments.append(fragment)
    return fragments


def coverage_report(expected_polygon, emitted_polygons, tile_length):
    expected = projected_area(expected_polygon)
    actual = sum(projected_area(loop) for loop in emitted_polygons)
    error = actual - expected
    return {
        'expected': expected, 'actual': actual, 'delta': error,
        'tolerance': coverage_tolerance(expected, tile_length),
        'ok': abs(error) <= coverage_tolerance(expected, tile_length),
    }


def convex_overlap_area(first, second, epsilon=1e-9):
    """Area shared by convex clipped fragments (orientation-independent)."""
    if len(first)<3 or len(second)<3:
        return 0.0
    clip=list(second)
    signed=sum(a.x*b.y-b.x*a.y for a,b in zip(clip, clip[1:]+clip[:1]))
    sign=1.0 if signed>=0.0 else -1.0
    output=list(first)
    for a,b in zip(clip, clip[1:]+clip[:1]):
        if not output:
            break
        def dist(point):
            return sign*((b.x-a.x)*(point.y-a.y)-(b.y-a.y)*(point.x-a.x))
        nxt=[]; old=output[-1]; d0=dist(old)
        for new in output:
            d1=dist(new)
            if (d0>=-epsilon)!=(d1>=-epsilon):
                denom=d0-d1
                if abs(denom)>1e-20:
                    t=max(0.0,min(1.0,d0/denom))
                    nxt.append(old.lerp(new,t))
            if d1>=-epsilon:
                nxt.append(new)
            old,d0=new,d1
        output=nxt
    return projected_area(output)


def assert_nonoverlapping_fragments(fragments, area_tolerance=1e-9):
    """A duplicated tile fragment must not compensate a missing region."""
    for i in range(len(fragments)):
        for j in range(i+1,len(fragments)):
            overlap=convex_overlap_area(fragments[i],fragments[j])
            if overlap>area_tolerance:
                raise ValueError(f'Cap Surface: overlapping UV fragments {i}/{j}; overlap={overlap:.9g}')
    return True
