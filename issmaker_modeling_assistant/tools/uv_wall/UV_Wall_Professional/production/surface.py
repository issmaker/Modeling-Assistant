# AUTO-EXTRACTED production module from the 1.4.0 verified baseline.

# Runtime integration symbols are bound by the package composition root.
import bpy
import bmesh
import traceback
import webbrowser
from datetime import datetime
from time import perf_counter
from math import ceil, floor, cos, sin, radians, isfinite, acos, degrees, hypot
from mathutils import Vector
from mathutils import kdtree
from mathutils.bvhtree import BVHTree
from mathutils.geometry import tessellate_polygon
from .cap_surface_geometry import patch_diagonal, nonplanar_deviation, height_at_xy, reconcile_patch_boundary_xy, ribbon_height_at_xy, split_local_nonplanar_face, CapClipPoint
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
def build_top_cap(
    outer_points,
    inner_points,
    source,
    tile_length,
    parameter_groups,
    angle_rad=0.0,
    z_offset=0.0,
    preferred_normal=None,
):
    """UV clipping of authored cap strips with per-strip coverage checks."""
    from .planar_integrity import validate_ribbon_coverage
    from .surface_coverage import clip_convex_to_cell, triangulated_cell_fragments, assert_nonoverlapping_fragments
    from .cap_surface_geometry import is_convex_ccw, cap_tile_fragments, reconcile_patch_boundary_xy, rotated_support, cap_cell_uv, cap_tile_ranges, CapClipPoint, ribbon_height_at_xy, split_local_nonplanar_face
    ribbon_sections = validate_ribbon_coverage(outer_points, inner_points)
    log_event(f'CAP_SURFACE segments={ribbon_sections}; method=DETERMINISTIC_TILE_CLIP', 'TOPOLOGY')
    if not isfinite(float(tile_length)) or tile_length <= EPS:
        raise ValueError('Cap Surface: invalid UV module length')
    face_normal_dir = preferred_normal if preferred_normal is not None else Vector((0.0, 0.0, 1.0))
    vertices, faces, face_uvs = [], [], []
    source_segments = 0
    planar_segments = 0
    nonplanar_segments = 0
    concave_segments = 0
    tile_count = 0
    tolerance = max(1.0e-9, min(EPS, tile_length * 1.0e-7))
    area_tolerance = max(tolerance * tolerance, tile_length * tile_length * 1.0e-14)
    # Keep each authored section intact: UV-cell clipping supplies the grid.
    # Extra wall stations remain API-compatible; stitch them downstream.
    for outer_start, outer_end, inner_start, inner_end in zip(
        outer_points, outer_points[1:], inner_points, inner_points[1:],
    ):
        for parameter_0, parameter_1 in ((0.0, 1.0),):
            patch = [
                outer_start.lerp(outer_end, parameter_0),
                outer_start.lerp(outer_end, parameter_1),
                inner_start.lerp(inner_end, parameter_1),
                inner_start.lerp(inner_end, parameter_0),
            ]
            patch = [Vector((point.x, point.y, point.z + z_offset)) for point in patch]
            # This also rejects zero-area, crossed and collapsed miter patches.
            triangles = patch_diagonal(patch)
            deviation = nonplanar_deviation(patch)
            nonplanar = deviation > max(1.0e-6, tile_length * 1.0e-7)
            if nonplanar:
                nonplanar_segments += 1
            else:
                planar_segments += 1
            source_segments += 1
            rotated, support_triangles = rotated_support(
                patch, triangles, angle_rad, CapClipPoint, rotate_xy, signed_area_2d)
            convex = is_convex_ccw(rotated, tolerance)
            if not convex:
                concave_segments += 1
            # Variable Z changes height only; convex XY remains one ribbon domain.
            use_support_triangles = not convex
            smooth_variable_height = nonplanar and convex
            first_u, last_u, first_v, last_v = cap_tile_ranges(rotated, tile_length, tolerance)
            cells = (last_u - first_u + 1) * (last_v - first_v + 1)
            tile_count += cells
            if tile_count > MAX_ROOF_GRID_CELLS:
                raise ValueError('Cap Surface: UV cell count exceeds safety limit')
            expected = abs(signed_area_2d(patch))
            emitted_area = 0.0
            emitted_area_precise = 0.0
            pending = []  # atomic per-patch: do not publish partial clipping
            for tile_u in range(first_u, last_u + 1):
                u_min = tile_u * tile_length
                for tile_v in range(first_v, last_v + 1):
                    v_min = tile_v * tile_length
                    loops = cap_tile_fragments(
                        rotated, support_triangles, use_support_triangles,
                        u_min, v_min, tile_length, tolerance,
                        clip_convex_to_cell, triangulated_cell_fragments,
                    )
                    if len(loops) > 1:
                        assert_nonoverlapping_fragments(loops, area_tolerance)
                    for loop in loops:
                        clipped = []
                        xy_precise = []
                        for point in loop:
                            x, y = unrotate_xy(point.x, point.y, angle_rad)
                            # A valid float32 tile corner can miss the patch by
                            # micrometres; snap XY only to a real patch edge.
                            x, y = reconcile_patch_boundary_xy(
                                patch, x, y, epsilon=tolerance * 4)
                            z = (ribbon_height_at_xy(patch, x, y, epsilon=tolerance * 4)
                                 if smooth_variable_height
                                 else height_at_xy(patch, x, y, triangles, epsilon=tolerance * 4))
                            xy_precise.append(CapClipPoint((x, y, z)))
                            clipped.append(Vector((x, y, z)))
                        clipped = clean_polygon_strict(clipped, min_dist=tolerance)
                        if len(clipped) < 3:
                            continue
                        area = abs(signed_area_2d(clipped))
                        if area <= area_tolerance:
                            continue
                        emitted_area += area
                        emitted_area_precise += abs(signed_area_2d(xy_precise))
                        uvs = cap_cell_uv(clipped, u_min, v_min, tile_length,
                                          angle_rad, rotate_xy, source, bilinear_source_uv)
                        clipped, uvs = orient_polygon(clipped, uvs, face_normal_dir)
                        # Split warped geometry only inside this UV cell, never across the whole ribbon.
                        local_planarity = max(1.0e-6, min(0.001, tile_length * 1.0e-5))
                        local_faces = (split_local_nonplanar_face(clipped, uvs, local_planarity)
                                       if smooth_variable_height else [(clipped, uvs)])
                        pending.extend(local_faces)
                        if len(faces) + len(pending) > MAX_GENERATED_FACES:
                            raise ValueError('Cap Surface: generated face limit exceeded')
            # A hard invariant, with genuine missing area NEVER accepted.
            allowed = max(area_tolerance * 4, expected * 1.0e-5)
            # Two independent checks: (1) float64 clipping must cover the
            # authored patch, and (2) Blender f32 vertex quantization may
            # change the measured area only within its calculated error budget.
            f32_extent = max(1.0, *(max(abs(p.x), abs(p.y)) for p in patch))
            f32_ulp = 2.0 ** (int(f32_extent).bit_length() - 24)
            patch_perimeter = sum(((b.x-a.x)**2+(b.y-a.y)**2)**0.5
                                  for a, b in zip(patch, patch[1:] + patch[:1]))
            quantization_budget = patch_perimeter * 4.0 * f32_ulp + allowed
            if (abs(emitted_area_precise - expected) > allowed or
                    abs(emitted_area - emitted_area_precise) > quantization_budget):
                # Field diagnostics: exact patch coordinates make a future
                # Blender-only failure reproducible without the user's scene.
                log_event(
                    f'CAP_CLIP_FAILURE patch={source_segments}; tile={tile_length}; angle={angle_rad}; '
                    f'expected={expected:.9f}; emitted={emitted_area:.9f}; precise={emitted_area_precise:.9f}; '
                    f'xyz={[(round(p.x, 7), round(p.y, 7), round(p.z, 7)) for p in patch]}',
                    'ERROR',
                )
                raise ValueError(
                    'Cap Surface: deterministic clipping coverage mismatch; '
                    f'patch={source_segments}, expected={expected:.9f}, '
                    f'emitted={emitted_area:.9f}; precise={emitted_area_precise:.9f}; do not publish partial geometry'
                )
            for clipped, uvs in pending:
                offset = len(vertices)
                vertices.extend(tuple(point) for point in clipped)
                faces.append(tuple(offset + index for index in range(len(clipped))))
                face_uvs.append(tuple(uvs))
    if not faces:
        raise ValueError('Cap Surface did not produce any faces')
    log_event(
        f'CAP_SURFACE_TOPOLOGY segments={source_segments}; planar={planar_segments}; '
        f'nonplanar={nonplanar_segments}; concave={concave_segments}; variable_height_mode=LOCAL_CELL; '
        f'cells={tile_count}; polygons={len(faces)}; '
        'ribbon_pre_refinement=OFF; deterministic_surface_clip=ON', 'TOPOLOGY',
    )
    return vertices, faces, face_uvs

