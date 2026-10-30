bl_info = {
    "name": "Collision Inspector — Selected Object Shells",
    "author": "OpenAI",
    "version": (3, 5, 2),
    "warning": "RC12.2 — persistent Viewport controls; native Blender validation required",
    "blender": (4, 0, 0),
    "location": "3D Viewport > Sidebar > Collision",
    "description": (
        "Профессиональная проверка и безопасное исправление активного Mesh-объекта по отдельным "
        "оболочкам: топология, вырожденная геометрия, выпуклость, самопересечения, "
        "пересечения, вложения и точные малые зазоры"
    ),
    "category": "Mesh",
}

import bpy
import bmesh
import hashlib
import math
import time
import struct
import traceback
import json
import platform
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, deque
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_ray_tri, tessellate_polygon
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from . import viewport_ui as _viewport_ui
from .intersection_trace import triangle_segment as _triangle_intersection_trace
from .overlap_witness import overlap_polygon as _triangle_coplanar_witness
from .coplanar_broadphase import face_aabb_pairs as _face_aabb_pairs
from . import example_library as _examples
from .fixture_geometry import CATEGORIES as _EXAMPLE_CATEGORIES
from ...core.reliability.predicates import (
    exact_segment_triangle_crossing_3d as _rf_segment_tri_cross,
    exact_coplanar_triangle_positive_overlap_3d as _rf_coplanar_overlap,
)
from ...core.reliability.legacy import classify_text as _rf_classify_legacy
from ...core.reliability.ledger import record as _rf_record_decision
from ...core.reliability.decision import (
    Decision as _RFDecision,
    SETUP_REQUIRED as _RF_SETUP_REQUIRED,
    INTERNAL_ERROR as _RF_INTERNAL_ERROR,
)


# =============================================================================
# Constants
# =============================================================================

MIN_BLENDER_VERSION = (4, 0, 0)
MAX_SUPPORTED_MAJOR = 5
_ICON_NAMES = None
# Runtime-only ownership. These are Python tokens, never stored in .blend and
# never hold Blender datablocks or modal instances alive after unregister.
_PICKER_SESSIONS = {}
_PICKER_CONFIRM_SCENES = set()
_PICKER_REARM_TRIED = {}


def _resume_visible_picker(window, area):
    """Recover input after repair/Undo without toggling an intentionally hidden HUD.

    Called by a Blender UI timer, not by GPU drawing. The control remains
    visually present while a confirmation operator or undo temporarily owns
    input. Never start a controller in Edit Mode, on an unrelated object, or
    while Blender's own confirmation for suggested repair is visible.
    """
    if bpy.app.background or area.type != 'VIEW_3D':
        return False
    scene = window.scene
    settings = getattr(scene, 'collision_inspector', None)
    if (settings is None or not settings.overlay_enabled or
            not settings.analysis_signature or settings.overlay_pick_mode or
            scene.as_pointer() in _PICKER_CONFIRM_SCENES):
        return False
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region is None:
        return False
    key = (window.as_pointer(), area.as_pointer(), scene.as_pointer())
    now = time.monotonic()
    if now - _PICKER_REARM_TRIED.get(key, -100.0) < 1.2:
        return False
    _PICKER_REARM_TRIED[key] = now
    if len(_PICKER_REARM_TRIED) > 96:
        for obsolete in list(_PICKER_REARM_TRIED):
            if now - _PICKER_REARM_TRIED[obsolete] > 15.0:
                _PICKER_REARM_TRIED.pop(obsolete, None)
    # This override ensures both Window and Viewport are correct; relying on
    # bpy.context.area in a timer can silently select a different editor.
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            ctx = bpy.context
            obj = ctx.active_object
            if (ctx.scene != scene or obj is None or obj.type != 'MESH' or
                    obj.name != settings.object_name or obj.mode != 'OBJECT'):
                return False
            result = bpy.ops.collision_inspector.pick_markers('INVOKE_DEFAULT')
            if 'RUNNING_MODAL' in result:
                _trace_action('VIEWPORT_CONTROLS_RECOVERED', ctx)
                return True
    except (RuntimeError, ReferenceError, ValueError, AttributeError, TypeError) as exc:
        # No hard failure or repeated print storm from an unavailable area.
        _viewport_ui.note_controller_error(exc)
    return False

KIND_LABELS = {
    "UNCLOSED": "Незамкнутость",
    "NONMANIFOLD": "Non-manifold",
    "NONCONVEX": "Невыпуклость",
    "DEGENERATE": "Вырожденная геометрия",
    "SELF_INTERSECTION": "Самопересечение",
    "INTERSECTION": "Пересечение",
    "NESTED": "Полное вложение",
    "GAP": "Малый зазор",
    "CONTACT": "Касание",
    "COINCIDENT": "Совпадающие оболочки",
    "SURFACE_OVERLAP": "Наложение поверхностей",
}

KIND_ICONS = {
    "UNCLOSED": "MESH_CIRCLE",
    "NONMANIFOLD": "ERROR",
    "NONCONVEX": "MOD_TRIANGULATE",
    "DEGENERATE": "MESH_DATA",
    "SELF_INTERSECTION": "SELECT_INTERSECT",
    "INTERSECTION": "SELECT_INTERSECT",
    "NESTED": "FULLSCREEN_ENTER",
    "GAP": "SNAP_MIDPOINT",
    "CONTACT": "SNAP_MIDPOINT",
    "COINCIDENT": "DUPLICATE",
    "SURFACE_OVERLAP": "SELECT_INTERSECT",
}


# =============================================================================
# Session action trace — bounded and opt-in ZIP export (never writes to .blend)
# =============================================================================

_ACTION_TRACE = deque(maxlen=400)


def _trace_action(action, context=None, **metadata):
    context = context or bpy.context
    obj = getattr(context, 'active_object', None)
    settings = getattr(getattr(context, 'scene', None), 'collision_inspector', None)
    _ACTION_TRACE.append({
        'utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'action': action,
        'object': getattr(obj, 'name', None),
        'mode': getattr(context, 'mode', None),
        'active_finding': getattr(settings, 'event_index', None),
        'event_total': len(settings.events) if settings else None,
        'detail': metadata,
    })


def _diagnostic_snapshot(context):
    settings = context.scene.collision_inspector
    obj = bpy.data.objects.get(settings.object_name)
    snapshot = {'object': settings.object_name, 'analysis_signature': settings.analysis_signature,
                'events_total': len(settings.events), 'events_sample': [],
                'sampling_limits': {'events': 120, 'vertex_coordinates_per_event': 64}}
    if obj is not None and obj.type == 'MESH':
        mesh = obj.data
        snapshot['mesh'] = {'vertices':len(mesh.vertices), 'edges':len(mesh.edges),
                            'polygons':len(mesh.polygons), 'object_matrix_world':
                            [list(float(x) for x in row) for row in obj.matrix_world]}
        for event in list(settings.events)[:120]:
            entry = {'kind':event.kind,'text':event.text,'detail':event.detail,
                     'shells':[event.shell_a,event.shell_b],
                     'mode':event.select_mode,
                     'selected_a':event.indices_a[:3000],
                     'selected_b':event.indices_b[:3000],
                     'trace_segments':event.trace_segments[:18000],
                     'overlap_polygons':event.overlap_polygons[:18000],
                     'repair_available':event.repair_available,
                     'repair_reason':event.repair_reason[:250],
                     'repair_plan':event.repair_plan[:14000],
                     'repair_preview_lines':event.repair_preview_lines[:16000],
                     'indices_truncated':len(event.indices_a)>3000 or len(event.indices_b)>3000,
                     'witness_points':([list(event.witness_a),list(event.witness_b)]
                                       if event.has_witness_pair else None)}
            try:
                ids, capped = _viewport_ui._collect_vertex_indices(obj,event,limit=64)
                entry['sample_vertices'] = [
                    {'index':i, 'xyz':tuple(float(x) for x in mesh.vertices[i].co)}
                    for i in ids[:64] if 0 <= i < len(mesh.vertices)]
                entry['sample_capped'] = capped
            except Exception as exc:
                entry['snapshot_error'] = repr(exc)
            snapshot['events_sample'].append(entry)
    return snapshot


def _write_diagnostics_zip(context):
    settings = context.scene.collision_inspector
    home_downloads = Path.home() / 'Downloads'
    target = home_downloads if home_downloads.is_dir() else Path(tempfile.gettempdir())
    filename = 'CollisionInspector_Diagnostics_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.zip'
    path = target / filename
    areas = []
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                areas.append({'width':area.width,'height':area.height,
                              'x':area.x,'y':area.y,
                              'side_panel_width':next((region.width for region in area.regions if region.type=='UI'),0)})
    metadata = {
        'addon_version': bl_info['version'],
        'blender': bpy.app.version_string,
        'python': sys.version,
        'platform': platform.platform(),
        'blender_binary': bpy.app.binary_path,
        'scene': context.scene.name,
        'active_object':getattr(context.active_object,'name',None),
        'selected_objects':[obj.name for obj in context.selected_objects],
        'mode': context.mode,
        'status':settings.status,
        'viewport_areas':areas,
        'ui_state':{'overlay_enabled':settings.overlay_enabled,
                    'overlay_hud':settings.overlay_hud,'overlay_markers':settings.overlay_markers,
                    'all_markers':settings.overlay_all_markers,'side':settings.overlay_side,
                    'xray':settings.overlay_xray,'opacity':settings.overlay_opacity,
                    'picker_running':settings.overlay_pick_mode,'filter':settings.issue_filter,
                    'marker_style':settings.overlay_marker_style,'pulse':settings.overlay_pulse,
                    'nav_focus':settings.overlay_nav_focus,
                    'active_index':settings.event_index,
                    'example_structure':settings.example_structure,
                    'example_labels':settings.example_labels},
        'check_settings':{'convex_angle':settings.convex_angle,
                          'gap_tolerance_mm':settings.gap_tolerance_mm,
                          'check_gaps':settings.check_gaps,
                          'check_degenerate':settings.check_degenerate,
                          'check_self_intersections':settings.check_self_intersections},
        'repair_status':settings.repair_last_status,
        'gpu_overlay_error':_viewport_ui.draw_error(),
    }
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        def put(name, data):
            archive.writestr(name,json.dumps(data,ensure_ascii=False,indent=2,default=str))
        put('environment_ui.json',metadata)
        put('geometry_problem_snapshot.json',_diagnostic_snapshot(context))
        root = _examples.owned_root(context.scene,settings.example_root_name)
        if root is not None:
            put('diagnostic_gallery_manifest.json', {
                'collection':root.name,'expected':55,
                'structure':root.get('ci_gallery_mode','SEPARATE'),
                'labels':root.get('ci_gallery_labels','NONE'),
                'objects':[{'name':ob.name,'kind':ob.get('ci_kind'),
                            'variant':ob.get('ci_variant'),'description':ob.get('ci_case_description'),
                            'combined_cases':ob.get('ci_combined_cases','')[:12000]}
                           for ob in _examples.meshes_in(root)]})
        if settings.example_report_path:
            try:
                report_file=Path(settings.example_report_path)
                if report_file.is_file() and report_file.stat().st_size < 2000000:
                    archive.writestr('fixture_verification.json',report_file.read_bytes())
            except Exception as exc:
                archive.writestr('fixture_verification_error.txt',repr(exc))
        archive.writestr('action_trace.jsonl', '\n'.join(json.dumps(x,ensure_ascii=False,default=str)
                                                      for x in _ACTION_TRACE))
        archive.writestr('repair_log.txt',settings.repair_last_log or 'No repair was attempted')
        archive.writestr('last_error.txt',settings.last_error or 'No error recorded')
        try:
            archive.writestr('full_inspection_log.txt',_make_log(context))
        except Exception:
            archive.writestr('full_inspection_log_error.txt',traceback.format_exc())
        archive.writestr('README.txt',
            'Collision Inspector 2.8.1 RC5.1 diagnostics. No .blend mesh was modified by this operation.\n'
            'Includes sampled geometry, action trace since addon load, scene/UI state and logs.\n'
            'This ZIP may contain object/file names; inspect before sharing.\n'
            'Actual GPU screenshot and full mesh payload are not included.\n')
    return path


# =============================================================================
# Generic helpers
# =============================================================================


def _ui_icon(name, fallback="NONE"):
    """Return only icons that exist in the running Blender build."""
    global _ICON_NAMES
    try:
        if _ICON_NAMES is None:
            parameter = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"]
            _ICON_NAMES = {item.identifier for item in parameter.enum_items}
        return name if name in _ICON_NAMES else fallback
    except (AttributeError, KeyError, TypeError):
        return fallback


def _supported_version():
    version = bpy.app.version
    return version >= MIN_BLENDER_VERSION and version[0] <= MAX_SUPPORTED_MAJOR


def _safe_object_mode(context):
    if context.mode == "OBJECT":
        return True
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
        return True
    except RuntimeError:
        return False


def _active_selected_mesh(context, operator=None):
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        if operator:
            operator.report({"WARNING"}, "Выберите активный Mesh-объект")
        return None
    try:
        selected = obj.select_get()
    except Exception:
        selected = obj in context.selected_objects
    if not selected:
        if operator:
            operator.report({"WARNING"}, "Активный Mesh-объект должен быть выделен")
        return None
    return obj


def _wrap_text(text, width=56):
    words = (text or "").split()
    if not words:
        return [""]
    lines = []
    current = []
    current_len = 0
    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and current_len + extra > width:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += extra
    if current:
        lines.append(" ".join(current))
    return lines


def _indices_to_string(indices):
    return ",".join(str(int(i)) for i in sorted(set(indices)))


def _string_to_indices(value):
    result = set()
    if not value:
        return result
    for token in value.split(","):
        try:
            result.add(int(token))
        except (TypeError, ValueError):
            pass
    return result


def _view3d_area_region(context):
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type == "WINDOW":
                return area, region
    return None, None


def _deselect_other_objects(context, target):
    for obj in context.view_layer.objects:
        try:
            if obj != target and obj.select_get():
                obj.select_set(False)
        except Exception:
            pass
    try:
        target.hide_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
    except Exception:
        pass


# =============================================================================
# Object signature — protects selections from stale indices
# =============================================================================


def _update_digest_float(digest, value):
    digest.update(struct.pack("<d", float(value)))


def _object_signature(obj):
    if obj is None or obj.type != "MESH":
        return ""

    digest = hashlib.blake2b(digest_size=20)
    digest.update(obj.name.encode("utf-8", errors="replace"))
    digest.update(b"\0")

    for row in obj.matrix_world:
        for value in row:
            _update_digest_float(digest, value)

    mesh = obj.data
    digest.update(struct.pack("<III", len(mesh.vertices), len(mesh.edges), len(mesh.polygons)))

    for vert in mesh.vertices:
        _update_digest_float(digest, vert.co.x)
        _update_digest_float(digest, vert.co.y)
        _update_digest_float(digest, vert.co.z)

    for edge in mesh.edges:
        digest.update(struct.pack("<II", edge.vertices[0], edge.vertices[1]))

    for poly in mesh.polygons:
        digest.update(struct.pack("<I", len(poly.vertices)))
        for vertex_index in poly.vertices:
            digest.update(struct.pack("<I", vertex_index))

    return digest.hexdigest()


# =============================================================================
# Shell extraction
# =============================================================================


def _face_components(bm):
    """Connected components by shared EDGES. Vertex-only contact stays separate."""
    bm.faces.ensure_lookup_table()
    pending = set(bm.faces)
    components = []

    while pending:
        seed = pending.pop()
        stack = [seed]
        faces = {seed}

        while stack:
            face = stack.pop()
            for edge in face.edges:
                for linked_face in edge.link_faces:
                    if linked_face in pending:
                        pending.remove(linked_face)
                        faces.add(linked_face)
                        stack.append(linked_face)

        components.append(faces)

    components.sort(key=lambda faces: min((face.index for face in faces), default=-1))
    return components


def _edge_regions(edges):
    """Split loose/problem edges into connected regions by shared vertices."""
    pending = set(edges)
    regions = []

    while pending:
        seed = pending.pop()
        stack = [seed]
        region = {seed}

        while stack:
            edge = stack.pop()
            for vert in edge.verts:
                for linked_edge in vert.link_edges:
                    if linked_edge in pending:
                        pending.remove(linked_edge)
                        region.add(linked_edge)
                        stack.append(linked_edge)

        regions.append(region)

    regions.sort(key=lambda region: min((edge.index for edge in region), default=-1))
    return regions


