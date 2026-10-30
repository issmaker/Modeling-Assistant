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

def mesh_object_poll(_self, obj):
    return obj is not None and obj.type == "MESH"


def path_reference_poll(self, obj):
    return (
        obj is not None
        and obj.type == "MESH"
        and len(obj.data.edges) > 0
        and len(obj.data.polygons) == 0
        and obj != self.source_plane
        and obj != self.cornice_profile
    )


def profile_reference_poll(self, obj):
    return (
        obj is not None
        and obj.type == "MESH"
        and len(obj.data.edges) > 0
        and len(obj.data.polygons) == 0
        and obj != self.path_mesh
        and obj != self.source_plane
    )


def is_valid_source_reference(obj):
    if obj is None or obj.type != "MESH":
        return False
    mesh = obj.data
    if len(mesh.polygons) != 1 or len(mesh.polygons[0].vertices) != 4 or mesh.uv_layers.active is None:
        return False
    polygon = mesh.polygons[0]
    if polygon.material_index >= len(mesh.materials):
        return False
    return material_has_image_texture(mesh.materials[polygon.material_index])


def source_reference_poll(self, obj):
    return (
        is_valid_source_reference(obj)
        and obj != self.path_mesh
        and obj != self.cornice_profile
    )


def update_module_standard(self, _context):
    self.tile_length = float(self.module_standard)


def update_path_reference(self, _context):
    """Corner IDs belong to one exact path and must not leak to another."""
    new_key = ""
    if getattr(self, "path_mesh", None) is not None and self.path_mesh.type == "MESH":
        new_key = self.path_mesh.data.name_full
    old_key = getattr(self, "cornice_corner_path_key", "")
    if hasattr(self, "cornice_corners") and old_key and old_key != new_key:
        self.cornice_corners.clear()
    self.cornice_corner_path_key = new_key


def read_corner_uid_indices(path_obj):
    """Read corner identity during GPU draw without mutating Mesh/BMesh.

    The authoring operator may allocate UIDs on a click. A draw handler must
    never allocate mesh attributes or update an Edit Mode BMesh on a redraw.
    """
    if path_obj is None or path_obj.type != 'MESH':
        return {}
    mesh=path_obj.data
    if path_obj.mode == 'EDIT':
        bm=bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        layer=bm.verts.layers.int.get(CORNER_UID_ATTRIBUTE)
        if layer is None:return {}
        return {int(vert[layer]):int(vert.index) for vert in bm.verts if int(vert[layer])>0}
    attribute=mesh.attributes.get(CORNER_UID_ATTRIBUTE)
    if attribute is None or attribute.domain!='POINT' or attribute.data_type!='INT':
        return {}
    return {int(value.value):i for i,value in enumerate(attribute.data) if int(value.value)>0}


def sync_corner_uids(path_obj):
    """Return uid -> (vertex index, world position), repairing new/duplicate IDs."""
    if path_obj is None or path_obj.type != "MESH":
        return {}
    mesh = path_obj.data
    matrix = path_obj.matrix_world
    mapping = {}
    if path_obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        layer = bm.verts.layers.int.get(CORNER_UID_ATTRIBUTE)
        if layer is None:
            layer = bm.verts.layers.int.new(CORNER_UID_ATTRIBUTE)
        next_uid = max((vert[layer] for vert in bm.verts), default=0) + 1
        seen = set()
        for vert in bm.verts:
            uid = int(vert[layer])
            if uid <= 0 or uid in seen:
                uid = next_uid
                next_uid += 1
                vert[layer] = uid
            seen.add(uid)
            mapping[uid] = (vert.index, matrix @ vert.co.copy(), len(vert.link_edges))
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        attribute = mesh.attributes.get(CORNER_UID_ATTRIBUTE)
        if attribute is None or attribute.domain != "POINT" or attribute.data_type != "INT":
            if attribute is not None:
                mesh.attributes.remove(attribute)
            attribute = mesh.attributes.new(CORNER_UID_ATTRIBUTE, "INT", "POINT")
        values = [int(item.value) for item in attribute.data]
        next_uid = max(values, default=0) + 1
        seen = set()
        degree_counts = [0] * len(mesh.vertices)
        for edge in mesh.edges:
            degree_counts[edge.vertices[0]] += 1
            degree_counts[edge.vertices[1]] += 1
        for index, vertex in enumerate(mesh.vertices):
            uid = values[index] if index < len(values) else 0
            if uid <= 0 or uid in seen:
                uid = next_uid
                next_uid += 1
                attribute.data[index].value = uid
            seen.add(uid)
            mapping[uid] = (index, matrix @ vertex.co.copy(), degree_counts[index])
    return mapping


