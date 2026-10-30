"""PPA Wave 9: Blender-independent classification of issue witness staleness.

Geometry edit notifications are only invalidators; never recompute proofs here.
"""
from __future__ import annotations

_GEOMETRY_TYPES = frozenset({'MESH', 'CURVE', 'CURVES', 'SURFACE', 'FONT', 'LATTICE'})


def invalidates(id_type, geometry=False, transform=False):
    """Conservatively invalidate world-space witnesses for geometry changes.

    `id_type` is a Blender RNA identifier for a datablock (Mesh/Curve/Object)
    or `Object:<type>` for objects with a known Blender object type.
    """
    label = str(id_type or '').upper()
    if label in _GEOMETRY_TYPES:
        return bool(geometry)  # Material-only updates cannot invalidate positions.
    if label.startswith('OBJECT:'):
        kind = label.split(':', 1)[1]
        return kind in _GEOMETRY_TYPES and bool(geometry or transform)
    # Unknown object type with geometry update may still affect evaluated mesh.
    return label == 'OBJECT' and bool(geometry or transform)


def next_stale(was_stale, has_results, changed, reason):
    """Staleness is monotonic until the next explicit refresh."""
    if was_stale:
        return True, str(reason or 'Снимок требует обновления')
    return (True, str(reason or 'Геометрия изменилась')) if has_results and changed else (False, '')
