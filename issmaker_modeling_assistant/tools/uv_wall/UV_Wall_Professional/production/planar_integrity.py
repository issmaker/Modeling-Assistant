"""Geometrical policy for closed 2-D offset loops and cap ribbon coverage.

Pure functions: usable in CI without bpy/mathutils. All coordinates are taken
from actual generated geometry, never guessed from UI inset-side flags.
"""
from issmaker_modeling_assistant.core.reliability.predicates import signed_area_2d as _rf_signed_area_2d, exact_segment_contact_2d, exact_signed_area_2d
from issmaker_modeling_assistant.core.reliability.precision import policy_from_points


def xy(p):
    return (float(p.x), float(p.y)) if hasattr(p, 'x') else (float(p[0]), float(p[1]))


def signed_area(points):
    ring = list(points)
    if len(ring) >= 2 and xy(ring[0]) == xy(ring[-1]):
        ring = ring[:-1]
    return _rf_signed_area_2d([xy(p) for p in ring])


def interior_side(points):
    exact=exact_signed_area_2d([xy(p) for p in points])
    if exact==0:
        raise ValueError('Замкнутый контур имеет доказанно нулевую площадь: нельзя определить внутреннюю сторону')
    return 'LEFT' if exact>0 else 'RIGHT'


def inside_polygon(point, loop):
    # Translate the test point to the origin so CAD/world offset cannot affect
    # the ray-crossing arithmetic. This routine is advisory support for the
    # exact boundary-intersection proof above.
    x,y=xy(point)
    ring=[(px-x,py-y) for px,py in map(xy,loop)]
    if len(ring)>1 and ring[0]==ring[-1]:ring.pop()
    inside=False
    for (x1,y1),(x2,y2) in zip(ring,ring[1:]+ring[:1]):
        if ((y1>0)!=(y2>0)) and (0 < (x2-x1)*(-y1)/(y2-y1)+x1):
            inside=not inside
    return inside


def _point_on_segment_xy(p, a, b, tol=None):
    # Kept for compatibility. Exact stored-float contact is used for hard proof.
    return exact_segment_contact_2d(xy(p),xy(p),xy(a),xy(b))


def _segments_intersect_xy(a,b,c,d,tol=None):
    # Broad-phase tolerances may nominate candidates elsewhere, but this hard
    # boundary decision is exact for the binary coordinates supplied by Blender.
    return exact_segment_contact_2d(xy(a),xy(b),xy(c),xy(d))


def assert_nested_boundaries(outer, inner, label='Внутренняя поверхность'):
    """Reject offset loops escaping the inner usable area (CW and CCW)."""
    outer_xy=[xy(p) for p in outer];inner_xy=[xy(p) for p in inner]
    a_exact=exact_signed_area_2d(outer_xy); b_exact=exact_signed_area_2d(inner_xy)
    policy=policy_from_points(outer_xy+inner_xy,rel_linear=1e-10,min_linear=1e-12)
    if a_exact==0 or b_exact==0:
        raise ValueError(f'{label}: доказанно нулевая площадь контура')
    if (a_exact>0)!=(b_exact>0):
        raise ValueError(f'{label}: offset имеет доказанно неверное направление')
    if abs(b_exact)>=abs(a_exact):
        raise ValueError(f'{label}: внутренний контур доказанно не меньше внешнего')
    ring=list(inner)
    if len(ring)>1 and xy(ring[0])==xy(ring[-1]):ring=ring[:-1]
    outside=list(outer)
    if len(outside)>1 and xy(outside[0])==xy(outside[-1]):outside=outside[:-1]
    # AUD-01: five samples along an edge can miss a very narrow concave
    # notch. Exact segment intersection catches every departure, regardless
    # of where it lies between the sampling positions.
    for p,q in zip(ring,ring[1:]+ring[:1]):
        for a,b in zip(outside,outside[1:]+outside[:1]):
            if _segments_intersect_xy(p,q,a,b):
                raise ValueError(f'{label}: внутренний контур пересекает или касается внешней границы')
    for p,q in zip(ring,ring[1:]+ring[:1]):
        for t in (0.02,0.25,0.5,0.75,0.98):
            px,py=xy(p); qx,qy=xy(q)
            sample=(px+(qx-px)*t,py+(qy-py)*t)
            if not inside_polygon(sample,outer):
                raise ValueError(f'{label}: граница пересекает/выходит за внутреннюю стену')
    return True


def validate_ribbon_coverage(outer, inner, relative_tolerance=0.00005):
    """Every outer/inner edge pair must contribute one non-degenerate ribbon.

    Sum of unsigned patch areas must equal actual annular area, otherwise
    there are overlaps, inverted/folded patches or gaps at mitered corners.
    """
    if len(outer)!=len(inner) or len(outer)<2:
        raise ValueError('Top Cap: несовместимое количество точек двух стен')
    policy=policy_from_points([xy(p) for p in list(outer)+list(inner)],rel_linear=1e-10,min_linear=1e-12)
    patch_areas=[]
    for a,b,c,d in zip(outer,outer[1:],inner,inner[1:]):
        quad=[xy(a),xy(b),xy(d),xy(c)]
        exact=exact_signed_area_2d(quad)
        if exact==0:
            raise ValueError('Top Cap: доказанно нулевая или схлопнутая полоса на углу')
        patch_areas.append(float(exact))
    signs={1 if area>0 else -1 for area in patch_areas}
    if len(signs)>1:
        raise ValueError('Top Cap: пересекающиеся/перевёрнутые участки полосы')
    if xy(outer[0])==xy(outer[-1]) and xy(inner[0])==xy(inner[-1]):
        expected=abs(signed_area(outer)-signed_area(inner))
        actual=sum(abs(a) for a in patch_areas)
        if expected<=0.0:
            raise ValueError('Top Cap: доказанно нулевая площадь кольца')
        mismatch=abs(expected-actual)
        tolerance=max(policy.area,expected*relative_tolerance)
        if mismatch>tolerance:
            raise ValueError('Top Cap: доказанное несоответствие площади полос и кольца (перекрытие/разрыв)')
    # A signed-area comparison alone is NOT an overlap test: the area sum is
    # an algebraic identity for a stitched ribbon, even when two non-neighbour
    # patches cover exactly the same physical XY area. Validate actual footprints.
    assert_nonoverlapping_ribbon_patches(outer, inner)
    return len(patch_areas)


