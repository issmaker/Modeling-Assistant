"""DWOS 3.1 deterministic validation gates for adaptive opening geometry."""
from __future__ import annotations
from collections import Counter
from math import isfinite
try:
    from .adaptive_opening_core import cross,sub,length,contact_error
except ImportError:
    from adaptive_opening_core import cross,sub,length,contact_error

EPS=1e-8

def validate_topology(topology, *, contact_tolerance=0.001):
    if topology.get('schema')!='DWOS_opening_topology_v1':raise ValueError('Unknown adaptive topology schema')
    cls=topology.get('classification')
    if cls not in {'PLANAR','CURVED','CORNER','COMPLEX','INVALID'}:raise ValueError('Unknown opening classification')
    if cls in {'PLANAR','CURVED','CORNER'}:
        if not topology.get('build_supported'):raise ValueError('Supported class is marked not buildable')
        if len(topology.get('stations',[]))<2:raise ValueError('Adaptive topology has fewer than 2 stations')
        if min(float(topology.get('width',0)),float(topology.get('height',0)))<=EPS:raise ValueError('Adaptive topology has invalid dimensions')
        err=contact_error(topology)
        if err>contact_tolerance:raise ValueError(f'Opening contact mismatch {err:.6f} m')
        return {'classification':cls,'stations':len(topology['stations']),'contact_error_m':err,'patches':len(topology.get('patches',[]))}
    return {'classification':cls,'stations':len(topology.get('stations',[])),'reasons':list(topology.get('reasons',[]))}


def orient_consistent_faces(faces):
    """Orient staged polygon faces consistently across shared manifold edges.

    Keeps the first face of each component unchanged, preserves face ordering,
    vertex indices, roles and coordinates. Rejects nonorientable/nonmanifold
    input rather than allowing a silent half-fix.
    """
    from collections import defaultdict, deque
    output=[tuple(face) for face in faces]
    incidence=defaultdict(list)
    for fi,face in enumerate(output):
        if len(face)<3 or len(set(face))!=len(face):
            raise ValueError(f'Orientation: face {fi} has invalid ring')
        for a,b in zip(face,face[1:]+face[:1]):
            if a==b:raise ValueError('Orientation: zero length topological edge')
            key=(min(a,b),max(a,b))
            incidence[key].append((fi,1 if a==key[0] else -1))
    graph=defaultdict(list)
    for edge,items in incidence.items():
        if len(items)>2:
            raise ValueError(f'Orientation: non-manifold edge {edge}')
        if len(items)==2:
            (a,da),(b,db)=items
            # New direction is direction * (-1 if flipped else +1).
            # Facing edges must oppose each other across manifold adjacency.
            relation=(da==db)
            graph[a].append((b,relation))
            graph[b].append((a,relation))
    flip={}
    for start in range(len(output)):
        if start in flip:continue
        flip[start]=False
        todo=deque([start])
        while todo:
            a=todo.popleft()
            for b,reverse in graph[a]:
                desired=flip[a]^reverse
                if b in flip:
                    if flip[b]!=desired:
                        raise ValueError('Orientation: non-orientable mesh component')
                    continue
                flip[b]=desired
                todo.append(b)
    return [tuple(reversed(face)) if flip[i] else tuple(face)
            for i,face in enumerate(output)]


