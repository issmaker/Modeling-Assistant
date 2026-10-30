"""Read-only wall corner metrics. No path mutations and no unsafe auto-fixes."""
from __future__ import annotations
from math import atan2, degrees, hypot, isfinite


def inspect_wall_corners(points, inset_points=None, *, inset_distance=0.0,
                         limit=12):
    """Measure exact authored turn and displaced *derived* inner corners.

    Produces telemetry only. A large miter is a review flag, NOT permission to
    alter the user path, and not sufficient evidence that a solid is invalid.
    """
    if len(points)<2:
        return {'corners':0,'sharp':0,'high_z':0,'max_turn_deg':0.,
                'max_miter_ratio':0.,'risky':[]}
    xy=[(float(p.x),float(p.y),float(p.z)) for p in points]
    if not all(isfinite(t) for v in xy for t in v):
        raise ValueError('Corner audit: non-finite authored coordinates')
    closed=len(xy)>2 and hypot(xy[0][0]-xy[-1][0],xy[0][1]-xy[-1][1])<1e-7 and abs(xy[0][2]-xy[-1][2])<1e-7
    work=xy[:-1] if closed else xy
    inner=None
    if inset_points is not None:
        inner=[(float(p.x),float(p.y),float(p.z)) for p in inset_points]
        if closed and len(inner)==len(xy):inner=inner[:-1]
        if len(inner)!=len(work):raise ValueError('Corner audit: unpaired derived contour')
        if any(not isfinite(t) for v in inner for t in v):
            raise ValueError('Corner audit: non-finite derived contour')
    indices=range(len(work)) if closed else range(1,len(work)-1)
    result={'corners':0,'sharp':0,'high_z':0,'max_turn_deg':0.,
            'max_miter_ratio':0.,'risky':[]}
    for i in indices:
        previous=work[i-1];current=work[i];following=work[(i+1)%len(work)]
        before=(current[0]-previous[0],current[1]-previous[1]);after=(following[0]-current[0],following[1]-current[1])
        len_before=hypot(*before);len_after=hypot(*after)
        if min(len_before,len_after)<1e-8:
            raise ValueError('Corner audit: zero horizontal edge')
        cross=(before[0]*after[1]-before[1]*after[0])/(len_before*len_after)
        dot=(before[0]*after[0]+before[1]*after[1])/(len_before*len_after)
        turn=degrees(abs(atan2(cross,dot)))
        result['corners']+=1
        if turn>30:result['sharp']+=1
        if abs(current[2]-previous[2])>max(4.0,len_before) or abs(current[2]-following[2])>max(4.0,len_after):
            result['high_z']+=1
        result['max_turn_deg']=max(result['max_turn_deg'],turn)
        ratio=0.
        if inner is not None and inset_distance>1e-7:
            ratio=hypot(inner[i][0]-current[0],inner[i][1]-current[1])/inset_distance
            result['max_miter_ratio']=max(result['max_miter_ratio'],ratio)
        if (ratio>=5 or turn>=150) and len(result['risky'])<limit:
            result['risky'].append({'index':i,'turn_deg':round(turn,5),
                                    'miter_ratio':round(ratio,5),
                                    'min_neighbor_xy_m':round(min(len_before,len_after),5),
                                    'z':current[2]})
    return result
