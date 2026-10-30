"""Strict, Blender-independent coverage verification for a tessellated UV reveal.

An n-gon with redundant collinear boundary vertices can validly tessellate to
fewer than n-2 triangles. Accept only if all missing vertices are redundant in
BOTH world space and UV, and triangles form exactly one oriented face partition.
Never create/repair triangles or modify source geometry/UV here.
"""
from __future__ import annotations

import math
from decimal import Decimal, localcontext
from collections import Counter


class TessellationCoverageError(ValueError):
    """Failed proof that Blender's tessellation covers the source polygon."""


def _distance(a, b):
    return math.dist(a, b)


def _sub(a, b):
    return tuple(float(x)-float(y) for x, y in zip(a, b))


def _cross2(a, b, c):
    x, y = _sub(b, a)
    u, v = _sub(c, a)
    return x*v-y*u


def _signed_area(points):
    """Translation-stable signed area of the *stored* 2D vertices.

    The absolute-coordinate shoelace loses small faces at large CAD offsets:
    a 4x4 face translated by 1e9 can be reported as zero area. Anchor all
    terms on the same vertex and sum with math.fsum instead.
    """
    anchor = points[0]
    return 0.5 * math.fsum(
        (points[i][0]-anchor[0]) * (points[i+1][1]-anchor[1]) -
        (points[i+1][0]-anchor[0]) * (points[i][1]-anchor[1])
        for i in range(1, len(points)-1)
    )


def _point_on_segment(point, a, b, tolerance):
    d = _sub(b, a)
    squared = sum(v*v for v in d)
    if squared <= tolerance*tolerance:
        return False, None
    v = _sub(point, a)
    t = sum(v[i]*d[i] for i in range(len(d)))/squared
    if t < -1.0e-8 or t > 1.0+1.0e-8:
        return False, t
    foot = tuple(a[i]+t*d[i] for i in range(len(a)))
    return _distance(foot, point) <= tolerance, t


def _project(world):
    """Project from a LOCAL origin using an anchored area-vector normal.

    Newell sums in absolute coordinates lose the face normal under large
    translations. The result is local 2D coordinates so all subsequent
    area, contact and triangulation operations are translation stable.
    """
    anchor = world[0]
    rel = [tuple(p[k] - anchor[k] for k in range(3)) for p in world]
    vectors = [
        (rel[i][1]*rel[i+1][2] - rel[i][2]*rel[i+1][1],
         rel[i][2]*rel[i+1][0] - rel[i][0]*rel[i+1][2],
         rel[i][0]*rel[i+1][1] - rel[i][1]*rel[i+1][0])
        for i in range(1, len(rel)-1)
    ]
    normal = tuple(math.fsum(v[k] for v in vectors) for k in range(3))
    magnitude = math.hypot(*normal)
    spans = [max(v[k] for v in rel)-min(v[k] for v in rel) for k in range(3)]
    scale = math.hypot(*spans)
    # No implicit one-meter scale: centimeter/mm panels are valid geometry.
    if scale == 0 or magnitude <= max(1e-28, 1e-14 * scale * scale):
        raise TessellationCoverageError('AREA_ZERO: невозможно определить плоскость грани')
    if max(abs(math.fsum(v[k]*normal[k] for k in range(3))) / magnitude
           for v in rel) > max(1e-12, 1e-6 * scale):
        raise TessellationCoverageError('NONPLANAR: полигон после UV-разреза не лежит в одной плоскости')
    drop = max(range(3), key=lambda k: abs(normal[k]))
    axes = tuple(k for k in range(3) if k != drop)
    return [(v[axes[0]], v[axes[1]]) for v in rel]


def _exact_segment_contact(a, b, c, d):
    """Exact predicate for the *stored binary floats* of two 2D segments.

    Signed-distance tolerances are suitable for finding a candidate, not for
    proving a topological contact: e.g. a BMesh float32 endpoint lying 1e-8 m
    above an edge is NEAR it, but not ON it. An incorrect positive here used
    to abort otherwise fully covered collinear five-loop reveal polygons.
    Decimal.from_float preserves the exact binary value; a high precision
    context is used only for this uncommon ambiguous case.
    """
    with localcontext() as ctx:
        ctx.prec = 100
        A, B, C, D = (tuple(Decimal.from_float(float(v)) for v in point)
                      for point in (a, b, c, d))

        def orientation(p, q, r):
            return ((q[0]-p[0])*(r[1]-p[1]) -
                    (q[1]-p[1])*(r[0]-p[0]))

        def sign(v):
            return (v > 0) - (v < 0)

        def on_segment(p, q, r):
            return (min(p[0],r[0]) <= q[0] <= max(p[0],r[0])
                    and min(p[1],r[1]) <= q[1] <= max(p[1],r[1]))

        o1, o2 = sign(orientation(A,B,C)), sign(orientation(A,B,D))
        o3, o4 = sign(orientation(C,D,A)), sign(orientation(C,D,B))
        if o1*o2 < 0 and o3*o4 < 0:
            return True
        return ((o1==0 and on_segment(A,C,B)) or
                (o2==0 and on_segment(A,D,B)) or
                (o3==0 and on_segment(C,A,D)) or
                (o4==0 and on_segment(C,B,D)))