def validate_mesh(mesh, *, topology=None, contact_tolerance=1e-7):
    verts=list(mesh.get('verts',[]));faces=list(mesh.get('faces',[]));roles=list(mesh.get('roles',[]))
    if not verts or not faces:raise ValueError('Adaptive mesh is empty')
    if len(faces)!=len(roles):raise ValueError('Adaptive mesh roles mismatch')
    if any(not all(isfinite(float(x)) for x in p) for p in verts):raise ValueError('Adaptive mesh contains NaN/Inf')
    incidence=Counter();directed={};used=set();min_area=float('inf');face_keys=set()
    for fi,face in enumerate(faces):
        if len(face)<3 or len(set(face))<3:raise ValueError(f'Adaptive face {fi}: degenerate indices')
        if any(i<0 or i>=len(verts) for i in face):raise ValueError(f'Adaptive face {fi}: invalid vertex index')
        fkey=tuple(sorted(face))
        if fkey in face_keys:raise ValueError(f'Adaptive face {fi}: duplicate polygon')
        face_keys.add(fkey)
        a,b,c=(verts[face[i]] for i in range(3));area=length(cross(sub(b,a),sub(c,a)))*.5
        min_area=min(min_area,area)
        if area<=1e-12:raise ValueError(f'Adaptive face {fi}: zero area')
        for aidx,bidx in zip(face,face[1:]+face[:1]):
            edge=tuple(sorted((aidx,bidx)))
            incidence[edge]+=1;directed.setdefault(edge,[]).append((aidx,bidx));used.update((aidx,bidx))
    if used!=set(range(len(verts))):raise ValueError('Adaptive mesh contains unused vertices')
    max_inc=max(incidence.values()) if incidence else 0
    if max_inc>2:raise ValueError('Adaptive mesh has non-manifold edge incidence >2')
    for edge,rows in directed.items():
        if len(rows)==2 and rows[0]==rows[1]:
            raise ValueError(f'Adaptive mesh has inconsistent manifold winding at edge {edge}')
    if any(r in {'FRAME_CONTACT','OUTER_CONTACT'} for r in roles):raise ValueError('Adaptive frame contains forbidden Host-contact side faces')
    metrics={'verts':len(verts),'faces':len(faces),'boundary_edges':sum(v==1 for v in incidence.values()),
             'max_edge_incidence':max_inc,'min_face_area':min_area}
    params=mesh.get('vertex_params')
    if topology is not None and params is not None:
        validate_topology(topology)
        w=float(topology['width']);h=float(topology['height']);hx=w*.5;hy=h*.5
        front=float(mesh['z_front']);full='FRAME_INTERIOR_BACK' in roles
        expected_top=sorted(set(round(float(x['s']),7) for x in topology.get('top_contact',[])))
        expected_bottom=sorted(set(round(float(x['s']),7) for x in topology.get('bottom_contact',[])))
        expected_left=sorted(set(round(float(x.get('v',0.0)),7) for x in topology.get('left_contact',[])))
        expected_right=sorted(set(round(float(x.get('v',0.0)),7) for x in topology.get('right_contact',[])))
        def boundary_params(dd):
            top=[];bottom=[];left=[];right=[]
            for s,v,d in params:
                if abs(d-dd)>contact_tolerance:continue
                if abs(v-hy)<=contact_tolerance:top.append(round(s,7))
                if abs(v+hy)<=contact_tolerance:bottom.append(round(s,7))
                if abs(s+hx)<=contact_tolerance:left.append(round(v,7))
                if abs(s-hx)<=contact_tolerance:right.append(round(v,7))
            return tuple(sorted(set(x)) for x in (top,bottom,left,right))
        front_params=boundary_params(front)
        expected=(expected_top,expected_bottom,expected_left,expected_right)
        if front_params!=expected:
            raise ValueError('Adaptive front contact does not match Host topology stations')
        unique_front=set()
        for s in front_params[0]:unique_front.add((s,round(hy,7)))
        for s in front_params[1]:unique_front.add((s,round(-hy,7)))
        for v in front_params[2]:unique_front.add((round(-hx,7),v))
        for v in front_params[3]:unique_front.add((round(hx,7),v))
        metrics['contact_front_vertices']=len(unique_front)
        if full:
            back_params=boundary_params(float(mesh['z_back']))
            if back_params!=front_params:raise ValueError('Adaptive back contact differs from front topology')
            metrics['contact_back_vertices']=len(unique_front)
    return metrics
