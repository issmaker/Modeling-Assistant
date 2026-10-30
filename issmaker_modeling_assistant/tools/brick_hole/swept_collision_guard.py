"""Conservative, read-only collision checks for Brick Hole EDGE ruled surfaces.

This code intentionally has no Blender imports so a separate oracle can exercise it.
A collision is an intersection other than an explicitly permitted, topologically
shared seam. BH0472 separates proof from uncertainty: coarse tolerances discover
candidates; only a refined local narrow-phase blocks geometry. Resource exhaustion
is recorded as UNCERTAIN, then blocked as RESOURCE_LIMIT (not collision).
"""
import math
from bisect import bisect_right
from time import perf_counter

TOLERANCE = 1.0e-7


def _v(p):
    try:
        a = tuple(float(x) for x in p)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('COLLISION: некорректные координаты XYZ') from exc
    if len(a) != 3 or not all(math.isfinite(x) for x in a):
        raise ValueError('COLLISION: некорректные координаты XYZ')
    return a


def _add(a,b): return tuple(a[i]+b[i] for i in range(3))
def _sub(a,b): return tuple(a[i]-b[i] for i in range(3))
def _mul(a,s): return tuple(c*s for c in a)
def _dot(a,b): return sum(a[i]*b[i] for i in range(3))
def _cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _norm(v): return math.sqrt(_dot(v,v))
def _lerp(a,b,t): return _add(a,_mul(_sub(b,a),t))


def _normal(tri, tol):
    n=_cross(_sub(tri[1],tri[0]),_sub(tri[2],tri[0]))
    length=_norm(n)
    if length < tol*tol: raise ValueError('COLLISION: вырожденный треугольник результата или исходной сетки')
    return _mul(n,1.0/length)


def _project(p, axis): return tuple(p[i] for i in range(3) if i != axis)
def _cross2(a,b): return a[0]*b[1]-a[1]*b[0]
def _sub2(a,b): return a[0]-b[0],a[1]-b[1]

def _clip_poly(subject, target, tol):
    """2D triangle clip, preserving zero-area contacts."""
    sign=1 if _cross2(_sub2(target[1],target[0]),_sub2(target[2],target[0]))>0 else -1
    points=list(subject)
    for j in range(3):
        edge_a=target[j]; edge_b=target[(j+1)%3]
        def signed(p): return sign*_cross2(_sub2(edge_b,edge_a),_sub2(p,edge_a))
        previous=points[-1] if points else None
        output=[]
        for current in points:
            pc, cc=signed(previous),signed(current)
            if cc>=-tol:
                if pc < -tol:
                    t=pc/(pc-cc)
                    output.append(tuple(previous[k]+t*(current[k]-previous[k]) for k in range(2)))
                output.append(current)
            elif pc>=-tol:
                t=pc/(pc-cc)
                output.append(tuple(previous[k]+t*(current[k]-previous[k]) for k in range(2)))
            previous=current
        points=output
        if not points: break
    return points


def _area2(poly):
    return abs(sum(_cross2(poly[i],poly[(i+1)%len(poly)]) for i in range(len(poly)))) if len(poly)>2 else 0.0


def _inside_barycentric_3d(p, tri, tol):
    a,b,c=tri; u=_sub(b,a); v=_sub(c,a); q=_sub(p,a)
    uu=_dot(u,u); vv=_dot(v,v); uv=_dot(u,v); det=uu*vv-uv*uv
    if det<tol**4: raise ValueError('COLLISION: неустойчивая геометрия треугольника')
    qu=_dot(q,u); qv=_dot(q,v)
    t=(qu*vv-qv*uv)/det; w=(qv*uu-qu*uv)/det
    return (t,w,1-t-w)


def _clip_on_plane_segment(a,b,tri,tol):
    """Segment contained in triangle plane, clipped by three barycentric inequalities."""
    u=_inside_barycentric_3d(a,tri,tol); v=_inside_barycentric_3d(b,tri,tol)
    low=0.0; high=1.0
    for start,end in zip(u,v):
        delta=end-start
        if abs(delta)<tol:
            if start < -tol: return []
            continue
        t=(-tol-start)/delta
        if delta>0: low=max(low,t)
        else: high=min(high,t)
        if low>high: return []
    if high < 0 or low > 1: return []
    return [_lerp(a,b,max(0,min(1,low))),_lerp(a,b,max(0,min(1,high)))]


