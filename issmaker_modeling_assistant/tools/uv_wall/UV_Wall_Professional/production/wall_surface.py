# AUTO-EXTRACTED production module from the 1.4.0 verified baseline.

# Runtime integration symbols are bound by the package composition root.
import bpy
import bmesh
import traceback
import webbrowser
from datetime import datetime
from time import perf_counter
from math import ceil, floor, cos, sin, radians, isfinite, acos, degrees
from mathutils import Vector
from mathutils import kdtree
from mathutils.bvhtree import BVHTree
from mathutils.geometry import tessellate_polygon

from .vertical_uv import vertical_levels, uv_v_factor

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
from .rotation import rotated_wall_cells

def clean_polygon_strict(points, min_dist):
    """Remove near-duplicates; keep shape. min_dist in world units."""
    if not points:
        return []
    result = [points[0].copy()]
    for point in points[1:]:
        if (point - result[-1]).length > min_dist:
            result.append(point.copy())
    if len(result) > 1 and (result[0] - result[-1]).length <= min_dist:
        result.pop()
    return result


def clip_wall_segment_to_z_slab(p0, z0_bot, z0_top, p1, z1_bot, z1_top, z_lo, z_hi):
    """Clip one vertical wall segment (trapezoid) to a horizontal Z slab."""
    if z0_top < z0_bot:
        z0_bot, z0_top = z0_top, z0_bot
    if z1_top < z1_bot:
        z1_bot, z1_top = z1_top, z1_bot
    poly = [
        Vector((p0.x, p0.y, z0_bot)),
        Vector((p1.x, p1.y, z1_bot)),
        Vector((p1.x, p1.y, z1_top)),
        Vector((p0.x, p0.y, z0_top)),
    ]
    clipped = clip_polygon_axis(poly, "z", z_lo, True)
    clipped = clip_polygon_axis(clipped, "z", z_hi, False)
    return clean_polygon(clipped)


