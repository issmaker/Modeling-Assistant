"""Universal Surface Construction Engine / Phase 1: immutable surface topology.

Pure Python, no bpy, bmesh or scene mutation. It accepts polygon vertex rings
in arbitrary 3D orientations, including disconnected patches and inner loops.
It does NOT claim that a surface can already be constructed as a shared tool window.
Consumers must keep their own physical-profile and UV safety validators.
"""
from __future__ import annotations
from collections import defaultdict, Counter
from math import isfinite, dist, sqrt

SCHEMA = 'ISSMAKER_SURFACE_GRAPH_V1'


def _point(p, size, reason):
    v = tuple(float(x) for x in p)
    if len(v) != size or not all(isfinite(x) for x in v):
        raise ValueError(reason)
    return v


def _area(points, ring):
    nx=ny=nz=0.0
    for ia,ib in zip(ring, ring[1:]+ring[:1]):
        a=points[ia]; b=points[ib]
        nx+=(a[1]-b[1])*(a[2]+b[2])
        ny+=(a[2]-b[2])*(a[0]+b[0])
        nz+=(a[0]-b[0])*(a[1]+b[1])
    return 0.5*sqrt(nx*nx+ny*ny+nz*nz)


def _uv_seams(faces,uv,uses):
    if uv is None:return None
    face_uv=[]
    for ring,loops in zip(faces,uv):
        if len(ring)!=len(loops):raise ValueError('SURFACE_GRAPH_UV_LOOP_MISMATCH')
        face_uv.append({v:_point(pt,2,'SURFACE_GRAPH_NONFINITE_UV')
                        for v,pt in zip(ring,loops)})
    return sum(any(dist(face_uv[fa][v],face_uv[fb][v])>1e-5 for v in edge)
               for edge,refs in uses.items() if len(refs)==2
               for fa,fb in [(refs[0][0],refs[1][0])])


def _cycles(points,edge_uses,face_owner):
    """Walk bounded undirected boundary graph; expose branched cases safely."""
    by_component=defaultdict(list)
    for edge,refs in edge_uses.items():
        if len(refs)==1:
            by_component[face_owner[refs[0][0]]].append((edge,refs[0]))
    output={};invalid={}
    for component,edges in sorted(by_component.items()):
        adj=defaultdict(set);directed={};unvisited=set()
        for (a,b),(_fi,src,dst) in edges:
            adj[a].add(b);adj[b].add(a)
            directed[(a,b)]=(src,dst)
            unvisited.add((a,b))
        branch=sorted(v for v,neighbors in adj.items() if len(neighbors)!=2)
        if branch:
            invalid[component]=branch
            output[component]=[]
            continue
        loops=[]
        while unvisited:
            start=min(min(e) for e in unvisited)
            options=sorted(v for v in adj[start] if tuple(sorted((start,v))) in unvisited)
            if not options:break
            nxt=options[0];ring=[start];previous=None;here=start
            for _ in range(len(edges)+1):
                edge=tuple(sorted((here,nxt)))
                if edge not in unvisited:raise ValueError('SURFACE_GRAPH_CYCLE_REPEAT')
                unvisited.remove(edge)
                if nxt==start:break
                ring.append(nxt);previous,here=here,nxt
                nxt=next((v for v in sorted(adj[here]) if v!=previous),None)
                if nxt is None:raise ValueError('SURFACE_GRAPH_BROKEN_LOOP')
            else:raise ValueError('SURFACE_GRAPH_CYCLE_OVERFLOW')
            # Preserve authored face winding when it is coherent, without
            # assuming a global XY plane or trusting unordered BMesh edge sets.
            score=sum(directed[tuple(sorted((a,b)))]==(a,b)
                      for a,b in zip(ring,ring[1:]+ring[:1]))
            if score<len(ring)-score:
                ring=[ring[0]]+list(reversed(ring[1:]))
            length=sum(dist(points[a],points[b]) for a,b in zip(ring,ring[1:]+ring[:1]))
            loops.append({'vertex_ids':ring,'edge_count':len(ring),
                          'perimeter_m':length,'start_vertex':ring[0]})
        loops.sort(key=lambda l:(-l['perimeter_m'],l['start_vertex'],l['vertex_ids']))
        output[component]=loops
    return output,invalid


