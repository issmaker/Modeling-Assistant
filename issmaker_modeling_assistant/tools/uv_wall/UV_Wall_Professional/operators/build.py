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

from .build_context import build_readiness
from ..core.build_guard import build_transaction, BuildReentryError
from ..core.support_logging import set_build_runtime, build_runtime_state, append_traceback, record_failure
from ..core.definitions import (
    definition_for, initialize_from_scene, sync_scene_from_definition, sync_definition_from_scene,
    ensure_source_uid, unresolved_source_uid_collision, serialize_definition, apply_serialized_definition, snapshot_definition, restore_definition, analyze_path,
    CURRENT_SCHEMA_VERSION, runtime_schema_status, ensure_runtime_schema, virtual_asset_for_module,
)
from ..core.virtual_assets import resolve_virtual_source, release_virtual_source, purge_virtual_cache
from ..core.material_intelligence import inspect_material_asset
from ..core.problem_registry import add_problem, clear_problems, scan_result_mesh, scan_cornice_result_metadata
from ..core.surface_points import apply_surface_heights, rollback_surface_heights
from ..core.semantic_geometry import (
    FACE_ROLE_ATTRIBUTE, FACE_ROLE_CODES, face_role_codes, face_role_name,
    set_face_roles, role_counts, validate_face_role_coverage,
    validate_and_fix_role_normals, expected_component_count,
)
from ..core.result_lifecycle import (
    preferred_stable_result, install_mesh_on_result, cleanup_duplicate_results,
    prepare_result_metadata, shutdown_result_lifecycle,
    snapshot_result_metadata, restore_result_metadata, ownership_guard,
    assign_result_identity, ensure_result_uid, active_owned_results,
)
from ..core.diagnostics import log_result_snapshot as _log_result_snapshot
from ..core.performance import (
    PERFORMANCE_REGISTRY, StageTimer, cached_surface_call, clear_performance_caches, record_stage,
)
import json
import sys
from issmaker_modeling_assistant.core.reliability.legacy import format_legacy as _rf_format_legacy
from issmaker_modeling_assistant.core.reliability.ledger import record as _rf_record_decision
from issmaker_modeling_assistant.core.reliability.decision import Decision as _RFDecision


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

