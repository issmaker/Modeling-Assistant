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
from ..core.performance import (
    PERFORMANCE_REGISTRY, StageTimer, cached_surface_call, clear_performance_caches, record_stage,
)
import json

_WARNING_DEDUP = {}


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

def cornice_uv_guidance(uv_mode, path_mode):
    """Short, mode-specific help shared by the panel and confirmation dialog."""
    uv_help = {
        "MAX_CLIPPED": (
            "3ds Max 2: подрезка только на отмеченных вершинах",
            "Вы сами решаете, какие точки пути являются углами",
            "Удалённые вершины автоматически исчезнут из списка",
        ),
        "MAX_REAL_WORLD": (
            "3ds Max: рекомендуется для плавных и частых изгибов",
            "Масштаб текстуры непрерывно считается в реальных метрах",
            "На резких углах рисунок может заметно деформироваться",
        ),
    }.get(uv_mode, ("Неизвестный UV-метод", "Проверьте настройки", ""))
    path_help = {
        "AUTO": "Авто углы: безопасный miter + локальный fallback на экстремальных поворотах",
        "AUTHORED": "Как нарисовано: опасный miter считается ошибкой",
        "ADAPTIVE": "Адаптивно: добавляются только необходимые виртуальные станции",
        "DENSE_LINEAR": "Добавить точки: прямые участки делятся заданным шагом",
    }.get(path_mode, "Геометрия пути используется строго как нарисована")
    return (*uv_help, path_help)


def detect_cornice_sharp_vertices(path_obj, threshold_degrees=45.0):
    """Return authored degree-2 path vertices whose XY turn is sharp."""
    if path_obj is None or path_obj.type != "MESH":
        return []
    mesh = path_obj.data
    adjacency = {index: [] for index in range(len(mesh.vertices))}
    for edge in mesh.edges:
        first, second = map(int, edge.vertices)
        if first == second:
            continue
        adjacency[first].append(second)
        adjacency[second].append(first)
    world = path_obj.matrix_world
    coordinates = [world @ vertex.co for vertex in mesh.vertices]
    sharp = []
    for vertex_index, neighbors in adjacency.items():
        if len(neighbors) != 2:
            continue
        center = coordinates[vertex_index]
        incoming = center - coordinates[neighbors[0]]
        outgoing = coordinates[neighbors[1]] - center
        incoming.z = 0.0
        outgoing.z = 0.0
        if incoming.length <= EPS or outgoing.length <= EPS:
            continue
        incoming.normalize()
        outgoing.normalize()
        turn = degrees(acos(max(-1.0, min(1.0, incoming.dot(outgoing)))))
        if turn >= threshold_degrees:
            sharp.append((vertex_index, turn))
    return sharp


def log_event(message, level="INFO"):
    """Write diagnostics to Blender Text + durable per-session log files."""
    if level == "WARNING":
        # Repeated preview/Build warnings remain represented by one structured
        # Problem record. Keep the forensic log readable during long sessions.
        now = perf_counter()
        previous = _WARNING_DEDUP.get(str(message))
        _WARNING_DEDUP[str(message)] = now
        if previous is not None and now - previous < 60.0:
            return
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message}\n"
    print("UV Wall:", line.rstrip())
    try:
        text_block = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
        text_block.write(line)
    except Exception:
        pass
    try:
        from ..core.support_logging import append_text, set_build_runtime
        append_text(line)
        if level == "STAGE" and str(message).startswith("STAGE "):
            set_build_runtime(stage=str(message)[6:].strip())
    except Exception:
        pass


def log_bulk(lines, level):
    """Append large geometry sections to Text and persistent support logs."""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = "".join(f"[{stamp}] [{level}] {line}\n" for line in lines)
        text_block = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
        text_block.write(payload)
        try:
            from ..core.support_logging import append_text
            append_text(payload)
        except Exception:
            pass
    except Exception as error:
        log_event(f"Ошибка пакетной записи журнала: {error}", "LOG_WARNING")


def _format_vector(vector):
    return f"({vector.x:.9f}, {vector.y:.9f}, {vector.z:.9f})"


def log_operation_snapshot(context, settings, source_obj, path_obj):
    """Record enough input data to reconstruct the user's path and settings."""
    from ..core import definitions as live_definitions
    schema_ok, schema_message = live_definitions.runtime_schema_status()
    try:
        log_event("=" * 72, "SNAPSHOT")
        from ..core.runtime_identity import log_identity
        runtime = log_identity(log_event)
        live_version = runtime['version']
        log_event(
            f"ADDON={live_version}; SCHEMA={live_definitions.CURRENT_SCHEMA_VERSION}; RNA={'OK' if schema_ok else 'INVALID'}; "
            f"Blender={bpy.app.version_string}; mode={settings.build_mode}; scene={context.scene.name}",
            "SNAPSHOT",
        )
        if not schema_ok:
            log_event(f"RNA_SCHEMA_STATUS {schema_message}", "ERROR")
        log_event(
            "SETTINGS "
            f"module={settings.tile_length:.9f}; standard={settings.module_standard}; "
            f"stories={settings.stories}; custom_first={settings.custom_first_story}; "
            f"first_height={settings.first_story_height:.9f}; direction={settings.story_direction}; "
            f"wall_normals={settings.wall_normal_mode}; surface_normals={settings.roof_normal}; "
            f"surface_method={'SMOOTH_LANDSCAPE' if settings.surface_method == 'SMOOTH_LEGACY' else settings.surface_method}; "
            f"inner={settings.create_inner}; "
            f"inset={settings.inset_distance:.9f}; inset_side={settings.inset_side}; alignment={getattr(settings,'wall_alignment','LEGACY')}; "
            f"top={settings.create_top_cap}; bottom={settings.create_bottom_cap}; "
            f"align_bottom={settings.align_bottom}; bottom_extend={settings.bottom_extend:.9f}; "
            f"cap_rotation={settings.cap_rotation:.6f}; "
            f"cornice_uv={getattr(settings, 'cornice_uv_mode', 'N/A')}; "
            f"cornice_path={getattr(settings, 'cornice_corner_mode', 'N/A')}; "
            f"cornice_flip={getattr(settings, 'cornice_flip_side', False)}; "
            f"linear_step={getattr(settings, 'cornice_linear_step', 0.0):.6f}",
            "SNAPSHOT",
        )
        for role, obj in (("SOURCE", source_obj), ("PATH", path_obj)):
            matrix_rows = "; ".join(
                "[" + ", ".join(f"{value:.9f}" for value in row) + "]"
                for row in obj.matrix_world
            )
            log_event(
                f"{role} name={obj.name}; data={obj.data.name}; "
                f"verts={len(obj.data.vertices)}; edges={len(obj.data.edges)}; "
                f"polygons={len(obj.data.polygons)}; dimensions={_format_vector(obj.dimensions)}; "
                f"matrix_world={matrix_rows}",
                "SNAPSHOT",
            )
        source_poly = source_obj.data.polygons[0]
        material = source_obj.data.materials[source_poly.material_index]
        images = [
            node.image.name for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        ]
        log_event(f"SOURCE_MATERIAL name={material.name}; images={images}", "SNAPSHOT")
        world = path_obj.matrix_world
        log_event("PATH_VERTICES_BEGIN", "CONTOUR")
        for vertex in path_obj.data.vertices:
            point = world @ vertex.co
            log_event(f"V {vertex.index}: {_format_vector(point)}", "CONTOUR")
        log_event("PATH_VERTICES_END", "CONTOUR")
        log_event("PATH_EDGES_BEGIN", "CONTOUR")
        for edge in path_obj.data.edges:
            log_event(f"E {edge.index}: {edge.vertices[0]} -> {edge.vertices[1]}", "CONTOUR")
        log_event("PATH_EDGES_END", "CONTOUR")
        return runtime
    except Exception as error:
        log_event(f"Не удалось полностью записать входной паспорт: {error}", "LOG_WARNING")
        return None


