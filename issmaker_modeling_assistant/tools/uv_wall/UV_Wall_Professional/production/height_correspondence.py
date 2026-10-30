"""Paired wall stationing. Blender-independent and source-Path immutable.

The inner offset changes horizontal segment lengths, not the authored height
function z(segment, t). Keep equal t across both shells at ALL modular cuts.
"""
from bisect import bisect_right
from math import isfinite


def _source_parameters(distances, cuts):
    if len(distances) < 2:
        raise ValueError('Double wall: empty station intervals')
    if any(not isfinite(d) for d in distances) or any(
        end <= start for start, end in zip(distances, distances[1:])
    ):
        raise ValueError('Double wall: degenerate horizontal segment')
    groups = [set((0.0, 1.0)) for _ in range(len(distances) - 1)]
    for cut in cuts:
        if not isfinite(cut):
            raise ValueError('Double wall: non-finite module cut')
        if not distances[0] - 1e-7 <= cut <= distances[-1] + 1e-7:
            raise ValueError('Double wall: module cut outside path')
        index = max(0, min(len(groups)-1, bisect_right(distances, cut)-1))
        start, end = distances[index], distances[index+1]
        t = max(0.0, min(1.0, (cut-start)/(end-start)))
        groups[index].add(t)
    return groups


def synchronize_wall_station_cuts(outer_points, inner_points, outer_distances,
                                  inner_distances, outer_cuts, inner_cuts,
                                  weld_clearance=0.002, allow_independent_z=False):
    """Return both station cut lists with exactly matching segment fractions.

    Source corners and per-vertex Z are *never* moved.  Virtual module cuts
    near each other are consolidated only inside the weld safety clearance.
    This prevents spike-like unmatched inner wall subdivisions at large Z.
    """
    if (len(outer_points) != len(inner_points) or
        len(outer_distances) != len(inner_distances) or
        len(outer_distances) != len(outer_points)):
        raise ValueError('Double wall: inner/outer segment correspondence lost')
    if weld_clearance <= 0 or not isfinite(weld_clearance):
        raise ValueError('Double wall: invalid weld clearance')
    for index, (outer, inner) in enumerate(zip(outer_points, inner_points)):
        if not all(isfinite(v) for v in (outer.x, outer.y, outer.z, inner.x, inner.y, inner.z)):
            raise ValueError('Double wall: non-finite position/Z')
        if not allow_independent_z and abs(outer.z-inner.z) > 1e-7:
            raise ValueError('Double wall: inner vertex height mismatches original Z at '+str(index))
    outer_groups = _source_parameters(outer_distances, outer_cuts)
    inner_groups = _source_parameters(inner_distances, inner_cuts)
    outer_result = [outer_distances[0]]
    inner_result = [inner_distances[0]]
    for index, (og, ig) in enumerate(zip(outer_groups, inner_groups)):
        outer_length = outer_distances[index+1]-outer_distances[index]
        inner_length = inner_distances[index+1]-inner_distances[index]
        tolerance_t = weld_clearance*1.06 / min(outer_length, inner_length)
        params = sorted(og | ig)
        consolidated = [0.0]
        for raw_t in params[1:-1]:
            if raw_t <= tolerance_t or 1.0-raw_t <= tolerance_t:
                continue
            if raw_t - consolidated[-1] <= tolerance_t:
                continue
            consolidated.append(raw_t)
        if 1.0-consolidated[-1] <= tolerance_t and len(consolidated) > 1:
            consolidated.pop()
        consolidated.append(1.0)
        # Both walls sample exactly the same z(t), including high authored Z.
        for t in consolidated[1:]:
            outer_result.append(outer_distances[index]+t*outer_length)
            inner_result.append(inner_distances[index]+t*inner_length)
    if len(outer_result) != len(inner_result):
        raise ValueError('Double wall: paired station count mismatch')
    return outer_result, inner_result
