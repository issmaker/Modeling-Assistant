"""Predictably repair winding in existing DWOS meshes (no bpy dependency).

The normal direction of an *open* shell cannot be inferred from volume. Anchor
orientations to named DWOS faces and propagate the opposite-edge rule through
all connected components. Refuse inconsistent/non-manifold source topology.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque


def _face_normal(verts, face):
    """Newell normal supports valid n-gons whose first three points are collinear."""
    if len(face) < 3:
        raise ValueError('Нужны минимум три вершины')
    points = [tuple(float(x) for x in verts[i]) for i in face]
    if any(len(p) != 3 or not all(math.isfinite(x) for x in p) for p in points):
        raise ValueError('Некорректные координаты полигона')
    origin = points[0]
    local = [tuple(p[k] - origin[k] for k in range(3)) for p in points]
    scale = max(abs(x) for p in local for x in p)
    if not math.isfinite(scale) or scale < 1e-12:
        raise ValueError('Нулевая площадь полигона: нельзя определить нормаль')
    local = [tuple(x / scale for x in p) for p in local]
    n = tuple(math.fsum(a[(k+1)%3]*b[(k+2)%3] - a[(k+2)%3]*b[(k+1)%3]
                        for a, b in zip(local, local[1:]+local[:1]))
              for k in range(3))
    size = math.hypot(*n)
    if not math.isfinite(size) or size < 1e-14:
        raise ValueError('Нулевая площадь полигона: нельзя определить нормаль')
    return tuple(value / size for value in n)


def anchor_direction(role, interior_sign):
    exterior = -interior_sign
    if role in {'FRAME_EXTERIOR', 'GLASS_EXTERIOR', 'GLASS_SINGLE'}:
        return (0., 0., float(exterior))
    if role in {'FRAME_INTERIOR_BACK', 'GLASS_INTERIOR', 'INTERIOR_V_BACK_RETURN'}:
        return (0., 0., float(interior_sign))
    if role == 'INTERIOR_BACK':
        # Back of an open-front cavity looks toward the aperture, not away.
        return (0., 0., float(exterior))
    return None


def compute_face_flips(vertices, faces, roles, interior_sign=-1):
    """Return indices of faces requiring reversal; raise on conflicting topology.

    Anchored faces (front/back frame, glass sheets, interior cap) keep a known
    direction. All other faces are fixed using consistent edge-winding parity.
    """
    if interior_sign not in (-1, 1):
        raise ValueError('Некорректный Interior direction')
    if len(faces) != len(roles):
        raise ValueError('DWOS roles не совпадают с количеством граней')
    edges = defaultdict(list)
    for fi, face in enumerate(faces):
        if len(face) < 3 or len(set(face)) != len(face):
            raise ValueError(f'Проблемная грань {fi}')
        for a, b in zip(face, face[1:] + face[:1]):
            if a == b:
                raise ValueError(f'Вырожденное ребро грани {fi}')
            edge = (a, b) if a < b else (b, a)
            edges[edge].append((fi, 1 if a < b else -1))
    adjacent = defaultdict(list)
    for edge, using in edges.items():
        if len(using) > 2:
            raise ValueError(f'Non-manifold ребро {edge}: {len(using)} граней')
        if len(using) == 2:
            (f0, d0), (f1, d1) = using
            if f0 == f1:
                raise ValueError(f'Грань использует ребро {edge} дважды')
            adjacent[f0].append((f1, d0 == d1))
            adjacent[f1].append((f0, d0 == d1))

    anchored = {}
    for fi, (face, role) in enumerate(zip(faces, roles)):
        wanted = anchor_direction(str(role).upper(), interior_sign)
        if wanted is None:
            continue
        real = _face_normal(vertices, face)
        alignment = sum(real[i] * wanted[i] for i in range(3))
        if abs(alignment) < .75:
            raise ValueError(f'Грань {fi} / {role}: нормаль не совпадает с осью Z')
        anchored[fi] = alignment < 0

    flipped = {}
    for start in range(len(faces)):
        if start in flipped:
            continue
        # Find the anchor for this component BEFORE choosing its orientation.
        component = {start}
        queue = [start]
        while queue:
            cur = queue.pop()
            for neighbor, _ in adjacent[cur]:
                if neighbor not in component:
                    component.add(neighbor)
                    queue.append(neighbor)
        roots = [fi for fi in sorted(component) if fi in anchored]
        if not roots:
            raise ValueError(f'Не найден опорный DWOS полигон для оболочки {start}')
        root = roots[0]
        flipped[root] = anchored[root]
        todo = deque([root])
        while todo:
            fi = todo.popleft()
            for neighbor, same_direction in adjacent[fi]:
                needed = flipped[fi] ^ same_direction
                if neighbor in flipped:
                    if flipped[neighbor] != needed:
                        raise ValueError(f'Противоречивая ориентация грани {neighbor}; проверьте Mesh')
                    continue
                flipped[neighbor] = needed
                todo.append(neighbor)
        for fi in roots:
            if flipped[fi] != anchored[fi]:
                raise ValueError(f'Опорные нормали противоречат топологии (грань {fi})')
    return {fi for fi, value in flipped.items() if value}
