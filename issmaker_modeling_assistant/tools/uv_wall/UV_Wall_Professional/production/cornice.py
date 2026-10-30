# AUTO-EXTRACTED production module from the 1.4.0 verified baseline.

# Runtime integration symbols are bound by the package composition root.
import bpy
import bmesh
import json
import traceback
import webbrowser
from datetime import datetime
from time import perf_counter
from math import ceil, floor, cos, sin, radians, isfinite, acos, degrees
from mathutils import Vector
from mathutils import kdtree
from mathutils.bvhtree import BVHTree
from mathutils.geometry import tessellate_polygon

from ..core.definitions import (
    definition_for, initialize_from_scene, sync_scene_from_definition, sync_definition_from_scene,
    ensure_source_uid, serialize_definition, apply_serialized_definition,
    CURRENT_SCHEMA_VERSION, runtime_schema_status, ensure_runtime_schema, virtual_asset_for_module,
)
from ..core.virtual_assets import resolve_virtual_source, release_virtual_source, purge_virtual_cache
from ..core.surface_points import apply_surface_heights, rollback_surface_heights
from ..core.semantic_geometry import (
    FACE_ROLE_ATTRIBUTE, FACE_ROLE_CODES, face_role_codes, face_role_name,
    set_face_roles, role_counts, validate_face_role_coverage,
    validate_and_fix_role_normals, expected_component_count,
)
from ..core.result_lifecycle import (
    preferred_stable_result, install_mesh_on_result, cleanup_duplicate_results,
    prepare_result_metadata, shutdown_result_lifecycle,
)
from ..core.diagnostics import log_result_snapshot as _log_result_snapshot
from ..core.performance import (
    PERFORMANCE_REGISTRY, StageTimer, cached_surface_call, clear_performance_caches, record_stage,
)
import json


EPS = 1.0e-6
MAX_ROOF_GRID_CELLS = 250_000
MAX_GENERATED_FACES = 500_000
MITER_LIMIT = 20.0
LOG_TEXT_NAME = "UV_Wall_Diagnostics"
REGULATION_WELD_DISTANCE = 0.002
MAX_LOGGED_RESULT_ELEMENTS = 64
MAX_CORNICE_SEAM_SNAP_DISTANCE = 0.025  # 25 mm: geometry-only safety cap near authored corners
CORNER_UID_ATTRIBUTE = "uvwall_corner_uid"


bl_info = {'version': (1, 13, 0)}

def cornice_profile_data(profile_obj, tolerance=REGULATION_WELD_DISTANCE):
    """Read one open or closed unbranched profile in local XZ."""
    mesh = profile_obj.data
    adjacency = {index: [] for index in range(len(mesh.vertices))}
    for edge in mesh.edges:
        a, b = edge.vertices
        if a != b:
            adjacency[a].append(b); adjacency[b].append(a)
    used = {index for index, neighbors in adjacency.items() if neighbors}
    if not used:
        raise ValueError("Профиль карниза должен содержать хотя бы одно ребро")
    isolated = sorted(set(range(len(mesh.vertices))) - used)
    if isolated:
        raise ValueError(f"Профиль содержит изолированные вершины: {isolated[:20]}")
    branches = sorted(index for index in used if len(adjacency[index]) > 2)
    if branches:
        raise ValueError(f"Профиль содержит ветвления/T-узлы в вершинах: {branches[:20]}")
    endpoints = sorted(index for index in used if len(adjacency[index]) == 1)
    invalid = sorted(index for index in used if len(adjacency[index]) not in {1, 2})
    if invalid or len(endpoints) not in {0, 2}:
        raise ValueError(
            "Профиль должен быть одной открытой или замкнутой неразветвлённой цепью; "
            f"конечных вершин={len(endpoints)}"
        )
    order = _order_chain_indices(adjacency, used)
    if order is None:
        raise ValueError("Не удалось определить последовательность рёбер профиля")
    profile_closed = len(endpoints) == 0
    expected_order = len(used) + (1 if profile_closed else 0)
    if len(order) != expected_order:
        raise ValueError("Профиль содержит несколько цепей или неправильное замыкание")
    local_points = [mesh.vertices[index].co.copy() for index in order]
    local_unique = local_points[:-1] if len(local_points) > 1 and order[0] == order[-1] else local_points
    local_y_span = max(point.y for point in local_unique) - min(point.y for point in local_unique)
    if local_y_span > tolerance:
        raise ValueError(
            f"Профиль должен лежать в локальной плоскости XZ; разброс Y={local_y_span:.6f} м"
        )

    # Path coordinates already include matrix_world. Profile coordinates used
    # to ignore object scale completely, so a visibly scaled profile generated
    # the unscaled mesh and wrong texel proportions. Rotation/location remain
    # irrelevant by design (the profile is defined in local XZ), while the
    # actual world lengths of its local X/Z axes are respected.
    basis = profile_obj.matrix_world.to_3x3()
    world_x = basis @ Vector((1.0, 0.0, 0.0))
    world_z = basis @ Vector((0.0, 0.0, 1.0))
    if world_x.length <= EPS or world_z.length <= EPS:
        raise ValueError("Профиль имеет нулевой Scale по X или Z")
    axis_dot = abs(world_x.normalized().dot(world_z.normalized()))
    if axis_dot > 1.0e-5:
        raise ValueError(
            "Профиль содержит shear между локальными X и Z. "
            "Примените трансформации объекта перед построением"
        )
    scale_x = world_x.length * (-1.0 if profile_obj.scale.x < 0.0 else 1.0)
    scale_z = world_z.length * (-1.0 if profile_obj.scale.z < 0.0 else 1.0)
    points = [Vector((point.x * scale_x, 0.0, point.z * scale_z)) for point in local_points]
    unique_points = points[:-1] if profile_closed else points
    minimum_points = 3 if profile_closed else 2
    if len(unique_points) < minimum_points:
        raise ValueError(
            f"{'Замкнутый' if profile_closed else 'Открытый'} профиль должен содержать "
            f"минимум {minimum_points} вершины"
        )
    y_span = local_y_span
    pivot_index = min(range(len(unique_points)), key=lambda index: unique_points[index].length)
    pivot_distance = unique_points[pivot_index].length
    if pivot_distance > tolerance:
        raise ValueError(f"Origin должен касаться вершины профиля; расстояние={pivot_distance:.6f} м")
    if profile_closed:
        # A closed ring may safely start at the contact vertex. This keeps V
        # phase deterministic and does not alter its topology.
        profile_points = unique_points[pivot_index:] + unique_points[:pivot_index]
        profile_points.append(profile_points[0].copy())
        pivot_role = "RING_START"
    else:
        # An open chain cannot be rotated without breaking an edge. If the
        # contact vertex is an endpoint, orient the chain from that endpoint;
        # an interior contact remains valid and the full chain is preserved.
        profile_points = [point.copy() for point in unique_points]
        if pivot_index == len(profile_points) - 1:
            profile_points.reverse()
            pivot_index = 0
        pivot_role = "ENDPOINT" if pivot_index == 0 else "INTERIOR"
    for index, (start, end) in enumerate(zip(profile_points, profile_points[1:])):
        length = (end - start).length
        safe_minimum = tolerance * 1.05
        if length <= safe_minimum:
            raise ValueError(
                f"Ребро профиля {index}: {length:.6f} м слишком короткое для Weld "
                f"{tolerance:.3f} м; требуется больше {safe_minimum:.6f} м"
            )
    log_event(
        f"CORNICE_PROFILE object={profile_obj.name}; topology={'CLOSED' if profile_closed else 'OPEN'}; "
        f"points={len(unique_points)}; edges={len(profile_points)-1}; endpoints={endpoints}; "
        f"pivot_vertex={pivot_index}; pivot_role={pivot_role}; "
        f"pivot_distance={pivot_distance:.9f}; y_span={y_span:.9f}; "
        f"effective_scale_x={scale_x:.9f}; effective_scale_z={scale_z:.9f}",
        "CORNICE",
    )
    logged_points = profile_points[:-1] if profile_closed else profile_points
    for index, point in enumerate(logged_points):
        log_event(
            f"CORNICE_PROFILE_POINT index={index}; x={point.x:.9f}; y={point.y:.9f}; z={point.z:.9f}",
            "CORNICE_GEOMETRY",
        )
    return profile_points, profile_closed


