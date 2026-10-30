"""Independent analytic 3D corner-cut research kernel, no Blender imports.

This is not the legacy vertex-miter: each straight segment creates *its own*
start/end cut rings by intersecting its parallel profile rails with vertical
corner planes. Both sides are compared in XYZ before they can be stitched.

Phase T1 is intentionally fail-closed for mismatched grade seams. A Blender
Boolean preview lab can inspect those volumes separately, but no production
Result is changed by this module. Profile XZ geometry and author path are never
edited. No production UV is generated here.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import hypot, isfinite, sqrt, atan2, degrees
from typing import Sequence

EPS = 1e-9


class TrimError(ValueError):
    def __init__(self, code: str, description: str):
        self.code = code
        super().__init__(f"{code}: {description}")


@dataclass(frozen=True)
class Cut:
    vertex: int
    turn_deg: float
    normal_xy: tuple[float, float]
    incoming_parameters: tuple[float, ...]
    outgoing_parameters: tuple[float, ...]
    max_z_gap: float
    max_xy_gap: float
    status: str


@dataclass(frozen=True)
class SegmentTrim:
    index: int
    start_ring: tuple[tuple[float, float, float], ...]
    end_ring: tuple[tuple[float, float, float], ...]
    start_parameters: tuple[float, ...]
    end_parameters: tuple[float, ...]


@dataclass(frozen=True)
class TrimResult:
    segments: tuple[SegmentTrim, ...]
    corners: tuple[Cut, ...]
    closed: bool
    source_intact: bool
    production_uv: bool = False


def _finite_vector(v, count):
    if len(v) != count or not all(isfinite(float(x)) for x in v):
        raise TrimError('NONFINITE_INPUT', f'Expected finite vector({count})')
    return tuple(float(x) for x in v)


def _delta(a,b): return (b[0]-a[0],b[1]-a[1])
def _dot(a,b): return a[0]*b[0]+a[1]*b[1]
def _cross(a,b): return a[0]*b[1]-a[1]*b[0]
def _normal(d,side): return (-side*d[1], side*d[0])


def _direction(a,b):
    v=_delta(a,b); length=hypot(*v)
    if length <= EPS:
        raise TrimError('ZERO_LENGTH_XY', 'Авторский путь содержит нулевой XY-сегмент')
    return (v[0]/length,v[1]/length),length


def _point(p, n, t, offset, along, height):
    return (p[0]+n[0]*offset+t[0]*along,
            p[1]+n[1]*offset+t[1]*along,
            p[2]+height)


def _rail(start, end, direction, normal, lateral, height, along):
    length=hypot(end[0]-start[0],end[1]-start[1])
    u=along/length
    return (start[0]+direction[0]*along+normal[0]*lateral,
            start[1]+direction[1]*along+normal[1]*lateral,
            start[2]+u*(end[2]-start[2])+height)


def build_true_trim(path:Sequence[Sequence[float]], profile:Sequence[Sequence[float]],
                    *, closed=False, side_sign=1, flip_x=False, flip_z=False,
                    reverse_path=False, grade_tolerance=2e-5,
                    safety_fraction=.47) -> TrimResult:
    """Independently cut longitudinal section rails against junction planes.

    Output is a mesh-ready collection of segment cut rings. Coincident rings are
    a condition of an analytic weld, not an assumption. A short segment that
    would get two crossing cut rings is refused. The original path is untouched.

    `path`: author world XYZ; `profile`: ordered (signed lateral, height) XZ.
    `side_sign` is LEFT/RIGHT selected by caller, not deduced from profile flips.
    """
    if side_sign not in (-1,1) or not 0 < safety_fraction < .5 or grade_tolerance < 0:
        raise TrimError('INVALID_OPTIONS', 'side_sign, tolerance or safety fraction')
    source=tuple(_finite_vector(p,3) for p in path)
    points=source
    if closed and len(points)>1 and max(abs(points[0][i]-points[-1][i]) for i in range(3))<EPS:
        points=points[:-1]
    if reverse_path: points=tuple(reversed(points))
    if len(points)<(3 if closed else 2):
        raise TrimError('SHORT_PATH', 'Insufficient path vertices')
    xs=tuple(_finite_vector(q,2) for q in profile)
    if len(xs)<2: raise TrimError('SHORT_PROFILE','Insufficient profile vertices')
    if len(xs)>2 and xs[-1] == xs[0]: xs=xs[:-1]
    xs=tuple(((-q[0] if flip_x else q[0]),(-q[1] if flip_z else q[1])) for q in xs)
    if all(abs(q[0])<EPS for q in xs):
        raise TrimError('ZERO_PROFILE_WIDTH','All lateral offsets are zero')
    count=len(points); segment_count=count if closed else count-1
    directions=[]; lengths=[]; normals=[]
    for i in range(segment_count):
        d,l=_direction(points[i],points[(i+1)%count]);directions.append(d);lengths.append(l)
        normals.append(_normal(d,side_sign))
    # Parametric t distances are measured along the *own segment* from its
    # first authored vertex. At internal vertex j the incoming cut is at
    # length(incoming) + t_in and the outgoing cut is at t_out.
    in_offsets={};out_offsets={};cuts=[]
    active=range(count) if closed else range(1,count-1)
    for i in active:
        i_prev=(i-1)%segment_count;i_next=i%segment_count
        dp=directions[i_prev];dn=directions[i_next]
        nx,ny=dp[0]+dn[0],dp[1]+dn[1]
        size=hypot(nx,ny)
        turn=degrees(atan2(_cross(dp,dn),_dot(dp,dn)))
        if size<1e-7:
            raise TrimError('REVERSAL',f'Corner {i}: 180-degree reversal')
        t=(nx/size,ny/size)
        # Plane is *vertical in world-Z*, through the author point, with
        # XY-normal = (incoming + outgoing) / ||sum||.
        denom_p=_dot(dp,t);denom_n=_dot(dn,t)
        if min(denom_p,denom_n) < 1.e-8:
            raise TrimError('DEGENERATE_TRIM_PLANE',f'Corner {i}: invalid plane')
        in_dist=[];out_dist=[];max_gap=0.;max_xy=0.
        for off,_height in xs:
            tin = -off*_dot(normals[i_prev],t)/denom_p
            tout = -off*_dot(normals[i_next],t)/denom_n
            in_dist.append(tin);out_dist.append(tout)
            pi=_rail(points[i_prev],points[i],dp,normals[i_prev],off,_height,lengths[i_prev]+tin)
            pn=_rail(points[i],points[(i+1)%count],dn,normals[i_next],off,_height,tout)
            gap_xy=hypot(pi[0]-pn[0],pi[1]-pn[1]);max_xy=max(max_xy,gap_xy)
            max_gap=max(max_gap,abs(pi[2]-pn[2]))
            if abs(tin)>=lengths[i_prev]*safety_fraction or abs(tout)>=lengths[i_next]*safety_fraction:
                raise TrimError('TRIM_EXCEEDS_SEGMENT', f'Corner {i}, profile offset {off:.5f}, '
                    f'previous={tin:.5f}m next={tout:.5f}m')
        if max_xy>1.e-6:
            raise TrimError('TRIM_XY_DIVERGENCE',f'Corner {i}: gap {max_xy:.9f} m')
        cuts.append(Cut(i,turn,t,tuple(in_dist),tuple(out_dist),max_gap,max_xy,
            'JOIN_READY' if max_gap<=grade_tolerance else 'GRADE_3D_INTERSECTION_REQUIRED'))
        in_offsets[i]=tuple(in_dist);out_offsets[i]=tuple(out_dist)
    segments=[]
    for i in range(segment_count):
        j=(i+1)%count
        t_start=out_offsets.get(i,tuple(0. for _ in xs))
        t_end=tuple(lengths[i]+t for t in in_offsets.get(j,tuple(0. for _ in xs)))
        if any(stop-start<=1.e-5 for start,stop in zip(t_start,t_end)):
            raise TrimError('TRIM_RINGS_CROSS',f'Segment {i}: cut rings cross or collapse')
        p=points[i];q=points[j]
        segments.append(SegmentTrim(i,
            tuple(_rail(p,q,directions[i],normals[i],x,z,u) for (x,z),u in zip(xs,t_start)),
            tuple(_rail(p,q,directions[i],normals[i],x,z,u) for (x,z),u in zip(xs,t_end)),
            t_start,t_end))
    return TrimResult(tuple(segments),tuple(cuts),closed,source==tuple(_finite_vector(p,3) for p in path))


def assert_join_ready(result:TrimResult, tolerance=2.e-5):
    """Refuse welding non-coincident 3D rings (especially variable-Z corners)."""
    for cut in result.corners:
        if cut.max_z_gap>tolerance:
            raise TrimError('GRADE_3D_INTERSECTION_REQUIRED',
              f'Corner {cut.vertex}: parallel cut rings differ in Z by {cut.max_z_gap:.6f} m')
    return True


def stitch_preview_mesh(result:TrimResult, *, profile_closed=True, tolerance=2.e-5):
    """Return {vertices, faces} of a welded mathematical preview, NO UV.

    `profile_closed` must be true for a manifold solid. Only exact 3D cut
    coincidence is supported; no grade-gap bridging is invented.
    """
    assert_join_ready(result,tolerance)
    if not profile_closed:
        raise TrimError('OPEN_PROFILE_VOLUME_UNSUPPORTED','Surface profile needs a separate trim solver')
    vertices=[];faces=[];lookup={}
    def idx(p):
        key=tuple(round(float(v)/tolerance) for v in p)
        old=lookup.get(key)
        if old is not None:
            if max(abs(vertices[old][i]-p[i]) for i in range(3)) <= tolerance:return old
            raise TrimError('VERTEX_HASH_COLLISION','Position hash collides outside weld tolerance')
        key_idx=len(vertices);lookup[key]=key_idx;vertices.append(tuple(p));return key_idx
    for piece in result.segments:
        a=[idx(p) for p in piece.start_ring];b=[idx(p) for p in piece.end_ring]
        if len(a)<3 or len(set(a))!=len(a) or len(set(b))!=len(b):
            raise TrimError('DEGENERATE_SECTION','Repeated profile section vertices')
        for j in range(len(a)):
            k=(j+1)%len(a)
            if len({a[j],a[k],b[k],b[j]})<4:
                raise TrimError('DEGENERATE_FACE','Degenerate quad')
            faces.append((a[j],b[j],b[k],a[k]))
    if not result.closed:
        ends=[list(idx(p) for p in result.segments[0].start_ring),
              list(idx(p) for p in result.segments[-1].end_ring)]
        faces.extend((tuple(reversed(ends[0])),tuple(ends[1])))
    return {'vertices':tuple(vertices),'faces':tuple(faces),'production_uv':False,
            'source_intact':result.source_intact}
