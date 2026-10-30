"""BH-03.2H.1 Blender readonly per-direction probe adapter.

Scene direction settings are temporarily changed and restored. Mesh, selection
and UV are never edited. Production safety_preflight remains sole authority.
"""
from .direction_alternatives_core import evaluate_candidates, VECTORS
from .direction_snap_blender import available_axes


def query_alternatives(context, signed_depth, *, max_probes=8, max_seconds=4.):
    from . import unified_preflight
    scene=context.scene
    eligibility=available_axes(context)  # one read of the current selection
    original=(scene.brick_hole_direction_source,
              tuple(scene.brick_hole_manual_normal))
    def geometry_gate(axis):
        return eligibility.get(axis,(False,'Ось не проверена'))
    def probe(mode,axis,depth):
        try:
            if mode=='GLOBAL':
                scene.brick_hole_manual_normal=VECTORS[axis]
            scene.brick_hole_direction_source=mode
            unified_preflight(context,depth)
        finally:
            # Even a failed production Preflight must restore the user settings.
            scene.brick_hole_direction_source=original[0]
            scene.brick_hole_manual_normal=original[1]
    try:
        return evaluate_candidates(geometry_gate,probe,signed_depth,
                                   max_probes=max_probes,max_seconds=max_seconds)
    finally:
        scene.brick_hole_direction_source=original[0]
        scene.brick_hole_manual_normal=original[1]


def probe_single_candidate(context,mode,axis,signed_depth):
    """One full synchronous readonly production preflight; always restores scene."""
    from . import unified_preflight
    from .direction_alternatives_core import VECTORS
    scene=context.scene
    original=(scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal))
    try:
        if mode not in ('AUTO','GLOBAL'):
            raise ValueError('Неизвестный режим направления')
        if mode=='GLOBAL':
            scene.brick_hole_manual_normal=VECTORS[axis]
        scene.brick_hole_direction_source=mode
        unified_preflight(context,signed_depth)
        return 'VERIFIED_CANDIDATE','Полный Preflight пройден'
    except Exception as exc:
        return 'PREFLIGHT_BLOCKED',str(exc)[:170]
    finally:
        scene.brick_hole_direction_source=original[0]
        scene.brick_hole_manual_normal=original[1]