def resample_profile(points, tile_length):
    distances = [0.0]
    for start, end in zip(points, points[1:]):
        distances.append(distances[-1] + (end - start).length)
    total = distances[-1]
    cuts = set(distances)
    cuts.update(index * tile_length for index in range(1, int(total // tile_length) + 1) if index * tile_length < total - EPS)
    samples = []
    segment = 0
    for distance in sorted(cuts):
        while segment + 1 < len(distances) and distance > distances[segment + 1] + EPS:
            segment += 1
        span = distances[segment + 1] - distances[segment]
        factor = 0.0 if span <= EPS else (distance - distances[segment]) / span
        samples.append((points[segment].lerp(points[segment + 1], factor), distance))
    # `distances` already includes the final endpoint (or the repeated start
    # of a closed ring). Never append it twice: that creates a zero strip.
    if not samples or abs(samples[-1][1] - total) > EPS:
        samples.append((points[0].copy(), total))
    return samples, total


def cornice_side_sign(path_points, flip=False):
    if is_path_closed(path_points):
        sign = -1.0 if signed_area_2d(path_points[:-1]) > 0.0 else 1.0
    else:
        sign = -1.0
    return -sign if flip else sign


def prepare_cornice_path(points, corner_mode, linear_step):
    """Optionally add collinear stations without changing the authored path."""
    closed = is_path_closed(points)
    ring = [point.copy() for point in (points[:-1] if closed else points)]
    if len(ring) < (3 if closed else 2):
        return points, {"rounded": 0, "sharp": 0, "max_turn": 0.0, "inserted": 0}

    turn_data = []
    for index, center in enumerate(ring):
        if not closed and index in {0, len(ring) - 1}:
            turn_data.append((0.0, None, None))
            continue
        before = ring[(index - 1) % len(ring)]
        after = ring[(index + 1) % len(ring)]
        incoming = Vector((center.x - before.x, center.y - before.y, 0.0))
        outgoing = Vector((after.x - center.x, after.y - center.y, 0.0))
        if incoming.length <= EPS or outgoing.length <= EPS:
            turn_data.append((0.0, None, None))
            continue
        incoming.normalize(); outgoing.normalize()
        turn = degrees(acos(max(-1.0, min(1.0, incoming.dot(outgoing)))))
        turn_data.append((turn, incoming, outgoing))

    rebuilt = [point.copy() for point in ring]
    sharp = sum(1 for turn, _incoming, _outgoing in turn_data if turn >= 45.0)

    if corner_mode == "DENSE_LINEAR":
        dense = []
        pairs = list(zip(rebuilt, rebuilt[1:] + ([rebuilt[0]] if closed else [])))
        for start, end in pairs:
            if not dense or (start - dense[-1]).length > EPS:
                dense.append(start.copy())
            # Cornice UV represents real travelled metres. A sloped segment
            # must therefore be sampled by its full XYZ length, not only XY.
            subdivisions = max(1, int(ceil((end - start).length / max(linear_step, REGULATION_WELD_DISTANCE))))
            for part in range(1, subdivisions):
                dense.append(start.lerp(end, part / subdivisions))
        rebuilt = dense

    if closed and rebuilt and (rebuilt[0] - rebuilt[-1]).length > EPS:
        rebuilt.append(rebuilt[0].copy())
    stats = {
        "rounded": 0,
        "sharp": sharp,
        "max_turn": max((item[0] for item in turn_data), default=0.0),
        "inserted": max(0, len(rebuilt) - len(points)),
    }
    log_event(
        f"CORNICE_PATH_PREP mode={corner_mode}; input_points={len(points)}; output_points={len(rebuilt)}; "
        f"rounded_corners=0; sharp_corners={sharp}; inserted_points={stats['inserted']}; "
        f"max_turn_deg={stats['max_turn']:.6f}; geometry_changed=False; linear_step={linear_step:.6f}",
        "CORNICE_GEOMETRY",
    )
    return rebuilt, stats


def cornice_vertex_frame(path, index, side_sign, miter_limit=None):
    closed = is_path_closed(path)
    last = len(path) - 1
    center = path[index % last] if closed else path[index]
    before = path[(index - 1) % last] if closed else path[max(0, index - 1)]
    after = path[(index + 1) % last] if closed else path[min(last, index + 1)]
    d0 = Vector((center.x - before.x, center.y - before.y, 0.0))
    d1 = Vector((after.x - center.x, after.y - center.y, 0.0))
    if d0.length <= EPS:
        d0 = d1.copy()
    if d1.length <= EPS:
        d1 = d0.copy()
    d0.normalize(); d1.normalize()
    n0 = Vector((-d0.y, d0.x, 0.0)) * side_sign
    n1 = Vector((-d1.y, d1.x, 0.0)) * side_sign
    summed = n0 + n1
    if summed.length <= EPS:
        return n1, 1.0, True
    miter = summed.normalized()
    denominator = miter.dot(n1)
    scale = 1.0 if abs(denominator) <= EPS else 1.0 / denominator
    # Never clip the exact bisector: that altered profile shape without
    # changing the authored Path. Reach/overlap is validated by corner_trim.
    limited = abs(denominator) <= EPS or (miter_limit is not None and abs(scale) > miter_limit)
    return miter, scale, limited



def cornice_corner_metrics(path, index):
    """Return authored XY turn and unbounded miter ratio for one path vertex."""
    closed = is_path_closed(path)
    logical_last = len(path) - 1 if closed else len(path)
    if logical_last <= 1:
        return {"turn_deg": 0.0, "miter_ratio": 1.0, "near_reversal": False}
    if not closed and index in {0, logical_last-1}:
        return {"turn_deg": 0.0, "miter_ratio": 1.0, "near_reversal": False}
    center = path[index % logical_last]
    before = path[(index-1) % logical_last]
    after = path[(index+1) % logical_last]
    incoming = Vector((center.x-before.x, center.y-before.y, 0.0))
    outgoing = Vector((after.x-center.x, after.y-center.y, 0.0))
    if incoming.length <= EPS or outgoing.length <= EPS:
        return {"turn_deg": 0.0, "miter_ratio": 1.0, "near_reversal": False}
    incoming.normalize(); outgoing.normalize()
    dot = max(-1.0, min(1.0, incoming.dot(outgoing)))
    turn = degrees(acos(dot))
    half = radians(turn * 0.5)
    denominator = max(1.0e-8, abs(cos(half)))
    ratio = 1.0 / denominator
    return {"turn_deg": turn, "miter_ratio": ratio, "near_reversal": turn >= 170.0}


def cornice_transport_up_vectors(stations, side_sign, closed=False):
    """Return a stable, no-flip profile up-vector for every generated station.

    Older builds always added profile Z in world Z.  On variable-height paths
    that sheared the swept profile relative to the 3D path tangent.  1.6.0 keeps
    the architectural world-up preference, projects it through the station
    tangent/side frame and explicitly preserves orientation between neighbours.
    The user side flip changes only profile X; it never turns profile Z upside-down.
    """
    if not stations:
        return []
    count = len(stations)
    logical_last = count - 1 if closed and count > 1 else count
    ups = []
    previous_up = None
    world_up = Vector((0.0, 0.0, 1.0))
    for index in range(logical_last):
        point = stations[index][0]
        if closed:
            before = stations[(index - 1) % logical_last][0]
            after = stations[(index + 1) % logical_last][0]
        elif index == 0:
            before = point
            after = stations[min(1, count - 1)][0]
        elif index == count - 1:
            before = stations[index - 1][0]
            after = point
        else:
            before = stations[index - 1][0]
            after = stations[index + 1][0]
        tangent = after - before
        if tangent.length <= EPS:
            tangent = Vector((1.0, 0.0, 0.0))
        else:
            tangent.normalize()
        # Remove the user side flip before deriving profile Z orientation.
        base_side = stations[index][1].copy() * side_sign
        base_side -= tangent * base_side.dot(tangent)
        if base_side.length <= EPS:
            base_side = world_up.cross(tangent)
        if base_side.length <= EPS:
            base_side = Vector((0.0, 1.0, 0.0))
        base_side.normalize()
        up = tangent.cross(base_side)
        if up.length <= EPS:
            up = world_up.copy()
        else:
            up.normalize()
        if up.z < 0.0:
            up.negate()
        if previous_up is not None and up.dot(previous_up) < 0.0:
            up.negate()
        ups.append(up)
        previous_up = up
    if closed and count > logical_last:
        ups.append(ups[0].copy())
    while len(ups) < count:
        ups.append(ups[-1].copy())
    return ups

def build_cornice(
    context, source_obj, path_obj, profile_obj, tile_length, output_name,
    flip_side=False, uv_mode="MAX_REAL_WORLD", corner_mode="AUTHORED", linear_step=0.25,
    manual_corner_points=None,
    manual_corner_methods=None,
    _auto_side_retry=False,
    target_obj=None,
    profile_rotation_deg=0.0,
    profile_flip_x=False,
    profile_flip_z=False,
    reverse_path=False,
):
    source = source_quad_data(source_obj, tile_length)
    active_path_modifiers = [modifier.name for modifier in path_obj.modifiers if modifier.show_viewport]
    active_profile_modifiers = [modifier.name for modifier in profile_obj.modifiers if modifier.show_viewport]
    if active_path_modifiers or active_profile_modifiers:
        raise ValueError(
            "Карниз использует исходный Mesh и не может безопасно угадать результат модификаторов. "
            f"Примените модификаторы: путь={active_path_modifiers}, профиль={active_profile_modifiers}"
        )
    path_world = path_obj.matrix_world
    raw_vertical_edges = []
    for edge in path_obj.data.edges:
        raw_start = path_world @ path_obj.data.vertices[edge.vertices[0]].co
        raw_end = path_world @ path_obj.data.vertices[edge.vertices[1]].co
        if (raw_end - raw_start).length > EPS and horizontal_length(raw_start, raw_end) <= EPS:
            raw_vertical_edges.append(edge.index)
    if raw_vertical_edges:
        raise ValueError(
            "Карниз не поддерживает строго вертикальные рёбра пути, потому что для них "
            f"невозможно определить горизонтальную сторону профиля: {raw_vertical_edges[:10]}"
        )
    path = ordered_path_vertices(path_obj)
    if reverse_path:
        path=list(reversed(path))
    unsafe_path_segments = []
    safe_path_minimum = REGULATION_WELD_DISTANCE * 1.05
    for index, (start, end) in enumerate(zip(path, path[1:])):
        spatial_length = (end - start).length
        if spatial_length <= safe_path_minimum:
            unsafe_path_segments.append((index, spatial_length))
    if unsafe_path_segments:
        raise ValueError(
            "Карниз: рёбра пути слишком короткие для обязательного Weld 2 мм: "
            f"{unsafe_path_segments[:10]}"
        )
    path, seam_start_index, seam_clearance = choose_safe_closed_start(
        path, tile_length, clearance=REGULATION_WELD_DISTANCE * 2.1,
        spatial_distance=True,
    )
    path, corner_stats = prepare_cornice_path(path, corner_mode, linear_step)
    profile, profile_closed = cornice_profile_data(profile_obj)
    rotation=radians(profile_rotation_deg)
    co,si=cos(rotation),sin(rotation)
    profile=[Vector(((p.x*(-1 if profile_flip_x else 1))*co - (p.z*(-1 if profile_flip_z else 1))*si,
                     0.0,
                     (p.x*(-1 if profile_flip_x else 1))*si + (p.z*(-1 if profile_flip_z else 1))*co)) for p in profile]
    from .normal_math import section_signed_area_xz, quad_outward_decision
    from .normal_integrity import orient_connected_shells
    section_area = section_signed_area_xz([(v.x, v.z) for v in profile])
    if profile_closed and abs(section_area) <= EPS * EPS:
        raise ValueError(
            'Карниз: замкнутый профиль имеет нулевую площадь сечения; '
            'наружное направление нормалей не определено'
        )
    if not profile_closed:
        log_event(
            f'CORNICE_OPEN_PROFILE_NORMAL_POLICY virtual_section_area={section_area:.9f}; '
            'outside=CONVENTIONAL_FRONT; mathematically_unique=False',
            'NORMAL_CHECK',
        )
    profile_samples, profile_length = resample_profile(profile, tile_length)
    distances = [0.0]
    for start, end in zip(path, path[1:]):
        # The generated sweep follows the complete 3D path. Using XY-only
        # distance here made every inclined cornice receive too little U and
        # visibly stretched its texture.
        distances.append(distances[-1] + (end - start).length)
    total_length = distances[-1]
    path_cuts = set(distances)
    snapped_module_cuts = []
    geometric_seam_snaps = 0
    base_seam_clearance = REGULATION_WELD_DISTANCE * 2.1
    # The profile can straddle the authored path. Both sides MUST be measured:
    # a corner's inward side can collide even when the outer profile is safe.
    profile_signed_offsets = [float(point.x) for point, _distance in profile_samples]
    profile_max_offset = max((abs(x) for x in profile_signed_offsets), default=0.0)
    from .cornice_corner_preflight import inspect_cornice_reach
    corner_reach_report=inspect_cornice_reach(
        [(p.x,p.y,p.z) for p in path],profile_signed_offsets,
        closed=is_path_closed(path))
    log_event('CORNICE_TRIM_REACH status={}; checked={}; risk_corners={}; '
              'authored_path_unchanged=True'.format(
                  corner_reach_report['status'],corner_reach_report['checked'],
                  corner_reach_report['issues'][:12]),
              'CORNICE_GEOMETRY' if corner_reach_report['status']=='OK' else 'WARNING')
    log_event(
        f'CORNICE_PROFILE_ENVELOPE x_min={min(profile_signed_offsets,default=0.0):.9f}; '
        f'x_max={max(profile_signed_offsets,default=0.0):.9f}; '
        f'width={profile_max_offset:.9f}; check=SIGNED_BOTH_SIDES', 'CORNICE_GEOMETRY',
    )

    def module_seam_clearance(authored_index):
        """Return a safe no-split zone around one authored corner.

        A UV module cut that lands inside the tangential reach of a miter can
        create a tiny extra sweep strip. The strip does not change the source
        path, but it can partition one local miter fan into non-neighbouring
        polygons that overlap each other after welding. The required reach of
        the outermost profile rail is r*tan(turn/2). We use it only to decide
        whether to reuse the authored station; the source path itself is never
        moved. A hard 25 mm cap prevents a sharp 90-degree corner from moving
        a seam by hundreds of millimetres.
        """
        if authored_index <= 0 or authored_index >= len(path) - 1 or profile_max_offset <= EPS:
            return base_seam_clearance, 0.0, 0.0
        before = path[authored_index] - path[authored_index - 1]
        after = path[authored_index + 1] - path[authored_index]
        before.z = 0.0
        after.z = 0.0
        before_length = before.length
        after_length = after.length
        if before_length <= EPS or after_length <= EPS:
            return base_seam_clearance, 0.0, 0.0
        before /= before_length
        after /= after_length
        dot_value = max(-1.0, min(1.0, before.dot(after)))
        half_turn = 0.5 * acos(dot_value)
        cosine = cos(half_turn)
        miter_reach = 0.0 if abs(cosine) <= EPS else profile_max_offset * abs(sin(half_turn) / cosine)
        local_cap = min(
            MAX_CORNICE_SEAM_SNAP_DISTANCE,
            max(base_seam_clearance, min(before_length, after_length) * 0.25),
        )
        clearance = max(base_seam_clearance, min(miter_reach + base_seam_clearance, local_cap))
        return clearance, miter_reach, degrees(half_turn * 2.0)

    for index in range(1, int(total_length // tile_length) + 1):
        module_distance = index * tile_length
        if module_distance >= total_length - EPS:
            continue
        nearest_index = min(range(len(distances)), key=lambda item: abs(distances[item] - module_distance))
        nearest_authored = distances[nearest_index]
        separation = abs(nearest_authored - module_distance)
        safe_clearance, miter_reach, turn_deg = module_seam_clearance(nearest_index)
        if separation < safe_clearance:
            # Reuse the authored station instead of creating a micro-strip
            # inside its miter fan. This is a topology partitioning guard: the
            # authored path and the generated corner position remain unchanged.
            path_cuts.add(nearest_authored)
            geometric = separation >= base_seam_clearance
            geometric_seam_snaps += int(geometric)
            snapped_module_cuts.append((
                index, module_distance, nearest_authored, separation, safe_clearance,
                miter_reach, turn_deg, geometric,
            ))
        else:
            path_cuts.add(module_distance)
    if snapped_module_cuts:
        log_event(
            f"CORNICE_SEAM_SNAP count={len(snapped_module_cuts)}; "
            f"base_clearance={base_seam_clearance:.9f}; geometric_cap={MAX_CORNICE_SEAM_SNAP_DISTANCE:.9f}; "
            f"profile_max_offset={profile_max_offset:.9f}; geometric={geometric_seam_snaps}; "
            f"items={[(i, round(raw, 9), round(snap, 9), round(gap, 9), round(clearance, 9), round(reach, 9), round(turn, 6), geom) for i, raw, snap, gap, clearance, reach, turn, geom in snapped_module_cuts[:40]]}",
            "CORNICE_GEOMETRY",
        )
        if geometric_seam_snaps:
            log_event(
                f"CORNICE_SEAM_GEOMETRY_GUARD adjusted={geometric_seam_snaps}; "
                f"max_shift={max(item[3] for item in snapped_module_cuts if item[7]):.9f}; "
                "reason=module_cut_inside_local_miter_reach",
                "AUTO_FIX",
            )
    manual_corner_points = list(manual_corner_points or [])
    manual_corner_methods = list(manual_corner_methods or [])
    corner_match_tolerance = max(EPS * 10.0, REGULATION_WELD_DISTANCE * 0.1)
    from .corner_methods import MODE_IDS, select_frame
    for corner_point, method in manual_corner_methods:
        if method not in MODE_IDS:
            raise ValueError('Неизвестный вариант Sharp-угла: '+str(method))
        if not any((corner_point - point).length <= corner_match_tolerance
                   for point in manual_corner_points):
            raise ValueError('Метод угла назначен вершине, отсутствующей в Sharp списке')
    applied_corner_methods = {}
    side_sign = cornice_side_sign(path, flip_side)

    def source_vertex_index_for_world(point):
        """Resolve reordered production-path points back to authored Mesh vertices."""
        best_index = -1
        best_distance = float("inf")
        for vertex in path_obj.data.vertices:
            world_point = path_obj.matrix_world @ vertex.co
            distance = (world_point - point).length
            if distance < best_distance:
                best_distance = distance
                best_index = int(vertex.index)
        return best_index

    from .corner_trim import exact_miter_trim
    stations, limited_miters = [], 0
    limited_miter_records = []
    auto_corner_fallbacks = 0
    corner_problem_records = []
    for distance in sorted(path_cuts):
        authored = next((index for index, value in enumerate(distances) if abs(value - distance) <= EPS), None)
        if authored is not None:
            point = path[authored].copy()
            frame, scale, limited = cornice_vertex_frame(path, authored, side_sign)
            metrics = cornice_corner_metrics(path, authored)
            raw_ratio = float(metrics["miter_ratio"])
            turn_deg = float(metrics["turn_deg"])
            authored_sharp = any((path[authored] - manual_point).length <= corner_match_tolerance for manual_point in manual_corner_points)
            strategy = "AUTHORED_MITER" if authored_sharp else "MITER"
            selected_method = next((method for corner_point,method in manual_corner_methods
                if (path[authored] - corner_point).length <= corner_match_tolerance), 'CLASSIC')
            if authored_sharp:
                applied_corner_methods[str(int(source_vertex_index_for_world(point)))]=selected_method
                corner_count = len(path)-1 if is_path_closed(path) else len(path)
                if 0 < authored < corner_count-1 or is_path_closed(path):
                    pprev, pnext = path[(authored-1)%corner_count], path[(authored+1)%corner_count]
                    ax, ay = point.x-pprev.x, point.y-pprev.y
                    bx, by = pnext.x-point.x, pnext.y-point.y
                    signed_turn = ax*by-ay*bx
                    log_event(
                        f'CORNICE_SHARP_MIRROR source_vertex={source_vertex_index_for_world(point)}; '
                        f'method={selected_method}; signed_turn={signed_turn:.9f}; '
                        f'side_sign={side_sign:+.0f}; flip_side={flip_side}; '
                        f'flip_x={profile_flip_x}; flip_z={profile_flip_z}; '
                        f'reverse_path={reverse_path}; '
                        f'profile_x_range=({min(profile_signed_offsets):.6f},'
                        f'{max(profile_signed_offsets):.6f}); immutable_path=True',
                        'CORNICE_GEOMETRY',
                    )
            if authored_sharp and selected_method != 'CLASSIC':
                logical_last=len(path)-1 if is_path_closed(path) else len(path)
                if not is_path_closed(path) and authored in (0,logical_last-1):
                    raise ValueError('Экспериментальный угол: конец открытого пути не является углом')
                prev_point=path[(authored-1)%logical_last]
                next_point=path[(authored+1)%logical_last]
                normal,scale=select_frame(
                    (prev_point.x,prev_point.y),(point.x,point.y),
                    (next_point.x,next_point.y),side_sign,selected_method)
                frame=Vector((normal[0],normal[1],0.))
                strategy='EXP_'+selected_method
                # Experimental square/bisector variants are not true bisector
                # intersections; guard the longest tangential reach on both
                # adjacent segments, never auto-bevel or move the authored Path.
                p_tangent=(point-prev_point);p_tangent.z=0.
                n_tangent=(next_point-point);n_tangent.z=0.
                len_pre=p_tangent.length;len_next=n_tangent.length
                if min(len_pre,len_next)<=EPS:
                    raise ValueError('Экспериментальный угол: нулевой сегмент')
                p_tangent.normalize();n_tangent.normalize()
                displacement=frame*(scale*profile_max_offset)
                reach_prev=abs(displacement.dot(p_tangent))
                reach_next=abs(displacement.dot(n_tangent))
                if reach_prev>=len_pre*.47 or reach_next>=len_next*.47:
                    raise ValueError(
                        f'Экспериментальный угол {authored} ({selected_method}): '
                        'сечение перекрывает соседний сегмент; предыдущий Result сохранён')
                log_event(
                    f'CORNICE_CORNER_METHOD source_vertex={source_vertex_index_for_world(point)}; '
                    f'method={selected_method}; scale={scale:.9f}; '
                    f'reach_prev={reach_prev:.6f}; reach_next={reach_next:.6f}; '
                    'path_modified=False; UV_MODULES_UNCHANGED=True', 'CORNICE_GEOMETRY')
            # Exact bisector-plane trim: no bevel, rounding or authored-vertex
            # movement. The former fixed 4x limit rejected valid steep turns
            # even when both neighboring straight lengths were sufficient.
            if authored not in {0, len(path)-1} or is_path_closed(path):
                logical_last = len(path)-1 if is_path_closed(path) else len(path)
                before = path[(authored-1)%logical_last]
                after = path[(authored+1)%logical_last]
                before_xy = horizontal_length(before, point)
                after_xy = horizontal_length(point, after)
                try:
                    trim_ratio, trim_reach = exact_miter_trim(
                        turn_deg, profile_max_offset, before_xy, after_xy,
                        weld=REGULATION_WELD_DISTANCE,
                    )
                    log_event(
                        f'CORNICE_EXACT_TRIM corner={authored}; ratio={trim_ratio:.6f}; '
                        f'reach_m={trim_reach:.6f}; before_xy={before_xy:.6f}; '
                        f'after_xy={after_xy:.6f}; authored_path_unchanged=True',
                        'CORNICE_GEOMETRY',
                    )
                except ValueError as trim_error:
                    # A distorted or overlapping cornice is not a successful
                    # result. Fail before creating/replacing any Result mesh.
                    limited = True
                    strategy = "UNSAFE_MITER"
                    corner_problem_records.append({
                        'vertex': int(authored),
                        'source_vertex_index': int(source_vertex_index_for_world(point)),
                        'position': [float(point.x), float(point.y), float(point.z)],
                        'turn_deg': float(turn_deg),
                        'miter_ratio': float(raw_ratio),
                        'strategy': strategy,
                        'authored_sharp': bool(authored_sharp),
                        'reason': str(trim_error),
                    })
                    log_event(f'CORNICE_EXACT_TRIM_UNSAFE corner={authored}; reason={trim_error}', 'ERROR')
            if limited:
                strategy = "UNSAFE_MITER"
            limited_miters += int(limited)
            if limited:
                limited_miter_records.append((authored, distance, scale))
            if strategy in {"CLIPPED_MITER", "SAFE_FALLBACK"}:
                corner_problem_records.append({
                    "vertex": int(authored),
                    "source_vertex_index": int(source_vertex_index_for_world(point)),
                    "position": [float(point.x), float(point.y), float(point.z)],
                    "turn_deg": float(turn_deg),
                    "miter_ratio": float(raw_ratio),
                    "strategy": strategy,
                    "authored_sharp": bool(authored_sharp),
                })
            if len(path) <= 200 or authored in {0, len(path) - 1} or limited or abs(scale - 1.0) >= 0.01 or strategy != "MITER":
                log_event(
                    f"CORNICE_CORNER path_vertex={authored}; distance={distance:.9f}; "
                    f"position=({point.x:.9f},{point.y:.9f},{point.z:.9f}); "
                    f"miter=({frame.x:.9f},{frame.y:.9f}); scale={scale:.9f}; "
                    f"turn_deg={turn_deg:.6f}; raw_miter_ratio={raw_ratio:.6f}; authored_sharp={authored_sharp}; "
                    f"limited={limited}; strategy={strategy}",
                    "CORNICE_GEOMETRY",
                )
        else:
            segment = next(index for index in range(len(distances)-1) if distances[index] < distance < distances[index+1])
            factor = (distance - distances[segment]) / (distances[segment+1] - distances[segment])
            point = path[segment].lerp(path[segment+1], factor)
            direction = path[segment+1] - path[segment]; direction.z = 0.0; direction.normalize()
            frame = Vector((-direction.y, direction.x, 0.0)) * side_sign
            scale, limited = 1.0, False
        stations.append((point, frame, scale, distance))
    if limited_miter_records:
        raise ValueError(
            "Карниз: путь содержит почти обратный/слишком острый поворот, "
            "для которого miter пришлось бы искусственно ограничить. "
            "Точный срез физически пересекает соседние участки; уменьшите вынос профиля "
            "или длину конфликтующей части сечения, не меняя путь: "
            f"{limited_miter_records[:10]}"
        )
    if is_path_closed(path) and abs(stations[-1][3] - total_length) > EPS:
        stations.append((stations[0][0].copy(), stations[0][1].copy(), stations[0][2], total_length))

    manual_corner_station_indices = set()
    for corner_point in manual_corner_points:
        matches = [
            index for index, station in enumerate(stations[:-1])
            if (station[0] - corner_point).length <= corner_match_tolerance
        ]
        if not matches:
            raise ValueError(
                "Отмеченный угол больше не существует в текущем пути. "
                "Обновите список углов"
            )
        manual_corner_station_indices.add(matches[0])
    if uv_mode == "MAX_CLIPPED" and not manual_corner_station_indices:
        raise ValueError(
            "Для режима «Острые углы · Sharp» отметьте хотя бы одну вершину пути как Sharp"
        )
    log_event(
        f"CORNICE_MANUAL_CORNERS requested={len(manual_corner_points)}; "
        f"matched={len(manual_corner_station_indices)}; stations={sorted(manual_corner_station_indices)}",
        "CORNICE_UV",
    )
    manual_corner_distances = sorted(
        stations[index][3] for index in manual_corner_station_indices
    )

    # Build the complete geometry grid first.  Every profile point is a
    # separate rail around the path and therefore has its own real length.
    # A single center-line distance stretches outer/inner corner faces.
    grid = []
    # BETA-compatible world-up sweep: path station Z follows the authored 3D
    # path, while profile Z stays architectural/world vertical. Transporting
    # an Up vector along a sloped path tilted the cross-section itself and was
    # the main geometric divergence from the known-good BETA implementation.
    max_up_tilt = 0.0
    max_frame_delta = 0.0
    for point, frame, scale, _distance in stations:
        grid.append([
            point + frame * (profile_point.x * scale) + Vector((0.0, 0.0, profile_point.z))
            for profile_point, _profile_distance in profile_samples
        ])
    log_event(
        f"CORNICE_FRAME_POLICY policy=WORLD_Z_BETA_COMPAT; stations={len(stations)}; "
        f"full_3d_u=True; auto_corner_fallbacks={auto_corner_fallbacks}",
        "CORNICE_GEOMETRY",
    )
    # Mirror-aware 2D projection guard: a single centerline can look valid
    # while a negative-X rail collapses or folds at a reflected sharp corner.
    # Test *every* signed profile rail before creating a Mesh. The check is
    # invariant to UV layout and never edits the authored Path or section.
    from .mirror_corner_integrity import audit_sweep_strips, require_valid_sweep
    mirror_geometry_report = audit_sweep_strips(
        stations, [p.x for p, _d in profile_samples], side_sign=int(side_sign),
        mirror_side=flip_side, mirror_x=profile_flip_x,
        mirror_z=profile_flip_z, reverse_path=reverse_path,
    )
    log_event(
        f'CORNICE_MIRROR_GEOMETRY status={mirror_geometry_report["status"]}; '
        f'mirror_side={bool(flip_side)}; profile_flip_x={bool(profile_flip_x)}; '
        f'profile_flip_z={bool(profile_flip_z)}; reverse_path={bool(reverse_path)}; '
        f'effective_mirror_parity={mirror_geometry_report["effective_mirror_parity"]}; '
        f'full_orientation_parity={mirror_geometry_report["full_orientation_parity"]}; '
        f'signed_x=[{mirror_geometry_report["signed_x_min"]:.6f},'
        f'{mirror_geometry_report["signed_x_max"]:.6f}]; '
        f'min_forward_m={mirror_geometry_report["minimum_forward_m"]}; '
        f'min_jacobian={mirror_geometry_report["minimum_triangle_ratio"]}; '
        f'issues={mirror_geometry_report["issue_count"]}; '
        f'examples={mirror_geometry_report["issues"][:3]}',
        'CORNICE_GEOMETRY' if mirror_geometry_report['status'] == 'OK' else 'ERROR',
    )
    require_valid_sweep(mirror_geometry_report)

    seam_key_scale = 1.0 / max(EPS * 10.0, REGULATION_WELD_DISTANCE * 0.1)

    def seam_point_key(point):
        return tuple(int(round(value * seam_key_scale)) for value in point)

    manual_seam_keys = set()
    for station_index in manual_corner_station_indices:
        for profile_index in range(len(profile_samples) - 1):
            first_key = seam_point_key(grid[station_index][profile_index])
            second_key = seam_point_key(grid[station_index][profile_index + 1])
            manual_seam_keys.add(tuple(sorted((first_key, second_key))))
    rail_distances = []
    for profile_index in range(len(profile_samples)):
        cumulative = [0.0]
        for station_index in range(len(stations) - 1):
            cumulative.append(
                cumulative[-1]
                + (grid[station_index + 1][profile_index] - grid[station_index][profile_index]).length
            )
        rail_distances.append(cumulative)

    base_face_estimate = max(0, len(stations) - 1) * max(0, len(profile_samples) - 1)
    if base_face_estimate > MAX_GENERATED_FACES:
        raise ValueError(
            f"Карниз слишком сложный: минимум {base_face_estimate} граней, "
            f"безопасный предел {MAX_GENERATED_FACES}. Упростите путь или профиль"
        )

    vertices, faces, face_uvs = [], [], []
    generated_uv_cuts = 0
    forced_seam_resets = 0
    max_old_center_stretch = 1.0
    max_new_edge_error = 0.0
    independent_uv_islands = 0
    max_face_planar_deviation = 0.0
    max_face_uv_span = 0.0
    near_degenerate_planar_faces = 0
    profile_array_sections = 0
    adaptive_cuts = 0
    adaptive_capped_intervals = 0
    max_rail_mismatch = 0.0
    max2_continuous_intervals = 0
    max2_clipped_intervals = 0
    max2_max_predicted_distortion = 0.0
    face_continuous_intervals = 0
    face_clipped_intervals = 0
    distortion_warning_records = []
    manual_projection_cuts = 0
    for station_index in range(len(stations) - 1):
        interval_start_index = station_index
        interval_end_index = station_index + 1
        if is_path_closed(path) and interval_end_index == len(stations) - 1:
            interval_end_index = 0
        starts_at_manual_corner = interval_start_index in manual_corner_station_indices
        ends_at_manual_corner = interval_end_index in manual_corner_station_indices
        station_start_distance = stations[station_index][3]
        section_origin_distance = max(
            (
                distance for distance in manual_corner_distances
                if distance <= station_start_distance + EPS
            ),
            default=0.0,
        )
        center_span = max(EPS, stations[station_index + 1][3] - stations[station_index][3])
        rail_spans = [
            rail_distances[profile_index][station_index + 1]
            - rail_distances[profile_index][station_index]
            for profile_index in range(len(profile_samples))
        ]
        center_3d_span = max(
            EPS,
            (stations[station_index + 1][0] - stations[station_index][0]).length,
        )
        max2_absolute_error = max(
            (abs(span - center_3d_span) for span in rail_spans),
            default=0.0,
        )
        max2_relative_error = max2_absolute_error / center_3d_span
        max2_max_predicted_distortion = max(
            max2_max_predicted_distortion,
            max2_relative_error,
        )
        # Distortion remains diagnostic only. In 3ds Max 2 the user, not a
        # hard-coded angle/error threshold, decides where clipping starts.
        max2_error_limit = max(0.010, center_3d_span * 0.05)
        max2_needs_clip = (
            uv_mode == "MAX_CLIPPED"
            and (starts_at_manual_corner or ends_at_manual_corner)
        )
        if max2_relative_error > 0.05 and (
            uv_mode == "MAX_REAL_WORLD"
            or (uv_mode == "MAX_CLIPPED" and not max2_needs_clip)
        ):
            distortion_warning_records.append((
                station_index,
                stations[station_index][3],
                stations[station_index + 1][3],
                max2_relative_error,
            ))
        face_needs_clip = (
            uv_mode == "FACE_REAL_WORLD"
            and max2_absolute_error > max2_error_limit
        )
        if uv_mode == "MAX_CLIPPED":
            if max2_needs_clip:
                max2_clipped_intervals += 1
                forced_seam_resets += 1
            else:
                max2_continuous_intervals += 1
            if max2_needs_clip or max2_relative_error >= 0.005:
                log_event(
                    f"MAX2_DECISION interval={station_index}; "
                    f"distance={stations[station_index][3]:.9f}->{stations[station_index + 1][3]:.9f}; "
                    f"center_span={center_3d_span:.9f}; rail_error={max2_absolute_error:.9f}; "
                    f"relative={max2_relative_error:.9f}; limit={max2_error_limit:.9f}; "
                    f"starts_at_manual_corner={starts_at_manual_corner}; "
                    f"ends_at_manual_corner={ends_at_manual_corner}; "
                    f"action={'CLIP' if max2_needs_clip else 'CONTINUE'}",
                    "CORNICE_UV",
                )
        if uv_mode == "FACE_REAL_WORLD":
            if face_needs_clip:
                face_clipped_intervals += 1
            else:
                face_continuous_intervals += 1
            if face_needs_clip or max2_relative_error >= 0.005:
                log_event(
                    f"FACE_UV_DECISION interval={station_index}; "
                    f"distance={stations[station_index][3]:.9f}->{stations[station_index + 1][3]:.9f}; "
                    f"center_span={center_3d_span:.9f}; rail_error={max2_absolute_error:.9f}; "
                    f"relative={max2_relative_error:.9f}; limit={max2_error_limit:.9f}; "
                    f"action={'CLIP' if face_needs_clip else 'CONTINUE'}",
                    "CORNICE_UV",
                )
        if uv_mode == "FACE_REAL_WORLD":
            # Union of exact module crossings for every longitudinal rail.
            # This keeps each profile band continuous through authored path
            # vertices and cuts all bands only where one rail reaches a UV
            # boundary, so the resulting topology remains aligned.
            factor_set = {0.0, 1.0}
            for rail_index in range(len(rail_spans) - 1):
                start_distance = (
                    rail_distances[rail_index][station_index]
                    + rail_distances[rail_index + 1][station_index]
                ) * 0.5
                end_distance = (
                    rail_distances[rail_index][station_index + 1]
                    + rail_distances[rail_index + 1][station_index + 1]
                ) * 0.5
                span = end_distance - start_distance
                if span <= EPS:
                    continue
                first_boundary = int(floor(start_distance / tile_length)) + 1
                last_boundary = int(floor((end_distance - EPS) / tile_length))
                for boundary_index in range(first_boundary, last_boundary + 1):
                    factor = (boundary_index * tile_length - start_distance) / span
                    if EPS < factor < 1.0 - EPS:
                        factor_set.add(factor)
            factors = sorted(factor_set)
        elif uv_mode == "MAX_CLIPPED" and max2_needs_clip:
            # Restore the proven 3.4.4 projection rule. The whole authored
            # straight interval keeps one projection direction; if its outer
            # miter rail is longer than one tile, split the interval into the
            # minimum number of equal straight pieces. Do not introduce a
            # different cut position for every rail: that changes the visual
            # method and creates unnecessary micro-sections near the corner.
            subdivisions = max(
                1,
                int(ceil(max(rail_spans, default=center_3d_span) / tile_length)),
            )
            factors = [index / subdivisions for index in range(subdivisions + 1)]
            manual_projection_cuts += max(0, subdivisions - 1)
        elif uv_mode in {"MAX_REAL_WORLD", "MAX_CLIPPED"}:
            # Cut at the exact boundaries of the distance system used by the
            # UV mode. MAX_CLIPPED restarts its distance at every user corner,
            # so global 4 m cuts are not sufficient after a manual seam.
            factor_set = {0.0, 1.0}
            phase_origin = section_origin_distance if uv_mode == "MAX_CLIPPED" else 0.0
            interval_d0 = stations[station_index][3] - phase_origin
            interval_d1 = stations[station_index + 1][3] - phase_origin
            interval_span = interval_d1 - interval_d0
            if interval_span > EPS:
                first_boundary = int(floor(interval_d0 / tile_length)) + 1
                last_boundary = int(floor((interval_d1 - EPS) / tile_length))
                for boundary_index in range(first_boundary, last_boundary + 1):
                    factor = (boundary_index * tile_length - interval_d0) / interval_span
                    if EPS < factor < 1.0 - EPS:
                        factor_set.add(factor)
            factors = sorted(factor_set)
        else:
            subdivisions = max(1, int(ceil(max(rail_spans, default=0.0) / tile_length)))
            factors = [index / subdivisions for index in range(subdivisions + 1)]
        if corner_mode == "ADAPTIVE":
            # Refine only intervals where neighboring profile rails travel
            # noticeably different distances. Added points lie on existing
            # generated edges, so neither the source path nor the swept shape
            # changes. The 2 mm regulation value is also the target maximum
            # rail mismatch per generated section.
            mismatch = max(
                (abs(a - b) for a, b in zip(rail_spans, rail_spans[1:])),
                default=0.0,
            )
            max_rail_mismatch = max(max_rail_mismatch, mismatch)
            adaptive_parts = max(1, int(ceil(mismatch / REGULATION_WELD_DISTANCE)))
            positive_spans = [span for span in rail_spans if span > EPS]
            shortest_span = min(positive_spans, default=0.0)
            # Never generate sections that the mandatory 0.002 m weld would
            # immediately collapse. Keep a small numeric safety margin.
            spacing_cap = max(1, int(floor(shortest_span / (REGULATION_WELD_DISTANCE * 2.1))))
            safe_cap = min(512, spacing_cap)
            if adaptive_parts > safe_cap:
                adaptive_parts = safe_cap
                adaptive_capped_intervals += 1
            if adaptive_parts > 1:
                old_count = len(factors)
                factors = sorted(set(factors) | {index / adaptive_parts for index in range(1, adaptive_parts)})
                adaptive_cuts += len(factors) - old_count
        generated_uv_cuts += max(0, len(factors) - 2)
        for profile_index in range(len(profile_samples) - 1):
            length_a = rail_spans[profile_index]
            length_b = rail_spans[profile_index + 1]
            max_old_center_stretch = max(
                max_old_center_stretch,
                length_a / center_span,
                length_b / center_span,
                center_span / max(EPS, length_a),
                center_span / max(EPS, length_b),
            )
            (_q0, v0), (_q1, v1) = profile_samples[profile_index:profile_index + 2]
            v_tile = floor(((v0 + v1) * 0.5) / tile_length)
            for factor0, factor1 in zip(factors, factors[1:]):
                if factor1 - factor0 <= EPS:
                    continue
                quad = [
                    grid[station_index][profile_index].lerp(grid[station_index + 1][profile_index], factor0),
                    grid[station_index][profile_index].lerp(grid[station_index + 1][profile_index], factor1),
                    grid[station_index][profile_index + 1].lerp(grid[station_index + 1][profile_index + 1], factor1),
                    grid[station_index][profile_index + 1].lerp(grid[station_index + 1][profile_index + 1], factor0),
                ]
                segment_a = length_a * (factor1 - factor0)
                segment_b = length_b * (factor1 - factor0)
                if uv_mode == "FACE_REAL_WORLD":
                    center_start = (
                        rail_distances[profile_index][station_index]
                        + rail_distances[profile_index + 1][station_index]
                    ) * 0.5
                    center_end = (
                        rail_distances[profile_index][station_index + 1]
                        + rail_distances[profile_index + 1][station_index + 1]
                    ) * 0.5
                    distance0 = center_start + (center_end - center_start) * factor0
                    distance1 = center_start + (center_end - center_start) * factor1
                    array_tile = floor(((distance0 + distance1) * 0.5) / tile_length)
                    width_start = (quad[3] - quad[0]).length
                    width_end = (quad[2] - quad[1]).length
                    if not face_needs_clip:
                        params = [
                            (distance0 / tile_length - array_tile, 0.0),
                            (distance1 / tile_length - array_tile, 0.0),
                            (distance1 / tile_length - array_tile, width_end / tile_length),
                            (distance0 / tile_length - array_tile, width_start / tile_length),
                        ]
                    else:
                        center_start_point = stations[station_index][0]
                        axis_u = stations[station_index + 1][0] - center_start_point
                        if axis_u.length <= EPS:
                            raise ValueError("Карниз: нулевая длина прямого участка")
                        axis_u.normalize()
                        projected_u = [(point - center_start_point).dot(axis_u) for point in quad]
                        min_projected = min(projected_u)
                        projected_span = max(projected_u) - min_projected
                        if projected_span > tile_length + REGULATION_WELD_DISTANCE:
                            raise ValueError(
                                "Карниз: подрезанная грань не помещается в UV-модуль; "
                                f"проекция={projected_span:.6f} м, модуль={tile_length:.6f} м"
                            )
                        raw_u = [(stations[station_index][3] + value) / tile_length for value in projected_u]
                        tile_min = floor(min(raw_u) + EPS)
                        tile_max = floor(max(raw_u) - EPS)
                        if tile_min == tile_max:
                            mapped_u = [value - tile_min for value in raw_u]
                        else:
                            mapped_u = [(value - min_projected) / tile_length for value in projected_u]
                            forced_seam_resets += 1
                        params = [
                            (mapped_u[0], 0.0),
                            (mapped_u[1], 0.0),
                            (mapped_u[2], width_end / tile_length),
                            (mapped_u[3], width_start / tile_length),
                        ]
                    max_face_uv_span = max(
                        max_face_uv_span, segment_a, segment_b, width_start, width_end,
                    )
                    profile_array_sections += 1
                elif uv_mode == "HYBRID_PLANAR":
                    # Every physical profile band is an independent island.
                    # Project the actual swept quad to
                    # its own orthonormal plane in metres. This deliberately
                    # trades continuity across natural profile edges for exact
                    # texel density and removes trapezoid/corner stretching.
                    origin = quad[0]
                    longitudinal = (quad[1] - quad[0]) + (quad[2] - quad[3])
                    if longitudinal.length <= EPS * EPS:
                        longitudinal = quad[1] - quad[0]
                    if longitudinal.length <= EPS * EPS:
                        raise ValueError("Карниз: вырожденная продольная грань")
                    axis_u = longitudinal.normalized()
                    normal = (quad[1] - quad[0]).cross(quad[3] - quad[0])
                    if normal.length <= EPS * EPS:
                        normal = (quad[2] - quad[1]).cross(quad[0] - quad[1])
                    if normal.length <= EPS * EPS:
                        raise ValueError("Карниз: вырожденная полоса профиля")
                    if normal.length <= EPS:
                        near_degenerate_planar_faces += 1
                    normal.normalize()
                    axis_v = normal.cross(axis_u).normalized()
                    if axis_v.dot(quad[3] - quad[0]) < 0.0:
                        axis_v.negate()
                    projected = [
                        ((point - origin).dot(axis_u), (point - origin).dot(axis_v))
                        for point in quad
                    ]
                    min_u = min(value[0] for value in projected)
                    max_u = max(value[0] for value in projected)
                    min_v = min(value[1] for value in projected)
                    max_v = max(value[1] for value in projected)
                    span_u, span_v = max_u - min_u, max_v - min_v
                    max_face_uv_span = max(max_face_uv_span, span_u, span_v)
                    if span_u > tile_length + REGULATION_WELD_DISTANCE or span_v > tile_length + REGULATION_WELD_DISTANCE:
                        raise ValueError(
                            f"Карниз: полоса не помещается в UV-модуль {tile_length:g} м "
                            f"(размер проекции {span_u:.6f}×{span_v:.6f} м)"
                        )
                    params = [
                        ((u - min_u) / tile_length, (v - min_v) / tile_length)
                        for u, v in projected
                    ]
                    # Keep the global path phase when it remains inside the
                    # source tile. Otherwise retain the exact local scale and
                    # introduce a safe UV seam for this face.
                    station_d0 = stations[station_index][3]
                    station_d1 = stations[station_index + 1][3]
                    d_start = station_d0 + (station_d1 - station_d0) * factor0
                    global_phase = (d_start / tile_length) % 1.0
                    if global_phase + span_u / tile_length <= 1.0 + EPS:
                        params = [(u + global_phase, v) for u, v in params]
                    else:
                        forced_seam_resets += 1
                    max_face_planar_deviation = max(
                        max_face_planar_deviation,
                        *(abs((point - origin).dot(normal)) for point in quad),
                    )
                    independent_uv_islands += 1
                elif uv_mode == "MAX_REAL_WORLD":
                    # Autodesk Sweep convention in real scene units: one axis
                    # around the section, the other along the base spline.
                    # Geometry was already cut at both 2/4 m module borders.
                    station_d0 = stations[station_index][3]
                    station_d1 = stations[station_index + 1][3]
                    d_start = station_d0 + (station_d1 - station_d0) * factor0
                    d_end = station_d0 + (station_d1 - station_d0) * factor1
                    path_tile = floor(((d_start + d_end) * 0.5) / tile_length)
                    u0 = d_start / tile_length - path_tile
                    u1 = d_end / tile_length - path_tile
                    params = [
                        (u0, v0 / tile_length - v_tile),
                        (u1, v0 / tile_length - v_tile),
                        (u1, v1 / tile_length - v_tile),
                        (u0, v1 / tile_length - v_tile),
                    ]
                elif uv_mode == "MAX_CLIPPED":
                    if not max2_needs_clip:
                        # Inside one user-defined section the mapping follows
                        # 3ds Max real-world scale. After a marked corner the
                        # new section restarts from local distance zero.
                        station_d0 = stations[station_index][3]
                        station_d1 = stations[station_index + 1][3]
                        d_start = station_d0 + (station_d1 - station_d0) * factor0 - section_origin_distance
                        d_end = station_d0 + (station_d1 - station_d0) * factor1 - section_origin_distance
                        path_tile = floor(((d_start + d_end) * 0.5) / tile_length)
                        u0 = d_start / tile_length - path_tile
                        u1 = d_end / tile_length - path_tile
                        params = [
                            (u0, v0 / tile_length - v_tile),
                            (u1, v0 / tile_length - v_tile),
                            (u1, v1 / tile_length - v_tile),
                            (u0, v1 / tile_length - v_tile),
                        ]
                    else:
                        # Both faces touching a manual corner are projected in
                        # their own straight-segment axes. The incoming plane
                        # is clipped at the seam; the outgoing plane starts at
                        # the same seam and only then follows the new heading.
                        center_start = stations[station_index][0]
                        center_end = stations[station_index + 1][0]
                        axis_u = center_end - center_start
                        if axis_u.length <= EPS:
                            raise ValueError("Карниз: нулевая длина прямого участка")
                        axis_u.normalize()
                        projected_u = [(point - center_start).dot(axis_u) for point in quad]
                        min_projected = min(projected_u)
                        max_projected = max(projected_u)
                        projected_span = max_projected - min_projected
                        projection_denominator = tile_length
                        if projected_span > tile_length + REGULATION_WELD_DISTANCE:
                            # A miter rail can exceed the center-line module by
                            # a few millimetres after geometric seam snapping.
                            # Rejecting the whole cornice is worse than the
                            # explicit MAX_CLIPPED contract: keep geometry and
                            # fit this isolated sharp-corner island exactly.
                            projection_denominator = projected_span
                            forced_seam_resets += 1
                            manual_projection_cuts += 1
                            log_event(
                                f"CORNICE_CLIPPED_FIT projection={projected_span:.9f}; "
                                f"module={tile_length:.9f}; compression={tile_length/projected_span:.9f}; "
                                "policy=LOCAL_SHARP_ISLAND",
                                "CORNICE_UV",
                            )
                        local_base_distance = (
                            stations[station_index][3] - section_origin_distance
                        )
                        raw_u = [
                            (local_base_distance + value) / tile_length
                            for value in projected_u
                        ]
                        tile_min = floor(min(raw_u) + EPS)
                        tile_max = floor(max(raw_u) - EPS)
                        if tile_min == tile_max:
                            mapped_u = [value - tile_min for value in raw_u]
                        else:
                            mapped_u = [(value - min_projected) / projection_denominator for value in projected_u]
                        params = [
                            (mapped_u[0], v0 / tile_length - v_tile),
                            (mapped_u[1], v0 / tile_length - v_tile),
                            (mapped_u[2], v1 / tile_length - v_tile),
                            (mapped_u[3], v1 / tile_length - v_tile),
                        ]
                else:
                    # Corner sections keep exact real lengths of both miter
                    # rails. The UV phase restarts only when the trapezoid no
                    # longer fits into the current source tile.
                    global_phase = (stations[station_index][3] / tile_length) % 1.0
                    largest_u_span = max(segment_a, segment_b) / tile_length
                    if global_phase + largest_u_span <= 1.0 + EPS:
                        start_u = global_phase
                    else:
                        start_u = 0.0
                        forced_seam_resets += 1
                    params = [
                        (start_u, v0 / tile_length - v_tile),
                        (start_u + segment_a / tile_length, v0 / tile_length - v_tile),
                        (start_u + segment_b / tile_length, v1 / tile_length - v_tile),
                        (start_u, v1 / tile_length - v_tile),
                    ]
                # Never hide a broken cut by clamping a large UV overflow.
                # That old behaviour collapsed part of a polygon onto the
                # tile border and appeared in Blender as unexplained stretch.
                uv_tolerance = max(EPS * 10.0, REGULATION_WELD_DISTANCE / tile_length)
                invalid_params = [
                    (index, u, v) for index, (u, v) in enumerate(params)
                    if u < -uv_tolerance or u > 1.0 + uv_tolerance
                    or v < -uv_tolerance or v > 1.0 + uv_tolerance
                ]
                if invalid_params:
                    raise ValueError(
                        "Карниз: внутренний UV-разрез вышел за границы тайла; "
                        f"интервал={station_index}, полоса={profile_index}, значения={invalid_params}"
                    )
                uvs = [
                    bilinear_source_uv(source, max(0.0, min(1.0, u)), max(0.0, min(1.0, v)))
                    for u, v in params
                ]
                # Audit every quad edge. Planar modes use both projected
                # axes; legacy modes intentionally measure longitudinal U.
                planar_uv = uv_mode in {"FACE_REAL_WORLD", "HYBRID_PLANAR"}
                edge_audit = ((0, 1), (1, 2), (2, 3), (3, 0)) if planar_uv else ((0, 1), (3, 2))
                for start_index, end_index in edge_audit:
                    length_3d = (quad[end_index] - quad[start_index]).length
                    uv_start, uv_end = params[start_index], params[end_index]
                    if planar_uv:
                        represented = (((uv_end[0] - uv_start[0]) ** 2 + (uv_end[1] - uv_start[1]) ** 2) ** 0.5) * tile_length
                    else:
                        represented = abs(uv_end[0] - uv_start[0]) * tile_length
                    max_new_edge_error = max(max_new_edge_error, abs(represented - length_3d))
                offset = len(vertices)
                if len(faces) >= MAX_GENERATED_FACES:
                    raise ValueError(
                        f"Карниз превысил безопасный предел {MAX_GENERATED_FACES} граней. "
                        "Упростите путь/профиль или увеличьте размер UV-модуля"
                    )
                vertices.extend(tuple(point) for point in quad)
                # The cross-section, not profile mirroring or path traversal,
                # determines which side of a solid cornice is exterior.
                # Keep UV values with their matching vertices on reversal.
                fwd=(0, 1, 2, 3)
                if abs(section_area)>EPS*EPS:
                    start_frame=stations[station_index][1]
                    end_frame=stations[station_index + 1][1]
                    frame_mid=start_frame+end_frame
                    if frame_mid.length <= EPS:
                        raise ValueError(
                            'Карниз: ориентация рамки у поворота неоднозначна; '
                            f'станция={station_index}, полоса={profile_index}'
                        )
                    frame_mid.normalize()
                    p0=profile_samples[profile_index][0]
                    p1=profile_samples[profile_index+1][0]
                    decision=quad_outward_decision(
                        [tuple(v) for v in quad],
                        (frame_mid.x,frame_mid.y),
                        (p1.x-p0.x,p1.z-p0.z),section_area,
                    )
                    if decision == 0:
                        raise ValueError(
                            'Карниз: невозможно надёжно определить наружную сторону грани; '
                            f'станция={station_index}, полоса={profile_index}'
                        )
                    if decision<0:
                        fwd=(3,2,1,0)
                faces.append(tuple(offset+i for i in fwd))
                face_uvs.append(tuple(uvs[i] for i in fwd))

    mesh = bpy.data.meshes.new((output_name or "UV_Cornice") + "_Mesh")
    mesh.from_pydata(vertices, [], faces); mesh.update()
    set_face_roles(mesh, ["CORNICE"] * len(mesh.polygons))
    for material in source_obj.data.materials: mesh.materials.append(material)
    if mesh.materials:
        for polygon in mesh.polygons: polygon.material_index = min(source["material_index"], len(mesh.materials)-1)
    uv_layer = mesh.uv_layers.new(name=source_obj.data.uv_layers.active.name)
    for polygon, uvs in zip(mesh.polygons, face_uvs):
        for loop_index, uv in zip(polygon.loop_indices, uvs): uv_layer.data[loop_index].uv = uv
    # Cornice now follows the same semantic Weld contract as Wall/Surface.
    # Authored path/corner points are protected from the mandatory 2 mm weld.
    cornice_protected = list(path)
    if manual_corner_points:
        cornice_protected.extend(Vector(point) for point in manual_corner_points)
    smart_regulation_weld(mesh, protected_points=cornice_protected)
    bm = bmesh.new(); bm.from_mesh(mesh)
    bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=EPS)
    bm.to_mesh(mesh); bm.free(); mesh.update()
    merge_coincident_vertex_indices(mesh)
    # Every cornice strip uses the same union of cut factors. There are no
    # intentional T-junctions to repair; the generic stitch pass can mistake
    # very close sharp-corner cuts for T-junctions and create non-manifold
    # edges after the mandatory weld.
    stitched = 0
    sanitize_generated_topology(mesh)
    validate_face_role_coverage(mesh, {"CORNICE"})
    marked_seams = 0
    if manual_seam_keys:
        for edge in mesh.edges:
            first_key = seam_point_key(mesh.vertices[edge.vertices[0]].co)
            second_key = seam_point_key(mesh.vertices[edge.vertices[1]].co)
            if tuple(sorted((first_key, second_key))) in manual_seam_keys:
                edge.use_seam = True
                marked_seams += 1
        mesh.update()
        if marked_seams == 0:
            raise ValueError(
                "Отмеченные углы найдены, но поперечный UV-разрез не был создан"
            )
    log_event(
        f"CORNICE_MANUAL_SEAMS requested_edges={len(manual_seam_keys)}; "
        f"marked_edges={marked_seams}; status={'OK' if marked_seams or not manual_seam_keys else 'FAILED'}",
        "CORNICE_UV",
    )
    from .module_seam_flags import mark_mesh_uv_seams
    _cornice_seams=mark_mesh_uv_seams(mesh)
    log_event(f'CORNICE_MODULE_SEAM_FLAGS {json.dumps(_cornice_seams, sort_keys=True)}', 'CORNICE_UV')
    try:
        validate_mesh_self_intersections(mesh)
    except ValueError as intersection_error:
        # Open paths do not have a unique mathematical "outside". The
        # deterministic default side can fold a wide profile into itself at
        # concave authored corners. In AUTO mode, retry exactly once on the
        # opposite side. Closed paths keep signed-area outside semantics, and
        # the manual flip remains an explicit user choice.
        if not flip_side and not is_path_closed(path):
            log_event(
                "CORNICE_SIDE_AUTO_RETRY reason=self_intersection; "
                "path_topology=OPEN; first_sign=-1; retry_sign=+1; "
                f"details={intersection_error}",
                "AUTO_FIX",
            )
            bpy.data.meshes.remove(mesh)
            return build_cornice(
                context, source_obj, path_obj, profile_obj, tile_length, output_name,
                True, uv_mode, corner_mode, linear_step,
                manual_corner_points=manual_corner_points,
                manual_corner_methods=manual_corner_methods,
                _auto_side_retry=True,
                profile_rotation_deg=profile_rotation_deg,
                profile_flip_x=profile_flip_x,
                profile_flip_z=profile_flip_z,
                reverse_path=reverse_path,
                target_obj=target_obj,
            )
        raise
    # Topology-consistent normals for *whole* shells, before publishing.
    # A side flip, profile reflection or reverse path never changes the
    # meaning of exterior on a closed profile.
    normal_report = orient_connected_shells(mesh, kind='CORNICE')
    log_event(
        f'CORNICE_NORMAL_INTEGRITY components={normal_report["components"]}; '
        f'closed={normal_report["closed"]}; open={normal_report["open"]}; '
        f'parity_flips={normal_report["parity_flips"]}; '
        f'outward_flips={normal_report["outward_flips"]}; '
        f'profile_closed={profile_closed}; mirror={flip_side}; '
        f'profile_flip_x={profile_flip_x}; profile_flip_z={profile_flip_z}; '
        f'reverse_path={reverse_path}',
        'NORMAL_CHECK',
    )
    preserve_mixed_wall_shading(mesh)
    validate_uv_faces_not_folded(mesh)
    uv_orientation_report = repair_uv_winding_islands(mesh, expected_positive=True)
    validate_uv_winding(mesh, expected_positive=True)
    validate_uv_safe_range(mesh, source)
    log_event(
        f"CORNICE_UV_INVARIANT repaired_islands={uv_orientation_report['repaired_islands']}; "
        f"repaired_faces={uv_orientation_report['repaired_faces']}; flipped=0", "UV_CHECK")
    obj, reused_stable = install_mesh_on_result(context, mesh, output_name or "UV_Cornice", target_obj)
    if reused_stable:
        log_event(f"STABLE_RESULT_REUSED object={obj.name}; mode=CORNICE", "LIFECYCLE")
    prepare_result_metadata(obj, "CORNICE")
    obj["path_object"] = path_obj.name; obj["profile_object"] = profile_obj.name
    obj["profile_length"] = profile_length
    obj["profile_perimeter"] = profile_length  # backward compatibility
    obj["profile_closed"] = bool(profile_closed)
    obj["uvwall_normal_integrity"] = json.dumps(normal_report, ensure_ascii=False)
    obj["side_flipped"] = bool(flip_side)
    obj["side_auto_flipped"] = bool(_auto_side_retry)
    obj["side_requested_flip"] = bool(flip_side and not _auto_side_retry)
    distortion_problem_records = []
    logical_authored_count = len(path) - 1 if is_path_closed(path) else len(path)
    authored_distances = distances[:logical_authored_count]
    for interval_index, start_distance, end_distance, relative_error in distortion_warning_records:
        # The diagnostic interval can start/end on a module cut. Attach it to
        # the closest authored corner so Problem Navigator can perform a real
        # "make Sharp" action instead of showing an anonymous global warning.
        if authored_distances:
            nearest_authored = min(
                range(len(authored_distances)),
                key=lambda index: min(
                    abs(authored_distances[index] - start_distance),
                    abs(authored_distances[index] - end_distance),
                ),
            )
            corner_point = path[nearest_authored].copy()
            source_vertex_index = source_vertex_index_for_world(corner_point)
        else:
            nearest_authored = -1
            corner_point = (stations[interval_index][0] + stations[interval_index + 1][0]) * 0.5
            source_vertex_index = source_vertex_index_for_world(corner_point)
        distortion_problem_records.append({
            "interval": int(interval_index),
            "start_distance": float(start_distance),
            "end_distance": float(end_distance),
            "relative_error": float(relative_error),
            "path_vertex": int(nearest_authored),
            "source_vertex_index": int(source_vertex_index),
            "position": [float(corner_point.x), float(corner_point.y), float(corner_point.z)],
            "authored_sharp": bool(any(
                (corner_point - manual_point).length <= corner_match_tolerance
                for manual_point in manual_corner_points
            )),
        })

    obj["cornice_uv_distortion_warnings"] = len(distortion_warning_records)
    obj["cornice_uv_distortion_records"] = json.dumps(distortion_problem_records, ensure_ascii=False)
    obj["cornice_uv_max_predicted_distortion"] = float(max2_max_predicted_distortion)
    obj["cornice_auto_corner_fallbacks"] = int(auto_corner_fallbacks)
    obj["cornice_mirror_geometry_json"] = json.dumps(mirror_geometry_report, sort_keys=True)
    obj["cornice_corner_methods_json"] = json.dumps(applied_corner_methods,sort_keys=True)
    log_event(f'CORNICE_METHOD_RESULT {obj["cornice_corner_methods_json"]}', 'CORNICE_GEOMETRY')
    obj["cornice_frame_max_up_tilt_deg"] = float(max_up_tilt)
    obj["cornice_frame_max_delta_deg"] = float(max_frame_delta)
    obj["cornice_corner_problem_records"] = json.dumps(corner_problem_records, ensure_ascii=False)
    if distortion_warning_records:
        log_event(
            f"CORNICE_UV_DISTORTION_WARNING count={len(distortion_warning_records)}; "
            f"threshold=0.05; records={[(i, round(a, 6), round(b, 6), round(error, 6)) for i, a, b, error in distortion_warning_records[:40]]}; "
            f"recommendation={'review_authored_sharp_corners' if uv_mode == 'MAX_CLIPPED' else 'mark_sharp_corners_or_switch_sharp_mode'}",
            "WARNING",
        )
    for selected in context.selected_objects: selected.select_set(False)
    obj.select_set(True); context.view_layer.objects.active = obj
    log_event(
        f"CORNICE_FINISH object={obj.name}; path_length={total_length:.9f}; "
        f"profile_topology={'CLOSED' if profile_closed else 'OPEN'}; profile_length={profile_length:.9f}; "
        f"profile_perimeter={profile_length:.9f}; "
        f"stations={len(stations)}; profile_stations={len(profile_samples)}; faces={len(mesh.polygons)}; "
        f"side={'AUTO_FLIPPED' if _auto_side_retry else ('FLIPPED' if flip_side else 'AUTO')}; limited_miters={limited_miters}; "
        f"corner_mode={corner_mode}; rounded_corners={corner_stats['rounded']}; "
        f"sharp_corners={corner_stats['sharp']}; virtual_points={corner_stats['inserted']}; "
        f"uv_mode={uv_mode}; rail_uv={'PROFILE_LONGITUDINAL_ARRAYS' if uv_mode == 'FACE_REAL_WORLD' else ('HYBRID_PLANAR_PHASE' if uv_mode == 'HYBRID_PLANAR' else ('CENTER_PATH' if uv_mode == 'MAX_REAL_WORLD' else ('SEGMENT_CLIPPED_PROJECTION' if uv_mode == 'MAX_CLIPPED' else 'CORNER_SEAMS')))}; "
        f"uv_boundary_cuts={generated_uv_cuts}; forced_seam_resets={forced_seam_resets}; t_stitches={stitched}; "
        f"manual_projection_cuts={manual_projection_cuts}; "
        f"profile_arrays={len(profile_samples)-1 if uv_mode == 'FACE_REAL_WORLD' else 0}; "
        f"profile_array_sections={profile_array_sections}; independent_uv_islands={independent_uv_islands}; "
        f"adaptive_cuts={adaptive_cuts}; adaptive_capped_intervals={adaptive_capped_intervals}; "
        f"seam_start_index={seam_start_index}; seam_clearance={seam_clearance:.9f}; "
        f"snapped_module_cuts={len(snapped_module_cuts)}; geometric_seam_snaps={geometric_seam_snaps}; "
        f"max_seam_snap={max((item[3] for item in snapped_module_cuts), default=0.0):.9f}; "
        f"max_rail_mismatch={max_rail_mismatch:.9f}; "
        f"max2_continuous_intervals={max2_continuous_intervals}; "
        f"max2_clipped_intervals={max2_clipped_intervals}; "
        f"max2_max_predicted_distortion={max2_max_predicted_distortion:.9f}; "
        f"face_continuous_intervals={face_continuous_intervals}; "
        f"face_clipped_intervals={face_clipped_intervals}; "
        f"max_face_uv_span={max_face_uv_span:.9f}; "
        f"max_face_planar_deviation={max_face_planar_deviation:.9f}; "
        f"near_degenerate_planar_faces={near_degenerate_planar_faces}; "
        f"old_center_stretch_max={max_old_center_stretch:.9f}; "
        f"new_longitudinal_error_max={max_new_edge_error:.9f}; end_caps=False",
        "CORNICE",
    )
    return obj, len(mesh.polygons)


