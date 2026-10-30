"""Brick Hole UV handedness without centroid guesses or mirrored island toggles.

Use signed polygon winding in UV space, independent of concavity.
Caller provides source loop order, UV at the chosen (directed) boundary edge,
and whether the source face's loop sequence traverses the edge A -> B.
"""
import math


def outward_from_source_uv(uv_a, uv_b, loop_uvs, a_to_b_forward):
    """Return a unit UV vector away from the face through edge A--B.

    Winding is defined on the actual ordered UV face loops. For polygons that
    overlap themselves in UV space or have near-zero signed area this is
    ambiguous and MUST fail closed rather than mirror randomly.
    """
    ring=tuple(tuple(float(c) for c in pair) for pair in loop_uvs)
    if len(ring)<3 or any(len(p)!=2 or not all(map(math.isfinite,p)) for p in ring):
        raise ValueError('Некорректный исходный UV-полигон')
    a,b=tuple(float(x) for x in uv_a),tuple(float(x) for x in uv_b)
    if any(not math.isfinite(c) for c in a+b):
        raise ValueError('Исходное UV-ребро содержит NaN/Inf')
    dx,dy=b[0]-a[0],b[1]-a[1]
    length=math.hypot(dx,dy)
    if length<1.e-10:raise ValueError('Нулевая UV-длина исходного ребра')
    # Shoelace computed near a local origin for large tiled UV coordinates.
    ox,oy=ring[0]
    signed_area_twice=math.fsum((ring[i][0]-ox)*(ring[(i+1)%len(ring)][1]-oy)-
                                (ring[(i+1)%len(ring)][0]-ox)*(ring[i][1]-oy)
                                for i in range(len(ring)))
    bounds=max(max(abs(p[0]-ox),abs(p[1]-oy)) for p in ring)
    if abs(signed_area_twice) <= max(1.e-15,1.e-12*bounds*bounds):
        raise ValueError('UV-полигон имеет неоднозначную ориентацию; зеркальное продолжение запрещено')
    # For a positive (CCW) ring its interior is left of each forward edge.
    # For reverse edge orientation, the sign changes correspondingly.
    polygon_inside_left=(signed_area_twice>0) == bool(a_to_b_forward)
    sign=-1. if polygon_inside_left else 1.
    return (sign*(-dy)/length,sign*dx/length)
