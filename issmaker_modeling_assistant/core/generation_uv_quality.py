"""Generation-time, opt-in, Blender-independent UV staging.

DWOS creates frame/glass/interior surfaces without UV data in the 1.8.8.47
baseline. This module provides a conservative metric, positive-winding UV
projection for **whole** eligible meshes. It deliberately refuses any partial
UV map, oversized face, non-planar face or degenerate geometry. Face islands
may stack (repeat the same texture); this is not atlas packing or UDIM tiling.
No geometry, face order or materials are modified here.
"""
from math import isfinite, sqrt

UV_SCHEMA = 'DWOS_FACE_LOCAL_1000_PX_M_V1'


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2],
            a[0]*b[1] - a[1]*b[0])


def _norm(a):
    return sqrt(_dot(a, a))


def _unit(a):
    length = _norm(a)
    if length <= 1.e-10:
        raise ValueError('Degenerate surface basis')
    return tuple(x / length for x in a)


def _newell(points):
    n = [0., 0., 0.]
    for a, b in zip(points, points[1:] + points[:1]):
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    return _unit(n)


def _area(points):
    return .5 * sum(a[0]*b[1]-b[0]*a[1]
                    for a, b in zip(points, points[1:]+points[:1]))


def _folded_polygon(points, tol=1.e-10):
    """Conservatively reject self-crossing or overlapping non-adjacent edges."""
    n=len(points)
    def orient(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def on_edge(a,b,c):
        return (min(a[0],b[0])-tol<=c[0]<=max(a[0],b[0])+tol
                and min(a[1],b[1])-tol<=c[1]<=max(a[1],b[1])+tol
                and abs(orient(a,b,c))<=tol)
    for i in range(n):
        a,b=points[i],points[(i+1)%n]
        for j in range(i+1,n):
            if j==i+1 or (i==0 and j==n-1):
                continue
            c,d=points[j],points[(j+1)%n]
            o1,o2=orient(a,b,c),orient(a,b,d)
            o3,o4=orient(c,d,a),orient(c,d,b)
            if ((o1>tol and o2<-tol or o1<-tol and o2>tol)
                    and (o3>tol and o4<-tol or o3<-tol and o4>tol)):
                return True
            if ((abs(o1)<=tol and on_edge(a,b,c)) or
                (abs(o2)<=tol and on_edge(a,b,d)) or
                (abs(o3)<=tol and on_edge(c,d,a)) or
                (abs(o4)<=tol and on_edge(c,d,b))):
                return True
    return False


def stage_face_uvs(vertices, faces, resolution=4096, pixels_per_meter=1000,
                   margin_pixels=8, coplanar_tolerance_m=0.0001):
    """Return {state, face_uvs, reason, ...}; never return incomplete UV loops.

    Coordinates are in mesh-local metres; no object scale is folded in. The
    caller must guarantee applied, metric-safe object transforms. 4K has an
    inner 4080-pixel region, so a 4-metre face can fit at 1000 px/metre.
    """
    if resolution not in (2048, 4096) or pixels_per_meter != 1000:
        raise ValueError('Supported contracts are 2K/4K at 1000 px/metre')
    if not (0 <= margin_pixels <= 24) or not 0 < coplanar_tolerance_m < .01:
        raise ValueError('Invalid UV staging tolerances')
    verts = tuple(tuple(float(c) for c in v) for v in vertices)
    polys = tuple(tuple(int(index) for index in f) for f in faces)
    if any(len(v) != 3 or not all(isfinite(c) for c in v) for v in verts):
        raise ValueError('Non-finite or malformed mesh vertices')
    if not polys:
        return {'state': 'EMPTY', 'face_uvs': (), 'reason': 'NO_FACES',
                'faces': 0, 'resolution': resolution}
    if len(verts) > 500000 or len(polys) > 250000:
        return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'FACE_LIMIT',
                'faces': len(polys), 'resolution': resolution}
    scale = pixels_per_meter / float(resolution)
    pad = margin_pixels / float(resolution)
    limit = 1. - pad
    output = []
    for fi, face in enumerate(polys):
        if len(face) > 128:
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'COMPLEX_FACE_UNSUPPORTED',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        if len(face) < 3 or len(set(face)) != len(face) or any(v < 0 or v >= len(verts) for v in face):
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'INVALID_TOPOLOGY',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        pts = [verts[i] for i in face]
        try:
            normal = _newell(pts)
            edges = [_sub(pts[(j + 1) % len(pts)], pts[j]) for j in range(len(pts))]
        except ValueError:
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'DEGENERATE_FACE',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        anchor = pts[0]
        if any(abs(_dot(_sub(p, anchor), normal)) > coplanar_tolerance_m for p in pts):
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'NONPLANAR_FACE',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        # A longest-edge axis can make a 2 x 2m right triangle project to a
        # 2.83m-wide bounding box. Try all authored edge directions and choose
        # the most compact metric rectangle without rotating/altering vertices.
        candidates = []
        for edge in edges:
            if _dot(edge, edge) <= 1.e-20:
                continue
            tangent = _unit(edge)
            bitangent = _unit(_cross(normal, tangent))
            metric_candidate = [(_dot(_sub(p, anchor), tangent),
                                 _dot(_sub(p, anchor), bitangent)) for p in pts]
            width = max(u for u,v in metric_candidate)-min(u for u,v in metric_candidate)
            height = max(v for u,v in metric_candidate)-min(v for u,v in metric_candidate)
            candidates.append((max(width,height),width*height,metric_candidate))
        if not candidates:
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'DEGENERATE_FACE',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        _, _, metric = min(candidates, key=lambda c: (c[0],c[1]))
        if _folded_polygon(metric):
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'FOLDED_FACE',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        if _area(metric) < 1.e-12:
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'DEGENERATE_UV',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        low_u = min(u for u, _ in metric)
        low_v = min(v for _, v in metric)
        projected = tuple((round((u - low_u)*scale + pad, 12),
                           round((v - low_v)*scale + pad, 12)) for u, v in metric)
        if any(u < pad-1.e-8 or v < pad-1.e-8 or u > limit+1.e-8 or v > limit+1.e-8
               for u, v in projected):
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'FACE_EXCEEDS_TILE',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        if _area(projected) <= 1.e-12:
            return {'state': 'UNMAPPED', 'face_uvs': (), 'reason': 'PROJECTION_COLLAPSED',
                    'face_index': fi, 'faces': len(polys), 'resolution': resolution}
        output.append(projected)
    return {'state': 'READY', 'face_uvs': tuple(output), 'reason': 'OK',
            'faces': len(output), 'resolution': resolution,
            'pixels_per_meter': pixels_per_meter,
            'schema': UV_SCHEMA, 'face_islands_may_stack': True}


def winding_summary(face_uvs, epsilon=1.e-12):
    """Read-only diagnostic; a negative area is not necessarily a defect."""
    positives = negatives = zeros = 0
    for face in face_uvs:
        area = _area([tuple(float(x) for x in uv) for uv in face])
        if area > epsilon:
            positives += 1
        elif area < -epsilon:
            negatives += 1
        else:
            zeros += 1
    return {'positive': positives, 'negative': negatives, 'zero': zeros,
            'review_required': bool(negatives or zeros)}
