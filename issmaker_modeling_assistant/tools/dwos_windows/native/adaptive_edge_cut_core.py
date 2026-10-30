"""W58.4 experimental topology-aware edge-distance cutting (2D domains).

No scene mutation. Signed-distance contour extraction permits split interiors
and new topology; outer authored vertices remain unchanged. The level set is
numerically approximated. Every returned contour is independently checked
against exact segment distances, expected containment, length, and topology.
Not a production-certified exact arrangement or straight-skeleton solver.
"""
from __future__ import annotations
import math

SCHEMA = "ISSMAKER_W58_4_ADAPTIVE_EDGE_CUT_V1"
EPS = 1e-10

def cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

def signed_area(points):
    return .5*sum(x[0]*y[1]-x[1]*y[0] for x,y in zip(points,points[1:]+points[:1]))

def distance_segment(p,a,b):
    dx=b[0]-a[0];dy=b[1]-a[1];ll=dx*dx+dy*dy
    if ll<1e-24:return math.hypot(p[0]-a[0],p[1]-a[1])
    t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/ll))
    return math.hypot(p[0]-a[0]-dx*t,p[1]-a[1]-dy*t)

def inside(p,points):
    x,y=p;hit=False
    for a,b in zip(points,points[1:]+points[:1]):
        if (a[1]>y)!=(b[1]>y):
            if x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:hit=not hit
    return hit

def check_source(poly):
    pts=[(float(x),float(y)) for x,y in poly]
    if len(pts)>2 and math.dist(pts[0],pts[-1])<1e-10:pts.pop()
    # BMesh plane bisect can produce sub-10-micron duplicate vertices even
    # with an exact authored contour. Normalize the ANALYSIS COPY ONLY,
    # never change the original UV Wall vertices or their UV data.
    cleaned=[]
    for pt in pts:
        if not cleaned or math.dist(pt,cleaned[-1])>=1e-5:
            cleaned.append(pt)
    if len(cleaned)>2 and math.dist(cleaned[0],cleaned[-1])<1e-5:
        cleaned.pop()
    pts=cleaned
    if not 3<=len(pts)<=1024 or any(not math.isfinite(x) or not math.isfinite(y) for x,y in pts):
        raise ValueError("BAD_SOURCE_COORDINATES")
    if abs(signed_area(pts))<1e-9:raise ValueError("ZERO_SOURCE_AREA")
    if any(math.dist(a,b)<1e-9 for a,b in zip(pts,pts[1:]+pts[:1])):
        raise ValueError("ZERO_SOURCE_EDGE")
    # No normalization/removal of authored vertices. Source identity preserved.
    from .surface_step_core import find_self_crossings
    collisions=find_self_crossings(pts)
    if collisions:raise ValueError("SOURCE_SELF_INTERSECTION:"+str(collisions[:2]))
    if signed_area(pts)<0:pts.reverse()
    return pts

def _level(p,poly,w):
    delta=min(distance_segment(p,a,b) for a,b in zip(poly,poly[1:]+poly[:1]))
    return (delta if inside(p,poly) else -delta)-w