def cleanup_created_datablocks(objects_before, meshes_before, uvwall_only=False):
    """Rollback failed-build IDs without deleting unrelated handler output.

    When ``uvwall_only`` is enabled the rollback is deliberately fail-safe:
    only Objects already stamped as UV Wall output are removed, and only Meshes
    proven to belong to those removed Objects are purged afterwards.  An
    unrelated callback may create even an *orphan* Mesh while Build is running;
    treating every new orphan as ours would still be cross-tool data loss.

    Ambiguous unowned temporary data is allowed to leak rather than deleting
    foreign Blender IDs.  A dedicated temporary-ID registry can recover those
    rare leftovers without weakening this ownership boundary.
    """
    owned_meshes = set()
    for obj in list(bpy.data.objects):
        if obj in objects_before:
            continue
        if uvwall_only:
            try:
                owned = bool(
                    obj.get("uv_wall_mode") in {"WALL", "SURFACE", "CORNICE"}
                    or obj.get("uvwall_generated")
                    or obj.get("uvwall_child_surface")
                    or obj.get("uvwall_guide")
                )
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                owned = False
            if not owned:
                continue
        try:
            data = getattr(obj, "data", None)
            if data is not None:
                try:
                    owned_meshes.add(data)
                except TypeError:
                    pass
            bpy.data.objects.remove(obj, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass
    for mesh in list(bpy.data.meshes):
        if mesh in meshes_before or mesh.users != 0:
            continue
        if uvwall_only and mesh not in owned_meshes:
            continue
        try:
            bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError):
            pass