def _tri_intersection(first, second, tol):
    """Returns (positive_coplanar_area, 3D contact points).

    BH0472: narrow-phase arithmetic is translated to a shared local origin
    before plane and clipping math. This removes CAD/world-offset cancellation;
    returned witnesses are translated back to the caller's coordinates.
    """
    first=tuple(_v(p) for p in first)
    second=tuple(_v(p) for p in second)
    anchor=first[0]
    first_local=tuple(_sub(p,anchor) for p in first)
    second_local=tuple(_sub(p,anchor) for p in second)

    def local_intersection(a_tri,b_tri):
        n1=_normal(a_tri,tol); n2=_normal(b_tri,tol)
        relative=[_dot(n1,_sub(p,a_tri[0])) for p in b_tri]
        if min(relative)>tol or max(relative)<-tol:return False,[]
        relative_reverse=[_dot(n2,_sub(p,b_tri[0])) for p in a_tri]
        if min(relative_reverse)>tol or max(relative_reverse)<-tol:return False,[]
        if abs(_dot(n1,n2))>1-1.0e-10:
            if any(abs(d)>tol for d in relative):return False,[]
            axis=max(range(3),key=lambda i:abs(n1[i]))
            first2=[_project(p,axis) for p in a_tri]
            second2=[_project(p,axis) for p in b_tri]
            clipped=_clip_poly(first2,second2,tol)
            if not clipped:return False,[]
            area=_area2(clipped)
            positive=area > tol*tol*4
            a,b,c=first2;den=_cross2(_sub2(b,a),_sub2(c,a))
            if abs(den)<=tol*tol:raise ValueError('COLLISION: вырожденная проекция треугольника')
            points=[]
            for q in clipped:
                u=_cross2(_sub2(q,a),_sub2(c,a))/den
                v=_cross2(_sub2(b,a),_sub2(q,a))/den
                points.append(_add(a_tri[0],_add(_mul(_sub(a_tri[1],a_tri[0]),u),_mul(_sub(a_tri[2],a_tri[0]),v))))
            return positive,points
        hits=[]
        for tri,plane_tri,n in ((a_tri,b_tri,n2),(b_tri,a_tri,n1)):
            for i in range(3):
                a=tri[i];b=tri[(i+1)%3]
                d0=_dot(n,_sub(a,plane_tri[0]));d1=_dot(n,_sub(b,plane_tri[0]))
                if abs(d0)<=tol and abs(d1)<=tol:
                    hits.extend(_clip_on_plane_segment(a,b,plane_tri,tol))
                elif min(d0,d1)<=tol and max(d0,d1)>=-tol and abs(d0-d1)>tol*0.01:
                    ratio=d0/(d0-d1)
                    if -tol<=ratio<=1+tol:
                        point=_lerp(a,b,max(0,min(1,ratio)))
                        if min(_inside_barycentric_3d(point,plane_tri,tol))>=-tol:
                            hits.append(point)
        unique=[]
        for point in hits:
            if not any(_norm(_sub(point,q))<=tol*2 for q in unique):unique.append(point)
        return False,unique

    positive,hits=local_intersection(first_local,second_local)
    return positive,[tuple(point[i]+anchor[i] for i in range(3)) for point in hits]


def _proven_tri_intersection(first, second, candidate_tol):
    """Refine a coarse candidate before declaring a hard collision.

    The public collision tolerance is deliberately conservative for broad-phase
    discovery. A second, much smaller local tolerance must reproduce the contact
    before it is considered proven. The localised `_tri_intersection` prevents
    world-translation cancellation from influencing this decision.
    """
    coarse_positive, coarse_hits = _tri_intersection(first, second, candidate_tol)
    if not coarse_positive and not coarse_hits:
        return False, [], False
    points=tuple(first)+tuple(second)
    scale=max((_norm(_sub(points[i],points[j])) for i in range(len(points))
               for j in range(i+1,len(points))),default=1.0)
    proof_tol=max(1e-12,min(candidate_tol*0.05,max(1.0,scale)*1e-10))
    positive,hits=_tri_intersection(first,second,proof_tol)
    if positive or hits:
        # Even if exact thin-plane arithmetic sees one shared corner, a
        # projected area may be fully nested at Blender float32 precision.
        # Prove area independently inside a tight float32 ULP envelope.
        from ...core.reliability.contact_classification_core import (
            prove_near_coplanar_area_overlap)
        area_proof=prove_near_coplanar_area_overlap(
            first,second,tolerance_m=candidate_tol)
        if area_proof is not None:
            return True,coarse_hits or hits,True
        return positive,hits,True
    from ...core.reliability.contact_classification_core import (
        prove_near_coplanar_area_overlap)
    area_proof=prove_near_coplanar_area_overlap(
        first,second,tolerance_m=candidate_tol)
    if area_proof is not None:
        return True,coarse_hits,True
    return False,[],False


