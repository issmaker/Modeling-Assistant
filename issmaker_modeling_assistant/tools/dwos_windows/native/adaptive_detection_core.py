"""Pure topology discovery for adaptive openings on one-sided facade shells."""
from __future__ import annotations
from collections import defaultdict, deque
try:
    from .adaptive_opening_core import build_opening_topology, sub, cross, length, norm
except ImportError:
    from adaptive_opening_core import build_opening_topology, sub, cross, length, norm


def _face_normal(vertices, face):
    if len(face)<3:return (0.0,0.0,0.0)
    # Newell is stable for quads/ngons.
    n=[0.0,0.0,0.0]
    pts=[vertices[i] for i in face]
    for a,b in zip(pts,pts[1:]+pts[:1]):
        n[0]+=(a[1]-b[1])*(a[2]+b[2]);n[1]+=(a[2]-b[2])*(a[0]+b[0]);n[2]+=(a[0]-b[0])*(a[1]+b[1])
    try:return norm(tuple(n))
    except ValueError:return (0.0,0.0,0.0)


def _edge_key(a,b):return (a,b) if a<b else (b,a)


def _boundary_components(boundary_edges):
    v2e=defaultdict(list)
    for ei,(a,b,_fi) in enumerate(boundary_edges):v2e[a].append(ei);v2e[b].append(ei)
    remaining=set(range(len(boundary_edges)));out=[]
    while remaining:
        root=remaining.pop();q=deque([root]);ids={root};verts=set()
        while q:
            ei=q.popleft();a,b,_=boundary_edges[ei];verts.update((a,b))
            for v in (a,b):
                for ne in v2e[v]:
                    if ne in remaining:remaining.remove(ne);ids.add(ne);q.append(ne)
        out.append((sorted(ids),verts))
    return out


def _ordered_loop(boundary_edges, ids):
    adj=defaultdict(list)
    for ei in ids:
        a,b,_=boundary_edges[ei];adj[a].append((b,ei));adj[b].append((a,ei))
    if not adj or any(len(v)!=2 for v in adj.values()):return []
    start=min(adj);loop=[start];prev=None;cur=start
    for _ in range(len(ids)+2):
        opts=adj[cur];nxt=opts[0][0] if prev is None or opts[0][0]!=prev else opts[1][0]
        if nxt==start:return loop if len(loop)==len(adj) else []
        if nxt in loop:return []
        loop.append(nxt);prev,cur=cur,nxt
    return []


def _filter_large_ambiguous_contours(simple, complex_boundary, face_comp):
    """Conservatively exclude a likely exterior silhouette in a broken shell.

    A 45-metre false CURVED window has a perimeter near 98 m while a
    horizontal closed wall rim on the same component is near 126 m. This is
    a *rejection-only* heuristic: it never invents a new aperture or
    reclassifies unsupported geometry as a valid window.
    """
    rejected=[]
    if not complex_boundary:
        return simple, rejected
    horizontal=[item['perimeter'] for item in simple
                if 'NO_VERTICAL_SPAN' in item['topology'].get('reasons',())]
    largest=max(horizontal,default=0.0)
    if largest<=0:
        return simple, rejected
    accepted=[]
    for item in simple:
        topo=item['topology']
        if topo.get('build_supported') and item['perimeter']>=.50*largest:
            rejected.append({
                'face_component':face_comp,
                'status':'AMBIGUOUS_LARGE_OUTER_CONTOUR',
                'perimeter':item['perimeter'],
                'reference_horizontal_perimeter':largest,
                'classification':topo.get('classification'),
                'reason':'Branched boundary and large perimeter; use manually selected face/edge contour',
            })
        else:
            accepted.append(item)
    return accepted,rejected


