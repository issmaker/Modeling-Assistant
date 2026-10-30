"""W55: exact 2D planar union of two angled open Interior 4-face sleeves.

Combines XY rectangles at 45/90 deg into a single open-topologically-connected
shell with two top/bottom faces triangulated and only original exposed terminal
walls. This intentionally does not fill the front or rear aperture.
Fail closed for same-axis, disjoint, multi-sleeve or >1 UV tile triangles.
"""
import math
from mathutils import Vector, geometry
from . import spatial_safety_core as spatial
EPS=1.0e-5
def _cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def _area(p):
    return .5*sum(p[i][0]*p[(i+1)%len(p)][1]-p[(i+1)%len(p)][0]*p[i][1] for i in range(len(p)))
def _uniq(pts):
    out=[]
    for p in pts:
        if not any(math.dist(p,q)<EPS for q in out):out.append(tuple(p))
    return out
def _box(mesh):
    verts=mesh['verts']
    if len(verts)!=8 or len(mesh['faces'])!=4 or any(len(f)!=4 for f in mesh['faces']):
        raise ValueError("W55_OPEN_SLEEVE_EXPECTED_8_VERTS_4_FACES")
    if any(not math.isfinite(float(x)) for v in verts for x in v):
        raise ValueError("W55_NONFINITE_COORD")
    zlo=min(v[2] for v in verts);zhi=max(v[2] for v in verts)
    if zhi-zlo<.1 or any(min(abs(v[2]-zlo),abs(v[2]-zhi))>EPS for v in verts):
        raise ValueError("W55_NONUPRIGHT")
    poly=_uniq([(p[0],p[1]) for p in verts])
    if len(poly)!=4:raise ValueError("W55_SOURCE_NOT_EXACT_RECT")
    center=(sum(x for x,y in poly)/4,sum(y for x,y in poly)/4)
    poly.sort(key=lambda p:math.atan2(p[1]-center[1],p[0]-center[0]))
    if _area(poly)<0:poly.reverse()
    vec=[(poly[(i+1)%4][0]-poly[i][0],poly[(i+1)%4][1]-poly[i][1]) for i in range(4)]
    lengths=[math.hypot(*v) for v in vec]
    if min(lengths)<.003 or max(lengths)<.2:raise ValueError("W55_BAD_SOURCE_SLEEVE_WIDTH")
    if any(abs(vec[i][0]*vec[(i+1)%4][0]+vec[i][1]*vec[(i+1)%4][1])>lengths[i]*lengths[(i+1)%4]*1e-4 for i in range(4)):
        raise ValueError("W55_NOT_RECTANGULAR_SLEEVE")
    if abs(lengths[0]-lengths[2])>.0001 or abs(lengths[1]-lengths[3])>.0001:
        raise ValueError("W55_BAD_OPPOSITE_EDGES")
    return {'poly':poly,'zlo':zlo,'zhi':zhi,'lengths':lengths,'rear_line':mesh.get('rear_line')}
def _inside(p,poly,strict=True):
    vals=[_cross(poly[i],poly[(i+1)%len(poly)],p) for i in range(len(poly))]
    return all(x>EPS for x in vals) if strict else all(x>=-EPS for x in vals)
def _intersect(a,b,c,d):
    rx=b[0]-a[0];ry=b[1]-a[1];sx=d[0]-c[0];sy=d[1]-c[1]
    det=rx*sy-ry*sx
    if abs(det)<1e-10:return None
    cx=c[0]-a[0];cy=c[1]-a[1]
    t=(cx*sy-cy*sx)/det;u=(cx*ry-cy*rx)/det
    if -EPS<t<1+EPS and -EPS<u<1+EPS:
        return max(0.,min(1.,t)),max(0.,min(1.,u)),(a[0]+t*rx,a[1]+t*ry)
    return None
def _segments(a,b):
    segments=[]
    for own,other in ((a,b),(b,a)):
        poly=own['poly'];q=other['poly']
        for idx,v in enumerate(poly):
            end=poly[(idx+1)%4]
            cuts=[(0.,v),(1.,end)]
            for j,c in enumerate(q):
                hit=_intersect(v,end,c,q[(j+1)%4])
                if hit is not None:cuts.append((hit[0],hit[2]))
            cuts.sort(key=lambda x:x[0])
            for (ta,p0),(tb,p1) in zip(cuts,cuts[1:]):
                if tb-ta<EPS:continue
                midpoint=((p0[0]+p1[0])*.5,(p0[1]+p1[1])*.5)
                if _inside(midpoint,q,strict=True):continue
                if math.dist(p0,p1)>EPS:
                    segments.append((p0,p1,own,idx))
    return segments