def _on_segment(p,a,b,tol):
    ab=_sub(b,a); ap=_sub(p,a)
    length_sq=_dot(ab,ab)
    if length_sq<=tol*tol: return _norm(ap)<=tol
    t=_dot(ap,ab)/length_sq
    if t < -tol or t > 1+tol: return False
    return _norm(_sub(ap,_mul(ab,max(0,min(1,t)))))<=tol*2


def _shared_generated_edge_source_contact(strip, obstacle, hits, tol):
    """Prove an edge-only contact with an existing edge at a welded source corner.

    Both the swept surface and the source triangle must touch along their
    boundaries. Authorised source BMVert identity is required, not XYZ alone.
    Never call this for a proven area overlap (rejected by the caller).
    """
    if not hits:
        return False
    edge = tuple(strip['edge'])
    source_ids = tuple(obstacle.get('verts', ()))
    if len(edge) != 2 or len(source_ids) != 3:
        return False
    common = [i for i, vert in enumerate(edge) if vert in source_ids]
    if len(common) != 1:
        return False
    i = common[0]
    quad = tuple(_v(p) for p in strip['quad'])
    root, tip = quad[i], quad[3-i]
    if _norm(_sub(root, tip)) <= tol*2:
        return False
    if not all(_on_segment(p, root, tip, tol) for p in hits):
        return False
    j = source_ids.index(edge[i])
    tri = tuple(_v(p) for p in obstacle['tri'])
    if _norm(_sub(root, tri[j])) > tol:
        return False
    return any(
        all(_on_segment(p, tri[j], tri[k], tol) for p in hits)
        for k in range(3) if k != j
    )


def _bounds(tri):return [(min(p[i] for p in tri),max(p[i] for p in tri)) for i in range(3)]
def _overlap(a,b,tol):return all(a[i][0]<=b[i][1]+tol and b[i][0]<=a[i][1]+tol for i in range(3))


def _zero_area_segment(tri, tol):
    """Only exact/near-exact duplicated-coordinate corners qualify.

    Three distinct collinear points are *not* automatically treated as a
    harmless tessellation artifact: keep those fail-closed for a separate
    source polygon repair pass.
    """
    close = [(i, j) for i in range(3) for j in range(i+1, 3)
             if _norm(_sub(tri[i], tri[j])) <= tol * 0.1]
    if len(close) != 1:
        return None
    i, j = close[0]
    k = next(i0 for i0 in range(3) if i0 not in (i, j))
    if _norm(_sub(tri[i], tri[k])) <= tol * 10:
        return None  # All three coordinates collapse; cannot prove anything.
    return (i, k)


def _segment_triangle_contacts(a, b, tri, tol):
    """Conservative segment versus *nondegenerate* triangle intersection.

    Return contact endpoints or None when numerical ambiguity prevents proof.
    The source segment is never turned into a fictitious area-bearing face.
    """
    normal = _normal(tri, tol)
    d0 = _dot(normal, _sub(a, tri[0]))
    d1 = _dot(normal, _sub(b, tri[0]))
    if abs(d0) <= tol and abs(d1) <= tol:
        # Co-planar segment. The clip can return an entire overlap, which is
        # forbidden unless it degenerates to the exact shared source vertex.
        try:
            return _clip_on_plane_segment(a, b, tri, tol * 0.01)
        except (ValueError, ZeroDivisionError):
            return None
    if d0 > tol and d1 > tol or d0 < -tol and d1 < -tol:
        return []
    if abs(d0 - d1) < tol * 0.01:
        return None
    t = d0/(d0-d1)
    if t < -tol or t > 1+tol:
        return []
    point = _lerp(a, b, max(0.0, min(1.0, t)))
    try:
        bary = _inside_barycentric_3d(point, tri, tol)
    except (ValueError, ZeroDivisionError):
        return None
    return [point] if min(bary) >= -tol*0.01 else []


