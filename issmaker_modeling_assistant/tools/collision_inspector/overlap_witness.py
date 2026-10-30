"""Conservative, Blender-independent evidence of positive-area coplanar overlap.

Returns the actual closed-region vertices of a *triangle pair*, in 3D,
without modifying the meshes. It intentionally does not claim to compute the
union boundary across multiple triangulated faces. A shared edge or point,
non-coplanar pair, degenerate triangle or numerical ambiguity returns None.
"""
import math
from ...core.reliability.precision import policy_from_points
from ...core.reliability.predicates import localize_points


def _sub(a, b):
    return tuple(x-y for x,y in zip(a,b))


def _dot(a, b):
    return sum(x*y for x,y in zip(a,b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _norm(a):
    return math.sqrt(_dot(a,a))


def _area2(poly):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(poly,poly[1:]+poly[:1]))


def overlap_polygon(triangle_a, triangle_b):
    """Return 3+ world/local-space overlap vertices, or None.

    Coordinates are returned in the coordinate system of input triangles.
    Edges of individual clipped triangles are NOT asserted to be the outline
    of the union of multiple coplanar face overlaps.
    """
    a = tuple(tuple(float(v) for v in p) for p in triangle_a)
    b = tuple(tuple(float(v) for v in p) for p in triangle_b)
    if len(a)!=3 or len(b)!=3 or any(len(p)!=3 for p in a+b):
        return None
    if not all(math.isfinite(v) for p in a+b for v in p):
        return None
    policy = policy_from_points(a+b)
    origin = a[0]
    a_local = tuple(localize_points(a, origin))
    b_local = tuple(localize_points(b, origin))
    edges = [_sub(tri[(i+1)%3],tri[i]) for tri in (a_local,b_local) for i in range(3)]
    scale = max((_norm(v) for v in edges),default=0.0)
    if not scale or not math.isfinite(scale) or scale < policy.linear:
        return None
    a, b = a_local, b_local
    na = _cross(_sub(a[1],a[0]),_sub(a[2],a[0]))
    nb = _cross(_sub(b[1],b[0]),_sub(b[2],b[0]))
    la, lb = _norm(na), _norm(nb)
    if min(la,lb) <= policy.area:
        return None
    na, nb = tuple(x/la for x in na),tuple(x/lb for x in nb)
    if _norm(_cross(na,nb)) > policy.angular:
        return None
    plane_epsilon=policy.plane
    if any(abs(_dot(_sub(pt,a[0]),na))>plane_epsilon for pt in b):
        return None
    major=max(range(3),key=lambda k:abs(na[k]))
    axes=tuple(k for k in range(3) if k!=major)
    project=lambda tri:[(p[axes[0]],p[axes[1]]) for p in tri]
    poly, clip = project(a),project(b)
    if _area2(clip)==0:
        return None
    direction=1 if _area2(clip)>0 else -1
    # This is a signed-area edge test. Clamp crossing interpolation only where
    # it is supported by distinct signed distances.
    eps=policy.linear
    for p,q in zip(clip,clip[1:]+clip[:1]):
        if not poly:
            return None
        def side(v):
            return direction*((q[0]-p[0])*(v[1]-p[1])-(q[1]-p[1])*(v[0]-p[0]))
        output=[]
        prev=poly[-1]; prev_dist=side(prev)
        for cur in poly:
            cur_dist=side(cur)
            was_in=prev_dist>=-eps*scale
            is_in=cur_dist>=-eps*scale
            if was_in!=is_in:
                denom=prev_dist-cur_dist
                if not denom:
                    return None
                t=max(0.0,min(1.0,prev_dist/denom))
                output.append((prev[0]+t*(cur[0]-prev[0]),prev[1]+t*(cur[1]-prev[1])))
            if is_in:
                output.append(cur)
            prev,prev_dist=cur,cur_dist
        # Remove repeated points from clipped parallel/corner edges.
        poly=[]
        for pt in output:
            if not poly or math.dist(pt,poly[-1])>eps:
                poly.append(pt)
        if len(poly)>1 and math.dist(poly[0],poly[-1])<=eps:
            poly.pop()
    if len(poly)<3:
        return None
    # Projection onto major-plane removes area by |normal.major|.
    if abs(_area2(poly)) <= max(policy.area, scale*scale*1e-13):
        return None
    plane=_dot(na,a[0])
    points=[]
    for pt in poly:
        full=[0.0,0.0,0.0]
        full[axes[0]],full[axes[1]]=pt
        full[major]=(plane-na[axes[0]]*pt[0]-na[axes[1]]*pt[1])/na[major]
        if not all(math.isfinite(x) for x in full):
            return None
        if abs(_dot(_sub(full,a[0]),na))>plane_epsilon:
            return None
        points.append(tuple(full[i] + origin[i] for i in range(3)))
    return points
