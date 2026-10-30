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
    validate_and_fix_role_normals, expected_component_count,
)
from ..core.result_lifecycle import (
    preferred_stable_result, install_mesh_on_result, cleanup_duplicate_results,
    prepare_result_metadata, shutdown_result_lifecycle,
)
from ..core.diagnostics import log_result_snapshot as _log_result_snapshot
from ..core.path_proximity import vertex_edge_candidates
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

def rotate_xy(x, y, angle_rad):
    """Rotate (x, y) into the cap-tile coordinate frame."""
    c = cos(angle_rad)
    s = sin(angle_rad)
    return x * c + y * s, -x * s + y * c


def unrotate_xy(u, v, angle_rad):
    """Rotate (u, v) from the cap-tile frame back to world XY."""
    c = cos(angle_rad)
    s = sin(angle_rad)
    return u * c - v * s, u * s + v * c


def _order_chain_indices(adjacency, component):
    local = {i: [n for n in adjacency[i] if n in component] for i in component}
    if any(len(local[i]) > 2 for i in component):
        return None
    ends = [i for i in component if len(local[i]) == 1]
    order = []
    if len(ends) == 2:
        previous = None
        current = ends[0]
        while current is not None:
            order.append(current)
            following = [i for i in local[current] if i != previous]
            previous, current = current, (following[0] if following else None)
        if len(order) != len(component):
            return None
        return order
    if len(ends) == 0 and component and all(len(local[i]) == 2 for i in component):
        start = min(component)
        previous = None
        current = start
        visited=set()
        while True:
            order.append(current)
            visited.add(current)
            following = sorted(i for i in local[current] if i != previous)
            next_vertex = following[0]
            previous, current = current, next_vertex
            if current == start:
                break
            if current in visited:
                return None
        if len(order) != len(component):
            return None
        order.append(start)
        return order
    return None


