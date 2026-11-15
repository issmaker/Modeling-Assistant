"""Universal Surface Construction Engine: first *constructive* profile ribbon.

Pure Python, deterministic, read-only with respect to all caller data. Builds
actual vertices/faces for a planar source perimeter, in world-length units.
Unlike a patch classification, returned topology has explicit bevel triangles.

This is a profile surface candidate, NOT an already validated shared tool window:
application still requires HP/LP/Glass/Interior, texture, collision, contact,
and publish/Undo checks. Non-planar contours fail closed.
"""
from __future__ import annotations
from collections import Counter
from itertools import combinations
from math import acos, degrees, dist, isfinite, sqrt

SCHEMA='ISSMAKER_PROFILE_JOIN_SOLVER_V1'
EPS=1.0e-10


def _sub(a,b):return tuple(a[i]-b[i] for i in range(3))
def _add(a,b):return tuple(a[i]+b[i] for i in range(3))
def _mul(a,t):return tuple(x*t for x in a)
def _dot(a,b):return sum(a[i]*b[i] for i in range(3))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _length(a):return sqrt(max(0.,_dot(a,a)))
def _normal(a):
    m=_length(a)
    if m<=EPS:raise ValueError('PROFILE_ZERO_VECTOR')
    return _mul(a,1./m)


def _plane_normal(points):
    nx=ny=nz=0.
    for a,b in zip(points,points[1:]+points[:1]):
        nx+=(a[1]-b[1])*(a[2]+b[2])
        ny+=(a[2]-b[2])*(a[0]+b[0])
        nz+=(a[0]-b[0])*(a[1]+b[1])
    return _normal((nx,ny,nz))


def _basis(points,normal):
    for a,b in zip(points,points[1:]+points[:1]):
        d=_sub(b,a)
        d=_sub(d,_mul(normal,_dot(normal,d)))
        if _length(d)>EPS:
            u=_normal(d)
            return u,_cross(normal,u)
    raise ValueError('PROFILE_DEGENERATE_PERIMETER')


