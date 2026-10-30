"""DWOS RC24: angle-based availability of the *central* CORNER post.

This module has no Blender dependencies. A recorded corner turn is the angle
between successive facade tangents, *not* the interior angle used in the five
DWOS fixtures. The effective interior sector is 180 - turn.

Legacy ON windows are not mutated merely by opening a project. A new rebuild
of an unsupported explicit ON state is rejected with an actionable reason.
AUTO is resolved without modifying the saved property, allowing reversible OFF.
"""
from __future__ import annotations
import math

MIN_INTERIOR_ANGLE_DEG=65.0 # W58.23: window ALWAYS allowed; only optional corner post limited
# Geometry extracted from float32 Blender vertices can drift slightly around 70°.
# This tolerance (0.0005°) admits known A04 without changing 55°/70° policy.
EPS_DEG=2e-5
WARNING_CODE='CORNER_POST_ACUTE_ANGLE_UNSUPPORTED'


def evaluate_corner_post(topology, requested_mode='AUTO'):
    mode=str(requested_mode).upper()
    if mode not in {'AUTO','ON','OFF'}:
        raise ValueError('Неизвестный режим центрального импоста')
    cls=str((topology or {}).get('classification','')).upper()
    result=dict(classification=cls,requested_mode=mode,effective_mode=mode,
                available=True,angle_deg=None,threshold_deg=MIN_INTERIOR_ANGLE_DEG,
                warning_code='',message='')
    if cls!='CORNER':
        return result
    junctions=(topology or {}).get('corner_junctions') or ()
    if len(junctions)!=1:
        # Never infer safety for an ambiguous multi-junction opening.
        angle=None
    else:
        try:
            turn=float(junctions[0]['angle_deg'])
            angle=180.0-turn if math.isfinite(turn) and 0.0 < turn < 180.0 else None
        except (KeyError, TypeError, ValueError, OverflowError):
            angle=None
    result['angle_deg']=angle
    available=angle is not None and angle + EPS_DEG >= MIN_INTERIOR_ANGLE_DEG
    result['available']=available
    if not available:
        result['warning_code']=WARNING_CODE if angle is not None else 'CORNER_POST_ANGLE_UNAVAILABLE'
        if angle is None:
            result['message']='! Угол CORNER не определён. Центральный импост недоступен до повторного анализа проёма.'
        else:
            result['message']=(f'Окно построено. Центральный импост недоступен при угле {angle:.1f}°. '
                               f'Минимум {MIN_INTERIOR_ANGLE_DEG:.0f}°. Можно оставить окно без центральной стойки. Другие функции окна доступны.')
        # Explicit ON on an unsupported angle is a request for an optional
        # feature, not permission to deny or destroy the entire window.
        result['effective_mode']='OFF'
    elif mode=='AUTO':
        result['effective_mode']='ON'
    return result


def require_post_allowed(topology, requested_mode):
    """W58.23: never block the WINDOW for an optional acute corner post.

    Caller may display warning_code and message in HUD. An unresolved
    geometry failure remains the responsibility of the main window solver.
    """
    return evaluate_corner_post(topology,requested_mode)