def validate_path_mesh_topology(path_obj, require_single_chain=True, tolerance=REGULATION_WELD_DISTANCE, allow_branches=False):
    """Validate the authored edge network before it is converted to chains."""
    if path_obj is None or path_obj.type != "MESH":
        raise ValueError("Путь должен быть Mesh-объектом")
    mesh = path_obj.data
    world = path_obj.matrix_world
    coordinates = [world @ vertex.co for vertex in mesh.vertices]
    adjacency = {index: [] for index in range(len(mesh.vertices))}
    edge_keys = {}
    zero_edges, duplicate_edges = [], []
    valid_edges = []
    for edge in mesh.edges:
        a, b = map(int, edge.vertices)
        if a == b or (coordinates[a] - coordinates[b]).length <= EPS:
            zero_edges.append(edge.index)
            continue
        key = tuple(sorted((a, b)))
        if key in edge_keys:
            duplicate_edges.append((edge_keys[key], edge.index))
            continue
        edge_keys[key] = edge.index
        adjacency[a].append((b, edge.index)); adjacency[b].append((a, edge.index))
        valid_edges.append((edge.index, a, b))

    used = {index for index, links in adjacency.items() if links}
    isolated = sorted(set(range(len(mesh.vertices))) - used)
    branches = sorted(index for index in used if len(adjacency[index]) > 2)
    invalid_degrees = sorted(index for index in used if len(adjacency[index]) not in {1, 2})

    components = []
    remaining = set(used)
    while remaining:
        start = min(remaining); stack = [start]; component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(neighbor for neighbor, _edge in adjacency[current] if neighbor not in component)
        remaining -= component
        components.append(component)

    spatial_tree = kdtree.KDTree(len(coordinates))
    for index, point in enumerate(coordinates):
        spatial_tree.insert(point, index)
    spatial_tree.balance()
    coincident_vertices = []
    for index, point in enumerate(coordinates):
        for _co, other, distance in spatial_tree.find_range(point, tolerance):
            if other <= index:
                continue
            coincident_vertices.append((index, other, distance))
            if len(coincident_vertices) >= 100:
                break
        if len(coincident_vertices) >= 100:
            break

    # A disconnected vertex lying on the interior of another edge is a
    # geometric T-junction even though mesh vertex degrees alone look valid.
    t_junctions = []
    candidate_checks=0
    for vertex_index, edge_index, a, b in vertex_edge_candidates(coordinates, valid_edges, tolerance):
        candidate_checks+=1
        point=coordinates[vertex_index]
        start, end = coordinates[a], coordinates[b]
        edge_vector = end - start
        length_squared = edge_vector.length_squared
        if length_squared <= EPS * EPS:
            continue
        factor = (point - start).dot(edge_vector) / length_squared
        if factor <= EPS or factor >= 1.0 - EPS:
            continue
        projected = start + edge_vector * factor
        distance = (point - projected).length
        if distance < tolerance:
            t_junctions.append((vertex_index, edge_index, factor, distance))
            if len(t_junctions) >= 100:
                break

    log_event(
        f"PATH_PREFLIGHT object={path_obj.name}; verts={len(mesh.vertices)}; edges={len(mesh.edges)}; "
        f"components={len(components)}; isolated={len(isolated)}; zero_edges={len(zero_edges)}; "
        f"duplicate_edges={len(duplicate_edges)}; branch_vertices={len(branches)}; "
        f"invalid_degrees={len(invalid_degrees)}; coincident_vertices={len(coincident_vertices)}; "
        f"t_junctions={len(t_junctions)}; vertex_edge_candidates={candidate_checks}; "
        f"status={'FAILED' if (isolated or zero_edges or duplicate_edges or ((not allow_branches) and invalid_degrees) or coincident_vertices or t_junctions or (require_single_chain and len(components) != 1)) else 'OK'}",
        "PATH_CHECK",
    )
    if isolated:
        log_event(f"PATH_ISOLATED_VERTICES indices={isolated[:40]}", "PATH_CHECK")
    if zero_edges:
        log_event(f"PATH_ZERO_EDGES indices={zero_edges[:40]}", "PATH_CHECK")
    if duplicate_edges:
        log_event(f"PATH_DUPLICATE_EDGES pairs={duplicate_edges[:40]}", "PATH_CHECK")
    if branches:
        log_event(f"PATH_BRANCH_VERTICES indices={branches[:40]}", "PATH_CHECK")
    if coincident_vertices:
        formatted = [(a, b, round(distance, 9)) for a, b, distance in coincident_vertices[:40]]
        log_event(f"PATH_COINCIDENT_VERTICES vertex_pairs_distance={formatted}", "PATH_CHECK")
    if t_junctions:
        formatted = [(v, e, round(f, 9), round(d, 9)) for v, e, f, d in t_junctions[:40]]
        log_event(f"PATH_T_JUNCTIONS vertex_edge_factor_distance={formatted}", "PATH_CHECK")

    problems = []
    if isolated: problems.append(f"изолированные вершины: {isolated[:10]}")
    if zero_edges: problems.append(f"нулевые рёбра: {zero_edges[:10]}")
    if duplicate_edges: problems.append(f"дубли рёбер: {duplicate_edges[:10]}")
    if invalid_degrees and not allow_branches: problems.append(f"ветвления/T-узлы в вершинах: {invalid_degrees[:10]}")
    if coincident_vertices: problems.append(f"несоединённые близкие вершины: {[(a, b) for a, b, _d in coincident_vertices[:10]]}")
    if t_junctions: problems.append(f"вершина на чужом ребре: {[(v, e) for v, e, _f, _d in t_junctions[:10]]}")
    if require_single_chain and len(components) != 1:
        problems.append(f"отдельных компонентов: {len(components)}")
    if problems:
        raise ValueError("Неправильный путь — " + "; ".join(problems))
    return len(components)


