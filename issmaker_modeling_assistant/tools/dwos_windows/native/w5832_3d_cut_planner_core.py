"""ISSMAKER W58.32 — double precision 3D plane-contact cut planner.

For NON-coplanar convex polygon surface pairs, prove the finite segment
where planes AND polygon interiors intersect. No geometry writes.
"""
import math
def dot(a,b):return sum(a[i]*b[i] for i in range(3))
def sub(a,b):return tuple(a[i]-b[i] for i in range(3))
def add(a,b):return tuple(a[i]+b[i] for i in range(3))
def scale(a,v):return tuple(v*q for q in a)
def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def length(a):return math.sqrt(dot(a,a))
def normal(points):
    origin=points[0]; best=(0.,None)
    for i in range(1,len(points)):
        for j in range(i+1,len(points)):
            cr=cross(sub(points[i],origin),sub(points[j],origin))
            if length(cr)>best[0]:best=length(cr),cr
    if best[0]<1e-12:raise ValueError('3D_ARRANGEMENT_FACE_DEGENERATE')
    return scale(best[1],1./best[0])
def plane_slice(poly,origin,n,eps=1e-8):
    """Intersect convex planar polygon edges with plane (origin,n)."""
    points=[]
    for i,a in enumerate(poly):
        b=poly[(i+1)%len(poly)]
        da=dot(n,sub(a,origin));db=dot(n,sub(b,origin))
        if abs(da)<=eps:points.append(a)
        if da*db < -(eps*eps):
            t=da/(da-db)
            points.append(add(a,scale(sub(b,a),t)))
        elif abs(db)<=eps:points.append(b)
    uniq=[]
    for q in points:
        if not any(math.dist(q,x)<=eps*2 for x in uniq):uniq.append(q)
    return uniq
def noncoplanar_intersection_segment(face_a,face_b,*,eps=1e-7):
    a=[tuple(float(v) for v in p) for p in face_a]
    b=[tuple(float(v) for v in p) for p in face_b]
    if min(len(a),len(b))<3:raise ValueError('3D_ARRANGEMENT_SHORT_POLYGON')
    na=normal(a);nb=normal(b)
    d=cross(na,nb);dl=length(d)
    if dl<1e-8:
        return {'classification':'PARALLEL_NOT_3D_SEGMENT','normals_abs_dot':abs(dot(na,nb))}
    u=scale(d,1/dl)
    qa=plane_slice(a,b[0],nb,eps)
    qb=plane_slice(b,a[0],na,eps)
    if min(len(qa),len(qb))<2:
        return {'classification':'NO_INTERIOR_3D_CUT','slice_counts':[len(qa),len(qb)]}
    origin=qa[0]
    t1=sorted(dot(sub(q,origin),u) for q in qa)
    t2=sorted(dot(sub(q,origin),u) for q in qb)
    lo=max(t1[0],t2[0]);hi=min(t1[-1],t2[-1])
    if hi-lo<=eps:
        return {'classification':'ZERO_LENGTH_3D_CONTACT','length_m':max(0.,hi-lo)}
    p=add(origin,scale(u,lo));q=add(origin,scale(u,hi))
    max_error=max(abs(dot(na,sub(x,a[0]))) for x in (p,q))
    max_error=max(max_error,max(abs(dot(nb,sub(x,b[0]))) for x in (p,q)))
    if max_error>eps*8:
        # Finite-precision Blender vertices can be non-coplanar by up to
        # several float32 ULPs at large world coordinates. Do not call
        # them malformed geometry or claim a proven cut in this case.
        coords=[abs(float(c)) for poly in (a,b) for point in poly for c in point]
        def fp32_ulp(v):
            return 2.**(math.floor(math.log2(v))-23) if v>1.17549435e-38 else 1.401298464e-45
        envelope=max(eps*8,8*max(fp32_ulp(v) for v in coords))
        if max_error<=envelope:
            return {'classification':'NUMERICALLY_UNCERTAIN_FLOAT32_PLANE',
                    'max_plane_residual_m':max_error,
                    'float32_ulp_envelope_m':envelope,
                    'nominal_segment_length_m':hi-lo,
                    'not_authorized_for_mesh_cut':True}
        raise ValueError('3D_ARRANGEMENT_PLANE_RESIDUAL_TOO_LARGE')
    return {'classification':'FINITE_3D_INTERSECTION_SEGMENT','length_m':hi-lo,
            'endpoints_xyz':[list(p),list(q)],'max_plane_residual_m':max_error,
            'normals_abs_dot':abs(dot(na,nb))}