def _extract_shell_topology(obj):
    """Return face shells and loose geometry using indices from obj.data."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()

        components = _face_components(bm)
        shells = []

        for shell_index, faces in enumerate(components, start=1):
            edges = {edge for face in faces for edge in face.edges}
            verts = {vert for face in faces for vert in face.verts}
            bad_edges = {edge for edge in edges if not edge.is_manifold}

            shells.append({
                "index": shell_index,
                "faces": tuple(sorted(face.index for face in faces)),
                "edges": tuple(sorted(edge.index for edge in edges)),
                "verts": tuple(sorted(vert.index for vert in verts)),
                "bad_edges": tuple(sorted(edge.index for edge in bad_edges)),
                "boundary_edges": tuple(sorted(edge.index for edge in edges if len(edge.link_faces) == 1)),
                "is_closed": not bool(bad_edges),
                "euler": len(verts) - len(edges) + len(faces),
            })

        face_edges = {edge for face in bm.faces for edge in face.edges}
        loose_edges = {edge for edge in bm.edges if edge not in face_edges}
        loose_regions = [
            tuple(sorted(edge.index for edge in region))
            for region in _edge_regions(loose_edges)
        ]

        used_verts = {vert for face in bm.faces for vert in face.verts}
        loose_edge_verts = {vert for edge in loose_edges for vert in edge.verts}
        isolated_verts = tuple(sorted(
            vert.index
            for vert in bm.verts
            if vert not in used_verts and vert not in loose_edge_verts
        ))

        return shells, loose_regions, isolated_verts
    finally:
        bm.free()


# =============================================================================
# Checker convexity criterion — adapted from Object -> Shell
# =============================================================================


def _checker_convexity_by_shell(obj, shells, angle_tolerance=0.1):
    """Return (bad_faces_by_shell, metrics_by_shell) using the Checker criterion.

    The real mesh is never changed. Face winding is normalized only on the
    temporary BMesh so the signed-angle verdict does not depend on imported
    normal orientation.
    """
    face_to_shell = {}
    for shell in shells:
        for face_index in shell["faces"]:
            face_to_shell[face_index] = shell["index"]

    bad_faces = {shell["index"]: set() for shell in shells}
    metrics = {
        shell["index"]: {
            "bad_edges": 0,
            "worst_angle_deg": 0.0,
            "worst_violation_deg": 0.0,
            "worst_edge_vertices": (),
        }
        for shell in shells
    }
    if not face_to_shell:
        return bad_faces, metrics

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()

        # Normalize winding on the analysis copy only.
        if bm.faces:
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])

        origin_layer = bm.faces.layers.int.new("collision_inspector_src_face")
        for face in bm.faces:
            face[origin_layer] = face.index

        if bm.faces:
            bmesh.ops.triangulate(
                bm,
                faces=bm.faces[:],
                quad_method="FIXED",
                ngon_method="BEAUTY",
            )

        limit = math.radians(angle_tolerance)
        for edge in bm.edges:
            if len(edge.link_faces) != 2:
                continue

            face_1, face_2 = edge.link_faces
            src_1 = int(face_1[origin_layer])
            src_2 = int(face_2[origin_layer])
            shell_1 = face_to_shell.get(src_1)
            shell_2 = face_to_shell.get(src_2)
            if shell_1 is None or shell_1 != shell_2:
                continue

            try:
                angle = edge.calc_face_angle_signed(0.0)
            except (ValueError, RuntimeError):
                continue

            # A diagonal created inside one source polygon is expected to be flat.
            if src_1 == src_2:
                violation = abs(angle) - limit
                is_bad = violation > 0.0
                signed_angle_for_report = abs(angle)
            else:
                violation = (-angle) - limit
                is_bad = angle < -limit
                signed_angle_for_report = angle

            if not is_bad:
                continue

            bad_faces[shell_1].add(src_1)
            bad_faces[shell_1].add(src_2)
            metric = metrics[shell_1]
            metric["bad_edges"] += 1
            violation_deg = math.degrees(max(0.0, violation))
            if violation_deg > metric["worst_violation_deg"]:
                metric["worst_violation_deg"] = violation_deg
                metric["worst_angle_deg"] = math.degrees(signed_angle_for_report)
                metric["worst_edge_vertices"] = tuple(sorted(v.index for v in edge.verts))

        return bad_faces, metrics
    finally:
        bm.free()


def _checker_bad_convex_faces_by_shell(obj, shells, angle_tolerance=0.1):
    """Compatibility wrapper used by repair code."""
    return _checker_convexity_by_shell(obj, shells, angle_tolerance)[0]

# =============================================================================
# Checker pairwise geometry — adapted from Object pair -> Shell pair
# =============================================================================


def _bbox_from_coords(coords):
    if not coords:
        zero = Vector((0.0, 0.0, 0.0))
        return zero.copy(), zero.copy()

    return (
        Vector((
            min(value.x for value in coords),
            min(value.y for value in coords),
            min(value.z for value in coords),
        )),
        Vector((
            max(value.x for value in coords),
            max(value.y for value in coords),
            max(value.z for value in coords),
        )),
    )


def _build_shell_collision_data(obj, shells, origin=None, gap_tolerance=0.0):
    """Build world-space BVHs for each shell, shifted near the local origin.

    Two BVHs are kept: exact (epsilon 0) for intersections and an expanded BVH
    for discovering surface pairs that may be within the requested gap.
    """
    matrix = obj.matrix_world
    if origin is not None:
        matrix = Matrix.Translation(-origin) @ matrix

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.transform(matrix)
        bm.normal_update()
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()

        data = {}
        for shell in shells:
            global_vert_indices = list(shell["verts"])
            global_face_indices = list(shell["faces"])
            remap = {g: i for i, g in enumerate(global_vert_indices)}

            verts = [bm.verts[index].co.copy() for index in global_vert_indices]
            face_verts = []
            polys = []
            face_orig = []
            for face_index in global_face_indices:
                face = bm.faces[face_index]
                polygon = tuple(remap[vert.index] for vert in face.verts)
                if len(polygon) < 3:
                    continue
                face_verts.append(polygon)
                polys.append((face.calc_center_median().copy(), face.normal.copy()))
                face_orig.append(face_index)

            edge_to_faces = {}
            for local_face_index, polygon in enumerate(face_verts):
                for edge_index in range(len(polygon)):
                    a = polygon[edge_index]
                    b = polygon[(edge_index + 1) % len(polygon)]
                    key = tuple(sorted((a, b)))
                    edge_to_faces.setdefault(key, set()).add(local_face_index)
            edge_segments = []
            for (a, b), local_faces in edge_to_faces.items():
                p0, p1 = verts[a], verts[b]
                mn, mx = _bbox_from_coords((p0, p1))
                edge_segments.append({
                    "a": a, "b": b, "p0": p0, "p1": p1,
                    "faces": tuple(sorted(local_faces)), "min": mn, "max": mx,
                })

            def make_bvh(epsilon):
                if not verts or not face_verts:
                    return None
                try:
                    return BVHTree.FromPolygons(
                        verts, face_verts, all_triangles=False, epsilon=max(0.0, epsilon)
                    )
                except TypeError:
                    return BVHTree.FromPolygons(verts, face_verts, all_triangles=False)

            bbox_min, bbox_max = _bbox_from_coords(verts)
            data[shell["index"]] = {
                "shell": shell,
                "bvh": make_bvh(0.0),
                "gap_bvh": make_bvh(gap_tolerance),
                "verts": verts,
                "vert_orig": global_vert_indices,
                "polys": polys,
                "face_verts": face_verts,
                "face_orig": face_orig,
                "edge_segments": edge_segments,
                "bbox_min": bbox_min,
                "bbox_max": bbox_max,
            }
        return data
    finally:
        bm.free()


def _aabb_has_positive_shared_volume(data_1, data_2):
    """True only when each bbox axis overlaps with positive *physical* width.

    A plane-face touching pair (e.g. 11-03..11-05) has no common volume:
    some ray/face implementations can report it as penetration from a boundary.
    This veto cannot prove actual 3D intersection, merely rule it out.
    """
    ext = [max(float(data_1['bbox_max'][i]-data_1['bbox_min'][i]),
               float(data_2['bbox_max'][i]-data_2['bbox_min'][i])) for i in range(3)]
    for i in range(3):
        width = min(float(data_1['bbox_max'][i]), float(data_2['bbox_max'][i])) - max(
            float(data_1['bbox_min'][i]), float(data_2['bbox_min'][i]))
        if width <= max(1e-9, ext[i]*1e-9):
            return False
    return True


def _aabb_overlap(data_1, data_2, tolerance):
    return (
        data_1["bbox_min"].x <= data_2["bbox_max"].x + tolerance
        and data_1["bbox_max"].x >= data_2["bbox_min"].x - tolerance
        and data_1["bbox_min"].y <= data_2["bbox_max"].y + tolerance
        and data_1["bbox_max"].y >= data_2["bbox_min"].y - tolerance
        and data_1["bbox_min"].z <= data_2["bbox_max"].z + tolerance
        and data_1["bbox_max"].z >= data_2["bbox_min"].z - tolerance
    )


def _is_inside_convex(inner_verts, outer_verts, outer_polys, tolerance=1.0e-9):
    """Containment independent of source face winding.

    Each supporting plane is oriented so the outer shell centroid lies on the
    inside side. Call this only for a closed, manifold, convex outer shell.
    """
    if not inner_verts or not outer_verts or not outer_polys:
        return False
    outer_center = sum(outer_verts, Vector((0.0, 0.0, 0.0))) / len(outer_verts)
    oriented = []
    for center, normal in outer_polys:
        n = normal.copy()
        if n.length_squared <= 1.0e-24:
            continue
        if n.dot(outer_center - center) > 0.0:
            n.negate()
        oriented.append((center, n))
    if len(oriented) != len(outer_polys):
        return False
    # Strict containment: touching/coplanar/coincident meshes are NOT nested.
    # Normals coming from BMesh are unit length, but normalize defensively.
    return all(
        all((vert - center).dot(normal) < -tolerance * normal.length for center, normal in oriented)
        for vert in inner_verts
    )


def _segment_pierces_triangle(point, other, triangle, eps=1.0e-9):
    direction = other - point
    length_sq = direction.dot(direction)
    if length_sq <= eps * eps:
        return False
    hit = intersect_ray_tri(
        triangle[0], triangle[1], triangle[2], direction, point, True
    )
    if hit is None:
        return False
    along = (hit - point).dot(direction) / length_sq
    # Only a transversal crossing in the INTERIOR of the tested edge counts as
    # penetration. Boundary/endpoint contacts are handled separately.
    normal = (triangle[1] - triangle[0]).cross(triangle[2] - triangle[0])
    if normal.length_squared == 0.0:
        return False
    signed_a = normal.dot(point - triangle[0]) / normal.length
    signed_b = normal.dot(other - triangle[0]) / normal.length
    return (eps < along < 1.0 - eps) and (
        (signed_a < -eps and signed_b > eps)
        or (signed_a > eps and signed_b < -eps)
    )


def _triangulate_polygon(verts, indices):
    """Tessellate concave n-gons, never produce artificial fan triangles.

    The returned vertices are original world coordinates; Blender's tessellator
    is used only for a scratch polygon and never mutates the source Mesh.
    """
    if len(indices) < 3:
        return []
    points = [verts[index].copy() for index in indices]
    if len(points) == 3:
        return [tuple(points)]
    # Blender 4.2–5.x returns *vertex indices*, not Vector coordinates.
    # Using those integer triples directly caused TypeError: intersect_ray_tri
    # in the 55-case Blender 4.5 field verification (06-01..06-05, 11-03..11-05).
    triangle_ids = tessellate_polygon([points])
    if len(triangle_ids) != len(points) - 2:
        raise ValueError("Невозможно корректно триангулировать n-gon; анализ остановлен")
    result = []
    for ids in triangle_ids:
        if len(ids) != 3 or any(not isinstance(i, int) or not 0 <= i < len(points) for i in ids):
            raise ValueError("Blender tessellate_polygon вернул некорректные индексы")
        result.append(tuple(points[i] for i in ids))
    return result


def _tri_2d(triangle):
    normal = (triangle[1] - triangle[0]).cross(triangle[2] - triangle[0])
    major = max(range(3), key=lambda axis: abs(normal[axis]))
    axes = tuple(axis for axis in range(3) if axis != major)
    return [(point[axes[0]], point[axes[1]]) for point in triangle]


def _polygon_signed_area_2d(poly):
    return 0.5 * sum(
        a[0] * b[1] - a[1] * b[0]
        for a, b in zip(poly, poly[1:] + poly[:1])
    )


def _coplanar_triangle_overlap_area(triangle_a, triangle_b, eps=1.0e-9):
    """Positive area only; a shared edge or vertex is NOT face overlap."""
    n1 = (triangle_a[1] - triangle_a[0]).cross(triangle_a[2] - triangle_a[0])
    n2 = (triangle_b[1] - triangle_b[0]).cross(triangle_b[2] - triangle_b[0])
    if n1.length <= eps * eps or n2.length <= eps * eps:
        return 0.0
    if n1.cross(n2).length > 1.0e-7 * n1.length * n2.length:
        return 0.0
    normal = n1 / n1.length
    if any(abs(normal.dot(v - triangle_a[0])) > eps for v in triangle_b):
        return 0.0
    major = max(range(3), key=lambda axis: abs(normal[axis]))
    axes = tuple(axis for axis in range(3) if axis != major)
    project = lambda tri: [(v[axes[0]], v[axes[1]]) for v in tri]
    subject, clip = project(triangle_a), project(triangle_b)
    direction = 1.0 if _polygon_signed_area_2d(clip) >= 0.0 else -1.0
    for c1, c2 in zip(clip, clip[1:] + clip[:1]):
        if not subject:
            return 0.0
        def side(p):
            return direction * ((c2[0] - c1[0]) * (p[1] - c1[1])
                                - (c2[1] - c1[1]) * (p[0] - c1[0]))
        result = []
        previous = subject[-1]
        previous_side = side(previous)
        for current in subject:
            current_side = side(current)
            if (current_side >= 0.0) != (previous_side >= 0.0):
                denom = previous_side - current_side
                if denom:
                    t = previous_side / denom
                    result.append((previous[0] + t * (current[0] - previous[0]),
                                   previous[1] + t * (current[1] - previous[1])))
            if current_side >= 0.0:
                result.append(current)
            previous, previous_side = current, current_side
        subject = result
    return abs(_polygon_signed_area_2d(subject)) if len(subject) >= 3 else 0.0


def _polygons_coplanar_overlap(verts_1, face_1, verts_2, face_2):
    for triangle_a in _triangulate_polygon(verts_1, face_1):
        for triangle_b in _triangulate_polygon(verts_2, face_2):
            if _rf_coplanar_overlap(triangle_a, triangle_b):
                return True
    return False


def _shells_exactly_coincident(data_1, data_2, tolerance=1.0e-9):
    """Conservative exact surface match; tolerantly reject near/partial copies."""
    if len(data_1['face_verts']) != len(data_2['face_verts']):
        return False
    if len(data_1['verts']) != len(data_2['verts']):
        return False
    def faces(data):
        verts = data['verts']
        def vertex_id(v):
            return tuple(round(value / tolerance) for value in v)
        return Counter(
            tuple(sorted(vertex_id(verts[index]) for index in face))
            for face in data['face_verts']
        )
    return faces(data_1) == faces(data_2)


def _polygons_intersect(verts_1, face_1, verts_2, face_2):
    """Checker narrow phase: hard verdict requires shared exact proof."""
    if len(face_1) < 3 or len(face_2) < 3:
        return False
    triangles_1 = _triangulate_polygon(verts_1, face_1)
    triangles_2 = _triangulate_polygon(verts_2, face_2)
    for tri_a in triangles_1:
        for tri_b in triangles_2:
            if _rf_coplanar_overlap(tri_a,tri_b):
                return True
            for p,q in zip(tri_a,tri_a[1:]+tri_a[:1]):
                if _rf_segment_tri_cross(p,q,*tri_b,include_boundary=True,exclude_endpoints=True):
                    return True
            for p,q in zip(tri_b,tri_b[1:]+tri_b[:1]):
                if _rf_segment_tri_cross(p,q,*tri_a,include_boundary=True,exclude_endpoints=True):
                    return True
    return False


def _overlap_trace_segments(data_1, data_2, overlap, world_origin, limit=48):
    """Only geometric evidence: line segments verified by triangle intersections.

    The original BVH may classify coincident/tangential pairs as overlaps;
    those are never converted to an invented line by this helper.
    """
    traces = []
    for a, b in overlap:
        triangles_a = _triangulate_polygon(data_1['verts'], data_1['face_verts'][a])
        triangles_b = _triangulate_polygon(data_2['verts'], data_2['face_verts'][b])
        for tri_a in triangles_a:
            for tri_b in triangles_b:
                segment = _triangle_intersection_trace(tri_a, tri_b)
                if segment is None:
                    continue
                traces.append([tuple(float(p[k] + world_origin[k]) for k in range(3)) for p in segment])
                if len(traces) >= limit:
                    return traces
    return traces


def _coplanar_witness_polygons(data_1, data_2, pairs, world_origin, limit=48):
    """Bounded verified 3D overlap regions, never inferred face centroids.

    Each region belongs to a pair of coplanar triangles. Multiple polygons
    may have shared/internal triangulation edges; no union boundary is claimed.
    """
    polygons = []
    seen = set()
    for a, b in pairs:
        for tri_a in _triangulate_polygon(data_1['verts'], data_1['face_verts'][a]):
            for tri_b in _triangulate_polygon(data_2['verts'], data_2['face_verts'][b]):
                region = _triangle_coplanar_witness(tri_a, tri_b)
                if region is None:
                    continue
                world = [tuple(float(pt[k]+world_origin[k]) for k in range(3)) for pt in region]
                key = tuple(sorted(tuple(round(v, 9) for v in p) for p in world))
                if key in seen:
                    continue
                seen.add(key)
                polygons.append(world)
                if len(polygons)>=limit:
                    return polygons
    return polygons


def _real_face_overlaps(data_1, data_2, candidates):
    result = []
    for face_1_index, face_2_index in candidates:
        if not (0 <= face_1_index < len(data_1["face_verts"])):
            continue
        if not (0 <= face_2_index < len(data_2["face_verts"])):
            continue
        if _polygons_intersect(
            data_1["verts"], data_1["face_verts"][face_1_index],
            data_2["verts"], data_2["face_verts"][face_2_index],
        ):
            result.append((face_1_index, face_2_index))
    return result


def _closest_point_on_triangle(point, a, b, c):
    """Closest point on triangle; implementation from Real-Time Collision Detection."""
    ab = b - a
    ac = c - a
    ap = point - a
    d1, d2 = ab.dot(ap), ac.dot(ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a.copy()
    bp = point - b
    d3, d4 = ab.dot(bp), ac.dot(bp)
    if d3 >= 0.0 and d4 <= d3:
        return b.copy()
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / max(d1 - d3, 1.0e-30)
        return a + v * ab
    cp = point - c
    d5, d6 = ab.dot(cp), ac.dot(cp)
    if d6 >= 0.0 and d5 <= d6:
        return c.copy()
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / max(d2 - d6, 1.0e-30)
        return a + w * ac
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denom = (d4 - d3) + (d5 - d6)
        w = (d4 - d3) / max(denom, 1.0e-30)
        return b + w * (c - b)
    denom = max(va + vb + vc, 1.0e-30)
    v = vb / denom
    w = vc / denom
    return a + ab * v + ac * w


def _segment_segment_closest(p1, q1, p2, q2, eps=1.0e-12):
    """Closest points of two segments, scale-aware (Ericson, RTCD)."""
    d1, d2 = q1 - p1, q2 - p2
    r = p1 - p2
    a, e = d1.dot(d1), d2.dot(d2)
    c, f = d1.dot(r), d2.dot(r)
    # Compare SQUARED lengths with squared linear epsilon.
    len_eps_sq = eps * eps
    if a <= len_eps_sq and e <= len_eps_sq:
        return p1.copy(), p2.copy(), (p1 - p2).length
    if a <= len_eps_sq:
        s, t = 0.0, max(0.0, min(1.0, f / e))
    elif e <= len_eps_sq:
        s, t = max(0.0, min(1.0, -c / a)), 0.0
    else:
        b = d1.dot(d2)
        denom = a * e - b * b
        # a*e has units length^4; an absolute threshold is incorrect.
        s = max(0.0, min(1.0, (b * f - c * e) / denom)) if (
            denom > 1.0e-14 * a * e
        ) else 0.0
        t = (b * s + f) / e
        if t < 0.0:
            t, s = 0.0, max(0.0, min(1.0, -c / a))
        elif t > 1.0:
            t, s = 1.0, max(0.0, min(1.0, (b - c) / a))
    c1, c2 = p1 + d1 * s, p2 + d2 * t
    return c1, c2, (c1 - c2).length


def _triangle_triangle_min_distance(tri_1, tri_2):
    best = (float('inf'), None, None)
    for p in tri_1:
        q = _closest_point_on_triangle(p, *tri_2)
        d = (p - q).length
        if d < best[0]:
            best = (d, p.copy(), q)
    for p in tri_2:
        q = _closest_point_on_triangle(p, *tri_1)
        d = (p - q).length
        if d < best[0]:
            best = (d, q, p.copy())
    edges_1 = ((tri_1[0], tri_1[1]), (tri_1[1], tri_1[2]), (tri_1[2], tri_1[0]))
    edges_2 = ((tri_2[0], tri_2[1]), (tri_2[1], tri_2[2]), (tri_2[2], tri_2[0]))
    for a, b in edges_1:
        for c, d in edges_2:
            p, q, dist = _segment_segment_closest(a, b, c, d)
            if dist < best[0]:
                best = (dist, p, q)
    return best


def _polygon_pair_min_distance(data_1, local_face_1, data_2, local_face_2):
    face_1 = data_1["face_verts"][local_face_1]
    face_2 = data_2["face_verts"][local_face_2]
    best = (float('inf'), None, None)
    for tri_1 in _triangulate_polygon(data_1["verts"], face_1):
        for tri_2 in _triangulate_polygon(data_2["verts"], face_2):
            result = _triangle_triangle_min_distance(tri_1, tri_2)
            if result[0] < best[0]:
                best = result
    return best


def _minimum_shell_gap(data_1, data_2, tolerance):
    """Exact near-surface distance after expanded-BVH broad phase."""
    best = {
        "distance": float('inf'),
        "face_1": None,
        "face_2": None,
        "point_1": None,
        "point_2": None,
    }
    if data_1["gap_bvh"] is None or data_2["gap_bvh"] is None:
        return best

    candidates = data_1["gap_bvh"].overlap(data_2["gap_bvh"])
    for local_1, local_2 in candidates:
        if not (0 <= local_1 < len(data_1["face_verts"]) and 0 <= local_2 < len(data_2["face_verts"])):
            continue
        distance, p1, p2 = _polygon_pair_min_distance(data_1, local_1, data_2, local_2)
        if distance < best["distance"]:
            best.update(distance=distance, face_1=local_1, face_2=local_2, point_1=p1, point_2=p2)

    # Vertex->surface fallback also protects Blender builds whose BVH epsilon is
    # not exposed in the Python API.
    for direction in (0, 1):
        source, target = (data_1, data_2) if direction == 0 else (data_2, data_1)
        for vertex in source["verts"]:
            nearest = target["bvh"].find_nearest(vertex, tolerance) if target["bvh"] else (None, None, None, None)
            if nearest[0] is None or nearest[3] is None:
                continue
            distance = float(nearest[3])
            if distance < best["distance"]:
                if direction == 0:
                    best.update(distance=distance, face_1=None, face_2=nearest[2], point_1=vertex.copy(), point_2=nearest[0].copy())
                else:
                    best.update(distance=distance, face_1=nearest[2], face_2=None, point_1=nearest[0].copy(), point_2=vertex.copy())

    # Guaranteed edge-edge fallback independent of BVH epsilon support. Sweep on
    # X with expanded segment AABBs keeps this practical on dense shells.
    edges_1 = sorted(data_1.get("edge_segments", ()), key=lambda e: e["min"].x)
    edges_2 = sorted(data_2.get("edge_segments", ()), key=lambda e: e["min"].x)
    start_2 = 0
    for edge_1 in edges_1:
        while start_2 < len(edges_2) and edges_2[start_2]["max"].x < edge_1["min"].x - tolerance:
            start_2 += 1
        j = start_2
        while j < len(edges_2) and edges_2[j]["min"].x <= edge_1["max"].x + tolerance:
            edge_2 = edges_2[j]
            j += 1
            if edge_1["min"].y > edge_2["max"].y + tolerance or edge_1["max"].y < edge_2["min"].y - tolerance:
                continue
            if edge_1["min"].z > edge_2["max"].z + tolerance or edge_1["max"].z < edge_2["min"].z - tolerance:
                continue
            p1, p2, distance = _segment_segment_closest(
                edge_1["p0"], edge_1["p1"], edge_2["p0"], edge_2["p1"]
            )
            if distance < best["distance"]:
                best.update(
                    distance=distance,
                    face_1=(edge_1["faces"][0] if edge_1["faces"] else None),
                    face_2=(edge_2["faces"][0] if edge_2["faces"] else None),
                    point_1=p1, point_2=p2,
                )
    return best


def _degenerate_geometry(obj, length_eps=1.0e-9, area_eps=1.0e-12):
    """Return degenerate geometry indices without modifying the source mesh."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.transform(obj.matrix_world)
        bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
        bm.verts.index_update(); bm.edges.index_update(); bm.faces.index_update()
        nonfinite_verts = {
            v.index for v in bm.verts
            if not all(math.isfinite(value) for value in v.co)
        }
        zero_edges = {
            e.index for e in bm.edges
            if (e.verts[0].co - e.verts[1].co).length <= length_eps
        }
        zero_faces = {f.index for f in bm.faces if f.calc_area() <= area_eps}
        duplicate_faces = set()
        seen = {}
        for face in bm.faces:
            key = tuple(sorted(v.index for v in face.verts))
            if key in seen:
                duplicate_faces.add(face.index)
                duplicate_faces.add(seen[key])
            else:
                seen[key] = face.index
        return {
            "nonfinite_verts": nonfinite_verts,
            "zero_edges": zero_edges,
            "zero_faces": zero_faces,
            "duplicate_faces": duplicate_faces,
        }
    finally:
        bm.free()


from .self_contact_core import shared_edge_only_contact as _shared_edge_only_contact


def _self_intersections_by_shell(obj, shells, *, with_witnesses=False):
    """Detect non-adjacent triangle intersections inside each shell.

    A sweep on triangle AABBs avoids quadratic testing for distant triangles.
    Ordinary shared vertices/edges are ignored by a strict interior crossing
    test, while nontrivial overlaps even between adjacent triangles are found.
    """
    face_to_shell = {}
    for shell in shells:
        for face_index in shell["faces"]:
            face_to_shell[face_index] = shell["index"]
    result = {shell["index"]: set() for shell in shells}
    traces = {shell['index']: [] for shell in shells}
    regions = {shell['index']: [] for shell in shells}
    if not face_to_shell:
        return (result, traces, regions) if with_witnesses else result

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table(); bm.faces.index_update()
        bm.verts.ensure_lookup_table(); bm.verts.index_update()
        origin_layer = bm.faces.layers.int.new("collision_inspector_self_src")
        for face in bm.faces:
            face[origin_layer] = face.index
        if bm.faces:
            bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='FIXED', ngon_method='BEAUTY')
        bm.transform(obj.matrix_world)
        bm.verts.ensure_lookup_table(); bm.faces.ensure_lookup_table()

        per_shell = {shell["index"]: [] for shell in shells}
        for face in bm.faces:
            src = int(face[origin_layer])
            shell_index = face_to_shell.get(src)
            if shell_index is None or len(face.verts) != 3:
                continue
            coords = tuple(v.co.copy() for v in face.verts)
            ids = tuple(v.index for v in face.verts)
            mn, mx = _bbox_from_coords(coords)
            per_shell[shell_index].append({"src": src, "ids": set(ids), "indices": ids, "coords": coords, "min": mn, "max": mx})

        eps = 1.0e-10
        for shell_index, triangles in per_shell.items():
            triangles.sort(key=lambda t: t["min"].x)
            for i, first in enumerate(triangles):
                j = i + 1
                while j < len(triangles) and triangles[j]["min"].x <= first["max"].x + eps:
                    second = triangles[j]
                    j += 1
                    if first["src"] == second["src"]:
                        continue
                    if first["min"].y > second["max"].y + eps or first["max"].y < second["min"].y - eps:
                        continue
                    if first["min"].z > second["max"].z + eps or first["max"].z < second["min"].z - eps:
                        continue
                    a, b = first["coords"], second["coords"]
                    # A valid shared mesh edge is a contact, not penetration.
                    # Coplanar overlapping pairs must still go through the
                    # narrow phase; this pure test never changes the mesh.
                    if _shared_edge_only_contact(first["indices"], a, second["indices"], b):
                        continue
                    hit = _rf_coplanar_overlap(a, b)
                    if not hit:
                        for tri_a, tri_b in ((a, b), (b, a)):
                            for k in range(3):
                                if _rf_segment_tri_cross(tri_a[k], tri_a[(k + 1) % 3], *tri_b,
                                                         include_boundary=True, exclude_endpoints=True):
                                    hit = True
                                    break
                            if hit:
                                break
                    if hit:
                        result[shell_index].add(first["src"])
                        result[shell_index].add(second["src"])
                        if with_witnesses:
                            if len(traces[shell_index]) < 48:
                                segment = _triangle_intersection_trace(a, b)
                                if segment:
                                    traces[shell_index].append([tuple(float(x) for x in p) for p in segment])
                            if len(regions[shell_index]) < 48:
                                region = _triangle_coplanar_witness(a, b)
                                if region:
                                    regions[shell_index].append(region)
        return (result, traces, regions) if with_witnesses else result
    finally:
        bm.free()

# =============================================================================
# Repair engine
# =============================================================================


def _bm_update_indices(bm):
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()


def _bm_face_components(bm):
    """Connected BMesh face shells by shared edges."""
    _bm_update_indices(bm)
    pending = set(bm.faces)
    components = []
    while pending:
        seed = pending.pop()
        stack = [seed]
        faces = {seed}
        while stack:
            face = stack.pop()
            for edge in face.edges:
                for linked in edge.link_faces:
                    if linked in pending:
                        pending.remove(linked)
                        faces.add(linked)
                        stack.append(linked)
        components.append(faces)
    components.sort(key=lambda faces: min((face.index for face in faces), default=-1))
    return components


def _edge_signed_convex_violation(edge, angle_limit):
    """Violation amount for a real manifold edge under the Checker rule.

    Once a repair triangulation is committed, its diagonal is a real edge of the
    collision mesh. Therefore it is evaluated exactly like any other source edge:
    a signed inward fold below -angle_limit is invalid.
    """
    if not edge.is_valid or len(edge.link_faces) != 2:
        return 0.0
    try:
        angle = edge.calc_face_angle_signed(0.0)
    except (ValueError, RuntimeError):
        return 0.0
    if angle >= -angle_limit:
        return 0.0
    return (-angle) - angle_limit


