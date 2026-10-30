"""DWOS 3.3 Corner Interior Boundary Solver.

The Frame contact boundary and the Interior reveal boundary are different
architectural entities.  For a hard CORNER the back/reveal offset follows the
normal of each facade patch; only the actual hard junction uses the miter
intersection.  With a Corner Post, each side terminates before the junction and
there is no artificial diagonal bridge through the post volume.
"""
from __future__ import annotations
try:
    from .adaptive_opening_core import (EPS, add,sub,mul,dot,cross,length,norm,lerp,
        host_to_window_local,map_svd_local)
except ImportError:
    from adaptive_opening_core import (EPS, add,sub,mul,dot,cross,length,norm,lerp,
        host_to_window_local,map_svd_local)


def _segment(topology,s):
    stations=topology.get('stations') or []
    width=float(topology.get('width',0.0));u=max(0.0,min(width,float(s)+width*.5))
    if len(stations)<2:raise ValueError('Corner Interior: недостаточно stations')
    if u<=float(stations[0]['s']):return 0,0.0,u
    for i,(a,b) in enumerate(zip(stations,stations[1:])):
        sa=float(a['s']);sb=float(b['s'])
        if sa-EPS<=u<=sb+EPS:
            t=0.0 if sb-sa<=EPS else (u-sa)/(sb-sa)
            return i,max(0.0,min(1.0,t)),u
    return len(stations)-2,1.0,u


def _segment_normal(topology,index):
    stations=topology.get('stations') or []
    a=tuple(stations[index]['center']);b=tuple(stations[index+1]['center'])
    up=norm(tuple(topology['up_axis']))
    tangent=sub(b,a);tangent=sub(tangent,mul(up,dot(tangent,up)))
    tangent=norm(tangent)
    sign=float(topology.get('orientation_global_sign',1.0))
    return mul(norm(cross(up,tangent)),sign)


def _hard_station_s(topology):
    stations=topology.get('stations') or [];width=float(topology.get('width',0.0))
    out=[]
    for idx in topology.get('hard_junction_station_indices') or []:
        if 0<int(idx)<len(stations)-1:
            out.append(float(stations[int(idx)]['s'])-width*.5)
    return out


def map_reveal_local(topology,s,v,d,*,miter_at_hard=True):
    """Map an Interior boundary point using patch-local depth on CORNER windows."""
    if topology.get('classification')!='CORNER':
        return map_svd_local(topology,s,v,d)
    if miter_at_hard:
        tol=max(1e-8,float(topology.get('width',1.0))*1e-9)
        if any(abs(float(s)-q)<=tol for q in _hard_station_s(topology)):
            # Exact intersection of the two offset planes.  OpeningTopology's
            # miter vector is already the normalized offset-plane intersection.
            return map_svd_local(topology,s,v,d)
    idx,t,_u=_segment(topology,s)
    stations=topology['stations']
    center=lerp(tuple(stations[idx]['center']),tuple(stations[idx+1]['center']),t)
    up=norm(tuple(topology['up_axis']))
    depth=_segment_normal(topology,idx)
    host=add(center,add(mul(up,float(v)),mul(depth,float(d))))
    return host_to_window_local(topology,host)


def boundary_samples(topology,s0,s1,*,include_hard=True):
    """Stations needed to author a reveal/back boundary without diagonal shortcuts."""
    lo,hi=sorted((float(s0),float(s1)));values=[lo,hi]
    width=float(topology.get('width',0.0))
    for st in topology.get('stations') or []:
        s=float(st['s'])-width*.5
        if lo+EPS<s<hi-EPS:values.append(s)
    if include_hard:
        values.extend(s for s in _hard_station_s(topology) if lo+EPS<s<hi-EPS)
    values=sorted(set(round(v,10) for v in values))
    return values if s0<=s1 else list(reversed(values))
