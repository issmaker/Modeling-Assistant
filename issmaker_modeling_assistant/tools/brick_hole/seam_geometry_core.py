"""BH0467 independent, read-only geometry oracle for an internal module edge.

Pure Python; no Blender, UV edit, GPU or dependency on CheckToolBox code.
A 4×4 m wall with two 2×4 m quads must prove that their shared
vertical edge is INSIDE the 4×4 module, rather than a tile boundary.
This is positive geometric evidence, not a universal module recognizer.
"""
import math


def _diff(a, b):
    return tuple(float(a[i]) - float(b[i]) for i in range(3))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1],
            a[2]*b[0]-a[0]*b[2],
            a[0]*b[1]-a[1]*b[0])


def _normalized(a):
    length=math.sqrt(_dot(a,a))
    if length <= 1.e-10 or not math.isfinite(length):
        return None
    return tuple(v/length for v in a)


def _normal(points):
    if len(points)<3:return None
    origin=points[0]
    for index in range(1,len(points)-1):
        a=_diff(points[index],origin)
        for j in range(index+1,len(points)):
            result=_normalized(_cross(a,_diff(points[j],origin)))
            if result is not None:return result
    return None


def module_pair_signature(shared_edge, face_a, face_b, sizes=(4.0, 2.0),
                          size_tolerance_m=0.025, plane_tolerance_m=0.001):
    """Recognize a *complete* coplanar square split into exactly two faces.

    Uses the physical common edge as one in-plane basis direction, hence
    works equally for XY / XZ / tilted walls and independently of UV edits.
    Does not assume that every coplanar patch is a UV Wall module. Returns
    a reasoned witness only for a measured 4×4 or 2×2 pair.
    """
    try:
        a,b=tuple(tuple(map(float,p)) for p in shared_edge)
        pa=[tuple(map(float,p)) for p in face_a]
        pb=[tuple(map(float,p)) for p in face_b]
        all_pts=pa+pb
        if not all_pts or any(len(p)!=3 or not all(math.isfinite(v) for v in p)
                              for p in all_pts+[a,b]):return None
        along=_normalized(_diff(b,a))
        normal_a=_normal(pa);normal_b=_normal(pb)
        if along is None or normal_a is None or normal_b is None:return None
        # Opposing normals can be intentional double-sided geometry, NOT one module.
        if _dot(normal_a,normal_b)<0.9995:return None
        if max(abs(_dot(_diff(p,a),normal_a)) for p in all_pts)>plane_tolerance_m:
            return None
        across=_normalized(_cross(normal_a,along))
        if across is None:return None
        length_bounds=tuple(_dot(_diff(p,a),along) for p in all_pts)
        width_bounds=tuple(_dot(_diff(p,a),across) for p in all_pts)
        length=max(length_bounds)-min(length_bounds)
        width=max(width_bounds)-min(width_bounds)
        # The common edge must be an internal separator, not a module border.
        mid_a=sum(_dot(_diff(p,a),across) for p in pa)/len(pa)
        mid_b=sum(_dot(_diff(p,a),across) for p in pb)/len(pb)
        if mid_a*mid_b>=-1.e-8:return None
        if abs(mid_a-mid_b)<0.015:return None
        for size in sizes:
            if abs(length-size)<=size_tolerance_m and abs(width-size)<=size_tolerance_m:
                return {'module_size_m':float(size),
                        'length_m':round(length,7), 'width_m':round(width,7),
                        'shared_edge_inside':True,
                        'confidence':'GEOMETRY_PAIR_EXACT'}
    except (ValueError,TypeError,IndexError,ZeroDivisionError,OverflowError):
        return None
    return None
