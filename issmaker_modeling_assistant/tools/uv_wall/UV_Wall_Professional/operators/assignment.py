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
from ..core.material_intelligence import (
    material_image_candidates, inspect_material_asset, set_active_definition_asset,
)
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

class OBJECT_OT_uv_wall_use_active_as_path(bpy.types.Operator):
    bl_idname = "object.uv_wall_use_active_as_path"
    bl_label = "Активный → Путь"
    bl_description = "Назначить активный (выделенный) объект как Path Mesh"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and context.active_object is not None
            and context.active_object.type == "MESH"
            and hasattr(context.scene, "uv_wall_settings")
        )

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.uv_wall_settings
        if obj in {settings.source_plane, settings.cornice_profile}:
            self.report({"WARNING"}, "Путь должен быть отдельным объектом")
            return {"CANCELLED"}
        if len(obj.data.edges) == 0 or len(obj.data.polygons) != 0:
            self.report({"ERROR"}, "Путь должен быть отдельной Mesh-линией только из рёбер, без полигонов")
            return {"CANCELLED"}
        settings.path_mesh = obj
        self.report({"INFO"}, f"Путь: {obj.name}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_use_active_as_source(bpy.types.Operator):
    bl_idname = "object.uv_wall_use_active_as_source"
    bl_label = "Активный → Плейн"
    bl_description = "Назначить активный (выделенный) объект как Source Plane"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and context.active_object is not None
            and context.active_object.type == "MESH"
            and hasattr(context.scene, "uv_wall_settings")
        )

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.uv_wall_settings
        if obj in {settings.path_mesh, settings.cornice_profile}:
            self.report({"WARNING"}, "Эталон, путь и профиль должны быть разными объектами")
            return {"CANCELLED"}
        if not is_valid_source_reference(obj):
            self.report({"ERROR"}, "Эталон должен быть одним quad с UV, материалом и Image Texture")
            return {"CANCELLED"}
        settings.source_plane = obj
        self.report({"INFO"}, f"Source Plane: {obj.name}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_assign_from_selection(bpy.types.Operator):
    bl_idname = "object.uv_wall_assign_from_selection"
    bl_label = "Из выделения"
    bl_description = (
        "Умное назначение из выделения: "
        "1 объект — угадать (один квад → Source, иначе → Path); "
        "2 объекта — активный = Source, второй = Path"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT" or not hasattr(context.scene, "uv_wall_settings"):
            return False
        selected = [obj for obj in context.selected_objects if obj.type == "MESH"]
        return len(selected) >= 1

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        settings.tile_length = float(settings.module_standard)
        selected = [obj for obj in context.selected_objects if obj.type == "MESH"]
        active = context.active_object if context.active_object and context.active_object.type == "MESH" else None
        if len(selected) > 2:
            self.report({"ERROR"}, "Оставьте выделенными только два объекта: путь и UV-эталон")
            return {"CANCELLED"}

        def looks_like_source(obj):
            return is_valid_source_reference(obj)

        def looks_like_path(obj):
            return len(obj.data.edges) > 0 and len(obj.data.polygons) == 0

        if len(selected) == 1:
            obj = selected[0]
            if looks_like_source(obj):
                settings.source_plane = obj
                self.report({"INFO"}, f"Source Plane ← {obj.name}")
            elif looks_like_path(obj):
                settings.path_mesh = obj
                self.report({"INFO"}, f"Path ← {obj.name}")
            else:
                self.report({"ERROR"}, "Объект не является ни UV-эталоном, ни Mesh-линией без полигонов")
                return {"CANCELLED"}
            return {"FINISHED"}

        source_candidate = next((obj for obj in selected if looks_like_source(obj)), None)
        path_candidate = next(
            (obj for obj in selected if obj != source_candidate and looks_like_path(obj)),
            None,
        )
        if source_candidate is None or path_candidate is None:
            self.report({"ERROR"}, "Нужны два разных объекта: Mesh-линия без полигонов и UV-эталон quad")
            return {"CANCELLED"}
        settings.source_plane = source_candidate
        settings.path_mesh = path_candidate
        self.report({"INFO"}, f"Source ← {source_candidate.name}, Path ← {path_candidate.name}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_create_reference(bpy.types.Operator):
    bl_idname = "object.uv_wall_create_reference"
    bl_label = "Создать UV-эталон"
    bl_description = "Создать квадратный UV-эталон 2×2 или 4×4 м с плотностью 1000 px/m"
    bl_options = {"REGISTER", "UNDO"}

    module_size: bpy.props.FloatProperty(default=4.0, options={"HIDDEN"})
    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.uv_wall_settings
        size = 2 if self.module_size < 3.0 else 4
        layout.label(text=f"Эталон {size}×{size} м — 1000 px/m", icon="UV")
        layout.prop(settings, "reference_material", text="Материал")
        layout.label(text="Текстура определяется автоматически материалом (2K / 4K)", icon="INFO")

    def execute(self, context):
        from ..core.material_intelligence import material_module
        settings = context.scene.uv_wall_settings
        material = settings.reference_material
        module, image, reason = material_module(material)
        if module is None:
            self.report({"ERROR"}, reason)
            return {"CANCELLED"}
        # Resolution is determined by Material; no independent selector or
        # temporary mutation of the user's shader graph is allowed.
        size = float(module)
        half = size * 0.5
        cursor = context.scene.cursor.location.copy()
        vertices = [
            (cursor.x - half, cursor.y, cursor.z - half),
            (cursor.x + half, cursor.y, cursor.z - half),
            (cursor.x + half, cursor.y, cursor.z + half),
            (cursor.x - half, cursor.y, cursor.z + half),
        ]
        mesh = bpy.data.meshes.new(f"UV_Reference_{int(size)}m_Mesh")
        mesh.from_pydata(vertices, [], [(0, 1, 2, 3)])
        mesh.update()
        uv_layer = mesh.uv_layers.new(name="UVMap")
        margin = 0.01171875
        for loop_index, uv in zip(mesh.polygons[0].loop_indices,
                ((margin, margin), (1.0-margin, margin),
                 (1.0-margin, 1.0-margin), (margin, 1.0-margin))):
            uv_layer.data[loop_index].uv = uv
        obj = bpy.data.objects.new(f"UV_Reference_{int(size)}x{int(size)}m", mesh)
        context.collection.objects.link(obj)
        mesh.materials.append(material)
        for selected in context.selected_objects:
            selected.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        settings.source_plane = obj
        settings.module_standard = module
        settings.tile_length = size
        obj["uv_wall_reference"] = True
        obj["module_size"] = size
        obj["texel_density"] = 1000
        log_event(f"Создан эталон {size:.0f}×{size:.0f} м по материалу; 1000 px/m")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_load_texture(bpy.types.Operator):
    bl_idname = "object.uv_wall_load_texture"
    bl_label = "Открыть текстуру"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.tga;*.bmp", options={"HIDDEN"})

    def invoke(self, context, _event):
        self.report({"INFO"}, "Назначьте текстуру внутри материала Blender; отдельный выбор отключён")
        return {"CANCELLED"}

    def execute(self, context):
        self.report({"INFO"}, "Текстура берётся из Base Color материала")
        return {"CANCELLED"}


class OBJECT_OT_uv_wall_use_active_as_profile(bpy.types.Operator):
    bl_idname = "object.uv_wall_use_active_as_profile"
    bl_label = "Активный → Профиль"
    bl_description = "Назначить активный Mesh как открытый или замкнутый профиль карниза"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        obj = context.active_object
        if obj in {settings.path_mesh, settings.source_plane}:
            self.report({"ERROR"}, "Путь, эталон и профиль должны быть разными объектами")
            return {"CANCELLED"}
        if len(obj.data.edges) == 0 or len(obj.data.polygons) != 0:
            self.report({"ERROR"}, "Профиль должен быть отдельной Mesh-линией из рёбер, без полигонов")
            return {"CANCELLED"}
        settings.cornice_profile = obj
        self.report({"INFO"}, f"Профиль карниза: {obj.name}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_add_cornice_corners(bpy.types.Operator):
    bl_idname = "object.uv_wall_add_cornice_corners"
    bl_label = "Добавить выделенные углы"
    bl_description = "Сохранить выделенные вершины Path как authored Sharp-углы карниза"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uv_wall_settings", None)
        return settings is not None and settings.path_mesh is not None

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        path = settings.path_mesh
        if context.active_object != path or path.mode != "EDIT":
            self.report({"ERROR"}, "Выберите путь, войдите в Edit Mode и выделите его вершины")
            return {"CANCELLED"}
        mapping = sync_corner_uids(path)
        bm = bmesh.from_edit_mesh(path.data)
        layer = bm.verts.layers.int.get(CORNER_UID_ATTRIBUTE)
        selected_uids = [
            int(vertex[layer]) for vertex in bm.verts
            if vertex.select and len(vertex.link_edges) == 2
        ]
        if not selected_uids:
            self.report({"ERROR"}, "Выделите вершину пути, соединённую ровно с двумя рёбрами")
            return {"CANCELLED"}
        existing = {marker.vertex_uid for marker in settings.cornice_corners}
        added = 0
        for uid in selected_uids:
            if uid in existing or uid not in mapping:
                continue
            marker = settings.cornice_corners.add()
            marker.vertex_uid = uid
            marker.cached_index = mapping[uid][0]
            marker.path_name = path.name
            marker.corner_type = "SHARP"
            existing.add(uid)
            added += 1
        validated_cornice_corner_points(settings, path, remove_stale=True)
        sync_definition_from_scene(path, settings)
        self.report({"INFO"}, f"Добавлено Sharp-углов: {added}; всего: {len(settings.cornice_corners)}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_mark_cornice_smooth(bpy.types.Operator):
    bl_idname = "object.uv_wall_mark_cornice_smooth"
    bl_label = "Сделать выделенные плавными"
    bl_description = "Убрать Sharp-семантику у выделенных вершин пути"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "uv_wall_settings", None)
        return settings is not None and settings.path_mesh is not None

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        path = settings.path_mesh
        if context.active_object != path or path.mode != "EDIT":
            self.report({"ERROR"}, "Выберите путь, войдите в Edit Mode и выделите вершины")
            return {"CANCELLED"}
        mapping = sync_corner_uids(path)
        bm = bmesh.from_edit_mesh(path.data)
        layer = bm.verts.layers.int.get(CORNER_UID_ATTRIBUTE)
        selected = {int(v[layer]) for v in bm.verts if v.select and len(v.link_edges) == 2}
        if not selected:
            self.report({"ERROR"}, "Выделите вершины углов пути")
            return {"CANCELLED"}
        removed = 0
        for index in range(len(settings.cornice_corners)-1, -1, -1):
            if int(settings.cornice_corners[index].vertex_uid) in selected:
                settings.cornice_corners.remove(index)
                removed += 1
        validated_cornice_corner_points(settings, path, remove_stale=True)
        sync_definition_from_scene(path, settings)
        self.report({"INFO"}, f"Плавными отмечено: {len(selected)}; Sharp снято: {removed}")
        return {"FINISHED"}


class OBJECT_OT_uv_wall_use_active_material(bpy.types.Operator):
    bl_idname = "object.uv_wall_use_active_material"
    bl_label = "Материал из выделенного"
    bl_description = "Взять активный Material выделенного объекта для текущего 2K/4K модуля"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(getattr(context, "active_object", None)) and hasattr(context.scene, "uv_wall_settings")

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        path = settings.path_mesh
        definition = definition_for(path) if path is not None else None
        if definition is None:
            self.report({"ERROR"}, "Сначала назначьте Path / Definition")
            return {"CANCELLED"}
        material = getattr(context.active_object, "active_material", None)
        if material is None:
            self.report({"ERROR"}, "У выделенного объекта нет активного материала")
            return {"CANCELLED"}
        from ..core.material_intelligence import material_module
        module, image, reason = material_module(material)
        if module is None:
            self.report({"ERROR"}, reason)
            return {"CANCELLED"}
        if not set_active_definition_asset(definition, material=material):
            self.report({"ERROR"}, reason)
            return {"CANCELLED"}
        sync_scene_from_definition(path, settings)
        state = inspect_material_asset(virtual_asset_for_module(definition)[0], None, definition.module_standard)
        if state["status"] == "VALID":
            self.report({"INFO"}, f"Материал {material.name}: {state['message']}")
        else:
            self.report({"WARNING"}, state["message"])
        return {"FINISHED"}


class OBJECT_OT_uv_wall_use_material_image(bpy.types.Operator):
    bl_idname = "object.uv_wall_use_material_image"
    bl_label = "Текстура из материала"
    bl_description = "Если у текущего материала одна Image Texture — назначить её текущему 2K/4K модулю"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        self.report({"INFO"}, "Текстура определяется автоматически материалом — отдельное назначение отключено")
        return {"CANCELLED"}


class OBJECT_OT_uv_wall_remove_cornice_corner(bpy.types.Operator):
    bl_idname = "object.uv_wall_remove_cornice_corner"
    bl_label = "Удалить угол"
    bl_options = {"REGISTER", "UNDO"}
    marker_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        corners = context.scene.uv_wall_settings.cornice_corners
        if 0 <= self.marker_index < len(corners):
            corners.remove(self.marker_index)
            settings = context.scene.uv_wall_settings
            if settings.path_mesh is not None:
                sync_definition_from_scene(settings.path_mesh, settings)
            return {"FINISHED"}
        return {"CANCELLED"}


class OBJECT_OT_uv_wall_clear_cornice_corners(bpy.types.Operator):
    bl_idname = "object.uv_wall_clear_cornice_corners"
    bl_label = "Очистить углы"
    bl_description = "Удалить все пользовательские углы текущего пути"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.uv_wall_settings
        settings.cornice_corners.clear()
        if settings.path_mesh is not None:
            sync_definition_from_scene(settings.path_mesh, settings)
        return {"FINISHED"}


def draw_cornice_corner_controls(layout, settings):
    box = layout.box()
    box.label(text="Острые углы карниза", icon="VERTEXSEL")
    auto_candidates = sum(
        1 for problem in getattr(settings, "problems", ())
        if str(getattr(problem, "subsystem", "")) == "CORNICE_UV"
        and getattr(problem, "element_kind", "") == "CORNICE_VERTEX"
    )
    box.label(text=f"Authored Sharp: {len(settings.cornice_corners)} · Auto candidates: {auto_candidates}")
    box.label(text="Edit Mode: выделите вершины пути", icon="INFO")
    row = box.row(align=True)
    row.operator(
        OBJECT_OT_uv_wall_add_cornice_corners.bl_idname,
        text="Сделать острыми",
        icon="ADD",
    )
    row.operator(
        OBJECT_OT_uv_wall_mark_cornice_smooth.bl_idname,
        text="Сделать плавными",
        icon="IPO_EASE_IN_OUT",
    )
    row = box.row(align=True)
    row.operator(
        OBJECT_OT_uv_wall_clear_cornice_corners.bl_idname,
        text="Сбросить все Sharp",
        icon="TRASH",
    )
    if not settings.cornice_corners:
        box.label(text="Sharp-углы не назначены", icon="INFO")
    else:
        for index, marker in enumerate(settings.cornice_corners):
            row = box.row(align=True)
            row.label(text=f"Sharp · вершина #{marker.cached_index}", icon="VERTEXSEL")
            operator = row.operator(
                OBJECT_OT_uv_wall_remove_cornice_corner.bl_idname,
                text="",
                icon="X",
            )
            operator.marker_index = index