def _edge_set_score(edges, angle_limit):
    count = 0
    severity = 0.0
    for edge in edges:
        violation = _edge_signed_convex_violation(edge, angle_limit)
        if violation > 0.0:
            count += 1
            severity += violation
    return count, severity


def _score_is_better(after, before, eps=1.0e-12):
    """Lexicographic repair score: fewer violations, then lower total severity."""
    if after[0] != before[0]:
        return after[0] < before[0]
    return after[1] < before[1] - eps


def _repair_quality_profile(obj, angle_tolerance, gap_tolerance_mm=0.2):
    """Independently re-evaluate all safety-critical properties of a repair.

    A failed/inconclusive geometric test raises, hence cannot silently COMMIT.
    This can be expensive; reliable safety takes priority over repair speed.
    """
    shells, loose_regions, isolated_verts = _extract_shell_topology(obj)
    bad_faces, metrics = _checker_convexity_by_shell(obj, shells, angle_tolerance)
    topology_bad = sum(1 for shell in shells if shell["bad_edges"])
    bad_shells = sum(1 for faces in bad_faces.values() if faces)
    bad_face_count = sum(len(faces) for faces in bad_faces.values())
    bad_edge_count = sum(metric["bad_edges"] for metric in metrics.values())
    worst = max((metric["worst_violation_deg"] for metric in metrics.values()), default=0.0)
    deg = _degenerate_geometry(obj)
    deg_count = sum(len(indices) for indices in deg.values())
    self_count = sum(len(indices) for indices in _self_intersections_by_shell(obj, shells).values())
    pair_crossing = pair_touching = pair_nesting = 0
    if len(shells) >= 2:
        tolerance = gap_tolerance_mm / 1000.0
        geo = _build_shell_collision_data(obj, shells, obj.matrix_world.translation.copy(), tolerance)
        for i, shell_a in enumerate(shells):
            a = geo[shell_a["index"]]
            if not a["bvh"]:
                continue
            for shell_b in shells[i + 1:]:
                b = geo[shell_b["index"]]
                if not b["bvh"] or not _aabb_overlap(a, b, tolerance):
                    continue
                if _shells_exactly_coincident(a, b):
                    pair_crossing += 1
                    continue
                candidates = a["bvh"].overlap(b["bvh"])
                if _real_face_overlaps(a, b, candidates):
                    pair_crossing += 1
                elif any(_polygons_coplanar_overlap(
                        a["verts"], a["face_verts"][af],
                        b["verts"], b["face_verts"][bf]) for af, bf in candidates):
                    pair_touching += 1
                elif (not shell_a["bad_edges"] and not shell_b["bad_edges"]
                      and (_is_inside_convex(a["verts"], b["verts"], b["polys"])
                           or _is_inside_convex(b["verts"], a["verts"], a["polys"]))):
                    pair_nesting += 1
                elif tolerance > 0.0:
                    gap = _minimum_shell_gap(a, b, tolerance)
                    if gap["distance"] <= tolerance:
                        pair_touching += 1
    return {
        "topology_bad": topology_bad,
        "loose_regions": len(loose_regions),
        "isolated_verts": len(isolated_verts),
        "bad_shells": bad_shells,
        "bad_faces": bad_face_count,
        "bad_edges": bad_edge_count,
        "worst_violation": worst,
        "degenerate": deg_count,
        "self_intersection_faces": self_count,
        "pair_crossing": pair_crossing,
        "pair_touching": pair_touching,
        "pair_nesting": pair_nesting,
    }


_REPAIR_NONREGRESSION_KEYS = (
    "topology_bad", "loose_regions", "isolated_verts",
    "bad_shells", "bad_faces", "bad_edges", "worst_violation",
    "degenerate", "self_intersection_faces", "pair_crossing",
    "pair_touching", "pair_nesting",
)


def _repair_cleanup_safe(after, before):
    """Reject any increased defect count or severity (Pareto-safe profile)."""
    return all(after[k] <= before[k] + (1.0e-8 if k == "worst_violation" else 0)
               for k in _REPAIR_NONREGRESSION_KEYS)


def _repair_profile_better(after, before):
    """Improvement must be monotonic in EVERY monitored risk dimension."""
    if not _repair_cleanup_safe(after, before):
        return False
    return any(after[k] < before[k] - (1.0e-8 if k == "worst_violation" else 0)
               for k in ("bad_shells", "bad_faces", "bad_edges", "worst_violation"))


def _format_quality_profile(profile):
    return (
        f"topology={profile['topology_bad']}; convex_shells={profile['bad_shells']}; "
        f"bad_edges={profile['bad_edges']}; bad_faces={profile['bad_faces']}; "
        f"worst={profile['worst_violation']:.6g}°; "
        f"degenerate={profile['degenerate']}; self={profile['self_intersection_faces']}; "
        f"cross={profile['pair_crossing']}; contact/gap={profile['pair_touching']}; "
        f"nested={profile['pair_nesting']}"
    )


def _preflight_repair_data(obj):
    """Refuse unknown data-loss cases rather than changing production assets."""
    mesh = obj.data
    if obj.library is not None or getattr(obj, 'override_library', None) is not None or mesh.library is not None:
        raise ValueError('Автоисправление заблокировано: linked/override-library данные')
    if getattr(obj, 'modifiers', None) and len(obj.modifiers):
        raise ValueError('Автоисправление заблокировано: модификаторы требуют отдельной проверки evaluated mesh')
    if mesh.shape_keys is not None:
        raise ValueError("Автоисправление заблокировано: у Mesh есть Shape Keys")
    if getattr(mesh, "has_custom_normals", False):
        raise ValueError("Автоисправление заблокировано: у Mesh есть custom split normals")
    if len(mesh.polygons) > 150000:
        raise ValueError("Слишком много граней для полной верификации ремонта (150000)")
    uv_names = {layer.name for layer in mesh.uv_layers}
    known = {"position", "corner_vert", "corner_edge", "material_index",
             "sharp_face", "sharp_edge", "crease_edge", "crease_vert", "bevel_weight_edge"}
    for attr in mesh.attributes:
        if attr.name not in known and attr.name not in uv_names and not attr.is_internal:
            raise ValueError(
                f"Автоисправление заблокировано: атрибут '{attr.name}' "
                "может пострадать при изменении топологии"
            )


def _mesh_guard_snapshot(obj):
    """Non-positional, material, UV, and seam invariants for staged repairs."""
    mesh = obj.data
    positions = Counter(tuple(float(co) for co in v.co) for v in mesh.vertices)
    uv_mapping = {}
    uv_areas = {}
    for layer in mesh.uv_layers:
        vertex_uvs = {}
        total_area = 0.0
        for face in mesh.polygons:
            polygon_uv = []
            for li in face.loop_indices:
                loop = mesh.loops[li]
                uv = tuple(float(x) for x in layer.data[li].uv)
                vertex_uvs.setdefault(tuple(float(x) for x in mesh.vertices[loop.vertex_index].co), set()).add(uv)
                polygon_uv.append(uv)
            if polygon_uv:
                total_area += abs(_polygon_signed_area_2d(polygon_uv))
        uv_mapping[layer.name] = {k: frozenset(v) for k, v in vertex_uvs.items()}
        uv_areas[layer.name] = total_area
    materials = {}
    for face in mesh.polygons:
        materials[face.material_index] = materials.get(face.material_index, 0.0) + face.area
    def edge_key(edge):
        return tuple(sorted(tuple(float(c) for c in mesh.vertices[vi].co) for vi in edge.vertices))
    protected_edges = set()
    protected_attributes = [mesh.attributes.get(name) for name in
                            ("sharp_edge", "crease_edge", "bevel_weight_edge")]
    for edge in mesh.edges:
        protected = edge.use_seam or getattr(edge, "use_edge_sharp", False)
        for attr in protected_attributes:
            if attr is not None and attr.domain == "EDGE" and bool(attr.data[edge.index].value):
                protected = True
        if protected:
            protected_edges.add(edge_key(edge))
    return {"positions": positions, "uv": uv_mapping,
            "uv_areas": uv_areas, "materials": materials,
            "protected_edges": protected_edges}


def _verify_mesh_guard(before, after, allow_loose_removal=False):
    if allow_loose_removal:
        if any(after["positions"][position] > number for position, number in before["positions"].items()):
            return False, "Вершины переместились или добавились"
        if any(position not in before["positions"] for position in after["positions"]):
            return False, "Добавлены новые координаты вершин"
    elif before["positions"] != after["positions"]:
        return False, "Нарушена инвариантность координат вершин"
    if not before["protected_edges"].issubset(after["protected_edges"]):
        return False, "Потеряны seams/sharp-edges"
    if before["uv"].keys() != after["uv"].keys():
        return False, "Изменился список UV-развёрток"
    for layer, vertex_uvs in after["uv"].items():
        for vertex, coords in vertex_uvs.items():
            if vertex not in before["uv"][layer] or coords != before["uv"][layer][vertex]:
                return False, f"UV-данные изменились в слое {layer}"
        a, b = before["uv_areas"][layer], after["uv_areas"][layer]
        if not math.isclose(a, b, rel_tol=1.0e-6, abs_tol=1.0e-8):
            return False, f"Изменилась общая UV-площадь {layer}"
    for index in set(before["materials"]) | set(after["materials"]):
        if not math.isclose(before["materials"].get(index, 0.0),
                            after["materials"].get(index, 0.0),
                            rel_tol=1.0e-6, abs_tol=1.0e-8):
            return False, f"Изменилась площадь материала {index}"
    return True, "OK"


def _repair_mesh_backup(obj):
    """Stage edits on a fresh Mesh, preserving the ORIGINAL datablock intact."""
    original = obj.data
    working = original.copy()
    try:
        obj.data = working
    except Exception:
        if working.users == 0:
            bpy.data.meshes.remove(working)
        raise
    return original, None, True


def _repair_commit_cleanup(obj, original, backup, made_single_user):
    """Commit by keeping the staged Mesh, discard unused prior datablock."""
    if original is not obj.data and original.users == 0 and original.library is None:
        bpy.data.meshes.remove(original)
    return original, made_single_user


def _repair_rollback(obj, original, backup, made_single_user):
    """Rollback restores EXACT original Mesh datablock (not a clone)."""
    current = obj.data
    obj.data = original
    if current is not original and current.users == 0:
        bpy.data.meshes.remove(current)
    if backup is not None and backup.users == 0:
        bpy.data.meshes.remove(backup)


def _repair_recalc_normals(bm):
    faces = [face for face in bm.faces if face.is_valid]
    if not faces:
        return 0
    bmesh.ops.recalc_face_normals(bm, faces=faces)
    return len(faces)


def _repair_remove_loose(bm):
    """Delete only geometry that belongs to no face."""
    _bm_update_indices(bm)
    loose_edges = [edge for edge in bm.edges if len(edge.link_faces) == 0]
    removed_edges = len(loose_edges)
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')

    _bm_update_indices(bm)
    isolated_verts = [
        vert for vert in bm.verts
        if not vert.link_edges and not vert.link_faces
    ]
    removed_verts = len(isolated_verts)
    if isolated_verts:
        bmesh.ops.delete(bm, geom=isolated_verts, context='VERTS')

    _bm_update_indices(bm)
    return removed_edges, removed_verts


def _repair_dissolve_coplanar(bm, angle_degrees):
    """Dissolve manifold internal edges whose adjacent faces are effectively flat."""
    _bm_update_indices(bm)
    limit = math.radians(max(0.0, angle_degrees))
    candidates = []
    for edge in bm.edges:
        if len(edge.link_faces) != 2 or edge.seam or not edge.smooth:
            continue
        crease = bm.edges.layers.float.get("crease_edge")
        bevel = bm.edges.layers.float.get("bevel_weight_edge")
        if (crease is not None and edge[crease] > 1.0e-9) or (bevel is not None and edge[bevel] > 1.0e-9):
            continue
        first_face, second_face = edge.link_faces
        if first_face.material_index != second_face.material_index:
            continue
        uv_layers = tuple(bm.loops.layers.uv.values())
        has_uv_boundary = False
        for layer in uv_layers:
            for vertex in edge.verts:
                loop_a = next((loop for loop in first_face.loops if loop.vert == vertex), None)
                loop_b = next((loop for loop in second_face.loops if loop.vert == vertex), None)
                if loop_a is None or loop_b is None or (loop_a[layer].uv - loop_b[layer].uv).length > 1.0e-6:
                    has_uv_boundary = True
                    break
            if has_uv_boundary:
                break
        if has_uv_boundary:
            continue
        try:
            angle = edge.calc_face_angle(0.0)
        except (ValueError, RuntimeError):
            continue
        if angle <= limit:
            candidates.append(edge)

    count = len(candidates)
    if candidates:
        bmesh.ops.dissolve_edges(
            bm,
            edges=candidates,
            use_verts=False,
            use_face_split=False,
        )
        _bm_update_indices(bm)
    return count


def _repair_basic_cleanup_bmesh(bm, settings):
    stats = {
        'normals_faces': 0,
        'loose_edges': 0,
        'loose_verts': 0,
        'coplanar_edges': 0,
    }

    if settings.repair_remove_loose:
        edges, verts = _repair_remove_loose(bm)
        stats['loose_edges'] = edges
        stats['loose_verts'] = verts

    if settings.repair_recalc_normals:
        stats['normals_faces'] = _repair_recalc_normals(bm)

    if settings.repair_dissolve_coplanar:
        stats['coplanar_edges'] = _repair_dissolve_coplanar(
            bm,
            settings.repair_coplanar_angle,
        )
        # Dissolving changes faces; normalize the result once more.
        if settings.repair_recalc_normals:
            _repair_recalc_normals(bm)

    return stats


def _target_nonconvex_shells(obj, angle_tolerance):
    """Return topology + shell ids that currently fail the Checker convexity rule."""
    shells, _loose_regions, _isolated_verts = _extract_shell_topology(obj)
    bad = _checker_bad_convex_faces_by_shell(obj, shells, angle_tolerance)
    bad_shells = {shell_index for shell_index, faces in bad.items() if faces}
    return shells, bad_shells


def _repair_triangulate_and_flip_bmesh(
    bm,
    target_face_indices,
    *,
    triangulation_method,
    angle_tolerance,
    max_attempts,
    allow_existing,
):
    """Triangulate target faces and greedily rotate diagonals that improve convexity.

    Structural edges are protected by default. The normal mode may rotate only
    edges created by this function. Existing triangle-to-triangle edges become
    candidates only when the explicit advanced option is enabled.
    """
    _bm_update_indices(bm)
    angle_limit = math.radians(angle_tolerance)
    target_face_indices = set(int(index) for index in target_face_indices)

    target_faces = [
        bm.faces[index]
        for index in sorted(target_face_indices)
        if 0 <= index < len(bm.faces) and bm.faces[index].is_valid
    ]
    if not target_faces:
        return {
            'faces_triangulated': 0,
            'generated_diagonals': 0,
            'attempts': 0,
            'accepted': 0,
            'limit_hit': False,
            'before_bad_edges': 0,
            'after_bad_edges': 0,
        }

    # Normalize winding before signed-angle decisions.
    bmesh.ops.recalc_face_normals(bm, faces=[face for face in target_faces if face.is_valid])
    _bm_update_indices(bm)

    target_vert_indices = {
        vert.index
        for face in target_faces
        if face.is_valid
        for vert in face.verts
    }

    # Existing edge identity is remembered by vertex pair. New pairs after
    # triangulation are the only edges considered generated by this repair.
    existing_pairs = {
        tuple(sorted((edge.verts[0].index, edge.verts[1].index)))
        for edge in bm.edges
        if edge.is_valid
    }

    faces_to_triangulate = [face for face in target_faces if len(face.verts) > 3]
    faces_triangulated = len(faces_to_triangulate)
    if faces_to_triangulate:
        bmesh.ops.triangulate(
            bm,
            faces=faces_to_triangulate,
            quad_method=triangulation_method,
            ngon_method='BEAUTY',
        )
        _bm_update_indices(bm)

    generated_layer = bm.edges.layers.int.get('collision_inspector_generated_diagonal')
    if generated_layer is None:
        generated_layer = bm.edges.layers.int.new('collision_inspector_generated_diagonal')

    generated = []
    for edge in bm.edges:
        if not edge.is_valid:
            continue
        pair = tuple(sorted((edge.verts[0].index, edge.verts[1].index)))
        is_generated = pair not in existing_pairs
        edge[generated_layer] = 1 if is_generated else 0
        if is_generated:
            generated.append(edge)

    generated_count = len(generated)

    initial_target_edges = {
        edge for edge in bm.edges
        if edge.is_valid
        and all(vert.index in target_vert_indices for vert in edge.verts)
    }
    before_score = _edge_set_score(initial_target_edges, angle_limit)

    # The target shell may have contained triangles already. Target membership
    # is retained through the original target vertex set captured before
    # triangulation invalidated/replaced source BMFaces.
    def candidate_edges():
        result = []
        for edge in bm.edges:
            if not edge.is_valid or len(edge.link_faces) != 2:
                continue
            if not all(len(face.verts) == 3 for face in edge.link_faces):
                continue
            if not all(vert.index in target_vert_indices for vert in edge.verts):
                continue
            if edge[generated_layer] == 1:
                result.append(edge)
                continue
            if allow_existing:
                # Advanced mode: rotate only edges fully surrounded by the target
                # shell. This still preserves all vertex positions and boundary
                # edges, but it can change the triangulated surface of a nonplanar
                # quad, so it is deliberately opt-in.
                linked_verts = {vert.index for face in edge.link_faces for vert in face.verts}
                if linked_verts.issubset(target_vert_indices):
                    result.append(edge)
        result.sort(
            key=lambda edge: _edge_signed_convex_violation(edge, angle_limit),
            reverse=True,
        )
        return result

    attempts = 0
    accepted = 0
    limit_hit = False

    while attempts < max_attempts:
        improvement_found = False
        candidates = candidate_edges()
        if not candidates:
            break

        for edge in candidates:
            if attempts >= max_attempts:
                limit_hit = True
                break
            if not edge.is_valid or len(edge.link_faces) != 2:
                continue

            attempts += 1
            old_faces = list(edge.link_faces)
            local_before_edges = {
                local_edge
                for face in old_faces
                if face.is_valid
                for local_edge in face.edges
                if local_edge.is_valid
            }
            before_local = _edge_set_score(local_before_edges, angle_limit)
            was_generated = bool(edge[generated_layer])

            try:
                result = bmesh.ops.rotate_edges(bm, edges=[edge], use_ccw=False)
            except (RuntimeError, ValueError):
                continue

            new_edge = next(
                (candidate for candidate in result.get('edges', []) if candidate.is_valid),
                None,
            )
            if new_edge is None or len(new_edge.link_faces) != 2:
                continue

            new_edge[generated_layer] = 1 if was_generated else 0
            new_faces = list(new_edge.link_faces)
            local_after_edges = {
                local_edge
                for face in new_faces
                if face.is_valid
                for local_edge in face.edges
                if local_edge.is_valid
            }
            after_local = _edge_set_score(local_after_edges, angle_limit)

            if _score_is_better(after_local, before_local):
                accepted += 1
                improvement_found = True
                _bm_update_indices(bm)
                break

            # Restore the previous diagonal when this alternative does not help.
            try:
                restored = bmesh.ops.rotate_edges(bm, edges=[new_edge], use_ccw=False)
                restored_edge = next(
                    (candidate for candidate in restored.get('edges', []) if candidate.is_valid),
                    None,
                )
                if restored_edge is not None:
                    restored_edge[generated_layer] = 1 if was_generated else 0
                _bm_update_indices(bm)
            except (RuntimeError, ValueError):
                # If Blender cannot rotate back, abort the search. The caller's
                # mesh transaction still protects the real mesh until commit.
                limit_hit = True
                break

        if limit_hit or not improvement_found:
            break

    target_edges_after = {
        edge for edge in bm.edges
        if edge.is_valid
        and any(vert.index in target_vert_indices for vert in edge.verts)
        and all(vert.index in target_vert_indices for vert in edge.verts)
    }
    after_score = _edge_set_score(target_edges_after, angle_limit)

    # This layer is an implementation detail of the repair search and must not
    # leak into the user's Mesh attributes.
    try:
        bm.edges.layers.int.remove(generated_layer)
    except (AttributeError, RuntimeError, ValueError):
        pass

    return {
        'faces_triangulated': faces_triangulated,
        'generated_diagonals': generated_count,
        'attempts': attempts,
        'accepted': accepted,
        'limit_hit': limit_hit,
        'before_bad_edges': before_score[0],
        'after_bad_edges': after_score[0],
    }


def _apply_basic_repair(obj, settings):
    """Apply non-positional cleanup in one BMesh commit."""
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        stats = _repair_basic_cleanup_bmesh(bm, settings)
        bm.to_mesh(obj.data)
        obj.data.update()
        return stats
    finally:
        bm.free()


def _apply_triangulation_repair(obj, settings, include_basic_cleanup=False,
                                target_shell_indices=None, attempt_limit=None,
                                allow_existing=None):
    shells, bad_shells = _target_nonconvex_shells(obj, settings.convex_angle)
    if target_shell_indices is not None:
        bad_shells.intersection_update(set(target_shell_indices))
    if not bad_shells:
        return {
            'faces_triangulated': 0,
            'generated_diagonals': 0,
            'attempts': 0,
            'accepted': 0,
            'limit_hit': False,
            'before_bad_edges': 0,
            'after_bad_edges': 0,
            'target_shells': 0,
            'cleanup': None,
        }

    target_faces = set()
    for shell in shells:
        if shell['index'] in bad_shells:
            target_faces.update(shell['faces'])

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        cleanup_stats = None
        if include_basic_cleanup:
            cleanup_stats = _repair_basic_cleanup_bmesh(bm, settings)
            # Cleanup may have changed indices/topology. In auto mode it is safer
            # to optimize all current faces rather than trust stale face indices.
            _bm_update_indices(bm)
            target_faces = {face.index for face in bm.faces}

        stats = _repair_triangulate_and_flip_bmesh(
            bm,
            target_faces,
            triangulation_method=settings.repair_triangulation_method,
            angle_tolerance=settings.convex_angle,
            max_attempts=(settings.repair_max_flip_attempts if attempt_limit is None else attempt_limit),
            allow_existing=(settings.repair_allow_existing_diagonals if allow_existing is None else allow_existing),
        )
        stats['target_shells'] = len(bad_shells)
        stats['cleanup'] = cleanup_stats
        bm.to_mesh(obj.data)
        obj.data.update()
        return stats
    finally:
        bm.free()


def _format_cleanup_stats(stats):
    return (
        f"loose edges {stats['loose_edges']}; loose verts {stats['loose_verts']}; "
        f"coplanar edges {stats['coplanar_edges']}; normals faces {stats['normals_faces']}"
    )


def _format_triangulation_stats(stats):
    suffix = ' (достигнут лимит)' if stats.get('limit_hit') else ''
    return (
        f"оболочек {stats.get('target_shells', 0)}; "
        f"триангулировано граней {stats.get('faces_triangulated', 0)}; "
        f"создано диагоналей {stats.get('generated_diagonals', 0)}; "
        f"проверено перестановок {stats.get('attempts', 0)}; "
        f"принято {stats.get('accepted', 0)}; "
        f"локальных нарушений {stats.get('before_bad_edges', 0)} -> "
        f"{stats.get('after_bad_edges', 0)}{suffix}"
    )

# =============================================================================
# Non-mutating diagonal preview and gated application (Pass 3.5)
# =============================================================================