def validated_cornice_corner_points(settings, path_obj, remove_stale=True):
    """Resolve saved corner IDs against the current edited path."""
    mapping = sync_corner_uids(path_obj)
    points = []
    stale = []
    for index, marker in enumerate(settings.cornice_corners):
        if marker.vertex_uid not in mapping or mapping[marker.vertex_uid][2] != 2:
            stale.append(index)
            continue
        vertex_index, point, _degree = mapping[marker.vertex_uid]
        marker.cached_index = vertex_index
        points.append(point.copy())
    if remove_stale:
        for index in reversed(stale):
            settings.cornice_corners.remove(index)
        if stale:
            log_event(f"CORNICE_CORNERS stale_removed={len(stale)}", "AUTO_FIX")
    return points


class UVWALL_PG_corner_marker(bpy.types.PropertyGroup):
    vertex_uid: bpy.props.IntProperty(default=0)
    cached_index: bpy.props.IntProperty(default=-1)
    path_name: bpy.props.StringProperty(default="")
    corner_type: bpy.props.EnumProperty(
        name="Тип угла",
        items=(("SHARP", "Острый", "Авторский острый угол карниза"),
               ("SMOOTH", "Плавный", "Явно помеченный плавный угол")),
        default="SHARP",
    )
    corner_method: bpy.props.EnumProperty(
        name="Способ острого угла",
        items=(
            ('CLASSIC','Classic · Miter','Исходное построение'),
            ('BISECTOR','Bisector · Compact','Биссектриса без miter-растяжения'),
            ('INCOMING','Previous · Safe Bias','Сечение с безопасным уклоном к входящему сегменту'),
            ('OUTGOING','Next · Safe Bias','Сечение с безопасным уклоном к выходящему сегменту'),
            ('LIMITED','Bounded · 1.35×','Ограниченный miter'),
        ),
        default='CLASSIC',
    )


class UVWALL_PG_problem(bpy.types.PropertyGroup):
    problem_id: bpy.props.StringProperty(default="")
    problem_code: bpy.props.StringProperty(default="")
    definition_uid: bpy.props.StringProperty(default="")
    subsystem: bpy.props.StringProperty(default="GENERAL")
    category: bpy.props.EnumProperty(
        items=(("USER_INPUT", "Input", "Некорректный пользовательский ресурс"),
               ("GEOMETRY_BLOCKED", "Blocked", "Геометрия отклонена до commit"),
               ("RECOVERABLE_WARNING", "Warning", "Исправимое предупреждение"),
               ("INTERNAL_ERROR", "Internal", "Нарушение внутреннего инварианта")),
        default="RECOVERABLE_WARNING",
    )
    state: bpy.props.EnumProperty(
        items=(("ACTIVE", "Active", "Требует внимания"),
               ("ACKNOWLEDGED", "Acknowledged", "Просмотрено"),
               ("RESOLVED", "Resolved", "Исправлено"),
               ("STALE", "Stale", "Относится к старой ревизии")),
        default="ACTIVE",
    )
    occurrence_count: bpy.props.IntProperty(default=1, min=1)
    updated_utc: bpy.props.StringProperty(default="")
    severity: bpy.props.EnumProperty(
        name="Важность",
        items=(("INFO", "Info", "Информация"),
               ("WARNING", "Warning", "Предупреждение"),
               ("ERROR", "Error", "Ошибка")),
        default="WARNING",
    )
    title: bpy.props.StringProperty(default="")
    description: bpy.props.StringProperty(default="")
    object_name: bpy.props.StringProperty(default="")
    world_position: bpy.props.FloatVectorProperty(size=3, subtype="XYZ")
    radius: bpy.props.FloatProperty(default=0.5, min=0.05)
    element_kind: bpy.props.StringProperty(default="")
    element_indices: bpy.props.StringProperty(default="")
    suggested_action: bpy.props.StringProperty(default="")