def _ring(segs):
    if not segs:raise ValueError("W55_NO_OUTER_BOUNDARY")
    def key(p):return tuple(round(x,5) for x in p)
    nxt={};vertices={}
    for a,b,own,edge in segs:
        ka=key(a);kb=key(b)
        if ka==kb:continue
        if ka in nxt and nxt[ka]!=kb:raise ValueError("W55_BRANCHING_UNION_BOUNDARY")
        nxt[ka]=kb;vertices.setdefault(ka,a);vertices.setdefault(kb,b)
    start=min(nxt)
    loop=[start];seen={start}
    while True:
        nextk=nxt.get(loop[-1])
        if nextk is None:raise ValueError("W55_OPEN_UNION_CHAIN")
        if nextk==start:break
        if nextk in seen:raise ValueError("W55_LOOP_TANGLE")
        seen.add(nextk);loop.append(nextk)
        if len(loop)>128:raise ValueError("W55_UNION_LOOP_TOO_COMPLEX")
    if len(seen)!=len(nxt):raise ValueError("W55_DISCONNECTED_UNION_BOUNDARY")
    poly=[vertices[x] for x in loop]
    if _area(poly)<EPS:raise ValueError("W55_UNION_NONPOSITIVE_AREA")
    return poly
def _uv_triangle(points,res):
    scale=1000./res
    # A canonical metric orientation, independent of the scene world position.
    for ang in (0,math.pi/4,-math.pi/4,math.pi/2,math.pi/8,-math.pi/8,*[k*math.pi/180 for k in range(180)]):
        c=math.cos(ang);s=math.sin(ang)
        xy=[(p[0]*c+p[1]*s,-p[0]*s+p[1]*c) for p in points]
        x0=min(a for a,b in xy);y0=min(b for a,b in xy)
        uv=[((a-x0)*scale,(b-y0)*scale) for a,b in xy]
        if max(max(u,v) for u,v in uv)<1-1e-6:return uv
    raise ValueError("W55_METRIC_TRIANGLE_EXCEEDS_4K_UV_TILE")
