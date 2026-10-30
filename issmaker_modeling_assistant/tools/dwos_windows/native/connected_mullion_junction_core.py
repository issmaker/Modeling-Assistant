"""W58.7 prepublication contract for CURVED zone-tree mullion junctions.

All selected V-mullion faces must be part of the *same* frame mesh, with
front edges shared with the adjacent upper/lower frame/H-mullion surfaces.
This verifies that no detached prism is allowed to overlap a frame reveal.
SOURCE UV Wall coordinates and authored path points are never mutated.
"""
from __future__ import annotations
from collections import defaultdict
import math
from .adaptive_opening_core import map_svd_local

def audit_connected_mullions(data,topology,zone_tree,frame_width,front_offset,depth,interior_sign,
                             *,contact_tolerance_m=2.e-5):
    if topology.get("classification")!="CURVED" or not zone_tree:
        return {"status":"NOT_APPLICABLE","checked":0}
    from .zone_core import layout
    _leaves,rows=layout(zone_tree,float(topology["width"]),float(topology["height"]),float(frame_width))
    bands=[r for r in rows if r[1]=="V"]
    if not bands:
        return {"status":"NO_VERTICAL_MULLIONS","checked":0}
    verts=data["verts"];faces=data["faces"];params=data["vertex_params"];roles=data["roles"]
    if len(params)!=len(verts) or len(faces)!=len(roles):
        raise ValueError("JUNCTION_AUDIT_DATA_INCONSISTENT")
    zf=float(front_offset);zb=zf+float(interior_sign)*float(depth)
    def near(a,b):return abs(float(a)-float(b))<2.e-6
    edge_users=defaultdict(list)
    for fi,face in enumerate(faces):
        for i in range(len(face)):
            a,b=face[i],face[(i+1)%len(face)]
            edge_users[frozenset((a,b))].append(fi)
    if any(len(v)>2 for v in edge_users.values()):
        raise ValueError("JUNCTION_NONMANIFOLD_EDGE")
    # Connected face graph, independent of normal orientation or material.
    graph=defaultdict(set)
    for incidence in edge_users.values():
        if len(incidence)==2:
            a,b=incidence;graph[a].add(b);graph[b].add(a)
    checked=[]
    max_contact=0.
    for _id,axis,_s,_width,rect,band in bands:
        x0,x1,y0,y1=map(float,band)
        active=[]
        for fi,(face,role) in enumerate(zip(faces,roles)):
            if role!="FRAME_EXTERIOR":continue
            coord=[params[v] for v in face]
            if not all(near(c[2],zf) for c in coord):continue
            cx=sum(c[0] for c in coord)/len(coord)
            cy=sum(c[1] for c in coord)/len(coord)
            if x0+1.e-6<cx<x1-1.e-6 and y0+1.e-6<cy<y1-1.e-6:
                active.append(fi)
                for vi in face:
                    pp=params[vi]
                    expected=map_svd_local(topology,float(pp[0]),float(pp[1]),zf)
                    err=math.dist(verts[vi],expected)
                    max_contact=max(max_contact,err)
        if not active:
            raise ValueError(f"JUNCTION_MISSING_MULLION_FRONT:{_id}")
        # The two band endpoints need actual shared edges leading into their
        # neighboring frame cells, not two overlapping faces in different islands.
        boundary_counts={}
        for bound,label in ((y0,"BOTTOM"),(y1,"TOP")):
            shared=0
            for fi in active:
                face=faces[fi]
                for k in range(len(face)):
                    va,vb=face[k],face[(k+1)%len(face)]
                    pa,pb=params[va],params[vb]
                    if (near(pa[1],bound) and near(pb[1],bound)
                            and near(pa[2],zf) and near(pb[2],zf)):
                        neighbors=edge_users[frozenset((va,vb))]
                        if len(neighbors)==2 and any(n not in active for n in neighbors):
                            shared+=1
            boundary_counts[label]=shared
            if shared==0:
                raise ValueError(f"JUNCTION_DETACHED_{label}:{_id}")
        # Traverse entire actual frame topology from V-strip face. Detached
        # object islands must never be treated as connected because they overlap.
        visited=set(active[:1]);queue=list(visited)
        while queue:
            q=queue.pop()
            for other in graph[q]:
                if other not in visited:visited.add(other);queue.append(other)
        if len(visited)!=len(faces):
            raise ValueError(f"JUNCTION_DISCONNECTED_FRAME:{_id}:{len(faces)-len(visited)}")
        checked.append({"id":str(_id),"front_faces":len(active),
                        "shared_top_edges":boundary_counts["TOP"],
                        "shared_bottom_edges":boundary_counts["BOTTOM"]})
    if max_contact>contact_tolerance_m:
        raise ValueError(f"JUNCTION_SOURCE_WALL_CONTACT_DRIFT:{max_contact}")
    return {"status":"CONNECTED","checked":len(checked),
            "source_contact_max_m":max_contact,
            "face_count":len(faces),"mullions":checked,
            "topology":"SINGLE_SHARED_FACE_GRAPH_V1"}
