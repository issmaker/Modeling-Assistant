"""BH-03 Smart UV plans for *new* bridge geometry only; no bpy dependency.

The authored source edge is immutable. CONTINUE preserves its complete 2D
boundary field. ADAPTIVE interpolates to 1000 px/m in new geometry via rows.
STRICT remains owned by the legacy BH-02 bridge planner. All choices are
explicit; no automatic distortion or hidden texture-density repair.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class UVStripPlan:
    mode: str
    rows: tuple                  # ((A xyz, B xyz, A uv, B uv), ...) (world XYZ)
    source_density: float
    end_density: float
    min_density: float
    max_density: float
    transition_m: float
    source_locked: bool
    target_reached: bool
    maximum_distortion_pct: float
    far_boundary_seam_px: float | None
    tile_cells: tuple = ()  # independent new polygons clipped into SAME UV tile
    repeat_count: int = 0


def _xy(item):
    values = tuple(float(x) for x in item)
    if len(values) != 2 or any(not math.isfinite(v) for v in values):
        raise ValueError('Smart UV: недопустимая UV-координата')
    return values


def _xyz(item):
    values = tuple(float(x) for x in item)
    if len(values) != 3 or any(not math.isfinite(v) for v in values):
        raise ValueError('Smart UV: недопустимая 3D-координата')
    return values


def _dot(a, b):
    return sum(x*y for x,y in zip(a,b))


def _add(a,b):
    return tuple(a[i]+b[i] for i in range(len(a)))


def _mul(a,m):
    return tuple(x*m for x in a)


def _sub(a,b):
    return tuple(a[i]-b[i] for i in range(len(a)))


def _length(v):
    return math.sqrt(_dot(v,v))


def _lerp(a,b,t):
    return _add(_mul(a,1-t),_mul(b,t))


def _cross2(a,b):
    return a[0]*b[1]-a[1]*b[0]


def _smooth(t):
    t=max(0.,min(1.,t))
    return t*t*(3.-2.*t)


def density_observation(uv_a, uv_b, physical_length, resolution, target=1000.0, tolerance=5.):
    """Do not infer provenance: a mismatch does not prove a manual UV edit."""
    ua,ub=_xy(uv_a),_xy(uv_b)
    length=float(physical_length);resolution=float(resolution)
    if not all(map(math.isfinite,(length,resolution))) or length<=1e-9 or resolution<=0:
        raise ValueError('Smart UV: некорректная длина ребра или разрешение')
    uvspan=_length(_sub(ub,ua))
    if uvspan<=1e-12:
        raise ValueError('Smart UV: UV ребра вырождено')
    ppm=uvspan*resolution/length
    return dict(density=ppm, mismatch=abs(ppm-target)>tolerance,
                severity=('INFO' if abs(ppm-target)<=tolerance else 'WARNING'),
                message=('Исходная UV-плотность отличается от 1000 px/м; '
                         'проверьте продолжение текстуры, исходные UV не изменяются.'
                         if abs(ppm-target)>tolerance else 'UV-плотность ребра соответствует цели'))


def plan_uv_strip(a0,a1,b0,b1,source_uv0,source_uv1,outward_uv,
                  resolution=4096, mode='CONTINUE', target_ppm=1000.,
                  transition_m=0.5, far_uv=None, tile_min=0., tile_max=1.,
                  maximum_rows=24, maximum_density_deviation=0.6):
    """Build a UV-strip candidate across a *planar parallelogram* bridge.

    a0,a1 are the authored face boundary; b0,b1 are corresponding opposite
    vertices. Coordinates must be world-space meters. The source boundary is
    copied EXACTLY. The far UV boundary is not promised seamless: it may have
    a different adjacent source face. Reject illegal/flipped/out-of-tile UV.

    Adaptive uses at least 4 rows of NEW faces to reproduce non-affine UV
    fields in the mesh; it must never pretend a single quad is sufficient.
    """
    mode=str(mode).upper()
    if mode not in ('CONTINUE','ADAPTIVE'):
        raise ValueError('Smart UV: неизвестная или неподдержанная стратегия')
    a0,a1,b0,b1=map(_xyz,(a0,a1,b0,b1))
    u0,u1=map(_xy,(source_uv0,source_uv1))
    out=_xy(outward_uv)
    resolution=float(resolution); target_ppm=float(target_ppm)
    transition_m=float(transition_m)
    if (not all(map(math.isfinite,(resolution,target_ppm,transition_m))) or
            resolution<=0 or target_ppm<=0 or transition_m<=0):
        raise ValueError('Smart UV: некорректные параметры масштаба')
    if not all(map(math.isfinite,(tile_min,tile_max))) or tile_max<=tile_min:
        raise ValueError('Smart UV: некорректный UV-тайл')
    edge=_sub(a1,a0)
    span=_sub(b0,a0)
    length=_length(edge)
    if length<=1e-9:
        raise ValueError('Smart UV: нулевое исходное ребро')
    if _length(_sub(_sub(b1,b0),edge))>max(1e-4,length*1e-5):
        raise ValueError('Smart UV: требуется параллелограмм; сложный угол — BH-04')
    axis=_mul(edge,1./length)
    along=_dot(span,axis)
    cross_span=_sub(span,_mul(axis,along))
    breadth=_length(cross_span)
    if breadth<=1.e-7:
        raise ValueError('Smart UV: вырожденная новая поверхность')
    obs=density_observation(u0,u1,length,resolution,target_ppm)
    p0=obs['density']
    u_delta=_sub(u1,u0)
    uv_tangent=_mul(u_delta,1./_length(u_delta))
    norm_out=_length(out)
    if norm_out<1.e-9:
        raise ValueError('Smart UV: не определено направление UV наружу')
    out=_mul(out,1./norm_out)
    if abs(_dot(uv_tangent,out))>1.e-5:
        raise ValueError('Smart UV: направление поперёк ребра не ортогонально UV-ребру')
    if mode=='ADAPTIVE' and breadth < transition_m-1.e-9:
        raise ValueError('Smart UV: ширины полосы недостаточно для возврата к 1000; увеличьте полосу или уменьшите длину перехода')
    # Nonstandard authored UV is advisory, NOT an automatic operation blocker.
    # The geometric/Jacobian and tile guards below still reject unsafe fields.
    n=1 if mode=='CONTINUE' else min(int(maximum_rows),max(4,math.ceil(transition_m/0.08)))
    if n<=0 or n>maximum_rows or n>32:
        raise ValueError('Smart UV: недопустимое количество новых рядов')
    center=_mul(_add(u0,u1),0.5)
    target_half=_mul(uv_tangent,length*target_ppm/(resolution*2))
    target0=_sub(center,target_half)
    target1=_add(center,target_half)
    rows=[]
    for k in range(n+1):
        t=k/n
        d=breadth*t
        w=0. if mode=='CONTINUE' else _smooth(d/transition_m)
        def row_uv(source,corrected,rate):
            src=_add(source,_add(_mul(uv_tangent,along*t*p0/resolution),
                                 _mul(out,d*p0/resolution)))
            goal=_add(corrected,_add(_mul(uv_tangent,along*t*target_ppm/resolution),
                                    _mul(out,d*target_ppm/resolution)))
            return _lerp(src,goal,w)
        rows.append((_lerp(a0,b0,t),_lerp(a1,b1,t),
                     row_uv(u0,target0,p0),row_uv(u1,target1,p0)))
    # UV orientation must not invert at any strip cell, nor degenerate.
    signs=[]
    densities=[]
    for k in range(n):
        _,_,uv_a,uv_b=rows[k]
        _,_,uv_an,uv_bn=rows[k+1]
        signed=_cross2(_sub(uv_b,uv_a),_sub(uv_an,uv_a))
        signed2=_cross2(_sub(uv_bn,uv_b),_sub(uv_an,uv_b))
        if min(abs(signed),abs(signed2))<1e-12 or signed*signed2<=0:
            raise ValueError('Smart UV: UV Flip или вырожденный переход; Reduce Transition / NEW UV')
        signs.extend((signed,signed2))
        density=_length(_sub(uv_b,uv_a))*resolution/length
        densities.append(density)
    if any(s*signs[0]<=0 for s in signs):
        raise ValueError('Smart UV: инверсия UV между соседними рядами')
    # One physical tile. Clip only NEW faces, wrap their face-loop UV into
    # the same local square and retain shared world-space vertices. This may
    # introduce an intentional periodic texture boundary, NOT a new UDIM.
    from .single_tile_repeat import clip_plan_to_single_tile
    cells=clip_plan_to_single_tile(rows,tile_min,tile_max,
                                   max_cells=512,protect_start=True,
                                   protect_end=far_uv is not None)
    if rows[0][2]!=u0 or rows[0][3]!=u1:
        raise AssertionError('Smart UV: нарушена защита исходной UV-границы')
    fard=_length(_sub(rows[-1][3],rows[-1][2]))*resolution/length
    seam=None
    if far_uv is not None:
        f0,f1=map(_xy,far_uv)
        seam=max(_length(_sub(rows[-1][2],f0)),_length(_sub(rows[-1][3],f1)))*resolution
    return UVStripPlan(mode,tuple(rows),p0,fard,min(densities+[fard]),
                      max(densities+[fard]),transition_m,True,
                      mode=='ADAPTIVE' and abs(fard-target_ppm)<1e-5,
                      abs(p0-target_ppm)/target_ppm*100,seam,
                      cells,len({cell.repeat for cell in cells})-1)
