"""Independent 2D geometry oracle for DWOS Surface-Driven step laboratory.

This solver NEVER changes the authored boundary or silently reduces a frame width.
Topology conflicts are returned as evidence; nonrectangular complex inset events
are explicitly blocked until a certified arrangement solver is available.
"""
from __future__ import annotations
import math

EPS=1.e-9


def sub(a,b):return (a[0]-b[0],a[1]-b[1])
def add(a,b):return (a[0]+b[0],a[1]+b[1])
def mul(a,s):return (a[0]*s,a[1]*s)
def cross(a,b):return a[0]*b[1]-a[1]*b[0]
def dot(a,b):return a[0]*b[0]+a[1]*b[1]
def length(a):return math.hypot(*a)
def signed_area(pts):
    return 0.5*sum(cross(p,q) for p,q in zip(pts,pts[1:]+pts[:1]))


def segment_intersection(a,b,c,d,eps=EPS):
    """Interior crossings or collinear overlap, not shared endpoint adjacency."""
    r=sub(b,a);s=sub(d,c);den=cross(r,s);t=sub(c,a)
    if abs(den)<=eps:
        if abs(cross(t,r))>eps:return None
        rr=dot(r,r)
        if rr<=eps**2:return {'type':'DEGENERATE_EDGE'}
        t0=dot(sub(c,a),r)/rr;t1=dot(sub(d,a),r)/rr
        lo=max(0.,min(t0,t1));hi=min(1.,max(t0,t1))
        if hi-lo>eps:return {'type':'COLLINEAR_OVERLAP','a':list(a),'b':list(b),'c':list(c),'d':list(d)}
        return None
    u=cross(t,r)/den;v=cross(t,s)/den
    if -eps<=u<=1+eps and -eps<=v<=1+eps:
        return {'type':'SEGMENT_TOUCH','point':list(add(a,mul(r,v))),'t':v,'u':u}
    return None


def find_self_crossings(points,eps=EPS):
    n=len(points);out=[]
    for i in range(n):
        for j in range(i+1,n):
            if (j-i)==1 or (i==0 and j==n-1):continue
            hit=segment_intersection(points[i],points[(i+1)%n],points[j],points[(j+1)%n],eps)
            if hit:out.append({'edges':[i,j],**hit})
    return out


def point_in_polygon(point,poly):
    x,y=point;inside=False
    for p,q in zip(poly,poly[1:]+poly[:1]):
        if (p[1]>y)!=(q[1]>y):
            x_at=p[0]+(q[0]-p[0])*(y-p[1])/(q[1]-p[1])
            if x<x_at:inside=not inside
    return inside


def point_segment_distance(p,a,b):
    e=sub(b,a);ll=dot(e,e)
    if ll<EPS**2:return length(sub(p,a))
    t=max(0.,min(1.,dot(sub(p,a),e)/ll))
    return length(sub(p,add(a,mul(e,t))))


def normalized_boundary(points):
    pts=[tuple(map(float,p)) for p in points]
    if len(pts)<3 or len(pts)>512:raise ValueError('BOUNDARY_VERTEX_COUNT')
    if any(len(p)!=2 or not all(math.isfinite(v) for v in p) for p in pts):raise ValueError('INVALID_COORDINATES')
    if length(sub(pts[0],pts[-1]))<EPS:pts.pop()
    if any(length(sub(a,b))<EPS for a,b in zip(pts,pts[1:]+pts[:1])):
        raise ValueError('ZERO_LENGTH_SOURCE_EDGE')
    crossings=find_self_crossings(pts)
    if crossings:raise ValueError('SOURCE_SELF_INTERSECTION:'+str(crossings[:2]))
    area=signed_area(pts)
    if abs(area)<EPS:raise ValueError('ZERO_SOURCE_AREA')
    if area<0:pts.reverse()
    return pts


