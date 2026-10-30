"""Readable precision feedback for existing geometry-anchored Blender controls.

Pure presentation only. The formulas mirror controller._apply_drag and
precision_markers.numeric_spin; they are NOT a second snapping implementation.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PrecisionCue:
    heading: str
    description: str
    role: str  # neutral / precise / snapped
    mode: str


# Only these drag kinds actually QUANTIZE to a positional grid.
_SNAPS = {
    'PATH_Z': ('Z точки', '5 см', '5 мм'),
    'SURFACE_Z': ('Z поверхности', '5 см', '5 мм'),
    'OFFSET': ('Смещение', '5 см', '5 мм'),
    'HEIGHT': ('Высота', '5 см', None),
    'BOTTOM_LEVEL': ('Уровень низа', '5 см', None),
    'THICKNESS': ('Толщина', '1 см', None),
}
_SPINS = {
    'HEIGHT': ('Высота', '5 мм/пикс.', '0,5 мм/пикс.', '5 см/пикс.'),
    'SURFACE_OFFSET': ('Z поверхности', '5 мм/пикс.', '0,5 мм/пикс.', '5 см/пикс.'),
    'SURFACE_ROT': ('Поворот поверхности', '0,25°/пикс.', '0,025°/пикс.', '2,5°/пикс.'),
    'CORNICE_ROT': ('Поворот карниза', '0,25°/пикс.', '0,025°/пикс.', '2,5°/пикс.'),
}


def precision_cue(kind, *, shift=False, ctrl=False, spin=False):
    """Accurate RU cue, including SHIFT priority for simultaneous modifiers.

    No 'snap' claim for numeric_spin: its modifiers change *sensitivity*.
    """
    kind=str(kind)
    if spin:
        info=_SPINS.get(kind)
        if info is None:return None
        label,normal,fine,coarse=info
        # numeric_spin multiplies .1 and 10 separately, so both cancel out.
        if shift and ctrl:return PrecisionCue('ПЛАВНО · '+normal,label+' · Shift+Ctrl: обычная чувствительность','neutral','BOTH')
        if shift: return PrecisionCue('ТОЧНО · '+fine,label+' · SHIFT · чувствительность','precise','SHIFT')
        if ctrl: return PrecisionCue('БЫСТРО · '+coarse,label+' · CTRL · чувствительность','precise','CTRL')
        return PrecisionCue('ПЛАВНО · '+normal,label+' · Shift точнее / Ctrl быстрее','neutral','FREE')
    info=_SNAPS.get(kind)
    if info is None:return None
    label,shift_step,ctrl_step=info
    if shift:return PrecisionCue('ШАГ '+shift_step+' · SHIFT',label+' · привязка к сетке','snapped','SHIFT')
    if ctrl and ctrl_step:return PrecisionCue('ШАГ '+ctrl_step+' · CTRL',label+' · привязка к сетке','precise','CTRL')
    options=('Shift '+shift_step+' / Ctrl '+ctrl_step) if ctrl_step else ('Shift '+shift_step)
    return PrecisionCue('СВОБОДНОЕ ПЕРЕМЕЩЕНИЕ',label+' · '+options,'neutral','FREE')


def place_cue(anchor, size, viewport, obstacles=(), *, margin=8.0, offset=20.0):
    """Noninteractive HUD placement, fail-closed on narrow/covered Viewports.

    Returns rect x0,y0,x1,y1. The card creates no hitbox; previous cards
    and existing geometry markers always have priority over this hint.
    """
    try:
        x,y=map(float,anchor);w,h=map(float,size);W,H=map(float,viewport)
        gap=float(offset);margin=float(margin)
        if not all(isfinite(v) for v in (x,y,w,h,W,H,gap,margin)) or min(w,h,W,H)<=0 or min(gap,margin)<0:
            return None
        if w>W-2*margin or h>H-2*margin:return None
        rects=[]
        for rect in obstacles:
            if rect is None:continue
            a,b,c,d=map(float,rect)
            if not all(isfinite(v) for v in (a,b,c,d)):return None
            if c>a and d>b:rects.append((a,b,c,d))
        # Keep geometry feedback physically near the pointer; no screen-corner jump.
        candidates=((x+gap,y+gap),(x-w-gap,y+gap),
                    (x+gap,y-h-gap),(x-w-gap,y-h-gap))
        for cx,cy in candidates:
            rect=(cx,cy,cx+w,cy+h)
            if cx<margin or cy<margin or cx+w>W-margin or cy+h>H-margin:continue
            if any(cx<r[2] and cx+w>r[0] and cy<r[3] and cy+h>r[1] for r in rects):continue
            return rect
        return None
    except (TypeError,ValueError,OverflowError):
        return None
