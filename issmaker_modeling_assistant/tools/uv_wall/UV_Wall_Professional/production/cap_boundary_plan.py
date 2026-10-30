"""UV Wall 1.18.2 -- conservative, Blender-independent shared WALL/CAP rim plan.

Use prewelded vertices, but do not construct a Blender Mesh until contact
vertices have been inserted in *boundary* polygon loops only.  The checker
never visits the interior of a WALL module for a TOP_CAP attachment.
"""
from collections import defaultdict
from math import isfinite, sqrt

WALL_ROLES = frozenset(("WALL_OUTER", "WALL_INNER", "END_CAP"))
CAP_ROLES = frozenset(("TOP_CAP", "BOTTOM_CAP"))


def _v3(x):
    coords = tuple(float(t) for t in x)
    if len(coords) != 3 or not all(isfinite(t) for t in coords):
        raise ValueError("WALL/CAP: nonfinite XYZ")
    return coords


def _boundary_edges(faces, roles, side):
    """Boundary per *separate* WALL side / CAP side, by topological index.

    WALL_OUTER and WALL_INNER must never cancel each other's edges.
    TOP_CAP and BOTTOM_CAP must never cancel each other's edges.
    Returns polygon id, directed edge-start slot, vertex IDs and role.
    """
    buckets = defaultdict(list)
    for fi, (face, role) in enumerate(zip(faces, roles)):
        if role not in side:
            continue
        for i, src in enumerate(face):
            dest = face[(i+1) % len(face)]
            key = (role, min(src,dest), max(src,dest))
            buckets[key].append((fi, i, src, dest, role))
    if any(len(uses) > 2 for uses in buckets.values()):
        raise ValueError("WALL/CAP: nonmanifold intra-surface edge")
    return [uses[0] for uses in buckets.values() if len(uses) == 1]


def extract_wall_rim(vertices, faces, roles):
    """Record wall boundary edge identities before building any cap faces."""
    if len(faces) != len(roles):
        raise ValueError("WALL/CAP: role count mismatch")
    edges = _boundary_edges(faces, roles, WALL_ROLES)
    return {
        "outer_edges": sum(item[4] == "WALL_OUTER" for item in edges),
        "inner_edges": sum(item[4] == "WALL_INNER" for item in edges),
        "end_cap_edges": sum(item[4] == "END_CAP" for item in edges),
        "boundary_edges": len(edges),
        "boundary_vertex_count": len({v for edge in edges for v in edge[2:4]}),
    }


def _fraction_on_edge(point, start, end, tol):
    delta = [b-a for a,b in zip(start, end)]
    sq = sum(t*t for t in delta)
    if sq <= (tol*2)**2:
        return None
    t = sum((p-a)*d for p,a,d in zip(point,start,delta))/sq
    inset = tol/sqrt(sq)
    if t <= inset or t >= 1.0-inset:
        return None
    err = sum((p-(a+t*d))**2 for p,a,d in zip(point,start,delta))
    return (t,sqrt(err)) if err <= tol*tol else None


def _edge_insertion_plan(edges, candidate_ids, vertices, tol, *, max_insertions):
    """Only insert opposite-surface *boundary* vertices into these edges."""
    plan = defaultdict(list)
    checked = 0
    for fi, slot, va, vb, role in edges:
        a,b = vertices[va], vertices[vb]
        lo = [min(x,y)-tol for x,y in zip(a,b)]
        hi = [max(x,y)+tol for x,y in zip(a,b)]
        line = []
        for idx in candidate_ids:
            if idx in (va,vb):
                continue
            p = vertices[idx]
            if any(q < l or q > h for q,l,h in zip(p,lo,hi)):
                continue
            fit = _fraction_on_edge(p,a,b,tol)
            checked += 1
            if fit is not None:
                line.append((fit[0],fit[1],idx))
        if line:
            # Repeated vertices along UV tile boundaries count once. If two
            # different point IDs represent the same XYZ within tolerance,
            # prefer nearest projected candidate.
            line.sort(key=lambda e:(e[0],e[1],e[2]))
            distinct = []
            for item in line:
                if distinct and abs(item[0]-distinct[-1][0])*sqrt(sum((y-x)**2 for x,y in zip(a,b))) <= tol:
                    if item[1] < distinct[-1][1]:
                        distinct[-1] = item
                else:
                    distinct.append(item)
            plan[(fi,slot)] = [(t,idx) for t,err,idx in distinct]
            if sum(map(len,plan.values())) > max_insertions:
                raise ValueError("WALL/CAP: boundary insertion limit exceeded")
    return plan, checked


