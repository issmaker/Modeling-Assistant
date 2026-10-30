"""BH-03.2D: pure, readonly eligibility and vertex snapping math.

The direction filter is a necessary geometric criterion, NOT proof of a safe
extrusion. A production unified_preflight remains compulsory on selection,
snapping and Apply. The snap projection never changes any authored vertex.
"""
import math


def _unit(vector):
    try:
        a=tuple(float(x) for x in vector)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Некорректный вектор направления') from exc
    if len(a)!=3 or not all(math.isfinite(x) for x in a):
        raise ValueError('Направление должно иметь три конечных координаты')
    length=math.hypot(*a)
    if not math.isfinite(length) or length < 1e-12:raise ValueError('Нулевое направление')
    return tuple(x/length for x in a)


def axis_eligibility(axis, *, edge_tangents=(), face_normals=(), edge_limit=0.025,
                     face_limit=0.0001):
    """No false-positive ENABLE for known tangency. Unrelated guards run later."""
    a=_unit(axis)
    try:
        for index,t in enumerate(edge_tangents):
            q=_unit(t)
            if abs(sum(a[i]*q[i] for i in range(3))) > edge_limit:
                return False, f'Ось не перпендикулярна ребру {index+1}'
        for index,n in enumerate(face_normals):
            q=_unit(n)
            if abs(sum(a[i]*q[i] for i in range(3))) < face_limit:
                return False, f'Ось лежит в плоскости грани {index+1}'
    except ValueError as exc:
        return False,str(exc)
    return True,'Предварительная геометрия допустима; Apply проверяется отдельно'


def project_vertex_depth(origin, direction, vertex, *, tolerance_m=0.025,
                         minimum_m=0.001, maximum_m=50.):
    """Signed scalar for an arbitrary nonunit world-space handle offset.

    Rejects visually-near but geometrically-off-axis vertices, and refuses
    zero/out-of-range depth rather than silently clamp or distort placement.
    """
    if tolerance_m is not None:
        try:allowed=float(tolerance_m)
        except (TypeError,ValueError,OverflowError) as exc:
            raise ValueError('Недопустимый 3D допуск Vertex Snap') from exc
        if not math.isfinite(allowed) or allowed < 0:
            raise ValueError('Недопустимый 3D допуск Vertex Snap')
        tolerance_m=allowed
    u=_unit(direction)
    d=tuple(float(x) for x in direction)
    scale=math.hypot(*d)
    p=tuple(float(x) for x in origin)
    q=tuple(float(x) for x in vertex)
    if len(p)!=3 or len(q)!=3 or not all(math.isfinite(x) for x in p+q):
        raise ValueError('Некорректные координаты вершины привязки')
    delta=tuple(q[i]-p[i] for i in range(3))
    length=sum(delta[i]*u[i] for i in range(3))
    residual=math.hypot(*(delta[i]-length*u[i] for i in range(3)))
    if tolerance_m is not None and residual>tolerance_m:
        raise ValueError(f'Вершина в {residual:.3f} м от оси ручки')
    scalar=length/scale
    if not (minimum_m<=abs(scalar)<=maximum_m):
        raise ValueError('Глубина привязки вне допустимого диапазона')
    return scalar,residual


def choose_vertex_candidate(candidates, pointer_xy, origin, direction, *,
                            pixel_radius=18., axis_tolerance_m=None,
                            minimum_m=.001, maximum_m=50.):
    """Find a *real* BMesh vertex under the cursor and project onto handle.

    Candidate tuples: (actual_BMesh_index, world_xyz, (pixel_x,pixel_y)).
    World-axis projection (not teleport to the lateral vertex) is deliberate:
    wall endpoints frequently lie metres away from the middle handle axis.
    The returned ``projected_xyz`` is the point ON the handle, while
    ``world_xyz`` remains the vertex picked by the user. Never mutates Mesh.
    No occlusion proof is implied by merely having a screen projection.
    """
    px, py = (float(k) for k in pointer_xy)
    r=float(pixel_radius)
    if not (math.isfinite(px) and math.isfinite(py) and math.isfinite(r)
            and 1 <= r <= 60):
        raise ValueError('Недопустимые пиксельные параметры Vertex Snap')
    dx=tuple(float(x) for x in direction)
    if len(dx)!=3 or not all(math.isfinite(x) for x in dx):
        raise ValueError('Недопустимое направление Vertex Snap')
    anchor=tuple(float(x) for x in origin)
    if len(anchor)!=3 or not all(math.isfinite(x) for x in anchor):
        raise ValueError('Недопустимая опорная точка Vertex Snap')
    nearest=None
    for vertex_index, xyz, screen in candidates:
        if screen is None:
            continue
        try:
            sx,sy=(float(k) for k in screen)
            if not (math.isfinite(sx) and math.isfinite(sy)):
                continue
            px2=(px-sx)**2+(py-sy)**2
            if px2 > r*r:
                continue
            depth,residual=project_vertex_depth(
                anchor,dx,xyz,tolerance_m=axis_tolerance_m,
                minimum_m=minimum_m,maximum_m=maximum_m)
            candidate=(px2,residual,int(vertex_index),tuple(float(x) for x in xyz),depth)
            if nearest is None or candidate[:3]<nearest[:3]:
                nearest=candidate
        except (ValueError,TypeError,OverflowError):
            continue
    if nearest is None:
        return None
    px2,residual,index,world,depth=nearest
    projection=tuple(anchor[i]+dx[i]*depth for i in range(3))
    return {'vertex_index':index,'world_xyz':world,'projected_xyz':projection,
            'signed_depth':depth,'axis_residual_m':residual,'screen_distance_px':math.sqrt(px2),
            'is_projected':residual>.025}