def _recover_identical_weld_groups(mesh, groups, exact):
    """Last-resort explicit BMesh targetmap for vertices ALREADY at same anchor.

    ``remove_doubles`` can leave separate indexes after joining many clipped
    face islands. This repair is strictly limited to vertices whose positions
    were reconciled in the protection-aware pass. It must not collapse a true
    1-2mm authored feature or relax the regulatory post-condition.
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        targetmap = {}
        for group in groups:
            candidates = sorted(set(group))
            if len(candidates) < 2:
                continue
            # A connected neighbour cluster can span more than 2mm. Only
            # vertices ALREADY coincident to within local numerical precision
            # may be target-mapped to one BMesh index.
            target = bm.verts[candidates[0]]
            for index in candidates[1:]:
                other = bm.verts[index]
                if (target.co - other.co).length <= exact * 2.0:
                    targetmap[other] = target
        if not targetmap:
            return 0
        old_count = len(mesh.vertices)
        bmesh.ops.weld_verts(bm, targetmap=targetmap)
        bm.to_mesh(mesh)
        mesh.update()
        return max(0, old_count - len(mesh.vertices))
    finally:
        bm.free()


def smart_regulation_weld(mesh, protected_points=(), distance=REGULATION_WELD_DISTANCE):
    """Weld near-duplicates while preserving semantic/protected source points.

    Every regulation pass is protection-aware.  Older builds performed one
    protected pass and then up to sixteen raw ``remove_doubles(dist=0.002)``
    passes, which could still move/collapse authored points afterwards.  This
    implementation recomputes near-neighbour clusters after every merge and
    re-applies the protected-anchor policy before any vertices are collapsed.
    """
    if len(mesh.vertices) < 2:
        return 0

    protected = [Vector(point) for point in protected_points]
    protected_tree = None
    if protected:
        protected_tree = kdtree.KDTree(len(protected))
        for index, point in enumerate(protected):
            protected_tree.insert(point, index)
        protected_tree.balance()

    exact = max(1.0e-7, cap_weld_tolerance(4.0))

    def near_groups():
        count = len(mesh.vertices)
        if count < 2:
            return []
        tree = kdtree.KDTree(count)
        for vertex in mesh.vertices:
            tree.insert(vertex.co, vertex.index)
        tree.balance()
        parent = list(range(count))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[max(a, b)] = min(a, b)

        for vertex in mesh.vertices:
            for _co, other_index, _found_distance in tree.find_range(vertex.co, distance):
                if other_index > vertex.index:
                    union(vertex.index, other_index)
        groups = {}
        for index in range(count):
            groups.setdefault(find(index), []).append(index)
        return [indices for indices in groups.values() if len(indices) > 1]

    total_groups = 0
    moved = 0
    maximum_move = 0.0
    iterative_passes = 0
    iterative_merged = 0

    for pass_index in range(16):
        groups = near_groups()
        if not groups:
            break
        total_groups += len(groups)
        before_count = len(mesh.vertices)

        for indices in groups:
            diameter = max(
                (mesh.vertices[a].co - mesh.vertices[b].co).length
                for position, a in enumerate(indices)
                for b in indices[position + 1:]
            )
            semantic = set()
            if protected_tree is not None:
                for index in indices:
                    _co, protected_index, found_distance = protected_tree.find(mesh.vertices[index].co)
                    if found_distance <= exact:
                        semantic.add(protected_index)
            distinct_semantic = []
            for protected_index in sorted(semantic):
                point = protected[protected_index]
                if not any((point - protected[other]).length <= exact for other in distinct_semantic):
                    distinct_semantic.append(protected_index)
            if len(distinct_semantic) > 1:
                positions = [tuple(protected[index]) for index in distinct_semantic]
                log_event(
                    f"WELD_AUTHORED_CONFLICT pass={pass_index + 1}; indices={indices}; "
                    f"protected={distinct_semantic}; positions={positions}; "
                    "action=BLOCKED_PRESERVE_AUTHORED",
                    "ERROR",
                )
                raise ValueError(
                    "Weld остановлен: две разные protected/authored-точки находятся ближе "
                    f"{distance:.3f} м. UV Wall не объединяет их молча; "
                    "разведите точки или соедините их намеренно. "
                    f"Protected indices={distinct_semantic}."
                )

            anchor = (
                protected[distinct_semantic[0]].copy()
                if distinct_semantic else mesh.vertices[min(indices)].co.copy()
            )
            try:
                _level = bpy.context.scene.uv_wall_settings.diagnostic_detail
            except (AttributeError, ReferenceError):
                _level = 'NORMAL'
            if _level == 'FORENSIC':
                log_event(
                    f"WELD_CLUSTER pass={pass_index + 1}; indices={indices}; "
                    f"diameter={diameter:.9f}; protected_indices={sorted(semantic)}; "
                    f"anchor={_format_vector(anchor)}",
                    "WELD",
                )
            for index in indices:
                delta = (mesh.vertices[index].co - anchor).length
                maximum_move = max(maximum_move, delta)
                mesh.vertices[index].co = anchor
                moved += 1

        mesh.update()
        # Vertices have already been moved to a policy-approved common anchor.
        # Collapse only exact/numerically-equal copies; never run a blind 2 mm
        # remove_doubles here, otherwise protected semantics could be lost again.
        weld_bm = bmesh.new()
        try:
            weld_bm.from_mesh(mesh)
            bmesh.ops.remove_doubles(
                weld_bm,
                verts=list(weld_bm.verts),
                dist=exact * 2.0,
            )
            bmesh.ops.dissolve_degenerate(
                weld_bm,
                edges=list(weld_bm.edges),
                dist=exact * 2.0,
            )
            weld_bm.to_mesh(mesh)
        finally:
            weld_bm.free()
        mesh.update()

        merged_this_pass = before_count - len(mesh.vertices)
        iterative_passes += 1
        iterative_merged += max(0, merged_this_pass)
        log_event(
            f"WELD_ITERATION pass={iterative_passes}; clusters={len(groups)}; "
            f"merged={merged_this_pass}; verts_after={len(mesh.vertices)}",
            "WELD",
        )
        if merged_this_pass <= 0:
            # ``remove_doubles`` may retain coincident indexes in complex
            # clipped islands. Explicit BMesh targetmap tries just the
            # identical, already-authorized positions, never a 2mm blind weld.
            recovered = _recover_identical_weld_groups(mesh, near_groups(), exact)
            log_event(
                f"WELD_EXACT_RECOVERY pass={iterative_passes}; merged={recovered}; "
                f"precision={exact * 2.0:.9f}", 'WELD')
            iterative_merged += recovered
            if recovered <= 0:
                break

    # Regulatory post-condition: no two separate vertices may remain closer
    # than the configured 2 mm weld radius.
    verify_tree = kdtree.KDTree(len(mesh.vertices))
    for vertex in mesh.vertices:
        verify_tree.insert(vertex.co, vertex.index)
    verify_tree.balance()
    remaining_pairs = set()
    for vertex in mesh.vertices:
        for _co, other_index, found_distance in verify_tree.find_range(vertex.co, distance):
            if other_index > vertex.index and found_distance < distance - exact:
                remaining_pairs.add((vertex.index, other_index))
    if remaining_pairs:
        sample = sorted(remaining_pairs)[:20]
        coordinates = [
            {"indices": (a, b),
             "a": tuple(round(float(v), 9) for v in mesh.vertices[a].co),
             "b": tuple(round(float(v), 9) for v in mesh.vertices[b].co),
             "distance": round((mesh.vertices[a].co - mesh.vertices[b].co).length, 9)}
            for a, b in sample
        ]
        log_event(f"WELD_RESIDUAL_FORENSIC {coordinates}", "ERROR")
        raise ValueError(
            f"Weld {distance:.3f} м не завершён безопасно: осталось близких пар "
            f"{len(remaining_pairs)}; координаты={coordinates}."
        )
    log_event(
        f"Semantic Weld {distance:.3f} м: обработано кластеров {total_groups}, "
        f"вершин перемещено {moved}, максимальное перемещение {maximum_move:.6f} м; "
        f"итераций={iterative_passes}, объединено={iterative_merged}; "
        "остаточных близких пар=0"
    )
    return total_groups


def weld_generated_roundoff_pairs(mesh, protected_points=(), distance=0.000020):
    """Weld only micro-coincident generated seams, in staging Mesh.

    20 micrometres is 0.02 texel at 1000 px/m, not a permissive 2 cm
    "fix". Exclude adjacent-edge and same-face pairs, protect different
    authored Path anchors, and abort before publication if topology changes.
    UV loop data and semantic face attributes remain Blender BMesh layers.
    """
    import math
    if not mesh.polygons or len(mesh.vertices) < 2:
        return 0
    if not (0 < distance <= 2.0e-5):
        raise ValueError('Micro-stitch tolerance exceeds the 20-micron safety cap')
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        before_faces = len(bm.faces)
        before_degenerate = sum(1 for f in bm.faces if f.calc_area() <= 1.0e-12)
        before_nonmanifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
        verts = list(bm.verts)
        anchor_positions = [Vector(p) for p in protected_points]
        # Query exactly the same Euclidean distance policy as the legacy
        # exhaustive O(mesh_vertices * authored_points) scan. Only the search
        # changes; source points, anchors, UV loops and weld geometry do not.
        from .anchor_neighbors import AnchorNeighbors
        _anchor_started = perf_counter()
        anchor_index = AnchorNeighbors(anchor_positions, distance)
        own = {v: anchor_index.matches(v.co) for v in verts}
        record_stage(log_event, 'WALL_ANCHOR_LOOKUP', _anchor_started)
        log_event(f'WALL_ANCHOR_LOOKUP indexed_anchors={len(anchor_positions)}; '
                  f'queried_vertices={len(verts)}; policy=EXACT_DISTANCE', 'PERFORMANCE')
        tree=kdtree.KDTree(len(verts))
        for i,v in enumerate(verts):tree.insert(v.co,i)
        tree.balance()
        busy=set();targets={};skipped=0
        for i,v in enumerate(verts):
            if i in busy:continue
            neighbors=sorted(((float(d),j) for _co,j,d in tree.find_range(v.co,distance)
                              if j>i and j not in busy), key=lambda p:(p[0],p[1]))
            for found_distance,j in neighbors:
                other=verts[j]
                if not v.is_valid or not other.is_valid:continue
                if any(e.other_vert(v) is other for e in v.link_edges):
                    skipped+=1;continue
                if any(f in other.link_faces for f in v.link_faces):
                    skipped+=1;continue
                # Two independent path points must not be coerced into one.
                referenced=own[v] | own[other]
                if (len(referenced)>1 and any(
                        (anchor_positions[a]-anchor_positions[b]).length > 1.0e-9
                        for a in referenced for b in referenced if a<b)):
                    skipped+=1;continue
                # Shared authored anchor is chosen as the surviving vertex;
                # otherwise retain the lower-index generated coordinate.
                if own[other] and not own[v]:
                    survivor,removed=other,v
                else:
                    survivor,removed=v,other
                targets[removed]=survivor
                busy.update((i,j))
                break
        if not targets:
            log_event(f'WALL_ROUNDOFF_STITCH pairs=0; skipped={skipped}; tolerance_m={distance:.8f}', 'TOPOLOGY')
            return 0
        bmesh.ops.weld_verts(bm,targetmap=targets)
        if len(bm.faces) != before_faces:
            raise ValueError('WALL_ROUNDOFF_STITCH refused: face count changed; previous wall retained')
        if sum(1 for e in bm.edges if len(e.link_faces)>2)>before_nonmanifold:
            raise ValueError('WALL_ROUNDOFF_STITCH refused: introduced non-manifold edges')
        if sum(1 for f in bm.faces if f.calc_area()<=1.0e-12) > before_degenerate:
            raise ValueError('WALL_ROUNDOFF_STITCH refused: new degenerate face')
        bm.to_mesh(mesh)
        mesh.update()
        log_event(f'WALL_ROUNDOFF_STITCH welded={len(targets)}; skipped={skipped}; '
                  f'tolerance_m={distance:.8f}; topology=VALIDATED; path_object=UNCHANGED', 'TOPOLOGY')
        return len(targets)
    finally:
        bm.free()


def merge_coincident_vertex_indices(mesh):
    """Make exactly coincident generated vertices share one mesh index.

    UVs are loop data and are therefore preserved independently on every face.
    This is deterministic and performs no additional geometric movement.
    """
    if not mesh.polygons:
        return 0
    uv_layer = mesh.uv_layers.active
    uv_name = uv_layer.name if uv_layer is not None else "UVMap"
    coordinates = []
    index_by_coordinate = {}
    remap = {}
    for vertex in mesh.vertices:
        key = tuple(float(value) for value in vertex.co)
        target = index_by_coordinate.get(key)
        if target is None:
            target = len(coordinates)
            index_by_coordinate[key] = target
            coordinates.append(vertex.co.copy())
        remap[vertex.index] = target

    merged = len(mesh.vertices) - len(coordinates)
    if merged <= 0:
        log_event("INDEX_MERGE merged=0", "TOPOLOGY")
        return 0

    faces = []
    face_uvs = []
    materials = []
    source_face_roles = face_role_codes(mesh)
    rebuilt_face_roles = []
    for polygon in mesh.polygons:
        ids = [remap[index] for index in polygon.vertices]
        uvs = [uv_layer.data[index].uv.copy() for index in polygon.loop_indices] if uv_layer else []
        clean_ids = []
        clean_uvs = []
        for position, index in enumerate(ids):
            if not clean_ids or index != clean_ids[-1]:
                clean_ids.append(index)
                if uv_layer:
                    clean_uvs.append(uvs[position])
        if len(clean_ids) > 2 and clean_ids[0] == clean_ids[-1]:
            clean_ids.pop()
            if clean_uvs:
                clean_uvs.pop()
        if len(set(clean_ids)) >= 3:
            faces.append(tuple(clean_ids))
            face_uvs.append(tuple(clean_uvs))
            materials.append(polygon.material_index)
            rebuilt_face_roles.append(source_face_roles[polygon.index])

    mesh.clear_geometry()
    mesh.from_pydata([tuple(coordinate) for coordinate in coordinates], [], faces)
    mesh.update()
    set_face_roles(mesh, rebuilt_face_roles)
    if mesh.uv_layers.active is None:
        rebuilt_uv = mesh.uv_layers.new(name=uv_name)
    else:
        rebuilt_uv = mesh.uv_layers.active
        rebuilt_uv.name = uv_name
    for polygon, uvs, material_index in zip(mesh.polygons, face_uvs, materials):
        polygon.material_index = material_index
        for loop_index, uv in zip(polygon.loop_indices, uvs):
            rebuilt_uv.data[loop_index].uv = uv
    mesh.update()
    log_event(
        f"INDEX_MERGE merged={merged}; verts={len(mesh.vertices)}; faces={len(mesh.polygons)}",
        "TOPOLOGY",
    )
    return merged


def sanitize_generated_topology(mesh):
    """Remove only topology that cannot represent a valid surface."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    before = (len(bm.verts), len(bm.edges), len(bm.faces))

    zero_edges = [edge for edge in bm.edges if edge.calc_length() <= 1.0e-9]
    if zero_edges:
        bmesh.ops.dissolve_degenerate(bm, edges=zero_edges, dist=1.0e-9)

    bm.verts.index_update()
    seen_faces = {}
    duplicate_faces = []
    for face in bm.faces:
        key = tuple(sorted(vertex.index for vertex in face.verts))
        if key in seen_faces:
            duplicate_faces.append(face)
        else:
            seen_faces[key] = face
    if duplicate_faces:
        bmesh.ops.delete(bm, geom=duplicate_faces, context="FACES_ONLY")

    degenerate_faces = [face for face in bm.faces if face.calc_area() <= 1.0e-12]
    if degenerate_faces:
        bmesh.ops.delete(bm, geom=degenerate_faces, context="FACES_ONLY")

    wire_edges = [edge for edge in bm.edges if edge.is_wire]
    if wire_edges:
        bmesh.ops.delete(bm, geom=wire_edges, context="EDGES")
    loose_vertices = [vertex for vertex in bm.verts if not vertex.link_edges and not vertex.link_faces]
    if loose_vertices:
        bmesh.ops.delete(bm, geom=loose_vertices, context="VERTS")

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    remaining_wire = [edge.index for edge in bm.edges if edge.is_wire]
    remaining_loose = [vertex.index for vertex in bm.verts if not vertex.link_edges and not vertex.link_faces]
    remaining_degenerate = [face.index for face in bm.faces if face.calc_area() <= 1.0e-12]
    remaining_multi_face = [edge.index for edge in bm.edges if len(edge.link_faces) > 2]
    after = (len(bm.verts), len(bm.edges), len(bm.faces))
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    log_event(
        f"TOPOLOGY_CLEANUP before={before}; after={after}; "
        f"removed_loose_verts={len(loose_vertices)}; removed_wire_edges={len(wire_edges)}; "
        f"removed_zero_edges={len(zero_edges)}; removed_degenerate_faces={len(degenerate_faces)}; "
        f"removed_duplicate_faces={len(duplicate_faces)}",
        "TOPOLOGY",
    )
    if remaining_wire or remaining_loose or remaining_degenerate or remaining_multi_face:
        raise ValueError(
            "Финальная проверка топологии не пройдена: остались изолированные "
            f"вершины={len(remaining_loose)}, рёбра={len(remaining_wire)}, "
            f"вырожденные грани={len(remaining_degenerate)}, "
            f"рёбра более чем с двумя гранями={len(remaining_multi_face)}."
        )


