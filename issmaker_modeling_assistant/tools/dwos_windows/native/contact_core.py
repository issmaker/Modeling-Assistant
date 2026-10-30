"""DWOS 2.1.6 / pure core: validate four-corner OPEN external wall contact.

A join-ready *profile* does NOT mean that Blender Join welds two objects.
It only proves the frame can match a rectangular four-vertex wall opening
without its own extra split vertices on that contact perimeter.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite


def validate_outer_contact(mesh, width, height, tolerance=1e-7):
    vertices = mesh['verts']
    faces = mesh['faces']
    roles = mesh['roles']
    if not isfinite(width) or not isfinite(height) or min(width, height) <= 0:
        raise ValueError('Некорректные размеры контура окна')
    if len(faces) != len(roles):
        raise ValueError('Несоответствие полигонов и семантических ролей')
    if any(role in {'FRAME_CONTACT', 'OUTER_CONTACT'} for role in roles):
        raise ValueError('Наружные боковые полигоны рамы должны отсутствовать')
    if any(not all(isfinite(v) for v in pos) for pos in vertices):
        raise ValueError('NaN/Inf координата в наружной раме')
    hx, hy = width / 2, height / 2
    expected = {(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)}
    z_front, z_back = mesh['z_front'], mesh['z_back']
    full = 'FRAME_INTERIOR_BACK' in roles
    incidence = Counter()
    for face in faces:
        for a, b in zip(face, face[1:] + face[:1]):
            if a == b or a not in range(len(vertices)) or b not in range(len(vertices)):
                raise ValueError('Некорректная грань рамы')
            incidence[tuple(sorted((a, b)))] += 1
    if not incidence or max(incidence.values()) > 2:
        raise ValueError('Non-manifold ребро в раме')
    used = {v for edge in incidence for v in edge}
    if used != set(range(len(vertices))):
        raise ValueError('Обнаружены неиспользуемые вершины')
    metrics = {}
    for label, z, expected_count in (
        ('front', z_front, 4), ('back', z_back, 4 if full else 0)
    ):
        candidates = {
            i for i, (x, y, zz) in enumerate(vertices)
            if i in used and abs(zz-z) <= tolerance and
            (abs(abs(x)-hx) <= tolerance or abs(abs(y)-hy) <= tolerance)
        }
        # z_front and z_back must be distinct, even for a 2D preview.
        if abs(z_front-z_back) <= tolerance:
            raise ValueError('Передняя и задняя плоскости рамы совпадают')
        if len(candidates) != expected_count:
            raise ValueError(f'{label}: наружных вершин {len(candidates)} вместо {expected_count}')
        actual = {(vertices[i][0], vertices[i][1]) for i in candidates}
        if len(actual) != len(candidates):
            raise ValueError(f'{label}: наложенные вершины наружного контура')
        if expected_count and any(not any(abs(px-x) <= tolerance and abs(py-y) <= tolerance
                                          for px, py in actual) for x, y in expected):
            raise ValueError(f'{label}: наружные вершины не совпадают с углами проёма')
        edge_count = 0
        for (a, b), incidence_count in incidence.items():
            if a not in candidates or b not in candidates:
                continue
            pa, pb = vertices[a], vertices[b]
            outer_side = (abs(abs(pa[0])-hx) <= tolerance and abs(abs(pb[0])-hx) <= tolerance
                          and pa[0] * pb[0] > 0) or (
                          abs(abs(pa[1])-hy) <= tolerance and abs(abs(pb[1])-hy) <= tolerance
                          and pa[1] * pb[1] > 0)
            if outer_side:
                if incidence_count != 1:
                    raise ValueError(f'{label}: открытое контурное ребро занято несколькими гранями')
                edge_count += 1
        if edge_count != expected_count:
            raise ValueError(f'{label}: наружных рёбер {edge_count} вместо {expected_count}')
        metrics[label + '_corners'] = len(candidates)
        metrics[label + '_edges'] = edge_count
    return metrics