def extract_all_path_chains(path_obj):
    """Each connected unbranched edge chain as its own polyline (world space)."""
    mesh = path_obj.data
    if not mesh.edges:
        raise ValueError("Путь должен содержать хотя бы одно ребро")

    adjacency = {i: [] for i in range(len(mesh.vertices))}
    for edge in mesh.edges:
        a, b = edge.vertices
        if a == b:
            continue
        adjacency[a].append(b)
        adjacency[b].append(a)

    used = {i for i, n in adjacency.items() if n}
    if len(used) < 2:
        raise ValueError("Путь должен содержать хотя бы одно ребро")

    remaining = set(used)
    components = []
    while remaining:
        # Deterministic traversal: the first wall chain must not change with
        # Python's set iteration order.
        start = min(remaining)
        stack = [start]
        comp = set()
        while stack:
            v = stack.pop()
            if v in comp:
                continue
            comp.add(v)
            for n in adjacency[v]:
                if n not in comp:
                    stack.append(n)
        remaining -= comp
        components.append(comp)

    components.sort(key=lambda comp: min(comp))

    matrix = path_obj.matrix_world
    chains = []
    skipped_branch = 0
    skipped_invalid = 0
    for comp in components:
        if len(comp) < 2:
            continue
        order = _order_chain_indices(adjacency, comp)
        if order is None:
            local = {i: [n for n in adjacency[i] if n in comp] for i in comp}
            if any(len(local[i]) > 2 for i in comp):
                skipped_branch += 1
            else:
                skipped_invalid += 1
            continue
        points = [matrix @ mesh.vertices[i].co for i in order]
        cleaned = [points[0].copy()]
        for pt in points[1:]:
            if horizontal_length(cleaned[-1], pt) > EPS:
                cleaned.append(pt.copy())
            else:
                cleaned[-1] = pt.copy()
        if len(cleaned) < 2:
            skipped_invalid += 1
            continue
        if order[0] == order[-1] and (cleaned[0] - cleaned[-1]).length > EPS:
            cleaned.append(cleaned[0].copy())
        chains.append(cleaned)

    if not chains:
        raise ValueError("Не найдено ни одной валидной линии")
    log_event(
        f"PATH_ANALYSIS components={len(components)}; valid_chains={len(chains)}; "
        f"skipped_branch={skipped_branch}; skipped_invalid={skipped_invalid}",
        "CONTOUR",
    )
    for index, chain in enumerate(chains):
        length_xy = sum(horizontal_length(a, b) for a, b in zip(chain, chain[1:]))
        log_event(
            f"CHAIN {index}: points={len(chain)}; closed={is_path_closed(chain)}; "
            f"length_xy={length_xy:.9f}; z_min={min(p.z for p in chain):.9f}; "
            f"z_max={max(p.z for p in chain):.9f}",
            "CONTOUR",
        )
    return chains, skipped_branch, skipped_invalid


def ordered_path_vertices(path_obj):
    """First chain (wall mode uses one path)."""
    validate_path_mesh_topology(path_obj, require_single_chain=True)
    chains, _, _ = extract_all_path_chains(path_obj)
    return chains[0]


def horizontal_length(a, b):
    """XY-only distance so vertical slope does not stretch along-path UVs."""
    dx = b.x - a.x
    dy = b.y - a.y
    return (dx * dx + dy * dy) ** 0.5


def is_path_closed(points, tol=1.0e-4):
    """True if first and last points coincide in XYZ (tolerant)."""
    if len(points) < 2:
        return False
    return (points[0] - points[-1]).length <= tol


def mesh_path_is_closed(path_obj):
    """Fast topology-only hint for UI; exact validation remains in the builder."""
    if path_obj is None or path_obj.type != "MESH" or not path_obj.data.edges:
        return False
    degree = {index: 0 for index in range(len(path_obj.data.vertices))}
    used = set()
    for edge in path_obj.data.edges:
        a, b = edge.vertices
        degree[a] += 1
        degree[b] += 1
        used.update((a, b))
    return bool(used) and all(degree[index] == 2 for index in used)


def choose_safe_closed_start(
    points, tile_length, clearance=REGULATION_WELD_DISTANCE, spatial_distance=False,
):
    """Rotate a closed chain to keep module seams away from authored vertices."""
    if not is_path_closed(points) or len(points) < 4:
        return points, 0, float("inf")
    ring = [point.copy() for point in points[:-1]]
    best_points = points
    best_index = 0
    best_clearance = -1.0
    for start_index in range(len(ring)):
        rotated = ring[start_index:] + ring[:start_index]
        rotated.append(rotated[0].copy())
        distance = 0.0
        minimum = float("inf")
        for a, b in zip(rotated, rotated[1:]):
            distance += (b - a).length if spatial_distance else horizontal_length(a, b)
            if b is rotated[-1]:
                continue
            remainder = distance % tile_length
            gap = min(remainder, tile_length - remainder)
            # Exact coincidence is desirable because the authored vertex is
            # reused as the boundary. Every non-zero near miss participates
            # in the score; the start with the largest minimum gap wins.
            if gap > EPS:
                minimum = min(minimum, gap)
        if minimum > best_clearance:
            best_points = rotated
            best_index = start_index
            best_clearance = minimum
    if best_index:
        log_event(
            f"UV-старт автоматически перенесён на вершину контура {best_index}; "
            f"минимальный зазор до разреза {best_clearance:.6f} м"
        )
    return best_points, best_index, best_clearance



def resolve_story_heights(source_height, stories, first_story_height=0.0):
    """Return list of per-story heights (bottom → top).

    first_story_height <= 0 means «same as source plane».
    Remaining stories always use source plane height.
    """
    stories = max(1, int(stories))
    first = first_story_height if first_story_height > EPS else source_height
    if first <= EPS:
        first = source_height
    return [first] + [source_height] * (stories - 1)


