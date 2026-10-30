"""DWOS 3.7.3: world-XY corner mitre for UPRIGHT PLANAR BOX interiors.

A contact in an architectural 90-degree corner usually lives in the horizontal
footprints of two open-front vertical prisms. A half-space split in XY produces
nonrectangular, but fully manifold, 3D Interior geometry. Neither FRAME nor
GLASS is touched. An absent exact footprint is never approximated.

All predicates, clipping and rebuilding are pure Python, without Blender APIs.
This intentionally excludes CURVED/CORNER, tilted windows, and non-prismatic
meshes. Those are handled by the existing safe solver/PENDING path.
"""
from __future__ import annotations
import math

TOL = 1e-6
MAX_FRONT_TRIM_M = 0.025  # maximum *total* shortening per aperture (m)
MIN_FOOTPRINT_RETAINED = 0.25


def _dist(a,b):return math.hypot(a[0]-b[0],a[1]-b[1])
def _dot(a,b):return a[0]*b[0]+a[1]*b[1]
def _cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def _area(poly):return 0.5*sum(poly[i][0]*poly[(i+1)%len(poly)][1]-poly[(i+1)%len(poly)][0]*poly[i][1] for i in range(len(poly)))


def _on_segment(p,a,b):
    ab=(b[0]-a[0],b[1]-a[1]);ap=(p[0]-a[0],p[1]-a[1])
    d2=_dot(ab,ab)
    return d2>TOL*TOL and abs(ab[0]*ap[1]-ab[1]*ap[0])<=TOL*math.sqrt(d2) and -TOL<=_dot(ap,ab)/d2<=1+TOL


def _convex(poly):
    turns=[_cross(poly[i],poly[(i+1)%len(poly)],poly[(i+2)%len(poly)]) for i in range(len(poly))]
    return all(v>=-TOL*TOL for v in turns) and sum(v>TOL*TOL for v in turns)>=3


def _simplify_loop(poly):
    """Remove collinear internal station vertices from a continuous boundary."""
    pts=list(poly)
    for _ in range(len(pts)):
        if len(pts)<=3:break
        removable=[]
        for i,p in enumerate(pts):
            q=pts[(i-1)%len(pts)];r=pts[(i+1)%len(pts)]
            if _dist(q,r)<=TOL or (abs(_cross(q,p,r))<=TOL*_dist(q,r) and _on_segment(p,q,r)):
                removable.append(i)
        if not removable:break
        # Removing all collinear points simultaneously can destroy a polygon.
        pts.pop(removable[0])
    return pts


def _perimeter(face_rows,verts):
    """Boundary of one continuous, simply connected horizontal cap."""
    count={};
    for face in face_rows:
        for i,a in enumerate(face):
            b=face[(i+1)%len(face)];key=tuple(sorted((a,b)))
            count[key]=count.get(key,0)+1
            if count[key]>2:return None
    edges=[key for key,c in count.items() if c==1]
    if len(edges)<3 or len(edges)>512:return None
    adjacency={}
    for a,b in edges:
        adjacency.setdefault(a,[]).append(b)
        adjacency.setdefault(b,[]).append(a)
    if any(len(v)!=2 for v in adjacency.values()):return None
    start=min(adjacency);order=[];prev=None;cur=start
    while len(order)<len(adjacency):
        order.append(cur)
        nxt=[i for i in adjacency[cur] if i!=prev]
        if not nxt:return None
        next_id=min(nxt) if prev is None else nxt[0]
        prev,cur=cur,next_id
        if cur==start:break
    if cur!=start or len(order)!=len(adjacency):return None
    return [tuple(verts[i][:2]) for i in order]


