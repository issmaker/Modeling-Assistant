"""W58.21 closed-wall exterior orientation oracle.

Pure geometry math. Never changes the authored path or normals. A reference
wall/path pair must be verified by the Blender glue before use.
"""
from math import hypot, isfinite
from collections import defaultdict

TOL=1e-10

def ordered_ring(vertices, edges):
    v=[tuple(float(c) for c in p) for p in vertices]
    if len(v)<3 or len(edges)!=len(v):
        raise ValueError("PATH_NOT_SINGLE_CLOSED_LOOP")
    adj=defaultdict(list)
    for x,y in edges:
        x=int(x);y=int(y)
        if x==y or x<0 or y<0 or x>=len(v) or y>=len(v):
            raise ValueError("PATH_BAD_EDGE")
        adj[x].append(y);adj[y].append(x)
    if len(adj)!=len(v) or any(len(set(neigh))!=2 for neigh in adj.values()):
        raise ValueError("PATH_NOT_SINGLE_DEGREE_2_LOOP")
    start=min(adj)
    seq=[start];last=-1
    while True:
        ne=adj[seq[-1]]
        nxt=ne[0] if ne[0]!=last else ne[1]
        if nxt==start:break
        if nxt in seq or len(seq)>len(v):raise ValueError("PATH_MULTIPLE_LOOPS")
        last=seq[-1];seq.append(nxt)
    if len(seq)!=len(v):raise ValueError("PATH_INCOMPLETE_LOOP")
    ring=[v[i] for i in seq]
    area=sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(ring,ring[1:]+ring[:1]))*.5
    if abs(area)<1e-6:raise ValueError("PATH_DEGENERATE_AREA")
    if any(hypot(a[0]-b[0],a[1]-b[1])<1e-7 for a,b in zip(ring,ring[1:]+ring[:1])):
        raise ValueError("PATH_ZERO_EDGE")
    return ring,area

def _dist2_segment(p,a,b):
    dx=b[0]-a[0];dy=b[1]-a[1]
    dd=dx*dx+dy*dy
    t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/dd))
    px=a[0]+t*dx;py=a[1]+t*dy
    return (p[0]-px)**2+(p[1]-py)**2,t

def sample_outward(p,ring,area,edge_distance_tolerance=.075):
    """At a face center, use its closest authored path edge.
    Distances and normals here are in a flat wall's local XY plane.
    """
    candidates=[]
    for a,b in zip(ring,ring[1:]+ring[:1]):
        dist2,t=_dist2_segment(p,a,b)
        dx=b[0]-a[0];dy=b[1]-a[1];length=hypot(dx,dy)
        if area>0.:n=(dy/length,-dx/length)
        else:n=(-dy/length,dx/length)
        candidates.append((dist2,n,t))
    best=min(x[0] for x in candidates)
    if best>edge_distance_tolerance**2:return None, "SOURCE_FACE_AWAY_FROM_PATH"
    # At a junction two edges may be equally close. If their outward normals
    # disagree strongly, avoid inventing a direction for this particular face.
    close=[x for x in candidates if x[0]-best<1e-10]
    sx=sum(x[1][0] for x in close);sy=sum(x[1][1] for x in close)
    length=hypot(sx,sy)
    if length<.35:return None,"AMBIGUOUS_CLOSEST_EDGES"
    return (sx/length,sy/length),"VERIFIED_PATH_EDGE"

def orient_face_normals(samples,ring,area):
    """Flip only evidence inconsistent with the ground-truth closed contour.
    Returns source normals in original dimensions, with metadata.
    """
    corrected=[];nflip=0;verified=0;uncertain=0;max_dist=0.
    for r in samples:
        item=dict(r)
        point=tuple(item["point"]);n=tuple(item["normal"])
        exterior,status=sample_outward(point,ring,area)
        length=hypot(n[0],n[1])
        if exterior is None or length<.1:
            uncertain+=1;corrected.append(item);continue
        alignment=(n[0]*exterior[0]+n[1]*exterior[1])/length
        if abs(alignment)<.3:
            uncertain+=1;corrected.append(item);continue
        verified+=1
        if alignment<0:
            item["normal"]=tuple(-x for x in n)
            nflip+=1
        corrected.append(item)
    return corrected,{"schema":"W58.21_CLOSED_PATH_EXTERIOR_V1",
                      "status":"VERIFIED" if verified>0 and uncertain==0 else
                               "PARTIAL" if verified>0 else "UNCERTAIN",
                      "source_samples":len(samples),
                      "verified_samples":verified,
                      "flipped_samples":nflip,
                      "uncertain_samples":uncertain,
                      "path_signed_area":area}
