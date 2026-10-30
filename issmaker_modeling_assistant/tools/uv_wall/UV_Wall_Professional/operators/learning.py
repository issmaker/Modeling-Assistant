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

class OBJECT_OT_uv_wall_set_mode(bpy.types.Operator):
    bl_idname = "object.uv_wall_set_mode"
    bl_label = "Выбрать режим UV Wall"
    bl_options = {"INTERNAL"}

    target_mode: bpy.props.EnumProperty(
        items=(
            ("WALL", "Стена", "Строить стены"),
            ("ROOF_ONLY", "Поверхность", "Строить поверхность"),
            ("CORNICE", "Карниз", "Строить карниз"),
            ("TEST", "Тест", "Проверить аддон"),
            ("LEARN", "Обучение", "Пошаговая помощь"),
        )
    )

    def execute(self, context):
        context.scene.uv_wall_settings.build_mode = self.target_mode
        return {"FINISHED"}


class OBJECT_OT_uv_wall_create_learning_example(bpy.types.Operator):
    bl_idname = "object.uv_wall_create_learning_example"
    bl_label = "Создать учебный пример"
    bl_description = "Создать готовую правильную линию для текущего урока"
    bl_options = {"REGISTER", "UNDO"}

    example: bpy.props.EnumProperty(
        items=(
            ("WALL_OPEN", "Простая стена", "Открытая линия"),
            ("WALL_CLOSED", "Замкнутая стена", "Контур для стены с толщиной"),
            ("SURFACE", "Поверхность", "Замкнутый высотный контур"),
            ("CORNICE", "Карниз", "Путь и профиль"),
        )
    )

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and hasattr(context.scene, "uv_wall_settings")

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        origin = context.scene.cursor.location.copy()

        def shifted(points):
            return [tuple(origin + Vector(point)) for point in points]

        if self.example == "WALL_OPEN":
            path = create_quick_check_line(
                context, "UV_Learn_Wall_Open",
                shifted(((0, 0, 3), (6, 0, 3), (10, 3, 3.5), (15, 3, 3))),
                False,
            )
            settings.path_mesh = path
            settings.create_inner = False
            settings.create_top_cap = False
            settings.create_bottom_cap = False
        elif self.example == "WALL_CLOSED":
            path = create_quick_check_line(
                context, "UV_Learn_Wall_Closed",
                shifted(((0, 0, 4), (10, 0, 4), (10, 7, 4.5), (0, 7, 4))),
                True,
            )
            settings.path_mesh = path
            settings.create_inner = False
            settings.create_top_cap = True
            settings.create_bottom_cap = False
        elif self.example == "SURFACE":
            smooth_surface = tuple(
                (
                    7.0 + cos(radians(index * 30.0)) * 7.0,
                    5.0 + sin(radians(index * 30.0)) * 5.0,
                    0.45 + sin(radians(index * 60.0)) * 0.35,
                )
                for index in range(12)
            )
            path = create_quick_check_line(
                context, "UV_Learn_Surface",
                shifted(smooth_surface),
                True,
            )
            settings.path_mesh = path
        else:
            path = create_quick_check_line(
                context, "UV_Learn_Cornice_Path",
                shifted(((0, 0, 4), (7, 0, 4), (11, 4, 4.4), (17, 4, 4))),
                False,
            )
            profile = create_quick_check_profile(context)
            profile.name = "UV_Learn_Cornice_Profile"
            settings.path_mesh = path
            settings.cornice_profile = profile
            settings.cornice_corner_mode = "AUTO"
            settings.cornice_uv_mode = "MAX_REAL_WORLD"

        for selected in context.selected_objects:
            selected.select_set(False)
        path.select_set(True)
        context.view_layer.objects.active = path
        self.report({"INFO"}, "Учебные объекты созданы и назначены. Добавьте UV-эталон, затем стройте")
        return {"FINISHED"}


def learning_step_ready(settings):
    """Return whether the current tutorial checkpoint has been completed."""
    step = settings.learning_step
    source_ready = is_valid_source_reference(settings.source_plane)
    path_ready = (
        settings.path_mesh is not None
        and settings.path_mesh.type == "MESH"
        and len(settings.path_mesh.data.edges) > 0
        and len(settings.path_mesh.data.polygons) == 0
    )
    surface_path_ready = path_ready and mesh_path_is_closed(settings.path_mesh)
    profile_ready = (
        settings.cornice_profile is not None
        and settings.cornice_profile.type == "MESH"
        and len(settings.cornice_profile.data.edges) > 0
        and len(settings.cornice_profile.data.polygons) == 0
        and settings.cornice_profile != settings.path_mesh
    )
    checks = {
        2: source_ready,
        3: surface_path_ready,
        5: settings.learning_surface_done,
        7: source_ready,
        8: path_ready,
        9: path_ready and not settings.create_inner and not settings.create_top_cap and not settings.create_bottom_cap,
        10: settings.learning_wall_simple_done,
        11: path_ready and mesh_path_is_closed(settings.path_mesh) and not settings.create_inner and (settings.create_top_cap or settings.create_bottom_cap),
        12: settings.learning_wall_caps_done,
        13: path_ready and settings.create_inner,
        14: settings.learning_wall_inner_done,
        16: source_ready,
        17: path_ready and profile_ready,
        20: settings.learning_cornice_done,
    }
    return checks.get(step, True)


