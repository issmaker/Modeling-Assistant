"""Pure UI numeric spinner layout/stepping, no Blender/GPU dependencies.

Drawing and hit registration receive *the same* returned rectangles. In narrow
panels controls are omitted, never synthesized invisibly or pushed over a peer.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from math import isfinite


def adjacent_step_buttons(field, panel, *, side=18., gap=2., offset=3., padding=10.):
    """Return {minus,plus} in logical xywh or empty dict (fail-closed)."""
    try:
        fx, fy, fw, fh = map(float, field)
        px, py, pw, ph = map(float, panel)
        side=float(side); gap=float(gap); offset=float(offset); padding=float(padding)
        if not all(isfinite(v) for v in (fx,fy,fw,fh,px,py,pw,ph,side,gap,offset,padding)):
            return {}
        if min(fw,fh,pw,ph,side) <= 0 or min(gap,offset,padding) < 0 or side > fh:
            return {}
        # Exact original field bounds must remain inside the panel, never overlap.
        if fx < px+padding or fy < py+padding or fx+fw > px+pw-padding or fy+fh > py+ph-padding:
            return {}
        x1=fx+fw+offset; x2=x1+side+gap
        if x2+side > px+pw-padding+1e-8:
            return {}
        y=fy+(fh-side)*0.5
        return {'minus':(x1,y,side,side),'plus':(x2,y,side,side)}
    except (ValueError,TypeError,OverflowError):
        return {}


def step_metre_text(text, direction, *, increment='0.001', signed=False,
                    minimum=None, maximum=1000000):
    """Exact decimal step; reject invalid changes before touching any Blender data."""
    if direction not in (-1, 1) or isinstance(direction,bool):
        raise ValueError('Направление шага должно быть -1 или +1')
    try:
        value=Decimal(str(text).strip().replace(' ','').replace(',','.'))
        delta=Decimal(str(increment))
        high=Decimal(str(maximum))
        low=Decimal(str(minimum)) if minimum is not None else (None if signed else Decimal('0'))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise ValueError('Введите корректное числовое значение') from exc
    if not value.is_finite() or not delta.is_finite() or not high.is_finite() or delta<=0 or high<=0:
        raise ValueError('Недопустимое число или шаг')
    result=value+(delta*direction)
    if not result.is_finite() or abs(result)>high or (low is not None and result<low):
        raise ValueError('Шаг выходит за допустимый диапазон')
    # The number of decimals in the chosen step is intentional.
    output=format(result,'f').rstrip('0').rstrip('.') if '.' in format(result,'f') else format(result,'f')
    return output if output not in ('','-0') else '0'


def depth_field_rects(left,bottom):
    """Canonical unscaled Brick Hole field, with non-overlapping +/- buttons."""
    l=float(left); b=float(bottom)
    return {'value_hit': (l+7.,b+38.,l+209.,b+65.),
            'depth_minus': (l+214.,b+38.,l+239.,b+65.),
            'depth_plus': (l+244.,b+38.,l+269.,b+65.)}