def fit_plane_z(points):
    """Least-squares plane z = ax + by + c. Continuous roof surface."""
    n = len(points)
    if n < 3:
        z = points[0].z if points else 0.0
        return 0.0, 0.0, z
    sx = sy = sz = 0.0
    sxx = syy = sxy = sxz = syz = 0.0
    for p in points:
        x, y, z = p.x, p.y, p.z
        sx += x; sy += y; sz += z
        sxx += x * x; syy += y * y; sxy += x * y
        sxz += x * z; syz += y * z
    det = (
        sxx * (syy * n - sy * sy)
        - sxy * (sxy * n - sy * sx)
        + sx * (sxy * sy - syy * sx)
    )
    if abs(det) <= 1.0e-14:
        return 0.0, 0.0, sz / n
    det_a = (
        sxz * (syy * n - sy * sy)
        - sxy * (syz * n - sy * sz)
        + sx * (syz * sy - syy * sz)
    )
    det_b = (
        sxx * (syz * n - sy * sz)
        - sxz * (sxy * n - sy * sx)
        + sx * (sxy * sz - sxz * sx)
    )
    det_c = (
        sxx * (syy * sz - syz * sy)
        - sxy * (sxy * sz - sxz * sy)
        + sxz * (sxy * sy - syy * sx)
    )
    return det_a / det, det_b / det, det_c / det

def project_to_plane_z(point, a, b, c):
    return Vector((point.x, point.y, a * point.x + b * point.y + c))

def cap_weld_tolerance(tile_length):
    """Small scale-aware 3D weld distance for independently clipped pieces."""
    return max(1.0e-8, min(1.0e-5, tile_length * 1.0e-6))

def topology_stitch_tolerance(tile_length):
    """Precision tolerance for true T-junction incidence.

    This is intentionally NOT the 2 mm regulation Weld distance. A vertex that
    is merely 1-2 mm near an unrelated edge is not a topological T-junction.
    For 4 m modules the cap is 0.1 mm; for 2 m modules it is 0.05 mm.
    """
    return max(1.0e-6, min(1.0e-4, float(tile_length) * 2.5e-5))

def dissolve_internal_cap_edges(
    bm,
    uv_tolerance=1.0e-5,
    normal_dot_min=0.999998,
):
    """Remove helper triangulation edges while preserving module UV seams.

    Robust clipping triangulates a concave contour internally.  Two fragments
    belong to the same final module exactly when their UV coordinates agree on
    their shared edge.  Grid/module borders have a UV discontinuity and remain.
    Requiring two non-vertical faces also prevents dissolving wall/cap borders.
    """
    uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        return 0

    bm.normal_update()
    role_layer = bm.faces.layers.int.get(FACE_ROLE_ATTRIBUTE)
    if role_layer is None:
        return 0  # Never dissolve geometry without a verified cap semantic role.
    cap_codes = {FACE_ROLE_CODES["TOP_CAP"], FACE_ROLE_CODES["BOTTOM_CAP"]}
    candidates = []
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        face_a, face_b = edge.link_faces
        if (face_a[role_layer] != face_b[role_layer]
                or face_a[role_layer] not in cap_codes
                or face_a.material_index != face_b.material_index):
            continue
        if abs(face_a.normal.z) <= EPS or abs(face_b.normal.z) <= EPS:
            continue
        # Only remove a cut through the same local support plane.  Edges whose
        # normals differ carry the remeshed height field and must remain.
        if face_a.normal.dot(face_b.normal) < normal_dot_min:
            continue

        uv_by_face = []
        valid = True
        for face in (face_a, face_b):
            mapping = {
                loop.vert: loop[uv_layer].uv.copy()
                for loop in face.loops
                if loop.vert in edge.verts
            }
            if len(mapping) != 2:
                valid = False
                break
            uv_by_face.append(mapping)
        if not valid:
            continue

        if all(
            (uv_by_face[0][vertex] - uv_by_face[1][vertex]).length
            <= uv_tolerance
            for vertex in edge.verts
        ):
            candidates.append(edge)

    if candidates:
        bmesh.ops.dissolve_edges(
            bm,
            edges=candidates,
            use_verts=False,
            use_face_split=False,
        )
    return len(candidates)


