"""Geometric limits of an EXACT cornice miter (no path changes).

An exact sweep corner is the intersection (trim) of adjacent segment
extrusions along their angle-bisector plane. This is not bevel/rounding:
the authored vertex and both tangent directions remain unchanged.
"""
from math import isfinite, cos, radians, tan


def exact_miter_trim(turn_deg, max_profile_offset, before_xy, after_xy, weld=0.002):
    """Return (miter scale, tangent trim reach) or fail before mesh mutation.

    The bisector reaches profile_offset*tan(turn/2) along each straight
    segment. When that exceeds an adjacent segment, a local joint would
    overlap the next authored station and exact topology cannot be guaranteed.
    """
    values = (turn_deg, max_profile_offset, before_xy, after_xy, weld)
    if any(not isfinite(v) for v in values):
        raise ValueError('Карниз: некорректная числовая геометрия угла')
    if not 0 <= turn_deg < 180 or max_profile_offset < 0 or weld <= 0:
        raise ValueError('Карниз: вырожденный угол/профиль')
    if before_xy <= weld or after_xy <= weld:
        raise ValueError('Карниз: соседний участок короче допуска Weld')
    c=cos(radians(turn_deg/2))
    if c <= 1e-8:
        raise ValueError('Карниз: точный разворот 180° не имеет единой плоскости сопряжения')
    ratio=1.0/c
    reach=max_profile_offset*tan(radians(turn_deg/2))
    # Geometric safety — independent from former arbitrary 4x scale clamp.
    safe_before=max(0., before_xy - 2.1*weld)
    safe_after=max(0., after_xy - 2.1*weld)
    if reach >= min(safe_before,safe_after):
        raise ValueError(
            'Карниз: пересечение среза острого угла захватывает соседний участок. '
            f'Нужно {reach:.4f} м, доступно {min(safe_before,safe_after):.4f} м. '
            'Путь остаётся неизменным; Result не заменён.'
        )
    return ratio,reach
