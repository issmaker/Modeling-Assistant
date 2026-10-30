"""W58.5: exact-ish 2D arrangement union for native 3D curved Interior strips.

Interior overlapping trapezoid footprints cannot be 'fixed' by narrowing them:
union the actual 3D footprint sections, preserve full authored depth, then
triangulate a single OPEN-FRONT shell. No changes to host UV or frame geometry.
The algorithm refuses holes/branches/nonmanifold results: no misleading PASS.
"""
from __future__ import annotations
import math
from collections import defaultdict
from mathutils import Vector
from mathutils.geometry import tessellate_polygon

EPS=1.e-7

def cross(a,b):return a[0]*b[1]-a[1]*b[0]
def sub(a,b):return (a[0]-b[0],a[1]-b[1])
def lerp(a,b,t):return (a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t)
def on_segment(q,a,b,eps=1.e-6):
    d=sub(b,a);v=sub(q,a);l=math.hypot(*d)
    if l<eps:return math.dist(q,a)<eps
    return abs(cross(d,v))<=eps*l and -eps<=sum(x*y for x,y in zip(v,d))/l**2<=1.+eps

def point_in_polygon(p,poly):
    x,y=p;inside=False
    for a,b in zip(poly,poly[1:]+poly[:1]):
        if (a[1]>y)!=(b[1]>y):
            if x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
                inside=not inside
    return inside

def _intersection_params(a,b,c,d):
    v=sub(b,a);w=sub(d,c);den=cross(v,w)
    if abs(den)>1.e-11:
        t=cross(sub(c,a),w)/den
        u=cross(sub(c,a),v)/den
        if -1.e-7<=t<=1.+1.e-7 and -1.e-7<=u<=1.+1.e-7:
            return [max(0.,min(1.,t))]
        return []
    # Coincident edges may overlap; split at all on-edge endpoints.
    if abs(cross(sub(c,a),v))>1.e-7*max(1.,math.hypot(*v)):
        return []
    return [max(0.,min(1.,sum(x*y for x,y in zip(sub(q,a),v))/max(1.e-18,sum(t*t for t in v))))
            for q in (c,d) if on_segment(q,a,b)]

def _union_outline(polys):
    segments=[(a,b) for poly in polys for a,b in zip(poly,poly[1:]+poly[:1])]
    fragments=[]
    for i,(a,b) in enumerate(segments):
        cuts=[0.,1.]
        for j,(c,d) in enumerate(segments):
            if j==i:continue
            cuts.extend(_intersection_params(a,b,c,d))
        cuts=sorted(set(round(t,11) for t in cuts))
        for lo,hi in zip(cuts,cuts[1:]):
            if hi-lo<=1.e-9:continue
            p=lerp(a,b,lo);q=lerp(a,b,hi)
            length=math.dist(p,q)
            if length<1.e-7:continue
            mid=lerp(p,q,.5)
            offset=min(1.e-5,length*.001)
            n=(-(q[1]-p[1])/length,(q[0]-p[0])/length)
            left=(mid[0]+offset*n[0],mid[1]+offset*n[1])
            right=(mid[0]-offset*n[0],mid[1]-offset*n[1])
            il=any(point_in_polygon(left,poly) for poly in polys)
            ir=any(point_in_polygon(right,poly) for poly in polys)
            if il==ir:continue
            fragments.append((p,q) if il else (q,p))
    def key(p):return tuple(round(x,7) for x in p)
    canonical={}
    edges={}
    for a,b in fragments:
        u,v=key(a),key(b)
        if u==v:continue
        canonical[u]=a;canonical[v]=b
        edges[u,v]=1
    graph=defaultdict(list)
    for u,v in edges:graph[u].append(v)
    if any(len(children)!=1 for children in graph.values()):
        raise ValueError("INTERIOR_UNION_BOUNDARY_BRANCH")
    remaining=set(edges)
    loops=[]
    while remaining:
        edge=min(remaining);start=edge[0];cursor=start;ring=[]
        for _ in range(len(edges)+1):
            ring.append(cursor)
            nxt=graph[cursor][0]
            remaining.discard((cursor,nxt))
            cursor=nxt
            if cursor==start:break
        else:raise ValueError("INTERIOR_UNION_NOT_CLOSED")
        loops.append([canonical[k] for k in ring])
    if len(loops)!=1:
        raise ValueError(f"INTERIOR_UNION_MULTIPLE_LOOPS_OR_HOLES:{len(loops)}")
    return loops[0]

