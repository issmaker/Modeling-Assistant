from .core.version import VERSION
bl_info = {
    "name": "UV Wall Professional — NEXTGEN",
    "author": "issmaker",
    "version": (2, 0, 0),
    "blender": (4, 0, 0),
    "location": "3D View > UV Wall Viewport Inspector",
    "description": "Modular viewport-first parametric UV Wall/Surface/Cornice",
    "category": "Object",
}
from .core.startup_trace import report_failure as _uvwall_report_startup_failure
_UVWALL_BOOT_STAGE = "initialization"
try:
    _UVWALL_BOOT_STAGE = "import.bpy"
    import bpy
    import bmesh
    import gpu
    _UVWALL_BOOT_STAGE = "feature_gate"
    from .core.blender_compat import assert_supported_environment as _uvwall_check_blender
    _UVWALL_RUNTIME_API = _uvwall_check_blender(bpy, bmesh, gpu)
    import importlib
    import sys
    # Installation preflight MUST run before importing production.surface.  In a
    # mixed-version installation surface.py may request cap functions absent from the
    # old cap_surface_geometry.py; otherwise Blender reports an opaque ImportError.
    from pathlib import Path as _UVWALL_Path
    _UVWALL_BOOT_STAGE = "package_preflight"
    from .core.package_preflight import assert_package_integrity as _uvwall_assert_package_integrity
    _uvwall_assert_package_integrity(_UVWALL_Path(__file__).resolve().parent, bl_info["version"])
    # Clean up the old classes BEFORE reloading Python modules: avoids stale RNA.
    if globals().get("_UVWALL_EXECUTED", False):
        try:
            unregister()
        except Exception:
            pass
        for _suffix in (
            "core.version", "core.support_mesh_audit", "core.blender_compat", "core.definitions", "core.virtual_assets", "core.surface_points",
            "core.semantic_geometry", "core.result_lifecycle", "core.diagnostics",
            "core.performance", "core.runtime_identity", "core.support_logging", "core.build_guard", "core.material_intelligence", "core.problem_registry", "core.undo_transactions", "core.undo_handlers", "ui.numeric_input", "preview.engine", "viewport.surface_ui", "viewport.cornice_direct", "viewport.spatial_layout", "viewport.workspace_layout", "viewport.parameter_windows", "viewport.resource_picker",
            "production.corner_methods", "production.mirror_corner_integrity", "production.cap_surface_geometry", "production.validation", "production.uv_robustness", "production.cornice_corner_preflight", "production.path", "production.wall_surface",
            "production.surface", "production.wall", "production.cornice", "production.cap_uv_integrity", "production.normal_math", "production.normal_integrity",
            "production.inner_height", "production.secondary_points", "production.wall_seams", "production.wall_topology", "production.wall_uv_field", "production.vertex_minimal", "production.topology_first", "production.cap_boundary_plan", "production.corner_audit", "production.rotation", "production.dependent_surface", "production.wall_alignment", "production.surface_coverage", "production.cap_grid", "production.vertical_uv",
            "operators.assignment", "operators.build_context", "operators.build", "operators.diagnostics", "operators.problems",
            "operators.learning", "operators.revisions", "core.revisions", "operators.mesh_recovery", "core.mesh_recovery", "operators.numeric", "operators.context_editor", "operators.segment_joint_preview", "production.segment_joint_graph", "production.true_corner_trim",
            "ui.settings", "ui.panel", "ui.preferences", "ui.studio_dock", "ui.dock_workspace_state", "viewport.studio_dock_layout", "viewport.controller", "viewport.tool_eligibility",
        ):
            _module = sys.modules.get(f"{__package__}.{_suffix}")
            if _module:
                importlib.reload(_module)
    _UVWALL_EXECUTED = True
    _UVWALL_BOOT_STAGE = "runtime_imports"
    from .core import definitions as _definitions
    from .core.virtual_assets import purge_virtual_cache
    from .core.result_lifecycle import shutdown_result_lifecycle
    from .core.performance import clear_performance_caches
    from .core.build_guard import reset_build_guard
    from .core.support_logging import initialize_logging_session
    from .core import undo_handlers as _undo_handlers
    from .viewport import controller as _viewport_controller
    from .ui import preferences as _ui_preferences
    from .ui import studio_dock as _studio_dock
    # Runtime source guard runs in register(), so every guarded module must
    # already be imported. Pass 50 accidentally omitted this pure-Python module.
    from .production import secondary_points as _secondary_points
    from .production import validation, path, wall_surface, surface, wall, cornice, inner_height, topology_first, cap_boundary_plan
    from .operators import assignment, build, diagnostics, problems, revisions, mesh_recovery, learning, numeric, context_editor, segment_joint_preview
    from .ui import settings, panel
    _MODULES = (validation, path, wall_surface, surface, wall, cornice,
                assignment, build, diagnostics, problems, learning, settings, panel)
    # Each function uses its own module global namespace, not the old root monolith.
    # Wire only dependencies that are defined in our extracted modules.
    _exports = {}
    for _module in _MODULES:
        for _name, _value in vars(_module).items():
            if getattr(_value, "__module__", None) == _module.__name__ and not _name.startswith('__'):
                _exports[_name] = _value
    for _module in _MODULES:
        for _name, _value in _exports.items():
            _module.__dict__.setdefault(_name, _value)
        _module.__dict__["bl_info"] = bl_info
    # Keep root compatibility with existing scripts/operator string lookups.
    globals().update(_exports)
    _UVWALL_BOOT_STAGE = "class_manifest"
    classes = (
        _ui_preferences.UVWALL_AddonPreferences,
        _studio_dock.UVWALL_OT_studio_set_mode,
        _studio_dock.UVWALL_OT_studio_build,
        _studio_dock.UVWALL_OT_hover_palette,
        _studio_dock.UVWALL_OT_inner_height_point,
        _studio_dock.UVWALL_OT_dock_field,
        _studio_dock.UVWALL_OT_dock_switch,
        _studio_dock.UVWALL_OT_dock_resource,
        _studio_dock.UVWALL_OT_dock_resource_action,
        _studio_dock.UVWALL_OT_dock_presets,
        settings.UVWALL_PG_corner_marker,
        settings.UVWALL_PG_problem,
        settings.UVWALL_PG_settings,
        assignment.OBJECT_OT_uv_wall_use_active_as_path,
        assignment.OBJECT_OT_uv_wall_use_active_as_source,
        assignment.OBJECT_OT_uv_wall_assign_from_selection,
        assignment.OBJECT_OT_uv_wall_load_texture,
        assignment.OBJECT_OT_uv_wall_use_active_as_profile,
        assignment.OBJECT_OT_uv_wall_add_cornice_corners,
        assignment.OBJECT_OT_uv_wall_mark_cornice_smooth,
        assignment.OBJECT_OT_uv_wall_use_active_material,
        assignment.OBJECT_OT_uv_wall_use_material_image,
        assignment.OBJECT_OT_uv_wall_remove_cornice_corner,
        assignment.OBJECT_OT_uv_wall_clear_cornice_corners,
        assignment.OBJECT_OT_uv_wall_create_reference,
        build.OBJECT_OT_build_uv_wall_along_path,
        diagnostics.OBJECT_OT_uv_wall_copy_log,
        diagnostics.OBJECT_OT_uv_wall_save_log,
        diagnostics.OBJECT_OT_uv_wall_collect_support,
        diagnostics.OBJECT_OT_uv_wall_open_logs_folder,
        diagnostics.OBJECT_OT_uv_wall_clear_log,
        diagnostics.OBJECT_OT_uv_wall_create_stress_test,
        diagnostics.OBJECT_OT_uv_wall_quick_check,
        problems.OBJECT_OT_uv_wall_problem_navigate,
        problems.OBJECT_OT_uv_wall_problem_refresh,
        problems.OBJECT_OT_uv_wall_problem_clear,
        problems.OBJECT_OT_uv_wall_problem_make_sharp,
        problems.OBJECT_OT_uv_wall_restore_last_successful,
        revisions.OBJECT_OT_uvwall_load_revision,
        mesh_recovery.OBJECT_OT_uvwall_restore_mesh_snapshot,
        learning.OBJECT_OT_uv_wall_set_mode,
        learning.OBJECT_OT_uv_wall_create_learning_example,
        learning.OBJECT_OT_uv_wall_learning_build,
        learning.OBJECT_OT_uv_wall_learning_step,
        learning.OBJECT_OT_uv_wall_support,
        numeric.UVWALL_OT_numeric_parameter,
        context_editor.UVWALL_OT_context_editor,
        panel.VIEW3D_PT_uv_wall,
    )
