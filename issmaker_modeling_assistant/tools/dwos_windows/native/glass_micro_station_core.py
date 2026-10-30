"""W58.21 micro-station glazing contact solver.

A coincident source facade station must not become a reversed 3D glass strip
when its adjacent 1000-px/m inset segments have different miter depth vectors.
Do not edit the authored host path. Geometry adjustment is limited to
glass pane vertices and strictly constrained at sub-centimetre scales.
"""
from math import hypot, dist

def _cross(a,b):return a[0]*b[1]-a[1]*b[0]

def _intersect(a,b,c,d):
    r=(b[0]-a[0],b[1]-a[1])
    s=(d[0]-c[0],d[1]-c[1])
    den=_cross(r,s)
    if abs(den)<1e-8:return None
    t=_cross((c[0]-a[0],c[1]-a[1]),s)/den
    return (a[0]+t*r[0],a[1]+t*r[1])

def join_coincident_stations(ss,row0,row1,vertices,topology):
    stations=topology.get("stations",())
    origin=float(topology["width"])*.5
    pairs=[]
    for a,b in zip(stations,stations[1:]):
        da=dist(tuple(a["center"]),tuple(b["center"]))
        ds=abs(float(a["s"])-float(b["s"]))
        if da<1.e-5 and ds<1.e-5:
            pairs.append((float(a["s"])-origin,float(b["s"])-origin))
    if not pairs or len(ss)<4:return set()
    removed=set()
    for j in range(1,len(ss)-2):
        if not any(abs(ss[j]-a)<2.e-7 and abs(ss[j+1]-b)<2.e-7 for a,b in pairs):
            continue
        verts=vertices
        before=verts[row0[j]]
        after=verts[row0[j+1]]
        a=tuple(float(verts[row0[j-1]][k]) for k in (0,2))
        b=tuple(float(before[k]) for k in (0,2))
        c=tuple(float(after[k]) for k in (0,2))
        d=tuple(float(verts[row0[j+2]][k]) for k in (0,2))
        gap=dist(b,c)
        # A source coincident station can be offset in GLASS by a large
        # finite miter at a >90deg architectural turn. Use the true junction
        # of adjacent glass segments, bounded by their visible segment length.
        if gap<.002:
            xy=((b[0]+c[0])*.5,(b[1]+c[1])*.5)
        else:
            xy=_intersect(a,b,c,d)
            flank=min(dist(a,b),dist(c,d))
            allowed=min(.12, flank*.25)
            if (xy is None or gap>2*allowed
                    or max(dist(xy,b),dist(xy,c))>allowed):
                raise ValueError(f"W5830_GLASS_LOCAL_MITER_UNSUPPORTED: j={j} gap={gap:.7g} allowed={allowed:.7g} flank={flank:.7g} a={a} b={b} c={c} d={d} intersect={xy}")
        # Adjust pane *only*, preserving height (local Y), thickness/depth
        # and all source path coordinates. Make the next real pane share the
        # actual same vertex ID, not merely coincident floating point positions.
        for row in (row0,row1):
            va=row[j]
            vb=row[j+1]
            xyz=list(verts[va])
            xyz[0]=xy[0];xyz[2]=xy[1]
            verts[va]=tuple(xyz)
            row[j+1]=va
        removed.add(j)
    return removed