def build_wall_surface(
    path_points,
    source,
    tile_length,
    is_inner=False,
    inset_side="LEFT",
    include_cap_grid_cuts=False,
    extra_cut_distances=None,
    stories=1,
    angle_rad=0.0,
    first_story_height=0.0,
    align_bottom=False,
    global_bottom_z=None,
    stories_down=True,
    module_anchor_z=0.0,
    uv_anchor_z=None,
    uv_origin_mode="PATH",
    outer_normal_side=None,
    module_angle_rad=0.0,
    station_cut_distances=None,
    vertex_wall_heights=None,
):
    """Build MAIN or SECONDARY facade with path-anchored Z-module bands.

    stories_down=True:  path Z = top, floors extend downward.
    stories_down=False: path Z = bottom, floors extend upward.
    First story may use a custom height; the rest use the source plane height.
    If align_bottom: shared bottom Z (global_bottom_z); modules fill the gap.
    Physical module levels use the one authored Path extremum. The optional
    legacy WORLD UV phase may add UV cuts, never replace physical grid cuts.
    Each facade still owns independent XY stations and normal orientation.
    Along-path UV uses horizontal (XY) arc length.
    """
    stories = max(1, int(stories))
    closed = is_path_closed(path_points)
    distances, cuts = wall_station_data(
        path_points,
        tile_length,
        include_cap_grid_cuts,
        extra_cut_distances,
        angle_rad=angle_rad,
    )

    if station_cut_distances is not None:
        # Shared exact segment-fraction stationing for paired high-Z walls.
        # No private seam is allowed to introduce a different inner Z(t).
        cuts = list(station_cut_distances)
        if (not cuts or abs(cuts[0]-distances[0]) > 1e-6 or
            abs(cuts[-1]-distances[-1]) > 1e-6 or
            any(end <= start for start, end in zip(cuts, cuts[1:]))):
            raise ValueError('Несогласованные станции внутренней/внешней стены')

    height = source["height"]
    if height <= EPS:
        raise ValueError("Высота исходного плейна должна быть больше нуля")

    wall_h = total_wall_height(height, stories, first_story_height)
    if vertex_wall_heights is not None:
        from .inner_height import height_at_station, validate_vertex_heights
        vertex_wall_heights = validate_vertex_heights(vertex_wall_heights, len(path_points))

    role = "SECONDARY" if is_inner else "PRIMARY"
    # Stations: path is top (stories_down) or bottom (stories up).
    stations = []
    for distance in cuts:
        base = sample_polyline(path_points, distances, distance)
        local_h = (height_at_station(distances, vertex_wall_heights, distance)
                   if vertex_wall_heights is not None else wall_h)
        if stories_down:
            z_top = base.z
            if align_bottom and global_bottom_z is not None:
                z_bot = global_bottom_z
            else:
                z_bot = base.z - local_h
        else:
            z_bot = base.z
            z_top = base.z + local_h
            if align_bottom and global_bottom_z is not None:
                z_bot = global_bottom_z
        stations.append({
            "co": base,
            "z_bot": z_bot,
            "z_top": z_top,
            "distance": distance,
        })

    if closed and len(stations) > 1:
        # Last station duplicates first for segment iteration; drop after.
        pass

    # Authoring floors stay relative to the path; texture phase may be
    # independently fixed in world coordinates to span separate wall objects.
    ref_z = float(module_anchor_z)
    uv_origin_z = ref_z if uv_anchor_z is None else float(uv_anchor_z)
    # 1.18.0: compute ONLY the Z cells actually intersecting each segment.
    # Remote path vertex heights must not add wall-wide geometry bands.
    # UV phase stays global; story and tile cuts stay aligned at shared stations.
    from .topology_first import local_vertical_slabs
    custom_first = first_story_height if first_story_height > EPS else height
    story_heights = [custom_first] + [height] * (stories - 1)
    slab_plan = [
        local_vertical_slabs(
            a["z_bot"], a["z_top"], b["z_bot"], b["z_top"],
            path_anchor_z=ref_z, uv_anchor_z=uv_origin_z,
            tile_height=height, story_heights=story_heights,
            stories_down=stories_down, include_story_cuts=False,
            grid_anchor_z=ref_z,
        )
        for a, b in zip(stations, stations[1:])
    ]
    estimated_faces = sum(len(slabs) for slabs in slab_plan)
    if estimated_faces > MAX_GENERATED_FACES:
        raise ValueError(
            f"Стена может создать примерно {estimated_faces:,} секций. "
            f"Безопасный предел: {MAX_GENERATED_FACES:,}. "
            "Увеличьте UV-модуль, сократите путь или число этажей."
        )
    log_event(
        f'WALL_TOPOLOGY_FIRST role={role}; segments={len(slab_plan)}; '
        f'local_z_cells={estimated_faces}; '
        f'max_segment_cells={max(map(len, slab_plan), default=0)}; '
        f'uv_origin={uv_origin_z:.6f}; story_geom_cuts=OMITTED; '
        f'remote_z_bands=False', 'TOPOLOGY',
    )
    log_event(
        f'WALL_UV_PHASE role={role}; mode={uv_origin_mode}; '
        f'path_anchor={ref_z:.6f}; uv_origin={uv_origin_z:.6f}; '
        f'slab_count={estimated_faces}; floor_count={stories}; '
        f'scope=PER_SEGMENT', 'UV_CHECK',
    )

    vertices = []
    faces = []
    face_uvs = []
    area_eps = max(EPS, height * tile_length * 1e-8)

    # Canonical once-per-build UV phase plan, shared by all floors of each
    # horizontal segment. This catches missing cuts instead of silently
    # stretching/clamping a texture across several modules.
    from .wall_uv_continuity import module_uv_plan, cyclic_endpoint_seam
    uv_segments, uv_report = module_uv_plan(cuts, tile_length)
    if uv_report['internal_phase_breaks']:
        raise ValueError(
            'WALL_UV: unexpected internal texture phase breaks '
            + str(uv_report['internal_phase_breaks'][:8]))
    if uv_report['snapped_cells']:
        log_event(
            f"WALL_UV_NEAR_GRID snapped={uv_report['snapped_cells']}; "
            f"max_adjustment_m={uv_report['maximum_snapped_error_m']:.7f}; "
            'author_xyz_unchanged=True', 'UV_CHECK')
    if closed:
        log_event(
            'WALL_UV_LOOP_SEAM location=AUTHORED_START; '
            f'perimeter_phase_gap_m={cyclic_endpoint_seam(cuts[-1],tile_length):.7f}; '
            'internal_modular_repeats=EXPECTED', 'UV_CHECK')
    segment_count = len(stations) - 1
    for i in range(segment_count):
        if closed and i == segment_count - 1:
            # Closing segment: stations[-1] should equal stations[0] in XY; still use both.
            pass

        s0 = stations[i]
        s1 = stations[i + 1]
        d0 = s0["distance"]
        d1 = s1["distance"]
        p0 = s0["co"]
        p1 = s1["co"]

        for z_lo, z_hi, story_anchor_z in slab_plan[i]:
            # Skip bands that cannot intersect this segment's Z range
            seg_z_min = min(s0["z_bot"], s1["z_bot"])
            seg_z_max = max(s0["z_top"], s1["z_top"])
            if z_hi < seg_z_min + EPS or z_lo > seg_z_max - EPS:
                continue

            clipped = clip_wall_segment_to_z_slab(
                p0, s0["z_bot"], s0["z_top"],
                p1, s1["z_bot"], s1["z_top"],
                z_lo, z_hi,
            )
            if len(clipped) < 3:
                continue
            # Signed area in the vertical plane approx via XY projection of edges is wrong;
            # use 3D cross accumulation projected on outward horizontal normal.
            area2 = 0.0
            for a, b in zip(clipped, clipped[1:] + clipped[:1]):
                area2 += a.x * b.y - b.x * a.y
            if abs(area2) <= area_eps and abs(sum(
                (clipped[j] - clipped[0]).cross(clipped[j + 1] - clipped[0]).length
                for j in range(1, len(clipped) - 1)
            )) <= area_eps:
                continue

            # A non-zero wall rotation is a *geometric modular cut*, not merely UV
            # rotation. Local clipped cells do not propagate into other stories.
            if abs(module_angle_rad) > 1e-8:
                if is_inner or outer_normal_side is None:
                    outward = segment_outward_xy(p0, p1, inset_side, is_inner=is_inner)
                else:
                    outward = segment_side_xy(p0, p1, outer_normal_side)
                for cell, factors in rotated_wall_cells(
                    clipped, p0, p1, d0, story_anchor_z, tile_length, module_angle_rad,
                ):
                    uv_points = [bilinear_source_uv(source, u, v) for u, v in factors]
                    cell, uv_points = orient_polygon(cell, uv_points, outward)
                    vertex_offset = len(vertices)
                    vertices.extend(tuple(point) for point in cell)
                    faces.append(tuple(vertex_offset + k for k in range(len(cell))))
                    face_uvs.append(tuple(uv_points))
                    if len(faces)>MAX_GENERATED_FACES:
                        raise ValueError("Rotated wall exceeded geometry safety face limit")
                continue
            # UV: U along path (XY arc), V within the module band (world Z → 0..1)
            uvs = []
            xy_len = horizontal_length(p0, p1)
            for point in clipped:
                if xy_len > EPS:
                    t = (
                        (point.x - p0.x) * (p1.x - p0.x)
                        + (point.y - p0.y) * (p1.y - p0.y)
                    ) / (xy_len * xy_len)
                    t = max(0.0, min(1.0, t))
                else:
                    t = 0.0
                # Each virtual UV tile has one source of truth. Interpolate
                # only within a verified module; never hide missing cuts with
                # an unconstrained per-corner [0,1] clamp.
                u_start, u_end, _tile_origin, _snapped = uv_segments[i]
                u_factor = u_start + t * (u_end - u_start)
                # One phase for every floor: genuine UV boundaries repeat the
                # tile, whereas fractional-height story seams preserve V.
                v_factor = uv_v_factor(point.z, 0.5 * (z_lo + z_hi), story_anchor_z, height)
                uvs.append(bilinear_source_uv(source, u_factor, v_factor))

            # Orient normal outward (horizontal); reverse verts+UV together (no UV flip)
            if is_inner or outer_normal_side is None:
                outward = segment_outward_xy(p0, p1, inset_side, is_inner=is_inner)
            else:
                outward = segment_side_xy(p0, p1, outer_normal_side)
            clipped, uvs = orient_polygon(clipped, uvs, outward)

            vertex_offset = len(vertices)
            vertices.extend(tuple(point) for point in clipped)
            faces.append(tuple(vertex_offset + k for k in range(len(clipped))))
            face_uvs.append(tuple(uvs))

    if not faces:
        raise ValueError("Стена не создала ни одной грани — проверьте путь и высоту плейна")
    return vertices, faces, face_uvs



