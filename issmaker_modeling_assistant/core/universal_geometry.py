"""Shared non-destructive geometry analysis for all tools and wall shapes.

No Blender dependencies, no classification-specific rejection and no hidden
mesh modifications. Tools consume the same measurements, but their distinct
geometry generators remain responsible for making physical geometry.
"""
from __future__ import annotations
from collections import Counter,defaultdict
import math
import statistics

SCHEMA='ISSMAKER_UNIVERSAL_GEOMETRY_ANALYSIS_V1'


def _vec(v):
    t=tuple(float(x) for x in v)
    if len(t)!=3 or not all(math.isfinite(x) for x in t):
        raise ValueError('UGA_NONFINITE_POINT')
    return t


def _length(a,b):
    return math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))


def _face_area(points):
    n=(0.,0.,0.)
    nx=ny=nz=0.
    for a,b in zip(points,points[1:]+points[:1]):
        nx+=(a[1]-b[1])*(a[2]+b[2])
        ny+=(a[2]-b[2])*(a[0]+b[0])
        nz+=(a[0]-b[0])*(a[1]+b[1])
    return math.sqrt(nx*nx+ny*ny+nz*nz)*.5


def _degrees(a,b,c):
    x=[b[i]-a[i] for i in range(3)]
    y=[c[i]-b[i] for i in range(3)]
    d1=math.sqrt(sum(z*z for z in x));d2=math.sqrt(sum(z*z for z in y))
    if min(d1,d2)<=1e-12:return 180.
    return math.degrees(math.acos(max(-1.,min(1.,sum(u*v for u,v in zip(x,y))/(d1*d2)))))


def analyze_polyline(points, closed=False, *, protect=()):
    """Measure actual geometry; no flat/curved/corner mode switch."""
    vertices=tuple(_vec(p) for p in points)
    n=len(vertices)
    if closed and n>2 and _length(vertices[0],vertices[-1])<=1e-12:
        vertices=vertices[:-1];n=len(vertices)
    edges=[(i,(i+1)%n) for i in range(n if closed else max(n-1,0))]
    lengths=[_length(vertices[i],vertices[j]) for i,j in edges]
    nonzero=[v for v in lengths if v>1e-10]
    median=float(statistics.median(nonzero)) if nonzero else 0.
    short_threshold=min(.12,max(.002,median*.18)) if median else 0.
    shorts=[i for i,v in enumerate(lengths) if v>1e-10 and v<short_threshold]
    angles=[]
    for i in range(n if closed else max(n-2,0)):
        j=i if closed else i+1
        angles.append((j,_degrees(vertices[(j-1)%n],vertices[j],vertices[(j+1)%n])))
    protected=set(int(i) for i in protect if 0<=int(i)<n)
    if not closed:protected.update((0,n-1))
    protected.update(i for i,angle in angles if angle>=45.)
    return {'schema':SCHEMA,'kind':'POLYLINE','closed':bool(closed),
            'vertices':n,'segments':len(edges),
            'min_edge_m':min(lengths) if lengths else 0.,
            'median_edge_m':median,'max_edge_m':max(lengths) if lengths else 0.,
            'short_edge_count':len(shorts),'short_edge_indices':shorts,
            'short_edge_threshold_m':short_threshold,
            'zero_edge_count':len(lengths)-len(nonzero),
            'max_turn_deg':max((a for _,a in angles),default=0.),
            'hard_junction_indices':sorted(protected),
            'max_coord_m':max((abs(x) for p in vertices for x in p),default=0.),
            'source_intact':True}


