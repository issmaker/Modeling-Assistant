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
    validate_and_fix_role_normals, validate_and_fix_wall_normals, expected_component_count,
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

def build_roof_only(
    context,
    source_obj,
    path_obj,
    tile_length,
    output_name,
    cap_rotation_deg=0.0,
    normal_direction="UP",
    surface_method="TRIANGULATED",
    target_obj=None,
    surface_z_offset=0.0,
):
    """Full UV roof for each closed chain in the path mesh (v1.33)."""
    angle_rad = radians(cap_rotation_deg)
    _perf_started = perf_counter()
    source = source_quad_data(source_obj, tile_length)
    validate_path_mesh_topology(path_obj, require_single_chain=False, allow_branches=True)
    chains, skipped_branch, skipped_invalid = extract_all_path_chains(path_obj)
    record_stage(log_event, "SURFACE_PREPARE", _perf_started)

    built = []
    errors = []
    for index, path_points in enumerate(chains):
        pts = [p.copy() for p in path_points]
        closed = is_path_closed(pts)
        if not closed:
            if len(pts) >= 2 and (pts[0] - pts[-1]).length <= 1.0e-3:
                pts[-1] = pts[0].copy()
                closed = True
            else:
                errors.append(f"линия {index + 1}: не замкнута")
                continue
        if (pts[0] - pts[-1]).length > EPS:
            pts.append(pts[0].copy())
        validate_simple_path(pts, f"Контур крыши {index + 1}")

        name = output_name if len(chains) == 1 else f"{output_name}_{index + 1:02d}"

        # A standalone landscape does not need wall-style cuts every module
        # along the contour arc length. The surface builder itself adds only
        # real intersections with the rotated XY UV grid.
        boundary = [point.copy() for point in pts]
        log_event(
            f"SURFACE_BOUNDARY original_points={max(0, len(boundary) - 1)}; "
            "arc_length_module_cuts=0; grid_crossings=handled_by_surface",
            "TOPOLOGY",
        )

        try:
            builder = build_triangulated_surface if surface_method == "TRIANGULATED" else build_full_top_cap
            vertices, faces, face_uvs = builder(
                boundary, source, tile_length,
                angle_rad=angle_rad, z_offset=surface_z_offset,
                preferred_normal=Vector((0.0, 0.0, 1.0 if normal_direction == "UP" else -1.0)),
            )
        except ValueError as err:
            errors.append(f"линия {index + 1}: {err}")
            continue

        _perf_started = perf_counter()
        mesh = bpy.data.meshes.new(name + "_Mesh")
        try:
            mesh.from_pydata(vertices, [], faces)
            mesh.update()
            set_face_roles(mesh, ["SURFACE"] * len(mesh.polygons))
            for material in source_obj.data.materials:
                mesh.materials.append(material)
            if mesh.materials:
                mi = min(source["material_index"], len(mesh.materials) - 1)
                for poly in mesh.polygons:
                    poly.material_index = mi
            uv_name = "UVMap"
            if source_obj.data.uv_layers.active:
                uv_name = source_obj.data.uv_layers.active.name
            uv_layer = mesh.uv_layers.new(name=uv_name)
            for polygon, uvs in zip(mesh.polygons, face_uvs):
                for loop_index, uv in zip(polygon.loop_indices, uvs):
                    uv_layer.data[loop_index].uv = uv
            record_stage(log_event, "SURFACE_MESH_AUTHOR", _perf_started)

            # Weld only numerically coincident pieces; never pull the boundary.
            _perf_started = perf_counter()
            merge_mesh_by_xy(
                mesh,
                dist=cap_weld_tolerance(tile_length),
                dissolve_cap_triangles=False,
            )
            smart_regulation_weld(mesh, protected_points=pts)
            merge_coincident_vertex_indices(mesh)
            stitch_t_junctions(mesh, tolerance=topology_stitch_tolerance(tile_length))
            sanitize_generated_topology(mesh)
            record_stage(log_event, "SURFACE_WELD_TOPOLOGY", _perf_started)
            _perf_started = perf_counter()
            validate_face_role_coverage(mesh, {"SURFACE"})
            validate_surface_boundary_coverage(mesh, pts, tile_length)
            validate_surface_projected_area(mesh, pts, tile_length)
            finalize_surface_normals_and_shading(
                mesh,
                expected_sign=1.0 if normal_direction == "UP" else -1.0,
            )
            validate_uv_faces_not_folded(mesh)
            validate_uv_winding(mesh, expected_positive=True)
            validate_uv_safe_range(mesh, source)
            from .module_seam_flags import mark_mesh_uv_seams
            _surface_seams=mark_mesh_uv_seams(mesh)
            log_event(f'SURFACE_MODULE_SEAM_FLAGS {json.dumps(_surface_seams, sort_keys=True)}', 'UV_CHECK')
            record_stage(log_event, "SURFACE_VALIDATION", _perf_started)

            reuse_target = target_obj if len(chains) == 1 and not built else None
            _perf_started = perf_counter()
            obj, reused_stable = install_mesh_on_result(context, mesh, name, reuse_target)
            record_stage(log_event, "RESULT_SWAP", _perf_started)
        except Exception:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            raise
        if reused_stable:
            log_event(f"STABLE_RESULT_REUSED object={obj.name}; mode=SURFACE", "LIFECYCLE")
        prepare_result_metadata(obj, "SURFACE")
        obj["roof_only"] = True
        obj["chain_index"] = index
        obj["path_object"] = path_obj.name
        built.append((obj, len(mesh.polygons)))

    if not built:
        raise ValueError("Крышка не построена: " + ("; ".join(errors) or "нет контуров"))

    for o in context.selected_objects:
        o.select_set(False)
    for obj, _ in built:
        obj.select_set(True)
    context.view_layer.objects.active = built[-1][0]
    return built[-1][0], sum(fc for _, fc in built), len(built), skipped_branch, errors


