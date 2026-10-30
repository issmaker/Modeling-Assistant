"""BH-02: isolated geometry and metric-UV planner for a single bridge quad.

No bpy imports. All input coordinates are world-space metres. This strict
foundation supports only a planar parallelogram spanning two boundary edges.
General chain matching, intersections and source-UV continuity are BH-03/04.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BridgeQuad:
    # Face order, UV are paired index-wise. Reversed=True means swap B endpoints.
    reversed_b: bool
    corners: tuple
    uv: tuple
    area_m2: float
    max_edge_m: float
    span_pixels: tuple


def _vec(a, b):
    return tuple(b[i] - a[i] for i in range(3))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _norm(a):
    return math.sqrt(_dot(a,a))


def _unit(a):
    n = _norm(a)
    if n < 1.e-10: raise ValueError('Bridge Cap: нулевое ребро')
    return tuple(v/n for v in a)


def plan_single_bridge(a0, a1, b0, b1, resolution=4096,
                       ppm=1000., safe_fraction=4000./4096.,
                       planar_tolerance_m=0.0001):
    """Plan one exact metric parallelogram bridge; reject ambiguous geometry.

    Strict limit: each selected side is one edge, no source mesh edits. The
    solver cannot know about existing faces: Blender adapter checks topology.
    """
    points = tuple(tuple(float(v) for v in p) for p in (a0,a1,b0,b1))
    if any(len(p) != 3 for p in points) or not all(math.isfinite(v) for p in points for v in p):
        raise ValueError('Bridge Cap: некорректные XYZ')
    resolution, ppm, safe_fraction = float(resolution), float(ppm), float(safe_fraction)
    if (not all(math.isfinite(v) for v in (resolution,ppm,safe_fraction)) or
            resolution <= 0 or ppm <= 0 or not 0 < safe_fraction <= 1):
        raise ValueError('Bridge Cap: неправильный UV-тайл')
    a0,a1,b0,b1 = points
    direct = _norm(_vec(a0,b0))**2 + _norm(_vec(a1,b1))**2
    reversed_cost = _norm(_vec(a0,b1))**2 + _norm(_vec(a1,b0))**2
    if abs(direct-reversed_cost) < 1.e-9:
        raise ValueError('Bridge Cap: направление пар рёбер неоднозначно')
    reverse = reversed_cost < direct
    if reverse: b0,b1 = b1,b0
    along = _vec(a0,a1)
    opposite = _vec(b0,b1)
    length = _norm(along)
    if length <= 1.e-9:
        raise ValueError('Bridge Cap: длина ребра равна нулю')
    # A single quad can be exactly isometric via affine UV only if the source
    # sides form a planar parallelogram. Otherwise require BH-04 topology solver.
    mismatch = _norm(_vec(along, opposite))
    if mismatch > max(planar_tolerance_m, length*1.e-5):
        raise ValueError('Bridge Cap: рёбра не образуют параллелограмм; Smart Corner Solver ещё требуется')
    direction = _unit(along)
    step = _vec(a0,b0)
    longitudinal = _dot(step,direction)
    perpendicular = tuple(step[i]-direction[i]*longitudinal for i in range(3))
    breadth = _norm(perpendicular)
    if breadth <= 1.e-7:
        raise ValueError('Bridge Cap: новая грань имеет нулевую площадь')
    a_cross = _cross(along,step)
    plane_err = abs(_dot(_vec(a0,b1),_unit(a_cross)))
    if plane_err > planar_tolerance_m:
        raise ValueError('Bridge Cap: будущая грань неплоская')
    area = length * breadth
    # World-isometric UV: horizontal coordinate includes the along-edge part
    # of the connector; the perpendicular coordinate follows the real width.
    uv_m = ((0.,0.),(length,0.),(length+longitudinal,breadth),(longitudinal,breadth))
    lo_u = min(u for u,v in uv_m)
    hi_u = max(u for u,v in uv_m)
    lo_v = min(v for u,v in uv_m)
    hi_v = max(v for u,v in uv_m)
    max_world_span = resolution * safe_fraction / ppm
    if max(hi_u-lo_u, hi_v-lo_v) > max_world_span+1.e-9:
        raise ValueError('Bridge Cap: геометрия выходит за один UV-тайл; разбиение будет в BH-03')
    safe_min = (1.-safe_fraction)/2.
    uv = tuple((safe_min+(u-lo_u)*ppm/resolution,
                safe_min+(v-lo_v)*ppm/resolution) for u,v in uv_m)
    return BridgeQuad(reverse, (a0,a1,b1,b0), uv, area, length,
                      ((hi_u-lo_u)*ppm,(hi_v-lo_v)*ppm))
