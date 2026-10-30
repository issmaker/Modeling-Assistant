"""Read-only cornice profile-reach audit; never mutates the author's path.

A true 3D trim is not always representable by one sweep cross section. This
module detects short-span/large-profile conflicts before the BMesh builder,
where later UV and topology errors would be much harder to reproduce.
"""
from math import atan2, hypot, degrees, tan, radians, isfinite

def inspect_cornice_reach(path, lateral_offsets, *, closed=False):
    vertices=[tuple(float(t) for t in p) for p in path]
    if closed and len(vertices)>1 and vertices[0]==vertices[-1]:vertices.pop()
    if len(vertices)<(3 if closed else 2):
        raise ValueError('Карниз: исходный путь слишком короткий')
    if not lateral_offsets:raise ValueError('Карниз: у профиля нет X-координат')
    reach=max(abs(float(v)) for v in lateral_offsets)
    if not isfinite(reach):raise ValueError('Карниз: нечисловой вынос профиля')
    issues=[];samples=[]
    corners=range(len(vertices)) if closed else range(1,len(vertices)-1)
    for i in corners:
        a=vertices[(i-1)%len(vertices)];b=vertices[i];c=vertices[(i+1)%len(vertices)]
        prev=(b[0]-a[0],b[1]-a[1]);nxt=(c[0]-b[0],c[1]-b[1])
        la=hypot(*prev);lb=hypot(*nxt)
        if min(la,lb)<1e-9:
            issues.append(dict(index=i,code='ZERO_XY_EDGE'));continue
        cross=prev[0]*nxt[1]-prev[1]*nxt[0]
        dot=prev[0]*nxt[0]+prev[1]*nxt[1]
        angle=abs(degrees(atan2(cross,dot)))
        if angle>=179.9:
            issues.append(dict(index=i,code='REVERSAL',angle=angle));continue
        tangential_reach=reach*abs(tan(radians(angle)*.5))
        ratio=tangential_reach/min(la,lb) if min(la,lb)>0 else float('inf')
        samples.append(dict(index=i,turn_deg=angle,reach_m=tangential_reach,
                            relative_to_shorter_edge=ratio))
        if tangential_reach>=.47*min(la,lb):
            issues.append(dict(index=i,code='TRIM_OVERLAP_RISK',angle=angle,
                               reach_m=tangential_reach,short_edge_m=min(la,lb)))
    return {'status':'REVIEW' if issues else 'OK', 'checked':len(samples),
            'issues':issues,'samples':samples,'path_unchanged':True}
