"""Conservative dissolve of verified redundant WALL and CAP helper edges.

Never dissolves required UV seams, authored corners, material boundaries,
non-coplanar faces, semantic-role boundaries, or non-convex joint polygons.
Only tested connected pairs are processed; input Path is never modified.
"""
from __future__ import annotations
from math import isfinite


def convex_uv_ring(points, *, eps=1.e-8):
    """A strict convex polygon with collinear subdivisions is permitted.

    A 180-degree reversal is forbidden even where the cross product vanishes.
    """
    if len(points) < 3 or len(points) > 16:
        return False
    points=[(float(a),float(b)) for a,b in points]
    if not all(isfinite(v) for p in points for v in p):
        return False
    positive=negative=False
    length=len(points)
    for i in range(length):
        a=points[i-1];b=points[i];c=points[(i+1)%length]
        u=(b[0]-a[0], b[1]-a[1]);v=(c[0]-b[0],c[1]-b[1])
        if u[0]*u[0]+u[1]*u[1]<=eps*eps or v[0]*v[0]+v[1]*v[1]<=eps*eps:
            return False
        cross=u[0]*v[1]-u[1]*v[0]
        if cross>eps: positive=True
        elif cross<-eps: negative=True
        if positive and negative: return False
        if abs(cross)<=eps and u[0]*v[0]+u[1]*v[1]<-eps:
            return False
    return positive != negative


def mergeable_uv_boundary(boundary, *, max_uv_span=1.00002):
    """Guard against foldovers or crossing an entire modular UV tile."""
    if not convex_uv_ring(boundary): return False
    us=[p[0] for p in boundary];vs=[p[1] for p in boundary]
    return max(us)-min(us)<=max_uv_span and max(vs)-min(vs)<=max_uv_span