def _intersection_point_in_triangle_2d(point, triangle, tolerance=1.0e-9, strict=False):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    values = [orient(triangle[0], triangle[1], point), orient(triangle[1], triangle[2], point), orient(triangle[2], triangle[0], point)]
    if strict:
        return min(values) > tolerance or max(values) < -tolerance
    return not (min(values) < -tolerance and max(values) > tolerance)


def _intersection_segments_2d(a, b, c, d, tolerance=1.0e-9, strict=False):
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    ab_c, ab_d = orient(a, b, c), orient(a, b, d)
    cd_a, cd_b = orient(c, d, a), orient(c, d, b)
    boxes_overlap = (
        max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0])) + tolerance
        and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1])) + tolerance
    )
    if strict:
        return boxes_overlap and ab_c * ab_d < -(tolerance * tolerance) and cd_a * cd_b < -(tolerance * tolerance)
    return boxes_overlap and min(ab_c, ab_d) <= tolerance and max(ab_c, ab_d) >= -tolerance and min(cd_a, cd_b) <= tolerance and max(cd_a, cd_b) >= -tolerance


def _coplanar_triangles_overlap(first, second, normal, proper=True):
    axis = max(range(3), key=lambda index: abs(normal[index]))
    keep = [index for index in range(3) if index != axis]
    a = [tuple(point[index] for index in keep) for point in first]
    b = [tuple(point[index] for index in keep) for point in second]
    for edge_a in ((0, 1), (1, 2), (2, 0)):
        for edge_b in ((0, 1), (1, 2), (2, 0)):
            if _intersection_segments_2d(a[edge_a[0]], a[edge_a[1]], b[edge_b[0]], b[edge_b[1]], strict=proper):
                return True
    return any(_intersection_point_in_triangle_2d(point, b, strict=proper) for point in a) or any(
        _intersection_point_in_triangle_2d(point, a, strict=proper) for point in b
    )


def _segment_hits_triangle(start, end, triangle, tolerance=1.0e-8, proper=True):
    direction = end - start
    edge1 = triangle[1] - triangle[0]
    edge2 = triangle[2] - triangle[0]
    cross = direction.cross(edge2)
    determinant = edge1.dot(cross)
    if abs(determinant) <= tolerance:
        return False
    inverse = 1.0 / determinant
    delta = start - triangle[0]
    u = delta.dot(cross) * inverse
    if u < -tolerance or u > 1.0 + tolerance:
        return False
    q = delta.cross(edge1)
    v = direction.dot(q) * inverse
    if v < -tolerance or u + v > 1.0 + tolerance:
        return False
    if proper and min(u, v, 1.0-u-v) <= tolerance:
        # Intersection with a triangle boundary is contact. Stitched rims
        # contain collinear vertices; their triangulation diagonals must not
        # turn a shared endpoint into a penetrating edge.
        return False
    factor = edge2.dot(q) * inverse
    # A hit exactly at the source segment endpoint is a geometric contact,
    # not proof that one surface penetrates the other. True triangle
    # crossings always contain a hit inside at least one tested edge.
    return tolerance < factor < 1.0 - tolerance if proper else -tolerance <= factor <= 1.0 + tolerance


def _triangles_intersect_exact(first, second, tolerance=1.0e-8, proper=True):
    normal_a = (first[1] - first[0]).cross(first[2] - first[0])
    normal_b = (second[1] - second[0]).cross(second[2] - second[0])
    if normal_a.length_squared <= tolerance * tolerance or normal_b.length_squared <= tolerance * tolerance:
        return False
    normal_a.normalize(); normal_b.normalize()
    distances_b = [normal_a.dot(point - first[0]) for point in second]
    distances_a = [normal_b.dot(point - second[0]) for point in first]
    if all(value > tolerance for value in distances_b) or all(value < -tolerance for value in distances_b):
        return False
    if all(value > tolerance for value in distances_a) or all(value < -tolerance for value in distances_a):
        return False
    if max(abs(value) for value in distances_b + distances_a) <= tolerance:
        return _coplanar_triangles_overlap(first, second, normal_a, proper=proper)
    edges = ((0, 1), (1, 2), (2, 0))
    return any(_segment_hits_triangle(first[a], first[b], second, tolerance, proper=proper) for a, b in edges) or any(_segment_hits_triangle(second[a], second[b], first, tolerance, proper=proper) for a, b in edges)


def _polygons_intersect_exact(first_indices, second_indices, coordinates, proper=True):
    first = [coordinates[index] for index in first_indices]
    second = [coordinates[index] for index in second_indices]
    # Blender stores these positions as float32. Comparisons below one ULP
    # invent penetration at a shared rim, especially at survey coordinates.
    precision = max(1e-8, min(0.00025,
        max(abs(float(c)) for p in first+second for c in p) * 2.0**-22))
    # A fan from vertex zero leaves the polygon at concave stitched rims and
    # invents intersections. Use Blender's polygon tessellation instead.
    first_triangles = [tuple(first[v] if isinstance(v, int) else v for v in tri)
                       for tri in tessellate_polygon([first])]
    second_triangles = [tuple(second[v] if isinstance(v, int) else v for v in tri)
                        for tri in tessellate_polygon([second])]
    return any(_triangles_intersect_exact(a, b, tolerance=precision, proper=proper)
               for a in first_triangles for b in second_triangles)


class GeometryProblemError(ValueError):
    """Validation failure carrying stable, UI-safe problem locations.

    The geometry may be rolled back immediately after the exception, so the
    navigator must not retain Blender Mesh/Face RNA references. Records contain
    plain tuples/indices only and are safe to persist in diagnostics.
    """
    def __init__(self, message, problem_records=None):
        super().__init__(message)
        self.problem_records = list(problem_records or [])