def _triangle_area_xy(triangle):
    a, b, c = map(xy, triangle)
    return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5


def _quad_triangles(patch, tolerance=None):
    """Triangulate a simple convex/concave quad without relying on Blender.

    Preserve the authored polygon footprint; do not include area outside a
    re-entrant corner. Crossing patches are explicitly rejected.
    """
    points = list(map(xy, patch))
    policy=policy_from_points(points,rel_linear=1e-10,min_linear=1e-12)
    tolerance=policy.area if tolerance is None else float(tolerance)
    edges = ((0,1),(1,2),(2,3),(3,0))
    # Proper opposite-edge crossing is never a valid ribbon patch.
    def proper(a,b,c,d):
        def orient(p,q,r):return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
        return orient(a,b,c)*orient(a,b,d)<-tolerance and orient(c,d,a)*orient(c,d,b)<-tolerance
    if proper(points[0],points[1],points[2],points[3]) or proper(points[1],points[2],points[3],points[0]):
        raise ValueError(f'Top Cap: самопересечение полосы в углу (XY): {points}')
    area=abs(signed_area(points))
    if area <= tolerance:
        raise ValueError('Top Cap: нулевая площадь угловой полосы')
    for indices in (((0,1,2),(0,2,3)), ((0,1,3),(1,2,3))):
        triangles = [[points[j] for j in tri] for tri in indices]
        if abs(sum(_triangle_area_xy(tri) for tri in triangles)-area)<=max(tolerance,area*1e-8):
            return triangles
    raise ValueError('Top Cap: невозможно корректно разбить угловую полосу')


def _triangle_overlap_area(first, second):
    """Translation-stable convex clipping; shared boundaries have zero area."""
    first=list(map(xy,first));second=list(map(xy,second))
    policy=policy_from_points(first+second,rel_linear=1e-10,min_linear=1e-12)
    ox,oy=first[0]
    local=lambda pts:[(p[0]-ox,p[1]-oy) for p in pts]
    subject=local(first);clip=local(second)
    sign=1 if _rf_signed_area_2d(clip)>0 else -1
    for start,end in zip(clip,clip[1:]+clip[:1]):
        if not subject:return 0.0
        def dist(q):return sign*((end[0]-start[0])*(q[1]-start[1])-(end[1]-start[1])*(q[0]-start[0]))
        result=[]
        prev=subject[-1];pd=dist(prev)
        for curr in subject:
            cd=dist(curr)
            was_in=pd>=-policy.area;is_in=cd>=-policy.area
            if was_in!=is_in:
                denom=pd-cd
                if abs(denom)>max(policy.area*1e-6,1e-30):
                    tt=pd/denom
                    result.append((prev[0]+tt*(curr[0]-prev[0]),prev[1]+tt*(curr[1]-prev[1])))
            if is_in:result.append(curr)
            prev,pd=curr,cd
        subject=result
    return abs(_rf_signed_area_2d(subject)) if len(subject)>2 else 0.0


def assert_nonoverlapping_ribbon_patches(outer,inner, area_tolerance=None):
    """Reject intersecting cap domains, including valid-manifold XY overlaps.

    Only tests *actual* generated strip footprints; user path/height unchanged.
    Broad phase uses X intervals to avoid O(n²) all-to-all on long paths.
    """
    if len(outer)!=len(inner):
        raise ValueError('Top Cap: контуры разной длины')
    policy=policy_from_points([xy(p) for p in list(outer)+list(inner)],rel_linear=1e-10,min_linear=1e-12)
    area_tolerance=max(policy.area,1e-12) if area_tolerance is None else float(area_tolerance)
    patches=[]
    for i,(a,b,c,d) in enumerate(zip(outer,outer[1:],inner,inner[1:])):
        q=[xy(a),xy(b),xy(d),xy(c)]
        triangles=_quad_triangles(q)
        minx=min(p[0] for p in q);maxx=max(p[0] for p in q)
        miny=min(p[1] for p in q);maxy=max(p[1] for p in q)
        patches.append((minx,maxx,miny,maxy,i,triangles))
    patches.sort(key=lambda row:row[0])
    active=[]
    for item in patches:
        a,b,c,d,i,triangles=item
        active=[prev for prev in active if prev[1]>=a-policy.linear]
        for prev in active:
            pa,pb,pc,pd,j,pt=prev
            if pd<c-policy.linear or d<pc-policy.linear:continue
            overlap=sum(_triangle_overlap_area(t0,t1) for t0 in pt for t1 in triangles)
            if overlap > area_tolerance:
                raise ValueError('Top Cap: угловые полосы перекрываются: '
                                 f'сегменты {j+1} и {i+1}, XY-перекрытие {overlap:.7f} м²; '
                                 'путь не изменён, прежний Result защищён')
        active.append(item)
    return len(patches)