def footprint(mesh):
    """Exact XY footprint of ONE upright open-front prism, or None.

    Supports both simple 8-vertex PLANAR BOX and multi-station PLANAR walls.
    Multiple strips must form a single continuous cap without gaps or holes.
    No concave/curved/tilted approximation is ever attempted.
    """
    verts=mesh.get('verts',());faces=mesh.get('faces',());roles=mesh.get('roles',())
    if len(verts)<6 or len(faces)<4 or len(faces)!=len(roles):return None
    zlo=min(v[2] for v in verts);zhi=max(v[2] for v in verts)
    if zhi-zlo<.1:return None
    if any(min(abs(v[2]-zlo),abs(v[2]-zhi))>TOL for v in verts):return None
    high=[f for f in faces if len(f)>=3 and all(abs(verts[i][2]-zhi)<TOL for i in f)]
    low=[f for f in faces if len(f)>=3 and all(abs(verts[i][2]-zlo)<TOL for i in f)]
    if not high or not low:return None
    raw=_perimeter(high,verts)
    bottom=_perimeter(low,verts)
    if raw is None or bottom is None:return None
    poly=_simplify_loop(raw)
    if len(poly)>18:return None
    if _area(poly)<0:poly.reverse()
    if len(poly)<3 or abs(_area(poly))<1e-6 or not _convex(poly):return None
    # Prevent accidentally filling a missing strip or internal hole.
    # Sum of top-face areas must equal the exact reconstructed footprint.
    top_area=sum(abs(_area([tuple(verts[i][:2]) for i in f])) for f in high)
    bottom_area=sum(abs(_area([tuple(verts[i][:2]) for i in f])) for f in low)
    area=abs(_area(poly))
    if abs(top_area-area)>max(1e-6,area*1e-5) or abs(bottom_area-area)>max(1e-6,area*1e-5):return None
    # Every footprint station exists at both elevations, no loose extra ribs.
    tops={(round(v[0],6),round(v[1],6)) for v in verts if abs(v[2]-zhi)<TOL}
    bottoms={(round(v[0],6),round(v[1],6)) for v in verts if abs(v[2]-zlo)<TOL}
    if tops!=bottoms:return None
    incidence={}
    for face in faces:
        for i,a in enumerate(face):
            b=face[(i+1)%len(face)];key=tuple(sorted((a,b)))
            incidence[key]=incidence.get(key,0)+1
            if incidence[key]>2:return None
    rim={v for (a,b),number in incidence.items() if number==1 for v in (a,b)}
    mouth=[]
    for i in rim:
        p=tuple(verts[i][:2])
        if all(_dist(p,q)>TOL for q in mouth):mouth.append(p)
    if len(mouth)<2:return None
    m0,m1=max(((p,q) for p in mouth for q in mouth if p!=q),key=lambda pair:_dist(*pair))
    if any(not _on_segment(p,m0,m1) for p in mouth):return None
    edges=[i for i in range(len(poly)) if _on_segment(poly[i],m0,m1) and _on_segment(poly[(i+1)%len(poly)],m0,m1)]
    if len(edges)!=1:return None
    rearpts=[]
    for face,role in zip(faces,roles):
        if role!='INTERIOR_BACK':continue
        for i in face:
            p=tuple(verts[i][:2])
            if all(_dist(p,q)>TOL for q in rearpts):rearpts.append(p)
    rear=None
    if len(rearpts)>=2:
        r0,r1=max(((p,q) for p in rearpts for q in rearpts if p!=q),key=lambda pair:_dist(*pair))
        if all(_on_segment(p,r0,r1) for p in rearpts):rear=(r0,r1)
    return {'poly':poly,'front':(m0,m1),'rear':rear,'zlo':zlo,'zhi':zhi}


def clip_polygon(poly,n,bound,*,keep_low):
    out=[];sense=1.0 if keep_low else -1.0
    def sd(p):return sense*(_dot(p,n)-bound)
    for idx,a in enumerate(poly):
        b=poly[(idx+1)%len(poly)];da=sd(a);db=sd(b)
        if da<=1e-9:out.append(a)
        if (da<-1e-9 and db>1e-9) or (da>1e-9 and db<-1e-9):
            t=da/(da-db);out.append((a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t))
    clean=[]
    for p in out:
        if not clean or _dist(clean[-1],p)>1e-8:clean.append(p)
    if len(clean)>1 and _dist(clean[0],clean[-1])<=1e-8:clean.pop()
    if len(clean)<3 or abs(_area(clean))<1e-7:return None
    if _area(clean)<0:clean.reverse()
    return clean if _convex(clean) else None


def mouth_trim(source,clipped):
    a,b=source['front'];L=_dist(a,b)
    if L<=.05:return None
    pts=[p for p in clipped if _on_segment(p,a,b)]
    if len(pts)!=2:return None
    remain=_dist(pts[0],pts[1])
    if remain<max(.05,L*.60):return None
    return max(0.0,L-remain)