def safe_inset(points,width):
    """Parallel edge offset + independent topology check. No width changes.

    For topological collapse / split (nontrivial skeleton event) returns BLOCKED
    with a witness instead of producing invalid polygons. Near-collinear edges
    are handled via supporting line intersection with no source simplification.
    """
    pts=normalized_boundary(points)
    w=float(width)
    if not math.isfinite(w) or w<=EPS:raise ValueError('INVALID_INSET_WIDTH')
    n=len(pts);dirs=[];offset_points=[]
    for a,b in zip(pts,pts[1:]+pts[:1]):
        e=sub(b,a);ln=length(e);dirs.append(mul(e,1/ln))
        offset_points.append(add(a,(-w*e[1]/ln,w*e[0]/ln)))
    inner=[]
    for i in range(n):
        j=(i-1)%n
        p=offset_points[j];r=dirs[j]
        q=offset_points[i];s=dirs[i]
        den=cross(r,s)
        if abs(den)<1e-10:
            if dot(r,s)<0:raise ValueError('INSET_BACKTRACK_EDGE:'+str(i))
            # Topological edge event: a same-direction collinear source edge
            # contributes a zero-length INNER edge at the previous inset
            # intersection. Keep both authored OUTER vertices; collapse only
            # the GENERATED inner edge in the polygon graph.
            inner.append(inner[-1] if inner else q)
            continue
        t=cross(sub(q,p),s)/den
        v=add(p,mul(r,t))
        if not all(math.isfinite(k) for k in v):raise ValueError('NONFINITE_INSET_VERTEX')
        inner.append(v)
    # Collapse coincident GENERATED vertices, keeping an explicit original
    # vertex -> inner vertex map. No changes to authored source polygon.
    mapping=[];reduced=[]
    for p in inner:
        if reduced and length(sub(p,reduced[-1]))<1e-8:
            mapping.append(len(reduced)-1)
        else:
            mapping.append(len(reduced));reduced.append(p)
    if len(reduced)>2 and length(sub(reduced[0],reduced[-1]))<1e-8:
        last=len(reduced)-1
        mapping=[0 if i==last else i for i in mapping]
        reduced.pop()
    if len(reduced)<3:raise ValueError('INSET_TOPOLOGY_COLLAPSE: fewer than 3 vertices')
    inner=reduced
    witness={'width':w,'source_n':n,'inset_n':len(inner),'inset_area':signed_area(inner)}
    if signed_area(inner)<=EPS:
        raise ValueError('INSET_TOPOLOGY_COLLAPSE:'+str(witness))
    crossings=find_self_crossings(inner)
    if crossings:raise ValueError('INSET_SELF_INTERSECTION:'+str(crossings[:3]))
    for i,p in enumerate(inner):
        if not point_in_polygon(p,pts):
            raise ValueError('INSET_ESCAPES_SOURCE:'+str({'vertex':i,'point':p,**witness}))
        d=min(point_segment_distance(p,a,b) for a,b in zip(pts,pts[1:]+pts[:1]))
        # Offset corners at acute convex angles are farther than w, never closer.
        if d+1e-7<w:
            raise ValueError('INSET_WIDTH_CONFLICT:'+str({'vertex':i,'clearance':d,**witness}))
    for i,(p,q) in enumerate(zip(inner,inner[1:]+inner[:1])):
        if length(sub(q,p))<1e-8:
            raise ValueError('INSET_TOPOLOGY_EVENT_SHORT_EDGE:'+str({'edge':i,**witness}))
        mid=mul(add(p,q),.5)
        if not point_in_polygon(mid,pts):raise ValueError('INSET_EDGE_OUTSIDE_SOURCE:'+str({'edge':i,**witness}))
        for j,(a,b) in enumerate(zip(pts,pts[1:]+pts[:1])):
            h=segment_intersection(p,q,a,b)
            if h:raise ValueError('INSET_CROSSES_SOURCE:'+str({'inner_edge':i,'outer_edge':j,'hit':h}))
    return {'outer':pts,'inner':inner,'outer_to_inner':mapping,'width':w,
            'topology':'GENERATED_EDGE_COLLAPSE' if len(inner)!=len(pts) else 'SIMPLE_ONE_TO_ONE',
            'source_area':signed_area(pts),'inner_area':signed_area(inner)}


