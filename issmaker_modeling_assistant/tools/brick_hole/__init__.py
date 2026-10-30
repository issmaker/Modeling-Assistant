bl_info = {
    "name": "Brick Hole Extrude — BETA 1 · FILLED UV Reliability",
    "author": "issmaker",
    "version": (1, 0, 17),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Brick Hole",
    "description": "Создание откосов отверстий с непрерывной тайловой UV-развёрткой",
    "category": "Mesh",
}

ADDON_VERSION_STRING = ".".join(map(str, bl_info["version"]))
BH0463_BUILD_ID = 'ISSMAKER_BH0463_GEOMETRY_UI_SMART_SEAM_20261026'
BH0464_BUILD_ID = 'ISSMAKER_BH0464_LARGE_CONTOUR_SAFE_DEPTH_20261026'
BH0465_BUILD_ID = 'ISSMAKER_BH0465_COLLISION_RECOVERY_20261026'
BH0466_BUILD_ID = 'ISSMAKER_BH0466_COLLISION_SWITCH_20261026'
BH0467_BUILD_ID = 'ISSMAKER_BH0467_MODULE_SEAM_NO_300_GATE_20261026'
BH0468_BUILD_ID = 'ISSMAKER_BH0468_TESSELLATION_CONTACT_ORACLE_20261026'
BH0471_BUILD_ID = 'ISSMAKER_BH0471_PRECISION_FOUNDATION_20261026'
BH0472_BUILD_ID = 'ISSMAKER_BH0472_PRO_GEOMETRY_DECISION_KERNEL_20261026'

import bpy
import bmesh
import math
import traceback
import time
import json
import hashlib
import os
import platform
import tempfile
import zipfile
from pathlib import Path
from collections import deque
from collections import Counter
from itertools import islice
from functools import wraps
from datetime import datetime, timezone
from mathutils import Vector
from bpy.props import FloatVectorProperty, BoolProperty, FloatProperty, EnumProperty, StringProperty, IntProperty
from .fixture_walls import (build_wall_fixture_gallery, validate_wall_fixture_gallery,
                            SCHEMA as FIXTURE_WALL_SCHEMA, TYPES as FIXTURE_WALL_TYPES,
                            KINDS as FIXTURE_WALL_KINDS)
from . import issue_inspector, seam_inspector
from ...core import viewport_skin as _viewport_skin
from .topology_guard import checked_edge, checked_direction, checked_depth
from .uv_source_policy import corrected_reveal_uv_edge, source_uv_warning_message
from .bridge_planner import plan_single_bridge
from .smart_uv_continuity import density_observation, plan_uv_strip
from .tessellation_reliability import verify_collinear_tessellation, TessellationCoverageError
from .robust_predicates import exact_segment_contact_3d, polygon_area_vector
from .geometry_decision import format_failure, classify_failure
from .picked_frame import frame_from_polygon, axis_vector, is_orthonormal_right_handed
from .direction_snap_core import axis_eligibility, project_vertex_depth
from .fixture_navigation import (validate_manifest, selection_target, assert_original_fixture,
                                 fixture_expected_fingerprint, geometry_fingerprint, gallery_summary,
                                 fixture_hud_layout, in_rect)


# Pure calculation helpers live here to avoid stale submodule imports.
def dot(a,b): return sum(x*y for x,y in zip(a,b))

def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])

def normalized(v):
    length=math.sqrt(dot(v,v))
    if not math.isfinite(length) or length<1e-12: raise ValueError('Нулевое направление')
    return tuple(x/length for x in v)

def canonical(v):
    v=normalized(v)
    axis=max(range(3),key=lambda i:abs(v[i]))
    return tuple(-x for x in v) if v[axis]<0 else v

def fresh_direction(candidates,tangents):
    tangents=[normalized(t) for t in tangents]
    if not tangents: raise ValueError('Нет рёбер')
    limit=math.sin(math.radians(7.5))
    valid=[]
    for n,w in candidates:
        if dot(n,n)<1e-24: continue
        n=normalized(n)
        if all(abs(dot(n,t))<=limit for t in tangents): valid.append((n,w))
    if valid:
        cosine=math.cos(math.radians(7.5))
        clusters=[]
        for seed,_ in valid:
            group=[(n,w) for n,w in valid if dot(n,seed)>=cosine]
            total=sum(w for n,w in group)
            n=normalized(tuple(sum(n[i]*w for n,w in group) for i in range(3)))
            clusters.append((total,n))
        clusters.sort(key=lambda item:(-item[0],item[1]))
        weight,n=clusters[0]
        if any(w>=weight*.95 and dot(other,n)<-cosine for w,other in clusters):
            return canonical(n),'ambiguous sign; positive dominant world axis'
        return n,'adjacent geometry'
    # At the free end of a reveal, side-face normals cannot orient depth.
    # Derive the plane from two independent contour tangents instead.
    seed=max(tangents,key=lambda v:tuple(abs(x) for x in v))
    normal=max((cross(seed,t) for t in tangents),key=lambda n:dot(n,n))
    if dot(normal,normal)<1e-12:
        raise ValueError('По прямой цепочке направление не определяется; задайте нормаль вручную')
    normal=canonical(normal)
    if any(abs(dot(normal,t))>limit for t in tangents):
        raise ValueError('Неплоский контур: автоматическая нормаль неоднозначна. Выберите X/Y/Z или XYZ в панели Brick Hole → Направление')
    return normal,'contour plane; positive dominant world axis'

def make_chart(origin, edge, depth, uv_origin, uv_edge, uv_depth):
    ee, dd, ed = dot(edge, edge), dot(depth, depth), dot(edge, depth)
    determinant = ee*dd-ed*ed
    if not math.isfinite(determinant) or determinant <= ee*dd*1e-12:
        raise ValueError('Вырожденный базис откоса')
    dual_edge = tuple((dd*edge[i]-ed*depth[i])/determinant for i in range(3))
    dual_depth = tuple((ee*depth[i]-ed*edge[i])/determinant for i in range(3))
    gradients = tuple(tuple(uv_edge[a]*dual_edge[i]+uv_depth[a]*dual_depth[i]
                            for i in range(3)) for a in (0, 1))
    return tuple(origin), tuple(uv_origin), gradients

def chart_uv(chart, coordinate):
    origin, uv, gradients = chart
    relative = tuple(float(coordinate[i])-origin[i] for i in range(3))
    return tuple(uv[a]+dot(gradients[a], relative) for a in (0, 1))

def chart_plane(chart, axis, border):
    origin, uv, gradients = chart
    gradient = gradients[axis]
    length2 = dot(gradient, gradient)
    if length2 <= 1e-24:
        raise ValueError('Нулевая UV-производная при разрезании')
    offset = (border-uv[axis])/length2
    point = tuple(origin[i]+gradient[i]*offset for i in range(3))
    length = math.sqrt(length2)
    return point, tuple(x/length for x in gradient)

# Pass 2: geometric invariants. Pure math so they can be fuzz-tested without bpy.
UV_AFFINE_MAX_ERROR_PIXELS = 0.25
UV_METRIC_MAX_ERROR_PIXELS_PER_METER = 7.5
UV_METRIC_ABSOLUTE_ERROR_PIXELS = 0.25
CONTOUR_COLLISION_DISTANCE_METERS = 1.0e-6


def quad_nonplanarity_meters(points):
    """Height of the fourth quad corner above the other three-corner plane."""
    if len(points) != 4:
        raise ValueError("Проверка планарности требует четыре угла")
    a, b, c, d = points
    ab = tuple(b[i] - a[i] for i in range(3))
    ad = tuple(d[i] - a[i] for i in range(3))
    n = cross(ab, ad)
    magnitude = math.sqrt(dot(n, n))
    if magnitude <= 1e-12:
        raise ValueError("Вырожденная грань: невозможно построить плоскость")
    ac = tuple(c[i] - a[i] for i in range(3))
    return abs(dot(n, ac)) / magnitude


def affine_corner_error_pixels(positions, uvs, chart, resolution):
    """An affine chart is usable only if it reproduces EVERY original UV corner.

    Twisted and varying-depth Filled-mode quads are not assumed affine.
    """
    if len(positions) != len(uvs) or len(positions) < 3:
        raise ValueError("Некорректная UV-геометрия для проверки аффинности")
    result = 0.0
    for point, uv in zip(positions, uvs):
        predicted = chart_uv(chart, point)
        result = max(result, math.hypot(*(resolution * (predicted[i] - uv[i]) for i in (0, 1))))
    if not math.isfinite(result):
        raise ValueError("UV-карта содержит NaN или бесконечность")
    return result


def triangle_uv_metric_error_pixels(positions, uvs, resolution, pixels_per_meter=1000.0):
    """Absolute UV length error in pixels, max across the three triangle edges.

    Works in WORLD metres (caller transforms vertices); does not depend on
    chart orientation, mirrored UV, or a triangulation's diagonal choice.
    """
    if len(positions) != 3 or len(uvs) != 3:
        raise ValueError("Для проверки текселя нужен треугольник")
    errors = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        distance = math.dist(positions[i], positions[j])
        pixel_length = math.dist(uvs[i], uvs[j]) * resolution
        if not math.isfinite(distance) or not math.isfinite(pixel_length):
            raise ValueError("Некорректные координаты при проверке плотности UV")
        errors.append((abs(pixel_length - distance * pixels_per_meter), distance))
    return errors


def validate_triangle_uv_metric(positions, uvs, resolution):
    """Reject stretched/skewed triangles, not just out-of-tile UV bounds."""
    for error_px, length_m in triangle_uv_metric_error_pixels(positions, uvs, resolution):
        allowed = max(UV_METRIC_ABSOLUTE_ERROR_PIXELS,
                      length_m * UV_METRIC_MAX_ERROR_PIXELS_PER_METER)
        if error_px > allowed:
            raise ValueError(
                f"Искажение UV откоса: {error_px:.3f} px при длине {length_m:.5f} м "
                f"(допустимо {allowed:.3f} px). Требуется 1000 px/м"
            )


def closest_segment_distance_sq(a, b, c, d):
    """Squared minimum 3-D distance between finite segments, including overlaps."""
    u = tuple(b[i] - a[i] for i in range(3))
    v = tuple(d[i] - c[i] for i in range(3))
    w = tuple(a[i] - c[i] for i in range(3))
    aa, bb, cc = dot(u, u), dot(u, v), dot(v, v)
    dd, ee = dot(u, w), dot(v, w)
    if aa <= 1e-24 or cc <= 1e-24:
        raise ValueError("Контур содержит вырожденный сегмент")
    denom = aa * cc - bb * bb
    sn, sd = denom, denom
    tn, td = denom, denom
    if denom <= 1e-24 * max(aa * cc, 1e-24):
        sn, sd = 0.0, 1.0
        tn, td = ee, cc
    else:
        sn = bb * ee - cc * dd
        tn = aa * ee - bb * dd
        if sn < 0:
            sn, tn, td = 0.0, ee, cc
        elif sn > sd:
            sn, tn, td = sd, ee + bb, cc
    if tn < 0:
        tn = 0.0
        if -dd < 0:
            sn = 0.0
        elif -dd > aa:
            sn, sd = 1.0, 1.0
        else:
            sn, sd = -dd, aa
    elif tn > td:
        tn = td
        if -dd + bb < 0:
            sn = 0.0
        elif -dd + bb > aa:
            sn, sd = 1.0, 1.0
        else:
            sn, sd = -dd + bb, aa
    sc = 0.0 if abs(sn) < 1e-24 else sn / sd
    tc = 0.0 if abs(tn) < 1e-24 else tn / td
    diff = tuple(w[i] + sc * u[i] - tc * v[i] for i in range(3))
    return max(0.0, dot(diff, diff))


def verify_contour_segments(segments, tolerance=CONTOUR_COLLISION_DISTANCE_METERS):
    """Detect proven 3D contacts between segments without a common topology vertex.

    BH0472: ``tolerance`` is only a broad-phase / near-miss radius.  A hard
    block requires ``exact_segment_contact_3d`` on the stored binary floats.
    This prevents a visually valid contour from being rejected merely because
    two independent edges pass within one micron of one another.
    """
    ordered = []
    for a_id, b_id, a, b in segments:
        if not all(math.isfinite(v) for point in (a, b) for v in point):
            raise ValueError("В контуре найдены некорректные координаты")
        if math.dist(a, b) <= max(1e-15, math.ulp(max(map(abs, (*a, *b)), default=0.0)) * 4):
            raise ValueError("Контур содержит доказанно вырожденное ребро")
        ordered.append((min(a[0], b[0]), max(a[0], b[0]), a_id, b_id, tuple(a), tuple(b)))
    ordered.sort(key=lambda item: item[0])
    active = []
    tol2 = tolerance ** 2
    near_misses = 0
    for item in ordered:
        xlo, _, a_id, b_id, a, b = item
        active = [other for other in active if other[1] + tolerance >= xlo]
        for other in active:
            _, _, c_id, d_id, c, d = other
            if a_id is c_id or a_id is d_id or b_id is c_id or b_id is d_id:
                continue
            if (max(min(a[1], b[1]), min(c[1], d[1])) >
                    min(max(a[1], b[1]), max(c[1], d[1])) + tolerance or
                max(min(a[2], b[2]), min(c[2], d[2])) >
                    min(max(a[2], b[2]), max(c[2], d[2])) + tolerance):
                continue
            distance_sq = closest_segment_distance_sq(a, b, c, d)
            if distance_sq > tol2:
                continue
            if exact_segment_contact_3d(a, b, c, d):
                issue_inspector.capture_collision(a,b,c,d)
                record_action('contour_collision_witness',
                              f'issues={len(issue_inspector.issues())}; exact stored-coordinate contact')
                raise ValueError("Контур имеет доказанное самопересечение/касание несвязанных рёбер")
            near_misses += 1
            # Diagnostic only: near geometry is not proof of invalid topology.
            if near_misses <= 8:
                record_action('contour_near_miss_allowed',
                              f'distance_m={math.sqrt(max(0.0,distance_sq)):.12g}; '
                              f'broadphase_tol_m={tolerance:.12g}; exact_contact=NO')
        active.append(item)
    return {'near_misses': near_misses, 'proven_contacts': 0}


def _uv_cross2(a, b):
    return a[0] * b[1] - a[1] * b[0]


def unfolded_third_uv(p, q, r, uv_p, uv_q, resolution, side):
    """Isometrically unfold a real 3D triangle, anchored to two known UVs.

    Using the existing UV length for P-Q preserves the source brick texture.
    Both remaining physical edge lengths are enforced to 1000 px/m. A badly
    scaled source edge or degenerate triangle fails closed.
    """
    if resolution <= 0 or side not in (-1, 1):
        raise ValueError('Некорректные параметры UV-развёртки')
    coords = tuple(tuple(float(x) for x in v) for v in (p, q, r))
    values = tuple(tuple(float(x) for x in v) for v in (uv_p, uv_q))
    if not all(math.isfinite(x) for v in (*coords, *values) for x in v):
        raise ValueError('Неопределённые координаты UV-развёртки')
    scale = float(resolution) / PIXELS_PER_METER
    edge_uv = (values[1][0]-values[0][0], values[1][1]-values[0][1])
    base = math.hypot(*edge_uv) * scale
    pr = math.dist(coords[0], coords[2])
    qr = math.dist(coords[1], coords[2])
    physical_base = math.dist(coords[0], coords[1])
    triangle_rejected = (min(base, pr, qr) <= 1.0e-8 or
                         base >= pr + qr - 1.0e-9 or
                         base <= abs(pr-qr) + 1.0e-9)
    # W58.20: A ~2-micron nearly collapsed geometric edge, after float UV
    # transfer, can be shorter in UV by <0.002 px. Exact circle intersection
    # is ill-conditioned; keep the authored anchor UV and use the closest
    # right-triangle chart only when *both* independent metric errors are
    # under 0.05 pixels. This is numeric uncertainty, NOT bad geometry.
    tiny = (triangle_rejected and 1.0e-8 < base <= 1.0e-5 and
            1.0e-8 < physical_base <= 1.0e-5 and
            pr > 1.0e-5 and qr > 1.0e-5)
    if tiny:
        error_anchor_px = abs(base - physical_base) * PIXELS_PER_METER
        error_side_px = abs(math.hypot(pr, base) - qr) * PIXELS_PER_METER
        if error_anchor_px <= 0.05 and error_side_px <= 0.05:
            x = 0.0
            h = pr * side
            e = (edge_uv[0]/math.hypot(*edge_uv), edge_uv[1]/math.hypot(*edge_uv))
            factor = PIXELS_PER_METER / float(resolution)
            print('W5820_NUMERIC_MICROEDGE_CONDITIONED',
                  f'anchor_err_px={error_anchor_px:.6g}',
                  f'side_err_px={error_side_px:.6g}',
                  flush=True)
            return (values[0][0] + (e[0]*x-e[1]*h)*factor,
                    values[0][1] + (e[1]*x+e[0]*h)*factor)
    if triangle_rejected:
        raise ValueError(
            f'UV_ANCHOR_TRIANGLE_METRIC_CONFLICT: anchor_uv_m={base:.8g} '
            f'world_anchor_m={physical_base:.8g} pr_m={pr:.8g} qr_m={qr:.8g}; '
            f'class={"NUMERIC_UNCERTAINTY" if tiny else "PROVEN_GEOMETRY_OR_UV_INCONSISTENCY"}')
    x = (pr*pr + base*base - qr*qr) / (2.0*base)
    h_sq = pr*pr-x*x
    if h_sq <= 1.0e-16:
        raise ValueError('Вырожденная развёртка треугольника откоса')
    h = math.sqrt(h_sq) * side
    e = (edge_uv[0]/math.hypot(*edge_uv), edge_uv[1]/math.hypot(*edge_uv))
    factor = PIXELS_PER_METER / float(resolution)
    return (values[0][0] + (e[0]*x-e[1]*h)*factor,
            values[0][1] + (e[1]*x+e[0]*h)*factor)


def unfold_reveal_quad(world_points, uv_a, uv_b, uv_out, resolution, triangles):
    """Unfold BMesh's ACTUAL two triangles; maintain their common diagonal.

    Point indices 0=A and 1=B are the authored source wall edge; 2=A' and
    3=B' are native Blender's extruded counterparts. The triangle with A-B
    anchors the original UV brick layout. The other opens on the opposite side
    of its shared diagonal. This also works for twisted/non-developable quads,
    where forcing four precomputed UV corners onto both triangles cannot work.
    """
    if len(world_points)!=4 or len(triangles)!=2 or any(len(t)!=3 for t in triangles):
        raise ValueError('Для UV-развёртки требуется quad из двух треугольников')
    faces=[tuple(int(i) for i in t) for t in triangles]
    if any(len(set(t))!=3 or any(i not in range(4) for i in t) for t in faces):
        raise ValueError('Повреждены индексы треугольников откоса')
    shared=set(faces[0])&set(faces[1])
    if len(shared)!=2 or set(faces[0])|set(faces[1])!=set(range(4)):
        raise ValueError('Триангуляция откоса содержит разрыв или наложение')
    anchors=[i for i,t in enumerate(faces) if 0 in t and 1 in t]
    if len(anchors)!=1:
        raise ValueError('Исходное ребро стены потеряно при триангуляции')
    first=faces[anchors[0]]
    third=(set(first)-{0,1}).pop()
    uv={0:tuple(uv_a),1:tuple(uv_b)}
    uv_edge=(uv[1][0]-uv[0][0],uv[1][1]-uv[0][1])
    sense=_uv_cross2(uv_edge,uv_out)
    if abs(sense)<1.0e-12:
        raise ValueError('Неоднозначная ориентация исходных UV')
    uv[third]=unfolded_third_uv(world_points[0],world_points[1],world_points[third],
                               uv[0],uv[1],resolution,1 if sense>0 else -1)
    second=faces[1-anchors[0]]
    a,b=sorted(shared)
    first_unique=(set(first)-shared).pop()
    last=(set(second)-shared).pop()
    reference=_uv_cross2((uv[b][0]-uv[a][0],uv[b][1]-uv[a][1]),
                         (uv[first_unique][0]-uv[a][0],uv[first_unique][1]-uv[a][1]))
    if abs(reference)<1.0e-12:
        raise ValueError('Треугольники UV на одной линии')
    uv[last]=unfolded_third_uv(world_points[a],world_points[b],world_points[last],
                              uv[a],uv[b],resolution,-1 if reference>0 else 1)
    for t in faces:
        validate_triangle_uv_metric([world_points[i] for i in t],[uv[i] for i in t],resolution)
    return uv


def quad_uv_metric_exceeds_tolerance(positions, uvs, resolution):
    """Return True when a nominal quad needs two physically exact charts."""
    if len(positions)!=4 or len(uvs)!=4:
        raise ValueError('Некорректный quad откоса')
    for tri in ((0,1,2),(0,2,3)):
        for error_px, length in triangle_uv_metric_error_pixels(
                [positions[i] for i in tri],[uvs[i] for i in tri],resolution):
            if error_px > max(UV_METRIC_ABSOLUTE_ERROR_PIXELS,
                              length*UV_METRIC_MAX_ERROR_PIXELS_PER_METER):
                return True
    return False


def safe_tessellation_indices(tessellation, vertex_count):
    """Check true polygon tessellation instead of assuming a vertex-zero fan."""
    if vertex_count<3 or vertex_count>MAX_PARTS:
        raise ValueError('Недопустимое количество вершин UV-полигона')
    triangles=[]
    for raw in tessellation:
        if len(raw)!=3 or any(type(i) is not int or not 0<=i<vertex_count for i in raw):
            raise ValueError('Некорректная триангуляция UV-полигона')
        if len(set(raw))!=3:
            raise ValueError('Триангуляция содержит повторяющуюся вершину')
        triangles.append(tuple(raw))
    if len(triangles)!=vertex_count-2:
        raise ValueError('Триангуляция полигона неполная')
    return triangles


def fixture_selected_case_ids(face_ids, expected_kind, case_metadata):
    """Map the ACTUAL selected faces / linked-edge wall faces to case names.

    Never replace absent/multiple/invalid case provenance with the navigation
    cursor, which can point to a completely different test wall.
    """
    ids=set()
    for raw in face_ids:
        if type(raw) is not int or raw not in range(1,len(case_metadata)+1):
            continue
        ids.add(raw)
    if not ids:
        return 'UNKNOWN', 'NO_MATCH'
    found=[]
    for i in sorted(ids):
        rec=case_metadata[i-1]
        if rec.get('kind')!=expected_kind:
            return 'UNKNOWN', 'KIND_MISMATCH'
        found.append(str(rec['id']))
    return ','.join(found), 'MATCH' if len(found)==1 else 'MULTIPLE'


def face_uv_density_gate(face, uv_layer, world_matrix, resolution):
    """Validate the triangles actually tessellated by Blender (not a fan).

    On concave n-gons, a fan from vertex zero can include triangles outside
    the polygon and falsely reject/accept UV. The same real tessellation used
    by Blender is the geometric authority for the per-triangle metric.
    """
    loops=list(face.loops)
    if len(loops)<3:
        raise ValueError('Вырожденная грань откоса')
    # The UV metric depends on scale/rotation but NOT on world translation.
    # Blender Vector/Matrix arithmetic can round small geometry away at large
    # CAD/world offsets before the tessellation checker even sees the vertices.
    metric_transform = world_matrix.to_3x3()
    coords=[tuple(metric_transform @ loop.vert.co) for loop in loops]
    uvs=[tuple(loop[uv_layer].uv) for loop in loops]
    def capture_metric_witness(reason):
        _capture_tessellation_witness(
            face.index,
            [tuple(world_matrix @ loop.vert.co) for loop in loops],
            uvs, raw_tessellation, resolution, reason,
            metric_coords=coords,
            world_origin=tuple(world_matrix.translation))
    # The Blender tessellator may include every polygon vertex while local
    # authored UV contains a kink on a *straight* 3D boundary. Triangle
    # lengths alone can conceal a short-edge angular change. Test source
    # boundary straightness and UV consistency independently before tessellating.
    from .tessellation_reliability import reject_collinear_uv_kinks
    reject_collinear_uv_kinks(coords, uvs, resolution)
    from mathutils.geometry import tessellate_polygon
    from mathutils import Vector
    raw_tessellation = [tuple(i for i in tri) for tri in
                        tessellate_polygon([[Vector(p) for p in coords]])]
    try:
        triangles = safe_tessellation_indices(raw_tessellation, len(loops))
    except ValueError as exc:
        if str(exc) == 'Триангуляция полигона неполная':
            try:
                coverage_evidence = {}
                triangles = verify_collinear_tessellation(
                    coords, uvs, raw_tessellation, resolution,
                    diagnostics=coverage_evidence)
                uncertainty = bool(coverage_evidence.get('precision_uncertain'))
                write_log(f'UV_TESSELLATION_COLLINEAR_VERIFIED '
                          f'face={face.index}; loops={len(loops)}; '
                          f'triangles={len(triangles)}; source UV preserved; '
                          f'precision_uncertain={uncertainty}')
                if uncertainty:
                    record_action('tessellation_precision_advisory',
                                  f'face={face.index}; accepted_by_area_boundary_topology=YES; '
                                  f'ulp={coverage_evidence.get("stored_ulp")}; '
                                  f'extent={coverage_evidence.get("extent")}; '
                                  f'uncertain_skipped={coverage_evidence.get("uncertain_skipped_vertices",())}')
            except (ValueError, TypeError) as coverage_error:
                capture_metric_witness(str(coverage_error))
                raise ValueError(
                    f'Триангуляция полигона неполная: {coverage_error}') from coverage_error
        else:
            capture_metric_witness(str(exc))
            raise
    valid=0
    for tri in triangles:
        a,b,c=(coords[i] for i in tri)
        ab=tuple(b[i]-a[i] for i in range(3))
        ac=tuple(c[i]-a[i] for i in range(3))
        if dot(cross(ab,ac),cross(ab,ac))<=1.0e-20:
            continue  # BMesh cut may produce collinear vertices.
        validate_triangle_uv_metric([coords[i] for i in tri],
                                    [uvs[i] for i in tri],resolution)
        valid+=1
    if valid==0:
        raise ValueError('UV-проверка: у откоса нет треугольника ненулевой площади')


def non_target_mesh_invariants(bm, uv_layer, result_layer, world_matrix):
    """Material-wise 3D surface and UV integral for all NON-reveal faces.

    BH0472: world surface area vectors are accumulated in a face-local origin.
    The previous absolute ``cross(world_a, world_b)`` shoelace is mathematically
    translation invariant but numerically unstable at CAD-scale offsets.
    """
    result = {}
    for face in bm.faces:
        if face[result_layer] != 0:
            continue
        values = result.setdefault(face.material_index, [0.0] * 7)
        loops = list(face.loops)
        if not loops:
            continue
        # W58.31: subtract the author-local anchor *before* transforming.
        # Blender mathutils Matrix/Vector are float32-backed and adding a
        # 1e12m world translation to 1m geometry destroys all 1m deltas.
        # Cross-products depend only on these linear transformed differences.
        anchor_local=loops[0].vert.co.copy()
        linear=world_matrix.to_3x3()
        relative=[tuple(linear @ (loop.vert.co-anchor_local)) for loop in loops]
        area_vector=polygon_area_vector(relative)
        for axis in range(3):
            values[axis] += area_vector[axis]
        # UV first moments are already in texture-local coordinates.
        for index, loop in enumerate(loops):
            nxt = loops[(index + 1) % len(loops)]
            u, v = tuple(loop[uv_layer].uv)
            u_next, v_next = tuple(nxt[uv_layer].uv)
            delta = u * v_next - u_next * v
            values[3] += delta * 0.5
            values[4] += delta * (u + u_next) / 6.0
            values[5] += delta * (v + v_next) / 6.0
        values[6] += 1
    return result


def compare_non_target_mesh_invariants(before, after):
    """Fail closed if bisect alters untouched wall/cap material or UV surface."""
    if set(before) != set(after):
        raise ValueError("Нецелевые полигоны изменили список используемых материалов")
    for material in before:
        old = before[material]
        new = after[material]
        for i, name in enumerate(('world X area', 'world Y area', 'world Z area',
                                  'UV area', 'UV U moment', 'UV V moment')):
            scale = max(abs(old[i]), abs(new[i]))
            tolerance = max(2.0e-5, scale * 5.0e-4)
            if not all(math.isfinite(x) for x in (old[i], new[i])) or abs(old[i] - new[i]) > tolerance:
                raise ValueError(
                    f"Неизбранная геометрия: изменился {name} материала {material}; "
                    f"до {old[i]:.9g}, после {new[i]:.9g}"
                )


UV_BORDER_SNAP_TOLERANCE = 4.0e-6
UNIT_SCALE_TOLERANCE = 1.0e-5
NORMAL_SIGN_TOLERANCE = 1.0e-4


def snap_uv_to_border(value, low, span, tolerance=4.0e-6):
    """Remove float drift introduced when a BMesh bisect lands on a UV border."""
    border = low + round((value - low) / span) * span
    return border if abs(value - border) <= tolerance else value


def tile_for_values(values, low, span, tolerance=4.0e-6):
    """Choose a tile containing every vertex; never conceal an uncut face."""
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError('Некорректные UV')
    minimum = math.ceil((max(values)-low-span-tolerance)/span)
    maximum = math.floor((min(values)-low+tolerance)/span)
    if minimum > maximum:
        raise ValueError(f'Грань пересекает UV-границу: [{min(values):.9f}, {max(values):.9f}]')
    center = math.floor((sum(values)/len(values)-low)/span)
    return min(max(center, minimum), maximum)


EPS = 1.0e-6
FIXED_TILE_SIZE_METERS = 4.0
TEXTURE_RESOLUTION = 4096.0
PIXELS_PER_METER = 1000.0
CONTENT_PIXELS = FIXED_TILE_SIZE_METERS * PIXELS_PER_METER
UV_MARGIN = (TEXTURE_RESOLUTION - CONTENT_PIXELS) / (2.0 * TEXTURE_RESOLUTION)
UV_SAFE_MIN = UV_MARGIN
UV_SAFE_MAX = 1.0 - UV_MARGIN
UV_USABLE_SPAN = UV_SAFE_MAX - UV_SAFE_MIN
LOG_TEXT_NAME = "BrickHole_Log"
RESULT_LAYER_NAME = "brick_hole_result"
ERROR_LAYER_NAME = "brick_hole_error"
TEMP_CAP_LAYER_NAME = "brick_hole_temp_cap"



MAX_SOURCE_ELEMENTS = 1500000
MAX_SELECTED_EDGES = 2000
MAX_CUTS = 4000
MAX_PARTS = 20000
_TRANSACTION_ACTIVE = False
# PASS05: original safety rollback is retained. Store the FIRST precise failure
# from nested bpy.ops so the outer operator never erases it with generic FILLED.
_TRANSACTION_FAILURE_REASON = None
_TRANSACTION_STAGE = 'IDLE'


def _build_stage(stage, detail=''):
    global _TRANSACTION_STAGE
    _TRANSACTION_STAGE = str(stage)[:64]
    record_action('build_stage', f'{_TRANSACTION_STAGE}: {detail}'[:400])
    write_log(f'BUILD_STAGE [{_TRANSACTION_STAGE}]: {detail}')


def _build_failed(reason):
    global _TRANSACTION_FAILURE_REASON
    if _TRANSACTION_FAILURE_REASON is None:
        _TRANSACTION_FAILURE_REASON = f'{_TRANSACTION_STAGE}: {reason}'[:420]
        record_action('build_failure', _TRANSACTION_FAILURE_REASON)
    return _TRANSACTION_FAILURE_REASON


def _recent_failure_log():
    """Best-effort last explicit ERROR entry, never a replacement for traceback."""
    try:
        text = bpy.data.texts.get(LOG_TEXT_NAME)
        if text is not None:
            for line in reversed(text.as_string().splitlines()):
                line = line.strip()
                if line.startswith(('ERROR:', 'ERROR during', 'VIOLATION')):
                    return line[:220]
    except (RuntimeError, AttributeError, ReferenceError):
        pass
    return 'подробности в BrickHole_Log / диагностике ZIP'



def selected_face_components(faces):
    """Stable connected FACE components in the order supplied by the Edit BMesh.

    The previous implementation seeded from an unordered set and returned sets,
    making native batch order depend on hash iteration.  Input is the BMesh face
    sequence (never reconstruct a mesh ordering from transient Python object ids).
    """
    ordered = list(dict.fromkeys(faces))
    rank = {face: index for index, face in enumerate(ordered)}
    remaining = set(ordered)
    components = []
    for seed in ordered:
        if seed not in remaining:
            continue
        queue = [seed]
        cursor = 0
        component = []
        while cursor < len(queue):
            face = queue[cursor]
            cursor += 1
            if face not in remaining:
                continue
            remaining.remove(face)
            component.append(face)
            neighbors = {other for edge in face.edges for other in edge.link_faces
                         if other in remaining}
            queue.extend(sorted(neighbors, key=rank.__getitem__))
        components.append(tuple(component))
    return components


def boundary_edges_for_faces(faces):
    """Return each selected region boundary once, in input face/loop order."""
    ordered = list(dict.fromkeys(faces))
    face_set = set(ordered)
    result, seen = [], set()
    for face in ordered:
        for edge in face.edges:
            if edge in seen:
                continue
            seen.add(edge)
            if sum(linked in face_set for linked in edge.link_faces) == 1:
                result.append(edge)
    return result


def created_faces_since(before_faces, current_faces):
    """BMesh operators may omit created side faces from their result geom."""
    before = set(before_faces)
    return [face for face in current_faces if face not in before]


def face_coordinate_signature(face, digits=7):
    return tuple(sorted(coordinate_key(vert.co, digits) for vert in face.verts))


def created_faces_from_signatures(before_signatures, current_faces):
    """Find new faces after a native bpy operator invalidated BMesh handles."""
    remaining = Counter(before_signatures)
    created = []
    for face in current_faces:
        key = face_coordinate_signature(face)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            created.append(face)
    return created


def extruded_counterpart(face, source_co, other_source_co):
    """Return the new vertex connected to one old endpoint of a side quad."""
    source_loop = min(
        face.loops,
        key=lambda loop: (loop.vert.co - source_co).length_squared,
    )
    if (source_loop.vert.co - source_co).length_squared > 1.0e-10:
        raise RuntimeError("Не найдена исходная вершина на боковой грани")
    neighbors = (
        source_loop.link_loop_prev.vert,
        source_loop.link_loop_next.vert,
    )
    candidates = [
        vert for vert in neighbors
        if (vert.co - other_source_co).length_squared > 1.0e-10
    ]
    if len(candidates) != 1:
        raise RuntimeError("Не удалось определить новую вершину Extrude Along Normals")
    return candidates[0]


def selected_region_world_normal(obj, faces, require_consistent_sign=False):
    """Area-weighted diagnostic direction; Blender owns native FACE displacement.

    For strong bends the sign of a face normal is not an orientation test: a
    correctly wound 120-degree fold has a negative normal dot product.  If a
    selected component contains such a fold, verify shared-edge winding before
    accepting the diagnostic, while rejecting near-opposite/ambiguous cases.
    """
    faces = list(faces)
    normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()
    orientation_sign = -1.0 if obj.matrix_world.to_3x3().determinant() < 0 else 1.0
    normals = []
    for face in faces:
        normal = orientation_sign * (normal_matrix @ face.normal)
        if normal.length <= EPS:
            raise ValueError("В выделении найден полигон с нулевой нормалью")
        normal.normalize()
        linear=obj.matrix_world.to_3x3()
        origin=face.verts[0].co
        local_world=[linear @ (vert.co-origin) for vert in face.verts]
        area=math.fsum(
            local_world[index].cross(local_world[index+1]).length*0.5
            for index in range(1,len(local_world)-1)
        )
        normals.append((normal, max(area, EPS)))
    if not normals:
        raise ValueError("Нет полигонов для проверки нормалей")
    reference = max(normals, key=lambda item: item[1])[0]
    signed_alignments = [normal.dot(reference) for normal, area in normals]
    strong_fold = require_consistent_sign and any(
        alignment < -NORMAL_SIGN_TOLERANCE for alignment in signed_alignments
    )
    if strong_fold:
        # A 175..180 degree reversal has virtually no safe reveal thickness.
        if min(signed_alignments) <= -math.cos(math.radians(5.0)):
            raise ValueError("Нормали почти противоположны (сгиб 175–180°); откос небезопасен")
    if require_consistent_sign and len(faces) > 1:
        selected = set(faces)
        shared_count = 0
        seen_edges = set()
        for face in faces:
            for edge in getattr(face, 'edges', ()):
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                adjacent = [other for other in edge.link_faces if other in selected]
                if len(adjacent) > 2 or (len(adjacent) == 2 and len(edge.link_faces) > 2):
                    raise ValueError('Non-manifold внутреннее ребро FILLED-компонента')
                if len(adjacent) != 2:
                    continue
                shared_count += 1
                a, b = edge.verts
                directions = []
                for neighbor in adjacent:
                    ring = list(neighbor.verts)
                    directions.append(next((1 if ring[i] is a and ring[(i + 1) % len(ring)] is b
                                            else -1
                                            for i in range(len(ring))
                                            if (ring[i] is a and ring[(i + 1) % len(ring)] is b)
                                            or (ring[i] is b and ring[(i + 1) % len(ring)] is a)), 0))
                if directions[0] == 0 or directions[1] == 0 or directions[0] == directions[1]:
                    raise ValueError('Несогласованная ориентация соседних FILLED-граней; исправьте normals')
        if strong_fold and shared_count < len(faces) - 1:
            raise ValueError('Нельзя доказать связность и направление FILLED-сгиба')
        if strong_fold:
            write_log('Нативный FILLED-сгиб: нормали >90°, общая ориентация проверена по рёбрам; UV и геометрия подлежат итоговой проверке')
    aligned = []
    maximum_angle = 0.0
    for (normal, area), alignment in zip(normals, signed_alignments):
        maximum_angle = max(
            maximum_angle,
            math.degrees(math.acos(max(-1.0, min(1.0, alignment if require_consistent_sign
                                         else abs(alignment))))),
        )
        if alignment < 0.0 and not require_consistent_sign:
            normal = -normal
        aligned.append((normal, area))
    result = sum((normal * area for normal, area in aligned),
                 Vector((0.0, 0.0, 0.0)))
    if result.length <= EPS:
        result = reference.copy()
    else:
        result.normalize()
    # Every face retains its true inverse-transpose world normal; in
    # particular disconnected areas and 90° folds are never displaced by
    # the average direction displayed here. Actual displacement stays native.
    min_alignment = min(signed_alignments)
    write_log(
        f"Selected region normals (diagnostic only): faces={len(faces)}; "
        f"maximum deviation={maximum_angle:.3f} deg; "
        f"minimum signed alignment={min_alignment:.6f}; "
        f"area-weighted normal={vec_text(result)}; movement=NATIVE_PER_FACE_EVEN"
    )
    return result


def resolve_source_face(obj, edge, dominant_normal, selected_faces=None, face_mode=False):
    """A single UV source policy shared by preflight and the geometry core.

    Filled mode uses the surrounding wall before the selected cap; edge mode
    takes the most aligned neighboring wall face. Never mutate the mesh here.
    """
    if face_mode:
        selected = set(selected_faces or ())
        outside = [face for face in edge.link_faces if face not in selected]
        if outside:
            return max(outside, key=lambda face: (
                face_world_normal(obj, face).dot(dominant_normal), -face.index,
            ))
        return next((face for face in edge.link_faces if face in selected), None)
    return source_wall_face(obj, edge, dominant_normal)


def invalidate_qa(scene):
    """Do not display stale validation counts as a check of newly built geometry."""
    if hasattr(scene, 'brick_hole_qa_state'):
        scene.brick_hole_qa_state = 'Не проверено после изменения геометрии'
    if hasattr(scene, 'brick_hole_last_checked'):
        scene.brick_hole_last_checked = 0
    if hasattr(scene, 'brick_hole_last_errors'):
        scene.brick_hole_last_errors = 0
    # A successful extrude invalidates the earlier preflight snapshot.
    if hasattr(scene, 'brick_hole_preview_text'):
        scene.brick_hole_preview_text = 'Геометрия изменена — запустите Preview заново'
        if hasattr(scene, 'brick_hole_preview_warnings'):
            scene.brick_hole_preview_warnings = 1
        if hasattr(scene, 'brick_hole_preview_verified'):
            scene.brick_hole_preview_verified = False
        if hasattr(scene, 'brick_hole_preview_signature'):
            scene.brick_hole_preview_signature = ''


def partition_brick_selection(bm, mesh_select_mode=None):
    """Exactly one edit selection domain per operation, never FACE+EDGE.

    Blender implicitly selects edges/vertices belonging to selected faces.
    This is NOT a mixed selection: face mode owns the complete region.
    Multi-mode selection is intentionally blocked before any mutation.
    """
    mode = tuple(bool(v) for v in mesh_select_mode) if mesh_select_mode is not None else None
    if mode is not None:
        if len(mode) != 3 or sum(mode) != 1 or mode[0]:
            raise ValueError('Выберите один режим выделения: рёбра (2) или грани (3); совместный режим отключён')
        if mode[2]:
            faces = [f for f in bm.faces if f.select and not f.hide]
            return faces, [], {edge for f in faces for edge in f.edges}
        return [], [e for e in bm.edges if e.select and not e.hide], set()
    # Older headless tools may not provide tool_settings: fail closed on any
    # ambiguous independent edge alongside selected faces.
    faces = [f for f in bm.faces if f.select and not f.hide]
    face_edges = {e for f in faces for e in f.edges}
    separate_edges = [e for e in bm.edges if e.select and not e.hide and e not in face_edges]
    if faces and separate_edges:
        raise ValueError('Нельзя одновременно обрабатывать рёбра и грани; выберите один режим')
    if faces:
        return faces, [], face_edges
    return [], [e for e in bm.edges if e.select and not e.hide], set()


def filled_boundary_world_normal(obj, edge, selected_faces):
    """Use THIS boundary face's local normal, not a global averaged normal.

    Disconnected regions may legitimately point in different directions.
    Blender's native Extrude Faces Along Normals owns the displacement.
    """
    selected = set(selected_faces)
    adjacent = [f for f in edge.link_faces if f in selected]
    if len(adjacent) != 1:
        raise ValueError('Неоднозначная граница FILLED: ожидается ровно одна выбранная грань у ребра')
    normal = face_world_normal(obj, adjacent[0])
    if not all(math.isfinite(float(v)) for v in normal) or normal.length <= EPS:
        raise ValueError('Грань имеет неверную локальную нормаль')
    return normal


def unified_preflight(context, depth):
    """One read-only FACE or EDGE gate; never execute a mixed transaction."""
    obj = getattr(context, 'edit_object', None)
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        raise ValueError('Нужен Mesh в Edit Mode')
    bm = bmesh.from_edit_mesh(obj.data)
    faces, edges, face_owned = partition_brick_selection(
        bm, getattr(getattr(context, 'tool_settings', None), 'mesh_select_mode', None))
    if not faces and not edges:
        raise ValueError('Выделите рёбра ИЛИ полигоны в соответствующем режиме')
    is_face = bool(faces)
    summary = safety_preflight(context, depth, face_mode=is_face,
                               selected_faces_override=faces if is_face else None,
                               selected_edges_override=edges if not is_face else None)
    return {'mode': 'FILLED' if is_face else 'EDGE',
            'faces': len(faces), 'independent_edges': len(edges),
            'consumed_edges': len(face_owned), 'cuts': summary['cuts'],
            'parts': summary['parts'], 'edge_total': summary['edges'],
            'uv_warning_count': summary['uv_warning_count'],
            'uv_warning_sample': summary['uv_warning_sample']}


def safety_preflight(context, depth, face_mode=False,
                     selected_faces_override=None, selected_edges_override=None):
    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        raise ValueError("Нужен Mesh в Edit Mode")
    select_mode = getattr(getattr(context, 'tool_settings', None), 'mesh_select_mode', None)
    if select_mode is not None:
        mode = tuple(bool(value) for value in select_mode)
        expected = (False, False, True) if face_mode else (False, True, False)
        if mode != expected:
            raise ValueError('Неверный режим выделения: выберите только грани (3)' if face_mode
                             else 'Неверный режим выделения: выберите только рёбра (2)')
    if len(context.objects_in_mode) != 1:
        raise ValueError("Оставьте в Edit Mode только один объект")
    if obj.data.shape_keys or obj.data.library or obj.data.users > 1:
        raise ValueError("Нужен локальный меш без Shape Keys, используемый одним объектом")
    if not math.isfinite(depth) or abs(depth) <= EPS:
        raise ValueError("Глубина должна быть конечной и отличаться от нуля")
    matrix = obj.matrix_world.to_3x3()
    if not all(math.isfinite(v) for row in matrix for v in row) or abs(matrix.determinant()) < 1e-12:
        raise ValueError("Нулевой или некорректный масштаб объекта")
    unit_settings = getattr(context.scene, 'unit_settings', None)
    if unit_settings is not None:
        unit_scale = float(getattr(unit_settings, 'scale_length', 1.0))
        if not math.isfinite(unit_scale) or abs(unit_scale - 1.0) > UNIT_SCALE_TOLERANCE:
            raise ValueError(
                "Масштаб единиц Scene не равен 1.0; пересчёт 1000 px/м не подтверждён. "
                "Сначала приведите Unit Scale к 1.0 на резервной копии проекта"
            )
    if face_mode:
        basis = tuple(tuple(matrix[row][col] for row in range(3)) for col in range(3))
        if any(abs(dot(basis[i], basis[j])) > 1e-5
               for i, j in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("Along Normals не поддерживает shear в матрице объекта")
        axis_lengths = [matrix.col[index].length for index in range(3)]
        if any(abs(length - 1.0) > UNIT_SCALE_TOLERANCE for length in axis_lengths):
            raise ValueError(
                "Для режима Along Normals примените Scale объекта: "
                "Ctrl+A → Scale. Иначе глубина в метрах будет неверной"
            )
    bm = bmesh.from_edit_mesh(obj.data)
    if len(bm.verts) + len(bm.edges) + len(bm.faces) > MAX_SOURCE_ELEMENTS:
        raise ValueError("Слишком большой меш для безопасной рабочей копии; отделите участок стены")
    selected_faces = ([face for face in bm.faces if face.select and not face.hide]
                      if selected_faces_override is None else list(selected_faces_override))
    if face_mode:
        if not selected_faces:
            raise ValueError("Выделите полигоны, заполняющие проём")
        if len(selected_faces) > 2000:
            raise ValueError("Выбрано больше 2000 полигонов; разделите операцию")
        edges = boundary_edges_for_faces(selected_faces)
        components = selected_face_components(selected_faces)
        # W58.6: fail READ-ONLY before entering the transactional Mesh copy.
        # A mixed-normal selection is unsupported by this FACE-mode contract;
        # previous builds silently distorted the UV density by 40..170 px/m.
        # Connected FACE regions may cross corners or curved wall segments.
        # Preserve native region extrusion and let the existing shared-edge
        # winding, contour, transactional and UV audits decide validity.
        for component in components:
            component_boundary = boundary_edges_for_faces(component)
            cycles = split_selected_cycles(component_boundary)
            if any(cycle[0][1] is not cycle[-1][2] for cycle in cycles):
                raise ValueError("Граница выбранных полигонов должна быть замкнута")
            selected_region_world_normal(obj, component, require_consistent_sign=True)
        # Native Blender extrusion is chosen *before* creating the working Mesh.
        # This prevents an unsafe manual vector from ever reaching a mutating op.
        from .native_direction_contract import plan_native_faces
        face_plan = plan_native_faces(
            getattr(context.scene, 'brick_hole_direction_source', 'AUTO'),
            getattr(context.scene, 'brick_hole_manual_normal', (0., 1., 0.)),
            depth,
            (tuple(face_world_normal(obj, face)) for face in selected_faces),
        )
        write_log('FACE DIRECTION PREFLIGHT: mode=%s, min_alignment=%s' % (
            face_plan.mode, face_plan.normal_alignment_min))
    else:
        edges = ([e for e in bm.edges if e.select and not e.hide]
                 if selected_edges_override is None else list(selected_edges_override))
    if not edges or len(edges) > MAX_SELECTED_EDGES:
        raise ValueError("Выделите от 1 до 2000 рёбер; большой пакет разделите на части")
    edge_normals = {}
    if not face_mode:
        # BH0464: one GLOBAL guard for all disconnected contour components.
        # Previous preflight rebuilt the entire source tessellation/BVH for
        # EACH chain and only tested new strips within that chain.  The global
        # sweep both eliminates redundant source scans and detects contacts
        # BETWEEN disjoint selected chains (a former safety blind spot).
        chains = split_selected_cycles(edges)  # still validates all topology
        edge_normals, _local_offsets = edge_local_frame(
            obj, edges, context.scene, depth, bm=bm,
        )
        record_action('global_contour_collision_gate',
                      f'edges={len(edges)}; chains={len(chains)}; '+
                      ('swept_checks=1; cross_chain_checks=YES' if collision_guard_enabled(context.scene)
                       else 'swept_checks=0; cross_chain_checks=NOT_CHECKED · USER_OVERRIDE')+
                      '; source_uv_unchanged=YES')
    if any(not math.isfinite(v) for vert in bm.verts for v in vert.co):
        raise ValueError("В меше есть некорректные координаты")
    for edge in edges:
        if not edge.link_faces or world_distance(obj, *edge.verts) <= EPS:
            raise ValueError("Контур содержит свободное или нулевое ребро")
        if len(edge.link_faces) > 2:
            raise ValueError("Контур содержит non-manifold ребро с более чем двумя гранями")
    verify_contour_segments([
        (edge.verts[0], edge.verts[1],
         tuple(obj.matrix_world @ edge.verts[0].co),
         tuple(obj.matrix_world @ edge.verts[1].co))
        for edge in edges
    ])
    # Bound quadratic normal clustering as well as geometry growth.
    if len({f for e in edges for f in e.link_faces}) > 2000:
        raise ValueError("Слишком много соседних полигонов; уменьшите выделение")

    scene = context.scene
    uv_name = scene.brick_hole_uv_map
    uv = bm.loops.layers.uv.get(uv_name) if uv_name else bm.loops.layers.uv.active
    if uv is None:
        raise ValueError("Не найдена выбранная UV-карта")
    if scene.brick_hole_material_mode == 'OVERRIDE' and scene.brick_hole_material_index >= len(obj.data.materials):
        raise ValueError("Выбранного слота материала нет у объекта")
    resolution = resolution_value(scene)
    depth_uv = abs(depth) * PIXELS_PER_METER / resolution
    estimated_cuts = estimated_parts = 0
    result_layer = bm.faces.layers.int.get(RESULT_LAYER_NAME)
    source_uv_warnings = []
    # Repeated operation is permitted by default, including old .blend files
    # where brick_hole_allow_repeat remains False. Existing result tags still
    # participate in the non-target / UV integrity checks.
    for edge in edges:
        # FILLED reads each boundary's own normal, never the average of all caps.
        # EDGE uses the local wall face normal at EACH contour edge.
        local_normal = (filled_boundary_world_normal(obj, edge, selected_faces)
                        if face_mode else edge_normals.get(edge))
        if local_normal is None:
            raise ValueError('Не удалось определить нормаль выбранного ребра')
        source_face = resolve_source_face(
            obj, edge, local_normal, selected_faces, face_mode=face_mode,
        )
        if source_face is None:
            raise ValueError("Не найден исходный UV-полигон выбранного контура")
        values = [loop[uv].uv.copy() for loop in source_face.loops if loop.vert in edge.verts]
        if len(values) != 2 or not all(math.isfinite(x) for v in values for x in v):
            raise ValueError("Некорректная UV на исходной стене")
        length_m = world_distance(obj, *edge.verts)
        mapped = corrected_reveal_uv_edge(values[0], values[1], length_m,
                                          resolution, PIXELS_PER_METER)
        if mapped['needs_warning']:
            source_uv_warnings.append(mapped['source_density'])
        # Estimate UV cuts from the future NEW reveal UV, never from the
        # mis-scaled source. This is read-only and does not alter source loops.
        target_values = (mapped['uv_a'], mapped['uv_b'])
        axis_counts = [
            len(safe_uv_borders(min(v[axis] for v in target_values) - depth_uv,
                                max(v[axis] for v in target_values) + depth_uv))
            for axis in (0, 1)
        ]
        estimated_cuts += sum(axis_counts)
        estimated_parts += (axis_counts[0] + 1) * (axis_counts[1] + 1)
        if estimated_cuts > MAX_CUTS or estimated_parts > MAX_PARTS:
            raise ValueError("Операция слишком тяжёлая: уменьшите глубину или число контуров")
    write_log(f"PREFLIGHT: estimated cuts <= {estimated_cuts}; parts <= {estimated_parts}")
    advisory = {'uv_warning_count': len(source_uv_warnings),
                'uv_warning_sample': tuple(source_uv_warnings[:3])}
    if source_uv_warnings:
        msg = source_uv_warning_message(advisory)
        write_log('SOURCE_UV_DENSITY_WARNING: ' + msg)
        record_action('source_uv_density_advisory', msg)
    return {'edges': len(edges), 'cuts': estimated_cuts,
            'parts': estimated_parts, 'filled': bool(face_mode), **advisory}



def readonly_edge_safe_depth(context, requested_depth):
    """One explicitly user-triggered, non-mutating safety search.

    The SAME production EDGE preflight is used at every candidate, including
    UV checks and BH-03.2B swept collisions. This is only a conservative
    candidate, NOT proof of an analytic global maximum. Never writes scene.
    """
    from .safe_depth_core import find_safe_depth, DepthSearchFailure
    if not collision_guard_enabled(context.scene):
        raise ValueError('SAFE DEPTH: защита пересечений выключена. Включите её для поиска проверенной глубины')
    obj = getattr(context, 'edit_object', None)
    if (getattr(context, 'mode', None) != 'EDIT_MESH' or obj is None
            or getattr(obj, 'type', None) != 'MESH'):
        raise ValueError('SAFE DEPTH: требуется Mesh в Edit Mode')
    flags=tuple(bool(x) for x in context.tool_settings.mesh_select_mode)
    if flags!=(False,True,False):
        raise ValueError('SAFE DEPTH: работает только для выделенных рёбер (2)')
    # BH0464: >100 selected edges is NOT a geometry fault.  The production
    # EDGE preflight already supports up to MAX_SELECTED_EDGES; the old safe
    # depth helper imposed an unrelated hard cap of 100. Support a bounded
    # large-contour user-triggered search, never from mousemove or redraw.
    # Fail closed beyond 512: repeated full-mesh swept guards are expensive.
    bm=bmesh.from_edit_mesh(obj.data)
    edges=[e for e in bm.edges if e.select and not e.hide]
    count=len(edges)
    if not 1 <= count <= 512:
        raise ValueError('SAFE DEPTH: выберите 1–512 рёбер; большой контур разделите на части')
    if _TRANSACTION_ACTIVE:
        raise ValueError('SAFE DEPTH: дождитесь завершения операции Mesh')
    # A 138-edge/44k-triangle production test spends about a second per
    # swept-collision probe. Use a bounded 12-call search, still verifying
    # the returned candidate with the EXACT production preflight. Precision
    # is a search target, never a promise about authored mesh coordinates.
    # BH0465: a *large source mesh* is costly even with only 46 selected
    # edges. The real scene has 22k+ faces / ~44k tessellated triangles.
    # Cap the user-triggered search, never silently bypass a collision.
    from .collision_recovery_core import probe_budget
    max_calls,target_resolution=probe_budget(count,len(bm.faces))
    record_action('safe_depth_resource_budget',
                  f'edges={count}; source_faces={len(bm.faces)}; max_probes={max_calls}; '
                  f'search_resolution_m={target_resolution}; read_only=YES')
    def _probe(signed):
        safety_preflight(context, signed, face_mode=False,
                         selected_edges_override=edges)
    return find_safe_depth(_probe, float(requested_depth), max_calls=max_calls,
                           target_resolution=target_resolution)


class MESH_OT_brick_hole_safe_depth(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_safe_depth'
    bl_label = 'Brick Hole · Найти допустимую глубину'
    bl_description = ('Только чтение Mesh: найти консервативную глубину, '
                      'проверенную производственным Edge Preflight')
    bl_options = {'REGISTER', 'INTERNAL'}

    action: EnumProperty(name='Действие', items=(
        ('FIND','Найти','Не менять глубину'),
        ('USE','Применить','Перепроверить и изменить лишь поле глубины')),
        default='FIND')

    @classmethod
    def poll(cls, context):
        return (getattr(context,'mode',None)=='EDIT_MESH'
                and getattr(context,'edit_object',None) is not None
                and tuple(bool(x) for x in context.tool_settings.mesh_select_mode)==(False,True,False))

    def execute(self, context):
        scene=context.scene
        if _INTERACTIVE_SESSION is not None:
            self.report({'WARNING'},'При открытом Preview используйте S для безопасной глубины')
            return {'CANCELLED'}
        try:
            requested=scene_signed_depth(scene)
            found=readonly_edge_safe_depth(context,requested)
            text=(f'Проверено {found.calls} кандидатов: '
                  f'{found.candidate:+.6f} м. '
                  'Это консервативная рекомендация, не гарантированный максимум.')
            scene.brick_hole_safe_depth_candidate=float(found.candidate)
            scene.brick_hole_safe_depth_status=text[:400]
            record_action('safe_depth_candidate',
                          f'requested={requested:+.7g}; safe={found.candidate:+.7g}; '
                          f'calls={found.calls}; action={self.action}')
            if self.action=='USE':
                # The result was just freshly recomputed on the current mesh.
                # Changing an RNA value does not modify Mesh or author UV.
                scene.brick_hole_depth=abs(found.candidate)
                scene.brick_hole_direction='INWARD' if found.candidate<0 else 'OUTWARD'
                scene.brick_hole_safe_depth_status='Установлено после повторной проверки: '+text
                record_action('safe_depth_selected',text)
            self.report({'INFO'},text[:245])
            return {'FINISHED'}
        except (ValueError,RuntimeError,ReferenceError) as exc:
            scene.brick_hole_safe_depth_candidate=0.0
            scene.brick_hole_safe_depth_status='Не определено: '+str(exc)[:300]
            record_action('safe_depth_blocked',str(exc)[:300])
            self.report({'WARNING'},str(exc)[:245])
            return {'CANCELLED'}

def rebase_working_mesh(mesh):
    # Only the disposable working mesh is translated. Keep object transform
    # unchanged; native world-space transforms are not used by this core.
    selected = [v for v in mesh.vertices if v.select]
    if not selected:
        raise ValueError("Не найдены вершины выделенного контура")
    anchor = tuple((min(float(v.co[a]) for v in selected) +
                    max(float(v.co[a]) for v in selected)) * 0.5 for a in range(3))
    for vertex in mesh.vertices:
        vertex.co = tuple(float(vertex.co[a]) - anchor[a] for a in range(3))
    return anchor


def restore_working_coordinates(mesh, anchor):
    for vertex in mesh.vertices:
        vertex.co = tuple(float(vertex.co[a]) + anchor[a] for a in range(3))


def mesh_gap_health_snapshot(mesh, threshold=0.025, max_vertices=100000, max_examples=12):
    """Non-mutating post-extrusion audit; detects near-coincident *unwelded*
    vertex pairs. Never calls remove_doubles: 2cm merge would damage shape.

    Candidate pairs are near vertices NOT directly connected by an edge; they
    are diagnostic signals, not automatic evidence of a topological defect.
    """
    verts=list(mesh.vertices)
    if len(verts)>max_vertices:
        return {'status':'SKIPPED_TOO_LARGE','vertices':len(verts)}
    from collections import defaultdict
    cell=float(threshold)
    if not math.isfinite(cell) or cell<=0:
        raise ValueError('Invalid geometry audit threshold')
    # A temporary transactional Mesh can be rebased to the origin; using
    # absolute coordinates would falsely mark ALL old close pairs as new.
    # Vertex 0 is an audit reference only, not a welding target.
    origin=tuple(float(c) for c in verts[0].co) if verts else (0.0,0.0,0.0)
    neighbor_edges=set(tuple(sorted(map(int,edge.vertices))) for edge in mesh.edges)
    grid=defaultdict(list)
    examples=[]
    count=0
    for idx,vertex in enumerate(verts):
        p=tuple(float(c) for c in vertex.co)
        key=tuple(math.floor(c/cell) for c in p)
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    for other_index,other_point in grid.get((key[0]+dx,key[1]+dy,key[2]+dz),()):
                        if (other_index,idx) in neighbor_edges:
                            continue
                        distance=math.sqrt(sum((p[a]-other_point[a])**2 for a in range(3)))
                        if distance<cell:
                            count+=1
                            if len(examples)<max_examples:
                                examples.append({'vertices':(other_index,idx),
                                                 'distance_m':round(distance,9),
                                                 'positions_local':(
                                                     tuple(round(other_point[j]-origin[j],6) for j in range(3)),
                                                     tuple(round(p[j]-origin[j],6) for j in range(3)))})
        grid[key].append((idx,p))
    return {'status':'OK','vertices':len(verts),'unconnected_close_pairs':count,
            'threshold_m':cell,'examples':examples}


def protected_operation(method):
    @wraps(method)
    def wrapped(self, context):
        global _TRANSACTION_ACTIVE, _TRANSACTION_FAILURE_REASON
        if _TRANSACTION_ACTIVE:
            try:
                result = method(self, context)
                if 'FINISHED' not in result:
                    _build_failed(_recent_failure_log())
                return result
            except Exception as exc:
                _build_failed(f'{type(exc).__name__}: {exc}')
                write_log(traceback.format_exc())
                self.report({'WARNING'}, str(exc)[:240])
                return {'CANCELLED'}

        obj = context.edit_object
        original = working = None
        anchor = None
        success = False
        operation_result = {'CANCELLED'}
        cleanup_errors = []
        critical_cleanup_errors = []
        select_mode = tuple(context.tool_settings.mesh_select_mode)
        source_uv_advisory = ''

        def cleanup_step(label, callback, critical=False):
            try:
                callback()
                write_log(f"CLEANUP OK: {label}")
                return True
            except Exception:
                cleanup_errors.append(label)
                if critical:
                    critical_cleanup_errors.append(label)
                write_log(f"CLEANUP ERROR [{label}]: " + traceback.format_exc())
                return False

        def change_mode(mode):
            bpy.ops.object.mode_set(mode=mode)
            if obj.mode != mode:
                raise RuntimeError(f"Blender не перешёл в {mode}: остался {obj.mode}")

        try:
            reset_log()
            _TRANSACTION_FAILURE_REASON = None
            _build_stage('READ_ONLY_PREFLIGHT', 'start')
            depth = getattr(self, 'depth', context.scene.brick_hole_depth)
            unified = bool(getattr(self, '_brick_unified_mode', False))
            face_mode = bool(getattr(self, 'face_region', False) or
                             getattr(self, '_brick_face_mode', False))
            write_log(f"TRANSACTION PREFLIGHT: unified={unified}; face_mode={face_mode}")
            if unified:
                preflight_result = unified_preflight(context, depth)
            else:
                preflight_result = safety_preflight(context, depth, face_mode)
            source_uv_advisory = source_uv_warning_message(preflight_result)
            original = obj.data
            # This audit only inspects geometry. It does not weld or move a vertex.
            # Blender Mesh vertex arrays in Edit Mode may be stale, therefore
            # inspect after safely switching to OBJECT.
            change_mode('OBJECT')
            baseline_health=mesh_gap_health_snapshot(original)
            write_log('GEOMETRY_BEFORE: '+repr(baseline_health))
            working = original.copy()
            anchor = rebase_working_mesh(working)
            write_log(f"PRECISION ORIGIN: {anchor}")
            obj.data = working
            if obj.data is not working:
                raise RuntimeError('Blender не установил рабочую копию меша')
            change_mode('EDIT')
            _TRANSACTION_ACTIVE = True
            write_log('TRANSACTION START: working mesh copy')
            result = method(self, context)
            if 'FINISHED' not in result:
                reason = _TRANSACTION_FAILURE_REASON or _recent_failure_log()
                raise RuntimeError(f'FILLED/EDGE: {reason}')
            _build_stage('COMMIT_READY', 'geometry and UV validated')
            change_mode('OBJECT')
            after_health=mesh_gap_health_snapshot(working)
            write_log('GEOMETRY_AFTER: '+repr(after_health))
            # Compare stable spatial signatures rather than changing BMesh
            # indices.  Example-only audit: totals still describe all pairs.
            def pair_signatures(snapshot):
                result=set()
                for item in snapshot.get('examples', ()):
                    pts=item.get('positions_local')
                    if pts and len(pts)==2:
                        result.add(tuple(sorted(tuple(p) for p in pts)))
                return result
            before_signatures=pair_signatures(baseline_health)
            after_signatures=pair_signatures(after_health)
            write_log('GEOMETRY_CLOSE_PAIRS_DELTA: introduced_examples=%d; '
                      'resolved_examples=%d; persistent_examples=%d; '
                      'sample_limited=True; NO_AUTO_WELD' % (
                      len(after_signatures-before_signatures),
                      len(before_signatures-after_signatures),
                      len(after_signatures & before_signatures)))
            if (after_health.get('status') == baseline_health.get('status') == 'OK'
                    and after_health['unconnected_close_pairs'] > baseline_health['unconnected_close_pairs']):
                count=(after_health['unconnected_close_pairs']-
                       baseline_health['unconnected_close_pairs'])
                write_log(f'GEOMETRY_UNWELDED_WARNING: +{count} near-disconnected pairs. '
                          'No automatic weld performed; inspect the support ZIP.')
                self.report({'WARNING'},f'После операции найдено {count} новых близких несвязанных пар вершин. Проверьте Mesh.')
            change_mode('EDIT')
            success = True
            operation_result = result
        except Exception as exc:
            reason = _build_failed(f'{type(exc).__name__}: {exc}')
            decision, user_reason = format_failure(exc)
            write_log(f'DECISION: {decision}; raw={type(exc).__name__}: {exc}')
            write_log(traceback.format_exc())
            # The error remains readable in the integrated N-panel as well as
            # the Blender status report, even when Blender's report fades.
            try:
                context.scene.brick_hole_preview_text = f'ERROR: {user_reason}'[:380]
                context.scene.brick_hole_preview_warnings = 1
                if hasattr(context.scene, 'ima_notice_message'):
                    context.scene.ima_notice_message = f'Проёмы: {user_reason}'[:400]
                    context.scene.ima_notice_tool = 'BRICK_HOLE'
                    context.scene.ima_notice_kind = 'ERROR'
            except (RuntimeError, AttributeError, ReferenceError):
                pass
            self.report({'ERROR'}, f'Операция отменена: {user_reason}'[:245])
        finally:
            _TRANSACTION_ACTIVE = False
            if working is not None:
                # All mandatory restoration must succeed *before* deleting the
                # original datablock. A failed return to Edit now causes rollback.
                if obj.mode == 'EDIT' and not cleanup_step(
                    'leave Edit Mode', lambda: change_mode('OBJECT'), critical=True,
                ):
                    success = False

                if success and anchor is not None:
                    if cleanup_step(
                        'restore precision coordinates',
                        lambda: restore_working_coordinates(working, anchor),
                        critical=True,
                    ):
                        write_log(f"PRECISION RESTORE: {anchor}")
                    else:
                        success = False
                        write_log('COMMIT ABORTED: precision coordinates not restored')

                # Respect the user's mode instead of silently switching to
                # EDGE/FACE selection at the end of an operation.
                if not cleanup_step(
                    'restore mesh select mode',
                    lambda: setattr(context.tool_settings, 'mesh_select_mode', select_mode),
                    critical=True,
                ):
                    success = False

                if success and obj.mode != 'EDIT':
                    if not cleanup_step(
                        'return to Edit Mode before commit',
                        lambda: change_mode('EDIT'), critical=True,
                    ):
                        success = False
                if success and (obj.mode != 'EDIT' or obj.data is not working):
                    success = False
                    critical_cleanup_errors.append('verify working mesh and mode')
                    write_log('COMMIT ABORTED: working mesh / Edit Mode invariant failed')

                if not success:
                    # Re-enter Object Mode to safely restore the original even
                    # if the working copy was already re-entered into Edit Mode.
                    if obj.mode == 'EDIT':
                        cleanup_step(
                            'leave Edit Mode for rollback',
                            lambda: change_mode('OBJECT'), critical=True,
                        )
                    if original is not None and obj.data is not original:
                        cleanup_step(
                            'restore original mesh datablock',
                            lambda: setattr(obj, 'data', original), critical=True,
                        )
                    if original is not None and obj.data is not original:
                        cleanup_errors.append('verify original mesh datablock')
                        critical_cleanup_errors.append('verify original mesh datablock')
                        write_log('CLEANUP ERROR: object is not using original mesh')
                    if obj.mode != 'EDIT':
                        cleanup_step(
                            'return to Edit Mode after rollback',
                            lambda: change_mode('EDIT'), critical=True,
                        )
                    operation_result = {'CANCELLED'}
                elif original is not None:
                    # Native Blender Undo may still reference the source Mesh ID
                    # even when `users == 0`. Deleting it synchronously from a
                    # UNDO operator caused a reproducible access-violation
                    # suspect in DepsgraphNodeBuilder::build_materials.
                    # Leave it unlinked for normal Blender orphan housekeeping;
                    # do not call bpy.data.meshes.remove() here.
                    if original.users == 0:
                        write_log('UNDO_ID_LIFETIME: retained original Mesh ID '
                                  'until Blender safely releases unused data; '
                                  'NO_MANUAL_ID_REMOVE')
                    write_log('TRANSACTION COMMIT (source datablock retained)')
                    # A successful build alters the one-object fixture gallery.
                    # Its cached ORIGINAL label must not survive that mutation:
                    # the real Blender support capture showed ORIGINAL after
                    # an EDGE extrusion had already created four reveal faces.
                    try:
                        if (callable(getattr(obj, 'get', None)) and
                                obj.get('BH_fixture_schema', '') == FIXTURE_WALL_SCHEMA):
                            obj['BH_fixture_last_check'] = 'MODIFIED'
                            obj['BH_fixture_last_report_json'] = ''
                            record_action('fixture_invalidated_after_build', obj.name)
                    except (RuntimeError, ReferenceError, TypeError) as exc:
                        write_log('FIXTURE METADATA INVALIDATION WARNING: '+str(exc))
                    invalidate_qa(context.scene)

                if not success and working.users == 0:
                    # A just-detached Edit Mesh may still be referenced by an
                    # in-flight Undo/depsgraph refresh. Avoid immediate ID
                    # removal even on rollback; Blender cleans unused data.
                    write_log('UNDO_ID_LIFETIME: retained discarded Mesh ID '
                              'for deferred Blender orphan cleanup')
                if not success and obj.data is original and obj.mode == 'EDIT':
                    write_log('TRANSACTION ROLLBACK: original mesh restored')
            elif obj is not None and original is not None and obj.mode != 'EDIT':
                cleanup_step('return to Edit Mode',
                             lambda: change_mode('EDIT'), critical=True)

            cleanup_step('finish progress', context.window_manager.progress_end)
            if getattr(context, 'workspace', None):
                cleanup_step('clear status text',
                             lambda: context.workspace.status_text_set(text=None))
            context.scene.brick_hole_progress = 100 if success and not critical_cleanup_errors else 0
            if cleanup_errors:
                self.report(
                    {'ERROR'} if critical_cleanup_errors else {'WARNING'},
                    'Восстановление завершилось с ошибками: ' + ', '.join(cleanup_errors),
                )
            if not success:
                operation_result = {'CANCELLED'}
        if critical_cleanup_errors:
            return {'CANCELLED'}
        if success:
            if source_uv_advisory:
                self.report({'WARNING'}, source_uv_advisory[:245])
                write_log('COMMIT_SOURCE_UV_ADVISORY: ' + source_uv_advisory)
                record_action('build_source_uv_density_advisory', source_uv_advisory)
            try:
                scene = context.scene
                if source_uv_advisory:
                    scene.brick_hole_preview_text = source_uv_advisory[:380]
                    scene.brick_hole_preview_warnings = 1
                if getattr(scene, 'ima_notice_tool', '') == 'BRICK_HOLE':
                    scene.ima_notice_message = ''
                    scene.ima_notice_tool = ''
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        return operation_result
    return wrapped


def reset_log():
    text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
    text.clear()
    text.write("BRICK HOLE EXTRUDE — DIAGNOSTIC LOG\n")
    text.write(f"Time: {datetime.now().isoformat(timespec='seconds')}\n")
    text.write(f"Blender: {bpy.app.version_string}\n")
    text.write(f"Add-on: BETA 1 / {ADDON_VERSION_STRING} FILLED UV Reliability (UV core: 0.9.0)\n")
    text.write(f"Build ID: {BH0472_BUILD_ID}\n")
    text.write("-" * 72 + "\n")



# Session-only action trace. Saved to ZIP when requested, not to the .blend.
_ACTION_TRACE = deque(maxlen=2400)
_TESSELLATION_WITNESSES = deque(maxlen=12)
_COLLISION_WITNESSES = deque(maxlen=48)
_CORNER_TRIM_WITNESSES = deque(maxlen=48)


def _capture_collision_witness(details):
    """Retain first offending pair before preflight fails, without BMesh pointers."""
    item = {
        'schema': 1, 'at_utc': datetime.now(timezone.utc).isoformat(),
        'stage': 'EDGE_SWEPT_COLLISION_PREFLIGHT',
        **details,
    }
    _COLLISION_WITNESSES.append(item)
    # The standard support bundle contains the recent action trace but not
    # the separate Brick Hole evidence ZIP. Store a bounded witness directly
    # in that trace so the actual contact is reproducible from ONE upload.
    xyz=item.get('contact_xyz',())
    try:
        points=tuple(tuple(round(float(axis),6) for axis in point)
                     for point in xyz[:4])
    except (TypeError,ValueError,IndexError):
        points=()
    detail=(f"kind={item.get('kind')}; strip={item.get('primary_strip')}; "
            f"source_face={item.get('source_face_index')}; "
            f"source_incident={item.get('source_face_incident')}; "
            f"shared_vertices={item.get('source_shared_vertex_count')}; "
            f"contact_count={len(xyz)}; contact_xyz={points}")
    record_action('collision_witness',detail)
    write_log('COLLISION WITNESS: '+detail)



def _capture_tessellation_witness(face_index, world_coords, uvs, triangles,
                                   resolution, reason, *, metric_coords=None,
                                   world_origin=None):
    """Preserve a plain-data witness before the failed working Mesh rolls back."""
    witness = {
        'schema': 1, 'at_utc': datetime.now(timezone.utc).isoformat(),
        'stage': 'UV_DENSITY_VALIDATION', 'face_index': int(face_index),
        'resolution': int(resolution), 'error': str(reason)[:500],
        'world_loops': [[float(v) for v in p] for p in world_coords],
        'uv_loops': [[float(v) for v in p] for p in uvs],
        'blender_triangles': [[int(i) for i in tri] for tri in triangles],
    }
    if metric_coords is not None:
        witness['metric_frame'] = 'WORLD_LINEAR_WITHOUT_TRANSLATION'
        witness['metric_loops'] = [[float(v) for v in p] for p in metric_coords]
        witness['world_origin_xyz'] = [float(v) for v in world_origin] if world_origin is not None else None
    _TESSELLATION_WITNESSES.append(witness)
    record_action('uv_tessellation_witness',
                  f"face={face_index}; loops={len(world_coords)}; "
                  f"triangles={len(triangles)}; reason={reason}")
    write_log(f"UV TESSELLATION WITNESS: face={face_index}; "
              f"loops={len(world_coords)}; triangles={len(triangles)}; "
              f"reason={reason}; details=tessellation_witnesses.json")

_VIEWPORT_HANDLER = None
_VIEWPORT_LABEL_HANDLER = None
_OVERLAY_ERROR_CACHE = None
_MAX_OVERLAY_SEGMENTS = 12000
_MAX_OVERLAY_LABELS = 12
_OVERLAY_REGISTRY_KEY = 'issmaker.brick_hole.viewport_handlers.v1'
# All geometry snapshots are transient. Only numerical tuples and strings may
# outlive a draw callback; BMesh elements and GPU batches must never be cached.


_INTEGRATION_ACTION_SINK = None

def set_action_sink(sink):
    global _INTEGRATION_ACTION_SINK
    _INTEGRATION_ACTION_SINK = sink

def record_action(action, detail=''):
    _ACTION_TRACE.append({
        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'action': str(action)[:80],
        'detail': str(detail)[:900],
    })
    if _INTEGRATION_ACTION_SINK is not None:
        try:
            _INTEGRATION_ACTION_SINK(str(action)[:80], str(detail)[:500])
        except Exception:
            pass  # diagnostics must never interrupt Blender mesh operations


def invalidate_preview(scene, reason='Изменилось состояние модели — повторите проверку'):
    if hasattr(scene, 'brick_hole_preview_text'):
        scene.brick_hole_preview_text = reason
    if hasattr(scene, 'brick_hole_preview_warnings'):
        scene.brick_hole_preview_warnings = 1
    if hasattr(scene, 'brick_hole_preview_verified'):
        scene.brick_hole_preview_verified = False
    if hasattr(scene, 'brick_hole_preview_signature'):
        scene.brick_hole_preview_signature = ''


def preview_signature(context, face_mode):
    """Stable read-only snapshot of selection, linked UV and critical settings.

    Prevents a previous OK being displayed for a different input. No BMesh
    indices, pointer IDs, write operations or stored live references are used.
    """
    obj = getattr(context, 'edit_object', None)
    if obj is None or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return ''
    bm = bmesh.from_edit_mesh(obj.data)
    if face_mode is None:
        selected_faces, loose_edges, _consumed = partition_brick_selection(
            bm,getattr(getattr(context, "tool_settings", None), "mesh_select_mode", None))
        edges = loose_edges + boundary_edges_for_faces(selected_faces)
    else:
        selected_faces = []
        edges = ([edge for edge in bm.edges if edge.select and not edge.hide]
                 if not face_mode else boundary_edges_for_faces(
                     [f for f in bm.faces if f.select and not f.hide]))
    if len(edges) > MAX_SELECTED_EDGES:
        return ''
    sc = context.scene
    uv = bm.loops.layers.uv.get(sc.brick_hole_uv_map) if sc.brick_hole_uv_map else bm.loops.layers.uv.active
    h = hashlib.blake2b(digest_size=20)
    configuration = (
        obj.name, len(bm.verts), len(bm.edges), len(bm.faces),
        len(obj.data.materials), bool(face_mode), float(sc.brick_hole_depth),sc.brick_hole_direction,
        sc.brick_hole_direction_source,tuple(sc.brick_hole_manual_normal),
        sc.brick_hole_uv_map,sc.brick_hole_resolution,brick_hole_uv_policy(sc),
        float(sc.brick_hole_uv_transition_m),sc.brick_hole_material_mode,
        sc.brick_hole_material_index,sc.brick_hole_allow_repeat,sc.brick_hole_reverse_u,
        collision_guard_enabled(sc),
        tuple(tuple(float(v) for v in row) for row in obj.matrix_world),
        float(sc.unit_settings.scale_length),
        tuple(getattr(getattr(context,'tool_settings',None),'mesh_select_mode',())),
    )
    h.update(repr(configuration).encode('utf-8'))
    signatures = []
    for edge in edges:
        coords = tuple(sorted(tuple(float(x).hex() for x in vert.co) for vert in edge.verts))
        adjacent = []
        for face in edge.link_faces:
            if len(face.loops) > 24000:
                return ''  # Fail closed rather than lock the UI for huge ngons.
            data = []
            for loop in face.loops:
                data.append((tuple(float(x).hex() for x in loop.vert.co),
                             tuple(float(x).hex() for x in loop[uv].uv) if uv else None))
            adjacent.append((int(face.material_index), tuple(data)))
        signatures.append((coords,tuple(sorted(adjacent))))
    h.update(repr(tuple(sorted(signatures))).encode('utf-8'))
    if face_mode is None:
        # Interior selected faces can change without changing their perimeter.
        if len(selected_faces) > 2000:
            return ''
        face_data = []
        for face in selected_faces:
            if len(face.loops) > 24000:
                return ''
            face_data.append((int(face.material_index),tuple(sorted(
                (tuple(float(x).hex() for x in loop.vert.co),
                 tuple(float(x).hex() for x in loop[uv].uv) if uv else None)
                for loop in face.loops))))
        h.update(repr(tuple(sorted(face_data))).encode('utf-8'))
    return h.hexdigest()


# Overlay data is read from the *current* Edit BMesh and does not retain
# Blender RNA, BMesh objects or GPU batches after a redraw.
def _overlay_edge_segments(obj, edges, max_segments):
    result, seen, truncated = [], set(), 0
    for edge in edges:
        if edge.hide or len(edge.verts) != 2:
            continue
        key = tuple(sorted((id(edge.verts[0]), id(edge.verts[1]))))
        if key in seen:
            continue
        seen.add(key)
        if len(result) >= max_segments:
            truncated += 1
            continue
        a = tuple(float(v) for v in obj.matrix_world @ edge.verts[0].co)
        b = tuple(float(v) for v in obj.matrix_world @ edge.verts[1].co)
        if all(math.isfinite(v) for v in a+b):
            result.append((a, b))
    return result, truncated


def overlay_error_snapshot(context, max_segments=_MAX_OVERLAY_SEGMENTS,
                           max_labels=_MAX_OVERLAY_LABELS):
    """(outline, truncated, numbered anchors). Exactly one validation pass.

    Marker positions are world coordinates; markers carry no live BMesh refs.
    The 25k-face limit is explicit rather than silently drawing stale data.
    """
    obj = getattr(context, 'edit_object', None)
    if obj is None or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return [], 0, []
    bm = bmesh.from_edit_mesh(obj.data)
    if len(bm.faces) > 25000:
        return [], -1, []
    try:
        _bm, _checked, failures = current_validation(context)
    except (ValueError, RuntimeError, ReferenceError):
        return [], 0, []
    edges = (edge for face, _ in failures for edge in face.edges)
    segments, truncated = _overlay_edge_segments(obj, edges, max_segments)
    anchors = []
    for i, (face, reasons) in enumerate(failures[:max_labels]):
        if not face.verts:
            continue
        coords = [tuple(float(v) for v in obj.matrix_world @ vert.co) for vert in face.verts]
        center = tuple(sum(point[axis] for point in coords) / len(coords) for axis in range(3))
        if all(math.isfinite(v) for v in center):
            anchors.append((i + 1, center, '; '.join(str(reason) for reason in reasons)[:100]))
    return segments, truncated, anchors


def overlay_segments(context, mode, max_segments=_MAX_OVERLAY_SEGMENTS):
    if max_segments < 1:
        return [], 0
    obj = getattr(context, 'edit_object', None)
    if obj is None or obj.type != 'MESH' or context.mode != 'EDIT_MESH':
        return [], 0
    bm = bmesh.from_edit_mesh(obj.data)
    if mode == 'CONTOUR':
        face_mode = bool(context.tool_settings.mesh_select_mode[2])
        selected_faces = [f for f in bm.faces if f.select and not f.hide] if face_mode else []
        edges = (boundary_edges_for_faces(selected_faces) if selected_faces else
                 [edge for edge in bm.edges if edge.select and not edge.hide])
        return _overlay_edge_segments(obj, edges, max_segments)
    if mode == 'ERRORS':
        segments, truncated, _ = overlay_error_snapshot(context, max_segments=max_segments)
        return segments, truncated
    return [], 0


def _rna_identity(item):
    pointer = getattr(item, 'as_pointer', None)
    return pointer() if callable(pointer) else id(item)


def _overlay_cache_key(context):
    """Every visible scene/object/transform gets a separate 250ms window.

    Two objects can share a Mesh datablock but have *different* world matrices.
    The old mesh-only key could show the previous object's error outlines.
    """
    obj = getattr(context, 'edit_object', None)
    if obj is None or context.mode != 'EDIT_MESH':
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    scene = context.scene
    matrix = tuple(float(v) for row in obj.matrix_world for v in row)
    return (_rna_identity(scene), _rna_identity(obj), _rna_identity(obj.data), matrix,
            len(bm.verts), len(bm.edges), len(bm.faces),
            scene.brick_hole_uv_map, scene.brick_hole_resolution,
            int(time.monotonic() * 4))


def draw_overlay_snapshot(context, scene):
    """Cached (segments, truncated, anchor labels) for BOTH draw callbacks."""
    global _OVERLAY_ERROR_CACHE
    if scene.brick_hole_overlay_mode != 'ERRORS':
        segments, truncated = overlay_segments(context, scene.brick_hole_overlay_mode)
        return segments, truncated, []
    key = _overlay_cache_key(context)
    if key is None:
        _OVERLAY_ERROR_CACHE = None
        return [], 0, []
    if _OVERLAY_ERROR_CACHE is not None and _OVERLAY_ERROR_CACHE[0] == key:
        return _OVERLAY_ERROR_CACHE[1]
    value = overlay_error_snapshot(context)
    _OVERLAY_ERROR_CACHE = (key, value)
    return value


def draw_overlay_segments(context, scene):
    segments, truncated, _labels = draw_overlay_snapshot(context, scene)
    return segments, truncated


def _overlay_draw_failure(scene, exc, phase):
    """Fail visibly closed; avoid repeated exceptions on every GPU redraw."""
    message = f'{phase}: {type(exc).__name__}: {exc}'[:250]
    record_action('overlay_exception', message)
    if scene is not None:
        scene.brick_hole_overlay_error = message
        scene.brick_hole_show_overlay = False


def draw_brick_hole_overlay():
    context = bpy.context
    scene = getattr(context, 'scene', None)
    if scene is None or not getattr(scene, 'brick_hole_show_overlay', False):
        return
    gpu_module, previous = None, {}
    try:
        segments, _truncated, _labels = draw_overlay_snapshot(context, scene)
        if not segments:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        gpu_module = gpu
        state = gpu.state
        for name in ('blend', 'depth_test', 'line_width'):
            getter = getattr(state, name + '_get', None)
            if getter is not None:
                previous[name] = getter()
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINES', {'pos': [p for pair in segments for p in pair]})
        state.blend_set('ALPHA')
        state.depth_test_set('NONE')
        state.line_width_set(2.5)
        shader.bind()
        shader.uniform_float('color', (0.96, 0.28, 0.23, 0.95) if scene.brick_hole_overlay_mode == 'ERRORS'
                             else (1.0, 0.73, 0.20, 0.92))
        batch.draw(shader)
    except Exception as exc:
        _overlay_draw_failure(scene, exc, 'POST_VIEW')
    finally:
        if gpu_module is not None:
            # Restore the surrounding renderer state where introspection exists;
            # fall back to the API defaults, never leave ALPHA blend enabled.
            for name, fallback in (('line_width', 1.0), ('depth_test', 'NONE'), ('blend', 'NONE')):
                try:
                    getattr(gpu_module.state, name + '_set')(previous.get(name, fallback))
                except Exception as exc:
                    _overlay_draw_failure(scene, exc, 'GPU_STATE_RESTORE/' + name)


def draw_brick_hole_overlay_labels():
    """Small, bounded on-screen error IDs/reasons in POST_PIXEL coordinates."""
    context = bpy.context
    scene = getattr(context, 'scene', None)
    if scene is None or not getattr(scene, 'brick_hole_show_overlay', False):
        return
    if scene.brick_hole_overlay_mode != 'ERRORS' or not scene.brick_hole_overlay_labels:
        return
    region = getattr(context, 'region', None)
    region_3d = getattr(context, 'region_data', None)
    if region_3d is None:
        region_3d = getattr(getattr(context, 'space_data', None), 'region_3d', None)
    if region is None or region_3d is None:
        return
    try:
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        import blf
        _segments, _truncated, anchors = draw_overlay_snapshot(context, scene)
        from ...core import diagnostic_caption, theme_engine, ui_layout_scaling, viewport_skin
        from ...core import window_visibility
        from ...appearance_gpu import Painter
        addon = context.preferences.addons.get('issmaker_modeling_assistant')
        prefs = addon.preferences if addon else None
        profile = ui_layout_scaling.from_preferences(prefs, context=context)
        font_px = max(10, round(viewport_skin.text_px('control') * profile.font))
        blf.size(0, font_px)
        scope = window_visibility.scope_from_context(context)
        peers = tuple(rect for _key, rect in window_visibility.peer_rectangles(
            scope, 'BRICK_ERROR_READONLY')) if scope else ()
        visible = []
        for number, xyz, reason in anchors:
            screen = location_3d_to_region_2d(region, region_3d, Vector(xyz))
            if screen is None:
                continue
            if not (0 <= screen.x <= region.width and 0 <= screen.y <= region.height):
                continue
            result = diagnostic_caption.layout_caption(
                f'#{number} {reason[:42]}', (region.width, region.height),
                (float(screen.x),float(screen.y)),
                lambda part: blf.dimensions(0,part)[0],
                scale=profile.font, location='near', obstacles=peers,
                placed=tuple(rect for rect,_ in visible), max_width_ratio=.52)
            if result is not None:
                rect, value = result
                visible.append(((rect[0],rect[1],rect[0]+rect[2],rect[1]+rect[3]),value))
        if visible:
            theme = theme_engine.from_preferences(prefs)
            painter = Painter()
            try:
                for bounds, value in visible:
                    diagnostic_caption.draw_caption(
                        painter, (bounds[0],bounds[1],bounds[2]-bounds[0],bounds[3]-bounds[1]),
                        value, theme, size=font_px, semantic=(1.0,.42,.34,1.0))
            finally:
                painter.close()
    except Exception as exc:
        _overlay_draw_failure(scene, exc, 'POST_PIXEL')


def _overlay_driver_registry():
    # Survives a Python module reload in the same Blender session. This makes
    # unregister/register clean up handlers created by an older module object.
    return getattr(getattr(bpy, 'app', None), 'driver_namespace', None)


def stop_viewport_overlay():
    global _VIEWPORT_HANDLER, _VIEWPORT_LABEL_HANDLER, _OVERLAY_ERROR_CACHE
    registry = _overlay_driver_registry()
    old = registry.pop(_OVERLAY_REGISTRY_KEY, ()) if registry is not None else ()
    # Older releases stored a single GPU handle (an int on some Blender
    # builds), newer releases store 1–2 handles in a tuple. An int cannot be
    # passed to list(), which previously raised 'int object is not iterable'.
    old_handles = old if isinstance(old, (tuple,list)) else (() if old is None else (old,))
    handles = [_VIEWPORT_HANDLER, _VIEWPORT_LABEL_HANDLER] + list(old_handles)
    _VIEWPORT_HANDLER = None
    _VIEWPORT_LABEL_HANDLER = None
    _OVERLAY_ERROR_CACHE = None
    removed = set()
    for handler in handles:
        if handler is None or id(handler) in removed:
            continue
        removed.add(id(handler))
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
        except Exception as exc:
            record_action('overlay_cleanup_error', f'{type(exc).__name__}: {exc}')


def any_overlay_scene_enabled():
    return any(getattr(scene, 'brick_hole_show_overlay', False)
               for scene in getattr(bpy.data, 'scenes', ()))


def redraw_viewports(context):
    """Tag every Blender window, not only the currently focused screen."""
    screens = []
    manager = getattr(context, 'window_manager', None)
    for window in getattr(manager, 'windows', ()):
        screen = getattr(window, 'screen', None)
        if screen is not None:
            screens.append(screen)
    if not screens and getattr(context, 'screen', None) is not None:
        screens.append(context.screen)
    visited = set()
    for screen in screens:
        for area in getattr(screen, 'areas', ()):
            if area.type == 'VIEW_3D' and id(area) not in visited:
                visited.add(id(area))
                area.tag_redraw()


class VIEW3D_OT_brick_hole_toggle_overlay(bpy.types.Operator):
    bl_idname = 'view3d.brick_hole_toggle_overlay'
    bl_label = 'Подсветка в 3D Viewport'
    bl_description = 'Жёлтый: текущий контур. Красный: ошибки и подписи в текущем Edit Mesh'
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _VIEWPORT_HANDLER, _VIEWPORT_LABEL_HANDLER, _OVERLAY_ERROR_CACHE
        scene = context.scene
        new_state = not scene.brick_hole_show_overlay
        try:
            if new_state:
                if _VIEWPORT_HANDLER is None:
                    _VIEWPORT_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                        draw_brick_hole_overlay, (), 'WINDOW', 'POST_VIEW')
                if _VIEWPORT_LABEL_HANDLER is None:
                    _VIEWPORT_LABEL_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
                        draw_brick_hole_overlay_labels, (), 'WINDOW', 'POST_PIXEL')
                scene.brick_hole_overlay_error = ''
                registry = _overlay_driver_registry()
                if registry is not None:
                    registry[_OVERLAY_REGISTRY_KEY] = (_VIEWPORT_HANDLER, _VIEWPORT_LABEL_HANDLER)
            scene.brick_hole_show_overlay = new_state
            _OVERLAY_ERROR_CACHE = None
            if not new_state and not any_overlay_scene_enabled():
                stop_viewport_overlay()
            redraw_viewports(context)
            record_action('overlay_toggle', f'enabled={new_state}; mode={scene.brick_hole_overlay_mode}')
            self.report({'INFO'}, 'Подсветка включена' if new_state else 'Подсветка выключена')
            return {'FINISHED'}
        except Exception as exc:
            scene.brick_hole_show_overlay = False
            scene.brick_hole_overlay_error = f'{type(exc).__name__}: {exc}'[:250]
            if not any_overlay_scene_enabled():
                stop_viewport_overlay()
            self.report({'ERROR'}, f'Подсветка не включена: {exc}')
            return {'CANCELLED'}


def _safe_float_tuple(values, limit=16):
    return [float(v) if math.isfinite(float(v)) else None for v in list(values)[:limit]]


def collect_diagnostics_state(context):
    scene = context.scene
    obj = getattr(context, 'edit_object', None) or getattr(context, 'active_object', None)
    fields = ('brick_hole_depth', 'brick_hole_direction', 'brick_hole_direction_source',
              'brick_hole_manual_normal', 'brick_hole_face_frame_x', 'brick_hole_face_frame_y', 'brick_hole_face_frame_z', 'brick_hole_face_frame_valid', 'brick_hole_face_axis', 'brick_hole_global_axis', 'brick_hole_resolution', 'brick_hole_uv_map',
              'brick_hole_material_mode', 'brick_hole_material_index', 'brick_hole_allow_repeat',
              'brick_hole_check_intersections',
              'brick_hole_reverse_u', 'brick_hole_preview_text', 'brick_hole_preview_warnings',
              'brick_hole_qa_state', 'brick_hole_last_errors', 'brick_hole_last_checked',
              'brick_hole_show_overlay', 'brick_hole_overlay_mode',
              'brick_hole_overlay_labels', 'brick_hole_overlay_error',
              'brick_hole_interactive_state', 'brick_hole_hud_placement',
              'brick_hole_show_source','brick_hole_show_wire','brick_hole_show_fill',
              'brick_hole_show_handles','brick_hole_show_labels')
    settings = {}
    for name in fields:
        value = getattr(scene, name, None)
        if name == 'brick_hole_manual_normal' and value is not None:
            value = _safe_float_tuple(value, 3)
        if isinstance(value, (bool, int, float, str, type(None), list)):
            settings[name] = value
        else:
            settings[name] = str(value)
    payload = {
        'schema': 3, 'addon_version': ADDON_VERSION_STRING,
        'build_id':BH0472_BUILD_ID,
        'capture_time_utc': datetime.now(timezone.utc).isoformat(),
        'blender': getattr(bpy.app, 'version_string', 'unknown'),
        'python': platform.python_version(), 'os': platform.platform(),
        'blend_file_path': str(getattr(bpy.data, 'filepath', '')),
        'mode': str(getattr(context, 'mode', 'unknown')),
        'mesh_select_mode': list(getattr(context.tool_settings, 'mesh_select_mode', ())),
        'settings': settings, 'viewport_layout': [], 'object': None,
        'selection_sample': [], 'selection_sample_truncated': False,
        'issue_samples': {}, 'validation_snapshot': {'status': 'not_checked'},
        'interactive': {'status':'idle'},
    }
    session = globals().get('_INTERACTIVE_SESSION')
    if session is not None and getattr(session, '_active', False):
        try:
            payload['interactive'] = {
                'status':'preview_running',
                'depth_m':float(session._depth),
                'direction':str(session._direction),
                'preview_precision':str(getattr(session,'_preview_precision','UNKNOWN')),
                'collapsed':bool(getattr(session,'_collapsed',False)),
                'view_open':bool(getattr(session,'_view_open',False)),
                'picking_face':bool(getattr(session,'_picking_face',False)),
                'regions':[(len(triangles),kind) for triangles,_normal,kind in
                           getattr(session,'_surfaces',())[:32]],
                'history_steps':len(getattr(session,'_history',())),
                'hovered_region':int(getattr(session,'_hover_region',-1)),
                'gpu_error':str(getattr(session,'_draw_error',''))[:200],
                'numeric_entry_pending':bool(getattr(session,'_numeric','')),
                'alternatives_open':bool(getattr(session,'_options_open',False)),
                'alternatives':tuple(dict(key=a['key'],status=a['status'],reason=a['reason'][:130])
                                      for a in getattr(session,'_direction_options',())[:8]),
                'preview_block_reason':str(getattr(session,'_preview_block_reason',''))[:180],
            }
        except (ValueError,TypeError,RuntimeError,ReferenceError,AttributeError) as exc:
            payload['interactive']={'status':'unavailable', 'reason':str(exc)[:160]}
    screen = getattr(context, 'screen', None)
    if screen:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                payload['viewport_layout'].append({
                    'x': area.x, 'y': area.y, 'width': area.width, 'height': area.height,
                    'regions': [{ 'type': r.type, 'x': r.x, 'y': r.y,
                                  'width': r.width, 'height': r.height} for r in area.regions],
                })
    if obj is not None and getattr(obj, 'type', None) == 'MESH':
        payload['object'] = {'name': obj.name,
                             'matrix_world': [_safe_float_tuple(row, 4) for row in obj.matrix_world],
                             'mesh_name': obj.data.name,
                             'scale': _safe_float_tuple(obj.scale, 3)}
        if callable(getattr(obj, 'get', None)) and obj.get('BH_fixture_schema', '') == FIXTURE_WALL_SCHEMA:
            payload['fixture_gallery'] = {
                'schema': obj.get('BH_fixture_schema', ''),
                'object': obj.name,
                'expected_cases': int(obj.get('BH_fixture_expected_cases', 0)),
                'resolution': int(obj.get('BH_fixture_resolution', 0)),
                'kind_counts': {'OPEN': 15, 'FILLED': 15},
                'manifest_available': bool(obj.get('BH_fixture_cases_json', '')),
                'mesh_vertices':len(obj.data.vertices),
                'mesh_faces':len(obj.data.polygons),
                'case_cursor':int(getattr(scene,'brick_hole_fixture_index',0)),
                'check_status':str(obj.get('BH_fixture_last_check','NOT_RUN'))[:120],
                'last_check_report':str(obj.get('BH_fixture_last_report_json',''))[:2200],
            }
            # The diagnostic bundle can identify the exact authored fixture
            # even after the user has edited its topology. Never trust an
            # arbitrary custom property as an unbounded JSON payload.
            fixture_manifest_raw = str(obj.get('BH_fixture_cases_json', ''))
            if len(fixture_manifest_raw) <= 40000:
                try:
                    fixture_manifest = json.loads(fixture_manifest_raw)
                    if isinstance(fixture_manifest, list) and len(fixture_manifest) <= 30:
                        payload['fixture_gallery']['cases'] = [
                            {key: case.get(key) for key in
                             ('id','type','kind','face_start','face_count',
                              'vertex_start','vertex_count')}
                            for case in fixture_manifest if isinstance(case, dict)
                        ]
                    else:
                        payload['fixture_gallery']['manifest_status']='INVALID_STRUCTURE'
                except (TypeError,ValueError):
                    payload['fixture_gallery']['manifest_status']='INVALID_JSON'
            else:
                payload['fixture_gallery']['manifest_status']='TOO_LARGE'
        try:
            if obj.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(obj.data)
                payload['object']['counts'] = {'vertices': len(bm.verts),
                                               'edges': len(bm.edges), 'faces': len(bm.faces)}
                selected = [edge for edge in bm.edges if edge.select and not edge.hide]
                payload['object']['selected_edges'] = len(selected)
                payload['object']['selected_faces'] = sum(f.select and not f.hide for f in bm.faces)
                for edge in selected[:MAX_SELECTED_EDGES]:
                    payload['selection_sample'].append([
                        _safe_float_tuple(obj.matrix_world @ vert.co, 3) for vert in edge.verts])
                payload['selection_sample_truncated'] = len(selected) > MAX_SELECTED_EDGES
                # Five tangible examples per category, bounded even if hundreds
                # of thousands of failures occur. This is a read-only QA pass.
                if len(bm.faces) > 25000:
                    payload['validation_snapshot'] = {
                        'status':'skipped_large_mesh', 'mesh_faces':len(bm.faces),
                        'limit_faces':25000,
                    }
                else:
                    try:
                        _bm, checked, failures = current_validation(context)
                        payload['validation_snapshot'] = {
                            'status':'checked', 'checked_faces':checked,
                            'failure_faces':len(failures), 'max_samples_per_category':5,
                        }
                        for face, reasons in failures:
                            for reason in reasons:
                                category = str(reason)[:160]
                                if category not in payload['issue_samples']:
                                    if len(payload['issue_samples']) >= 32:
                                        continue
                                    payload['issue_samples'][category] = []
                                examples = payload['issue_samples'][category]
                                if len(examples) >= 5:
                                    continue
                                coords = [tuple(float(v) for v in obj.matrix_world @ vert.co)
                                          for vert in islice(face.verts, 32)]
                                examples.append({
                                    'face_index':int(face.index),
                                    'material_index':int(face.material_index),
                                    'world_vertices':[_safe_float_tuple(v,3) for v in coords],
                                    'vertex_sample_truncated':len(face.verts)>32,
                                })
                    except (ValueError, RuntimeError, ReferenceError) as exc:
                        payload['validation_snapshot'] = {
                            'status':'unavailable', 'reason':f'{type(exc).__name__}: {exc}'[:300],
                        }
        except Exception as exc:
            payload['mesh_capture_error'] = f'{type(exc).__name__}: {exc}'
    return payload


def build_diagnostics_zip(context, destination=None):
    root = Path(destination) if destination is not None else Path.home() / 'Downloads'
    if not root.is_dir():
        root = Path(tempfile.gettempdir()) / 'BrickHoleDiagnostics'
    root.mkdir(parents=True, exist_ok=True)
    # Avoid overwriting diagnostics from simultaneous Blender sessions.
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    final = root / f'Brick_Hole_Diagnostics_{stamp}_{os.getpid()}.zip'
    temporary = final.with_suffix('.zip.partial')
    try:
        state = collect_diagnostics_state(context)
        log_data = bpy.data.texts.get(LOG_TEXT_NAME)
        log_content = log_data.as_string() if log_data else 'No Brick Hole log in this session.\n'
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('state.json', json.dumps(state, ensure_ascii=False, indent=2))
            archive.writestr('action_trace.json', json.dumps(list(_ACTION_TRACE), ensure_ascii=False, indent=2))
            archive.writestr('tessellation_witnesses.json', json.dumps(list(_TESSELLATION_WITNESSES), ensure_ascii=False, indent=2))
            archive.writestr('collision_witnesses.json', json.dumps(list(_COLLISION_WITNESSES), ensure_ascii=False, indent=2))
            # BH0467: fresh object-local evidence even when the user did not
            # open the inspector before creating a support ZIP. No Mesh edits.
            try:
                uv_evidence=seam_inspector.diagnostic_scan(context)
            except Exception as exc:
                uv_evidence={'summary':{'status':'SCAN_ERROR',
                             'error':f'{type(exc).__name__}: {exc}'[:400]},
                             'issues':[], 'image_pixels_tested':False}
            archive.writestr('uv_seam_inspector.json', json.dumps(
                uv_evidence,ensure_ascii=False,indent=2))
            archive.writestr('corner_trim_witnesses.json', json.dumps(list(_CORNER_TRIM_WITNESSES), ensure_ascii=False, indent=2))
            archive.writestr('runtime.log', log_content[:2_000_000])
            archive.writestr('README_RU.txt', (
                'Brick Hole: снимок для диагностики, включая collision_witnesses.json.\n'
                'Содержит версии, настройки, названия объектов, путь к .blend,\n'
                'выделение, примеры до 5 ошибок на категорию, панели и логи.\n'
                'НЕ содержит сам .blend, все меши, текстуры или скриншот экрана.\n'
                'Проверьте содержимое перед передачей третьим лицам.\n'
                'Без воспроизведения в Blender этот пакет не доказывает корректность геометрии.\n'))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('ZIP checksum failed')
        os.replace(temporary, final)
        return final
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class VIEW3D_OT_brick_hole_focus_error(bpy.types.Operator):
    bl_idname = 'view3d.brick_hole_focus_error'
    bl_label = 'Показать следующую ошибку'
    bl_description = 'Переместить камеру к проблемной грани без изменения выделения'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (getattr(context, 'area', None) is not None and
                context.area.type == 'VIEW_3D' and
                getattr(context, 'edit_object', None) is not None and
                context.edit_object.type == 'MESH')

    def execute(self, context):
        try:
            _bm, checked, problems = current_validation(context)
            if not problems:
                self.report({'INFO'}, f'Проблем нет: проверено {checked} граней')
                return {'CANCELLED'}
            scene = context.scene
            current = int(scene.brick_hole_issue_cursor) % len(problems)
            face, reasons = problems[current]
            center = sum((context.edit_object.matrix_world @ loop.vert.co
                          for loop in face.loops), Vector((0.0, 0.0, 0.0))) / len(face.loops)
            view = context.area.spaces.active.region_3d
            if view is None:
                raise RuntimeError('У 3D View нет региона камеры')
            view.view_location = center
            scene.brick_hole_issue_cursor = current + 1
            scene.brick_hole_last_error_summary = (
                f'#{current+1}/{len(problems)} · грань {face.index}: {", ".join(reasons)}')[:350]
            record_action('focus_error', scene.brick_hole_last_error_summary)
            context.area.tag_redraw()
            self.report({'WARNING'}, scene.brick_hole_last_error_summary)
            return {'FINISHED'}
        except (ValueError, RuntimeError, TypeError, ZeroDivisionError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class WM_OT_brick_hole_diagnostics_zip(bpy.types.Operator):
    bl_idname = 'wm.brick_hole_diagnostics_zip'
    bl_label = 'Собрать диагностику ZIP'
    bl_description = 'Собирает логи, состояния и выборку геометрии в Downloads; копирует путь к ZIP'

    def execute(self, context):
        try:
            record_action('diagnostics_request', 'one click')
            path = build_diagnostics_zip(context)
            context.window_manager.clipboard = str(path)
            self.report({'INFO'}, 'ZIP создан; путь скопирован в буфер обмена')
            return {'FINISHED'}
        except Exception as exc:
            record_action('diagnostics_error', f'{type(exc).__name__}: {exc}')
            self.report({'ERROR'}, f'Не удалось создать ZIP: {exc}')
            return {'CANCELLED'}


def write_log(message):
    text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
    text.write(str(message) + "\n")
    record_action("runtime", str(message))


def vec_text(value, digits=6):
    return "(" + ", ".join(f"{component:.{digits}f}" for component in value) + ")"


def safe_uv_borders(low, high):
    """All 4 m content boundaries crossed between two continued UV values."""
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("UV содержит NaN или бесконечность")
    if high < low:
        low, high = high, low
    first_index = math.floor((low - UV_SAFE_MIN) / UV_USABLE_SPAN) + 1
    last_index = math.ceil((high - UV_SAFE_MIN) / UV_USABLE_SPAN) - 1
    if last_index - first_index + 1 > MAX_CUTS:
        raise ValueError("Слишком много UV-разрезов; уменьшите глубину или выделение")
    # Avoid requesting a cut when one side is a float32/sub-pixel sliver.
    # Such grazing planes generated zero-area reveal fragments on curved walls.
    # This tolerance is STRICTLY below snap_uv_to_border's 4e-6 and therefore
    # does not silently push any real UV out of the useful tile.
    crossing_epsilon = 3.0e-6
    return [
        border for index in range(first_index, last_index + 1)
        for border in (UV_SAFE_MIN + index * UV_USABLE_SPAN,)
        if low < border - crossing_epsilon and high > border + crossing_epsilon
    ]


def cut_plane_crosses_polygon(points, plane_co, plane_no, epsilon=1.0e-6):
    """Reject tangent/grazing UV cuts before calling BMesh bisect_plane.

    ``points`` are the actual vertices of an individual reveal polygon. A
    genuine split needs vertices strictly on BOTH sides of the plane. Touching
    a polygon vertex or boundary must not create a microscopic triangle.
    """
    if not points:
        return False
    signed = [sum((float(p[i])-float(plane_co[i]))*float(plane_no[i])
                  for i in range(3)) for p in points]
    if not all(math.isfinite(v) for v in signed):
        raise ValueError('Некорректная геометрия UV-разреза')
    return min(signed) < -epsilon and max(signed) > epsilon


def reveal_degenerate_details(faces, threshold=1.0e-12):
    """Report exact faulty reveal faces; never delete or ignore bad geometry."""
    found=[]
    for face in faces:
        area=face.calc_area()
        if not math.isfinite(area) or area <= threshold:
            edge_lengths=[(v.co-face.verts[(i+1)%len(face.verts)].co).length
                          for i,v in enumerate(face.verts)]
            found.append((face.index, area, min(edge_lengths) if edge_lengths else 0.0))
    return found


def resolution_value(scene):
    return float(scene.brick_hole_resolution)


def physical_tile_size(resolution):
    return resolution * UV_USABLE_SPAN / PIXELS_PER_METER


def ordered_cycle(edges):
    """Order either one closed loop or one open, non-branching edge chain."""
    adjacency = {}
    for edge in edges:
        for vert in edge.verts:
            adjacency.setdefault(vert, []).append(edge)

    if not adjacency:
        raise ValueError("Не выделены рёбра")
    if any(len(linked) > 2 for linked in adjacency.values()):
        raise ValueError("Контур содержит ответвление — в одной вершине соединено больше двух рёбер")

    endpoints = [vert for vert, linked in adjacency.items() if len(linked) == 1]
    if len(endpoints) not in (0, 2):
        raise ValueError("Открытая цепочка должна иметь ровно две конечные вершины")
    if any(len(linked) not in (1, 2) for linked in adjacency.values()):
        raise ValueError("Контур содержит изолированную или неправильную вершину")

    start_vert = endpoints[0] if endpoints else next(iter(adjacency))
    current_vert = start_vert
    previous_edge = None
    result = []

    for _ in range(len(edges)):
        candidates = [e for e in adjacency[current_vert] if e is not previous_edge]
        if not candidates:
            raise ValueError("Цепочка неожиданно оборвалась")
        edge = candidates[0]
        next_vert = edge.other_vert(current_vert)
        result.append((edge, current_vert, next_vert))
        previous_edge = edge
        current_vert = next_vert

    used_edges = {item[0] for item in result}
    is_closed = not endpoints
    valid_end = current_vert is start_vert if is_closed else current_vert is endpoints[1]
    if not valid_end or len(used_edges) != len(edges):
        raise ValueError("Не удалось построить одну непрерывную цепочку")
    return result


def split_selected_cycles(edges):
    """Split BMesh selection into repeatable closed loops or open chains.

    Preserve the input BMesh traversal order rather than choosing arbitrary
    set seeds (hash order previously changed which contour ran first).
    """
    ordered_edges = list(edges)
    remaining = set(ordered_edges)
    cycles = []
    for seed in ordered_edges:
        if seed not in remaining:
            continue
        component = set()
        stack = [seed]
        while stack:
            edge = stack.pop()
            if edge in component:
                continue
            component.add(edge)
            for vert in edge.verts:
                stack.extend(linked for linked in vert.link_edges if linked in remaining)
        remaining.difference_update(component)
        cycles.append(ordered_cycle([edge for edge in ordered_edges if edge in component]))
    return cycles


def coordinate_key(coordinate, digits=6):
    return tuple(round(value, digits) for value in coordinate)


def world_distance(obj, a, b):
    return (obj.matrix_world.to_3x3() @ (b.co - a.co)).length


def discard_legacy_direction_layers(bm):
    # Migration only. No replacement attributes or persistent edge history.
    for name in ("brick_hole_dir_x", "brick_hole_dir_y", "brick_hole_dir_z"):
        layer = bm.edges.layers.float.get(name)
        if layer is not None:
            bm.edges.layers.float.remove(layer)
    layer = bm.edges.layers.int.get("brick_hole_dir_valid")
    if layer is not None:
        bm.edges.layers.int.remove(layer)



def miter_unit_direction(normal_a, normal_b, maximum_factor=4.0):
    """One shared vertex direction with unit offset along BOTH local normals.

    A welded bend cannot move the same vertex independently for two edges.
    Solving dot(displacement, normal_a/b) == 1 gives the symmetric miter.
    Nearly opposite normals or extreme miters fail closed; do not clamp a
    miter because clamping silently changes the requested physical depth.
    """
    a, b = normalized(normal_a), normalized(normal_b)
    cosine = dot(a, b)
    if cosine <= -0.875:
        raise ValueError('EDGE: резкий или обратный стык нормалей; корректное сопряжение невозможно')
    if cosine >= 1.0 - 1e-10:
        return a
    vector = tuple((a[i] + b[i]) / (1.0 + cosine) for i in range(3))
    factor = math.sqrt(dot(vector, vector))
    if not math.isfinite(factor) or factor > maximum_factor:
        raise ValueError('EDGE: слишком длинное сопряжение нормалей на углу')
    if abs(dot(vector, a) - 1.0) > 1e-6 or abs(dot(vector, b) - 1.0) > 1e-6:
        raise ValueError('EDGE: не удалось сохранить заданную глубину на стыке')
    return vector


def collision_guard_enabled(scene):
    """The default is deliberately ON for scenes created before BH-04B.6.6."""
    from .collision_policy_core import intersections_enabled
    return intersections_enabled(scene)


def _brick_hole_collision_guard_updated(scene, context):
    """Discard safety evidence whenever the user changes collision policy."""
    enabled=collision_guard_enabled(scene)
    invalidate_preview(scene, 'Защита пересечений изменена — требуется новая проверка')
    scene.brick_hole_safe_depth_candidate=0.0
    scene.brick_hole_safe_depth_status=(
        'Нажмите «Найти допустимую»' if enabled
        else 'КОЛЛИЗИИ НЕ ПРОВЕРЯЮТСЯ · SAFE DEPTH недоступен')
    record_action('collision_guard_setting_changed',
                  'ON · full validation' if enabled else 'OFF · user override, unchecked')


def edge_local_frame(obj, boundary_edges, scene, signed_depth, validate_shape=True, bm=None):
    """Readonly world-space normals and welded vertex offsets for EDGE.

    Returns (edge -> wall normal, vert -> displacement *per metre*).
    Single-face boundary edges inherit their actual face normal. Ambiguous
    internal edges are refused rather than arbitrarily picking a wall side.
    Manual XYZ remains an explicit uniform override for special cases.
    No mesh, UV, selection or Blender operators are mutated here.
    """
    edges = list(boundary_edges)
    if not edges:
        raise ValueError('EDGE: отсутствуют рёбра для вычисления направления')
    if not math.isfinite(float(signed_depth)) or abs(float(signed_depth)) <= EPS:
        raise ValueError('EDGE: недопустимая глубина')
    manual = getattr(scene, 'brick_hole_direction_source', 'AUTO') in {'MANUAL', 'FACE', 'GLOBAL'}
    normal_by_edge = {}
    incident = {}
    if manual:
        manual_normal = Vector(scene.brick_hole_manual_normal)
        if not all(math.isfinite(float(v)) for v in manual_normal) or manual_normal.length <= EPS:
            raise ValueError('EDGE: ручное направление XYZ должно быть ненулевым')
        manual_normal.normalize()
    result_tag = (bm.faces.layers.int.get(RESULT_LAYER_NAME) if bm is not None else None)
    for edge in edges:
        if len(edge.verts) != 2 or not edge.link_faces or len(edge.link_faces) > 2:
            raise ValueError('EDGE: ребро не имеет однозначной стеновой поверхности')
        tangent = obj.matrix_world.to_3x3() @ (edge.verts[1].co - edge.verts[0].co)
        if tangent.length <= EPS:
            raise ValueError('EDGE: найдено ребро нулевой длины')
        tangent.normalize()
        if manual:
            normal = manual_normal.copy()
        else:
            source = edge.link_faces[0]
            if len(edge.link_faces) == 2:
                # Repeated extrusion may leave one original wall polygon and
                # one generated reveal on the same selected source edge.
                # Honour the authored wall provenance, never guess by angle.
                originals = ([f for f in edge.link_faces if f[result_tag] == 0]
                             if result_tag is not None else [])
                if len(originals) == 1:
                    source = originals[0]
                else:
                    normals = [face_world_normal(obj, f) for f in edge.link_faces]
                    if normals[0].dot(normals[1]) < 0.999:
                        raise ValueError('EDGE: выбран внутренний шов между разными плоскостями. Выберите граничное ребро стены или задайте ручное направление XYZ')
            normal = face_world_normal(obj, source)
        if not all(math.isfinite(float(x)) for x in normal) or normal.length <= EPS:
            raise ValueError('EDGE: некорректная нормаль соседней грани')
        normal.normalize()
        if abs(normal.dot(tangent)) > 0.025:
            raise ValueError('EDGE: направление не перпендикулярно ребру стены')
        normal_by_edge[edge] = normal
        for vertex in edge.verts:
            incident.setdefault(vertex, []).append(normal)
    vertex_dirs = {}
    for vert, normals in incident.items():
        if len(normals) > 2:
            raise ValueError('EDGE: разветвление контура (более двух рёбер в вершине)')
        if manual or len(normals) == 1:
            direction = normals[0]
        else:
            direction = miter_unit_direction(tuple(normals[0]), tuple(normals[1]))
        vertex_dirs[vert] = Vector(direction)
    if validate_shape:
        transformed = []
        for edge in edges:
            a, b = edge.verts
            aw = tuple(obj.matrix_world @ a.co)
            bw = tuple(obj.matrix_world @ b.co)
            da = vertex_dirs[a] * float(signed_depth)
            db = vertex_dirs[b] * float(signed_depth)
            cw = tuple(aw[i] + da[i] for i in range(3))
            dw = tuple(bw[i] + db[i] for i in range(3))
            if math.sqrt(dot(tuple(dw[i]-cw[i] for i in range(3)),
                             tuple(dw[i]-cw[i] for i in range(3)))) <= EPS:
                raise ValueError('EDGE: смещение схлопывает ребро')
            transformed.append((a, b, cw, dw))
        if not collision_guard_enabled(scene):
            # Explicit expert opt-out: do NOT run intersection tests on
            # new strips; all degenerate/topology/UV gates stay active.
            record_action('collision_guard_bypassed',
                          f'edges={len(edges)}; depth={signed_depth:+.7g}; '
                          'USER_OVERRIDE=YES; intersections=NOT_CHECKED')
            write_log('BH0466 COLLISION PROTECTION OFF: unchecked intersections; mesh may overlap')
        else:
            # Check displaced contours against themselves, and ensure that a new
            # segment has not crossed a non-adjacent source edge. Shared BMVert
            # identities are preserved across neighbouring segments.
            verify_contour_segments(transformed)
            crossing_edges = []
            new_ids = {v: object() for v in vertex_dirs}
            for edge in edges:
                a, b = edge.verts
                aw = tuple(obj.matrix_world @ a.co)
                bw = tuple(obj.matrix_world @ b.co)
                da = vertex_dirs[a] * float(signed_depth)
                db = vertex_dirs[b] * float(signed_depth)
                cw = tuple(aw[i] + da[i] for i in range(3))
                dw = tuple(bw[i] + db[i] for i in range(3))
                crossing_edges.extend(((a,b,aw,bw), (new_ids[a],new_ids[b],cw,dw)))
            verify_contour_segments(crossing_edges)
            # BH-03.2B: fail closed BEFORE BMesh extrusion. Unlike segment checks,
            # this detects the full ruled side surfaces colliding with existing
            # source triangles or with one another. Original source UV is untouched.
            # Face Extrude Along Normals follows a different native operator path
            # and is NOT yet covered by this EDGE-specific guard.
            if bm is None:
                raise ValueError('COLLISION: требуется BMesh для проверки поверхностей')
            from .swept_collision_guard import validate_swept_edges
            swept = []
            for edge in edges:
                va, vb = edge.verts
                pa = tuple(float(v) for v in obj.matrix_world @ va.co)
                pb = tuple(float(v) for v in obj.matrix_world @ vb.co)
                qa = tuple(pa[k] + float(vertex_dirs[va][k]) * float(signed_depth)
                           for k in range(3))
                qb = tuple(pb[k] + float(vertex_dirs[vb][k]) * float(signed_depth)
                           for k in range(3))
                swept.append({'edge': (va, vb), 'quad': (pa, pb, qb, qa)})
            source_triangles = []
            source_extract_started = time.perf_counter()
            # Blender's own BMesh tessellation: do not guess a diagonal for a
            # concave polygon. Every candidate retains parent-face provenance.
            source_face_edges = {}
            source_face_indices = {}
            for face_i, face in enumerate(bm.faces):
                source_face_edges[face] = tuple((ed.verts[0], ed.verts[1]) for ed in face.edges)
                source_face_indices[face] = int(face_i)  # BMesh face.index may be dirty.
            for loops in bm.calc_loop_triangles():
                face = loops[0].face
                source_triangles.append({
                    'face': face,
                    'face_index': source_face_indices[face],
                    'tri': tuple(tuple(float(x) for x in obj.matrix_world @ loop.vert.co)
                                 for loop in loops),
                    'linked_edges': source_face_edges[face],
                    'verts': tuple(loop.vert for loop in loops),
                })
            source_extract_ms = (time.perf_counter()-source_extract_started)*1000.
            collision_stats = {}
            collision_started = time.perf_counter()
            try:
                checked_candidates = validate_swept_edges(
                    swept, source_triangles, witness=_capture_collision_witness,
                    statistics=collision_stats)
            except ValueError as exc:
                record_action('collision_swept_block',
                              f'EDGE strips={len(swept)}; triangles={len(source_triangles)}; '
                              f'depth={signed_depth:.7g}; method={collision_stats.get("broadphase", "N/A")}; '
                              f'extract_ms={source_extract_ms:.3f}; '
                              f'index_ms={collision_stats.get("build_index_ms", "N/A")}; '
                              f'guard_ms={(time.perf_counter()-collision_started)*1000.:.3f}; '
                              f'cause={str(exc)[:350]}')
                raise
            validation_state=collision_stats.get('validation_state','VALID')
            record_action('collision_swept_preflight',
                          f'EDGE strips={len(swept)}; triangles={len(source_triangles)}; '
                          f'method={collision_stats.get("broadphase", "N/A")}; '
                          f'extract_ms={source_extract_ms:.3f}; '
                          f'index_ms={collision_stats.get("build_index_ms", "N/A")}; '
                          f'guard_ms={collision_stats.get("elapsed_ms", "N/A")}; '
                          f'narrow_phase_candidates={checked_candidates}; '
                          f'remote_degenerate_skipped={collision_stats.get("remote_source_degenerate_ignored", 0)}; '
                          f'zero_area_children_proven_nonblocking={collision_stats.get("source_zero_area_children_proven_nonblocking", 0)}; '
                          f'validation_state={validation_state}')
            if collision_stats.get('budget_exhausted'):
                # Resource exhaustion is not geometry evidence. The transaction
                # remains protected by all topology/UV/post-build validators;
                # diagnostics explicitly state that collision coverage was partial.
                write_log('COLLISION ADVISORY: narrow-phase work budget exhausted; '
                          'no proven collision found in tested candidates; build allowed as UNCERTAIN')
                record_action('collision_guard_uncertain_allowed',
                              f'checked={checked_candidates}; source_triangles={len(source_triangles)}; '
                              'reason=RESOURCE_BUDGET_NOT_GEOMETRY_FAILURE')
    return normal_by_edge, vertex_dirs


def edge_local_uv_offset(world_edge, world_displacement, uv_edge_unit, uv_perp_unit, resolution):
    """Place the depth endpoint using its real physical tangent/perpendicular distances."""
    tangent = normalized(world_edge)
    lengthwise = dot(world_displacement, tangent)
    perp = tuple(world_displacement[i]-lengthwise*tangent[i] for i in range(3))
    crosswise = math.sqrt(max(0.0, dot(perp, perp)))
    if crosswise <= EPS:
        raise ValueError('EDGE: смещение вырождается в линию')
    factor = PIXELS_PER_METER / float(resolution)
    return Vector((uv_edge_unit[0] * lengthwise * factor + uv_perp_unit[0] * crosswise * factor,
                   uv_edge_unit[1] * lengthwise * factor + uv_perp_unit[1] * crosswise * factor))


def average_world_normal(obj, boundary_edges, scene=None):
    scene = scene if scene is not None else bpy.context.scene
    if scene.brick_hole_direction_source in {'MANUAL', 'FACE', 'GLOBAL'}:
        direction = Vector(scene.brick_hole_manual_normal)
        if not all(math.isfinite(v) for v in direction) or direction.length <= EPS:
            raise ValueError("Задайте ненулевую нормаль стены в мировых координатах")
        direction.normalize()
        return direction
    linear = obj.matrix_world.to_3x3()
    normal_matrix = linear.inverted().transposed()
    tangents = [tuple(linear @ (e.verts[1].co-e.verts[0].co)) for e in boundary_edges]
    faces = {face for edge in boundary_edges for face in edge.link_faces}
    orientation_sign = -1.0 if linear.determinant() < 0 else 1.0
    candidates = []
    for face in faces:
        origin = face.verts[0].co
        points = [linear @ (vert.co-origin) for vert in face.verts]
        area = sum(points[i].cross(points[i+1]).length*.5 for i in range(1,len(points)-1))
        candidates.append((tuple(orientation_sign * (normal_matrix @ face.normal)),max(area,EPS)))
    direction, source = fresh_direction(candidates,tangents)
    direction = Vector(direction)
    write_log(f"DIRECTION SOURCE: current selection ({source}); normal={vec_text(direction)}")
    return direction


def face_world_normal(obj, face):
    linear = obj.matrix_world.to_3x3()
    # For oriented triangles: cross(Ma, Mb) = det(M) * M^-T * cross(a,b).
    # Normal matrix alone is wrong for negative determinant (Mirror/scale).
    sign = -1.0 if linear.determinant() < 0 else 1.0
    return (sign * (linear.inverted().transposed() @ face.normal)).normalized()


def source_wall_face(obj, edge, dominant_normal):
    """Pick the linked face belonging to the dominant wall plane."""
    if not edge.link_faces:
        return None
    return max(edge.link_faces, key=lambda face:
               (face_world_normal(obj, face).dot(dominant_normal), -face.index))



class MESH_OT_brick_hole_extrude(bpy.types.Operator):
    bl_idname = "mesh.brick_hole_extrude"
    bl_label = "Экструдировать отверстие"
    bl_options = {'REGISTER', 'UNDO'}

    depth: FloatProperty(
        name="Глубина",
        description="Глубина откоса в метрах; знак меняет направление",
        default=0.30,
        soft_min=-2.0,
        soft_max=2.0,
        unit='LENGTH',
    )
    tile_size: FloatProperty(
        name="Размер UV-тайла",
        default=FIXED_TILE_SIZE_METERS,
        min=0.1,
        unit='LENGTH',
    )
    texture_resolution: IntProperty(default=4096, min=2048, options={'HIDDEN'})
    reverse_u: BoolProperty(name="Развернуть кладку по горизонтали", default=False)
    append_log: BoolProperty(default=False, options={'HIDDEN'})
    override_material_index: IntProperty(default=-1, options={'HIDDEN'})
    face_region: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        obj = context.edit_object
        return obj is not None and obj.type == 'MESH'

    @protected_operation
    def execute(self, context):
        if not self.append_log and not _TRANSACTION_ACTIVE:
            reset_log()
        # Project rule: one UV tile always represents exactly 4 x 4 metres.
        # Do not trust an old Scene value left behind by earlier script builds.
        active_resolution = float(self.texture_resolution)
        self.tile_size = physical_tile_size(active_resolution)
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        # Do not remove legacy named attributes inside a modelling operation.
        # They may belong to the user; cleanup requires separate consent.
        bm.faces.ensure_lookup_table()
        selected_faces = [face for face in bm.faces if face.select and not face.hide]
        selected_edges = (
            boundary_edges_for_faces(selected_faces)
            if self.face_region
            else [edge for edge in bm.edges if edge.select and not edge.hide]
        )
        write_log(f"Object: {obj.name}")
        if obj.get('BH_fixture_schema') == FIXTURE_WALL_SCHEMA:
            idx = int(getattr(context.scene, 'brick_hole_fixture_index', -1))
            case_id, match_status = 'UNKNOWN','UNVERIFIABLE'
            try:
                case_list=json.loads(obj.get('BH_fixture_cases_json',''))
                case_layer=bm.faces.layers.int.get('BH_fixture_case')
                if case_layer is not None and isinstance(case_list,list):
                    actual=(selected_faces if self.face_region else
                            [face for edge in selected_edges for face in edge.link_faces])
                    case_id,match_status=fixture_selected_case_ids(
                        [int(face[case_layer]) for face in actual],
                        'FILLED' if self.face_region else 'OPEN',case_list)
            except (TypeError, ValueError, KeyError, AttributeError, ReferenceError) as exc:
                match_status=f'UNVERIFIABLE_{type(exc).__name__}'
            write_log(f"FIXTURE TRACE: cursor_only={idx}; case_id={case_id}; "
                      f"source=selected_mesh_faces; status={match_status}")
            record_action('fixture_operation',
                          f'{case_id}; status={match_status}; '
                          f'kind={"FILLED" if self.face_region else "OPEN"}')
        write_log(f"Mode: {context.mode}; mesh select mode: {tuple(context.tool_settings.mesh_select_mode)}")
        write_log(f"Mesh counts V/E/F: {len(bm.verts)}/{len(bm.edges)}/{len(bm.faces)}")
        write_log(f"Matrix determinant: {obj.matrix_world.to_3x3().determinant():.9g}")
        write_log(f"Object dimensions, m: {vec_text(obj.dimensions)}")
        write_log(f"Object scale: {vec_text(obj.scale)}")
        write_log(f"Depth, m: {self.depth:.6f}")
        write_log(f"Tile size, m: {self.tile_size:.6f}")
        write_log(f"Target density: {PIXELS_PER_METER:.2f} px/m")
        write_log(f"Texture resolution: {int(active_resolution)} px")
        write_log(f"UV safe range: [{UV_SAFE_MIN:.8f}, {UV_SAFE_MAX:.8f}]")
        write_log(f"UV margin: {UV_MARGIN:.8f} = {(UV_MARGIN * active_resolution):.1f} px per side")
        write_log(f"Input mode: {'FILLED POLYGONS' if self.face_region else 'BOUNDARY EDGES'}")
        write_log(f"Selected faces: {len(selected_faces) if self.face_region else 0}")
        write_log(f"Boundary edges: {len(selected_edges)}")
        selected_world_points = [
            obj.matrix_world @ vert.co
            for edge in selected_edges
            for vert in edge.verts
        ]
        if selected_world_points:
            ranges = [
                max(point[axis] for point in selected_world_points)
                - min(point[axis] for point in selected_world_points)
                for axis in range(3)
            ]
            write_log(f"Selected contour world bounds, m: {vec_text(ranges)}")
        for index, edge in enumerate(selected_edges, 1):
            length = world_distance(obj, edge.verts[0], edge.verts[1])
            write_log(
                f"  Edge {index}: length={length:.6f} m; "
                f"A={vec_text(edge.verts[0].co)}; B={vec_text(edge.verts[1].co)}"
            )

        if abs(self.depth) <= EPS:
            write_log("ERROR: Depth equals zero")
            self.report({'ERROR'}, "Глубина не должна быть равна нулю")
            return {'CANCELLED'}
        try:
            cycles = split_selected_cycles(selected_edges)
            if self.face_region:
                if not selected_faces:
                    raise ValueError("Не выбраны полигоны")
                if any(cycle[0][1] is not cycle[-1][2] for cycle in cycles):
                    raise ValueError("Граница выбранных полигонов должна быть замкнута")
                boundary_normal = filled_boundary_world_normal(obj, selected_edges[0], selected_faces)
            else:
                if len(cycles) != 1:
                    raise ValueError("Ядро ожидает один контур")
                edge_normals, edge_vertex_offsets = edge_local_frame(
                    obj, selected_edges, context.scene, self.depth, bm=bm,
                )
                boundary_normal = edge_normals[selected_edges[0]]
                write_log(f"EDGE LOCAL FRAME: edges={len(edge_normals)}; "
                          f"verts={len(edge_vertex_offsets)}; "
                          f"max_miter={max(v.length for v in edge_vertex_offsets.values()):.5f}; "
                          f"mode={context.scene.brick_hole_direction_source}")
            write_log(f"Boundary loops: {len(cycles)}; edges: {len(selected_edges)}")
            write_log(
                f"Contour perimeter, m: "
                f"{sum(world_distance(obj, edge.verts[0], edge.verts[1]) for edge in selected_edges):.6f}"
            )
            write_log(f"Extrusion world normal: {vec_text(boundary_normal)}")
        except (ValueError, ReferenceError) as exc:
            write_log(f"ERROR during contour validation: {exc}")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        edge_lengths = [world_distance(obj, edge.verts[0], edge.verts[1]) for edge in selected_edges]
        long_edges = [length for length in edge_lengths if length > self.tile_size + EPS]
        write_log(f"Edges longer than one 4 m tile: {len(long_edges)}")
        if long_edges:
            write_log("Long edge lengths, m: " + ", ".join(f"{length:.6f}" for length in long_edges))
        if abs(self.depth) > self.tile_size + EPS:
            write_log(
                f"Depth crosses approximately {math.ceil(abs(self.depth) / self.tile_size)} UV tiles; "
                "multi-tile cutting enabled"
            )

        # Preserve the existing wall UV exactly at every opening edge.  This
        # is what makes the brick pattern continue instead of restarting.
        requested_uv_name = context.scene.brick_hole_uv_map
        source_uv_layer = (
            bm.loops.layers.uv.get(requested_uv_name)
            if requested_uv_name else bm.loops.layers.uv.active
        )
        if source_uv_layer is None:
            write_log("ERROR: No active UV map")
            self.report({'ERROR'}, "У стены нет активной UV-карты")
            return {'CANCELLED'}
        uv_layer_name = source_uv_layer.name
        write_log(f"Active UV map: {uv_layer_name}")
        edge_uv_data = []
        for edge in selected_edges:
            if not edge.link_faces:
                write_log("ERROR: Selected edge has no linked wall face")
                self.report({'ERROR'}, "У одного из рёбер нет полигона стены с UV")
                return {'CANCELLED'}
            source_face = resolve_source_face(
                obj, edge,
                filled_boundary_world_normal(obj, edge, selected_faces)
                if self.face_region else edge_normals[edge],
                selected_faces, face_mode=self.face_region,
            )
            source_kind = (
                "surrounding wall" if self.face_region and
                source_face not in selected_faces else
                "selected cap fallback" if self.face_region else "boundary wall"
            )
            if source_face is None:
                self.report({'ERROR'}, "Не найден исходный полигон у границы выделения")
                return {'CANCELLED'}
            a, b = edge.verts
            loop_a = next((loop for loop in source_face.loops if loop.vert is a), None)
            loop_b = next((loop for loop in source_face.loops if loop.vert is b), None)
            if loop_a is None or loop_b is None:
                write_log("ERROR: Failed to find boundary loops in source face")
                self.report({'ERROR'}, "Не удалось прочитать UV вдоль контура")
                return {'CANCELLED'}

            host_uv_a = loop_a[source_uv_layer].uv.copy()
            host_uv_b = loop_b[source_uv_layer].uv.copy()
            physical_edge_m = world_distance(obj, a, b)
            # Only NEW reveal loops use the rescaled endpoints. Source wall
            # loops remain untouched, even on complex paths with 816 px/m.
            source_policy = corrected_reveal_uv_edge(
                host_uv_a, host_uv_b, physical_edge_m,
                active_resolution, PIXELS_PER_METER)
            uv_a = Vector(source_policy['uv_a'])
            uv_b = Vector(source_policy['uv_b'])
            if source_policy['needs_warning']:
                write_log('NEW_REVEAL_UV_1000 SOURCE_UV_ADVISORY: '
                          f"source={source_policy['source_density']:.2f} px/m; "
                          f"target=1000.00 px/m; max_seam_shift="
                          f"{source_policy['max_seam_shift_pixels']:.2f} px; "
                          'host_uv_preserved=YES')
            from .uv_orientation_policy import outward_from_source_uv
            # On a concave face the centroid of the *other* UV loops can lie
            # on the wrong side of the boundary. The signed UV winding of the
            # original source polygon fixes handedness without guessing.
            forward=(loop_a.link_loop_next.vert is b)
            backward=(loop_b.link_loop_next.vert is a)
            if forward == backward:
                raise RuntimeError('Исходное ребро не сопоставлено направлению UV-полигона')
            uv_direction=outward_from_source_uv(
                tuple(host_uv_a),tuple(host_uv_b),
                [tuple(loop[source_uv_layer].uv) for loop in source_face.loops],
                forward)
            uv_out=Vector(uv_direction)
            if self.reverse_u:
                # Legacy reverse_u reflected the reveal UV across its source
                # edge, creating mirrored texture islands. The wall's source
                # UV and outward-unfolded orientation remain authoritative.
                write_log('UV_FLIP_LEGACY_IGNORED: reflection prohibited; original UV handedness retained')

            edge_uv_data.append({
                "a_co": a.co.copy(),
                "b_co": b.co.copy(),
                "uv_a": uv_a,
                "uv_b": uv_b,
                "uv_out": uv_out,
                "material_index": source_face.material_index,
                "source_kind": source_kind,
            })
            world_len = world_distance(obj, a, b)
            uv_len = (uv_b - uv_a).length
            write_log(
                f"  UV edge: world={world_len:.6f} m; uv_len={uv_len:.6f}; "
                f"A={vec_text(uv_a)}; B={vec_text(uv_b)}; out={vec_text(uv_out)}"
                f"; source={source_kind}"
            )
            for axis, axis_name in ((0, 'U'), (1, 'V')):
                low = min(uv_a[axis], uv_b[axis])
                high = max(uv_a[axis], uv_b[axis])
                borders = safe_uv_borders(low, high)
                for next_border in borders:
                    fraction = (next_border - low) / (high - low)
                    write_log(
                        f"    {axis_name} safe border {next_border:.8f}: approx {fraction * world_len:.6f} m "
                        f"from lower-{axis_name} endpoint"
                    )

        edge_data_by_key = {}
        for item in edge_uv_data:
            key = frozenset((
                coordinate_key(item["a_co"], 7),
                coordinate_key(item["b_co"], 7),
            ))
            if key in edge_data_by_key:
                raise RuntimeError(
                    "В контуре найдены совпадающие рёбра. Удалите двойную геометрию"
                )
            edge_data_by_key[key] = item

        def source_data_for_side(face):
            matches = []
            for side_edge in face.edges:
                key = frozenset(
                    coordinate_key(vert.co, 7) for vert in side_edge.verts
                )
                item = edge_data_by_key.get(key)
                if item is not None and all(item is not existing for existing in matches):
                    matches.append(item)
            if len(matches) > 1:
                raise RuntimeError(
                    f"Боковая грань {face.index} совпала с несколькими рёбрами"
                )
            return matches[0] if matches else None

        # Edge mode uses BMesh extrusion. Filled polygon mode deliberately
        # uses Blender's own "Extrude Faces Along Normals" macro.
        world_displacement = boundary_normal * self.depth
        local_displacement = obj.matrix_world.to_3x3().inverted() @ world_displacement
        if bm.faces.layers.int.get(RESULT_LAYER_NAME) is None:
            bm.faces.layers.int.new(RESULT_LAYER_NAME)
        if self.face_region and bm.faces.layers.int.get(TEMP_CAP_LAYER_NAME) is None:
            bm.faces.layers.int.new(TEMP_CAP_LAYER_NAME)
        bm = bmesh.from_edit_mesh(obj.data)
        result_layer = bm.faces.layers.int.get(RESULT_LAYER_NAME)
        cap_layer = bm.faces.layers.int.get(TEMP_CAP_LAYER_NAME)
        uv_layer = bm.loops.layers.uv.get(uv_layer_name)
        selected_faces = [face for face in bm.faces if face.select and not face.hide]
        selected_edges = (
            boundary_edges_for_faces(selected_faces)
            if self.face_region
            else [edge for edge in bm.edges if edge.select and not edge.hide]
        )
        next_region_id = max((f[result_layer] for f in bm.faces), default=0) + 1
        if self.face_region:
            from .native_direction_contract import plan_native_faces
            face_plan = plan_native_faces(
                getattr(context.scene, 'brick_hole_direction_source', 'AUTO'),
                getattr(context.scene, 'brick_hole_manual_normal', (0., 1., 0.)),
                self.depth,
                (tuple(face_world_normal(obj, face)) for face in selected_faces),
            )
            source_face_count = len(selected_faces)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            region_kwargs = {
                "use_normal_flip": False,
                "use_dissolve_ortho_edges": False,
                "mirror": False,
            }
            if face_plan.mode == 'ALONG_NORMALS_EVEN':
                _build_stage('BLENDER_ALONG_NORMALS', f'faces={source_face_count}; depth={self.depth:.6g}')
                native_result = bpy.ops.mesh.extrude_region_shrink_fatten(
                    MESH_OT_extrude_region=region_kwargs,
                    TRANSFORM_OT_shrink_fatten={
                        "value": self.depth,
                        "use_even_offset": True,
                        "mirror": False,
                        "use_proportional_edit": False,
                    },
                )
            else:
                # Native Extrude Region + world-space Move (not Shrink/Fatten).
                # Do not modify face normals: manual XYZ is only a displacement.
                _build_stage('BLENDER_REGION_TRANSLATE',
                    f'faces={source_face_count}; world_delta={face_plan.world_displacement}')
                native_result = bpy.ops.mesh.extrude_region_move(
                    MESH_OT_extrude_region=region_kwargs,
                    TRANSFORM_OT_translate={
                        "value": face_plan.world_displacement,
                        "orient_type": "GLOBAL",
                        "mirror": False,
                        "use_proportional_edit": False,
                    },
                )
            if 'FINISHED' not in native_result:
                raise RuntimeError("Blender отменил штатную экструзию FILLED")
            bm = bmesh.from_edit_mesh(obj.data)
            result_layer = bm.faces.layers.int.get(RESULT_LAYER_NAME)
            cap_layer = bm.faces.layers.int.get(TEMP_CAP_LAYER_NAME)
            uv_layer = bm.loops.layers.uv.get(uv_layer_name)
            # Native Blender selects the freshly extruded caps. Determine
            # the reveal quads by adjacency to those caps, not coordinate
            # signatures: exact coordinate hashes are fragile under an Even
            # Offset, floating precision and strong bends.
            result_faces = [face for face in bm.faces if face.select and not face.hide]
            _build_stage('CAP_TOPOLOGY', f'caps={len(result_faces)}; expected={source_face_count}')
            if len(result_faces) != source_face_count:
                raise RuntimeError('Blender Along Normals: число новых лицевых граней отличается от исходного')
            cap_set = set(result_faces)
            side_candidates = {other for cap in result_faces for edge in cap.edges
                               for other in edge.link_faces if other not in cap_set}
            created_faces = list(side_candidates)
            new_vertices = []
            new_vertex_set = set()
        else:
            source_face_count = 0
            faces_before_extrude = set(bm.faces)
            source_verts = set(bm.verts)
            result = bmesh.ops.extrude_edge_only(
                bm, edges=selected_edges, use_normal_flip=False
            )
            new_vertices = [g for g in result['geom'] if isinstance(g, bmesh.types.BMVert)]
            new_vertex_set = set(new_vertices)
            result_faces = [g for g in result['geom'] if isinstance(g, bmesh.types.BMFace)]
            created_faces = created_faces_since(faces_before_extrude, bm.faces)
            # Move each newly extruded vertex according to its source wall
            # normals. Shared bend vertices use the SAME miter direction.
            # No guessed position matching: topology in the new side quads
            # establishes source->extruded correspondence.
            source_to_new = {}
            for side in result_faces:
                for loop in side.loops:
                    old_vert = loop.vert
                    if old_vert not in source_verts or old_vert not in edge_vertex_offsets:
                        continue
                    neighboring_new = [l.vert for l in (loop.link_loop_prev, loop.link_loop_next)
                                       if l.vert in new_vertex_set]
                    if len(neighboring_new) != 1:
                        raise RuntimeError('EDGE: неоднозначная связь старой и новой вершины')
                    new_vert = neighboring_new[0]
                    prior = source_to_new.get(old_vert)
                    if prior is not None and prior is not new_vert:
                        raise RuntimeError('EDGE: вершина стыка была разъединена экструзией')
                    source_to_new[old_vert] = new_vert
            if set(source_to_new) != set(edge_vertex_offsets) or len(set(source_to_new.values())) != len(source_to_new):
                raise RuntimeError('EDGE: не все новые вершины соответствуют исходным; откат')
            local_matrix_inverse = obj.matrix_world.to_3x3().inverted()
            for old_vert, new_vert in source_to_new.items():
                new_vert.co = old_vert.co + local_matrix_inverse @ (
                    edge_vertex_offsets[old_vert] * self.depth)
            bm.normal_update()
        face_data_map = {}
        if self.face_region:
            _build_stage('REVEAL_MAPPING', f'native cap neighbors={len(created_faces)}; edges={len(edge_uv_data)}')
            new_faces = []
            for face in created_faces:
                if len(face.verts) != 4:
                    continue  # Only Blender's side quads can own an original opening edge.
                source_data = source_data_for_side(face)
                if source_data is not None:
                    new_faces.append(face)
                    face_data_map[face] = source_data
            cap_faces = result_faces
            write_log(
                f"Native {face_plan.mode}: selected caps={len(cap_faces)}; "
                f"cap-neighbor faces={len(created_faces)}; "
                f"mapped reveals={len(new_faces)}; expected={len(edge_uv_data)}"
            )
        else:
            new_faces = result_faces
            cap_faces = []
        if len(new_faces) != len(edge_uv_data):
            raise RuntimeError(
                f'Blender создал {len(new_faces)} боковых граней для {len(edge_uv_data)} рёбер; '
                'пришлите BrickHole Diagnostics ZIP — детали находятся в BUILD_STAGE REVEAL_MAPPING'
            )
        if any(len(face.verts) != 4 for face in new_faces):
            raise RuntimeError("Экструзия создала боковую грань, которая не является четырёхугольником")
        if self.face_region and len(cap_faces) != source_face_count:
            raise RuntimeError(
                f"Окно потеряно при экструзии: было полигонов {source_face_count}, "
                f"стало {len(cap_faces)}"
            )
        if self.face_region:
            for face in cap_faces:
                face[cap_layer] = 1
            write_log(
                f"Window cap preserved: faces={len(cap_faces)}; "
                f"side reveals={len(new_faces)}"
            )

        assigned_faces = 0
        reveal_regions = []
        for face in new_faces:
            data = face_data_map.get(face)
            if data is None:
                data = source_data_for_side(face)
            if data is None:
                raise RuntimeError("Новый откос не сопоставлен исходному ребру")

            face.material_index = (
                self.override_material_index
                if self.override_material_index >= 0
                else data["material_index"]
            )
            face[result_layer] = next_region_id
            next_region_id += 1

            if self.face_region:
                new_a = extruded_counterpart(face, data["a_co"], data["b_co"])
                new_b = extruded_counterpart(face, data["b_co"], data["a_co"])
                depth_vector_a = new_a.co - data["a_co"]
                depth_vector_b = new_b.co - data["b_co"]
                depth_vector = (depth_vector_a + depth_vector_b) * 0.5
                depth_a_world = (
                    obj.matrix_world.to_3x3() @ depth_vector_a
                ).length
                depth_b_world = (
                    obj.matrix_world.to_3x3() @ depth_vector_b
                ).length
                # Along Normals can slide an endpoint ALONG the source
                # edge. A perpendicular-only UV offset silently distorts the
                # physical diagonal. Project real world displacement into
                # tangent/perpendicular axes, just like the EDGE local core.
                linear = obj.matrix_world.to_3x3()
                edge_world = tuple(linear @ (data["b_co"] - data["a_co"]))
                uv_tangent = tuple((data["uv_b"] - data["uv_a"]).normalized())
                depth_uv_a = edge_local_uv_offset(
                    edge_world, tuple(linear @ depth_vector_a),
                    uv_tangent, tuple(data["uv_out"]), active_resolution)
                depth_uv_b = edge_local_uv_offset(
                    edge_world, tuple(linear @ depth_vector_b),
                    uv_tangent, tuple(data["uv_out"]), active_resolution)
                new_a_co = new_a.co.copy()
                new_b_co = new_b.co.copy()
            else:
                new_a = extruded_counterpart(face, data["a_co"], data["b_co"])
                new_b = extruded_counterpart(face, data["b_co"], data["a_co"])
                new_a_co = new_a.co.copy()
                new_b_co = new_b.co.copy()
                disp_a = new_a_co - data["a_co"]
                disp_b = new_b_co - data["b_co"]
                depth_vector = (disp_a + disp_b) * 0.5
                linear = obj.matrix_world.to_3x3()
                edge_world = tuple(linear @ (data["b_co"] - data["a_co"]))
                depth_uv_a = edge_local_uv_offset(
                    edge_world, tuple(linear @ disp_a),
                    tuple((data["uv_b"] - data["uv_a"]).normalized()),
                    tuple(data["uv_out"]), active_resolution,
                )
                depth_uv_b = edge_local_uv_offset(
                    edge_world, tuple(linear @ disp_b),
                    tuple((data["uv_b"] - data["uv_a"]).normalized()),
                    tuple(data["uv_out"]), active_resolution,
                )
            targets = (
                (data["a_co"], data["uv_a"]),
                (data["b_co"], data["uv_b"]),
                (new_a_co, data["uv_a"] + depth_uv_a),
                (new_b_co, data["uv_b"] + depth_uv_b),
            )
            corners = []
            unmatched = set(range(4))
            for loop in face.loops:
                best = min(unmatched,
                           key=lambda i: (loop.vert.co - targets[i][0]).length_squared)
                distance_sq = (loop.vert.co - targets[best][0]).length_squared
                if distance_sq > 1.0e-10:
                    raise RuntimeError("Неоднозначное сопоставление угла откоса и его UV")
                unmatched.remove(best)
                target_uv = targets[best][1]
                loop[uv_layer].uv = target_uv
                corners.append((loop.vert.co.copy(), target_uv.copy()))
            if unmatched:
                raise RuntimeError("Не все углы откоса сопоставлены UV")

            edge_vector = data["b_co"] - data["a_co"]
            region_normal = edge_vector.cross(depth_vector)
            if edge_vector.length <= EPS or depth_vector.length <= EPS or region_normal.length <= EPS:
                raise RuntimeError("Направление создаёт вырожденный откос")
            chart = make_chart(tuple(data["a_co"]), tuple(edge_vector),
                               tuple(depth_vector), tuple(data["uv_a"]),
                               tuple(data["uv_b"] - data["uv_a"]), tuple(depth_uv_a))
            error_px = affine_corner_error_pixels(
                [tuple(co) for co, _uv in corners],
                [tuple(uv) for _co, uv in corners], chart, active_resolution,
            )
            twist_m = quad_nonplanarity_meters([tuple(co) for co, _uv in corners])
            reveal_regions.append({
                "source_face": face,
                "material_index": face.material_index,
                "needs_triangles": (
                    error_px > UV_AFFINE_MAX_ERROR_PIXELS or twist_m > 1.0e-5
                    or quad_uv_metric_exceeds_tolerance(
                        [tuple(co) for co,_ in corners],
                        [tuple(uv) for _,uv in corners], active_resolution)
                ),
                "nonplanarity_m": twist_m,
                "affine_error_px": error_px,
                "corner_uvs": {tuple(co): tuple(uv) for co, uv in corners},
                "quad_points": (tuple(data["a_co"]),tuple(data["b_co"]),
                                tuple(new_a_co),tuple(new_b_co)),
                "source_uv": (tuple(data["uv_a"]),tuple(data["uv_b"])),
                "source_uv_out":tuple(data["uv_out"]),
                "chart": chart,
                "region_id": face[result_layer],
                "origin": data["a_co"].copy(),
                "edge_dir": edge_vector.normalized(),
                "edge_length": edge_vector.length,
                "depth_dir": depth_vector.normalized(),
                "depth_length": depth_vector.length,
                "normal": region_normal.normalized(),
                "corners": corners,
            })
            u_values = [uv.x for _co, uv in corners]
            v_values = [uv.y for _co, uv in corners]
            write_log(
                f"  Reveal face {face.index}: raw U=[{min(u_values):.6f}, {max(u_values):.6f}], "
                f"raw V=[{min(v_values):.6f}, {max(v_values):.6f}]"
            )
            assigned_faces += 1

        # Capture non-target mesh invariants after the native extrusion and
        # before triangulating/bisecting tagged reveal geometry.
        baseline_non_target = non_target_mesh_invariants(
            bm, uv_layer, result_layer, obj.matrix_world,
        )
        # The old one-chart-for-a-quad approximation is only exact when all
        # four corners fit one affine mapping. Native Along Normals may yield
        # different depths per endpoint or a twisted quad. Triangulate ONLY
        # these reveals; each triangle receives an exact three-corner chart.
        corrected_regions = []
        for region in reveal_regions:
            if not region["needs_triangles"]:
                corrected_regions.append(region)
                continue
            source_face = region["source_face"]
            original_uvs = region["corner_uvs"]
            write_log(
                f"TRIANGLE FALLBACK: region {region['region_id']}; "
                f"affine error {region['affine_error_px']:.4f} px; "
                f"twist {region['nonplanarity_m']:.7f} m"
            )
            # BEAUTY chooses a diagonal with better shaped triangles on the
            # strongly bent 11-face/24-boundary-edge case. FIXED can produce
            # a zero-area triangle when native Even Offset shears a quad.
            # Still validate exact areas: NEVER conceal bad topology.
            result_tri = bmesh.ops.triangulate(
                bm, faces=[source_face], quad_method='BEAUTY', ngon_method='EAR_CLIP',
            )
            triangles = list(result_tri.get('faces', []))
            if len(triangles) != 2 or any(len(f.verts) != 3 for f in triangles):
                raise RuntimeError("Не удалось безопасно разделить неплоский откос на два треугольника")
            invalid = reveal_degenerate_details(triangles)
            if invalid:
                raise RuntimeError(f"Треугольник откоса вырожден после нативного Along Normals: {invalid[:4]}; "
                                   "уменьшите глубину или скорректируйте геометрию изгиба")
            # Unfold the exact triangulation returned by BMesh, not a guess
            # about quad diagonals or a single non-isometric 4-corner chart.
            quad_points=region["quad_points"]
            def corner_id(v):
                options=[i for i,p in enumerate(quad_points)
                         if math.dist(tuple(v.co),p)<=1.0e-5]
                if len(options)!=1:
                    raise RuntimeError('Неоднозначная вершина UV-треугольника')
                return options[0]
            triangle_ids=[tuple(corner_id(loop.vert) for loop in tri.loops)
                          for tri in triangles]
            world_points=[tuple(obj.matrix_world @ Vector(p)) for p in quad_points]
            exact_uvs=unfold_reveal_quad(
                world_points, *region["source_uv"],region["source_uv_out"],
                active_resolution,triangle_ids)
            write_log(f"TRIANGLE UV UNFOLD: region {region['region_id']}; "
                      f"two exact world-metric triangles")
            for index, tri in enumerate(triangles):
                uid = region["region_id"] if index == 0 else next_region_id
                if index:
                    next_region_id += 1
                tri[result_layer] = uid
                tri.material_index = region["material_index"]
                tri_corners = []
                for loop in tri.loops:
                    raw=exact_uvs[corner_id(loop.vert)]
                    loop[uv_layer].uv=raw
                    tri_corners.append((tuple(loop.vert.co),tuple(raw)))
                p0, p1, p2 = (item[0] for item in tri_corners)
                t0, t1, t2 = (item[1] for item in tri_corners)
                chart = make_chart(p0,
                                   tuple(p1[j] - p0[j] for j in range(3)),
                                   tuple(p2[j] - p0[j] for j in range(3)),
                                   t0,
                                   tuple(t1[j] - t0[j] for j in (0, 1)),
                                   tuple(t2[j] - t0[j] for j in (0, 1)))
                corrected_regions.append({
                    **region, "source_face": tri, "region_id": uid,
                    "needs_triangles": False, "chart": chart,
                    "corner_uvs": {co: uv for co, uv in tri_corners},
                    "corners": [(Vector(co), Vector(uv)) for co, uv in tri_corners],
                })
        reveal_regions = corrected_regions
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=True)

        def region_faces(current_bm, region):
            layer = current_bm.faces.layers.int.get(RESULT_LAYER_NAME)
            if layer is None:
                raise RuntimeError("Потеряны метки откосов")
            return [f for f in current_bm.faces if f[layer] == region["region_id"]]

        def cut_requests(region):
            # An affine UV coordinate is a plane equation. No endpoint
            # deduplication or short-line threshold can silently omit a cut.
            for axis in (0, 1):
                values = [uv[axis] for _co, uv in region["corners"]]
                for border in safe_uv_borders(min(values), max(values)):
                    point, normal = chart_plane(region["chart"], axis, border)
                    yield axis, border, point, normal

        cuts_done = 0
        _build_stage('UV_SAFE_CUTS', f'areas={len(reveal_regions)}')
        # Split only tagged reveal geometry at safe UV borders.
        for region in reveal_regions:
            requests = list(cut_requests(region))
            if cuts_done + len(requests) > MAX_CUTS:
                raise RuntimeError("Достигнут лимит реальных UV-разрезов; модель сохранена откатом")
            write_log(f"Region {region['region_id']} cut requests: {len(requests)}")
            for request_index, (axis, border, point, normal) in enumerate(requests, 1):
                point_a, plane_normal = Vector(point), Vector(normal)
                write_log(f"  Cut {request_index}: axis={axis}; border={border:.9f}")
                try:
                    # Explicit geometry prevents selection flushing from including wall faces.
                    bm = bmesh.from_edit_mesh(obj.data)
                    affected = region_faces(bm, region)
                    if not affected:
                        raise RuntimeError("Потеряны грани откоса перед разрезом")
                    before_count = len(affected)
                    # A request from the full chart can be tangent to an
                    # individual face. Skip unless there is a REAL crossing;
                    # bisect on a boundary produces a zero-area polygon.
                    candidates = [f for f in affected if cut_plane_crosses_polygon(
                        [tuple(v.co) for v in f.verts], point_a, plane_normal)]
                    if not candidates:
                        write_log('    SKIP tangent/grazing cut (no polygon spans plane)')
                        continue
                    cut_edges = {e for f in candidates for e in f.edges}
                    cut_verts = {v for e in cut_edges for v in e.verts}
                    bmesh.ops.bisect_plane(
                        bm, geom=list(cut_verts) + list(cut_edges) + candidates,
                        plane_co=point_a, plane_no=plane_normal,
                        dist=1.0e-7, clear_inner=False, clear_outer=False,
                    )
                    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=True)
                    cuts_done += 1
                    bm = bmesh.from_edit_mesh(obj.data)
                    after_count = len(region_faces(bm, region))
                    if not after_count or after_count > MAX_PARTS:
                        raise RuntimeError("После bisect потерян откос или превышен лимит частей")
                    invalid = reveal_degenerate_details(region_faces(bm, region))
                    if invalid:
                        raise RuntimeError(f'UV-разрез {request_index} создал вырожденные грани ' 
                                           f'в регионе {region["region_id"]}: {invalid[:6]}')
                    write_log(f"    Bisect faces: {before_count} -> {after_count}")
                except RuntimeError as exc:
                    write_log(f"    ERROR during bisect: {exc}")
                    self.report({'ERROR'}, f"Не удалось безопасно разрезать UV-границу: {exc}")
                    return {'CANCELLED'}

        _build_stage('UV_RECONSTRUCTION', f'cuts={cuts_done}')
        # Rebuild raw UV from the original affine chart. BMesh interpolation
        # is not the authority after multiple cuts through shared vertices.
        bm = bmesh.from_edit_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.get(uv_layer_name)
        packed_faces = 0
        violations = 0
        processed = set()
        for region in reveal_regions:
            for face in region_faces(bm, region):
                if face in processed:
                    continue
                processed.add(face)
                loops = list(face.loops)
                # Original corners already have authoritative source UV.
                # Preserve them exactly; reconstruct only vertices added by cuts.
                raw_uvs = [region["corner_uvs"].get(tuple(loop.vert.co)) for loop in loops]
                raw_uvs = [raw if raw is not None else chart_uv(region["chart"], loop.vert.co)
                           for raw, loop in zip(raw_uvs, loops)]
                # A bisected vertex can miss the mathematical border by a few
                # millionths because BMesh stores coordinates as float32.
                # Snap only this sub-pixel numerical drift to the exact UV grid.
                raw_uvs = [
                    (
                        snap_uv_to_border(raw[0], UV_SAFE_MIN, UV_USABLE_SPAN),
                        snap_uv_to_border(raw[1], UV_SAFE_MIN, UV_USABLE_SPAN),
                    )
                    for raw in raw_uvs
                ]
                for loop, raw in zip(loops, raw_uvs):
                    loop[uv_layer].uv = raw
                try:
                    tile_u = tile_for_values([v[0] for v in raw_uvs], UV_SAFE_MIN, UV_USABLE_SPAN)
                    tile_v = tile_for_values([v[1] for v in raw_uvs], UV_SAFE_MIN, UV_USABLE_SPAN)
                except ValueError as exc:
                    write_log(f"REGION {region['region_id']} raw UV: {raw_uvs}")
                    raise RuntimeError(f"Откос {region['region_id']}: {exc}") from exc
                offset = Vector((tile_u * UV_USABLE_SPAN, tile_v * UV_USABLE_SPAN))
                for loop in loops:
                    loop[uv_layer].uv -= offset
                values_u = [loop[uv_layer].uv.x for loop in loops]
                values_v = [loop[uv_layer].uv.y for loop in loops]
                if (
                    min(values_u) < UV_SAFE_MIN - EPS
                    or max(values_u) > UV_SAFE_MAX + EPS
                    or min(values_v) < UV_SAFE_MIN - EPS
                    or max(values_v) > UV_SAFE_MAX + EPS
                ):
                    violations += 1
                    write_log(
                        f"VIOLATION face {face.index}: U=[{min(values_u):.6f}, {max(values_u):.6f}], "
                        f"V=[{min(values_v):.6f}, {max(values_v):.6f}]"
                    )
                packed_faces += 1

        if packed_faces > MAX_PARTS:
            raise RuntimeError("Превышен лимит UV-частей; изменения отменены")
        _build_stage('NEIGHBOR_INVARIANTS', f'processed={len(processed)}')
        try:
            compare_non_target_mesh_invariants(
                baseline_non_target,
                non_target_mesh_invariants(bm, uv_layer, result_layer, obj.matrix_world),
            )
        except ValueError as exc:
            raise RuntimeError(f"Защита соседней геометрии: {exc}") from exc
        write_log("NEIGHBOR SURFACE/UV INVARIANTS: PASS")
        if violations or packed_faces < assigned_faces or assigned_faces != len(edge_uv_data):
            raise RuntimeError(
                f"Проверка результата: UV-нарушений {violations}, "
                f"откосов {assigned_faces}, частей {packed_faces}"
            )
        _build_stage('UV_DENSITY_VALIDATION', f'faces={len(processed)}; target=1000px/m')
        invalid = reveal_degenerate_details(processed)
        if invalid:
            raise RuntimeError(
                f"Обнаружены вырожденные откосы после UV-разрезов: {invalid[:8]}; "
                "геометрия отменена. Проверьте слишком острые углы и глубину экструзии")
        # Verify while BMesh handles are still valid, before edit-mesh flush.
        for face in processed:
            try:
                face_uv_density_gate(face, uv_layer, obj.matrix_world, active_resolution)
            except ValueError as exc:
                raise RuntimeError(
                    f"Строгая проверка UV/м у откоса {face.index} не пройдена: {exc}"
                ) from exc
        write_log(f"POST-VALIDATE: {len(processed)} faces; density 1000 px/m PASS")
        # Read-only orientation audit for newly created reveals. Unlike a
        # face-local window UV chart, reveal direction inherits the Host's
        # authored wall UV and cannot safely be guessed from signed area alone.
        from ...core.generation_uv_quality import winding_summary
        _uv_winding = winding_summary([
            [tuple(loop[uv_layer].uv) for loop in face.loops]
            for face in processed
        ])
        write_log('BRICK_HOLE_GENERATION_UV_ORIENTATION '
                  f"positive={_uv_winding['positive']} "
                  f"negative={_uv_winding['negative']} "
                  f"zero={_uv_winding['zero']} "
                  f"status={'REVIEW' if _uv_winding['review_required'] else 'OK'}; "
                  'source_host_uv_preserved=YES; automatic_mirror=NO')
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
        write_log("-" * 72)
        write_log(
            f"RESULT: reveals={assigned_faces}; cuts={cuts_done}; "
            f"uv_parts={packed_faces}; violations={violations}"
        )
        if self.face_region:
            bm = bmesh.from_edit_mesh(obj.data)
            cap_layer = bm.faces.layers.int.get(TEMP_CAP_LAYER_NAME)
            for face in bm.faces:
                face.select_set(False)
            for face in bm.faces:
                if cap_layer is not None and face[cap_layer] == 1:
                    face.select_set(True)
            bm.select_flush(True)
            if cap_layer is not None:
                bm.faces.layers.int.remove(cap_layer)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        write_log("END OF LOG")
        self.report(
            {'INFO'},
            f"Откосов: {assigned_faces}; разрезов: {cuts_done}; UV-частей: {packed_faces}; нарушений: {violations}",
        )
        return {'FINISHED'}


def face_selection_signature(face):
    return tuple(sorted(coordinate_key(vert.co, 7) for vert in face.verts))


def set_unified_work_selection(context, edge_keys=(), face_keys=()):
    """Select a single subset on the transaction WORKING COPY only.

    Exact one-to-one matching rejects coincident topology rather than choosing
    an arbitrary candidate. This function must never be called at preflight.
    """
    obj = context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    edge_map, face_map = {}, {}
    for edge in bm.edges:
        key = frozenset(coordinate_key(v.co) for v in edge.verts)
        edge_map.setdefault(key, []).append(edge)
    for face in bm.faces:
        face_map.setdefault(face_selection_signature(face), []).append(face)
    requested_edges = tuple(edge_keys)
    requested_faces = tuple(face_keys)
    if len(set(requested_edges)) != len(requested_edges) or any(
            len(edge_map.get(key, ())) != 1 for key in requested_edges):
        raise RuntimeError('Исходные рёбра потеряны или неоднозначны — транзакция отменена')
    if len(set(requested_faces)) != len(requested_faces) or any(
            len(face_map.get(key, ())) != 1 for key in requested_faces):
        raise RuntimeError('Исходные полигоны потеряны или неоднозначны — транзакция отменена')
    for f in bm.faces:
        f.select_set(False)
    for e in bm.edges:
        e.select_set(False)
    for v in bm.verts:
        v.select_set(False)
    for key in requested_edges:
        edge_map[key][0].select_set(True)
    for key in requested_faces:
        face_map[key][0].select_set(True)
    bm.select_flush(True)
    # All the changes above are on the temporary mesh inside the transaction.
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


class MESH_OT_brick_hole_extrude_unified(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_extrude_unified'
    bl_label = 'Создать откосы по выбранному типу'
    bl_description = 'Один режим за раз: рёбра ИЛИ грани; много отдельных участков поддерживается'
    bl_options = {'REGISTER', 'UNDO'}
    _brick_unified_mode = True

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    @protected_operation
    def execute(self, context):
        bm = bmesh.from_edit_mesh(context.edit_object.data)
        faces, edges, _ = partition_brick_selection(
            bm, getattr(getattr(context, 'tool_settings', None), 'mesh_select_mode', None))
        if faces and edges:
            raise ValueError('Смешанная экструзия отключена: выберите только рёбра или грани')
        if not faces and not edges:
            raise RuntimeError('Ничего не выделено: выберите рёбра или грани')
        mode = 'FILLED' if faces else 'EDGE'
        record_action('single_domain_dispatch', f'{mode}; faces={len(faces)}; edges={len(edges)}')
        write_log(f'SINGLE DOMAIN START: {mode}; faces={len(faces)}; edges={len(edges)}')
        # The batch operators retain one outer protected transaction, and
        # the native FACE core uses Blender's local normals / Even Offset.
        result = (bpy.ops.mesh.brick_hole_extrude_faces() if faces else
                  bpy.ops.mesh.brick_hole_extrude_all())
        if 'FINISHED' not in result:
            raise RuntimeError(f'{mode}: построение отменено, вся транзакция откатывается')
        write_log(f'SINGLE DOMAIN DONE: {mode}')
        self.report({'INFO'}, f'{mode}: обработано {len(faces) if faces else len(edges)} элементов')
        return {'FINISHED'}


class MESH_OT_brick_hole_preview_unified(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_preview_unified'
    bl_label = 'Проверить выбранное'
    bl_description = 'Проверить только активный режим: рёбра ИЛИ грани; Mesh не изменяется'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        invalidate_preview(scene, 'Проверяется выбранный тип геометрии…')
        depth = scene_signed_depth(scene)
        try:
            summary = unified_preflight(context, depth)
            signature = preview_signature(context, summary['mode'] == 'FILLED')
            if not signature:
                raise RuntimeError('Не удалось закрепить снимок выбранной геометрии')
        except (ValueError,RuntimeError,ReferenceError) as exc:
            decision, message = format_failure(exc)
            scene.brick_hole_preview_text = 'BLOCKED: '+message[:360]
            record_action('unified_preflight_blocked',f'decision={decision}; {str(exc)[:160]}')
            self.report({'ERROR'},message[:245])
            return {'CANCELLED'}
        scene.brick_hole_preview_verified = True
        scene.brick_hole_preview_signature = signature
        scene.brick_hole_preview_face_mode = summary['mode'] == 'FILLED'
        scene.brick_hole_preview_edges = summary['edge_total']
        scene.brick_hole_preview_cuts = summary['cuts']
        advisory = source_uv_warning_message(summary)
        scene.brick_hole_preview_warnings = int(bool(advisory))
        scene.brick_hole_preview_text = (advisory if advisory else
            f'PREFLIGHT {summary["mode"]}: {summary["faces"]} граней, '
            f'{summary["independent_edges"]} рёбер; Mesh не изменён')
        record_action('unified_preflight_pass',scene.brick_hole_preview_text)
        self.report({'WARNING'} if advisory else {'INFO'}, scene.brick_hole_preview_text[:245])
        return {'FINISHED'}


class MESH_OT_brick_hole_preview(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_preview'
    bl_label = 'Проверить перед экструзией'
    bl_description = 'Настоящий read-only preflight выбранного режима; без изменения Mesh'
    bl_options = {'REGISTER'}
    face_mode: BoolProperty(default=False, options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        scene.brick_hole_preview_text = 'Проверяется…'
        scene.brick_hole_preview_warnings = 1
        scene.brick_hole_preview_loops = 0
        scene.brick_hole_preview_edges = 0
        scene.brick_hole_preview_cuts = 0
        scene.brick_hole_preview_verified = False
        scene.brick_hole_preview_signature = ''
        record_action('preview_start', f'face_mode={bool(self.face_mode)}')
        signed_depth = scene_signed_depth(scene)
        try:
            summary = safety_preflight(context, signed_depth, bool(self.face_mode))
            bm = bmesh.from_edit_mesh(context.edit_object.data)
            if self.face_mode:
                faces = [f for f in bm.faces if f.select and not f.hide]
                components = selected_face_components(faces)
                cycles = [cycle for component in components
                          for cycle in split_selected_cycles(boundary_edges_for_faces(component))]
            else:
                selected = [edge for edge in bm.edges if edge.select and not edge.hide]
                cycles = split_selected_cycles(selected)
        except (ValueError, RuntimeError) as exc:
            decision, message = format_failure(exc)
            scene.brick_hole_preview_text = 'BLOCKED: ' + message[:360]
            scene.brick_hole_preview_warnings = 1
            record_action('preview_blocked', f'decision={decision}; {str(exc)}')
            self.report({'ERROR'}, message[:245])
            return {'CANCELLED'}
        scene.brick_hole_preview_loops = len(cycles)
        scene.brick_hole_preview_edges = summary['edges']
        scene.brick_hole_preview_cuts = summary['cuts']
        advisory = source_uv_warning_message(summary)
        scene.brick_hole_preview_warnings = int(bool(advisory))
        # Re-capture after read-only preflight: if selection/settings mutate,
        # the panel no longer claims the old check is current.
        scene.brick_hole_preview_signature = preview_signature(context, bool(self.face_mode))
        scene.brick_hole_preview_face_mode = bool(self.face_mode)
        scene.brick_hole_preview_verified = bool(scene.brick_hole_preview_signature)
        record_action('preview_ok', f'edges={summary["edges"]}; cuts<={summary["cuts"]}')
        mode = 'FILLED' if self.face_mode else 'EDGE'
        scene.brick_hole_preview_text = (advisory if advisory else
                                         'PREFLIGHT OK (' + mode + ') — без построения геометрии')
        self.report({'WARNING'} if advisory else {'INFO'}, scene.brick_hole_preview_text[:245])
        return {'FINISHED'}


class MESH_OT_brick_hole_set_manual_axis(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_set_manual_axis'
    bl_label = 'Задать мировую ось направления'
    bl_description = 'Выбрать мировую ось экструзии для неплоского контура; модель не изменяется'
    bl_options = {'REGISTER', 'UNDO'}
    axis: EnumProperty(items=(('X','X','Мировая X'),('Y','Y','Мировая Y'),('Z','Z','Мировая Z')),default='Y')

    def execute(self, context):
        try:
            axis = str(self.axis)
            if axis not in ('X','Y','Z'):
                raise ValueError('Неизвестная ось направления')
            values = {'X':(1.,0.,0.),'Y':(0.,1.,0.),'Z':(0.,0.,1.)}[axis]
            context.scene.brick_hole_manual_normal = values
            context.scene.brick_hole_direction_source = 'GLOBAL'
            invalidate_qa(context.scene)
            record_action('manual_direction_axis',f'world {axis}')
            self.report({'INFO'},f'Направление по мировой оси {axis}. Внутрь/наружу регулирует знак')
            return {'FINISHED'}
        except (ValueError,AttributeError,ReferenceError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}


class MESH_OT_brick_hole_flip_direction(bpy.types.Operator):
    bl_idname = "mesh.brick_hole_flip_direction"
    bl_label = "Сменить направление"

    def execute(self, context):
        scene = context.scene
        scene.brick_hole_direction = 'OUTWARD' if scene.brick_hole_direction == 'INWARD' else 'INWARD'
        return {'FINISHED'}


class MESH_OT_brick_hole_extrude_all(bpy.types.Operator):
    bl_idname = "mesh.brick_hole_extrude_all"
    bl_label = "Экструдировать все отверстия"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    @protected_operation
    def execute(self, context):
        obj = context.edit_object
        scene = context.scene
        bm = bmesh.from_edit_mesh(obj.data)
        selected_edges = [edge for edge in bm.edges if edge.select and not edge.hide]
        try:
            cycles = split_selected_cycles(selected_edges)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        uv_name = scene.brick_hole_uv_map
        if uv_name:
            uv_item = obj.data.uv_layers.get(uv_name)
            if uv_item is None:
                self.report({'ERROR'}, f"UV-карта не найдена: {uv_name}")
                return {'CANCELLED'}
            obj.data.uv_layers.active = uv_item

        snapshots = []
        for cycle in cycles:
            snapshots.append([
                frozenset((coordinate_key(a.co), coordinate_key(b.co)))
                for _edge, a, b in cycle
            ])

        write_log(f"BATCH START: contours={len(snapshots)}")
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        signed_depth = scene_signed_depth(scene)
        material_index = -1
        if scene.brick_hole_material_mode == 'OVERRIDE':
            material_index = scene.brick_hole_material_index
        active_resolution = int(resolution_value(scene))
        active_tile_size = physical_tile_size(active_resolution)

        context.window_manager.progress_begin(0, len(snapshots))
        try:
            for index, snapshot in enumerate(snapshots, 1):
                scene.brick_hole_progress = int((index - 1) * 100 / max(1, len(snapshots)))
                context.window_manager.progress_update(index - 1)
                if getattr(context, 'workspace', None):
                    context.workspace.status_text_set(
                        text=f"Brick Hole: контур {index}/{len(snapshots)}")
                bm = bmesh.from_edit_mesh(obj.data)
                for vert in bm.verts:
                    vert.select = False
                for edge in bm.edges:
                    edge.select = False
                for face in bm.faces:
                    face.select = False
                bm.select_flush(False)
                edge_map = {}
                for candidate in bm.edges:
                    key = frozenset(coordinate_key(v.co) for v in candidate.verts)
                    edge_map.setdefault(key, []).append(candidate)
                if any(len(edge_map.get(key, [])) != 1 for key in snapshot):
                    raise RuntimeError("Контур потерян или имеет совпадающие рёбра; разделите геометрию")
                edge_map = {
                    frozenset((coordinate_key(edge.verts[0].co), coordinate_key(edge.verts[1].co))): edge
                    for edge in bm.edges
                }
                current_edges = [edge_map.get(key) for key in snapshot]
                if any(edge is None for edge in current_edges):
                    write_log(f"BATCH ERROR contour {index}: original edges not found")
                    self.report({'ERROR'}, f"Контур {index}: исходные рёбра потеряны, пакет отменён")
                    return {'CANCELLED'}
                for edge in current_edges:
                    edge.select_set(True)
                bm.select_flush(True)
                context.tool_settings.mesh_select_mode = (False, True, False)
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                _build_stage('EDGE_COMPONENT', f'{index}/{len(snapshots)}; edges={len(current_edges)}')
                result = bpy.ops.mesh.brick_hole_extrude(
                    depth=signed_depth,
                    tile_size=active_tile_size,
                    texture_resolution=active_resolution,
                    reverse_u=scene.brick_hole_reverse_u,
                    append_log=True,
                    override_material_index=material_index,
                )
                if 'FINISHED' not in result:
                    write_log(f"BATCH ERROR contour {index}: core cancelled")
                    self.report({'ERROR'}, f"Контур {index} не обработан, весь пакет отменён")
                    return {'CANCELLED'}

            bm = bmesh.from_edit_mesh(obj.data)
            original_keys = {key for snapshot in snapshots for key in snapshot}
            for face in bm.faces:
                face.select_set(False)
            for edge in bm.edges:
                edge.select_set(False)
            for vert in bm.verts:
                vert.select_set(False)
            for edge in bm.edges:
                key = frozenset(coordinate_key(v.co) for v in edge.verts)
                if key in original_keys:
                    edge.select_set(True)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            scene.brick_hole_progress = 100
            context.window_manager.progress_update(len(snapshots))
            scene.brick_hole_last_contours = len(snapshots)
            write_log("BATCH END: success")
            self.report({'INFO'}, f"Обработано отверстий: {len(snapshots)}")
            return {'FINISHED'}
        finally:
            context.window_manager.progress_end()
            if getattr(context, 'workspace', None):
                context.workspace.status_text_set(text=None)


# BH-04B.6 one authoritative scene policy, with saved-scene migration.
def brick_hole_uv_policy(scene):
    # Always read the PUBLIC RNA value, not the enum IDProperty encoding.
    return str(getattr(scene,'brick_hole_uv_policy','STRICT'))

def _brick_hole_uv_legacy_conflict(scene):
    # Existing .blend UV preferences may not agree. Never silently choose.
    if bool(scene.get('brick_hole_uv_policy_set',False)):return False
    return (getattr(scene,'brick_hole_edge_uv_mode','STRICT')!='STRICT' or
            getattr(scene,'brick_hole_bridge_uv_mode','STRICT')!='STRICT')

def _brick_hole_uv_policy_updated(scene,context):
    # Explicit user change makes the new enum authoritative for ALL operations.
    scene['brick_hole_uv_policy_set']=True
    invalidate_preview(scene,'UV-стратегия изменена — требуется новая проверка')
    try:record_action('brick_hole_uv_policy_change',brick_hole_uv_policy(scene))
    except (RuntimeError,ReferenceError,AttributeError):pass

def _bh0462_create_clipped_faces(bm, obj, cells, uv_layer, anchors):
    """Single-tile polygons, topologically welded at all new cut points.

    Anchors is a tuple of (world_xyz, BMVert) for ORIGINAL perimeter verts.
    No old faces/loops or source UV are assigned here. Fail closed on tiny
    clipped facets, duplicate faces or unsafe mesh topology.
    """
    from mathutils import Vector
    invert=obj.matrix_world.inverted_safe()
    created=[]
    seen={}
    for xyz,vertex in anchors:
        key=tuple(round(float(c),8) for c in xyz)
        if key in seen and seen[key] is not vertex:
            raise ValueError('SingleTile: неоднозначные авторские вершины с одинаковыми XYZ')
        seen[key]=vertex
    for cell in cells:
        verts=[];uvs=[]
        for xyz,uv in cell.vertices:
            key=tuple(round(float(c),8) for c in xyz)
            vert=seen.get(key)
            if vert is None:
                vert=bm.verts.new(invert @ Vector(xyz));seen[key]=vert
            verts.append(vert);uvs.append(uv)
        if len(set(verts))<3 or len(set(verts))!=len(verts):
            raise ValueError('SingleTile: нулевое ребро или слитая вершина в разрезе')
        try:face=bm.faces.new(tuple(verts))
        except ValueError as exc:
            raise ValueError('SingleTile: повторная или конфликтующая грань') from exc
        for loop,uv in zip(face.loops,uvs):loop[uv_layer].uv=uv
        created.append(face)
    if len(created)!=len(cells):raise RuntimeError('SingleTile: неполный Commit')
    # The UV discontinuity on a periodic cut is INTENTIONAL; mark only its
    # NEW internal mesh edge as a Seam. Never set .seam on authored edges.
    created_set=set(created)
    period=UV_SAFE_MAX-UV_SAFE_MIN
    for face in created:
        for edge in face.edges:
            if len(edge.link_faces)!=2 or not all(f in created_set for f in edge.link_faces):
                continue
            f0,f1=edge.link_faces
            def boundary(face):
                l=next((l for l in face.loops if l.edge is edge),None)
                if l is None:raise RuntimeError('SingleTile: missing UV loop')
                return {l.vert:tuple(l[uv_layer].uv),
                        l.link_loop_next.vert:tuple(l.link_loop_next[uv_layer].uv)}
            uv0,uv1=boundary(f0),boundary(f1)
            delta=[tuple(uv0[v][i]-uv1[v][i] for i in (0,1)) for v in edge.verts]
            if all(abs(v)<1e-6 for d in delta for v in d):continue
            if (any(abs(d) > 1e-6 for d in (delta[0][0],delta[0][1])) and
                    all(abs(delta[0][i]-delta[1][i])<1e-5 for i in (0,1)) and
                    all(abs(d-round(d/period)*period)<1e-5 for d in delta[0])):
                edge.seam=True
            else:
                raise RuntimeError('SingleTile: unexpected UV boundary jump between NEW faces')
    return created


class MESH_OT_brick_hole_edge_smart_uv(bpy.types.Operator):
    """BH-03.1, isolated UV-continuous extension of 1-2 single boundary edges.

    The legacy EDGE operator and its metric tile cutter are NEVER bypassed for
    STRICT mode. This path is restricted to disconnected straight edges and a
    single safe tile, using the existing atomic working-mesh transaction.
    """
    bl_idname = 'mesh.brick_hole_edge_smart_uv'
    bl_label = 'Edge Extend: Smart UV (проверенный ограниченный режим)'
    bl_description = ('Для одного/двух отдельных прямых граничных рёбер. '
                      'CONTINUE наследует UV; ADAPTIVE переходит к 1000. '
                      'Для сложных контуров используйте обычный Edge Extend')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.edit_object is not None and context.edit_object.type == 'MESH'
                and context.mode == 'EDIT_MESH')

    @protected_operation
    def execute(self, context):
        scene=context.scene
        if _brick_hole_uv_legacy_conflict(scene):
            raise ValueError('Старые режимы Edge/Bridge конфликтуют; выберите SMART UV')
        mode=brick_hole_uv_policy(scene)
        if mode=='STRICT':
            result=bpy.ops.mesh.brick_hole_extrude_all()
            if 'FINISHED' not in result:
                raise RuntimeError('STRICT Edge Extend отменён; откат всей операции')
            return {'FINISHED'}
        if mode not in {'CONTINUE','ADAPTIVE'}:
            raise ValueError('Edge Smart UV: неподдержанный режим')
        obj=context.edit_object
        bm=bmesh.from_edit_mesh(obj.data)
        chosen=[e for e in bm.edges if e.select and not e.hide]
        if not 1<=len(chosen)<=2:
            raise ValueError('Edge Smart UV: выберите одно или два независимых прямых ребра')
        if any(len(e.link_faces)!=1 for e in chosen):
            raise ValueError('Edge Smart UV: только граничные рёбра с одной исходной гранью')
        used=set()
        for e in chosen:
            if any(v in used for v in e.verts):
                raise ValueError('Edge Smart UV: связанные рёбра требуют Smart Corner Solver (BH-04)')
            used.update(e.verts)
        uv_name=scene.brick_hole_uv_map
        uv_layer=(bm.loops.layers.uv.get(uv_name) if uv_name else bm.loops.layers.uv.active)
        if uv_layer is None:
            raise ValueError('Edge Smart UV: UV-карта не найдена')
        signed_depth=scene_signed_depth(scene)
        # Read-only established selection frame: no guessed normals or offsets.
        _,directions=edge_local_frame(obj,chosen,scene,signed_depth,validate_shape=True,bm=bm)
        resolution=int(resolution_value(scene))
        linear=obj.matrix_world.to_3x3()
        inverse=linear.inverted()
        from .uv_orientation_policy import outward_from_source_uv
        # Precompute ALL candidates before touching the working BMesh.
        candidates=[]
        for e in chosen:
            host=e.link_faces[0]
            host_loop=next((l for l in host.loops if l.edge is e),None)
            if host_loop is None or host_loop.link_loop_next.vert not in e.verts:
                raise ValueError('Edge Smart UV: ошибка исходного порядка face-loop')
            a,b=host_loop.vert,host_loop.link_loop_next.vert
            lu_a=host_loop
            lu_b=host_loop.link_loop_next
            source0=tuple(lu_a[uv_layer].uv)
            source1=tuple(lu_b[uv_layer].uv)
            uv_out=outward_from_source_uv(
                source0,source1,[tuple(l[uv_layer].uv) for l in host.loops],True)
            wa=tuple(obj.matrix_world @ a.co)
            wb=tuple(obj.matrix_world @ b.co)
            step_a=directions[a]*signed_depth
            step_b=directions[b]*signed_depth
            wc=tuple(wa[k]+step_a[k] for k in range(3))
            wd=tuple(wb[k]+step_b[k] for k in range(3))
            plan=plan_uv_strip(wa,wb,wc,wd,source0,source1,uv_out,
                              resolution=resolution,mode=mode,
                              target_ppm=PIXELS_PER_METER,
                              transition_m=scene.brick_hole_uv_transition_m,
                              tile_min=UV_SAFE_MIN,tile_max=UV_SAFE_MAX)
            # Never silently approximate mitered or sheared edges with a parallelogram.
            if len(plan.rows)>25 or not plan.source_locked:
                raise ValueError('Edge Smart UV: не прошла проверка граничных UV')
            # Blender may invalidate BMFace wrappers across topology edits.
            # Freeze the host's scalar attributes before any bm.verts.new or
            # bm.faces.new; never consult a saved host BMFace during creation.
            host_material_index=int(host.material_index)
            candidates.append((host_material_index,a,b,source0,source1,step_a,step_b,plan))
        result_layer=bm.faces.layers.int.get(RESULT_LAYER_NAME)
        if result_layer is None:
            result_layer=bm.faces.layers.int.new(RESULT_LAYER_NAME)
        policy_layer=bm.faces.layers.int.get('brick_hole_uv_policy')
        if policy_layer is None:
            policy_layer=bm.faces.layers.int.new('brick_hole_uv_policy')
        next_id=max((f[result_layer] for f in bm.faces),default=0)+1
        created=[]
        baseline_uv=[(face,tuple(tuple(l[uv_layer].uv) for l in face.loops))
                     for face in bm.faces]
        for host_material_index,a,b,source0,source1,step_a,step_b,plan in candidates:
            wa=tuple(obj.matrix_world @ a.co)
            wb=tuple(obj.matrix_world @ b.co)
            new_faces=_bh0462_create_clipped_faces(
                bm,obj,plan.tile_cells,uv_layer,((wa,a),(wb,b)))
            for face in new_faces:
                face.material_index=(scene.brick_hole_material_index
                    if scene.brick_hole_material_mode=='OVERRIDE' else host_material_index)
                face[result_layer]=next_id
                face[policy_layer]=1 if mode=='CONTINUE' else 2
                next_id+=1
                created.append(face)
            write_log(f'BH0462 EDGE_SINGLE_TILE: mode={mode}; '
                      f'cells={len(plan.tile_cells)}; repeats={plan.repeat_count}; '
                      f'source_ppm={plan.source_density:.3f}; '
                      f'end_ppm={plan.end_density:.3f}; source UV LOCKED; '
                      'image pixel seam NOT VERIFIED')
        # Independent invariants on working copy, before commit.
        for face,snapshot in baseline_uv:
            if not face.is_valid or tuple(tuple(l[uv_layer].uv) for l in face.loops)!=snapshot:
                raise RuntimeError('Edge Smart UV: авторские UV изменились, откат')
        if not created or len(created)>512:
            raise RuntimeError('Edge Smart UV: некорректное число новых граней')
        invalid=reveal_degenerate_details(created)
        if invalid:
            raise RuntimeError(f'Edge Smart UV: вырожденные новые грани: {invalid[:3]}')
        for f in bm.faces:
            f.select_set(False)
        for f in created:
            f.select_set(True)
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data,loop_triangles=True,destructive=True)
        write_log('BH04B4_BMFACE_SNAPSHOT: Edge Smart UV inherited material indices captured pre-mutation')
        _build_stage('EDGE_SMART_UV',f'mode={mode}; originals={len(candidates)}; '
                     f'new_faces={len(created)}; host_uv_locked=YES; target=1000 only when ADAPTIVE ends')
        self.report({'WARNING'},f'Edge Smart UV: {mode}, граней {len(created)}. '
                    'Preview пока схематичен; авторские UV сохранены.')
        return {'FINISHED'}



class WM_OT_brick_hole_bh031_auto_verify(bpy.types.Operator):
    """Run isolated, actual Blender/BMesh BH03.1 evidence tests and export ZIP."""
    bl_idname = 'wm.brick_hole_bh031_auto_verify'
    bl_label = 'BH-03.1 · AUTO TEST / ZIP'
    bl_description = ('Создать отдельную сцену, проверить реальный EDGE SMART UV '
                      'в Blender и автоматически собрать Evidence ZIP. '
                      'Использовать только Object Mode без активного модального инструмента.')
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def execute(self, context):
        try:
            from .bh031_native_lab import run_lab
            path, result = run_lab(context)
            context.window_manager.clipboard = str(path)
            passed, failed = result['passed'], result['failed']
            severity = {'WARNING'} if failed else {'INFO'}
            self.report(severity,
                        f'BH-03.1 Native: PASS={passed}; FAIL={failed}. ZIP: {path}')
            return {'FINISHED'}
        except Exception as exc:
            write_log('BH-03.1 AUTO VERIFY ERROR: '+traceback.format_exc())
            self.report({'ERROR'}, 'BH-03.1 AUTO VERIFY: '+str(exc)[:180])
            return {'CANCELLED'}


class MESH_OT_brick_hole_confirm_uv_policy(bpy.types.Operator):
    """Explicitly authorize the NEW common policy for old .blend files."""
    bl_idname='mesh.brick_hole_confirm_uv_policy'
    bl_label='Подтвердить единый UV-режим'
    bl_options={'REGISTER'}
    def execute(self,context):
        scene=context.scene
        value=brick_hole_uv_policy(scene)
        if value not in {'STRICT','CONTINUE','ADAPTIVE'}:
            self.report({'ERROR'},'Неверный UV режим');return {'CANCELLED'}
        scene['brick_hole_uv_policy_set']=True
        invalidate_preview(scene,'UV-стратегия подтверждена; повторите Preflight')
        record_action('brick_hole_uv_policy_confirmed',value)
        self.report({'INFO'},'Единая UV стратегия: '+value)
        return {'FINISHED'}

class MESH_OT_brick_hole_uv_policy_apply(bpy.types.Operator):
    """Single Apply dispatcher: unsupported modes refuse, NEVER fake STRICT."""
    bl_idname='mesh.brick_hole_uv_policy_apply'
    bl_label='Brick Hole · SMART UV Apply'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        return context.mode=='EDIT_MESH' and context.edit_object is not None and context.edit_object.type=='MESH'

    def execute(self,context):
        from .uv_policy_contract import support_decision
        scene=context.scene
        if _brick_hole_uv_legacy_conflict(scene):
            self.report({'ERROR'},'Старые Edge/Bridge UV-режимы различаются. Явно выберите новый SMART UV')
            return {'CANCELLED'}
        policy=brick_hole_uv_policy(scene)
        bm=bmesh.from_edit_mesh(context.edit_object.data)
        faces,edges,_=partition_brick_selection(bm,context.tool_settings.mesh_select_mode)
        shape='FACE' if faces else 'EDGE'
        selected=faces if faces else edges
        connected=bool(edges and len({v for e in edges for v in e.verts})!=2*len(edges))
        boundary=bool(edges and all(len(e.link_faces)==1 for e in edges))
        decision=support_decision(policy,shape,len(selected),connected,boundary)
        if not decision['ok']:
            reason=decision['reason']
            if shape=='EDGE' and policy in {'ADAPTIVE','CONTINUE'}:
                # Distinguish a policy/solver coverage limitation from a
                # collision, a corrupt mesh, or a broken Blender operator.
                # This log is an evidence witness, not permission to silently
                # fall back to STRICT (which has different UV semantics).
                chains=0
                try:
                    chains=len(split_selected_cycles(edges))
                except (ValueError,RuntimeError,ReferenceError):
                    pass
                reason += (f' [выделено {len(edges)} рёбер, групп {chains}; '
                           'сложный контур: STRICT + S (SAFE DEPTH) '
                           'либо отдельные прямые рёбра]')
            record_action('uv_policy_refused',reason)
            self.report({'ERROR'},reason[:230]);return {'CANCELLED'}
        try:
            result=(bpy.ops.mesh.brick_hole_extrude_unified() if policy=='STRICT'
                    else bpy.ops.mesh.brick_hole_edge_smart_uv())
            if 'FINISHED' not in result:
                raise RuntimeError('Производственный Apply отказал; см. Brick Hole Diagnostics')
            record_action('uv_policy_committed',
                f'{policy}; {shape}; count={len(selected)}; collision_guard='+
                ('ON' if collision_guard_enabled(scene) else 'OFF · NOT_CHECKED'))
            return {'FINISHED'}
        except (ValueError,RuntimeError,ReferenceError) as exc:
            write_log('BH046 APPLY ERROR: '+traceback.format_exc())
            self.report({'ERROR'},str(exc)[:230]);return {'CANCELLED'}

class MESH_OT_brick_hole_bridge_cap(bpy.types.Operator):
    """BH-02 strict single-quad bridge. NOT a general corner/path solver.

    This action deliberately refuses the top and bottom edges of an existing
    wall face: joining them would duplicate the wall. Use EDGE EXTEND instead.
    """
    bl_idname = 'mesh.brick_hole_bridge_cap'
    bl_label = 'Bridge Cap: соединить два граничных ребра'
    bl_description = ('Соединить два граничных ребра. STRICT сохраняет старые UV 1000 px/м; '
                      'CONTINUE сохраняет UV выбранного края; ADAPTIVE добавляет новые ряды '
                      'для перехода к 1000. Второй стык может быть видим.')
    bl_options = {'REGISTER', 'UNDO'}
    _brick_bridge_mode = True

    @classmethod
    def poll(cls, context):
        return (context.edit_object is not None and context.edit_object.type == 'MESH'
                and context.mode == 'EDIT_MESH')

    @protected_operation
    def execute(self, context):
        obj=context.edit_object
        bm=bmesh.from_edit_mesh(obj.data)
        edges=[e for e in bm.edges if e.select and not e.hide]
        if len(edges)!=2:
            raise ValueError('Bridge Cap: выберите ровно два отдельных граничных ребра')
        if set(edges[0].verts) & set(edges[1].verts):
            raise ValueError('Bridge Cap: рёбра соединены общей вершиной; нужен Edge Extend')
        if any(len(e.link_faces)!=1 for e in edges):
            raise ValueError('Bridge Cap: оба ребра должны иметь ровно одну примыкающую грань')
        if set(edges[0].link_faces) & set(edges[1].link_faces):
            raise ValueError('Bridge Cap: между рёбрами уже есть грань стены; используйте EDGE EXTEND')
        uv_name=context.scene.brick_hole_uv_map
        uv_layer=(bm.loops.layers.uv.get(uv_name) if uv_name else bm.loops.layers.uv.active)
        if uv_layer is None:
            raise ValueError('Bridge Cap: UV-карта отсутствует')
        # Stable orientation from source face boundary loop, not BMesh hash order.
        a_edge,b_edge=edges
        face_a=a_edge.link_faces[0]
        # Freeze source scalar before new BMesh topology invalidates wrappers.
        source_bridge_material=int(face_a.material_index)
        face_loop=next((loop for loop in face_a.loops if loop.edge is a_edge),None)
        if face_loop is None:
            raise RuntimeError('Bridge Cap: исходное ребро не найдено в loop')
        a0=face_loop.vert
        a1=face_loop.link_loop_next.vert
        if a1 not in a_edge.verts:
            raise RuntimeError('Bridge Cap: исходная ориентация ребра нарушена')
        b0,b1=b_edge.verts
        resolution=int(resolution_value(context.scene))
        result=plan_single_bridge(
            tuple(obj.matrix_world @ a0.co),tuple(obj.matrix_world @ a1.co),
            tuple(obj.matrix_world @ b0.co),tuple(obj.matrix_world @ b1.co),
            resolution=resolution,ppm=PIXELS_PER_METER,
            safe_fraction=UV_USABLE_SPAN,
        )
        if result.reversed_b:
            b0,b1=b1,b0
        # Protect against already connected geometry and degeneracy.
        if len({a0,a1,b0,b1})!=4:
            raise ValueError('Bridge Cap: для Quad требуются четыре различные вершины')
        if bm.faces.get((a0,a1,b1,b0)) is not None:
            raise ValueError('Bridge Cap: такая грань уже существует')
        # Verify no bridge-side edge becomes non-manifold after insertion.
        for v0,v1 in ((a1,b1),(b0,a0)):
            side=bm.edges.get((v0,v1))
            if side is not None and len(side.link_faces)>=2:
                raise ValueError('Bridge Cap: боковая связь станет неманифолдной')
        # BH-03 intentionally does not alter the existing EDGE extrusion UV
        # pipeline: that pipeline contains metric tile-cut/triangle validators.
        # Only strict BH-02 Bridge Cap or this isolated new strip is supported.
        if _brick_hole_uv_legacy_conflict(context.scene):
            raise ValueError('Старые режимы Edge/Bridge конфликтуют; выберите SMART UV')
        mode=brick_hole_uv_policy(context.scene)
        new_faces=[]
        far_seam=None
        src_loop_a=next((l for l in face_a.loops if l.vert is a0),None)
        src_loop_b=next((l for l in face_a.loops if l.vert is a1),None)
        if src_loop_a is None or src_loop_b is None:
            raise ValueError('Smart UV: отсутствует исходный face-loop')
        authored_uv0=tuple(src_loop_a[uv_layer].uv)
        authored_uv1=tuple(src_loop_b[uv_layer].uv)
        if mode=='STRICT':
            face=bm.faces.new((a1,a0,b0,b1))
            uv_by_vert={v:uv for v,uv in zip((a0,a1,b1,b0),result.uv)}
            for loop in face.loops:
                loop[uv_layer].uv=uv_by_vert[loop.vert]
            new_faces.append(face)
            write_log('BH-03 BRIDGE: STRICT; original source loops unchanged; '
                      'target=1000 px/m; source seam may be visible')
        else:
            from .uv_orientation_policy import outward_from_source_uv
            forward=(src_loop_a.link_loop_next.vert is a1)
            backward=(src_loop_b.link_loop_next.vert is a0)
            if forward==backward:
                raise ValueError('Smart UV: неоднозначный порядок исходного UV-ребра')
            outward=outward_from_source_uv(
                authored_uv0,authored_uv1,
                [tuple(l[uv_layer].uv) for l in face_a.loops],forward)
            face_b=b_edge.link_faces[0]
            far_loops=[next((l for l in face_b.loops if l.vert is v),None)
                       for v in (b0,b1)]
            far_uv=(tuple(far_loops[0][uv_layer].uv),
                    tuple(far_loops[1][uv_layer].uv)) if all(far_loops) else None
            plan=plan_uv_strip(
                tuple(obj.matrix_world @ a0.co),tuple(obj.matrix_world @ a1.co),
                tuple(obj.matrix_world @ b0.co),tuple(obj.matrix_world @ b1.co),
                authored_uv0,authored_uv1,outward,
                resolution=resolution,mode=mode,
                target_ppm=PIXELS_PER_METER,
                transition_m=context.scene.brick_hole_uv_transition_m,
                far_uv=far_uv,tile_min=UV_SAFE_MIN,tile_max=UV_SAFE_MAX)
            far_seam=plan.far_boundary_seam_px
            # UV polygons are clipped to one local tile without changing source
            # edge coordinates or UV. Reuse all four existing boundary verts.
            anchors=tuple((tuple(obj.matrix_world @ v.co),v)
                           for v in (a0,a1,b0,b1))
            new_faces.extend(_bh0462_create_clipped_faces(
                bm,obj,plan.tile_cells,uv_layer,anchors))
            write_log(f'BH0462 BRIDGE_SINGLE_TILE: {mode}; source_uv=LOCKED; '
                      f'source_ppm={plan.source_density:.3f}; '
                      f'end_ppm={plan.end_density:.3f}; '
                      f'cells={len(plan.tile_cells)}; repeats={plan.repeat_count}; '
                      f'far_seam_px={far_seam}; image pixel seam NOT VERIFIED')
        layer=bm.faces.layers.int.get(RESULT_LAYER_NAME)
        if layer is None:
            layer=bm.faces.layers.int.new(RESULT_LAYER_NAME)
        region_id=max((f[layer] for f in bm.faces if f not in new_faces),default=0)+1
        uv_policy_layer=bm.faces.layers.int.get('brick_hole_uv_policy')
        if uv_policy_layer is None:
            uv_policy_layer=bm.faces.layers.int.new('brick_hole_uv_policy')
        uv_policy_code={'STRICT':0,'CONTINUE':1,'ADAPTIVE':2}[mode]
        for index,face in enumerate(new_faces):
            face.material_index=source_bridge_material
            face[layer]=region_id+index
            face[uv_policy_layer]=uv_policy_code
            face.select_set(True)
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=True)
        _build_stage('BRIDGE_CAP',f'faces={len(new_faces)}; area_m2={result.area_m2:.9g}; '
                     f'uv_mode={mode}; source_uv_unchanged=YES; far_seam_px={far_seam}')
        warning=('; UV-шов на второй границе возможен' if mode!='STRICT' else '; шов возможен')
        self.report({'WARNING'} if far_seam is not None and far_seam>0.5 else {'INFO'},
                    f'Bridge: {mode}, новых граней {len(new_faces)}{warning}')
        return {'FINISHED'}


class MESH_OT_brick_hole_extrude_faces(bpy.types.Operator):
    bl_idname = "mesh.brick_hole_extrude_faces"
    bl_label = "Создать откосы из полигонов"
    bl_description = (
        "Переместить выбранные полигоны на глубину и создать откосы "
        "только по внешней границе"
    )
    bl_options = {'REGISTER', 'UNDO'}
    _brick_face_mode = True

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    @protected_operation
    def execute(self, context):
        obj = context.edit_object
        scene = context.scene
        bm = bmesh.from_edit_mesh(obj.data)
        selected_faces = [face for face in bm.faces if face.select and not face.hide]
        components = selected_face_components(selected_faces)
        if not components:
            self.report({'ERROR'}, "Выделите полигоны, заполняющие проём")
            return {'CANCELLED'}

        # For bent FILLED regions validate consistent shared-edge winding
        # before the native transaction. Never assume all normals are equal.
        # The post-build UV density and geometry checks remain mandatory.
        for component in components:
            selected_region_world_normal(obj, component, require_consistent_sign=True)

        uv_name = scene.brick_hole_uv_map
        if uv_name:
            uv_item = obj.data.uv_layers.get(uv_name)
            if uv_item is None:
                self.report({'ERROR'}, f"UV-карта не найдена: {uv_name}")
                return {'CANCELLED'}
            obj.data.uv_layers.active = uv_item

        def signature(face):
            return tuple(sorted(coordinate_key(vert.co, 7) for vert in face.verts))

        snapshots = [[signature(face) for face in component] for component in components]
        signed_depth = scene_signed_depth(scene)
        material_index = (
            scene.brick_hole_material_index
            if scene.brick_hole_material_mode == 'OVERRIDE' else -1
        )
        active_resolution = int(resolution_value(scene))
        cap_signatures = set()
        write_log(
            f"FILLED REGION BATCH START: regions={len(snapshots)}; "
            f"source faces={len(selected_faces)}"
        )
        context.window_manager.progress_begin(0, len(snapshots))
        try:

            for index, snapshot in enumerate(snapshots, 1):
                bm = bmesh.from_edit_mesh(obj.data)
                for face in bm.faces:
                    face.select_set(False)
                for edge in bm.edges:
                    edge.select_set(False)
                for vert in bm.verts:
                    vert.select_set(False)
                face_map = {}
                for face in bm.faces:
                    face_map.setdefault(signature(face), []).append(face)
                if any(len(face_map.get(key, [])) != 1 for key in snapshot):
                    raise RuntimeError(
                        f"Участок {index}: исходные полигоны потеряны или совпадают"
                    )
                current_faces = [face_map[key][0] for key in snapshot]
                for face in current_faces:
                    face.select_set(True)
                bm.select_flush(True)
                context.tool_settings.mesh_select_mode = (False, False, True)
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                _build_stage('FILLED_COMPONENT', f'{index}/{len(snapshots)}; faces={len(current_faces)}')

                scene.brick_hole_progress = int((index - 1) * 100 / len(snapshots))
                context.window_manager.progress_update(index - 1)
                if getattr(context, 'workspace', None):
                    context.workspace.status_text_set(
                        text=f"Brick Hole: заполненный участок {index}/{len(snapshots)}")
                result = bpy.ops.mesh.brick_hole_extrude(
                    depth=signed_depth,
                    texture_resolution=active_resolution,
                    reverse_u=scene.brick_hole_reverse_u,
                    append_log=True,
                    override_material_index=material_index,
                    face_region=True,
                )
                if 'FINISHED' not in result:
                    raise RuntimeError(f"Участок {index} не обработан")
                bm = bmesh.from_edit_mesh(obj.data)
                cap_signatures.update(signature(face) for face in bm.faces if face.select)

            bm = bmesh.from_edit_mesh(obj.data)
            for face in bm.faces:
                face.select_set(False)
            for edge in bm.edges:
                edge.select_set(False)
            for vert in bm.verts:
                vert.select_set(False)
            for face in bm.faces:
                if signature(face) in cap_signatures:
                    face.select_set(True)
            bm.select_flush(True)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            scene.brick_hole_progress = 100
            scene.brick_hole_last_contours = len(snapshots)
            context.window_manager.progress_update(len(snapshots))
            write_log("FILLED REGION BATCH END: success")
            self.report(
                {'INFO'},
                f"Обработано заполненных участков: {len(snapshots)}; окно сохранено",
            )
            return {'FINISHED'}
        finally:
            context.window_manager.progress_end()
            if getattr(context, 'workspace', None):
                context.workspace.status_text_set(text=None)


class WM_OT_brick_hole_copy_log(bpy.types.Operator):
    bl_idname = "wm.brick_hole_copy_log"
    bl_label = "Копировать лог"
    bl_description = "Копировать полный журнал последнего запуска в буфер обмена"

    def execute(self, context):
        text = bpy.data.texts.get(LOG_TEXT_NAME)
        if text is None or not text.as_string().strip():
            self.report({'ERROR'}, "Лог пока пуст — сначала запустите экструзию")
            return {'CANCELLED'}
        context.window_manager.clipboard = text.as_string()
        self.report({'INFO'}, "Лог скопирован — вставьте его в сообщение")
        return {'FINISHED'}


def collect_validation_errors(obj, bm, uv_layer, result_layer, resolution):
    """Read-only QA pass. No face flags, selection or mode mutation."""
    errors = []
    checked = 0
    # BH-03: a user-selected CONTINUE/ADAPTIVE bridge explicitly relaxes
    # exact 1000 px/m. Its density is recorded in the build log and HUD;
    # treating this conscious deviation as a failed STRICT metric check would
    # give misleading validation results. Bounds, areas and manifold rules
    # remain mandatory in every mode.
    uv_policy_layer=bm.faces.layers.int.get('brick_hole_uv_policy')
    for face in bm.faces:
        if face[result_layer] == 0:
            continue
        checked += 1
        reasons = []
        u_values = [loop[uv_layer].uv.x for loop in face.loops]
        v_values = [loop[uv_layer].uv.y for loop in face.loops]
        if (
            not u_values or not v_values or
            not all(math.isfinite(v) for v in u_values + v_values) or
            min(u_values) < UV_SAFE_MIN - EPS or max(u_values) > UV_SAFE_MAX + EPS or
            min(v_values) < UV_SAFE_MIN - EPS or max(v_values) > UV_SAFE_MAX + EPS
        ):
            reasons.append('UV вышел за рабочую область')
        if face.calc_area() <= 1.0e-10:
            reasons.append('вырожденный полигон')
        if any(len(edge.link_faces) > 2 for edge in face.edges):
            reasons.append('non-manifold ребро')
        for loop in face.loops:
            next_loop = loop.link_loop_next
            length_m = ((obj.matrix_world @ next_loop.vert.co) -
                        (obj.matrix_world @ loop.vert.co)).length
            uv_length = (next_loop[uv_layer].uv - loop[uv_layer].uv).length
            if length_m > EPS:
                density = uv_length * resolution / length_m
                policy=(face[uv_policy_layer] if uv_policy_layer is not None else 0)
                if not math.isfinite(density):
                    reasons.append('UV: некорректная плотность (NaN/Inf)')
                    break
                if policy==0 and abs(density - PIXELS_PER_METER) > 25.0:
                    reasons.append(f'тексель {density:.1f} px/м')
                    break
        if reasons:
            errors.append((face, reasons))
    return checked, errors


def current_validation(context):
    obj = context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    uv_name = context.scene.brick_hole_uv_map
    uv_layer = bm.loops.layers.uv.get(uv_name) if uv_name else bm.loops.layers.uv.active
    result_layer = bm.faces.layers.int.get(RESULT_LAYER_NAME)
    if uv_layer is None or result_layer is None:
        raise ValueError('Нет результата Brick Hole или выбранной UV-карты')
    checked, errors = collect_validation_errors(
        obj, bm, uv_layer, result_layer, resolution_value(context.scene),
    )
    if checked == 0:
        raise ValueError('На этом объекте нет полигонов результата Brick Hole')
    return bm, checked, errors


class MESH_OT_brick_hole_validate(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_validate'
    bl_label = 'Проверить результат (без изменения выделения)'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        try:
            _bm, checked, errors = current_validation(context)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for face, reasons in errors:
            write_log(f'VALIDATION face {face.index}: ' + '; '.join(reasons))
        context.scene.brick_hole_last_checked = checked
        context.scene.brick_hole_last_errors = len(errors)
        context.scene.brick_hole_qa_state = 'Проверено: ' + context.edit_object.name
        context.scene.brick_hole_issue_cursor = 0
        context.scene.brick_hole_last_error_summary = (
            f'Грань {errors[0][0].index}: {"; ".join(errors[0][1])}' if errors
            else 'Ошибок в последней проверке не найдено')
        record_action('validate', f'checked={checked}; errors={len(errors)}')
        self.report(
            {'WARNING'} if errors else {'INFO'},
            f'Проверено: {checked}; проблемных полигонов: {len(errors)}; выделение не изменено',
        )
        return {'FINISHED'}


class MESH_OT_brick_hole_select_errors(bpy.types.Operator):
    bl_idname = 'mesh.brick_hole_select_errors'
    bl_label = 'Выделить ошибки (меняет выделение)'
    bl_description = 'Явно заменить Mesh-выделение ошибочными гранями; выполняет свежую проверку'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.edit_object is not None and context.edit_object.type == 'MESH'

    def execute(self, context):
        try:
            bm, checked, errors = current_validation(context)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        # Validate first; make a destructive selection change only on explicit click.
        for face in bm.faces:
            face.select_set(False)
        for edge in bm.edges:
            edge.select_set(False)
        for vert in bm.verts:
            vert.select_set(False)
        for face, _reasons in errors:
            face.select_set(True)
        bm.select_flush(bool(errors))
        context.tool_settings.mesh_select_mode = (False, False, True)
        bmesh.update_edit_mesh(context.edit_object.data,
                               loop_triangles=False, destructive=False)
        context.scene.brick_hole_last_checked = checked
        context.scene.brick_hole_last_errors = len(errors)
        context.scene.brick_hole_qa_state = 'Проверено: ' + context.edit_object.name
        record_action('select_errors', f'changed selection; errors={len(errors)}')
        self.report({'INFO'}, f'Выделено ошибочных полигонов: {len(errors)}')
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Pass 7: read-only EDGE + FILLED viewport preview and modal edit layer.
# The geometric operator runs ONLY after an explicit APPLY action.
# -----------------------------------------------------------------------------
_INTERACTIVE_SESSION = None
_INTERACTIVE_REGISTRY_KEY = 'ISSMAKER_BRICK_HOLE_INTERACTIVE_V105'
# BH0467: shared operator/preflight budget. No independent Preview=300 gate.
_MAX_INTERACTIVE_EDGES = MAX_SELECTED_EDGES
_MAX_INTERACTIVE_FACES = 2500
_MAX_INTERACTIVE_FACE_CORNERS = 16000


def interactive_signed_depth(magnitude, direction):
    """Signed displacement in metres. Scene values are unchanged in preview."""
    value = abs(float(magnitude))
    if not math.isfinite(value) or value <= EPS:
        raise ValueError('Глубина должна быть конечной и больше нуля')
    return -value if direction == 'INWARD' else value


def scene_signed_depth(scene):
    """One signed depth contract for sidebar preview, Build and Viewport HUD.

    The Blender property is allowed to contain a negative number; this reverses
    its separate legacy INWARD/OUTWARD direction rather than discarding sign.
    Viewport commit stores a positive magnitude plus the appropriate direction
    so existing .blend values and modal rendering remain compatible.
    """
    depth=float(scene.brick_hole_depth)
    if not math.isfinite(depth) or abs(depth)<=EPS:
        raise ValueError('Нулевая либо нечисловая глубина Brick Hole')
    direction=getattr(scene,'brick_hole_direction','OUTWARD')
    return -depth if direction=='INWARD' else depth


def interactive_mouse_delta(start, now, axis_pixels_per_metre, fallback=0.005):
    """Project a drag onto the screen-space normal; fall back at edge-on views."""
    dx, dy = float(now[0]) - float(start[0]), float(now[1]) - float(start[1])
    ax, ay = (float(v) for v in axis_pixels_per_metre)
    squared = ax * ax + ay * ay
    if squared < 24.0 * 24.0:
        return dx * fallback
    return (dx * ax + dy * ay) / squared


def interactive_numeric_value(text):
    """Parse an exact metre depth; optional mm/cm/m, decimal comma or dot.

    The numeric edit is not applied to Mesh or Scene until user confirmation.
    Reject partial input and malformed unit expressions instead of applying a
    previously valid value by accident. Pure function for offline testing.
    """
    import re
    if not isinstance(text, str):
        raise ValueError('Введите глубину числом, например 0,250 м')
    match = re.fullmatch(r'\s*([+-]?)(?:([0-9]+(?:[.,][0-9]+)?)|([.,][0-9]+))\s*(mm|cm|m|мм|см|м)?\s*', text.strip().replace('−','-'), flags=re.I)
    if match is None:
        raise ValueError('Введите число, например −0,250, +25 cm или −250 mm')
    number = float((match.group(2) or match.group(3)).replace(',', '.'))
    if match.group(1) == '-':number = -number
    unit = (match.group(4) or 'm').lower()
    scale = {'mm':.001,'мм':.001,'cm':.01,'см':.01,'m':1.,'м':1.}[unit]
    result = number * scale
    if not math.isfinite(result) or not 0.001 <= abs(result) <= 50.0:
        raise ValueError('Допустимая глубина: от −50 до −0,001 м или от +0,001 до +50 м')
    return result



def preview_planarity(normals, positions, tolerance_m=1.e-5, normal_cosine=.999999):
    """Fail-closed: only an oriented, coplanar FILLED component gets exact
    displacement geometry. Blender Even Offset is a uniform normal offset
    for coplanar faces. Curved components get an explicitly schematic guide.

    Pure numerical function: never touches a live BMesh or Blender operator.
    """
    if not normals or len(positions) < 3:
        return False
    try:
        ns = [tuple(float(c) for c in n) for n in normals]
        pts = [tuple(float(c) for c in p) for p in positions]
        if any(len(v) != 3 or not all(math.isfinite(c) for c in v)
               for v in ns + pts):
            return False
        def norm(v):
            magnitude = math.sqrt(sum(c*c for c in v))
            return tuple(c/magnitude for c in v) if magnitude > 1.e-12 else None
        reference = norm(ns[0])
        # Do not classify coincident/collinear 'polygons' as precise.
        if reference is not None:
            origin=pts[0]
            spread=max(sum((p[i]-origin[i])**2 for i in range(3)) for p in pts)
            if spread <= 1.e-18:
                return False
            # O(N): furthest point from the origin yields a robust axis.
            # Pairing all points would make Preview O(N²) on a large selection.
            axis=max(pts,key=lambda point:sum((point[i]-origin[i])**2 for i in range(3)))
            delta=tuple(axis[i]-origin[i] for i in range(3))
            area=max(math.sqrt(sum((
                delta[(i+1)%3]*(p[(i+2)%3]-origin[(i+2)%3])-
                delta[(i+2)%3]*(p[(i+1)%3]-origin[(i+1)%3])
            )**2 for i in range(3))) for p in pts)
            if area <= spread*1.e-10:
                return False
        if reference is None or any((norm(n) is None or
              sum(a*b for a,b in zip(reference,norm(n))) < normal_cosine)
              for n in ns):
            return False
        origin = pts[0]
        return all(abs(sum(reference[i]*(p[i]-origin[i]) for i in range(3))) <=
                   tolerance_m for p in pts)
    except (TypeError,ValueError,OverflowError):
        return False


def interactive_hud_layout(width, height, anchor=None, *, scale=1.0,
                           collapsed=False, view_open=False, options_open=False):
    """DWOS-style sectioned bottom control with shared draw/hit rectangles.

    Logical controls are downscaled together for narrow viewports. The Preview
    menu is an overlay, not a geometry modifier. In compact view only Expand
    remains clickable. No hit rectangle may escape the WINDOW region.
    """
    from ...core.ui_layout_scaling import sanitized
    scale=sanitized(scale)
    width,height=float(width)/scale,float(height)/scale
    if not (math.isfinite(width) and math.isfinite(height)) or width<335 or height<215:
        return None
    w=min(568.,width-24.)
    left=(width-w)*.5
    bottom=14.
    if collapsed:
        rect={'panel':(left,bottom,left+w,bottom+36.),
              'expand':(left+w-110.,bottom+5.,left+w-9.,bottom+31.),
              'title':(left+14.,bottom+12.)}
        return {k:tuple(v*scale for v in values) for k,values in rect.items()}
    rect={
        'panel':(left,bottom,left+w,bottom+264.),
        'accent_strip':(left+3.,bottom+260.,left+w-3.,bottom+263.),
        'mode_back':(left+7.,bottom+115.,left+w-7.,bottom+150.),
        'axis_back':(left+7.,bottom+79.,left+w-7.,bottom+113.),
        'numeric_back':(left+7.,bottom+42.,left+w-7.,bottom+77.),
        'apply':(left+9.,bottom+8.,left+173.,bottom+36.),
        'cancel':(left+181.,bottom+319.,left+181.,bottom+319.), # replaced below
        'title':(left+14.,bottom+201.),
        'axis_caption':(left+15.,bottom+91.),
        'value':(left+19.,bottom+54.),
        'value_hit':(left+12.,bottom+46.,left+265.,bottom+73.),
        'depth_minus':(left+272.,bottom+46.,left+301.,bottom+73.),
        'depth_plus':(left+306.,bottom+46.,left+335.,bottom+73.),
        'vertex_snap':(left+344.,bottom+46.,left+556.,bottom+73.),
        'apply_text':(left+25.,bottom+17.),
        'cancel_text':(left+194.,bottom+17.),
        'warning':(left+12.,bottom+271.),
        'gpu_error':(left+12.,bottom+289.),
        'collision_guard':(left+12.,bottom+230.,left+556.,bottom+256.),
        'collapse':(left+w-38.,bottom+197.,left+w-10.,bottom+218.),
        'view':(left+w-123.,bottom+197.,left+w-45.,bottom+218.),
        'uv_back':(left+7.,bottom+153.,left+w-7.,bottom+189.),
        'uv_strict':(left+12.,bottom+158.,left+174.,bottom+184.),
        'uv_adaptive':(left+182.,bottom+158.,left+365.,bottom+184.),
        'uv_continue':(left+373.,bottom+158.,left+556.,bottom+184.),
        'mode_auto':(left+12.,bottom+119.,left+100.,bottom+145.),
        'mode_face':(left+108.,bottom+119.,left+237.,bottom+145.),
        'mode_global':(left+245.,bottom+119.,left+345.,bottom+145.),
        'options_find':(left+353.,bottom+119.,left+556.,bottom+145.),
        'axis_x':(left+168.,bottom+83.,left+238.,bottom+109.),
        'axis_y':(left+245.,bottom+83.,left+315.,bottom+109.),
        'axis_z':(left+322.,bottom+83.,left+392.,bottom+109.),
    }
    rect['cancel']=(left+181.,bottom+8.,left+344.,bottom+36.)
    # Direction Alternatives: same hitboxes for draw and click. This flyout
    # can overlay the panel in a very short viewport; background hit-tests
    # must be suppressed while open.
    if options_open:
        start_y=bottom+187. if height>=450. else bottom+22.
        rect['options_menu']=(left+9.,start_y,left+w-9.,start_y+157.)
        for i in range(8):
            column=i%2; line=i//2
            cell_w=(w-30.)/2.
            x=left+14.+column*(cell_w+3.)
            y=start_y+111.-line*34.
            rect['option_'+str(i)]=(x,y,x+cell_w-4.,y+30.)
    if view_open:
        # Small WINDOW: the menu overlays other controls, and the event handler
        # consumes hits on its panel before underlying widgets.
        drop_bottom=bottom+190. if height>=350. else bottom+20.
        rect['view_menu']=(left+w-225.,drop_bottom,left+w-9.,drop_bottom+137.)
        for idx,key in enumerate(('source','wire','fill','handles','labels')):
            y=drop_bottom+110.-idx*25.
            rect['view_'+key]=(left+w-218.,y,left+w-16.,y+22.)
    if w<568.:
        ratio=w/568.
        for key,bounds in tuple(rect.items()):
            if key=='panel':continue
            if len(bounds)==4:
                rect[key]=(left+(bounds[0]-left)*ratio,bounds[1],
                           left+(bounds[2]-left)*ratio,bounds[3])
            else:
                rect[key]=(left+(bounds[0]-left)*ratio,bounds[1])
    # Entire clickable layout fits small VIEW_3D windows, never negative rects.
    vertical_ratio=min(1., max(.60,(height-30.)/265.))
    if vertical_ratio<1.:
        for key,bounds in tuple(rect.items()):
            if len(bounds)==4:
                rect[key]=(bounds[0],bottom+(bounds[1]-bottom)*vertical_ratio,
                           bounds[2],bottom+(bounds[3]-bottom)*vertical_ratio)
            else:
                rect[key]=(bounds[0],bottom+(bounds[1]-bottom)*vertical_ratio)
    return {key:tuple(v*scale for v in vals) for key,vals in rect.items()}


def interactive_preview_primitives(groups, signed_depth, edge_local_offsets=None):
    """Read-only snapshot to simple GPU primitives. Exact planar filled
    quads are triangulated; curved Filled never masquerades as an exact cap.
    Output: source edges, destination/connector lines, transparent side tris.
    """
    source, wire, triangles = [], [], []
    signed_depth = checked_depth(signed_depth)
    for edges, normal, kind in groups:
        n = checked_direction(normal)
        delta = tuple(signed_depth*x for x in n)
        for a,b in edges:
            a,b = checked_edge(a,b)
            # An amber source, cyan offset wire and translucent side shape
            # are ONLY an approximate *guide*. Native Blender Even Offset uses
            # per-vertex topology and must never be called 'exact' here.
            if kind == 'EDGE_LOCAL':
                if edge_local_offsets is None or a not in edge_local_offsets or b not in edge_local_offsets:
                    raise ValueError('EDGE Preview: локальные нормали отсутствуют')
                offset_a,offset_b=edge_local_offsets[a],edge_local_offsets[b]
                offset_a,offset_b=checked_direction(offset_a),checked_direction(offset_b)
                c=tuple(a[i]+signed_depth*offset_a[i] for i in range(3))
                d=tuple(b[i]+signed_depth*offset_b[i] for i in range(3))
            else:
                c = tuple(a[i]+delta[i] for i in range(3))
                d = tuple(b[i]+delta[i] for i in range(3))
            source.extend((a,b))
            if kind == 'FILLED_GUIDE':
                # Short normal indicators are NOT destination edges or a cap.
                # They provide visible depth/direction without claiming a
                # production-equivalent Even Offset preview.
                mid=tuple((a[i]+b[i])*.5 for i in range(3))
                tip=tuple(mid[i]+delta[i] for i in range(3))
                wire.extend((mid,tip))
            else:
                wire.extend((a,c,c,d,d,b))
            if kind in {'FILLED_PLANAR', 'EDGE', 'EDGE_LOCAL'}:
                triangles.extend((a,b,d,a,d,c))
    return source,wire,triangles


def interactive_surface_preview_primitives(face_groups, signed_depth):
    """Return source highlight triangles and exact planar displaced cap triangles.

    face_groups entries: (triangles, world_normal, classification). This is
    entirely read-only cached geometry, never a BMesh operation or selection.
    GUIDE regions show their source faces only: no invented destination cap.
    """
    depth=float(signed_depth)
    if not math.isfinite(depth):
        raise ValueError('Недопустимая глубина подсветки')
    result=[]
    for triangles,normal,kind in face_groups:
        normal=tuple(float(v) for v in normal)
        if len(normal)!=3 or not all(math.isfinite(v) for v in normal):
            raise ValueError('Некорректная нормаль поверхности')
        if kind not in ('FILLED_PLANAR','FILLED_GUIDE'):
            raise ValueError('Неизвестный тип поверхности Preview')
        source=[]
        dest=[]
        for triangle in triangles:
            if len(triangle)!=3:
                raise ValueError('Некорректная триангуляция Preview')
            positions=[tuple(float(v) for v in point) for point in triangle]
            if any(len(pt)!=3 or not all(math.isfinite(v) for v in pt) for pt in positions):
                raise ValueError('Неверные координаты поверхности')
            source.extend(positions)
            if kind in {'FILLED_PLANAR','FILLED_GUIDE'}:
                if kind == 'FILLED_PLANAR':
                    dest.extend(tuple(pt[i]+depth*normal[i] for i in range(3)) for pt in positions)
                # FILLED_GUIDE: no false destination cap. Actual Blender
                # Even Offset uses mesh adjacency, not this average normal.
        result.append((source,dest,kind))
    return result


def interactive_point_in_triangle(point, triangle, tolerance=1.e-8):
    """Read-only point hit-test in screen coordinates, including triangle edges."""
    x,y=(float(v) for v in point)
    (ax,ay),(bx,by),(cx,cy)=[tuple(float(v) for v in q) for q in triangle]
    area=(bx-ax)*(cy-ay)-(by-ay)*(cx-ax)
    if not math.isfinite(area) or abs(area)<1.e-8:
        return False
    u=((bx-x)*(cy-y)-(by-y)*(cx-x))/area
    v=((cx-x)*(ay-y)-(cy-y)*(ax-x))/area
    w=1.-u-v
    return all(-tolerance<=q<=1.+tolerance for q in (u,v,w))


def interactive_preview_status(face_groups):
    """The requested preview precision has to be explicit and conservative."""
    if not face_groups:
        return 'EDGE_GUIDE'
    if any(item[2]=='FILLED_GUIDE' for item in face_groups):
        return 'SCHEMATIC_FILLED'
    return 'PLANAR_GEOMETRY_ONLY'  # UV splitting is deliberately not simulated.


def _interactive_registry():
    return _overlay_driver_registry()


def stop_interactive_preview():
    """Idempotent cleanup, including handles from a previous module reload."""
    global _INTERACTIVE_SESSION
    registry = _interactive_registry()
    record = registry.pop(_INTERACTIVE_REGISTRY_KEY, None) if registry is not None else None
    instance = _INTERACTIVE_SESSION
    _INTERACTIVE_SESSION = None
    records = []
    if isinstance(record,dict):
        records.append(record)
    elif record is not None:
        # Legacy registry may contain a single native GPU handle.
        records.append({'handlers':(record,)})
    if instance is not None and getattr(instance, '_handler_record', None) is not record:
        extra = getattr(instance, '_handler_record', None)
        if isinstance(extra,dict):
            records.append(extra)
        elif extra is not None:
            records.append({'handlers':(extra,)})
    seen = set()
    for item in records:
        raw_handles=item.get('handlers', ())
        for handle in (raw_handles if isinstance(raw_handles,(list,tuple)) else (raw_handles,)):
            if handle is None or id(handle) in seen:
                continue
            seen.add(id(handle))
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except Exception as exc:
                record_action('interactive_cleanup_error', str(exc)[:160])
        timer = item.get('timer')
        manager = item.get('manager')
        if timer is not None and manager is not None:
            try:
                manager.event_timer_remove(timer)
            except Exception as exc:
                record_action('interactive_timer_cleanup_error', str(exc)[:160])
    for item in records:
        owner = item.get('owner')
        if owner is not None:
            owner._active = False
            owner._handler_record = None
    if instance is not None:
        instance._active = False
        instance._handler_record = None




def isolated_filled_volume_template(context):
    """Copy the *live* edit BMesh; never extrude/edit the user's source Mesh.

    Native Extrude Along Normals uses Extrude Region then Transform Shrink/Fatten
    with Even Offset. This BMesh-only preview follows the same two conceptual
    stages: extrude_face_region and normal * calc_shell_factor.  It is a
    candidate preview, NOT certified equivalent to the transform operator until
    the dedicated Blender parity fixture passes. In particular there are
    subtleties involving connected boundary geometry and object scaling.

    Returns an immutable map of only newly extruded faces (cap + reveals). No
    temporary Blender objects, undo steps, Scene properties or GPU resources.
    """
    if getattr(context,'mode',None)!='EDIT_MESH':
        raise ValueError('Для FILLED Preview требуется Edit Mode')
    obj=getattr(context,'edit_object',None)
    if obj is None or obj.type!='MESH':
        raise ValueError('Не выделен Mesh')
    source=bmesh.from_edit_mesh(obj.data)
    selected_ids=[index for index,face in enumerate(source.faces)
                  if face.select and not face.hide]
    if not selected_ids:
        raise ValueError('Нет выбранных граней FILLED')
    transform=obj.matrix_world.copy()
    from .native_direction_contract import plan_native_faces
    direction_plan=plan_native_faces(
        getattr(context.scene,'brick_hole_direction_source','AUTO'),
        getattr(context.scene,'brick_hole_manual_normal',(0.,1.,0.)),
        1.0,
        (tuple(face_world_normal(obj,face)) for face in source.faces
         if face.select and not face.hide),
    )
    # Blender's Transform Shrink/Fatten can use world orientation and scaled
    # normals; do not imply parity for nonuniformly transformed meshes.
    scale=transform.to_scale()
    if any(abs(abs(float(value))-1.0)>1.e-5 for value in scale):
        raise ValueError('Призрак требует применённого масштаба объекта (1,1,1)')
    clone=source.copy()
    try:
        source_faces=list(clone.faces)
        if max(selected_ids)>=len(source_faces):
            raise ValueError('Не удалось сопоставить грани копии BMesh')
        region=[source_faces[i] for i in selected_ids]
        before_verts=set(clone.verts)
        before_faces=set(clone.faces)
        bmesh.ops.extrude_face_region(
            clone,geom=region,use_normal_flip=False,
            use_dissolve_ortho_edges=False)
        clone.normal_update()
        new_verts=set(clone.verts)-before_verts
        new_faces=set(clone.faces)-before_faces
        caps=[f for f in new_faces if all(v in new_verts for v in f.verts)]
        if len(caps)!=len(region):
            raise ValueError(f'Preview: найдено {len(caps)} крышек вместо {len(region)}')
        if not new_verts or not new_faces:
            raise ValueError('Preview: копия BMesh не создала новую геометрию')
        # World space makes the GPU drawing independent of object origin.
        coords={}
        for vert in clone.verts:
            point=transform @ vert.co
            if vert in new_verts:
                if direction_plan.mode=='REGION_TRANSLATE_WORLD':
                    displacement=Vector(direction_plan.world_displacement)
                else:
                    if not hasattr(vert,'calc_shell_factor'):
                        raise ValueError('Blender BMesh не поддерживает calc_shell_factor')
                    factor=float(vert.calc_shell_factor())
                    if not math.isfinite(factor) or factor<=0 or factor>40.:
                        raise ValueError('Preview: неоднозначный Even Offset у острого угла')
                    displacement=transform.to_3x3() @ (vert.normal*factor)
            else:
                displacement=Vector((0.,0.,0.))
            pv=tuple(float(x) for x in point)
            dv=tuple(float(x) for x in displacement)
            if not all(math.isfinite(x) for x in pv+dv):
                raise ValueError('Preview: неверные координаты BMesh')
            coords[vert]=(pv,dv)
        # Only staged faces, never unmodified portions of the source wall.
        face_rows=[]
        for face in new_faces:
            face_rows.append(tuple(coords[vert] for vert in face.verts))
        # Deduplicate rendered lines by actual vertex indices, not world XYZ.
        lines=[]
        touched=set()
        for face in new_faces:
            for edge in face.edges:
                if edge in touched:continue
                touched.add(edge)
                lines.append((coords[edge.verts[0]],coords[edge.verts[1]]))
        return {'faces':tuple(face_rows),'lines':tuple(lines),
                'caps':len(caps),'side_faces':len(new_faces)-len(caps),
                'selected_faces':len(region),'method':('BMESH_WORLD_TRANSLATE_CANDIDATE' if direction_plan.mode=='REGION_TRANSLATE_WORLD' else 'BMESH_SHELL_FACTOR_CANDIDATE')}
    finally:
        clone.free()


def isolated_filled_volume_primitives(template,signed_depth):
    """Evaluate the frozen BMesh result at a modal depth without touching bpy.

    Tessellation is GPU-only and DOES NOT triangulate user's production Mesh.
    """
    depth=float(signed_depth)
    if not math.isfinite(depth):raise ValueError('Preview: нечисловая глубина')
    from mathutils import Vector
    from mathutils.geometry import tessellate_polygon
    all_tris=[]
    for face in template['faces']:
        verts=[tuple(p[k]+depth*d[k] for k in range(3)) for p,d in face]
        if len(verts)<3:raise ValueError('Preview: вырожденная грань')
        if len(verts)==3:
            tris=((0,1,2),)
        else:
            tris=tessellate_polygon([[Vector(p) for p in verts]])
        for tri in tris:
            pts=[verts[i] if isinstance(i,int) else tuple(i) for i in tri]
            a,b,c=pts
            u=tuple(b[i]-a[i] for i in range(3))
            v=tuple(c[i]-a[i] for i in range(3))
            n=cross(u,v)
            if dot(n,n)<1.e-20:
                raise ValueError('Preview: вырожденный треугольник при смещении')
            all_tris.extend((a,b,c))
    wire=[]
    for first,second in template['lines']:
        wire.extend((tuple(first[0][k]+depth*first[1][k] for k in range(3)),
                     tuple(second[0][k]+depth*second[1][k] for k in range(3))))
    return wire,all_tris


def interactive_tessellated_world_triangles(world_ring, tessellation):
    """Convert mathutils.geometry.tessellate_polygon output to world XYZ.

    Blender 4.5 returns vertex *indices*, not coordinate vectors, for each
    tessellated triangle. Older test stubs/other implementations may return
    vectors. Both forms are supported, with strict shape/finite/degeneracy
    checks before the GPU preview. This routine never mutates BMesh.
    """
    def as_xyz(value):
        try:
            result = tuple(float(coordinate) for coordinate in value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Некорректная вершина триангуляции Preview') from exc
        if len(result) != 3 or not all(math.isfinite(c) for c in result):
            raise ValueError('Координаты треугольника Preview должны быть конечными XYZ')
        return result

    vertices = [as_xyz(point) for point in world_ring]
    if len(vertices) < 3:
        raise ValueError('У полигона Preview меньше трёх вершин')
    tris = list(tessellation)
    if len(tris) != len(vertices) - 2:
        raise ValueError('Триангуляция полигона не полна; нельзя рисовать неверный Preview')
    result = []
    for triangle in tris:
        try:
            corners = tuple(triangle)
        except TypeError as exc:
            raise ValueError('Треугольник Preview не содержит трёх вершин') from exc
        if len(corners) != 3:
            raise ValueError('Треугольник Preview должен иметь ровно три вершины')
        if all(isinstance(point, int) and not isinstance(point, bool) for point in corners):
            if len(set(corners)) != 3 or any(index < 0 or index >= len(vertices) for index in corners):
                raise ValueError('Недопустимые индексы вершин триангуляции Preview')
            positions = tuple(vertices[index] for index in corners)
        else:
            if any(isinstance(point, (int, float, bool)) for point in corners):
                raise ValueError('Смешаны индексы и координаты триангуляции Preview')
            positions = tuple(as_xyz(point) for point in corners)
        a,b,c = positions
        u = tuple(b[i]-a[i] for i in range(3))
        v = tuple(c[i]-a[i] for i in range(3))
        area2 = cross(u, v)
        if not all(math.isfinite(x) for x in area2) or dot(area2,area2) <= 1e-30:
            raise ValueError('Вырожденный треугольник Preview')
        result.append(positions)
    return result


def interactive_selection_snapshot(context):
    """Build a read-only multi-component preview without applying Blender ops.

    Native FILLED uses Extrude Faces Along Normals with Even Offset. Only
    oriented coplanar components permit a geometrically precise displacement
    preview; other components are marked GUIDE, never labelled precise.
    """
    obj = context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    faces, edges, _ = partition_brick_selection(
            bm,getattr(getattr(context, 'tool_settings',None), 'mesh_select_mode',None))
    if not faces and not edges:
        raise ValueError('Выделите полигоны или рёбра')
    if len(faces)>_MAX_INTERACTIVE_FACES or sum(len(f.verts) for f in faces)>_MAX_INTERACTIVE_FACE_CORNERS:
        raise ValueError('Слишком много полигонов для плавного Preview; используйте меньшую область')
    components=[]
    edge_preview_offsets = {}
    if edges:
        for chain in split_selected_cycles(edges):
            group_edges=[entry[0] for entry in chain]
            local_normals,vertex_dirs=edge_local_frame(
                obj,group_edges,context.scene,1.0,validate_shape=False,bm=bm)
            normal=local_normals[group_edges[0]]
            for vert,direction in vertex_dirs.items():
                key=tuple(float(v) for v in obj.matrix_world @ vert.co)
                if key in edge_preview_offsets and any(abs(edge_preview_offsets[key][i]-direction[i])>1e-7 for i in range(3)):
                    raise ValueError('EDGE Preview: вершины с одинаковыми координатами неоднозначны')
                edge_preview_offsets[key]=tuple(float(v) for v in direction)
            components.append((group_edges,normal,'EDGE_LOCAL'))
    if faces:
        from .native_direction_contract import plan_native_faces
        plan = plan_native_faces(
            getattr(context.scene,'brick_hole_direction_source','AUTO'),
            getattr(context.scene,'brick_hole_manual_normal',(0.,1.,0.)),
            1.0,
            (tuple(face_world_normal(obj,f)) for f in faces),
        )
        for component in selected_face_components(faces):
            normal = (Vector(plan.world_displacement)
                      if plan.mode=='REGION_TRANSLATE_WORLD' else
                      selected_region_world_normal(obj,component,require_consistent_sign=True))
            world_points=[tuple(float(x) for x in obj.matrix_world @ v.co)
                          for f in component for v in f.verts]
            normals=[tuple(float(x) for x in face_world_normal(obj,f)) for f in component]
            # Even Offset considers the surrounding mesh; shifting the
            # selection's normal is NOT exact, even if it is planar.
            kind = 'FILLED_GUIDE' 
            components.append((boundary_edges_for_faces(component),normal,kind))
    segments=[]
    surface_groups=[]
    # Tessellate readonly polygons once outside GPU draw callbacks.
    # A planar ngon may have a concave outline; a triangle fan is invalid.
    from mathutils.geometry import tessellate_polygon
    for component in (selected_face_components(faces) if faces else []):
        normal=(Vector(plan.world_displacement) if plan.mode=='REGION_TRANSLATE_WORLD'
                else selected_region_world_normal(obj,component,require_consistent_sign=True).normalized())
        points=[tuple(float(x) for x in obj.matrix_world @ v.co)
                for f in component for v in f.verts]
        normals=[tuple(float(x) for x in face_world_normal(obj,f)) for f in component]
        planar=preview_planarity(normals,points)
        tris=[]
        for face in component:
            world_ring=[obj.matrix_world @ v.co for v in face.verts]
            if len(world_ring)<3:
                raise ValueError('В выделении обнаружен вырожденный полигон')
            try:
                tess=tessellate_polygon([world_ring])
            except (ValueError,RuntimeError) as exc:
                raise ValueError('Не удалось безопасно триангулировать полигон Preview') from exc
            # Blender tessellate_polygon returns (i0, i1, i2) indices.
            # Decode them against this exact world-space polygon ring.
            tris.extend(interactive_tessellated_world_triangles(world_ring,tess))
        surface_groups.append((tris,tuple(float(x) for x in normal),
                               'FILLED_GUIDE'))
    for group_edges,normal,kind in components:
        if not math.isfinite(float(normal.length)) or normal.length<=EPS:
            raise ValueError(f'Не определена нормаль {kind}')
        normal=normal.normalized()
        world_edges=[
            (tuple(float(v) for v in obj.matrix_world @ edge.verts[0].co),
             tuple(float(v) for v in obj.matrix_world @ edge.verts[1].co))
            for edge in group_edges
        ]
        segments.append((world_edges,tuple(float(v) for v in normal),kind))
    all_edges=[edge for group_edges,_normal,_kind in segments for edge in group_edges]
    if not 1<=len(all_edges)<=_MAX_INTERACTIVE_EDGES:
        raise ValueError(f'Для Preview выберите 1–{_MAX_INTERACTIVE_EDGES} рёбер границы')
    coords=[p for edge in all_edges for p in edge]
    center=tuple(sum(p[i] for p in coords)/len(coords) for i in range(3))
    main_normal=segments[0][1]
    return all_edges,main_normal,center,segments,(len(faces),len(edges)),surface_groups,edge_preview_offsets


def interactive_edges_snapshot(context):
    """Compatibility for earlier QA; interactive path selects one domain."""
    _edges, _normal, _center, _groups, _counts, _surfaces = interactive_selection_snapshot(context)
    return _edges,_normal,_center


class VIEW3D_OT_brick_hole_interactive(bpy.types.Operator):
    bl_idname = 'view3d.brick_hole_interactive'
    bl_label = 'Интерактивные откосы · один тип выделения'
    bl_description = 'Viewport Preview рёбер ИЛИ граней, много независимых областей; Enter/Esc'
    bl_options = {'REGISTER', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return (getattr(context, 'area', None) is not None
                and context.area.type == 'VIEW_3D'
                and context.mode == 'EDIT_MESH'
                and context.edit_object is not None
                and context.edit_object.type == 'MESH')

    def _screen_region(self):
        return next((r for r in self._area.regions if r.type == 'WINDOW'), None)

    def _screen_xyz(self, xyz):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        screen = location_3d_to_region_2d(
            self._region, self._area.spaces.active.region_3d, Vector(xyz))
        return (float(screen.x), float(screen.y)) if screen is not None else None

    def _world_target(self):
        return tuple(self._center[i] + self._normal[i] * self._signed_depth()
                     for i in range(3))

    def _signed_depth(self):
        return interactive_signed_depth(self._depth, self._direction)

    def _mouse_pos(self, event):
        # event.mouse_region_* can refer to the original N-panel, not WINDOW.
        return float(event.mouse_x - self._region.x), float(event.mouse_y - self._region.y)

    def _handle_targets(self):
        result=[]
        for edges,normal,kind in self._groups[:12]:
            coords=[pt for edge in edges for pt in edge]
            if not coords:
                continue
            center=tuple(sum(p[i] for p in coords)/len(coords) for i in range(3))
            if kind=='EDGE_LOCAL':
                offsets=getattr(self,'_edge_local_preview',{})
                local=[offsets[tuple(p)] for p in coords if tuple(p) in offsets]
                if not local:
                    raise ValueError('EDGE LOCAL: отсутствуют направления ручки')
                normal=Vector(tuple(sum(p[i] for p in local)/len(local) for i in range(3)))
            target=tuple(center[i]+normal[i]*self._signed_depth() for i in range(3))
            result.append((center,normal,target,kind))
        return result

    def _hit_handle(self, xy):
        if not self._view_enabled('handles'):
            return None
        for origin,normal,target,kind in self._handle_targets():
            point=self._screen_xyz(target)
            if point and (xy[0]-point[0])**2+(xy[1]-point[1])**2<=22**2:
                return origin,normal
        return None

    def _hit_surface(self, xy):
        # Snapshot projected positions once per camera matrix/window size.
        # In particular, mouse movement never touches BMesh or operators.
        surfaces=getattr(self,'_surfaces',None) or ()
        if not surfaces:
            return -1
        try:
            m=self._area.spaces.active.region_3d.perspective_matrix
            view_key=(self._region.width,self._region.height,
                      tuple(round(float(v),9) for row in m for v in row))
        except (AttributeError, ReferenceError, RuntimeError, ValueError, TypeError):
            view_key=None
        if view_key is None or view_key!=getattr(self,'_hover_cache_key',None):
            boxes=[]
            count=0
            for group_index,(triangles,_normal,_kind) in enumerate(surfaces):
                for triangle in triangles:
                    if count>=900:
                        break
                    screen=[self._screen_xyz(point) for point in triangle]
                    count+=1
                    if any(pt is None for pt in screen):
                        continue
                    xs=[pt[0] for pt in screen]
                    ys=[pt[1] for pt in screen]
                    boxes.append((group_index,(min(xs),min(ys),max(xs),max(ys)),screen))
                if count>=900:
                    break
            # No cache reuse when Blender's matrix is unavailable.
            self._hover_cache_key=view_key
            self._hover_cache=boxes
        for group_index,box,screen in self._hover_cache:
            if box[0]<=xy[0]<=box[2] and box[1]<=xy[1]<=box[3]:
                if interactive_point_in_triangle(xy,screen):
                    return group_index
        return -1

    def _is_handle(self, xy):
        return self._hit_handle(xy) is not None

    def _hud_layout(self):
        try:
            anchor=self._screen_xyz(self._center) if getattr(self,'_center',None) is not None else None
        except (ImportError,RuntimeError,ReferenceError,AttributeError):
            anchor=None
        mode=getattr(getattr(getattr(bpy,'context',None),'scene',None),'brick_hole_hud_placement','AUTO')
        try:
            addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            from ...core.ui_layout_scaling import from_preferences
            scale=from_preferences(addon.preferences if addon else None).safe_window
        except (AttributeError,ReferenceError,RuntimeError):scale=1.0
        base=interactive_hud_layout(self._region.width,self._region.height,
                                    anchor if mode=='AUTO' else None,scale=scale,
                                    collapsed=getattr(self,'_collapsed',False),
                                    view_open=getattr(self,'_view_open',False),
                                    options_open=getattr(self,'_options_open',False))
        if base is None:
            return None
        # Pass 07: a single strict placement result drives paint AND hitboxes.
        # The text/warning lines reach beyond the 88px panel: reserve their
        # whole envelope so viewport chrome or peer panels cannot cover them.
        from ...core.window_manager import place_unobstructed
        from ...core.window_visibility import scope_from_context
        px0,py0,px1,py1=base['panel']
        origin=(px0,py0)
        # Reserve the COMPLETE expanded flyout envelope.  Previously only the
        # warnings under the HUD were accounted for; the View dropdown could
        # extend above the reserved rect and be hidden by another HUD.
        all_tops=(v[3] for v in base.values() if len(v)==4)
        extent=(px1-px0, max(py1-py0,
            base.get('gpu_error',(0.,py1))[1]-py0+20*scale,
            max(all_tops, default=py1)-py0+7*scale))
        vw,vh=self._region.width,self._region.height
        chrome=(max(0,vw-96*scale),max(0,vh-96*scale),vw,vh)
        placement=place_unobstructed((vw,vh),'BRICK_HOLE_PREVIEW',extent,origin,
                                     blockers=(chrome,),margin=8*scale,gap=6*scale,
                                     scope=scope_from_context(
                                                      bpy.context,area=self._area,region=self._region))
        if placement is None:
            return None
        dx,dy=placement[0]-px0,placement[1]-py0
        return {key:((v[0]+dx,v[1]+dy,v[2]+dx,v[3]+dy) if len(v)==4
                     else (v[0]+dx,v[1]+dy)) for key,v in base.items()}

    def _button_regions(self):
        layout=self._hud_layout()
        return (layout['apply'],layout['cancel']) if layout and 'apply' in layout else ((0,0,0,0),(0,0,0,0))

    def _redraw(self):
        try:
            self._area.tag_redraw()
        except (ReferenceError, RuntimeError, AttributeError):
            pass

    def _view_enabled(self, key):
        return bool(getattr(getattr(bpy.context,'scene',None), 'brick_hole_show_'+key, True))

    def _set_normal_source(self, context, mode, normal=None):
        """Update direction + all preview caches together or restore old state."""
        scene=context.scene
        previous=(scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal))
        try:
            if mode not in {'AUTO','FACE','GLOBAL'}:
                raise ValueError('Неизвестный источник направления')
            if mode in {'FACE','GLOBAL'}:
                direction=Vector(normal if normal is not None else scene.brick_hole_manual_normal)
                if direction.length<=EPS or not all(math.isfinite(float(x)) for x in direction):
                    raise ValueError('Нормаль должна быть конечной и ненулевой')
                direction.normalize()
                scene.brick_hole_manual_normal=tuple(direction)
            scene.brick_hole_direction_source=mode
            checked=unified_preflight(context,self._signed_depth())
            capture=interactive_selection_snapshot(context)
            candidate=None
            candidate_warning=''
            if capture[4][0]:
                try:
                    candidate=isolated_filled_volume_template(context)
                except (ValueError, RuntimeError, AttributeError, ReferenceError) as exc:
                    candidate_warning=str(exc)[:180]
            sig=preview_signature(context,None)
            if not sig:raise ValueError('Не удалось сохранить новый снимок Preview')
            self._isolated_volume=candidate
            self._isolated_volume_warning=candidate_warning
            self._preview_precision=('BMESH_CANDIDATE' if candidate is not None
                                     else 'FILLED_GUIDE' if capture[4][0] else 'EDGE_GUIDE')
            self._edges,self._normal,self._center,self._groups,self._counts=capture[:5]
            self._surfaces=capture[5] if len(capture)>5 else []
            self._edge_local_preview=capture[6] if len(capture)>6 else {}
            self._signature=sig
            self._hover_cache_key=None
            self._source_uv_advisory=source_uv_warning_message(checked)
            self._preview_block_reason=''
            self._refresh_axis_availability(context)
            record_action('normal_source_changed',f'{mode} {tuple(round(v,6) for v in scene.brick_hole_manual_normal)}')
            self._redraw()
        except Exception:
            scene.brick_hole_direction_source=previous[0]
            scene.brick_hole_manual_normal=previous[1]
            self._refresh_axis_availability(context)
            raise

    def _hover_pick_face(self,context,event):
        """Read-only ray against the active EDIT BMesh; no selection mutation."""
        from bpy_extras.view3d_utils import region_2d_to_origin_3d,region_2d_to_vector_3d
        from mathutils.bvhtree import BVHTree
        xy=self._mouse_pos(event)
        if not (0 <= xy[0] < self._region.width and 0 <= xy[1] < self._region.height):
            return None
        obj=context.edit_object
        bmesh_src=bmesh.from_edit_mesh(obj.data)
        # Cached for the short picker session; source geometry is protected by
        # the same signature as the main modal Brick Hole tool.
        if getattr(self,'_pick_bvh',None) is None:
            bmesh_src.faces.index_update()
            self._pick_bvh=BVHTree.FromBMesh(bmesh_src,epsilon=0.)
        origin=region_2d_to_origin_3d(self._region,context.space_data.region_3d,xy)
        direction=region_2d_to_vector_3d(self._region,context.space_data.region_3d,xy)
        inv=obj.matrix_world.inverted_safe()
        local_origin=inv @ origin
        local_dir=(inv.to_3x3() @ direction).normalized()
        position,local_normal,face_id,distance=self._pick_bvh.ray_cast(local_origin,local_dir)
        if face_id is None:return None
        bmesh_src.faces.ensure_lookup_table()
        if face_id >= len(bmesh_src.faces):return None
        face=bmesh_src.faces[face_id]
        linear=obj.matrix_world.to_3x3()
        normal=(linear.inverted().transposed() @ face.normal).normalized()
        # A mirrored matrix changes the winding of the world-space polygon.
        if linear.determinant()<0:normal.negate()
        center=obj.matrix_world @ position
        outline=[obj.matrix_world @ vertex.co for vertex in face.verts]
        return (center,normal,outline,face.index)

    def _stored_face_frame(self, scene):
        if not bool(getattr(scene,'brick_hole_face_frame_valid',False)):
            raise ValueError('Сначала нажмите «ПОЛИГОН» и выберите грань')
        frame=tuple(tuple(getattr(scene,'brick_hole_face_frame_'+k)) for k in 'xyz')
        if not is_orthonormal_right_handed(frame):
            raise ValueError('Локальные оси полигона повреждены; выберите грань заново')
        return frame

    def _refresh_axis_availability(self, context):
        """Read selection once per mode/selection change, never per GPU frame."""
        try:
            from .direction_snap_blender import available_axes
            basis=(self._stored_face_frame(context.scene)
                   if context.scene.brick_hole_direction_source=='FACE' else None)
            self._axis_availability=available_axes(context,face_basis=basis)
        except (ValueError, RuntimeError, ReferenceError, AttributeError) as exc:
            self._axis_availability={key:(False,str(exc)[:125]) for key in 'XYZ'}
        return self._axis_availability

    def _set_uv_strategy(self,context,policy):
        if policy not in {'STRICT','ADAPTIVE','CONTINUE'}:
            raise ValueError('Неверный UV режим')
        if brick_hole_uv_policy(context.scene)==policy and not _brick_hole_uv_legacy_conflict(context.scene):return
        context.scene.brick_hole_uv_policy=policy
        context.scene['brick_hole_uv_policy_set']=True
        self._options_in_progress=False
        self._options_open=False
        self._preview_precision='EDGE_GUIDE'
        self._isolated_volume=None
        self._signature=preview_signature(context,None)
        if not self._signature:
            raise ValueError('Изменён UV-режим, но исходная сетка более недоступна')
        record_action('interactive_uv_mode',policy+'; source UV unchanged; exact preview NOT VERIFIED')
        self._redraw()

    def _find_direction_options(self,context):
        """Cheap geometry filter now, one full candidate per subsequent TIMER."""
        if not collision_guard_enabled(context.scene):
            raise ValueError('ВАРИАНТЫ: включите защиту пересечений, иначе направления нельзя назвать проверенными')
        from .direction_alternatives_core import candidate_specs
        from .direction_snap_blender import available_axes
        eligibility=available_axes(context)
        prepared=[]
        for key,mode,axis,sign in candidate_specs():
            ok,reason=(eligibility[axis] if mode=='GLOBAL' else (True,'AUTO требует Preflight'))
            prepared.append(dict(key=key,mode=mode,axis=axis,sign=sign,
                                 status='NOT_CHECKED' if ok else 'GEOMETRY_BLOCKED',
                                 reason='Ожидает Preflight' if ok else str(reason)[:170]))
        self._direction_options=prepared
        self._options_open=True
        self._view_open=False
        self._options_depth=self._signed_depth()
        self._options_signature=self._signature
        self._options_start=time.perf_counter()
        self._options_probes=0
        self._options_in_progress=any(a['status']=='NOT_CHECKED' for a in prepared)
        self._options_feedback='ВАРИАНТЫ: предварительная проверка готова; Preflight по одному'
        record_action('brick_hole_direction_alternatives_begin',
            '; '.join(a['key']+':'+a['status'] for a in prepared))
        self._redraw()

    def _advance_direction_options(self,context):
        if not getattr(self,'_options_in_progress',False):return
        if not getattr(self,'_options_open',False):
            self._options_in_progress=False;return
        if (self._signed_depth()!=self._options_depth or
                preview_signature(context,None)!=self._options_signature):
            self._options_in_progress=False
            for candidate in self._direction_options:
                if candidate['status']=='VERIFIED_CANDIDATE':
                    candidate['status']='NOT_CHECKED'
                    candidate['reason']='Проверка устарела: изменились входные данные'
            self._options_feedback='Выделение, направление или глубина изменились: повторите поиск'
            return
        # Soft time budget: unlike a loop of 8 expensive collision checks,
        # each TIMER event runs at most one synchronous production Preflight.
        if self._options_probes>=8 or time.perf_counter()-self._options_start>=8.:
            self._options_in_progress=False
            self._options_feedback='Проверка неполная · остальные направления не проверены'
            return
        item=next((a for a in self._direction_options if a['status']=='NOT_CHECKED'),None)
        if item is None:
            self._options_in_progress=False;return
        from .direction_alternatives_blender import probe_single_candidate
        self._options_probes+=1
        outcome,reason=probe_single_candidate(context,item['mode'],item['axis'],
                                              abs(self._options_depth)*item['sign'])
        item['status']=outcome
        item['reason']=reason[:170]
        record_action('brick_hole_direction_candidate',
            f"{item['key']}; {outcome}; {reason[:120]}")
        if not any(a['status']=='NOT_CHECKED' for a in self._direction_options):
            self._options_in_progress=False
        passed=[a['key'] for a in self._direction_options if a['status']=='VERIFIED_CANDIDATE']
        self._options_feedback=(f'Проверено {self._options_probes}: '+
                                (', '.join(passed) if passed else 'пока нет допустимых'))
        if not self._options_in_progress:
            self._options_feedback+=' · завершено' if not any(
                a['status']=='NOT_CHECKED' for a in self._direction_options) else ' · НЕПОЛНО'
        self._redraw()

    def _choose_direction_option(self,context,index):
        opts=getattr(self,'_direction_options',[])
        if not 0<=index<len(opts):raise ValueError('Вариант отсутствует')
        if (self._signed_depth()!=getattr(self,'_options_depth',None) or
                preview_signature(context,None)!=getattr(self,'_options_signature',None)):
            raise ValueError('Варианты устарели: изменились выделение, глубина или настройки')
        item=opts[index]
        if item['status']!='VERIFIED_CANDIDATE':
            raise ValueError(item['key']+': '+item['reason'])
        original=(self._depth,self._direction)
        sign=item['sign']
        try:
            self._depth=abs(original[0])
            self._direction='OUTWARD' if sign>0 else 'INWARD'
            if item['mode']=='AUTO':
                self._set_normal_source(context,'AUTO')
            else:
                from .direction_alternatives_core import VECTORS
                self._set_normal_source(context,'GLOBAL',VECTORS[item['axis']])
                context.scene.brick_hole_global_axis=item['axis']
            self._preview_block_reason=''
            self._remember_parameter()
            self._options_open=False
            self._options_in_progress=False
            self._options_feedback=item['key']+' · выбран после повторного Preflight'
            record_action('brick_hole_alternative_selected',self._options_feedback)
        except Exception:
            self._depth,self._direction=original
            raise
        finally:self._redraw()

    def _toggle_snap(self,context):
        self._snap_enabled=not bool(getattr(self,'_snap_enabled',False))
        context.scene.brick_hole_vertex_snap=self._snap_enabled
        self._clear_snap_index()
        self._snap_feedback=('VERTEX SNAP · ON (проекция вершины на ось ручки)'
                             if self._snap_enabled else 'VERTEX SNAP · OFF')
        record_action('brick_hole_vertex_snap_toggle',f'enabled={self._snap_enabled}')
        if self._snap_enabled and not getattr(self,'_dragging',False):
            self._cache_snap_vertices(context)
        self._redraw()

    def _snap_view_key(self, context):
        """The projected grid is invalid after orbit/pan/zoom/object transform."""
        rv3d=self._area.spaces.active.region_3d
        matrix=context.edit_object.matrix_world
        return (int(self._region.width),int(self._region.height),
                tuple(round(float(v),7) for row in rv3d.perspective_matrix for v in row),
                tuple(round(float(v),7) for row in matrix for v in row))

    def _clear_snap_index(self):
        self._snap_grid=None
        self._snap_checker=None
        self._snap_index_cursor=0
        self._snap_index_total=0
        self._snap_index_ready=False
        self._snap_index_key=None
        self._snap_index_disabled=False
        self._snap_hit=None
        self._snap_rejected=None
        self._snap_world_vertices=()  # compatibility; no O(N) per-MOVE scan

    def _cache_snap_vertices(self, context):
        """Begin incremental construction; never project all vertices at drag start."""
        from .snap_spatial_index import SnapScreenGrid
        self._clear_snap_index()
        bm=bmesh.from_edit_mesh(context.edit_object.data)
        bm.verts.ensure_lookup_table()
        # True BMesh identifiers are updated only on index creation.
        bm.verts.index_update()
        total=len(bm.verts)
        if total>250000:
            self._snap_index_disabled=True
            self._snap_feedback='SNAP: более 250 000 вершин — уменьшите область/упростите Mesh'
            record_action('brick_hole_snap_budget_exceeded',f'vertices={total};limit=250000')
            return
        self._snap_index_key=self._snap_view_key(context)
        self._snap_index_total=total
        self._snap_grid=SnapScreenGrid(self._region.width,self._region.height)
        self._snap_index_started=time.perf_counter()
        self._snap_feedback=f'SNAP · индексируем вершины 0/{total}'
        record_action('brick_hole_snap_index_started',f'vertices={total};mode=incremental')
        self._advance_snap_index(context,budget_ms=6.,batch_limit=4000)

    def _advance_snap_index(self, context, *, budget_ms=12., batch_limit=12000):
        """Bound the expensive Blender viewport projection in a TIMER slice."""
        import time as _time
        if self._snap_grid is None or self._snap_index_ready:
            return
        if self._snap_index_key!=self._snap_view_key(context):
            self._cache_snap_vertices(context)
            return
        bm=bmesh.from_edit_mesh(context.edit_object.data)
        bm.verts.ensure_lookup_table()
        if len(bm.verts)!=self._snap_index_total:
            self._cache_snap_vertices(context)
            return
        cursor=self._snap_index_cursor
        count=self._snap_index_total
        deadline=_time.perf_counter()+budget_ms/1000.
        matrix=context.edit_object.matrix_world
        processed=0
        while cursor<count and processed<batch_limit:
            vert=bm.verts[cursor]
            if not vert.hide:
                world=tuple(matrix @ vert.co)
                xy=self._screen_xyz(world)
                self._snap_grid.add(cursor,world,xy)
            cursor+=1
            processed+=1
            if processed%128==0 and _time.perf_counter()>=deadline:
                break
        self._snap_index_cursor=cursor
        if cursor>=count:
            # Construct a single visibility BVH; do not raycast every vertex.
            try:
                from .snap_occlusion_blender import VisibleFaceOcclusion
                bvh_started=_time.perf_counter()
                self._snap_checker=VisibleFaceOcclusion(context)
                bvh_ms=round(1000.*(_time.perf_counter()-bvh_started),3)
            except Exception as exc:
                self._snap_checker=None
                self._snap_feedback='SNAP: проверка видимости недоступна: '+str(exc)[:90]
                record_action('brick_hole_snap_visibility_unavailable',str(exc)[:150])
                self._snap_index_ready=True
                return
            self._snap_index_ready=True
            self._snap_feedback=f'SNAP V · {self._snap_grid.count} вершин в поле зрения · BVH готов'
            record_action('brick_hole_snap_index_ready',
                f'total={count};on_screen={self._snap_grid.count};occlusion=bvh;'
                f'bvh_ms={bvh_ms};elapsed_ms={round(1000.*(_time.perf_counter()-self._snap_index_started),3)}')
        else:
            self._snap_feedback=f'SNAP · индексируем {cursor}/{count} (без ложных захватов)'

    def _snap_drag(self,context,pos):
        """Screen-nearest true vertex -> depth on handle -> readonly preflight.

        This is *axially projected snapping*. If the picked vertex is not on
        the handle axis, the HUD explicitly shows the projected point/offset.
        No claim of literal 3D coincidence is made.
        """
        if not self._snap_enabled:return 'MISS'
        if (self._snap_grid is not None and
                self._snap_index_key!=self._snap_view_key(context)):
            self._cache_snap_vertices(context)
        if self._snap_grid is None:
            return 'BLOCKED' if self._snap_feedback.startswith('SNAP:') else 'MISS'
        if not self._snap_index_ready:
            return 'PENDING'
        if self._snap_checker is None:
            return 'BLOCKED'
        from .direction_snap_core import choose_vertex_candidate
        try:
            nearby,overcrowded=self._snap_grid.query(pos)
        except ValueError:
            return 'BLOCKED'
        if overcrowded:
            self._snap_hit=None
            self._snap_feedback='SNAP: слишком много вершин под курсором — приблизьте вид'
            return 'BLOCKED'
        candidate=None
        blocked=0
        # Deterministic near-to-far screen ordering, then first non-occluded
        # projectable target. No full-mesh linear scan per mouse event.
        for vertex in nearby:
            if not self._snap_checker.is_visible(vertex[2],vertex[1]):
                blocked+=1
                continue
            candidate=choose_vertex_candidate((vertex,),pos,
                            self._drag_origin,self._drag_normal)
            if candidate is not None:
                break
        if candidate is None:
            self._snap_hit=None
            self._snap_feedback=('SNAP: вершина закрыта другой гранью'
                                  if blocked else '')
            return 'BLOCKED' if blocked else 'MISS'
        index=candidate['vertex_index']
        depth=candidate['signed_depth']
        residual=candidate['axis_residual_m']
        world=candidate['world_xyz']
        projected=candidate['projected_xyz']
        if getattr(self,'_snap_rejected',None)==(index,round(depth,8)):
            return 'BLOCKED'
        if (getattr(self,'_snap_hit',None) is not None and
                self._snap_hit[0]==index and abs(self._signed_depth()-depth)<1e-9):
            return 'HIT'
        try:
            # The exact same signed-depth contract as production Apply.
            unified_preflight(context,depth)
        except (ValueError,RuntimeError,ReferenceError) as exc:
            self._snap_hit=None
            self._snap_rejected=(index,round(depth,8))
            self._snap_feedback='SNAP недоступен: '+str(exc)[:110]
            record_action('brick_hole_vertex_snap_blocked',
                          f'vert={index};depth={depth:+.6f};reason={str(exc)[:120]}')
            return 'BLOCKED'
        self._depth=abs(depth)
        self._direction='INWARD' if depth<0 else 'OUTWARD'
        self._snap_rejected=None
        self._snap_hit=(index,world,projected)
        if candidate['is_projected']:
            self._snap_feedback=(f'SNAP V{index} ПРОЕКЦИЯ · {depth:+.6f} м · '
                                 f'вне оси {residual:.3f} м')
        else:
            self._snap_feedback=f'SNAP V{index} · {depth:+.6f} м · отклонение {residual*1000:.1f} мм'
        record_action('brick_hole_vertex_snap_hit',self._snap_feedback)
        return 'HIT'

    def _select_manual_axis(self,context,axis):
        if not (self._counts[0] or self._counts[1]):
            raise ValueError('Сначала выберите грани или рёбра')
        eligibility=getattr(self,'_axis_availability',{}).get(axis,(False,'Направление не проверено'))
        if not eligibility[0]:
            self._blocked_axis=(axis,time.monotonic()+1.5)
            self._axis_block_reason=eligibility[1]
            record_action('brick_hole_axis_blocked',f'axis={axis}; reason={eligibility[1]}')
            self._redraw()
            raise ValueError('Ось '+axis+': '+eligibility[1])
        scene=context.scene
        previous_face_axis=scene.brick_hole_face_axis
        previous_global_axis=scene.brick_hole_global_axis
        if scene.brick_hole_direction_source=='FACE':
            vector=axis_vector(self._stored_face_frame(scene),axis)
            mode='FACE'
        else:
            vector={'X':(1.,0.,0.),'Y':(0.,1.,0.),'Z':(0.,0.,1.)}[axis]
            mode='GLOBAL'
        try:
            self._set_normal_source(context,mode,vector)
            if mode=='FACE':scene.brick_hole_face_axis=axis
            else:scene.brick_hole_global_axis=axis
            record_action('normal_axis_selected',
                f'basis={"FACE_LOCAL" if mode=="FACE" else "GLOBAL_WORLD"}; axis={axis}; world_xyz={tuple(round(c,9) for c in vector)}')
        except Exception as exc:
            scene.brick_hole_face_axis=previous_face_axis
            scene.brick_hole_global_axis=previous_global_axis
            self._blocked_axis=(axis,time.monotonic()+1.5)
            self._axis_block_reason=str(exc)[:135]
            raise

    def _capture_picked_face(self,context,hover):
        scene=context.scene
        position,normal,outline,index=hover
        basis=frame_from_polygon([tuple(p) for p in outline],tuple(normal))
        if not is_orthonormal_right_handed(basis):
            raise ValueError('Оси выбранного полигона не ортонормированы')
        previous=(scene.brick_hole_face_frame_valid,
                  tuple(scene.brick_hole_face_frame_x),tuple(scene.brick_hole_face_frame_y),
                  tuple(scene.brick_hole_face_frame_z),scene.brick_hole_face_axis)
        try:
            scene.brick_hole_face_frame_x=basis[0]
            scene.brick_hole_face_frame_y=basis[1]
            scene.brick_hole_face_frame_z=basis[2]
            scene.brick_hole_face_frame_valid=True
            # Z is the picked polygon normal, but it can be parallel to the
            # selected boundary. Do NOT reject the entire picked frame when
            # its X or Y is a safe extrusion direction for that selection.
            failed=[]
            for axis in ('Z','X','Y'):
                try:
                    self._set_normal_source(context,'FACE',axis_vector(basis,axis))
                    scene.brick_hole_face_axis=axis
                    break
                except (ValueError,RuntimeError,ReferenceError) as exc:
                    failed.append(f'{axis}: {exc}')
            else:
                raise ValueError('Ни одна локальная ось этого полигона не подходит: '+
                                 '; '.join(failed)[:160])
            self._face_frame_origin=tuple(position)
            record_action('normal_face_picked',
                f'face={index}; initial_axis={scene.brick_hole_face_axis}; '+
                f'basis_x={basis[0]}; basis_y={basis[1]}; basis_z={basis[2]}')
        except Exception:
            (scene.brick_hole_face_frame_valid,scene.brick_hole_face_frame_x,
             scene.brick_hole_face_frame_y,scene.brick_hole_face_frame_z,
             scene.brick_hole_face_axis)=previous
            raise

    def _draw_collapsed_bar(self, layout):
        """Same matte DWOS shell and Blender DPI as the expanded HUD.

        Paint only: the original layout remains the single hit-test source.
        """
        import gpu, blf
        from ...core.studio_gpu_components import draw_component, fill_rect, shell_background
        from ...core import theme_engine, design_tokens
        from ...core.ui_layout_scaling import from_preferences
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        prefs=addon.preferences if addon else None
        appearance=theme_engine.from_preferences(prefs)
        scale=from_preferences(prefs).safe_window
        state=gpu.state
        old=state.blend_get() if hasattr(state,'blend_get') else 'NONE'
        try:
            state.blend_set('ALPHA')
            panel=layout['panel']; button=layout['expand']
            if not draw_component(panel,part='card',theme=appearance,scale=scale):
                fill_rect(*panel,design_tokens.PANEL_BG,9*scale)
            shell_background(appearance,panel)
            if not draw_component(button,part='button',state='active',role='primary',
                                  theme=appearance,scale=scale):
                fill_rect(*button,design_tokens.PANEL_SURFACE,6*scale)
            blf.size(0,from_preferences(prefs).font_px(_viewport_skin.text_px('control')))
            blf.color(0,*_viewport_skin.ink(appearance))
            title='BRICK HOLE · Preview'
            title,_=design_tokens.fit_label(title,
                max(0.,button[0]-layout['title'][0]-8*scale),
                lambda value:blf.dimensions(0,value)[0])
            blf.position(0,*layout['title'],0.)
            blf.draw(0,title)
            label='РАЗВЕРНУТЬ'
            label,_=design_tokens.fit_label(label,
                max(0.,button[2]-button[0]-14*scale),
                lambda value:blf.dimensions(0,value)[0])
            blf.color(0,*_viewport_skin.control_ink(appearance,part='button',state='active',role='primary'))
            blf.position(0,button[0]+7*scale,button[1]+8*scale,0.)
            blf.draw(0,label)
        finally:
            state.blend_set(old)

    def _draw_picker_highlight(self):
        hover=getattr(self,'_picked_face_hover',None)
        if hover is None:return
        import gpu
        from gpu_extras.batch import batch_for_shader
        center,normal,outline,index=hover
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        points=[p for a,b in zip(outline,outline[1:]+outline[:1]) for p in (a,b)]
        state=gpu.state
        if points:
            batch=batch_for_shader(shader,'LINES',{'pos':points})
            shader.bind();shader.uniform_float('color',(.96,.73,.27,1.));batch.draw(shader)
        line=batch_for_shader(shader,'LINES',{'pos':[center,center+normal*.75]})
        shader.bind();shader.uniform_float('color',(.27,.77,1.,1.));line.draw(shader)

    def _draw_picked_frame(self,shader):
        """Three orientation axes in Viewport, only for the selected polygon."""
        if not self._view_enabled('handles'):
            return
        scene=bpy.context.scene
        if scene.brick_hole_direction_source!='FACE' or not scene.brick_hole_face_frame_valid:
            return
        origin=getattr(self,'_face_frame_origin',None)
        if origin is None:return
        from gpu_extras.batch import batch_for_shader
        frame=self._stored_face_frame(scene)
        chosen=scene.brick_hole_face_axis
        for label,vector,color in zip('XYZ',frame,((1.,.39,.41,1.),(.45,1.,.57,1.),(.48,.73,1.,1.))):
            size=.82 if label==chosen else .58
            end=tuple(origin[i]+size*vector[i] for i in range(3))
            poly=batch_for_shader(shader,'LINES',{'pos':[origin,end]})
            shader.bind()
            shader.uniform_float('color',color)
            poly.draw(shader)

    def _draw_wire(self):
        if not getattr(self, '_active', False) or bpy.context.area != self._area:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        state=gpu.state
        previous={}
        for name in ('blend','depth_test','line_width'):
            getter=getattr(state,name+'_get',None)
            if getter:
                previous[name]=getter()
        try:
            original,coords,triangles=interactive_preview_primitives(
                self._groups,self._signed_depth(),getattr(self,'_edge_local_preview',{}))
            shader=gpu.shader.from_builtin('UNIFORM_COLOR')
            state.blend_set('ALPHA')
            state.depth_test_set('NONE')
            if self._surfaces and self._view_enabled('fill'):
                layers=interactive_surface_preview_primitives(self._surfaces,self._signed_depth())
                for index,(origin,cap,kind) in enumerate(layers):
                    hovered=index==getattr(self,'_hover_region',-1)
                    for positions,color in (
                        (origin,(1.0,.69,.22,.21 if hovered else .095)),
                        (cap,(.13,.9,.75,.21 if hovered else .09)),
                    ):
                        if positions:
                            layer=batch_for_shader(shader,'TRIS',{'pos':positions})
                            shader.bind()
                            shader.uniform_float('color',color)
                            layer.draw(shader)
            if getattr(self,'_isolated_volume',None) is not None and (self._view_enabled('fill') or self._view_enabled('wire')):
                try:
                    volume_lines,volume_triangles=isolated_filled_volume_primitives(
                        self._isolated_volume,self._signed_depth())
                    state.blend_set('ALPHA')
                    if volume_triangles and self._view_enabled('fill'):
                        staged=batch_for_shader(shader,'TRIS',{'pos':volume_triangles})
                        shader.bind()
                        shader.uniform_float('color',(.13,.91,.72,.27))
                        staged.draw(shader)
                    if volume_lines and self._view_enabled('wire'):
                        state.line_width_set(2.5)
                        edges=batch_for_shader(shader,'LINES',{'pos':volume_lines})
                        shader.bind()
                        shader.uniform_float('color',(.18,1.,.82,1.))
                        edges.draw(shader)
                except Exception as exc:
                    self._isolated_volume_warning=str(exc)[:160]
                    self._isolated_volume=None
                    record_action('preview_bmesh_draw_fallback',self._isolated_volume_warning)
            if triangles and self._view_enabled('fill'):
                side=batch_for_shader(shader,'TRIS',{'pos':triangles})
                shader.bind()
                shader.uniform_float('color',(.13,.83,.67,.19))
                side.draw(shader)
            state.line_width_set(2.0)
            for segments,color in ((original if self._view_enabled('source') else [],(1.0,.74,.25,.83)),
                                   (coords if self._view_enabled('wire') else [],(.26,.9,.76,.94))):
                if segments:
                    batch=batch_for_shader(shader,'LINES',{'pos':segments})
                    shader.bind()
                    shader.uniform_float('color',color)
                    batch.draw(shader)
            for center,normal,target,kind in (self._handle_targets() if self._view_enabled('handles') else []):
                drag=batch_for_shader(shader,'LINES',{'pos':[center,target]})
                shader.bind()
                shader.uniform_float('color',(.96,.98,.98,.96))
                state.line_width_set(2.5)
                drag.draw(shader)
            if getattr(self,'_picking_face',False):
                self._draw_picker_highlight()
            else:
                self._draw_picked_frame(shader)
        except Exception as exc:
            self._draw_error=f'{type(exc).__name__}: {exc}'[:120]
            self._draw_trace=traceback.format_exc()[-3200:]
        finally:
            for name,fallback in (('line_width',1.),('depth_test','NONE'),('blend','NONE')):
                try:
                    getattr(state,name+'_set')(previous.get(name,fallback))
                except Exception as exc:
                    self._draw_error=f'GPU_STATE: {exc}'[:120]

    def _draw_hud(self):
        if not getattr(self, '_active', False) or bpy.context.area != self._area:
            return
        try:
            import blf
            import gpu
            from gpu_extras.batch import batch_for_shader
            layout=self._hud_layout()
            if layout is None:
                raise ValueError('Viewport слишком мал для управления')
            if getattr(self,'_collapsed',False):
                self._draw_collapsed_bar(layout)
                return
            state=gpu.state
            old=state.blend_get() if hasattr(state,'blend_get') else 'NONE'
            try:
                state.blend_set('ALPHA')
                shader=gpu.shader.from_builtin('UNIFORM_COLOR')
                from ...core import design_tokens as theme
                from ...core import theme_engine
                item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
                appearance=theme_engine.from_preferences(item.preferences if item else None)
                accent=appearance.accent
                from ...core.workspace_components import component_visual as _workspace_visual
                from ...core import window_shell as _shell
                source=getattr(bpy.context.scene,'brick_hole_direction_source','AUTO')
                face_axis=getattr(bpy.context.scene,'brick_hole_face_axis','Z')
                global_axis=getattr(bpy.context.scene,'brick_hole_global_axis','Z')
                active_axis=face_axis if source=='FACE' else global_axis
                # DWOS-native semantic paint states, not hand-written accent
                # colors. No hitbox sizes or modal actions are changed here.
                from ...core.dwos_hud_contract import paint_spec
                raw_keys=('panel','accent_strip','mode_back','axis_back','numeric_back',
                          'value_hit','depth_minus','depth_plus','vertex_snap','apply','cancel',
                          'uv_back','uv_strict','uv_adaptive','uv_continue',
                          'collapse','view','mode_auto','mode_face','mode_global','options_find',
                          'axis_x','axis_y','axis_z','collision_guard')
                raw_keys += (('view_menu',) if 'view_menu' in layout else ())
                raw_keys += tuple('view_'+option for option in
                                  ('source','wire','fill','handles','labels') if
                                  'view_'+option in layout)
                def _selected(key):
                    return ((key=='mode_auto' and source=='AUTO') or
                            (key=='mode_face' and source=='FACE') or
                            (key=='mode_global' and source in {'GLOBAL','MANUAL'}) or
                         (key=='vertex_snap' and bool(getattr(self,'_snap_enabled',False))) or
                            (key=='collision_guard' and collision_guard_enabled(bpy.context.scene)) or
                            (key=='uv_'+brick_hole_uv_policy(bpy.context.scene).lower()) or
                            (key=='axis_'+active_axis.lower() and source in {'FACE','GLOBAL','MANUAL'}) or
                            (key=='view' and getattr(self,'_view_open',False)) or
                            (key.startswith('view_') and key!='view_menu' and
                             self._view_enabled(key[5:])))
                specs={key:paint_spec(key, selected=_selected(key),
                        focused=(key=='value_hit' and self._editing_numeric),
                        disabled=(key.startswith('axis_') and not getattr(self,'_axis_availability',{}).get(key[-1].upper(),(False,''))[0])) # disabled axes still receive clicks to show the reason
                       for key in raw_keys}
                surfaces={key: _workspace_visual(
                    data['part'],data['state'],data['role'],appearance).background
                    for key,data in specs.items()}
                surfaces['accent_strip']=(*accent,.83)

                from ...core.studio_gpu_components import fill_rect, draw_component
                from ...core.ui_layout_scaling import from_preferences as _from_preferences
                k_window=_from_preferences(item.preferences if item else None).safe_window
                for key,color in surfaces.items():
                    x1,y1,x2,y2=layout[key]
                    data=specs[key]
                    radius=(10. if key=='panel' else 6.)*k_window
                    if key=='accent_strip':
                        drawn=fill_rect(x1,y1,x2,y2,color,0.)
                    else:
                        drawn=draw_component((x1,y1,x2,y2),part=data['part'],
                                state=data['state'],role=data['role'],
                                theme=appearance,scale=k_window)
                    if not drawn:
                        vertices=((x1,y1),(x2,y1),(x2,y2),(x1,y1),(x2,y2),(x1,y2))
                        batch=batch_for_shader(shader,'TRIS',{'pos':vertices})
                        shader.bind()
                        shader.uniform_float('color',color)
                        batch.draw(shader)
                    if key=='panel':
                        from issmaker_modeling_assistant.appearance_gpu import draw_shared_background
                        px0,py0,px1,py1=layout['panel']
                        draw_shared_background(appearance,(px0,py0,px1-px0,py1-py0),strength_factor=.3)
            finally:
                state.blend_set(old)
            font=0
            from ...core.ui_layout_scaling import from_preferences
            item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            style=from_preferences(item.preferences if item else None)
            blf.size(font,style.font_px(_viewport_skin.text_px('body')))
            k=style.safe_window
            if style.debug:
                try:
                    from ...core.ui_layout_scaling import record_layout
                    boxes=[(name,layout[name]) for name in ('apply','cancel','value_hit','depth_minus','depth_plus')]
                    record_layout('Brick Hole',self._region.width,self._region.height,boxes)
                except (AttributeError,RuntimeError,ValueError,TypeError):
                    pass
            def label(x,y,message,color=None, max_width=None):
                available=max(0.,min(float(self._region.width)-float(x)-12.,
                                      float(max_width) if max_width is not None else float('inf')))
                message,_clipped=theme.fit_label(message,available,
                                                  lambda sample:blf.dimensions(font,sample)[0])
                if message:
                    blf.color(font,*(color if color is not None else _viewport_skin.ink(appearance)))
                    from ...core.hud_readability import snap as _snap_text
                    blf.position(font,_snap_text(x),_snap_text(y),0)
                    blf.draw(font,message)
            label(*layout['title'],f'BRICK HOLE  /  F {self._counts[0]}   E {self._counts[1]}',max_width=390*k)
            label(*layout['axis_caption'],
                  ('ЛОКАЛЬНЫЕ' if bpy.context.scene.brick_hole_direction_source=='FACE' else 'МИРОВЫЕ'),
                  _viewport_skin.ink(appearance,kind='muted'),max_width=147*k)
            source=getattr(bpy.context.scene,'brick_hole_direction_source','AUTO')
            for key,text in (('mode_auto','АВТО'),('mode_face','ПОЛИГОН'),('mode_global','GLOBAL'),('options_find','ВАРИАНТЫ'),
                             ('axis_x','X'),('axis_y','Y'),('axis_z','Z'),('vertex_snap','SNAP V'),
                             ('uv_strict','1000 px/м'),('uv_adaptive','ПЛАВНО'),('uv_continue','КАК У СТЕНЫ'),
                             ('collapse','−'),('view','ВИД ▾'),
                             ('collision_guard', '☑  ЗАЩИТА ПЕРЕСЕЧЕНИЙ · ВКЛ'
                              if collision_guard_enabled(bpy.context.scene)
                              else '☐  ЗАЩИТА ПЕРЕСЕЧЕНИЙ · ВЫКЛ — РИСК НАЛОЖЕНИЙ')):
                rect=layout[key]
                focused=((key=='mode_auto' and source=='AUTO') or
                         (key=='mode_face' and source=='FACE') or
                         (key=='mode_global' and source in {'GLOBAL','MANUAL'}) or
                         (key=='vertex_snap' and bool(getattr(self,'_snap_enabled',False))) or
                         (key=='uv_'+brick_hole_uv_policy(bpy.context.scene).lower()) or
                         (key=='axis_'+(bpy.context.scene.brick_hole_face_axis if source=='FACE' else bpy.context.scene.brick_hole_global_axis).lower() and source in {'FACE','GLOBAL','MANUAL'}))
                part=specs[key]['part']
                state_name=specs[key]['state']
                role=specs[key]['role']
                color=_viewport_skin.control_ink(appearance,part=part,state=state_name,role=role)
                if key=='collision_guard' and not collision_guard_enabled(bpy.context.scene):
                    color=(1.,.55,.32,1.)
                if key.startswith('axis_') and not getattr(self,'_axis_availability',{}).get(key[-1].upper(),(False,''))[0]:
                    color=(.93,.36,.34,.9)
                    blink=getattr(self,'_blocked_axis',None)
                    if blink and blink[0]==key[-1].upper() and time.monotonic()<blink[1]:
                        color=(1.,.25+.25*abs(math.sin(time.monotonic()*13)),.22,1.)
                label(rect[0]+9*k,rect[1]+7*k,text,color,
                      max_width=rect[2]-rect[0]-15*k)
            ppm_sample=getattr(self,'_uv_source_sample',None)
            ppm_hint=('ИСТОЧНИК %.2f px/м · ' % ppm_sample['mean'] if ppm_sample else 'ИСТОЧНИК не измерен · ')
            if brick_hole_uv_policy(bpy.context.scene)=='ADAPTIVE':
                # Accurate parameter shown even while Preview is schematic.
                label(*layout['warning'],
                      ppm_hint+'ПЛАВНО · переход %.3f м · Preview схематичен' %
                      float(bpy.context.scene.brick_hole_uv_transition_m),
                      (.91,.74,.34,1.))
            elif brick_hole_uv_policy(bpy.context.scene)=='CONTINUE':
                label(*layout['warning'],
                      ppm_hint+'КАК У СТЕНЫ · 1–2 ребра · Preview схематичен',
                      (.91,.74,.34,1.))
            if 'options_menu' in layout:
                fill_rect(*layout['options_menu'],(.075,.105,.14,.96),8*k)
                from .direction_alternatives_core import candidate_specs
                names=[a[0] for a in candidate_specs()]
                available=getattr(self,'_direction_options',[])
                for i in range(8):
                    rect=layout['option_'+str(i)]
                    data=available[i] if i<len(available) else None
                    status=data['status'] if data else 'NOT_CHECKED'
                    col=((.29,.64,.46,.88) if status=='VERIFIED_CANDIDATE' else
                         (.43,.16,.18,.92) if status in ('GEOMETRY_BLOCKED','PREFLIGHT_BLOCKED')
                         else (.22,.25,.29,.92))
                    fill_rect(*rect,col,5*k)
                    short={'VERIFIED_CANDIDATE':'OK', 'GEOMETRY_BLOCKED':'ОСЬ',
                           'PREFLIGHT_BLOCKED':'КОНФЛИКТ','NOT_CHECKED':'НЕ ПРОВЕРЕНО'}[status]
                    label(rect[0]+7*k,rect[1]+9*k,
                          names[i]+' · '+short,(.95,.97,1.,1.),max_width=rect[2]-rect[0]-10*k)
            if 'view_menu' in layout:
                for key,text in (('source','Исходный контур'),('wire','Новая сетка'),('fill','Заливка'),('handles','Ручки'),('labels','Подписи')):
                    rect=layout['view_'+key]
                    label(rect[0]+6*k,rect[1]+6*k,('☑  ' if self._view_enabled(key) else '☐  ')+text,
                          _viewport_skin.control_ink(appearance,part=specs['view_'+key]['part'],
                            state=specs['view_'+key]['state'],role=specs['view_'+key]['role']),
                          max_width=rect[2]-rect[0]-12*k)
            if getattr(self,'_picking_face',False):
                label(*layout['warning'],'ПОЛИГОН: наведите на грань · ЛКМ взять нормаль · Esc отменить',(.94,.79,.42,1.))

            if self._editing_numeric:
                caret=max(0,min(len(self._numeric),getattr(self,'_numeric_caret',len(self._numeric))))
                text=self._numeric[:caret]+'│'+self._numeric[caret:]
            else:
                text=f'{self._signed_depth():+.6f}'
            # ISS-004: an unmistakable labelled, bordered input replaces
            # the old barely noticeable plain-text numeric depth line.
            label(*layout['value'],f'ГЛУБИНА  {text} м',
                  _viewport_skin.control_ink(appearance,part=specs['value_hit']['part'],
                    state=specs['value_hit']['state'],role=specs['value_hit']['role']),max_width=192*k)
            for box_key, glyph in (('depth_minus','−'),('depth_plus','+')):
                rect=layout[box_key]
                label(rect[0]+7*k,rect[1]+7*k,glyph,
                      _viewport_skin.control_ink(appearance,part=specs[box_key]['part'],
                        state=specs[box_key]['state'],role=specs[box_key]['role']),max_width=14*k)
            label(*layout['apply_text'],'ENTER  СОЗДАТЬ',
                  _viewport_skin.control_ink(appearance,part=specs['apply']['part'],
                    state=specs['apply']['state'],role=specs['apply']['role']),max_width=108*k)
            label(*layout['cancel_text'],'ESC  ОТМЕНА',
                  _viewport_skin.control_ink(appearance,part=specs['cancel']['part'],
                    state=specs['cancel']['state'],role=specs['cancel']['role']),max_width=108*k)
            for index,(center,normal,target,kind) in enumerate(self._handle_targets() if self._view_enabled('handles') else []):
                xy=self._screen_xyz(target)
                if xy is None or not 0<=xy[0]<=self._region.width or not 0<=xy[1]<=self._region.height:
                    continue
                shader=gpu.shader.from_builtin('UNIFORM_COLOR')
                vertices=[(xy[0],xy[1])]
                vertices += [(xy[0]+8*math.cos(2*math.pi*i/16),
                              xy[1]+8*math.sin(2*math.pi*i/16)) for i in range(17)]
                tri=[point for i in range(1,17) for point in (vertices[0],vertices[i],vertices[i+1])]
                old_blend=state.blend_get() if hasattr(state,'blend_get') else 'NONE'
                try:
                    state.blend_set('ALPHA')
                    dot=batch_for_shader(shader,'TRIS',{'pos':tri})
                    shader.bind()
                    shader.uniform_float('color',(.22,.93,.73,.99) if kind!='FILLED_GUIDE'
                                               else (.99,.68,.27,.99))
                    dot.draw(shader)
                finally:
                    state.blend_set(old_blend)
                if index<4 and self._view_enabled('labels'):
                    label(xy[0]+13,xy[1]+10,f'{self._signed_depth():+.3f}m',(.54,1.,.84,1.))
            snap_hit=getattr(self,'_snap_hit',None)
            if snap_hit and getattr(self,'_snap_enabled',False):
                xy=self._screen_xyz(snap_hit[1])
                if xy is not None:
                    shader=gpu.shader.from_builtin('UNIFORM_COLOR')
                    from gpu_extras.batch import batch_for_shader
                    state.blend_set('ALPHA')
                    ring=[(xy[0]+12*math.cos(2*math.pi*i/32),xy[1]+12*math.sin(2*math.pi*i/32)) for i in range(33)]
                    circle=batch_for_shader(shader,'LINE_STRIP',{'pos':ring})
                    shader.bind();shader.uniform_float('color',(.55,1.,.65,1.));circle.draw(shader)
                    # Show the actual axial landing point. If vertex is away
                    # from the handle axis, a clear tether prevents the false
                    # impression that geometry moved *to* the vertex itself.
                    if len(snap_hit)>2:
                        projected_xy=self._screen_xyz(snap_hit[2])
                        if projected_xy is not None and math.dist(xy,projected_xy)>2.:
                            guide=batch_for_shader(shader,'LINES',
                                {'pos':[xy,projected_xy]})
                            shader.bind();shader.uniform_float('color',(.94,.7,.29,.85));guide.draw(shader)
                            cross=[(projected_xy[0]-6,projected_xy[1]),
                                   (projected_xy[0]+6,projected_xy[1]),
                                   (projected_xy[0],projected_xy[1]-6),
                                   (projected_xy[0],projected_xy[1]+6)]
                            cross_batch=batch_for_shader(shader,'LINES',{'pos':cross})
                            shader.bind();shader.uniform_float('color',(.94,.7,.29,1.));cross_batch.draw(shader)
            planar=sum(kind=='FILLED_PLANAR' for _,_,kind in self._groups)
            guide=sum(kind=='FILLED_GUIDE' for _,_,kind in self._groups)
            if guide:
                if getattr(self,'_isolated_volume',None) is not None:
                    label(*layout['warning'],'FILLED: 3D BMesh · ожидает сверки с Blender Even Offset',
                          (.71,1.,.82,1.))
                else:
                    label(*layout['warning'],
                          'FILLED: точный объёмный Preview недоступен · '+
                          getattr(self,'_isolated_volume_warning','')[:35],(.99,.72,.43,1.))
            elif planar:
                label(*layout['warning'],f'Геометрия плоских: {planar} · UV-разрезы не показаны',
                      (.61,.97,.80,1.))
            elif any(g[2] == 'EDGE_LOCAL' for g in self._groups):
                label(*layout['warning'],'EDGE: локальные нормали · UV-разрезы не показаны',
                      (.61,.97,.80,1.))
            if self._draw_error:
                label(*layout['gpu_error'],'GPU: '+self._draw_error[:64],(1.,.4,.35,1.))
            elif getattr(self,'_preview_block_reason',''):
                label(*layout['gpu_error'],'Нельзя применить: '+self._preview_block_reason[:105],
                      (1.,.42,.32,1.),max_width=505*k)
            elif getattr(self,'_options_feedback',''):
                label(*layout['gpu_error'],self._options_feedback[:105],
                      (.74,1.,.82,1.),max_width=505*k)
            elif getattr(self,'_axis_block_reason','') and getattr(self,'_blocked_axis',None) and time.monotonic()<self._blocked_axis[1]:
                label(*layout['gpu_error'],'Ось: '+self._axis_block_reason,(1.,.34,.32,1.),max_width=505*k)
            elif getattr(self,'_snap_feedback',''):
                snap_color=((1.,.42,.33,1.) if self._snap_feedback.startswith('SNAP недоступен')
                            else (.97,.74,.35,1.) if 'ПРОЕКЦИЯ' in self._snap_feedback
                            else (.7,1.,.82,1.))
                label(*layout['gpu_error'],self._snap_feedback,snap_color,max_width=505*k)
        except Exception as exc:
            self._draw_error=f'HUD {type(exc).__name__}: {exc}'[:120]
            self._draw_trace=traceback.format_exc()[-3200:]

    def _numeric_start(self, *, replace=False):
        # Explicit focus (D/click) preserves the value and moves caret to the end:
        # 4 + 0 -> 40. Starting by typing a digit keeps legacy direct-replace.
        self._editing_numeric = True
        self._numeric = '' if replace else f'{self._signed_depth():g}'
        self._numeric_caret = len(self._numeric)
        self._numeric_selected = False
        self._dragging = False
        record_action('interactive_numeric_focus','replace' if replace else 'preserve')
        self._redraw()

    def _numeric_command(self, command, insert=''):
        from ...core.text_input_core import edit as text_edit
        self._numeric, self._numeric_caret, self._numeric_selected = text_edit(
            self._numeric, getattr(self,'_numeric_caret',len(self._numeric)),
            command, insert=insert, selected=getattr(self,'_numeric_selected',False),
            allowed=frozenset('0123456789.,-+ mcMМмСс'))
        self._redraw()

    def _numeric_commit(self):
        if not getattr(self, '_editing_numeric', False):
            return False
        # A blank edit is not confirmation; no previous value fallback.
        value = interactive_numeric_value(self._numeric)
        self._depth = abs(value)
        self._direction = 'INWARD' if value < 0 else 'OUTWARD'
        self._numeric = ''
        self._editing_numeric = False
        if str(getattr(self,'_preview_block_reason','')).startswith('Коллизия:'):
            self._preview_block_reason=''
            # A new depth is unverified, not still "blocked" by the old
            # collision witness. No expensive preflight while typing.
            try:
                bpy.context.scene.brick_hole_interactive_state=(
                    'PREVIEW · новая глубина требует проверки при Apply')
            except (ReferenceError,AttributeError):
                pass
        self._numeric_caret = 0
        self._numeric_selected = False
        self._remember_parameter()
        record_action('interactive_numeric_committed', f'signed_depth_m={value:.9f}; direction={self._direction}')
        self._redraw()
        return True

    def _remember_parameter(self):
        value=(round(float(self._depth),9),self._direction)
        if not hasattr(self,'_history'):
            self._history=[value]
            self._history_cursor=0
        elif self._history[self._history_cursor] != value:
            self._history=self._history[:self._history_cursor+1]+[value]
            self._history_cursor+=1
            self._history=self._history[-64:]
            self._history_cursor=len(self._history)-1

    def _navigate_parameter_history(self, redo=False):
        if not hasattr(self,'_history'):
            return False
        cursor=self._history_cursor+(1 if redo else -1)
        if not 0<=cursor<len(self._history):
            return False
        self._history_cursor=cursor
        self._depth,self._direction=self._history[cursor]
        if str(getattr(self,'_preview_block_reason','')).startswith('Коллизия:'):
            self._preview_block_reason=''
            try:
                bpy.context.scene.brick_hole_interactive_state=(
                    'PREVIEW · Undo/Redo параметров: требуется новый Preflight при Apply')
            except (ReferenceError,AttributeError):
                pass
        self._numeric=''
        self._dragging=False
        self._redraw()
        record_action('interactive_redo' if redo else 'interactive_undo',
                      f'depth={self._depth:.6f}; direction={self._direction}')
        return True

    def _close(self, context, state):
        stop_interactive_preview()
        # Drop immutable preview cache immediately, including cancel / Undo.
        self._isolated_volume = None
        self._pick_bvh = None
        self._clear_snap_index()
        self._picked_face_hover = None
        self._face_frame_origin = None
        self._picking_face = False
        try:
            context.scene.brick_hole_interactive_state = state
        except ReferenceError:
            pass
        self._redraw()

    def invoke(self, context, event):
        global _INTERACTIVE_SESSION
        if _INTERACTIVE_SESSION is None:
            issue_inspector.clear()
            seam_inspector.clear()
        if _INTERACTIVE_SESSION is not None:
            self.report({'WARNING'}, 'Интерактивный Brick Hole уже запущен')
            return {'CANCELLED'}
        if callable(globals().get('stop_fixture_nav_hud')):stop_fixture_nav_hud()
        self._handler_record = None
        self._active = False
        self._area = context.area
        self._region = self._screen_region()
        if self._region is None:
            self.report({'ERROR'}, 'Не найден WINDOW-регион 3D Viewport')
            return {'CANCELLED'}
        scene = context.scene
        try:
            if interactive_hud_layout(self._region.width,self._region.height) is None:
                raise ValueError('Увеличьте область 3D Viewport для безопасного управления')
            current_signed=scene_signed_depth(scene)
            self._direction = 'INWARD' if current_signed<0 else 'OUTWARD'
            self._depth = abs(current_signed)
            # A bad saved direction/depth must NOT prevent entering the HUD.
            # Apply remains guarded by the original production Preflight.
            self._preview_block_reason=''
            try:
                preflight_result=unified_preflight(context,self._signed_depth())
            except ValueError as exc:
                preflight_result=None
                self._preview_block_reason=str(exc)[:190]
                record_action('brick_hole_hud_recoverable_preflight',self._preview_block_reason)
            from .uv_policy_metrics import selected_edge_ppm
            try:self._uv_source_sample=selected_edge_ppm(context)
            except (ValueError,RuntimeError,ReferenceError,AttributeError):self._uv_source_sample=None
            self._source_uv_advisory = (source_uv_warning_message(preflight_result)
                                        if preflight_result is not None else '')
            if self._source_uv_advisory:
                # Persist the full warning for the sidebar, not just a fading
                # Blender report. Preflight remains non-blocking.
                scene.brick_hole_preview_text = self._source_uv_advisory[:380]
                scene.brick_hole_preview_warnings = 1
                record_action('interactive_uv_advisory', self._source_uv_advisory)
                self.report({'WARNING'}, self._source_uv_advisory[:245])
            try:
                capture=interactive_selection_snapshot(context)
            except ValueError as exc:
                if preflight_result is not None:
                    raise
                # Diagnostic-only HUD, never depict an unverified extrusion.
                # Keep selection identity and counts for later direction choice.
                obj=context.edit_object
                bm=bmesh.from_edit_mesh(obj.data)
                faces,edges,_=partition_brick_selection(
                    bm,context.tool_settings.mesh_select_mode)
                if not (faces or edges):raise
                source_edges=(list(edges) if edges else list({e for f in faces for e in f.edges}))
                positions=[obj.matrix_world @ v.co for e in source_edges for v in e.verts]
                if not positions:raise
                center=sum(positions,Vector((0.,0.,0.)))/len(positions)
                capture=([],Vector((0.,0.,1.)),tuple(center),[],(len(faces),len(edges)),[],{})
                record_action('brick_hole_hud_diagnostic_only',str(exc)[:160])
            # Older integrations/test mocks supplied only the five-element tuple.
            self._edges,self._normal,self._center,self._groups,self._counts=capture[:5]
            self._surfaces=capture[5] if len(capture)>5 else []
            self._edge_local_preview=capture[6] if len(capture)>6 else {}
            self._preview_precision=interactive_preview_status(self._surfaces) if 'interactive_preview_status' in globals() else 'EDGE_GUIDE'
            self._isolated_volume = None
            self._isolated_volume_warning = ''
            if self._counts[0]:
                try:
                    self._isolated_volume = isolated_filled_volume_template(context)
                    self._preview_precision = 'BMESH_CANDIDATE'
                    record_action('preview_bmesh_created',
                        f"caps={self._isolated_volume['caps']}; sides={self._isolated_volume['side_faces']}")
                except Exception as exc:
                    # Never mutate source Mesh or pretend a false final surface.
                    self._isolated_volume_warning=str(exc)[:180]
                    record_action('preview_bmesh_unavailable',self._isolated_volume_warning)
            if self._preview_block_reason:
                self._groups=[];self._surfaces=[];self._edge_local_preview={}
                self._isolated_volume=None;self._preview_precision='BLOCKED'
            self._signature = preview_signature(context, None)
            self._refresh_axis_availability(context)
            if not self._signature:
                raise ValueError('Не удалось закрепить снимок исходной геометрии')
            self._collapsed=False
            self._view_open=False
            self._options_open=False
            self._options_in_progress=False
            self._direction_options=[]
            self._options_feedback=''
            self._picking_face=False
            self._picked_face_hover=None
            self._pick_bvh=None
            self._face_frame_origin=None
            self._draw_error = ''
            self._draw_trace = ''
            self._draw_error_logged = False
            self._dragging = False
            self._snap_enabled=bool(getattr(scene,'brick_hole_vertex_snap',False))
            self._snap_hit=None
            self._snap_feedback=''
            self._snap_rejected=None
            self._clear_snap_index()
            self._blocked_axis=None
            self._axis_block_reason=''
            self._hover_region = -1
            self._hover_cache = []
            self._hover_cache_key = None
            self._editing_numeric = False
            self._numeric = ''
            self._history=[(round(float(self._depth),9),self._direction)]
            self._history_cursor=0
            self._active = True
            _INTERACTIVE_SESSION = self
            handle1 = bpy.types.SpaceView3D.draw_handler_add(self._draw_wire,(), 'WINDOW','POST_VIEW')
            record = {'handlers':[handle1], 'timer':None, 'manager':context.window_manager,
                      'owner':self}
            self._handler_record = record
            registry = _interactive_registry()
            if registry is not None:
                registry[_INTERACTIVE_REGISTRY_KEY] = record
            handle2 = bpy.types.SpaceView3D.draw_handler_add(self._draw_hud,(), 'WINDOW','POST_PIXEL')
            record['handlers'].append(handle2)
            record['timer'] = context.window_manager.event_timer_add(.35,window=context.window)
            context.window_manager.modal_handler_add(self)
            scene.brick_hole_interactive_state = (
                ('PREVIEW · FILLED: BMesh объём (паритет не подтверждён)' if self._isolated_volume
                 else 'PREVIEW · FILLED: схема, копия BMesh недоступна')
                if self._counts[0] else 'PREVIEW · EDGE: локальные направления')
            if self._source_uv_advisory:
                scene.brick_hole_interactive_state += ' · UV стены ≠ 1000; новые откосы 1000 px/м'
            record_action('interactive_start',f'faces={self._counts[0]}; edges={self._counts[1]}; depth={self._depth}; preview={[(g[2]) for g in self._groups]}')
            self._redraw()
            return {'RUNNING_MODAL'}
        except Exception as exc:
            if isinstance(exc, ValueError):
                write_log('INTERACTIVE PREFLIGHT BLOCKED: '+str(exc))
            else:
                write_log('INTERACTIVE INVOKE ERROR: '+traceback.format_exc())
            if self._active:
                self._close(context,'ERROR · '+str(exc)[:85])
            else:
                scene.brick_hole_interactive_state = 'BLOCKED · '+str(exc)[:85]
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}

    def _apply(self, context):
        try:
            if getattr(self,'_editing_numeric',False) and not self._numeric:
                raise ValueError('Введите точное значение глубины перед применением')
            if self._numeric:
                numeric_signed=interactive_numeric_value(self._numeric)
                self._depth=abs(numeric_signed)
                self._direction='INWARD' if numeric_signed<0 else 'OUTWARD'
            if self._draw_error:
                raise ValueError('Не удалось отобразить Preview: '+self._draw_error)
            if preview_signature(context,None) != self._signature:
                raise ValueError('Контур, UV или настройки изменились; запустите Preview заново')
            signed = self._signed_depth()
            if _brick_hole_uv_legacy_conflict(context.scene):
                raise ValueError('Разные старые UV-настройки: выберите режим в HUD')
            policy=brick_hole_uv_policy(context.scene)
            if policy=='STRICT':
                preflight_result=unified_preflight(context,signed)
                self._source_uv_advisory=source_uv_warning_message(preflight_result)
            else:
                # BH-04B.6.1: NO duplicate live-BMEdge scan in modal Apply.
                # The selection was already fingerprinted by preview_signature
                # above, and the authoritative operator
                # MESH_OT_brick_hole_uv_policy_apply independently executes
                # support_decision on the current BMesh before mutating it.
                # Iterating old BMEdge wrappers here produced a real Blender
                # ReferenceError after a prior atomic Mesh datablock swap.
                # Never suppress/translate that error into a false success;
                # leave final policy+topology validation to the dispatcher.
                self._source_uv_advisory='Стыки и UV проверяются в производственном Smart UV Apply' 
        except Exception as exc:
            write_log(('INTERACTIVE PREFLIGHT BLOCKED: '+str(exc)) if isinstance(exc, ValueError)
                      else ('INTERACTIVE APPLY ERROR: '+traceback.format_exc()))
            # BH0465: a genuine swept-collision is a recoverable PREVIEW
            # parameter failure, not a fatal modal/lifetime error.  Keep the
            # live selection and draw handlers so the user can press S for a
            # verified shallower signed depth, or Esc to exit. No mesh writes.
            from .collision_recovery_core import is_recoverable_collision, recovery_hint
            if isinstance(exc, ValueError) and is_recoverable_collision(exc):
                hint=recovery_hint(str(exc))
                self._preview_block_reason=hint
                context.scene.brick_hole_interactive_state='BLOCKED · '+hint[:170]
                context.scene.brick_hole_preview_text=hint[:380]
                context.scene.brick_hole_preview_warnings=1
                record_action('interactive_collision_recoverable',
                              f'depth_m={self._signed_depth():+.7g}; '+hint)
                self.report({'WARNING'},hint[:240])
                self._redraw()
                return {'RUNNING_MODAL'}
            self._close(context,'BLOCKED · '+str(exc)[:85])
            # Even a read-only preflight can locate a real crossing.  Open its
            # immutable witness HUD after the original modal is fully closed.
            if issue_inspector.issues():
                try:
                    region=next((r for r in self._area.regions if r.type=='WINDOW'),None)
                    if region is not None:
                        with context.temp_override(window=context.window,area=self._area,region=region):
                            bpy.ops.view3d.brick_hole_issue_inspector('INVOKE_DEFAULT')
                except Exception as inspector_exc:
                    write_log('ISSUE INSPECTOR PREFLIGHT START FAILED: '+repr(inspector_exc))
            self.report({'ERROR'},str(exc))
            record_action('interactive_blocked',str(exc)[:150])
            return {'CANCELLED'}
        scene = context.scene
        old_depth, old_direction = scene.brick_hole_depth, scene.brick_hole_direction
        self._close(context,'APPLYING')
        try:
            scene.brick_hole_depth = self._depth
            scene.brick_hole_direction = self._direction
            result = bpy.ops.mesh.brick_hole_uv_policy_apply()
            if 'FINISHED' not in result:
                diagnostic = _TRANSACTION_FAILURE_REASON or _recent_failure_log()
                raise RuntimeError('FILLED/EDGE: ' + diagnostic)
            scene.brick_hole_interactive_state = (
                ('DONE · UV '+brick_hole_uv_policy(scene)+
                 ('; КОЛЛИЗИИ НЕ ПРОВЕРЯЛИСЬ' if not collision_guard_enabled(scene)
                  else '; защита пересечений ON')))
            if self._source_uv_advisory:
                self.report({'WARNING'}, self._source_uv_advisory[:245])
            record_action('interactive_applied',f'depth={signed:.6f}; faces={self._counts[0]}; independent_edges={self._counts[1]}')
            return {'FINISHED'}
        except Exception as exc:
            write_log('INTERACTIVE APPLY ERROR: '+traceback.format_exc())
            scene.brick_hole_depth = old_depth
            scene.brick_hole_direction = old_direction
            scene.brick_hole_interactive_state = 'ERROR · '+str(exc)[:85]
            record_action('interactive_apply_failed',str(exc)[:160])
            if issue_inspector.issues():
                try:
                    region=next((r for r in self._area.regions if r.type=='WINDOW'),None)
                    if region is not None:
                        with context.temp_override(window=context.window,area=self._area,region=region):
                            bpy.ops.view3d.brick_hole_issue_inspector('INVOKE_DEFAULT')
                    record_action('contour_issue_inspector_open',f'count={len(issue_inspector.issues())}')
                except Exception as inspection_exc:
                    write_log('ISSUE INSPECTOR START FAILED: '+repr(inspection_exc))
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}

    def modal(self, context, event):
        if not getattr(self,'_active',False):
            return {'CANCELLED'}
        if (context.area != self._area or context.mode != 'EDIT_MESH'
                or context.edit_object is None):
            self._close(context,'STALE · изменён режим или область')
            return {'CANCELLED'}
        if event.type == 'TIMER':
            if self._draw_error and not getattr(self,'_draw_error_logged',False):
                write_log('INTERACTIVE DRAW FAILURE: '+str(self._draw_error)+'\n'+str(getattr(self,'_draw_trace','')))
                self._draw_error_logged=True
            try:
                if preview_signature(context,None) != self._signature:
                    self._close(context,'STALE · выделение, UV или настройки изменились')
                    self.report({'WARNING'},'Состояние изменилось. Preview остановлен')
                    return {'CANCELLED'}
            except (ValueError,RuntimeError,ReferenceError) as exc:
                self._close(context,'ERROR · '+str(exc)[:85])
                return {'CANCELLED'}
            if getattr(self,'_options_in_progress',False):
                try:self._advance_direction_options(context)
                except Exception as exc:
                    self._options_in_progress=False
                    self._options_feedback='Проверка вариантов прервана: '+str(exc)[:120]
                    record_action('brick_hole_direction_alternatives_error',str(exc)[:150])
            if getattr(self,'_snap_enabled',False):
                try:
                    if (not getattr(self,'_snap_index_disabled',False) and
                            (self._snap_grid is None or
                             self._snap_index_key!=self._snap_view_key(context))):
                        self._cache_snap_vertices(context)
                    if not self._snap_index_ready:
                        self._advance_snap_index(context)
                except (ValueError,RuntimeError,ReferenceError,AttributeError) as exc:
                    self._clear_snap_index()
                    self._snap_index_disabled=True
                    self._snap_feedback='SNAP: индекс недоступен: '+str(exc)[:90]
                    record_action('brick_hole_snap_index_failed',str(exc)[:140])
            self._redraw()
            return {'RUNNING_MODAL'}
        if getattr(self,'_picking_face',False):
            if event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS':
                self._picking_face=False
                self._pick_bvh=None
                self._picked_face_hover=None
                self._redraw()
                return {'RUNNING_MODAL'}
            if event.type in {'MIDDLEMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE'}:
                return {'PASS_THROUGH'}
            if event.type in {'MOUSEMOVE','LEFTMOUSE'}:
                try:
                    self._picked_face_hover=self._hover_pick_face(context,event)
                except Exception as exc:
                    self.report({'WARNING'},'Подбор полигона: '+str(exc)[:155])
                    self._picking_face=False
                    self._pick_bvh=None
                    return {'RUNNING_MODAL'}
                if event.type=='LEFTMOUSE' and event.value=='PRESS':
                    if self._picked_face_hover is None:
                        self.report({'WARNING'},'Наведите на полигон активного Mesh')
                        return {'RUNNING_MODAL'}
                    try:self._capture_picked_face(context,self._picked_face_hover)
                    except (ValueError,RuntimeError,ReferenceError) as exc:
                        self.report({'WARNING'},'Нормаль отклонена: '+str(exc)[:155])
                        return {'RUNNING_MODAL'}
                    self._picking_face=False
                    self._pick_bvh=None
                    self._picked_face_hover=None
                self._redraw()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}
        if event.type=='ESC' and event.value=='PRESS' and getattr(self,'_editing_numeric',False):
            self._editing_numeric=False
            self._numeric=''
            self._numeric_caret=0
            self._numeric_selected=False
            self._dragging=False
            record_action('interactive_numeric_cancel','Esc · restored existing depth')
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type=='ESC' and event.value=='PRESS' and getattr(self,'_options_open',False):
            self._options_open=False
            self._options_in_progress=False
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type=='ESC' and event.value=='PRESS' and getattr(self,'_view_open',False):
            self._view_open=False
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC','RIGHTMOUSE'} and event.value == 'PRESS':
            self._close(context,'CANCELLED · модель не изменена')
            record_action('interactive_cancel','Esc/right-click')
            return {'CANCELLED'}
        if event.type in {'MIDDLEMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE',
                          'NDOF_MOTION','TRACKPADPAN','TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        if getattr(self,'_editing_numeric',False) and event.ctrl and event.type=='A' and event.value=='PRESS':
            self._numeric_command('SELECT_ALL')
            return {'RUNNING_MODAL'}
        if event.ctrl and event.type in {'Z','Y'} and event.value == 'PRESS':
            redo=(event.type=='Y' or bool(getattr(event,'shift',False)))
            if self._numeric or getattr(self,'_editing_numeric',False):
                self._numeric=''
                self._numeric_caret=0
                self._numeric_selected=False
                self._editing_numeric=False
                self._dragging=False
                self._depth,self._direction=self._history[self._history_cursor]
                record_action('interactive_numeric_discarded','Ctrl+Z/Y')
            elif not self._navigate_parameter_history(redo=redo):
                self.report({'INFO'},'Нет изменений параметров Preview для отмены/повтора')
            self._redraw()
            return {'RUNNING_MODAL'}
        if (event.type == 'S' and event.value == 'PRESS' and not
                self._editing_numeric and not self._numeric and not self._dragging
                and not getattr(self,'_picking_face',False)):
            try:
                found=readonly_edge_safe_depth(context,self._signed_depth())
                self._depth=abs(found.candidate)
                self._direction='INWARD' if found.candidate<0 else 'OUTWARD'
                self._preview_block_reason=''
                context.scene.brick_hole_interactive_state=(
                    f'PREVIEW · проверенная глубина {found.candidate:+.6f} м; '
                    'контур / UV ещё проверяются при Apply')
                self._remember_parameter()
                self.report({'INFO'},
                            f'Безопасная глубина: {found.candidate:+.6f} м; '
                            f'{found.calls} проверок; не глобальный максимум')
                record_action('interactive_safe_depth',
                              f'candidate={found.candidate:+.8g}; probes={found.calls}')
            except (ValueError,RuntimeError,ReferenceError) as exc:
                self.report({'WARNING'},str(exc)[:225])
                record_action('interactive_safe_depth_blocked',str(exc)[:200])
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'B' and event.value == 'PRESS' and not self._editing_numeric and not self._numeric:
            try:self._find_direction_options(context)
            except (ValueError,RuntimeError,ReferenceError) as exc:
                self.report({'WARNING'},str(exc)[:175])
            return {'RUNNING_MODAL'}
        if event.type == 'V' and event.value == 'PRESS' and not self._editing_numeric and not self._numeric:
            self._toggle_snap(context)
            return {'RUNNING_MODAL'}
        if event.type == 'F' and event.value == 'PRESS' and not self._editing_numeric and not self._numeric:
            self._direction = 'OUTWARD' if self._direction == 'INWARD' else 'INWARD'
            self._remember_parameter()
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'R' and event.value == 'PRESS' and not self._editing_numeric:
            current_signed=scene_signed_depth(context.scene)
            self._depth=abs(current_signed)
            self._direction='INWARD' if current_signed<0 else 'OUTWARD'
            self._numeric = ''
            self._remember_parameter()
            self._redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'RET','NUMPAD_ENTER'} and event.value == 'PRESS':
            if getattr(self, '_editing_numeric', False):
                try:
                    self._numeric_commit()
                except ValueError as exc:
                    self.report({'WARNING'}, str(exc))
                return {'RUNNING_MODAL'}
            if self._numeric:
                try:
                    numeric_signed=interactive_numeric_value(self._numeric)
                    self._depth=abs(numeric_signed)
                    self._direction='INWARD' if numeric_signed<0 else 'OUTWARD'
                except ValueError as exc:
                    self.report({'WARNING'},str(exc))
                    return {'RUNNING_MODAL'}
            return self._apply(context)
        if event.type == 'D' and event.value == 'PRESS' and not (event.ctrl or event.alt):
            if not self._editing_numeric:
                self._numeric_start()
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and not (event.ctrl or event.alt):
            # Blender modal receives ASCII Unicode from the main keyboard and
            # often empty unicode for NUMPAD_0..9 and decimal.
            typed = str(getattr(event,'unicode','') or '')
            if not typed and event.type.startswith('NUMPAD_'):
                suffix=event.type[7:]
                typed=(suffix if suffix in '0123456789' and len(suffix)==1
                       else '.' if suffix in {'PERIOD','DECIMAL'}
                       else '-' if suffix in {'MINUS','SUBTRACT'}
                       else '+' if suffix in {'PLUS','ADD'} else '')
            if not typed and event.type in {'MINUS','PLUS'}:
                typed='-' if event.type=='MINUS' else '+'
            if typed=='−':typed='-'
            if not self._editing_numeric and typed in '0123456789.,-+' and typed:
                self._numeric_start(replace=True)
            if self._editing_numeric:
                if event.type in {'HOME','END','LEFT_ARROW','RIGHT_ARROW'}:
                    self._numeric_command({'LEFT_ARROW':'LEFT','RIGHT_ARROW':'RIGHT'}.get(event.type,event.type))
                    return {'RUNNING_MODAL'}
                if event.type in {'BACK_SPACE','DEL','DELETE'}:
                    self._numeric_command('BACKSPACE' if event.type=='BACK_SPACE' else 'DELETE')
                    return {'RUNNING_MODAL'}
                if typed and typed in '0123456789.,-+ mcMМмСс' and len(self._numeric)<24:
                    self._numeric_command('INSERT',typed)
                    return {'RUNNING_MODAL'}
                return {'RUNNING_MODAL'}  # typing never flips direction
        if event.type == 'LEFTMOUSE':
            pos = self._mouse_pos(event)
            if event.value == 'DOUBLE_CLICK' and self._hit_handle(pos) is not None:
                self._numeric_start()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS':
                layout=self._hud_layout()
                if layout and getattr(self,'_collapsed',False):
                    rect=layout['expand']
                    if rect[0]<=pos[0]<=rect[2] and rect[1]<=pos[1]<=rect[3]:
                        self._collapsed=False
                        record_action('hud_expand','Brick Hole')
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    return {'PASS_THROUGH'}
                if layout:
                    def inside(key):
                        box=layout.get(key)
                        return bool(box and len(box)==4 and box[0]<=pos[0]<=box[2] and box[1]<=pos[1]<=box[3])
                    if getattr(self,'_options_open',False):
                        for i in range(8):
                            if inside('option_'+str(i)):
                                try:self._choose_direction_option(context,i)
                                except (ValueError,RuntimeError,ReferenceError) as exc:
                                    self._options_feedback=str(exc)[:160]
                                    self.report({'WARNING'},self._options_feedback)
                                    self._redraw()
                                return {'RUNNING_MODAL'}
                        if inside('options_menu'):
                            return {'RUNNING_MODAL'}
                        self._options_open=False
                        self._options_in_progress=False
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    if inside('collision_guard'):
                        enabled=not collision_guard_enabled(context.scene)
                        context.scene.brick_hole_check_intersections=enabled
                        self._signature=preview_signature(context,None)
                        self._options_in_progress=False
                        self._options_open=False
                        self._direction_options=[]
                        context.scene.brick_hole_safe_depth_candidate=0.0
                        context.scene.brick_hole_safe_depth_status=('Проверки отключены' if not enabled
                                else 'Нажмите «Найти допустимую»')
                        record_action('interactive_collision_guard_toggle',
                                      'ON' if enabled else 'OFF · INTERSECTIONS_NOT_CHECKED')
                        write_log('BH0466 COLLISION PROTECTION '+('ON' if enabled else 'OFF: USER OVERRIDE'))
                        self.report({'WARNING'} if not enabled else {'INFO'},
                            'Коллизии НЕ проверяются — возможны наложения' if not enabled
                            else 'Защита от пересечений включена')
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    for name,mode in (('uv_strict','STRICT'),('uv_adaptive','ADAPTIVE'),
                                      ('uv_continue','CONTINUE')):
                        if inside(name):
                            self._set_uv_strategy(context,mode)
                            return {'RUNNING_MODAL'}
                    if inside('options_find'):
                        try:self._find_direction_options(context)
                        except (ValueError,RuntimeError,ReferenceError) as exc:
                            self.report({'WARNING'},str(exc)[:175])
                        return {'RUNNING_MODAL'}
                    if inside('collapse'):
                        self._collapsed=True
                        self._view_open=False
                        record_action('hud_collapse','Brick Hole')
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    if inside('view'):
                        self._view_open=not self._view_open
                        record_action('hud_view_toggle',f'Brick Hole; open={self._view_open}')
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    for key in ('source','wire','fill','handles','labels'):
                        if inside('view_'+key):
                            attr='brick_hole_show_'+key
                            setattr(context.scene,attr,not getattr(context.scene,attr))
                            record_action('preview_visibility',f'{key}={getattr(context.scene,attr)}')
                            self._redraw()
                            return {'RUNNING_MODAL'}
                    if self._view_open:
                        if inside('view_menu'):
                            return {'RUNNING_MODAL'}
                        self._view_open=False
                        record_action('hud_view_close','Brick Hole; outside dropdown')
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    if inside('vertex_snap'):
                        self._toggle_snap(context)
                        return {'RUNNING_MODAL'}
                    if inside('mode_face'):
                        self._picking_face=True
                        self._picked_face_hover=None
                        self._pick_bvh=None
                        self._redraw()
                        return {'RUNNING_MODAL'}
                    for key,mode in (('mode_auto','AUTO'),('mode_global','GLOBAL')):
                        if inside(key):
                            try:
                                if mode=='GLOBAL':
                                    from .direction_snap_blender import available_axes
                                    choices=available_axes(context)
                                    chosen=context.scene.brick_hole_global_axis
                                    valid=[axis for axis in 'XYZ' if choices[axis][0]]
                                    if not valid:
                                        raise ValueError('Нет доступных мировых осей для текущего выделения')
                                    if chosen not in valid:chosen=valid[0]
                                    self._set_normal_source(context,'GLOBAL',
                                        {'X':(1.,0.,0.),'Y':(0.,1.,0.),'Z':(0.,0.,1.)}[chosen])
                                    context.scene.brick_hole_global_axis=chosen
                                else:
                                    self._set_normal_source(context,mode)
                            except (ValueError,RuntimeError,ReferenceError) as exc:
                                self.report({'WARNING'},str(exc)[:175])
                            return {'RUNNING_MODAL'}
                    for axis in 'XYZ':
                        if inside('axis_'+axis.lower()):
                            try:self._select_manual_axis(context,axis)
                            except (ValueError,RuntimeError,ReferenceError) as exc:
                                self.report({'WARNING'},str(exc)[:175])
                            return {'RUNNING_MODAL'}
                if layout:
                    from ...core.numeric_spinner import step_metre_text
                    for direction in ('minus','plus'):
                        box=layout['depth_'+direction]
                        if box[0] <= pos[0] <= box[2] and box[1] <= pos[1] <= box[3]:
                            if self._editing_numeric:
                                try:self._numeric_commit()
                                except ValueError as exc:
                                    self.report({'WARNING'},str(exc))
                                    return {'RUNNING_MODAL'}
                            try:
                                stepped=step_metre_text(str(self._signed_depth()),
                                    1 if direction=='plus' else -1,
                                    signed=True,minimum=None,maximum='50')
                                signed_step=interactive_numeric_value(stepped)
                                self._depth=abs(signed_step)
                                self._direction='INWARD' if signed_step<0 else 'OUTWARD'
                            except ValueError as exc:
                                self.report({'WARNING'},str(exc))
                                return {'RUNNING_MODAL'}
                            self._remember_parameter()
                            record_action('interactive_depth_step',f'button={direction};signed_depth_m={self._signed_depth():.6f}')
                            self._redraw()
                            return {'RUNNING_MODAL'}
                if layout and (layout['value_hit'][0] <= pos[0] <= layout['value_hit'][2] and
                               layout['value_hit'][1] <= pos[1] <= layout['value_hit'][3]):
                    self._numeric_start()
                    return {'RUNNING_MODAL'}
                apply_box,cancel_box = self._button_regions()
                if apply_box[0] <= pos[0] <= apply_box[2] and apply_box[1] <= pos[1] <= apply_box[3]:
                    return self._apply(context)
                if cancel_box[0] <= pos[0] <= cancel_box[2] and cancel_box[1] <= pos[1] <= cancel_box[3]:
                    self._close(context,'CANCELLED · модель не изменена')
                    return {'CANCELLED'}
                hit=self._hit_handle(pos)
                if hit is not None:
                    self._dragging=True
                    self._editing_numeric=False
                    self._numeric=''
                    self._drag_begin=pos
                    self._drag_start_depth=self._signed_depth()
                    origin,normal=hit
                    self._drag_origin=tuple(origin)
                    self._drag_normal=tuple(normal)
                    self._snap_hit=None
                    self._snap_rejected=None
                    if (self._snap_enabled and not self._snap_index_disabled and
                            (self._snap_grid is None or
                             self._snap_index_key!=self._snap_view_key(context))):
                        self._cache_snap_vertices(context)
                    origin_xy=self._screen_xyz(origin)
                    dest_xy=self._screen_xyz(tuple(origin[i]+normal[i] for i in range(3)))
                    self._drag_axis=((dest_xy[0]-origin_xy[0],dest_xy[1]-origin_xy[1])
                                     if origin_xy and dest_xy else (0.,0.))
                    record_action('interactive_drag_start',f'signed_depth={self._drag_start_depth:.6f}')
                    return {'RUNNING_MODAL'}
                self._hover_region=self._hit_surface(pos)
                self._redraw()
                # A click outside control is a normal Blender click; edits to
                # selection will invalidate the snapshot on next TIMER event.
                return {'PASS_THROUGH'}
            if event.value == 'RELEASE' and self._dragging:
                self._dragging = False
                self._remember_parameter()
                record_action('interactive_drag_end',f'depth={self._signed_depth():.6f}')
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE':
                return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE' and not self._dragging:
            next_hover=self._hit_surface(self._mouse_pos(event))
            if next_hover != self._hover_region:
                self._hover_region=next_hover
                record_action('interactive_hover_region',f'index={next_hover}')
                self._redraw()
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE' and self._dragging:
            snap_state=self._snap_drag(context,self._mouse_pos(event))
            if snap_state in {'HIT','BLOCKED'}:
                self._redraw()
                return {'RUNNING_MODAL'}
            sensitivity=.1 if bool(getattr(event,'shift',False)) else 1.
            signed = self._drag_start_depth + sensitivity*interactive_mouse_delta(
                self._drag_begin,self._mouse_pos(event),self._drag_axis)
            self._direction = 'INWARD' if signed < 0 else 'OUTWARD'
            self._depth = min(50.,max(.001,abs(signed)))
            self._redraw()
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._close(context,'CANCELLED · модель не изменена')


class ED_OT_brick_hole_undo_last(bpy.types.Operator):
    bl_idname = "ed.brick_hole_undo_last"
    bl_label = "Отменить последний результат"

    def execute(self, context):
        self.report(
            {'WARNING'},
            "Используйте Ctrl+Z сразу после операции. Аддон не отменяет чужие действия автоматически",
        )
        return {'CANCELLED'}



class MESH_OT_brick_hole_corner_analyze(bpy.types.Operator):
    bl_idname='mesh.brick_hole_corner_analyze'
    bl_label='BH-04A · Анализ угла (без изменений Mesh)'
    bl_description=('Найти контакты двух соседних новых поверхностей и потенциальную линию разреза; '
                    'НЕ выполняет Trim, НЕ меняет Mesh/UV')
    bl_options={'REGISTER','INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode=='EDIT_MESH' and context.edit_object is not None and context.edit_object.type=='MESH'

    def execute(self,context):
        from .corner_trim_oracle import analyze_corner_pair
        obj=context.edit_object
        try:
            bm=bmesh.from_edit_mesh(obj.data)
            edges=[e for e in bm.edges if e.select and not e.hide]
            if len(edges)!=2:
                raise ValueError('BH-04A: выберите ровно два соседних ребра')
            if len(set(edges[0].verts)&set(edges[1].verts))!=1:
                raise ValueError('BH-04A: выбранные рёбра должны иметь одну общую вершину')
            depth=scene_signed_depth(context.scene)
            normals, dirs=edge_local_frame(obj,edges,context.scene,depth,
                                           validate_shape=False,bm=bm)
            strips=[]
            for e in edges:
                v0,v1=e.verts
                p=tuple(float(x) for x in obj.matrix_world@v0.co)
                q=tuple(float(x) for x in obj.matrix_world@v1.co)
                r=tuple(q[k]+float(dirs[v1][k])*depth for k in range(3))
                t=tuple(p[k]+float(dirs[v0][k])*depth for k in range(3))
                strips.append({'edge':tuple(e.verts),'quad':(p,q,r,t)})
            outcome=analyze_corner_pair(*strips)
        except (ValueError, RuntimeError, ReferenceError) as exc:
            outcome={'schema':'issmaker.bh04a.corner_witness.v1',
                     'mode':'READ_ONLY_NOT_APPLY','status':'BLOCK_INPUT',
                     'reason':str(exc)}
        item={'at_utc':datetime.now(timezone.utc).isoformat(),
              'object':obj.name,**outcome}
        _CORNER_TRIM_WITNESSES.append(item)
        record_action('bh04a_corner_analyze',
                      f"status={item['status']}; reason={item['reason'][:130]}")
        context.scene.brick_hole_corner_analysis_status=(
            item['status']+': '+item['reason'])[:250]
        # No mesh or UV changes are allowed in this operator.
        if item['status'].startswith('BLOCK'):
            self.report({'WARNING'},context.scene.brick_hole_corner_analysis_status[:200])
        else:
            self.report({'INFO'},context.scene.brick_hole_corner_analysis_status[:200])
        return {'FINISHED'}


class MESH_OT_brick_hole_corner_miter_build(bpy.types.Operator):
    """BH04B: real welded miter junction for two compatible boundary edges.

    NOT a Boolean. Never cuts an authored/source face. Ambiguous corners are
    refused, retaining the BH04A read-only Oracle as the safe alternative.
    """
    bl_idname='mesh.brick_hole_corner_miter_build'
    bl_label='BH-04B · Построить безопасное сопряжение'
    bl_description=('Реально построить только геометрически проверенный miter двух '
                    'смежных граничных рёбер. Требует исходный Mesh с UV. '
                    'Не решает произвольные самопересечения/Boolean')
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        return (context.mode=='EDIT_MESH' and context.edit_object is not None
                and context.edit_object.type=='MESH')

    @protected_operation
    def execute(self,context):
        from .corner_miter_builder import plan_two_edge_miter
        from .corner_trim_oracle import analyze_corner_pair
        from .uv_orientation_policy import outward_from_source_uv
        obj=context.edit_object
        bm=bmesh.from_edit_mesh(obj.data)
        scene=context.scene
        if _brick_hole_uv_legacy_conflict(scene):
            raise ValueError('Старые режимы Edge/Bridge конфликтуют; выберите SMART UV')
        if brick_hole_uv_policy(scene)!='STRICT':
            raise ValueError('Miter: для ADAPTIVE/CONTINUE пока нет подтверждённого UV-построения. Выберите 1000')
        # Single selection mode, with only boundary edges selected. No face
        # changes before we have a completely verified plan.
        faces,edges,_=partition_brick_selection(bm,context.tool_settings.mesh_select_mode)
        if faces or len(edges)!=2 or any(len(e.link_faces)!=1 for e in edges):
            raise ValueError('BH-04B: выберите ровно два смежных ГРАНИЧНЫХ ребра в Edge Mode')
        shared_verts=set(edges[0].verts)&set(edges[1].verts)
        if len(shared_verts)!=1:
            raise ValueError('BH-04B: рёбра должны иметь одну общую сваренную вершину')
        shared=next(iter(shared_verts))
        ends=[next(v for v in e.verts if v is not shared) for e in edges]
        signed_depth=scene_signed_depth(scene)
        # Full production collision guard on the same original state; no
        # relaxation for an attempted Smart Corner repair.
        normals,offsets=edge_local_frame(obj,edges,scene,signed_depth,
                                         validate_shape=True,bm=bm)
        uv_name=scene.brick_hole_uv_map
        uv_layer=(bm.loops.layers.uv.get(uv_name) if uv_name else bm.loops.layers.uv.active)
        if uv_layer is None:raise ValueError('BH-04B: на исходной стене нет UV-карты')
        hosts=[e.link_faces[0] for e in edges]
        # Capture scalar source attributes while every BMFace is provably valid.
        # A retained BMFace handle must not be dereferenced after topology edits.
        host_material_indices=tuple(int(f.material_index) for f in hosts)
        uv_pairs=[];reversed_flags=[]
        for edge,far,host in zip(edges,ends,hosts):
            lo_shared=next((l for l in host.loops if l.vert is shared),None)
            lo_far=next((l for l in host.loops if l.vert is far),None)
            if lo_shared is None or lo_far is None:
                raise ValueError('BH-04B: исходная грань не содержит оба UV-угла')
            direct=any(l.vert is shared and l.link_loop_next.vert is far
                       for l in host.loops)
            reverse=any(l.vert is far and l.link_loop_next.vert is shared
                        for l in host.loops)
            if direct==reverse:
                raise ValueError('BH-04B: выбранное ребро неоднозначно в исходных face-loops')
            uv_s=tuple(lo_shared[uv_layer].uv)
            uv_far=tuple(lo_far[uv_layer].uv)
            out=outward_from_source_uv(uv_s,uv_far,
                                       [tuple(l[uv_layer].uv) for l in host.loops], direct)
            # The two source faces may have different UV winding. Create only
            # consistently positive, non-mirrored NEW triangles; never change
            # the authored host UV loops. Flipping the NEW transverse axis is
            # explicit in the Corner Witness and may introduce an UV seam.
            uv_edge=(uv_far[0]-uv_s[0],uv_far[1]-uv_s[1])
            determinant=uv_edge[0]*out[1]-uv_edge[1]*out[0]
            if abs(determinant)<1e-10:
                raise ValueError('BH-04B: исходный UV ориентирован неоднозначно')
            flip_new_uv=(determinant<0) != bool(direct)
            if flip_new_uv:out=(-out[0],-out[1])
            uv_pairs.append((uv_s,uv_far,out));reversed_flags.append(direct)
        s_world=tuple(float(v) for v in obj.matrix_world@shared.co)
        far_xyz=[tuple(float(v) for v in obj.matrix_world@v.co) for v in ends]
        plan=plan_two_edge_miter(
            s_world,*far_xyz,tuple(normals[edges[0]]),tuple(normals[edges[1]]),signed_depth,
            *uv_pairs[0],*uv_pairs[1],resolution=int(resolution_value(scene)),
            target_ppm=PIXELS_PER_METER,
            uv_safe_min=UV_SAFE_MIN,uv_safe_max=UV_SAFE_MAX)
        expected=tuple(s_world[k]+float(offsets[shared][k])*signed_depth for k in range(3))
        if math.dist(plan.seam_end,expected)>1e-5:
            raise ValueError('BH-04B: независимый miter не совпадает с производственным направлением')
        record=analyze_corner_pair(
            # Oracle's quad order is (S, FAR, MOVED_FAR, MITER). BMEdge.verts
            # ordering is NOT guaranteed to be (S, FAR) in Blender; passing
            # it raw makes the oracle compare the wrong seam and reject a
            # valid corner with BLOCK_OFFSET_MISMATCH.
            {'edge':(shared,ends[0]),'quad':plan.polygons[0]},
            {'edge':(shared,ends[1]),'quad':plan.polygons[1]})
        if record['status']!='SAFE_SHARED_SEAM':
            raise ValueError('BH-04B: Corner Oracle запретил сопряжение: '+record['status'])
        # Immutable authored faces and UV loops. The transaction decorator
        # also owns full working-mesh rollback on every failure.
        from .source_face_ledger import (capture_source_face_ledger,
                                         verify_source_face_ledger)
        # All authored UV maps, not just the active one. Freeze values BEFORE
        # topology edits; Blender may invalidate old BMFace Python wrappers.
        uv_layers_for_guard=tuple((name,bm.loops.layers.uv.get(name))
                                  for name in sorted(bm.loops.layers.uv.keys()))
        if not uv_layers_for_guard:
            raise ValueError('BH-04B: source UV maps unavailable for integrity guard')
        source_ledger=capture_source_face_ledger(bm,hosts,uv_layers_for_guard)
        inverse=obj.matrix_world.inverted_safe()
        if abs(obj.matrix_world.to_3x3().determinant())<1e-10:
            raise ValueError('BH-04B: объект имеет вырожденную матрицу масштабирования')
        to_local=lambda p: inverse@Vector(p)
        meshverts={'S':shared,'A':ends[0],'B':ends[1],
                   'AP':bm.verts.new(to_local(plan.outer_points[0])),
                   'BP':bm.verts.new(to_local(plan.outer_points[1])),
                   'Q':bm.verts.new(to_local(plan.seam_mid)),
                   'M':bm.verts.new(to_local(plan.seam_end))}
        tag=bm.faces.layers.int.get(RESULT_LAYER_NAME)
        if tag is None:tag=bm.faces.layers.int.new(RESULT_LAYER_NAME)
        added=[]
        for side in range(2):
            for tri_names in plan.triangle_names[side]:
                # Reverse source-edge face orientation to prevent both adjacent
                # faces traversing the same source boundary in the same sense.
                directed=tuple(reversed(tri_names)) if reversed_flags[side] else tri_names
                face=bm.faces.new([meshverts[name] for name in directed])
                face.material_index=host_material_indices[side]
                face[tag]=1
                for loop,name in zip(face.loops,directed):
                    loop[uv_layer].uv=plan.uv_faces[side][name]
                added.append(face)
        # Strong topology assertions on the actual BMesh: shared seam is welded,
        # reverse-wound in adjacent new triangles and has no T-junction.
        for va,vb in ((meshverts['S'],meshverts['Q']),
                      (meshverts['Q'],meshverts['M'])):
            e=bm.edges.get((va,vb))
            if e is None or len(e.link_faces)!=2 or any(f not in added for f in e.link_faces):
                raise ValueError('BH-04B: новая общая кромка не является сваренным manifold-швом')
            oriented=[]
            for face in e.link_faces:
                oriented.append(next((l.vert is va) for l in face.loops if l.edge is e))
            if oriented!=[True,False] and oriented!=[False,True]:
                raise ValueError('BH-04B: противоположная ориентация новых граней не доказана')
        for e in edges:
            if len(e.link_faces)!=2:
                raise ValueError('BH-04B: исходное граничное ребро не соединено с новой поверхностью')
        # Validate authored faces by current BMesh CONTENT, not by stale
        # Python BMFace wrappers retained through the topology mutation.
        # Missing/reoriented/material-changed/UV-edited sources still fail.
        source_check=verify_source_face_ledger(bm,source_ledger)
        write_log('BH04B5_SOURCE_LEDGER_VERIFIED: '+repr(source_check))
        for f in added:
            if f.calc_area()<=1e-10:
                raise ValueError('BH-04B: новая грань вырождена')
            face_uv_density_gate(f,uv_layer,obj.matrix_world,int(resolution_value(scene)))
        bm.normal_update()
        bmesh.update_edit_mesh(obj.data,loop_triangles=True,destructive=True)
        write_log('BH04B4_BMFACE_SNAPSHOT: Corner Miter source material indices captured pre-mutation')
        _build_stage('BH04B_MITER_COMMIT',
                     f'new_faces=6; welded_new_seam=2; ppm=1000; '
                     f'source_uv_locked=YES; source_ppm={plan.source_ppm}; '
                     f'uv_seam_gap_px={plan.seam_gap_px:.3f}')
        record_action('bh04b_corner_miter_build',
                      f'faces=6; miter_factor={plan.max_miter_factor:.5f}; '
                      f'uv_gap_px={plan.seam_gap_px:.3f}; source_uv_unchanged=YES')
        # Persist evidence even when Source UV seam is not physically continuous.
        _CORNER_TRIM_WITNESSES.append({
            'schema':'issmaker.bh04b.corner_build.v1',
            'at_utc':datetime.now(timezone.utc).isoformat(),'object':obj.name,
            'status':'BUILT_MITER_SAFE_SUBSET_NOT_BOOLEAN',
            'depth_m':signed_depth,'new_faces':6,
            'new_seam_xyz':[plan.seam_start,plan.seam_mid,plan.seam_end],
            'original_source_density_px_m':plan.source_ppm,
            'new_density_target_px_m':PIXELS_PER_METER,
            'authored_uv_unchanged':True,
            'uv_seam_gap_px':plan.seam_gap_px,'uv_seam_warning':plan.seam_warning,
            'new_uv_positive_winding':True})
        if plan.seam_warning:
            self.report({'WARNING'},'BH-04B: стык построен, но возможен UV-шов '+
                        f'{plan.seam_gap_px:.1f} px; исходные UV сохранены')
        else:
            self.report({'INFO'},'BH-04B: сваренный miter, 6 новых граней, UV 1000 px/м')
        return {'FINISHED'}


class VIEW3D_PT_brick_hole_extrude(bpy.types.Panel):
    bl_label = "Brick Hole"
    bl_idname = "VIEW3D_PT_brick_hole_extrude"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Brick Hole"

    def draw(self, context):
        layout=self.layout
        ready=(context.mode=='EDIT_MESH' and context.edit_object is not None
               and context.edit_object.type=='MESH')
        faces=edges=0
        select_error=''
        if ready:
            try:
                selected_faces,selected_edges,_=partition_brick_selection(
                    bmesh.from_edit_mesh(context.edit_object.data),
                    context.tool_settings.mesh_select_mode)
                faces,edges=len(selected_faces),len(selected_edges)
            except (ReferenceError,RuntimeError,ValueError) as exc:
                ready=False
                select_error=str(exc)
        if select_error:
            layout.label(text=select_error[:95],icon='ERROR')
        label=(f'{faces} граней' if faces else f'{edges} рёбер' if ready else 'Выберите режим 2 (рёбра) или 3 (грани)')
        layout.label(text=label,icon='CHECKMARK' if ready and (faces or edges) else 'INFO')
        scan=layout.box()
        scan.label(text='UV-ШВЫ · SMART INSPECTOR',icon='UV')
        scan.operator('view3d.brick_hole_seam_scan',
                      text='UV Seam Inspector · найти швы',icon='VIEWZOOM')
        scan.label(text='Красные / жёлтые · 1 / 2 — фильтры',icon='INFO')
        scan.label(text='← → навигация · F фокус · Esc выход',icon='INFO')
        layout.prop(context.scene,'brick_hole_uv_policy',text='SMART UV')
        if _brick_hole_uv_legacy_conflict(context.scene):
            layout.label(text='Старые Edge/Bridge UV · подтвердите режим',icon='ERROR')
            layout.operator(MESH_OT_brick_hole_confirm_uv_policy.bl_idname,
                            text='Подтвердить UV-режим',icon='CHECKMARK')
        if brick_hole_uv_policy(context.scene)=='ADAPTIVE':
            layout.prop(context.scene,'brick_hole_uv_transition_m',text='Переход, м')
        if faces and brick_hole_uv_policy(context.scene)!='STRICT':
            layout.label(text='Face: выбранный UV режим НЕ поддерживается',icon='ERROR')
        elif edges>2 and brick_hole_uv_policy(context.scene)!='STRICT':
            layout.label(text=f'Edge: Smart UV пока для 1–2 рёбер; выделено {edges}',icon='ERROR')
        layout.prop(context.scene, 'brick_hole_depth', text='Точная глубина, м')
        guard_row=layout.row(align=True)
        guard_row.alert=not collision_guard_enabled(context.scene)
        guard_row.prop(context.scene,'brick_hole_check_intersections',
                       text='Защита от пересечений')
        if not collision_guard_enabled(context.scene):
            layout.label(text='ПРОВЕРКА КОЛЛИЗИЙ ВЫКЛ · возможны наложения',icon='ERROR')
        layout.prop(context.scene, 'brick_hole_direction', text='Внутрь / наружу')
        if ready and edges:
            safe_box=layout.box()
            safe_box.label(text='SAFE DEPTH · Edge · до 512 рёбер',icon='LOCKED')
            row=safe_box.row(align=True)
            row.enabled=collision_guard_enabled(context.scene)
            row.operator('mesh.brick_hole_safe_depth',text='Найти',icon='VIEWZOOM').action='FIND'
            row.operator('mesh.brick_hole_safe_depth',text='Использовать',icon='CHECKMARK').action='USE'
            safe_box.label(text=context.scene.brick_hole_safe_depth_status[:90],icon='INFO')
        if ready and (faces or edges):
            if faces:
                layout.label(text='AUTO: Blender Along Normals + Even Offset', icon='INFO')
                layout.label(text='Ручной XYZ: Blender Extrude Region + Move', icon='INFO')
            quick=layout.row(align=True)
            quick.label(text='Направление:')
            quick.prop(context.scene, 'brick_hole_direction_source', text='')
            axes=layout.row(align=True)
            axes.label(text='Мировая ось:')
            for axis in ('X','Y','Z'):
                axes.operator('mesh.brick_hole_set_manual_axis',text=axis).axis=axis
            if context.scene.brick_hole_direction_source in {'GLOBAL','FACE','MANUAL'}:
                layout.prop(context.scene, 'brick_hole_manual_normal', text='Вектор XYZ')
        col=layout.column(align=True)
        col.enabled=ready and bool(faces or edges)
        button=col.row()
        button.scale_y=1.7
        button.operator(VIEW3D_OT_brick_hole_interactive.bl_idname,
                        text='НАСТРОИТЬ В VIEWPORT',icon='GIZMO')
        row=col.row(align=True)
        row.operator(MESH_OT_brick_hole_uv_policy_apply.bl_idname,
                     text='Создать · UV-режим',icon='MOD_SOLIDIFY')
        preview_row=row
        preview_row.operator(MESH_OT_brick_hole_preview_unified.bl_idname,
                     text='Проверить',icon='VIEWZOOM')
        if brick_hole_uv_policy(context.scene)!='STRICT':
            layout.label(text='UV Preview схематичен · точность НЕ подтверждена',icon='ERROR')
        # BH-02: expose the existing verified batch EDGE algorithm by its
        # actual task name. Two wall edges -> two strips, NOT Bridge Cap.
        if ready and edges:
            edge_box=layout.box()
            edge_box.label(text='BH-02 · Действия с рёбрами',icon='EDGESEL')
            corner_box=edge_box.box()
            corner_box.label(text='BH-04A · Smart Corner · анализ',icon='VIEWZOOM')
            corner_run=corner_box.row()
            corner_run.enabled=(edges==2)
            corner_run.operator(MESH_OT_brick_hole_corner_analyze.bl_idname,
                               text='ПРОВЕРИТЬ УГОЛ · БЕЗ APPLY',icon='VIEWZOOM')
            corner_box.label(text=context.scene.brick_hole_corner_analysis_status[:95],icon='INFO')
            corner_box.label(text='Разрез не применяется; результат в ZIP',icon='LOCKED')
            build=corner_box.row()
            build.enabled=(edges==2 and ready and brick_hole_uv_policy(context.scene)=='STRICT')
            build.operator(MESH_OT_brick_hole_corner_miter_build.bl_idname,
                           text='BH-04B · СОЗДАТЬ MITER (ТЕСТ)',icon='MOD_BEVEL')
            corner_box.label(text='Только два ребра; не Boolean / не все углы',icon='INFO')
            edge_box.label(text='Edge Extend: отдельная полоса от каждой цепочки')
            run=edge_box.row()
            run.scale_y=1.25
            run.operator(MESH_OT_brick_hole_uv_policy_apply.bl_idname,
                         text=f'EDGE · SMART UV · {edges} рёбер',icon='MOD_SOLIDIFY')
            edge_smart=edge_box.box()
            edge_smart.label(text='BH-03.1 · Smart UV для EDGE',icon='UV')
            edge_smart.label(text='UV-стратегия — вверху и в Viewport')
            edge_mode=brick_hole_uv_policy(context.scene)
            if edge_mode=='STRICT':
                edge_smart.label(text='STRICT: используйте EDGE EXTEND выше',icon='INFO')
            else:
                if edge_mode=='ADAPTIVE':
                    edge_smart.prop(context.scene,'brick_hole_uv_transition_m',text='Переход, м')
                edge_smart.label(text='Поддержка: 1–2 независимых прямых граничных ребра')
                edge_smart.label(text='Один UV-тайл; сложные углы и змейки — BH-04',icon='INFO')
                smart_run=edge_smart.row()
                smart_run.enabled=(1<=edges<=2)
                smart_run.operator(MESH_OT_brick_hole_edge_smart_uv.bl_idname,
                    text='EDGE SMART UV · применить',icon='MOD_SOLIDIFY')
                edge_smart.label(text='Preview НЕ точный · BH-05',icon='ERROR')
            bridge=edge_box.row()
            bridge.scale_y=1.15
            # Bridge Cap never duplicates an existing wall face and supports
            # only two disconnected boundary edges at this stage.
            bridge_ok=False
            bridge_reason=''
            if edges==2:
                try:
                    selected=[e for e in bmesh.from_edit_mesh(
                        context.edit_object.data).edges if e.select and not e.hide]
                    bridge_ok=(len(selected)==2 and
                        all(len(e.link_faces)==1 for e in selected) and
                        not (set(selected[0].verts)&set(selected[1].verts)) and
                        not (set(selected[0].link_faces)&set(selected[1].link_faces)))
                    if bridge_ok:
                        # Use the exact directed source-loop order as Apply;
                        # unordered BMesh edge.verts may reverse the UV field.
                        first,second=selected
                        host=first.link_faces[0]
                        host_loop=next((l for l in host.loops if l.edge is first),None)
                        if host_loop is None:
                            raise ValueError('Bridge: исходный UV-loop не найден')
                        v0,v1=host_loop.vert,host_loop.link_loop_next.vert
                        w0,w1=second.verts
                        geometry_plan=plan_single_bridge(
                            tuple(context.edit_object.matrix_world @ v0.co),
                            tuple(context.edit_object.matrix_world @ v1.co),
                            tuple(context.edit_object.matrix_world @ w0.co),
                            tuple(context.edit_object.matrix_world @ w1.co),
                            resolution=int(resolution_value(context.scene)),
                            ppm=PIXELS_PER_METER,safe_fraction=UV_USABLE_SPAN)
                        if brick_hole_uv_policy(context.scene)!='STRICT':
                            from .uv_orientation_policy import outward_from_source_uv
                            if geometry_plan.reversed_b:w0,w1=w1,w0
                            name=context.scene.brick_hole_uv_map
                            uv_layer=(bmesh.from_edit_mesh(context.edit_object.data).loops.layers.uv.get(name)
                                      if name else bmesh.from_edit_mesh(context.edit_object.data).loops.layers.uv.active)
                            if uv_layer is None:
                                raise ValueError('Bridge: UV-карта не найдена')
                            lu0=next(l for l in host.loops if l.vert is v0)
                            lu1=next(l for l in host.loops if l.vert is v1)
                            forward=(lu0.link_loop_next.vert is v1)
                            backward=(lu1.link_loop_next.vert is v0)
                            if forward==backward:
                                raise ValueError('Bridge: UV-ориентация неоднозначна')
                            authored0=tuple(lu0[uv_layer].uv)
                            authored1=tuple(lu1[uv_layer].uv)
                            outward=outward_from_source_uv(
                                authored0,authored1,
                                [tuple(l[uv_layer].uv) for l in host.loops],forward)
                            plan_uv_strip(
                                tuple(context.edit_object.matrix_world @ v0.co),
                                tuple(context.edit_object.matrix_world @ v1.co),
                                tuple(context.edit_object.matrix_world @ w0.co),
                                tuple(context.edit_object.matrix_world @ w1.co),
                                authored0,authored1,outward,
                                resolution=int(resolution_value(context.scene)),
                                mode=brick_hole_uv_policy(context.scene),
                                transition_m=context.scene.brick_hole_uv_transition_m,
                                tile_min=UV_SAFE_MIN,tile_max=UV_SAFE_MAX)
                except (ReferenceError,RuntimeError,AttributeError,ValueError) as exc:
                    bridge_ok=False
                    bridge_reason=str(exc)
            bridge.enabled=bridge_ok
            bridge.operator(MESH_OT_brick_hole_bridge_cap.bl_idname,
                            text='BRIDGE CAP · соединить рёбра',icon='MESH_PLANE')
            uv_box=edge_box.box()
            uv_box.label(text='BH-03 · Smart UV для BRIDGE',icon='UV')
            uv_box.label(text='Стратегия — вверху и в Viewport')
            if brick_hole_uv_policy(context.scene)=='ADAPTIVE':
                uv_box.prop(context.scene,'brick_hole_uv_transition_m',text='Переход, м')
                uv_box.label(text='Добавляет ряды только на новой поверхности',icon='INFO')
            elif brick_hole_uv_policy(context.scene)=='CONTINUE':
                uv_box.label(text='Исходный UV сохраняется; плотность может быть не 1000',icon='INFO')
            else:
                uv_box.label(text='1000 px/м; авторский стык может не совпасть',icon='INFO')
            uv_box.label(text='Единая стратегия; EDGE поддержка ограничена',icon='INFO')
            uv_box.label(text='Второй стык BRIDGE может быть видимым',icon='INFO')
            if edges==2 and not bridge_ok:
                edge_box.label(text=(bridge_reason or 'Bridge недопустим: проверьте рёбра')[:104],icon='ERROR')
            # Read-only notice: the original authored UV remains the source
            # of truth. Do NOT claim provenance ('edited manually') or offer
            # an unavailable BH-03 UV correction as a working button.
            if edges<=8:
                try:
                    obj=context.edit_object
                    bm=bmesh.from_edit_mesh(obj.data)
                    uv_name=context.scene.brick_hole_uv_map
                    uv_layer=(bm.loops.layers.uv.get(uv_name) if uv_name
                              else bm.loops.layers.uv.active)
                    ppm_values=[]
                    if uv_layer is not None:
                        for src_edge in bm.edges:
                            if not src_edge.select or src_edge.hide or len(src_edge.link_faces)!=1:
                                continue
                            host=src_edge.link_faces[0]
                            host_loops=[loop for loop in host.loops if loop.vert in src_edge.verts]
                            if len(host_loops)!=2:
                                continue
                            length=world_distance(obj,*src_edge.verts)
                            if length>EPS:
                                ppm_values.append(math.dist(
                                    tuple(host_loops[0][uv_layer].uv),
                                    tuple(host_loops[1][uv_layer].uv)) *
                                    int(resolution_value(context.scene))/length)
                    mismatch=[v for v in ppm_values if abs(v-PIXELS_PER_METER)>5]
                    if mismatch:
                        uv_notice=edge_box.row()
                        uv_notice.alert=True
                        uv_notice.label(text=f'UV ребра {mismatch[0]:.0f} px/м (цель 1000)',icon='ERROR')
                        if bridge_ok:
                            edge_box.label(text='Рекомендация: CONTINUE — сохранить стык',icon='INFO')
                            edge_box.label(text='ADAPTIVE — возврат к 1000, если хватит места')
                        else:
                            edge_box.label(text='EDGE: STRICT или Smart UV (только 1–2 прямых ребра)')
                    # Identical source ppm is not proof of matching UV origin,
                    # gradient or the adjacent material: show no false PASS.
                except (ReferenceError,RuntimeError,AttributeError,ValueError,TypeError,ZeroDivisionError):
                    pass
            edge_box.label(text='Preview схематичен · BH-05 True Preview ещё требуется',icon='INFO')
        status=context.scene.brick_hole_interactive_state
        if status.startswith(('ERROR','STALE','BLOCKED')):
            alert=layout.row()
            alert.alert=True
            alert.label(text=status[:75],icon='ERROR')


class BRICKHOLE_PT_Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Brick Hole"
    bl_parent_id = "VIEW3D_PT_brick_hole_extrude"


class VIEW3D_PT_brick_hole_geometry(BRICKHOLE_PT_Base, bpy.types.Panel):
    bl_label = "Настройки построения"
    bl_idname = "VIEW3D_PT_brick_hole_geometry"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout=self.layout
        scene=context.scene
        layout.use_property_split=True
        layout.use_property_decorate=False
        layout.label(text='Глубина и направление — в основной панели',icon='INFO')
        obj=context.edit_object
        if obj and obj.type=='MESH':
            layout.prop_search(scene,'brick_hole_uv_map',obj.data,'uv_layers',text='UV-карта')
        else:
            layout.prop(scene,'brick_hole_uv_map',text='UV-карта')
        layout.prop(scene,'brick_hole_resolution',text='Тайл')
        layout.prop(scene,'brick_hole_material_mode',text='Материал')
        if scene.brick_hole_material_mode=='OVERRIDE':
            layout.prop(scene,'brick_hole_material_index',text='Слот')
        layout.label(text='UV Flip: отражение отключено, сохранена ориентация',icon='INFO')
        layout.label(text='Повтор разрешён · 1000 px/м',icon='CHECKMARK')


class VIEW3D_PT_brick_hole_texture(BRICKHOLE_PT_Base, bpy.types.Panel):
    bl_label = "Внешний вид"
    bl_idname = "VIEW3D_PT_brick_hole_texture"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout=self.layout
        scene=context.scene
        layout.prop(scene,'brick_hole_hud_placement',text='Панель Viewport')
        layout.prop(scene,'brick_hole_overlay_mode',text='Подсветка')
        if scene.brick_hole_overlay_mode=='ERRORS':
            layout.prop(scene,'brick_hole_overlay_labels',text='Подписи ошибок')
        layout.operator(VIEW3D_OT_brick_hole_toggle_overlay.bl_idname,
                        text='Выключить подсветку' if scene.brick_hole_show_overlay
                             else 'Включить подсветку',icon='HIDE_OFF')
        layout.label(text='Жёлтый: выбор · мятный: Preview',icon='INFO')


class VIEW3D_PT_brick_hole_preview_ui(BRICKHOLE_PT_Base, bpy.types.Panel):
    bl_label = "Предпросмотр"
    bl_idname = "VIEW3D_PT_brick_hole_preview_ui"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row(align=True)
        row.scale_y = 1.1
        row.operator(MESH_OT_brick_hole_preview.bl_idname, text="EDGE", icon='EDGESEL').face_mode = False
        row.operator(MESH_OT_brick_hole_preview.bl_idname, text="FILLED", icon='FACESEL').face_mode = True
        preview_current = False
        if scene.brick_hole_preview_verified:
            try:
                preview_current = (scene.brick_hole_preview_signature ==
                                   preview_signature(context,scene.brick_hole_preview_face_mode))
            except (ValueError, RuntimeError, AttributeError, TypeError):
                preview_current = False
        if preview_current:
            layout.label(text="Preflight OK — результат НЕ построен", icon='CHECKMARK')
        elif scene.brick_hole_preview_verified:
            alert = layout.row()
            alert.alert = True
            alert.label(text="Предпросмотр устарел — проверьте снова", icon='ERROR')
        overlay = layout.box()
        overlay.label(text="Подсветка геометрии в 3D View", icon='HIDE_OFF')
        overlay.prop(scene, 'brick_hole_overlay_mode', text='Показать')
        if scene.brick_hole_overlay_mode == 'ERRORS':
            overlay.prop(scene, 'brick_hole_overlay_labels', text='Номера и причины возле проблем')
        button = overlay.row()
        button.scale_y = 1.15
        button.operator(VIEW3D_OT_brick_hole_toggle_overlay.bl_idname,
                        text=('Выключить подсветку' if scene.brick_hole_show_overlay
                              else 'Включить подсветку'), icon='HIDE_OFF')
        if scene.brick_hole_overlay_mode == 'CONTOUR':
            overlay.label(text="Жёлтый: текущее выделение (не проверка)")
        else:
            overlay.label(text="Красный: проблемы · до 12 подписей · 4 Гц")
            if context.edit_object and context.mode == 'EDIT_MESH':
                try:
                    if len(bmesh.from_edit_mesh(context.edit_object.data).faces) > 25000:
                        warning = overlay.row()
                        warning.alert = True
                        warning.label(text="Больше 25k граней: подсветка недоступна", icon='ERROR')
                except (ValueError, RuntimeError):
                    pass
        if scene.brick_hole_overlay_error:
            alert = overlay.row()
            alert.alert = True
            alert.label(text=('GPU: ' + scene.brick_hole_overlay_error)[:95], icon='ERROR')
        overlay.label(text="X-Ray контуры · меш не меняется", icon='INFO')
        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
        grid.label(text="Контуры")
        grid.label(text=str(scene.brick_hole_preview_loops))
        grid.label(text="Рёбра")
        grid.label(text=str(scene.brick_hole_preview_edges))
        grid.label(text="UV-разрезы")
        grid.label(text=str(scene.brick_hole_preview_cuts))
        state = layout.row()
        state.alert = bool(scene.brick_hole_preview_warnings)
        label_text = ('Предпросмотр устарел — повторите проверку' if
                      scene.brick_hole_preview_verified and not preview_current else
                      scene.brick_hole_preview_text)
        state.alert = bool(scene.brick_hole_preview_warnings) or bool(scene.brick_hole_preview_verified and not preview_current)
        state.label(text=(label_text[:100] if label_text else 'Preview не запускался'),
                    icon='ERROR' if state.alert else 'INFO')
        if len(label_text) > 100:
            layout.label(text='Полная причина — в журнале диагностики', icon='TEXT')


class VIEW3D_PT_brick_hole_result_ui(BRICKHOLE_PT_Base, bpy.types.Panel):
    bl_label = "Результат"
    bl_idname = "VIEW3D_PT_brick_hole_result_ui"

    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        valid_obj = bool(context.edit_object and
                         scene.brick_hole_qa_state == 'Проверено: ' + context.edit_object.name)
        status = ('Последняя проверка: '+str(scene.brick_hole_last_errors)+' ошибок'
                  if valid_obj else 'Результат ещё не проверен / устарел')
        layout.label(text=status[:90],icon='ERROR' if valid_obj and scene.brick_hole_last_errors else 'INFO')
        row = layout.row(align=True)
        row.operator(MESH_OT_brick_hole_validate.bl_idname,
                     text='Проверить результат',icon='CHECKMARK')
        row.operator(MESH_OT_brick_hole_select_errors.bl_idname,
                     text='Выделить ошибки',icon='ERROR')
        if scene.brick_hole_last_errors > 0:
            layout.operator(VIEW3D_OT_brick_hole_focus_error.bl_idname,
                            text='Следующая проблема',icon='VIEWZOOM')
        layout.label(text='Ctrl+Z — отмена последней операции',icon='INFO')




FIXTURE_WALL_COLORS = {
    ('PLANAR','OPEN'): (.21,.42,.65,1.0),
    ('PLANAR','FILLED'): (.37,.60,.83,1.0),
    ('CURVED','OPEN'): (.12,.49,.41,1.0),
    ('CURVED','FILLED'): (.40,.73,.57,1.0),
    ('CORNER','OPEN'): (.67,.35,.17,1.0),
    ('CORNER','FILLED'): (.86,.60,.27,1.0),
}


class OBJECT_OT_brick_hole_fixture_walls(bpy.types.Operator):
    bl_idname = 'object.brick_hole_fixture_walls'
    bl_label = 'Создать 30 тестовых стен · 1 объект'
    bl_description = ('Создаёт один редактируемый Mesh: 15 открытых отверстий и '
                      '15 отверстий, закрытых полигонами; прямые, изогнутые, угловые. '
                      'UV 1000 px/м под 2K/4K. Существующие модели не изменяются')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Never leave the user's current Edit Mesh halfway through an implicit
        # mode change. Switch to Object Mode manually, then create the gallery.
        return (context.scene is not None and context.view_layer is not None
                and context.mode == 'OBJECT')

    def execute(self, context):
        old_active = context.view_layer.objects.active
        old_selected = tuple(context.selected_objects)
        created_object = None
        created_mesh = None
        created_materials = []
        try:
            if getattr(globals().get('_INTERACTIVE_SESSION'), '_active', False):
                raise RuntimeError('Закройте интерактивный Preview перед созданием тестов')
            resolution = int(context.scene.brick_hole_resolution)
            fixture = build_wall_fixture_gallery(resolution=resolution)
            evidence = validate_wall_fixture_gallery(fixture)
            if len(evidence) != 30:
                raise RuntimeError('Неполный набор: ожидалось 30 валидных стен')
            created_mesh = bpy.data.meshes.new('BH_Fixture_Walls_30_Mesh')
            created_mesh.from_pydata(fixture['vertices'], [], fixture['faces'])
            created_mesh.update(calc_edges=True)
            if (len(created_mesh.vertices) != len(fixture['vertices']) or
                    len(created_mesh.polygons) != len(fixture['faces'])):
                raise RuntimeError('Blender изменил количество вершин/граней тестовых стен')
            if any(tuple(p.vertices) != tuple(face)
                   for p, face in zip(created_mesh.polygons, fixture['faces'])):
                raise RuntimeError('Blender изменил индексы или порядок вершин граней')
            # Per-face independent tiles: no UV leaves the useful 2K/4K area,
            # and each quad retains exactly 1000 px/m in both physical axes.
            uv_name = str(context.scene.brick_hole_uv_map or 'UVMap')
            uv_layer = created_mesh.uv_layers.new(name=uv_name)
            if uv_layer is None:
                raise RuntimeError('Blender не создал UV-карту')
            for poly, expected_uv in zip(created_mesh.polygons, fixture['face_uvs']):
                if poly.loop_total != 4:
                    raise RuntimeError('Нарушен порядок UV quad')
                for j, loop_index in enumerate(poly.loop_indices):
                    uv_layer.data[loop_index].uv = expected_uv[j]
            attr = created_mesh.attributes.new('BH_fixture_case', 'INT', 'FACE')
            kind_attr = created_mesh.attributes.new('BH_fixture_filled', 'INT', 'FACE')
            # Make all six combinations visible when Solid viewport is set to
            # Color: Material; do not change the user's viewport preferences.
            pairs = [(t,k) for t in FIXTURE_WALL_TYPES for k in FIXTURE_WALL_KINDS]
            for shape,kind in pairs:
                name = f'BH Fixture · {shape} · {kind}'
                mat = bpy.data.materials.get(name)
                if mat is None:
                    mat = bpy.data.materials.new(name)
                    mat.diffuse_color = FIXTURE_WALL_COLORS[(shape,kind)]
                    created_materials.append(mat)
                created_mesh.materials.append(mat)
            cases = fixture['cases']
            for poly,case_id in zip(created_mesh.polygons,fixture['face_case_ids']):
                case=cases[case_id-1]
                attr.data[poly.index].value=case_id
                kind_attr.data[poly.index].value=int(case['kind']=='FILLED')
                poly.material_index=pairs.index((case['type'],case['kind']))
            # No extra empties, text labels, collections, or linked meshes.
            created_object = bpy.data.objects.new('BH_30_Test_Walls_OPEN_FILLED', created_mesh)
            target_collection = getattr(context, 'collection', None) or context.scene.collection
            target_collection.objects.link(created_object)
            created_object['BH_fixture_schema']=FIXTURE_WALL_SCHEMA
            created_object['BH_fixture_version']=ADDON_VERSION_STRING
            created_object['BH_fixture_expected_cases']=30
            created_object['BH_fixture_resolution']=resolution
            created_object['BH_fixture_cases_json']=json.dumps(cases, ensure_ascii=False, separators=(',',':'))
            created_object['BH_fixture_validation_json']=json.dumps(evidence, ensure_ascii=False, separators=(',',':'))
            created_object['BH_fixture_expected_fingerprint']=fixture_expected_fingerprint(fixture)
            created_object['BH_fixture_last_check']='ORIGINAL'
            created_object['BH_fixture_instruction']='Tab → Edit Mode. OPEN: рёбра (2); FILLED: грани (3). Не смешивайте режимы. Повтор разрешён.'
            created_object.location = context.scene.cursor.location.copy()
            for obj in old_selected:
                obj.select_set(False)
            created_object.select_set(True)
            context.view_layer.objects.active = created_object
            context.scene.brick_hole_fixture_index=0
            invalidate_qa(context.scene)
            record_action('fixture_walls_created', f'30 (15 OPEN + 15 FILLED), {resolution} px, single mesh')
            self.report({'INFO'},'Создан один Mesh: 15 открытых + 15 закрытых проёмов. Tab → Edit, выделяйте рёбра и грани')
            return {'FINISHED'}
        except Exception as exc:
            # The only created scene object is rolled back. Existing Mesh
            # datablocks and original selection remain untouched on failure.
            if created_object is not None:
                try:bpy.data.objects.remove(created_object, do_unlink=True)
                except Exception:pass
            if created_mesh is not None and created_mesh.users==0:
                try:bpy.data.meshes.remove(created_mesh)
                except Exception:pass
            for mat in created_materials:
                if mat.users==0:
                    try:bpy.data.materials.remove(mat)
                    except Exception:pass
            for obj in old_selected:
                try:obj.select_set(True)
                except (ReferenceError, RuntimeError):pass
            try:context.view_layer.objects.active = old_active
            except (ReferenceError,RuntimeError):pass
            record_action('fixture_walls_failed', f'{type(exc).__name__}: {exc}')
            self.report({'ERROR'},f'Не удалось создать тестовые стены: {exc}')
            return {'CANCELLED'}



# Pass 10: DWOS-style case focus + Collision Inspector-style independent navigation,
# with a strictly explicit Edit Mesh selection step. Source geometry may be freely
# edited by the user; only automated *old-index* selection is disabled if modified.
def _bh_fixture_active(context):
    obj=getattr(context,'active_object',None)
    return (obj is not None and getattr(obj,'type',None)=='MESH' and
            obj.get('BH_fixture_schema','')==FIXTURE_WALL_SCHEMA and
            getattr(context,'mode','') in ('OBJECT','EDIT_MESH'))


def _bh_fixture_source(obj):
    raw=str(obj.get('BH_fixture_cases_json',''))
    if not raw or len(raw)>40000:
        raise ValueError('Отсутствует или повреждён паспорт тестовой галереи')
    gallery=build_wall_fixture_gallery(resolution=int(obj.get('BH_fixture_resolution',0)))
    validate_manifest(json.loads(raw),gallery['cases'])
    return gallery


def _bh_fixture_actual(obj):
    """Read current Blender Mesh (including uncommitted Edit BMesh); no mutation."""
    if obj.mode=='EDIT':
        bm=bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table(); bm.faces.ensure_lookup_table()
        bm.verts.index_update(); bm.faces.index_update()
        layer=bm.faces.layers.int.get('BH_fixture_case')
        if layer is None:
            raise ValueError('Нет атрибута принадлежности полигонов тестам')
        return ([tuple(float(x) for x in v.co) for v in bm.verts],
                [tuple(int(v.index) for v in f.verts) for f in bm.faces],
                [int(f[layer]) for f in bm.faces])
    mesh=obj.data
    attr=mesh.attributes.get('BH_fixture_case')
    if attr is None or attr.domain!='FACE' or attr.data_type!='INT':
        raise ValueError('Нет атрибута принадлежности полигонов тестам')
    return ([tuple(float(x) for x in v.co) for v in mesh.vertices],
            [tuple(int(i) for i in p.vertices) for p in mesh.polygons],
            [int(item.value) for item in attr.data])


def _bh_fixture_original(obj,gallery):
    return assert_original_fixture(*_bh_fixture_actual(obj),gallery)


def _bh_fixture_focus(context,gallery,idx):
    area=getattr(context,'area',None)
    if area is None or area.type!='VIEW_3D':
        raise ValueError('Откройте инструмент в 3D Viewport')
    rv=getattr(context.space_data,'region_3d',None)
    if rv is None:
        raise ValueError('Область просмотра 3D недоступна')
    obj=context.active_object
    spec=gallery['cases'][idx]
    lo=int(spec['vertex_start']); hi=lo+int(spec['vertex_count'])
    if not 0<=lo<hi<=len(gallery['vertices']):
        raise ValueError('Повреждены границы тестового участка')
    points=[obj.matrix_world @ Vector(v) for v in gallery['vertices'][lo:hi]]
    lower=Vector(tuple(min(v[axis] for v in points) for axis in range(3)))
    upper=Vector(tuple(max(v[axis] for v in points) for axis in range(3)))
    rv.view_location=(lower+upper)*0.5
    rv.view_distance=max(3.0,(upper-lower).length*1.45)
    area.tag_redraw()
    return spec['id']


class VIEW3D_OT_brick_hole_fixture_step(bpy.types.Operator):
    bl_idname='view3d.brick_hole_fixture_step'
    bl_label='Перейти к тестовому проёму'
    bl_description='Листать 30 тестов и приближать к выбранному. Не меняет геометрию или выделение'
    bl_options={'REGISTER'}
    delta: IntProperty(name='Шаг',default=1,min=-30,max=30)

    @classmethod
    def poll(cls,context):
        return _bh_fixture_active(context) and context.area and context.area.type=='VIEW_3D'

    def execute(self,context):
        try:
            gallery=_bh_fixture_source(context.active_object)
            idx=(int(context.scene.brick_hole_fixture_index)+self.delta)%len(gallery['cases'])
            case_id=_bh_fixture_focus(context,gallery,idx)
            context.scene.brick_hole_fixture_index=idx
            record_action('fixture_focus',f'{case_id}; index={idx}; selection_unchanged=true')
            self.report({'INFO'},f'{idx+1}/30 · {case_id} · выбор не изменён')
            return {'FINISHED'}
        except (ValueError,TypeError,RuntimeError,ReferenceError,IndexError,KeyError) as exc:
            record_action('fixture_focus_blocked',str(exc)[:150])
            self.report({'WARNING'},f'Навигация: {exc}')
            return {'CANCELLED'}


class MESH_OT_brick_hole_fixture_select(bpy.types.Operator):
    bl_idname='mesh.brick_hole_fixture_select'
    bl_label='Выделить проём тестовой стены'
    bl_description=('Явно выбрать только OPEN контур или FILLED закрывающие полигоны. '
                    'Добавлять можно только проёмы того же типа. При изменении исходного Mesh выбор блокируется')
    bl_options={'REGISTER','UNDO'}
    append: BoolProperty(name='Добавить к выделению',default=False)

    @classmethod
    def poll(cls,context):
        return _bh_fixture_active(context) and context.mode=='EDIT_MESH'

    def execute(self,context):
        obj=context.active_object
        try:
            gallery=_bh_fixture_source(obj)
            _bh_fixture_original(obj,gallery)  # fail closed: never select stale indices
            idx=int(context.scene.brick_hole_fixture_index)
            target=selection_target(gallery,idx)
            bm=bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table();bm.edges.ensure_lookup_table();bm.faces.ensure_lookup_table()
            bm.verts.index_update();bm.edges.index_update();bm.faces.index_update()
            edge_map={tuple(sorted(v.index for v in e.verts)):e for e in bm.edges}
            edges=[edge_map[tuple(edge)] for edge in target['edges']]
            faces=[bm.faces[i] for i in target['faces']]
            if len(edges)!=len(target['edges']) or len(faces)!=len(target['faces']):
                raise ValueError('Не все элементы проёма существуют')
            mode_before=tuple(context.tool_settings.mesh_select_mode)
            previous=(tuple(v.select for v in bm.verts),
                      tuple(e.select for e in bm.edges),
                      tuple(f.select for f in bm.faces))
            try:
                # Append never creates mixed Edit select modes. A previous
                # selection from another domain requires a fresh selection.
                if self.append:
                    current_mode=tuple(bool(v) for v in mode_before)
                    required=(False,True,False) if edges else (False,False,True)
                    if current_mode != required:
                        raise ValueError('Нельзя добавить OPEN к FILLED: выберите один тип и начните новый выбор')
                # Pre-resolve the entire target before changing anything.
                if not self.append:
                    for f in bm.faces:f.select_set(False)
                    for e in bm.edges:e.select_set(False)
                    for v in bm.verts:v.select_set(False)
                for e in edges:e.select_set(True)
                for f in faces:f.select_set(True)
                if edges:
                    context.tool_settings.mesh_select_mode=(False,True,False)
                else:
                    context.tool_settings.mesh_select_mode=(False,False,True)
                bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
            except Exception:
                # A selection error is never allowed to leave a half-selected
                # mesh or a different mode behind; geometry remains untouched.
                for f,flag in zip(bm.faces,previous[2]):f.select_set(flag)
                for e,flag in zip(bm.edges,previous[1]):e.select_set(flag)
                for v,flag in zip(bm.verts,previous[0]):v.select_set(flag)
                context.tool_settings.mesh_select_mode=mode_before
                bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
                raise
            record_action('fixture_explicit_select',f'{target["id"]}; edges={len(edges)}; faces={len(faces)}; append={self.append}; previous_mode={mode_before}')
            self.report({'INFO'},f'{target["id"]}: {len(edges)} рёбер / {len(faces)} граней; '+('добавлено' if self.append else 'новое выделение'))
            return {'FINISHED'}
        except (ValueError,TypeError,RuntimeError,ReferenceError,IndexError,KeyError) as exc:
            record_action('fixture_select_blocked',str(exc)[:150])
            self.report({'WARNING'},f'Выделение заблокировано: {exc}')
            return {'CANCELLED'}


class OBJECT_OT_brick_hole_fixture_verify(bpy.types.Operator):
    bl_idname='object.brick_hole_fixture_verify'
    bl_label='Проверить целостность 30 тестов'
    bl_description='Сравнить текущие геометрию, топологию и ID тестов с исходным эталоном. Изменённые стены не удаляет и не восстанавливает'
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):
        return _bh_fixture_active(context)

    def execute(self,context):
        obj=context.active_object
        try:
            gallery=_bh_fixture_source(obj)
            source=fixture_expected_fingerprint(gallery)
            current=geometry_fingerprint(*_bh_fixture_actual(obj))
            original=(source==current)
            status='ORIGINAL' if original else 'MODIFIED'
            obj['BH_fixture_last_check']=status
            obj['BH_fixture_last_report_json']=json.dumps({
                'schema':'BH_FIXTURE_CHECK_V2','status':status,'count':30,
                'expected_fingerprint':source,'current_fingerprint':current,
                'auto_selection_safe':original,'geometry_restored':False,
            },separators=(',',':'))
            record_action('fixture_check',f'{status}, count=30')
            self.report({'INFO' if original else 'WARNING'},
                        '30/30 оригинальные тесты — структура не изменена' if original else
                        'Тестовый Mesh изменён: сохранён без изменений; автоматическое выделение отключено')
            return {'FINISHED'}
        except (ValueError,RuntimeError,ReferenceError,TypeError,IndexError,KeyError) as exc:
            try:obj['BH_fixture_last_check']='UNVERIFIABLE'
            except (ReferenceError,RuntimeError):pass
            record_action('fixture_check_error',str(exc)[:160])
            self.report({'ERROR'},f'Невозможно проверить тесты: {exc}')
            return {'CANCELLED'}



# Persisted across module reloads through Blender's driver_namespace. Keep the
# key declared BEFORE register/load_pre/unregister can call the cleanup hook.
_FIXTURE_NAV_REGISTRY_KEY = 'issmaker.brick_hole.fixture_navigator_handlers.v1'
_FIXTURE_NAV_HUD = None


def stop_fixture_nav_hud():
    """Idempotent shutdown, including half-initialized and old-module HUDs.

    A previous release failed at startup because the registry key was absent.
    Cleanup must not turn an incomplete previous registration into another
    NameError, or leave a draw handler attached after the owner fails to stop.
    """
    global _FIXTURE_NAV_HUD
    registry = _overlay_driver_registry()
    previous = registry.pop(_FIXTURE_NAV_REGISTRY_KEY, None) if registry is not None else None
    existing = _FIXTURE_NAV_HUD
    _FIXTURE_NAV_HUD = None
    old_owner = previous.get('owner') if isinstance(previous, dict) else None
    old_handle = previous.get('handler') if isinstance(previous, dict) else previous
    owners = []
    for owner in (existing, old_owner):
        if owner is not None and all(owner is not item for item in owners):
            owners.append(owner)
    old_owner_stopped = False
    for owner in owners:
        try:
            owner._stop()
            if owner is old_owner:
                old_owner_stopped = True
        except Exception as exc:
            # Older module versions can have broken globals inside _stop().
            # A failed cleanup must never make Blender add-on enable fail.
            try:
                record_action('fixture_nav_cleanup_error', f'{type(exc).__name__}: {exc}')
            except Exception:
                pass
    # Fallback for abandoned handlers, e.g. a module-reload from a version
    # where the old owner's _stop() raised before releasing its GPU callback.
    if old_handle is not None and not old_owner_stopped:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(old_handle, 'WINDOW')
        except (ReferenceError, RuntimeError, ValueError) as exc:
            try:
                record_action('fixture_nav_handler_cleanup_error', f'{type(exc).__name__}: {exc}')
            except Exception:
                pass


class VIEW3D_OT_brick_hole_fixture_hud(bpy.types.Operator):
    """Opt-in read-only bottom-center navigator, inspired by Collision Inspector."""
    bl_idname='view3d.brick_hole_fixture_hud'
    bl_label='Навигатор тестов во Viewport'
    bl_description=('Мини-панель со стрелками по центру внизу 3D Viewport. '
                    'Клик мимо не блокирует Blender; Esc закрывает навигатор')
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):
        return (_bh_fixture_active(context) and
                context.area and context.area.type=='VIEW_3D')

    def invoke(self,context,event):
        global _FIXTURE_NAV_HUD
        if getattr(globals().get('_INTERACTIVE_SESSION'),'_active',False):
            self.report({'WARNING'},'Сначала завершите экструзию Preview')
            return {'CANCELLED'}
        stop_fixture_nav_hud()
        try:
            _bh_fixture_source(context.active_object)
            self._area=context.area
            self._region=next(r for r in self._area.regions if r.type=='WINDOW')
            if self._safe_fixture_layout() is None:
                self.report({'WARNING'},'Расширьте окно 3D Viewport для панели навигации')
                return {'CANCELLED'}
            self._object=context.active_object
            self._obj_pointer=self._object.as_pointer()
            self._gallery=_bh_fixture_source(self._object)
            self._active=True
            self._draw_error=''
            self._handler=bpy.types.SpaceView3D.draw_handler_add(
                self._draw,(), 'WINDOW','POST_PIXEL')
            _FIXTURE_NAV_HUD=self
            registry=_overlay_driver_registry()
            if registry is not None:
                registry[_FIXTURE_NAV_REGISTRY_KEY]={'owner':self,'handler':self._handler}
            context.window_manager.modal_handler_add(self)
            self._area.tag_redraw()
            record_action('fixture_viewport_hud_open',self._object.name)
            return {'RUNNING_MODAL'}
        except (RuntimeError,ReferenceError,AttributeError,TypeError,ValueError,StopIteration) as exc:
            self._stop()
            self.report({'WARNING'},f'Навигатор не запущен: {exc}')
            return {'CANCELLED'}

    def _stop(self):
        global _FIXTURE_NAV_HUD
        self._active=False
        handler=getattr(self,'_handler',None)
        self._handler=None
        if handler is not None:
            try:bpy.types.SpaceView3D.draw_handler_remove(handler,'WINDOW')
            except (ReferenceError,ValueError,RuntimeError):pass
        try:self._area.tag_redraw()
        except (RuntimeError,ReferenceError,AttributeError):pass
        if _FIXTURE_NAV_HUD is self:_FIXTURE_NAV_HUD=None
        registry=_overlay_driver_registry()
        if registry is not None and registry.get(_FIXTURE_NAV_REGISTRY_KEY,{}).get('owner') is self:
            registry.pop(_FIXTURE_NAV_REGISTRY_KEY,None)

    def _safe_fixture_layout(self):
        """Pass 08: one reserved rectangle governs draw and click targets.

        The navigator is an informational legacy HUD previously omitted from
        the strict placement pass. Its entirety moves together with hitboxes.
        Main bottom tool strip and Blender navigation corner stay reserved.
        """
        base=fixture_hud_layout(self._region.width,self._region.height)
        if base is None:
            return None
        from ...core.window_manager import place_unobstructed
        from ...core.window_visibility import scope_from_context
        vw,vh=self._region.width,self._region.height
        x0,y0,x1,y1=base['panel']
        reserved=(
            (max(0.,vw-98.),max(0.,vh-98.),float(vw),float(vh)),
            (max(0.,vw/2.-170.),0.,min(float(vw),vw/2.+170.),30.),
        )
        rect=place_unobstructed((vw,vh),'BRICK_FIXTURE_NAV',
             (x1-x0,y1-y0),(x0,y0),blockers=reserved,margin=8.,gap=6.,
             scope=scope_from_context(
                              bpy.context,area=self._area,region=self._region))
        if rect is None:
            return None
        dx,dy=rect[0]-x0,rect[1]-y0
        return {key:((v[0]+dx,v[1]+dy,v[2]+dx,v[3]+dy) if len(v)==4
                     else (v[0]+dx,v[1]+dy)) for key,v in base.items()}

    def _draw(self):
        if not getattr(self,'_active',False):return
        try:
            if bpy.context.area!=self._area:return
            import blf
            import gpu
            from gpu_extras.batch import batch_for_shader
            layout=self._safe_fixture_layout()
            if layout is None:return
            from ...core import viewport_skin as _skin
            from ...core import theme_engine as _theme
            from ...core.studio_gpu_components import draw_component
            from ...core.design_tokens import fit_label
            item=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
            appearance=_theme.from_preferences(item.preferences if item else None)
            shader=gpu.shader.from_builtin('UNIFORM_COLOR')
            state=gpu.state
            previous=state.blend_get() if hasattr(state,'blend_get') else 'NONE'
            try:
                state.blend_set('ALPHA')
                for key in ('panel','left','right','select'):
                    x1,y1,x2,y2=layout[key]
                    part='card' if key=='panel' else 'button'
                    role='primary' if key=='select' else 'neutral'
                    surface=_skin.component(part,'normal',role,appearance)
                    if not draw_component((x1,y1,x2,y2),part=part,state='normal',role=role,
                                          theme=appearance,scale=1.):
                        tris=((x1,y1),(x2,y1),(x2,y2),(x1,y1),(x2,y2),(x1,y2))
                        batch=batch_for_shader(shader,'TRIS',{'pos':tris})
                        shader.bind();shader.uniform_float('color',surface.background);batch.draw(shader)
            finally:
                state.blend_set(previous)
            idx=max(0,min(29,int(bpy.context.scene.brick_hole_fixture_index)))
            spec=self._gallery['cases'][idx]
            font=0
            blf.size(font,_skin.text_px('body'))
            def label(x,y,msg,color=None,max_width=None):
                value=str(msg)
                if max_width is not None:
                    value,_=fit_label(value,max_width,lambda sample:blf.dimensions(font,sample)[0])
                if not value:return
                blf.color(font,*(color or _skin.ink(appearance)))
                blf.position(font,x,y,0)
                blf.draw(font,value)
            button_ink=_skin.control_ink(appearance,part='button',state='normal')
            primary_ink=_skin.control_ink(appearance,part='button',state='normal',role='primary')
            x,y=layout['left'][:2];label(x+12,y+12,'‹',button_ink)
            x,y=layout['right'][:2];label(x+13,y+12,'›',button_ink)
            x,y=layout['select'][:2];label(x+8,y+12,'SELECT' if self._object.mode=='EDIT' else 'TAB',primary_ink,max_width=67)
            x,y=layout['title'];label(x,y,f'{idx+1:02}/30  {spec["id"]}',max_width=max(0,layout['select'][0]-x-8))
            x,y=layout['hint'];label(x,y,f'{spec["type"]}  /  {spec["kind"]}',
                _skin.ink(appearance,kind='muted'),max_width=max(0,layout['select'][0]-x-8))
        except Exception as exc:
            self._draw_error=f'{type(exc).__name__}: {exc}'[:160]

    def modal(self,context,event):
        if not getattr(self,'_active',False):return {'CANCELLED'}
        try:
            if (context.active_object is None or
                context.active_object.as_pointer()!=self._obj_pointer or
                not _bh_fixture_active(context) or
                getattr(globals().get('_INTERACTIVE_SESSION'),'_active',False) or
                self._draw_error):
                if self._draw_error:
                    record_action('fixture_viewport_hud_gpu_failed',self._draw_error)
                self._stop();return {'CANCELLED'}
            if event.type=='ESC' and event.value=='PRESS':
                self._stop();return {'CANCELLED'}
            if event.value!='PRESS':return {'PASS_THROUGH'}
            action=None
            if event.type in ('LEFT_ARROW','RIGHT_ARROW'):
                action='left' if event.type=='LEFT_ARROW' else 'right'
            elif event.type=='LEFTMOUSE':
                layout=self._safe_fixture_layout()
                if layout is None:return {'PASS_THROUGH'}
                xy=(event.mouse_x-self._region.x,event.mouse_y-self._region.y)
                for key in ('left','right','select'):
                    if in_rect(*xy,layout[key]):action=key;break
                if action is None:return {'PASS_THROUGH'}
            if action in ('left','right'):
                delta=-1 if action=='left' else 1
                bpy.ops.view3d.brick_hole_fixture_step('EXEC_DEFAULT',delta=delta)
                self._area.tag_redraw();return {'RUNNING_MODAL'}
            if action=='select':
                if context.mode=='EDIT_MESH':
                    bpy.ops.mesh.brick_hole_fixture_select('EXEC_DEFAULT',append=bool(event.shift))
                else:
                    self.report({'INFO'},'Tab → Edit Mode, затем SELECT (Shift = добавить)')
                self._area.tag_redraw();return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        except (RuntimeError,ReferenceError,AttributeError,TypeError,ValueError) as exc:
            record_action('fixture_viewport_hud_error',str(exc)[:160])
            self._stop();self.report({'WARNING'},f'Навигатор закрыт: {exc}')
            return {'CANCELLED'}


class VIEW3D_PT_brick_hole_advanced(BRICKHOLE_PT_Base, bpy.types.Panel):
    bl_label = "Защита и диагностика"
    bl_idname = "VIEW3D_PT_brick_hole_advanced"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        row=layout.row()
        row.scale_y=1.3
        row.operator(OBJECT_OT_brick_hole_fixture_walls.bl_idname,
                     text='СОЗДАТЬ 30 ТЕСТОВЫХ СТЕН',icon='MESH_GRID')
        layout.label(text='15 открытых + 15 закрытых · один Mesh',icon='INFO')
        if _bh_fixture_active(context):
            layout.separator()
            nav=layout.row(align=True)
            nav.operator(VIEW3D_OT_brick_hole_fixture_step.bl_idname,text='',icon='TRIA_LEFT').delta=-1
            idx=max(0,min(29,int(context.scene.brick_hole_fixture_index)))
            gallery_kind=('OPEN' if idx%10<5 else 'FILLED')
            nav.label(text=f'{idx+1:02}/30 · {gallery_kind}')
            nav.operator(VIEW3D_OT_brick_hole_fixture_step.bl_idname,text='',icon='TRIA_RIGHT').delta=1
            nav.operator(VIEW3D_OT_brick_hole_fixture_step.bl_idname,text='',icon='VIEWZOOM').delta=0
            actions=layout.row(align=True)
            actions.enabled=context.mode=='EDIT_MESH'
            actions.operator(MESH_OT_brick_hole_fixture_select.bl_idname,text='Выделить проём',icon='RESTRICT_SELECT_OFF').append=False
            actions.operator(MESH_OT_brick_hole_fixture_select.bl_idname,text='+ Добавить',icon='ADD').append=True
            layout.operator(VIEW3D_OT_brick_hole_fixture_hud.bl_idname,text='Стрелки в Viewport · Esc закрыть',icon='GIZMO')
            layout.operator(OBJECT_OT_brick_hole_fixture_verify.bl_idname,text='ПРОВЕРИТЬ 30 ТЕСТОВ',icon='CHECKMARK')
            label=str(context.active_object.get('BH_fixture_last_check','NOT_RUN'))
            if label!='ORIGINAL':
                warn=layout.row();warn.alert=True
                warn.label(text='Геометрия изменена / проверка требуется',icon='ERROR')
            else:
                layout.label(text='Последняя проверка: эталон · может устареть',icon='INFO')
        if context.mode != 'OBJECT':
            layout.label(text='Для создания перейдите в Object Mode',icon='ERROR')
        row = layout.row(align=True)
        lab_box=layout.box()
        lab_box.label(text='BH-03.1 · Доказательная проверка Blender',icon='CHECKMARK')
        lab_btn=lab_box.row()
        lab_btn.enabled=(context.mode=='OBJECT')
        lab_btn.scale_y=1.25
        lab_btn.operator(WM_OT_brick_hole_bh031_auto_verify.bl_idname,
                         text='AUTO TEST BRICK HOLE → ZIP',icon='PLAY')
        if context.mode!='OBJECT':
            lab_box.label(text='Для автотеста перейдите в Object Mode',icon='INFO')
        lab_box.label(text='Сцена создаётся временно; реальная проверка BMesh',icon='INFO')
        row.operator(WM_OT_brick_hole_diagnostics_zip.bl_idname,
                     text='Диагностика ZIP',icon='FILE_FOLDER')
        row.operator(WM_OT_brick_hole_copy_log.bl_idname,
                     text='Лог',icon='COPYDOWN')
        row=layout.row(align=True)
        row.operator(MESH_OT_brick_hole_validate.bl_idname,text='Проверить результат',icon='CHECKMARK')
        row.operator(MESH_OT_brick_hole_select_errors.bl_idname,text='Выделить ошибки',icon='ERROR')
        if context.scene.brick_hole_last_errors:
            layout.operator(VIEW3D_OT_brick_hole_focus_error.bl_idname,text='К следующей ошибке',icon='VIEWZOOM')
        layout.label(text='Сбой → откат · Повтор разрешён',icon='LOCKED')
        layout.label(text='ZIP содержит данные BH-04A / BH-04B углов',icon='INFO')


CLASSES = (
    MESH_OT_brick_hole_extrude,
    MESH_OT_brick_hole_preview,
    MESH_OT_brick_hole_flip_direction,
    MESH_OT_brick_hole_set_manual_axis,
    MESH_OT_brick_hole_extrude_all,
    MESH_OT_brick_hole_safe_depth,
    MESH_OT_brick_hole_corner_analyze,
    MESH_OT_brick_hole_corner_miter_build,
    MESH_OT_brick_hole_edge_smart_uv,
    MESH_OT_brick_hole_uv_policy_apply,
    MESH_OT_brick_hole_confirm_uv_policy,
    WM_OT_brick_hole_bh031_auto_verify,
    MESH_OT_brick_hole_bridge_cap,
    MESH_OT_brick_hole_extrude_faces,
    MESH_OT_brick_hole_extrude_unified,
    MESH_OT_brick_hole_preview_unified,
    WM_OT_brick_hole_copy_log,
    WM_OT_brick_hole_diagnostics_zip,
    OBJECT_OT_brick_hole_fixture_walls,
    VIEW3D_OT_brick_hole_fixture_step,
    MESH_OT_brick_hole_fixture_select,
    OBJECT_OT_brick_hole_fixture_verify,
    VIEW3D_OT_brick_hole_fixture_hud,
    VIEW3D_OT_brick_hole_focus_error,
    VIEW3D_OT_brick_hole_toggle_overlay,
    VIEW3D_OT_brick_hole_interactive,
    issue_inspector.VIEW3D_OT_brick_hole_issue_inspector,
    seam_inspector.VIEW3D_OT_brick_hole_seam_scan,
    seam_inspector.VIEW3D_OT_brick_hole_seam_inspector,
    MESH_OT_brick_hole_validate,
    MESH_OT_brick_hole_select_errors,
    ED_OT_brick_hole_undo_last,
    VIEW3D_PT_brick_hole_extrude,
    VIEW3D_PT_brick_hole_geometry,
    VIEW3D_PT_brick_hole_texture,
    VIEW3D_PT_brick_hole_advanced,
)


def _brick_hole_load_pre(_dummy):
    if callable(globals().get('stop_fixture_nav_hud')):stop_fixture_nav_hud()
    """A new .blend invalidates the old viewport and its transient state."""
    stop_viewport_overlay()
    stop_interactive_preview()
    issue_inspector.clear()
    seam_inspector.clear()


def _register_overlay_load_handler():
    from bpy.app.handlers import persistent
    persistent(_brick_hole_load_pre)
    handlers = bpy.app.handlers.load_pre
    if _brick_hole_load_pre not in handlers:
        handlers.append(_brick_hole_load_pre)


def _unregister_overlay_load_handler():
    handlers = bpy.app.handlers.load_pre
    if _brick_hole_load_pre in handlers:
        handlers.remove(_brick_hole_load_pre)


# Integrated entry point. Geometry kernels and production preflight are preserved;
# BH-03.2H.1 deliberately updates the interactive HUD and diagnostic actions.
_OWNED_CLASSES = []
_OWNED_SCENE_PROPERTIES = []
_INTEGRATED = False


def _register_properties():
    bpy.types.Scene.brick_hole_direction_source = EnumProperty(
        name="Источник направления",
        items=(('AUTO', "Автоматически", "FILLED: штатный Blender Along Normals + Even Offset; EDGE: контур по нормалям стены"),
               ('FACE', "По полигону", "Выбрать полигон в Viewport и взять его мировую нормаль"),
               ('GLOBAL', "Global", "Явный мировой вектор XYZ; FILLED: Blender Region Move, EDGE: заданное направление"),
               ('MANUAL', "Legacy XYZ", "Нормаль старых проектов; сохранена обратная совместимость")),
        default='AUTO',
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_direction_source')
    bpy.types.Scene.brick_hole_manual_normal = FloatVectorProperty(
        name="Направление экструзии", size=3, default=(0.0, 1.0, 0.0),
        description="Мировое XYZ направления (не изменяет нормали граней). Знак глубины меняет сторону",
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_manual_normal')
    for key,axis in (('x',(1.0,0.0,0.0)),('y',(0.0,1.0,0.0)),('z',(0.0,0.0,1.0))):
        prop='brick_hole_face_frame_'+key
        setattr(bpy.types.Scene,prop,FloatVectorProperty(
            name='Полигон · локальная '+key.upper(),size=3,default=axis,
            description='Мировой ортонормированный базис выбранного полигона'))
        _OWNED_SCENE_PROPERTIES.append(prop)
    bpy.types.Scene.brick_hole_face_frame_valid=BoolProperty(
        name='Полигон выбран',default=False)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_face_frame_valid')
    for name in ('brick_hole_face_axis','brick_hole_global_axis'):
        setattr(bpy.types.Scene,name,EnumProperty(
            name='Текущая ось',items=tuple((k,k,'Активная ось '+k) for k in 'XYZ'),default='Z'))
        _OWNED_SCENE_PROPERTIES.append(name)
    bpy.types.Scene.brick_hole_depth = FloatProperty(
        name="Глубина",
        default=0.30,
        soft_min=-2.0,
        soft_max=2.0,
        unit='LENGTH',
        precision=6,
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_depth')
    bpy.types.Scene.brick_hole_vertex_snap = BoolProperty(
        name='Vertex Snap ручки',description='Привязка ручки к вершинам активного Mesh, осевой допуск 25 мм',
        default=False,options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_vertex_snap')
    bpy.types.Scene.brick_hole_check_intersections = BoolProperty(
        name='Защита от пересечений', default=True,
        description=('ВКЛ: проверять пересечения новых поверхностей с исходной сеткой и собой. '
                     'ВЫКЛ: разрешить Apply даже при пересечении; результат может содержать наложения'),
        update=_brick_hole_collision_guard_updated)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_check_intersections')
    bpy.types.Scene.brick_hole_safe_depth_candidate = FloatProperty(
        name='Проверенная рекомендуемая глубина',default=0.,options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_safe_depth_candidate')
    bpy.types.Scene.brick_hole_safe_depth_status = StringProperty(
        name='Результат проверки глубины',default='Нажмите «Найти допустимую»',
        options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_safe_depth_status')
    bpy.types.Scene.brick_hole_tile_size = FloatProperty(
        name="Размер тайла",
        default=FIXED_TILE_SIZE_METERS,
        min=FIXED_TILE_SIZE_METERS,
        max=FIXED_TILE_SIZE_METERS,
        unit='LENGTH',
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_tile_size')
    bpy.types.Scene.brick_hole_reverse_u = BoolProperty(
        name="Устаревший UV Flip (без отражения)",
        description="Для совместимости со старыми .blend; зеркалирование UV больше не выполняется",
        default=False,
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_reverse_u')
    bpy.types.Scene.brick_hole_direction = EnumProperty(
        name="Направление",
        items=(
            ('INWARD', "Внутрь", "Экструдировать против нормали стены"),
            ('OUTWARD', "Наружу", "Экструдировать по нормали стены"),
        ),
        default='INWARD',
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_direction')
    bpy.types.Scene.brick_hole_uv_map = StringProperty(name="UV-карта", default="UVMap")
    _OWNED_SCENE_PROPERTIES.append('brick_hole_uv_map')
    bpy.types.Scene.brick_hole_uv_policy = EnumProperty(
        name='BRICK HOLE · SMART UV',
        items=(('STRICT','1000 px/м','Строгий UV новой геометрии; возможен шов'),
               ('ADAPTIVE','Плавно к 1000','Непрерывность источника и плавный переход'),
               ('CONTINUE','Как у стены','Продолжить исходную UV-параметризацию')),
        default='STRICT',update=_brick_hole_uv_policy_updated)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_uv_policy')
    bpy.types.Scene.brick_hole_edge_uv_mode = EnumProperty(
        name='UV для Edge Extend',
        description='STRICT остаётся старым ядром. CONTINUE/ADAPTIVE только для 1–2 независимых граничных рёбер в одном тайле',
        items=(
            ('STRICT','Строго 1000','Прежняя проверенная метрическая экструзия, возможен UV-шов'),
            ('CONTINUE','Как у ребра','Продолжить авторские UV новой геометрии; не гарантируется 1000 px/м'),
            ('ADAPTIVE','Плавно к 1000','Сохранить первый UV-стык и адаптировать плотность на новых рядах'),
        ), default='STRICT')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_edge_uv_mode')
    bpy.types.Scene.brick_hole_bridge_uv_mode = EnumProperty(
        name='UV для Bridge',
        description='Меняет только новую геометрию BRIDGE CAP; Edge Extend остаётся STRICT',
        items=(
            ('STRICT','Строго 1000','Точная метрическая плотность, возможен шов с исходной UV'),
            ('CONTINUE','Как у ребра','Сохранить авторские UV на первом стыке и продолжить их масштаб'),
            ('ADAPTIVE','Плавно к 1000','Сохранить первый стык и добавить новые ряды до 1000 px/м'),
        ), default='STRICT')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_bridge_uv_mode')
    bpy.types.Scene.brick_hole_uv_transition_m = FloatProperty(
        name='Длина UV-перехода',description='Метров по новой полосе до 1000 px/м',
        default=0.3,min=0.05,max=4.,precision=3,subtype='DISTANCE')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_uv_transition_m')
    bpy.types.Scene.brick_hole_resolution = EnumProperty(
        name="Разрешение",
        items=(
            ('4096', "4K", "4096 px, полезные 4×4 м, поля по 48 px"),
            ('2048', "2K", "2048 px, полезные 2×2 м, поля по 24 px"),
        ),
        default='4096',
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_resolution')
    bpy.types.Scene.brick_hole_material_mode = EnumProperty(
        name="Материал откосов",
        items=(
            ('INHERIT', "Как у стены", "Наследовать материал каждой соседней грани"),
            ('OVERRIDE', "Один слот", "Назначить всем откосам выбранный слот"),
        ),
        default='INHERIT',
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_material_mode')
    bpy.types.Scene.brick_hole_material_index = IntProperty(
        name="Индекс материала",
        description="Номер слота, начиная с нуля",
        default=0,
        min=0,
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_material_index')
    bpy.types.Scene.brick_hole_allow_repeat = BoolProperty(
        name="Разрешить повторную обработку",
        default=True,
    )
    _OWNED_SCENE_PROPERTIES.append('brick_hole_allow_repeat')
    bpy.types.Scene.brick_hole_fixture_index = IntProperty(name='Тестовый проём',default=0,min=0,max=29,options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_fixture_index')
    bpy.types.Scene.brick_hole_issue_cursor = IntProperty(default=0, min=0, options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_issue_cursor')
    bpy.types.Scene.brick_hole_last_error_summary = StringProperty(default='Проверка не запускалась', options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_last_error_summary')
    bpy.types.Scene.brick_hole_show_overlay = BoolProperty(
        name='Подсветка 3D Viewport', default=False, options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_show_overlay')
    bpy.types.Scene.brick_hole_overlay_labels = BoolProperty(
        name='Подписи ошибок', default=True, options={'SKIP_SAVE'},
        description='До 12 подписей проблем возле граней; без изменения меша')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_overlay_labels')
    bpy.types.Scene.brick_hole_overlay_error = StringProperty(
        name='Ошибка отрисовки', default='', options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_overlay_error')
    bpy.types.Scene.brick_hole_overlay_mode = EnumProperty(
        name='Режим подсветки',
        items=(('CONTOUR', 'Контур', 'Текущее выделение; жёлтые линии'),
               ('ERRORS', 'Проблемы', 'Проверка и красная подсветка проблемных граней')),
        default='CONTOUR')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_overlay_mode')
    bpy.types.Scene.brick_hole_hud_placement = EnumProperty(
        name='Размещение Viewport HUD',
        items=(('BOTTOM','Снизу','Нижняя панель управления Brick Hole'),
               ('AUTO','Рядом с моделью','Режим совместимости со старой панелью'),
               ('CORNER','В углу','Режим совместимости')),
        default='BOTTOM',options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_hud_placement')
    for key, title, default in (
        ('show_source','Исходный контур',True),
        ('show_wire','Будущие рёбра',True),
        ('show_fill','Полупрозрачные поверхности',True),
        ('show_handles','Маркеры направления',True),
        ('show_labels','Подписи и подсказки',True),
    ):
        name='brick_hole_'+key
        setattr(bpy.types.Scene, name, BoolProperty(name=title,default=default,options={'SKIP_SAVE'}))
        _OWNED_SCENE_PROPERTIES.append(name)
    bpy.types.Scene.brick_hole_interactive_state = StringProperty(
        default='IDLE · выберите полигоны или рёбра', options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_interactive_state')
    bpy.types.Scene.brick_hole_preview_verified = BoolProperty(default=False, options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_verified')
    bpy.types.Scene.brick_hole_preview_face_mode = BoolProperty(default=False, options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_face_mode')
    bpy.types.Scene.brick_hole_preview_signature = StringProperty(default='', options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_signature')
    bpy.types.Scene.brick_hole_preview_loops = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_loops')
    bpy.types.Scene.brick_hole_preview_edges = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_edges')
    bpy.types.Scene.brick_hole_preview_cuts = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_cuts')
    bpy.types.Scene.brick_hole_preview_warnings = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_warnings')
    bpy.types.Scene.brick_hole_preview_text = StringProperty(default="Нажмите «Проверить»")
    _OWNED_SCENE_PROPERTIES.append('brick_hole_preview_text')
    bpy.types.Scene.brick_hole_progress = IntProperty(default=0, min=0, max=100, subtype='PERCENTAGE')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_progress')
    bpy.types.Scene.brick_hole_last_contours = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_last_contours')
    bpy.types.Scene.brick_hole_last_checked = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_last_checked')
    bpy.types.Scene.brick_hole_last_errors = IntProperty(default=0, min=0)
    _OWNED_SCENE_PROPERTIES.append('brick_hole_last_errors')
    bpy.types.Scene.brick_hole_qa_state = StringProperty(default='Не проверено')
    _OWNED_SCENE_PROPERTIES.append('brick_hole_qa_state')
    bpy.types.Scene.brick_hole_corner_analysis_status = StringProperty(
        default='Ещё не проверено',options={'SKIP_SAVE'})
    _OWNED_SCENE_PROPERTIES.append('brick_hole_corner_analysis_status')


def register_integrated():
    """Register only internal operators and RNA, never standalone Sidebar panels.

    Fail closed on another installed Brick Hole; never unregister another addon.
    Roll back only RNA/classes actually registered by this module.
    """
    global _INTEGRATED
    if _INTEGRATED:
        return
    if _OWNED_CLASSES or _OWNED_SCENE_PROPERTIES:
        raise RuntimeError('ISSMAKER Brick Hole: incomplete prior registration; restart/reload addon')
    registered = tuple(cls for cls in CLASSES if not issubclass(cls, bpy.types.Panel))
    for cls in registered:
        if getattr(bpy.types, cls.__name__, None) is not None:
            raise RuntimeError('ISSMAKER: обнаружен внешний Brick Hole класс %s. Отключите standalone Brick Hole перед установкой.' % cls.__name__)
    for name in (('brick_hole_uv_policy', 'brick_hole_edge_uv_mode', 'brick_hole_bridge_uv_mode', 'brick_hole_uv_transition_m', 'brick_hole_direction_source', 'brick_hole_manual_normal', 'brick_hole_face_frame_x', 'brick_hole_face_frame_y', 'brick_hole_face_frame_z', 'brick_hole_face_frame_valid', 'brick_hole_face_axis', 'brick_hole_global_axis', 'brick_hole_depth', 'brick_hole_check_intersections', 'brick_hole_safe_depth_candidate', 'brick_hole_safe_depth_status', 'brick_hole_vertex_snap', 'brick_hole_tile_size', 'brick_hole_reverse_u', 'brick_hole_direction', 'brick_hole_uv_map', 'brick_hole_resolution', 'brick_hole_material_mode', 'brick_hole_material_index', 'brick_hole_allow_repeat', 'brick_hole_fixture_index', 'brick_hole_issue_cursor', 'brick_hole_last_error_summary', 'brick_hole_show_overlay', 'brick_hole_overlay_labels', 'brick_hole_overlay_error', 'brick_hole_overlay_mode', 'brick_hole_hud_placement', 'brick_hole_interactive_state', 'brick_hole_preview_verified', 'brick_hole_preview_face_mode', 'brick_hole_preview_signature', 'brick_hole_preview_loops', 'brick_hole_preview_edges', 'brick_hole_preview_cuts', 'brick_hole_preview_warnings', 'brick_hole_preview_text', 'brick_hole_progress', 'brick_hole_last_contours', 'brick_hole_last_checked', 'brick_hole_last_errors', 'brick_hole_qa_state')):
        if hasattr(bpy.types.Scene, name):
            raise RuntimeError('ISSMAKER: свойство Scene.%s занято. Отключите standalone Brick Hole.' % name)
    try:
        stop_interactive_preview()
        stop_viewport_overlay()
        for cls in registered:
            bpy.utils.register_class(cls)
            _OWNED_CLASSES.append(cls)
        _register_properties()
        _register_overlay_load_handler()
        _INTEGRATED = True
    except Exception:
        unregister_integrated()
        raise


def unregister_integrated():
    global _INTEGRATED
    issue_inspector.clear()
    seam_inspector.clear()
    if callable(globals().get('stop_fixture_nav_hud')):
        stop_fixture_nav_hud()
    stop_interactive_preview()
    stop_viewport_overlay()
    _unregister_overlay_load_handler()
    while _OWNED_SCENE_PROPERTIES:
        name = _OWNED_SCENE_PROPERTIES.pop()
        try:
            delattr(bpy.types.Scene, name)
        except (AttributeError, RuntimeError):
            pass
    while _OWNED_CLASSES:
        cls = _OWNED_CLASSES.pop()
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ReferenceError):
            pass
    _INTEGRATED = False


def register():
    register_integrated()


def unregister():
    unregister_integrated()


if __name__ == '__main__':
    register()