def _harmless_source_triangle_artifact(ob, tri, *, sibling_faces, collected, strips, tol):
    """Narrow exemption for a zero-area *tessellation child*, not a mesh face.

    Requires an area-bearing sibling from the SAME parent polygon, a truly
    collapsed edge, a topologically shared source vertex, and at most contact
    at that vertex. Every normal sibling remains in exact collision checking.
    """
    face_id = ob.get('face_index')
    if not isinstance(face_id, int) or face_id < 0 or face_id not in sibling_faces:
        return False
    # Four or more parent polygon edges: this triangle must be one of several
    # children of a nondegenerate polygon, not an independent invalid tri face.
    if len(ob.get('linked_edges', ())) < 4:
        return False
    ends = _zero_area_segment(tri, tol)
    if ends is None:
        return False
    segment = (tri[ends[0]], tri[ends[1]])
    ids = ob.get('verts', ())
    if len(ids) != 3:
        return False
    for strip_index, gen, gen_bbox in collected:
        if not _overlap(_bounds(segment), gen_bbox, tol):
            continue
        shared_positions = []
        for k, vertex in enumerate(strips[strip_index]['edge']):
            if vertex in (ids[ends[0]], ids[ends[1]]):
                for src_i in ends:
                    if ids[src_i] == vertex and _norm(_sub(tri[src_i],strips[strip_index]['quad'][k])) <= tol:
                        shared_positions.append(strips[strip_index]['quad'][k])
        if not shared_positions:
            return False
        contacts = _segment_triangle_contacts(*segment, gen, tol)
        if contacts is None:
            return False
        if any(not any(_norm(_sub(p, q)) <= tol*2 for q in shared_positions)
               for p in contacts):
            return False
    return True


def _strip_triangles(strip,tol):
    """Strip dict: {'edge':(idA,idB), 'quad':(sourceA,sourceB,moveB,moveA)}."""
    q=tuple(_v(p) for p in strip['quad'])
    if len(q)!=4: raise ValueError('COLLISION: неверное описание новой грани')
    n1=_normal((q[0],q[1],q[2]),tol)
    n2=_normal((q[0],q[2],q[3]),tol)
    if _dot(n1,n2)<0.9:
        # A twisted side quad may still be valid but its triangles need a
        # shared trusted triangulation; this guard does not pretend that.
        raise ValueError('COLLISION: новая боковая грань неплоская или самоскладывается')
    return ((q[0],q[1],q[2]),(q[0],q[2],q[3]))



# BH-03.2H.2: immutable, per-preflight 3D bounding volume hierarchy.
# BVH is an *acceleration*, never a substitute for `_tri_intersection`.
# Leaf entries and query results refer to the original X-sorted source list,
# preserving deterministic collision witnesses and the pair work budget.
def _join_bounds(a, b):
    return tuple((min(a[i][0], b[i][0]), max(a[i][1], b[i][1])) for i in range(3))


def _build_bounds_tree(sorted_source, leaf_size=12):
    """Static binary bbox tree; one centroid sort, linear hierarchy build.

    `source` remains in its original X-sweep order, independent of tree order,
    so the narrow-phase witness can be compared to the original oracle.
    """
    if not sorted_source:
        return None
    n = len(sorted_source)
    centers = [[(s[2][axis][0] + s[2][axis][1]) * .5
                for axis in range(3)] for s in sorted_source]
    span = [max(c[a] for c in centers)-min(c[a] for c in centers)
            for a in range(3)]
    axis = max(range(3), key=lambda a: span[a])
    ids = sorted(range(n), key=lambda i: (centers[i][axis], i))

    def make(lo, hi):
        if hi-lo <= leaf_size:
            bbox = sorted_source[ids[lo]][2]
            for pos in range(lo+1,hi):
                bbox = _join_bounds(bbox,sorted_source[ids[pos]][2])
            return (bbox, None, None, tuple(ids[lo:hi]))
        middle = (lo+hi)//2
        left=make(lo,middle)
        right=make(middle,hi)
        return (_join_bounds(left[0],right[0]),left,right,None)
    return make(0,n)


def _query_bounds_tree(root, bounds, tol, source):
    if root is None:
        return []
    results = []
    todo = [root]
    while todo:
        node = todo.pop()
        if not _overlap(node[0], bounds, tol):
            continue
        if node[3] is not None:
            results.extend(index for index in node[3] if
                           _overlap(bounds, source[index][2], tol))
        else:
            todo.append(node[2])
            todo.append(node[1])
    results.sort()
    return results

