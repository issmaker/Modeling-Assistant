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
from pathlib import Path
import os


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

class OBJECT_OT_uv_wall_copy_log(bpy.types.Operator):
    bl_idname = "object.uv_wall_copy_log"
    bl_label = "Скопировать лог"
    bl_description = "Скопировать полный отчёт: runtime, последний failure и весь UV_Wall_Diagnostics"

    def execute(self, context):
        try:
            from ..core.support_logging import clipboard_report
            payload = clipboard_report(context)
        except Exception as error:
            self.report({"ERROR"}, f"Не удалось собрать лог: {error}")
            return {"CANCELLED"}
        if not payload.strip():
            self.report({"WARNING"}, "Журнал пока пуст")
            return {"CANCELLED"}
        context.window_manager.clipboard = payload
        self.report({"INFO"}, "Полный UV Wall лог скопирован в буфер обмена")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_save_log(bpy.types.Operator):
    bl_idname = "object.uv_wall_save_log"
    bl_label = "Сохранить лог"
    bl_description = "Сохранить полный текстовый лог в папку Downloads"

    def execute(self, context):
        try:
            from ..core.support_logging import save_diagnostics_log
            path = save_diagnostics_log(context)
            context.window_manager.clipboard = str(path)
            self.report({"INFO"}, f"Лог сохранён: {path}")
            return {"FINISHED"}
        except Exception as error:
            self.report({"ERROR"}, f"Не удалось сохранить лог: {error}")
            return {"CANCELLED"}


class OBJECT_OT_uv_wall_collect_support(bpy.types.Operator):
    bl_idname = "object.uv_wall_collect_support"
    bl_label = "Пакет для отправки"
    bl_description = "Один ZIP: Mesh, WALL↔CAP, UV, высоты, Viewport, действия, логи и причины ошибок"

    def execute(self, context):
        try:
            from ..core.support_logging import create_support_bundle
            path = create_support_bundle(context)
            context.window_manager.clipboard = str(path)
            self.report({"INFO"}, f"Пакет готов: {path.name}; путь скопирован")
            return {"FINISHED"}
        except Exception as error:
            self.report({"ERROR"}, f"Не удалось собрать пакет: {error}")
            return {"CANCELLED"}


class OBJECT_OT_uv_wall_open_logs_folder(bpy.types.Operator):
    bl_idname = "object.uv_wall_open_logs_folder"
    bl_label = "Открыть папку логов"
    bl_description = "Открыть постоянную папку UV Wall с latest/session/failure логами"

    def execute(self, context):
        try:
            from ..core.support_logging import log_directory
            path = str(log_directory())
            try:
                bpy.ops.wm.path_open(filepath=path)
            except Exception:
                if os.name == "nt":
                    os.startfile(path)
                else:
                    raise
            self.report({"INFO"}, path)
            return {"FINISHED"}
        except Exception as error:
            self.report({"ERROR"}, f"Не удалось открыть папку логов: {error}")
            return {"CANCELLED"}


class OBJECT_OT_uv_wall_clear_log(bpy.types.Operator):
    bl_idname = "object.uv_wall_clear_log"
    bl_label = "Очистить лог"
    bl_description = "Начать новую диагностическую сессию; старый session-log остаётся в папке логов"

    def execute(self, _context):
        text_block = bpy.data.texts.get(LOG_TEXT_NAME)
        if text_block is not None:
            text_block.clear()
        try:
            from ..core.support_logging import initialize_logging_session, set_build_runtime
            initialize_logging_session(force=True)
            set_build_runtime(stage="IDLE", token="", status="CLEARED")
        except Exception:
            pass
        self.report({"INFO"}, "Новая диагностическая сессия начата")
        return {"FINISHED"}