def validate_mesh_self_intersections(mesh, max_logged_pairs=40):
    """Validate self-intersections *within generated output Mesh only*.

    SOURCE↔GENERATED collision is a distinct domain decision and is not part of
    this validator. Shared-edge face pairs receive an exact coplanar check:
    lawful adjoining faces are accepted, positive-area overlap is rejected.
    """
    if len(mesh.polygons) < 2:
        log_event("SELF_INTERSECTION faces=0; candidate_pairs=0; intersections=0; status=OK", "INTERSECTION_CHECK")
        return 0
    coordinates = [vertex.co.copy() for vertex in mesh.vertices]
    polygons = [tuple(polygon.vertices) for polygon in mesh.polygons]
    tree = BVHTree.FromPolygons(coordinates, polygons, all_triangles=False, epsilon=1.0e-7)
    candidates = tree.overlap(tree)
    intersections = []
    contacts = []
    seen = set()
    broad_phase = 0
    vertex_sets = [set(indices) for indices in polygons]
    # Distinguish an actual shared MESH edge from two shared vertex IDs that
    # belong to non-adjacent corners of a polygon. False adjacency used to skip
    # potentially genuine intersections in complex generated n-gons.
    polygon_edges = [set(frozenset((vs[k],vs[(k+1)%len(vs)]))
                         for k in range(len(vs))) for vs in polygons]
    tessellated_by_face = None  # Built lazily only for shared-edge n-gon pairs.
    uncertain_tessellation = 0
    shared_edge_tests = 0
    for first, second in candidates:
        if first == second:
            continue
        pair = (min(first, second), max(first, second))
        if pair in seen:
            continue
        seen.add(pair)
        shared_vertices = vertex_sets[pair[0]] & vertex_sets[pair[1]]
        # Faces meeting at just ONE point are not necessarily neighbours.
        # At sharp sweep corners two such faces may overlap beyond that point.
        # The old unconditional shared-vertex skip hid exactly this class.
        # An ordinary shared rim is expected contact, not a self-intersection.
        # However two NEW coplanar triangles may overlap on the SAME side of
        # their shared edge. A blanket shared-edge skip previously hid this
        # proven invalid case. Exact rational arithmetic on stored XYZ only;
        # no epsilon based hard blocks. N-gon rim handling stays conservative
        # until triangulation can preserve each original face ownership.
        if len(shared_vertices) >= 2 and polygon_edges[pair[0]].intersection(polygon_edges[pair[1]]):
            shared_edge_tests += 1
            if len(polygons[pair[0]]) == len(polygons[pair[1]]) == 3:
                # Fast conservative rejection: if the two third vertices are
                # safely on opposite sides of their common edge, the triangles
                # cannot have positive-area coplanar overlap. A generous sign
                # margin leaves near-degenerate cases to exact arithmetic.
                common = tuple(sorted(shared_vertices))
                opposite_a = next(k for k in polygons[pair[0]] if k not in shared_vertices)
                opposite_b = next(k for k in polygons[pair[1]] if k not in shared_vertices)
                edge = coordinates[common[1]] - coordinates[common[0]]
                na = edge.cross(coordinates[opposite_a] - coordinates[common[0]])
                nb = edge.cross(coordinates[opposite_b] - coordinates[common[0]])
                product = na.dot(nb)
                if product < -1.0e-8 * na.length * nb.length:
                    continue
                from issmaker_modeling_assistant.core.reliability.predicates import exact_coplanar_triangle_positive_overlap_3d
                face_a = tuple(coordinates[k] for k in polygons[pair[0]])
                face_b = tuple(coordinates[k] for k in polygons[pair[1]])
                if exact_coplanar_triangle_positive_overlap_3d(face_a, face_b):
                    intersections.append(pair)
            else:
                # A shared EDGE does not make all n-gon pairs safe. Convert
                # Blender's polygon-owned tessellation to triangles and prove
                # any positive-area overlap using the same exact RF1 predicate.
                # The computed triangulation is a read-only derived Mesh cache.
                if tessellated_by_face is None:
                    mesh.calc_loop_triangles()
                    tessellated_by_face = {}
                    for tri in mesh.loop_triangles:
                        tessellated_by_face.setdefault(int(tri.polygon_index), []).append(
                            tuple(tuple(coordinates[int(k)]) for k in tri.vertices))
                from issmaker_modeling_assistant.core.reliability.predicates import exact_coplanar_face_triangle_overlap_3d
                first_tris=tessellated_by_face.get(pair[0], ())
                second_tris=tessellated_by_face.get(pair[1], ())
                if not first_tris or not second_tris:
                    uncertain_tessellation += 1
                    log_event(f'SELF_INTERSECTION_TESSELLATION_UNCERTAIN pair={pair}', 'INTERSECTION_CHECK')
                elif exact_coplanar_face_triangle_overlap_3d(first_tris, second_tris) is not None:
                    intersections.append(pair)
            continue
        broad_phase += 1
        if _polygons_intersect_exact(polygons[pair[0]], polygons[pair[1]], coordinates, proper=True):
            intersections.append(pair)
        elif _polygons_intersect_exact(polygons[pair[0]], polygons[pair[1]], coordinates, proper=False):
            contacts.append(pair)
    sample = intersections[:max_logged_pairs]
    log_event(
        f"SELF_INTERSECTION faces={len(polygons)}; bvh_pairs={len(seen)}; "
        f"broad_phase_unrelated={broad_phase}; shared_edge_tests={shared_edge_tests}; "
        f"tessellation_uncertain={uncertain_tessellation}; exact_tests={broad_phase}; "
        f"contacts={len(contacts)}; contact_sample={contacts[:max_logged_pairs]}; "
        f"intersections={len(intersections)}; sample={sample}; "
        f"status={'FAILED' if intersections else 'OK'}",
        "INTERSECTION_CHECK",
    )
    if intersections:
        problem_records = []
        centers = []
        radius = 0.5
        for first, second in intersections[:max_logged_pairs]:
            first_points = [coordinates[index] for index in polygons[first]]
            second_points = [coordinates[index] for index in polygons[second]]
            points = first_points + second_points
            if points:
                center = tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))
                pair_radius = max((point - Vector(center)).length for point in points)
            else:
                center, pair_radius = (0.0, 0.0, 0.0), 0.5
            centers.append(center)
            radius = max(radius, max(0.25, float(pair_radius)))
        focus = tuple(sum(point[axis] for point in centers) / len(centers) for axis in range(3)) if centers else (0.0, 0.0, 0.0)
        problem_records.append({
            "subsystem": "CORNICE",
            "severity": "ERROR",
            "category": "GEOMETRY_BLOCKED",
            "code": "CORNICE_SELF_INTERSECTION",
            "title": "Самопересечение карниза",
            "description": f"Найдено пар: {len(intersections)} · первые пары: {sample[:10]}",
            "world_position": focus,
            "radius": radius,
            "element_kind": "FACE_PAIR_GROUP",
            "element_indices": ";".join(f"{a},{b}" for a,b in sample[:10]),
            "suggested_action": "Focus · Mark Sharp · Flip Side · уменьшить профиль",
            "occurrence_count": len(intersections),
        })
        raise GeometryProblemError(
            f"Найдены самопересечения карниза: {len(intersections)} пар граней. "
            f"Первые пары: {sample[:10]}",
            problem_records=problem_records,
        )
    return 0