def _contours(poly, w, step):
    minx=min(p[0] for p in poly)-step*2;maxx=max(p[0] for p in poly)+step*2
    miny=min(p[1] for p in poly)-step*2;maxy=max(p[1] for p in poly)+step*2
    nx=max(2,math.ceil((maxx-minx)/step));ny=max(2,math.ceil((maxy-miny)/step))
    if (nx+1)*(ny+1)>800000:
        raise RuntimeError("RESOURCE_LIMIT_GRID_TOO_LARGE")
    sx=(maxx-minx)/nx;sy=(maxy-miny)/ny
    xs=[minx+i*sx for i in range(nx+1)]
    ys=[miny+j*sy for j in range(ny+1)]
    values=[[ _level((xs[i],ys[j]),poly,w) for j in range(ny+1)] for i in range(nx+1)]
    points={};segments=[]
    def crossing(tag,i,j,pa,pb,fa,fb):
        key=(tag,i,j)
        if key not in points:
            lo=0.;hi=1.
            # original fa and fb have opposite signs by caller
            for _ in range(21):
                t=(lo+hi)*.5
                mid=(pa[0]*(1-t)+pb[0]*t,pa[1]*(1-t)+pb[1]*t)
                v=_level(mid,poly,w)
                if (v>=0)==(fa>=0):lo=t
                else:hi=t
            t=(lo+hi)*.5
            points[key]=(pa[0]*(1-t)+pb[0]*t,pa[1]*(1-t)+pb[1]*t)
        return key
    for i in range(nx):
        for j in range(ny):
            coords=((xs[i],ys[j]),(xs[i+1],ys[j]),(xs[i+1],ys[j+1]),(xs[i],ys[j+1]))
            val=(values[i][j],values[i+1][j],values[i+1][j+1],values[i][j+1])
            edge_defs=((0,1,("H",i,j)),(1,2,("V",i+1,j)),
                       (3,2,("H",i,j+1)),(0,3,("V",i,j)))
            crossed={}
            for index,(a,b,edge_key) in enumerate(edge_defs):
                if (val[a]>=0)!=(val[b]>=0):
                    crossed[index]=crossing(edge_key[0],edge_key[1],edge_key[2],coords[a],coords[b],val[a],val[b])
            if len(crossed)==2:
                pair=list(crossed.values());segments.append(tuple(pair))
            elif len(crossed)==4:
                cc=((xs[i]+xs[i+1])*.5,(ys[j]+ys[j+1])*.5)
                center=_level(cc,poly,w)>=0
                surrounding=((3,0),(0,1),(1,2),(2,3))
                for corner,(e1,e2) in enumerate(surrounding):
                    if (val[corner]>=0)!=center:
                        segments.append((crossed[e1],crossed[e2]))
            elif len(crossed) not in (0,):
                raise RuntimeError("NUMERIC_MARCHING_SADDLE:"+str((i,j,len(crossed))))
    graph={}
    for a,b in segments:
        graph.setdefault(a,[]).append(b)
        graph.setdefault(b,[]).append(a)
    open_nodes=[(k,len(v)) for k,v in graph.items() if len(v)!=2]
    if open_nodes:
        raise RuntimeError("NUMERIC_OPEN_OR_BRANCH_CONTOUR:"+str(open_nodes[:4]))
    seen=set();loops=[]
    for first in graph:
        if first in seen:continue
        cur=first;prev=None;loop=[]
        for _ in range(len(graph)+1):
            if cur==first and loop:break
            if cur in seen:raise RuntimeError("NUMERIC_CONTOUR_REVISIT")
            seen.add(cur);loop.append(points[cur])
            nxt=[node for node in graph[cur] if node!=prev]
            if not nxt:raise RuntimeError("NUMERIC_CONTOUR_INTERRUPTED")
            node=nxt[0];prev,cur=cur,node
        if cur!=first:raise RuntimeError("NUMERIC_CONTOUR_NOT_CLOSED")
        if len(loop)>=3 and abs(signed_area(loop))>1e-9:loops.append(loop)
    return loops,{"grid":[nx,ny],"grid_step_m":[sx,sy],"sample_count":(nx+1)*(ny+1),
                  "intersection_events":len(points),"segments":len(segments)}



def _simplify_contour(points, tolerance):
    """Compact only NEW generated contour using bounded Hausdorff-like error.

    Keeps two farthest cyclic anchors and recursively keeps maximal deviations.
    Full geometry oracle still checks resulting contour and refuses bad cuts.
    """
    if len(points)<6:return list(points)
    first=0
    opposite=max(range(1,len(points)),key=lambda i:math.dist(points[first],points[i]))
    def rdp(chain):
        if len(chain)<=2:return chain
        a=chain[0];b=chain[-1]
        distances=[distance_segment(x,a,b) for x in chain[1:-1]]
        if not distances:return [a,b]
        ix=max(range(len(distances)),key=lambda k:distances[k])
        if distances[ix]<=tolerance:return [a,b]
        at=ix+1
        return rdp(chain[:at+1])[:-1]+rdp(chain[at:])
    a=rdp(points[:opposite+1])
    b=rdp(points[opposite:]+points[:1])
    compact=a[:-1]+b[:-1]
    return compact if len(compact)>=3 else list(points)