def rectangle_axes(poly,tol=1e-5):
    """Read-only rectangular recognition with arbitrary collinear edge splits.

    Unlike the legacy four-vertex check, this validates EVERY edge as a
    segment on one bounding-box side and requires all four corners. A concave
    polygon/diagonal is never accepted by simply matching the bounding box.
    Returned axes are classifier coordinates, not a destructive repair.
    """
    pts=normalized_boundary(poly)
    if len(pts)<4:return None
    xs=[p[0] for p in pts];ys=[p[1] for p in pts]
    lo_x,hi_x=min(xs),max(xs);lo_y,hi_y=min(ys),max(ys)
    width=hi_x-lo_x;height=hi_y-lo_y
    if min(width,height)<max(1e-6,4*tol):return None
    corners=((lo_x,lo_y),(hi_x,lo_y),(hi_x,hi_y),(lo_x,hi_y))
    if not all(any(length(sub(p,c))<=tol for p in pts) for c in corners):return None
    for a,b in zip(pts,pts[1:]+pts[:1]):
        if not (any(abs(a[0]-x)<=tol and abs(b[0]-x)<=tol for x in (lo_x,hi_x))
                or any(abs(a[1]-y)<=tol and abs(b[1]-y)<=tol for y in (lo_y,hi_y))):
            return None
    # Necessary area witness: all four sides have to enclose the full box.
    if abs(signed_area(pts)-width*height)>tol*2*(width+height):return None
    return lo_x,hi_x,lo_y,hi_y


def metric_uv_audit(vertices,faces,roles,uvs,resolution,parts=None,tolerance=0.002):
    """Independent edge-length UV audit; ignore intentionally unmapped roles.

    1000 px/m applies to frame/mullion faces, not glass/interior. Shared
    FRONT vertex UVs are compared across faces as a seam witness. Long spans
    are noted for texture repetition, not silently rescaled or called UDIM.
    """
    result={'target_px_m':1000.,'texture_resolution':int(resolution),
            'measured_edges':0,'bad_edges':[],'min_px_m':None,'max_px_m':None,
            'front_seam_edges':[],'uv_outside_unit_tile':False,
            'unmapped_faces':0,'front_faces':0,'front_uv_mirror_faces':[]}
    front={}; densities=[]
    for fi,(face,role,uv) in enumerate(zip(faces,roles,uvs)):
        if role not in ('FRAME','MULLION'):
            result['unmapped_faces']+=1
            continue
        if uv is None or len(uv)!=len(face):
            result['bad_edges'].append({'face':fi,'reason':'MISSING_UV','role':role});continue
        part=parts[fi] if parts is not None else None
        if part=='FRONT':
            result['front_faces']+=1
            oriented_uv_area=sum(a[0]*b[1]-b[0]*a[1]
                                 for a,b in zip(uv,uv[1:]+uv[:1]))
            if oriented_uv_area<0:
                result['front_uv_mirror_faces'].append(fi)
            for vid,co in zip(face,uv):
                key=tuple(round(float(x),10) for x in vertices[vid])
                prior=front.get(key)
                if prior is not None and math.dist(prior,co)>1.e-6:
                    result['front_seam_edges'].append({'face':fi,'vertex':vid,'prior_uv':prior,'uv':co})
                front[key]=co
        if any(u<-1.e-6 or u>1+1.e-6 or v<-1.e-6 or v>1+1.e-6 for u,v in uv):
            result['uv_outside_unit_tile']=True
        for va,vb,ua,ub in zip(face,face[1:]+face[:1],uv,uv[1:]+uv[:1]):
            d=math.dist(vertices[va],vertices[vb])
            if d<=1.e-8:continue
            density=math.dist(ua,ub)*float(resolution)/d
            densities.append(density)
            if abs(density-1000.)>1000.*tolerance:
                result['bad_edges'].append({'face':fi,'role':role,'part':part,
                                             'edge':[va,vb],'px_m':round(density,6)})
    result['measured_edges']=len(densities)
    if densities:
        result['min_px_m']=round(min(densities),6)
        result['max_px_m']=round(max(densities),6)
    result['bad_edge_count']=len(result['bad_edges'])
    result['front_seam_count']=len(result['front_seam_edges'])
    result['front_uv_mirror_count']=len(result['front_uv_mirror_faces'])
    result['tile_sampling_warning']=('UV_OUTSIDE_UNIT_TILE: REPEAT vs UDIM not verified'
                                     if result['uv_outside_unit_tile'] else None)
    return result


