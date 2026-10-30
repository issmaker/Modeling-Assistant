from __future__ import annotations
import math
from ....core.reliability import (
    PROVEN_INVALID, UNCERTAIN, Decision, proven_invalid, uncertain, valid,
    policy_from_scale,
)


def _scale(width,height,frame_width,frame_depth,splits):
    values=[abs(float(width)),abs(float(height)),abs(float(frame_width)),abs(float(frame_depth))]
    for s in splits:
        try:values.extend((abs(float(s.position)),abs(float(s.thickness))))
        except (TypeError,ValueError,OverflowError):pass
    return max(values or [1.0])


def validate_window_decisions(width,height,frame_width,frame_depth,splits):
    """Return shared reliability decisions; only proven invalid data hard-blocks."""
    raw=(width,height,frame_width,frame_depth)
    if not all(math.isfinite(float(x)) for x in raw):
        return [proven_invalid('DWOS_NONFINITE','Геометрия содержит NaN/Infinity',
            tool='DWOS',proof='Non-finite scalar cannot define a finite window mesh')]
    width,height,frame_width,frame_depth=map(float,raw)
    policy=policy_from_scale(_scale(width,height,frame_width,frame_depth,splits),rel_linear=1e-10,min_linear=1e-10)
    out=[]
    if width<=0 or height<=0:
        out.append(proven_invalid('DWOS_OPENING_SIZE','Размер проёма должен быть положительным',
            tool='DWOS',proof=f'width={width}, height={height}'))
    if frame_width<=0:
        out.append(proven_invalid('DWOS_FRAME_WIDTH','Толщина основной рамы должна быть больше нуля',
            tool='DWOS',proof=f'frame_width={frame_width}'))
    elif frame_width*2>=min(width,height):
        penetration=frame_width*2-min(width,height)
        if penetration>policy.linear:
            out.append(proven_invalid('DWOS_FRAME_ENVELOPE','Толщина основной рамы перекрывает проём',
                tool='DWOS',proof=f'2*frame_width-min_size={penetration}',witness=policy.witness()))
        else:
            out.append(uncertain('DWOS_FRAME_NEAR_CONTACT','Рама находится на границе численной точности проёма',
                tool='DWOS',witness={'clearance_m':-penetration,**policy.witness()}))
    if frame_depth<0.005:
        out.append(proven_invalid('DWOS_FRAME_DEPTH','Глубина рамы должна быть не меньше 0,005 м',
            tool='DWOS',proof=f'frame_depth={frame_depth}<0.005'))
    hx=width*.5-frame_width;hy=height*.5-frame_width
    normalized=[]
    for i,s in enumerate(splits):
        try:pos=float(s.position);th=float(s.thickness);axis=str(s.axis)
        except (TypeError,ValueError,OverflowError,AttributeError):
            out.append(proven_invalid('DWOS_SPLIT_VALUE',f'Split {i+1}: некорректные значения',
                tool='DWOS',proof='Split fields are not finite numeric data'))
            continue
        if axis not in {'V','H'} or not math.isfinite(pos) or not math.isfinite(th):
            out.append(proven_invalid('DWOS_SPLIT_VALUE',f'Split {i+1}: некорректные значения',
                tool='DWOS',proof=f'axis={axis}, position={pos}, thickness={th}'))
            continue
        if th<=0:
            out.append(proven_invalid('DWOS_SPLIT_THICKNESS',f'Split {i+1}: толщина должна быть больше нуля',
                tool='DWOS',proof=f'thickness={th}'))
            continue
        bound=hx if axis=='V' else hy
        clearance=bound-(abs(pos)+th*.5)
        if clearance < -policy.linear:
            out.append(proven_invalid('DWOS_SPLIT_FRAME_OVERLAP',f'Split {i+1}: профиль доказанно входит в основную раму',
                tool='DWOS',proof=f'clearance={clearance} m',witness=policy.witness()))
        elif clearance <= policy.linear:
            out.append(uncertain('DWOS_SPLIT_FRAME_NEAR_CONTACT',f'Split {i+1}: профиль почти касается границы рамы',
                tool='DWOS',witness={'clearance_m':clearance,**policy.witness()}))
        normalized.append((i,axis,pos,th))
    for idx,a in enumerate(normalized):
        i,axis,pos,th=a
        for j,axis_b,pos_b,th_b in normalized[:idx]:
            if axis!=axis_b:continue
            overlap=(th+th_b)*.5-abs(pos-pos_b)
            if overlap>policy.linear:
                out.append(proven_invalid('DWOS_SPLIT_OVERLAP',f'Split {i+1} и {j+1}: внутренние рамы перекрываются',
                    tool='DWOS',proof=f'interval_overlap={overlap} m',witness=policy.witness()))
            elif overlap>=-policy.linear:
                out.append(uncertain('DWOS_SPLIT_NEAR_CONTACT',f'Split {i+1} и {j+1}: зазор на границе численной точности',
                    tool='DWOS',witness={'signed_overlap_m':overlap,**policy.witness()}))
    return out or [valid('DWOS_WINDOW_OK','Window scalar geometry is valid',tool='DWOS')]


def validate_window(width,height,frame_width,frame_depth,splits):
    """Legacy API: only proven-invalid decisions remain blocking errors."""
    return [d.message for d in validate_window_decisions(width,height,frame_width,frame_depth,splits)
            if d.kind==PROVEN_INVALID]


def validation_advisories(width,height,frame_width,frame_depth,splits):
    return [d for d in validate_window_decisions(width,height,frame_width,frame_depth,splits)
            if d.kind==UNCERTAIN]
