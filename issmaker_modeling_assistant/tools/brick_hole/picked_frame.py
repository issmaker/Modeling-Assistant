"""Stable face-local world axes for Brick Hole (pure Python, no Blender API).

Z follows the selected polygon world normal. X follows its strongest edge,
projected onto the polygon tangent plane, and Y=Z cross X. The frame is
right-handed and valid after arbitrary transforms, including mirrored ones
when the caller supplies the polygon's *world-facing* normal.
"""
from __future__ import annotations
import math


def _vec3(value):
    try:
        result = tuple(float(v) for v in value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Некорректные координаты полигона') from exc
    if len(result) != 3 or not all(math.isfinite(v) for v in result):
        raise ValueError('Невалидный вектор полигона')
    return result


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _unit(value):
    magnitude = math.sqrt(_dot(value, value))
    if not math.isfinite(magnitude) or magnitude < 1e-10:
        raise ValueError('Невозможно определить локальные оси полигона')
    return tuple(v / magnitude for v in value)


def frame_from_polygon(vertices_world, normal_world):
    """Return right-handed world-space (X,Y,Z); never mutate source vertices."""
    vertices = tuple(_vec3(p) for p in vertices_world)
    if len(vertices) < 3:
        raise ValueError('Для локального базиса нужна грань с тремя вершинами')
    z = _unit(_vec3(normal_world))
    # Longest nonzero projected boundary edge = robust tangent on dense/irregular
    # contours, avoiding fragile dependence on a tiny first edge.
    candidates = []
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        edge = tuple(b[i]-a[i] for i in range(3))
        projection = _dot(edge, z)
        tangent = tuple(edge[i]-projection*z[i] for i in range(3))
        score = _dot(tangent, tangent)
        if score > 1e-18:
            candidates.append((score, tangent))
    if not candidates:
        raise ValueError('Нельзя определить ось X: вырожденные рёбра полигона')
    x = _unit(max(candidates, key=lambda p:p[0])[1])
    y = _unit(_cross(z, x))
    x = _unit(_cross(y, z))
    return (x, y, z)


def axis_vector(frame, axis):
    if axis not in ('X', 'Y', 'Z'):
        raise ValueError('Ось должна быть X, Y или Z')
    if len(frame) != 3:
        raise ValueError('Локальный базис полигона не определён')
    return _unit(_vec3(frame['XYZ'.index(axis)]))


def is_orthonormal_right_handed(frame, tolerance=1e-6):
    try:
        x,y,z = (axis_vector(frame, axis) for axis in 'XYZ')
        return (all(abs(_dot(_vec3(v),_vec3(v))-1) <= tolerance for v in frame)
                and max(abs(_dot(x,y)),abs(_dot(y,z)),abs(_dot(z,x))) <= tolerance
                and _dot(_cross(x,y),z) >= 1-tolerance)
    except (ValueError,TypeError,IndexError):
        return False