except Exception as _uvwall_boot_error:
    _uvwall_boot_log = _uvwall_report_startup_failure(
        _UVWALL_BOOT_STAGE, _uvwall_boot_error, blender=globals().get("bpy"), addon_version="2.0.0")
    raise RuntimeError(
        f"UV Wall 2.0.0 bootstrap stage={_UVWALL_BOOT_STAGE}: "
        f"{type(_uvwall_boot_error).__name__}: {_uvwall_boot_error}; "
        f"FULL TRACE: {_uvwall_boot_log}") from _uvwall_boot_error

def _register_class_replace_stale(cls):
    stale = getattr(bpy.types, cls.__name__, None)
    if stale is not None and stale is not cls:
        try:
            bpy.utils.unregister_class(stale)
        except Exception:
            pass
    if not bool(getattr(cls, "is_registered", False)):
        bpy.utils.register_class(cls)
# Keep this synchronized with core.definitions; Blender install must never ship
# a root guard left over from an older release. Checked before registering UI.
EXPECTED_PATH_DEFINITION_SCHEMA = 5
_REGISTERED = False
def _validate_registered_schema():
    live_version = getattr(_definitions, "CURRENT_SCHEMA_VERSION", None)
    version_label = ".".join(map(str, bl_info["version"]))
    if live_version != EXPECTED_PATH_DEFINITION_SCHEMA:
        raise RuntimeError(
            f"UV Wall {version_label}: mixed Python modules; loaded Path Definition "
            f"Schema {live_version}, expected Schema {EXPECTED_PATH_DEFINITION_SCHEMA}. "
            "Close Blender completely, remove the old UV_Wall_Professional add-on folder "
            "and install the new ZIP."
        )
    ok, reason = _definitions.runtime_schema_status()
    if not ok:
        raise RuntimeError(
            f"UV Wall {version_label}: Path Definition Schema "
            f"{EXPECTED_PATH_DEFINITION_SCHEMA} RNA registration failed: {reason}. "
            "Close Blender completely before reinstalling."
        )
    return True
