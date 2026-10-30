"""Read-only surface orientation oracle shared by every generated Mesh.

UV signed winding is NOT a defect verdict: intentionally mirrored materials can
be valid. Only source triangles have definitive winding. Quads are inspected
using both *virtual* diagonals, but never promoted to a verified orientation.
Object negative determinant affects the 3D winding, NOT UV data.
"""
from __future__ import annotations
import math


def _point(p, size):
    try:
        p = tuple(float(x) for x in p)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('SURFACE_INVALID_COORDINATES') from exc
    if len(p) != size or not all(map(math.isfinite, p)):
        raise ValueError('SURFACE_NONFINITE_COORDINATES')
    return p


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _triangle(coords, uv, indices):
    a, b, c = indices
    p, q, r = (coords[i] for i in (a, b, c))
    u, v, w = (uv[i] for i in (a, b, c))
    e1 = tuple(q[i]-p[i] for i in range(3))
    e2 = tuple(r[i]-p[i] for i in range(3))
    # Scaled products avoid overflow for large, valid coordinates.
    span = max((abs(x) for e in (e1, e2) for x in e), default=0.)
    if not math.isfinite(span) or span == 0.:
        return 'DEGENERATE', 'DEGENERATE', None
    n = _cross(tuple(x/span for x in e1), tuple(x/span for x in e2))
    mag = math.hypot(*n)
    if not math.isfinite(mag) or mag <= 1e-14:
        return 'DEGENERATE', 'DEGENERATE', None
    n = tuple(x/mag for x in n)
    du = (v[0]-u[0], v[1]-u[1]); dv = (w[0]-u[0], w[1]-u[1])
    uvspan = max(abs(x) for e in (du, dv) for x in e)
    if not math.isfinite(uvspan) or uvspan == 0.:
        return 'OK', 'DEGENERATE', n
    uvdet = (du[0]/uvspan)*(dv[1]/uvspan)-(du[1]/uvspan)*(dv[0]/uvspan)
    if not math.isfinite(uvdet) or abs(uvdet) < 1e-14:
        return 'OK', 'DEGENERATE', n
    return 'OK', 'POSITIVE' if uvdet > 0 else 'NEGATIVE', n


def face_orientation(coords, uv):
    """Surface 3D/UV checks without Blender, no geometry edits.

    For quad, check triangles for BOTH diagonals and report advisory counts.
    For n-gons, virtual fan is insufficient for concave polygons => unknown.
    """
    coords = tuple(_point(p, 3) for p in coords)
    uv = tuple(_point(p, 2) for p in uv)
    if len(coords) != len(uv) or len(coords) < 3:
        raise ValueError('SURFACE_INVALID_FACE')
    if len(coords) == 3:
        tris = ((0, 1, 2),)
    elif len(coords) == 4:
        tris = ((0, 1, 2), (0, 2, 3), (0, 1, 3), (1, 2, 3))
    else:
        return {'confidence': 'NOT_TRIANGULATED', 'uv': 'UNVERIFIED_NGON',
                'normal': 'UNVERIFIED_NGON', 'virtual_triangles': 0}
    results = tuple(_triangle(coords, uv, tri) for tri in tris)
    normals = tuple(n for state, uv_state, n in results if n is not None)
    if any(state != 'OK' for state, _, _ in results):
        norm = 'DEGENERATE_OR_FOLDED'
    elif any(sum(a[i]*b[i] for i in range(3)) < 0.9998476951563913
             for a in normals for b in normals):  # >1° difference
        norm = 'NONPLANAR_OR_FOLDED'
    else:
        norm = 'CONSISTENT'
    uv_states = {state for _, state, _ in results}
    if len(uv_states) == 1:
        uv_state = uv_states.pop()
    else:
        uv_state = 'FOLDED_OR_DEGENERATE'
    return {'confidence': 'TRIANGLE' if len(coords) == 3 else 'VIRTUAL_ONLY',
            'uv': uv_state, 'normal': norm,
            'virtual_triangles': len(tris)}


def mirrored_transform(matrix3):
    """Return determinant sign; never infer a UV defect from a mirror transform."""
    r = tuple(_point(row, 3) for row in matrix3)
    if len(r) != 3:
        raise ValueError('SURFACE_INVALID_TRANSFORM')
    det = (r[0][0]*(r[1][1]*r[2][2]-r[1][2]*r[2][1])
           - r[0][1]*(r[1][0]*r[2][2]-r[1][2]*r[2][0])
           + r[0][2]*(r[1][0]*r[2][1]-r[1][1]*r[2][0]))
    if not math.isfinite(det) or abs(det) < 1e-30:
        raise ValueError('SURFACE_SINGULAR_TRANSFORM')
    return 'MIRRORED' if det < 0 else 'REGULAR'
