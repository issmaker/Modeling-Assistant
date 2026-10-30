"""Pure, deterministic precise H/V drag snapping. World metres, no bpy."""
from __future__ import annotations
import math


def snap_position(position,axis,rect,other_positions=(),*,tolerance=None):
    if axis not in {'H','V'}:raise ValueError('Invalid mullion axis')
    lo,hi=(rect[0],rect[1]) if axis=='V' else (rect[2],rect[3])
    if not all(math.isfinite(float(x)) for x in (position,lo,hi)) or hi<=lo:
        raise ValueError('Invalid mullion bounds')
    span=hi-lo
    tol=float(tolerance if tolerance is not None else max(0.002,min(0.02,span/180)))
    choices=[]
    for fraction,name in ((.5,'ЦЕНТР'),(.25,'1/4'),(.75,'3/4'),(1/3,'1/3'),(2/3,'2/3')):
        choices.append((lo+span*fraction,name))
    for other in other_positions:
        if math.isfinite(float(other)) and lo<float(other)<hi:
            choices.append((float(other),'ИМПОСТ'))
    # tie-breaking fully deterministic, prefer centre over other choices
    choices.sort(key=lambda item:(abs(item[0]-position),item[1]!='ЦЕНТР',item[0]))
    if choices and abs(choices[0][0]-position)<=tol:
        return choices[0][0],choices[0][1]
    return float(position),''