STRESS_TEST_SCENARIOS = {
    "LANDSCAPE": {
        "name": "UV_Stress_Landscape",
        "mode": "ROOF_ONLY",
        "closed": True,
        "description": "Вогнутый высотный контур: глобальное плавное поле, UV-сетка и Weld",
        "points": (
            (0.0, 0.0, 0.0), (17.8, -3.2, 2.6), (35.9, 3.7, -1.4),
            (52.1, -0.4, 4.8), (63.0, 15.8, 1.1), (55.4, 32.2, -3.2),
            (39.1, 27.1, 5.4), (27.2, 42.0, 0.3), (10.0, 36.3, -2.5),
            (-6.1, 25.0, 3.2), (-2.2, 9.9, -0.8),
        ),
    },
    "WALL_CLOSED": {
        "name": "UV_Stress_Wall_Closed",
        "mode": "WALL",
        "closed": True,
        "description": "Замкнутая стена: острые углы, короткие участки и высотный верх",
        "points": (
            (100.0, 0.0, 16.0), (103.9992, -0.3, 16.4), (119.7, 1.1, 15.2),
            (127.9, 8.0, 18.1), (124.2, 20.1, 17.0), (116.0, 15.8, 16.2),
            (111.8, 28.0, 19.4), (99.9, 25.1, 18.0), (92.0, 13.9, 15.6),
            (96.0011, 8.0, 16.8),
        ),
    },
    "WALL_OPEN": {
        "name": "UV_Stress_Wall_Open",
        "mode": "WALL",
        "closed": False,
        "description": "Открытая одинарная стена: повороты, уклоны и смена длины сегментов",
        "points": (
            (180.0, 0.0, 12.0), (183.9991, 0.2, 12.0), (191.7, 5.8, 14.3),
            (199.9, 1.9, 13.1), (204.0010, 10.0, 16.7), (216.2, 13.1, 15.0),
            (218.0, 22.0, 18.2), (229.9, 25.0, 17.1), (236.0, 18.2, 20.0),
        ),
    },
    "UV_WELD": {
        "name": "UV_Stress_UV_Weld",
        "mode": "ROOF_ONLY",
        "closed": True,
        "description": "Пограничный контур: вершины около линий 4 м и зазоры около Weld 0,002 м",
        "points": (
            (300.0, 0.0, 0.0), (303.9988, 0.0, 0.5), (308.0012, 0.0011, 1.0),
            (316.0, 0.0, 2.0), (320.0010, 7.9991, 3.0), (319.9990, 16.0010, 1.5),
            (312.0011, 20.0, -1.0), (304.0009, 20.0010, 0.8), (300.0, 12.0, 2.2),
            (299.9989, 4.0010, 0.4),
        ),
    },
}


