"""DWOS 3.4 Host-Driven Interior target resolver.

The frame contact contour and the interior target contour are separate concepts.
A physical Corner Post belongs to the frame junction and must not split the
one-metre Interior BOX for its full depth.

Pure module: no bpy/mathutils dependency.
"""
from __future__ import annotations
import math

EPS=1e-8


def resolve_interior_target(topology, payload, requested_depth, direction):
    if direction not in (-1,1):raise ValueError('Interior direction must be +/-1')
    depth=float(requested_depth)
    if not math.isfinite(depth) or depth<=EPS:raise ValueError('Interior depth must be positive')
    payload=payload if isinstance(payload,dict) else {}
    wall_mode=str(payload.get('wall_mode','SURFACE'))
    wall_depth=float(payload.get('wall_depth') or 0.0)
    classification=str((topology or {}).get('classification','PLANAR'))
    # Existing legacy SOLID detector proves front/back pairing through reveal
    # connectivity. When that proof exists, the real wall inner plane is a valid
    # architectural target for the reveal portion.
    if wall_mode=='SOLID' and wall_depth>EPS and payload.get('back_loop'):
        return {
            'mode':'HOST_DRIVEN',
            'classification':classification,
            'wall_depth':wall_depth,
            'requested_depth':depth,
            'target_depth':direction*wall_depth,
            'box_depth':direction*depth,
            'confidence':'HIGH',
            'reason':'PROVEN_FRONT_BACK_REVEAL_PAIR',
        }
    # Adaptive CURVED/CORNER discovery currently works on one-sided shells. Be
    # explicit: there is no hidden inner wall to infer. Use patch-local normals
    # and a true offset-plane miter, not a blind global extrusion.
    return {
        'mode':'PARAMETRIC_BOX',
        'classification':classification,
        'wall_depth':0.0,
        'requested_depth':depth,
        'target_depth':direction*depth,
        'box_depth':direction*depth,
        'confidence':'EXPLICIT_FALLBACK',
        'reason':'ONE_SIDED_HOST_NO_PROVEN_INTERIOR_SURFACE',
    }


def corner_post_affects_interior_target(solution):
    """Corner Post never partitions the far InteriorTargetBoundary.

    It is a frame-junction solid. Front reveal clipping may be added separately,
    but the room-side target contour remains continuous through the true inner
    corner.
    """
    return False