def discover_surface_openings(vertices, faces, *, face_normals=None, up_hint=(0,0,1), source='AUTO_DETECTED'):
    """Discover hole boundary loops on an open facade shell.

    This intentionally does not claim thick-solid adaptive support.  For a
    watertight solid there are no one-face boundary edges; the legacy planar
    detector remains responsible for proven SOLID windows.
    """
    vertices=[tuple(map(float,p)) for p in vertices]
    faces=[tuple(map(int,f)) for f in faces]
    normals=list(face_normals or [_face_normal(vertices,f) for f in faces])
    if len(normals)!=len(faces):raise ValueError('face_normals length mismatch')
    face_centers=[]
    for f in faces:
        if not f:face_centers.append((0.0,0.0,0.0));continue
        face_centers.append(tuple(sum(vertices[i][k] for i in f)/len(f) for k in range(3)))
    edge_faces=defaultdict(list)
    face_edges=[]
    for fi,f in enumerate(faces):
        keys=[]
        for a,b in zip(f,f[1:]+f[:1]):
            k=_edge_key(a,b);edge_faces[k].append(fi);keys.append(k)
        face_edges.append(keys)
    # Face connected components.
    adj=[set() for _ in faces]
    for fs in edge_faces.values():
        for a in fs:
            for b in fs:
                if a!=b:adj[a].add(b)
    comp=[-1]*len(faces);cid=0
    for root in range(len(faces)):
        if comp[root]>=0:continue
        comp[root]=cid;q=deque([root])
        while q:
            f=q.popleft()
            for g in adj[f]:
                if comp[g]<0:comp[g]=cid;q.append(g)
        cid+=1
    by_comp=defaultdict(list)
    for (a,b),fs in edge_faces.items():
        if len(fs)==1:by_comp[comp[fs[0]]].append((a,b,fs[0]))
    accepted=[];diagnostics=[];boundary_loops=[]
    for face_comp,bedges in by_comp.items():
        components=_boundary_components(bedges)
        simple=[];complex_boundary=False
        for ids,_verts in components:
            loop=_ordered_loop(bedges,ids)
            perimeter=sum(length(sub(vertices[loop[(i+1)%len(loop)]],vertices[loop[i]])) for i in range(len(loop))) if loop else 0.0
            if not loop:
                complex_boundary=True
                diagnostics.append({'face_component':face_comp,'status':'BRANCHED_OR_OPEN_BOUNDARY','edge_count':len(ids),'perimeter':perimeter})
                continue
            adjacent=[];normal_samples=[]
            edge_to_face={_edge_key(a,b):fi for a,b,fi in bedges}
            boundary_faces=set()
            for a,b in zip(loop,loop[1:]+loop[:1]):
                fi=edge_to_face.get(_edge_key(a,b))
                if fi is None:continue
                boundary_faces.add(fi)
                n=normals[fi];adjacent.append(n)
                pa,pb=vertices[a],vertices[b]
                normal_samples.append({'point':((pa[0]+pb[0])*.5,(pa[1]+pb[1])*.5,(pa[2]+pb[2])*.5),
                                       'normal':n,'source':'BOUNDARY_FACE'})
            # A hole boundary edge may belong to a reveal/top/bottom face rather
            # than to the facade skin itself.  Pull one-ring neighboring face
            # normals with their real centroids so the orientation solver sees
            # the actual facade patches around the opening.
            neighborhood=set(boundary_faces)
            for fi in list(boundary_faces):neighborhood.update(adj[fi])
            for fi in sorted(neighborhood):
                normal_samples.append({'point':face_centers[fi],'normal':normals[fi],
                                       'source':'FACE_ONE_RING','face_index':fi})
            topo=build_opening_topology([vertices[i] for i in loop],vertex_ids=loop,adjacent_normals=adjacent,
                                        adjacent_normal_samples=normal_samples,up_hint=up_hint,source=source)
            simple.append({'loop':loop,'perimeter':perimeter,'topology':topo})
        if not simple:continue
        # When this face component has a broken/branched outside boundary AND
        # only one valid closed loop, topology alone cannot prove whether the
        # loop is the opening or a detached exterior silhouette.  Production
        # UV_Wall fixtures produced a false 29.712 m CURVED 'opening' here.
        # Fail closed for AUTO detection; the manual selected-edge workflow
        # remains available, and other face components remain unaffected.
        if complex_boundary and len(simple) == 1:
            item=simple[0]
            diagnostics.append({
                'face_component':face_comp,
                'status':'AMBIGUOUS_OPENING_OR_OUTER_SILHOUETTE',
                'perimeter':item['perimeter'],
                'classification':item['topology'].get('classification'),
                'reason':'Branched exterior + one closed contour; select the real opening edges in Edit Mode',
            })
            continue
        simple, ambiguous = _filter_large_ambiguous_contours(
            simple, complex_boundary, face_comp)
        diagnostics.extend(ambiguous)
        if not simple:continue
        # If all boundary components are simple closed rings, the largest is the
        # exterior silhouette of this connected surface.  A branched/open exterior
        # is common in production models; then no valid simple aperture is discarded.
        outer_index=None
        if not complex_boundary:
            outer_index=max(range(len(simple)),key=lambda i:simple[i]['perimeter'])
        for i,item in enumerate(simple):
            topo=item['topology']
            if i==outer_index:
                diagnostics.append({'face_component':face_comp,'status':'OUTER_SILHOUETTE','perimeter':item['perimeter'],'classification':topo.get('classification')})
                continue
            rec={'face_component':face_comp,'status':'OPENING_LOOP','perimeter':item['perimeter'],'classification':topo.get('classification'),'reasons':topo.get('reasons',[])}
            diagnostics.append(rec)
            boundary_loops.append({'face_component':face_comp,'loop_vertex_ids':list(item['loop']),
                                   'classification':topo.get('classification'),
                                   'build_supported':bool(topo.get('build_supported')),
                                   'reasons':list(topo.get('reasons',[]))})
            if topo.get('build_supported'):
                accepted.append(topo)
    return {'openings':accepted,'diagnostics':diagnostics,'boundary_loops':boundary_loops,'face_components':cid,
            'boundary_edges':sum(len(v) for v in by_comp.values()),
            'adaptive_solid_support':False}