def safe_affine_wall_patch(positions, uv_points, *, eps=1.e-7,
                           flat_rel=1.e-7, uv_eps=1.e-6):
    """Proof that a 3D WALL polygon can safely survive an edge dissolve.

    A pair of quads with individually valid UVs can still have DIFFERENT UV
    gradients.  Joining them creates a distorted n-gon even if the shared UV
    edge is continuous.  Verify affine agreement across the entire joint,
    coplanarity, and projected convexity before any BMesh mutation.
    """
    from math import sqrt
    if len(positions) != len(uv_points) or not 3 <= len(positions) <= 16:
        return False
    p=[tuple(float(x) for x in v) for v in positions]
    uv=[tuple(float(x) for x in v) for v in uv_points]
    if any(len(v)!=3 or not all(isfinite(x) for x in v) for v in p):return False
    if any(len(v)!=2 or not all(isfinite(x) for x in v) for v in uv):return False
    base=p[0]
    d=lambda a,b:tuple(x-y for x,y in zip(a,b))
    dot=lambda a,b:sum(x*y for x,y in zip(a,b))
    cross=lambda a,b:(a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    norm=lambda v:sqrt(dot(v,v))
    scale=max(norm(d(v,base)) for v in p)
    if scale<=eps:return False
    u=None;v=None;normal=None
    for i in range(1,len(p)):
        a=d(p[i],base)
        if norm(a)<=eps:continue
        for j in range(i+1,len(p)):
            b=d(p[j],base); c=cross(a,b)
            if norm(c)>max(eps*eps, 1.e-10*scale*scale):
                u=a;v=b;normal=tuple(x/norm(c) for x in c);break
        if normal is not None:break
    if normal is None:return False
    plane_tolerance=max(eps,scale*flat_rel)
    if any(abs(dot(d(q,base),normal))>plane_tolerance for q in p):return False
    # Check that there are no reflex corners or overlapping opposite edges in
    # the union boundary.  Collinear, same-direction subdivisions are fine.
    orientation=0
    for i in range(len(p)):
        first=d(p[i],p[i-1]);second=d(p[(i+1)%len(p)],p[i])
        if norm(first)<=eps or norm(second)<=eps:return False
        signed=dot(cross(first,second),normal)
        if abs(signed)>max(eps*eps,1.e-10*scale*scale):
            if orientation and orientation*signed < 0:return False
            orientation=1 if signed>0 else -1
        elif dot(first,second)<-eps*eps:return False
    if not orientation:return False
    # Express every vertex in the 2D basis defined by two NONcollinear
    # points.  An exact affine UV mapping must satisfy the same coordinates.
    uu=dot(u,u);uv_basis=dot(u,v);vv=dot(v,v);det=uu*vv-uv_basis*uv_basis
    if det<=max(eps**4,1.e-16*scale**4):return False
    i=next((k for k,q in enumerate(p) if all(abs(q[n]-base[n]-u[n])<eps for n in range(3))),None)
    j=next((k for k,q in enumerate(p) if all(abs(q[n]-base[n]-v[n])<eps for n in range(3))),None)
    if i is None or j is None:return False
    du=d(uv[i],uv[0]);dv=d(uv[j],uv[0])
    for q,target in zip(p,uv):
        pq=d(q,base);ru=dot(pq,u);rv=dot(pq,v)
        alpha=(ru*vv-rv*uv_basis)/det
        beta=(rv*uu-ru*uv_basis)/det
        expected=(uv[0][0]+alpha*du[0]+beta*dv[0], uv[0][1]+alpha*du[1]+beta*dv[1])
        if max(abs(target[0]-expected[0]),abs(target[1]-expected[1]))>uv_eps:return False
    return True


def _boundary_uv_ring(edge, a, b, uv_layer, tolerance):
    """Return shared-boundary UV outline or None on UV discontinuity."""
    # Both faces must share exactly this one edge, otherwise a multigraph
    # perimeter and BMesh dissolve semantics would be ambiguous.
    if sum(e in b.edges for e in a.edges) != 1:
        return None
    uv_by_vert={}; neighbors={}
    for face in (a,b):
        for loop in face.loops:
            vert=loop.vert
            uv=(float(loop[uv_layer].uv.x),float(loop[uv_layer].uv.y))
            if vert in uv_by_vert and (
                abs(uv_by_vert[vert][0]-uv[0])>tolerance
                or abs(uv_by_vert[vert][1]-uv[1])>tolerance
            ): return None
            uv_by_vert[vert]=uv
            if loop.edge==edge:continue
            other=loop.link_loop_next.vert
            neighbors.setdefault(vert,set()).add(other)
            neighbors.setdefault(other,set()).add(vert)
    if not neighbors or any(len(n)!=2 for n in neighbors.values()):return None
    def stable_key(vertex):
        return tuple(round(float(c), 9) for c in vertex.co)
    start=min(neighbors,key=stable_key);ring=[start];previous=None;current=start
    for _ in range(len(neighbors)):
        choices=neighbors[current]-({previous} if previous is not None else set())
        if not choices:return None
        nxt=min(choices,key=stable_key)
        if nxt==start:
            return (ring, [uv_by_vert[v] for v in ring]) if len(ring)==len(neighbors) else None
        if nxt in ring:return None
        ring.append(nxt);previous,current=current,nxt
    return None


def _uv_face_area(face, layer):
    """Absolute UV shoelace area in polygon loop order (BMesh 4.x/5.x)."""
    uv=[(float(l[layer].uv.x),float(l[layer].uv.y)) for l in face.loops]
    return abs(sum(uv[i][0]*uv[(i+1)%len(uv)][1]
                   -uv[(i+1)%len(uv)][0]*uv[i][1]
                   for i in range(len(uv)))*0.5)


def _boundary_signature(bm):
    """Independent of vertex ids; dissolving an interior edge cannot move boundary."""
    def xyz(v):return tuple(round(float(c),6) for c in v.co)
    return sorted(tuple(sorted((xyz(e.verts[0]),xyz(e.verts[1]))))
                  for e in bm.edges if len(e.link_faces)==1)


def protected_path_corners_xy(points, *, lateral_tolerance=1.e-5):
    """Return exact authored XY corners; straight collinear subdivisions excluded.

    This guard is shared by MAIN and SECONDARY, but callers must pass each
    path separately: concatenation would create a fictitious cross-facade edge.
    """
    from math import hypot
    result=set()
    for a,b,c in zip(points,points[1:],points[2:]):
        x0,y0=float(a[0]),float(a[1])
        x1,y1=float(b[0]),float(b[1])
        x2,y2=float(c[0]),float(c[1])
        span=hypot(x2-x0,y2-y0)
        if span<=1.e-10:
            result.add((round(x1,5),round(y1,5)))
            continue
        cross=abs((x1-x0)*(y2-y0)-(y1-y0)*(x2-x0))/span
        ahead=(x1-x0)*(x2-x1)+(y1-y0)*(y2-y1)
        if cross>lateral_tolerance or ahead<=0:
            result.add((round(x1,5),round(y1,5)))
    return result


def protected_path_stations_xy(points):
    """All authored XY positions (not just corners) where a full-height rail is required.

    PRIMARY and SECONDARY must be passed independently by the caller.  A
    collinear authored station is still authored and must not be dissolved.
    """
    result = set()
    for point in points:
        x, y = float(point[0]), float(point[1])
        if not (isfinite(x) and isfinite(y)):
            raise ValueError('Authored Path station is not finite')
        result.add((round(x, 5), round(y, 5)))
    return result


def authored_vertical_edge_signature(mesh, station_xy, *, tolerance=1.e-6):
    """Read-only geometry invariant: authored vertical edges may not disappear.

    Signature intentionally concerns existing edges, not invented missing ones;
    it is checked before and after both reduction passes.
    """
    stations = set(station_xy)
    result = set()
    vertices = mesh.vertices
    for edge in mesh.edges:
        a = vertices[edge.vertices[0]].co
        b = vertices[edge.vertices[1]].co
        xy_a = (round(float(a.x), 5), round(float(a.y), 5))
        xy_b = (round(float(b.x), 5), round(float(b.y), 5))
        if xy_a != xy_b or xy_a not in stations:
            continue
        if abs(float(a.z) - float(b.z)) <= tolerance:
            continue
        z_lo, z_hi = sorted((round(float(a.z), 5), round(float(b.z), 5)))
        result.add((xy_a, z_lo, z_hi))
    return result


def dissolve_redundant_wall_edges(mesh, *, max_passes=64, normal_dot=0.9999995,
                                  uv_tolerance=1.e-6, face_budget=180000,
                                  protected_path_points=(),
                                  protected_corner_xy=(), protected_station_xy=(), allow_caps=True):
    """Optimize generated wall *in place* only after full BMesh validation.

    The caller owns an unpublished temporary Mesh, never the prior Result.
    WALL_OUTER/WALL_INNER are processed independently; flat CAP pairs can
    join only under exactly the same UV/material/role/normal proof.  A rejected
    dissolve never mutates the Blender Mesh because BMesh is temporary.

    All numbers refer to individual candidate edges, not promised removals.
    Counters are intentionally stable diagnostics for 4.x/5.x field reports.
    """
    import bmesh
    from collections import Counter
    from ..core.semantic_geometry import FACE_ROLE_ATTRIBUTE, FACE_ROLE_CODES
    allowed={FACE_ROLE_CODES['WALL_OUTER'], FACE_ROLE_CODES['WALL_INNER']}
    if allow_caps:
        allowed.update((FACE_ROLE_CODES['TOP_CAP'], FACE_ROLE_CODES['BOTTOM_CAP']))
    result={'passes':0,'edges_removed':0,'faces_before':len(mesh.polygons),
            'faces_after':len(mesh.polygons),'candidates':0,
            'skipped_large_mesh':False,'strategy':'DUAL_WALL_CAP_COPLANAR_AFFINE_UV',
            'rejects':{},'removed_by_role':{},'protected_path_points':len(protected_path_points),
            'boundary_preserved':True,'uv_area_preserved':True,
            'geometry_area_preserved':True, 'fixed_point_verified':False}
    if len(mesh.polygons)>face_budget:
        result['skipped_large_mesh']=True;return result
    bm=bmesh.new()
    try:
        bm.from_mesh(mesh)
        layer=bm.loops.layers.uv.active
        roles=bm.faces.layers.int.get(FACE_ROLE_ATTRIBUTE)
        if layer is None or roles is None:
            result['rejects']={'NO_UV_OR_ROLE_LAYER':len(bm.edges)}
            return result
        bm.normal_update()
        baseline_boundary=_boundary_signature(bm)
        baseline_uv=sum(_uv_face_area(f,layer) for f in bm.faces)
        baseline_area=sum(f.calc_area() for f in bm.faces)
        baseline_roles={int(f[roles]) for f in bm.faces}
        # Only authored 3D vertices can be protected here.  The 1.17.7
        # XY-only guard incorrectly protected the ENTIRE vertical column at
        # every authored path XY, including arbitrary generated UV/story rows.
        # use_verts=False also retains boundary vertices after an edge dissolve.
        protected_xyz={tuple(round(float(v),7) for v in p)
                       for p in protected_path_points}
        corner_xy={tuple(round(float(v),5) for v in point)
                   for point in protected_corner_xy}
        station_xy={tuple(round(float(v),5) for v in point)
                    for point in protected_station_xy}
        rejections=Counter();removed_by_role=Counter()
        result['protected_path_points_3d']=len(protected_xyz)
        result['pass_history']=[]
        result['max_passes']=int(max_passes)
        result['stop_reason']='NOT_STARTED'
        if not isinstance(max_passes,int) or max_passes < 1 or max_passes > 512:
            raise ValueError('Topology pass budget must be between 1 and 512')
        for pass_index in range(max_passes):
            claimed=set(); candidates=[];roles_to_remove=[]
            for edge in bm.edges:
                if len(edge.link_faces)!=2:
                    rejections['OPEN_OR_NONMANIFOLD']+=1;continue
                if edge.seam:
                    rejections['MARKED_UV_SEAM']+=1;continue
                if not edge.smooth:
                    rejections['SHARP_EDGE']+=1;continue
                a,b=edge.link_faces
                if a in claimed or b in claimed:
                    rejections['PASS_CONFLICT']+=1;continue
                role_a=int(a[roles]); role_b=int(b[roles])
                if role_a!=role_b or role_a not in allowed:
                    rejections['ROLE_BOUNDARY']+=1;continue
                if a.material_index!=b.material_index:
                    rejections['MATERIAL_BOUNDARY']+=1;continue
                if a.normal.dot(b.normal)<normal_dot:
                    rejections['NONPLANAR_OR_REVERSED_NORMAL']+=1;continue
                # Preserve the *actual* authored point where it occurs on the
                # result. A matching XY coordinate at some unrelated Z is
                # NOT a reason to retain a redundant internal mesh edge.
                # Corners and silhouette edges are independently protected by
                # face normals, strict convexity and full boundary signature.
                if any(tuple(round(float(c),7) for c in v.co) in protected_xyz
                       for v in edge.verts):
                    rejections['AUTHORED_PATH_STATION']+=1;continue
                if role_a in (FACE_ROLE_CODES['WALL_OUTER'], FACE_ROLE_CODES['WALL_INNER']) and any(
                        (round(float(v.co.x),5),round(float(v.co.y),5)) in station_xy
                        for v in edge.verts):
                    rejections['AUTHORED_VERTICAL_STATION_ALL_Z']+=1;continue
                if role_a in (FACE_ROLE_CODES['WALL_OUTER'], FACE_ROLE_CODES['WALL_INNER']) and any(
                        (round(float(v.co.x),5),round(float(v.co.y),5)) in corner_xy
                        for v in edge.verts):
                    rejections['AUTHORED_CORNER_ALL_Z']+=1;continue
                boundary=_boundary_uv_ring(edge,a,b,layer,uv_tolerance)
                if boundary is None:
                    rejections['UV_SEAM_OR_TOPOLOGY']+=1;continue
                verts,uv_ring=boundary
                if not mergeable_uv_boundary(uv_ring):
                    rejections['UV_TILE_OR_CONCAVE']+=1;continue
                if not safe_affine_wall_patch(
                        [tuple(v.co) for v in verts],uv_ring,
                        uv_eps=max(uv_tolerance,1.e-7)):
                    rejections['NONPLANAR_OR_UV_GRADIENT']+=1;continue
                candidates.append(edge)
                roles_to_remove.append(role_a)
                claimed.update((a,b))
            if not candidates:
                result['stop_reason']='FIXED_POINT'
                break
            result['candidates']+=len(candidates)
            before_edge_count=len(bm.edges)
            before_face_count=len(bm.faces)
            # BMesh 4.x/5.x shared signature. Fail-closed before mesh export.
            bmesh.ops.dissolve_edges(bm,edges=candidates,use_verts=False,use_face_split=False)
            bm.normal_update()
            count_removed=before_edge_count-len(bm.edges)
            face_removed=before_face_count-len(bm.faces)
            if count_removed!=len(candidates) or face_removed!=len(candidates):
                raise ValueError('Topology dissolve mismatch: expected one removed edge and face per disjoint pair')
            result['edges_removed']+=count_removed
            for role in roles_to_remove: removed_by_role[role]+=1
            result['passes']+=1
            result['pass_history'].append({'pass':pass_index+1,
                                           'removed_edges':count_removed,
                                           'faces_remaining':len(bm.faces)})
        else:
            # Never advertise a fixed point if a finite pass budget expired.
            result['stop_reason']='PASS_BUDGET_REACHED'
        if result['edges_removed']:
            # Invariants are checked while the original Mesh is still intact.
            if _boundary_signature(bm)!=baseline_boundary:
                raise ValueError('Topology dissolve changed the authored/result boundary')
            if {int(f[roles]) for f in bm.faces}!=baseline_roles:
                raise ValueError('Topology dissolve lost semantic face roles')
            if any(len(f.verts)<3 or f.calc_area()<=1.e-12 for f in bm.faces):
                raise ValueError('Topology dissolve produced a degenerate face')
            uv_after=sum(_uv_face_area(f,layer) for f in bm.faces)
            area_after=sum(f.calc_area() for f in bm.faces)
            if abs(uv_after-baseline_uv)>max(1.e-5,baseline_uv*1.e-5):
                raise ValueError(f'Topology dissolve changed projected UV area: {uv_after} vs {baseline_uv}')
            if abs(area_after-baseline_area)>max(1.e-5,baseline_area*1.e-6):
                raise ValueError(f'Topology dissolve changed geometric area: {area_after} vs {baseline_area}')
            bm.to_mesh(mesh)
            mesh.update()
            result['faces_after']=len(mesh.polygons)
        result['fixed_point_verified']=(result['stop_reason']=='FIXED_POINT')
        # Fail closed if the operator cannot prove that another pass would
        # make no further admissible changes. The stable Result is untouched.
        if not result['fixed_point_verified']:
            raise ValueError('Topology optimization did not reach a verified fixed point')
        result['rejects']=dict(sorted(rejections.items()))
        result['removed_by_role']={name:int(removed_by_role[code])
                                   for name,code in FACE_ROLE_CODES.items()
                                   if removed_by_role[code]}
        return result
    finally:
        bm.free()