class OBJECT_OT_uv_wall_learning_build(bpy.types.Operator):
    bl_idname = "object.uv_wall_learning_build"
    bl_label = "Построить учебный результат"
    bl_description = "Выполнить текущий учебный пример и остаться в режиме обучения"
    bl_options = {"REGISTER", "UNDO"}

    target_mode: bpy.props.EnumProperty(
        items=(
            ("ROOF_ONLY", "Поверхность", "Построить поверхность"),
            ("WALL", "Стена", "Построить стену"),
            ("CORNICE", "Карниз", "Построить карниз"),
        )
    )
    wall_lesson: bpy.props.EnumProperty(
        items=(
            ("SIMPLE", "Простая", "Одна стена без крышки, пола и внутренней стороны"),
            ("CAPS", "Крышка и пол", "Замкнутая стена с верхней или нижней поверхностью"),
            ("INNER", "Внутренняя", "Внешняя и внутренняя стены"),
        ),
        default="SIMPLE",
    )

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and hasattr(context.scene, "uv_wall_settings")

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        source_obj = settings.source_plane
        path_obj = settings.path_mesh
        if not is_valid_source_reference(source_obj) or path_obj is None:
            self.report({"ERROR"}, "Назначьте правильный UV-эталон и путь")
            return {"CANCELLED"}
        if self.target_mode == "CORNICE" and settings.cornice_profile is None:
            self.report({"ERROR"}, "Назначьте профиль карниза")
            return {"CANCELLED"}
        if self.target_mode == "WALL":
            if self.wall_lesson == "SIMPLE" and (
                settings.create_inner or settings.create_top_cap or settings.create_bottom_cap
            ):
                self.report({"ERROR"}, "Для простой стены выключите толщину, крышку и пол")
                return {"CANCELLED"}
            if self.wall_lesson == "CAPS" and (
                settings.create_inner
                or not mesh_path_is_closed(path_obj)
                or not (settings.create_top_cap or settings.create_bottom_cap)
            ):
                self.report({"ERROR"}, "Нужен замкнутый путь, выключенная толщина и включённая крышка или пол")
                return {"CANCELLED"}
            if self.wall_lesson == "INNER" and not settings.create_inner:
                self.report({"ERROR"}, "Включите внутреннюю стену")
                return {"CANCELLED"}
        settings.tile_length = float(settings.module_standard)
        objects_before = set(bpy.data.objects)
        meshes_before = set(bpy.data.meshes)
        started = perf_counter()
        log_event(f"LEARNING_BUILD_BEGIN target={self.target_mode}", "LEARNING")
        try:
            if self.target_mode == "ROOF_ONLY":
                result_obj, _faces, _chains, _skipped, _errors = build_roof_only(
                    context, source_obj, path_obj, settings.tile_length,
                    settings.output_name or "UV_Learn_Surface", settings.cap_rotation,
                    settings.roof_normal, settings.surface_method,
                )
            elif self.target_mode == "WALL":
                cap_mode = (
                    "BETWEEN" if settings.create_inner
                    else "OUTER_ONLY" if settings.create_top_cap or settings.create_bottom_cap
                    else settings.top_cap_mode
                )
                result_obj, _faces, _inset = build_wall(
                    context, source_obj, path_obj, settings.tile_length,
                    settings.output_name or "UV_Learn_Wall", settings.create_inner,
                    settings.inset_distance, settings.inset_side, settings.close_ends,
                    settings.keep_inner_path, settings.create_top_cap, cap_mode,
                    settings.stories, settings.cap_rotation, settings.create_bottom_cap,
                    settings.first_story_height if settings.custom_first_story else 0.0,
                    settings.align_bottom, settings.bottom_extend,
                    settings.story_direction == "DOWN", settings.wall_normal_mode,
                )
            else:
                result_obj, _faces = build_cornice(
                    context, source_obj, path_obj, settings.cornice_profile,
                    settings.tile_length, settings.output_name or "UV_Learn_Cornice",
                    settings.cornice_flip_side, settings.cornice_uv_mode,
                    "AUTHORED", settings.cornice_linear_step,
                    manual_corner_points=validated_cornice_corner_points(settings, settings.path_mesh, remove_stale=True),
                )
        except Exception as error:
            cleanup_created_datablocks(objects_before, meshes_before)
            log_event(
                f"LEARNING_BUILD_ERROR target={self.target_mode}; "
                f"{type(error).__name__}: {error}\n{traceback.format_exc()}",
                "ERROR",
            )
            self.report({"ERROR"}, f"Учебное построение отменено: {error}")
            return {"CANCELLED"}
        if self.target_mode == "ROOF_ONLY":
            settings.learning_surface_done = True
        elif self.target_mode == "WALL":
            if self.wall_lesson == "CAPS":
                settings.learning_wall_caps_done = True
            elif self.wall_lesson == "INNER":
                settings.learning_wall_inner_done = True
            else:
                settings.learning_wall_simple_done = True
        else:
            settings.learning_cornice_done = True
        log_event(
            f"LEARNING_BUILD_FINISH target={self.target_mode}; object={result_obj.name}; "
            f"elapsed={perf_counter() - started:.6f}s",
            "LEARNING",
        )
        self.report({"INFO"}, "Результат создан. Осмотрите его и переходите дальше")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_learning_step(bpy.types.Operator):
    bl_idname = "object.uv_wall_learning_step"
    bl_label = "Шаг обучения"
    bl_options = {"INTERNAL"}

    direction: bpy.props.IntProperty(default=1, min=-1, max=1)

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        if self.direction > 0 and not learning_step_ready(settings):
            self.report({"WARNING"}, "Сначала выполните отмеченное действие текущего шага")
            return {"CANCELLED"}
        settings.learning_step = max(1, min(22, settings.learning_step + self.direction))
        return {"FINISHED"}


