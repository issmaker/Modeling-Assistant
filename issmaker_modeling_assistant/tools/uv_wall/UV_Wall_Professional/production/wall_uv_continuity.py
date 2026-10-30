"""Pure, Blender-independent modular U phase planning for vertical wall strips.

The user-authored XY path is authoritative.  This module does not edit a path,
construct Mesh, or assume that an image's border is texture-seamless.  A single
physical module boundary can have UV 1 on one side and 0 on the other.
"""
from math import floor, hypot, isfinite


def authored_start_clearance(distances, tile_length):
    """Measure near-module seam clearance WITHOUT rotating the authored path."""
    if len(distances) < 2 or not isfinite(tile_length) or tile_length <= 0:
        raise ValueError('WALL_UV: invalid path length or module size')
    closest = float('inf')
    for distance in distances[1:-1]:
        if not isfinite(distance):
            raise ValueError('WALL_UV: non-finite station')
        phase = distance % tile_length
        gap = min(phase, tile_length-phase)
        if gap > 1e-10:
            closest = min(closest, gap)
    return closest


def xy_distances(points):
    """Exact polyline XY chord lengths; independent of varying path Z."""
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        dx, dy = float(b.x) - float(a.x), float(b.y) - float(a.y)
        length = hypot(dx, dy)
        if not isfinite(length) or length <= 1e-9:
            raise ValueError('WALL_UV: degenerate or non-finite XY edge')
        distances.append(distances[-1] + length)
    return distances


def module_uv_plan(cuts, tile_length, *, clearance=0.002, epsilon=1e-8):
    """Plan U coordinates per UV cell, exposing any snapped near-grid deviation.

    Returns (segments, diagnostics). A segment is
    (u_start, u_end, tile_origin, snapped_delta_m).
    Missing a non-nearby tile cut is a fatal programming error: clamping the
    coordinates to [0,1] would silently stretch several metres of UV.
    """
    if not (isfinite(tile_length) and tile_length > 0 and
            isfinite(clearance) and 0 <= clearance < tile_length / 4):
        raise ValueError('WALL_UV: invalid module/clearance')
    if len(cuts) < 2 or any(not isfinite(float(v)) for v in cuts):
        raise ValueError('WALL_UV: invalid cuts')
    if any(b <= a for a, b in zip(cuts, cuts[1:])):
        raise ValueError('WALL_UV: cuts must strictly increase')
    from math import floor
    cells = []
    snapped = 0
    max_adjustment = 0.0
    for d0, d1 in zip(cuts, cuts[1:]):
        # The start of a cell close to an existing module boundary belongs to
        # the new module. Avoid U=-0.00025 at an authored corner 1 mm before it.
        nearest = round(d0 / tile_length)
        if abs(d0 - nearest * tile_length) <= clearance + epsilon:
            tile_start = nearest * tile_length
        else:
            tile_start = floor((d0 + epsilon) / tile_length) * tile_length
        u0 = (d0 - tile_start) / tile_length
        u1 = (d1 - tile_start) / tile_length
        # Only errors <= clearance are recoverable by virtual seam snapping.
        deviation = max(0.0, -u0 * tile_length, (u1 - 1.0) * tile_length)
        if deviation > clearance + epsilon:
            raise ValueError(
                'WALL_UV: module boundary omitted between '
                f'{d0:.7f} m and {d1:.7f} m; excess={deviation:.7f} m'
            )
        if deviation > epsilon:
            snapped += 1
            max_adjustment = max(max_adjustment, deviation)
        cells.append((max(0.0, u0), min(1.0, u1), tile_start, deviation))
    # Adjacent faces must have consistent repeat phase (1 -> 0 is allowed).
    # A 2 mm correction at a sliver corner is bounded by the weld clearance.
    discontinuities = []
    for i in range(len(cells)-1):
        left = cells[i][1]
        right = cells[i+1][0]
        repeat_gap = min(abs(left-right), abs(left-right-1), abs(left-right+1))
        gap_m = repeat_gap * tile_length
        if gap_m > clearance + 2*epsilon:
            discontinuities.append((i, gap_m))
    return cells, {
        'stations': len(cuts),
        'snapped_cells': snapped,
        'maximum_snapped_error_m': max_adjustment,
        'internal_phase_breaks': tuple(discontinuities),
    }


def cyclic_endpoint_seam(total_horizontal_length, tile_length):
    """A closed ring with noninteger perimeter/texture period needs one seam.

    It belongs at the authored beginning/end, never at a randomized corner.
    """
    if not (isfinite(total_horizontal_length) and isfinite(tile_length)
            and tile_length > 0 and total_horizontal_length > 0):
        raise ValueError('WALL_UV: invalid closed perimeter')
    phase = total_horizontal_length % tile_length
    return min(phase, tile_length-phase)
