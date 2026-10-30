"""Problem Navigator operators for UV Wall NEXTGEN 1.6.2."""
from __future__ import annotations
import bpy
from mathutils import Vector
from ..core.problem_registry import active_problem, navigate_problem, clear_problems, scan_result_mesh, scan_result_uv_quality, scan_cornice_result_metadata, add_problem, explain_build_failure
from ..core.definitions import (
    definition_for, analyze_path, virtual_asset_for_module, sync_definition_from_scene,
    apply_serialized_definition,
)
from ..core.material_intelligence import inspect_material_asset
from ..production.validation import detect_cornice_sharp_vertices
from ..ui.settings import sync_corner_uids, validated_cornice_corner_points


def _focus_problem(context, problem):
    if problem is None or str(getattr(problem,'element_kind',''))=='BUILD':
        # A cancelled build does not automatically reveal the actual failed
        # polygon's coordinate; never imply that the path origin is that spot.
        return False
    pos = Vector(problem.world_position)
    radius = max(0.05, float(problem.radius))
    screen = getattr(context, "screen", None)
    areas = [a for a in getattr(screen, "areas", ()) if a.type == "VIEW_3D"]
    if not areas and getattr(context, "area", None) is not None and context.area.type == "VIEW_3D":
        areas = [context.area]
    changed = False
    for area in areas:
        space = area.spaces.active
        region_3d = getattr(space, "region_3d", None)
        if region_3d is None:
            continue
        region_3d.view_location = pos
        region_3d.view_distance = max(0.75, radius * 6.0)
        changed = True
        try:
            area.tag_redraw()
        except Exception:
            pass
    return changed


