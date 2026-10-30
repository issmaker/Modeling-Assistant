"""Pure UV preview geometry helpers; no bpy dependencies."""


def bounded_outlines(polygons, face_limit=400, segment_limit=4800):
    """Get immutable, bounded polygon outlines. Never alter selection.

    Input: iterable of (face_index, uv_vertices). Each UV vertex is (u, v).
    An over-sized polygon is skipped, never partially rendered.
    Returns (outlines, total_faces, omitted_faces).
    """
    items = tuple(polygons)
    outlines = []
    segments = 0
    for face_index, points in items:
        if len(outlines) >= face_limit:
            break
        pts = tuple((float(x), float(y)) for x, y in points)
        if len(pts) < 3 or len(pts) + segments > segment_limit:
            break
        outlines.append((int(face_index), pts))
        segments += len(pts)
    return tuple(outlines), len(items), len(items) - len(outlines)


def outline_segments(outlines):
    """Yield UV-space segments without closing gaps between distinct islands."""
    for _index, points in outlines:
        for i, a in enumerate(points):
            yield a, points[(i + 1) % len(points)]


def face_points_equal(expected, current):
    """Fail closed when UV or source-vertex coordinates/topology change."""
    return expected == current
