"""Pure local-frame orientation checks for vertical architectural openings.

DWOS X/Y are a right-handed pair in the opening plane. Reversing both axes
changes the physical placement of asymmetric horizontal Split layouts.
"""
from __future__ import annotations
import math


def _as3(value):
    out = tuple(float(v) for v in value)
    if len(out) != 3 or not all(math.isfinite(v) for v in out):
        raise ValueError('Non-finite opening axis')
    return out


def stabilize_up(x_axis, y_axis, up=(0, 0, 1), *, threshold=0.15):
    """Flip BOTH in-plane vectors only if Y is demonstrably downward.

    Preserve rotation/alignment for roof lights or strongly tilted planes where
    projected world Z provides no meaningful orientation cue.
    """
    x, y, u = _as3(x_axis), _as3(y_axis), _as3(up)
    y_len=math.sqrt(sum(v*v for v in y))
    u_len=math.sqrt(sum(v*v for v in u))
    if y_len < 1e-10 or u_len < 1e-10:
        raise ValueError('Zero-length opening axis')
    vertical_cos=sum(a*b for a,b in zip(y,u))/(y_len*u_len)
    if vertical_cos < -threshold:
        return tuple(-v for v in x), tuple(-v for v in y), True
    return x, y, False
