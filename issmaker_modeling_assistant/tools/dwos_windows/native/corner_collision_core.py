"""RC22: physical XY/XZ footprints for CORNER vertical mullions.

Blender-independent, read-only fail-closed prepublish gate.  Each V mullion
is an orthonormal rectangular prism in its facade branch; independent branch
S positions alone cannot prove two projected prisms do not overlap in a corner.
"""
from __future__ import annotations
from math import isfinite
try:
    from .corner_assembly_core import _junction_contract
    from .profile_prism_core import branch_profile_basis
    from .adaptive_opening_core import map_svd_local
except ImportError:
    from corner_assembly_core import _junction_contract
    from profile_prism_core import branch_profile_basis
    from adaptive_opening_core import map_svd_local


def _cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _area(poly):
    return sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(poly,poly[1:]+poly[:1]))*.5


def _clip(subject,clipper):
    # Sutherland-Hodgman intersection for convex physical footprints.
    pts=list(subject)
    ccw=1 if _area(clipper)>0 else -1
    for a,b in zip(clipper,clipper[1:]+clipper[:1]):
        if not pts:break
        out=[]
        prev=pts[-1];pv=ccw*_cross(a,b,prev)
        for curr in pts:
            cv=ccw*_cross(a,b,curr)
            pin=pv>=-1.e-11;cin=cv>=-1.e-11
            if pin!=cin:
                den=pv-cv
                if abs(den)>1.e-14:
                    t=pv/den
                    out.append((prev[0]+t*(curr[0]-prev[0]),prev[1]+t*(curr[1]-prev[1])))
            if cin:out.append(curr)
            prev,pv=curr,cv
        pts=out
    return pts


def _footprint(row,topology,depth,front_offset,interior_sign):
    b=branch_profile_basis(topology,row['branch_id'],float(row['position']),float(front_offset))
    p=tuple(float(x) for x in b['origin'])
    u=tuple(float(x) for x in b['width_axis'])
    d=tuple(float(x) for x in b['depth_axis'])
    x0,x1=sorted(float(x) for x in row['band'][:2]);s=float(row['position'])
    if not all(isfinite(v) for v in (*p,*u,*d,x0,x1,depth)) or x1-x0<=0:raise ValueError('Invalid V mullion footprint')
    def make(x,k):
        w=(p[0]+(x-s)*u[0]+k*d[0],p[2]+(x-s)*u[2]+k*d[2]);return w
    k=int(interior_sign)*float(depth)
    return [make(x0,0),make(x1,0),make(x1,k),make(x0,k)]


def _post_footprint(topology,posts,depth,front_offset):
    jc=_junction_contract(topology,posts,depth)
    if jc is None:return None
    hard=tuple(float(x) for x in map_svd_local(topology,jc['s'],0.0,float(front_offset)))
    a=jc['axis_a'];b=jc['axis_b'];dep=float(depth)
    return [(hard[0],hard[2]),(hard[0]+a[0]*dep,hard[2]+a[2]*dep),
            (hard[0]+(a[0]+b[0])*dep,hard[2]+(a[2]+b[2])*dep),
            (hard[0]+b[0]*dep,hard[2]+b[2]*dep)]