def _add_plan_to_faces(faces, uvs, plan, *, allowed_face_indices):
    new_faces=[]
    new_uvs=[]
    count=0
    for fi,(face,ring) in enumerate(zip(faces,uvs)):
        if len(face) != len(ring):
            raise ValueError("WALL/CAP: UV corner count mismatch")
        f=[]; v=[]
        for i,vertex in enumerate(face):
            f.append(vertex);v.append(tuple(float(q) for q in ring[i]))
            if (fi,i) not in plan:
                continue
            if fi not in allowed_face_indices:
                raise ValueError("WALL/CAP: attempted insertion into internal face")
            after = tuple(float(q) for q in ring[(i+1)%len(ring)])
            before = tuple(float(q) for q in ring[i])
            for t,idx in plan[(fi,i)]:
                f.append(idx)
                v.append(tuple(x+(y-x)*t for x,y in zip(before,after)))
                count += 1
        if len(set(f)) != len(f) or len(f)<3:
            raise ValueError("WALL/CAP: boundary planning creates degenerate polygon")
        new_faces.append(tuple(f));new_uvs.append(tuple(v))
    return new_faces,new_uvs,count


def plan_shared_rim(vertices,faces,face_uvs,face_roles,*, tolerance=4e-6,
                    max_insertions=40000):
    """Produce only boundary-loop insertions BEFORE mesh.from_pydata.

    CAP interior and WALL side/floor/module boundaries cannot be used for
    contact insertions: geometric coincidence with opposite *boundary*
    vertices is required. Leaves original XYZ list and original loop UVs.
    """
    if len(faces)!=len(face_uvs) or len(faces)!=len(face_roles):
        raise ValueError("WALL/CAP: inconsistent face and UV arrays")
    if not isfinite(tolerance) or tolerance <= 0:
        raise ValueError("WALL/CAP: invalid tolerance")
    coords=[_v3(p) for p in vertices]
    wall_edges=_boundary_edges(faces,face_roles,WALL_ROLES)
    cap_edges=_boundary_edges(faces,face_roles,CAP_ROLES)
    wall_ids=sorted({q for item in wall_edges for q in item[2:4]})
    cap_ids=sorted({q for item in cap_edges for q in item[2:4]})
    wp,wchecked=_edge_insertion_plan(wall_edges,cap_ids,coords,tolerance,
                                     max_insertions=max_insertions)
    cp,cchecked=_edge_insertion_plan(cap_edges,wall_ids,coords,tolerance,
                                     max_insertions=max_insertions)
    if set(wp).intersection(cp):
        raise ValueError("WALL/CAP: overlapping role faces")
    allowed={fi for fi,*_ in wall_edges+cap_edges}
    new_faces,new_uvs,count=_add_plan_to_faces(faces,face_uvs,{**wp,**cp},
                                                allowed_face_indices=allowed)
    wall_count=sum(len(x) for x in wp.values())
    cap_count=sum(len(x) for x in cp.values())
    if count!=wall_count+cap_count:
        raise ValueError("WALL/CAP: insertion accounting mismatch")
    return {
        "faces":new_faces,
        "face_uvs":new_uvs,
        "report":{
            "schema":"BOUNDARY_FIRST_1182",
            "wall_boundary_edges":len(wall_edges),
            "cap_boundary_edges":len(cap_edges),
            "cap_to_wall_loop_insertions":wall_count,
            "wall_to_cap_loop_insertions":cap_count,
            "contact_faces_modified":len({fi for fi,_ in (*wp.keys(),*cp.keys())}),
            "wall_internal_faces_modified":0,
            "cap_internal_faces_modified":0,
            "geometry_vertices_added":0,
            "face_count_unchanged":True,
            "original_uv_corners_preserved":True,
            "wall_candidate_pairs_checked":wchecked,
            "cap_candidate_pairs_checked":cchecked,
            "late_mesh_edge_stitch_required":False,
        },
    }