def rebuild_prism(source,poly):
    """Create outward-consistent top/bottom polygons and side walls.

    The one unbuilt vertical footprint edge is the (possibly locally shortened)
    front aperture; no extra front cap is ever inserted.
    """
    n=len(poly);mouth=source['front'];zlo=source['zlo'];zhi=source['zhi']
    front=[i for i in range(n) if _on_segment(poly[i],*mouth) and _on_segment(poly[(i+1)%n],*mouth)]
    if len(front)!=1:raise ValueError('Footprint miter lost the open front edge')
    verts=[(x,y,zlo) for x,y in poly]+[(x,y,zhi) for x,y in poly]
    faces=[tuple(reversed(range(n))),tuple(range(n,2*n))]
    roles=['INTERIOR_REVEAL','INTERIOR_REVEAL']
    original=source['poly']
    for i in range(n):
        if i==front[0]:continue
        a,b=poly[i],poly[(i+1)%n]
        # Rebuilt side faces are all planar vertical quads; clip-generated
        # interior diagonals get their own material/diagnostic role.
        old_edge=any(_on_segment(a,original[j],original[(j+1)%len(original)]) and
                     _on_segment(b,original[j],original[(j+1)%len(original)])
                     for j in range(len(original)))
        if source.get('rear') and _on_segment(a,*source['rear']) and _on_segment(b,*source['rear']):
            role='INTERIOR_BACK'
        else:
            role='INTERIOR_REVEAL' if old_edge else 'INTERIOR_SMART_CONTACT'
        faces.append((i,(i+1)%n,(i+1)%n+n,i+n));roles.append(role)
    return {'verts':verts,'faces':faces,'roles':roles}


def mitre_pair(a,b,*,clearance,original_a=None,original_b=None):
    """Adapt two upright planar cavity footprints with exact XY offset gap.

    Returns both new world meshes or None. A/B polygons lie in opposing closed
    half-spaces separated by clearance, therefore their 3D surfaces cannot
    intersect (even when the original horizontal footprints overlapped).
    """
    fa=footprint(a);fb=footprint(b)
    oa=footprint(original_a) if original_a is not None else fa
    ob=footprint(original_b) if original_b is not None else fb
    if not all((fa,fb,oa,ob)):return None
    if min(fa['zhi'],fb['zhi'])-max(fa['zlo'],fb['zlo'])<=TOL:return None
    ea=(fa['front'][1][0]-fa['front'][0][0],fa['front'][1][1]-fa['front'][0][1])
    eb=(fb['front'][1][0]-fb['front'][0][0],fb['front'][1][1]-fb['front'][0][1])
    if abs(ea[0]*eb[1]-ea[1]*eb[0]) <= .3*math.hypot(*ea)*math.hypot(*eb):
        return None  # corner solver, not a generic parallel-window overlap hack
    original_area_a=abs(_area(oa['poly']));original_area_b=abs(_area(ob['poly']))
    candidates=[]
    for i in range(144):
        t=2*math.pi*i/144;n=(math.cos(t),math.sin(t))
        for orient in (1,-1):
            normal=(n[0]*orient,n[1]*orient)
            max_a=max(_dot(p,normal) for p in fa['front'])
            min_b=min(_dot(p,normal) for p in fb['front'])
            mid=(max_a+min_b)*.5
            for bias in (-.0075,0.0,.0075):
                split=mid+bias
                pa=clip_polygon(fa['poly'],normal,split-clearance*.5,keep_low=True)
                pb=clip_polygon(fb['poly'],normal,split+clearance*.5,keep_low=False)
                if pa is None or pb is None:continue
                ta=mouth_trim(oa,pa);tb=mouth_trim(ob,pb)
                if ta is None or tb is None or ta>MAX_FRONT_TRIM_M+TOL or tb>MAX_FRONT_TRIM_M+TOL:continue
                ra=abs(_area(pa))/original_area_a;rb=abs(_area(pb))/original_area_b
                if min(ra,rb)<MIN_FOOTPRINT_RETAINED:continue
                # Preserve as much volume on BOTH sides, not a near-zero
                # sliver on one window and an oversized other Interior.
                score=3*min(ra,rb)+.25*(ra+rb)-.3*(ta+tb)/MAX_FRONT_TRIM_M
                candidates.append((score,pa,pb,normal,split,ta,tb))
    for _,pa,pb,n,split,ta,tb in sorted(candidates,key=lambda item:-item[0])[:24]:
        try:
            ma=rebuild_prism(fa,pa);mb=rebuild_prism(fb,pb)
            from .adaptive_validation import validate_mesh
            from .spatial_safety_core import self_intersections,mesh_intersections
            validate_mesh(ma);validate_mesh(mb)
            if self_intersections(ma['verts'],ma['faces']) or self_intersections(mb['verts'],mb['faces']):continue
            if mesh_intersections(ma['verts'],ma['faces'],mb['verts'],mb['faces'],max_hits=1):continue
            return ma,mb,{'separator_xy':n,'boundary':split,'clearance_m':clearance,
                          'front_trim_m':(ta,tb),'method':'PLANAR_UPRIGHT_CONTOUR_MITRE'}
        except (ValueError,ZeroDivisionError):continue
    return None