def validate_swept_edges(strips, obstacles, *, tol=TOLERANCE, max_candidate_pairs=50000,
                         witness=None, broadphase='AUTO', statistics=None):
    """Fail closed on penetrations of swept EDGE strips against source and each other.

    strips: edge=(stable topological vertex identities), quad=(A,B,B',A').
    obstacles: face=(stable face identity), tri=(world xyz triangle), verts=(source
    topological identities at the triangle corners), linked_edges=(selected source
    edge id-pairs belonging to the ORIGINAL polygon).

    Returns tested candidate pairs; raises ValueError containing collision kind.
    Rejects geometric contact unless it lies entirely on an authorized, shared
    topological seam. Does not calculate first-safe-depth or inspect other objects.
    """
    if not math.isfinite(tol) or tol<=0:raise ValueError('COLLISION: неверный геометрический допуск')
    if broadphase not in ('AUTO', 'X_SWEEP', 'BVH'):
        raise ValueError('COLLISION: неизвестный метод пространственного отбора')
    if not isinstance(max_candidate_pairs, int) or max_candidate_pairs < 1:
        raise ValueError('COLLISION: неверный бюджет геометрических проверок')
    started = perf_counter() if statistics is not None else None
    strips=list(strips); obstacles=list(obstacles)
    if not strips:return 0

    def degenerate_witness(kind, *, strip_index, tri, quad=None, source_index=None, source_face=None):
        # Diagnostic only. Never permit an invalid generated surface or alter
        # collision decisions if telemetry fails. Include full float-precision
        # coordinates for deterministic reproduction without Blender references.
        if witness is None:
            return
        n = _cross(_sub(tri[1],tri[0]), _sub(tri[2],tri[0]))
        lengths = [_norm(_sub(tri[(k+1)%3],tri[k])) for k in range(3)]
        record = {
            'kind': kind,
            'reason': ('Вырожденная новая поверхность' if kind=='new_degenerate'
                       else 'Вырожденный исходный треугольник в зоне выдавливания'),
            'tolerance_m': float(tol),
            'primary_strip': int(strip_index),
            'triangle_a_xyz': [list(map(float, p)) for p in tri],
            'triangle_double_area_m2': float(_norm(n)),
            'triangle_edge_lengths_m': [float(v) for v in lengths],
            'source_triangle_index': int(source_index) if source_index is not None else None,
            'source_face_index': int(source_face) if source_face is not None else None,
        }
        if quad is not None:
            record['primary_quad_xyz'] = [list(map(float, p)) for p in quad]
        try:
            witness(record)
        except Exception:
            pass

    collected=[]
    for i,s in enumerate(strips):
        if len(s['edge'])!=2:raise ValueError('COLLISION: неверная топология исходного ребра')
        try:
            triangles=_strip_triangles(s,tol)
        except ValueError as exc:
            if 'вырожденный треугольник' in str(exc):
                # The new surface must ALWAYS fail, even if it is remote from
                # all source faces: a zero-area result is not a valid Mesh.
                q=tuple(_v(v) for v in s['quad'])
                for tri in ((q[0],q[1],q[2]), (q[0],q[2],q[3])):
                    if _norm(_cross(_sub(tri[1],tri[0]),_sub(tri[2],tri[0]))) < tol*tol:
                        degenerate_witness('new_degenerate',strip_index=i,tri=tri,quad=q)
                        break
                raise ValueError('COLLISION: вырожденный треугольник НОВОЙ поверхности') from exc
            raise
        for tri in triangles:
            collected.append((i,tri,_bounds(tri)))
    source=[]
    remote_degenerate=0
    validated_zero_area_children=0
    # The full scan is lazy: valid source meshes never pay the cost of
    # indexing sibling triangles. Only a nearby zero-area tessellation child
    # triggers this one-time, per-preflight sibling proof.
    sibling_faces = None
    for j,ob in enumerate(obstacles):
        tri=tuple(_v(p) for p in ob['tri'])
        if len(tri)!=3:raise ValueError('COLLISION: повреждён исходный треугольник')
        ob_bounds=_bounds(tri)
        try:
            _normal(tri,tol)
        except ValueError as exc:
            # AABB disjointness is a *proof* that even a zero-area source
            # triangle cannot touch a swept surface. Global invalid mesh
            # outside the operation must not block unrelated directions.
            nearby=next(((strip_i,new_tri) for strip_i,new_tri,new_bb in collected
                         if _overlap(ob_bounds,new_bb,tol)),None)
            if nearby is None:
                remote_degenerate += 1
                continue
            # BH-04B.2: a zero-area child triangle from tessellation of an
            # area-bearing parent face may touch ONLY at the genuinely shared
            # BMesh vertex. Treat the child as a segment, never as a surface.
            # All other cases, including collinear distinct points, fail closed.
            if sibling_faces is None:
                sibling_faces = set()
                for related in obstacles:
                    try:
                        pt = tuple(_v(p) for p in related['tri'])
                        if (len(pt) == 3 and _norm(_cross(
                                _sub(pt[1],pt[0]),_sub(pt[2],pt[0]))) >= tol*tol):
                            sibling_faces.add(related.get('face_index'))
                    except (TypeError, ValueError, KeyError):
                        continue
            if _harmless_source_triangle_artifact(ob, tri,
                    sibling_faces=sibling_faces, collected=collected,
                    strips=strips, tol=tol):
                validated_zero_area_children += 1
                continue
            strip_i,new_tri=nearby
            degenerate_witness('source_degenerate_nearby',strip_index=strip_i,
                               tri=tri,quad=strips[strip_i]['quad'],
                               source_index=j,source_face=ob.get('face_index',-1))
            raise ValueError('COLLISION: вырожденный треугольник ИСХОДНОЙ сетки в зоне выдавливания') from exc
        source.append((j,tri,ob_bounds))
    if statistics is not None:
        statistics['remote_source_degenerate_ignored'] = remote_degenerate
        statistics['source_zero_area_children_proven_nonblocking'] = validated_zero_area_children
    # Avoid scanning the entire source mesh for every EDGE quad. The narrow
    # phase still uses exact 3-D triangles after the X-axis broad phase.
    index_started = perf_counter() if statistics is not None else None
    source.sort(key=lambda entry: entry[2][0][0])
    source_xlo = [entry[2][0][0] for entry in source]
    # For small calls, the old X sweep avoids building an index. Big walls
    # with multiple swept strips benefit from a 3-D tree. Each validation
    # builds from current BMesh triangulation: no stale cross-scene cache.
    effective = ('BVH' if len(source) >= 1500 and len(collected) >= 4
                 else 'X_SWEEP') if broadphase == 'AUTO' else broadphase
    bvh = _build_bounds_tree(source) if effective == 'BVH' else None
    if statistics is not None:
        statistics.update(broadphase=effective, source_triangles=len(source),
                          strips=len(strips), generated_triangles=len(collected),
                          geometry_validation_ms=round((index_started-started)*1000., 3),
                          build_index_ms=round((perf_counter()-index_started)*1000., 3))
    checked=0
    budget_exhausted=False
    near_miss_candidates=0
    def collision_block(reason, kind, primary_strip, first_tri, second_tri,
                        *, obstacle_index=None, other_strip=None, contact_points=(), coplanar=False):
        # Diagnostic callback is optional; never let it change collision decisions.
        # BMesh references are deliberately excluded: only immutable XYZ and IDs.
        if witness is not None:
            details = {
                'kind': kind, 'reason': reason, 'tolerance_m': float(tol),
                'primary_strip': int(primary_strip),
                'primary_edge_xyz': [[float(x) for x in p] for p in strips[primary_strip]['quad'][:2]],
                'primary_quad_xyz': [[float(x) for x in p] for p in strips[primary_strip]['quad']],
                'triangle_a_xyz': [[float(x) for x in p] for p in first_tri],
                'triangle_b_xyz': [[float(x) for x in p] for p in second_tri],
                'contact_xyz': [[float(x) for x in p] for p in tuple(contact_points)[:16]],
                'coplanar_area_overlap': bool(coplanar),
                'tested_candidate_pairs_before_block': int(checked),
            }
            if obstacle_index is not None:
                ob = obstacles[obstacle_index]
                primary_ids = strips[primary_strip]['edge']
                obstacle_ids = ob.get('verts', ())
                detail_source_tri = tuple(_v(p) for p in ob['tri'])
                common_points = [strips[primary_strip]['quad'][idx]
                                 for idx, vertex in enumerate(primary_ids)
                                 if vertex in obstacle_ids]
                details.update(source_triangle_index=int(obstacle_index),
                               source_face_index=int(ob.get('face_index', -1)),
                               source_face_incident=bool(frozenset(primary_ids) in
                                   {frozenset(e) for e in ob.get('linked_edges', ())}),
                               source_shared_vertex_count=len(common_points),
                               # Distinguishes a welded BMesh vertex from an
                               # unwelded vertex occupying the same XYZ.
                               source_coordinate_near_vertex_count=sum(
                                   any(_norm(_sub(p, q)) <= tol * 2
                                       for q in detail_source_tri)
                                   for p in strips[primary_strip]['quad'][:2]))
            if other_strip is not None:
                details['other_strip'] = int(other_strip)
            try:
                witness(details)
            except Exception:
                pass  # Never replace the original collision with a telemetry error.
        raise ValueError(reason)

    def candidate(a,b):
        nonlocal checked,budget_exhausted
        if not _overlap(a[2],b[2],tol):return False
        if checked>=max_candidate_pairs:
            budget_exhausted=True
            return False
        checked+=1
        return True
    for i,tri,bb in collected:
        strip=strips[i]
        if effective == 'BVH':
            # Complete three-axis candidate set; sorted original X order makes
            # the first collision witness deterministic across both methods.
            candidates = _query_bounds_tree(bvh, bb, tol, source)
        else:
            eligible = bisect_right(source_xlo, bb[0][1]+tol)
            candidates = range(eligible)
        for entry_index in candidates:
            j, other, obb = source[entry_index]
            if not candidate((i,tri,bb),(j,other,obb)):continue
            positive,hits,proven=_proven_tri_intersection(tri,other,tol)
            if not proven:
                # Broad-phase/numerical proximity is not collision evidence.
                # Count only candidates that the coarse pass considered contact.
                coarse_positive,coarse_hits=_tri_intersection(tri,other,tol)
                if coarse_positive or coarse_hits:
                    near_miss_candidates+=1
                continue
            if positive:collision_block('COLLISION: новая поверхность накладывается на существующую грань', 'source_overlap', i, tri, other, obstacle_index=j, contact_points=hits, coplanar=True)
            # Only contacts along this strip's authored source edge are legal
            # when source polygon is topologically incident to it.
            edge_key=frozenset(strip['edge'])
            face_edges={frozenset(e) for e in obstacles[j].get('linked_edges',())}
            allowed=(strip['quad'][0],strip['quad'][1]) if edge_key in face_edges else None
            # A second neighbouring polygon may share only a source vertex,
            # not the selected source edge. Point-only topological touching is
            # legal, while contact beyond that vertex remains forbidden.
            common_vertices = set(strip['edge']) & set(obstacles[j].get('verts', ()))
            vertex_contacts = [strip['quad'][strip['edge'].index(vertex)]
                               for vertex in common_vertices]
            def authorized(p):
                if allowed is not None and _on_segment(p,*allowed,tol):
                    return True
                return any(_norm(_sub(p,q)) <= tol*2 for q in vertex_contacts)
            if any(not authorized(p) for p in hits):
                # A neighbouring source face may meet the NEW swept boundary
                # along its existing edge, at a welded corner. Point-only tip
                # contact counts, since both triangulation halves are checked.
                # True area overlaps and interior contacts still block.
                if _shared_generated_edge_source_contact(strip, obstacles[j], hits, tol):
                    if statistics is not None:
                        statistics['authorized_generated_edge_touches'] = (
                            statistics.get('authorized_generated_edge_touches', 0) + 1)
                    continue
                # W58.31: A *point-only* coincidence of two geometrical
                # vertices is not penetration even when their topology IDs
                # differ. This is a warning/unwelded contact, not a hard
                # obstruction. Edge/area overlaps and interior hits still
                # go through the original strict collision decision.
                from ...core.reliability.contact_classification_core import (
                    classify_isolated_vertex_touch)
                point_proof=classify_isolated_vertex_touch(
                    tri,other,hits,coplanar_area_overlap=positive,tolerance_m=tol)
                if point_proof is not None:
                    if statistics is not None:
                        statistics['nonblocking_unwelded_vertex_touches']=(
                            statistics.get('nonblocking_unwelded_vertex_touches',0)+1)
                    if witness is not None:
                        try:
                            witness({'kind':'contact_not_penetration',
                                     'reason':'ISOLATED_UNWELDED_VERTEX_TOUCH',
                                     'primary_strip':int(i),
                                     'source_triangle_index':int(j),
                                     'contact_xyz':[point_proof['contact_xyz']],
                                     'proof':point_proof})
                        except Exception:
                            pass
                    continue
                # BH-03.2G.1: The barycentric clipper deliberately expands the
                # triangle by a *dimensionless* tol. At the end of a long edge
                # this produces synthetic hits as far as edge_length * tol
                # beyond a genuinely shared mesh vertex. Never broaden the
                # production seam tolerance or accept coordinate-only contact.
                # Re-evaluate ONLY topologically shared point contacts using a
                # finer numerical clip; the final authorization still uses the
                # original world-space tolerance and the actual BMesh IDs.
                source_verts = obstacles[j].get('verts', ())
                matched_shared = any(
                    vertex == source_verts[k]
                    and _norm(_sub(strip['quad'][strip['edge'].index(vertex)], other[k])) <= tol
                    for vertex in common_vertices
                    for k in range(min(3, len(source_verts)))
                )
                if matched_shared and allowed is None:
                    refined_positive, refined_hits = _tri_intersection(tri, other, tol * 0.01)
                    if (not refined_positive and refined_hits
                            and all(authorized(p) for p in refined_hits)):
                        continue  # Proven shared-vertex-only contact, no penetration.
                collision_block('COLLISION: новая поверхность пересекает исходную сетку', 'source_contact', i, tri, other, obstacle_index=j, contact_points=hits)
    # Compare strips, but adjacent strips may meet along welded vertical edge.
    for k,(i,tri,bb) in enumerate(collected):
        for ii,tri2,bb2 in collected[k+1:]:
            if i==ii or not candidate((i,tri,bb),(ii,tri2,bb2)):continue
            positive,hits,proven=_proven_tri_intersection(tri,tri2,tol)
            if not proven:
                coarse_positive,coarse_hits=_tri_intersection(tri,tri2,tol)
                if coarse_positive or coarse_hits:
                    near_miss_candidates+=1
                continue
            if positive:collision_block('COLLISION: новые поверхности пересекаются между собой', 'new_overlap', i, tri, tri2, other_strip=ii, contact_points=hits, coplanar=True)
            common=set(strips[i]['edge'])&set(strips[ii]['edge'])
            if len(common)!=1: collision_block('COLLISION: независимые новые поверхности соприкасаются', 'new_unrelated_contact', i, tri, tri2, other_strip=ii, contact_points=hits)
            common_id=next(iter(common))
            start_idx=strips[i]['edge'].index(common_id)
            end_idx=strips[ii]['edge'].index(common_id)
            a=strips[i]['quad'][start_idx]
            b=strips[i]['quad'][3-start_idx]  # 0->3, 1->2
            c=strips[ii]['quad'][end_idx]
            d=strips[ii]['quad'][3-end_idx]
            if _norm(_sub(a,c))>tol*2 or _norm(_sub(b,d))>tol*2:
                collision_block('COLLISION: общая вершина смещена несогласованно', 'new_vertex_offset', i, tri, tri2, other_strip=ii, contact_points=hits)
            if any(not _on_segment(p,a,b,tol) for p in hits):
                collision_block('COLLISION: соседние поверхности пересекаются вне общего ребра', 'new_seam_violation', i, tri, tri2, other_strip=ii, contact_points=hits)
    if statistics is not None:
        statistics.update(narrow_phase_pairs=int(checked),
                          near_miss_candidates_allowed=int(near_miss_candidates),
                          budget_exhausted=bool(budget_exhausted),
                          validation_state=('UNCERTAIN' if budget_exhausted else 'VALID'),
                          elapsed_ms=round((perf_counter()-started)*1000., 3))
    # Reaching the candidate budget leaves potentially intersecting pairs unchecked.
    # Do not treat an incomplete proof as a safe extrusion: fail closed before
    # the caller can mutate BMesh. Keep UNCERTAIN in diagnostics, not COLLISION.
    if budget_exhausted:
        raise ValueError(
            'RESOURCE_LIMIT: проверка пересечений неполная '
            f'(проверено {checked} пар, лимит {max_candidate_pairs}). '
            'Уменьшите выделение или разделите операцию.'
        )
    return checked