def create_stress_test_path(context, scenario_id):
    scenario = STRESS_TEST_SCENARIOS[scenario_id]
    points = list(scenario["points"])
    edges = [(index, index + 1) for index in range(len(points) - 1)]
    if scenario["closed"]:
        edges.append((len(points) - 1, 0))
    mesh = bpy.data.meshes.new(scenario["name"] + "_Mesh")
    mesh.from_pydata(points, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(scenario["name"], mesh)
    context.collection.objects.link(obj)
    obj["uv_wall_stress_test"] = scenario_id
    obj["expected_mode"] = scenario["mode"]
    obj["test_description"] = scenario["description"]
    obj["module_hint"] = 4.0
    log_event(
        f"STRESS_TEST_CREATED id={scenario_id}; object={obj.name}; "
        f"mode={scenario['mode']}; points={len(points)}; edges={len(edges)}; "
        f"closed={scenario['closed']}; description={scenario['description']}",
        "TEST",
    )
    return obj


class OBJECT_OT_uv_wall_create_stress_test(bpy.types.Operator):
    bl_idname = "object.uv_wall_create_stress_test"
    bl_label = "Создать стресс-контур"
    bl_options = {"REGISTER", "UNDO"}

    scenario: bpy.props.EnumProperty(
        items=(
            ("LANDSCAPE", "Высотный ландшафт", "Сложная плавная поверхность"),
            ("WALL_CLOSED", "Замкнутая стена", "Сложный замкнутый путь стены"),
            ("WALL_OPEN", "Открытая стена", "Сложный открытый путь стены"),
            ("UV_WELD", "UV/Weld границы", "Пограничные значения модуля и Weld"),
            ("ALL", "Весь набор", "Создать все тестовые контуры"),
        ),
        default="LANDSCAPE",
    )

    def execute(self, context):
        scenario_ids = tuple(STRESS_TEST_SCENARIOS) if self.scenario == "ALL" else (self.scenario,)
        created = [create_stress_test_path(context, scenario_id) for scenario_id in scenario_ids]
        for selected in context.selected_objects:
            selected.select_set(False)
        for obj in created:
            obj.select_set(True)
        context.view_layer.objects.active = created[-1]
        context.scene.uv_wall_settings.path_mesh = created[-1]
        self.report({"INFO"}, f"Создано тестовых контуров: {len(created)}")
        return {"FINISHED"}


def create_quick_check_reference(context):
    """Create a self-contained 2K/2 m reference without disk files."""
    image = bpy.data.images.new(
        "UV_QuickCheck_2K", width=2048, height=2048,
        alpha=False, float_buffer=False,
    )
    image.generated_color = (0.18, 0.42, 0.8, 1.0)
    material = bpy.data.materials.new("UV_QuickCheck_Material")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    texture = nodes.new("ShaderNodeTexImage")
    texture.name = "UV_QuickCheck_Texture"
    texture.label = "Quick Check 2K / 1000 px/m"
    texture.image = image
    principled = next((node for node in nodes if node.type == "BSDF_PRINCIPLED"), None)
    if principled is not None:
        material.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])

    mesh = bpy.data.meshes.new("UV_QuickCheck_Reference_Mesh")
    mesh.from_pydata(
        [(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0),
         (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)],
        [], [(0, 1, 2, 3)],
    )
    mesh.update()
    margin = 0.01171875
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_values = (
        (margin, margin), (1.0 - margin, margin),
        (1.0 - margin, 1.0 - margin), (margin, 1.0 - margin),
    )
    for loop_index, uv in zip(mesh.polygons[0].loop_indices, uv_values):
        uv_layer.data[loop_index].uv = uv
    mesh.materials.append(material)
    obj = bpy.data.objects.new("UV_QuickCheck_Reference_2x2m", mesh)
    context.collection.objects.link(obj)
    obj["uv_wall_reference"] = True
    obj["uv_wall_quick_check"] = True
    obj["module_size"] = 2.0
    obj["texel_density"] = 1000
    return obj


def create_quick_check_line(context, name, points, closed=False):
    mesh = bpy.data.meshes.new(name + "_Mesh")
    edges = [(index, index + 1) for index in range(len(points) - 1)]
    if closed:
        edges.append((len(points) - 1, 0))
    mesh.from_pydata(points, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)
    obj["uv_wall_quick_check"] = True
    return obj


def create_quick_check_profile(context):
    profile = create_quick_check_line(
        context, "UV_QuickCheck_Profile",
        ((0.0, 0.0, 0.0), (0.30, 0.0, 0.0),
         (0.30, 0.0, 0.22), (0.08, 0.0, 0.35)),
        closed=False,
    )
    profile["uv_wall_quick_check_role"] = "CORNICE_PROFILE"
    return profile


