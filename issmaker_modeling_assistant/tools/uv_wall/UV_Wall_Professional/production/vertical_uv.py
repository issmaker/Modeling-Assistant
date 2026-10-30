"""Stable floor cuts and texture-grid levels without Blender dependencies."""
from math import floor, ceil, isfinite


def vertical_levels(path_anchor_z, uv_anchor_z, height, story_heights,
                    z_min, z_max, stories_down, max_levels=20000):
    """Story markers and continuous UV grid share one strictly ordered Z list.

    A non-multiple first-story height adds a *geometry* seam, not a UV reset.
    World UV origin allows independent walls from different floors to align.
    """
    values = (path_anchor_z, uv_anchor_z, height, z_min, z_max, *story_heights)
    if any(not isfinite(float(v)) for v in values) or height <= 0 or z_max < z_min:
        raise ValueError('Wall UV: invalid finite height/range')
    cursor = float(path_anchor_z)
    levels = [cursor]
    direction = -1.0 if stories_down else 1.0
    for story_height in story_heights:
        if story_height <= 0:
            raise ValueError('Wall UV: non-positive story height')
        cursor += direction * float(story_height)
        levels.append(cursor)
    first = floor((z_min - uv_anchor_z) / height)
    last = ceil((z_max - uv_anchor_z) / height)
    if last-first > max_levels or len(levels)+last-first+1 > max_levels:
        raise ValueError('Wall UV: vertical grid exceeds safe cell limit')
    levels.extend(uv_anchor_z + k*height for k in range(first,last+1))
    levels.sort()
    merged = []
    epsilon = min(1.0e-7, height * 1.0e-8)
    for z in levels:
        if not merged or z-merged[-1] > epsilon:
            merged.append(z)
    return merged


def uv_v_factor(z, z_mid, uv_anchor_z, height):
    """Unwrapped V in [0,1] for the slab containing z_mid.

    Adjacent story bands inside one UV tile use identical factors at the seam.
    Only true module boundaries may transition from 1 to 0.
    """
    tile = floor((z_mid - uv_anchor_z) / height)
    raw = (z - uv_anchor_z) / height - tile
    return max(0.0, min(1.0, raw))
