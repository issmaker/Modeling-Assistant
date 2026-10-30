"""Dependency-free, read-only contour diagnostics.

BH0472 uses translation-stable local coordinates and exact stored-float segment
contact predicates. Distance tolerances may discover candidates but never prove
self-intersection.
"""
import math
from .robust_predicates import exact_segment_contact_3d


def _sub(a,b):return tuple(a[i]-b[i] for i in range(3))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _dot(a,b):return sum(a[i]*b[i] for i in range(3))


def analyze_contour(points, *, tolerance=1e-8):
    """Validate a simple planar 3D polygon with reproducible predicates."""
    try:
        vertices=[tuple(float(v) for v in p) for p in points]
    except (TypeError,ValueError,OverflowError) as exc:
        raise ValueError('Контур: ожидаются конечные XYZ') from exc
    if len(vertices)<3 or any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in vertices):
        raise ValueError('Контур: требуется не менее трёх конечных XYZ')
    if not math.isfinite(tolerance) or tolerance<=0:
        raise ValueError('Контур: некорректный допуск')
    if vertices[0]==vertices[-1]:vertices.pop()
    if len(vertices)<3:raise ValueError('Контур: недостаточно вершин')

    anchor=vertices[0]
    local=[_sub(p,anchor) for p in vertices]
    # Stable polygon area vector as a fan around a local origin.
    vectors=[_cross(local[i],local[i+1]) for i in range(1,len(local)-1)]
    n=tuple(math.fsum(v[k] for v in vectors) for k in range(3))
    length=math.hypot(*n)
    span=math.hypot(*(max(p[k] for p in local)-min(p[k] for p in local) for k in range(3)))
    if span<=0 or length<=max(1e-28,span*span*1e-14):
        raise ValueError('Контур: доказанно вырожденная площадь')
    normal=tuple(v/length for v in n)
    planar_tol=max(1e-12,tolerance,span*1e-9)
    if any(abs(_dot(p,normal))>planar_tol for p in local):
        raise ValueError('Контур: доказанно неплоский')
    for i,(a,b) in enumerate(zip(vertices,vertices[1:]+vertices[:1])):
        edge_len=math.dist(a,b)
        representation=max(math.ulp(abs(v)) for p in (a,b) for v in p)
        if edge_len<=max(1e-15,representation*4):
            raise ValueError('Контур: доказанное нулевое ребро')
        for j in range(i+1,len(vertices)):
            if j==i+1 or (i==0 and j==len(vertices)-1):continue
            c,d=vertices[j],vertices[(j+1)%len(vertices)]
            if exact_segment_contact_3d(a,b,c,d):
                raise ValueError('Контур: доказанное самопересечение или касание')
    return {'vertices':len(vertices),'normal':normal,'area':length/2,'decision':'VALID'}