def merge_mesh_by_xy(mesh, dist=1.0e-6, dissolve_cap_triangles=False):
    """Weld coincident 3D vertices without moving nearby boundary geometry.

    Kept under the old name for compatibility with existing calls.  The old
    implementation bucketed vertices by XY, averaged their XYZ coordinates,
    and welded at 0.003 units.  Distinct boundary and interior vertices could
    therefore be pulled together, moving the roof across (or away from) the
    source contour.
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
    bmesh.ops.dissolve_degenerate(bm, dist=dist, edges=bm.edges)
    if dissolve_cap_triangles:
        dissolve_internal_cap_edges(bm)
    wire = [e for e in bm.edges if e.is_wire]
    if wire:
        bmesh.ops.delete(bm, geom=wire, context="EDGES")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def _point_in_poly_xy(x, y, poly):
    """Ray-cast point-in-polygon (XY). poly without duplicate closing point."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i].x, poly[i].y
        xj, yj = poly[j].x, poly[j].y
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


def _closest_on_boundary(x, y, poly):
    """Closest point on boundary polyline (XY) and distance²."""
    best_d = None
    best_pt = poly[0].copy()
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        abx, aby = b.x - a.x, b.y - a.y
        len2 = abx * abx + aby * aby
        if len2 <= EPS:
            t = 0.0
            px, py = a.x, a.y
            d = (x - a.x) ** 2 + (y - a.y) ** 2
        else:
            t = ((x - a.x) * abx + (y - a.y) * aby) / len2
            t = max(0.0, min(1.0, t))
            px = a.x + abx * t
            py = a.y + aby * t
            d = (x - px) ** 2 + (y - py) ** 2
        if best_d is None or d < best_d:
            best_d = d
            pz = a.z + (b.z - a.z) * t
            best_pt = Vector((px, py, pz))
    return best_pt, (best_d if best_d is not None else 0.0)


def _closest_boundary_z(x, y, poly):
    pt, d2 = _closest_on_boundary(x, y, poly)
    return pt.z, d2


def _mean_value_z(x, y, poly, boundary_z=None, boundary_d2=None):
    """Smooth interior Z from boundary via mean-value coordinates (Hormann/Floater).

    On/near boundary returns exact boundary Z. Always uses heights (even tiny span).
    """
    n = len(poly)
    if n < 3:
        return poly[0].z

    if boundary_z is None or boundary_d2 is None:
        # Standalone call: resolve both vertex/edge proximity here.
        for p in poly:
            if (x - p.x) ** 2 + (y - p.y) ** 2 <= EPS * EPS:
                return p.z
        z_b, d2 = _closest_boundary_z(x, y, poly)
    else:
        # The caller already performed the expensive boundary search.
        z_b, d2 = boundary_z, boundary_d2
    if d2 <= (1.0e-6) ** 2:
        return z_b

    def _tan_half(ax, ay, bx, by):
        la = (ax * ax + ay * ay) ** 0.5
        lb = (bx * bx + by * by) ** 0.5
        if la <= EPS or lb <= EPS:
            return 0.0
        cos_a = (ax * bx + ay * by) / (la * lb)
        sin_a = (ax * by - ay * bx) / (la * lb)
        cos_a = max(-1.0, min(1.0, cos_a))
        denom = 1.0 + cos_a
        if abs(denom) <= 1.0e-12:
            return 0.0
        return sin_a / denom

    weights = [0.0] * n
    for i in range(n):
        vi = poly[i]
        v_im = poly[(i - 1) % n]
        v_ip = poly[(i + 1) % n]

        dx, dy = vi.x - x, vi.y - y
        di = (dx * dx + dy * dy) ** 0.5
        if di <= EPS:
            return vi.z

        t1 = _tan_half(v_im.x - x, v_im.y - y, dx, dy)
        t2 = _tan_half(dx, dy, v_ip.x - x, v_ip.y - y)
        weights[i] = (t1 + t2) / di

    wsum = sum(weights)
    if abs(wsum) <= 1.0e-30:
        return z_b
    z = 0.0
    for i in range(n):
        z += weights[i] * poly[i].z
    return z / wsum


def _smooth_local_boundary_z(x, y, poly, neighbor_count=8):
    """Stable local Shepard interpolation from the nearest boundary segments.

    All weights are positive, so concave contours cannot create overshoot or
    deep spikes.  Close to the border the closest grass/path segment dominates;
    toward the center several nearby segments blend smoothly.
    """
    candidates = []
    count = len(poly)
    for index, start in enumerate(poly):
        end = poly[(index + 1) % count]
        dx = end.x - start.x
        dy = end.y - start.y
        length_sq = dx * dx + dy * dy
        if length_sq <= EPS * EPS:
            factor = 0.0
        else:
            factor = (
                (x - start.x) * dx + (y - start.y) * dy
            ) / length_sq
            factor = max(0.0, min(1.0, factor))
        closest_x = start.x + dx * factor
        closest_y = start.y + dy * factor
        distance_sq = (x - closest_x) ** 2 + (y - closest_y) ** 2
        z = start.z + (end.z - start.z) * factor
        if distance_sq <= EPS * EPS:
            return z
        candidates.append((distance_sq, z))

    candidates.sort(key=lambda item: item[0])
    selected = candidates[:max(1, min(int(neighbor_count), len(candidates)))]
    if not selected:
        return poly[0].z if poly else 0.0

    # Softening avoids a harsh Voronoi seam while inverse-square distance keeps
    # the result strongly local and inside the surrounding height range.
    nearest_distance_sq = selected[0][0]
    softening = max(EPS * EPS, nearest_distance_sq * 0.05)
    weighted_z = 0.0
    weight_sum = 0.0
    for distance_sq, z in selected:
        weight = 1.0 / (distance_sq + softening)
        weighted_z += weight * z
        weight_sum += weight
    return weighted_z / weight_sum if weight_sum > 0.0 else selected[0][1]