def total_wall_height(source_height, stories, first_story_height=0.0):
    return sum(resolve_story_heights(source_height, stories, first_story_height))


def edge_module_cut_distances(path_points, distances, cuts, edge_z_values, height, ref_z):
    """Extra arc-length cuts where world-Z module planes cross a wall edge.

    edge_z_values[i] is the Z of the edge (top or bottom) at cuts[i].
    Wall modules are clipped to horizontal Z bands, so the edge gains vertices
    at those intersections. Cap/floor boundaries must share the same points
    for remove_doubles to weld cleanly.
    """
    if height <= EPS or len(cuts) < 2 or len(edge_z_values) != len(cuts):
        return []

    extra = []
    for i in range(len(cuts) - 1):
        d0, z0 = cuts[i], edge_z_values[i]
        d1, z1 = cuts[i + 1], edge_z_values[i + 1]
        if abs(z1 - z0) <= EPS:
            continue
        z_lo = min(z0, z1)
        z_hi = max(z0, z1)
        k_first = int(floor((z_lo - ref_z) / height)) + 1
        k_last = int(ceil((z_hi - ref_z) / height)) - 1
        for k in range(k_first, k_last + 1):
            z_plane = ref_z + k * height
            if z_lo + EPS < z_plane < z_hi - EPS:
                factor = (z_plane - z0) / (z1 - z0)
                extra.append(d0 + factor * (d1 - d0))
    return unique_sorted(extra)


def cuts_to_parameter_groups(path_points, cuts):
    """Convert arc-length cut distances into per-segment parameters [0..1]."""
    distances = [0.0]
    for start, end in zip(path_points, path_points[1:]):
        distances.append(distances[-1] + horizontal_length(start, end))
    groups = []
    for i in range(len(path_points) - 1):
        d0 = distances[i]
        d1 = distances[i + 1]
        length = d1 - d0
        params = [0.0, 1.0]
        if length > EPS:
            for cut in cuts:
                if d0 + EPS < cut < d1 - EPS:
                    params.append((cut - d0) / length)
        groups.append(unique_sorted(params))
    return groups