def _clean_union_outline(poly, *, tolerance=2.e-6):
    """Remove provably redundant collinear vertices from a union boundary.

    Preserve true corners and authored outline locations. Necessary because
    the Blender tessellator emits 0-area triangles for interior strip joins
    when three consecutive union points lie on the same exact straight line.
    """
    out=list(poly);removed=0
    for _ in range(len(poly)+1):
        if len(out)<3:raise ValueError("INTERIOR_UNION_OUTLINE_COLLAPSED")
        next_out=[];changed=False
        for i,p in enumerate(out):
            a=out[i-1];b=out[(i+1)%len(out)]
            if math.dist(a,p)<tolerance:
                changed=True;removed+=1;continue
            va=sub(p,a);vb=sub(b,p)
            la=math.hypot(*va);lb=math.hypot(*vb)
            if (min(la,lb)>tolerance
                    and abs(cross(va,vb))/(la*lb)<2.e-6
                    and va[0]*vb[0]+va[1]*vb[1]>0):
                changed=True;removed+=1;continue
            next_out.append(p)
        if not changed:
            break
        out=next_out
    if len(out)<3:raise ValueError("INTERIOR_UNION_OUTLINE_COLLAPSED")
    return out,removed

def union_native_interior(raw):
    verts=raw["verts"];faces=raw["faces"];roles=raw["roles"]
    if not verts or len(roles)!=len(faces):
        raise ValueError("INTERIOR_UNION_EMPTY_OR_BAD_ROLES")
    ylo=min(float(p[1]) for p in verts);yhi=max(float(p[1]) for p in verts)
    if yhi-ylo<.1:
        raise ValueError("INTERIOR_UNION_INVALID_HEIGHT")
    footprints=[]
    fronts=[]
    for face,role in zip(faces,roles):
        if role!="INTERIOR_REVEAL" or len(face)!=4:continue
        points=[verts[i] for i in face]
        if max(abs(float(pt[1])-ylo) for pt in points)>1.e-5:continue
        poly=[(float(pt[0]),float(pt[2])) for pt in points]
        if abs(sum(cross(a,b) for a,b in zip(poly,poly[1:]+poly[:1])))<1.e-9:
            continue
        footprints.append(poly)
        fronts.append((poly[0],poly[1]))
    if len(footprints)<2:
        raise ValueError("INTERIOR_UNION_NOT_MULTISEGMENT")
    outline,removed_collinear=_clean_union_outline(_union_outline(footprints))
    if len(outline)<3:raise ValueError("INTERIOR_UNION_NO_OUTLINE")
    triangulated=tessellate_polygon([[Vector((x,y,0)) for x,y in outline]])
    if not triangulated:raise ValueError("INTERIOR_UNION_CANNOT_TRIANGULATE")
    index={tuple(round(float(z),7) for z in pt):i for i,pt in enumerate(outline)}
    n=len(outline);out_verts=[];out_faces=[];out_roles=[]
    for y in (ylo,yhi):
        for x,z in outline:out_verts.append((float(x),float(y),float(z)))
    for tri in triangulated:
        # Blender's tessellate_polygon returns triangle vertex indices here.
        ids=[int(p) for p in tri]
        if len(set(ids))!=3:raise ValueError("INTERIOR_UNION_TRI_DEGENERATE")
        out_faces.append(list(reversed(ids)));out_roles.append("INTERIOR_REVEAL")
        out_faces.append([j+n for j in ids]);out_roles.append("INTERIOR_REVEAL")
    omitted=0
    for i,(a,b) in enumerate(zip(outline,outline[1:]+outline[:1])):
        midpoint=lerp(a,b,.5)
        is_front=any(on_segment(midpoint,x,y,1.e-5) for x,y in fronts)
        if is_front:
            omitted+=1;continue
        j=(i+1)%n
        out_faces.append([i,j,j+n,i+n]);out_roles.append("INTERIOR_BACK")
    if not omitted:
        raise ValueError("INTERIOR_UNION_DID_NOT_PRESERVE_OPEN_FRONT")
    return {"verts":out_verts,"faces":out_faces,"roles":out_roles}, {
        "policy":"UNION_OF_NATIVE_DEPTH_STRIPS_OPEN_FRONT_V1",
        "source_strips":len(footprints),
        "outline_vertices":len(outline),
        "removed_proven_collinear_vertices":removed_collinear,
        "open_front_edges":omitted,
        "repaired":True,"seam_gap_m":0.0,"physical_shells":1}
