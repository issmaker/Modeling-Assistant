"""EXPERIMENTAL exact-path corner geometry; no bpy, bmesh or third-party APIs.

RD1 solves XY offset intersections and constructs *untextured mesh previews* of
planar path sweeps. Sloped-path corner joining may be underconstrained: it is
reported, never silently distorted. It is not a production Mesh/UV replacement.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from math import hypot, isfinite, sqrt
from typing import Sequence

EPS = 1e-9


class CornerGeometryError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    z: float = 0.0

    def as_xyz(self):
        return self.x, self.y, self.z


@dataclass(frozen=True)
class ProfilePoint:
    lateral: float
    height: float


@dataclass(frozen=True)
class CornerResult:
    index: int
    turn_deg: float
    reach_previous_m: float
    reach_next_m: float
    path_z: float
    max_grade_height_disagreement_m: float
    status: str
    message: str


@dataclass(frozen=True)
class OffsetResult:
    points: tuple[Point, ...]
    corners: tuple[CornerResult, ...]
    source_unchanged: bool


@dataclass(frozen=True)
class SweepPreview:
    vertices: tuple[tuple[float, float, float], ...]
    faces: tuple[tuple[int, ...], ...]
    corners: tuple[CornerResult, ...]
    preview_only: bool = True
    has_production_uv: bool = False


def _finite(*v):
    return all(isfinite(x) for x in v)


def _xy_dir(a: Point, b: Point):
    dx, dy = b.x - a.x, b.y - a.y
    length = hypot(dx, dy)
    if not _finite(*a.as_xyz(), *b.as_xyz()) or length <= EPS:
        raise CornerGeometryError('DEGENERATE_PATH', 'Невалидные координаты или нулевой XY-участок')
    return dx / length, dy / length, length


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def _left(d):
    return -d[1], d[0]


def _intersection(a, d, b, e):
    den = _cross(d, e)
    if abs(den) < 1e-9:
        raise CornerGeometryError('PARALLEL_CORNER', 'Участки параллельны либо образуют разворот')
    vec = b[0] - a[0], b[1] - a[1]
    t = _cross(vec, e)/den
    s = _cross(vec, d)/den
    return (a[0]+t*d[0], a[1]+t*d[1]), t, s


def _segment_intersect(a, b, c, d, tol=1e-8):
    ab=(b.x-a.x,b.y-a.y)
    ac=(c.x-a.x,c.y-a.y)
    ad=(d.x-a.x,d.y-a.y)
    cd=(d.x-c.x,d.y-c.y)
    ca=(a.x-c.x,a.y-c.y)
    cb=(b.x-c.x,b.y-c.y)
    x1,x2=_cross(ab,ac),_cross(ab,ad)
    y1,y2=_cross(cd,ca),_cross(cd,cb)
    # Strict proper crossings (non-adjacent collinear overlaps also require rejection).
    if x1*x2 < -tol and y1*y2 < -tol:
        return True
    def on(p,q,r):
        return (min(p.x,r.x)-tol <= q.x <= max(p.x,r.x)+tol and
                min(p.y,r.y)-tol <= q.y <= max(p.y,r.y)+tol and
                abs(_cross((r.x-p.x,r.y-p.y),(q.x-p.x,q.y-p.y)))<=tol)
    return ((abs(x1)<=tol and on(a,c,b)) or (abs(x2)<=tol and on(a,d,b)) or
            (abs(y1)<=tol and on(c,a,d)) or (abs(y2)<=tol and on(c,b,d)))


def _path(path: Sequence[Point], closed: bool):
    points = tuple(path)
    if closed and len(points)>1 and hypot(points[0].x-points[-1].x,points[0].y-points[-1].y)<EPS:
        if abs(points[0].z-points[-1].z)>1e-7:
            raise CornerGeometryError('CLOSED_Z_MISMATCH','У замкнутой линии разные Z в одной XY-точке')
        points=points[:-1]
    if len(points) < (3 if closed else 2):
        raise CornerGeometryError('SHORT_PATH', 'Недостаточно вершин пути')
    edges=list(zip(points, points[1:] + (points[:1] if closed else ())))
    for a,b in edges:_xy_dir(a,b)
    for i, (a,b) in enumerate(edges):
        for j, (c,d) in enumerate(edges):
            if j<=i or j == i+1 or (closed and i==0 and j==len(edges)-1):continue
            if _segment_intersect(a,b,c,d):
                raise CornerGeometryError('PATH_SELF_INTERSECTION',f'Исходный путь пересекается: {i} x {j}')
    return points


def _corner_xy(prev: Point, here: Point, following: Point, offset: float):
    pdx,pdy,len_pre = _xy_dir(prev,here)
    ndx,ndy,len_next = _xy_dir(here,following)
    dn=(pdx,pdy); en=(ndx,ndy)
    # Collinear forward path needs no miter; U-turn is ambiguous.
    turn=_cross(dn,en)
    dot=dn[0]*en[0]+dn[1]*en[1]
    if abs(turn)<1e-10:
        if dot < 0:
            raise CornerGeometryError('REVERSAL', 'Разворот пути 180 градусов')
        normal=_left(dn)
        return (here.x+offset*normal[0],here.y+offset*normal[1]), 0.0, 0.0, 0.0, len_pre, len_next
    n0=_left(dn);n1=_left(en)
    a=(here.x+offset*n0[0], here.y+offset*n0[1]); b=(here.x+offset*n1[0],here.y+offset*n1[1])
    joint,t,s=_intersection(a,dn,b,en)
    from math import atan2,degrees
    angle=degrees(atan2(turn,dot))
    return joint,t,s,angle,len_pre,len_next


def _corner_profile_info(points, i, offsets, closed, safety_fraction, z_tolerance):
    n=len(points)
    if not closed and (i==0 or i==n-1):
        return CornerResult(i,0.,0.,0.,points[i].z,0.,'ENDPOINT','Начало/конец пути')
    before,here,after=points[(i-1)%n],points[i],points[(i+1)%n]
    ratios=[]; max_z=0.; angle=0.; reach0=reach1=0.
    pdir=_xy_dir(before,here); ndir=_xy_dir(here,after)
    for lateral in offsets:
        _,t,s,angle,len_prev,len_next=_corner_xy(before,here,after,lateral)
        # Parameter on previous line is measured FROM authored corner here;
        # t and s are signed in the direction of the source segments.
        grade_p=(here.z-before.z)/pdir[2]
        grade_n=(after.z-here.z)/ndir[2]
        z_pre=here.z+t*grade_p
        z_next=here.z+s*grade_n
        max_z=max(max_z,abs(z_pre-z_next))
        reach0=max(reach0,abs(t)); reach1=max(reach1,abs(s))
        if (abs(t) > pdir[2]*safety_fraction or abs(s)>ndir[2]*safety_fraction):
            return CornerResult(i,angle,reach0,reach1,here.z,max_z,'TRIM_EXCEEDS_SEGMENT',
              'Срез профиля выходит за предел безопасного участка; точный Path сохранён')
    status='GRADE_TRIM_UNSUPPORTED' if max_z>z_tolerance else 'READY'
    msg=('У двух прямых участков разные высоты среза; требуется точная пространственная trim-поверхность' if status!='READY'
         else 'Точный XY-срез профиля без изменения пути')
    return CornerResult(i,angle,reach0,reach1,here.z,max_z,status,msg)


def analyze_corners(path: Sequence[Point], lateral_offsets, *, closed=False,
                    safety_fraction=0.47, z_tolerance=1e-5):
    if not 0 < safety_fraction < .5 or not z_tolerance>=0:
        raise ValueError('Invalid trim safety parameters')
    points=_path(path,closed)
    offsets=tuple(float(x) for x in lateral_offsets)
    if not offsets or any(not isfinite(x) for x in offsets):
        raise CornerGeometryError('INVALID_PROFILE', 'Пустые или нечисловые смещения профиля')
    return tuple(_corner_profile_info(points,i,offsets,closed,safety_fraction,z_tolerance) for i in range(len(points)))


def offset_path(path: Sequence[Point], distance:float, *, closed=False, side='LEFT',
                safety_fraction=.47, z_tolerance=1e-6):
    """Offset corners only; preserved Z from authored point; detect unsafe intersections.

    This is a *candidate* inner wall contour, NOT a unioned solid wall footprint.
    """
    points=_path(path,closed)
    if not isfinite(distance) or distance<0 or side not in ('LEFT','RIGHT'):
        raise CornerGeometryError('INVALID_OFFSET','Нужны положительное смещение и LEFT/RIGHT')
    off=distance*(1 if side=='LEFT' else -1)
    info=analyze_corners(points,(off,),closed=closed,safety_fraction=safety_fraction,z_tolerance=z_tolerance)
    for c in info:
        if c.status not in ('READY','ENDPOINT','GRADE_TRIM_UNSUPPORTED'):
            raise CornerGeometryError(c.status, f'Вершина {c.index}: {c.message}')
    out=[]
    for i,here in enumerate(points):
        if not closed and i==0:
            d=_xy_dir(points[0],points[1]); n=_left(d); xy=(here.x+off*n[0],here.y+off*n[1])
        elif not closed and i==len(points)-1:
            d=_xy_dir(points[-2],points[-1]);n=_left(d);xy=(here.x+off*n[0],here.y+off*n[1])
        else:
            xy,*_=_corner_xy(points[(i-1)%len(points)],here,points[(i+1)%len(points)],off)
        out.append(Point(*xy,here.z))
    # The offset may fold back on itself even if source is simple.
    _path(out,closed)
    return OffsetResult(tuple(out),info,all(a==b for a,b in zip(points,tuple(path)[:len(points)])))


def cornice_preview(path:Sequence[Point], profile:Sequence[ProfilePoint], *, closed=False,
                    mirror_side=False, flip_x=False, reverse_path=False, safety_fraction=.47):
    """Construct a non-UV **planar corner** sweep. Refuse sloped and dangerous corners.

    This is a compareable mesh preview of actual miter-trim lines, not a
    production replacement. There is no reliable module UV on these polygons.
    """
    points=tuple(reversed(path)) if reverse_path else tuple(path)
    points=_path(points,closed)
    poly=tuple(profile)
    if len(poly)<3 or any(not _finite(q.lateral,q.height) for q in poly):
        raise CornerGeometryError('INVALID_PROFILE','Профиль должен содержать как минимум 3 корректные точки')
    # Poly can have a repeated last vertex; strip duplicate for ring mesh.
    if poly[0] == poly[-1]: poly=poly[:-1]
    if len(poly)<3:
        raise CornerGeometryError('INVALID_PROFILE','Невалидный замкнутый профиль')
    area=sum(poly[i].lateral*poly[(i+1)%len(poly)].height-poly[(i+1)%len(poly)].lateral*poly[i].height for i in range(len(poly)))/2
    if abs(area)<1e-10:
        raise CornerGeometryError('INVALID_PROFILE','Профиль не образует область с ненулевой площадью')
    for i in range(len(poly)):
        a,b=poly[i],poly[(i+1)%len(poly)]
        for j in range(i+2,len(poly)):
            if (i==0 and j==len(poly)-1):continue
            c,d=poly[j],poly[(j+1)%len(poly)]
            if _segment_intersect(Point(a.lateral,a.height),Point(b.lateral,b.height),Point(c.lateral,c.height),Point(d.lateral,d.height)):
                raise CornerGeometryError('PROFILE_SELF_INTERSECTION',f'Рёбра профиля {i} и {j} пересекаются')
    signed=lambda q: q.lateral*(-1 if mirror_side else 1)*(-1 if flip_x else 1)
    offsets=[signed(q) for q in poly]
    corners=analyze_corners(points,offsets,closed=closed,safety_fraction=safety_fraction)
    bad=[c for c in corners if c.status not in ('READY','ENDPOINT')]
    if bad:
        raise CornerGeometryError(bad[0].status,f'Corner {bad[0].index}: {bad[0].message}')
    # The source path Z must be retained, including its corner height.
    v=[]
    for i,p in enumerate(points):
        for q,off in zip(poly,offsets):
            if closed or 0<i<len(points)-1:
                xy,*_=_corner_xy(points[(i-1)%len(points)],p,points[(i+1)%len(points)],off)
            else:
                a,b=(points[0],points[1]) if i==0 else (points[-2],points[-1])
                d=_xy_dir(a,b);n=_left(d);xy=(p.x+off*n[0],p.y+off*n[1])
            v.append((xy[0],xy[1],p.z+q.height))
    faces=[];m=len(poly)
    segments=len(points) if closed else len(points)-1
    for i in range(segments):
        ni=(i+1)%len(points)
        for j in range(m):
            nj=(j+1)%m
            faces.append((i*m+j,i*m+nj,ni*m+nj,ni*m+j))
    if not closed:
        faces.append(tuple(reversed(tuple(range(m)))))
        faces.append(tuple((len(points)-1)*m+j for j in range(m)))
    # Orient closed profile sweep as one shell (same entire winding, no isolated flips).
    ox,oy,oz=v[0]
    volume6=0.0
    for face in faces:
        a=v[face[0]]
        for j in range(1,len(face)-1):
            b=v[face[j]];cc=v[face[j+1]]
            ar=(a[0]-ox,a[1]-oy,a[2]-oz)
            br=(b[0]-ox,b[1]-oy,b[2]-oz)
            cr=(cc[0]-ox,cc[1]-oy,cc[2]-oz)
            volume6 += ar[0]*(br[1]*cr[2]-br[2]*cr[1]) + ar[1]*(br[2]*cr[0]-br[0]*cr[2]) + ar[2]*(br[0]*cr[1]-br[1]*cr[0])
    if abs(volume6)<1e-9:
        raise CornerGeometryError('ZERO_VOLUME','Профиль либо срез образует нулевой объём')
    if volume6 < 0:
        faces=[tuple(reversed(f)) for f in faces]
    return SweepPreview(tuple(v),tuple(faces),corners)


def module_uv_budget(module_m:int,texture_px:int,density_px_per_m=1000):
    if module_m not in (2,4) or texture_px != module_m*1024:
        raise CornerGeometryError('UV_MODULE_SPEC','Допускаются только 2×2/2048 и 4×4/4096')
    if density_px_per_m !=1000:
        raise CornerGeometryError('UV_DENSITY','Плотность должна быть 1000 px/m')
    work=int(module_m*density_px_per_m)
    margin=(texture_px-work)//2
    return {'module_m':module_m,'texture_px':texture_px,'work_px':work,'margin_each_side_px':margin,
            'uv_min':margin/texture_px,'uv_max':1-margin/texture_px}