def simplify_height_boundary(poly, tolerance):
    """Drop intermediate points that are collinear in full XYZ.

    UV-grid densification adds many boundary points needed for welding, but
    those points do not add height information along a straight 3D segment.
    """
    if len(poly) <= 3:
        return [point.copy() for point in poly]
    result = []
    count = len(poly)
    for index, point in enumerate(poly):
        previous = poly[(index - 1) % count]
        following = poly[(index + 1) % count]
        span = following - previous
        span_sq = span.length_squared
        if span_sq <= tolerance * tolerance:
            result.append(point.copy())
            continue
        factor = (point - previous).dot(span) / span_sq
        projected = previous + span * factor
        if -EPS <= factor <= 1.0 + EPS and (point - projected).length <= tolerance:
            continue
        result.append(point.copy())
    return result if len(result) >= 3 else [point.copy() for point in poly]


def simplify_module_loop(points, boundary_poly, tolerance):
    """Remove service vertices on straight module sides, keep real boundary."""
    result = clean_polygon_strict(points, min_dist=tolerance)
    if len(result) <= 3:
        return result
    changed = True
    while changed and len(result) > 3:
        changed = False
        reduced = []
        count = len(result)
        for index, point in enumerate(result):
            _boundary_point, boundary_d2 = _closest_on_boundary(
                point.x, point.y, boundary_poly,
            )
            if boundary_d2 <= tolerance * tolerance:
                reduced.append(point.copy())
                continue
            previous = result[(index - 1) % count]
            following = result[(index + 1) % count]
            span_x = following.x - previous.x
            span_y = following.y - previous.y
            span_sq = span_x * span_x + span_y * span_y
            if span_sq <= tolerance * tolerance:
                reduced.append(point.copy())
                continue
            factor = (
                (point.x - previous.x) * span_x
                + (point.y - previous.y) * span_y
            ) / span_sq
            projected_x = previous.x + span_x * factor
            projected_y = previous.y + span_y * factor
            distance_sq = (
                (point.x - projected_x) ** 2
                + (point.y - projected_y) ** 2
            )
            if (
                -EPS <= factor <= 1.0 + EPS
                and distance_sq <= tolerance * tolerance
            ):
                changed = True
                continue
            reduced.append(point.copy())
        if len(reduced) < 3:
            break
        result = reduced
    return result


def merge_triangle_fragments_to_loops(fragments, tolerance):
    """Union one tile's flat fragments with BMesh and return final face loops.

    BMesh owns the topology: coincident fragment vertices are welded and every
    edge shared by two coplanar fragments is dissolved.  This avoids fragile
    hand-tracing at junctions and guarantees that service triangulation cannot
    survive inside a completed module.
    """
    if not fragments:
        return []
    weld_dist = max(tolerance, 1.0e-9)
    scale = 1.0 / weld_dist
    bm = bmesh.new()
    vertex_map = {}
    for fragment in fragments:
        polygon = clean_polygon_strict(fragment, min_dist=tolerance)
        if len(polygon) < 3:
            continue
        if signed_area_2d(polygon) < 0.0:
            polygon.reverse()
        face_verts = []
        for point in polygon:
            key = (
                int(round(point.x * scale)),
                int(round(point.y * scale)),
            )
            vertex = vertex_map.get(key)
            if vertex is None:
                vertex = bm.verts.new((point.x, point.y, 0.0))
                vertex_map[key] = vertex
            if not face_verts or face_verts[-1] is not vertex:
                face_verts.append(vertex)
        if len(face_verts) > 1 and face_verts[0] is face_verts[-1]:
            face_verts.pop()
        if len(set(face_verts)) < 3:
            continue
        try:
            bm.faces.new(face_verts)
        except ValueError:
            # An identical fragment can occur on a numeric tile boundary.
            continue

    if not bm.faces:
        bm.free()
        return []

    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=weld_dist)
    # Repeat because dissolving a group can expose another shared edge in the
    # newly merged ngon.  Stop only when the tile consists of boundary edges.
    for _pass in range(8):
        internal_edges = [edge for edge in bm.edges if len(edge.link_faces) == 2]
        if not internal_edges:
            break
        bmesh.ops.dissolve_edges(
            bm,
            edges=internal_edges,
            use_verts=False,
            use_face_split=False,
        )
    bmesh.ops.dissolve_degenerate(
        bm,
        dist=weld_dist,
        edges=list(bm.edges),
    )

    loops = []
    for face in bm.faces:
        loop = clean_polygon_strict(
            [vertex.co.copy() for vertex in face.verts],
            min_dist=tolerance,
        )
        if len(loop) >= 3 and abs(signed_area_2d(loop)) > tolerance * tolerance:
            loops.append(loop)
    bm.free()
    return loops