def polygon_normal(points):
    """Unit normal from Newell's method (works for non-planar polys too)."""
    if len(points) < 3:
        return Vector((0.0, 0.0, 1.0))
    normal = Vector((0.0, 0.0, 0.0))
    for index, a in enumerate(points):
        b = points[(index + 1) % len(points)]
        normal.x += (a.y - b.y) * (a.z + b.z)
        normal.y += (a.z - b.z) * (a.x + b.x)
        normal.z += (a.x - b.x) * (a.y + b.y)
    if normal.length <= EPS:
        normal = (points[1] - points[0]).cross(points[2] - points[0])
    if normal.length <= EPS:
        return Vector((0.0, 0.0, 1.0))
    return normal.normalized()


def orient_polygon(points, uvs, preferred_direction):
    """Reverse polygon (and matching UVs) if normal opposes preferred_direction.

    Reversing both keeps UV attached to the same corners — no UV mirror.
    """
    if len(points) < 3 or preferred_direction.length <= EPS:
        return points, uvs
    direction = preferred_direction.normalized()
    if polygon_normal(points).dot(direction) < 0.0:
        points = list(reversed(points))
        if uvs is not None:
            uvs = list(reversed(uvs))
    return points, uvs



def segment_side_xy(p0, p1, side):
    """Horizontal normal pointing to an explicit authored path side.

    Unlike :func:`segment_outward_xy`, ``side`` is already the requested
    direction and is therefore never inverted.  Keeping the two contracts
    separate prevents a semantic LEFT/RIGHT from being inverted twice.
    """
    direction = Vector((p1.x - p0.x, p1.y - p0.y, 0.0))
    if direction.length <= EPS:
        return Vector((1.0, 0.0, 0.0))
    direction.normalize()
    left = Vector((-direction.y, direction.x, 0.0))
    return left if side == "LEFT" else -left