def _single_footprint_clearance_xy(first,second):
    """World XY polygon distance for two supported upright PLANAR cavities.

    Returns None for non-prismatic geometry; never mistakes an unsupported
    measurement for a clean pass. Intersecting footprints return zero.
    """
    a=footprint(first);b=footprint(second)
    if a is None or b is None:return None
    if min(a['zhi'],b['zhi'])-max(a['zlo'],b['zlo'])<=TOL:return None
    from math import hypot
    def axes(poly):
        for p,q in zip(poly,poly[1:]+poly[:1]):
            dx,dy=q[0]-p[0],q[1]-p[1];n=hypot(dx,dy)
            if n>1e-10:yield (-dy/n,dx/n)
    pa,pb=a['poly'],b['poly']
    if all(min(max(_dot(v,n) for v in pa),max(_dot(v,n) for v in pb))-
           max(min(_dot(v,n) for v in pa),min(_dot(v,n) for v in pb))>=-1e-9
           for n in list(axes(pa))+list(axes(pb))):
        return 0.0
    def segment_distance(p,q,r,s):
        def point_to_segment(x,a,b):
            ab=(b[0]-a[0],b[1]-a[1]);den=_dot(ab,ab)
            t=max(0.,min(1.,_dot((x[0]-a[0],x[1]-a[1]),ab)/den)) if den>1e-14 else 0.
            return hypot(x[0]-a[0]-t*ab[0],x[1]-a[1]-t*ab[1])
        return min(point_to_segment(p,r,s),point_to_segment(q,r,s),
                   point_to_segment(r,p,q),point_to_segment(s,p,q))
    return min(segment_distance(p,q,r,s) for p,q in zip(pa,pa[1:]+pa[:1])
               for r,s in zip(pb,pb[1:]+pb[:1]))


def _separate_shells(mesh,limit=64):
    """Split physical disconnected face islands without inventing connecting faces.

    Used for safe multi-cavity *contact detection* only; the miter solver still
    requires an exact single-prism construction and may refuse the contact.
    """
    verts=list(mesh.get('verts') or ());faces=list(mesh.get('faces') or ())
    roles=list(mesh.get('roles') or ())
    if not faces or len(faces)!=len(roles):return []
    parents=list(range(len(verts)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    for f in faces:
        if len(f)<3 or any(not isinstance(i,int) or i<0 or i>=len(verts) for i in f):return []
        first=root(f[0])
        for v in f[1:]:parents[root(v)]=first
    islands={}
    for face,role in zip(faces,roles):
        islands.setdefault(root(face[0]),[]).append((face,role))
    if len(islands)>limit:return []
    out=[]
    for key in sorted(islands):
        entries=islands[key]
        used=sorted({i for face,_ in entries for i in face});remap={old:i for i,old in enumerate(used)}
        out.append({'verts':[verts[i] for i in used],
                    'faces':[tuple(remap[i] for i in face) for face,_ in entries],
                    'roles':[role for _,role in entries]})
    return out


def footprint_clearance_xy(first,second):
    """Exact closest XY distance over independent closed-back cavity islands.

    Does not merge disconnected cavities or make claims for curved/unsupported
    islands; the latter return None and must be handled by other safety gates.
    """
    aa=_separate_shells(first);bb=_separate_shells(second)
    if not aa or not bb:return None
    vals=[]
    for a in aa:
        for b in bb:
            distance=_single_footprint_clearance_xy(a,b)
            if distance is None:return None
            vals.append(distance)
    return min(vals) if vals else None
