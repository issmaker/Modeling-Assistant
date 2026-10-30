"""ISSMAKER W58.32 — planar exact-offset arrangement prototype.

Pure math, no bpy: computes disjoint surface fragments of oriented convex
coplanar polygons. NOT a license to publish mesh until shell connectivity,
normals and author UV invariants are independently verified.
"""
import math

EPS=1e-10
def _cross(a,b):return a[0]*b[1]-a[1]*b[0]
def _sub(a,b):return (a[0]-b[0],a[1]-b[1])
def signed_area(poly):
    return .5*sum(_cross(poly[i],poly[(i+1)%len(poly)]) for i in range(len(poly))) if len(poly)>2 else 0.

def _normalize(poly, tolerance=1e-10):
    pts=[]
    for p in poly:
        p=(float(p[0]),float(p[1]))
        if not (math.isfinite(p[0]) and math.isfinite(p[1])):
            raise ValueError('ARRANGEMENT_NONFINITE')
        if not pts or math.dist(pts[-1],p)>tolerance:pts.append(p)
    if len(pts)>1 and math.dist(pts[0],pts[-1])<=tolerance:pts.pop()
    if len(pts)>2 and signed_area(pts)<0:pts.reverse()
    return pts

def is_convex(poly,tolerance=1e-9):
    p=_normalize(poly)
    if len(p)<3:return False
    turns=[_cross(_sub(p[(i+1)%len(p)],p[i]),
                  _sub(p[(i+2)%len(p)],p[(i+1)%len(p)]))
           for i in range(len(p))]
    return min(turns)>=-tolerance and abs(signed_area(p))>tolerance

def clip_halfplane(poly,a,b,*,keep_left=True,eps=1e-10):
    """Clip polygon with directed infinite edge a→b, keeping left side."""
    if not poly:return []
    sign=1 if keep_left else -1
    def side(p):return sign*_cross(_sub(b,a),_sub(p,a))
    result=[]
    for idx,p in enumerate(poly):
        q=poly[(idx+1)%len(poly)]
        s,t=side(p),side(q)
        # Half-open ownership: two neighbors may share a boundary but not area.
        inside_p=s>=-eps;inside_q=t>=-eps
        if inside_p:result.append(p)
        if inside_p!=inside_q and abs(s-t)>1e-16:
            k=s/(s-t);result.append((p[0]+k*(q[0]-p[0]),p[1]+k*(q[1]-p[1])))
    result=_normalize(result)
    return result if len(result)>=3 and abs(signed_area(result))>1e-12 else []

def convex_intersection(a,b,*,eps=1e-10):
    lhs=_normalize(a);rhs=_normalize(b)
    if not is_convex(lhs) or not is_convex(rhs):
        raise ValueError('ARRANGEMENT_NONCONVEX')
    for i in range(len(rhs)):
        lhs=clip_halfplane(lhs,rhs[i],rhs[(i+1)%len(rhs)],eps=eps)
        if not lhs:break
    return lhs

def convex_difference(a,b,*,eps=1e-10):
    """Return nonoverlapping convex cells covering A\\B, no loss of area."""
    inside=_normalize(a);rhs=_normalize(b);outside=[]
    if not is_convex(inside) or not is_convex(rhs):
        raise ValueError('ARRANGEMENT_NONCONVEX')
    for i in range(len(rhs)):
        left=rhs[i];right=rhs[(i+1)%len(rhs)]
        removed=clip_halfplane(inside,left,right,keep_left=False,eps=eps)
        if removed:outside.append(removed)
        inside=clip_halfplane(inside,left,right,keep_left=True,eps=eps)
        if not inside:break
    area_expected=abs(signed_area(a))-abs(signed_area(convex_intersection(a,b,eps=eps)))
    area_actual=sum(abs(signed_area(p)) for p in outside)
    if abs(area_actual-area_expected)>max(1e-8,abs(area_expected)*1e-6):
        raise ValueError(f'ARRANGEMENT_AREA_CONSERVATION: {area_expected} != {area_actual}')
    return outside

def union_cells(polygons,*,eps=1e-10):
    """Disjoint partition of union of convex polygon surfaces."""
    cells=[]
    for polygon in polygons:
        incoming=[_normalize(polygon)]
        for existing in cells:
            next_cells=[]
            for part in incoming:
                next_cells.extend(convex_difference(part,existing,eps=eps))
            incoming=next_cells
            if not incoming:break
        cells.extend(incoming)
    return cells

def overlap_area(a,b,*,eps=1e-10):
    return abs(signed_area(convex_intersection(a,b,eps=eps)))


def analyze_3d_pair(face_a,face_b,*,plane_tolerance_m=1e-5,
                    orientation_tolerance=1e-6):
    """Prove whether a pair of source quads can use planar arrangement.

    Returns classifications; never silently project crossing 3D surfaces
    onto a common plane.
    """
    def vec(p,q):return tuple(a-b for a,b in zip(p,q))
    def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    def dot(a,b):return sum(a[i]*b[i] for i in range(3))
    def norm(q):return math.sqrt(dot(q,q))
    def basis(poly):
        pts=[tuple(map(float,p)) for p in poly]
        if len(pts)<3:return None
        origin=pts[0];best=(0,None)
        for i in range(1,len(pts)):
            for j in range(i+1,len(pts)):
                cr=cross(vec(pts[i],origin),vec(pts[j],origin))
                if norm(cr)>best[0]:best=(norm(cr),cr)
        if best[0]<1e-12:return None
        n=tuple(v/best[0] for v in best[1])
        longest=max((norm(vec(p,origin)),p) for p in pts[1:])
        if longest[0]<=1e-9:return None
        u=tuple(v/longest[0] for v in vec(longest[1],origin))
        v=cross(n,u)
        return origin,u,v,n
    aa=basis(face_a);bb=basis(face_b)
    if not aa or not bb:return {'classification':'DEGENERATE_INPUT'}
    origin,u,v,n=aa
    dotnorm=abs(dot(n,bb[3]))
    maxplane=max(abs(dot(n,vec(p,origin))) for p in [*face_a,*face_b])
    if maxplane>plane_tolerance_m or dotnorm<1-orientation_tolerance:
        return {'classification':'NONCOPLANAR_TRUE_3D_ARRANGEMENT_REQUIRED',
                'normal_abs_dot':dotnorm,'max_plane_distance_m':maxplane}
    ap=[(dot(vec(p,origin),u),dot(vec(p,origin),v)) for p in face_a]
    bp=[(dot(vec(p,origin),u),dot(vec(p,origin),v)) for p in face_b]
    if not is_convex(ap) or not is_convex(bp):
        return {'classification':'PLANAR_NONCONVEX_REQUIRES_SUBDIVISION',
                'max_plane_distance_m':maxplane}
    area=overlap_area(ap,bp)
    return {'classification':'COPLANAR_OVERLAP_TRIMMABLE' if area>1e-9 else 'COPLANAR_POINT_EDGE_CONTACT',
            'overlap_area_m2':area,'max_plane_distance_m':maxplane,
            'normal_abs_dot':dotnorm,'union_cell_count':len(union_cells([ap,bp]))}