def source_quad_data(source_obj, module_size):
    """Read UV corners from a quad independently of its world orientation."""
    mesh = source_obj.data
    if len(mesh.polygons) != 1 or len(mesh.polygons[0].vertices) != 4:
        raise ValueError("The active source object must contain exactly one quad")

    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise ValueError("The source plane has no active UV map")

    poly = mesh.polygons[0]
    if poly.material_index >= len(mesh.materials) or mesh.materials[poly.material_index] is None:
        raise ValueError("Эталон нельзя использовать: на его полигоне не назначен материал")
    source_material = mesh.materials[poly.material_index]
    if not material_has_image_texture(source_material):
        raise ValueError(
            f"Эталон нельзя использовать: материал «{source_material.name}» "
            "не содержит узел Image Texture с выбранным изображением"
        )
    from ..core.material_intelligence import material_primary_image
    source_image, image_problem = material_primary_image(source_material)
    if source_image is None:
        raise ValueError("Невозможно определить текстуру материала эталона: " + image_problem)
    world = source_obj.matrix_world
    positions = [world @ mesh.vertices[index].co for index in poly.vertices]
    edge_lengths = [
        (positions[(index + 1) % 4] - positions[index]).length
        for index in range(4)
    ]
    size_tolerance = REGULATION_WELD_DISTANCE
    if any(abs(length - module_size) > size_tolerance for length in edge_lengths):
        values = ", ".join(f"{length:.4f}" for length in edge_lengths)
        raise ValueError(
            f"Эталон не соответствует модулю {module_size:.0f}×{module_size:.0f} м. "
            f"Длины сторон: {values} м. Допуск: {size_tolerance:.3f} м."
        )
    samples = []
    for loop_index in poly.loop_indices:
        vertex_index = mesh.loops[loop_index].vertex_index
        uv = uv_layer.data[loop_index].uv.copy()
        samples.append(uv)

    u_min = min(uv.x for uv in samples)
    u_max = max(uv.x for uv in samples)
    v_min = min(uv.y for uv in samples)
    v_max = max(uv.y for uv in samples)
    source_uv_area = 0.5 * sum(
        a.x * b.y - b.x * a.y
        for a, b in zip(samples, samples[1:] + samples[:1])
    )
    if source_uv_area < -EPS:
        log_event(
            "У эталона обнаружен flipped UV. При построении используется "
            "автоматически нормализованная ориентация UV.",
            "FIXED",
        )
    if u_max - u_min <= EPS or v_max - v_min <= EPS:
        raise ValueError("UV эталона имеет нулевую ширину или высоту")
    safe_min = 0.01171875
    safe_max = 1.0 - safe_min
    if max(abs(u_min - safe_min), abs(v_min - safe_min), abs(u_max - safe_max), abs(v_max - safe_max)) > 1.0e-5:
        raise ValueError(
            "UV эталона не соответствует безопасной зоне 1000 px/m: "
            f"ожидается [{safe_min:.8f}, {safe_max:.8f}]."
        )
    texture_width, texture_height = int(source_image.size[0]), int(source_image.size[1])
    safe_span = safe_max - safe_min
    density_u = texture_width * safe_span / module_size
    density_v = texture_height * safe_span / module_size
    if abs(density_u - 1000.0) > 0.5 or abs(density_v - 1000.0) > 0.5:
        raise ValueError(
            "Текстура эталона не соответствует 1000 px/m с учётом безопасных границ: "
            f"изображение={texture_width}×{texture_height}px; "
            f"получено={density_u:.3f}×{density_v:.3f} px/m; ожидается 1000×1000 px/m."
        )
    # Texture node settings can create apparent seams even when mesh UV is
    # continuous. Inspect lightweight metadata once per build; do NOT read
    # potentially multi-gigabyte image pixels or rewrite the user's material.
    try:
        image_nodes = [node for node in source_material.node_tree.nodes
                       if node.type == 'TEX_IMAGE' and node.image == source_image]
        for node in image_nodes:
            vector_socket = node.inputs.get('Vector')
            if vector_socket is not None and vector_socket.is_linked and vector_socket.links:
                upstream = vector_socket.links[0]
                source_coordinate = f'{upstream.from_node.type}.{upstream.from_socket.name}'
            else:
                source_coordinate = 'UNLINKED_DEFAULT'
            texture_extension = str(getattr(node, 'extension', 'UNKNOWN'))
            log_event(
                f'WALL_TEXTURE_INPUT image={source_image.name}; '
                f'extension={texture_extension}; vector_source={source_coordinate}; '
                'tile_border_seamless=NOT_VERIFIED', 'UV_CHECK')
            if texture_extension not in {'REPEAT'}:
                log_event(
                    f'WALL_TEXTURE_WRAP_WARNING image={source_image.name}; '
                    f'extension={texture_extension}; '
                    'UV module repeats may show discontinuities. '
                    'Check image seamlessness and coordinate nodes; material unchanged.',
                    'WARNING')
    except (AttributeError, IndexError, KeyError, TypeError, RuntimeError) as audit_exc:
        log_event(f'WALL_TEXTURE_INPUT_AUDIT_UNAVAILABLE reason={audit_exc}', 'UV_CHECK')
    log_event(
        f"UV_TEXEL_REFERENCE image={source_image.name}; size={texture_width}x{texture_height}; "
        f"safe=[{safe_min:.8f},{safe_max:.8f}]; "
        f"margin_px=({safe_min * texture_width:.3f},{safe_min * texture_height:.3f}); "
        f"density=({density_u:.3f},{density_v:.3f}) px/m",
        "UV_CHECK",
    )

    def corner(u, v):
        return min(samples, key=lambda uv: (uv.x - u) ** 2 + (uv.y - v) ** 2).copy()

    return {
        "height": float(module_size),
        "base_z": 0.0,
        "bottom_left_uv": corner(u_min, v_min),
        "bottom_right_uv": corner(u_max, v_min),
        "top_left_uv": corner(u_min, v_max),
        "top_right_uv": corner(u_max, v_max),
        "material_index": poly.material_index,
        "texture_width": texture_width,
        "texture_height": texture_height,
        "texel_density_u": density_u,
        "texel_density_v": density_v,
    }