def refresh_scene_problems(context):
    settings = context.scene.uv_wall_settings
    path = settings.path_mesh
    definition = definition_for(path) if path else None
    # Preserve exact records emitted by GeometryProblemError even after users
    # press Refresh. Only preserve them while the Definition still reports a
    # failed build, and re-add *after* result mesh scans which clear GEOMETRY.
    failed=bool(getattr(definition,'last_error','')) if definition else False
    exact_failure_records=[]
    if failed:
        for old in settings.problems:
            if str(getattr(old,'category',''))!='GEOMETRY_BLOCKED':continue
            exact_failure_records.append(dict(
                subsystem=str(old.subsystem), severity=str(old.severity),
                title=str(old.title), description=str(old.description),
                object_name=str(old.object_name),world_position=tuple(old.world_position),
                radius=float(old.radius), element_kind=str(old.element_kind),
                element_indices=str(old.element_indices),
                suggested_action=str(old.suggested_action),
                category=str(old.category), code=str(old.problem_code),
            ))
    clear_problems(settings)

    # The old navigator only displayed successful result-mesh problems. A
    # cancelled build must still show a human explanation, including mode and
    # recovery advice, without pretending to know the failing face location.
    mode=str(getattr(settings,"build_mode","WALL") or "WALL").upper()
    mode={"ROOF_ONLY":"SURFACE"}.get(mode,mode)
    if definition is not None:
        mode=str(getattr(definition,"generator_type",mode) or mode).upper()
        failure=str(getattr(definition,"last_error","") or "").strip()
        if failure:
            name,explanation,action=explain_build_failure(mode,failure)
            center=path.matrix_world.translation if path is not None else (0.,0.,0.)
            add_problem(settings,subsystem=mode,severity="ERROR",
                        title=f"{name}: ошибка построения", description=explanation,
                        object_name=path.name if path is not None else "",
                        world_position=center, radius=1., code="LAST_BUILD_FAILURE",
                        suggested_action=action, element_kind="BUILD")
    # Path-level intersections are exact authored references and cheap to show.
    analysis = analyze_path(path) if path is not None else {}
    for index, point in enumerate(analysis.get("intersections", ())):
        from ..core.problem_registry import add_problem
        add_problem(settings, subsystem="PATH", severity="ERROR",
                    title="Пересечение пути", description=f"Пересечение #{index+1}",
                    object_name=getattr(path, "name", ""), world_position=point,
                    radius=0.65, suggested_action="Исправьте пересечение рёбер Path")

    # Module-aware material contract.
    if definition is not None:
        material, image = virtual_asset_for_module(definition)
        state = inspect_material_asset(material, image, definition.module_standard)
        if state["status"] != "VALID":
            from ..core.problem_registry import add_problem
            pos = getattr(path.matrix_world, "translation", (0.0,0.0,0.0)) if path else (0.0,0.0,0.0)
            add_problem(settings, subsystem="MATERIAL", severity="ERROR",
                        title="Материал/текстура модуля", description=state["message"],
                        object_name=getattr(path, "name", ""), world_position=pos,
                        radius=1.0, suggested_action="Проверьте Material Intelligence в панели")

    # Every material-bearing subsystem uses the same 2K/4K contract.
    if definition is not None:
        extra_materials=[]
        if getattr(definition,'create_inner_surface',False):
            extra_materials.append(('Материал клумбы/поверхности',getattr(definition,'inner_surface_material',None)))
        if getattr(definition,'create_top_cap',False) and getattr(definition,'cap_material_override',False):
            extra_materials.append(('Материал верхней крышки',getattr(definition,'cap_material',None)))
        if getattr(definition,'create_bottom_cap',False) and getattr(definition,'bottom_cap_material_override',False):
            extra_materials.append(('Материал нижней крышки',getattr(definition,'bottom_cap_material',None)))
        for title,material in extra_materials:
            state=inspect_material_asset(material,None,definition.module_standard)
            if state['status']!='VALID':
                pos=getattr(path.matrix_world,'translation',(0.,0.,0.)) if path else (0.,0.,0.)
                add_problem(settings,subsystem='MATERIAL',severity='ERROR',title=title,
                            description=state['message'],object_name=getattr(path,'name',''),world_position=pos,
                            radius=1.0,suggested_action='Назначьте материал с корректной текстурой и разрешением текущего стандарта')

    # Smooth Cornice mode warns about sharp authored turns instead of silently
    # stretching or hard-cancelling the entire build.
    if path is not None and mode == "CORNICE" and settings.cornice_uv_mode == "MAX_REAL_WORLD":
        threshold = float(getattr(settings, "cornice_smooth_warning_angle", 45.0))
        sharp = detect_cornice_sharp_vertices(path, threshold)
        world = path.matrix_world
        for vertex_index, turn in sharp:
            from ..core.problem_registry import add_problem
            point = world @ path.data.vertices[vertex_index].co
            add_problem(settings, subsystem="CORNICE", severity="WARNING",
                        title="Острый угол в плавном режиме",
                        description=f"Вершина {vertex_index} · поворот {turn:.1f}° · порог {threshold:.1f}°",
                        object_name=path.name, world_position=point, radius=0.55,
                        element_kind="CORNICE_VERTEX_WARNING", element_indices=str(vertex_index),
                        code="CORNICE_SMOOTH_SHARP_TURN",
                        suggested_action="Проверьте эту вершину. Если геометрия неверна, подкорректируйте путь или профиль. Для ручной подрезки используйте Sharp")

    if definition is not None and getattr(definition, "last_result_name", "") and settings.problem_auto_scan:
        result = bpy.data.objects.get(definition.last_result_name)
        if result is not None:
            scan_result_mesh(settings, result)
            scan_result_uv_quality(settings, result)
            if settings.build_mode == "CORNICE" or str(result.get("generator_type", "")) == "CORNICE":
                scan_cornice_result_metadata(settings, result)
    for record in exact_failure_records:
        add_problem(settings,**record)
    return len(settings.problems)


