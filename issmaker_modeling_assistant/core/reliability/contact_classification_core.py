"""W58.31 shared Brick Hole/DWOS geometric contact classification.

This is intentionally a *very narrow* proof, not an AABB or broad tolerance.
A pair of independent, non-coplanar faces that touch at ONE identical authored
3D vertex cannot have positive-area or positive-length penetration at that
contact. All other cases remain subject to the existing hard collision gates.

No Blender dependencies. Classification never modifies mesh or UV.
"""
from __future__ import annotations
import math

def classify_isolated_vertex_touch(tri_a, tri_b, hits, *,
                                  coplanar_area_overlap=False, tolerance_m=1e-6):
    """Return a proof record or None. Proof requires one shared XYZ vertex.

    The contact witness and both independently stored triangle vertices have
    to coincide within a much stricter proof tolerance. An edge intersection,
    vertex-to-interior penetration or coplanar overlap is never accepted.
    """
    if coplanar_area_overlap or len(hits)!=1:
        return None
    if len(tri_a)!=3 or len(tri_b)!=3:
        return None
    if not math.isfinite(tolerance_m) or tolerance_m<=0:
        raise ValueError('CONTACT_ORACLE_INVALID_TOLERANCE')
    points=[tuple(float(c) for c in p) for p in (*tri_a,*tri_b,*hits)]
    if not all(len(p)==3 and all(math.isfinite(c) for c in p) for p in points):
        raise ValueError('CONTACT_ORACLE_NONFINITE_INPUT')
    witness=points[-1]
    # Do not let broad-phase meters, scene coordinate magnitude or long edges
    # silently widen the proof of a point touching an authored vertex.
    proof=max(1e-10,min(1e-8,float(tolerance_m)*.005))
    def nearest(poly):
        return min((math.dist(witness,v),i) for i,v in enumerate(poly))
    da,ai=nearest(points[:3])
    db,bi=nearest(points[3:6])
    pa,pb=points[ai],points[3+bi]
    if da>proof or db>proof or math.dist(pa,pb)>proof:
        return None
    return {'classification':'POINT_TOUCH_ONLY_NO_PENETRATION',
            'contact_xyz':list(witness),'vertex_indices':[ai,bi],
            'proof_tolerance_m':proof,
            'max_vertex_error_m':max(da,db,math.dist(pa,pb))}


def prove_near_coplanar_area_overlap(tri_a,tri_b, *,
                                     tolerance_m=1e-6):
    """Evidence of genuine area overlap with Blender float32 rounding bounds.

    A fast ray/intersection proof at arbitrary precision can miss two nested
    coplanar triangles when one is stored as float32. Permit a tight, bounded
    uncertainty envelope based on float32 ULP and require a positive *projected
    area*, not point/edge proximity. Never use the full user collision tolerance
    as the coplanarity threshold.
    """
    if len(tri_a)!=3 or len(tri_b)!=3:
        return None
    a=[tuple(float(x) for x in p) for p in tri_a]
    b=[tuple(float(x) for x in p) for p in tri_b]
    if not all(len(p)==3 and all(math.isfinite(v) for v in p) for p in (a+b)):
        return None
    def sub(x,y):return (x[0]-y[0],x[1]-y[1],x[2]-y[2])
    def dot(x,y):return sum(x[i]*y[i] for i in range(3))
    def cross(x,y):return (x[1]*y[2]-x[2]*y[1],x[2]*y[0]-x[0]*y[2],x[0]*y[1]-x[1]*y[0])
    def norm(x):return math.sqrt(dot(x,x))
    n=cross(sub(a[1],a[0]),sub(a[2],a[0]))
    m=cross(sub(b[1],b[0]),sub(b[2],b[0]))
    if norm(n)<1e-12 or norm(m)<1e-12:return None
    n=tuple(q/norm(n) for q in n)
    m=tuple(q/norm(m) for q in m)
    if abs(dot(n,m))<1.-1.e-8:return None
    max_err=max(abs(dot(n,sub(q,a[0]))) for q in b)
    def float32_ulp(v):
        if abs(v)<1.17549435e-38:return 1.401298464e-45
        return 2.0**(math.floor(math.log2(abs(v)))-23)
    max_ulp=max(float32_ulp(v) for p in (a+b) for v in p)
    proof=max(1.e-10,min(float(tolerance_m)*.25,max_ulp*2.))
    if max_err>proof:return None
    drop=max(range(3),key=lambda k:abs(n[k]))
    def proj(p):return tuple(p[k] for k in range(3) if k!=drop)
    aa=list(map(proj,a));bb=list(map(proj,b))
    def cross2(u,v):return u[0]*v[1]-u[1]*v[0]
    def sub2(u,v):return (u[0]-v[0],u[1]-v[1])
    def signed(poly):
        return sum(cross2(poly[i],poly[(i+1)%len(poly)]) for i in range(len(poly)))*.5
    flip=1. if signed(aa)>0 else -1.
    current=bb
    for i in range(3):
        x,y=aa[i],aa[(i+1)%3]
        def side(pt):return flip*cross2(sub2(y,x),sub2(pt,x))
        out=[]
        for j,p in enumerate(current):
            q=current[(j+1)%len(current)]
            sp,sq=side(p),side(q)
            inside_p=sp>=-1e-12
            inside_q=sq>=-1e-12
            if inside_p:out.append(p)
            if inside_p!=inside_q and abs(sp-sq)>1e-18:
                t=sp/(sp-sq)
                out.append((p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])))
        current=out
        if not current:break
    area=abs(signed(current)) if len(current)>=3 else 0.0
    # Projected 2D triangle area cannot be merely a long-edge contact.
    scale=max(math.dist(u,v) for u in aa for v in bb)
    minimum=max(1e-12,scale*scale*1e-12)
    if area<=minimum:return None
    return {'classification':'FLOAT32_COPLANAR_AREA_OVERLAP',
            'projected_overlap_area_m2':area,
            'maximum_plane_distance_m':max_err,
            'float32_ulp_envelope_m':proof}