def material_has_image_texture(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return False
    return any(
        node.type == "TEX_IMAGE" and node.image is not None
        for node in material.node_tree.nodes
    )


def lerp_uv(left, right, factor):
    return left.lerp(right, factor)


def sample_polyline(points, distances, distance):
    """Sample a world-space position at an arc-length distance."""
    if distance <= 0.0:
        return points[0].copy()
    if distance >= distances[-1]:
        return points[-1].copy()

    for i in range(len(points) - 1):
        if distance <= distances[i + 1] + EPS:
            span = distances[i + 1] - distances[i]
            factor = (distance - distances[i]) / span
            return points[i].lerp(points[i + 1], factor)
    return points[-1].copy()


def unique_sorted(values):
    result = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > EPS:
            result.append(value)
    return result


def cross_2d(a, b):
    return a.x * b.y - a.y * b.x


def offset_intersection(point, previous_direction, next_direction, previous_normal, next_normal):
    previous_offset = point + previous_normal
    next_offset = point + next_normal
    denominator = cross_2d(previous_direction, next_direction)
    if abs(denominator) <= EPS:
        # Aligned segments have one unique offset; an about-face has two
        # opposite offsets and cannot be resolved by averaging (zero width).
        if previous_direction.dot(next_direction) < -0.999999:
            raise ValueError('Offset: разворот пути на 180° схлопывает дополнительную стену')
        result = (previous_offset + next_offset) * 0.5
        # Wall thickness/placement is a plan (XY) operation. A sloped authored
        # path must keep its exact station Z; miter fallback may never average
        # neighbouring vertical levels into a lifted/dropped corner.
        result.z = point.z
        return result
    parameter = cross_2d(next_offset - previous_offset, next_direction) / denominator
    intersection = previous_offset + previous_direction * parameter
    intersection.z = point.z
    requested_offset = max(previous_normal.length, next_normal.length)
    if requested_offset > EPS and (intersection - point).length > requested_offset * MITER_LIMIT:
        raise ValueError(
            "Слишком острый угол внутренней стены: offset создаёт опасный длинный выступ. "
            "Уменьшите отступ или измените угол пути."
        )
    return intersection


def offset_polyline(points, distance, side):
    """Create an exact mitered 2D offset for an open or closed polyline."""
    if distance <= EPS:
        return [point.copy() for point in points]

    side_sign = 1.0 if side == "LEFT" else -1.0
    closed = (points[0] - points[-1]).length <= EPS
    working_points = points[:-1] if closed else points
    directions = []
    normals = []
    segment_pairs = list(zip(working_points, working_points[1:]))
    if closed:
        segment_pairs.append((working_points[-1], working_points[0]))

    for a, b in segment_pairs:
        direction = b - a
        direction.z = 0.0
        if direction.length <= EPS:
            raise ValueError('Offset: нулевой горизонтальный сегмент пути')
        direction.normalize()
        directions.append(direction)
        normals.append(Vector((-direction.y, direction.x, 0.0)) * side_sign * distance)

    if closed:
        result = []
        for index, point in enumerate(working_points):
            result.append(offset_intersection(
                point,
                directions[index - 1],
                directions[index],
                normals[index - 1],
                normals[index],
            ))
        result.append(result[0].copy())
        return result

    result = [working_points[0] + normals[0]]
    for index in range(1, len(working_points) - 1):
        result.append(offset_intersection(
            working_points[index],
            directions[index - 1],
            directions[index],
            normals[index - 1],
            normals[index],
        ))
    result.append(working_points[-1] + normals[-1])
    return result



def offset_edges_reference_polyline(points, distance, side):
    """Derive a second *edge chain* using edit_mesh_tools Offset Edges rails.

    Conformance target: ``mesh_offset_edges.get_directions`` default mode:
      geometry_mode=offset, depth_mode=angle, angle=0, follow_face=False,
      edge_rail=False, mirror_modifier=False, depth=0.
    Its fallback loop normal is +Z; a non-collinear 3D chain gets the actual
    Newell normal (not a forced XY normal).  At each point the width rail is
    the normalized average of incident (edge_direction CROSS loop_normal)
    tangents. The reference then applies the along-edge correction to its
    width vector; this is essential for miter geometry at sharp corners.
    Positive/negative Width maps to authored LEFT/RIGHT consistently.

    This operation never edits the source path. Z is obtained *by the 3D
    offset geometry itself*, not from independent secondary height controls.
    No cap geometry or vertices of the primary wall are involved.

    Reference: edit_mesh_tools/mesh_offset_edges.py, calc_loop_normal(),
    get_directions(), move_verts(); Blender Foundation, GPL-2.0-or-later.
    """
    from math import isfinite, sqrt

    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1],
                a[2]*b[0]-a[0]*b[2],
                a[0]*b[1]-a[1]*b[0])

    def normalize(a):
        length=sqrt(sum(v*v for v in a))
        if length<=1.0e-10 or not isfinite(length):
            raise ValueError('Offset Edges: вырожденная геометрия или разворот 180°')
        return tuple(v/length for v in a)

    if side not in ('LEFT','RIGHT'):
        raise ValueError('Offset Edges: неверная сторона')
    distance=float(distance)
    if not isfinite(distance) or distance<0:
        raise ValueError('Offset Edges: расстояние должно быть неотрицательным и конечным')
    if len(points)<2:
        raise ValueError('Offset Edges: минимум две вершины')
    if distance<=EPS:
        return [point.copy() for point in points]
    closed=(points[0]-points[-1]).length<=EPS
    vertices=list(points[:-1] if closed else points)
    if len(vertices)<(3 if closed else 2):
        raise ValueError('Offset Edges: вырожденная цепочка')
    coords=[(float(p.x),float(p.y),float(p.z)) for p in vertices]
    if any(not all(isfinite(v) for v in co) for co in coords):
        raise ValueError('Offset Edges: нечисловая координата')

    # Newell loop normal. The reference closes even half-loops implicitly.
    normal=[0.0,0.0,0.0]
    for prev,cur in zip(coords[-1:]+coords[:-1],coords):
        normal[0]+=(prev[1]-cur[1])*(prev[2]+cur[2])
        normal[1]+=(prev[2]-cur[2])*(prev[0]+cur[0])
        normal[2]+=(prev[0]-cur[0])*(prev[1]+cur[1])
    try:
        n=normalize(normal)
    except ValueError:
        n=(0.0,0.0,1.0)  # reference normal_fallback=Z_UP

    edges=[]
    count=len(vertices) if closed else len(vertices)-1
    for i in range(count):
        a,b=coords[i],coords[(i+1)%len(vertices)]
        if (b[0]-a[0])**2+(b[1]-a[1])**2<=EPS*EPS:
            raise ValueError('Offset Edges: нулевой горизонтальный сегмент')
        direction=normalize(tuple(b[j]-a[j] for j in range(3)))
        edges.append(direction)

    # Exact width correction from edit_mesh_tools.get_directions():
    # the *normalized* tangent average is ONLY the starting direction.
    # At bends the reference projects it away from a neighbouring edge,
    # then adds the along-edge correction so that Width has the same
    # perpendicular measurement on both adjacent edges.  Replacing this
    # with a simple unit bisector (the Pass 52 bug) is NOT Offset Edges.
    def dot(a,b):
        return sum(a[i]*b[i] for i in range(3))

    def subtract(a,b):
        return tuple(a[i]-b[i] for i in range(3))

    def scale(a,f):
        return tuple(f*v for v in a)

    width_directions=[]
    for i in range(len(vertices)):
        right=edges[i] if closed or i<len(vertices)-1 else edges[-1]
        left=edges[i-1] if closed or i>0 else edges[0]
        tan_right=normalize(cross(right,n))
        tan_left=normalize(cross(left,n))
        tan_avr=normalize(tuple(tan_right[j]+tan_left[j] for j in range(3)))
        plane=cross(n,tan_avr)
        pd_right=dot(right,plane)
        pd_left=dot(left,plane)
        if abs(pd_right)>1.0e-12 or abs(pd_left)>1.0e-12:
            if pd_right>pd_left:
                edge,p_dot=right,pd_right
            else:
                edge,p_dot=left,pd_left
            if abs(p_dot)<=1.0e-12:
                raise ValueError('Offset Edges: неопределённое сопряжение')
            tangent=normalize(subtract(tan_avr,scale(edge,dot(tan_avr,edge))))
            width=subtract(tangent,scale(edge,dot(tangent,plane)/p_dot))
        else:
            width=tan_avr
        if not all(isfinite(v) for v in width) or sqrt(dot(width,width))>1.0e5:
            raise ValueError('Offset Edges: чрезмерный вынос угла')
        width_directions.append(width)

    # The reference reverses 3D loops by normal/upward orientation.  Map its
    # signed Width consistently to the directed authored-side contract.
    xdiff=coords[1][0]-coords[0][0]
    ydiff=coords[1][1]-coords[0][1]
    physical_right=(ydiff,-xdiff)
    lateral=width_directions[0][0]*physical_right[0]+width_directions[0][1]*physical_right[1]
    if abs(lateral)<=1e-10:
        raise ValueError('Offset Edges: нельзя определить боковое направление пути')
    width_sign=(1.0 if lateral>0 else -1.0)*(1.0 if side=='RIGHT' else -1.0)
    result=[]
    for co,rail in zip(coords,width_directions):
        result.append(Vector(tuple(co[k]+distance*width_sign*rail[k] for k in range(3))))
    if closed:result.append(result[0].copy())
    return result


