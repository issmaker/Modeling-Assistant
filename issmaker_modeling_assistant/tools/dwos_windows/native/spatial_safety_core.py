"""DWOS 3.5.3 pure mesh spatial-safety kernel.

Detects non-adjacent self intersections and cross-window Interior intersections.
No bpy/mathutils dependency; Blender scene integration lives in scene_spatial_safety.py.

3.5.3 closes two false-negative classes found by an independent audit:
- positive-area coplanar overlap, including identical triangles;
- self intersections between faces that share only one vertex.

Boundary-only contact (a single vertex or a shared edge) is intentionally not
reported as penetration.  Epsilon is derived from the local geometry scale so
large/small architectural scenes behave consistently.
"""
from __future__ import annotations
import math
from ....core.reliability.precision import policy_from_points
from ....core.reliability.predicates import (
    exact_segment_triangle_crossing_3d, exact_coplanar_triangle_positive_overlap_3d,
)

ABS_EPS = 1.0e-9
REL_EPS = 1.0e-9
EPS = ABS_EPS  # backward-compatible public constant


def sub(a,b): return (a[0]-b[0],a[1]-b[1],a[2]-b[2])
def dot(a,b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def length(v): return math.sqrt(max(0.0,dot(v,v)))


def _effective_eps(points, floor=ABS_EPS):
    # RF1: world-coordinate magnitude is not geometry scale. Coordinate ULP is
    # accounted for explicitly by the shared PrecisionPolicy.
    return policy_from_points(points,rel_linear=REL_EPS,min_linear=float(floor)).linear


def triangulate(faces):
    out=[]
    for fi,face in enumerate(faces):
        ids=tuple(int(i) for i in face)
        for j in range(1,len(ids)-1):out.append((fi,(ids[0],ids[j],ids[j+1])))
    return out


def tri_bounds(vs,t):
    pts=[vs[i] for i in t]
    lo=tuple(min(p[k] for p in pts) for k in range(3)); hi=tuple(max(p[k] for p in pts) for k in range(3))
    return lo,hi


def bounds(vs):
    if not vs:return ((0,0,0),(0,0,0))
    return tuple(min(p[k] for p in vs) for k in range(3)),tuple(max(p[k] for p in vs) for k in range(3))


def aabb_overlap(a,b,eps=EPS):
    return all(a[0][k] <= b[1][k]+eps and b[0][k] <= a[1][k]+eps for k in range(3))


def _seg_tri(p0,p1,a,b,c,eps):
    """Finite segment vs triangle using a unit plane normal + barycentrics.

    This avoids mixing length and area units in epsilon comparisons. Segment
    endpoints are excluded, while the target triangle boundary is included.
    """
    d=sub(p1,p0);e1=sub(b,a);e2=sub(c,a);n=cross(e1,e2)
    nl=length(n);dl=length(d)
    if nl<=eps*eps or dl<=eps:return False
    nu=tuple(x/nl for x in n)
    denom=dot(nu,d)
    if abs(denom)<=eps:return False
    t=dot(nu,sub(a,p0))/denom
    t_tol=min(0.25,eps/max(dl,eps))
    if not (t_tol<t<1.0-t_tol):return False
    p=(p0[0]+d[0]*t,p0[1]+d[1]*t,p0[2]+d[2]*t)
    v0=e1;v1=e2;v2=sub(p,a)
    d00=dot(v0,v0);d01=dot(v0,v1);d11=dot(v1,v1);d20=dot(v2,v0);d21=dot(v2,v1)
    den=d00*d11-d01*d01
    if abs(den)<=eps**4:return False
    v=(d11*d20-d01*d21)/den;w=(d00*d21-d01*d20)/den;u=1.0-v-w
    scale=max(length(v0),length(v1),length(sub(c,b)),eps)
    btol=max(1.0e-10,min(1.0e-5,eps/scale))
    return u>=-btol and v>=-btol and w>=-btol

def _project2(p,drop):return tuple(p[k] for k in range(3) if k!=drop)


def _positive_area_tri_overlap2(t1,t2,eps):
    """2D convex SAT test: True only when triangle interiors overlap by area.

    Identical/partially overlapping coplanar triangles return True.  A mere
    shared edge or vertex returns False because at least one separating-axis
    projection has zero interior overlap.
    """
    pts=list(t1)+list(t2)
    minx=min(p[0] for p in pts);maxx=max(p[0] for p in pts)
    miny=min(p[1] for p in pts);maxy=max(p[1] for p in pts)
    span=max(maxx-minx,maxy-miny,eps)
    tol=max(eps,span*REL_EPS)
    for tri in (t1,t2):
        for i in range(3):
            a=tri[i];b=tri[(i+1)%3]
            ex=b[0]-a[0];ey=b[1]-a[1]
            L=math.hypot(ex,ey)
            if L<=tol:continue
            ax=-ey/L;ay=ex/L
            p1=[p[0]*ax+p[1]*ay for p in t1]
            p2=[p[0]*ax+p[1]*ay for p in t2]
            overlap=min(max(p1),max(p2))-max(min(p1),min(p2))
            if overlap<=tol:
                return False
    return True


def _coplanar_overlap(a,b,c,d,e,f,n,eps):
    drop=max(range(3),key=lambda k:abs(n[k]))
    A,B,C,D,E,F=(_project2(p,drop) for p in (a,b,c,d,e,f))
    return _positive_area_tri_overlap2((A,B,C),(D,E,F),eps)


def triangles_intersect(vs1,t1,vs2,t2,eps=None):
    """Broad-phase with tolerance, hard verdict with exact stored-float proof."""
    pts=[vs1[i] for i in t1]+[vs2[i] for i in t2]
    eps=_effective_eps(pts) if eps is None else max(float(eps),ABS_EPS)
    if not aabb_overlap(tri_bounds(vs1,t1),tri_bounds(vs2,t2),eps):return False
    a,b,c=(vs1[i] for i in t1); d,e,f=(vs2[i] for i in t2)
    # Positive-area coplanar overlap is a proven collision; shared edge/vertex is not.
    if exact_coplanar_triangle_positive_overlap_3d((a,b,c),(d,e,f)):
        return True
    # Non-coplanar hard collision requires an exact edge/triangle crossing.
    for p,q in ((a,b),(b,c),(c,a)):
        if exact_segment_triangle_crossing_3d(p,q,d,e,f,include_boundary=True,exclude_endpoints=True):
            return True
    for p,q in ((d,e),(e,f),(f,d)):
        if exact_segment_triangle_crossing_3d(p,q,a,b,c,include_boundary=True,exclude_endpoints=True):
            return True
    return False


def self_intersections(verts,faces,max_hits=16):
    tris=triangulate(faces);hits=[];seen=set()
    if not tris:return hits
    eps=_effective_eps(verts)
    # 3.5.3 sweep broad phase: avoid O(T²) narrow-phase calls for spatially
    # separated triangles while preserving exact pair semantics.
    rows=[]
    for fi,t in tris:
        bb=tri_bounds(verts,t);rows.append((bb[0][0],bb[1][0],fi,t,bb))
    rows.sort(key=lambda r:r[0])
    for i,(_,xmax,fi,a,ba) in enumerate(rows):
        for xmin2,_,fj,b,bb in rows[i+1:]:
            if xmin2>xmax+eps:break
            if fi==fj or not aabb_overlap(ba,bb,eps):continue
            key=(min(fi,fj),max(fi,fj))
            if key in seen:continue
            # Do not skip merely because triangles share a vertex.  The geometric
            # test itself rejects boundary-only adjacency and still catches a face
            # that pivots through another face around a shared vertex.
            if triangles_intersect(verts,a,verts,b,eps):
                hits.append((fi,fj));seen.add(key)
                if len(hits)>=max_hits:return hits
    return hits


def mesh_intersections(verts_a,faces_a,verts_b,faces_b,max_hits=16):
    if not verts_a or not verts_b:return []
    eps=_effective_eps(list(verts_a)+list(verts_b))
    if not aabb_overlap(bounds(verts_a),bounds(verts_b),eps):return []
    ta=triangulate(faces_a);tb=triangulate(faces_b);hits=[];seen=set()
    bounds_b=[tri_bounds(verts_b,b) for _,b in tb]
    for fa,a in ta:
        ba=tri_bounds(verts_a,a)
        for (fb,b),bb in zip(tb,bounds_b):
            key=(fa,fb)
            if key in seen or not aabb_overlap(ba,bb,eps):continue
            if triangles_intersect(verts_a,a,verts_b,b,eps):
                hits.append(key);seen.add(key)
                if len(hits)>=max_hits:return hits
    return hits