class OBJECT_OT_build_uv_wall_along_path(bpy.types.Operator):
    bl_idname = "object.build_uv_wall_along_path"
    bl_label = "Построить UV-стену"
    bl_description = "Построить стену по объектам, выбранным в панели UV Wall"
    bl_options = {"REGISTER", "UNDO"}

    confirm_smooth_sharp: bpy.props.BoolProperty(
        name="Создать всё равно, несмотря на растяжение",
        description=(
            "Подтверждает создание карниза в непрерывном режиме, несмотря на "
            "возможное растяжение текстуры на найденных острых углах"
        ),
        default=False,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        ready, reason = build_readiness(context)
        if not ready:
            try:
                cls.poll_message_set(reason)
            except (AttributeError, TypeError, RuntimeError):
                pass
        return ready

    def invoke(self, context, _event):
        self.confirm_smooth_sharp = False
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        s = context.scene.uv_wall_settings
        layout = self.layout
        layout.label(text="Предварительная проверка", icon="INFO")
        box = layout.box()
        mode_label = {"ROOF_ONLY": "Поверхность", "CORNICE": "Карниз"}.get(s.build_mode, "Стена")
        box.label(text=f"Режим: {mode_label}")
        box.label(text=f"Путь: {s.path_mesh.name if s.path_mesh else 'не выбран'}")
        box.label(text=f"Эталон: {s.source_plane.name if s.source_plane else 'виртуальный модуль'}")
        box.label(text=f"UV-модуль: {s.module_standard}×{s.module_standard} м")
        if s.build_mode == "CORNICE":
            box.label(text=f"Профиль: {s.cornice_profile.name if s.cornice_profile else 'не выбран'}")
            cornice_open = bool(s.path_mesh) and not mesh_path_is_closed(s.path_mesh)
            if s.cornice_flip_side:
                side_label = "обратная (принудительно)"
            else:
                side_label = "автоматически безопасная" if cornice_open else "автоматически наружу"
            box.label(text=f"Сторона: {side_label}")
            box.label(text=f"UV: {s.cornice_uv_mode}")
            if s.cornice_uv_mode == "MAX_CLIPPED":
                box.label(text=f"Отмеченных углов: {len(s.cornice_corners)}")
            elif s.cornice_uv_mode == "MAX_REAL_WORLD":
                threshold = float(getattr(s, "cornice_smooth_warning_angle", 45.0))
                sharp_vertices = detect_cornice_sharp_vertices(s.path_mesh, threshold)
                if sharp_vertices:
                    warning = layout.box()
                    warning.alert = True
                    warning.label(text="Плавный режим: найдены острые повороты", icon="ERROR")
                    warning.label(text=f"Количество: {len(sharp_vertices)}; порог: {threshold:.1f}°")
                    warning.label(text="Build продолжится; точки будут добавлены в Problem Navigator")
                    warning.label(text="При необходимости отметьте эти вершины как Sharp")
            box.label(text="Профиль может быть открытым или замкнутым", icon="INFO")
            box.label(text="Origin профиля касается пути; торцы пути пока открыты", icon="INFO")
            warning = layout.box()
            guidance = cornice_uv_guidance(s.cornice_uv_mode, s.cornice_corner_mode)
            warning.label(text="Проверка UV карниза", icon="UV")
            for line in guidance:
                if line:
                    warning.label(text=line)
            warning.label(text="Карниз: бетон/штукатурка; кирпич лучше строить как стену")
        elif s.build_mode == "ROOF_ONLY":
            box.label(text=f"Нормали: {'вверх' if s.roof_normal == 'UP' else 'вниз'}")
            box.label(text="Поверхность: плавная" if s.surface_method != "TRIANGULATED" else "Сохранённый старый режим (совместимость)")
        else:
            labels = {"OUTWARD": "наружу", "INWARD": "внутрь", "PATH_LEFT": "влево от пути", "PATH_RIGHT": "вправо от пути"}
            is_open = not mesh_path_is_closed(s.path_mesh)
            effective_normal = s.wall_normal_mode
            if is_open and effective_normal in {"OUTWARD", "INWARD"}:
                effective_normal = "PATH_RIGHT" if effective_normal == "OUTWARD" else "PATH_LEFT"
            box.label(text=f"Контур: {'открытый' if is_open else 'замкнутый'}")
            box.label(text=f"Нормали: {labels.get(effective_normal, effective_normal)}")
            if is_open and not s.create_inner:
                box.label(text="Одинарная стена; крышка, пол и торцы не создаются", icon="INFO")
            box.label(text=f"Модулей по высоте: {s.stories}; направление: {'вниз' if s.story_direction == 'DOWN' else 'вверх'}")
        layout.label(text="После OK начнётся построение")

    def execute(self, context):
        # P0 crash-safety: no Build may enter while another UV Wall production
        # transaction owns Result/Undo state.  The context manager releases the
        # lock on every return/exception path.  1.6.2 also records the FULL
        # traceback before Blender wraps it into a shortened bpy.ops RuntimeError.
        started = perf_counter()
        transaction_token = ""
        try:
            with build_transaction(context) as transaction_token:
                set_build_runtime(stage="OPERATOR_ENTER", token=transaction_token, status="RUNNING")
                try:
                    log_event(f"BUILD_TRANSACTION_ENTER token={transaction_token}", "LIFECYCLE")
                except Exception:
                    pass
                try:
                    result = self._execute_impl(context)
                except Exception as error:
                    tb = traceback.format_exc()
                    state = build_runtime_state()
                    record_failure(
                        stage=state.get("stage", "OPERATOR_EXECUTE"), exception=error,
                        traceback_text=tb, context=context, category="BUILD_OPERATOR",
                    )
                    append_traceback(
                        "BUILD_FATAL_TRACEBACK", tb,
                        stage=state.get("stage", "OPERATOR_EXECUTE"), token=transaction_token,
                    )
                    try:
                        log_event(
                            f"BUILD_TRANSACTION_EXIT token={transaction_token}; status=FAILED; "
                            f"stage={state.get('stage')}; elapsed={perf_counter()-started:.6f}s; "
                            f"exception={type(error).__name__}: {error}",
                            "LIFECYCLE",
                        )
                    except Exception:
                        pass
                    set_build_runtime(status="FAILED")
                    raise
                state = build_runtime_state()
                status = "FINISHED" if "FINISHED" in result else "CANCELLED"
                try:
                    log_event(
                        f"BUILD_TRANSACTION_EXIT token={transaction_token}; status={status}; "
                        f"stage={state.get('stage')}; elapsed={perf_counter()-started:.6f}s",
                        "LIFECYCLE",
                    )
                except Exception:
                    pass
                set_build_runtime(status=status)
                return result
        except BuildReentryError as error:
            set_build_runtime(stage="REENTRANCY", token=transaction_token, status="DENIED")
            try:
                log_event(f"REENTRANCY_DENIED reason={error}", "ERROR")
            except Exception:
                pass
            level={"WARNING"} if reliability_kind in {"UNCERTAIN","RESOURCE_LIMIT"} else {"ERROR"}
            suffix=(" · Это не доказательство плохой геометрии; причина записана в Reliability Ledger."
                    if reliability_kind in {"UNCERTAIN","RESOURCE_LIMIT"} else "")
            self.report(level, reliability_message+suffix)
            return {"CANCELLED"}
        except Exception:
            # The inner block already wrote a full failure snapshot.  An error
            # entering build_transaction itself reaches here and needs one too.
            if build_runtime_state().get("status") != "FAILED":
                error = sys.exc_info()[1]
                tb = traceback.format_exc()
                state = build_runtime_state()
                record_failure(stage=state.get("stage", "TRANSACTION_ENTER"), exception=error,
                               traceback_text=tb, context=context, category="BUILD_TRANSACTION")
                append_traceback("BUILD_TRANSACTION_TRACEBACK", tb, stage=state.get("stage"), token=transaction_token)
                set_build_runtime(status="FAILED")
            raise

    def _execute_impl(self, context):
        set_build_runtime(stage="RNA_PREFLIGHT")
        # RNA preflight MUST happen before Definition migration, height edits,
        # new meshes or Stable Result Swap. A mixed 1.4/1.5 session used to
        # build the wall successfully and then crash on last_inner_surface_name.
        from ..core import definitions as live_definitions
        schema_ok, schema_message = live_definitions.runtime_schema_status()
        if live_definitions.CURRENT_SCHEMA_VERSION != 5 or not schema_ok:
            reason = (schema_message if not schema_ok else
                      f"загружена Schema {live_definitions.CURRENT_SCHEMA_VERSION}, требуется Schema 5")
            log_event(f"RNA_PREFLIGHT_FAILED before_geometry=True; {reason}", "ERROR")
            self.report({"ERROR"},
                        "UV Wall: несовместимая RNA-схема. " + reason +
                        ". Перезапустите Blender и включите только актуальную версию UV Wall")
            return {"CANCELLED"}
        set_build_runtime(stage="SETTINGS_RESOLVE")
        settings = context.scene.uv_wall_settings
        path_obj = settings.path_mesh
        definition = definition_for(path_obj) if path_obj is not None else None
        viewport_commit = bool(getattr(context.window_manager, "uvwall_viewport_commit_active", False))
        pending_definition = getattr(context.window_manager, "uvwall_pending_definition_json", "") if viewport_commit else ""
        pending_heights = getattr(context.window_manager, "uvwall_pending_surface_edits_json", "") if viewport_commit else ""
        try:
            height_edits = json.loads(pending_heights) if pending_heights else {}
        except (ValueError, TypeError):
            self.report({"ERROR"}, "Некорректный payload высот Surface")
            return {"CANCELLED"}
        # Capture the actual state BEFORE the UNDO operator mutates Definition.
        # Failed builds must not leave a new parameter paired with an old mesh.
        pending_before = snapshot_definition(definition) if pending_definition and definition else None
        def rollback_pending_definition():
            if pending_before is not None:
                restore_definition(definition, pending_before)
                sync_scene_from_definition(path_obj, settings)
        if viewport_commit and definition is not None and pending_definition:
            if not apply_serialized_definition(definition, pending_definition):
                rollback_pending_definition()
                self.report({"ERROR"}, "Не удалось применить transaction payload")
                return {"CANCELLED"}
            sync_scene_from_definition(path_obj, settings)
        settings.tile_length = float(settings.module_standard)
        source_obj = settings.source_plane
        virtual_source = None

        # Refresh pre-build Problems without blocking production. In smooth
        # Cornice mode sharp authored turns are warnings that the user can
        # inspect/navigate instead of a modal hard-cancel.
        clear_problems(settings)
        path_analysis = analyze_path(path_obj)
        for index, point in enumerate(path_analysis.get("intersections", ())):
            add_problem(
                settings, subsystem="PATH", severity="ERROR",
                title="Пересечение пути", description=f"Пересечение #{index+1}",
                object_name=path_obj.name, world_position=point, radius=0.65,
                suggested_action="Исправьте пересечение рёбер Path перед production Build",
            )
        for vertex_index in path_analysis.get("branches", ()):
            point = path_obj.matrix_world @ path_obj.data.vertices[vertex_index].co
            add_problem(settings, subsystem="PATH", severity="ERROR", title="Ветвление Path",
                        description=f"Вершина {vertex_index} имеет степень > 2", object_name=path_obj.name,
                        world_position=point, radius=0.55, element_kind="VERTEX", element_indices=str(vertex_index),
                        suggested_action="Разделите Path на независимые неветвящиеся цепи")
        if settings.build_mode == "CORNICE" and settings.cornice_uv_mode == "MAX_REAL_WORLD":
            threshold = float(getattr(settings, "cornice_smooth_warning_angle", 45.0))
            sharp_vertices = detect_cornice_sharp_vertices(path_obj, threshold)
            world = path_obj.matrix_world
            for vertex_index, turn in sharp_vertices:
                add_problem(
                    settings, subsystem="CORNICE", severity="WARNING",
                    title="Острый угол в плавном режиме",
                    description=f"Вершина {vertex_index} · поворот {turn:.1f}° · порог {threshold:.1f}°",
                    object_name=path_obj.name,
                    world_position=world @ path_obj.data.vertices[vertex_index].co,
                    radius=0.55, element_kind="VERTEX", element_indices=str(vertex_index),
                    suggested_action="Отметьте вершину Sharp или переключитесь в режим острых углов",
                )
            if sharp_vertices:
                self.report({"WARNING"}, f"Плавный карниз: острых мест {len(sharp_vertices)} — смотрите Problem Navigator")

        use_virtual = bool(
            definition is not None and definition.initialized and definition.use_virtual_reference
        )
        if use_virtual:
            active_material, active_image = virtual_asset_for_module(definition)
            asset_state = inspect_material_asset(active_material, None, definition.module_standard)
            if asset_state["status"] != "VALID":
                add_problem(
                    settings, subsystem="MATERIAL", severity="ERROR",
                    title="Материал и его текстура", description=asset_state["message"],
                    object_name=path_obj.name, world_position=path_obj.matrix_world.translation, radius=1.0,
                    suggested_action="Назначьте материал с Image Texture 2048²/4096², подключённой к Base Color",
                )
                self.report({"ERROR"}, asset_state["message"])
                rollback_pending_definition()
                return {"CANCELLED"}
            try:
                virtual_source = resolve_virtual_source(definition, settings)
                source_obj = virtual_source
            except ValueError as error:
                self.report({"ERROR"}, str(error))
                rollback_pending_definition()
                return {"CANCELLED"}
        if source_obj is None:
            self.report({"ERROR"}, "Не выбран UV-эталон и не настроен виртуальный модуль")
            rollback_pending_definition()
            return {"CANCELLED"}

        if source_obj == path_obj:
            if virtual_source is not None: release_virtual_source(virtual_source)
            self.report({"ERROR"}, "Source Plane и Path Mesh должны быть разными объектами")
            rollback_pending_definition()
            return {"CANCELLED"}
        if source_obj.type != "MESH" or path_obj.type != "MESH":
            if virtual_source is not None: release_virtual_source(virtual_source)
            self.report({"ERROR"}, "Оба объекта должны быть Mesh")
            rollback_pending_definition()
            return {"CANCELLED"}

        objects_before = set(bpy.data.objects)
        meshes_before = set(bpy.data.meshes)
        parametric_result = bool(definition is not None and getattr(definition, "initialized", False))
        owner_uid = ""
        if parametric_result:
            previous_uid = getattr(definition, "source_uid", "")
            owner_uid = ensure_source_uid(path_obj, definition)
            if previous_uid and owner_uid != previous_uid:
                log_event(
                    f"DEFINITION_UID_COLLISION repaired=True; path={path_obj.name}; "
                    f"old_uid={previous_uid}; new_uid={owner_uid}; copied_result_link_cleared=True",
                    "OWNERSHIP",
                )
            unresolved = unresolved_source_uid_collision(path_obj, definition)
            if unresolved:
                names = ",".join(sorted(obj.name for obj in unresolved))
                log_event(
                    f"DEFINITION_UID_AMBIGUOUS fail_closed=True; path={path_obj.name}; "
                    f"uid={owner_uid}; collisions={names}",
                    "ERROR",
                )
                rollback_pending_definition()
                if virtual_source is not None: release_virtual_source(virtual_source)
                self.report({"ERROR"}, "UV Wall: неоднозначный UID Definition после копирования/переименования. Build безопасно отменён")
                return {"CANCELLED"}
            active_results = active_owned_results(path_obj, definition)
            if len(active_results) > 1:
                names = ",".join(sorted(obj.name for obj in active_results))
                log_event(
                    f"RESULT_OWNERSHIP_AMBIGUOUS fail_closed=True; definition_uid={owner_uid}; results={names}",
                    "ERROR",
                )
                rollback_pending_definition()
                if virtual_source is not None: release_virtual_source(virtual_source)
                self.report({"ERROR"}, "UV Wall: у Definition найдено несколько активных Result. Автоматическое удаление запрещено")
                return {"CANCELLED"}
        stable_target = (
            preferred_stable_result(path_obj, definition)
            if parametric_result else None
        )
        if stable_target is not None:
            allowed, ownership_reason = ownership_guard(
                stable_target, owner_uid, operation="STABLE_RESULT_SWAP", allow_unowned_legacy=True
            )
            if not allowed:
                log_event(ownership_reason, "ERROR")
                rollback_pending_definition()
                if virtual_source is not None: release_virtual_source(virtual_source)
                self.report({"ERROR"}, "UV Wall: Result принадлежит другому Definition; Build отменён")
                return {"CANCELLED"}
        old_stable_mesh = stable_target.data if stable_target is not None else None
        old_stable_matrix = stable_target.matrix_world.copy() if stable_target is not None else None
        old_stable_metadata = snapshot_result_metadata(stable_target)

        def rollback_generated_mesh():
            # The generator may already have swapped stable_target.data before
            # a late validation failure. Restore identity and old mesh first.
            if stable_target is not None and old_stable_mesh is not None:
                try:
                    stable_target.data = old_stable_mesh
                    stable_target.matrix_world = old_stable_matrix
                except (ReferenceError, RuntimeError):
                    pass
                restore_result_metadata(stable_target, old_stable_metadata)
            from ..core.result_lifecycle import discard_pending_cleanup
            discard_pending_cleanup()
        if stable_target is not None:
            log_event(
                f"STABLE_RESULT_TARGET object={stable_target.name}; data={stable_target.data.name}; "
                f"definition_uid={owner_uid}; result_uid={ensure_result_uid(stable_target)}; "
                f"object_pointer={stable_target.as_pointer() if hasattr(stable_target, 'as_pointer') else 0}; "
                f"mesh_pointer={stable_target.data.as_pointer() if hasattr(stable_target.data, 'as_pointer') else 0}",
                "LIFECYCLE",
            )
        operation_started = perf_counter()
        stage = "INPUT_SNAPSHOT"
        surface_rollback = None
        surface_settings_before = str(getattr(settings, 'surface_heights_json', '{}'))
        runtime_identity = log_operation_snapshot(context, settings, source_obj, path_obj) or {}
        build_id = runtime_identity.get("operation_id", "UNKNOWN")
        log_event(
            f"START build_id={build_id}; definition_uid={owner_uid or 'UNMANAGED'}; "
            f"mode={settings.build_mode}; source={source_obj.name}; "
            f"path={path_obj.name}; module={settings.tile_length:.6g}"
        )
        try:
            if height_edits:
                if settings.build_mode not in {"ROOF_ONLY", "WALL"}:
                    raise ValueError("Height-point editing is only available in WALL or SURFACE")
                stage = "APPLY_AUTHORED_PATH_HEIGHTS"
                stage_started = perf_counter()
                surface_rollback = apply_surface_heights(path_obj, definition, height_edits)
                # The final sync_definition_from_scene() is authoritative.
                # Without this bridge it would overwrite the newly saved UID
                # baselines with the *old* scene metadata after a good build.
                if definition is not None:
                    settings.surface_heights_json = definition.surface_heights_json
                log_event(f"STAGE_TIME stage={stage}; elapsed={(perf_counter()-stage_started):.6f}s", "PERF")
            if getattr(settings, "build_mode", "WALL") == "CORNICE":
                if settings.cornice_profile in {source_obj, path_obj}:
                    raise ValueError("Путь, эталон и профиль карниза должны быть разными объектами")
                manual_corners = validated_cornice_corner_points(settings, path_obj, remove_stale=True)
                if settings.cornice_uv_mode == "MAX_CLIPPED" and not manual_corners:
                    log_event(
                        "CORNICE_CORNER_MODE_AUTO_RESET MAX_CLIPPED->MAX_REAL_WORLD; reason=no_authored_sharp",
                        "AUTO_FIX",
                    )
                    settings.cornice_uv_mode = "MAX_REAL_WORLD"
                    if definition is not None:
                        definition.cornice_uv_mode = "MAX_REAL_WORLD"
                stage = "BUILD_CORNICE"
                log_event("STAGE BUILD_CORNICE", "STAGE")
                stage_started = perf_counter()
                wall, face_count = build_cornice(
                    context, source_obj, path_obj, settings.cornice_profile,
                    settings.tile_length, settings.output_name or "UV_Cornice",
                    settings.cornice_flip_side,
                    settings.cornice_uv_mode,
                    settings.cornice_corner_mode,
                    settings.cornice_linear_step,
                    manual_corner_points=manual_corners,
                    manual_corner_methods=[(point, str(getattr(marker,'corner_method','CLASSIC')))
                        for point,marker in zip(manual_corners,settings.cornice_corners)],
                    target_obj=stable_target,
                    profile_rotation_deg=settings.cornice_profile_rotation,
                    profile_flip_x=settings.cornice_profile_flip_x,
                    profile_flip_z=settings.cornice_profile_flip_y,
                    reverse_path=settings.cornice_reverse_path,
                )
                log_event(f"STAGE_TIME stage={stage}; elapsed={(perf_counter()-stage_started):.6f}s", "PERF")
                distortion_warnings = int(wall.get("cornice_uv_distortion_warnings", 0))
                # Publish each field-detected corner/UV issue as an actionable
                # world-space Problem instead of one global warning at Path origin.
                cornice_problem_count = scan_cornice_result_metadata(settings, wall)
                if cornice_problem_count:
                    log_event(
                        f"CORNICE_PROBLEM_REGISTRY records={cornice_problem_count}; object={wall.name}",
                        "WARNING",
                    )
                frame_delta = float(wall.get("cornice_frame_max_delta_deg", 0.0))
                if frame_delta > 60.0:
                    add_problem(
                        settings, subsystem="CORNICE", severity="WARNING",
                        title="Резкий поворот frame карниза",
                        description=f"Максимальный поворот ориентации профиля: {frame_delta:.1f}°",
                        object_name=wall.name, world_position=path_obj.matrix_world.translation, radius=1.0,
                        suggested_action="Проверьте variable-Z участок и ориентацию профиля",
                    )
                if distortion_warnings:
                    self.report(
                        {"WARNING"},
                        f"Карниз создан: участков с UV-растяжением {distortion_warnings}; смотрите Problem Navigator",
                    )
                else:
                    self.report({"INFO"}, f"Создан карниз {wall.name}: {face_count} граней")
            elif getattr(settings, "build_mode", "WALL") == "ROOF_ONLY":
                stage = "BUILD_SURFACE"
                log_event("STAGE BUILD_SURFACE", "STAGE")
                stage_started = perf_counter()
                wall, face_count, n_chains, skipped, errors = build_roof_only(
                    context,
                    source_obj,
                    path_obj,
                    settings.tile_length,
                    settings.output_name or "UV_Roof",
                    settings.surface_module_rotation,
                    settings.roof_normal,
                    settings.surface_method,
                    target_obj=stable_target,
                    surface_z_offset=float(settings.surface_height_offset),
                )
                log_event(f"STAGE_TIME stage={stage}; elapsed={(perf_counter()-stage_started):.6f}s", "PERF")
                msg = f"Поверхность: {n_chains} контур(ов), {face_count} граней"
                if skipped:
                    msg += f", ветвлений: {skipped}"
                if errors:
                    msg += " | " + "; ".join(errors[:3])
                self.report({"INFO"}, msg)
            else:
                stage = "PREPARE_WALL"
                log_event("STAGE PREPARE_WALL", "STAGE")
                from ..production.wall_alignment import automatic_inset_side
                resolved_wall_side = automatic_inset_side(
                    getattr(settings, "wall_alignment", "LEGACY"), settings.inset_side)
                if resolved_wall_side != settings.inset_side:
                    log_event(
                        f"WALL_SIDE_AUTO corrected={settings.inset_side}->{resolved_wall_side}; "
                        f"alignment={getattr(settings, 'wall_alignment', 'LEGACY')}", "AUTO_FIX")
                    settings.inset_side = resolved_wall_side
                    if definition is not None:
                        definition.inset_side = resolved_wall_side
                if settings.create_inner and settings.create_inner_surface:
                    # IMPORTANT: planter/surface geometry is a dependent subsystem.
                    # It may derive which generated shell is geometrically inside,
                    # but it must never rewrite the user-authored WALL placement.
                    # In 1.9.0 this block fought WALL_SIDE_AUTO (RIGHT->LEFT->RIGHT),
                    # making RIGHT look impossible and coupling planter geometry to
                    # wall publication state.  Only validate the prerequisite here.
                    from ..production.path import ordered_path_vertices, is_path_closed
                    planar_points = ordered_path_vertices(path_obj)
                    if not is_path_closed(planar_points):
                        raise ValueError('Клумба требует замкнутого контура')
                    log_event(
                        f'PLANTER_SIDE_DERIVED wall_alignment={getattr(settings,"wall_alignment","LEGACY")}; '
                        f'wall_inset_side={settings.inset_side}; mutates_wall_state=False',
                        'DEPENDENCY_UPDATE',
                    )
                if settings.create_inner:
                    settings.top_cap_mode = "BETWEEN"
                elif settings.create_top_cap or settings.create_bottom_cap:
                    settings.top_cap_mode = "OUTER_ONLY"
                cap_material = None
                if settings.create_top_cap and getattr(settings, "cap_material_override", False):
                    cap_material = getattr(settings, "cap_material", None)
                    cap_state = inspect_material_asset(cap_material, None, settings.module_standard)
                    if cap_state["status"] != "VALID":
                        add_problem(
                            settings, subsystem="MATERIAL", severity="ERROR",
                            title="Материал верхней крышки", description=cap_state["message"],
                            object_name=path_obj.name, world_position=path_obj.matrix_world.translation, radius=1.0,
                            suggested_action="Назначьте совместимый материал верхней крышки для текущего 2K/4K стандарта",
                        )
                        raise ValueError("Материал верхней крышки: " + cap_state["message"])
                bottom_cap_material = None
                if settings.create_bottom_cap and getattr(settings, "bottom_cap_material_override", False):
                    bottom_cap_material = getattr(settings, "bottom_cap_material", None)
                    bottom_state = inspect_material_asset(bottom_cap_material, None, settings.module_standard)
                    if bottom_state["status"] != "VALID":
                        add_problem(
                            settings, subsystem="MATERIAL", severity="ERROR",
                            title="Материал нижней крышки", description=bottom_state["message"],
                            object_name=path_obj.name, world_position=path_obj.matrix_world.translation, radius=1.0,
                            suggested_action="Назначьте совместимый материал нижней крышки для текущего 2K/4K стандарта",
                        )
                        raise ValueError("Материал нижней крышки: " + bottom_state["message"])
                stage = "BUILD_WALL"
                log_event("STAGE BUILD_WALL", "STAGE")
                stage_started = perf_counter()
                wall, face_count, actual_inset = build_wall(
                    context,
                    source_obj,
                    path_obj,
                    settings.tile_length,
                    settings.output_name,
                    settings.create_inner,
                    settings.inset_distance,
                    settings.inset_side,
                    settings.close_ends,
                    settings.keep_inner_path,
                    settings.create_top_cap,
                    settings.top_cap_mode,
                    settings.stories,
                    settings.cap_rotation,
                    settings.create_bottom_cap,
                    settings.first_story_height if settings.custom_first_story else 0.0,
                    settings.align_bottom,
                    settings.bottom_extend,
                    settings.story_direction == "DOWN",
                    settings.wall_normal_mode,
                    float(getattr(settings, "wall_offset", 0.0)),
                    wall_alignment=getattr(settings,"wall_alignment","LEGACY"),
                    target_obj=stable_target,
                    wall_rotation_deg=float(getattr(settings,"wall_module_rotation",0.0)),
                    wall_uv_origin_mode=getattr(settings,"wall_uv_origin_mode","PATH"),
                    cap_material=cap_material,
                    bottom_cap_material=bottom_cap_material,
                    inner_wall_height_delta=float(getattr(settings, "inner_wall_height_delta", 0.0)),
                    inner_wall_height_overrides_json=getattr(settings, "inner_wall_height_overrides_json", "{}"),
                    secondary_points_json=getattr(settings, "secondary_points_json", "{}"),
                    close_start_end=bool(getattr(settings, "close_start_end", True)),
                    close_finish_end=bool(getattr(settings, "close_finish_end", True)),
                )
                log_event(f"STAGE_TIME stage={stage}; elapsed={(perf_counter()-stage_started):.6f}s", "PERF")
                self.report({"INFO"}, f"Создано {wall.name}: {face_count} граней")
                if settings.create_inner_surface and settings.create_inner:
                    try:
                        from ..production.dependent_surface import generate_inner_surface
                        generate_inner_surface(context,settings,definition,source_obj,log_event)
                    except Exception as child_error:
                        log_event(f"INNER_SURFACE_FAILED parent_kept=True; reason={child_error}","WARNING")
                        self.report({"WARNING"},f"Стена готова. Внутренняя поверхность: {child_error}")
                else:
                    from ..core.result_lifecycle import schedule_obsolete_children
                    owner_uid=ensure_source_uid(path_obj,definition) if path_obj else ''
                    removed=schedule_obsolete_children(owner_uid, getattr(definition,'last_inner_surface_name',''))
                    if definition is not None:
                        definition.last_inner_surface_name=''
                    log_event(f'CHILD_LIFECYCLE inactive=True; pending_removal={removed}; parent_uid={owner_uid}', 'LIFECYCLE')
            if getattr(settings, "problem_auto_scan", True) and wall is not None:
                problem_count = scan_result_mesh(settings, wall)
                if problem_count:
                    log_event(f"PROBLEM_REGISTRY result_scan={problem_count}; object={wall.name}", "WARNING")
                    self.report({"WARNING"}, f"Problem Navigator: найдено {len(settings.problems)} проблем/предупреждений")
        except ValueError as error:
            reliability_kind,reliability_message=_rf_format_legacy('UV_WALL',error)
            try:
                _rf_record_decision(_RFDecision(reliability_kind,'UV_WALL_LEGACY_VALIDATOR',str(error),{},'UV_WALL','legacy-explicit-proof' if reliability_kind=='PROVEN_INVALID' else ''),source='UV_Wall.build')
            except Exception:
                pass
            # An expected fail-closed validation cancellation is still the
            # MOST RECENT failure. Save it before rollback so one-click support
            # does not export a stale unrelated exception from a past session.
            set_build_runtime(stage=stage, status="CANCELLED")
            try:
                record_failure(
                    stage=stage, exception=error,
                    traceback_text=traceback.format_exc(), context=context,
                    category="BUILD_VALIDATION",
                    extra={"create_secondary":bool(getattr(settings,"create_inner",False)),
                           "module_m":float(getattr(settings,"tile_length",0.0)),
                           "direction":str(getattr(settings,"story_direction","UNKNOWN")),
                           "stable_result_policy":"ROLLBACK_NO_SWAP",
                           "reliability_kind":reliability_kind},
                )
            except Exception as diagnostic_error:
                log_event(f"LAST_VALIDATION_SNAPSHOT_FAILED {diagnostic_error}","WARNING")
            # Geometry validators may carry plain coordinate records because the
            # temporary Mesh is about to be rolled back. Preserve those records
            # in Problem Navigator before deleting any generated datablocks.
            for record in getattr(error, "problem_records", ()):
                try:
                    add_problem(
                        settings,
                        subsystem=record.get("subsystem", "GEOMETRY"),
                        severity=record.get("severity", "ERROR"),
                        title=record.get("title", "Ошибка геометрии"),
                        description=record.get("description", str(error)),
                        object_name=path_obj.name,
                        world_position=record.get("world_position", path_obj.matrix_world.translation),
                        radius=record.get("radius", 0.6),
                        element_kind=record.get("element_kind", ""),
                        element_indices=record.get("element_indices", ""),
                        suggested_action=record.get("suggested_action", "Проверьте проблемную область"),
                        category=record.get("category", "GEOMETRY_BLOCKED"),
                        code=record.get("code", "GEOMETRY_VALIDATION"),
                        occurrence_count=record.get("occurrence_count", 1),
                    )
                except Exception:
                    pass
            rollback_generated_mesh()
            rollback_pending_definition()
            if definition is not None:
                definition.last_error = str(error)
            if surface_rollback is not None:
                rollback_surface_heights(path_obj, definition, surface_rollback)
                settings.surface_heights_json = surface_settings_before
            cleanup_created_datablocks(objects_before, meshes_before, uvwall_only=True)
            log_event(
                f"stage={stage}; elapsed={perf_counter() - operation_started:.6f}s; {error}",
                "CANCELLED",
            )
            if virtual_source is not None: release_virtual_source(virtual_source)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except Exception as error:
            tb = traceback.format_exc()
            set_build_runtime(stage=stage, status="FAILED")
            record_failure(stage=stage, exception=error, traceback_text=tb, context=context, category="BUILD_GENERATOR")
            append_traceback("BUILD_GENERATOR_TRACEBACK", tb, stage=stage)
            rollback_generated_mesh()
            rollback_pending_definition()
            if definition is not None:
                definition.last_error = str(error)
            if surface_rollback is not None:
                rollback_surface_heights(path_obj, definition, surface_rollback)
                settings.surface_heights_json = surface_settings_before
            cleanup_created_datablocks(objects_before, meshes_before, uvwall_only=True)
            log_event(
                f"stage={stage}; elapsed={perf_counter() - operation_started:.6f}s; "
                f"{type(error).__name__}: {error}",
                "ERROR",
            )
            if virtual_source is not None: release_virtual_source(virtual_source)
            self.report(
                {"ERROR"},
                "Неожиданная ошибка. Новые данные удалены; используйте «Скопировать лог» или «Пакет для отправки»",
            )
            return {"CANCELLED"}
        # Sweep all owned ephemeral child results by UID after a successful
        # mode change, not only by the possibly-stale Definition.last_name.
        if definition is not None and path_obj is not None:
            from ..core.result_lifecycle import schedule_obsolete_children, schedule_obsolete_guides
            owner_uid=ensure_source_uid(path_obj,definition)
            if settings.build_mode != 'WALL':
                count=schedule_obsolete_children(owner_uid,getattr(definition,'last_inner_surface_name',''))
                definition.last_inner_surface_name=''
                log_event(f'CHILD_LIFECYCLE mode={settings.build_mode}; obsolete_surfaces={count}', 'LIFECYCLE')
            keep_guides=(settings.build_mode=='WALL' and settings.create_inner and settings.keep_inner_path)
            guides_removed=schedule_obsolete_guides(owner_uid,keep_guide=keep_guides)
            if guides_removed:
                log_event(f'GUIDE_LIFECYCLE removed={guides_removed}; keep={keep_guides}', 'LIFECYCLE')
        if virtual_source is not None: release_virtual_source(virtual_source)
        elapsed = perf_counter() - operation_started
        final_face_count = len(wall.data.polygons) if wall is not None and wall.type == "MESH" else face_count
        log_event(f"FINISHED object={wall.name}; faces={final_face_count}; elapsed={elapsed:.6f}s")
        log_event(f"STAGE_TIME stage=TOTAL; elapsed={elapsed:.6f}s", "PERF")
        _log_result_snapshot(
            wall, elapsed, getattr(settings, "diagnostic_detail", "NORMAL"), log_event, log_bulk
        )
        if path_obj is not None and definition_for(path_obj) is not None:
            sync_definition_from_scene(path_obj, settings)

        # Viewport commit lifecycle lives inside this UNDO operator. Stable
        # result identity is preserved whenever possible: generators replace
        # only the Mesh datablock on the previous result Object.
        if parametric_result and definition is not None:
            owner_uid = ensure_source_uid(path_obj, definition)
            created_objects = [obj for obj in bpy.data.objects if obj not in objects_before]
            result_objects = [
                obj for obj in created_objects
                if obj.type == "MESH" and obj.get("uv_wall_mode") in {"WALL", "SURFACE", "CORNICE"}
            ]
            if wall is not None and wall not in result_objects:
                result_objects.append(wall)
            definition_payload = serialize_definition(definition)
            committed_result_ids = []
            for obj in result_objects:
                mode = obj.get("uv_wall_mode") or settings.build_mode
                result_uid = assign_result_identity(
                    obj, owner_uid, path_obj.name, mode, definition.revision
                )
                committed_result_ids.append(result_uid)
                obj["uvwall_definition_revision"] = definition.revision
                obj["uvwall_definition_json"] = definition_payload
            log_event(
                f"COMMIT_PRECHECK build_id={build_id}; definition_uid={owner_uid}; "
                f"results={committed_result_ids}; keep={[obj.name for obj in result_objects]}",
                "OWNERSHIP",
            )
            cleanup_duplicate_results(path_obj, definition, result_objects)
            definition.last_result_name = wall.name
            definition.last_committed_revision = definition.revision
            definition.last_error = ""
            definition.last_successful_settings_json = serialize_definition(definition)
            # Successful-build parameter history. Keep it on the authored Path
            # so selecting a generated Result can never steal another owner's
            # revisions. This is advisory metadata only; never fail a valid
            # geometry commit merely because history storage fails.
            try:
                from ..core.revisions import PROPERTY as REVISION_PROPERTY, record_revision
                new_history, added, revision = record_revision(
                    str(path_obj.get(REVISION_PROPERTY, '')), owner_uid,
                    definition.last_successful_settings_json,
                    label=f"Принято: {settings.build_mode}",
                    parent_id=int(path_obj.get('uvwall_history_restore_parent_v1', 0)) or None,
                )
                if added:
                    old_path_history = path_obj.get(REVISION_PROPERTY, None)
                    old_result_history = wall.get(REVISION_PROPERTY, None)
                    try:
                        path_obj[REVISION_PROPERTY] = new_history
                        wall[REVISION_PROPERTY] = new_history
                    except Exception:
                        # Two metadata writes form one logical transaction.
                        for obj, old in ((path_obj, old_path_history), (wall, old_result_history)):
                            try:
                                if old is None:
                                    if REVISION_PROPERTY in obj: del obj[REVISION_PROPERTY]
                                else:
                                    obj[REVISION_PROPERTY] = old
                            except Exception:
                                pass
                        raise
                if 'uvwall_history_restore_parent_v1' in path_obj:
                    del path_obj['uvwall_history_restore_parent_v1']
                log_event(f"WALL_HISTORY owner={owner_uid}; revision={revision['id']}; "
                          f"changed={added}; fields={revision['change_count']}", 'HISTORY')
            except Exception as history_error:
                log_event(f"WALL_HISTORY_UNAVAILABLE {type(history_error).__name__}: {history_error}", 'WARNING')
            # PPA-W4: a single persistent recovery Mesh for the *accepted* WALL.
            # Snapshot failure is advisory; it MUST NOT roll back accepted geometry.
            if settings.build_mode == 'WALL' and wall is not None:
                try:
                    from ..core.mesh_recovery import store_accepted_mesh
                    backup_status = store_accepted_mesh(
                        wall, bpy.data, build_revision=definition.last_committed_revision,
                    )
                    log_event(f"RECOVERY_SNAPSHOT build_id={build_id}; "
                              f"status={backup_status.get('status')}; "
                              f"bytes={backup_status.get('estimated_bytes', 0)}; "
                              f"reason={backup_status.get('reason', '')}",
                              'RECOVERY' if backup_status.get('status') == 'STORED' else 'WARNING')
                except Exception as recovery_exception:
                    log_event(f"RECOVERY_SNAPSHOT_INTERNAL_ERROR "
                              f"{type(recovery_exception).__name__}: {recovery_exception}", 'WARNING')
            log_event(
                f"COMMIT_OK build_id={build_id}; definition_uid={owner_uid}; stable_object={wall.name}; "
                f"result_uid={ensure_result_uid(wall)}; reused={bool(stable_target is wall)}; "
                f"created_objects={len(created_objects)}; kept_results={len(result_objects)}",
                "LIFECYCLE",
            )
        from ..core.result_lifecycle import finalize_pending_cleanup
        finalize_pending_cleanup()
        return {"FINISHED"}