def build_adaptive_cap_support(poly, base_triangles, tile_length):
    """Create a fine conforming height-field mesh with an exact boundary.

    The contour tessellation is only a safe topological starting point.  Long
    edges are repeatedly split, all resulting faces are triangulated, and Z is
    evaluated at every added point from the boundary height field.  Original
    boundary points are reused exactly; points added on boundary edges receive
    the exact interpolated boundary Z.
    """
    if not base_triangles:
        return []

    area = abs(signed_area_2d(poly))
    # Half a UV module is sufficiently dense for a smooth support surface but
    # produces roughly four times fewer faces than the former quarter-module
    # step.
    desired_step = tile_length * 0.50
    # Bound pathological memory use on very large fields while keeping the
    # support substantially denser than the visible UV modules.
    safety_step = (area / 15000.0) ** 0.5 if area > EPS * EPS else desired_step
    target_edge = max(desired_step, safety_step, EPS * 8.0)
    boundary_snap = max(1.0e-8, min(EPS, tile_length * 1.0e-7)) * 4.0
    height_poly = simplify_height_boundary(
        poly,
        tolerance=max(EPS, tile_length * 1.0e-8),
    )
    bm = bmesh.new()
    vertex_map = {}
    height_cache = {}
    cache_scale = max(EPS, target_edge * 1.0e-8)

    def evaluated_position(x, y):
        """Exact boundary/smooth interior position, evaluated only once per XY."""
        key = (int(round(x / cache_scale)), int(round(y / cache_scale)))
        cached = height_cache.get(key)
        if cached is not None:
            return cached.copy()
        boundary_point, distance_sq = _closest_on_boundary(x, y, poly)
        if distance_sq <= boundary_snap * boundary_snap:
            position = boundary_point.copy()
        else:
            position = Vector((
                x,
                y,
                _mean_value_z(
                    x,
                    y,
                    height_poly,
                    boundary_z=boundary_point.z,
                    boundary_d2=distance_sq,
                ),
            ))
        height_cache[key] = position.copy()
        return position

    def support_vertex(point):
        key = (
            int(round(point.x / cache_scale)),
            int(round(point.y / cache_scale)),
        )
        vertex = vertex_map.get(key)
        if vertex is not None:
            return vertex
        vertex = bm.verts.new(evaluated_position(point.x, point.y))
        vertex_map[key] = vertex
        return vertex

    for triangle in base_triangles:
        verts = [support_vertex(point) for point in triangle]
        if len(set(verts)) != 3:
            continue
        try:
            bm.faces.new(verts)
        except ValueError:
            continue

    if not bm.faces:
        bm.free()
        return []

    # Midpoint refinement is conforming because BMesh splits every linked face
    # of a selected edge.  Retriangulating after each pass prevents large ngons
    # and limits all support edges, not only the initial contour diagonals.
    for _iteration in range(12):
        long_edges = []
        for edge in bm.edges:
            a, b = edge.verts
            dx = a.co.x - b.co.x
            dy = a.co.y - b.co.y
            if dx * dx + dy * dy > target_edge * target_edge * 1.000001:
                long_edges.append(edge)
        if not long_edges:
            break

        previous_vertices = set(bm.verts)
        bmesh.ops.subdivide_edges(
            bm,
            edges=long_edges,
            cuts=1,
            use_grid_fill=True,
        )
        if bm.faces:
            bmesh.ops.triangulate(
                bm,
                faces=list(bm.faces),
                quad_method="BEAUTY",
                ngon_method="BEAUTY",
            )

        # Existing vertices already have their final Z.  Re-evaluating all of
        # them on every pass made large contours orders of magnitude slower.
        for vertex in bm.verts:
            if vertex not in previous_vertices:
                vertex.co = evaluated_position(vertex.co.x, vertex.co.y)

    if bm.faces:
        bmesh.ops.triangulate(
            bm,
            faces=list(bm.faces),
            quad_method="BEAUTY",
            ngon_method="BEAUTY",
        )
        internal_edges = [edge for edge in bm.edges if len(edge.link_faces) == 2]
        # Global edge flipping is useful on moderate meshes but expensive and
        # barely visible once the support is already dense.
        if internal_edges and len(bm.faces) <= 25000:
            bmesh.ops.beautify_fill(
                bm,
                edges=internal_edges,
                method="AREA",
            )
    result = [
        [vertex.co.copy() for vertex in face.verts]
        for face in bm.faces
        if len(face.verts) == 3
    ]
    bm.free()
    return result

def clip_segment_to_rect_2d(a, b, x_min, x_max, y_min, y_max, tolerance):
    """Liang-Barsky clip of one 2D segment; returns zero or one segment."""
    dx = b.x - a.x
    dy = b.y - a.y
    t0 = 0.0
    t1 = 1.0
    for p, q in (
        (-dx, a.x - x_min),
        (dx, x_max - a.x),
        (-dy, a.y - y_min),
        (dy, y_max - a.y),
    ):
        if abs(p) <= tolerance:
            if q < -tolerance:
                return []
            continue
        ratio = q / p
        if p < 0.0:
            t0 = max(t0, ratio)
        else:
            t1 = min(t1, ratio)
        if t0 > t1 + tolerance:
            return []
    start = Vector((a.x + dx * t0, a.y + dy * t0, 0.0))
    end = Vector((a.x + dx * t1, a.y + dy * t1, 0.0))
    if (end - start).length <= tolerance:
        return []
    return [(start, end)]

def segment_split_parameters_2d(a, b, c, d, tolerance):
    """Parameters on a→b where it meets c→d, including collinear overlap."""
    rx = b.x - a.x
    ry = b.y - a.y
    sx = d.x - c.x
    sy = d.y - c.y
    denominator = rx * sy - ry * sx
    qx = c.x - a.x
    qy = c.y - a.y
    if abs(denominator) > tolerance:
        t = (qx * sy - qy * sx) / denominator
        u = (qx * ry - qy * rx) / denominator
        if -tolerance <= t <= 1.0 + tolerance and -tolerance <= u <= 1.0 + tolerance:
            return [max(0.0, min(1.0, t))]
        return []
    if abs(qx * ry - qy * rx) > tolerance:
        return []
    length_sq = rx * rx + ry * ry
    if length_sq <= tolerance * tolerance:
        return []
    return [
        max(0.0, min(1.0, ((point.x - a.x) * rx + (point.y - a.y) * ry) / length_sq))
        for point in (c, d)
    ]

def face_loops_from_planar_segments(segments, tolerance):
    """Create clean polygon loops from a planar boundary-segment network."""
    if not segments:
        return []
    weld_dist = max(tolerance, 1.0e-9)
    scale = 1.0 / weld_dist
    bm = bmesh.new()
    vertex_map = {}

    def vertex_for(point):
        key = (int(round(point.x * scale)), int(round(point.y * scale)))
        vertex = vertex_map.get(key)
        if vertex is None:
            vertex = bm.verts.new((point.x, point.y, 0.0))
            vertex_map[key] = vertex
        return vertex

    for start, end in segments:
        a = vertex_for(start)
        b = vertex_for(end)
        if a is b:
            continue
        try:
            bm.edges.new((a, b))
        except ValueError:
            continue
    if not bm.edges:
        bm.free()
        return []
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=weld_dist)
    bmesh.ops.contextual_create(bm, geom=list(bm.edges))
    loops = []
    for face in bm.faces:
        loop = clean_polygon_strict(
            [vertex.co.copy() for vertex in face.verts],
            min_dist=tolerance,
        )
        if len(loop) >= 3 and abs(signed_area_2d(loop)) > tolerance * tolerance:
            loops.append(loop)
    bm.free()
    return loops