def finalize_surface_normals_and_shading(mesh, expected_sign=None, sharp_angle_deg=35.0):
    """Finish a mesh known to contain only a landscape/floor surface."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    flipped = 0
    for face in bm.faces:
        face.smooth = True
        if expected_sign is not None and face.normal.z * expected_sign < 0.0:
            face.normal_flip()
            flipped += 1

    bm.normal_update()
    sharp_cosine = cos(radians(sharp_angle_deg))
    sharp_edges = 0
    for edge in bm.edges:
        linked = list(edge.link_faces)
        smooth = len(linked) == 2 and linked[0].normal.dot(linked[1].normal) >= sharp_cosine
        edge.smooth = smooth
        if len(linked) == 2 and not smooth:
            sharp_edges += 1

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    slopes = []
    nonplanar_faces = 0
    max_planar_deviation = 0.0
    for polygon in mesh.polygons:
        points = [mesh.vertices[index].co for index in polygon.vertices]
        nz = max(0.0, min(1.0, abs(polygon.normal.z)))
        slopes.append(degrees(acos(nz)))
        if len(points) > 3 and polygon.normal.length_squared > EPS * EPS:
            origin = points[0]
            deviation = max(abs((point - origin).dot(polygon.normal)) for point in points[1:])
            max_planar_deviation = max(max_planar_deviation, deviation)
            if deviation > REGULATION_WELD_DISTANCE:
                nonplanar_faces += 1
    log_event(
        f"SURFACE_FINISH faces={len(slopes)}; flipped_normals={flipped}; "
        f"smooth_shading=True; sharp_angle_deg={sharp_angle_deg:.3f}; "
        f"sharp_internal_edges={sharp_edges}; nonplanar_faces={nonplanar_faces}; "
        f"max_planar_deviation={max_planar_deviation:.9f}; "
        f"max_slope_deg={(max(slopes) if slopes else 0.0):.6f}",
        "NORMAL_CHECK",
    )
    return flipped


def preserve_mixed_wall_shading(mesh):
    """Keep generated architectural faces flat while preserving semantic roles."""
    for polygon in mesh.polygons:
        polygon.use_smooth = False
    for edge in mesh.edges:
        edge.use_edge_sharp = False
    mesh.update()
    roles = role_counts(mesh)
    unknown = roles.get("UNKNOWN", 0)
    log_event(
        f"MIXED_SURFACE_FINISH faces={len(mesh.polygons)}; smooth_shading=False; "
        f"classification={'SEMANTIC' if unknown == 0 else 'PARTIAL'}; roles={roles}; "
        f"unknown_roles={unknown}",
        "NORMAL_CHECK",
    )


def deduplicate_edge_insertions(candidates, edge_length, tolerance):
    """Resolve coincident vertex candidates at one station on an edge.

    Two distinct cap-grid vertices can project to the exact same wall-edge
    station (seen in the 2026-10-13 field log, factor=0.946854642 twice).
    Inserting both creates a microscopic backtrack / edge counted twice by
    one polygon. Prefer nearest-to-edge, then stable candidate ID. Never
    discard *distinct* stations or bypass the manifold safety check.
    """
    if not candidates:
        return []
    threshold = max(1.e-12, float(tolerance) / max(float(edge_length), 1.e-12))
    ordered = sorted(candidates, key=lambda item: (float(item[0]), float(item[2]), int(item[1])))
    groups = []
    for factor, index, distance in ordered:
        if groups and abs(float(factor) - groups[-1][-1][0]) <= threshold:
            groups[-1].append((float(factor), int(index), float(distance)))
        else:
            groups.append([(float(factor), int(index), float(distance))])
    return [min(group, key=lambda entry:(entry[2],entry[1])) for group in groups]


def stitch_t_junctions(
    mesh,
    tolerance=REGULATION_WELD_DISTANCE,
    target_face_indices=None,
    candidate_vertex_ids=None,
    log_label="T_STITCH",
):
    """Insert existing vertices into polygon edges without cutting across faces.

    Optional semantic filters let callers stitch only one geometry domain into
    another.  This is critical for wall/cap contact: a cap-grid vertex is
    inserted into the touching wall edge only, producing a collinear boundary
    vertex on that face instead of a full-height wall station.
    """
    if len(mesh.vertices) < 3 or not mesh.polygons:
        return 0
    target_face_set = None if target_face_indices is None else {int(index) for index in target_face_indices}
    candidate_vertex_set = None if candidate_vertex_ids is None else {int(index) for index in candidate_vertex_ids}
    coordinates = [vertex.co.copy() for vertex in mesh.vertices]
    tree = kdtree.KDTree(len(coordinates))
    for index, coordinate in enumerate(coordinates):
        tree.insert(coordinate, index)
    tree.balance()
    uv_layer = mesh.uv_layers.active
    uv_name = uv_layer.name if uv_layer is not None else "UVMap"
    new_faces = []
    new_face_uvs = []
    material_indices = []
    source_face_roles = face_role_codes(mesh)
    new_face_roles = []
    inserted_total = 0

    for polygon in mesh.polygons:
        old_ids = list(polygon.vertices)
        old_id_set = set(old_ids)
        old_uvs = [uv_layer.data[index].uv.copy() for index in polygon.loop_indices]
        if target_face_set is not None and polygon.index not in target_face_set:
            new_faces.append(tuple(old_ids))
            new_face_uvs.append(tuple(old_uvs))
            material_indices.append(polygon.material_index)
            new_face_roles.append(source_face_roles[polygon.index])
            continue
        expanded_ids = []
        expanded_uvs = []
        for edge_index, start_id in enumerate(old_ids):
            end_id = old_ids[(edge_index + 1) % len(old_ids)]
            start = coordinates[start_id]
            end = coordinates[end_id]
            start_uv = old_uvs[edge_index]
            end_uv = old_uvs[(edge_index + 1) % len(old_uvs)]
            expanded_ids.append(start_id)
            expanded_uvs.append(start_uv)
            direction = end - start
            length = direction.length
            if length <= tolerance * 2.0:
                continue
            midpoint = (start + end) * 0.5
            candidates = []
            for coordinate, candidate_id, _distance in tree.find_range(midpoint, length * 0.5 + tolerance):
                if candidate_id in old_id_set:
                    continue
                if candidate_vertex_set is not None and candidate_id not in candidate_vertex_set:
                    continue
                factor = (coordinate - start).dot(direction) / (length * length)
                if factor <= tolerance / length or factor >= 1.0 - tolerance / length:
                    continue
                projected = start.lerp(end, factor)
                line_distance = (coordinate - projected).length
                if line_distance <= tolerance:
                    candidates.append((factor, candidate_id, line_distance))
            candidates = deduplicate_edge_insertions(candidates, length, tolerance)
            used_candidates = set()
            for factor, candidate_id, line_distance in candidates:
                if candidate_id in used_candidates:
                    continue
                used_candidates.add(candidate_id)
                expanded_ids.append(candidate_id)
                expanded_uvs.append(start_uv.lerp(end_uv, factor))
                inserted_total += 1
                log_event(
                    f"{log_label} face={polygon.index}; edge={start_id}->{end_id}; "
                    f"insert_vertex={candidate_id}; factor={factor:.9f}; "
                    f"distance_to_edge={line_distance:.9f}",
                    "TOPOLOGY",
                )
        cleaned_ids = []
        cleaned_uvs = []
        for vertex_id, uv in zip(expanded_ids, expanded_uvs):
            if not cleaned_ids or vertex_id != cleaned_ids[-1]:
                cleaned_ids.append(vertex_id)
                cleaned_uvs.append(uv)
        if len(cleaned_ids) > 2 and cleaned_ids[0] == cleaned_ids[-1]:
            cleaned_ids.pop()
            cleaned_uvs.pop()
        if len(set(cleaned_ids)) >= 3:
            new_faces.append(tuple(cleaned_ids))
            new_face_uvs.append(tuple(cleaned_uvs))
            material_indices.append(polygon.material_index)
            new_face_roles.append(source_face_roles[polygon.index])

    if not inserted_total:
        log_event(f"{log_label} inserted=0; tolerance={tolerance:.9f}", "TOPOLOGY")
        return 0

    # Validate the proposed rebuilt topology before touching the real Mesh.
    # If a candidate insertion would create an edge shared by 3+ faces, fail
    # transactionally instead of mutating the mesh and discovering corruption
    # only afterwards.
    edge_uses = {}
    for face_index, face_ids in enumerate(new_faces):
        for index, a in enumerate(face_ids):
            b = face_ids[(index + 1) % len(face_ids)]
            key = (a, b) if a < b else (b, a)
            edge_uses.setdefault(key, []).append(face_index)
    manifold_conflicts = [(edge, faces) for edge, faces in edge_uses.items() if len(faces) > 2]
    if manifold_conflicts:
        sample = manifold_conflicts[:20]
        log_event(
            f"{log_label}_MANIFOLD_CONFLICT tolerance={tolerance:.9f}; "
            f"conflicts={len(manifold_conflicts)}; sample={sample}; action=ABORT_BEFORE_MUTATION",
            "TOPOLOGY",
        )
        raise ValueError(
            "Topology stitch остановлен до изменения Mesh: найдено "
            f"{len(manifold_conflicts)} потенциальных рёбер с 3+ гранями. "
            "Это означает неоднозначный T-junction; исходная геометрия результата не повреждена."
        )

    mesh.clear_geometry()
    mesh.from_pydata([tuple(coordinate) for coordinate in coordinates], [], new_faces)
    mesh.update()
    set_face_roles(mesh, new_face_roles)
    if mesh.uv_layers.active is None:
        rebuilt_uv = mesh.uv_layers.new(name=uv_name)
    else:
        rebuilt_uv = mesh.uv_layers.active
        rebuilt_uv.name = uv_name
    for polygon, uvs, material_index in zip(mesh.polygons, new_face_uvs, material_indices):
        polygon.material_index = material_index
        for loop_index, uv in zip(polygon.loop_indices, uvs):
            rebuilt_uv.data[loop_index].uv = uv
    mesh.update()
    log_event(f"{log_label} inserted={inserted_total}; rebuilt_faces={len(new_faces)}; tolerance={tolerance:.9f}", "TOPOLOGY")
    return inserted_total


def validate_uv_safe_range(mesh, source=None, safe_min=0.01171875, safe_max=0.98828125):
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise ValueError("У результата отсутствует активная UV-карта")
    for loop_index, loop in enumerate(uv_layer.data):
        u, v = loop.uv
        if u < safe_min - 1.0e-6 or u > safe_max + 1.0e-6 or v < safe_min - 1.0e-6 or v > safe_max + 1.0e-6:
            raise ValueError(
                f"UV-проверка не пройдена: loop {loop_index}, UV=({u:.7f}, {v:.7f}) "
                f"вне безопасной зоны [{safe_min:.8f}, {safe_max:.8f}]."
            )
    if source is not None:
        density_u = float(source.get("texel_density_u", 0.0))
        density_v = float(source.get("texel_density_v", 0.0))
        if abs(density_u - 1000.0) > 0.5 or abs(density_v - 1000.0) > 0.5:
            raise ValueError(
                f"Итоговая UV-проверка плотности не пройдена: "
                f"{density_u:.3f}×{density_v:.3f} px/m."
            )
        log_event(
            f"UV_TEXEL_RESULT loops={len(uv_layer.data)}; "
            f"safe=[{safe_min:.8f},{safe_max:.8f}]; "
            f"density=({density_u:.3f},{density_v:.3f}) px/m; status=OK",
            "UV_CHECK",
        )


def validate_uv_faces_not_folded(mesh, area_epsilon=1.0e-12):
    """Reject zero-area and internally folded UV polygons before commit."""
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise ValueError("У результата отсутствует активная UV-карта")
    checked_edges = 0
    for polygon in mesh.polygons:
        uvs = [uv_layer.data[index].uv.copy() for index in polygon.loop_indices]
        if len(uvs) < 3:
            raise ValueError(f"UV-полигон {polygon.index} содержит меньше трёх точек")
        signed_double_area = sum(
            first.x * second.y - second.x * first.y
            for first, second in zip(uvs, uvs[1:] + uvs[:1])
        )
        if abs(signed_double_area) <= area_epsilon:
            raise ValueError(
                f"UV-проверка не пройдена: полигон {polygon.index} имеет нулевую UV-площадь"
            )
        edge_count = len(uvs)
        for first_index in range(edge_count):
            a, b = uvs[first_index], uvs[(first_index + 1) % edge_count]
            for second_index in range(first_index + 1, edge_count):
                if second_index == first_index + 1 or (
                    first_index == 0 and second_index == edge_count - 1
                ):
                    continue
                c, d = uvs[second_index], uvs[(second_index + 1) % edge_count]
                checked_edges += 1
                from .uv_robustness import strict_uv_crossing
                if strict_uv_crossing(a, b, c, d, base_tolerance=area_epsilon):
                    # A visually corrupted corner can otherwise look like an
                    # inexplicable BUILD_CANCELLED. Keep bounded reproducible
                    # face XYZ and UV in the diagnostic log before rollback.
                    try:
                        codes=face_role_codes(mesh)
                        role_code=codes[polygon.index]
                    except (AttributeError, IndexError, KeyError, RuntimeError):
                        role_code='UNKNOWN'
                    log_event(
                        f'UV_FOLDED_FACE index={polygon.index}; role={role_code}; '
                        f'loop_count={len(uvs)}; '
                        f'xyz={[(round(float(mesh.vertices[k].co.x),7),round(float(mesh.vertices[k].co.y),7),round(float(mesh.vertices[k].co.z),7)) for k in polygon.vertices[:80]]}; '
                        f'uv={[(round(float(v.x),8),round(float(v.y),8)) for v in uvs[:80]]}',
                        'ERROR',
                    )
                    raise ValueError(
                        f"UV-проверка не пройдена: полигон {polygon.index} сложен сам на себя "
                        "(обнаружен внутренний flip UV)"
                    )
    log_event(
        f"UV_ORIENTATION faces={len(mesh.polygons)}; edge_pairs={checked_edges}; "
        "zero_area=0; folded=0; status=OK",
        "UV_CHECK",
    )



def repair_uv_winding_islands(mesh, expected_positive=True, area_epsilon=1.0e-12, uv_tolerance=1.0e-7):
    """Repair mirrored UV islands without changing geometry or island continuity.

    UV continuity across a shared mesh edge defines an island.  An entire
    consistently mirrored island is mirrored around its own U bounds.  A mixed
    winding *inside* one continuous island is unsafe to guess and therefore
    blocks publication rather than silently corrupting the unwrap.
    """
    uv_layer=mesh.uv_layers.active
    if uv_layer is None:
        raise ValueError('У результата отсутствует активная UV-карта')
    polygons=list(mesh.polygons)
    if not polygons:
        return {"repaired_islands":0,"repaired_faces":0,"mixed_islands":0}

    areas={}
    edge_uses={}
    for poly in polygons:
        loops=list(poly.loop_indices); verts=list(poly.vertices)
        coords=[uv_layer.data[i].uv.copy() for i in loops]
        area=.5*sum(a.x*b.y-b.x*a.y for a,b in zip(coords,coords[1:]+coords[:1]))
        areas[poly.index]=float(area)
        n=len(loops)
        for i in range(n):
            va=int(verts[i]); vb=int(verts[(i+1)%n])
            key=(va,vb) if va<vb else (vb,va)
            edge_uses.setdefault(key,[]).append((poly.index,{va:coords[i],vb:coords[(i+1)%n]}))

    adjacency={poly.index:set() for poly in polygons}
    tol2=uv_tolerance*uv_tolerance
    for uses in edge_uses.values():
        if len(uses)!=2:
            continue
        (pa,ma),(pb,mb)=uses
        shared=set(ma).intersection(mb)
        if len(shared)!=2:
            continue
        continuous=True
        for vid in shared:
            d=ma[vid]-mb[vid]
            if d.length_squared>tol2:
                continuous=False;break
        if continuous:
            adjacency[pa].add(pb);adjacency[pb].add(pa)

    unseen=set(adjacency); repaired_islands=0; repaired_faces=0; mixed=0
    while unseen:
        seed=unseen.pop(); stack=[seed]; island=[seed]
        while stack:
            current=stack.pop()
            for other in adjacency[current]:
                if other in unseen:
                    unseen.remove(other);stack.append(other);island.append(other)
        signs=set()
        for idx in island:
            area=areas.get(idx,0.0)
            if abs(area)>area_epsilon:
                signs.add(area>0.0)
        if len(signs)>1:
            # Mixed winding usually means geometry was authored with opposite
            # face direction after UVs were already continuous (the 1.9.0
            # RIGHT/variable-Z case).  Do not publish a mirrored island and do
            # not weaken validation.  Split UV continuity *only* along the sign
            # boundary by mirroring each wrong-sign component in place.  This
            # intentionally creates a UV seam at that boundary; final winding
            # validation below still has to pass for every polygon.
            mixed+=1
            wrong={idx for idx in island if abs(areas.get(idx,0.0))>area_epsilon and ((areas[idx]>0.0) != bool(expected_positive))}
            repaired_here=0
            while wrong:
                seed_wrong=wrong.pop(); component=[seed_wrong]; stack_wrong=[seed_wrong]
                while stack_wrong:
                    current=stack_wrong.pop()
                    for other in adjacency[current]:
                        if other in wrong:
                            wrong.remove(other); stack_wrong.append(other); component.append(other)
                loop_indices=[]
                for idx in component:
                    loop_indices.extend(mesh.polygons[idx].loop_indices)
                if not loop_indices:
                    continue
                us=[float(uv_layer.data[i].uv.x) for i in loop_indices]
                pivot=min(us)+max(us)
                for i in loop_indices:
                    uv=uv_layer.data[i].uv
                    uv.x=pivot-float(uv.x)
                repaired_islands+=1; repaired_faces+=len(component); repaired_here+=len(component)
            log_event(
                f'UV_MIXED_ISLAND_SPLIT source_island={min(island)}; repaired_faces={repaired_here}; '
                'policy=SIGN_BOUNDARY_SEAM; validator=REQUIRED', 'AUTO_FIX')
            continue
        if not signs or (expected_positive in signs):
            continue
        loop_indices=[]
        for idx in island:
            loop_indices.extend(mesh.polygons[idx].loop_indices)
        if not loop_indices:
            continue
        us=[float(uv_layer.data[i].uv.x) for i in loop_indices]
        pivot=min(us)+max(us)
        for i in loop_indices:
            uv=uv_layer.data[i].uv
            uv.x=pivot-float(uv.x)
        repaired_islands+=1;repaired_faces+=len(island)
    if repaired_islands:
        log_event(
            f'UV_AUTO_ORIENT repaired_islands={repaired_islands}; repaired_faces={repaired_faces}; '
            f'expected={"POSITIVE" if expected_positive else "NEGATIVE"}', 'AUTO_FIX')
    return {"repaired_islands":repaired_islands,"repaired_faces":repaired_faces,"mixed_islands":mixed}


def validate_uv_winding(mesh, expected_positive=True, area_epsilon=1.0e-12,
                        expected_signs=None):
    """Validate winding against the *physical* UV basis of each face.

    Legacy SURFACE/CAP/CORNICE retain positive winding. WALL facade faces,
    after the absolute U rephase, are allowed an expected negative winding
    for legitimate PATH_LEFT/PATH_RIGHT and UP/DOWN geometric directions.
    The mapping is computed from XY path arclength and world Z, independently
    of the resulting UV winding, rather than inferring correctness from UV.
    """
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise ValueError("У результата отсутствует активная UV-карта")
    wrong = []
    for polygon in mesh.polygons:
        uvs = [uv_layer.data[index].uv for index in polygon.loop_indices]
        signed_area = 0.5 * sum(
            first.x * second.y - second.x * first.y
            for first, second in zip(uvs, uvs[1:] + uvs[:1])
        )
        if abs(signed_area) <= area_epsilon:
            continue
        expected = (expected_signs.get(polygon.index, expected_positive)
                    if expected_signs is not None else expected_positive)
        if (signed_area > 0.0) != bool(expected):
            wrong.append(polygon.index)
    if wrong:
        raise ValueError(
            f"Обнаружен flipped UV относительно физического направления: "
            f"{len(wrong)} граней; первые индексы {wrong[:10]}"
        )
    log_event(
        f"UV_WINDING faces={len(mesh.polygons)}; expected={'POSITIVE' if expected_positive else 'NEGATIVE'}; "
        "flipped=0; status=OK",
        "UV_CHECK",
    )


def mirror_uvs_horizontally(uvs, source):
    """Mirror one tile so down-facing geometry keeps positive UV winding."""
    u_min = source["bottom_left_uv"].x
    u_max = source["bottom_right_uv"].x
    return [Vector((u_min + u_max - uv.x, uv.y)) for uv in uvs]


def validate_surface_projected_area(mesh, expected_boundary, tile_length):
    """XY coverage guard after welding (works for roof and planter child)."""
    from .surface_coverage import projected_area, coverage_tolerance
    boundary = list(expected_boundary)
    if len(boundary) > 1 and (boundary[0] - boundary[-1]).length <= 1.0e-3:
        boundary = boundary[:-1]
    expected = projected_area(boundary)
    actual = 0.0
    for polygon in mesh.polygons:
        actual += projected_area([mesh.vertices[index].co for index in polygon.vertices])
    delta = actual - expected
    tolerance = coverage_tolerance(expected, tile_length)
    log_event(
        f'SURFACE_AREA_COVERAGE status={"OK" if abs(delta)<=tolerance else "FAILED"}; '
        f'expected={expected:.6f}; actual={actual:.6f}; delta={delta:.8f}; '
        f'tolerance={tolerance:.8f}; faces={len(mesh.polygons)}', 'SURFACE_CHECK',
    )
    if abs(delta) > tolerance:
        raise ValueError(
            'Поверхность: отсутствует модуль или обнаружено наложение полигонов: '
            f'разница площадей {delta:.6f}. Текущий результат не заменён.'
        )
    return True


def validate_surface_boundary_coverage(mesh, expected_boundary, tile_length):
    """Reject silent holes and missing boundary cells in a generated surface."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    boundary_edges = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    if not boundary_edges:
        bm.free()
        raise ValueError("Поверхность не имеет внешней границы")

    adjacency = {}
    for edge in boundary_edges:
        a, b = edge.verts
        adjacency.setdefault(a.index, set()).add(b.index)
        adjacency.setdefault(b.index, set()).add(a.index)
    remaining = set(adjacency)
    components = 0
    while remaining:
        components += 1
        stack = [remaining.pop()]
        while stack:
            current = stack.pop()
            for neighbor in adjacency.get(current, ()):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)

    edge_points = [
        (edge.verts[0].co.copy(), edge.verts[1].co.copy())
        for edge in boundary_edges
    ]
    bm.free()
    if components != 1:
        log_event(
            f"SURFACE_BOUNDARY_COVERAGE status=FAILED; boundary_components={components}; "
            f"boundary_edges={len(edge_points)}; reason=hole_or_disconnected_boundary",
            "SURFACE_CHECK",
        )
        raise ValueError(
            f"В поверхности обнаружена дыра или разрыв: граничных контуров={components}. "
            "Результат отменён, исходный путь не изменён."
        )

    def distance_to_edge_xy(point, start, end):
        dx = end.x - start.x
        dy = end.y - start.y
        length_squared = dx * dx + dy * dy
        if length_squared <= EPS * EPS:
            return ((point.x - start.x) ** 2 + (point.y - start.y) ** 2) ** 0.5
        factor = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared
        factor = max(0.0, min(1.0, factor))
        x = start.x + dx * factor
        y = start.y + dy * factor
        return ((point.x - x) ** 2 + (point.y - y) ** 2) ** 0.5

    expected = list(expected_boundary)
    if len(expected) > 1 and (expected[0] - expected[-1]).length <= 1.0e-3:
        expected = expected[:-1]
    sample_step = max(REGULATION_WELD_DISTANCE * 2.0, tile_length * 0.25)
    tolerance = max(REGULATION_WELD_DISTANCE * 2.5, tile_length * 1.0e-4)
    worst = 0.0
    missing = []
    sample_index = 0
    for start, end in zip(expected, expected[1:] + expected[:1]):
        length_xy = horizontal_length(start, end)
        parts = max(1, int(ceil(length_xy / sample_step)))
        for part in range(parts + 1):
            point = start.lerp(end, part / parts)
            distance = min(distance_to_edge_xy(point, a, b) for a, b in edge_points)
            worst = max(worst, distance)
            if distance > tolerance and len(missing) < 20:
                missing.append((sample_index, tuple(point), distance))
            sample_index += 1
    log_event(
        f"SURFACE_BOUNDARY_COVERAGE status={'FAILED' if missing else 'OK'}; "
        f"boundary_components={components}; boundary_edges={len(edge_points)}; "
        f"samples={sample_index}; tolerance={tolerance:.9f}; max_distance={worst:.9f}; "
        f"missing_sample={missing[:10]}",
        "SURFACE_CHECK",
    )
    if missing:
        raise ValueError(
            f"Поверхность не дошла до исходной границы: найдено пропущенных участков "
            f"{len(missing)}, максимальный зазор {worst:.6f} м. Результат отменён."
        )