def segment_outward_xy(p0, p1, inset_side, is_inner=False):
    """Horizontal outward normal for a wall segment.

    Outer wall faces away from the inset (inner) side.
    Inner wall faces the opposite way (away from the solid between walls).
    """
    direction = Vector((p1.x - p0.x, p1.y - p0.y, 0.0))
    if direction.length <= EPS:
        outward = Vector((1.0, 0.0, 0.0))
    else:
        direction.normalize()
        left = Vector((-direction.y, direction.x, 0.0))
        # Inset LEFT → inner is on the left → outer faces right
        if inset_side == "LEFT":
            outward = Vector((direction.y, -direction.x, 0.0))  # right
        else:
            outward = left
    if is_inner:
        outward = -outward
    return outward

def signed_area_2d(points):
    return 0.5 * sum(
        a.x * b.y - b.x * a.y
        for a, b in zip(points, points[1:] + points[:1])
    )


def _orientation_2d(a, b, c):
    return cross_2d(b - a, c - a)


def _segments_intersect_2d(a, b, c, d, tolerance=EPS):
    o1 = _orientation_2d(a, b, c)
    o2 = _orientation_2d(a, b, d)
    o3 = _orientation_2d(c, d, a)
    o4 = _orientation_2d(c, d, b)
    return (
        ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance))
        and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance))
    )


def validate_simple_path(points, label="Контур"):
    """Reject zero-length and crossing XY edges; broadphase is fail-safe.

    Every candidate is still tested by the original exact orientation check.
    The earliest reported pair matches the old (i,j) scanning order.
    """
    _started = perf_counter()
    try:
        if len(points) < 2:
            raise ValueError(f"{label}: недостаточно точек")
        segments = list(zip(points, points[1:]))
        closed = is_path_closed(points)
        for index, (a, b) in enumerate(segments):
            if horizontal_length(a, b) <= EPS:
                raise ValueError(f"{label}: ребро {index + 1} имеет нулевую длину в XY")
        from .path_broadphase import candidate_pairs_xy
        candidates = candidate_pairs_xy(points)
        if candidates is None:
            # Legacy exact scan for small/degenerate/budget-limited paths.
            for i, (a, b) in enumerate(segments):
                for j in range(i + 1, len(segments)):
                    if j == i + 1 or (closed and i == 0 and j == len(segments) - 1):
                        continue
                    c, d = segments[j]
                    if _segments_intersect_2d(a, b, c, d):
                        raise ValueError(f"{label}: пересекаются рёбра {i + 1} и {j + 1}")
        else:
            for i,j in candidates:
                if j == i + 1 or (closed and i == 0 and j == len(segments) - 1):
                    continue
                a,b = segments[i]
                c,d = segments[j]
                if _segments_intersect_2d(a, b, c, d):
                    raise ValueError(f"{label}: пересекаются рёбра {i + 1} и {j + 1}")
    finally:
        # Includes candidate construction and exact checks; appears in the
        # existing diagnostics PerformanceRegistry without per-face log spam.
        record_stage(log_event, 'PATH_VALIDATION', _started)