def intersect_array_plane_with_contour(
    polygon,
    candidate_edges,
    x_min,
    x_max,
    y_min,
    y_max,
    tolerance,
):
    """Direct rectangle/polygon intersection without triangulating the plane."""
    if not candidate_edges:
        center_x = (x_min + x_max) * 0.5
        center_y = (y_min + y_max) * 0.5
        if not _point_in_poly_xy(center_x, center_y, polygon):
            return []
        return [[
            Vector((x_min, y_min, 0.0)),
            Vector((x_max, y_min, 0.0)),
            Vector((x_max, y_max, 0.0)),
            Vector((x_min, y_max, 0.0)),
        ]]

    segments = []
    for edge_index in candidate_edges:
        a = polygon[edge_index]
        b = polygon[(edge_index + 1) % len(polygon)]
        segments.extend(clip_segment_to_rect_2d(
            a, b, x_min, x_max, y_min, y_max, tolerance,
        ))

    rectangle = [
        Vector((x_min, y_min, 0.0)),
        Vector((x_max, y_min, 0.0)),
        Vector((x_max, y_max, 0.0)),
        Vector((x_min, y_max, 0.0)),
    ]
    polygon_edges = [
        (polygon[index], polygon[(index + 1) % len(polygon)])
        for index in candidate_edges
    ]
    for rect_start, rect_end in zip(rectangle, rectangle[1:] + rectangle[:1]):
        parameters = [0.0, 1.0]
        for edge_start, edge_end in polygon_edges:
            parameters.extend(segment_split_parameters_2d(
                rect_start, rect_end, edge_start, edge_end, tolerance,
            ))
        parameters = unique_sorted(parameters)
        for start_t, end_t in zip(parameters, parameters[1:]):
            if end_t - start_t <= tolerance:
                continue
            middle = rect_start.lerp(rect_end, (start_t + end_t) * 0.5)
            inside = _point_in_poly_xy(middle.x, middle.y, polygon)
            if not inside:
                _closest, distance_sq = _closest_on_boundary(
                    middle.x, middle.y, polygon,
                )
                inside = distance_sq <= tolerance * tolerance
            if inside:
                segments.append((
                    rect_start.lerp(rect_end, start_t),
                    rect_start.lerp(rect_end, end_t),
                ))
    return face_loops_from_planar_segments(segments, tolerance)