def discover_selected_openings(vertices, edges, selected_edge_ids, *, edge_face_normals=None,
                               up_hint=(0,0,1), source='USER_SELECTED'):
    """Build adaptive opening topologies from exact user-selected edge components.

    Selection is treated as user intent, not as geometric truth: open/branched or
    unsupported components are reported and never fabricated into a window.
    """
    vertices=[tuple(map(float,p)) for p in vertices]
    edges=[tuple(map(int,e)) for e in edges]
    selected=[]
    for raw in selected_edge_ids:
        ei=int(raw)
        if ei<0 or ei>=len(edges):raise ValueError(f'selected edge {ei} out of range')
        selected.append((edges[ei][0],edges[ei][1],ei))
    if not selected:
        return {'openings':[],'diagnostics':[],'component_count':0,'closed_count':0}
    components=_boundary_components(selected)
    accepted=[];diagnostics=[];closed=0
    for cid,(ids,_verts) in enumerate(components):
        loop=_ordered_loop(selected,ids)
        if not loop:
            diagnostics.append({'component':cid,'status':'OPEN_OR_BRANCHED','edge_count':len(ids)})
            continue
        closed+=1
        normals=[];normal_samples=[]
        if edge_face_normals:
            edge_to_global={_edge_key(a,b):global_ei for a,b,global_ei in selected}
            for a,b in zip(loop,loop[1:]+loop[:1]):
                global_ei=edge_to_global.get(_edge_key(a,b))
                if global_ei is None:continue
                pa,pb=vertices[a],vertices[b]
                midpoint=((pa[0]+pb[0])*.5,(pa[1]+pb[1])*.5,(pa[2]+pb[2])*.5)
                for n in edge_face_normals.get(global_ei,[]):
                    if n is not None:
                        normals.append(tuple(n));normal_samples.append({'point':midpoint,'normal':tuple(n)})
        topo=build_opening_topology([vertices[i] for i in loop],vertex_ids=loop,
                                    adjacent_normals=normals,adjacent_normal_samples=normal_samples,
                                    up_hint=up_hint,source=source)
        diagnostics.append({'component':cid,'status':'CLOSED','edge_count':len(ids),
                            'classification':topo.get('classification'),
                            'reasons':topo.get('reasons',[])})
        if topo.get('build_supported'):accepted.append(topo)
    return {'openings':accepted,'diagnostics':diagnostics,
            'component_count':len(components),'closed_count':closed}
