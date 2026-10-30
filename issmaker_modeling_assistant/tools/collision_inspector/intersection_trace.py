"""Conservative non-coplanar triangle intersection traces; no Blender dependencies.

The function returns an actual 3D segment only when there are two distinct,
verified intersection points. An empty result is NOT proof of no collision:
coplanar, tangential or numerically uncertain cases intentionally have no trace.
"""
import math


def _sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def _add(a, b):
    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])


def _mul(a, t):
    return tuple(x*t for x in a)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _norm(a):
    return math.sqrt(_dot(a, a))


def _inside_triangle(point, tri, tolerance):
    a, b, c = tri
    u, v, w = _sub(b, a), _sub(c, a), _sub(point, a)
    uu, uv, vv = _dot(u, u), _dot(u, v), _dot(v, v)
    wu, wv = _dot(w, u), _dot(w, v)
    denom = uu*vv - uv*uv
    if denom <= 0 or not math.isfinite(denom):
        return False
    s = (wu*vv - wv*uv)/denom
    t = (wv*uu - wu*uv)/denom
    return s >= -tolerance and t >= -tolerance and s+t <= 1+tolerance


def triangle_segment(triangle_a, triangle_b):
    """Return (endpoint_a, endpoint_b) or None for a strict noncoplanar segment.

    Coordinates can be tuples or mathutils.Vector. The returned segment is in
    the same coordinate system as inputs; callers add their BVH origin offset.
    """
    tri_a = tuple(tuple(float(x) for x in p) for p in triangle_a)
    tri_b = tuple(tuple(float(x) for x in p) for p in triangle_b)
    if not all(math.isfinite(x) for tri in (tri_a, tri_b) for p in tri for x in p):
        return None
    edges = [_sub(tri[i], tri[(i+1)%3]) for tri in (tri_a, tri_b) for i in range(3)]
    scale = max((_norm(edge) for edge in edges), default=0.0)
    if scale <= 1e-12:
        return None
    epsilon = max(1e-12, scale*1e-10)
    n_a = _cross(_sub(tri_a[1],tri_a[0]),_sub(tri_a[2],tri_a[0]))
    n_b = _cross(_sub(tri_b[1],tri_b[0]),_sub(tri_b[2],tri_b[0]))
    len_a, len_b = _norm(n_a), _norm(n_b)
    if len_a <= epsilon*scale or len_b <= epsilon*scale:
        return None
    unit_a, unit_b = _mul(n_a,1/len_a),_mul(n_b,1/len_b)
    if _norm(_cross(unit_a,unit_b)) <= 1e-8:
        return None  # coplanar or almost parallel -> no fabricated 3D trace
    hits=[]
    for source, target, normal in ((tri_a,tri_b,unit_b),(tri_b,tri_a,unit_a)):
        for i in range(3):
            start, end = source[i], source[(i+1)%3]
            d0 = _dot(_sub(start,target[0]),normal)
            d1 = _dot(_sub(end,target[0]),normal)
            if abs(d0-d1) <= epsilon or not ((d0 <= epsilon and d1 >= -epsilon) or (d1 <= epsilon and d0 >= -epsilon)):
                continue
            t = d0/(d0-d1)
            if not -1e-8 <= t <= 1+1e-8:
                continue
            p=_add(start,_mul(_sub(end,start),t))
            if _inside_triangle(p,target,1e-8):
                if all(_norm(_sub(p,q)) > epsilon*4 for q in hits):
                    hits.append(p)
    if len(hits) < 2:
        return None
    first, second=max(((p,q) for i,p in enumerate(hits) for q in hits[i+1:]),
                      key=lambda pair:_dot(_sub(pair[0],pair[1]),_sub(pair[0],pair[1])))
    if _norm(_sub(first,second)) <= epsilon*4:
        return None
    return first,second