def analyze_patch(vertices, faces, uv_faces=None):
    """Inspect arbitrary selected polygon patches, including disjoint islands.

    vertices: local/world XYZ, faces: vertex-index rings, uv_faces: optional
    per-face per-loop UV coordinates. Non-manifold and holes are reported,
    never assumed to be invalid for other tools.
    """
    points=tuple(_vec(v) for v in vertices)
    polygons=[tuple(int(i) for i in face) for face in faces]
    if not polygons:raise ValueError('UGA_NO_FACES')
    if len(polygons)>500000:raise ValueError('UGA_RESOURCE_LIMIT')
    uses=defaultdict(list);adj=defaultdict(set);areas=[];area_invalid=0
    uniqueverts=set();material_edges=[]
    for fidx,poly in enumerate(polygons):
        if len(poly)<3 or any(i<0 or i>=len(points) for i in poly):
            raise ValueError('UGA_INVALID_FACE_RING')
        if len(set(poly))!=len(poly):raise ValueError('UGA_REPEAT_FACE_VERTEX')
        uniqueverts.update(poly)
        area=_face_area([points[i] for i in poly]);areas.append(area)
        if area<=1e-10:area_invalid+=1
        for a,b in zip(poly,poly[1:]+poly[:1]):
            key=tuple(sorted((a,b)))
            uses[key].append(fidx)
    for refs in uses.values():
        for a in refs:
            adj[a].update(ref for ref in refs if ref!=a)
    pending=set(range(len(polygons)));connected=[]
    while pending:
        seed=min(pending);pending.remove(seed);stack=[seed];size=0
        while stack:
            f=stack.pop();size+=1
            for nei in adj[f]&pending:
                pending.remove(nei);stack.append(nei)
        connected.append(size)
    boundary=[key for key,ref in uses.items() if len(ref)==1]
    degree=Counter(i for edge in boundary for i in edge)
    boundadj=defaultdict(set)
    for a,b in boundary:boundadj[a].add(b);boundadj[b].add(a)
    remaining=set(boundadj);boundary_components=[]
    while remaining:
        v=min(remaining);remaining.remove(v);stack=[v];size=0
        while stack:
            here=stack.pop();size+=1
            for nxt in boundadj[here]&remaining:
                remaining.remove(nxt);stack.append(nxt)
        boundary_components.append(size)
    lens=[_length(points[a],points[b]) for a,b in uses]
    med=statistics.median([v for v in lens if v>1e-10]) if any(v>1e-10 for v in lens) else 0.
    short_threshold=min(.12,max(.002,med*.18)) if med else 0.
    seams=0
    if uv_faces is not None:
        if len(uv_faces)!=len(polygons):raise ValueError('UGA_UV_COUNT_MISMATCH')
        uvlookup=defaultdict(list)
        for f,ring in enumerate(polygons):
            if len(uv_faces[f])!=len(ring):raise ValueError('UGA_UV_LOOP_MISMATCH')
            for idx,v in enumerate(ring):
                c=tuple(float(x) for x in uv_faces[f][idx]);
                if len(c)!=2 or not all(math.isfinite(v) for v in c):raise ValueError('UGA_NONFINITE_UV')
                uvlookup[(f,v)]=c
        for (a,b),refs in uses.items():
            if len(refs)==2:
                fa,fb=refs
                if any(math.dist(uvlookup[(fa,v)],uvlookup[(fb,v)])>1e-5 for v in (a,b)):
                    seams+=1
    return {'schema':SCHEMA,'kind':'PATCH','face_count':len(polygons),
            'vertex_count':len(uniqueverts),'edge_count':len(uses),
            'connected_components':len(connected),'component_faces':sorted(connected),
            'boundary_edge_count':len(boundary),'boundary_components':len(boundary_components),
            'boundary_branch_vertices':sum(count!=2 for count in degree.values()),
            'nonmanifold_edges':sum(len(ref)>2 for ref in uses.values()),
            'zero_area_faces':area_invalid,'min_area_m2':min(areas),
            'min_edge_m':min(lens) if lens else 0.,'median_edge_m':med,
            'short_edge_count':sum(0<v<short_threshold for v in lens),
            'short_edge_threshold_m':short_threshold,
            'internal_uv_seams':seams if uv_faces is not None else None,
            'source_intact':True}