def offset_edges_style_polyline(points, distance, side):
    """Compatibility alias: use the authoritative Offset Edges rail kernel."""
    return offset_edges_reference_polyline(points, distance, side)


def create_guide_object(context, points, name, target_obj=None):
    closed = (points[0] - points[-1]).length <= EPS
    guide_points = points[:-1] if closed else points
    mesh = bpy.data.meshes.new(name + "_Mesh")
    edges = [(index, index + 1) for index in range(len(guide_points) - 1)]
    if closed:
        edges.append((len(guide_points) - 1, 0))
    mesh.from_pydata([tuple(point) for point in guide_points], edges, [])
    mesh.update()

    guide,_ = install_mesh_on_result(context,mesh,name,target_obj)
    if 'uvwall_pending_delete' in guide:
        del guide['uvwall_pending_delete']
    guide.hide_set(False)
    guide.hide_render = True
    guide.display_type = "WIRE"
    guide.hide_render = True
    guide.show_in_front = True
    return guide


def grid_cut_distances(path_points, tile_length, angle_rad=0.0):
    """Horizontal arc-length distances where the path crosses the (optionally rotated) tile grid."""
    cuts = []
    accumulated = 0.0

    for start, end in zip(path_points, path_points[1:]):
        length = horizontal_length(start, end)
        if length <= EPS:
            continue
        su, sv = rotate_xy(start.x, start.y, angle_rad)
        eu, ev = rotate_xy(end.x, end.y, angle_rad)

        for start_value, end_value in ((su, eu), (sv, ev)):
            delta = end_value - start_value
            if abs(delta) <= EPS:
                continue
            first_line = floor(min(start_value, end_value) / tile_length) + 1
            last_line = ceil(max(start_value, end_value) / tile_length)
            for grid_index in range(first_line, last_line):
                grid_value = grid_index * tile_length
                factor = (grid_value - start_value) / delta
                if EPS < factor < 1.0 - EPS:
                    cuts.append(accumulated + factor * length)

        accumulated += length
    return cuts


