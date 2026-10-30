"""DWOS 3.5 Corner Interior Lab + operation preflight.

The lab intentionally keeps several *validated* geometric policies available for
field comparison.  It also provides a conservative safe-depth contract so an
Interior edit is rejected before a self-intersecting mesh can be committed.

Pure module: no Blender dependency.
"""
from __future__ import annotations
import math
try:
    from .interior_boundary_core import boundary_samples
except ImportError:
    from interior_boundary_core import boundary_samples

EPS=1e-8
VARIANTS={
    'A':'HOST/REVEAL',
    'B':'BRANCH MITER',
    'C':'INDEPENDENT REVEALS',
    'D':'ORTHOGONAL RETURN',
}


def hard_corner_s(topology):
    w=float(topology.get('width',0.0));sts=topology.get('stations') or []
    out=[]
    for i in topology.get('hard_junction_station_indices') or []:
        i=int(i)
        if 0<i<len(sts)-1: out.append(float(sts[i]['s'])-w*.5)
    return out


def branch_spans(rect,topology):
    s0,s1,_,_=map(float,rect);cuts=[s0]+[s for s in hard_corner_s(topology) if s0+EPS<s<s1-EPS]+[s1]
    cuts=sorted(set(round(x,10) for x in cuts))
    return [(a,b) for a,b in zip(cuts,cuts[1:]) if b-a>EPS]


def geometric_depth_limit(topology,rect,variant='B'):
    """Conservative limit before a corner offset can fold past a neighbouring branch."""
    if not topology or topology.get('classification')!='CORNER': return float('inf')
    spans=branch_spans(rect,topology)
    if not spans:return 0.0
    min_span=min(b-a for a,b in spans)
    if variant=='C':
        # Independent reveals do not create a long miter, but still keep a sane
        # relation to the local opening size.
        return max(0.01,min_span*1.75)
    limits=[]
    junctions=topology.get('corner_junctions') or []
    for j in junctions:
        ang=max(5.0,min(175.0,float(j.get('angle_deg',90.0))))
        # Offset-plane miter reach = depth / tan(theta/2).
        limits.append(min_span*max(0.08,math.tan(math.radians(ang)*.5))*0.92)
    base=min(limits) if limits else min_span
    if variant=='D': base=min(base,min_span*.75)
    return max(0.01,base)


def _orient(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def _on(a,b,p): return min(a[0],b[0])-EPS<=p[0]<=max(a[0],b[0])+EPS and min(a[1],b[1])-EPS<=p[1]<=max(a[1],b[1])+EPS

def segments_intersect(a,b,c,d):
    o1,o2,o3,o4=_orient(a,b,c),_orient(a,b,d),_orient(c,d,a),_orient(c,d,b)
    if (o1>EPS and o2<-EPS or o1<-EPS and o2>EPS) and (o3>EPS and o4<-EPS or o3<-EPS and o4>EPS):return True
    for o,p,x,y in ((o1,c,a,b),(o2,d,a,b),(o3,a,c,d),(o4,b,c,d)):
        if abs(o)<=EPS and _on(x,y,p):return True
    return False


def polyline_self_intersection(points):
    p=[tuple(map(float,x[:2])) for x in points]
    for i in range(len(p)-1):
        for j in range(i+2,len(p)-1):
            if j==i+1:continue
            if segments_intersect(p[i],p[i+1],p[j],p[j+1]):return (i,j)
    return None


def preflight(topology,rect,depth,variant='B'):
    depth=float(depth);variant=str(variant or 'B').upper()
    if variant not in VARIANTS:raise ValueError('Unknown Corner Interior variant')
    if depth<=EPS or not math.isfinite(depth):return {'ok':False,'reason':'DEPTH_INVALID','max_safe_depth':0.0}
    limit=geometric_depth_limit(topology,rect,variant)
    ok=depth<=limit+1e-7
    return {'ok':ok,'reason':('OK' if ok else 'SELF_INTERSECTION_RISK'),
            'max_safe_depth':float(limit),'variant':variant,'label':VARIANTS[variant]}