class OBJECT_OT_uv_wall_problem_navigate(bpy.types.Operator):
    bl_idname = "object.uv_wall_problem_navigate"
    bl_label = "Показать проблему"
    bl_description = "Перейти к проблеме и сфокусировать 3D View"
    bl_options = {"REGISTER"}
    direction: bpy.props.IntProperty(default=0, min=-1, max=1)

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        problem = navigate_problem(settings, self.direction) if self.direction else active_problem(settings)
        if problem is None:
            self.report({"INFO"}, "Problem Navigator: проблем нет")
            return {"CANCELLED"}
        _focus_problem(context, problem)
        self.report({"INFO"}, f"{settings.active_problem_index+1}/{len(settings.problems)} · {problem.title}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_problem_refresh(bpy.types.Operator):
    bl_idname = "object.uv_wall_problem_refresh"
    bl_label = "Проверить проблемы"
    bl_description = "Обновить Problem Navigator для Path, Material и текущего Result"
    bl_options = {"REGISTER"}

    def execute(self, context):
        count = refresh_scene_problems(context)
        if count:
            _focus_problem(context, active_problem(context.scene.uv_wall_settings))
            self.report({"WARNING"}, f"Найдено проблем: {count}")
        else:
            self.report({"INFO"}, "Проблем не найдено")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_problem_clear(bpy.types.Operator):
    bl_idname = "object.uv_wall_problem_clear"
    bl_label = "Очистить проблемы"
    bl_options = {"REGISTER"}

    def execute(self, context):
        clear_problems(context.scene.uv_wall_settings)
        return {"FINISHED"}


class OBJECT_OT_uv_wall_problem_make_sharp(bpy.types.Operator):
    bl_idname = "object.uv_wall_problem_make_sharp"
    bl_label = "Сделать этот угол Sharp"
    bl_description = "Сохранить угол активной Cornice-проблемы как authored Sharp и включить локальный UV clipping"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uv_wall_settings", None)
        problem = active_problem(settings) if settings is not None else None
        return bool(
            settings and settings.path_mesh and problem
            and str(problem.subsystem).startswith("CORNICE")
            and str(problem.element_kind).startswith("CORNICE_VERTEX")
        )

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        path = settings.path_mesh
        problem = active_problem(settings)
        if problem is None or path is None:
            return {"CANCELLED"}
        try:
            vertex_index = int(str(problem.element_indices).split(",")[0])
        except Exception:
            self.report({"ERROR"}, "Проблема не содержит индекс вершины Path")
            return {"CANCELLED"}
        mapping = sync_corner_uids(path)
        uid = next((uid for uid, data in mapping.items() if int(data[0]) == vertex_index), None)
        if uid is None:
            self.report({"ERROR"}, f"Вершина Path #{vertex_index} больше не существует")
            return {"CANCELLED"}
        existing = {int(marker.vertex_uid) for marker in settings.cornice_corners}
        if int(uid) not in existing:
            marker = settings.cornice_corners.add()
            marker.vertex_uid = int(uid)
            marker.cached_index = vertex_index
            marker.path_name = path.name
            marker.corner_type = "SHARP"
        validated_cornice_corner_points(settings, path, remove_stale=True)
        settings.cornice_uv_mode = "MAX_CLIPPED"
        sync_definition_from_scene(path, settings)
        self.report({"INFO"}, f"Вершина {vertex_index}: authored Sharp · включён режим Острые углы")
        refresh_scene_problems(context)
        return {"FINISHED"}


class OBJECT_OT_uv_wall_restore_last_successful(bpy.types.Operator):
    bl_idname = "object.uv_wall_restore_last_successful"
    bl_label = "Последние рабочие настройки"
    bl_description = "Восстановить параметры последнего успешного Build без скрытой копии Mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(getattr(context, "scene", None), "uv_wall_settings", None)
        path = getattr(settings, "path_mesh", None) if settings else None
        definition = definition_for(path) if path else None
        return bool(definition and getattr(definition, "last_successful_settings_json", ""))

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        path = settings.path_mesh
        definition = definition_for(path) if path else None
        payload = getattr(definition, "last_successful_settings_json", "") if definition else ""
        if not definition or not payload or not apply_serialized_definition(definition, payload):
            self.report({"ERROR"}, "Нет сохранённой успешной конфигурации")
            return {"CANCELLED"}
        definition.last_error = ""
        sync_scene_from_definition(path, settings)
        refresh_scene_problems(context)
        self.report({"INFO"}, "Восстановлены параметры последнего успешного Build · требуется Rebuild")
        return {"FINISHED"}


CLASSES = (
    OBJECT_OT_uv_wall_problem_navigate,
    OBJECT_OT_uv_wall_problem_refresh,
    OBJECT_OT_uv_wall_problem_clear,
    OBJECT_OT_uv_wall_problem_make_sharp,
    OBJECT_OT_uv_wall_restore_last_successful,
)