def converge_shared_rim(vertices, faces, face_uvs, face_roles, *, tolerance=4e-6,
                        max_rounds=12, max_total_insertions=40000):
    """Bounded boundary-only contact refinement before Mesh authoring.

    An inserted rim vertex can expose a later contact on a previously
    unsplit edge. A single pass followed by an immediate assertion rejected
    otherwise valid sloped caps. Each pass preserves all original XYZ,
    face counts and pre-existing UV loops. No tolerance is relaxed.

    Failure is always fail-closed: no Mesh is created by this function.
    """
    if not 1 <= max_rounds <= 64 or max_total_insertions < 1:
        raise ValueError('WALL/CAP: invalid fixed-point safety bounds')
    current_faces = faces
    current_uvs = face_uvs
    cap_to_wall = 0
    wall_to_cap = 0
    rounds = 0
    first_report = None
    for _ in range(max_rounds):
        result = plan_shared_rim(vertices, current_faces, current_uvs, face_roles,
                                 tolerance=tolerance,
                                 max_insertions=max_total_insertions)
        report = result['report']
        if first_report is None:
            first_report = dict(report)
        count_cap = report['cap_to_wall_loop_insertions']
        count_wall = report['wall_to_cap_loop_insertions']
        count = count_cap + count_wall
        if count == 0:
            final_report = dict(first_report)
            final_report.update(
                cap_to_wall_loop_insertions=cap_to_wall,
                wall_to_cap_loop_insertions=wall_to_cap,
                contact_rounds=rounds,
                final_verified_no_insertions=True,
                fixed_point_verified=True,
                geometry_vertices_added=0,
                wall_internal_faces_modified=0,
                cap_internal_faces_modified=0,
            )
            return {'faces': current_faces, 'face_uvs': current_uvs,
                    'report': final_report}
        if count + cap_to_wall + wall_to_cap > max_total_insertions:
            raise ValueError('WALL/CAP: contact insertion budget exceeded')
        if result['faces'] == current_faces:
            raise ValueError('WALL/CAP: contact plan made no progress')
        # Strictly monotone polygon-loop growth is the termination invariant.
        old_loop_count = sum(len(f) for f in current_faces)
        new_loop_count = sum(len(f) for f in result['faces'])
        if new_loop_count != old_loop_count + count:
            raise ValueError('WALL/CAP: contact refinement lost polygon loops')
        cap_to_wall += count_cap
        wall_to_cap += count_wall
        rounds += 1
        current_faces, current_uvs = result['faces'], result['face_uvs']
    # A final read-only proof: do not silently accept unresolved contact.
    after = plan_shared_rim(vertices, current_faces, current_uvs, face_roles,
                            tolerance=tolerance,
                            max_insertions=max_total_insertions)['report']
    outstanding = (after['cap_to_wall_loop_insertions'] +
                   after['wall_to_cap_loop_insertions'])
    if outstanding:
        raise ValueError('WALL/CAP: bounded fixed-point did not converge '
                         f'after {max_rounds} rounds; '
                         f'{outstanding} unmatched contact vertices')
    final_report = dict(first_report)
    final_report.update(cap_to_wall_loop_insertions=cap_to_wall,
                        wall_to_cap_loop_insertions=wall_to_cap,
                        contact_rounds=rounds,
                        final_verified_no_insertions=True,
                        fixed_point_verified=True,
                        geometry_vertices_added=0,
                        wall_internal_faces_modified=0,
                        cap_internal_faces_modified=0)
    return {'faces': current_faces, 'face_uvs': current_uvs,
            'report': final_report}