def register():
    """Idempotent enable; a partially failed enable is always rolled back.
    Never wipe the live Scene properties when Blender calls register twice in
    one Python session.  Failed registration is reported with its real traceback;
    cleanup errors must not conceal the original failure.
    """
    global _REGISTERED
    _uvwall_stage = "register.start"
    if _REGISTERED:
        return
    try:
        _uvwall_stage = "register.clear_stale_scene_pointer"
        if hasattr(bpy.types.Scene, "uv_wall_settings"):
            del bpy.types.Scene.uv_wall_settings
        # Unit tests execute register() in an isolated namespace without a
        # package. The guard is mandatory in the installed Blender package.
        _uvwall_stage = "register.runtime_identity"
        if globals().get('__package__') == 'UV_Wall_Professional':
            from .core.runtime_identity import verify_critical_geometry_sources
            source_errors = verify_critical_geometry_sources()
        else:
            source_errors = []
        if source_errors and any(str(err).startswith('NOT_LOADED:') for err in source_errors):
            raise RuntimeError(
                "UV Wall: ошибка порядка загрузки модулей перед проверкой runtime_identity; "
                "NOT_LOADED само по себе не означает смешанную установку. "
                "Причины: " + "; ".join(source_errors)
            )
        if source_errors:
            raise RuntimeError(
                "UV Wall: загруженные функции не соответствуют исходникам текущей установки. "
                "Полностью закройте Blender, замените папку UV_Wall_Professional "
                "и запустите заново. Причины: " + "; ".join(source_errors)
            )
        _uvwall_stage = "register.initialize_logging"
        _init_logs = globals().get("initialize_logging_session")
        if callable(_init_logs):
            _init_logs()
        _uvwall_stage = "register.path_definition"
        _definitions.register()
        _uvwall_stage = "register.schema_check"
        _validate_registered_schema()
        for cls in classes:
            _uvwall_stage = f"register.class.{cls.__name__}"
            _register_class_replace_stale(cls)
        _uvwall_stage = "register.scene_settings_pointer"
        bpy.types.Scene.uv_wall_settings = bpy.props.PointerProperty(type=settings.UVWALL_PG_settings)
        _uvwall_stage = "register.viewport_controller"
        _viewport_controller.register()
        _uvwall_stage = "register.undo_handlers"
        _undo_handlers.register()
    except Exception as _uvwall_register_error:
        _uvwall_register_log = _uvwall_report_startup_failure(
            _uvwall_stage, _uvwall_register_error, blender=bpy, addon_version="2.0.0")
        try:
            unregister()
        except Exception:
            # Log cleanup failure as well, but do not hide the root exception.
            import traceback
            traceback.print_exc()
        raise RuntimeError(
            f"UV Wall 2.0.0 registration failed at {_uvwall_stage}: "
            f"{type(_uvwall_register_error).__name__}: {_uvwall_register_error}; "
            f"FULL TRACE: {_uvwall_register_log}") from _uvwall_register_error
    _REGISTERED = True


def unregister():
    """Best-effort cleanup of all registered pieces, even after partial enable."""
    global _REGISTERED
    try:
        for cleanup in (_undo_handlers.unregister, _viewport_controller.unregister):
            try:
                cleanup()
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                # A stale UI Area/WM must never leave our remaining RNA installed.
                pass
        try:
            if hasattr(bpy.types.Scene, "uv_wall_settings"):
                del bpy.types.Scene.uv_wall_settings
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        for cls in reversed(classes):
            try:
                if bool(getattr(cls, "is_registered", False)):
                    bpy.utils.unregister_class(cls)
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        _reset_guard = globals().get("reset_build_guard")
        for cleanup in (shutdown_result_lifecycle, purge_virtual_cache,
                        clear_performance_caches, _reset_guard, _definitions.unregister):
            if not callable(cleanup):
                continue
            try:
                cleanup()
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
    finally:
        _REGISTERED = False


if __name__ == "__main__":
    register()