class UVWALL_PG_settings(bpy.types.PropertyGroup):
    cornice_corner_path_key: bpy.props.StringProperty(default="", options={"HIDDEN"})
    build_mode: bpy.props.EnumProperty(
        name="Режим",
        items=(
            ("NONE", "Выберите", "Сначала выберите, что требуется построить", "QUESTION", 0),
            ("WALL", "Стена", "Стены + крышка/пол", "MOD_SOLIDIFY", 1),
            ("ROOF_ONLY", "Поверхность", "Кровля, площадка или ландшафт по замкнутому контуру", "MESH_GRID", 2),
            ("CORNICE", "Карниз", "Протянуть замкнутый профиль вдоль пути", "MOD_CURVE", 3),
            ("TEST", "Тест", "Создание эталонных стресс-контуров", "TOOL_SETTINGS", 4),
            ("LEARN", "Обучение", "Пошаговое знакомство с аддоном", "HELP", 5),
        ),
        default="NONE",
    )
    viewport_show_legacy_ui: bpy.props.BoolProperty(
        name="Показать Legacy / Advanced",
        description="Открыть старые расширенные панели. Повседневная работа выполняется во Viewport Inspector",
        default=False,
    )
    problem_inspector_visible: bpy.props.BoolProperty(
        name="Показывать инспектор ошибок",
        description="Показывать подробную карточку ошибки поверх 3D View; при скрытии остаётся компактный индикатор",
        default=True,
    )
    viewport_prefer_virtual: bpy.props.BoolProperty(
        name="Виртуальный UV-эталон",
        description="Новые Path Definition используют UV-модуль без Plane-объекта в сцене",
        default=True,
    )
    learning_step: bpy.props.IntProperty(
        name="Шаг обучения",
        default=1,
        min=1,
        max=22,
        options={"SKIP_SAVE"},
    )
    learning_surface_done: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})
    learning_wall_simple_done: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})
    learning_wall_caps_done: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})
    learning_wall_inner_done: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})
    learning_cornice_done: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})
    diagnostic_detail: bpy.props.EnumProperty(
        name="Детализация журнала",
        description="NORMAL — компактный production-журнал; DEEP — выборка геометрии; FORENSIC — полный дамп для редких воспроизводимых ошибок",
        items=(
            ("NORMAL", "Normal", "Компактная сводка и аномалии"),
            ("DEEP", "Deep", "До 256 вершин/граней на результат"),
            ("FORENSIC", "Forensic", "До 20 000 вершин/граней; только для расследования"),
        ),
        default="NORMAL",
    )
    quick_check_summary: bpy.props.StringProperty(
        name="Результат быстрой проверки",
        default="",
        options={"SKIP_SAVE"},
    )
    problems: bpy.props.CollectionProperty(type=UVWALL_PG_problem, options={"SKIP_SAVE"})
    active_problem_index: bpy.props.IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    problem_auto_scan: bpy.props.BoolProperty(
        name="Проверять проблемы после Build",
        description="Искать non-manifold и возможные пересечения и добавлять их в Problem Navigator",
        default=True,
    )
    path_mesh: bpy.props.PointerProperty(
        name="Путь",
        description="Открытая или замкнутая цепочка рёбер (Mesh). Контур стены",
        type=bpy.types.Object,
        poll=path_reference_poll,
        update=update_path_reference,
    )
    source_plane: bpy.props.PointerProperty(
        name="Исходный плейн",
        description="Квад с UV. Его положение и ориентация в сцене не влияют на построение",
        type=bpy.types.Object,
        poll=source_reference_poll,
    )
    cornice_profile: bpy.props.PointerProperty(
        name="Профиль карниза",
        description="Одна открытая или замкнутая цепь рёбер в локальной плоскости XZ; Origin касается пути",
        type=bpy.types.Object,
        poll=profile_reference_poll,
    )
    cornice_flip_side: bpy.props.BoolProperty(
        name="Развернуть сторону",
        description="Принудительно поменять сторону профиля; для открытого пути автоматический режим сам выбирает безопасную сторону без самопересечений",
        default=False,
    )
    cornice_uv_mode: bpy.props.EnumProperty(
        name="UV-метод карниза",
        items=(
            (
                "MAX_REAL_WORLD", "Плавные углы",
                "Рекомендуется для плавных, округлых или плотных линий: U вдоль пути, V вокруг профиля в метрах",
            ),
            (
                "MAX_CLIPPED", "Острые углы · Sharp",
                "Подрезает UV только на вершинах пути, которые пользователь добавил в список углов",
            ),
        ),
        default="MAX_REAL_WORLD",
    )
    cornice_corners: bpy.props.CollectionProperty(type=UVWALL_PG_corner_marker)
    cornice_corner_mode: bpy.props.EnumProperty(
        name="Обработка пути",
        items=(
            ("AUTO", "Авто углы — production", "Безопасный miter; на экстремальном угле локальный fallback без бесконечного вылета"),
            ("AUTHORED", "Как нарисовано", "Точные углы и точки исходного пути; опасный miter считается ошибкой"),
            ("ADAPTIVE", "Адаптивно", "Локально добавляет точки там, где соседние полосы профиля расходятся"),
            ("DENSE_LINEAR", "Добавить точки", "Делит прямые отрезки; форма пути и углы не изменяются"),
        ),
        default="AUTO",
    )
    cornice_linear_step: bpy.props.FloatProperty(
        name="Шаг прямых",
        description="Расстояние между виртуальными точками контрольного линейного режима",
        default=0.25, min=0.002, soft_max=2.0, precision=3, 
    )
    cornice_smooth_warning_angle: bpy.props.FloatProperty(
        name="Порог острого угла",
        description="В плавном режиме углы с таким поворотом и больше попадают в Problem Navigator",
        default=45.0, min=1.0, max=179.0, precision=1,
    )
    reference_image: bpy.props.PointerProperty(
        name="Устаревшая текстура", type=bpy.types.Image, options={"HIDDEN"},
    )
    reference_material: bpy.props.PointerProperty(
        name="Материал эталона",
        description="Существующий материал с Image Texture",
        type=bpy.types.Material,
    )
    create_reference_material: bpy.props.BoolProperty(
        name="Создать новый материал (старый режим)",
        default=False, options={"HIDDEN"},
    )
    reference_material_name: bpy.props.StringProperty(
        name="Имя нового материала",
        default="UV_Wall_Material",
    )
    module_standard: bpy.props.EnumProperty(
        name="Стандарт модуля",
        items=(
            ("2", "2×2 м / 2K", "2000 полезных пикселей и 24 px отступ с каждой стороны"),
            ("4", "4×4 м / 4K", "4000 полезных пикселей и 48 px отступ с каждой стороны"),
        ),
        default="4",
        update=update_module_standard,
    )
    tile_length: bpy.props.FloatProperty(
        name="Длина модуля",
        description="Размер одной UV-плитки в мировых единицах: вдоль стены и на сетке крышки",
        default=4.0,
        min=0.001,
        
    )
    wall_normal_mode: bpy.props.EnumProperty(
        name="Нормали стены",
        items=(
            ("OUTWARD", "Наружу", "Для замкнутого контура определить внешнюю сторону автоматически"),
            ("INWARD", "Внутрь", "Для замкнутого контура направить нормали внутрь"),
            ("PATH_LEFT", "Влево", "Нормали влево относительно направления рёбер пути"),
            ("PATH_RIGHT", "Вправо", "Нормали вправо относительно направления рёбер пути"),
        ),
        default="OUTWARD",
    )
    roof_normal: bpy.props.EnumProperty(
        name="Нормали крыши",
        items=(("UP", "Вверх", "Нормали по +Z"), ("DOWN", "Вниз", "Нормали по -Z")),
        default="UP",
    )
    # Retain legacy enum as serialized compatibility data. No UI selector in 1.5.13.
    surface_method: bpy.props.EnumProperty(
        name="Алгоритм поверхности",
        items=(
            ("SMOOTH_LEGACY", "Плавный ландшафт", "Регулярная модульная сетка с глобальным плавным распределением высоты от всего контура"),
            ("TRIANGULATED", "Адаптивная триангуляция", "Запасной режим: уплотнённая треугольная опорная сетка"),
        ),
        default="SMOOTH_LEGACY",
    )
    stories: bpy.props.IntProperty(
        name="Этажи",
        description="Сколько этажей вверх от Z пути. Каждый этаж = высота и UV исходного плейна",
        default=1,
        min=1,
        max=256,
    )
    first_story_height: bpy.props.FloatProperty(
        name="Высота 1-го этажа",
        description="Высота нижнего этажа в метрах. 0 = как у исходного плейна. Остальные этажи — высота плейна",
        default=0.0,
        min=0.0,
        soft_max=20.0,
        step=1,
        precision=3,
        
    )
    custom_first_story: bpy.props.BoolProperty(
        name="Особая высота первого модуля",
        description="Разрешить первому модулю отличаться от стандарта 2 или 4 м",
        default=False,
    )
    story_direction: bpy.props.EnumProperty(
        name="Направление этажей",
        description="Куда наращивать этажи относительно линии пути",
        items=(
            (
                "DOWN",
                "Вниз",
                "Линия пути = верх стены, этажи идут вниз",
            ),
            (
                "UP",
                "Вверх",
                "Линия пути = низ стены, этажи идут вверх",
            ),
        ),
        default="DOWN",
    )
    align_bottom: bpy.props.BoolProperty(
        name="Выровнять низ",
        description="Общий низ стены: от самой нижней точки пути ещё вниз на «Отступ вниз». Верх остаётся на линии пути",
        default=False,
    )
    bottom_extend: bpy.props.FloatProperty(
        name="Отступ вниз",
        description="На сколько метров опустить общий низ относительно самой нижней точки пути",
        default=0.0,
        min=0.0,
        soft_max=50.0,
        step=1,
        precision=3,
        
    )
    output_name: bpy.props.StringProperty(
        name="Имя объекта",
        description="Имя создаваемой стены",
        default="UV_Wall",
    )
    create_inner: bpy.props.BoolProperty(
        name="Внутренняя стена",
        description="Вторая параллельная стена. Нужна для режима крышки «Между стенами»",
        default=True,
    )
    inset_distance: bpy.props.FloatProperty(
        name="Отступ",
        description="Расстояние между внешней и внутренней стеной (толщина)",
        default=0.5,
        min=0.001,
        
    )
    inner_wall_height_delta: bpy.props.FloatProperty(
        name="Legacy secondary height (inactive)", default=0.0, precision=4, options={'HIDDEN'})
    inner_wall_height_overrides_json: bpy.props.StringProperty(
        name="Высоты точек дополнительной стены (старый режим)", default="{}", options={"HIDDEN"})
    secondary_points_json: bpy.props.StringProperty(
        name="Точки Offset-пути XY", default="{}", options={"HIDDEN"})
    inset_side: bpy.props.EnumProperty(
        name="Сторона",
        description="В какую сторону от пути сдвигать внутреннюю стену (по направлению рёбер)",
        items=(
            ("LEFT", "Влево", "Смещение влево по направлению пути"),
            ("RIGHT", "Вправо", "Смещение вправо по направлению пути"),
        ),
        default="LEFT",
    )
    close_ends: bpy.props.BoolProperty(
        name="Закрывать торцы открытого пути",
        description="Общий выключатель торцевых заглушек. Отдельные переключатели ниже",
        default=True,
    )
    close_start_end: bpy.props.BoolProperty(name="Начальный торец", default=True)
    close_finish_end: bpy.props.BoolProperty(name="Конечный торец", default=True)
    keep_inner_path: bpy.props.BoolProperty(
        name="Оставить гайд пути",
        description="Сохранить сгенерированный внутренний путь как wire-объект (без рендера)",
        default=False,
    )
    create_top_cap: bpy.props.BoolProperty(
        name="Крышка сверху",
        description="Горизонтальная UV-крышка наверху стены",
        default=True,
    )
    cap_material_override: bpy.props.BoolProperty(
        name="Отдельный материал крышки",
        description="Назначить верхней крышке отдельный проверенный материал текущего 2K/4K стандарта",
        default=False,
    )
    cap_material: bpy.props.PointerProperty(
        name="Материал крышки", type=bpy.types.Material,
        description="Материал применяется только к semantic role TOP_CAP",
    )
    create_bottom_cap: bpy.props.BoolProperty(
        name="Крышка снизу",
        description="Нижняя UV-крышка. Нормаль автоматически направляется наружу вниз",
        default=False,
    )
    bottom_cap_material_override: bpy.props.BoolProperty(
        name="Отдельный материал нижней крышки",
        description="Назначить нижней крышке отдельный материал с той же 2K/4K проверкой",
        default=False,
    )
    bottom_cap_material: bpy.props.PointerProperty(
        name="Материал нижней крышки", type=bpy.types.Material,
        description="Материал применяется только к semantic role BOTTOM_CAP",
    )
    top_cap_mode: bpy.props.EnumProperty(
        name="Режим крышки/пола",
        description="Как строится верхняя крышка и нижний пол",
        items=(
            (
                "BETWEEN",
                "Между стенами",
                "Лента между внешней и внутренней стеной. UV по сетке мира (с поворотом). Нужна внутренняя стена",
            ),
            (
                "OUTER_ONLY",
                "Полная площадь",
                "Заполняет всю площадь внутри ЗАМКНУТОГО пути. Путь должен быть замкнут",
            ),
        ),
        default="BETWEEN",
    )
    wall_offset: bpy.props.FloatProperty(name="Сдвиг пути стены", default=0.0, )
    wall_alignment: bpy.props.EnumProperty(
        name="Расположение стены",
        items=(("INSIDE", "Внутрь", "По внутренней стороне контура"),
               ("CENTER", "Центр", "Конструкция симметрична относительно пути"),
               ("OUTSIDE", "Наружу", "По наружной стороне контура"),
               ("LEGACY", "Исходное (старый проект)", "Сохранить поведение старой версии"),
               ("LEFT", "Слева (совместимость)", "Старое размещение слева"),
               ("RIGHT", "Справа (совместимость)", "Старое размещение справа")),default="LEGACY")
    wall_uv_origin_mode: bpy.props.EnumProperty(
        name="Вертикальная UV-фаза",
        items=(("PATH", "От пути", "Ровный путь: Z пути; UP: минимальный Z; DOWN: максимальный Z"),
               ("WORLD", "По мировому Z", "Общий UV-тайлинг на всех этажах и объектах")),
        default="PATH",
    )
    wall_module_rotation: bpy.props.FloatProperty(name="Поворот модулей стены", default=0.0, min=-180.0, max=180.0, precision=1)
    surface_module_rotation: bpy.props.FloatProperty(name="Поворот модулей поверхности", default=0.0, min=-180.0, max=180.0, precision=1)
    surface_height_offset: bpy.props.FloatProperty(name="Смещение поверхности по Z", default=0.0, )
    create_inner_surface: bpy.props.BoolProperty(name="Заполнить клумбу", default=False)
    inner_surface_offset_auto: bpy.props.BoolProperty(name="Авто: по внутренней стене", default=True)
    inner_surface_offset: bpy.props.FloatProperty(name="Дополнительное смещение внутрь", default=0.0, )
    inner_surface_height_offset: bpy.props.FloatProperty(name="Отступ по высоте", default=-0.05, )
    inner_surface_material: bpy.props.PointerProperty(name="Материал грунта/травы", type=bpy.types.Material)
    cornice_profile_rotation: bpy.props.FloatProperty(name="Поворот профиля", default=0.0, min=-180.0, max=180.0)
    cornice_profile_flip_x: bpy.props.BoolProperty(name="Flip X", default=False)
    cornice_profile_flip_y: bpy.props.BoolProperty(name="Flip Z", default=False)
    cornice_reverse_path: bpy.props.BoolProperty(name="Reverse Path", default=False)
    viewport_hover_color: bpy.props.FloatVectorProperty(
        name="Цвет наведения", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(1.0, 0.20, 0.17),
        description="Цвет semantic highlight и hover-маркеров во Viewport",
    )

    cap_rotation: bpy.props.FloatProperty(
        name="Поворот UV крышки",
        description="Угол поворота UV-сетки на крышке и полу (градусы). 0 = по осям мира",
        default=0.0,
        min=-180.0,
        max=180.0,
        step=100,
        precision=1,
    )


