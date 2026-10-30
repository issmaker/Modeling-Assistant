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
from ..core.material_intelligence import inspect_material_asset, material_image_candidates
from ..core.problem_registry import active_problem
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


bl_info = {'version': (1, 17, 5)}
from .preferences import active_ui_mode, get_preferences

class VIEW3D_PT_uv_wall(bpy.types.Panel):
    bl_label = "UV Wall"
    bl_idname = "VIEW3D_PT_uv_wall"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UV Wall"

    def draw(self, context):
        """Small N-sidebar: authoring stays in the actual Viewport inspector.

        This panel deliberately does not expose any experimental joining
        algorithms, legacy/advanced build forms, or debug geometry buttons.
        Existing .blend RNA fields remain available for backward compatibility.
        """
        from ..core.version import VERSION_STRING
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.uv_wall_settings
        prefs = get_preferences(context)
        active = bool(getattr(context.window_manager, "uvwall_viewport_active", False))

        head = layout.box()
        row = head.row(align=True)
        row.label(text=f"UV WALL · {VERSION_STRING}", icon="VIEW3D")
        if active:
            row.label(text="LIVE", icon="CHECKMARK")
        if prefs is not None:
            head.prop(prefs, "ui_mode", text="Интерфейс")

        project = layout.box()
        project.label(text="ПРОЕКТ", icon="OUTLINER_OB_MESH")
        project.prop(settings, "build_mode", text="Режим")
        path_row = project.row(align=True)
        path_row.prop(settings, "path_mesh", text="Путь")
        path_row.operator(OBJECT_OT_uv_wall_use_active_as_path.bl_idname,
                          text="", icon="EYEDROPPER")
        definition = (definition_for(settings.path_mesh)
                      if settings.path_mesh is not None else None)
        if definition is None:
            project.label(text="Укажите Mesh-путь", icon="INFO")
        else:
            name = str(getattr(definition, "last_result_name", "") or "")
            obj = bpy.data.objects.get(name) if name else None
            project.label(text="Result: " + ("готов" if obj else "не построен"),
                          icon="CHECKMARK" if obj else "INFO")

        launch = layout.box()
        row = launch.row()
        row.scale_y = 1.35
        if active:
            row.operator("uvwall.viewport_inspector_exit", text="Закрыть инспектор", icon="X")
        else:
            row.enabled = bool(settings.path_mesh or
                               (context.active_object and context.active_object.type == "MESH"))
            row.operator("uvwall.viewport_inspector", text="Открыть инструмент", icon="PLAY")
        if len(settings.problems):
            issue = launch.row(align=True)
            issue.alert = True
            issue.label(text=f"Проблем: {len(settings.problems)}", icon="ERROR")
            issue.operator("object.uv_wall_problem_navigate", text="Показать", icon="VIEWZOOM").direction = 0

        # Persistent PARAMETER history (not a mesh backup). Restoring an
        # entry loads parameters; the existing transactional Build publishes
        # geometry and provides Blender Undo.
        from ..operators.revisions import revision_path_and_ledger
        from ..core.revisions import describe_revision
        _history_path, _history_def, _history = revision_path_and_ledger(context)
        if _history is not None and _history['entries']:
            revisions_box = layout.box()
            revisions_box.label(text='ИСТОРИЯ ПРИНЯТЫХ СТЕН', icon='RECOVER_LAST')
            revisions_box.label(text='Параметры; Mesh изменится только после Build', icon='INFO')
            for item in reversed(_history['entries'][-5:]):
                row = revisions_box.row(align=True)
                row.label(text=f"#{item['id']} · {item['label'][:25]}")
                select = row.row(align=True)
                select.enabled = not active
                select.operator('object.uvwall_load_revision', text='Загрузить', icon='IMPORT').revision_id = item['id']
                revisions_box.label(text=describe_revision(item,_history,max_fields=2)[:95])

        # PPA-W4: optional persistent Mesh recovery. UI checks only cheap
        # metadata, never hashes a production Mesh on every panel redraw.
        from ..core.mesh_recovery import recovery_status
        backup_obj = getattr(context, 'active_object', None)
        if backup_obj is not None and getattr(backup_obj, 'type', None) == 'MESH' and backup_obj.get('uvwall_generated'):
            mesh_recovery_box = layout.box()
            mesh_recovery_box.label(text='ВОССТАНОВЛЕНИЕ MESH', icon='FILE_BACKUP')
            snapshot_state = recovery_status(backup_obj, bpy.data)
            if snapshot_state['available']:
                mesh_recovery_box.label(text=f"Последняя принятая версия · Build {snapshot_state['revision']}", icon='CHECKMARK')
                recovery_row = mesh_recovery_box.row()
                recovery_row.enabled = not active and context.mode == 'OBJECT'
                recovery_row.operator('object.uvwall_restore_mesh_snapshot', text='Восстановить Mesh', icon='LOOP_BACK')
            else:
                mesh_recovery_box.label(text='Резерв недоступен: '+snapshot_state['code'][:65], icon='INFO')

        support = layout.box()
        support.label(text="ДИАГНОСТИКА", icon="HELP")
        row = support.row(align=True)
        row.operator(OBJECT_OT_uv_wall_copy_log.bl_idname, text="Лог", icon="COPYDOWN")
        row.operator(OBJECT_OT_uv_wall_collect_support.bl_idname,
                     text="Собрать ZIP", icon="EXPORT")