def _refine_distance_chords(loop, source, width, tolerance):
    """Split only generated chords whose midpoint misses the exact w-level.

    Keeps refinement local instead of scaling the entire sampling grid.
    The nearest boundary feature is evaluated from original authored edges.
    """
    edges=list(zip(source,source[1:]+source[:1]))
    def nearest(p):
        best=None
        for a,b in edges:
            dx=b[0]-a[0];dy=b[1]-a[1];den=dx*dx+dy*dy
            t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/den))
            q=(a[0]+t*dx,a[1]+t*dy)
            dist=math.dist(p,q)
            if best is None or dist<best[0]:best=(dist,q)
        return best
    current=list(loop)
    for iteration in range(4):
        updated=[];adds=0
        for a,b in zip(current,current[1:]+current[:1]):
            updated.append(a)
            mid=((a[0]+b[0])*.5,(a[1]+b[1])*.5)
            dist,q=nearest(mid)
            if abs(dist-width)<=tolerance*.7:continue
            if not inside(mid,source) or dist<1e-10:
                raise RuntimeError("NUMERIC_CHORD_OUTSIDE_SOURCE")
            direction=((mid[0]-q[0])/dist,(mid[1]-q[1])/dist)
            projected=(q[0]+direction[0]*width,q[1]+direction[1]*width)
            if not inside(projected,source):
                raise RuntimeError("NUMERIC_CHORD_PROJECTION_OUTSIDE")
            updated.append(projected)
            adds+=1
        current=updated
        if adds==0:break
        if len(current)>10000:raise RuntimeError("RESOURCE_LIMIT_ADAPTIVE_CHORD_VERTICES")
    return current

def _restore_convex_miter_corners(loops, poly, w, grid_step):
    """Restore exact supporting-line intersections lost to grid chamfering.

    Only moves NEW generated points (never authored outer polygon). Convex
    corners should meet at miter vertices; concave corners retain rounded
    Euclidean-distance arcs. Every change is later checked by the oracle.
    """
    n=len(poly)
    for k in range(n):
        a=poly[(k-1)%n];v=poly[k];b=poly[(k+1)%n]
        if cross(a,v,b)<=1e-10:continue
        e0=(v[0]-a[0],v[1]-a[1]);e1=(b[0]-v[0],b[1]-v[1])
        l0=math.hypot(*e0);l1=math.hypot(*e1)
        e0=(e0[0]/l0,e0[1]/l0);e1=(e1[0]/l1,e1[1]/l1)
        p0=(v[0]-w*e0[1],v[1]+w*e0[0])
        p1=(v[0]-w*e1[1],v[1]+w*e1[0])
        den=e0[0]*e1[1]-e0[1]*e1[0]
        if abs(den)<1e-8:continue
        diff=(p1[0]-p0[0],p1[1]-p0[1])
        t=(diff[0]*e1[1]-diff[1]*e1[0])/den
        target=(p0[0]+e0[0]*t,p0[1]+e0[1]*t)
        if not inside(target,poly):continue
        dist=min(distance_segment(target,x,y) for x,y in zip(poly,poly[1:]+poly[:1]))
        if abs(dist-w)>1e-7:continue
        for idx,loop in enumerate(loops):
            if not loop:continue
            near=[i for i,pt in enumerate(loop) if math.dist(pt,target)<=grid_step*1.4]
            if not near:continue
            # Replace only nearby generated chamfer samples in the same loop.
            candidate=[(target if i in near else pt) for i,pt in enumerate(loop)]
            compact=[]
            for pt in candidate:
                if not compact or math.dist(compact[-1],pt)>1e-8:
                    compact.append(pt)
            if len(compact)>1 and math.dist(compact[0],compact[-1])<1e-8:
                compact.pop()
            if len(compact)>=3:loops[idx]=compact
    return loops

