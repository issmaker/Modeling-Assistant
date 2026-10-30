"""Pure Python, non-mutating geometry inspector core.

Operates on a frozen snapshot of vertices/edges, never bpy data. Each selected
connected component is reported, including invalid/branched/open components.
"""
from __future__ import annotations
from collections import Counter, defaultdict, deque
from math import sqrt, isfinite


def _vecsub(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _dot(a, b): return sum(x*y for x, y in zip(a, b))
def _cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _norm(a): return sqrt(_dot(a,a))


def _loop_from_component(edge_pairs, edge_ids):
    graph = defaultdict(list)
    for ei in edge_ids:
        a,b = edge_pairs[ei]
        graph[a].append(b); graph[b].append(a)
    degree = {k:len(v) for k,v in graph.items()}
    if not graph or any(d!=2 for d in degree.values()):
        return [], degree
    start = min(graph)
    loop = [start]
    prev = None; cur = start
    for _ in range(len(edge_ids)):
        options = sorted(graph[cur])
        nxt = options[0] if prev is None else next((x for x in options if x != prev), None)
        if nxt is None: return [], degree
        if nxt == start:
            return (loop if len(loop)==len(degree) else []), degree
        if nxt in loop: return [], degree
        loop.append(nxt)
        prev,cur = cur,nxt
    return [], degree


def _crossing_edges(pts, normal):
    """Count non-adjacent proper segment crossings in a projected closed ring."""
    if len(pts)>512:
        return None
    if _norm(normal)>1e-12:
        dropped=max(range(3),key=lambda k:abs(normal[k]))
    else:
        span=[max(p[k] for p in pts)-min(p[k] for p in pts) for k in range(3)]
        dropped=min(range(3),key=lambda k:span[k])
    use=[k for k in range(3) if k!=dropped]
    p2=[(p[use[0]],p[use[1]]) for p in pts]
    def orient(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    total=0;n=len(p2)
    for i in range(n):
        a=p2[i];b=p2[(i+1)%n]
        for j in range(i+1,n):
            if j==i or j==(i+1)%n or i==(j+1)%n:continue
            c=p2[j];d=p2[(j+1)%n]
            ab_c,ab_d=orient(a,b,c),orient(a,b,d)
            cd_a,cd_b=orient(c,d,a),orient(c,d,b)
            if ab_c*ab_d < -1e-16 and cd_a*cd_b < -1e-16:total+=1
    return total


def _loop_shape(vertices, loop):
    pts = [vertices[i] for i in loop]
    perimeter = sum(_norm(_vecsub(pts[(i+1)%len(pts)],pts[i])) for i in range(len(pts)))
    # Newell's method remains stable for rotated facades and collinear vertices.
    n=(0.0,0.0,0.0)
    for a,b in zip(pts,pts[1:]+pts[:1]):
        n=(n[0]+(a[1]-b[1])*(a[2]+b[2]),
           n[1]+(a[2]-b[2])*(a[0]+b[0]),
           n[2]+(a[0]-b[0])*(a[1]+b[1]))
    size=_norm(n)
    crossings=_crossing_edges(pts,n)
    if size < 1e-12:
        return {'perimeter':round(perimeter,6),'plane_status':'DEGENERATE','corner_indices':[],
                'rectangular_candidate':False,'area':0.0,
                'self_intersections':crossings,'self_intersection_check':
                    'CHECKED' if crossings is not None else 'SKIPPED_TOO_LARGE'}
    normal=tuple(x/size for x in n)
    area=size/2
    deviations=[abs(_dot(_vecsub(p,pts[0]),normal)) for p in pts]
    corners=[]; bad_angles=[]
    for i,p in enumerate(pts):
        prev = pts[i-1]; nxt = pts[(i+1)%len(pts)]
        incoming=_vecsub(p,prev); outgoing=_vecsub(nxt,p)
        il,ol=_norm(incoming),_norm(outgoing)
        if il<1e-10 or ol<1e-10: continue
        sin_turn=_norm(_cross(incoming,outgoing))/(il*ol)
        if sin_turn>0.015:
            corners.append(loop[i])
            bad_angles.append(abs(_dot(incoming,outgoing)/(il*ol)))
    tol=max(0.00025,perimeter*1e-5)
    planar=max(deviations)<=tol
    rect=planar and crossings==0 and len(corners)==4 and all(x<0.04 for x in bad_angles)
    return {'perimeter':round(perimeter,6),'area':round(area,6),
            'plane_status':'PLANAR' if planar else 'NONPLANAR',
            'max_plane_deviation':round(max(deviations),8),
            'plane_tolerance':round(tol,8),'normal_world':[round(c,7) for c in normal],
            'corner_indices':corners,'corner_count':len(corners),
            'rectangular_candidate':bool(rect),'self_intersections':crossings,
            'self_intersection_check':'CHECKED' if crossings is not None else 'SKIPPED_TOO_LARGE'}


def _diagnostic_class(shape, status):
    """Forensics only: these labels NEVER grant permission to create windows."""
    if status!='CLOSED_LOOP':
        return 'OPEN_CHAIN_REVIEW'
    if shape.get('rectangular_candidate'):
        return 'PLANAR_RECTANGLE_CANDIDATE'
    if shape.get('plane_status')=='DEGENERATE':
        return 'DEGENERATE_REVIEW'
    deviation=shape.get('max_plane_deviation',0)
    if shape.get('corner_count')==4 and deviation<=0.001 and shape.get('self_intersections')==0:
        return 'NEAR_PLANAR_FOUR_CORNERS_REVIEW'
    if deviation>0.01:
        return 'SPATIALLY_NONPLANAR_REVIEW'
    return 'OTHER_CONTOUR_REVIEW'


def _closing_path(vertices, edges, edge_face_counts, selected_ids, endpoints, max_edges=8):
    """Suggest existing *unselected boundary-edge* paths, never create geometry.

    An existing path is a hint for visual selection review, not proof that its
    edges belong to a missing window side. Limit exploration for large hosts.
    """
    if len(endpoints)!=2:
        return {'reason':'NOT_TWO_ENDPOINTS'}
    a,b=endpoints
    separation=_norm(_vecsub(vertices[a],vertices[b]))
    import heapq
    adj=defaultdict(list)
    for i,(v,w) in enumerate(edges):
        if i not in selected_ids and edge_face_counts[i]==1:
            dist=_norm(_vecsub(vertices[v],vertices[w]))
            adj[v].append((w,i,dist));adj[w].append((v,i,dist))
    heap=[(0.0,a,(),frozenset((a,)))];result=None
    # Each visit is limited to 8 edges; bounded in pathological cases.
    visited=0
    while heap and visited<30000:
        dist,v,path,seen=heapq.heappop(heap);visited+=1
        if v==b:
            result={'edge_ids':list(path),'length_m':round(dist,6),'edge_count':len(path)}
            break
        if len(path)>=max_edges:continue
        for w,i,d in adj[v]:
            if w not in seen:
                heapq.heappush(heap,(dist+d,w,path+(i,),seen|{w}))
    return {'endpoint_distance_m':round(separation,6),'existing_boundary_path':result,
            'path_is_suggestion_only':True,
            'reason':'NO_SHORT_EXISTING_BOUNDARY_PATH' if result is None else 'UNSELECTED_BOUNDARY_PATH_EXISTS'}


def inspect_selected_edges(vertices, edges, selected_edge_ids, edge_face_counts=None,
                           auto_edge_sets=None, selected_vertex_ids=None):
    """Return comprehensive, deterministic information about every selected component.

    Indices are per-snapshot and may change after Blender mesh editing. `auto_edge_sets`
    is an optional list of cached automatic candidate sets: this is a comparison only.
    """
    selected=sorted(set(int(i) for i in selected_edge_ids))
    vselected=sorted(set(int(i) for i in (selected_vertex_ids or [])))
    if any(not 0<=i<len(edges) for i in selected):raise ValueError('Selected edge index outside mesh')
    if any(not 0<=i<len(vertices) for i in vselected):raise ValueError('Selected vertex index outside mesh')
    face_counts=list(edge_face_counts or [0]*len(edges))
    if len(face_counts)!=len(edges):raise ValueError('edge_face_counts length mismatch')
    chosen=set(selected)
    vertex_to_edges=defaultdict(list)
    for eid in selected:
        a,b=edges[eid]
        if not 0<=a<len(vertices) or not 0<=b<len(vertices):
            raise ValueError('Invalid edge endpoints')
        vertex_to_edges[a].append(eid);vertex_to_edges[b].append(eid)
    remaining=set(selected); components=[]
    while remaining:
        root=min(remaining); remaining.remove(root)
        queue=deque([root]); comp={root}; vids=set()
        while queue:
            eid=queue.popleft(); a,b=edges[eid]; vids.update((a,b))
            for vi in (a,b):
                for neighbor in vertex_to_edges[vi]:
                    if neighbor in remaining:
                        remaining.remove(neighbor); comp.add(neighbor);queue.append(neighbor)
        ids=sorted(comp)
        loop, degree=_loop_from_component(edges, ids)
        dangling=sorted(v for v,deg in degree.items() if deg==1)
        junctions=sorted(v for v,deg in degree.items() if deg>2)
        closed=bool(loop)
        coords=[vertices[v] for v in sorted(vids)]
        bbox=[[round(min(c[k] for c in coords),6) for k in range(3)],
              [round(max(c[k] for c in coords),6) for k in range(3)]]
        flags=[]
        if not closed:flags.append('NOT_A_SIMPLE_CLOSED_LOOP')
        if dangling:flags.append('OPEN_ENDPOINTS')
        if junctions:flags.append('BRANCHED_SELECTION')
        boundary=sum(face_counts[i]==1 for i in ids)
        internal=sum(face_counts[i]==2 for i in ids)
        nonmanifold=sum(face_counts[i]>2 for i in ids)
        wire=sum(face_counts[i]==0 for i in ids)
        if nonmanifold:flags.append('NONMANIFOLD_SELECTED_EDGES')
        if internal:flags.append('SELECTED_EDGES_INSIDE_FACES')
        if wire:flags.append('WIRE_EDGES')
        geom=_loop_shape(vertices,loop) if closed else {}
        if closed and geom.get('self_intersections',0):flags.append('SELF_INTERSECTING_SELECTION')
        if closed and geom.get('self_intersection_check')=='SKIPPED_TOO_LARGE':flags.append('SELF_INTERSECTION_NOT_CHECKED')
        if closed and not geom.get('rectangular_candidate'):
            flags.append('NOT_CONFIRMED_RECTANGULAR')
        auto=[]
        for ai,ase in enumerate(auto_edge_sets or []):
            others=set(ase)
            union=len(comp|others)
            overlap=round(len(comp&others)/union,4) if union else 0.0
            if overlap: auto.append({'cached_candidate_index':ai,'edge_jaccard':overlap})
        auto.sort(key=lambda x:-x['edge_jaccard'])
        components.append({
            'id':len(components)+1,'status':'CLOSED_LOOP' if closed else ('BRANCHED' if junctions else 'OPEN_OR_AMBIGUOUS'),
            'edge_ids':ids,'vertex_ids':sorted(vids),'loop_vertex_ids':loop,
            'edge_count':len(ids),'vertex_count':len(vids),
            'endpoints':dangling,'branch_vertices':junctions,
            'edge_incidence':{'boundary':boundary,'internal':internal,'nonmanifold':nonmanifold,'wire':wire},
            'bbox_world':bbox,'shape':geom,'flags':flags,
            'diagnostic_class':_diagnostic_class(geom,'CLOSED_LOOP' if closed else 'OPEN_OR_AMBIGUOUS'),
            'open_chain_review':_closing_path(vertices,edges,face_counts,chosen,dangling) if not closed else None,
            'cached_automatic_matches':auto[:6],
        })
    used={i for comp in components for i in comp['vertex_ids']}
    isolates=sorted(set(vselected)-used)
    return {'selected_edges':selected,'selected_vertices':vselected,'isolated_selected_vertices':isolates,
            'component_count':len(components),'closed_count':sum(c['status']=='CLOSED_LOOP' for c in components),
            'rectangular_candidate_count':sum(bool(c['shape'].get('rectangular_candidate')) for c in components),
            'components':components,
            'diagnostic_class_counts':dict(sorted(Counter(c['diagnostic_class'] for c in components).items())),
            'warning':'Diagnostic classes are only review hints, not proof of a valid hole or weld readiness'}