def build_wall(
    context,
    source_obj,
    path_obj,
    tile_length,
    output_name,
    create_inner,
    inset_distance,
    inset_side,
    close_ends,
    keep_inner_path,
    create_top_cap,
    top_cap_mode,
    stories=1,
    cap_rotation_deg=0.0,
    create_bottom_cap=False,
    first_story_height=0.0,
    align_bottom=False,
    bottom_extend=0.0,
    stories_down=True,
    wall_normal_mode="OUTWARD",
    wall_offset=0.0,
    wall_alignment="LEGACY",
    target_obj=None,
    wall_rotation_deg=0.0,
    wall_uv_origin_mode="PATH",
    cap_material=None,
    bottom_cap_material=None,
    inner_wall_height_delta=0.0,
    inner_wall_height_overrides_json="{}",
    secondary_points_json="{}",
    close_start_end=True,
    close_finish_end=True,
):
    stories = max(1, int(stories))
    angle_rad = radians(cap_rotation_deg)
    wall_angle_rad = radians(wall_rotation_deg)
    # Snapshot the untouched authored Path before any production operation.
    from ..core.geometry_truth import path_snapshot, assert_path_unchanged, mesh_snapshot
    _source_path_truth = path_snapshot(path_obj)
    path_points = ordered_path_vertices(path_obj)
    # The authored 3D path is the only source for Z module grid origin.
    # Never derive the vertical phase from a shifted PRIMARY nor SECONDARY.
    authored_module_points = tuple((p.x, p.y, p.z) for p in path_points)
    # Secondary wall has NO manually authored per-point positions/heights.
    # Older definitions may retain a secondary_points_json blob for migration,
    # but it must not change the automatically derived Offset Edges chain.
    secondary_points = None
    if create_inner and str(secondary_points_json or '{}').strip() not in {'','{}'}:
        log_event('SECONDARY_LEGACY_POINT_OVERRIDES_IGNORED; Offset Edges=AUTHORITATIVE', 'MIGRATION')
    # Pass 51: secondary wall inherits Z and height from the primary wall.
    # Historical overrides remain serialized for safe file compatibility but
    # MUST NOT affect either derived path or published topology.
    inner_deltas = None
    if create_inner and (abs(float(inner_wall_height_delta)) > 1e-10 or
                         str(inner_wall_height_overrides_json or '{}').strip() not in {'', '{}'} or
                         (secondary_points and any(abs(v.get(key, 0.0)) > 1e-10
                                                   for v in secondary_points for key in ('dz','dh')))):
        log_event('SECONDARY_LEGACY_HEIGHT_IGNORED pass=51; primary_z_and_h_inherited=True', 'MIGRATION')
    from .wall_alignment import left_signed_shift, automatic_inset_side, resolve_placement
    physical_alignment = resolve_placement(wall_alignment, path_points, inset_side)
    resolved_inset_side = automatic_inset_side(physical_alignment, inset_side)
    if resolved_inset_side != inset_side:
        log_event(f"WALL_SIDE_AUTO corrected={inset_side}->{resolved_inset_side}; alignment={wall_alignment}", "AUTO_FIX")
        inset_side = resolved_inset_side
    # A single, zero-thickness wall follows the authored Path.  The inset
    # thickness belongs exclusively to a *double* wall; treating a disabled
    # inset as virtual placement used to move an otherwise single shell by
    # several metres, which could invert normals at sharp polygon corners.
    # Explicit wall_offset still intentionally moves the resulting shell.
    alignment_shift = (left_signed_shift(physical_alignment, inset_distance, inset_side, True)
                       if create_inner else 0.0)
    effective_wall_offset = float(wall_offset) + alignment_shift
    if not create_inner:
        log_event(
            f"WALL_SINGLE_PATH_PLACEMENT inherited_inset_ignored=True; "
            f"explicit_offset_m={effective_wall_offset:.9f}; "
            "authored_path_unchanged=True", "GEOMETRY",
        )
    # Authored path is immutable; move the *derived* wall footprint only.
    # Use the production miter/safety algorithm (not viewport approximation).
    if abs(effective_wall_offset) > EPS:
        path_points = offset_polyline(
            path_points, abs(effective_wall_offset), "LEFT" if effective_wall_offset > 0 else "RIGHT"
        )
        validate_simple_path(path_points, "Смещённый путь стены")
    if is_path_closed(path_points):
        # Preserve the authored first point as the only closed-loop texture seam.
        # Formerly choose_safe_closed_start() rotated this chain without user
        # approval and could relocate the visible seam onto a curved section.
        from .wall_uv_continuity import xy_distances, authored_start_clearance
        seam_clearance = authored_start_clearance(
            xy_distances(path_points), tile_length)
        _start_index = 0
        log_event(
            'WALL_UV_AUTHORED_START index=0; automatically_rotate_closed_path=False; '
            f'seam_clearance_m={seam_clearance:.7f}', 'UV_CHECK')
        if inner_deltas is not None:
            ring = inner_deltas[:-1]
            inner_deltas = ring[_start_index:] + ring[:_start_index]
            inner_deltas.append(inner_deltas[0])
        if secondary_points is not None:
            ring = secondary_points[:-1]
            secondary_points = ring[_start_index:] + ring[:_start_index]
            secondary_points.append(dict(secondary_points[0]))
        # Advisory only: moving a closed-loop seam can alter the established
        # texture phase. A different start cannot fix reversed UV parity.
        # Sample a bounded number of authored corners without copying Mesh.
        from .wall_uv_start_preflight import recommend_closed_seam_start
        try:
            seam_advice = recommend_closed_seam_start(
                xy_distances(path_points), tile_length,
                clearance=REGULATION_WELD_DISTANCE)
            log_event(
                f'WALL_UV_START_PREFLIGHT authored=0; candidate={seam_advice["index"]}; '
                f'original_near={seam_advice["original_near"]}; '
                f'candidate_near={seam_advice["candidate_near"]}; '
                'decision=KEEP_AUTHORED_VALID_START; geometry_unchanged=True', 'UV_CHECK')
        except ValueError as exc:
            log_event(f'WALL_UV_START_PREFLIGHT_SKIP reason={exc}', 'WARNING')
        if seam_clearance < REGULATION_WELD_DISTANCE:
            log_event(
                "WALL_SEAM_NEAR_CORNER: virtual module seams will use exact authored "
                "corner stations rather than create sub-weld-clearance sliver faces",
                "TOPOLOGY",
            )
    validate_simple_path(path_points, "Путь стены")
    # Height gradients are supported; only degenerate or non-finite segments
    # are rejected. Diagnostics are recorded before generating any Mesh.
    from .wall_seams import slope_diagnostics
    slope_stats = slope_diagnostics(path_points)
    log_event(
        "WALL_SLOPE horizontal_m={horizontal_m:.6f}; max_grade={max_grade:.6f}; "
        "max_z_jump_m={max_z_jump_m:.6f}; z_min={z_min:.6f}; z_max={z_max:.6f}".format(**slope_stats),
        "PATH_CHECK",
    )
    source = source_quad_data(source_obj, tile_length)
    wall_h = total_wall_height(source["height"], max(1, int(stories)), first_story_height)
    # No independent height field: both facade strips use the authored
    # station Z and identical story-height semantics.
    inner_total_heights = None
    # stories_down: path = top; else path = bottom.
    path_closed = is_path_closed(path_points)
    if not path_closed and wall_normal_mode in {"OUTWARD", "INWARD"}:
        resolved = "PATH_RIGHT" if wall_normal_mode == "OUTWARD" else "PATH_LEFT"
        log_event(
            f"Открытый путь: режим нормалей {wall_normal_mode} автоматически "
            f"преобразован в {resolved}",
            "AUTO_FIX",
        )
        wall_normal_mode = resolved
    if not path_closed and not create_inner and (create_top_cap or create_bottom_cap or close_ends):
        log_event(
            "Одинарная открытая стена: крышка, пол и торцы автоматически отключены — "
            "для них нет второй границы площади",
            "AUTO_FIX",
        )
        create_top_cap = False
        create_bottom_cap = False
        close_ends = False
    contour = path_points[:-1] if path_closed else path_points
    area = signed_area_2d(contour) if len(contour) >= 3 else 0.0
    # The cross of a positive signed area contour is CCW; its inside lies
    # LEFT of the directed edges, therefore geometric outside is RIGHT.
    # Explicit PATH_LEFT/PATH_RIGHT select those *same* physical sides.
    from .wall_alignment import resolve_wall_normal_side
    outer_normal_side = resolve_wall_normal_side(area, wall_normal_mode)
    if not create_inner:
        log_event(
            f"WALL_SINGLE_NORMAL_POLICY mode={wall_normal_mode}; "
            f"signed_area_xy={area:.9f}; side={outer_normal_side}; "
            "roof_outer_consistency=REQUIRED", "NORMAL_CHECK",
        )
    from .topology_first import path_module_phase
    module_phase = path_module_phase(authored_module_points, stories_down=stories_down)
    module_anchor_z = module_phase["anchor_z"]
    log_event(
        f'WALL_MODULE_GRID anchor_z={module_anchor_z:.9f}; '
        f'min_z={module_phase["min_z"]:.9f}; '
        f'max_z={module_phase["max_z"]:.9f}; '
        f'rule={module_phase["rule"]}; direction={module_phase["direction"]}; '
        f'uv_mode={wall_uv_origin_mode}; origin=AUTHORED_3D_PATH; '
        'main_secondary_z_phase=SHARED', 'TOPOLOGY',
    )
    if wall_uv_origin_mode not in {"PATH", "WORLD"}:
        raise ValueError(f"Unknown wall UV origin mode: {wall_uv_origin_mode}")
    uv_origin_z = 0.0 if wall_uv_origin_mode == "WORLD" else module_anchor_z
    path_z_min = min(point.z for point in path_points)
    global_bottom_z = None
    if align_bottom:
        # Level bottom: from the lowest path point, go down by bottom_extend.
        global_bottom_z = path_z_min - max(0.0, float(bottom_extend))

    # Auto-close nearly closed open chains for full roof/floor (duplicate ends within tol).
    if not path_closed and top_cap_mode == "OUTER_ONLY" and (create_top_cap or create_bottom_cap):
        if (path_points[0] - path_points[-1]).length <= 1.0e-3:
            path_points = list(path_points)
            path_points[-1] = path_points[0].copy()
            path_closed = True
        else:
            raise ValueError(
                "Режим «Полная крыша/пол» нужен замкнутый путь. "
                "Соедините концы цепи или выберите «Между стенами»."
            )

    # Determine endpoint closure AFTER near-closed Path normalization.  Never
    # generate a terminal cap on the closed seam of a full-roof outline.
    from .secondary_points import selected_endcaps
    cap_start, cap_finish = selected_endcaps(
        create_inner, path_closed, close_ends, close_start_end, close_finish_end)
    # BETWEEN: ribbon between outer and inner (needs create_inner).
    # OUTER_ONLY: fill interior of closed outer path (no inner required).
    do_between_top = (
        create_top_cap
        and top_cap_mode == "BETWEEN"
        and create_inner
    )
    do_full_roof = create_top_cap and top_cap_mode == "OUTER_ONLY" and path_closed
    do_between_bottom = create_bottom_cap and create_inner
    do_full_floor = (
        create_bottom_cap
        and top_cap_mode == "OUTER_ONLY"
        and path_closed
        and not create_inner
    )
    # Bottom with OUTER_ONLY + inner still uses between ribbon at bottom.
    if create_bottom_cap and top_cap_mode == "OUTER_ONLY" and create_inner:
        do_between_bottom = True
        do_full_floor = False

    need_between_params = do_between_top or do_between_bottom
    need_cap_contact_stitch = do_between_top or do_full_roof or do_between_bottom or do_full_floor

    # Cap topology must NEVER drive full-height wall stationing.  The cap has
    # its own UV grid, and grid/boundary intersection points are contact
    # vertices that belong only to the touching top/bottom wall edge.  Older
    # builds propagated those points through wall_station_data(), creating
    # vertical cuts through every story.  Keep wall modular stations
    # independent; the final topology stitch inserts cap vertices only into the
    # polygon edge they actually touch (the adjacent face becomes an ngon).
    wall_uses_cap_grid_cuts = False

    inner_points = None
    parameter_groups = None
    if create_inner:
        # Pass 52: first compute a true derivative path using the same
        # adjacent-edge direction contract as edit_mesh_tools Offset Edges.
        # Never offset the generated wall mesh or mutate the authored Path.
        from .path import offset_edges_reference_polyline
        inner_points = offset_edges_reference_polyline(
            path_points, inset_distance, inset_side)
        log_event('SECONDARY_OFFSET algorithm=EDIT_MESH_TOOLS_NEWELL_3D_RAILS; '
                  'authored_path_modified=False; facades=INDEPENDENT', 'GEOMETRY')
        validate_simple_path(inner_points, "Дополнительный Offset-путь")
        # Even without a top cap, a folded offset strip is not a valid wall.
        from .planar_integrity import assert_nonoverlapping_ribbon_patches
        assert_nonoverlapping_ribbon_patches(path_points, inner_points)
        if inner_deltas is not None and stories_down:
            inner_points = [Vector((point.x, point.y, point.z + inner_deltas[i]))
                            for i, point in enumerate(inner_points)]
        # No independent XY, Z or height control of the secondary facade.
        # Its path is derived via Offset Edges, and its vertical module phase
        # MUST be inherited from the authored PRIMARY path; never recompute
        # module_anchor_z from offset coordinates.
        # Read-only observability: measure large offset miters and high-Z
        # authored corners without modifying either contour.
        from .corner_audit import inspect_wall_corners
        corner_report = inspect_wall_corners(
            path_points, inner_points, inset_distance=inset_distance)
        log_event(
            f'WALL_CORNER_AUDIT corners={corner_report["corners"]}; '
            f'max_turn_deg={corner_report["max_turn_deg"]:.4f}; '
            f'max_miter_ratio={corner_report["max_miter_ratio"]:.4f}; '
            f'high_z={corner_report["high_z"]}; '
            f'review={corner_report["risky"]}', 'PATH_CHECK')
        if need_between_params:
            # These parameters belong to the ribbon cap only.  Do not convert
            # them into wall cut distances: the boundary handshake is resolved
            # locally after wall+cap assembly.
            parameter_groups = paired_station_parameters(
                path_points,
                inner_points,
                tile_length,
                angle_rad=angle_rad,
            )

    # Wall stations contain only wall semantics: authored corners and regular
    # along-path module seams.  No rotated cap-grid crossings are propagated
    # down the facade.
    outer_distances, outer_cuts = wall_station_data(
        path_points,
        tile_length,
        include_cap_grid_cuts=False,
        extra_cut_distances=None,
        angle_rad=0.0,
    )

    # Pass 51 — independent wall first. Each single-wall facade owns its
    # module stations. Never propagate module cuts from one wall to another.
    # Roof/floor contact is reconciled later only on actual touching rim edges.
    outer_wall_cuts = outer_cuts
    inner_wall_cuts = None
    if create_inner:
        _inner_distances, inner_wall_cuts = wall_station_data(
            inner_points, tile_length, include_cap_grid_cuts=False,
            extra_cut_distances=None, angle_rad=0.0,
        )
        log_event(
            f'WALL_INDEPENDENT_MODULE_STATIONS primary={len(outer_wall_cuts)}; '
            f'secondary={len(inner_wall_cuts)}; shared_station_propagation=0; '
            f'shared_module_z_anchor={module_anchor_z:.9f}; '
            'cap_handshake=LOCAL_RIM_ONLY; z=INHERITED', 'TOPOLOGY',
        )

    if need_between_params:
        from .planar_integrity import assert_nonoverlapping_ribbon_patches
        checked = assert_nonoverlapping_ribbon_patches(path_points, inner_points)
        log_event(
            f'WALL_CORNER_FOOTPRINT checked_strips={checked}; '
            'overlap_area=0; authored_path_unchanged=True', 'TOPOLOGY',
        )

    if need_cap_contact_stitch:
        log_event(
            "CAP_CONTACT_POLICY mode=LOCAL_EDGE_STITCH; "
            "wall_grid_propagation=False; wall_station_source=WALL_ONLY; "
            "cap_grid_source=CAP_ONLY",
            "TOPOLOGY",
        )

    vertices, faces, face_uvs = build_wall_surface(
        path_points,
        source,
        tile_length,
        is_inner=False,
        inset_side=inset_side,
        include_cap_grid_cuts=False,
        extra_cut_distances=None,
        stories=stories,
        angle_rad=0.0,
        module_angle_rad=wall_angle_rad,
        first_story_height=first_story_height,
        align_bottom=align_bottom,
        global_bottom_z=global_bottom_z,
        stories_down=stories_down,
        module_anchor_z=module_anchor_z,
        uv_anchor_z=uv_origin_z,
        uv_origin_mode=wall_uv_origin_mode,
        # For a double wall, the physical outer shell must face away from the
        # ribbon. Passing an already-resolved outward side through the old
        # ``segment_outward_xy`` contract inverted that decision a second time;
        # late normal repair then flipped geometry after UV authoring and could
        # create mixed-winding continuous UV islands.  Single walls still use
        # the explicit normal policy.
        outer_normal_side=(None if create_inner else outer_normal_side),
        station_cut_distances=outer_wall_cuts,
    )
    face_roles = ["WALL_OUTER"] * len(faces)

    actual_inset = 0.0
    guide = None
    if create_inner:
        actual_inset = inset_distance

        inner_vertices, inner_faces, inner_uvs = build_wall_surface(
            inner_points,
            source,
            tile_length,
            is_inner=True,
            inset_side=inset_side,
            include_cap_grid_cuts=False,
            extra_cut_distances=None,
            stories=stories,
            angle_rad=0.0,
            module_angle_rad=wall_angle_rad,
            first_story_height=first_story_height,
            align_bottom=align_bottom,
            global_bottom_z=global_bottom_z,
            stories_down=stories_down,
            module_anchor_z=module_anchor_z,
            uv_anchor_z=uv_origin_z,
            uv_origin_mode=wall_uv_origin_mode,
            station_cut_distances=inner_wall_cuts,
            vertex_wall_heights=inner_total_heights,
        )
        vertex_offset = len(vertices)
        vertices.extend(inner_vertices)
        faces.extend(tuple(index + vertex_offset for index in face) for face in inner_faces)
        face_uvs.extend(inner_uvs)
        face_roles.extend(["WALL_INNER"] * len(inner_faces))

        if cap_start or cap_finish:
            # Modular end caps between outer and secondary (world-Z bands, no stretch).
            height = source["height"]
            ref_z = uv_origin_z

            def add_end_cap(outer_pt, inner_pt, width_factor, outward_dir, inner_h):
                if stories_down:
                    z_top_o = outer_pt.z
                    z_top_i = inner_pt.z
                    if align_bottom and global_bottom_z is not None:
                        z_bot_o = global_bottom_z
                        z_bot_i = global_bottom_z
                    else:
                        z_bot_o = outer_pt.z - wall_h
                        z_bot_i = inner_pt.z - inner_h
                else:
                    z_top_o = outer_pt.z + wall_h
                    z_top_i = inner_pt.z + inner_h
                    if align_bottom and global_bottom_z is not None:
                        z_bot_o = global_bottom_z
                        z_bot_i = global_bottom_z
                    else:
                        z_bot_o = outer_pt.z
                        z_bot_i = inner_pt.z
                z_min = min(z_bot_o, z_bot_i)
                z_max = max(z_top_o, z_top_i)
                first_band = int(floor((z_min - ref_z) / height - EPS))
                last_band = int(floor((z_max - ref_z - EPS) / height))
                # Split the physical thickness into source-width cells.  A
                # single stretched polygon produced UV U=5.62 for the supplied
                # 22.99 m double wall and rejected the entire transaction.
                width_cells=max(1,int(ceil(max(width_factor,EPS)-EPS)))
                for width_cell in range(width_cells):
                    u0=float(width_cell)/float(width_factor) if width_factor>EPS else 0.0
                    u1=min(1.0,float(width_cell+1)/float(width_factor)) if width_factor>EPS else 1.0
                    cell_outer=outer_pt.lerp(inner_pt,u0)
                    cell_inner=outer_pt.lerp(inner_pt,u1)
                    cell_width=max(0.0,min(1.0,width_factor*(u1-u0)))
                    cell_bot_o=z_bot_o+(z_bot_i-z_bot_o)*u0
                    cell_bot_i=z_bot_o+(z_bot_i-z_bot_o)*u1
                    cell_top_o=z_top_o+(z_top_i-z_top_o)*u0
                    cell_top_i=z_top_o+(z_top_i-z_top_o)*u1
                    for band in range(first_band, last_band + 1):
                        z_lo = ref_z + band * height
                        z_hi = z_lo + height
                        clipped = clip_wall_segment_to_z_slab(
                            cell_outer, cell_bot_o, cell_top_o,
                            cell_inner, cell_bot_i, cell_top_i,
                            z_lo, z_hi,
                        )
                        if len(clipped) < 3:
                            continue
                        uvs = []
                        for point in clipped:
                            ox, oy = cell_outer.x, cell_outer.y
                            ix, iy = cell_inner.x, cell_inner.y
                            span = ((ix - ox) ** 2 + (iy - oy) ** 2) ** 0.5
                            if span > EPS:
                                t = ((point.x-ox)*(ix-ox)+(point.y-oy)*(iy-oy))/(span*span)
                                t=max(0.0,min(1.0,t))
                            else:t=0.0
                            v_factor=max(0.0,min(1.0,(point.z-z_lo)/height))
                            uvs.append(bilinear_source_uv(source,t*cell_width,v_factor))
                        preferred=Vector((outward_dir.x,outward_dir.y,0.0))
                        if preferred.length<=EPS:preferred=Vector((1.0,0.0,0.0))
                        clipped,uvs=orient_polygon(list(clipped),list(uvs),preferred)
                        off=len(vertices)
                        vertices.extend(tuple(p) for p in clipped)
                        faces.append(tuple(off+k for k in range(len(clipped))))
                        face_uvs.append(tuple(uvs));face_roles.append("END_CAP")

            start_w = horizontal_length(path_points[0], inner_points[0]) / tile_length
            end_w = horizontal_length(path_points[-1], inner_points[-1]) / tile_length
            # Outward along path at open ends
            start_out = Vector((
                path_points[0].x - path_points[1].x,
                path_points[0].y - path_points[1].y,
                0.0,
            ))
            end_out = Vector((
                path_points[-1].x - path_points[-2].x,
                path_points[-1].y - path_points[-2].y,
                0.0,
            ))
            if cap_start:
                add_end_cap(path_points[0], inner_points[0], start_w, start_out,
                            inner_total_heights[0] if inner_total_heights is not None else wall_h)
            if cap_finish:
                add_end_cap(path_points[-1], inner_points[-1], end_w, end_out,
                            inner_total_heights[-1] if inner_total_heights is not None else wall_h)

    if stories_down:
        top_z_offset = 0.0
        bottom_z_offset = -wall_h
    else:
        top_z_offset = wall_h
        bottom_z_offset = 0.0

    # Semantic ranges are recorded before Mesh/BMesh cleanup so contact
    # stitching can be constrained to WALL<->CAP only.
    # Determine the WALL rim *before* creating CAP geometry. Subsequent
    # contact planning may split ONLY those contact edges, never a module's
    # unrelated vertical/bottom boundary for an upper cap.
    from .cap_boundary_plan import extract_wall_rim, plan_shared_rim
    wall_rim = extract_wall_rim(vertices, faces, face_roles)
    log_event(
        f"WALL_RIM_BEFORE_CAP outer_edges={wall_rim['outer_edges']}; "
        f"inner_edges={wall_rim['inner_edges']}; "
        f"boundary_vertices={wall_rim['boundary_vertex_count']}",
        'TOPOLOGY',
    )
    structural_vertex_count = len(vertices)
    structural_face_count = len(faces)
    cap_vertex_ids = []
    cap_face_ids = []

    def append_cap(cap_vertices, cap_faces, cap_uvs, role):
        nonlocal vertices, faces, face_uvs, face_roles
        cap_offset = len(vertices)
        face_offset = len(faces)
        vertices.extend(cap_vertices)
        faces.extend(
            tuple(index + cap_offset for index in face)
            for face in cap_faces
        )
        face_uvs.extend(cap_uvs)
        face_roles.extend([role] * len(cap_faces))
        cap_vertex_ids.extend(range(cap_offset, cap_offset + len(cap_vertices)))
        cap_face_ids.extend(range(face_offset, face_offset + len(cap_faces)))

    def make_boundary_at_offset(z_off):
        boundary = []
        total_len = outer_distances[-1]
        for distance in outer_cuts:
            if distance >= total_len - EPS and boundary:
                continue
            base = sample_polyline(path_points, outer_distances, distance)
            boundary.append(Vector((base.x, base.y, base.z + z_off)))
        if boundary:
            boundary.append(boundary[0].copy())
        return boundary

    def make_boundary_at_absolute_z(z_abs):
        boundary = []
        total_len = outer_distances[-1]
        for distance in outer_cuts:
            if distance >= total_len - EPS and boundary:
                continue
            base = sample_polyline(path_points, outer_distances, distance)
            boundary.append(Vector((base.x, base.y, z_abs)))
        if boundary:
            boundary.append(boundary[0].copy())
        return boundary

    if do_between_top:
        if inner_total_heights is None:
            top_outer, top_inner = path_points, inner_points
            top_offset = top_z_offset
        else:
            top_outer = [Vector((p.x, p.y, p.z + top_z_offset)) for p in path_points]
            top_inner = [Vector((p.x, p.y, p.z + (0.0 if stories_down else inner_total_heights[i])))
                         for i,p in enumerate(inner_points)]
            top_offset = 0.0
        append_cap(*build_top_cap(
            top_outer,
            top_inner,
            source,
            tile_length,
            parameter_groups,
            angle_rad=angle_rad,
            z_offset=top_offset,
            preferred_normal=Vector((0.0, 0.0, 1.0)),
        ), "TOP_CAP")
    elif do_full_roof:
        append_cap(*build_full_top_cap(
            make_boundary_at_offset(top_z_offset),
            source,
            tile_length,
            angle_rad=angle_rad,
            z_offset=0.0,  # Z already baked into boundary
            preferred_normal=Vector((0.0, 0.0, 1.0)),
        ), "TOP_CAP")

    if do_between_bottom:
        if align_bottom and global_bottom_z is not None:
            # Ribbon at constant bottom Z: temporarily shift path/inner Z
            bot_outer = [Vector((p.x, p.y, global_bottom_z)) for p in path_points]
            bot_inner = [Vector((p.x, p.y, global_bottom_z)) for p in inner_points]
            append_cap(*build_top_cap(
                bot_outer,
                bot_inner,
                source,
                tile_length,
                parameter_groups,
                angle_rad=angle_rad,
                z_offset=0.0,
                preferred_normal=Vector((0.0, 0.0, -1.0)),
            ), "BOTTOM_CAP")
        else:
            if inner_total_heights is None:
                bottom_outer, bottom_inner = path_points, inner_points
                bottom_offset = bottom_z_offset
            else:
                bottom_outer = [Vector((p.x, p.y, p.z + bottom_z_offset)) for p in path_points]
                bottom_inner = [Vector((p.x, p.y, p.z - (inner_total_heights[i] if stories_down else 0.0)))
                                for i,p in enumerate(inner_points)]
                bottom_offset = 0.0
            append_cap(*build_top_cap(
                bottom_outer,
                bottom_inner,
                source,
                tile_length,
                parameter_groups,
                angle_rad=angle_rad,
                z_offset=bottom_offset,
                preferred_normal=Vector((0.0, 0.0, -1.0)),
            ), "BOTTOM_CAP")
    elif do_full_floor:
        if align_bottom and global_bottom_z is not None:
            append_cap(*build_full_top_cap(
                make_boundary_at_absolute_z(global_bottom_z),
                source,
                tile_length,
                angle_rad=angle_rad,
                z_offset=0.0,
                preferred_normal=Vector((0.0, 0.0, -1.0)),
            ), "BOTTOM_CAP")
        else:
            append_cap(*build_full_top_cap(
                make_boundary_at_offset(bottom_z_offset),
                source,
                tile_length,
                angle_rad=angle_rad,
                z_offset=0.0,
                preferred_normal=Vector((0.0, 0.0, -1.0)),
            ), "BOTTOM_CAP")

    # Shared exact WALL/CAP positions receive the same indices BEFORE mesh
    # authoring. Their independent UV loops remain untouched. Remaining
    # unmatched cap grid T-junctions still require local edge stitching.
    from .topology_first import preweld_shared_vertices
    preweld = preweld_shared_vertices(
        vertices, faces, structural_count=structural_vertex_count,
        cap_vertex_ids=cap_vertex_ids, tolerance=1e-8,
    )
    vertices = preweld["vertices"]
    faces = preweld["faces"]
    cap_vertex_ids = preweld["cap_vertex_ids"]
    structural_vertex_count = preweld["structural_count"]
    # Contact candidates exposed by the first boundary pass can require
    # another pass. Converge with fixed safety bounds while retaining exactly
    # the authored XYZ vertices and UV mapping; fail closed on any residue.
    from .cap_boundary_plan import converge_shared_rim
    # Boundary candidates were independently quantized to float32 in world
    # space. Use two coordinate ULPs for CONTACT INSERTION only, never a
    # global weld. 2m modules previously halved this tolerance and missed
    # their own cap-grid vertices, leaving penetrations along the rim.
    coordinate_extent = max(1.0, *(abs(float(c)) for p in vertices for c in p))
    rim_precision = max(min(1e-5, float(tile_length) * 2.5e-6),
                        min(0.00025, coordinate_extent * 2.0**-22))
    boundary_first = converge_shared_rim(
        vertices, faces, face_uvs, face_roles,
        tolerance=rim_precision,
        max_rounds=12, max_total_insertions=40000,
    )
    faces = boundary_first['faces']
    face_uvs = boundary_first['face_uvs']
    rim_report = boundary_first['report']
    rim_report['preweld_saved_vertices'] = preweld['saved_vertices']
    log_event(
        f"WALL_RIM_PLAN cap_to_wall={rim_report['cap_to_wall_loop_insertions']}; "
        f"wall_to_cap={rim_report['wall_to_cap_loop_insertions']}; "
        f"wall_interior_changed={rim_report['wall_internal_faces_modified']}; "
        f"total_vertices_added=0; after_author_stitch=PROHIBITED; "
        f"fixed_point={rim_report['fixed_point_verified']}; "
        f"contact_rounds={rim_report['contact_rounds']}",
        'TOPOLOGY',
    )
    log_event(
        f"WALL_TOPOLOGY_FIRST_PREWELD removed_duplicates={preweld['saved_vertices']}; "
        f"shared_cap_to_wall={preweld['shared_cap_to_wall']}; "
        "uv_loops_unchanged=True; face_count_unchanged=True", "TOPOLOGY",
    )
    _perf_started = perf_counter()
    mesh = bpy.data.meshes.new(output_name + "_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    set_face_roles(mesh, face_roles)

    for material in source_obj.data.materials:
        mesh.materials.append(material)
    if mesh.materials:
        material_index = min(source["material_index"], len(mesh.materials) - 1)
        for polygon in mesh.polygons:
            polygon.material_index = material_index
    roles_now = face_role_codes(mesh)
    def _assign_semantic_material(material, role_name):
        if material is None:
            return -1
        existing = next((i for i, candidate in enumerate(mesh.materials) if candidate == material), -1)
        if existing < 0:
            mesh.materials.append(material)
            existing = len(mesh.materials) - 1
        role_code = FACE_ROLE_CODES[role_name]
        for polygon in mesh.polygons:
            if roles_now[polygon.index] == role_code:
                polygon.material_index = existing
        return existing
    cap_material_index = _assign_semantic_material(cap_material, "TOP_CAP") if create_top_cap else -1
    bottom_cap_material_index = _assign_semantic_material(bottom_cap_material, "BOTTOM_CAP") if create_bottom_cap else -1

    uv_layer = mesh.uv_layers.new(name=source_obj.data.uv_layers.active.name)
    for polygon, uvs in zip(mesh.polygons, face_uvs):
        for loop_index, uv in zip(polygon.loop_indices, uvs):
            uv_layer.data[loop_index].uv = uv
    record_stage(log_event, "WALL_MESH_AUTHOR", _perf_started)

    # All contact vertices are already in polygon loops *before* Mesh author.
    # No late CAP_TO_WALL_EDGE / WALL_TO_CAP_EDGE mutating pass is permitted.
    _perf_started = perf_counter()
    cap_to_wall_insertions = rim_report['cap_to_wall_loop_insertions']
    wall_to_cap_insertions = rim_report['wall_to_cap_loop_insertions']
    log_event(
        f"CAP_CONTACT_LOCAL cap_to_wall={cap_to_wall_insertions}; "
        f"wall_to_cap={wall_to_cap_insertions}; "
        "stage=PRE_MESH_AUTHOR; wall_grid_propagation=False; "
        "only_role_boundary_edges=True", 'TOPOLOGY',
    )

    # Weld the separately UV-mapped cap to the wall(s). UV values remain
    # per-loop, so the necessary seams are preserved while geometry is manifold.
    # 3D weld only — never XY-only (that collapses wall top+bottom at same XY).
    weld_dist = cap_weld_tolerance(tile_length)
    weld_mesh = bmesh.new()
    try:
        weld_mesh.from_mesh(mesh)
        bmesh.ops.remove_doubles(weld_mesh, verts=weld_mesh.verts, dist=weld_dist)
        bmesh.ops.dissolve_degenerate(weld_mesh, dist=weld_dist, edges=weld_mesh.edges)
        wire_edges = [e for e in weld_mesh.edges if e.is_wire]
        if wire_edges:
            bmesh.ops.delete(weld_mesh, geom=wire_edges, context="EDGES")
        bmesh.ops.remove_doubles(weld_mesh, verts=weld_mesh.verts, dist=weld_dist)
        weld_mesh.to_mesh(mesh)
    finally:
        weld_mesh.free()
    mesh.update()
    protected = list(path_points)
    if inner_points is not None:
        protected.extend(inner_points)
    # Pass54: the cap fragments have ALREADY been precisely grid-clipped,
    # prewelded and rim-stitched. The historic 2 mm regulation weld can drag
    # a true UV-cell boundary onto a nearby, non-grid authored station. The
    # field capture measured a 1.174 mm movement immediately before the cap
    # grid invariant rejected the result. Only roundoff-level coincidences
    # may be merged at this final WALL/CAP stage; never alter the authored
    # path, the cap tile boundary, or the independently cut wall faces.
    _cap_precision_weld = cap_weld_tolerance(tile_length)
    log_event(
        f'WALL_CAP_PRECISION_WELD distance={_cap_precision_weld:.9f}; '
        'regulation_2mm_motion=DISABLED_FOR_PRESTITCHED_CAP; '
        'source_path_unchanged=True; uv_grid_protected=True', 'TOPOLOGY')
    smart_regulation_weld(
        mesh, protected_points=protected, distance=_cap_precision_weld)
    merge_coincident_vertex_indices(mesh)
    # Blender float32 quantization leaves 7-16 µm duplicate seam endpoints
    # that the historic 4 µm cap weld cannot join. Restrict to safe, disjoint
    # generated micro-pairs and fail before publishing any invalid topology.
    weld_generated_roundoff_pairs(
        mesh, protected_points=protected, distance=2.0e-5)
    # Validation, NOT late reconstruction: reject any left-over WALL/CAP
    # boundary contact, leaving the previous stable Result untouched.
    # The old global T_STITCH_RESIDUAL could touch unrelated lower modules.
    _post_faces = [tuple(poly.vertices) for poly in mesh.polygons]
    _post_uv = [[tuple(mesh.uv_layers.active.data[j].uv)
                 for j in poly.loop_indices] for poly in mesh.polygons]
    _role_codes = face_role_codes(mesh)
    _role_inverse = {v:k for k,v in FACE_ROLE_CODES.items()}
    _post_roles = [_role_inverse.get(int(code), 'UNKNOWN') for code in _role_codes]
    _post_plan = plan_shared_rim(
        [tuple(vert.co) for vert in mesh.vertices],
        _post_faces, _post_uv, _post_roles,
        tolerance=rim_precision,
    )['report']
    residual_t_junctions = (_post_plan['cap_to_wall_loop_insertions'] +
                            _post_plan['wall_to_cap_loop_insertions'])
    if residual_t_junctions:
        raise ValueError(
            f'WALL/CAP boundary not stitched after weld: '
            f'{residual_t_junctions} T-junctions; stable Result preserved'
        )
    rim_report['post_weld_residual_contact_t_junctions'] = residual_t_junctions
    # A 2 mm proximity alone is not proof of weld ownership. In FORENSIC
    # diagnostics inspect boundary endpoints without moving authored or UV
    # module vertices; report examples for a reproducible geometry hotfix.
    try:
        _forensic_gap_audit = (bpy.context.scene.uv_wall_settings.diagnostic_detail == 'FORENSIC')
    except (AttributeError, ReferenceError):
        _forensic_gap_audit = False
    if _forensic_gap_audit:
        from .boundary_gap_audit import boundary_gap_report
        _gap_started = perf_counter()
        _gap_report = boundary_gap_report(
            [tuple(v.co) for v in mesh.vertices],
            [tuple(p.vertices) for p in mesh.polygons])
        record_stage(log_event, 'WALL_GAP_AUDIT', _gap_started)
        log_event(f'WALL_GAP_2MM status={_gap_report["status"]}; '
                  f'boundary_vertices={_gap_report["boundary_vertices"]}; '
                  f'possible_pairs={_gap_report["suspects"]}; '
                  f'examples={_gap_report["examples"]}; '
                  'policy=DIAGNOSE_ONLY_NO_BLIND_WELD', 'TOPOLOGY')

    if need_cap_contact_stitch:
        log_event(
            f"CAP_CONTACT_STITCH cap_to_wall={cap_to_wall_insertions}; "
            f"wall_to_cap={wall_to_cap_insertions}; residual={residual_t_junctions}; "
            "policy=TOUCHING_EDGE_ONLY; vertical_propagation=0",
            "TOPOLOGY",
        )
    # Surface-style cleanup: weld/stitch first so required wall boundary
    # All WALL / CAP candidate edges now go through ONE fail-closed affine
    # optimizer. The legacy cap-only BMesh dissolve checked coplanarity and
    # UV endpoints but did not prove a shared affine UV gradient or safe ngon.
    # With variable Z it was disabled globally, including flat cap islands.
    # Preserve genuine nonplanar cap triangles; allow safe planar patches
    # regardless of height variation elsewhere in the path.
    log_event(
        'CAP_SURFACE_DISSOLVE delegated=SEMANTIC_AFFINE_OPTIMIZER; '
        'global_variable_z_disable=False; local_planarity=REQUIRED; '
        'module_seams=PROTECTED; wall_cap_contact=PROTECTED', 'TOPOLOGY',
    )
    sanitize_generated_topology(mesh)
    # Shared semantic optimizer: accepts both walls, even on variable-Z paths.
    # The published stable Result has not been touched: this is a temporary
    # mesh and any failed BMesh audit aborts the transaction.
    from .wall_topology import (dissolve_redundant_wall_edges,
                                protected_path_corners_xy,
                                protected_path_stations_xy,
                                authored_vertical_edge_signature)
    # A real XY corner must remain a facade boundary at every generated Z,
    # not merely at the top authored point. PRIMARY and SECONDARY have their
    # own independent corner positions and their own UV stationing.
    _protected_facade_corners = protected_path_corners_xy(path_points)
    if inner_points is not None:
        _protected_facade_corners.update(protected_path_corners_xy(inner_points))
    # All original PRIMARY and SECONDARY stations own a vertical column.
    # Protect straight-line authored points as well as true corners; they are
    # user-authored and cannot vanish for the sake of a lower face count.
    _protected_facade_stations = protected_path_stations_xy(path_points)
    if inner_points is not None:
        _protected_facade_stations.update(protected_path_stations_xy(inner_points))
    _rail_signature_before = authored_vertical_edge_signature(mesh, _protected_facade_stations)
    edge_report = dissolve_redundant_wall_edges(
        mesh, protected_path_points=protected,
        protected_corner_xy=_protected_facade_corners,
        protected_station_xy=_protected_facade_stations, allow_caps=True)
    log_event(
        f'WALL_EDGE_REDUCTION removed={edge_report["edges_removed"]}; '
        f'passes={edge_report["passes"]}; '
        f'fixed_point={edge_report["fixed_point_verified"]}; '
        f'stop_reason={edge_report["stop_reason"]}; '
        f'faces={edge_report["faces_before"]}->{edge_report["faces_after"]}; '
        f'candidates={edge_report["candidates"]}; '
        f'removed_by_role={edge_report["removed_by_role"]}; '
        f'rejects={edge_report["rejects"]}; '
        f'skipped_large_mesh={edge_report["skipped_large_mesh"]}; '
        'boundary_unchanged=True; uv_area_unchanged=True; '
        'protected=PATH_MODULE_UV_MATERIAL_ROLE_CORNER_NONPLANAR',
        'TOPOLOGY',
    )
    # Conservative final canonicalization for true degree-2 single-face
    # boundary pass-throughs. UV-module intersections and WALL/CAP shared
    # seams have larger valence and are not candidates. Fail closed if any
    # UV/material/role/geometry invariant cannot be proved.
    from .vertex_minimal import remove_pass_through_boundary_vertices
    vertex_report = remove_pass_through_boundary_vertices(
        mesh, tile_length=tile_length, protected_path_points=protected,
        protected_corner_xy=_protected_facade_corners,
        protected_station_xy=_protected_facade_stations)
    _rail_signature_after = authored_vertical_edge_signature(mesh, _protected_facade_stations)
    if _rail_signature_after != _rail_signature_before:
        lost = sorted(_rail_signature_before - _rail_signature_after)
        added = sorted(_rail_signature_after - _rail_signature_before)
        raise ValueError('WALL_AUTHORED_VERTICAL_RAIL: optimization changed '
                         f'authored columns: lost={lost[:6]}, added={added[:6]}')
    log_event(f'WALL_AUTHORED_VERTICAL_RAIL status=PASS; '
              f'edge_count={len(_rail_signature_after)}; '
              f'xy_stations={len(_protected_facade_stations)}; '
              'primary_secondary=INDEPENDENT', 'TOPOLOGY')
    log_event(
        f'WALL_BOUNDARY_VERTEX_MINIMAL removed={vertex_report["removed"]}; '
        f'candidates={vertex_report["candidates"]}; '
        f'checked={vertex_report["checked"]}; '
        f'UV_OR_GEOMETRY_REJECT={vertex_report["uv_or_geometry_reject"]}; '
        f'protected_authored={vertex_report["protected_authored"]}; '
        f'status={vertex_report["status"]}', 'TOPOLOGY')
    record_stage(log_event, "WALL_WELD_TOPOLOGY", _perf_started)
    _perf_started = perf_counter()
    validate_face_role_coverage(mesh, {"WALL_OUTER", "WALL_INNER", "END_CAP", "TOP_CAP", "BOTTOM_CAP"})
    role_normal_report = validate_and_fix_role_normals(mesh)
    if role_normal_report["violations"]:
        raise ValueError(
            f"Semantic normal validation failed: {role_normal_report['violations']} cap/surface faces"
        )
    log_event(
        f"SEMANTIC_NORMALS flipped={role_normal_report['flipped']}; "
        f"violations={role_normal_report['violations']}; roles={role_normal_report['counts']}",
        "NORMAL_CHECK",
    )
    wall_normal_report = validate_and_fix_wall_normals(
        mesh, path_points, inset_side=inset_side, create_inner=create_inner,
        outer_normal_side=outer_normal_side,
    )
    if wall_normal_report["violations"]:
        raise ValueError(
            f"Wall normal invariant failed: {wall_normal_report['violations']} faces remain reversed"
        )
    log_event(
        f"WALL_NORMAL_INVARIANT checked={wall_normal_report['checked']}; "
        f"auto_flipped={wall_normal_report['flipped']}; violations=0", "NORMAL_CHECK")
    # A single surface may be composed of outer, inner and cap roles.
    # Repair connected shells as wholes instead of leaving independent
    # local face flips that violate shared-edge winding.
    from .normal_integrity import orient_connected_shells
    normal_integrity = orient_connected_shells(
        mesh, kind='WALL', path_points=path_points,
        inset_side=inset_side, create_inner=create_inner,
        outer_normal_side=outer_normal_side,
    )
    log_event(
        f'WALL_SHELL_NORMAL_INTEGRITY components={normal_integrity["components"]}; '
        f'closed={normal_integrity["closed"]}; open={normal_integrity["open"]}; '
        f'parity_flips={normal_integrity["parity_flips"]}; '
        f'outward_flips={normal_integrity["outward_flips"]}',
        'NORMAL_CHECK',
    )
    preserve_mixed_wall_shading(mesh)
    # Stitched cap vertices inherit UV by edge interpolation, which is not
    # always consistent with the source tile projection on long/mitered
    # concave polygons. Restore exact XY-to-tile mapping of cap loops only.
    # Geometry and authored Path are not touched by this repair.
    if cap_face_ids:
        from .cap_uv_integrity import reproject_cap_uv
        corrected_cap_loops = reproject_cap_uv(
            mesh, source, tile_length, angle_rad, face_role_codes(mesh),
            FACE_ROLE_CODES, bilinear_source_uv,
        )
        log_event(
            f'CAP_UV_REPROJECT loops={corrected_cap_loops}; '
            'reference=ORIGINAL_ROTATED_TILE; geometry_changed=False',
            'UV_CHECK',
        )
    # Pass 15.23: island-local mirroring may leave every UV island positively
    # wound yet reset the texture at an arbitrary facade edge. Reconstruct
    # facade U from the physical arc-length field AFTER the late BMesh/normal
    # topology changes. V, cap/cornice UV and geometry remain unchanged.
    # This is deliberately BEFORE publication of the temporary Mesh.
    validate_uv_faces_not_folded(mesh)
    uv_orientation_report = repair_uv_winding_islands(mesh, expected_positive=True)
    phase_report = {'policy': 'ROTATED_GRID_LEGACY_NO_REPHASE'}
    physical_facade_winding = {}
    if abs(wall_angle_rad) <= 1e-8:
        from .wall_uv_field import rephase_uv_for_facade
        _phase_started = perf_counter()
        phase_report = rephase_uv_for_facade(
            mesh, path_points, inner_points if create_inner else None,
            tile_length, source, face_role_codes(mesh), FACE_ROLE_CODES,
            expected_winding_by_face=physical_facade_winding,
            positive_winding=True,
        )
        record_stage(log_event, 'WALL_UV_REPHASE', _phase_started)
        log_event(
            'WALL_UV_POST_TOPOLOGY_FIELD '
            f'facade_faces={phase_report["facade_faces"]}; '
            f'corrected_loops={phase_report["corrected_loops"]}; '
            f'max_source_u_delta={phase_report["max_source_u_delta"]:.8f}; '
            f'snapped_faces={phase_report["snapped_faces"]}; '
            f'shared_vertex_checks={phase_report["shared_vertex_checks"]}; '
            f'examples={phase_report["examples"]}; '
            'origin=AUTHORED_PATH_ARCLENGTH; island_pivot=IGNORED; '
            'source_geometry_unchanged=True', 'UV_CHECK')
    else:
        log_event(
            'WALL_UV_POST_TOPOLOGY_FIELD rotated_grid=True; '
            'one_dimensional_rephase=SKIPPED; '
            'existing_two_dimensional_grid=UNCHANGED', 'UV_CHECK')
    validate_uv_faces_not_folded(mesh)
    # A facade's signed UV area must follow physical path-station + world-Z
    # orientation. Other semantic roles retain the strict legacy contract.
    validate_uv_winding(mesh, expected_positive=True,
                        expected_signs=physical_facade_winding)
    if physical_facade_winding:
        log_event(f'WALL_UV_PHYSICAL_PARITY positive={phase_report["expected_positive_facades"]}; '
                  f'negative={phase_report["expected_negative_facades"]}; '
                  'policy=METRIC_U_Z_NOT_GLOBAL_POSITIVE', 'UV_CHECK')
    validate_uv_safe_range(mesh, source)
    log_event(
        f"WALL_UV_INVARIANT repaired_islands={uv_orientation_report['repaired_islands']}; "
        f"repaired_faces={uv_orientation_report['repaired_faces']}; flipped=0", "UV_CHECK")
    # Pass 15.29: actual Blender edge seam flags for every authored UV island
    # boundary, material transition and physical open rim. Do not alter UV.
    from .module_seam_flags import mark_mesh_uv_seams
    _seam_stats=mark_mesh_uv_seams(mesh)
    log_event(f'WALL_MODULE_SEAM_FLAGS {json.dumps(_seam_stats, sort_keys=True)}', 'UV_CHECK')
    record_stage(log_event, "WALL_VALIDATION", _perf_started)
    _truth_started = perf_counter()
    # Compute the complete result identity on the temporary mesh and verify
    # that no path edits occurred while building. Both happen BEFORE Result
    # swap; failures keep the existing result untouched.
    assert_path_unchanged(path_obj, _source_path_truth)
    _mesh_truth = mesh_snapshot(mesh, face_role_codes(mesh))
    _geometry_truth = {
        'mode': 'FULL' if bool(context.scene.get('uvwall_geometry_truth_full', False)) else 'SAMPLED',
        'path': _source_path_truth,
        'mesh': _mesh_truth,
        'wall_normals': wall_normal_report.get('geometry_truth', {}),
        'shell_normals': normal_integrity.get('geometry_truth', {}),
    }
    log_event(
        f'GEOMETRY_TRUTH status=PASS; mode={_geometry_truth["mode"]}; '
        f'path={_source_path_truth["path_digest"]}; '
        f'xyz={_mesh_truth["xyz_digest"]}; '
        f'topology={_mesh_truth["topology_digest"]}; '
        f'uv={_mesh_truth["uv_digest"]}; '
        f'facades={_mesh_truth["faces"]}; '
        f'reference_checks={_geometry_truth["wall_normals"].get("reference_checks",0)+_geometry_truth["shell_normals"].get("reference_checks",0)}',
        'GEOMETRY_TRUTH')
    record_stage(log_event, 'WALL_TRUTH_CONTRACT', _truth_started)

    _perf_started = perf_counter()
    wall, reused_stable = install_mesh_on_result(context, mesh, output_name, target_obj)
    record_stage(log_event, "RESULT_SWAP", _perf_started)
    if reused_stable:
        log_event(f"STABLE_RESULT_REUSED object={wall.name}; mode=WALL", "LIFECYCLE")
    prepare_result_metadata(wall, "WALL")
    wall["source_object"] = source_obj.name
    wall["path_object"] = path_obj.name
    wall["uv_tile_length"] = tile_length
    wall["uvwall_continuous_field"] = json.dumps(phase_report, ensure_ascii=False, sort_keys=True)
    wall["uvwall_geometry_truth_contract"] = json.dumps(_geometry_truth, ensure_ascii=False, sort_keys=True)
    wall["stories"] = stories
    wall["first_story_height"] = first_story_height
    wall["align_bottom"] = bool(align_bottom)
    wall["bottom_extend"] = float(bottom_extend) if align_bottom else 0.0
    wall["stories_down"] = bool(stories_down)
    wall["module_anchor_z"] = module_anchor_z
    wall["module_anchor_policy"] = module_phase["rule"]
    wall["module_anchor_path_z_min"] = module_phase["min_z"]
    wall["module_anchor_path_z_max"] = module_phase["max_z"]
    if create_inner:
        wall["secondary_module_anchor_z"] = module_anchor_z
    elif "secondary_module_anchor_z" in wall:
        del wall["secondary_module_anchor_z"]
    wall["primary_secondary_phase_shared"] = bool(create_inner)
    wall["wall_uv_origin_mode"] = wall_uv_origin_mode
    wall["wall_uv_origin_z"] = uv_origin_z
    wall["wall_normal_mode"] = wall_normal_mode
    wall["uvwall_normal_integrity"] = json.dumps(normal_integrity, ensure_ascii=False)
    wall["uvwall_topology_optimization"] = json.dumps(edge_report, ensure_ascii=False, sort_keys=True)
    wall["uvwall_vertex_minimal"] = json.dumps(vertex_report, ensure_ascii=False, sort_keys=True)
    wall["uvwall_boundary_first"] = json.dumps(rim_report, ensure_ascii=False, sort_keys=True)
    wall["uvwall_topology_first"] = json.dumps({
        "schema": "TOPOLOGY_FIRST_1180", "status": "GENERATED",
        "local_z_slab_planning": True, "physical_story_only_edges": False,
        "uv_phase_rule": "WORLD_UV_WITH_PATH_MODULES" if wall_uv_origin_mode == "WORLD" else module_phase["rule"],
        "physical_module_grid_rule": module_phase["rule"],
        "physical_module_anchor_z": float(module_anchor_z),
        "independent_primary_secondary_u": True,
        "shared_primary_secondary_z": bool(create_inner),
        "uv_anchor_z": float(uv_origin_z),
        "path_unchanged": True, "face_loops_preserved": True,
        "preweld_saved_vertices": int(preweld["saved_vertices"]),
        "preweld_shared_cap_to_wall": int(preweld["shared_cap_to_wall"]),
        "residual_t_junctions": int(residual_t_junctions),
        "optimizer_fixed_point": bool(edge_report["fixed_point_verified"]),
        "blender_live_verified": False,
    }, ensure_ascii=False, sort_keys=True)
    wall["cap_rotation"] = cap_rotation_deg
    wall["inner_wall"] = create_inner
    wall["inner_wall_height_delta"] = 0.0  # inactive in Pass 51
    wall["inner_wall_height_overrides_json"] = "{}"  # legacy values ignored
    wall["secondary_height_policy"] = "OFFSET_EDGES_3D_STATION_Z_AND_PRIMARY_WALL_H"
    wall["wall_station_policy"] = "INDEPENDENT_FACADE_STATIONS"
    wall["requested_inset"] = inset_distance if create_inner else 0.0
    wall["actual_inset"] = actual_inset
    wall["inset_side"] = inset_side
    wall["closed_ends"] = bool(cap_start or cap_finish)
    wall["closed_start_end"] = bool(cap_start)
    wall["closed_finish_end"] = bool(cap_finish)
    wall["secondary_points_json"] = str(secondary_points_json or '{}') if create_inner else '{}'  # migration only; inert
    wall["top_cap"] = do_between_top or do_full_roof
    wall["bottom_cap"] = do_between_bottom or do_full_floor
    wall["top_cap_mode"] = top_cap_mode
    wall["cap_material_override"] = bool(cap_material is not None and create_top_cap)
    wall["cap_material"] = cap_material.name if cap_material is not None and create_top_cap else ""
    wall["bottom_cap_material_override"] = bool(bottom_cap_material is not None and create_bottom_cap)
    wall["bottom_cap_material"] = bottom_cap_material.name if bottom_cap_material is not None and create_bottom_cap else ""
    # One closure contract: every closing surface records whether it inherits
    # the source wall material or uses an explicit override.  This metadata is
    # intentionally independent from material slot indexes, which can change
    # during rebuild/weld.
    wall["top_cap_material_source"] = "OVERRIDE" if cap_material is not None and create_top_cap else "WALL"
    wall["bottom_cap_material_source"] = "OVERRIDE" if bottom_cap_material is not None and create_bottom_cap else "WALL"
    wall["end_cap_material_source"] = "WALL"
    wall["path_z_min"] = min(float(point.z) for point in path_points)
    wall["path_z_max"] = max(float(point.z) for point in path_points)
    wall["height_reference"] = "PER_PATH_STATION"
    # Guide is optional: create/reuse only *after* the wall has passed all
    # geometry, UV and topology validations. A bad build must not mutate an
    # existing Guide Mesh while the previous stable Wall remains untouched.
    if keep_inner_path and inner_points is not None:
        try:
            owner_uid = ensure_source_uid(path_obj,definition_for(path_obj))
            from ..core.result_lifecycle import find_owned_guides
            guides = find_owned_guides(owner_uid)
            guide = create_guide_object(context,inner_points,output_name+'_InnerPath',
                                        target_obj=guides[0] if guides else None)
            guide['uvwall_guide']=True
            guide['uvwall_child_parent']=owner_uid
            wall['inner_path_object']=guide.name
        except Exception as guide_error:
            log_event(f'GUIDE_BUILD_WARNING wall_kept=True; reason={guide_error}', 'WARNING')

    for obj in context.selected_objects:
        obj.select_set(False)
    wall.select_set(True)
    context.view_layer.objects.active = wall
    return wall, len(mesh.polygons), actual_inset


