"""Read-only UV polygon winding for ISSMAKER's 'Найти Flip UV'.

This is a UV orientation detector (Zen UV-style winding convention), NOT a
normal validator or permission to auto-mirror UV. It works on polygon loops
without changing Blender topology. Folded UV polygons are never classified
as simply normal or flipped. O(n**2) self-cross checks are bounded.
"""
from math import isfinite


def classify_uv_polygon(points, *, absolute_tolerance=1e-14, max_sides=128, triangles=None):
    """Return 'FLIPPED', 'NORMAL', 'DEGENERATE', or 'UNVERIFIED'.

    Signed orientation matches the conventional winding detector: CW ->
    FLIPPED, CCW -> NORMAL. It is intentionally not role-aware: wall facade
    UV may be intentionally clockwise and still be correct for the material.
    """
    try:
        points = tuple((float(p[0]), float(p[1])) for p in points)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 'UNVERIFIED'
    n = len(points)
    if n < 3 or n > max_sides or not all(isfinite(v) for p in points for v in p):
        return 'UNVERIFIED'
    u0, v0 = points[0]
    local = tuple((u-u0, v-v0) for u, v in points)
    span = max(max(abs(u), abs(v)) for u,v in local)
    if not isfinite(span) or span <= 1e-14:
        return 'DEGENERATE'
    eps = max(absolute_tolerance, (span * span) * 1e-12)
    cross = lambda a,b,c: (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    if any((local[i][0]-local[(i+1)%n][0])**2 +
           (local[i][1]-local[(i+1)%n][1])**2 <= 1e-24 for i in range(n)):
        return 'DEGENERATE'

    def on_segment(a,b,c):
        return (min(a[0],b[0])-1e-12 <= c[0] <= max(a[0],b[0])+1e-12 and
                min(a[1],b[1])-1e-12 <= c[1] <= max(a[1],b[1])+1e-12)

    for i in range(n):
        a, b = local[i], local[(i+1)%n]
        for j in range(i+1,n):
            if j == i+1 or (i == 0 and j == n-1):
                continue
            c, d = local[j], local[(j+1)%n]
            ab_c, ab_d = cross(a,b,c), cross(a,b,d)
            cd_a, cd_b = cross(c,d,a), cross(c,d,b)
            if ((ab_c > eps and ab_d < -eps or ab_c < -eps and ab_d > eps) and
                (cd_a > eps and cd_b < -eps or cd_a < -eps and cd_b > eps)):
                return 'UNVERIFIED'  # crossed/folded UV
            if ((abs(ab_c) <= eps and on_segment(a,b,c)) or
                (abs(ab_d) <= eps and on_segment(a,b,d)) or
                (abs(cd_a) <= eps and on_segment(c,d,a)) or
                (abs(cd_b) <= eps and on_segment(c,d,b))):
                return 'UNVERIFIED'  # touching, coincident, ambiguous
    area2 = sum(local[i][0]*local[(i+1)%n][1] -
                local[(i+1)%n][0]*local[i][1] for i in range(n))
    if not isfinite(area2):
        return 'UNVERIFIED'
    if abs(area2) <= eps:
        return 'DEGENERATE'
    # Match ZenUV.utils.generic.is_face_flipped exactly for valid polygons.
    # Corner turning and polygon area need not agree on highly concave ngons.
    corner_sum = sum(
        (local[(i+1)%n][0]-local[i][0])*(local[i][1]-local[(i-1)%n][1]) -
        (local[(i+1)%n][1]-local[i][1])*(local[i][0]-local[(i-1)%n][0])
        for i in range(n))
    result = 'FLIPPED' if corner_sum >= 0 else 'NORMAL'
    if triangles is not None:
        rendered = classify_uv_triangles(triangles, absolute_tolerance=absolute_tolerance)
        if rendered in {'DEGENERATE','UNVERIFIED'}:return rendered
        if rendered != result:return 'UNVERIFIED'
    return result


def classify_uv_triangles(triangles, *, absolute_tolerance=1e-14):
    """Uniform orientation of actual render triangles; folds are unverified."""
    signs=set();degenerate=False
    for tri in triangles:
        try:
            a,b,c=tuple((float(p[0]),float(p[1])) for p in tri)
            if not all(isfinite(v) for p in (a,b,c) for v in p):return 'UNVERIFIED'
        except (ValueError,TypeError,IndexError):return 'UNVERIFIED'
        area=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        scale=max(abs(b[0]-a[0]),abs(b[1]-a[1]),abs(c[0]-a[0]),abs(c[1]-a[1]))
        if abs(area)<=max(absolute_tolerance,scale*scale*1e-12):degenerate=True
        else:signs.add(area>0)
    if len(signs)>1:return 'UNVERIFIED'
    if degenerate or not signs:return 'DEGENERATE'
    return 'NORMAL' if True in signs else 'FLIPPED'


def mesh_uv_states(mesh):
    """Read-only face states using Blender's loop triangles and active UV map."""
    layer=mesh.uv_layers.active
    if layer is None:return {}
    mesh.calc_loop_triangles()
    grouped={}
    for tri in mesh.loop_triangles:
        grouped.setdefault(tri.polygon_index,[]).append([layer.data[i].uv for i in tri.loops])
    return {p.index:classify_uv_polygon([layer.data[i].uv for i in p.loop_indices],
                triangles=grouped.get(p.index,())) for p in mesh.polygons}


def bmesh_face_uv_state(face,layer):
    from mathutils.geometry import tessellate_polygon
    loops=list(face.loops);coords=[loop[layer].uv for loop in loops]
    geo=[loop.vert.co.copy() for loop in loops]
    triangles=tessellate_polygon([geo])
    uv_triangles=[]
    for tri in triangles:
        indices=[v if isinstance(v,int) else geo.index(v) for v in tri]
        uv_triangles.append([coords[i] for i in indices])
    return classify_uv_polygon(coords,triangles=uv_triangles)