def clip_polygon_axis(points, axis, boundary, keep_greater):
    if not points:
        return []

    def inside(point):
        value = getattr(point, axis)
        return (
            value >= boundary - EPS
            if keep_greater
            else value <= boundary + EPS
        )

    def intersection(start, end):
        start_value = getattr(start, axis)
        end_value = getattr(end, axis)
        delta = end_value - start_value
        if abs(delta) <= EPS:
            return start.copy()
        factor = (boundary - start_value) / delta
        return start.lerp(end, factor)

    output = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside != previous_inside:
            output.append(intersection(previous, current))
        if current_inside:
            output.append(current.copy())
        previous = current
        previous_inside = current_inside
    return output


def clip_polygon_to_tile(points, x_min, x_max, y_min, y_max):
    clipped = clip_polygon_axis(points, "x", x_min, True)
    clipped = clip_polygon_axis(clipped, "x", x_max, False)
    clipped = clip_polygon_axis(clipped, "y", y_min, True)
    clipped = clip_polygon_axis(clipped, "y", y_max, False)
    return clipped


def bilinear_source_uv(source, u, v):
    bottom = source["bottom_left_uv"].lerp(source["bottom_right_uv"], u)
    top = source["top_left_uv"].lerp(source["top_right_uv"], u)
    return bottom.lerp(top, v)


def segment_station_parameters(path_points, tile_length, angle_rad=0.0):
    """Return local station parameters for every original path segment."""
    lengths = [
        horizontal_length(start, end)
        for start, end in zip(path_points, path_points[1:])
    ]
    accumulated = 0.0
    groups = []

    for (start, end), length in zip(zip(path_points, path_points[1:]), lengths):
        parameters = [0.0, 1.0]
        if length <= EPS:
            groups.append(parameters)
            continue
        next_module = (floor(accumulated / tile_length) + 1) * tile_length
        while next_module < accumulated + length - EPS:
            parameters.append((next_module - accumulated) / length)
            next_module += tile_length

        su, sv = rotate_xy(start.x, start.y, angle_rad)
        eu, ev = rotate_xy(end.x, end.y, angle_rad)
        for start_value, end_value in ((su, eu), (sv, ev)):
            delta = end_value - start_value
            if abs(delta) <= EPS:
                continue
            first_line = floor(min(start_value, end_value) / tile_length) + 1
            last_line = ceil(max(start_value, end_value) / tile_length)
            for grid_index in range(first_line, last_line):
                factor = (grid_index * tile_length - start_value) / delta
                if EPS < factor < 1.0 - EPS:
                    parameters.append(factor)

        groups.append(unique_sorted(parameters))
        accumulated += length
    return groups


def paired_station_parameters(outer_points, inner_points, tile_length, angle_rad=0.0):
    outer_groups = segment_station_parameters(outer_points, tile_length, angle_rad)
    inner_groups = segment_station_parameters(inner_points, tile_length, angle_rad)
    if len(outer_groups) != len(inner_groups):
        raise ValueError("Outer and inner paths do not have matching segments")
    return [
        unique_sorted(outer_parameters + inner_parameters)
        for outer_parameters, inner_parameters in zip(outer_groups, inner_groups)
    ]


def station_parameters_to_distances(path_points, parameter_groups):
    cuts = []
    accumulated = 0.0
    for start, end, parameters in zip(
        path_points,
        path_points[1:],
        parameter_groups,
    ):
        length = horizontal_length(start, end)
        cuts.extend(
            accumulated + parameter * length
            for parameter in parameters
        )
        accumulated += length
    return unique_sorted(cuts)


def clip_polygon_to_rotated_tile(world_points, u_min, u_max, v_min, v_max, angle_rad):
    """Clip a world-space polygon to one tile square in the rotated UV frame."""
    rotated = []
    for point in world_points:
        ru, rv = rotate_xy(point.x, point.y, angle_rad)
        rotated.append(Vector((ru, rv, point.z)))
    clipped_rot = clip_polygon_to_tile(rotated, u_min, u_max, v_min, v_max)
    result = []
    for point in clipped_rot:
        wx, wy = unrotate_xy(point.x, point.y, angle_rad)
        result.append(Vector((wx, wy, point.z)))
    return result