class OBJECT_OT_uv_wall_quick_check(bpy.types.Operator):
    bl_idname = "object.uv_wall_quick_check"
    bl_label = "Быстрая проверка"
    bl_description = (
        "Самостоятельно создать эталон, тестовые линии и профиль; "
        "проверить стену, поверхность и карниз; подготовить единый лог"
    )
    bl_options = {"REGISTER", "UNDO"}

    clear_previous_log: bpy.props.BoolProperty(
        name="Очистить старый лог перед проверкой",
        description="Оставить в буфере только текущую быструю проверку",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and hasattr(context.scene, "uv_wall_settings")

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Автоматическая проверка основных режимов", icon="EXPERIMENTAL")
        box = layout.box()
        box.label(text="Будут созданы:")
        box.label(text="• эталон 2×2 м / 2K и материал")
        box.label(text="• линии закрытой и открытой стены")
        box.label(text="• контур поверхности и профиль карниза")
        box.label(text="• четыре результата проверки")
        layout.label(text="Исходные объекты сцены не изменяются", icon="LOCKED")
        layout.label(text="После завершения лог автоматически скопируется", icon="COPYDOWN")
        layout.prop(self, "clear_previous_log")

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        started = perf_counter()
        results = []
        created_inputs = []
        built_objects = []
        if self.clear_previous_log:
            previous_log = bpy.data.texts.get(LOG_TEXT_NAME)
            if previous_log is not None:
                previous_log.clear()
        log_event("=" * 72, "QUICK_CHECK")
        log_event(
            f"QUICK_CHECK_BEGIN addon={'.'.join(map(str, bl_info['version']))}; "
            f"Blender={bpy.app.version_string}; scene={context.scene.name}",
            "QUICK_CHECK",
        )

        def run_case(case_id, callback):
            case_started = perf_counter()
            objects_before = set(bpy.data.objects)
            meshes_before = set(bpy.data.meshes)
            log_event(f"QUICK_CHECK_CASE_BEGIN id={case_id}", "QUICK_CHECK")
            try:
                produced = callback()
                if produced is not None:
                    built_objects.append(produced)
                elapsed = perf_counter() - case_started
                results.append((case_id, True, "", elapsed))
                log_event(
                    f"QUICK_CHECK_CASE_END id={case_id}; status=PASS; elapsed={elapsed:.6f}s",
                    "QUICK_CHECK",
                )
            except Exception as error:
                cleanup_created_datablocks(objects_before, meshes_before)
                elapsed = perf_counter() - case_started
                details = f"{type(error).__name__}: {error}"
                results.append((case_id, False, details, elapsed))
                log_event(
                    f"QUICK_CHECK_CASE_END id={case_id}; status=FAIL; elapsed={elapsed:.6f}s; "
                    f"error={details}\n{traceback.format_exc()}",
                    "QUICK_CHECK_ERROR",
                )

        try:
            reference = create_quick_check_reference(context)
            created_inputs.append(reference)
            source_quad_data(reference, 2.0)
            log_event("QUICK_CHECK_REFERENCE status=PASS; module=2; image=2048x2048", "QUICK_CHECK")

            wall_closed = create_quick_check_line(
                context, "UV_QuickCheck_Wall_Closed",
                ((0.0, 0.0, 5.0), (8.0, 0.0, 5.5), (10.0, 5.0, 4.8),
                 (5.0, 9.0, 6.0), (-1.0, 6.0, 5.2)), True,
            )
            wall_open = create_quick_check_line(
                context, "UV_QuickCheck_Wall_Open",
                ((20.0, 0.0, 6.0), (26.0, 0.0, 6.0),
                 (30.0, 3.0, 7.2), (36.0, 2.0, 6.4)), False,
            )
            surface = create_quick_check_line(
                context, "UV_QuickCheck_Surface",
                ((45.0, 0.0, 0.0), (57.0, -2.0, 2.0), (65.0, 6.0, -1.0),
                 (61.0, 17.0, 3.0), (50.0, 19.0, 0.5), (42.0, 10.0, -1.5)), True,
            )
            cornice_path = create_quick_check_line(
                context, "UV_QuickCheck_Cornice_Path",
                # First corner is deliberately not on a 2 m global module
                # boundary. This regresses local UV cuts after a manual seam.
                ((75.0, 0.0, 8.0), (80.3, 0.0, 8.0),
                 (85.0, 4.0, 8.6), (91.0, 4.0, 8.2)), False,
            )
            profile = create_quick_check_profile(context)
            created_inputs.extend((wall_closed, wall_open, surface, cornice_path, profile))

            def case_wall_closed():
                obj, _faces, _inset = build_wall(
                    context, reference, wall_closed, 2.0, "QC_Result_Wall_Closed",
                    True, 0.35, "LEFT", True, False, True, "BETWEEN",
                    stories=2, cap_rotation_deg=0.0, create_bottom_cap=True,
                    first_story_height=0.0, align_bottom=False, bottom_extend=0.0,
                    stories_down=True, wall_normal_mode="OUTWARD",
                )
                return obj

            def case_wall_open():
                obj, _faces, _inset = build_wall(
                    context, reference, wall_open, 2.0, "QC_Result_Wall_Open",
                    False, 0.35, "LEFT", False, False, False, "OUTER_ONLY",
                    stories=1, cap_rotation_deg=0.0, create_bottom_cap=False,
                    first_story_height=0.0, align_bottom=False, bottom_extend=0.0,
                    stories_down=True, wall_normal_mode="PATH_RIGHT",
                )
                return obj

            def case_surface():
                obj, _faces, _chains, _skipped, errors = build_roof_only(
                    context, reference, surface, 2.0, "QC_Result_Surface",
                    17.0, "UP", "SMOOTH_LEGACY",
                )
                if errors:
                    raise ValueError("; ".join(errors))
                return obj

            def case_surface_down():
                obj, _faces, _chains, _skipped, errors = build_roof_only(
                    context, reference, surface, 2.0, "QC_Result_Surface_Down",
                    17.0, "DOWN", "SMOOTH_LEGACY",
                )
                if errors:
                    raise ValueError("; ".join(errors))
                return obj

            def case_cornice():
                obj, _faces = build_cornice(
                    context, reference, cornice_path, profile, 2.0,
                    "QC_Result_Cornice", False, "MAX_REAL_WORLD", "AUTHORED", 0.25,
                )
                return obj

            def case_cornice_manual_corner():
                corner_point = cornice_path.matrix_world @ cornice_path.data.vertices[1].co
                obj, _faces = build_cornice(
                    context, reference, cornice_path, profile, 2.0,
                    "QC_Result_Cornice_Manual", False, "MAX_CLIPPED", "AUTHORED", 0.25,
                    manual_corner_points=[corner_point],
                )
                return obj

            run_case("WALL_CLOSED_INNER_CAPS", case_wall_closed)
            run_case("WALL_OPEN_SINGLE", case_wall_open)
            run_case("SURFACE_SMOOTH", case_surface)
            run_case("SURFACE_DOWN_NO_FLIP", case_surface_down)
            run_case("CORNICE_OPEN_PROFILE", case_cornice)
            run_case("CORNICE_MANUAL_CORNER", case_cornice_manual_corner)
        except Exception as error:
            results.append(("SETUP", False, f"{type(error).__name__}: {error}", 0.0))
            log_event(
                f"QUICK_CHECK_SETUP status=FAIL; error={type(error).__name__}: {error}\n"
                f"{traceback.format_exc()}", "QUICK_CHECK_ERROR",
            )

        passed = sum(1 for _case, ok, _details, _elapsed in results if ok)
        failed = sum(1 for _case, ok, _details, _elapsed in results if not ok)
        total_elapsed = perf_counter() - started
        summary = f"Быстрая проверка: PASS {passed}, FAIL {failed}, {total_elapsed:.2f} с"
        settings.quick_check_summary = summary
        log_event(
            f"QUICK_CHECK_SUMMARY passed={passed}; failed={failed}; total={len(results)}; "
            f"elapsed={total_elapsed:.6f}s; inputs={len(created_inputs)}; outputs={len(built_objects)}",
            "QUICK_CHECK",
        )
        for case_id, ok, details, elapsed in results:
            log_event(
                f"QUICK_CHECK_RESULT id={case_id}; status={'PASS' if ok else 'FAIL'}; "
                f"elapsed={elapsed:.6f}s; details={details or '-'}", "QUICK_CHECK",
            )
        log_event("QUICK_CHECK_END", "QUICK_CHECK")
        log_event("=" * 72, "QUICK_CHECK")

        text_block = bpy.data.texts.get(LOG_TEXT_NAME)
        if text_block is not None:
            context.window_manager.clipboard = text_block.as_string()
        for selected in context.selected_objects:
            selected.select_set(False)
        visible = [obj for obj in built_objects if obj and obj.name in bpy.data.objects]
        if visible:
            for obj in visible:
                obj.select_set(True)
            context.view_layer.objects.active = visible[-1]
        elif created_inputs:
            created_inputs[-1].select_set(True)
            context.view_layer.objects.active = created_inputs[-1]

        if failed:
            self.report({"WARNING"}, summary + ". Лог уже скопирован")
        else:
            self.report({"INFO"}, summary + ". Лог уже скопирован")
        return {"FINISHED"}


