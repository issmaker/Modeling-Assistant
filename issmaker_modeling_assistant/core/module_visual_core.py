"""Pure-Python UV discontinuity and smoothing policy for Pass 15.16.

Geometry and UVs are NEVER modified here. A UV discontinuity is *not* a
certification of a physical 2m/4m boundary; classify this distinction explicitly.
"""
from math import acos, isfinite, sqrt


def _uv_pair(value):
    x, y = float(value[0]), float(value[1])
    if not isfinite(x) or not isfinite(y):
        raise ValueError('Non-finite UV coordinate')
    return x, y


def classify_edges(faces, uv_tolerance=2e-6):
    """Return edge-id groups from faces with (edge_id, v0, v1, uv0, uv1).

    UV endpoints are keyed by vertex ID rather than face winding, which avoids
    falsely reporting seams on reversed triangles. No Blender imports.
    """
    tolerance = float(uv_tolerance)
    if not isfinite(tolerance) or not 0 <= tolerance <= .01:
        raise ValueError('Invalid UV tolerance')
    per_edge = {}
    for face in faces:
        material = int(face.get('material', 0))
        for edge_id, v0, v1, uv0, uv1 in face['edges']:
            idx, a, b = int(edge_id), int(v0), int(v1)
            if idx < 0 or a == b:
                raise ValueError('Degenerate edge ID or vertices')
            first, second = _uv_pair(uv0), _uv_pair(uv1)
            if a > b:
                first, second = second, first
            per_edge.setdefault(idx, []).append((min(a, b), max(a, b), first, second, material))
    seam, boundary, nonmanifold, material = set(), set(), set(), set()
    for idx, values in per_edge.items():
        if len(values) == 1:
            boundary.add(idx)
            continue
        if len(values) > 2:
            nonmanifold.add(idx)
            continue
        a, b = values
        if a[:2] != b[:2]:
            raise ValueError('Inconsistent vertex IDs for geometric edge')
        if a[4] != b[4]:
            material.add(idx)
        if any(abs(x-y) > tolerance for pair_a, pair_b in zip(a[2:4], b[2:4]) for x,y in zip(pair_a,pair_b)):
            seam.add(idx)
    return {'uv_seams': tuple(sorted(seam)),
            'open_edges': tuple(sorted(boundary)),
            'nonmanifold': tuple(sorted(nonmanifold)),
            'material_edges': tuple(sorted(material)),
            'edges_seen': len(per_edge)}


def hard_edge(normal_a, normal_b, degrees=35.0):
    """True when adjacent polygon normals exceed a user-selected angle."""
    angle = float(degrees)
    if not isfinite(angle) or not 1 <= angle <= 179:
        raise ValueError('Angle must be between 1 and 179 degrees')
    a = tuple(float(v) for v in normal_a)
    b = tuple(float(v) for v in normal_b)
    if len(a) != 3 or len(b) != 3 or not all(isfinite(v) for v in a+b):
        raise ValueError('Invalid face normal')
    sa = sqrt(sum(v*v for v in a)); sb = sqrt(sum(v*v for v in b))
    if sa < 1e-12 or sb < 1e-12:
        return True  # Degenerate faces must not smooth across a questionable edge.
    c = max(-1.0, min(1.0, sum(x*y for x,y in zip(a,b))/(sa*sb)))
    from math import cos, radians
    return c < cos(radians(angle))



def classify_indexed_mesh_edges(polygons, loops, uv_data, edge_count, uv_tolerance=2e-6):
    """Bounded-memory UV seam audit using polygon loop indices, not per-face copies.

    A large mesh may have millions of loops. Keeping two int32 loop references
    per edge plus its material avoids a giant Python list of duplicated UV data.
    Both sides of a shared 3D edge are aligned by vertex identity. Three or
    more incident faces are classified nonmanifold instead of regular seams.
    Read-only: never modifies polygon, loop, UV or mesh datablocks.
    """
    from array import array
    edge_count = int(edge_count)
    tolerance = float(uv_tolerance)
    if edge_count < 0 or not isfinite(tolerance) or not 0.0 <= tolerance <= .01:
        raise ValueError('Invalid UV edge count or tolerance')
    first = array('i', [-1]) * edge_count
    first_next = array('i', [-1]) * edge_count
    first_material = array('i', [0]) * edge_count
    incidence = bytearray(edge_count)
    uv_seams, material_edges = set(), set()
    for poly in polygons:
        start = int(poly.loop_start)
        total = int(poly.loop_total)
        if total < 3:
            raise ValueError('Degenerate polygon loop count')
        material = int(poly.material_index)
        end = start + total
        for i in range(start, end):
            nxt = start if i + 1 == end else i + 1
            a = loops[i]
            b = loops[nxt]
            edge_idx = int(a.edge_index)
            if not 0 <= edge_idx < edge_count or a.vertex_index == b.vertex_index:
                raise ValueError('Invalid or degenerate geometric edge')
            state = incidence[edge_idx]
            if state == 0:
                first[edge_idx], first_next[edge_idx] = i, nxt
                first_material[edge_idx] = material
                incidence[edge_idx] = 1
            elif state == 1:
                prev_i, prev_nxt = first[edge_idx], first_next[edge_idx]
                prev_a, prev_b = loops[prev_i], loops[prev_nxt]
                va, vb = int(a.vertex_index), int(b.vertex_index)
                pa, pb = int(prev_a.vertex_index), int(prev_b.vertex_index)
                if (va, vb) == (pa, pb):
                    idx_a, idx_b = i, nxt
                elif (va, vb) == (pb, pa):
                    idx_a, idx_b = nxt, i
                else:
                    raise ValueError('Inconsistent vertices on the same geometric edge')
                ua0, ub0 = _uv_pair(uv_data[prev_i].uv), _uv_pair(uv_data[prev_nxt].uv)
                ua1, ub1 = _uv_pair(uv_data[idx_a].uv), _uv_pair(uv_data[idx_b].uv)
                if any(abs(left-right) > tolerance for left,right in zip(ua0+ub0,ua1+ub1)):
                    uv_seams.add(edge_idx)
                if material != first_material[edge_idx]:
                    material_edges.add(edge_idx)
                incidence[edge_idx] = 2
            elif state == 2:
                # A nonmanifold edge must not ALSO be announced as a normal seam.
                uv_seams.discard(edge_idx)
                material_edges.discard(edge_idx)
                incidence[edge_idx] = 3
    return {
        'uv_seams': tuple(sorted(uv_seams)),
        'open_edges': tuple(i for i,n in enumerate(incidence) if n == 1),
        'nonmanifold': tuple(i for i,n in enumerate(incidence) if n == 3),
        'material_edges': tuple(sorted(material_edges)),
        'edges_seen': sum(1 for n in incidence if n),
    }