def merge_two(a,b,*,resolution=4096):
    aa=_box(a);bb=_box(b)
    if abs(aa['zlo']-bb['zlo'])>EPS or abs(aa['zhi']-bb['zhi'])>EPS:
        raise ValueError("W55_HEIGHTS_DIFFER")
    # At least 20deg deviation among long axes.  Parallel overlaps need a
    # different non-corner-specific policy.
    def long_axis(box):
        p=box['poly'];i=max(range(4),key=lambda k:box['lengths'][k])
        return ((p[(i+1)%4][0]-p[i][0])/box['lengths'][i],
                (p[(i+1)%4][1]-p[i][1])/box['lengths'][i])
    va=long_axis(aa);vb=long_axis(bb)
    dot=abs(va[0]*vb[0]+va[1]*vb[1])
    if dot>math.cos(math.radians(20)):
        raise ValueError("W55_PARALLEL_NEEDS_SEPARATE_SOLVER")
    segs=_segments(aa,bb)
    ring=_ring(segs)
    # Independent positive intersection test by convex Sutherland-Hodgman.
    clipped=list(aa['poly'])
    for i,c in enumerate(bb['poly']):
        d=bb['poly'][(i+1)%4]
        output=[]
        for j,p in enumerate(clipped):
            q=clipped[(j+1)%len(clipped)]
            inp=_cross(c,d,p)>-EPS;inq=_cross(c,d,q)>-EPS
            if inp:output.append(p)
            if inp!=inq:
                # Segment against INFINITE clipping line, not finite q-edge.
                rp=(q[0]-p[0],q[1]-p[1])
                rd=(d[0]-c[0],d[1]-c[1])
                denominator=rp[0]*rd[1]-rp[1]*rd[0]
                if abs(denominator)>1e-12:
                    diff=(c[0]-p[0],c[1]-p[1])
                    t=(diff[0]*rd[1]-diff[1]*rd[0])/denominator
                    output.append((p[0]+t*rp[0],p[1]+t*rp[1]))
        clipped=_uniq(output)
        if len(clipped)<3:break
    intersection=abs(_area(clipped)) if len(clipped)>=3 else 0.
    if intersection<1e-8:raise ValueError("W55_NO_POSITIVE_INTERSECTION")
    # Union of two overlapping convex rectangles is star-shaped about EVERY
    # point in their intersection.  A fan from the convex overlap centroid
    # gives bounded local triangles and keeps boundary edges conforming.
    # This avoids a single long ear-clipped diagonal exceeding a 4096 UV tile.
    hub=(sum(p[0] for p in clipped)/len(clipped),sum(p[1] for p in clipped)/len(clipped))
    if not _inside(hub,aa['poly']) or not _inside(hub,bb['poly']):
        raise ValueError("W55_INTERSECTION_CENTROID_NOT_INTERIOR")
    tri=[(ring[i],ring[(i+1)%len(ring)],hub) for i in range(len(ring))]
    area_fan=sum(abs(_area(q)) for q in tri)
    if abs(area_fan-abs(_area(ring)))>1e-4:
        raise ValueError("W55_STAR_FAN_AREA_MISMATCH")
    zlo=aa['zlo'];zhi=aa['zhi'];vv=[];lookup={};faces=[];uvs=[];roles=[]
    def vid(p):
        k=tuple(round(float(x),7) for x in p)
        if k not in lookup:lookup[k]=len(vv);vv.append(tuple(map(float,p)))
        return lookup[k]
    def face(points,uv,role):
        indices=tuple(vid(p) for p in points)
        if len(set(indices))!=len(indices):raise ValueError("W55_DEGENERATE_FACE")
        faces.append(indices);uvs.append(uv);roles.append(role)
    for triangle in tri:
        xy=list(triangle)
        if _area(xy)<0:xy.reverse()
        if _area(xy)<1e-9:raise ValueError("W55_STAR_FAN_ZERO_TRIANGLE")
        top=[(p[0],p[1],zhi) for p in xy]
        bottom=list(reversed([(p[0],p[1],zlo) for p in xy]))
        face(top,_uv_triangle(top,resolution),"INTERIOR_TOP")
        face(bottom,_uv_triangle(bottom,resolution),"INTERIOR_BOTTOM")
    # Existing side shells only had terminal cross-edges. Derive from physical
    # original terminal edges, skip long edges and hidden internal terminals.
    def on_terminal(p,q,box):
        poly=box['poly'];lens=box['lengths']
        shortest=min(lens)
        for i,length in enumerate(lens):
            if length>shortest+1e-4:continue
            a=poly[i];b=poly[(i+1)%4]
            if abs(_cross(a,b,p))>1e-4 or abs(_cross(a,b,q))>1e-4:continue
            dotp=(p[0]-a[0])*(b[0]-a[0])+(p[1]-a[1])*(b[1]-a[1])
            dotq=(q[0]-a[0])*(b[0]-a[0])+(q[1]-a[1])*(b[1]-a[1])
            if min(dotp,dotq)>-1e-4 and max(dotp,dotq)<length*length+1e-4:return True
        return False
    def on_back(p,q,box):
        hint=box.get('rear_line')
        if hint is None:return False
        x,y,nx,ny=hint
        norm=math.hypot(nx,ny)
        if norm<.5:raise ValueError('W56_INVALID_REAR_PLANE_NORMAL')
        # Distances from physical rear plane, in meters.
        return (abs((p[0]-x)*nx+(p[1]-y)*ny)<2e-4
                and abs((q[0]-x)*nx+(q[1]-y)*ny)<2e-4)
    terminal=0;back=0
    for i,p in enumerate(ring):
        q=ring[(i+1)%len(ring)]
        terminal_edge=on_terminal(p,q,aa) or on_terminal(p,q,bb)
        rear_edge=on_back(p,q,aa) or on_back(p,q,bb)
        if terminal_edge and rear_edge:
            raise ValueError('W56_AMBIGUOUS_BACK_AND_TERMINAL_EDGE')
        if terminal_edge or rear_edge:
            d=math.dist(p,q)
            pts=[(p[0],p[1],zlo),(q[0],q[1],zlo),(q[0],q[1],zhi),(p[0],p[1],zhi)]
            # Generated Interior gets no UV map; metric UV also not needed
            # for any of the union triangles, regardless of 4K tile size.
            face(pts,None,"INTERIOR_BACK" if rear_edge else "INTERIOR_TERMINAL")
            if rear_edge:back+=1
            else:terminal+=1
    if terminal<2 or back<2:
        raise ValueError("W56_BACK_CLOSURE_UNPROVEN:"+str((terminal,back)))
    if spatial.self_intersections(vv,faces,max_hits=1):
        raise ValueError("W55_ANGLED_UNION_SELF_INTERSECTION")
    return {"verts":vv,"faces":faces,"roles":roles,"uvs":uvs,"metric":{
        "method":"ORIENTED_RECTANGULAR_OPEN_SLEEVE_UNION","intersection_area_m2":intersection,
        "footprint_union_area_m2":abs(_area(aa['poly']))+abs(_area(bb['poly']))-intersection,
        "terminal_faces":terminal,"top_triangles":len(tri),"source_angle_deg":round(math.degrees(math.acos(min(1.,dot))),6),
        "resolution":resolution,"z_bounds":(zlo,zhi)}}