def _segments_conflict(a, b, c, d, tolerance):
    """True contact/crossing, not tolerance-only proximity of cut vertices.

    Use fast disjoint bounding-box pruning, then the exact-predicate oracle.
    Coverage, edge lengths, area and both world/UV collinearity remain separate
    mandatory invariants; proximity is NEVER used to waive those checks.
    """
    if (max(a[0], b[0]) < min(c[0], d[0]) or
        max(c[0], d[0]) < min(a[0], b[0]) or
        max(a[1], b[1]) < min(c[1], d[1]) or
        max(c[1], d[1]) < min(a[1], b[1])):
        return False
    return _exact_segment_contact(a,b,c,d)


def verify_collinear_tessellation(world_coords, uv_coords, tessellation, resolution, *,
                                  uv_tolerance_pixels=0.25, diagnostics=None):
    """Return indices when a short tessellation is provably valid.

    Verify world planar/simple polygon, nondegenerate CCW/CW triangular coverage,
    triangle edge multiplicity and boundary equality, area conservation, and
    that every skipped boundary vertex is linear in both 3D and UV.
    No change to geometry/UV is performed. This is *not* a generic n-gon repair.
    """
    n = len(world_coords)
    if n < 4 or n != len(uv_coords) or n > 256:
        raise TessellationCoverageError('INPUT: неверное количество вершин или UV')
    if not isinstance(resolution, (int,float)) or not math.isfinite(resolution) or resolution <= 0:
        raise TessellationCoverageError('INPUT: неверное разрешение')
    world = [tuple(map(float, p)) for p in world_coords]
    uv = [tuple(map(float, p)) for p in uv_coords]
    if any(len(p)!=3 or not all(math.isfinite(x) for x in p) for p in world) or any(
            len(p)!=2 or not all(math.isfinite(x) for x in p) for p in uv):
        raise TessellationCoverageError('INPUT: отсутствует конечная 3D/UV координата')
    tris = []
    for raw in tessellation:
        if len(raw)!=3 or any(type(k) is not int or k<0 or k>=n for k in raw) or len(set(raw))!=3:
            raise TessellationCoverageError('INDEX: недопустимый треугольник Blender')
        tris.append(tuple(raw))
    if not 1 <= len(tris) < n-2:
        raise TessellationCoverageError('COUNT: это не случай пропущенных коллинеарных точек')
    projected = _project(world)
    span_x = max(p[0] for p in projected) - min(p[0] for p in projected)
    span_y = max(p[1] for p in projected) - min(p[1] for p in projected)
    extent = math.hypot(span_x, span_y)
    if extent <= 1e-14:
        raise TessellationCoverageError('AREA_ZERO: недостаточная площадь контура')
    # Local geometric scale rather than a hard-coded meter-sized floor.
    # Coordinates already rounded to binary floats can carry up to an ULP of
    # transverse uncertainty after a large CAD translation. Do not reject a
    # mathematically straight authored midpoint solely for that uncertainty.
    # This affects the skipped-boundary-vertex proof, not the exact segment
    # crossing predicate. That still rejects every actual stored contact.
    stored_ulp = max(math.ulp(v) for point in world for v in point)
    tolerance = max(1e-13, 1e-7*extent, 3.0*stored_ulp)
    precision_uncertain = tolerance > extent*1e-3
    # BH0472: coarse coordinate precision is not proof that the polygon is
    # invalid. Continue with the independent area/boundary/topology proof.
    # Limit the local collinearity band so uncertainty cannot silently swallow
    # a visible geometric bend; area conservation below remains authoritative.
    point_tolerance = min(tolerance, max(1e-13, extent*1e-3))
    if diagnostics is not None:
        diagnostics.update(precision_uncertain=bool(precision_uncertain),
                           stored_ulp=float(stored_ulp), extent=float(extent),
                           requested_point_tolerance=float(tolerance),
                           effective_point_tolerance=float(point_tolerance))
    area_tolerance = max(1e-28, extent*extent*1e-9)
    polygon_area = _signed_area(projected)
    if abs(polygon_area) <= area_tolerance:
        raise TessellationCoverageError('AREA_ZERO: вырожденная геометрия')
    winding = 1 if polygon_area > 0 else -1
    for i in range(n):
        if _distance(projected[i], projected[(i+1)%n]) <= tolerance:
            raise TessellationCoverageError('ZERO_EDGE: совпадающие соседние вершины')
        for j in range(i+1,n):
            if j == i or (j-i)==1 or (i==0 and j==n-1):
                continue
            if _segments_conflict(projected[i],projected[(i+1)%n],
                                  projected[j],projected[(j+1)%n],tolerance):
                raise TessellationCoverageError(f'SELF_INTERSECTION: рёбра {i} и {j}')
    used = {v for tri in tris for v in tri}
    if len(used)==n:
        raise TessellationCoverageError('COVERAGE: пропущенных граничных точек нет')
    skipped = sorted(set(range(n))-used)
    for index in skipped:
        before = (index-1)%n
        after = (index+1)%n
        for _ in range(n):
            if before in used: break
            before = (before-1)%n
        for _ in range(n):
            if after in used: break
            after = (after+1)%n
        valid, t = _point_on_segment(world[index],world[before],world[after],point_tolerance)
        if not valid:
            # When the stored world coordinate itself is too coarse, the
            # local collinearity test can be inconclusive. Do not block here:
            # the strict signed-area and boundary partition proof below must
            # still succeed, otherwise the tessellation is rejected.
            if not precision_uncertain or t is None or t < -1.0e-8 or t > 1.0+1.0e-8:
                raise TessellationCoverageError(f'NONCOLLINEAR: точка {index} необходима геометрии')
            if diagnostics is not None:
                diagnostics.setdefault('uncertain_skipped_vertices', []).append(int(index))
        expected_uv = tuple(uv[before][k]+t*(uv[after][k]-uv[before][k]) for k in range(2))
        if _distance(expected_uv,uv[index])*resolution > uv_tolerance_pixels:
            raise TessellationCoverageError(f'UV_NONLINEAR: точка {index} имеет авторский UV-излом')
    # A valid triangulation is an oriented surface partition: boundary edges occur
    # once, internal edges twice in opposite directions, signed area is conserved.
    counts = Counter()
    oriented = Counter()
    total = 0.0
    for tri in tris:
        area = 0.5*_cross2(*(projected[k] for k in tri))
        if winding*area <= area_tolerance:
            raise TessellationCoverageError('TRIANGLE: вырожденный или инвертированный треугольник')
        total += area
        for a,b in zip(tri,(tri[1],tri[2],tri[0])):
            edge = (min(a,b),max(a,b))
            counts[edge] += 1
            oriented[(a,b)] += 1
    if abs(total-polygon_area)>max(area_tolerance,abs(polygon_area)*1e-6):
        raise TessellationCoverageError('AREA_MISMATCH: сумма треугольников не равна площади полигона')
    ordered = [i for i in range(n) if i in used]
    expected_boundary = {(min(a,b),max(a,b)) for a,b in zip(ordered,ordered[1:]+ordered[:1])}
    actual_boundary = {e for e,c in counts.items() if c==1}
    if expected_boundary != actual_boundary:
        raise TessellationCoverageError('BOUNDARY_MISMATCH: неполное покрытие внешней границы')
    for (a,b), count in counts.items():
        if count > 2 or (count==2 and (oriented[(a,b)]!=1 or oriented[(b,a)]!=1)):
            raise TessellationCoverageError('TOPOLOGY: наложение или несогласованные треугольники')
    return tris