def uv_density(p,q,uv_p,uv_q,resolution=4096):
    a=length(sub(p,q));b=length(sub(uv_p,uv_q))
    if a<=EPS:raise ValueError('DEGENERATE_UV_EDGE')
    return b*resolution/a


def mesh_topology_stats(verts,faces):
    edges={};bad=[]
    for fi,f in enumerate(faces):
        if len(set(f))<3:bad.append(fi)
        for a,b in zip(f,f[1:]+f[:1]):
            key=tuple(sorted((a,b)));edges[key]=edges.get(key,0)+1
    return {'vertices':len(verts),'faces':len(faces),'edges':len(edges),
            'boundary_edges':sum(v==1 for v in edges.values()),
            'nonmanifold_edges':sum(v>2 for v in edges.values()),
            'degenerate_faces':bad}


def mesh_orientation_connectivity_audit(vertices,faces,roles=None,selected_roles=None):
    """Independent face-index oracle: incidence, winding, components and volume.

    Vertex-index connectivity, not approximate/coincident coordinate matching.
    Signed volume is meaningful only when boundary_edges == 0.
    """
    if roles is None:roles=['ALL']*len(faces)
    eligible=[i for i,r in enumerate(roles) if selected_roles is None or r in selected_roles]
    edge_uses={};adj={i:set() for i in eligible}
    for fi in eligible:
        face=faces[fi]
        if len(face)<3 or len(set(face))<3:
            raise ValueError(f'ORACLE_DEGENERATE_FACE:{fi}')
        for a,b in zip(face,face[1:]+face[:1]):
            edge_uses.setdefault(tuple(sorted((a,b))),[]).append((fi,a,b))
    for edge,uses in edge_uses.items():
        for i in range(len(uses)):
            for j in range(i+1,len(uses)):
                adj[uses[i][0]].add(uses[j][0]);adj[uses[j][0]].add(uses[i][0])
    unvisited=set(eligible);components=[]
    while unvisited:
        start=unvisited.pop();stack=[start];count=1
        while stack:
            for other in adj[stack.pop()]:
                if other in unvisited:
                    unvisited.remove(other);stack.append(other);count+=1
        components.append(count)
    boundaries=[list(edge) for edge,uses in edge_uses.items() if len(uses)==1]
    nonmanifold=[list(edge) for edge,uses in edge_uses.items() if len(uses)>2]
    winding=[{'edge':list(edge),'faces':[uses[0][0],uses[1][0]]}
             for edge,uses in edge_uses.items()
             if len(uses)==2 and uses[0][1:]==uses[1][1:]]
    volume=None
    if not boundaries and not nonmanifold and eligible:
        origin=vertices[faces[eligible[0]][0]]
        v=0.
        for fi in eligible:
            points=[tuple(vertices[vid][k]-origin[k] for k in range(3)) for vid in faces[fi]]
            for j in range(1,len(points)-1):
                a,b,c=points[0],points[j],points[j+1]
                v+=(a[0]*(b[1]*c[2]-b[2]*c[1]) +
                    a[1]*(b[2]*c[0]-b[0]*c[2]) +
                    a[2]*(b[0]*c[1]-b[1]*c[0]))/6.
        volume=v
    return {'components':len(components),'component_face_counts':sorted(components,reverse=True),
            'eligible_faces':len(eligible),'edges':len(edge_uses),
            'boundary_edges':len(boundaries),'boundary_witnesses':boundaries[:12],
            'nonmanifold_edges':len(nonmanifold),'nonmanifold_witnesses':nonmanifold[:12],
            'winding_conflicts':len(winding),'winding_witnesses':winding[:12],
            'signed_volume_m3':float(volume) if volume is not None else None}
