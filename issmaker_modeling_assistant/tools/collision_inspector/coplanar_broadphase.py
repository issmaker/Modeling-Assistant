"""Non-mutating face AABB broad-phase for coplanar shell checks.

The Blender BVH ``overlap`` API can omit coincident/coplanar pairs; use this
as an independent sweep broad-phase before testing actual coplanar area.
Only boxes are proposed here; the exact witness must still verify the area.
"""

import math


def face_aabb_pairs(vertices_a, faces_a, vertices_b, faces_b, epsilon=1e-9):
    """Yield unique (face_a, face_b) pairs whose 3D face bounds overlap.

    Complexity is O(n log n + m log m + active candidates) for sorted X sweeps;
    worst-case dense overlapping geometry is necessarily quadratic in pairs.
    NaN / Inf coordinates are excluded (reported separately by degeneracy).
    """
    if not math.isfinite(epsilon) or epsilon < 0.0:
        raise ValueError('Invalid AABB epsilon')

    def boxes(vertices, faces):
        result = []
        for index, face in enumerate(faces):
            if len(face) < 3:
                continue
            try:
                points = [tuple(float(v) for v in vertices[i]) for i in face]
            except (IndexError, TypeError, ValueError):
                continue
            if any(len(v) != 3 or any(not math.isfinite(c) for c in v) for v in points):
                continue
            mins = tuple(min(v[axis] for v in points) for axis in range(3))
            maxs = tuple(max(v[axis] for v in points) for axis in range(3))
            result.append((mins[0], index, mins, maxs))
        return sorted(result)

    left = boxes(vertices_a, faces_a)
    right = boxes(vertices_b, faces_b)
    active = []
    j = 0
    for amin_x, ai, amin, amax in left:
        active = [item for item in active if item[3][0] + epsilon >= amin_x]
        while j < len(right) and right[j][0] <= amax[0] + epsilon:
            active.append(right[j])
            j += 1
        for bmin_x, bi, bmin, bmax in active:
            if (bmin_x > amax[0] + epsilon or
                any(amin[axis] > bmax[axis] + epsilon or
                    amax[axis] < bmin[axis] - epsilon for axis in range(3))):
                continue
            yield ai, bi