def reject_collinear_uv_kinks(world_coords, uv_coords, resolution, *, tolerance_pixels=0.75):
    """Reject a UV bend along a truly straight 3D face boundary.

    Only validates NEW faces passed to face_uv_density_gate, never rewrites
    authored wall UV. Independent of Blender's n-gon tessellation.
    """
    n=len(world_coords)
    if n != len(uv_coords) or n<3:
        raise TessellationCoverageError('UV_KINK_INPUT: mismatched polygon')
    res=float(resolution)
    if not math.isfinite(res) or res<=0:
        raise TessellationCoverageError('UV_KINK_INPUT: invalid resolution')
    for i in range(n):
        j=(i-1)%n; k=(i+1)%n
        a=tuple(map(float,world_coords[j]))
        p=tuple(map(float,world_coords[i]))
        b=tuple(map(float,world_coords[k]))
        if any(len(v)!=3 or not all(map(math.isfinite,v)) for v in (a,p,b)):
            raise TessellationCoverageError('UV_KINK_INPUT: nonfinite world point')
        d=_sub(b,a); norm2=sum(t*t for t in d)
        if norm2<=1e-16:
            continue
        v=_sub(p,a)
        t=sum(v[m]*d[m] for m in range(3))/norm2
        if not 1e-6<t<1-1e-6:
            continue
        foot=tuple(a[m]+t*d[m] for m in range(3))
        if math.dist(foot,p)>max(1e-7, 1e-7*math.sqrt(norm2)):
            continue
        ua=tuple(map(float,uv_coords[j])); up=tuple(map(float,uv_coords[i]));ub=tuple(map(float,uv_coords[k]))
        if any(len(v)!=2 or not all(map(math.isfinite,v)) for v in (ua,up,ub)):
            raise TessellationCoverageError('UV_KINK_INPUT: nonfinite UV point')
        interpolated=tuple(ua[m]+t*(ub[m]-ua[m]) for m in range(2))
        err=math.dist(up,interpolated)*res
        if err>tolerance_pixels:
            raise TessellationCoverageError(
                f'UV_NONLINEAR_BOUNDARY: UV-излом {err:.3f} px на прямом ребре (угол {i}); '
                f'допуск {tolerance_pixels:.3f} px')