def wall_station_data(
    path_points,
    tile_length,
    include_cap_grid_cuts,
    extra_cut_distances=None,
    angle_rad=0.0,
):
    # Distances are horizontal (XY) so UV modules stay consistent on slopes.
    distances = [0.0]
    for start, end in zip(path_points, path_points[1:]):
        distances.append(distances[-1] + horizontal_length(start, end))
    total_length = distances[-1]

    # A module cut closer than the production weld clearance to an authored
    # corner would generate a microscopic trapezoid, then be dissolved during
    # weld. Snap the virtual seam to the exact corner instead of moving XY/Z.
    from .wall_seams import modular_stations
    cuts = modular_stations(distances, tile_length)
    if include_cap_grid_cuts:
        cuts.extend(grid_cut_distances(path_points, tile_length, angle_rad))
    if extra_cut_distances:
        cuts.extend(extra_cut_distances)
    return distances, unique_sorted(cuts)


def clean_polygon(points):
    """Remove near-duplicate consecutive vertices; drop closing duplicate."""
    if not points:
        return []
    result = [points[0].copy()]
    for point in points[1:]:
        if (point - result[-1]).length > EPS:
            result.append(point.copy())
    if len(result) > 1 and (result[0] - result[-1]).length <= EPS:
        result.pop()
    return result


def densify_polygon_on_rotated_grid(points, tile_length, angle_rad):
    """Insert vertices where polygon edges cross rotated UV grid lines.

    Always treats input as a closed ring (connects last→first).
    """
    if len(points) < 2 or tile_length <= EPS:
        return [p.copy() for p in points]
    work = list(points)
    if (work[0] - work[-1]).length <= EPS:
        work = work[:-1]
    n = len(work)
    if n < 2:
        return [p.copy() for p in points]
    result = []
    for i in range(n):
        a = work[i]
        b = work[(i + 1) % n]
        result.append(a.copy())
        su, sv = rotate_xy(a.x, a.y, angle_rad)
        eu, ev = rotate_xy(b.x, b.y, angle_rad)
        factors = []
        for s_val, e_val in ((su, eu), (sv, ev)):
            delta = e_val - s_val
            if abs(delta) <= EPS:
                continue
            lo, hi = (s_val, e_val) if s_val < e_val else (e_val, s_val)
            k0 = int(floor(lo / tile_length)) + 1
            k1 = int(ceil(hi / tile_length))
            for k in range(k0, k1):
                g = k * tile_length
                f = (g - s_val) / delta
                if EPS < f < 1.0 - EPS:
                    factors.append(f)
        for f in sorted(set(factors)):
            result.append(a.lerp(b, f))
    return result



