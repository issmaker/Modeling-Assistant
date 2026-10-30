"""DWOS-only cleanup rules. Do not treat UV, materials or user data as trash."""
from __future__ import annotations

DWOS_PREFIX = 'DWOS_'


def owned_names(names):
    """Return ONLY explicit DWOS-owned custom data names (preserving input order)."""
    return [n for n in names if isinstance(n, str) and n.startswith(DWOS_PREFIX)]


def oriented_up(vector, up=(0,0,1), threshold=0.15):
    from .orientation_core import stabilize_up
    dummy=(1,0,0)
    _, _, flipped=stabilize_up(dummy,vector,up,threshold=threshold)
    return not flipped


def preview_counts(objects):
    """For preflight dialog/statistics; any Blender-like object or mock works."""
    data = {'objects': 0, 'dwos_object_properties': 0, 'dwos_mesh_attributes': 0,
            'dwos_mesh_properties': 0}
    for obj in objects:
        data['objects'] += 1
        data['dwos_object_properties'] += len(owned_names(obj.keys()))
        me = getattr(obj, 'data', None)
        if me is not None:
            data['dwos_mesh_attributes'] += len(owned_names(a.name for a in me.attributes))
            data['dwos_mesh_properties'] += len(owned_names(me.keys()))
    return data