def analyze(vertices, faces, uv_faces=None, *, source_face_ids=None, max_faces=100000):
    """Return graph components, ordered boundary rings and construction gates.

    All IDs refer to the caller's vertex arrays. No vertex, edge, loop or UV
    is changed. Holes are inferred from manifold topology per component;
    that does not depend on a planar projection of curved wall surfaces.
    """
    points=tuple(_point(p,3,'SURFACE_GRAPH_NONFINITE_VERTEX') for p in vertices)
    rings=[tuple(int(i) for i in face) for face in faces]
    if not rings:raise ValueError('SURFACE_GRAPH_NO_FACES')
    if len(rings)>max_faces or len(points)>4*max_faces+100000:
        raise ValueError('SURFACE_GRAPH_RESOURCE_LIMIT')
    if uv_faces is not None and len(uv_faces)!=len(rings):
        raise ValueError('SURFACE_GRAPH_UV_COUNT_MISMATCH')
    if source_face_ids is None:source=list(range(len(rings)))
    else:
        source=[int(i) for i in source_face_ids]
        if len(source)!=len(rings) or len(set(source))!=len(source):
            raise ValueError('SURFACE_GRAPH_INVALID_SOURCE_FACE_IDS')
    uses=defaultdict(list);area=0.;zero_area=[]
    for fi,ring in enumerate(rings):
        if len(ring)<3 or len(set(ring))!=len(ring) or any(i<0 or i>=len(points) for i in ring):
            raise ValueError('SURFACE_GRAPH_INVALID_FACE_RING')
        ar=_area(points,ring);area+=ar
        if ar<1e-10:zero_area.append(fi)
        for a,b in zip(ring,ring[1:]+ring[:1]):
            uses[tuple(sorted((a,b)))].append((fi,a,b))
    owners=defaultdict(set)
    for refs in uses.values():
        for fi,*_ in refs:
            owners[fi].update(other for other,*_ in refs if other!=fi)
    remaining=set(range(len(rings)));components=[];face_owner={}
    while remaining:
        seed=min(remaining);remaining.remove(seed);frontier=[seed];found=[]
        while frontier:
            fi=frontier.pop();found.append(fi)
            for other in sorted(owners[fi]&remaining):
                remaining.remove(other);frontier.append(other)
        found.sort();cid=len(components);components.append(found)
        for fi in found:face_owner[fi]=cid
    boundaries,branches=_cycles(points,uses,face_owner)
    winding_conflicts=[];nonmanifold=[]
    for edge,refs in sorted(uses.items()):
        if len(refs)>2:nonmanifold.append(edge)
        if len(refs)==2 and refs[0][1:]==refs[1][1:]:winding_conflicts.append(edge)
    reports=[]
    for cid,face_ids in enumerate(components):
        local_edges=set();local_vertices=set();local_area=0.
        for fi in face_ids:
            ring=rings[fi];local_vertices.update(ring);local_area+=_area(points,ring)
            for a,b in zip(ring,ring[1:]+ring[:1]):local_edges.add(tuple(sorted((a,b))))
        loops=boundaries.get(cid,[])
        euler=len(local_vertices)-len(local_edges)+len(face_ids)
        branches_here=branches.get(cid,[])
        nonmanifold_here=sum(edge in local_edges for edge in nonmanifold)
        closed_loops=not branches_here and not nonmanifold_here
        # For an orientable genus-zero sheet, b=2-chi. A ring with an inner
        # opening has b=2; a disk b=1. Never mislabel higher-genus geometry.
        estimated_holes=(len(loops)-1 if closed_loops and loops else None)
        orientable=not any(edge in local_edges for edge in winding_conflicts)
        genus=(2-len(loops)-euler)/2 if closed_loops and orientable else None
        if genus is not None and (genus<0 or abs(genus-round(genus))>1e-6):
            genus=None;estimated_holes=None
        status=('NONMANIFOLD' if nonmanifold_here else
                'BRANCHED_BOUNDARY' if branches_here else
                'DEGENERATE_FACES' if any(fi in zero_area for fi in face_ids) else
                'INCONSISTENT_WINDING' if not orientable else
                'VALID_SURFACE_GRAPH')
        reports.append({'component':cid,'face_indices':face_ids,
                        'source_face_ids':[source[fi] for fi in face_ids],
                        'vertices':len(local_vertices),'edges':len(local_edges),
                        'euler_characteristic':euler,'genus':genus,
                        'boundary_loops':loops,'boundary_count':len(loops),
                        'hole_count':estimated_holes,
                        'branch_vertices':branches_here,
                        'surface_area_m2':local_area,'status':status,
                        'legacy_single_loop_compatible':status=='VALID_SURFACE_GRAPH' and len(loops)==1})
    seam_count=_uv_seams(rings,uv_faces,uses)
    return {'schema':SCHEMA,'components':reports,'component_count':len(reports),
            'boundary_count':sum(r['boundary_count'] for r in reports),
            'nonmanifold_edges':nonmanifold,
            'winding_conflict_edges':winding_conflicts,
            'zero_area_face_indices':zero_area,
            'source_uv_seams':seam_count,
            'total_surface_area_m2':area,
            'legacy_single_loop_compatible':len(reports)==1 and reports[0]['legacy_single_loop_compatible'],
            'source_vertices_unchanged':True,'source_faces_unchanged':True,
            'source_uv_unchanged':True}
