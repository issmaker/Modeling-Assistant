"""Subpixel-safe UV self-intersection predicate for Blender float32 UV loops.

Do not disable winding verification. Intersections only count when their
orientation exceeds the conservative float32 quantization envelope of both
edges. A true bow-tie intersection remains an error.
"""
from math import hypot, isfinite


def strict_uv_crossing(a,b,c,d,base_tolerance=1.e-12):
    """Return True for reliable proper crossings; near-collinear is ambiguous.

    Blender MeshLoopUV stores single-precision UVs, so comparing determinant
    signs at ~1e-12 over a 0..1 tile can generate reproducible false positives.
    The tolerance is derived from the lengths of the two *original* edges,
    not a made-up shift of vertex coordinates or changes to published UVs.
    """
    vals=[tuple(float(t) for t in pair[:2]) if hasattr(pair,'__getitem__')
          else (float(pair.x),float(pair.y)) for pair in (a,b,c,d)]
    if not all(isfinite(v) for pt in vals for v in pt):
        raise ValueError('UV coordinates contain nonfinite values')
    a,b,c,d=vals
    orientation=lambda x,y,z: ((y[0]-x[0])*(z[1]-x[1])
                               -(y[1]-x[1])*(z[0]-x[0]))
    len_ab=hypot(b[0]-a[0],b[1]-a[1]);len_cd=hypot(d[0]-c[0],d[1]-c[1])
    # Float32 UV coordinates have absolute granularity around 1.2e-7 at U=1.
    # The determinant's worst-case perturbation scales with edge length.
    eps=max(float(base_tolerance),3.e-7*max(len_ab,len_cd))
    a1,a2=orientation(a,b,c),orientation(a,b,d)
    b1,b2=orientation(c,d,a),orientation(c,d,b)
    return ((a1>eps and a2<-eps or a1<-eps and a2>eps)
            and (b1>eps and b2<-eps or b1<-eps and b2>eps))


def face_has_uv_crossing(uvs,base_tolerance=1.e-12):
    uvs=list(uvs);n=len(uvs)
    for i in range(n):
        for j in range(i+1,n):
            if j==i+1 or (i==0 and j==n-1):continue
            if strict_uv_crossing(uvs[i],uvs[(i+1)%n],
                                  uvs[j],uvs[(j+1)%n],base_tolerance):
                return True
    return False