def _build_full_top_cap_uncached(
    path_points,
    source,
    tile_length,
    angle_rad=0.0,
    z_offset=0.0,
    preferred_normal=None,
):
    """Full UV roof for ONE closed contour (per-chain isolation).

    - Boundary densified to UV module grid; Z exact from the chain
    - Interior: regular UV tiles; Z = smooth mean-value field from boundary
    - Always respects heights (no flat shortcut)
    """
    from .surface_coverage import coverage_report, triangulated_cell_fragments
    if tile_length <= EPS:
        raise ValueError("Длина модуля UV должна быть больше нуля")

    pts = [Vector((p.x, p.y, p.z + z_offset)) for p in path_points]
    if len(pts) >= 2 and (pts[0] - pts[-1]).length <= 1.0e-3:
        pts[-1] = pts[0].copy()
    if not is_path_closed(pts):
        if len(pts) >= 3 and horizontal_length(pts[0], pts[-1]) <= 1.0e-3:
            pts = list(pts) + [pts[0].copy()]
        else:
            raise ValueError("Режим «Полная крыша» требует замкнутый путь")

    poly = clean_polygon(pts)
    if len(poly) < 3:
        raise ValueError("Контур полной крыши слишком мал")
    if abs(signed_area_2d(poly)) <= EPS:
        raise ValueError("Контур полной крыши имеет нулевую площадь в XY")
    if signed_area_2d(poly) < 0.0:
        poly.reverse()

    # Grid densify only (module seams). No extra arc-length densify.
    _perf_started = perf_counter()
    original_boundary_count = len(poly)
    poly = densify_polygon_on_rotated_grid(poly, tile_length, angle_rad)
    poly = clean_polygon_strict(poly, min_dist=max(EPS, tile_length * 1.0e-9))
    if len(poly) < 3:
        raise ValueError("Контур полной крыши слишком мал после уплотнения")
    if signed_area_2d(poly) < 0.0:
        poly.reverse()
    log_event(
        f"SURFACE_GRID_BOUNDARY original={original_boundary_count}; "
        f"after_grid={len(poly)}; added_grid_crossings={len(poly) - original_boundary_count}",
        "TOPOLOGY",
    )
    record_stage(log_event, "FULL_CAP_BOUNDARY_GRID", _perf_started)

    # Array-like construction: a regular grid of intact quad planes.  Only
    # planes crossed by the contour are clipped, directly by contour segments.
    rotated_poly = []
    for point in poly:
        u, v = rotate_xy(point.x, point.y, angle_rad)
        rotated_poly.append(Vector((u, v, 0.0)))
    if signed_area_2d(rotated_poly) < 0.0:
        rotated_poly.reverse()

    min_u = min(point.x for point in rotated_poly)
    max_u = max(point.x for point in rotated_poly)
    min_v = min(point.y for point in rotated_poly)
    max_v = max(point.y for point in rotated_poly)

    estimated_cells = (
        (int(floor(max_u / tile_length)) - int(floor(min_u / tile_length)) + 3)
        * (int(floor(max_v / tile_length)) - int(floor(min_v / tile_length)) + 3)
    )
    if estimated_cells > MAX_ROOF_GRID_CELLS:
        raise ValueError(
            f"Крыша требует обработать примерно {estimated_cells:,} UV-ячеек. "
            f"Безопасный предел: {MAX_ROOF_GRID_CELLS:,}. Увеличьте размер модуля."
        )

    contour_edges_by_tile = {}
    for edge_index, (start, end) in enumerate(zip(
        rotated_poly,
        rotated_poly[1:] + rotated_poly[:1],
    )):
        edge_first_u = int(floor(min(start.x, end.x) / tile_length))
        edge_last_u = int(floor(max(start.x, end.x) / tile_length))
        edge_first_v = int(floor(min(start.y, end.y) / tile_length))
        edge_last_v = int(floor(max(start.y, end.y) / tile_length))
        for tile_u in range(edge_first_u, edge_last_u + 1):
            for tile_v in range(edge_first_v, edge_last_v + 1):
                contour_edges_by_tile.setdefault((tile_u, tile_v), []).append(edge_index)

    first_u = int(floor(min_u / tile_length)) - 1
    last_u = int(floor(max_u / tile_length)) + 1
    first_v = int(floor(min_v / tile_length)) - 1
    last_v = int(floor(max_v / tile_length)) + 1

    face_normal_dir = preferred_normal if preferred_normal is not None else Vector((0.0, 0.0, 1.0))
    vertices = []
    faces = []
    face_uvs = []
    # EPS is a distance, therefore using it directly as an area threshold can
    # delete legitimate thin boundary fragments.  Keep linear/area tolerances
    # dimensionally correct and relative to the module size.
    linear_eps = max(1.0e-9, min(EPS, tile_length * 1.0e-7))
    area_eps = max(linear_eps * linear_eps, tile_length * tile_length * 1.0e-14)
    # Only snap numerical noise.  The old 1e-4-of-a-tile radius could collapse
    # a real narrow fragment before it reached the drawn boundary.
    snap = max(linear_eps * 8.0, tile_length * 1.0e-5)
    height_poly = simplify_height_boundary(
        poly,
        tolerance=max(EPS, tile_length * 1.0e-8),
    )
    height_min = min(point.z for point in height_poly)
    height_max = max(point.z for point in height_poly)
    height_cache = {}
    height_scale = 1.0 / linear_eps

    def place_vertex(point):
        key = (
            int(round(point.x * height_scale)),
            int(round(point.y * height_scale)),
        )
        cached = height_cache.get(key)
        if cached is not None:
            return cached.copy()
        bp, d2 = _closest_on_boundary(point.x, point.y, poly)
        if d2 <= snap * snap:
            result = bp.copy()
        else:
            field_z = _mean_value_z(
                point.x,
                point.y,
                height_poly,
                boundary_z=bp.z,
                boundary_d2=d2,
            )
            if not isfinite(field_z):
                field_z = bp.z
            field_z = max(height_min, min(height_max, field_z))
            result = Vector((point.x, point.y, field_z))
        height_cache[key] = result.copy()
        return result

    def emit(clipped, u_min, v_min):
        nonlocal vertices, faces, face_uvs
        if len(clipped) < 3:
            return
        clipped = clean_polygon_strict(clipped, min_dist=linear_eps)
        if len(clipped) < 3:
            return
        shaped = [place_vertex(p) for p in clipped]
        shaped = clean_polygon_strict(shaped, min_dist=linear_eps)
        if len(shaped) < 3:
            return
        area = abs(signed_area_2d(shaped))
        if area <= area_eps:
            return
        perim = sum((a - b).length for a, b in zip(shaped, shaped[1:] + shaped[:1]))
        if perim > EPS and area < perim * perim * 1e-11:
            return
        uvs = []
        for point in shaped:
            ru, rv = rotate_xy(point.x, point.y, angle_rad)
            u = max(0.0, min(1.0, (ru - u_min) / tile_length))
            v = max(0.0, min(1.0, (rv - v_min) / tile_length))
            uvs.append(bilinear_source_uv(source, u, v))
        shaped, uvs = orient_polygon(shaped, uvs, face_normal_dir)
        if face_normal_dir.z < 0.0:
            uvs = mirror_uvs_horizontally(uvs, source)
        off = len(vertices)
        vertices.extend(tuple(p) for p in shaped)
        faces.append(tuple(off + i for i in range(len(shaped))))
        face_uvs.append(tuple(uvs))

    log_event(
        f"SURFACE_HEIGHT_FIELD method=MEAN_VALUE_GLOBAL; "
        f"boundary_points={len(height_poly)}; module={tile_length:.6f}; "
        f"z_range=({height_min:.9f},{height_max:.9f}); clamp=True",
        "TOPOLOGY",
    )

    cells = []
    for tile_u in range(first_u, last_u + 1):
        u_min = tile_u * tile_length
        u_max = u_min + tile_length
        for tile_v in range(first_v, last_v + 1):
            v_min = tile_v * tile_length
            v_max = v_min + tile_length
            loops_uv = intersect_array_plane_with_contour(
                rotated_poly,
                contour_edges_by_tile.get((tile_u, tile_v), ()),
                u_min,
                u_max,
                v_min,
                v_max,
                tolerance=linear_eps,
            )
            cells.append((tile_u, tile_v, loops_uv,
                          bool(contour_edges_by_tile.get((tile_u, tile_v)))))
            for loop_uv in loops_uv:
                world_loop = []
                for point in loop_uv:
                    x, y = unrotate_xy(point.x, point.y, angle_rad)
                    world_loop.append(Vector((x, y, 0.0)))
                emit(world_loop, u_min, v_min)

    # Boundary reconstruction from intersecting segments can silently omit
    # a tile (e.g. when contextual_create sees a T-junction).  Validate the
    # *filled area*, not only the perimeter: a gap in the interior has a
    # perfectly good outside boundary and used to pass all old checks.
    report = coverage_report(poly, [[Vector(vertices[i]) for i in face] for face in faces], tile_length)
    status = 'OK'
    if not report['ok']:
        # Only on a detected loss/overlap, triangulate the original polygon
        # and reclip its true covered fragments to the same UV-module grid.
        # Intact interior quads stay quads; boundary cells get accurate planar
        # fragments.  No shapely dependency and no arbitrary new UV size.
        raw = tessellate_polygon([rotated_poly])
        triangles = []
        for triangle in raw:
            pieces = [rotated_poly[p] if isinstance(p, int) else p for p in triangle]
            if len(pieces) == 3:
                triangles.append(pieces)
        if not triangles:
            raise ValueError('Поверхность: невозможно восстановить пограничные UV-модули (триангуляция не удалась)')
        vertices.clear()
        faces.clear()
        face_uvs.clear()
        height_cache.clear()
        fixed_cells = 0
        for tile_u, tile_v, old_loops, touches_boundary in cells:
            u_min, v_min = tile_u * tile_length, tile_v * tile_length
            # Boundary tiles and any interior tile unexpectedly left blank
            # are reconstructed.  Valid full interior quads stay untouched.
            center_inside = (not old_loops and _point_in_poly_xy(
                u_min + tile_length * 0.5, v_min + tile_length * 0.5,
                rotated_poly))
            needs_repair = touches_boundary or center_inside
            repaired = (triangulated_cell_fragments(triangles, u_min, v_min, tile_length,
                                                    epsilon=linear_eps)
                        if needs_repair else old_loops)
            if needs_repair:
                fixed_cells += 1
            for loop_uv in repaired:
                world_loop = []
                for point in loop_uv:
                    x, y = unrotate_xy(point.x, point.y, angle_rad)
                    world_loop.append(Vector((x, y, 0.0)))
                emit(world_loop, u_min, v_min)
        final = coverage_report(poly, [[Vector(vertices[i]) for i in face] for face in faces], tile_length)
        log_event(
            f'SURFACE_TILE_REPAIR boundary_cells={fixed_cells}; '
            f'before_delta={report["delta"]:.8f}; after_delta={final["delta"]:.8f}; '
            f'tolerance={final["tolerance"]:.8f}; status={"OK" if final["ok"] else "FAILED"}',
            'SURFACE_CHECK',
        )
        if not final['ok']:
            raise ValueError(
                'Поверхность: неполное покрытие после восстановления UV-модулей; '
                f'площадь ожидаемая={final["expected"]:.6f}, фактическая={final["actual"]:.6f}. '
                'Старый результат сохранён.'
            )
        status = 'REPAIRED'
        report = final
    log_event(
        f'SURFACE_TILE_COVERAGE status={status}; expected={report["expected"]:.6f}; '
        f'actual={report["actual"]:.6f}; delta={report["delta"]:.8f}; '
        f'tolerance={report["tolerance"]:.8f}; faces={len(faces)}', 'SURFACE_CHECK',
    )
    record_stage(log_event, "FULL_CAP_CLIP_HEIGHT_FIELD", _perf_started)
    if not faces:
        raise ValueError("Полная крыша не создала ни одной грани")
    return vertices, faces, face_uvs