class OBJECT_OT_uv_wall_support(bpy.types.Operator):
    bl_idname = "object.uv_wall_support"
    bl_label = "Поддержка"
    bl_description = "Подготовить диагностический лог и открыть чат технической поддержки"

    @classmethod
    def poll(cls, context):
        text_block = bpy.data.texts.get(LOG_TEXT_NAME)
        return (
            hasattr(context.scene, "uv_wall_settings")
            and text_block is not None
            and bool(text_block.as_string().strip())
        )

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Что сейчас произойдёт", icon="INFO")
        box = layout.box()
        box.label(text="1. Аддон добавит в журнал версию Blender и своё состояние")
        box.label(text="2. Полный диагностический лог скопируется в буфер обмена")
        box.label(text="3. В браузере по умолчанию откроется Telegram поддержки")
        box.label(text="4. Вставьте лог в чат сочетанием Ctrl+V и отправьте")
        warning = layout.box()
        warning.label(text="Сообщение само не отправляется", icon="LOCKED")
        warning.label(text="Перед отправкой вы сможете просмотреть весь текст")

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        active = context.active_object
        selected_meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
        log_event("=" * 72, "SUPPORT")
        log_event(
            f"SUPPORT_SNAPSHOT addon={'.'.join(map(str, bl_info['version']))}; "
            f"Blender={bpy.app.version_string}; scene={context.scene.name}; "
            f"context_mode={context.mode}; build_mode={settings.build_mode}; "
            f"active={active.name if active else 'NONE'}; selected_meshes={[obj.name for obj in selected_meshes]}; "
            f"path={settings.path_mesh.name if settings.path_mesh else 'NONE'}; "
            f"source={settings.source_plane.name if settings.source_plane else 'NONE'}; "
            f"profile={settings.cornice_profile.name if settings.cornice_profile else 'NONE'}; "
            f"module={settings.module_standard}; quick_check={settings.quick_check_summary or 'NOT_RUN'}",
            "SUPPORT",
        )
        if active is not None and active.type == "MESH":
            mesh = active.data
            log_event(
                f"SUPPORT_ACTIVE_OBJECT name={active.name}; data={mesh.name}; "
                f"verts={len(mesh.vertices)}; edges={len(mesh.edges)}; faces={len(mesh.polygons)}; "
                f"location={tuple(active.location)}; rotation={tuple(active.rotation_euler)}; "
                f"scale={tuple(active.scale)}; modifiers={[modifier.type for modifier in active.modifiers]}",
                "SUPPORT",
            )
        log_event("SUPPORT_SNAPSHOT_END", "SUPPORT")
        text_block = bpy.data.texts.get(LOG_TEXT_NAME)
        context.window_manager.clipboard = text_block.as_string() if text_block else ""
        try:
            opened = webbrowser.open("https://t.me/mishashkonda", new=2)
        except Exception as error:
            log_event(f"Не удалось открыть браузер: {error}", "ERROR")
            self.report({"ERROR"}, "Лог скопирован, но браузер открыть не удалось")
            return {"CANCELLED"}
        if not opened:
            self.report({"WARNING"}, "Лог скопирован. Откройте t.me/mishashkonda вручную")
        else:
            self.report({"INFO"}, "Лог скопирован. Вставьте его в чат поддержки через Ctrl+V")
        return {"FINISHED"}


