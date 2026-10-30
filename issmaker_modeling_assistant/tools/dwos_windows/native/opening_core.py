from __future__ import annotations
from dataclasses import dataclass
from math import sqrt

EPS = 1e-7

@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float
    def __add__(self, o): return Vec3(self.x+o.x,self.y+o.y,self.z+o.z)
    def __sub__(self, o): return Vec3(self.x-o.x,self.y-o.y,self.z-o.z)
    def __mul__(self, s): return Vec3(self.x*s,self.y*s,self.z*s)
    __rmul__ = __mul__

def dot(a,b): return a.x*b.x+a.y*b.y+a.z*b.z
def cross(a,b): return Vec3(a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x)
def length(v): return sqrt(max(0.0,dot(v,v)))
def norm(v):
    l=length(v)
    if l<EPS: raise ValueError("zero vector")
    return v*(1.0/l)

def canonical_dir(v: Vec3) -> Vec3:
    n = norm(v)
    vals=(n.x,n.y,n.z)
    for c in vals:
        if abs(c)>1e-6:
            if c < 0: n=n*-1.0
            break
    return n

def polygon_area_2d(points):
    a=0.0
    for i,p in enumerate(points):
        q=points[(i+1)%len(points)]
        a += p[0]*q[1]-q[0]*p[1]
    return 0.5*a

def polygon_centroid_2d(points):
    # Area centroid when possible; average is a stable fallback for degenerate input.
    a = polygon_area_2d(points)
    if abs(a) < EPS:
        return (sum(p[0] for p in points)/len(points), sum(p[1] for p in points)/len(points))
    cx=0.0; cy=0.0
    for i,p in enumerate(points):
        q=points[(i+1)%len(points)]
        k=p[0]*q[1]-q[0]*p[1]
        cx += (p[0]+q[0])*k
        cy += (p[1]+q[1])*k
    f=1.0/(6.0*a)
    return cx*f, cy*f

def point_in_polygon_2d(point, polygon, eps=1e-9):
    """Even/odd test. Boundary points count as inside."""
    x,y=point
    inside=False
    n=len(polygon)
    for i,p in enumerate(polygon):
        q=polygon[(i+1)%n]
        # Boundary check.
        dx=q[0]-p[0]; dy=q[1]-p[1]
        px=x-p[0]; py=y-p[1]
        crossv=dx*py-dy*px
        if abs(crossv)<=eps*(abs(dx)+abs(dy)+1.0):
            dotv=px*dx+py*dy
            if -eps <= dotv <= dx*dx+dy*dy+eps:
                return True
        if (p[1] > y) != (q[1] > y):
            xin = p[0] + (y-p[1]) * dx / (dy if abs(dy)>eps else (eps if dy>=0 else -eps))
            if xin > x:
                inside = not inside
    return inside

def nesting_depths_2d(polygons):
    """Return containment depth for each closed polygon.

    Depth 0 = an outer disconnected shell, depth 1 = a hole, depth 2 = island in a
    hole, etc. This is what lets opening detection work on a single planar mesh
    containing several disconnected wall panels without treating the other panels as
    openings.
    """
    centers=[polygon_centroid_2d(p) for p in polygons]
    areas=[abs(polygon_area_2d(p)) for p in polygons]
    depths=[]
    for i,c in enumerate(centers):
        depth=0
        for j,poly in enumerate(polygons):
            if i==j or areas[j] <= areas[i] + EPS:
                continue
            if point_in_polygon_2d(c, poly):
                depth += 1
        depths.append(depth)
    return depths

def simplify_collinear_2d(points, eps=1e-6):
    if len(points) <= 4:
        return list(points)
    out=[]
    n=len(points)
    for i,p in enumerate(points):
        a=points[i-1]; b=points[(i+1)%n]
        ab=(b[0]-a[0],b[1]-a[1]); ap=(p[0]-a[0],p[1]-a[1])
        c=abs(ab[0]*ap[1]-ab[1]*ap[0])
        if c > eps*(abs(ab[0])+abs(ab[1])+1.0): out.append(p)
    return out or list(points)

def rectangle_from_projected(points, tol=1e-5):
    pts=simplify_collinear_2d(points,tol)
    if len(pts)!=4:
        return None
    edges=[]
    for i,p in enumerate(pts):
        q=pts[(i+1)%4]
        dx,dy=q[0]-p[0],q[1]-p[1]
        l=(dx*dx+dy*dy)**0.5
        if l<tol: return None
        edges.append((dx/l,dy/l,l))
    for i in range(4):
        a=edges[i]; b=edges[(i+1)%4]
        if abs(a[0]*b[0]+a[1]*b[1])>2e-3: return None
    if abs(edges[0][2]-edges[2][2])>tol*20 or abs(edges[1][2]-edges[3][2])>tol*20:
        return None
    cx=sum(p[0] for p in pts)/4.0; cy=sum(p[1] for p in pts)/4.0
    return {"points":pts,"center":(cx,cy),"width":0.5*(edges[0][2]+edges[2][2]),"height":0.5*(edges[1][2]+edges[3][2]),"area":abs(polygon_area_2d(pts))}