def _build_triangulated_surface_uncached(
    path_points,
    source,
    tile_length,
    angle_rad=0.0,
    z_offset=0.0,
    preferred_normal=None,
):
    """Stable non-planar surface: constrained boundary triangles, then UV tile clipping."""
    points = [Vector((p.x, p.y, p.z + z_offset)) for p in path_points]
    if is_path_closed(points):
        points = points[:-1]
    points = clean_polygon_strict(points, max(EPS, tile_length * 1.0e-9))
    if len(points) < 3 or abs(signed_area_2d(points)) <= EPS:
        raise ValueError("Контур поверхности слишком мал или имеет нулевую площадь")
    if signed_area_2d(points) < 0.0:
        points.reverse()
    raw_triangles = tessellate_polygon([points])
    if not raw_triangles:
        raise ValueError("Не удалось триангулировать контур поверхности")

    base_triangles = [
        [points[item].copy() if isinstance(item, int) else item.copy() for item in triangle]
        for triangle in raw_triangles
    ]
    triangles = build_adaptive_cap_support(points, base_triangles, tile_length)
    if not triangles:
        raise ValueError("Не удалось создать адаптивную опорную сетку поверхности")
    log_event(
        f"SURFACE_SUPPORT base_triangles={len(base_triangles)}; "
        f"adaptive_triangles={len(triangles)}; max_target_edge={tile_length * 0.5:.6f}",
        "TOPOLOGY",
    )

    preferred = preferred_normal or Vector((0.0, 0.0, 1.0))
    vertices, faces, face_uvs = [], [], []
    area_eps = max(1.0e-12, tile_length * tile_length * 1.0e-14)
    for triangle in triangles:
        tri = [point.copy() for point in triangle]
        rotated = [rotate_xy(point.x, point.y, angle_rad) for point in tri]
        first_u = int(floor(min(u for u, _v in rotated) / tile_length))
        last_u = int(floor((max(u for u, _v in rotated) - EPS) / tile_length))
        first_v = int(floor(min(v for _u, v in rotated) / tile_length))
        last_v = int(floor((max(v for _u, v in rotated) - EPS) / tile_length))
        for tile_u in range(first_u, last_u + 1):
            u_min = tile_u * tile_length
            u_max = u_min + tile_length
            for tile_v in range(first_v, last_v + 1):
                v_min = tile_v * tile_length
                v_max = v_min + tile_length
                clipped = clean_polygon_strict(
                    clip_polygon_to_rotated_tile(tri, u_min, u_max, v_min, v_max, angle_rad),
                    max(1.0e-9, tile_length * 1.0e-9),
                )
                if len(clipped) < 3 or abs(signed_area_2d(clipped)) <= area_eps:
                    continue
                uvs = []
                for point in clipped:
                    ru, rv = rotate_xy(point.x, point.y, angle_rad)
                    u = max(0.0, min(1.0, (ru - u_min) / tile_length))
                    v = max(0.0, min(1.0, (rv - v_min) / tile_length))
                    uvs.append(bilinear_source_uv(source, u, v))
                clipped, uvs = orient_polygon(clipped, uvs, preferred)
                if preferred.z < 0.0:
                    uvs = mirror_uvs_horizontally(uvs, source)
                offset = len(vertices)
                vertices.extend(tuple(point) for point in clipped)
                faces.append(tuple(offset + index for index in range(len(clipped))))
                face_uvs.append(tuple(uvs))
    if not faces:
        raise ValueError("Триангулированная поверхность не создала граней")
    return vertices, faces, face_uvs

def build_full_top_cap(path_points, source, tile_length, angle_rad=0.0, z_offset=0.0, preferred_normal=None):
    return cached_surface_call(
        "FULL_CAP_COVERAGE_V2", _build_full_top_cap_uncached, path_points, source, tile_length,
        angle_rad, z_offset, preferred_normal, log_event,
    )

def build_triangulated_surface(path_points, source, tile_length, angle_rad=0.0, z_offset=0.0, preferred_normal=None):
    return cached_surface_call(
        "TRI_SURFACE", _build_triangulated_surface_uncached, path_points, source, tile_length,
        angle_rad, z_offset, preferred_normal, log_event,
    )

