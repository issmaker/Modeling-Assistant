"""UV-safe collinear boundary-vertex normalization of a staged WALL Mesh.

Only considers an open-boundary vertex with exactly two incident edges and
one face: interior mesh crossings, WALL↔CAP shared seams, UV module edges,
authored points and non-planar triangulation remain untouched.

The BMesh operation runs exclusively on an unpublished temporary mesh.
"""
from math import isfinite, sqrt


def straight_uv_vertex(a, mid, b, uv_a, uv_mid, uv_b,
                       *, distance_tolerance=1e-5, uv_tolerance=2e-6):
    """Pure predicate; a safely removable point cannot reverse a straight line."""
    try:
        a, mid, b = [tuple(float(t) for t in x) for x in (a, mid, b)]
        ua, um, ub = [tuple(float(t) for t in x) for x in (uv_a, uv_mid, uv_b)]
    except (TypeError, ValueError):
        return False
    if not (len(a)==len(mid)==len(b)==3 and len(ua)==len(um)==len(ub)==2):
        return False
    if not all(isfinite(v) for pt in (a, mid, b, ua, um, ub) for v in pt):
        return False
    line=tuple(c-p for p,c in zip(a,b))
    length_sq=sum(q*q for q in line)
    if length_sq <= (distance_tolerance*2)**2:
        return False
    t=sum((m-p)*l for m,p,l in zip(mid,a,line))/length_sq
    if not (1e-6 < t < 1-1e-6):
        return False
    perpendicular=sqrt(sum((m-p-t*l)**2 for m,p,l in zip(mid,a,line)))
    if perpendicular > distance_tolerance:
        return False
    return max(abs(m-(p+t*(q-p))) for m,p,q in zip(um,ua,ub)) <= uv_tolerance


def remove_pass_through_boundary_vertices(mesh, *, tile_length=4.0,
                                          protected_path_points=(),
                                          protected_corner_xy=(), protected_station_xy=(), max_removed=100000):
    """Collapse only single-face boundary pass-throughs, fail-closed.

    Returns a fully measured report; no speculative removal of module crossings.
    """
    import bmesh
    from .wall_topology import _uv_face_area
    from ..core.semantic_geometry import FACE_ROLE_ATTRIBUTE
    if not isfinite(float(tile_length)) or tile_length<=0:
        raise ValueError('Vertex minimal: invalid UV module length')
    tolerance=max(2e-6,min(1e-5,float(tile_length)*2.5e-6))
    xyz_protected={tuple(round(float(x),7) for x in p)
                   for p in protected_path_points}
    xy_corners={tuple(round(float(x),5) for x in p)
                for p in protected_corner_xy}
    xy_stations={tuple(round(float(x),5) for x in p)
                 for p in protected_station_xy}
    report={'strategy':'STRICT_DEG2_OPEN_BOUNDARY_AFFINE_UV',
            'candidates':0,'removed':0,'boundary_vertices_before':0,
            'boundary_vertices_after':0,'checked':0,
            'protected_authored':0,'uv_or_geometry_reject':0,
            'module_crossings_preserved':True,'shared_cap_seams_preserved':True,
            'uv_area_preserved':True,'geometry_area_preserved':True,
            'status':'NO_CHANGE'}
    bm=bmesh.new()
    try:
        bm.from_mesh(mesh)
        uv=bm.loops.layers.uv.active
        roles=bm.faces.layers.int.get(FACE_ROLE_ATTRIBUTE)
        if uv is None or roles is None:
            report['status']='NO_UV_OR_ROLE_LAYER'
            return report
        bm.normal_update()
        area0=sum(f.calc_area() for f in bm.faces)
        uv_area0=sum(_uv_face_area(f,uv) for f in bm.faces)
        original_roles=sorted(int(f[roles]) for f in bm.faces)
        report['boundary_vertices_before']=sum(v.is_boundary for v in bm.verts)
        candidates=[]
        # Decouple selection from BMesh mutation. Multiple eligible vertices
        # in one face are processed one by one with fresh validation.
        for v in list(bm.verts):
            report['checked']+=1
            if not v.is_valid or not v.is_boundary or len(v.link_edges)!=2 or len(v.link_faces)!=1:
                continue
            if (tuple(round(float(c),7) for c in v.co) in xyz_protected or
                (round(float(v.co.x),5),round(float(v.co.y),5)) in xy_corners or
                (round(float(v.co.x),5),round(float(v.co.y),5)) in xy_stations):
                report['protected_authored']+=1
                continue
            if any(edge.seam or not edge.smooth for edge in v.link_edges):
                continue
            face=v.link_faces[0]
            if len(face.verts)<=3:
                continue  # deleting a triangle vertex would delete the face
            loops=[loop for loop in face.loops if loop.vert==v]
            if len(loops)!=1:
                continue
            loop=loops[0]
            a=loop.link_loop_prev.vert
            b=loop.link_loop_next.vert
            if a==b:
                continue
            if not straight_uv_vertex(a.co, v.co, b.co,
                                      loop.link_loop_prev[uv].uv,
                                      loop[uv].uv,
                                      loop.link_loop_next[uv].uv,
                                      distance_tolerance=tolerance):
                report['uv_or_geometry_reject']+=1
                continue
            candidates.append(v)
        report['candidates']=len(candidates)
        if len(candidates)>max_removed:
            raise ValueError('Vertex minimal: safety candidate budget exceeded')
        for v in candidates:
            if not v.is_valid or len(v.link_edges)!=2 or len(v.link_faces)!=1:
                continue
            f=v.link_faces[0]
            if len(f.verts)<=3:
                continue
            uv_layer=bm.loops.layers.uv.active
            loop=next((l for l in f.loops if l.vert==v),None)
            if loop is None:
                continue
            if not straight_uv_vertex(loop.link_loop_prev.vert.co,v.co,loop.link_loop_next.vert.co,
                                      loop.link_loop_prev[uv_layer].uv,loop[uv_layer].uv,
                                      loop.link_loop_next[uv_layer].uv,
                                      distance_tolerance=tolerance):
                continue
            bmesh.ops.dissolve_verts(bm,verts=[v],use_face_split=False,use_boundary_tear=False)
            report['removed']+=1
        if report['removed']:
            bm.normal_update()
            area1=sum(f.calc_area() for f in bm.faces)
            uv_area1=sum(_uv_face_area(f,uv) for f in bm.faces)
            if abs(area1-area0)>max(1e-5,area0*1e-6):
                raise ValueError('Vertex minimal: geometric area changed')
            if abs(uv_area1-uv_area0)>max(1e-5,uv_area0*1e-6):
                raise ValueError('Vertex minimal: UV area changed')
            if sorted(int(f[roles]) for f in bm.faces)!=original_roles:
                raise ValueError('Vertex minimal: semantic roles changed')
            if any(len(f.verts)<3 or f.calc_area()<1e-12 for f in bm.faces):
                raise ValueError('Vertex minimal: degenerate polygon')
            bm.to_mesh(mesh)
            mesh.update()
            report['status']='REMOVED'
        report['boundary_vertices_after']=sum(v.is_boundary for v in bm.verts)
        return report
    finally:
        bm.free()
