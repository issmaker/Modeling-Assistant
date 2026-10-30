"""Exact square-join miter recovery on raster-derived topology (W58.4 Pass02).

No ROUND or BEVEL fallback is ever permitted. The source is immutable.
Topology discovery still uses the numerical interior-distance contour.
"""
import math
from .adaptive_edge_cut_core import cross, inside, signed_area, distance_segment

def _miter(poly, k, width):
    a=poly[k-1];b=poly[k];c=poly[(k+1)%len(poly)]
    d0=(b[0]-a[0],b[1]-a[1]);d1=(c[0]-b[0],c[1]-b[1])
    z0=math.hypot(*d0);z1=math.hypot(*d1)
    if min(z0,z1)<1e-8:return None
    d0=(d0[0]/z0,d0[1]/z0);d1=(d1[0]/z1,d1[1]/z1)
    den=d0[0]*d1[1]-d0[1]*d1[0]
    if abs(den)<1e-9:return None
    l0=(b[0]-width*d0[1],b[1]+width*d0[0])
    l1=(b[0]-width*d1[1],b[1]+width*d1[0])
    delta=(l1[0]-l0[0],l1[1]-l0[1])
    t=(delta[0]*d1[1]-delta[1]*d1[0])/den
    return (l0[0]+t*d0[0],l0[1]+t*d0[1])

def _dedupe(points, eps=1e-7):
    result=[]
    for p in points:
        if not result or math.dist(p,result[-1])>eps:result.append(p)
    if len(result)>1 and math.dist(result[0],result[-1])<=eps:result.pop()
    return result

def sharp_reentrant(loops, outer, width, grid_step):
    """Replace round re-entrant arcs with exact intersections of offset lines.

    Geometry acceptance is deferred to independent contract validation.
    """
    loops=[list(loop) for loop in loops]
    report=[]
    for k,vertex in enumerate(outer):
        if cross(outer[k-1],vertex,outer[(k+1)%len(outer)])>=-1e-9:continue
        target=_miter(outer,k,width)
        if target is None or not inside(target,outer):
            report.append({"corner":k,"status":"MISSING_SHARP_JOIN"})
            continue
        for ix,loop in enumerate(loops):
            if len(loop)<3:continue
            # The corner was rendered as a circular Euclidean-distance arc.
            # Replace its contiguous, circular samples with a single sharp miter.
            near=[j for j,p in enumerate(loop) if math.dist(p,vertex)<width+grid_step*1.3]
            if not near:continue
            # Long circular runs are expected on sharp re-entrant corners.
            # Decide from run connectivity, not arbitrary vertex percentage.
            mask=set(near)
            groups=[]
            for j in near:
                if (j-1)%len(loop) not in mask:
                    group=[];cur=j
                    while cur in mask:
                        group.append(cur);cur=(cur+1)%len(loop)
                        if cur==j:break
                    groups.append(group)
            if len(groups)!=1:
                report.append({"corner":k,"status":"MULTIPLE_ARC_RUNS","runs":len(groups)});continue
            group=groups[0];first=group[0]
            # Rotate so the replaced arc is a contiguous prefix.
            rotated=loop[first:]+loop[:first]
            n=len(group)
            new=_dedupe([target]+rotated[n:])
            if len(new)>=3:
                loops[ix]=new
                report.append({"corner":k,"status":"SHARP_MITER","removed":n,"target":target})
                break
    return loops,report

def sharp_oracle(loops, original, width, tolerance=0.001):
    """No rounded turns, no out-of-source vertices or sub-width intrusion."""
    from .surface_step_core import find_self_crossings
    errs=[]
    source_edges=list(zip(original,original[1:]+original[:1]))
    for ix,loop in enumerate(loops):
        if find_self_crossings(loop,eps=1e-8):
            errs.append(f"SELF_CROSSING:{ix}");continue
        if signed_area(loop)<1e-10:errs.append(f"ZERO_AREA:{ix}")
        for a,b in zip(loop,loop[1:]+loop[:1]):
            # Sample both endpoints and interior to test the actual generated edges.
            for t in (0.,.25,.5,.75,1.):
                point=(a[0]*(1-t)+b[0]*t,a[1]*(1-t)+b[1]*t)
                if not inside(point,original):
                    errs.append(f"OUTSIDE:{ix}");break
                distance=min(distance_segment(point,x,y) for x,y in source_edges)
                if distance < width-tolerance:
                    errs.append(f"WIDTH_VIOLATION:{ix}:{distance:.5f}");break
            if errs:break
    return errs


def sharp_offset_oracle(loops, outer, width, tolerance=0.001):
    """Every generated straight edge must lie on an authored edge's offset line.

    This forbids rounded arcs, bevels and silently tapered profile widths.
    Checks supporting lines at both endpoints to distinguish a true segment
    from a curved collection of short chords.
    """
    errors=[]
    edges=list(zip(outer,outer[1:]+outer[:1]))
    for k,loop in enumerate(loops):
        for ei,(a,b) in enumerate(zip(loop,loop[1:]+loop[:1])):
            line_matches=[]
            for src_a,src_b in edges:
                dx=src_b[0]-src_a[0];dy=src_b[1]-src_a[1]
                length=math.hypot(dx,dy)
                if length<1e-10:continue
                def residual(point):
                    return abs((dx*(point[1]-src_a[1])-dy*(point[0]-src_a[0]))/length-width)
                if max(residual(a),residual(b))<=tolerance:
                    line_matches.append(True)
            if not line_matches:
                errors.append(f"UNSUPPORTED_SHARP_EDGE:{k}:{ei}")
    return errors