def _solve_once(source_polygon,width,*,step=None,geometry_tolerance=0.001):
    poly=check_source(source_polygon)
    w=float(width)
    if not math.isfinite(w) or w<=0:raise ValueError("FRAME_WIDTH_INVALID")
    if not math.isfinite(geometry_tolerance) or geometry_tolerance<=0:
        raise ValueError("GEOMETRY_TOLERANCE_INVALID")
    target_step=min(0.024,w/5) if step is None else float(step)
    if not 0.002<=target_step<=0.08:raise ValueError("GRID_STEP_OUT_OF_RANGE")
    loops,meta=_contours(poly,w,target_step)
    loops=_restore_convex_miter_corners(loops,poly,w,max(meta['grid_step_m']))
    oriented=[]
    from .surface_step_core import find_self_crossings
    for loop in loops:
        loop=_simplify_contour(loop, min(geometry_tolerance*.25,target_step*.08))
        loop=_refine_distance_chords(loop,poly,w,geometry_tolerance)
        if signed_area(loop)<0:loop.reverse()
        crossings=find_self_crossings(loop,eps=1e-8)
        if crossings:raise RuntimeError("ALGO_SELF_INTERSECTION:"+str(crossings[:2]))
        deviations=[]
        # A contour chord may deviate from the exact distance field; independently
        # verify vertices and midpoints, and refuse results outside tolerance.
        for a,b in zip(loop,loop[1:]+loop[:1]):
            for p in (a,((a[0]+b[0])*.5,(a[1]+b[1])*.5)):
                dist=min(distance_segment(p,c,d) for c,d in zip(poly,poly[1:]+poly[:1]))
                deviations.append(abs(dist-w))
                if not inside(p,poly):
                    raise RuntimeError("ALGO_INNER_CONTOUR_OUTSIDE_SOURCE")
        if max(deviations)>geometry_tolerance:
            raise RuntimeError("NUMERIC_DISTANCE_TOLERANCE:"+str(max(deviations)))
        oriented.append(loop)
    # W58.4-016: enforce SHARP architectural joins. Only the newly created
    # LP contour is modified; the source UV Wall and source UV are immutable.
    # No round/bevel fallback. Reject unresolvable joins instead of returning
    # rounded geometry while claiming a successful window frame.
    from .adaptive_edge_cut_sharp import (
        sharp_reentrant, sharp_oracle, sharp_offset_oracle)
    oriented,sharpened_events=sharp_reentrant(
        oriented,poly,w,max(meta["grid_step_m"]))
    events_missing=[e for e in sharpened_events if e["status"]!="SHARP_MITER"]
    oracle_errors=(sharp_oracle(oriented,poly,w,geometry_tolerance)
                   +sharp_offset_oracle(oriented,poly,w,geometry_tolerance))
    if events_missing or oracle_errors:
        raise RuntimeError("SHARP_TOPOLOGY_UNRESOLVED:"+
                           repr({"events":events_missing[:4],
                                 "oracle":oracle_errors[:4]}))
    meta["sharp_miter_joins"]=sum(
        x["status"]=="SHARP_MITER" for x in sharpened_events)
    meta["corner_policy"]="SHARP_ONLY_NO_ROUND_NO_BEVEL"
    areas=[abs(signed_area(x)) for x in oriented]
    if sum(areas)>=abs(signed_area(poly)) and areas:
        raise RuntimeError("ALGO_INNER_AREA_NOT_SMALLER")
    if not oriented:
        # A missing numerical contour is NOT automatically bad geometry.
        # Physical impossibility is proven only for a certified axis-aligned
        # rectangle whose smaller dimension is at most twice frame width.
        from .surface_step_core import rectangle_axes
        bounds=rectangle_axes(poly)
        if bounds is not None:
            lo,hi,bottom,top=bounds
            category=("BLOCKED_PHYSICAL" if min(hi-lo,top-bottom)<=2*w+1e-8
                      else "BLOCKED_NUMERIC_UNRESOLVED")
        else:
            category="BLOCKED_NUMERIC_UNRESOLVED"
    else:
        category="ACCEPTED_SPLIT" if len(oriented)>1 else "ACCEPTED_SINGLE"
    return {"schema":SCHEMA,"category":category,"width":w,"outer":poly,
            "inner_loops":oriented,"inner_areas":areas,
            "outer_area":abs(signed_area(poly)),"meta":meta,
            "resolution_status":"NUMERICAL_APPROXIMATION_WITH_ORACLE"}

def solve(source_polygon,width,*,step=None,geometry_tolerance=0.001):
    """Refine numerical discretization, never relax physical tolerance."""
    base=min(.024,float(width)/5.) if step is None else float(step)
    errors=[]
    for level in range(3):
        resolution=base/(2**level)
        try:
            result=_solve_once(source_polygon,width,step=resolution,
                               geometry_tolerance=geometry_tolerance)
            result["meta"]["refinement_level"]=level
            result["meta"]["requested_grid_step_m"]=base
            return result
        except RuntimeError as exc:
            why=str(exc)
            errors.append(why)
            if not why.startswith(("NUMERIC_DISTANCE_TOLERANCE", "NUMERIC_MARCHING_SADDLE",
                                    "NUMERIC_OPEN_OR_BRANCH_CONTOUR")):
                raise
            if level==2:
                raise RuntimeError("NUMERIC_UNRESOLVED_AFTER_REFINEMENT:"+str(errors)) from exc
    raise RuntimeError("NUMERIC_INTERNAL_REFINEMENT_FAILURE")
