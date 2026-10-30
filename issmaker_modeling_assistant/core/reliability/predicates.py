"""Robust predicates shared across ISSMAKER tools.

Exact predicates operate on the exact binary float values supplied by Blender.
They are intentionally reserved for narrow phase after cheap candidate search.
"""
from __future__ import annotations
import math
from fractions import Fraction


def _point(point, dims):
    try:q=tuple(float(v) for v in point)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('RELIABILITY_PREDICATE: invalid point') from exc
    if len(q)!=dims or not all(math.isfinite(v) for v in q):
        raise ValueError('RELIABILITY_PREDICATE: invalid point')
    return q

def _f(point):return tuple(Fraction.from_float(float(v)) for v in point)
def _sub(a,b):return tuple(a[i]-b[i] for i in range(len(a)))
def _cross3(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _orient2(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def localize_points(points, anchor=None):
    pts=tuple(tuple(float(v) for v in p) for p in points)
    if not pts:return ()
    if not all(p and all(math.isfinite(v) for v in p) for p in pts):
        raise ValueError('RELIABILITY_PREDICATE: non-finite point')
    origin=pts[0] if anchor is None else tuple(float(v) for v in anchor)
    return tuple(tuple(p[i]-origin[i] for i in range(len(p))) for p in pts)


def exact_segment_contact_2d(a,b,c,d):
    # Lossless float AABB rejection before Fraction conversion. If the stored
    # intervals are disjoint, exact contact is impossible; this keeps the exact
    # predicate cheap on long CAD/UV contours where most pairs are far apart.
    af,bf,cf,df=map(lambda p:_point(p,2),(a,b,c,d))
    if (max(af[0],bf[0]) < min(cf[0],df[0]) or max(cf[0],df[0]) < min(af[0],bf[0]) or
            max(af[1],bf[1]) < min(cf[1],df[1]) or max(cf[1],df[1]) < min(af[1],bf[1])):
        return False
    A,B,C,D=map(_f,(af,bf,cf,df))
    o1,o2,o3,o4=_orient2(A,B,C),_orient2(A,B,D),_orient2(C,D,A),_orient2(C,D,B)
    def on(p,x,y):
        return _orient2(x,y,p)==0 and min(x[0],y[0])<=p[0]<=max(x[0],y[0]) and min(x[1],y[1])<=p[1]<=max(x[1],y[1])
    if o1==0 and on(C,A,B):return True
    if o2==0 and on(D,A,B):return True
    if o3==0 and on(A,C,D):return True
    if o4==0 and on(B,C,D):return True
    return (o1>0)!=(o2>0) and (o3>0)!=(o4>0)


def _point_on_segment_exact3(point,a,b):
    ab=_sub(b,a);ap=_sub(point,a)
    if ab==(0,0,0):return point==a
    if _cross3(ab,ap)!=(0,0,0):return False
    axis=next(i for i,v in enumerate(ab) if v!=0)
    lo,hi=sorted((a[axis],b[axis]));return lo<=point[axis]<=hi


def exact_segment_contact_3d(a,b,c,d):
    af,bf,cf,df=map(lambda p:_point(p,3),(a,b,c,d))
    for axis in range(3):
        if max(af[axis],bf[axis]) < min(cf[axis],df[axis]) or max(cf[axis],df[axis]) < min(af[axis],bf[axis]):
            return False
    A,B,C,D=map(_f,(af,bf,cf,df))
    u=_sub(B,A);v=_sub(D,C);w=_sub(C,A)
    if u==(0,0,0):return _point_on_segment_exact3(A,C,D)
    if v==(0,0,0):return _point_on_segment_exact3(C,A,B)
    for i,j in ((0,1),(0,2),(1,2)):
        det=u[j]*v[i]-u[i]*v[j]
        if det==0:continue
        s=(-w[i]*v[j]+w[j]*v[i])/det
        t=(u[i]*w[j]-u[j]*w[i])/det
        if not(0<=s<=1 and 0<=t<=1):return False
        return tuple(A[k]+s*u[k] for k in range(3))==tuple(C[k]+t*v[k] for k in range(3))
    if _cross3(u,w)!=(0,0,0):return False
    axis=max(range(3),key=lambda k:abs(u[k]))
    a0,a1=sorted((A[axis],B[axis]));c0,c1=sorted((C[axis],D[axis]))
    return max(a0,c0)<=min(a1,c1)


def exact_shared_edge_only_contact(indices_a, coords_a, indices_b, coords_b):
    """Prove two nondegenerate triangles meet only on their shared mesh edge."""
    if len(indices_a)!=3 or len(indices_b)!=3:return False
    common=set(indices_a).intersection(indices_b)
    if len(common)!=2:return False
    A=tuple(_f(_point(p,3)) for p in coords_a)
    B=tuple(_f(_point(p,3)) for p in coords_b)
    na=_cross3(_sub(A[1],A[0]),_sub(A[2],A[0]))
    nb=_cross3(_sub(B[1],B[0]),_sub(B[2],B[0]))
    if na==(0,0,0) or nb==(0,0,0):return False
    # Nonparallel planes that share an edge intersect only on that line.
    if _cross3(na,nb)!=(0,0,0):return True
    ids=[i for i in indices_a if i in common]
    a=A[indices_a.index(ids[0])];b=A[indices_a.index(ids[1])]
    pa=A[next(k for k,i in enumerate(indices_a) if i not in common)]
    pb=B[next(k for k,i in enumerate(indices_b) if i not in common)]
    edge=_sub(b,a)
    sa=_dot(_cross3(edge,_sub(pa,a)),na)
    sb=_dot(_cross3(edge,_sub(pb,a)),na)
    return (sa>0 and sb<0) or (sa<0 and sb>0)


def signed_area_2d(points):
    pts=tuple(_point(p,2) for p in points)
    if len(pts)<3:return 0.0
    local=localize_points(pts)
    return 0.5*math.fsum(local[i][0]*local[(i+1)%len(local)][1]-local[(i+1)%len(local)][0]*local[i][1] for i in range(len(local)))


def polygon_area_vector(points):
    pts=tuple(_point(p,3) for p in points)
    if len(pts)<3:raise ValueError('RELIABILITY_PREDICATE: polygon requires 3+ points')
    local=localize_points(pts)
    crosses=[]
    for i in range(1,len(local)-1):crosses.append(_cross3(local[i],local[i+1]))
    return tuple(.5*math.fsum(float(c[k]) for c in crosses) for k in range(3))


def _tri_normal_exact(tri):
    return _cross3(_sub(tri[1],tri[0]),_sub(tri[2],tri[0]))


def exact_triangles_coplanar(a,b,c,d,e,f):
    A,B,C,D,E,F=tuple(_f(_point(p,3)) for p in (a,b,c,d,e,f))
    n1=_tri_normal_exact((A,B,C));n2=_tri_normal_exact((D,E,F))
    if n1==(0,0,0) or n2==(0,0,0):return False
    if _cross3(n1,n2)!=(0,0,0):return False
    return _dot(n1,_sub(D,A))==0 and _dot(n1,_sub(E,A))==0 and _dot(n1,_sub(F,A))==0


def exact_segment_triangle_crossing_3d(p0,p1,a,b,c, *, include_boundary=True, exclude_endpoints=True):
    """Exact stored-float segment/triangle plane crossing.

    Coplanar segments return False and are handled by dedicated coplanar tests.
    """
    P0,P1,A,B,C=tuple(_f(_point(p,3)) for p in (p0,p1,a,b,c))
    n=_tri_normal_exact((A,B,C))
    if n==(0,0,0):return False
    s0=_dot(n,_sub(P0,A));s1=_dot(n,_sub(P1,A))
    if s0==0 and s1==0:return False
    if (s0>0 and s1>0) or (s0<0 and s1<0):return False
    den=s0-s1
    if den==0:return False
    t=s0/den
    if exclude_endpoints:
        if not (0<t<1):return False
    elif not (0<=t<=1):return False
    P=tuple(P0[i]+t*(P1[i]-P0[i]) for i in range(3))
    signs=[]
    for U,V in ((A,B),(B,C),(C,A)):
        signs.append(_dot(_cross3(_sub(V,U),_sub(P,U)),n))
    if include_boundary:
        return all(s>=0 for s in signs) or all(s<=0 for s in signs)
    return all(s>0 for s in signs) or all(s<0 for s in signs)


def exact_coplanar_triangle_positive_overlap_3d(triangle_a, triangle_b):
    """True iff coplanar nondegenerate triangles overlap with positive area."""
    pts=tuple(_f(_point(p,3)) for p in tuple(triangle_a)+tuple(triangle_b))
    if len(pts)!=6:return False
    a,b,c,d,e,f=pts
    n1=_tri_normal_exact((a,b,c));n2=_tri_normal_exact((d,e,f))
    if n1==(0,0,0) or n2==(0,0,0):return False
    if _cross3(n1,n2)!=(0,0,0):return False
    if any(_dot(n1,_sub(p,a))!=0 for p in (d,e,f)):return False
    major=max(range(3),key=lambda k:abs(n1[k]))
    axes=tuple(k for k in range(3) if k!=major)
    A=[(p[axes[0]],p[axes[1]]) for p in (a,b,c)]
    B=[(p[axes[0]],p[axes[1]]) for p in (d,e,f)]
    # Strict convex SAT: zero overlap on any edge normal means edge/point contact only.
    for tri in (A,B):
        for i in range(3):
            p,q=tri[i],tri[(i+1)%3]
            edge=(q[0]-p[0],q[1]-p[1])
            if edge==(0,0):return False
            axis=(-edge[1],edge[0])
            proj_a=[x*axis[0]+y*axis[1] for x,y in A]
            proj_b=[x*axis[0]+y*axis[1] for x,y in B]
            overlap=min(max(proj_a),max(proj_b))-max(min(proj_a),min(proj_b))
            if overlap<=0:return False
    return True


def exact_signed_area_2d(points):
    """Exact signed polygon area (Fraction) for the supplied binary floats."""
    pts=tuple(_f(_point(p,2)) for p in points)
    if len(pts)<3:return Fraction(0)
    ox,oy=pts[0]
    local=[(x-ox,y-oy) for x,y in pts]
    twice=sum(local[i][0]*local[(i+1)%len(local)][1]-local[(i+1)%len(local)][0]*local[i][1]
              for i in range(len(local)))
    return twice/Fraction(2)



def exact_coplanar_face_triangle_overlap_3d(face_triangles_a, face_triangles_b):
    """Prove positive-area overlap between *generated* tessellated faces.

    Return the first (tri-a, tri-b) pair with exact coplanar positive overlap,
    or None. A deterministic interval sweep prevents O(N*M) empty-pair scans
    on large paths. This is a narrow-phase proof only; the caller supplies
    triangles with valid face ownership from Blender's read-only tessellation.
    Input is validated even when bounds are disjoint; zero-area triangles are
    rejected by the underlying exact triangle predicate, not guessed here.
    """
    faces=[]
    for raw in (face_triangles_a, face_triangles_b):
        parsed=[]
        for triangle in raw:
            pts=tuple(_point(point,3) for point in triangle)
            if len(pts)!=3:raise ValueError('RELIABILITY_PREDICATE: expected triangle')
            bounds=tuple((min(p[k] for p in pts),max(p[k] for p in pts)) for k in range(3))
            parsed.append((pts,bounds))
        faces.append(tuple(parsed))
    a,b=faces
    if not a or not b:return None
    all_tris=a+b
    axis=max(range(3),key=lambda k:max(t[1][k][1] for t in all_tris)-min(t[1][k][0] for t in all_tris))
    # Enter events sort before exit events at exact equality. Therefore a
    # boundary-touch candidate is not accidentally filtered as a near miss.
    events=[]
    for group,triangles in enumerate(faces):
        for index,(_,bounds) in enumerate(triangles):
            events.append((bounds[axis][0],0,group,index))
            events.append((bounds[axis][1],1,group,index))
    events.sort()
    active=({}, {})
    for _,kind,group,index in events:
        if kind==1:
            active[group].pop(index,None)
            continue
        tri,bounds=faces[group][index]
        other=1-group
        for other_index in active[other]:
            other_tri,other_bounds=faces[other][other_index]
            # Conservative lossless AABB broad phase in all three axes.
            if any(bounds[k][1]<other_bounds[k][0] or other_bounds[k][1]<bounds[k][0] for k in range(3)):
                continue
            if group==0:
                left,right=tri,other_tri
                left_index,right_index=index,other_index
            else:
                left,right=other_tri,tri
                left_index,right_index=other_index,index
            if exact_coplanar_triangle_positive_overlap_3d(left,right):
                return (left_index,right_index)
        active[group][index]=None
    return None