def _orient(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _strict_cross(a,b,c,d,eps=1.e-11):
    q=(_orient(a,b,c),_orient(a,b,d),_orient(c,d,a),_orient(c,d,b))
    return (q[0]*q[1]<-eps and q[2]*q[3]<-eps)


def _on_segment(a,b,p,eps):
    return (abs(_orient(a,b,p))<=eps and
            min(a[0],b[0])-eps<=p[0]<=max(a[0],b[0])+eps and
            min(a[1],b[1])-eps<=p[1]<=max(a[1],b[1])+eps)


def _inside_triangle(p,a,b,c,eps=1.e-11):
    t=(_orient(a,b,p),_orient(b,c,p),_orient(c,a,p))
    return min(t)>eps or max(t)<-eps


def _validate_mesh(vertices,faces,outer_count,normal,side,width):
    """Exact in-plane consistency + triangle-overlap guard, not UV approval."""
    edge_count=Counter();direction=Counter();triangles=[]
    area_sum=0.
    origin=vertices[0]
    u,v=_basis(vertices[:outer_count],normal)
    uv=[(_dot(_sub(point,origin),u),_dot(_sub(point,origin),v)) for point in vertices]
    for fi,face in enumerate(faces):
        if len(face) not in (3,4) or len(set(face))!=len(face):
            raise ValueError('PROFILE_INVALID_FACE')
        for a,b in zip(face,face[1:]+face[:1]):
            if dist(vertices[a],vertices[b])<1e-8:raise ValueError('PROFILE_MICRO_EDGE')
            key=tuple(sorted((a,b)))
            edge_count[key]+=1;direction[(a,b)]+=1
        # Quads must consist of two positive triangles, not just have a
        # positive signed aggregate (which also admits self-folded bow-ties).
        pairs=([(face[0],face[1],face[2]),(face[0],face[2],face[3])]
               if len(face)==4 else [tuple(face)])
        for tri in pairs:
            area=_orient(*(uv[i] for i in tri))*.5*side
            if area<=max(1.e-11,width*width*1.e-9):
                raise ValueError('PROFILE_FOLDED_FACE:'+str(fi))
            triangles.append((fi,tri));area_sum+=area
    if any(n>2 for n in edge_count.values()):raise ValueError('PROFILE_NONMANIFOLD')
    if any(direction[(a,b)]==direction[(b,a)]==0 for a,b in edge_count):
        raise ValueError('PROFILE_MISSING_EDGE')
    if any(edge_count[tuple(sorted((i,(i+1)%outer_count)))]!=1 for i in range(outer_count)):
        raise ValueError('PROFILE_SOURCE_PERIMETER_CHANGED')
    if any(edge_count[e]==2 and direction[(e[0],e[1])]!=1 for e in edge_count):
        raise ValueError('PROFILE_WINDING_CONFLICT')
    for ia,(fi,ta) in enumerate(triangles):
        va=[uv[i] for i in ta]
        for fj,tb in triangles[ia+1:]:
            if fi==fj:continue
            vb=[uv[i] for i in tb]
            if any(_strict_cross(va[i],va[(i+1)%3],vb[j],vb[(j+1)%3])
                   for i in range(3) for j in range(3)):
                raise ValueError('PROFILE_OVERLAPPING_FACES:'+str((fi,fj)))
            # Strict vertex-in-triangle captures complete containment.
            if any(_inside_triangle(p,*vb) for p in va) or any(_inside_triangle(p,*va) for p in vb):
                raise ValueError('PROFILE_OVERLAPPING_FACES:'+str((fi,fj)))
    boundary=[e for e,n in edge_count.items() if n==1]
    return {'face_count':len(faces),'triangle_count':len(triangles),
            'boundary_edge_count':len(boundary),'signed_area_m2':area_sum,
            'nonmanifold_edges':0,'overlapping_triangles':0,
            'source_outer_edges_preserved':outer_count}


def _collapse_position(points,tangents,normals,normal,index,width,miter_limit):
    """Intersect surviving offset lines around a swallowed short source edge.

    Retains original boundary edge, removes only its INNER offset segment.
    This is polygon erosion topology, not a silent change of width: adjacent
    surviving offset segments each remain exactly the requested width.
    """
    n=len(points);a=index;b=(index+1)%n
    previous=(index-1)%n
    origin_a=_add(points[a],_mul(normals[previous],width))
    origin_b=_add(points[b],_mul(normals[b],width))
    ta=tangents[previous];tb=tangents[b]
    denominator=_dot(_cross(ta,tb),normal)
    if abs(denominator)<1.e-7:
        raise ValueError('PROFILE_COLLAPSE_PARALLEL_ADJACENT_LINES')
    distance_a=_dot(_cross(_sub(origin_b,origin_a),tb),normal)/denominator
    point=_add(origin_a,_mul(ta,distance_a))
    max_extension=width*float(miter_limit)
    if max(dist(point,points[a]),dist(point,points[b]))>max_extension:
        raise ValueError('PROFILE_COLLAPSE_EXCEEDS_MITER_BUDGET')
    for source,normal_segment in ((points[a],normals[previous]),(points[b],normals[b])):
        error=abs(_dot(_sub(point,source),normal_segment)-width)
        if error>max(1.e-8,width*1.e-6):
            raise ValueError('PROFILE_COLLAPSE_LOST_ADJACENT_WIDTH')
    return point


def construct(points,width,*,join='AUTO',miter_limit=4.,plane_normal=None,
              side=None,planarity_tolerance_m=1.e-5,collapse_edges=()):
    """Build a safe closed perimeter band and independently validate it.

    points are ordered, on ONE planar facade plane. The original vertices are
    kept 1:1 at the beginning of the output. width is a physical perpendicular
    offset from every input boundary segment to its corresponding inner segment.
    join=MITER, BEVEL, AUTO. AUTO bevels outward bends if miter exceeds limit.
    """
    vertices=[tuple(float(x) for x in p) for p in points]
    if vertices and dist(vertices[0],vertices[-1])<EPS:vertices.pop()
    if len(vertices)<3 or len(vertices)>128:raise ValueError('PROFILE_VERTEX_COUNT')
    if any(len(v)!=3 or not all(isfinite(x) for x in v) for v in vertices):
        raise ValueError('PROFILE_INVALID_COORDINATE')
    if not isfinite(float(width)) or not 1.e-6<float(width)<=1000:
        raise ValueError('PROFILE_INVALID_WIDTH')
    if join not in ('AUTO','MITER','BEVEL'):
        raise ValueError('PROFILE_INVALID_JOIN_MODE')
    if not 1.0<=float(miter_limit)<=64.:
        raise ValueError('PROFILE_INVALID_MITER_LIMIT')
    normal=(_normal(tuple(float(x) for x in plane_normal))
            if plane_normal is not None else _plane_normal(vertices))
    origin=vertices[0]
    deviation=max(abs(_dot(_sub(p,origin),normal)) for p in vertices)
    if deviation>planarity_tolerance_m:raise ValueError('PROFILE_NONPLANAR_SURFACE')
    u,v=_basis(vertices,normal)
    projected=[(_dot(_sub(p,origin),u),_dot(_sub(p,origin),v)) for p in vertices]
    area=sum(a[0]*b[1]-b[0]*a[1]
             for a,b in zip(projected,projected[1:]+projected[:1]))*.5
    if abs(area)<1.e-10:raise ValueError('PROFILE_ZERO_AREA')
    orientation=1 if area>0 else -1
    offset_side=orientation if side is None else int(side)
    if offset_side not in (-1,1):raise ValueError('PROFILE_INVALID_SIDE')
    if offset_side!=orientation:
        raise ValueError('PROFILE_OUTER_OFFSET_REQUIRES_SEPARATE_CONTRACT')
    n=len(vertices)
    t=[];normals=[]
    for i in range(n):
        delta=_sub(vertices[(i+1)%n],vertices[i]);distance=_length(delta)
        if distance<1.e-7:raise ValueError('PROFILE_ZERO_SOURCE_EDGE')
        tang=_mul(delta,1./distance)
        if abs(_dot(tang,normal))>max(1.e-8,planarity_tolerance_m/distance):
            raise ValueError('PROFILE_NONPLANAR_SEGMENT')
        t.append(tang)
        normals.append(_mul(_cross(normal,tang),offset_side))
    collapsed=tuple(sorted(set(int(i) for i in collapse_edges)))
    if any(i<0 or i>=n for i in collapsed):raise ValueError('PROFILE_INVALID_COLLAPSE_EDGE')
    endpoints={vertex for i in collapsed for vertex in (i,(i+1)%n)}
    if len(endpoints)!=2*len(collapsed):
        raise ValueError('PROFILE_ADJACENT_COLLAPSES_FORBIDDEN')
    if any(dist(vertices[i],vertices[(i+1)%n])>float(width)*1.5 for i in collapsed):
        raise ValueError('PROFILE_COLLAPSE_EDGE_TOO_LONG')
    all_vertices=list(vertices)
    arrival=[-1]*n;departure=[-1]*n;joins=[]
    for i in range(n):
        prev=(i-1)%n
        turn=_dot(_cross(t[prev],t[i]),normal)*offset_side
        cosine=max(-1.,min(1.,_dot(t[prev],t[i])))
        angle=degrees(acos(cosine))
        normals_sum=_add(normals[prev],normals[i]);den=_dot(normals_sum,normals[prev])
        ratio=(_length(normals_sum)/den if den>1.e-12 else float('inf'))
        outward=turn<-1.e-8
        if i in endpoints:
            joins.append({'source_vertex':i,'strategy':'COLLAPSED_INNER_STATION',
                          'angle_deg':round(angle,6),'miter_scale':None,
                          'outward':outward,'generated_vertices':0})
            continue
        chosen=('BEVEL' if outward and
                (join=='BEVEL' or join=='AUTO' and ratio>float(miter_limit))
                else 'MITER')
        if join=='MITER' and ratio>float(miter_limit):
            raise ValueError('PROFILE_MITER_LIMIT_EXCEEDED:'+str(i))
        if not isfinite(ratio) and chosen!='BEVEL':
            raise ValueError('PROFILE_OPPOSITE_SEGMENT_NORMALS')
        if chosen=='MITER':
            if ratio>float(miter_limit):
                raise ValueError('PROFILE_INWARD_MITER_LIMIT_EXCEEDED:'+str(i))
            contact=_add(vertices[i],_mul(normals_sum,float(width)/den))
            index=len(all_vertices);all_vertices.append(contact)
            arrival[i]=index;departure[i]=index
        else:
            ia=len(all_vertices);all_vertices.append(_add(vertices[i],_mul(normals[prev],float(width))))
            ib=len(all_vertices);all_vertices.append(_add(vertices[i],_mul(normals[i],float(width))))
            arrival[i]=ia;departure[i]=ib
        joins.append({'source_vertex':i,'strategy':chosen,'angle_deg':round(angle,6),
                      'miter_scale':round(ratio,6) if isfinite(ratio) else None,
                      'outward':outward,'generated_vertices':1 if chosen=='MITER' else 2})
    for index in collapsed:
        point=_collapse_position(vertices,t,normals,normal,index,float(width),float(miter_limit))
        vid=len(all_vertices);all_vertices.append(point)
        next_index=(index+1)%n
        arrival[index]=departure[index]=vid
        arrival[next_index]=departure[next_index]=vid
    if any(i<0 for i in arrival+departure):
        raise ValueError('PROFILE_UNASSIGNED_INNER_STATION')
    faces=[];roles=[]
    for i in range(n):
        face=[i,(i+1)%n,arrival[(i+1)%n],departure[i]]
        if face[-1]==face[-2]:face.pop()
        if offset_side==-1:face.reverse()
        faces.append(face);roles.append('PROFILE_BAND')
    for i,j in enumerate(joins):
        if j['strategy']=='BEVEL':
            face=[i,departure[i],arrival[i]]
            if offset_side==-1:face.reverse()
            faces.append(face);roles.append('PROFILE_BEVEL')
    qa=_validate_mesh(all_vertices,faces,n,normal,offset_side,float(width))
    # Independent perpendicular-distance check for both ends of every
    # offset segment. This prevents an easy 'success' via width reduction.
    max_width_error=0.
    for i in range(n):
        if i in collapsed:continue  # edge swallowed by exact polygon offset
        j=(i+1)%n;normal_i=normals[i]
        for src,inner in ((vertices[i],all_vertices[departure[i]]),
                          (vertices[j],all_vertices[arrival[j]])):
            error=abs(_dot(_sub(inner,src),normal_i)-float(width))
            max_width_error=max(max_width_error,error)
    if max_width_error>max(1.e-8,float(width)*1.e-6):
        raise ValueError('PROFILE_WIDTH_CONSTRAINT_VIOLATED')
    return {'schema':SCHEMA,'vertices':all_vertices,'faces':faces,'roles':roles,
            'source_vertex_count':n,'source_vertices_unchanged':True,
            'outer_vertex_indices':list(range(n)),
            'inner_vertex_indices':[departure[i] for i in range(n)],
            'join_records':joins,'join_mode':join,'width_m':float(width),
            'max_width_error_m':max_width_error,'plane_normal':normal,
            'consumed_source_edge_indices':list(collapsed),
            'surviving_edges_with_exact_width':n-len(collapsed),
            'source_planarity_error_m':deviation,'validation':qa}


def search(points,width,*,plane_normal=None,max_miter_scale=4.):
    """Rank distinct real mesh candidates, no sampling or silent width change."""
    # Reject a non-planar source before generating speculative planar joins.
    # Curved facade strips require a separate local-surface construction
    # strategy and must never be silently projected into one plane.
    contour=[tuple(float(x) for x in p) for p in points]
    if contour and dist(contour[0],contour[-1])<EPS:contour.pop()
    if len(contour)>128:raise ValueError('PROFILE_VERTEX_COUNT')
    if len(contour)>=3 and all(len(p)==3 and all(isfinite(x) for x in p) for p in contour):
        normal=(_normal(tuple(float(v) for v in plane_normal))
                if plane_normal is not None else _plane_normal(contour))
        origin=contour[0]
        if max(abs(_dot(_sub(p,origin),normal)) for p in contour)>1.e-5:
            raise ValueError('PROFILE_NONPLANAR_SURFACE')
    histories=[];solutions=[]
    for join in ('MITER','AUTO','BEVEL'):
        try:
            result=construct(points,width,join=join,plane_normal=plane_normal,
                             miter_limit=max_miter_scale)
            # Prefer fewer extra faces if both shapes prove collision-free.
            score=(sum(j['strategy']=='BEVEL' for j in result['join_records']),
                   len(result['faces']),join)
            histories.append({'candidate':join,'state':'VALID',
                              'faces':len(result['faces'])})
            solutions.append((score,result))
        except ValueError as exc:
            histories.append({'candidate':join,'state':'REJECTED',
                              'reason':str(exc)[:170]})
    if not solutions and len(points)<=80:
        # Preserve every SOURCE edge. A short edge may vanish from the INNER
        # offset boundary when width exceeds local feature size. Search a
        # small, deterministic combination of local inner-station collapses.
        count=len(points)-(1 if len(points)>2 and dist(points[0],points[-1])<EPS else 0)
        small=sorted((dist(points[i],points[(i+1)%count]),i) for i in range(count)
                     if dist(points[i],points[(i+1)%count])<float(width)*1.5)
        candidates=[i for _,i in small[:9]]
        attempts=0
        for size in (1,2,3):
            for combination in combinations(candidates,size):
                if attempts>=55:break
                attempts+=1
                try:
                    candidate=construct(points,width,join='AUTO',plane_normal=plane_normal,
                                        miter_limit=max_miter_scale,
                                        collapse_edges=combination)
                    score=(len(combination),len(candidate['faces']),tuple(sorted(combination)))
                    histories.append({'candidate':'INNER_COLLAPSE_'+str(combination),
                                      'state':'VALID','faces':len(candidate['faces'])})
                    solutions.append((score,candidate))
                except ValueError as error:
                    histories.append({'candidate':'INNER_COLLAPSE_'+str(combination),
                                      'state':'REJECTED','reason':str(error)[:120]})
            if solutions or attempts>=55:break
    if not solutions:
        raise ValueError('PROFILE_SOLVER_NO_VALID_CANDIDATE: '+str(histories[-12:]))
    solutions.sort(key=lambda row:row[0]);winner=solutions[0][1]
    winner['search_history']=histories
    winner['chosen_strategy']=winner['join_mode']
    return winner