def audit_vertical_collisions(topology,rows,posts,depth,front_offset=0.0,interior_sign=-1,*,area_tolerance=1.e-8,post_polygon=None):
    """Collision gate for V/V and V/central cross-sectional intersections.

    Contacts with exactly zero 2D area are allowed. V/V comparisons also
    require vertical overlap. H/V designed T-joins are unaffected.
    """
    if not topology or topology.get('classification')!='CORNER':return []
    rows=[r for r in rows if r['axis']=='V']
    shapes=[]
    for r in rows:
        poly=_footprint(r,topology,depth,front_offset,interior_sign)
        if abs(_area(poly))<1.e-10:raise ValueError('Degenerate V mullion cross-section')
        lo,hi=sorted(float(v) for v in r['band'][2:4])
        shapes.append((r['id'],(lo,hi),poly))
    issues=[]
    for i,(ia,za,pa) in enumerate(shapes):
        for ib,zb,pb in shapes[i+1:]:
            if min(za[1],zb[1])-max(za[0],zb[0])<=1.e-6:continue
            intersect=_clip(pa,pb);area=abs(_area(intersect)) if len(intersect)>=3 else 0.0
            if area>area_tolerance:
                issues.append({'code':'VERTICAL_MULLION_PAIR_CONFLICT','a':ia,'b':ib,'intersection_m2':round(area,9)})
    if posts:
        post=(post_polygon if post_polygon is not None else _post_footprint(topology,posts,depth,front_offset))
        if post is not None:
            for ia,_za,pa in shapes:
                intersect=_clip(pa,post);area=abs(_area(intersect)) if len(intersect)>=3 else 0.0
                if area>area_tolerance:
                    issues.append({'code':'VERTICAL_MULLION_POST_CONFLICT','a':ia,'b':'CENTRAL_POST','intersection_m2':round(area,9)})
    return issues



def audit_branch_band_collisions(topology, branch_bands, posts, depth,
                                 front_offset=0.0, interior_sign=-1, *, post_polygon=None):
    """Typed adapter for the window-builder's normalized CORNER Branch rows.

    The builder deliberately stores rows as dictionaries. A prior version
    incorrectly treated these dictionaries as positional tuples, raising
    KeyError(0) whenever a user added any CORNER impost. Fail with useful
    messages on schema drift rather than a bare integer exception.
    """
    required=('id','axis','position','thickness','rect','band','branch_id')
    rows=[]
    for index,band in enumerate(branch_bands):
        if not isinstance(band,dict):
            raise ValueError(f'CORNER Split {index + 1}: неверный формат геометрических данных')
        missing=[key for key in required if key not in band]
        if missing:
            raise ValueError(f'CORNER Split {index + 1}: отсутствуют параметры {", ".join(missing)}')
        rows.append(band)
    return audit_vertical_collisions(topology,rows,posts,depth,front_offset,
                                     interior_sign,post_polygon=post_polygon)


def inner_miter_to_post_distance(topology,posts,depth,front_offset,glass_depth):
    """Read-only diagnostic: distance of planar INNER-glass miter from post.

    Not a proof of true rabbet contact: the glass edge may run underneath the
    post. A positive value means the inner miter apex is outside the physical
    cross-section and should be inspected rather than called a clean PASS.
    """
    if not posts:return {'status':'NOT_APPLICABLE','outside_distance_m':0.0}
    try:
        from .corner_flat_glass_core import postless_corner_point
    except ImportError:
        from corner_flat_glass_core import postless_corner_point
    poly=_post_footprint(topology,posts,depth,front_offset)
    if not poly:return {'status':'UNVERIFIED','outside_distance_m':None}
    p=postless_corner_point(topology,0,float(posts[0]['s']),0.0,float(glass_depth))
    q=(float(p[0]),float(p[2]))
    orient=1 if _area(poly)>0 else -1
    inside=all(orient*_cross(a,b,q)>=-1.e-8 for a,b in zip(poly,poly[1:]+poly[:1]))
    if inside:return {'status':'PASS','outside_distance_m':0.0}
    best=float('inf')
    for a,b in zip(poly,poly[1:]+poly[:1]):
        dx=b[0]-a[0];dy=b[1]-a[1];length_sq=dx*dx+dy*dy
        if length_sq<1.e-15:continue
        t=max(0.0,min(1.0,((q[0]-a[0])*dx+(q[1]-a[1])*dy)/length_sq))
        d=((q[0]-a[0]-t*dx)**2+(q[1]-a[1]-t*dy)**2)**.5
        best=min(best,d)
    if not isfinite(best):return {'status':'UNVERIFIED','outside_distance_m':None}
    return {'status':'WARNING' if best>1.e-4 else 'PASS','outside_distance_m':round(best,7),
            'code':'CORNER_INNER_MITER_BEYOND_POST_TIP' if best>1.e-4 else 'OK'}