def _temporary_repair_object(source):
    """Unlinked Blender object + copied Mesh; original cannot be modified."""
    preview = source.copy()
    mesh = None
    try:
        mesh = source.data.copy()
        preview.data = mesh
        preview.matrix_world = source.matrix_world.copy()
        return preview, mesh
    except BaseException:
        bpy.data.objects.remove(preview, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise


def _destroy_temporary_repair_object(preview, mesh):
    bpy.data.objects.remove(preview, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _find_shell_with_vertex(obj, vertex_id):
    shells, _loose, _isolated = _extract_shell_topology(obj)
    found = [shell['index'] for shell in shells if vertex_id in shell['verts']]
    # Require exactly one face-connected shell to avoid ambiguous proposals.
    return found[0] if len(found) == 1 else None


def _preview_diagonal_candidates(settings, obj, signature):
    findings = [ev for ev in settings.events if ev.kind == 'NONCONVEX']
    for ev in findings:
        ev.repair_available = False
        ev.repair_reason = 'Поиск не выполнялся'
        ev.repair_plan = ''
        ev.repair_preview_lines = ''
    if not findings or not settings.repair_preview_enabled:
        return
    try:
        _preflight_repair_data(obj)
    except (ValueError, RuntimeError) as exc:
        for ev in findings:
            ev.repair_reason = str(exc)[:240]
        return
    if len(obj.data.polygons) > settings.repair_preview_max_faces:
        for ev in findings:
            ev.repair_reason = 'Пропущено: большая сетка, измените лимит предварительного просмотра'
        return
    # The checker can find genuine concavities: re-triangulating them may not
    # be a solution. Only a FULL independent non-regression verdict is offered.
    try:
        base_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
        before_guard = _mesh_guard_snapshot(obj)
    except Exception as exc:
        for ev in findings:
            ev.repair_reason = f'Не удалось проверить безопасность: {type(exc).__name__}'
        return
    for index, ev in enumerate(findings):
        if index >= settings.repair_preview_max_shells:
            ev.repair_reason = 'Пропущено: превышен лимит оболочек для пробы'
            continue
        preview = mesh = None
        try:
            shells, _, _ = _extract_shell_topology(obj)
            source_shell = next(x for x in shells if x['index'] == ev.shell_a)
            if not source_shell['verts']:
                ev.repair_reason = 'Оболочка без вершин'
                continue
            anchor = min(source_shell['verts'])
            preview, mesh = _temporary_repair_object(obj)
            source_edge_pairs = {
                tuple(sorted(edge.vertices)) for edge in obj.data.edges
            }
            stats = _apply_triangulation_repair(
                preview, settings, target_shell_indices={ev.shell_a},
                attempt_limit=settings.repair_preview_attempts,
                allow_existing=False,  # Never change existing structural edges automatically.
            )
            after = _repair_quality_profile(preview, settings.convex_angle, settings.gap_tolerance_mm)
            guard_ok, guard_reason = _verify_mesh_guard(before_guard, _mesh_guard_snapshot(preview))
            repaired_id = _find_shell_with_vertex(preview, anchor)
            bad_after = set(_target_nonconvex_shells(preview, settings.convex_angle)[1])
            improved = (guard_ok and _repair_profile_better(after, base_profile)
                        and repaired_id is not None and repaired_id not in bad_after)
            if improved:
                plan = {
                    'source_signature': signature,
                    'source_data_signature': _guard_digest(before_guard),
                    'anchor_vertex': anchor,
                    'method': settings.repair_triangulation_method,
                    'attempt_limit': settings.repair_preview_attempts,
                    'angle_tolerance': settings.convex_angle,
                    'gap_tolerance_mm': settings.gap_tolerance_mm,
                    'expected_signature': _mesh_topology_fingerprint(preview.data),
                    'before_bad_edges': base_profile['bad_edges'],
                    'after_bad_edges': after['bad_edges'],
                    'before_bad_shells': base_profile['bad_shells'],
                    'after_bad_shells': after['bad_shells'],
                    'stats': stats,
                }
                ev.repair_plan = json.dumps(plan, ensure_ascii=False, sort_keys=True)
                new_pairs = sorted({tuple(sorted(edge.vertices)) for edge in preview.data.edges}
                                   - source_edge_pairs)
                segments = []
                for a, b in new_pairs[:48]:
                    pa = preview.matrix_world @ preview.data.vertices[a].co
                    pb = preview.matrix_world @ preview.data.vertices[b].co
                    segments.append([list(map(float, pa)), list(map(float, pb))])
                ev.repair_preview_lines = json.dumps(segments)
                ev.repair_available = True
                ev.repair_reason = (
                    f"Диагонали: {stats['generated_diagonals']} новых, "
                    f"{stats['accepted']} перестановок; проблемных рёбер "
                    f"{base_profile['bad_edges']} → {after['bad_edges']}. "
                    'Только предпросмотр, Mesh не изменён.'
                )
                settings.repair_preview_count += 1
            else:
                ev.repair_reason = ('Проба отклонена: ' + (
                    guard_reason if not guard_ok else
                    'невыпуклость не устранена либо ухудшился другой показатель'))
        except Exception as exc:
            ev.repair_reason = f'Безопасный вариант не подтверждён: {type(exc).__name__}: {str(exc)[:100]}'
        finally:
            if preview is not None:
                _destroy_temporary_repair_object(preview, mesh)


def _guard_digest(guard):
    """Deterministic state fingerprint for UV, material, protected-edge guards."""
    digest = hashlib.blake2b(digest_size=20)
    normalized = (
        sorted(guard['positions'].items()),
        sorted((name, sorted((co, sorted(uvs)) for co, uvs in values.items()))
               for name, values in guard['uv'].items()),
        sorted(guard['uv_areas'].items()),
        sorted(guard['materials'].items()),
        sorted(guard['protected_edges']),
    )
    digest.update(repr(normalized).encode('utf-8', errors='replace'))
    return digest.hexdigest()


def _mesh_topology_fingerprint(mesh):
    """Coordinate/face/UV fingerprint of a candidate, independent of object name."""
    digest = hashlib.blake2b(digest_size=20)
    digest.update(struct.pack('<III', len(mesh.vertices), len(mesh.edges), len(mesh.polygons)))
    for vertex in mesh.vertices:
        for c in vertex.co:
            _update_digest_float(digest, c)
    for polygon in mesh.polygons:
        digest.update(struct.pack('<II', len(polygon.vertices), polygon.material_index))
        for index in polygon.vertices:
            digest.update(struct.pack('<I', index))
    return digest.hexdigest()


def _plans_for_current_object(settings, obj):
    signature = _object_signature(obj)
    if not settings.analysis_signature or signature != settings.analysis_signature:
        raise ValueError('Результаты устарели: повторите проверку перед исправлением')
    if obj.name != settings.object_name:
        raise ValueError('Активный объект не соответствует проверенному')
    return [(event, json.loads(event.repair_plan)) for event in settings.events
            if event.kind == 'NONCONVEX' and event.repair_available and event.repair_plan]


def _apply_approved_diagonals(obj, settings, proposals, *, bulk=False):
    """Single user-confirmed mesh transaction. Commit only independently safe steps."""
    _preflight_repair_data(obj)
    original_sig = _object_signature(obj)
    for event, plan in proposals:
        if event.signature != original_sig or plan.get('source_signature') != original_sig:
            raise ValueError('План устарел, нужна новая проверка')
        if plan['method'] != settings.repair_triangulation_method or plan['angle_tolerance'] != settings.convex_angle or plan['gap_tolerance_mm'] != settings.gap_tolerance_mm:
            raise ValueError('Параметры изменены, повторите проверку')
    original_guard = _mesh_guard_snapshot(obj)
    current_guard_digest = _guard_digest(original_guard)
    if any(plan.get('source_data_signature') != current_guard_digest
           for event, plan in proposals):
        raise ValueError('UV/материалы/защищённые рёбра изменились после проверки; повторите проверку')
    original_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
    original = backup = None
    applied = []
    skipped = []
    try:
        original, backup, made_single = _repair_mesh_backup(obj)
        current_profile = original_profile
        for event, plan in proposals:
            # Make each candidate reversible independently, even in batch mode.
            step_previous = obj.data.copy()
            try:
                target = _find_shell_with_vertex(obj, plan['anchor_vertex'])
                if target is None:
                    raise ValueError('Неоднозначная идентификация оболочки')
                stats = _apply_triangulation_repair(
                    obj, settings, target_shell_indices={target},
                    attempt_limit=plan['attempt_limit'], allow_existing=False,
                )
                after = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
                ok, reason = _verify_mesh_guard(original_guard, _mesh_guard_snapshot(obj))
                final_shell = _find_shell_with_vertex(obj, plan['anchor_vertex'])
                bad_after = set(_target_nonconvex_shells(obj, settings.convex_angle)[1])
                if not ok or not _repair_profile_better(after, current_profile) or final_shell in bad_after or final_shell is None:
                    raise ValueError('Отклонено защитой: ' + (reason if not ok else 'нет доказанного улучшения'))
                # A single proposal must reproduce exactly the inspected plan.
                # Batch outcomes are individually revalidated because preceding
                # fixes can change the face order, so their digests may differ.
                if not bulk and _mesh_topology_fingerprint(obj.data) != plan['expected_signature']:
                    raise ValueError('Результат отличается от предварительно проверенного')
                current_profile = after
                applied.append((event.shell_a, stats))
            except Exception as exc:
                current = obj.data
                obj.data = step_previous
                if current.users == 0:
                    bpy.data.meshes.remove(current)
                skipped.append((event.shell_a, str(exc)))
                if not bulk:
                    raise
                continue
            finally:
                if step_previous.users == 0:
                    bpy.data.meshes.remove(step_previous)
        if not applied:
            raise ValueError('Ни один вариант не прошёл проверку: ' + str(skipped[:2]))
        final_guard, guard_reason = _verify_mesh_guard(original_guard, _mesh_guard_snapshot(obj))
        final_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
        if not final_guard or not _repair_profile_better(final_profile, original_profile):
            raise ValueError('Общий контроль отклонил изменения: ' + guard_reason)
        _repair_commit_cleanup(obj, original, backup, made_single)
        return applied, skipped, original_profile, final_profile
    except Exception:
        if original is not None:
            _repair_rollback(obj, original, backup, True)
        raise


# =============================================================================
# Events / analysis
# =============================================================================


def _event_add(
    settings,
    *,
    kind,
    obj,
    shell_a=-1,
    shell_b=-1,
    text,
    detail="",
    indices_a=(),
    indices_b=(),
    select_mode="OBJECT",
    signature="",
    metric_value=0.0,
    metric_limit=0.0,
    witness_a=None,
    witness_b=None,
    trace_segments=(),
    overlap_polygons=(),
):
    event = settings.events.add()
    event.kind = kind
    event.obj_name = obj.name
    event.shell_a = int(shell_a)
    event.shell_b = int(shell_b)
    event.text = text
    event.detail = detail
    event.indices_a = _indices_to_string(indices_a)
    event.indices_b = _indices_to_string(indices_b)
    event.select_mode = select_mode
    event.signature = signature or _object_signature(obj)
    event.metric_value = float(metric_value)
    event.metric_limit = float(metric_limit)
    event.trace_segments = json.dumps(trace_segments) if trace_segments else ""
    event.overlap_polygons = json.dumps(overlap_polygons) if overlap_polygons else ""
    if witness_a is not None and witness_b is not None:
        event.witness_a = tuple(float(x) for x in witness_a)
        event.witness_b = tuple(float(x) for x in witness_b)
        event.has_witness_pair = True
    return event


def _analyse_object(settings, obj, analysis_signature=""):
    shells, loose_regions, isolated_verts = _extract_shell_topology(obj)
    degenerate = _degenerate_geometry(obj) if settings.check_degenerate else {
        "nonfinite_verts": set(), "zero_edges": set(), "zero_faces": set(), "duplicate_faces": set()
    }
    invalid_pair_shells = set()
    bad_deg_faces = degenerate["zero_faces"] | degenerate["duplicate_faces"]
    for shell in shells:
        if (set(shell["verts"]) & degenerate["nonfinite_verts"]
                or set(shell["edges"]) & degenerate["zero_edges"]
                or set(shell["faces"]) & bad_deg_faces):
            invalid_pair_shells.add(shell["index"])

    safe_shells = [shell for shell in shells if shell["index"] not in invalid_pair_shells]
    bad_convex_by_shell, convex_metrics = _checker_convexity_by_shell(
        obj, safe_shells, settings.convex_angle
    )
    if settings.check_self_intersections and safe_shells:
        self_intersections, self_traces, self_regions = _self_intersections_by_shell(
            obj, safe_shells, with_witnesses=True)
    else:
        self_intersections, self_traces, self_regions = {}, {}, {}

    counts = {kind: 0 for kind in (
        "UNCLOSED", "NONMANIFOLD", "DEGENERATE", "NONCONVEX",
        "SELF_INTERSECTION", "INTERSECTION", "NESTED", "GAP",
        "CONTACT", "COINCIDENT", "SURFACE_OVERLAP",
    )}
    settings.shell_count = len(shells)
    settings.loose_region_count = len(loose_regions)
    settings.isolated_vert_count = len(isolated_verts)
    settings.pairs_examined = 0

    # Degenerate preflight is object-wide because a bad primitive may not belong
    # to a valid shell at all.
    if degenerate["nonfinite_verts"]:
        counts["DEGENERATE"] += 1
        _event_add(settings, kind="DEGENERATE", obj=obj,
            text="Некорректные координаты вершин",
            detail=f"NaN/Inf вершин: {len(degenerate['nonfinite_verts'])}.",
            indices_a=degenerate["nonfinite_verts"], select_mode="VERT",
            signature=analysis_signature)
    if degenerate["zero_edges"]:
        counts["DEGENERATE"] += 1
        _event_add(settings, kind="DEGENERATE", obj=obj,
            text="Рёбра нулевой длины",
            detail=f"Рёбер практически нулевой длины: {len(degenerate['zero_edges'])}.",
            indices_a=degenerate["zero_edges"], select_mode="EDGE",
            signature=analysis_signature)
    if bad_deg_faces:
        counts["DEGENERATE"] += 1
        _event_add(settings, kind="DEGENERATE", obj=obj,
            text="Вырожденные/дублирующиеся полигоны",
            detail=(f"Zero-area: {len(degenerate['zero_faces'])}; "
                    f"duplicate faces: {len(degenerate['duplicate_faces'])}."),
            indices_a=bad_deg_faces, select_mode="FACE", signature=analysis_signature)

    valid_convex_shells = set()
    for shell in shells:
        shell_index = shell["index"]
        bad_edges = set(shell["bad_edges"])
        euler = shell["euler"]
        boundary_edges = set(shell["boundary_edges"])
        nonmanifold_edges = bad_edges - boundary_edges
        if boundary_edges:
            counts["UNCLOSED"] += 1
            _event_add(settings, kind="UNCLOSED", obj=obj, shell_a=shell_index,
                text=f"Оболочка {shell_index}: открытые границы",
                detail=(f"Граничных рёбер: {len(boundary_edges)}; "
                        f"V−E+F={euler} — справочная метрика, не тест замкнутости."),
                indices_a=boundary_edges, select_mode="EDGE", signature=analysis_signature)
        if nonmanifold_edges:
            counts["NONMANIFOLD"] += 1
            _event_add(settings, kind="NONMANIFOLD", obj=obj, shell_a=shell_index,
                text=f"Оболочка {shell_index}: non-manifold",
                detail=f"Рёбер с числом соседних граней != 2: {len(nonmanifold_edges)}.",
                indices_a=nonmanifold_edges, select_mode="EDGE", signature=analysis_signature)

        # Even for open or non-manifold shells, keep checking all metrics which
        # are still interpretable (never silently suppress additional defects).
        if shell_index in invalid_pair_shells:
            continue

        bad_faces = bad_convex_by_shell.get(shell_index, set())
        if bad_faces:
            counts["NONCONVEX"] += 1
            metric = convex_metrics.get(shell_index, {})
            worst = float(metric.get("worst_violation_deg", 0.0))
            angle = float(metric.get("worst_angle_deg", 0.0))
            _event_add(settings, kind="NONCONVEX", obj=obj, shell_a=shell_index,
                text=f"Оболочка {shell_index}: невыпуклая",
                detail=(f"Проблемных рёбер: {metric.get('bad_edges', 0)}; "
                        f"исходных полигонов: {len(bad_faces)}. "
                        f"Худший угол: {angle:.6g}°; превышение допуска: {worst:.6g}°."),
                indices_a=bad_faces, select_mode="FACE", signature=analysis_signature,
                metric_value=worst, metric_limit=settings.convex_angle)
        elif not bad_edges and not self_intersections.get(shell_index):
            valid_convex_shells.add(shell_index)

        self_bad = self_intersections.get(shell_index, set())
        if self_bad:
            counts["SELF_INTERSECTION"] += 1
            _event_add(settings, kind="SELF_INTERSECTION", obj=obj, shell_a=shell_index,
                text=f"Оболочка {shell_index}: самопересечение",
                detail=f"Полигонов в подтверждённых внутренних пересечениях: {len(self_bad)}.",
                indices_a=self_bad, select_mode="FACE", signature=analysis_signature,
                trace_segments=self_traces.get(shell_index, ()),
                overlap_polygons=self_regions.get(shell_index, ()))

    for region_index, edge_indices in enumerate(loose_regions, start=1):
        counts["UNCLOSED"] += 1
        _event_add(settings, kind="UNCLOSED", obj=obj, shell_a=-1,
            text=f"Свободные рёбра: область {region_index}",
            detail=f"Рёбер без полигонов: {len(edge_indices)}.", indices_a=edge_indices,
            select_mode="EDGE", signature=analysis_signature)
    for number, vert_index in enumerate(isolated_verts, start=1):
        counts["UNCLOSED"] += 1
        _event_add(settings, kind="UNCLOSED", obj=obj, shell_a=-1,
            text=f"Отдельная вершина {number}", detail="Вершина не принадлежит ни одному ребру/полигону.",
            indices_a=[vert_index], select_mode="VERT", signature=analysis_signature)

    pair_shells = [shell for shell in shells if shell["index"] not in invalid_pair_shells]
    if len(pair_shells) >= 2:
        origin = obj.matrix_world.translation.copy()
        tolerance = settings.gap_tolerance_mm / 1000.0
        collision_data = _build_shell_collision_data(obj, pair_shells, origin, tolerance)
        for first_index in range(len(pair_shells)):
            shell_1 = pair_shells[first_index]
            data_1 = collision_data[shell_1["index"]]
            if data_1["bvh"] is None:
                continue
            for second_index in range(first_index + 1, len(pair_shells)):
                shell_2 = pair_shells[second_index]
                data_2 = collision_data[shell_2["index"]]
                if data_2["bvh"] is None or not _aabb_overlap(data_1, data_2, tolerance):
                    continue
                settings.pairs_examined += 1
                shell_a, shell_b = shell_1["index"], shell_2["index"]

                if _shells_exactly_coincident(data_1, data_2):
                    counts["COINCIDENT"] += 1
                    _event_add(settings, kind="COINCIDENT", obj=obj,
                        shell_a=shell_a, shell_b=shell_b,
                        text=f"Оболочки {shell_a} и {shell_b}: совпадают",
                        detail="Совпадает полный набор граней и координат; это не вложение.",
                        indices_a=set(data_1["face_orig"]),
                        indices_b=set(data_2["face_orig"]),
                        select_mode="FACE", signature=analysis_signature)
                    continue

                candidates = data_1["bvh"].overlap(data_2["bvh"])
                overlap = _real_face_overlaps(data_1, data_2, candidates)
                if overlap and _aabb_has_positive_shared_volume(data_1, data_2):
                    faces_1 = {data_1["face_orig"][a] for a, _ in overlap}
                    faces_2 = {data_2["face_orig"][b] for _, b in overlap}
                    counts["INTERSECTION"] += 1
                    _event_add(settings, kind="INTERSECTION", obj=obj, shell_a=shell_a, shell_b=shell_b,
                        text=f"Оболочки {shell_a} и {shell_b}: пересечение",
                        detail=f"Подтверждено реальное проникновение; пар граней: {len(overlap)}.",
                        indices_a=faces_1, indices_b=faces_2, select_mode="FACE", signature=analysis_signature,
                        trace_segments=_overlap_trace_segments(data_1, data_2, overlap, origin))
                    continue

                # BVH.overlap() is not a complete coplanar face-pair query.
                # In RC5, 11-01/11-02 were incorrectly classified CONTACT.
                # Supplement its candidates with a 3D AABB sweep before exact
                # positive-area clipping. AABB hits alone are never evidence.
                coplanar_candidates = set(candidates)
                coplanar_candidates.update(_face_aabb_pairs(
                    data_1["verts"], data_1["face_verts"],
                    data_2["verts"], data_2["face_verts"],
                ))
                coplanar_pairs = [
                    (a, b) for a, b in sorted(coplanar_candidates)
                    if _polygons_coplanar_overlap(
                        data_1["verts"], data_1["face_verts"][a],
                        data_2["verts"], data_2["face_verts"][b],
                    )
                ]
                if coplanar_pairs:
                    counts["SURFACE_OVERLAP"] += 1
                    _event_add(settings, kind="SURFACE_OVERLAP", obj=obj,
                        shell_a=shell_a, shell_b=shell_b,
                        text=f"Оболочки {shell_a} и {shell_b}: совпадение участков поверхности",
                        detail=f"Пар копланарно перекрывающихся граней: {len(coplanar_pairs)}.",
                        indices_a={data_1["face_orig"][a] for a, _ in coplanar_pairs},
                        indices_b={data_2["face_orig"][b] for _, b in coplanar_pairs},
                        select_mode="FACE", signature=analysis_signature,
                        overlap_polygons=_coplanar_witness_polygons(data_1, data_2,
                            coplanar_pairs, origin))
                    continue

                inner_data = outer_data = None
                if shell_a in valid_convex_shells and _is_inside_convex(
                    data_2["verts"], data_1["verts"], data_1["polys"]
                ):
                    inner_data, outer_data = data_2, data_1
                elif shell_b in valid_convex_shells and _is_inside_convex(
                    data_1["verts"], data_2["verts"], data_2["polys"]
                ):
                    inner_data, outer_data = data_1, data_2
                if inner_data is not None:
                    inner_shell = inner_data["shell"]["index"]
                    outer_shell = outer_data["shell"]["index"]
                    counts["NESTED"] += 1
                    _event_add(settings, kind="NESTED", obj=obj, shell_a=inner_shell, shell_b=outer_shell,
                        text=f"Оболочка {inner_shell} целиком внутри оболочки {outer_shell}",
                        detail="Внешняя оболочка замкнута и выпукла; вложение подтверждено плоскостями hull.",
                        indices_a=set(inner_data["face_orig"]),
                        indices_b=set(outer_data["face_orig"]),
                        select_mode="FACE", signature=analysis_signature)
                    continue

                if tolerance > 0.0:
                    gap = _minimum_shell_gap(data_1, data_2, tolerance)
                    if gap["distance"] <= tolerance:
                        faces_1 = set()
                        faces_2 = set()
                        if gap["face_1"] is not None and 0 <= gap["face_1"] < len(data_1["face_orig"]):
                            faces_1.add(data_1["face_orig"][gap["face_1"]])
                        if gap["face_2"] is not None and 0 <= gap["face_2"] < len(data_2["face_orig"]):
                            faces_2.add(data_2["face_orig"][gap["face_2"]])
                        distance_mm = gap["distance"] * 1000.0
                        linear_epsilon = 1.0e-9
                        is_contact = gap["distance"] <= linear_epsilon
                        if is_contact or settings.check_gaps:
                            kind = "CONTACT" if is_contact else "GAP"
                            counts[kind] += 1
                            label = "касаются" if is_contact else f"зазор {distance_mm:.4g} мм"
                            detail = (
                                "Поверхности имеют нулевое расстояние; "
                                "проверьте касание/наложение вручную."
                                if is_contact else
                                f"Минимальный зазор: {distance_mm:.6g} мм; требуется ≥ "
                                f"{settings.gap_tolerance_mm:.6g} мм."
                            )
                            _event_add(settings, kind=kind, obj=obj,
                                shell_a=shell_a, shell_b=shell_b,
                                text=f"Оболочки {shell_a} и {shell_b}: {label}",
                                detail=detail, indices_a=faces_1, indices_b=faces_2,
                                select_mode="FACE", signature=analysis_signature,
                                metric_value=distance_mm, metric_limit=settings.gap_tolerance_mm,
                                witness_a=(gap['point_1'] + origin if gap['point_1'] is not None else None),
                                witness_b=(gap['point_2'] + origin if gap['point_2'] is not None else None))
    return counts

def _reset_results(settings):
    _viewport_ui.invalidate()
    settings.events.clear()
    settings.repair_preview_count = 0
    settings.event_index = 0
    settings.last_error = ""
    settings.analysis_signature = ""
    settings.object_name = ""
    settings.shell_count = 0
    settings.loose_region_count = 0
    settings.isolated_vert_count = 0
    settings.pairs_examined = 0
    settings.unclosed_count = 0
    settings.nonmanifold_count = 0
    settings.nonconvex_count = 0
    settings.degenerate_count = 0
    settings.self_intersection_count = 0
    settings.intersection_count = 0
    settings.nested_count = 0
    settings.gap_count = 0
    settings.contact_count = 0
    settings.coincident_count = 0
    settings.surface_overlap_count = 0


def _run_analysis(context, operator=None, focus_first=True):
    settings = context.scene.collision_inspector

    if not _supported_version():
        message = "Поддерживаются Blender 4.0–5.x (проверка по каждой версии обязательна)"
        _rf_record_decision(_RFDecision(_RF_SETUP_REQUIRED, 'COLLISION_BLENDER_VERSION', message, {}, 'COLLISION_INSPECTOR', ''), source='CollisionInspector.analysis')
        settings.status = message
        if operator:
            operator.report({"ERROR"}, message)
        return False

    # A check must never unexpectedly change Edit/Object mode or selection.
    if context.mode != 'OBJECT':
        message = "Проверка выполняется в Object Mode. Режим и выделение не изменены."
        _rf_record_decision(_RFDecision(_RF_SETUP_REQUIRED, 'COLLISION_OBJECT_MODE_REQUIRED', message, {}, 'COLLISION_INSPECTOR', ''), source='CollisionInspector.analysis')
        settings.status = message
        if operator:
            operator.report({"WARNING"}, message)
        return False

    obj = _active_selected_mesh(context, operator)
    if obj is None:
        settings.status = "Выберите активный Mesh-объект"
        _rf_record_decision(_RFDecision(_RF_SETUP_REQUIRED, 'COLLISION_ACTIVE_MESH_REQUIRED', settings.status, {}, 'COLLISION_INSPECTOR', ''), source='CollisionInspector.analysis')
        return False

    _reset_results(settings)
    settings.object_name = obj.name

    if not obj.data.vertices and not obj.data.edges and not obj.data.polygons:
        settings.status = "В выбранном объекте нет геометрии"
        _rf_record_decision(_RFDecision(_RF_SETUP_REQUIRED, 'COLLISION_EMPTY_MESH', settings.status, {}, 'COLLISION_INSPECTOR', ''), source='CollisionInspector.analysis')
        if operator:
            operator.report({"WARNING"}, settings.status)
        return False

    try:
        analysis_signature = _object_signature(obj)
        counts = _analyse_object(settings, obj, analysis_signature)
        # Trial repairs are sandboxed and optional; one failed preview must not
        # discard the actual inspection findings or change the original Mesh.
        _preview_diagonal_candidates(settings, obj, analysis_signature)
        settings.unclosed_count = counts["UNCLOSED"]
        settings.nonmanifold_count = counts["NONMANIFOLD"]
        settings.nonconvex_count = counts["NONCONVEX"]
        settings.degenerate_count = counts["DEGENERATE"]
        settings.self_intersection_count = counts["SELF_INTERSECTION"]
        settings.intersection_count = counts["INTERSECTION"]
        settings.nested_count = counts["NESTED"]
        settings.gap_count = counts["GAP"]
        settings.contact_count = counts["CONTACT"]
        settings.coincident_count = counts["COINCIDENT"]
        settings.surface_overlap_count = counts["SURFACE_OVERLAP"]
        settings.analysis_signature = analysis_signature
        if settings.events:
            filtered_first = next((i for i, item in enumerate(settings.events)
                                   if _visible_issue(item.kind, settings.issue_filter)), None)
            if filtered_first is not None and not _visible_issue(settings.events[settings.event_index].kind, settings.issue_filter):
                settings.event_index = filtered_first

        total = len(settings.events)
        if total:
            settings.status = f"Найдено проблем: {total}"
            if operator:
                operator.report({"WARNING"}, settings.status)
            _viewport_ui.invalidate()
            _viewport_ui.redraw()
            if focus_first and settings.auto_zoom:
                ok, reason = _viewport_ui.frame_event(context, settings.events[0])
                if not ok and operator:
                    operator.report({"INFO"}, reason)
        else:
            settings.status = "Проверка пройдена: ошибок не найдено"
            if operator:
                operator.report({"INFO"}, settings.status)

        _viewport_ui.invalidate()
        return True

    except Exception as exc:
        # No partial result may be presented as a valid inspection report.
        error_text = traceback.format_exc()
        _reset_results(settings)
        settings.last_error = error_text
        # A thrown exception is not proof that the user's geometry is invalid.
        # Legacy geometry-sounding text is deliberately classified conservatively.
        kind = _rf_classify_legacy(str(exc))
        if kind == 'PROVEN_INVALID':
            # Unexpected exceptions do not carry a reproducible witness here, so do
            # not upgrade them to a hard geometry verdict.
            kind = _RF_INTERNAL_ERROR
        settings.status = f"Внутренняя ошибка проверки: {type(exc).__name__}: {exc}"
        _rf_record_decision(_RFDecision(kind, 'COLLISION_ANALYSIS_EXCEPTION', str(exc),
                                        {'exception_type': type(exc).__name__},
                                        'COLLISION_INSPECTOR', ''),
                            source='CollisionInspector.analysis')
        if operator:
            operator.report({"ERROR"}, settings.status)
        print(settings.last_error)
        return False


# =============================================================================
# View / selection
# =============================================================================


def _prepare_target_visibility(bm, indices, mode):
    if mode == "VERT":
        for index in indices:
            if 0 <= index < len(bm.verts):
                bm.verts[index].hide_set(False)

    elif mode == "EDGE":
        for index in indices:
            if 0 <= index < len(bm.edges):
                edge = bm.edges[index]
                edge.hide_set(False)
                for vert in edge.verts:
                    vert.hide_set(False)

    elif mode == "FACE":
        for index in indices:
            if 0 <= index < len(bm.faces):
                face = bm.faces[index]
                face.hide_set(False)
                for edge in face.edges:
                    edge.hide_set(False)
                for vert in face.verts:
                    vert.hide_set(False)


def _select_indices_in_edit_mesh(obj, indices, mode):
    if obj.mode != "EDIT":
        return 0

    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    for vert in bm.verts:
        vert.select_set(False)
    for edge in bm.edges:
        edge.select_set(False)
    for face in bm.faces:
        face.select_set(False)

    _prepare_target_visibility(bm, indices, mode)

    selected = 0
    if mode == "VERT":
        for index in indices:
            if 0 <= index < len(bm.verts):
                bm.verts[index].select_set(True)
                selected += 1

    elif mode == "EDGE":
        for index in indices:
            if 0 <= index < len(bm.edges):
                bm.edges[index].select_set(True)
                selected += 1

    elif mode == "FACE":
        for index in indices:
            if 0 <= index < len(bm.faces):
                bm.faces[index].select_set(True)
                selected += 1

    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return selected


def _focus_event(context, event, operator=None, side="BOTH"):
    obj = bpy.data.objects.get(event.obj_name)
    if obj is None or obj.type != "MESH":
        if operator:
            operator.report({"WARNING"}, "Проверенный объект больше не найден")
        return False

    if _object_signature(obj) != event.signature:
        if operator:
            operator.report({"WARNING"}, "Проверка устарела. Выделение не изменено; нажмите Перепроверить")
        return False
    if not _safe_object_mode(context):
        if operator:
            operator.report({"WARNING"}, "Не удалось перейти в Object Mode")
        return False

    _deselect_other_objects(context, obj)
    is_current = True

    indices_a = _string_to_indices(event.indices_a)
    indices_b = _string_to_indices(event.indices_b)
    if side == "A":
        indices = indices_a
    elif side == "B":
        indices = indices_b
    else:
        indices = indices_a | indices_b
    mode = event.select_mode
    have_elements = bool(indices) and mode in {"VERT", "EDGE", "FACE"}

    area, region = _view3d_area_region(context)
    if area is None or region is None:
        if operator:
            operator.report({"INFO"}, "Объект выделен; 3D Viewport для автоматического зума не найден")
        return True

    with context.temp_override(
        window=context.window,
        screen=context.screen,
        area=area,
        region=region,
    ):
        if have_elements and is_current:
            try:
                if mode == "VERT":
                    context.tool_settings.mesh_select_mode = (True, False, False)
                elif mode == "EDGE":
                    context.tool_settings.mesh_select_mode = (False, True, False)
                else:
                    context.tool_settings.mesh_select_mode = (False, False, True)

                bpy.ops.object.mode_set(mode="EDIT")
                _select_indices_in_edit_mesh(obj, indices, mode)
            except RuntimeError:
                if operator:
                    operator.report({"INFO"}, "Объект выделен, но Edit Mode не открылся")

        elif have_elements and not is_current:
            if operator:
                operator.report(
                    {"WARNING"},
                    "Геометрия или Transform изменились после проверки. "
                    "Старые индексы не выделяю — нажмите «Перепроверить объект».",
                )

        if context.scene.collision_inspector.auto_zoom:
            try:
                bpy.ops.view3d.view_selected(use_all_regions=False)
            except RuntimeError:
                pass

    return True


# =============================================================================
# Log
# =============================================================================


def _make_log(context):
    settings = context.scene.collision_inspector
    obj = bpy.data.objects.get(settings.object_name)

    lines = [
        "COLLISION INSPECTOR — SELECTED OBJECT / SHELL CHECK LOG",
        f"Blender: {bpy.app.version_string}",
        f"Add-on: {bl_info['version'][0]}.{bl_info['version'][1]}.{bl_info['version'][2]}",
        f"Object: {settings.object_name or '<none>'}",
        f"Convex angle: {settings.convex_angle:.6g} deg",
        f"Gap tolerance: {settings.gap_tolerance_mm:.6g} mm",
        f"Check degenerate: {settings.check_degenerate}",
        f"Check self intersections: {settings.check_self_intersections}",
        f"Check gaps: {settings.check_gaps}",
        f"Repair remove loose: {settings.repair_remove_loose}",
        f"Repair recalc normals: {settings.repair_recalc_normals}",
        f"Repair dissolve coplanar: {settings.repair_dissolve_coplanar}",
        f"Repair coplanar angle: {settings.repair_coplanar_angle:.6g} deg",
        f"Repair triangulation: {settings.repair_triangulation_method}",
        f"Repair flip limit: {settings.repair_max_flip_attempts}",
        f"Repair existing diagonals: {settings.repair_allow_existing_diagonals}",
        f"Viewport overlay: {settings.overlay_enabled}",
        f"Viewport A/B side: {settings.overlay_side}",
        f"Viewport X-Ray: {settings.overlay_xray}",
        f"Viewport markers: {settings.overlay_markers}",
        f"Marker presentation: {settings.overlay_marker_style}; pulse={settings.overlay_pulse}",
        f"Viewport draw error: {_viewport_ui.draw_error() or '<none>'}",
        f"Viewport performance: {json.dumps(_viewport_ui.cache_metrics(), ensure_ascii=False)}",
        f"Status: {settings.status}",
        f"Last repair: {settings.repair_last_status or '<none>'}",
        f"Diagonal proposals: {settings.repair_preview_count}",
        "-" * 78,
    ]

    if obj is not None and obj.type == "MESH":
        mesh = obj.data
        lines.extend([
            f"Mesh V/E/F: {len(mesh.vertices)}/{len(mesh.edges)}/{len(mesh.polygons)}",
            f"Transform location: {tuple(round(value, 9) for value in obj.location)}",
            f"Transform rotation_euler: {tuple(round(value, 9) for value in obj.rotation_euler)}",
            f"Transform scale: {tuple(round(value, 9) for value in obj.scale)}",
        ])

        try:
            shells, loose_regions, isolated_verts = _extract_shell_topology(obj)
            bad_convex = _checker_bad_convex_faces_by_shell(
                obj,
                shells,
                settings.convex_angle,
            )
            lines.append(f"Shells: {len(shells)}")
            lines.append(f"Loose edge regions: {len(loose_regions)}")
            lines.append(f"Isolated verts: {len(isolated_verts)}")
            lines.append("-" * 78)
            lines.append("SHELLS")
            for shell in shells:
                index = shell["index"]
                lines.append(
                    f"Shell {index}: V={len(shell['verts'])} E={len(shell['edges'])} "
                    f"F={len(shell['faces'])} Euler={shell['euler']} "
                    f"bad_edges={len(shell['bad_edges'])} "
                    f"bad_convex_faces={len(bad_convex.get(index, set()))}"
                )
        except Exception:
            lines.append("Shell diagnostic extraction failed:")
            lines.extend("    " + line for line in traceback.format_exc().splitlines())

    lines.extend([
        "-" * 78,
        f"Unclosed: {settings.unclosed_count}",
        f"Non-manifold: {settings.nonmanifold_count}",
        f"Non-convex: {settings.nonconvex_count}",
        f"Degenerate: {settings.degenerate_count}",
        f"Self-intersections: {settings.self_intersection_count}",
        f"Intersections: {settings.intersection_count}",
        f"Nested: {settings.nested_count}",
        f"Gaps: {settings.gap_count}",
        f"Contacts: {settings.contact_count}",
        f"Coincident shells: {settings.coincident_count}",
        f"Coplanar surface overlaps: {settings.surface_overlap_count}",
        f"Broad-phase shell pairs examined: {settings.pairs_examined}",
        f"Findings total: {len(settings.events)}",
        "-" * 78,
        "FINDINGS",
    ])

    for index, event in enumerate(settings.events, start=1):
        shell_text = (
            f"shell={event.shell_a}"
            if event.shell_b < 0
            else f"shells={event.shell_a}<->{event.shell_b}"
        )
        if event.shell_a < 0:
            shell_text = "loose_geometry"

        lines.append(
            f"[{index:04d}] {KIND_LABELS.get(event.kind, event.kind)} | "
            f"{shell_text} | mode={event.select_mode}"
        )
        lines.append(f"         {event.text}")
        if event.detail:
            lines.append(f"         {event.detail}")
        if event.has_witness_pair:
            lines.append(f"         witness A: {tuple(event.witness_a)}")
            lines.append(f"         witness B: {tuple(event.witness_b)}")
        if event.indices_a:
            lines.append(f"         A indices: {event.indices_a}")
        if event.indices_b:
            lines.append(f"         B indices: {event.indices_b}")

    if settings.last_error:
        lines.extend([
            "-" * 78,
            "LAST TRACEBACK",
            settings.last_error,
        ])

    lines.extend(["-" * 78, "RECENT ACTION TRACE"])
    for action in list(_ACTION_TRACE)[-100:]:
        lines.append(json.dumps(action, ensure_ascii=False, default=str))
    return "\n".join(lines)


# =============================================================================
# Properties
# =============================================================================


class COLLISIONINSPECTOR_Finding(PropertyGroup):
    kind: EnumProperty(
        name="Тип",
        items=(
            ("UNCLOSED", "Незамкнутость", "Открытые граничные рёбра или loose geometry"),
            ("NONMANIFOLD", "Non-manifold", "Есть рёбра, которые не являются manifold"),
            ("NONCONVEX", "Невыпуклость", "Угловой критерий SintezAGRChecker"),
            ("DEGENERATE", "Вырожденная", "Zero-area/zero-length/duplicate/NaN геометрия"),
            ("SELF_INTERSECTION", "Самопересечение", "Оболочка пересекает сама себя"),
            ("INTERSECTION", "Пересечение", "Реальное проникновение граней оболочек"),
            ("NESTED", "Полное вложение", "Одна оболочка целиком внутри другой"),
            ("GAP", "Малый зазор", "Расстояние между оболочками меньше допуска"),
            ("CONTACT", "Касание", "Касание поверхности с нулевым зазором"),
            ("COINCIDENT", "Совпадающие оболочки", "Точное совпадение сеток оболочек"),
            ("SURFACE_OVERLAP", "Наложение поверхностей", "Перекрывающиеся копланарные грани"),
        ),
    )
    obj_name: StringProperty()
    shell_a: IntProperty(default=-1)
    shell_b: IntProperty(default=-1)
    text: StringProperty()
    detail: StringProperty()
    indices_a: StringProperty()
    indices_b: StringProperty()
    signature: StringProperty(options={"HIDDEN"})
    select_mode: StringProperty(default="OBJECT")
    metric_value: FloatProperty(default=0.0)
    metric_limit: FloatProperty(default=0.0)
    has_witness_pair: BoolProperty(default=False)
    trace_segments: StringProperty(default="", options={"HIDDEN"})
    overlap_polygons: StringProperty(default="", options={"HIDDEN"})
    repair_available: BoolProperty(default=False, options={'SKIP_SAVE'})
    repair_reason: StringProperty(default='', options={'SKIP_SAVE'})
    repair_plan: StringProperty(default='', options={'SKIP_SAVE'})
    repair_preview_lines: StringProperty(default='', options={'SKIP_SAVE'})
    witness_a: FloatVectorProperty(size=3, default=(0.0,0.0,0.0), options={'HIDDEN'})
    witness_b: FloatVectorProperty(size=3, default=(0.0,0.0,0.0), options={'HIDDEN'})


ISSUE_GROUPS = {
    'CRITICAL': frozenset({'INTERSECTION', 'SELF_INTERSECTION', 'DEGENERATE', 'COINCIDENT', 'SURFACE_OVERLAP'}),
    'TOPOLOGY': frozenset({'UNCLOSED', 'NONMANIFOLD'}),
    'SHAPE': frozenset({'NONCONVEX', 'NESTED'}),
    'CLEARANCE': frozenset({'GAP', 'CONTACT'}),
}


def _visible_issue(kind, group):
    return group == 'ALL' or kind in ISSUE_GROUPS.get(group, ())


def _on_active_finding_update(self, context):
    _viewport_ui.redraw()


def _on_issue_filter_update(self, context):
    if self.events:
        first = next((i for i, event in enumerate(self.events)
                      if _visible_issue(event.kind, self.issue_filter)), None)
        if first is None:
            # Keep the user's requested group: zero findings in a category is a
            # valid result, not an invitation to silently revert to All.
            pass
        elif not _visible_issue(self.events[min(self.event_index, len(self.events)-1)].kind, self.issue_filter):
            self.event_index = first
        self.overlay_side = 'BOTH'
    _viewport_ui.invalidate()


def _nav_focus(context, settings):
    """Best-effort viewport-only framing; failures never cancel navigation."""
    if not getattr(settings, 'overlay_nav_focus', True) or not settings.events:
        return False
    index = min(settings.event_index, len(settings.events)-1)
    ok, reason = _viewport_ui.frame_event(context, settings.events[index])
    _trace_action('NAV_AUTOFOCUS', context, event=index+1, success=ok, reason=reason)
    return ok


def _move_issue(settings, direction):
    matches = [i for i, event in enumerate(settings.events)
               if _visible_issue(event.kind, settings.issue_filter)]
    if not matches:
        return False
    current = settings.event_index
    if current not in matches:
        settings.event_index = matches[0]
    else:
        settings.event_index = matches[(matches.index(current) + direction) % len(matches)]
    # A-only events need an A highlight, even after inspecting side B on another issue.
    settings.overlay_side = 'BOTH'
    _viewport_ui.redraw()
    return True


class COLLISIONINSPECTOR_Settings(PropertyGroup):
    convex_angle: FloatProperty(
        name="Выпуклость, °",
        description=(
            "Допуск невыпуклости в градусах. Метод и стандартное значение "
            "соответствуют SintezAGRChecker"
        ),
        default=0.1,
        min=0.01,
        max=2.0,
        precision=4,
    )

    gap_tolerance_mm: FloatProperty(
        name="Зазор, мм",
        description=(
            "Допуск малого зазора между отдельными оболочками внутри выбранного объекта"
        ),
        default=0.2,
        min=0.1,
        max=2.0,
        precision=4,
    )

    check_degenerate: BoolProperty(
        name="Вырожденная геометрия",
        description="Проверять zero-area faces, zero-length edges, duplicate faces и NaN/Inf",
        default=True,
    )
    check_self_intersections: BoolProperty(
        name="Самопересечения оболочек",
        description="Искать пересечения несоседних участков внутри каждой отдельной оболочки",
        default=True,
    )
    check_gaps: BoolProperty(
        name="Малые зазоры",
        description="Проверять точный минимальный зазор между отдельными оболочками",
        default=True,
    )

    overlay_nav_focus: BoolProperty(
        name="Фокус при переключении",
        description="Стрелки и кнопки навигации приближают вид к новой ошибке без изменения Mesh или выделения",
        default=True,
    )
    auto_zoom: BoolProperty(
        name="Автозум после проверки",
        description="Без изменения выделения приблизить вид к первой найденной проблеме",
        default=False,
    )

    overlay_enabled: BoolProperty(
        name="Подсветка во Viewport",
        description="Показывать диагностику над моделью без изменения её геометрии и выделения",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_hud: BoolProperty(
        name="Карточка проблемы",
        description="Компактное пояснение типа и номера активной проблемы в левом углу Viewport",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_markers: BoolProperty(
        name="Маркеры на модели",
        description="Показывать номера проблем непосредственно рядом с поврежденной геометрией",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_all_markers: BoolProperty(
        name="Все маркеры",
        description="Показывать также номера остальных проблем (до 80 за кадр), активная всегда отображается",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_side: EnumProperty(
        name="Подсветить",
        description="Область A, B либо обе; сторона B отображается только когда есть точные индексы",
        items=(('BOTH', 'A+B', 'Обе стороны'), ('A', 'A', 'Первая сторона'), ('B', 'B', 'Вторая сторона')),
        default='BOTH',
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_xray: BoolProperty(
        name="Видно сквозь модель (X-Ray)",
        description="Рисовать подсветку в том числе за заслоняющими поверхностями",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_opacity: FloatProperty(
        name="Насыщенность областей",
        description="Прозрачность подсвеченных граней",
        min=.06, max=.55, default=.23, precision=2,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_marker_style: EnumProperty(
        name="Вид маркеров",
        description="Тип маркера — выбирается в компактном выпадающем меню 3D Viewport",
        items=(('DOT', 'Точка', 'Компактная метка с номером'),
               ('HALO', 'Кольцо', 'Светящееся кольцо, не исчезающее на удалении'),
               ('BEACON', 'Маяк', 'Крупный маркер с выноской')),
        default='HALO',
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_pulse: BoolProperty(
        name="Мягкое мерцание",
        description="Анимация только поверх модели, не меняет геометрию",
        default=True,
        update=lambda self, context: _viewport_ui.redraw(),
    )
    overlay_pick_mode: BoolProperty(
        name="Выбор маркеров запущен", default=False, options={'SKIP_SAVE'},
    )
    overlay_ux_320_migrated: BoolProperty(
        name="Viewport UX 3.2 migration", default=False, options={'HIDDEN'},
    )

    example_labels: EnumProperty(
        name="Подписи примеров",
        description="Надписи создаются отдельными FONT-объектами, а не добавляются в Mesh",
        items=(('NONE','Без текста','Не создавать никаких надписей'),
               ('ID','Только ID','Короткие обозначения категорий и кейсов'),
               ('FULL','Полные','Полные названия категорий и вариантов')),
        default='NONE',
    )
    example_structure: EnumProperty(
        name="Структура примеров",
        description="Отдельные Mesh для точных 55 тестов или объединённые Mesh для стресс-проверки",
        items=(('SEPARATE','55 объектов','Каждый пример — самостоятельный объект'),
               ('CATEGORY','11 объектов','По одному Mesh на категорию'),
               ('ONE','1 общий Mesh','55 примеров в одном Mesh без слияния вершин')),
        default='SEPARATE',
    )
    example_root_name: StringProperty(
        name="Последняя тестовая коллекция", default="",
        description="Только коллекции, созданные этим генератором",
    )
    example_kind: EnumProperty(
        name="Тип тестовой проблемы", items=[
            (kind, label, f"Пять воспроизводимых случаев: {label}")
            for kind, label in _EXAMPLE_CATEGORIES
        ], default='UNCLOSED',
    )
    example_number: IntProperty(name="Вариант", min=1, max=5, default=1)
    example_last_summary: StringProperty(name="Результат проверки примеров", default="")
    example_report_path: StringProperty(name="Отчёт JSON", default="")

    repair_remove_loose: BoolProperty(
        name="Удалять loose geometry",
        description="Удалять только рёбра и вершины, которые не принадлежат ни одному полигону",
        default=True,
    )

    repair_recalc_normals: BoolProperty(
        name="Нормали наружу",
        description="Привести ориентацию граней каждой оболочки к согласованной внешней стороне",
        default=True,
    )

    repair_dissolve_coplanar: BoolProperty(
        name="Dissolve плоских рёбер",
        description="Удалять внутренние manifold-рёбра между практически копланарными гранями без перемещения вершин",
        default=True,
    )

    repair_coplanar_angle: FloatProperty(
        name="Плоское ребро, °",
        description="Максимальный угол между соседними гранями для безопасного Dissolve",
        default=0.05,
        min=0.0,
        max=2.0,
        precision=4,
    )

    repair_triangulation_method: EnumProperty(
        name="Стартовая триангуляция",
        description="Начальный способ разбиения quad/ngon перед перебором диагоналей",
        items=(
            ('BEAUTY', "Beauty", "Оптимальная стартовая триангуляция Blender"),
            ('FIXED', "Fixed", "Фиксированное направление диагонали"),
            ('ALTERNATE', "Alternate", "Альтернативное направление диагонали"),
        ),
        default='BEAUTY',
    )

    repair_max_flip_attempts: IntProperty(
        name="Лимит вариантов",
        description="Максимум попыток rotate edge за один запуск",
        default=2000,
        min=1,
        soft_max=10000,
        max=100000,
    )

    repair_allow_existing_diagonals: BoolProperty(
        name="Перебирать существующие диагонали",
        description=(
            "Расширенный режим: кроме диагоналей, созданных Inspector, разрешить rotate "
            "существующих рёбер между двумя треугольниками проблемной оболочки. "
            "Вершины не двигаются, но поверхность неплоской пары треугольников может измениться"
        ),
        default=False,
    )

    repair_preview_enabled: BoolProperty(
        name='Искать исправление при проверке', default=True,
        description='Пробный перебор диагоналей на отдельной копии Mesh без изменений оригинала',
    )
    repair_preview_max_shells: IntProperty(
        name='Максимум пробных оболочек', default=8, min=1, max=64,
    )
    repair_preview_max_faces: IntProperty(
        name='Предел граней для пробы', default=12000, min=100, max=150000,
    )
    repair_preview_attempts: IntProperty(
        name='Лимит попыток на оболочку', default=250, min=1, max=2000,
    )
    repair_preview_count: IntProperty(default=0, options={'SKIP_SAVE'})
    repair_last_status: StringProperty(default="")
    repair_last_log: StringProperty(default="")

    events: CollectionProperty(type=COLLISIONINSPECTOR_Finding)
    event_index: IntProperty(default=0, min=0, update=_on_active_finding_update)
    issue_filter: EnumProperty(
        name="Группа проблем",
        description="Ограничить список и маркеры типами проблем; проверка при этом не теряет результаты",
        items=(('ALL','Все','Показать все события'),
               ('CRITICAL','Критичные','Пересечения, совпадения и вырожденные участки'),
               ('TOPOLOGY','Топология','Открытые границы и non-manifold'),
               ('SHAPE','Форма','Невыпуклость и вложение'),
               ('CLEARANCE','Зазоры','Малые зазоры и касания')),
        default='ALL', update=_on_issue_filter_update,
    )

    status: StringProperty(default="Проверка ещё не запускалась")
    last_error: StringProperty()
    object_name: StringProperty()
    analysis_signature: StringProperty(options={"HIDDEN"})

    shell_count: IntProperty(default=0)
    loose_region_count: IntProperty(default=0)
    isolated_vert_count: IntProperty(default=0)
    pairs_examined: IntProperty(default=0)

    unclosed_count: IntProperty(default=0)
    nonmanifold_count: IntProperty(default=0)
    nonconvex_count: IntProperty(default=0)
    degenerate_count: IntProperty(default=0)
    self_intersection_count: IntProperty(default=0)
    intersection_count: IntProperty(default=0)
    nested_count: IntProperty(default=0)
    gap_count: IntProperty(default=0)
    contact_count: IntProperty(default=0)
    coincident_count: IntProperty(default=0)
    surface_overlap_count: IntProperty(default=0)


# =============================================================================
# Operators
# =============================================================================


class COLLISIONINSPECTOR_OT_check(Operator):
    bl_idname = "collision_inspector.check"
    bl_label = "Проверить выбранный объект"
    bl_description = (
        "Проверить активный выделенный Mesh; каждую несвязанную оболочку анализировать отдельно"
    )

    def execute(self, context):
        _trace_action('RUN_CHECK',context)
        if not _run_analysis(context, self, focus_first=True):
            return {'CANCELLED'}
        settings = context.scene.collision_inspector
        # A deliberate check always restores visibility, even if an earlier
        # project/add-on revision had left the overlay disabled.
        settings.overlay_enabled = True
        settings.overlay_markers = True
        settings.overlay_side = 'BOTH'  # A-only findings must not disappear after earlier B-only inspection.
        if not settings.overlay_ux_320_migrated:
            settings.overlay_xray = True
            settings.overlay_ux_320_migrated = True
        _viewport_ui.invalidate()
        _trace_action('OVERLAY_RESTORED', context, xray=settings.overlay_xray)
        # Auto-enable only after a user-requested check, not from batch tests.
        # Clicks outside marker/tool bar remain PASS_THROUGH to native Blender.
        if (not bpy.app.background and settings.analysis_signature and settings.overlay_enabled
                and context.area is not None and context.area.type == 'VIEW_3D'
                and not settings.overlay_pick_mode):
            try:
                bpy.ops.collision_inspector.pick_markers('INVOKE_DEFAULT')
            except (RuntimeError, ValueError) as exc:
                _trace_action('PICKER_START_SKIPPED', context, error=str(exc))
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_view(Operator):
    bl_idname = "collision_inspector.view"
    bl_label = "Выделить геометрию"
    bl_description = "Явно изменить выделение и перейти в Edit Mode для ручного исправления"

    side: EnumProperty(
        name="Сторона",
        items=(("BOTH", "Обе", "Показать обе стороны проблемы"),
               ("A", "A", "Показать только оболочку/область A"),
               ("B", "B", "Показать только оболочку/область B")),
        default="BOTH",
        options={'HIDDEN'},
    )

    def execute(self, context):
        settings = context.scene.collision_inspector
        if not settings.events:
            self.report({"INFO"}, "Список проблем пуст")
            return {"CANCELLED"}
        index = min(settings.event_index, len(settings.events) - 1)
        _trace_action('SELECT_PROBLEM_GEOMETRY',context,side=self.side)
        return {"FINISHED"} if _focus_event(
            context, settings.events[index], self, self.side
        ) else {"CANCELLED"}


class COLLISIONINSPECTOR_OT_previous(Operator):
    bl_idname = "collision_inspector.previous"
    bl_label = "Предыдущая"

    def execute(self, context):
        settings = context.scene.collision_inspector
        if not settings.events:
            return {"CANCELLED"}

        if _move_issue(settings, -1):
            _nav_focus(context, settings)
        _trace_action('PREVIOUS_FINDING',context)
        return {"FINISHED"}


class COLLISIONINSPECTOR_OT_next(Operator):
    bl_idname = "collision_inspector.next"
    bl_label = "Следующая"

    def execute(self, context):
        settings = context.scene.collision_inspector
        if not settings.events:
            return {"CANCELLED"}

        if _move_issue(settings, 1):
            _nav_focus(context, settings)
        _trace_action('NEXT_FINDING',context)
        return {"FINISHED"}


class COLLISIONINSPECTOR_OT_frame(Operator):
    bl_idname = "collision_inspector.frame"
    bl_label = "Приблизить к проблеме"
    bl_description = "Только переместить камеру к проблеме; НЕ менять выделение и режим"

    side: EnumProperty(
        items=(('BOTH', 'Обе', ''), ('A', 'A', ''), ('B', 'B', '')),
        default='BOTH', options={'HIDDEN'},
    )

    def execute(self, context):
        settings = context.scene.collision_inspector
        if not settings.events:
            return {'CANCELLED'}
        event = settings.events[min(settings.event_index, len(settings.events)-1)]
        _trace_action('FRAME_VIEWPORT_PROBLEM',context,side=self.side)
        ok, description = _viewport_ui.frame_event(context, event, self.side)
        if not ok:
            self.report({'WARNING'}, description)
            return {'CANCELLED'}
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_pick_markers(Operator):
    bl_idname = "collision_inspector.pick_markers"
    bl_label = "Выбирать проблемы во Viewport"
    bl_description = "ЛКМ по маркеру выбирает проблему; ←/→ листают; Esc скрывает весь Viewport-инспектор. Мимо маркера клики проходят в Blender"

    def invoke(self, context, event):
        settings = context.scene.collision_inspector
        if settings.overlay_pick_mode:
            # The shortcut is a true inspector visibility toggle. Leaving the
            # picker while continuing to draw would make Esc ineffective.
            _trace_action('HIDE_INSPECTOR_HOTKEY', context)
            settings.overlay_pick_mode = False
            settings.overlay_enabled = False
            _viewport_ui.close_marker_menu()
            _viewport_ui.close_footer_group_menu()
            _viewport_ui.redraw()
            return {'FINISHED'}
        if not settings.analysis_signature:
            self.report({'WARNING'}, 'Сначала проверьте модель')
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        obj = context.active_object
        if obj is None or obj.name != settings.object_name or obj.mode != 'OBJECT':
            self.report({'WARNING'}, 'Активируйте проверенный объект в Object Mode')
            return {'CANCELLED'}
        # A stale analysis still needs an interactive "Проверить всё" button.
        # Repair and individual issue picking remain blocked by _read_context.
        settings.overlay_enabled = True
        settings.overlay_markers = True
        settings.overlay_pick_mode = True
        self._area = context.area
        self._scene = context.scene
        self._picker_token = object()
        _PICKER_SESSIONS[self._scene.as_pointer()] = self._picker_token
        _trace_action('START_PICKER',context)
        context.window_manager.modal_handler_add(self)
        _viewport_ui.redraw()
        return {'RUNNING_MODAL'}

    def _close(self):
        try:
            key = self._scene.as_pointer()
            # An old handler must not hide the NEW controller's dropdowns.
            if _PICKER_SESSIONS.get(key) is getattr(self, '_picker_token', None):
                _PICKER_SESSIONS.pop(key, None)
                self._scene.collision_inspector.overlay_pick_mode = False
                _viewport_ui.clear_footer_hover()
                _viewport_ui.close_marker_menu()
                _viewport_ui.close_footer_group_menu()
                _viewport_ui.redraw()
        except (AttributeError, ReferenceError, RuntimeError):
            pass
        return {'FINISHED'}

    def modal(self, context, event):
        try:
            # Only the current session may process events. Old handlers still
            # unwinding after Undo must not act on a recovered session.
            if (_PICKER_SESSIONS.get(self._scene.as_pointer()) is not
                    getattr(self, '_picker_token', None)):
                return self._close()
            # Input from another editor must remain Blender's to handle.  In RC12
            # leaving the VIEW_3D area silently killed the modal controller, while
            # the floating overlay remained visible and Esc could no longer close it.
            if context.area != self._area:
                return {'PASS_THROUGH'}
            if context.scene != self._scene:
                # Keep presentation preference for when this scene is revisited.
                # Never intercept input belonging to the newly active scene.
                return self._close()
            settings = context.scene.collision_inspector
            if not settings.overlay_pick_mode or not settings.overlay_enabled:
                return self._close()
            # Esc has priority over any stale active object/mode: a valid inspector
            # controller must always allow closing its own floating UI in one key.
            if event.type == 'ESC' and event.value == 'PRESS':
                # One Esc dismisses the complete CI viewport presentation, even
                # when either dropdown is open. Do not touch Mesh/selection/mode.
                # No global Esc keymap: Blender's native modal tools keep Esc.
                settings.overlay_enabled = False
                _trace_action('HIDE_INSPECTOR_ESC', context,
                              object=settings.object_name, kept_results=len(settings.events))
                return self._close()
            if (not settings.analysis_signature or context.active_object is None or
                    context.active_object.name != settings.object_name or
                    context.active_object.mode != 'OBJECT'):
                # Suspend input while another Mesh or Edit Mode is active. Keep
                # overlay_enabled so the footer/card return together on reselect
                # or Object Mode. _read_context blocks stale geometry while away.
                return self._close()
            if event.type == 'MOUSEMOVE':
                try:
                    _viewport_ui.update_footer_hover(context, event.mouse_x, event.mouse_y)
                    _viewport_ui.update_marker_menu_hover(context, event.mouse_x, event.mouse_y)
                except Exception as exc:
                    # Hover is optional. Any future GPU/UI bug must not terminate
                    # the modal and strand Esc while keeping windows on screen.
                    if getattr(self, '_last_hover_error', None) != str(exc):
                        self._last_hover_error = str(exc)
                        _viewport_ui.note_controller_error(exc)
                        _trace_action('PICKER_HOVER_FAILURE', context, error=str(exc))
                    _viewport_ui.clear_footer_hover()
                return {'PASS_THROUGH'}
            if event.value == 'PRESS' and event.type in {'LEFT_ARROW', 'RIGHT_ARROW'}:
                step = -1 if event.type == 'LEFT_ARROW' else 1
                if _move_issue(settings, step):
                    _nav_focus(context, settings)
                _trace_action('PICKER_KEYBOARD_NAV',context,step=step)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                footer = _viewport_ui.hit_footer(context, event.mouse_x, event.mouse_y)
                if footer is not None:
                    if footer == 'GROUP':
                        _viewport_ui.toggle_footer_group_menu(context.area)
                        _trace_action('VIEWPORT_GROUP_DROPDOWN', context)
                    elif footer == 'GROUP_PANEL':
                        pass
                    elif footer.startswith('GROUP_'):
                        settings.issue_filter = footer[len('GROUP_'):]
                        _viewport_ui.close_footer_group_menu()
                        _nav_focus(context, settings)
                        _trace_action('VIEWPORT_FILTER_CHANGE', context, group=settings.issue_filter)
                    elif footer in ('PREV', 'NEXT'):
                        direction = -1 if footer == 'PREV' else 1
                        if _move_issue(settings, direction):
                            _nav_focus(context, settings)
                            _trace_action('VIEWPORT_FOOTER_NAV', context, direction=direction,
                                          group=settings.issue_filter)
                    elif footer == 'REPAIR':
                        _trace_action('VIEWPORT_APPROVED_REPAIR_REQUEST', context)
                        # Exit marker modal before opening Blender's confirmation.
                        settings.overlay_pick_mode = False
                        try:
                            bpy.ops.collision_inspector.apply_suggested('INVOKE_DEFAULT')
                        except (RuntimeError, ValueError) as exc:
                            self.report({'WARNING'}, str(exc)[:160])
                        return self._close()
                    elif footer == 'CHECK':
                        _trace_action('VIEWPORT_FULL_CHECK', context)
                        if not _run_analysis(context, self, focus_first=False):
                            return self._close()
                    _viewport_ui.redraw()
                    return {'RUNNING_MODAL'}
                # Unrelated clicks close the filter without blocking native Blender.
                if _viewport_ui.group_menu_is_open(context.area):
                    _viewport_ui.close_footer_group_menu()
                control = _viewport_ui.hit_toolbar(context, event.mouse_x, event.mouse_y)
                if control is not None:
                    if control == 'MENU_PANEL':
                        return {'RUNNING_MODAL'}
                    if control == 'MENU':
                        _viewport_ui.toggle_marker_menu(context.area)
                        _trace_action('VIEWPORT_MARKER_DROPDOWN', context)
                        return {'RUNNING_MODAL'}
                    if control in {'DOT', 'HALO', 'BEACON'}:
                        settings.overlay_marker_style = control
                        _viewport_ui.close_marker_menu()
                    elif control == 'PULSE':
                        settings.overlay_pulse = not settings.overlay_pulse
                    elif control == 'ALL':
                        settings.overlay_all_markers = not settings.overlay_all_markers
                    elif control == 'XRAY':
                        settings.overlay_xray = not settings.overlay_xray
                    _trace_action('VIEWPORT_MARKER_CONTROL', context, control=control,
                                  style=settings.overlay_marker_style,
                                  pulse=settings.overlay_pulse)
                    _viewport_ui.redraw()
                    return {'RUNNING_MODAL'}
                # Closing a popup never swallows an unrelated Blender click.
                if _viewport_ui.marker_menu_is_open(context.area):
                    _viewport_ui.close_marker_menu()
                match = _viewport_ui.pick_event(context, event.mouse_x, event.mouse_y)
                if match is not None:
                    settings.event_index = match
                    settings.overlay_side = 'BOTH'
                    _trace_action('PICKER_CLICK_MARKER',context,marker=match+1,shift=bool(event.shift))
                    if event.shift:
                        event_data = settings.events[match]
                        _viewport_ui.frame_event(context, event_data)
                    _viewport_ui.redraw()
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        except Exception as exc:
            # User controls must never become orphaned after an exception.
            try:
                if (_PICKER_SESSIONS.get(self._scene.as_pointer()) is
                        getattr(self, '_picker_token', None)):
                    self._scene.collision_inspector.overlay_enabled = False
            except (AttributeError, ReferenceError, RuntimeError):
                pass
            try:
                _viewport_ui.note_controller_error(exc)
                _trace_action('PICKER_MODAL_FAILURE', context, error=str(exc))
            except Exception:
                pass
            try:
                return self._close()
            except Exception:
                return {'FINISHED'}


class COLLISIONINSPECTOR_OT_recheck(Operator):
    bl_idname = "collision_inspector.recheck"
    bl_label = "Перепроверить объект"
    bl_description = (
        "После ручных исправлений заново построить оболочки и полностью повторить все проверки"
    )

    def execute(self, context):
        settings = context.scene.collision_inspector
        # Rechecking uses the active selected Mesh. Never switch objects/modes.
        _trace_action('RECHECK_ACTIVE_MESH',context)
        return {"FINISHED"} if _run_analysis(context, self, focus_first=False) else {"CANCELLED"}


class COLLISIONINSPECTOR_OT_basic_repair(Operator):
    bl_idname = "collision_inspector.basic_repair"
    bl_label = "Безопасная очистка"
    bl_description = (
        "Без перемещения вершин: удалить loose geometry, dissolve практически "
        "копланарных внутренних рёбер и пересчитать нормали согласно настройкам"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _trace_action('REPAIR_BASIC_REQUEST',context)
        settings = context.scene.collision_inspector
        # Repair is destructive: never silently leave Edit Mode or alter selection.
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Для исправления перейдите в Object Mode вручную; режим не изменён")
            return {'CANCELLED'}
        obj = _active_selected_mesh(context, self)
        if obj is None:
            return {'CANCELLED'}

        original = backup = None
        made_single_user = False
        try:
            _preflight_repair_data(obj)
            before_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            guard_before = _mesh_guard_snapshot(obj)
            original, backup, made_single_user = _repair_mesh_backup(obj)
            stats = _apply_basic_repair(obj, settings)
            after_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            guard_ok, guard_detail = _verify_mesh_guard(
                guard_before, _mesh_guard_snapshot(obj), allow_loose_removal=settings.repair_remove_loose
            )
            if not guard_ok or not _repair_cleanup_safe(after_profile, before_profile):
                _repair_rollback(obj, original, backup, made_single_user)
                backup = None
                message = (
                    f"Очистка отклонена ({guard_detail}) или ухудшила профиль — исходная сетка восстановлена. "
                    f"До: {_format_quality_profile(before_profile)}. "
                    f"Проба: {_format_quality_profile(after_profile)}."
                )
                settings.repair_last_status = message
                settings.repair_last_log = "CLEANUP ROLLBACK\n" + message
                self.report({'WARNING'}, "Очистка отменена: контрольный профиль ухудшился")
                _run_analysis(context, self, focus_first=False)
                return {'FINISHED'}

            _repair_commit_cleanup(obj, original, backup, made_single_user)
            backup = None

            message = "Безопасная очистка: " + _format_cleanup_stats(stats)
            settings.repair_last_status = message
            settings.repair_last_log = message
            self.report({'INFO'}, "Очистка выполнена; запускаю повторную проверку")
            _run_analysis(context, self, focus_first=False)
            extra = ""
            if settings.nonconvex_count:
                extra = (
                    f" Осталось невыпуклых оболочек: {settings.nonconvex_count}; "
                    "очистка их специально не меняет."
                )
            settings.repair_last_status = message + f". После проверки: {settings.status}." + extra
            return {'FINISHED'}

        except Exception as exc:
            if original is not None:
                try:
                    _repair_rollback(obj, original, backup, made_single_user)
                except Exception:
                    pass
            trace = traceback.format_exc()
            settings.last_error = trace
            settings.repair_last_status = f"Ошибка очистки: {type(exc).__name__}: {exc}"
            settings.repair_last_log = trace
            self.report({'ERROR'}, settings.repair_last_status)
            print(trace)
            return {'CANCELLED'}


class COLLISIONINSPECTOR_OT_optimize_triangulation(Operator):
    bl_idname = "collision_inspector.optimize_triangulation"
    bl_label = "Перебрать диагонали"
    bl_description = (
        "На рабочей копии перебрать диагонали проблемных оболочек и применить результат "
        "только если полный профиль выпуклости действительно улучшился"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _trace_action('REPAIR_DIAGONALS_REQUEST',context)
        settings = context.scene.collision_inspector
        # Repair is destructive: never silently leave Edit Mode or alter selection.
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Для исправления перейдите в Object Mode вручную; режим не изменён")
            return {'CANCELLED'}
        obj = _active_selected_mesh(context, self)
        if obj is None:
            return {'CANCELLED'}

        original = backup = None
        made_single_user = False
        try:
            _preflight_repair_data(obj)
            before_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            guard_before = _mesh_guard_snapshot(obj)
            original, backup, made_single_user = _repair_mesh_backup(obj)
            stats = _apply_triangulation_repair(obj, settings, include_basic_cleanup=False)
            after_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)

            guard_ok, guard_detail = _verify_mesh_guard(guard_before, _mesh_guard_snapshot(obj))
            improved = guard_ok and _repair_profile_better(after_profile, before_profile)
            if not improved:
                _repair_rollback(obj, original, backup, made_single_user)
                backup = None
                message = (
                    f"Перебор отклонён ({guard_detail}) или не улучшил профиль — исходная сетка восстановлена. "
                    f"До: {_format_quality_profile(before_profile)}. "
                    f"Проба: {_format_quality_profile(after_profile)}."
                )
                settings.repair_last_status = message
                settings.repair_last_log = "TRIANGULATION REPAIR ROLLBACK\n" + message + "\n" + _format_triangulation_stats(stats)
                self.report({'INFO'}, "Улучшения нет — изменения отменены")
                _run_analysis(context, self, focus_first=False)
                return {'FINISHED'}

            _repair_commit_cleanup(obj, original, backup, made_single_user)
            backup = None
            message = (
                "Перебор применён. " + _format_triangulation_stats(stats) + ". "
                f"До: {_format_quality_profile(before_profile)}. "
                f"После: {_format_quality_profile(after_profile)}."
            )
            settings.repair_last_status = message
            settings.repair_last_log = "TRIANGULATION REPAIR COMMIT\n" + message
            self.report({'INFO'}, "Выпуклость улучшена; запускаю повторную проверку")
            _run_analysis(context, self, focus_first=False)
            return {'FINISHED'}
        except Exception as exc:
            if original is not None:
                try:
                    _repair_rollback(obj, original, backup, made_single_user)
                except Exception:
                    pass
            trace = traceback.format_exc()
            settings.last_error = trace
            settings.repair_last_status = f"Ошибка перебора: {type(exc).__name__}: {exc}"
            settings.repair_last_log = trace
            self.report({'ERROR'}, settings.repair_last_status)
            print(trace)
            return {'CANCELLED'}

class COLLISIONINSPECTOR_OT_apply_suggested(Operator):
    bl_idname = 'collision_inspector.apply_suggested'
    bl_label = 'Доступно исправление'
    bl_description = 'Применить только подтверждённый пробой вариант для текущей невыпуклой оболочки'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        settings = context.scene.collision_inspector
        if not settings.events:
            return {'CANCELLED'}
        finding = settings.events[min(settings.event_index, len(settings.events) - 1)]
        if not finding.repair_available:
            self.report({'WARNING'}, finding.repair_reason or 'Для этой проблемы нет безопасного варианта')
            return {'CANCELLED'}
        # Do not rearm the Viewport controller over Blender's confirm dialog.
        _PICKER_CONFIRM_SCENES.add(context.scene.as_pointer())
        try:
            return context.window_manager.invoke_confirm(self, event)
        except Exception:
            _PICKER_CONFIRM_SCENES.discard(context.scene.as_pointer())
            raise

    def cancel(self, context):
        _PICKER_CONFIRM_SCENES.discard(context.scene.as_pointer())
        _viewport_ui.redraw()

    def execute(self, context):
        _PICKER_CONFIRM_SCENES.discard(context.scene.as_pointer())
        settings = context.scene.collision_inspector
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, 'Для исправления требуется Object Mode')
            return {'CANCELLED'}
        obj = _active_selected_mesh(context, self)
        if obj is None or not settings.events:
            return {'CANCELLED'}
        try:
            approved = _plans_for_current_object(settings, obj)
            selected = settings.events[min(settings.event_index, len(settings.events)-1)]
            proposals = [(ev, plan) for ev, plan in approved if ev.as_pointer() == selected.as_pointer()]
            if len(proposals) != 1:
                raise ValueError('Для выбранной проблемы нет актуального проверенного решения')
            applied, _, before, after = _apply_approved_diagonals(obj, settings, proposals)
            message = (f'Применено исправление оболочки {selected.shell_a}; '
                       f'{_format_quality_profile(before)} → {_format_quality_profile(after)}')
            _trace_action('DIAGONAL_SUGGESTED_APPLIED',context, shell=selected.shell_a)
            _run_analysis(context, self, focus_first=False)
            settings.repair_last_status = message
            settings.repair_last_log = message
            self.report({'INFO'}, 'Исправление применено после контрольной проверки')
            return {'FINISHED'}
        except Exception as exc:
            message = f'Исправление не применено: {type(exc).__name__}: {exc}'
            settings.repair_last_status = message
            settings.repair_last_log = message + '\n' + traceback.format_exc()
            self.report({'WARNING'}, message[:200])
            return {'CANCELLED'}


class COLLISIONINSPECTOR_OT_apply_bulk_suggested(Operator):
    bl_idname = 'collision_inspector.apply_bulk_suggested'
    bl_label = 'Исправить подтверждённые варианты'
    bl_description = 'Показать план для всех подтверждённых диагоналей и применить одной транзакцией после согласия'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, 'Нужен Object Mode')
            return {'CANCELLED'}
        settings = context.scene.collision_inspector
        obj = _active_selected_mesh(context, self)
        if obj is None:
            return {'CANCELLED'}
        try:
            proposals = _plans_for_current_object(settings, obj)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        if not proposals:
            self.report({'INFO'}, 'Нет подтверждённых вариантов; запустите полную проверку')
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        obj = context.active_object
        try:
            proposals = _plans_for_current_object(settings, obj)
        except (ValueError, AttributeError):
            layout.label(text='План устарел — выполните повторную проверку', icon='ERROR')
            return
        layout.label(text=f'ПРОБНЫЙ ПЛАН · {len(proposals)} подтверждённых оболочек', icon='MOD_TRIANGULATE')
        layout.label(text='Применяются только перестановки, не очистка и не Boolean.')
        layout.label(text='Безопасность перепроверяется после КАЖДОГО шага.')
        for event, plan in proposals[:20]:
            box = layout.box()
            box.label(text=f"Оболочка {event.shell_a} · {plan['method']} · "
                           f"новых диагоналей: {plan['stats']['generated_diagonals']}")
            box.label(text=f"Перестановок: {plan['stats']['accepted']}; "
                           f"рёбра {plan['before_bad_edges']} → {plan['after_bad_edges']}")
        others = [ev for ev in settings.events if ev.kind != 'NONCONVEX']
        if others:
            layout.label(text=f'Другие типы ошибок: {len(others)} — НЕ изменяются', icon='INFO')
        layout.label(text='Отмена = исходный Mesh без изменений. После подтверждения — Undo.', icon='LOCKED')

    def execute(self, context):
        settings = context.scene.collision_inspector
        if context.mode != 'OBJECT':
            return {'CANCELLED'}
        obj = _active_selected_mesh(context, self)
        if obj is None:
            return {'CANCELLED'}
        try:
            proposals = _plans_for_current_object(settings, obj)
            if not proposals:
                raise ValueError('Нет актуальных вариантов')
            applied, skipped, before, after = _apply_approved_diagonals(obj, settings, proposals, bulk=True)
            message = (f'Массовый перебор: применено {len(applied)}, отклонено {len(skipped)}. '
                       f'{_format_quality_profile(before)} → {_format_quality_profile(after)}')
            _trace_action('DIAGONAL_BULK_APPLIED', context, applied=len(applied), skipped=len(skipped))
            _run_analysis(context, self, focus_first=False)
            settings.repair_last_status = message
            settings.repair_last_log = message + '\n' + '\n'.join(
                f'Отклонена оболочка {shell}: {reason}' for shell, reason in skipped)
            self.report({'INFO'}, f'Применено {len(applied)} из {len(proposals)} предложений')
            return {'FINISHED'}
        except Exception as exc:
            message = f'Массовое исправление отменено: {type(exc).__name__}: {exc}'
            settings.repair_last_status = message
            settings.repair_last_log = message + '\n' + traceback.format_exc()
            self.report({'WARNING'}, message[:200])
            return {'CANCELLED'}


class COLLISIONINSPECTOR_OT_auto_repair(Operator):
    bl_idname = "collision_inspector.auto_repair"
    bl_label = "Автоисправление"
    bl_description = (
        "Выполнить безопасную очистку, затем попробовать улучшить диагонали; "
        "неудачный этап диагоналей откатывается автоматически"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _trace_action('REPAIR_AUTO_REQUEST',context)
        settings = context.scene.collision_inspector
        # Repair is destructive: never silently leave Edit Mode or alter selection.
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, "Для исправления перейдите в Object Mode вручную; режим не изменён")
            return {'CANCELLED'}
        obj = _active_selected_mesh(context, self)
        if obj is None:
            return {'CANCELLED'}

        root_original = root_backup = None
        root_single = False
        tri_checkpoint = None
        try:
            _preflight_repair_data(obj)
            before_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            guard_before = _mesh_guard_snapshot(obj)
            root_original, root_backup, root_single = _repair_mesh_backup(obj)
            cleanup = _apply_basic_repair(obj, settings)
            cleanup_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            cleanup_guard_ok, cleanup_guard_detail = _verify_mesh_guard(
                guard_before, _mesh_guard_snapshot(obj), allow_loose_removal=settings.repair_remove_loose
            )
            if not cleanup_guard_ok or not _repair_cleanup_safe(cleanup_profile, before_profile):
                _repair_rollback(obj, root_original, root_backup, root_single)
                root_backup = None
                message = (
                    f"Автоисправление остановлено: очистка ({cleanup_guard_detail}) или профиль ухудшились; "
                    "исходная сетка полностью восстановлена."
                )
                settings.repair_last_status = message
                settings.repair_last_log = (
                    "AUTO REPAIR ROLLBACK\n" + message + "\n"
                    f"Before: {_format_quality_profile(before_profile)}\n"
                    f"Cleanup try: {_format_quality_profile(cleanup_profile)}"
                )
                self.report({'WARNING'}, message)
                _run_analysis(context, self, focus_first=False)
                return {'FINISHED'}

            # Preserve the cleanup result as a checkpoint. Triangulation is an
            # optional second stage and must beat this checkpoint to survive.
            tri_guard = _mesh_guard_snapshot(obj)
            tri_checkpoint = obj.data.copy()
            tri = _apply_triangulation_repair(obj, settings, include_basic_cleanup=False)
            tri_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            tri_guard_ok, tri_guard_detail = _verify_mesh_guard(tri_guard, _mesh_guard_snapshot(obj))
            tri_kept = tri_guard_ok and _repair_profile_better(tri_profile, cleanup_profile)
            if not tri_kept:
                current = obj.data
                obj.data = tri_checkpoint
                if not root_single:
                    # The original single-user datablock is the current edited
                    # working mesh; after switching to the cleanup checkpoint,
                    # use the checkpoint as the committed root reference.
                    root_original = obj.data
                if current.users == 0:
                    bpy.data.meshes.remove(current)
            elif tri_checkpoint.users == 0:
                bpy.data.meshes.remove(tri_checkpoint)

            _repair_commit_cleanup(obj, root_original, root_backup, root_single)
            root_backup = None
            final_profile = _repair_quality_profile(obj, settings.convex_angle, settings.gap_tolerance_mm)
            cleanup_text = _format_cleanup_stats(cleanup)
            tri_text = _format_triangulation_stats(tri)
            tri_state = "применён" if tri_kept else "отклонён и восстановлен checkpoint"
            settings.repair_last_log = (
                "AUTO REPAIR · RC5.1\n"
                f"Cleanup: {cleanup_text}\n"
                f"Triangulation ({tri_state}): {tri_text}\n"
                f"Cleanup profile: {_format_quality_profile(cleanup_profile)}\n"
                f"Final profile: {_format_quality_profile(final_profile)}"
            )
            self.report({'INFO'}, "Автоисправление завершено; запускаю полную проверку")
            _run_analysis(context, self, focus_first=False)
            settings.repair_last_status = (
                f"Очистка: {cleanup_text}. Диагонали: {tri_state}. "
                f"После проверки: {settings.status}."
            )
            return {'FINISHED'}
        except Exception as exc:
            if root_original is not None:
                try:
                    _repair_rollback(obj, root_original, root_backup, root_single)
                except Exception:
                    pass
            if tri_checkpoint is not None and tri_checkpoint.users == 0:
                try:
                    bpy.data.meshes.remove(tri_checkpoint)
                except Exception:
                    pass
            trace = traceback.format_exc()
            settings.last_error = trace
            settings.repair_last_status = f"Ошибка автоисправления: {type(exc).__name__}: {exc}"
            settings.repair_last_log = trace
            self.report({'ERROR'}, settings.repair_last_status)
            print(trace)
            return {'CANCELLED'}

# =============================================================================
# Diagnostic fixture gallery — 11 kinds x 5 different geometries, per-object Mesh
# =============================================================================

class COLLISIONINSPECTOR_OT_example_create(Operator):
    bl_idname = "collision_inspector.example_create"
    bl_label = "Создать 55 примеров"
    bl_description = "Создать 55 случаев в выбранной структуре и режиме подписей, не меняя рабочие объекты и выделение"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    def execute(self, context):
        settings = context.scene.collision_inspector
        layout_mode = settings.example_structure
        label_mode = 'NONE' if layout_mode == 'ONE' else settings.example_labels
        _trace_action('EXAMPLES_CREATE_START', context, count=55, mode=layout_mode, labels=label_mode)
        try:
            root = _examples.create_gallery(context.scene, labels=label_mode, mode=layout_mode)
            fixtures = _examples._gallery_inventory(root)
            expected = 55 if layout_mode == 'SEPARATE' else 11 if layout_mode == 'CATEGORY' else 1
            if len(fixtures) != expected:
                _examples.remove_gallery(root, context.scene)
                raise RuntimeError(f'Неполный набор: {len(fixtures)}/{expected}')
            settings.example_root_name = root.name
            settings.example_last_summary = f'Создано 55 случаев: {expected} Mesh; подписи {label_mode}'
            settings.example_report_path = ''
            _trace_action('EXAMPLES_CREATED',context,collection=root.name,count=len(fixtures),mode=layout_mode,labels=label_mode)
            self.report({'INFO'}, f'Создано 55 случаев в {expected} Mesh; рабочая модель не менялась')
            return {'FINISHED'}
        except Exception as exc:
            settings.example_last_summary = 'Ошибка генератора: ' + str(exc)
            _trace_action('EXAMPLES_CREATE_ERROR',context,error=repr(exc))
            self.report({'ERROR'}, 'Не удалось создать примеры: ' + str(exc)[:160])
            return {'CANCELLED'}


class COLLISIONINSPECTOR_OT_example_focus(Operator):
    bl_idname = "collision_inspector.example_focus"
    bl_label = "Открыть и проверить пример"
    bl_description = "Явно выделить выбранный тестовый Mesh, приблизить вид и проверить дефект в инспекторе"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene is not None

    def execute(self, context):
        settings = context.scene.collision_inspector
        root=_examples.owned_root(context.scene,settings.example_root_name)
        if root is None:
            self.report({'WARNING'}, 'Сначала создайте примеры')
            return {'CANCELLED'}
        try:
            _examples._gallery_inventory(root)
        except ValueError as exc:
            self.report({'ERROR'},'Тестовый набор изменён: '+str(exc)[:135])
            return {'CANCELLED'}
        gallery_mode=root.get('ci_gallery_mode','SEPARATE')
        obj=_examples.case_object(root,settings.example_kind,settings.example_number)
        if obj is None:
            self.report({'ERROR'}, 'Тестовый объект отсутствует или перемещён')
            return {'CANCELLED'}
        if obj.hide_get() or obj.hide_viewport:
            self.report({'WARNING'}, 'Пример скрыт: покажите объект перед открытием')
            return {'CANCELLED'}
        # End the old modal picker before replacing the inspected object.
        # The existing modal handler detects the disabled flag and exits.
        if settings.overlay_pick_mode:
            settings.overlay_pick_mode = False
        # An explicit user-facing "Open" operation may change selection.
        for previous in context.selected_objects:
            previous.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active=obj
        context.view_layer.update()
        if context.area is not None and context.area.type=='VIEW_3D':
            space=context.area.spaces.active
            if space.region_3d is not None:
                local_center=sum((Vector(pt) for pt in obj.bound_box),Vector((0.0,0.0,0.0))) / 8
                space.region_3d.view_location=obj.matrix_world @ local_center
                space.region_3d.view_distance=max(5.0,max(obj.dimensions)*3.5)
        # Combined test layouts share one Mesh across multiple cases. Focus on
        # the selected case site rather than the bounding box of all 55 cases.
        if gallery_mode != 'SEPARATE' and context.area is not None and context.area.type == 'VIEW_3D':
            try:
                centers=json.loads(obj.get('ci_combined_cases','[]'))
                match=next(row for row in centers if row[0]==settings.example_kind
                           and row[1]==settings.example_number)
                space=context.area.spaces.active
                space.region_3d.view_location=Vector((float(match[2]),float(match[3]),0.0))
                space.region_3d.view_distance=9.0
            except (ValueError,TypeError,StopIteration,AttributeError):
                pass
        settings.issue_filter='ALL'
        settings.overlay_enabled=True
        settings.overlay_markers=True
        _trace_action('EXAMPLE_FOCUS',context,kind=settings.example_kind,number=settings.example_number)
        if not _run_analysis(context,self,focus_first=False):
            self.report({'WARNING'}, 'Объект открыт, проверка завершилась ошибкой: см. Диагностику')
            return {'CANCELLED'}
        expected=settings.example_kind
        matches=[index for index, ev in enumerate(settings.events) if ev.kind==expected]
        if matches:
            if gallery_mode != 'SEPARATE':
                # Prefer the same case's spatial cell, not an arbitrary matching
                # issue in the combined mesh. No guarantee of per-case proof.
                try:
                    row=next(item for item in json.loads(obj.get('ci_combined_cases','[]'))
                             if item[0]==expected and item[1]==settings.example_number)
                    target=(float(row[2]),float(row[3]))
                    def rank(i):
                        point=_viewport_ui._event_center(obj,settings.events[i])
                        if point is None: return float('inf')
                        return (point[0]-target[0])**2+(point[1]-target[1])**2
                    matches.sort(key=rank)
                except (ValueError,TypeError,StopIteration):
                    pass
            # Crucial: examples of NONMANIFOLD can also generate UNCLOSED,
            # but the viewport must show the REQUESTED defect, not event #1.
            settings.event_index=matches[0]
            _viewport_ui.redraw()
            target=settings.events[matches[0]]
            if expected=='INTERSECTION' and not target.trace_segments:
                self.report({'WARNING'}, 'Пересечение найдено, но точная линия не вычислена; показаны исходные грани')
            elif expected=='GAP' and not target.has_witness_pair:
                self.report({'WARNING'}, 'Зазор найден, но ближайшие точки не вычислены')
            elif expected=='SELF_INTERSECTION' and not (target.trace_segments or target.overlap_polygons):
                self.report({'WARNING'}, 'Самопересечение найдено, но точное геометрическое доказательство отсутствует')
            elif expected=='SURFACE_OVERLAP' and not target.overlap_polygons:
                self.report({'WARNING'}, 'Наложение найдено, но точный контур перекрытия не вычислен')
            else:
                self.report({'INFO'},f'Пример {expected} #{settings.example_number}: дефект найден' +
                            (' в объединённой модели (стресс-режим)' if gallery_mode!='SEPARATE' else ' и подсвечен'))
        else:
            reason='Инспектор не обнаружил ожидаемую ошибку'
            if expected=='DEGENERATE' and not settings.check_degenerate:
                reason='Проверка вырожденной геометрии отключена в настройках'
            elif expected=='SELF_INTERSECTION' and not settings.check_self_intersections:
                reason='Проверка самопересечений отключена в настройках'
            elif expected=='GAP' and (not settings.check_gaps or settings.gap_tolerance_mm<.2):
                reason='Для всех тестовых зазоров включите проверку и порог не менее 0.2 мм'
            self.report({'WARNING'},f'{expected} #{settings.example_number}: {reason}')
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_example_verify(Operator):
    bl_idname = "collision_inspector.example_verify"
    bl_label = "Проверить все 55 примеров"
    bl_description = "Прогнать ядро инспектора на всех примерах без изменения выделения/рабочих событий; сохранить подробный JSON с PASS/FAIL"

    def execute(self, context):
        settings=context.scene.collision_inspector
        root=_examples.owned_root(context.scene,settings.example_root_name)
        if root is None:
            self.report({'WARNING'},'Тестовая коллекция не найдена')
            return {'CANCELLED'}
        _trace_action('EXAMPLES_VERIFY_START',context,collection=root.name)
        try:
            result=_examples.validate_gallery(root,lambda st,obj:_analyse_object(st,obj),settings)
            path=_examples.report_path()
            path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            if result.get('analysis_level')=='AGGREGATE':
                settings.example_last_summary=(f"Стресс-проверка {result['passed']}/{result['total']} Mesh; "
                    f"сбоев {result['failed']}. Не заменяет отдельные 55 тестов")
            else:
                settings.example_last_summary=(f"Проверено {result['passed']}/{result['total']}; "
                    f"обнаружено {result['detection_passed']}, "
                    f"ошибок/неполных визуализаций {result['failed']}")
            settings.example_report_path=str(path)
            _trace_action('EXAMPLES_VERIFY_COMPLETE',context,passed=result['passed'],failed=result['failed'],report=str(path))
            if result['failed']:
                self.report({'WARNING'},settings.example_last_summary + ' — смотрите JSON')
            else:
                self.report({'INFO'},settings.example_last_summary)
            return {'FINISHED'}
        except Exception as exc:
            settings.example_last_summary='Ошибка общей проверки: '+str(exc)
            _trace_action('EXAMPLES_VERIFY_ERROR',context,error=traceback.format_exc())
            self.report({'ERROR'},settings.example_last_summary[:180])
            return {'CANCELLED'}


_FIXTURE_REPORT_CACHE = {'key': None, 'data': None}


def _load_fixture_report(settings, root):
    """Read bounded gallery report with a per-file change-token UI cache."""
    path = Path(settings.example_report_path) if settings.example_report_path else None
    if path is None:
        return None
    try:
        stats = path.stat()
        if stats.st_size > 2_000_000:
            return None
        key = (str(path), stats.st_mtime_ns, stats.st_size, root.name)
        if _FIXTURE_REPORT_CACHE['key'] == key:
            return _FIXTURE_REPORT_CACHE['data']
        report = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(report, dict) or report.get('schema') != _examples.SCHEMA:
            return None
        if report.get('collection') != root.name:
            return None
        if not isinstance(report.get('cases'), list):
            return None
        mode = root.get('ci_gallery_mode','SEPARATE') if hasattr(root, 'get') else 'SEPARATE'
        if mode == 'SEPARATE' and (report.get('total') != 55 or len(report['cases']) != 55):
            return None
        if mode != 'SEPARATE' and (report.get('analysis_level') != 'AGGREGATE' or
                                   report.get('mode') != mode or
                                   len(report['cases']) != (11 if mode == 'CATEGORY' else 1)):
            return None
        _FIXTURE_REPORT_CACHE.update(key=key, data=report)
        return report
    except (OSError, ValueError, TypeError):
        return None


class COLLISIONINSPECTOR_OT_example_next_failed(Operator):
    bl_idname = 'collision_inspector.example_next_failed'
    bl_label = 'Показать следующую ошибку тестирования'
    bl_description = 'Перейти к следующему FAIL/ERROR из отчёта 55 моделей и подсветить ожидаемый дефект'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene is not None

    def execute(self, context):
        settings = context.scene.collision_inspector
        root = _examples.owned_root(context.scene, settings.example_root_name)
        if root is None:
            self.report({'WARNING'}, 'Сначала создайте тестовую коллекцию')
            return {'CANCELLED'}
        report = _load_fixture_report(settings, root)
        if report is None:
            self.report({'WARNING'}, 'Сначала проверьте 55 примеров; отчёт должен принадлежать текущему набору')
            return {'CANCELLED'}
        if root.get('ci_gallery_mode','SEPARATE') != 'SEPARATE':
            self.report({'WARNING'},'Для перехода к отдельным FAIL создайте режим «55 объектов»')
            return {'CANCELLED'}
        failed = [(row.get('category'), row.get('variant'))
                  for row in report['cases'] if row.get('status') != 'PASS']
        failed = [(kind, num) for kind,num in failed
                  if kind in KIND_LABELS and type(num) is int and 1 <= num <= 5]
        if not failed:
            self.report({'INFO'}, 'В отчёте отсутствуют FAIL/ERROR')
            return {'CANCELLED'}
        current = (settings.example_kind,settings.example_number)
        index = (failed.index(current)+1) % len(failed) if current in failed else 0
        kind,number = failed[index]
        settings.example_kind = kind
        settings.example_number = number
        _trace_action('EXAMPLE_NEXT_FAILED', context, category=kind, variant=number,
                      position=index+1, failed_total=len(failed))
        return bpy.ops.collision_inspector.example_focus()


class COLLISIONINSPECTOR_OT_example_copy_report(Operator):
    bl_idname = "collision_inspector.example_copy_report"
    bl_label = "Скопировать путь отчёта"
    bl_description = "Скопировать полный путь к JSON-файлу результатов проверки в буфер обмена"

    def execute(self, context):
        path=context.scene.collision_inspector.example_report_path
        if not path or not Path(path).is_file():
            self.report({'WARNING'},'Отчёт ещё не сформирован или файл не найден')
            return {'CANCELLED'}
        context.window_manager.clipboard=path
        self.report({'INFO'},'Путь к JSON-отчёту скопирован')
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_example_remove(Operator):
    bl_idname = "collision_inspector.example_remove"
    bl_label = "Удалить тестовый набор"
    bl_description = "Удалить только помеченные генератором объекты и коллекции последнего набора; рабочие модели не затрагивать"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Do not delete a mesh that has unapplied Edit Mode changes.
        return context.scene is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self,event)

    def execute(self, context):
        settings=context.scene.collision_inspector
        root=_examples.owned_root(context.scene,settings.example_root_name)
        if root is None:
            self.report({'WARNING'},'Тестовая коллекция не найдена')
            return {'CANCELLED'}
        try:
            token=root.get(_examples.RUN_TAG)
            was_viewing_fixture=any(obj.name==settings.object_name for obj in _examples.meshes_in(root))
            _examples.remove_gallery(root,context.scene)
            if was_viewing_fixture:
                settings.overlay_pick_mode = False
                _reset_results(settings)
                settings.status='Тестовая модель удалена; результаты сброшены'
            settings.example_root_name=''
            settings.example_last_summary='Тестовый набор удалён'
            settings.example_report_path=''
            _trace_action('EXAMPLES_REMOVED',context,token=token)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},'Удаление остановлено ради безопасности: '+str(exc)[:130])
            return {'CANCELLED'}


class COLLISIONINSPECTOR_OT_diagnostics_zip(Operator):
    bl_idname = "collision_inspector.diagnostics_zip"
    bl_label = "Диагностика ZIP"
    bl_description = "Собрать ZIP с логами, UI, action trace и выборкой проблемных элементов; скопировать путь"

    def execute(self, context):
        _trace_action('GENERATE_DIAGNOSTICS_ZIP',context)
        try:
            path = _write_diagnostics_zip(context)
        except Exception as exc:
            self.report({'ERROR'},f'ZIP не создан: {type(exc).__name__}: {exc}')
            return {'CANCELLED'}
        context.window_manager.clipboard = str(path)
        self.report({'INFO'},'Диагностический ZIP создан. Путь скопирован в буфер обмена')
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_copy_repair_log(Operator):
    bl_idname = "collision_inspector.copy_repair_log"
    bl_label = "Скопировать лог исправления"
    bl_description = "Скопировать статистику последнего исправления в буфер обмена"

    def execute(self, context):
        text = context.scene.collision_inspector.repair_last_log
        if not text:
            self.report({'INFO'}, "Исправление ещё не запускалось")
            return {'CANCELLED'}
        context.window_manager.clipboard = text
        self.report({'INFO'}, "Лог исправления скопирован")
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_copy_log_path(Operator):
    bl_idname = "collision_inspector.copy_log_path"
    bl_label = "Скопировать путь к логу"
    bl_description = "Записать диагностический текстовый лог в Загрузки (или временную папку) и скопировать полный путь"

    def execute(self, context):
        _trace_action('COPY_LOG_PATH', context)
        try:
            home = Path.home() / 'Downloads'
            destination = home if home.is_dir() else Path(tempfile.gettempdir())
            name = 'CollisionInspector_Log_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.log'
            target = destination / name
            target.write_text(_make_log(context), encoding='utf-8')
            context.window_manager.clipboard = str(target)
        except (OSError, PermissionError, RuntimeError) as exc:
            self.report({'ERROR'}, 'Не удалось создать лог: ' + str(exc)[:150])
            return {'CANCELLED'}
        self.report({'INFO'}, 'Путь к логу скопирован: ' + str(target))
        return {'FINISHED'}


class COLLISIONINSPECTOR_OT_copy_log(Operator):
    bl_idname = "collision_inspector.copy_log"
    bl_label = "Скопировать лог"
    bl_description = "Скопировать полный диагностический лог в буфер обмена"

    def execute(self, context):
        text = _make_log(context)
        context.window_manager.clipboard = text
        self.report({"INFO"}, f"Лог скопирован: {len(text)} символов")
        return {"FINISHED"}


class COLLISIONINSPECTOR_OT_copy_error(Operator):
    bl_idname = "collision_inspector.copy_error"
    bl_label = "Скопировать traceback"
    bl_description = "Скопировать последний traceback проверки"

    def execute(self, context):
        text = context.scene.collision_inspector.last_error
        if not text:
            self.report({"INFO"}, "Последнего traceback нет")
            return {"CANCELLED"}

        context.window_manager.clipboard = text
        self.report({"INFO"}, "Traceback скопирован")
        return {"FINISHED"}


class COLLISIONINSPECTOR_OT_clear(Operator):
    bl_idname = "collision_inspector.clear"
    bl_label = "Очистить"
    bl_description = "Очистить результаты проверки; геометрия не изменяется"

    def execute(self, context):
        settings = context.scene.collision_inspector
        _reset_results(settings)
        settings.status = "Результаты очищены"
        return {"FINISHED"}


# =============================================================================
# UI
# =============================================================================


class COLLISIONINSPECTOR_UL_events(UIList):
    def filter_items(self, context, data, propname):
        flags = [self.bitflag_filter_item if _visible_issue(item.kind, data.issue_filter)
                 else 0 for item in getattr(data, propname)]
        return flags, []

    def draw_item(
        self,
        context,
        layout,
        data,
        item,
        icon,
        active_data,
        active_property,
        index=0,
        flt_flag=0,
    ):
        row = layout.row(align=True)
        row.label(text="", icon=_ui_icon(KIND_ICONS.get(item.kind, "ERROR"), "ERROR"))
        row.label(text=item.text)
        if index == data.event_index:
            row.label(text="", icon=_ui_icon("HIDE_OFF"))


class COLLISIONINSPECTOR_PT_main(Panel):
    bl_label = "Collision Inspector"
    bl_idname = "COLLISIONINSPECTOR_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        active = context.active_object

        target = layout.box()
        row = target.row(align=True)
        if active is not None and active.type == "MESH":
            row.label(text=active.name, icon=_ui_icon("MESH_DATA"))
        else:
            row.label(text="Выберите Mesh-объект", icon=_ui_icon("INFO"))
        if not _supported_version():
            target.label(text=f"Blender {bpy.app.version_string} не поддерживается", icon=_ui_icon("ERROR"))

        run = layout.column(align=True)
        run.scale_y = 1.5
        run.operator("collision_inspector.check", text="ПРОВЕРИТЬ ОБЪЕКТ", icon=_ui_icon("VIEWZOOM"))

        summary = layout.box()
        summary.label(text=settings.status, icon=_ui_icon("ERROR" if settings.events else "CHECKMARK"))
        if settings.object_name:
            top = summary.row(align=True)
            top.label(text=f"Оболочек: {settings.shell_count}")
            top.label(text=f"Проблем: {len(settings.events)}")
            # A summary, not a second report: all details belong to Problems and ZIP.
            if settings.last_error:
                summary.label(text="Сбой проверки — см. лог", icon=_ui_icon("ERROR"))
            gpu_error = _viewport_ui.draw_error()
            if gpu_error:
                summary.label(text="Ошибка GPU: " + gpu_error[:75], icon=_ui_icon("ERROR"))
            if settings.events and not settings.overlay_enabled:
                summary.label(text="Подсветка отключена; запустите проверку", icon=_ui_icon("INFO"))


class COLLISIONINSPECTOR_PT_findings(Panel):
    bl_label = "Проблемы"
    bl_idname = "COLLISIONINSPECTOR_PT_findings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "collision_inspector", None)
        return bool(settings and settings.events)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        layout.prop(settings, "issue_filter", text="Группа")
        layout.template_list(
            "COLLISIONINSPECTOR_UL_events", "", settings, "events", settings, "event_index",
            rows=min(max(len(settings.events), 3), 5),
        )
        index = min(settings.event_index, len(settings.events) - 1)
        event = settings.events[index]

        # Single source of navigation: footer and arrows in Viewport.
        # The sidebar retains the list and the explicitly requested frame action.
        op = layout.operator("collision_inspector.frame", text="Приблизить выбранную", icon=_ui_icon("VIEWZOOM")); op.side = "BOTH"

        if event.shell_b >= 0 and event.indices_b:
            sides = layout.row(align=True)
            sides.prop(settings, "overlay_side", expand=True)

        details = layout.column(align=True)
        details.label(text=f"{index + 1}/{len(settings.events)} · {KIND_LABELS.get(event.kind, event.kind)}")
        if event.detail:
            # Full technical explanation remains in diagnostics/hover; no 10-line N-panel.
            details.label(text=event.detail[:57] + ('…' if len(event.detail)>57 else ''))
        if event.trace_segments or event.overlap_polygons or event.has_witness_pair:
            witness_row = details.column(align=True)
            try:
                if event.trace_segments:
                    witness_row.label(text=f"Точные отрезки: {len(json.loads(event.trace_segments))}", icon=_ui_icon("CURVE_PATH"))
                if event.overlap_polygons:
                    witness_row.label(text=f"Контуры наложения: {len(json.loads(event.overlap_polygons))}", icon=_ui_icon("MESH_GRID"))
            except (TypeError, ValueError):
                witness_row.label(text="Некорректные данные подсветки", icon=_ui_icon("ERROR"))
            if event.has_witness_pair:
                witness_row.label(text=f"Точное расстояние: {event.metric_value:.6g} мм")

        select_row = layout.row(align=True)
        op = select_row.operator("collision_inspector.view", text="ВЫДЕЛИТЬ ГЕОМЕТРИЮ (Edit)", icon=_ui_icon("EDITMODE_HLT")); op.side = "BOTH"
        action = layout.column(align=True)
        if event.kind == "NONCONVEX":
            if event.repair_available:
                action.operator('collision_inspector.apply_suggested', text='ДОСТУПНО ИСПРАВЛЕНИЕ', icon=_ui_icon('CHECKMARK'))
            else:
                action.label(text=(event.repair_reason or 'Безопасное исправление не найдено')[:70], icon=_ui_icon('INFO'))
        elif event.kind == "UNCLOSED" and event.shell_a < 0:
            action.operator("collision_inspector.basic_repair", text="УДАЛИТЬ LOOSE GEOMETRY", icon=_ui_icon("BRUSH_DATA"))
        else:
            action.label(text="Автоизменение для этого типа отключено", icon=_ui_icon("LOCKED"))
        action.operator("collision_inspector.recheck", text="ПЕРЕПРОВЕРИТЬ", icon=_ui_icon("FILE_REFRESH"))


class COLLISIONINSPECTOR_PT_viewport(Panel):
    bl_label = "Внешний вид · дополнительно"
    bl_idname = "COLLISIONINSPECTOR_PT_viewport"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        layout.label(text="Режимы и стрелки — прямо во Viewport")
        layout.prop(settings, "overlay_enabled", text="Подсветка")
        col = layout.column(align=True)
        col.enabled = settings.overlay_enabled
        row = col.row(align=True)
        row.prop(settings, "overlay_hud", text="Карточка")
        row.prop(settings, "overlay_xray", text="X-Ray")
        col.prop(settings, "overlay_opacity", slider=True)
        if settings.events:
            active = settings.events[min(settings.event_index,len(settings.events)-1)]
            if active.indices_b:
                col.prop(settings, "overlay_side", expand=True)
        col.label(text="Верх: маркеры · Низ: навигация")
        col.label(text="Esc скрывает инспектор; Ctrl+Shift+M возвращает")
        layout.label(text="Только GPU, Mesh не меняется", icon=_ui_icon("LOCKED"))
        overlay_error = _viewport_ui.draw_error()
        if overlay_error:
            for line in _wrap_text(f"GPU: {overlay_error}", 42):
                layout.label(text=line, icon=_ui_icon("ERROR"))


class COLLISIONINSPECTOR_PT_repair(Panel):
    bl_label = "Исправление"
    bl_idname = "COLLISIONINSPECTOR_PT_repair"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        col = layout.column(align=True)
        if settings.events:
            op = col.operator("collision_inspector.view", text="Выделить проблемную геометрию (Edit)", icon=_ui_icon("EDITMODE_HLT"))
            op.side = 'BOTH'
        col.label(text=f'Безопасные предложения: {settings.repair_preview_count}')
        col.operator('collision_inspector.apply_bulk_suggested', text='План исправления всех · просмотр', icon=_ui_icon('MOD_TRIANGULATE'))
        col.operator("collision_inspector.auto_repair", text="АВТОИСПРАВЛЕНИЕ (старый режим)", icon=_ui_icon("CHECKMARK"))
        row = col.row(align=True)
        row.operator("collision_inspector.basic_repair", text="Очистка", icon=_ui_icon("BRUSH_DATA"))
        row.operator("collision_inspector.optimize_triangulation", text="Диагонали", icon=_ui_icon("MOD_TRIANGULATE"))
        layout.label(text="Неудачный перебор автоматически откатывается.", icon=_ui_icon("LOCKED"))
        if settings.repair_last_status:
            box = layout.box()
            for i, line in enumerate(_wrap_text(settings.repair_last_status, 50)):
                box.label(text=line, icon=_ui_icon("INFO" if i == 0 else "BLANK1"))
            box.operator("collision_inspector.copy_repair_log", text="Скопировать лог исправления", icon=_ui_icon("COPYDOWN"))


class COLLISIONINSPECTOR_PT_settings(Panel):
    bl_label = "Настройки"
    bl_idname = "COLLISIONINSPECTOR_PT_settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        checks = layout.box()
        checks.label(text="ПРОВЕРКИ")
        checks.prop(settings, "check_degenerate")
        checks.prop(settings, "check_self_intersections")
        checks.prop(settings, "check_gaps")
        checks.prop(settings, "convex_angle")
        if settings.check_gaps:
            checks.prop(settings, "gap_tolerance_mm")
        checks.prop(settings, "auto_zoom")
        checks.prop(settings, "overlay_nav_focus")

        clean = layout.box()
        clean.label(text="БЕЗОПАСНАЯ ОЧИСТКА")
        clean.prop(settings, "repair_remove_loose")
        clean.prop(settings, "repair_recalc_normals")
        clean.prop(settings, "repair_dissolve_coplanar")
        if settings.repair_dissolve_coplanar:
            clean.prop(settings, "repair_coplanar_angle")

        tri = layout.box()
        tri.label(text="ДИАГОНАЛИ")
        tri.prop(settings, "repair_triangulation_method")
        tri.prop(settings, "repair_max_flip_attempts")
        tri.prop(settings, "repair_allow_existing_diagonals")
        tri.separator()
        tri.prop(settings, 'repair_preview_enabled')
        if settings.repair_preview_enabled:
            tri.prop(settings, 'repair_preview_max_shells')
            tri.prop(settings, 'repair_preview_max_faces')
            tri.prop(settings, 'repair_preview_attempts')
        if settings.repair_allow_existing_diagonals:
            tri.label(text="Расширенный режим может менять поверхность", icon=_ui_icon("ERROR"))
            tri.label(text="неплоской пары треугольников.")


class COLLISIONINSPECTOR_PT_examples(Panel):
    bl_label = "Тестовые примеры · 55"
    bl_idname = "COLLISIONINSPECTOR_PT_examples"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.collision_inspector
        samples=layout.box()
        samples.label(text='ТЕСТОВЫЕ МОДЕЛИ · 11 × 5',icon=_ui_icon('MESH_CUBE'))
        samples.prop(settings,'example_structure',text='Структура')
        label_row=samples.row()
        label_row.enabled=settings.example_structure!='ONE'
        label_row.prop(settings,'example_labels',text='Подписи')
        if settings.example_structure=='ONE':
            samples.label(text='В общем Mesh надписей не будет')
        samples.operator('collision_inspector.example_create', text='СОЗДАТЬ 55 ПРИМЕРОВ', icon=_ui_icon('ADD'))
        root=_examples.owned_root(context.scene,settings.example_root_name)
        if root:
            samples.label(text=f'Коллекция: {root.name[:30]}')
            gallery_mode=root.get('ci_gallery_mode','SEPARATE')
            count=len(_examples.meshes_in(root))
            samples.label(text=f'55 кейсов → {count} Mesh; режим {gallery_mode}')
            samples.prop(settings,'example_kind',text='Проблема')
            samples.prop(settings,'example_number',text='Пример')
            samples.operator('collision_inspector.example_focus',text='ОТКРЫТЬ И ПРОВЕРИТЬ',icon=_ui_icon('VIEWZOOM'))
            samples.operator('collision_inspector.example_verify',
                text='ПРОВЕРИТЬ 55 ОТДЕЛЬНО' if gallery_mode=='SEPARATE' else 'СТРЕСС-ПРОВЕРКА MESH',
                icon=_ui_icon('CHECKMARK'))
            if settings.example_last_summary:
                for line in _wrap_text(settings.example_last_summary,45):
                    samples.label(text=line)
            if settings.example_report_path:
                samples.label(text='JSON: '+Path(settings.example_report_path).name[:43])
                report = _load_fixture_report(settings, root)
                if report and gallery_mode=='SEPARATE':
                    for kind, title in _EXAMPLE_CATEGORIES:
                        group = [row for row in report['cases'] if row.get('category') == kind]
                        passed = sum(row.get('status') == 'PASS' for row in group)
                        row = samples.row(align=True)
                        row.label(text=f'{title[:21]}: {passed}/5',
                                  icon=_ui_icon('CHECKMARK' if passed==5 else 'ERROR'))
                    if report.get('failed', 0):
                        samples.operator('collision_inspector.example_next_failed',
                                         text='ПОКАЗАТЬ СЛЕДУЮЩИЙ FAIL',icon=_ui_icon('VIEWZOOM'))
                elif report:
                    samples.label(text='Агрегат: не заменяет 55 отдельных тестов',icon=_ui_icon('INFO'))
                else:
                    samples.label(text='Отчёт чужой, устарел или повреждён',icon=_ui_icon('ERROR'))
                samples.operator('collision_inspector.example_copy_report',text='Скопировать путь JSON',icon=_ui_icon('COPYDOWN'))
            samples.operator('collision_inspector.example_remove',text='Удалить тестовый набор…',icon=_ui_icon('TRASH'))
        else:
            samples.label(text='Раздельные: точные 55 тестов')
            samples.label(text='Объединённые: стресс-анализ')
            samples.label(text='Рабочая модель и выделение сохраняются')


class COLLISIONINSPECTOR_PT_diagnostics(Panel):
    bl_label = "Диагностика"
    bl_idname = "COLLISIONINSPECTOR_PT_diagnostics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Collision"
    bl_parent_id = "COLLISIONINSPECTOR_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        self.layout.operator("collision_inspector.copy_log_path",
                             text="Скопировать путь к логу", icon=_ui_icon("COPYDOWN"))

# =============================================================================
# Registration
# =============================================================================


_CI_KEYMAPS = []


classes = (
    COLLISIONINSPECTOR_Finding,
    COLLISIONINSPECTOR_Settings,
    COLLISIONINSPECTOR_OT_check,
    COLLISIONINSPECTOR_OT_view,
    COLLISIONINSPECTOR_OT_previous,
    COLLISIONINSPECTOR_OT_next,
    COLLISIONINSPECTOR_OT_recheck,
    COLLISIONINSPECTOR_OT_frame,
    COLLISIONINSPECTOR_OT_pick_markers,
    COLLISIONINSPECTOR_OT_basic_repair,
    COLLISIONINSPECTOR_OT_optimize_triangulation,
    COLLISIONINSPECTOR_OT_auto_repair,
    COLLISIONINSPECTOR_OT_apply_suggested,
    COLLISIONINSPECTOR_OT_apply_bulk_suggested,
    COLLISIONINSPECTOR_OT_example_create,
    COLLISIONINSPECTOR_OT_example_focus,
    COLLISIONINSPECTOR_OT_example_verify,
    COLLISIONINSPECTOR_OT_example_remove,
    COLLISIONINSPECTOR_OT_example_copy_report,
    COLLISIONINSPECTOR_OT_example_next_failed,
    COLLISIONINSPECTOR_OT_diagnostics_zip,
    COLLISIONINSPECTOR_OT_copy_repair_log,
    COLLISIONINSPECTOR_OT_copy_log,
    COLLISIONINSPECTOR_OT_copy_log_path,
    COLLISIONINSPECTOR_OT_copy_error,
    COLLISIONINSPECTOR_OT_clear,
    COLLISIONINSPECTOR_UL_events,
    COLLISIONINSPECTOR_PT_main,
    COLLISIONINSPECTOR_PT_repair,
    COLLISIONINSPECTOR_PT_examples,
    COLLISIONINSPECTOR_PT_diagnostics,
    COLLISIONINSPECTOR_PT_settings,
)


def register():
    # Registration is a transaction: an API change must not leave behind
    # partially registered Blender types, handlers, timers or keymaps.
    registered = []
    try:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.collision_inspector = PointerProperty(type=COLLISIONINSPECTOR_Settings)
        _viewport_ui.install()
        if not bpy.app.background:
            wm = bpy.context.window_manager
            kc = getattr(getattr(wm, 'keyconfigs', None), 'addon', None)
            if kc is not None:
                km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
                kmi = km.keymap_items.new('collision_inspector.pick_markers', 'M', 'PRESS', ctrl=True, shift=True)
                _CI_KEYMAPS.append((km, kmi))
    except Exception:
        for km, kmi in list(_CI_KEYMAPS):
            try:
                km.keymap_items.remove(kmi)
            except (RuntimeError, ReferenceError, ValueError):
                pass
        _CI_KEYMAPS.clear()
        try:
            _viewport_ui.uninstall()
        except Exception:
            pass
        if hasattr(bpy.types.Scene, 'collision_inspector'):
            del bpy.types.Scene.collision_inspector
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError:
                pass
        raise


def unregister():
    _PICKER_CONFIRM_SCENES.clear()
    _PICKER_REARM_TRIED.clear()
    _PICKER_SESSIONS.clear()
    for km,kmi in list(_CI_KEYMAPS):
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError,ReferenceError,ValueError):
            pass
    _CI_KEYMAPS.clear()
    _viewport_ui.uninstall()
    if hasattr(bpy.types.Scene, "collision_inspector"):
        del bpy.types.Scene.collision_inspector

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
