import json
import uuid
import bpy
from mathutils import Vector

CURRENT_SCHEMA_VERSION = 5

GENERATOR_ITEMS = (
    ("NONE", "Не назначен", "Путь пока не имеет параметрического намерения"),
    ("WALL", "Стена", "Параметрическая UV-стена"),
    ("SURFACE", "Поверхность", "Параметрическая поверхность по замкнутому пути"),
    ("CORNICE", "Карниз", "Параметрический карниз вдоль пути"),
)


class UVWALL_PG_path_definition(bpy.types.PropertyGroup):
    """Persistent source-of-truth stored on the authored path object.

    Generated geometry is deliberately not authoritative.  This definition is
    enough to restore the viewport session and regenerate a production result.
    """

    initialized: bpy.props.BoolProperty(default=False, options={"HIDDEN"})
    schema_version: bpy.props.IntProperty(default=CURRENT_SCHEMA_VERSION, options={"HIDDEN"})
    revision: bpy.props.IntProperty(default=0, options={"HIDDEN"})
    source_uid: bpy.props.StringProperty(default="", options={"HIDDEN"})
    last_committed_revision: bpy.props.IntProperty(default=-1, options={"HIDDEN"})
    generator_type: bpy.props.EnumProperty(name="Тип", items=GENERATOR_ITEMS, default="NONE")
    output_name: bpy.props.StringProperty(name="Имя результата", default="UV_Wall")

    module_standard: bpy.props.EnumProperty(
        name="UV-модуль",
        items=(("2", "2×2 м / 2K", "Виртуальный модуль 2×2 м"), ("4", "4×4 м / 4K", "Виртуальный модуль 4×4 м")),
        default="4",
    )
    use_virtual_reference: bpy.props.BoolProperty(
        name="Виртуальный UV-эталон",
        description="Использовать внутренний модуль без Plane-объекта в сцене",
        default=True,
    )
    # Legacy shared asset kept only for migration from NEXTGEN schema v1/v2.
    # New work uses independent 2K and 4K assets so switching module size can
    # never silently reuse a 4K reference for a 2×2 module (or vice versa).
    virtual_material: bpy.props.PointerProperty(name="Материал (legacy)", type=bpy.types.Material, options={"HIDDEN"})
    virtual_image: bpy.props.PointerProperty(name="Текстура (legacy)", type=bpy.types.Image, options={"HIDDEN"})
    virtual_material_2k: bpy.props.PointerProperty(name="Материал 2×2 / 2K", type=bpy.types.Material)
    virtual_image_2k: bpy.props.PointerProperty(name="Устаревшее изображение 2K", type=bpy.types.Image, options={"HIDDEN"})
    virtual_material_4k: bpy.props.PointerProperty(name="Материал 4×4 / 4K", type=bpy.types.Material)
    virtual_image_4k: bpy.props.PointerProperty(name="Устаревшее изображение 4K", type=bpy.types.Image, options={"HIDDEN"})

    # Wall
    stories: bpy.props.IntProperty(name="Модулей по высоте", default=1, min=1, max=256)
    custom_first_story: bpy.props.BoolProperty(default=False)
    first_story_height: bpy.props.FloatProperty(name="Высота первого", default=0.0, min=0.0, )
    story_direction: bpy.props.EnumProperty(
        name="Направление",
        items=(("DOWN", "Вниз", "Путь является верхом стены"), ("UP", "Вверх", "Путь является низом стены")),
        default="DOWN",
    )
    align_bottom: bpy.props.BoolProperty(default=False)
    bottom_extend: bpy.props.FloatProperty(default=0.0, min=0.0, )
    create_inner: bpy.props.BoolProperty(name="Толщина", default=True)
    inset_distance: bpy.props.FloatProperty(name="Толщина", default=0.5, min=0.001, )
    inner_wall_height_delta: bpy.props.FloatProperty(
        name="Внутренняя высота Δ", description="Изменение высоты внутренней стены относительно внешней: + выше, − ниже; общий низ сохранён", default=0.0, precision=4)
    inner_wall_height_overrides_json: bpy.props.StringProperty(
        name="Высоты точек дополнительной стены (старый режим)", default="{}", options={"HIDDEN"})
    secondary_points_json: bpy.props.StringProperty(
        name="Точки дополнительной стены XY/Z/H", default="{}", options={"HIDDEN"})
    wall_offset: bpy.props.FloatProperty(name="Смещение стены", default=0.0, )
    wall_alignment: bpy.props.EnumProperty(
        name="Расположение стены", description="Относительно исходной линии пути",
        items=(("INSIDE", "Внутрь", "Вся конструкция внутрь замкнутого контура"),
               ("CENTER", "Центр", "Равномерно относительно пути"),
               ("OUTSIDE", "Наружу", "Вся конструкция наружу замкнутого контура"),
               ("LEGACY", "Исходное (старый проект)", "Без изменений для старых проектов"),
               ("LEFT", "Слева (совместимость)", "Старое размещение слева"),
               ("RIGHT", "Справа (совместимость)", "Старое размещение справа")),
        default="LEGACY",
    )
    wall_uv_origin_mode: bpy.props.EnumProperty(
        name="Фаза UV по высоте",
        items=(("PATH", "От пути", "Высотный экстремум исходного 3D пути для модульной сетки"),
               ("WORLD", "От мирового Z", "Один рисунок фасада через отдельно построенные этажи")),
        default="PATH",
    )
    wall_module_rotation: bpy.props.FloatProperty(name="Поворот модулей стены", default=0.0, min=-180.0, max=180.0, precision=1)
    create_inner_surface: bpy.props.BoolProperty(name="Внутренняя поверхность", default=False)
    inner_surface_offset_auto: bpy.props.BoolProperty(name="Автоматический отступ", default=True)
    inner_surface_offset: bpy.props.FloatProperty(name="Дополнительный отступ", default=0.0, )
    inner_surface_height_offset: bpy.props.FloatProperty(name="Высота поверхности относительно верха", default=-0.05, )
    inner_surface_material: bpy.props.PointerProperty(name="Материал внутренней поверхности", type=bpy.types.Material)
    last_inner_surface_name: bpy.props.StringProperty(default="", options={"HIDDEN"})
    inset_side: bpy.props.EnumProperty(
        name="Сторона",
        items=(("LEFT", "Влево", "Внутренняя стенка слева"), ("RIGHT", "Вправо", "Внутренняя стенка справа")),
        default="LEFT",
    )
    close_ends: bpy.props.BoolProperty(default=True)
    close_start_end: bpy.props.BoolProperty(name="Начальный торец", default=True)
    close_finish_end: bpy.props.BoolProperty(name="Конечный торец", default=True)
    keep_inner_path: bpy.props.BoolProperty(default=False)
    create_top_cap: bpy.props.BoolProperty(default=True)
    cap_material_override: bpy.props.BoolProperty(name="Отдельный материал крышки", default=False)
    cap_material: bpy.props.PointerProperty(name="Материал верхней крышки", type=bpy.types.Material)
    create_bottom_cap: bpy.props.BoolProperty(default=False)
    bottom_cap_material_override: bpy.props.BoolProperty(name="Отдельный материал нижней крышки", default=False)
    bottom_cap_material: bpy.props.PointerProperty(name="Материал нижней крышки", type=bpy.types.Material)
    wall_normal_mode: bpy.props.EnumProperty(
        name="Нормали стены",
        items=(
            ("OUTWARD", "Наружу", "Автоматически наружу"),
            ("INWARD", "Внутрь", "Автоматически внутрь"),
            ("PATH_LEFT", "Влево", "Относительно направления пути"),
            ("PATH_RIGHT", "Вправо", "Относительно направления пути"),
        ),
        default="OUTWARD",
    )

    # Surface: semantic world-Z overrides are recorded under mesh vertex UIDs.
    # Actual authored height is the source mesh coordinate, not this metadata.
    surface_heights_json: bpy.props.StringProperty(default="{}", options={"HIDDEN"})
    roof_normal: bpy.props.EnumProperty(
        name="Нормали поверхности",
        items=(("UP", "Вверх", "+Z"), ("DOWN", "Вниз", "-Z")),
        default="UP",
    )
    # Do not remove TRIANGULATED RNA ID: older .blend files must preserve their surface.
    surface_method: bpy.props.EnumProperty(
        name="Поверхность",
        items=(
            ("SMOOTH_LEGACY", "Плавный ландшафт", "Модульная плавная поверхность"),
            ("TRIANGULATED", "Адаптивная триангуляция", "Треугольная опорная сетка"),
        ),
        default="SMOOTH_LEGACY",
    )
    cap_rotation: bpy.props.FloatProperty(name="Поворот UV крышки", default=0.0, min=-180.0, max=180.0, precision=1)
    surface_module_rotation: bpy.props.FloatProperty(name="Поворот модулей поверхности", default=0.0, min=-180.0, max=180.0, precision=1)
    surface_height_offset: bpy.props.FloatProperty(name="Поверхность Z", default=0.0, )

    # Cornice
    cornice_profile: bpy.props.PointerProperty(name="Профиль", type=bpy.types.Object)
    cornice_flip_side: bpy.props.BoolProperty(default=False)
    cornice_profile_rotation: bpy.props.FloatProperty(name="Поворот профиля", default=0.0, min=-180.0, max=180.0)
    cornice_profile_flip_x: bpy.props.BoolProperty(name="Инверсия X", default=False)
    cornice_profile_flip_y: bpy.props.BoolProperty(name="Инверсия Z", default=False)
    cornice_reverse_path: bpy.props.BoolProperty(name="Обратный путь", default=False)
    cornice_uv_mode: bpy.props.EnumProperty(
        name="UV карниза",
        items=(
            ("MAX_REAL_WORLD", "Плавные углы", "Непрерывный real-world UV"),
            ("MAX_CLIPPED", "Острые углы · Sharp", "Подрезка в authored углах"),
        ),
        default="MAX_REAL_WORLD",
    )
    cornice_corner_mode: bpy.props.EnumProperty(
        name="Путь карниза",
        items=(
            ("AUTO", "Авто углы", "Production AUTO: miter when safe, local fallback on extreme corners"),
            ("AUTHORED", "Как нарисовано", "Authored path"),
            ("ADAPTIVE", "Адаптивно", "Adaptive path refinement"),
            ("DENSE_LINEAR", "Добавить точки", "Dense linear"),
        ),
        default="AUTO",
    )
    cornice_linear_step: bpy.props.FloatProperty(default=0.25, min=0.002, )
    cornice_smooth_warning_angle: bpy.props.FloatProperty(name="Порог острого угла", default=45.0, min=1.0, max=179.0)
    corner_uid_json: bpy.props.StringProperty(default="[]", options={"HIDDEN"})
    corner_methods_json: bpy.props.StringProperty(default="{}", options={"HIDDEN"})

    # Lightweight status persisted for recovery/debug.  Never source geometry from this.
    last_result_name: bpy.props.StringProperty(default="", options={"HIDDEN"})
    last_error: bpy.props.StringProperty(default="", options={"HIDDEN"})
    last_successful_settings_json: bpy.props.StringProperty(default="", options={"HIDDEN"})



SNAPSHOT_FIELDS = (
    "generator_type", "output_name", "module_standard", "use_virtual_reference",
    "virtual_material", "virtual_image",
    "virtual_material_2k", "virtual_image_2k", "virtual_material_4k", "virtual_image_4k",
    "stories", "custom_first_story", "first_story_height", "story_direction",
    "align_bottom", "bottom_extend", "create_inner", "inset_distance", "inner_wall_height_delta", "inner_wall_height_overrides_json", "secondary_points_json", "wall_offset", "wall_alignment", "inset_side",
    "close_ends", "close_start_end", "close_finish_end", "keep_inner_path", "create_top_cap", "cap_material_override", "cap_material", "create_bottom_cap", "bottom_cap_material_override", "bottom_cap_material",
    "wall_module_rotation", "wall_uv_origin_mode", "create_inner_surface", "inner_surface_offset_auto",
    "inner_surface_offset", "inner_surface_height_offset", "inner_surface_material", "last_inner_surface_name",
    "wall_normal_mode", "surface_heights_json", "roof_normal", "surface_method", "cap_rotation",
    "surface_module_rotation", "surface_height_offset",
    "cornice_profile", "cornice_flip_side", "cornice_uv_mode",
    "cornice_corner_mode", "cornice_linear_step", "cornice_smooth_warning_angle", "corner_uid_json", "corner_methods_json",
    "cornice_profile_rotation", "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
    "last_result_name", "last_error", "last_committed_revision",
    "last_successful_settings_json",
)


def _result_matches_path_identity(result_obj, path_obj, source_uid):
    """Return True only when a generated Result semantically belongs to this Path.

    Blender object names are merely a migration hint.  A duplicated Path can copy
    Definition RNA including ``source_uid`` and ``last_result_name``; accepting that
    copied name used to let the duplicate steal/reuse the original wall Result.
    """
    if result_obj is None or path_obj is None:
        return False
    try:
        if not result_obj.get("uvwall_generated"):
            return False
        owner_uid = str(result_obj.get("uvwall_owner_uid") or "")
        if source_uid and owner_uid not in {"", source_uid}:
            return False
        # UID is authoritative while it is scene-unique, so a harmless Path
        # rename does not orphan its Stable Result.  If Blender duplicated the
        # Path and copied the UID, fall back to the committed owner-name hint
        # and ultimately fail closed when that hint is itself ambiguous.
        if source_uid and owner_uid == source_uid:
            definition = definition_for(path_obj)
            if not source_uid_collisions(path_obj, definition):
                return True
        owner_name = result_obj.get("uvwall_owner_path") or result_obj.get("path_object")
        return owner_name == path_obj.name
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return False


def source_uid_collisions(path_obj, definition=None):
    """List other authored Paths that currently carry the same Definition UID."""
    definition = definition or definition_for(path_obj)
    if path_obj is None or definition is None or not getattr(definition, "source_uid", ""):
        return []
    uid = definition.source_uid
    collisions = []
    for obj in bpy.data.objects:
        if obj is path_obj or getattr(obj, "type", None) != "MESH":
            continue
        try:
            other = definition_for(obj)
            if other is not None and getattr(other, "initialized", False) and other.source_uid == uid:
                collisions.append(obj)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue
    return collisions


def _path_has_own_committed_result(path_obj, definition, source_uid):
    """Disambiguate a real owner from a Shift+D clone with copied metadata."""
    if path_obj is None or definition is None:
        return False
    last_name = getattr(definition, "last_result_name", "")
    if last_name:
        result = bpy.data.objects.get(last_name)
        if _result_matches_path_identity(result, path_obj, source_uid):
            return True
    for result in bpy.data.objects:
        try:
            if result.get("uvwall_generated") and result.get("uvwall_owner_uid") == source_uid:
                if _result_matches_path_identity(result, path_obj, source_uid):
                    return True
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue
    return False


def _generated_results_for_uid(source_uid):
    if not source_uid:
        return []
    results = []
    for obj in bpy.data.objects:
        try:
            if obj.get("uvwall_generated") and obj.get("uvwall_owner_uid") == source_uid:
                if not obj.get("uvwall_detached") and not obj.get("uvwall_baked"):
                    results.append(obj)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue
    return results


def unresolved_source_uid_collision(path_obj, definition=None):
    """Return colliding Paths when automatic clone repair would be unsafe.

    A renamed original Path followed by duplication is the hard case: both Paths
    can carry one copied UID while the committed Result still stores the old Path
    name.  Guessing which Path owns that Result can make the second Build steal the
    first wall.  1.6.1 therefore fails closed whenever a Result exists but neither
    Path can prove ownership.
    """
    definition = definition or definition_for(path_obj)
    if path_obj is None or definition is None:
        return []
    uid = str(getattr(definition, "source_uid", "") or "")
    collisions = source_uid_collisions(path_obj, definition)
    if not uid or not collisions or _path_has_own_committed_result(path_obj, definition, uid):
        return []
    proven_others = []
    for other in collisions:
        other_definition = definition_for(other)
        if other_definition is not None and _path_has_own_committed_result(other, other_definition, uid):
            proven_others.append(other)
    if len(proven_others) == 1:
        return []
    return collisions if _generated_results_for_uid(uid) else []


def ensure_source_uid(path_obj, definition=None):
    """Return a stable, scene-unique semantic Definition ID for an authored Path.

    Blender duplication copies PropertyGroup values including ``source_uid`` and
    ``last_result_name``.  A normal clone can be separated automatically when the
    original Path still proves ownership of its committed Result.  When ownership
    is ambiguous (for example rename → duplicate while an old Result keeps the
    previous Path name), 1.6.1 deliberately keeps the collision unresolved so the
    production Build can fail closed instead of guessing and stealing a Result.
    """
    definition = definition or definition_for(path_obj)
    if definition is None:
        return ""
    if not definition.source_uid:
        definition.source_uid = uuid.uuid4().hex
    uid = definition.source_uid
    collisions = source_uid_collisions(path_obj, definition)
    if collisions and not _path_has_own_committed_result(path_obj, definition, uid):
        proven_others = []
        for other in collisions:
            other_definition = definition_for(other)
            if other_definition is not None and _path_has_own_committed_result(other, other_definition, uid):
                proven_others.append(other)
        has_existing_result = bool(_generated_results_for_uid(uid))
        # Safe clone: one other Path proves ownership.  Also safe when no Result
        # exists yet, because no authored result can be stolen by assigning fresh ID.
        if len(proven_others) == 1 or not has_existing_result:
            definition.source_uid = uuid.uuid4().hex
            definition.last_result_name = ""
            try:
                definition.last_inner_surface_name = ""
            except Exception:
                pass
            try:
                definition.last_committed_revision = -1
            except Exception:
                pass
            uid = definition.source_uid
    try:
        path_obj["uvwall_definition_uid"] = uid
    except Exception:
        pass
    return uid


def definition_uid(path_obj, definition=None):
    """Public semantic name for the legacy ``source_uid`` storage field."""
    return ensure_source_uid(path_obj, definition)

def path_from_source_uid(source_uid):
    if not source_uid:
        return None
    matches = []
    for obj in bpy.data.objects:
        if getattr(obj, "type", None) != "MESH":
            continue
        definition = definition_for(obj)
        if definition is not None and definition.source_uid == source_uid:
            matches.append((obj, definition))
    if len(matches) == 1:
        return matches[0][0]
    # A duplicated Path can temporarily copy the same UID before its first
    # Build. Prefer the one that can prove ownership of an existing Result; if
    # ownership is ambiguous, return None rather than routing a Result to the
    # wrong authored Path.
    owned = [obj for obj, definition in matches
             if _path_has_own_committed_result(obj, definition, source_uid)]
    return owned[0] if len(owned) == 1 else None


def path_for_generated(result_obj):
    if result_obj is None:
        return None
    owner_uid = result_obj.get("uvwall_owner_uid")
    path = path_from_source_uid(owner_uid) if owner_uid else None
    if path is not None:
        return path
    owner_name = result_obj.get("uvwall_owner_path")
    candidate = bpy.data.objects.get(owner_name) if owner_name else None
    return candidate if candidate is not None and candidate.type == "MESH" else None


def snapshot_definition(definition):
    """Capture a lightweight transaction snapshot of user-editable Definition state."""
    if definition is None:
        return None
    snapshot = {"revision": int(definition.revision)}
    for name in SNAPSHOT_FIELDS:
        try:
            snapshot[name] = getattr(definition, name)
        except Exception:
            pass
    return snapshot


def restore_definition(definition, snapshot):
    """Restore a viewport edit transaction without touching authored geometry."""
    if definition is None or not snapshot:
        return
    for name in SNAPSHOT_FIELDS:
        if name in snapshot:
            try:
                setattr(definition, name, snapshot[name])
            except Exception:
                pass
    try:
        definition.revision = int(snapshot.get("revision", definition.revision))
    except Exception:
        pass


def has_committed_result(definition, path_obj=None):
    """Return True only for a committed Result belonging to this Definition.

    A duplicated Path copies ``last_result_name``.  Name-only checks therefore
    made a fresh clone look as if it already owned the original wall, which in
    turn changed failed-edit rollback semantics in the viewport.
    """
    if definition is None or not definition.last_result_name:
        return False
    obj = bpy.data.objects.get(definition.last_result_name)
    if not (obj and obj.get("uvwall_generated")):
        return False
    if path_obj is None:
        owner_uid = str(obj.get("uvwall_owner_uid") or "")
        expected_uid = str(getattr(definition, "source_uid", "") or "")
        return bool(not owner_uid or not expected_uid or owner_uid == expected_uid)
    return _result_matches_path_identity(obj, path_obj, getattr(definition, "source_uid", ""))

RUNTIME_SCHEMA_SIGNATURE = "path_definition_v5_20261016_185_independent_secondary_points"


def _required_rna_properties():
    return (
        "schema_version", "secondary_points_json", "close_start_end", "close_finish_end", "wall_offset", "wall_alignment", "wall_module_rotation", "wall_uv_origin_mode", "surface_module_rotation",
        "create_inner_surface", "inner_surface_offset_auto", "inner_surface_offset",
        "inner_surface_height_offset", "inner_surface_material", "last_inner_surface_name",
        "cap_material_override", "cap_material", "bottom_cap_material_override", "bottom_cap_material",
        "surface_height_offset", "cornice_profile", "cornice_profile_rotation",
        "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
        "corner_uid_json", "corner_methods_json", "source_uid", "last_result_name", "last_committed_revision", "last_successful_settings_json",
        "virtual_material_2k", "virtual_image_2k",
        "virtual_material_4k", "virtual_image_4k",
    )


def _rna_class_is_registered(cls):
    """Return Blender's registration state for the exact Python class object."""
    try:
        return bool(getattr(cls, "is_registered", False))
    except Exception:
        return False


def runtime_schema_status():
    """Validate the exact live RNA class used by this module.

    Do not depend on ``getattr(bpy.types, class_name)`` here. During extension
    installation/reload Blender can temporarily expose a registered Python RNA
    class without publishing that lookup in the way our previous guard assumed.
    The class object's ``is_registered``/``bl_rna`` state is authoritative.
    """
    cls = UVWALL_PG_path_definition
    if not _rna_class_is_registered(cls):
        return False, "Текущий Path Definition RNA-класс не зарегистрирован"
    if not hasattr(bpy.types.Object, "uvwall_definition"):
        return False, "Object.uvwall_definition не зарегистрирован"
    try:
        props = cls.bl_rna.properties
        for required in _required_rna_properties():
            if props.get(required) is None:
                return False, f"Схема Path Definition устарела: отсутствует {required}"
        pointer_prop = bpy.types.Object.bl_rna.properties.get("uvwall_definition")
        if pointer_prop is None:
            return False, "Object.uvwall_definition отсутствует в Object RNA"
        # The pointer can exist but refer to an older same-name PropertyGroup.
        # In that situation querying the new class alone is a false OK.
        fixed_type = getattr(pointer_prop, "fixed_type", None)
        if fixed_type is not None:
            live_ptr = getattr(fixed_type, "as_pointer", lambda: None)()
            expected_ptr = getattr(cls.bl_rna, "as_pointer", lambda: None)()
            if live_ptr and expected_ptr and live_ptr != expected_ptr:
                return False, "Object.uvwall_definition ссылается на устаревшую RNA-схему"
    except Exception as exc:
        return False, f"Path Definition RNA повреждена: {type(exc).__name__}: {exc}"
    return True, f"OK · Schema v{CURRENT_SCHEMA_VERSION}"


def _remove_definition_pointer():
    if hasattr(bpy.types.Object, "uvwall_definition"):
        try:
            del bpy.types.Object.uvwall_definition
            return True, "pointer removed"
        except Exception as exc:
            return False, f"remove pointer: {type(exc).__name__}: {exc}"
    return True, "pointer absent"


def _unregister_class_object(cls):
    if cls is None or not _rna_class_is_registered(cls):
        return True, "class absent"
    try:
        bpy.utils.unregister_class(cls)
        return True, "class unregistered"
    except Exception as exc:
        return False, f"unregister class: {type(exc).__name__}: {exc}"


def ensure_runtime_schema():
    """Repair stale/missing RNA only after normal add-on registration.

    Primary ``register()`` never calls this routine. This is an emergency path
    for a live Inspector / Reload Scripts scenario, where an old PropertyGroup
    may still be registered from a previous module generation.
    """
    ok, message = runtime_schema_status()
    if ok:
        return True, message, False

    diagnostics = [message]
    pointer_ok, pointer_message = _remove_definition_pointer()
    if not pointer_ok:
        return False, "Не удалось снять старый Object.uvwall_definition · " + pointer_message, False

    # If Blender exposes a stale class by name, remove it. The normal in-place
    # package reload path removes the old class *before* importlib.reload(), so
    # this is only a secondary recovery mechanism.
    stale = getattr(bpy.types, "UVWALL_PG_path_definition", None)
    if stale is not None and stale is not UVWALL_PG_path_definition:
        try:
            bpy.utils.unregister_class(stale)
            diagnostics.append("stale class removed")
        except Exception as exc:
            return False, (
                "Не удалось снять старую Path Definition RNA: "
                f"{type(exc).__name__}: {exc} · " + " | ".join(diagnostics)
            ), False

    if not _rna_class_is_registered(UVWALL_PG_path_definition):
        try:
            bpy.utils.register_class(UVWALL_PG_path_definition)
        except Exception as exc:
            return False, (
                "Не удалось зарегистрировать текущую Path Definition RNA: "
                f"{type(exc).__name__}: {exc} · " + " | ".join(diagnostics)
            ), False

    try:
        bpy.types.Object.uvwall_definition = bpy.props.PointerProperty(type=UVWALL_PG_path_definition)
    except Exception as exc:
        return False, f"Не удалось зарегистрировать Object.uvwall_definition: {type(exc).__name__}: {exc}", False

    ok, final_message = runtime_schema_status()
    if not ok:
        return False, "Автовосстановление RNA не завершено: " + final_message, False
    return True, final_message + " · RNA восстановлена автоматически", True

def migrate_definition(definition):
    if definition is None:
        return None
    try:
        version = int(getattr(definition, "schema_version", 1))
    except Exception:
        version = 1
    # v2 introduced wall_offset. v3 separates 2K/4K virtual assets. Blender RNA
    # supplies defaults for old .blend data; we also migrate a compatible legacy
    # shared asset into the correctly sized slot when its image dimensions prove
    # which module it belongs to.
    if version < 3:
        # Backward compatibility: old .blend textures are ignored; the
        # already-authored material is the sole source of module resolution.
        try:
            from .material_intelligence import set_active_definition_asset
            legacy = getattr(definition, "virtual_material", None)
            if legacy is not None:
                set_active_definition_asset(definition, material=legacy)
        except (TypeError, ValueError, AttributeError):
            pass
    if version < 5:
        # Old AUTO introduced an implicit + half-thickness. Make it explicit
        # when migrating a pre-1.5.7 *existing* planter, preserving geometry.
        try:
            definition.wall_alignment = 'LEGACY'
            if definition.initialized and definition.create_inner_surface and definition.inner_surface_offset_auto:
                definition.inner_surface_offset += float(definition.inset_distance)*0.5
                definition.inner_surface_offset_auto = False
        except Exception:
            pass
    if version < CURRENT_SCHEMA_VERSION:
        try:
            definition.schema_version = CURRENT_SCHEMA_VERSION
        except Exception:
            pass
    return definition


def definition_for(path_obj):
    if path_obj is None or path_obj.type != "MESH":
        return None
    return migrate_definition(getattr(path_obj, "uvwall_definition", None))


def _copy_attr(source, target, name):
    if hasattr(source, name) and hasattr(target, name):
        try:
            setattr(target, name, getattr(source, name))
        except Exception:
            pass


def _first_material_image(material):
    if material is None or not getattr(material, "use_nodes", False) or material.node_tree is None:
        return None
    node = next((n for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image is not None), None)
    return node.image if node else None


def assign_virtual_asset_by_resolution(definition, material=None, image=None):
    """Migrate old projects by MATERIAL only. Image argument is deprecated."""
    from .material_intelligence import set_active_definition_asset
    return set_active_definition_asset(definition, material=material)

def virtual_asset_for_module(definition):
    """Legacy tuple signature: second value intentionally always None."""
    if definition is None:
        return None, None
    is_2k = str(getattr(definition, "module_standard", "4")) == "2"
    material = getattr(definition, "virtual_material_2k" if is_2k else "virtual_material_4k", None)
    return material, None

def initialize_from_scene(path_obj, settings):
    """One-way migration for a legacy scene setup into a persistent Path Definition."""
    definition = definition_for(path_obj)
    if definition is None:
        return None
    if definition.initialized:
        ensure_source_uid(path_obj, definition)
        return definition
    for name in (
        "module_standard", "output_name", "stories", "custom_first_story", "first_story_height",
        "story_direction", "align_bottom", "bottom_extend", "create_inner", "inset_distance", "inner_wall_height_delta", "inner_wall_height_overrides_json", "secondary_points_json", "wall_offset", "wall_alignment",
        "wall_module_rotation", "wall_uv_origin_mode", "create_inner_surface", "inner_surface_offset_auto", "inner_surface_offset",
        "inner_surface_height_offset", "inner_surface_material",
        "inset_side", "close_ends", "close_start_end", "close_finish_end", "keep_inner_path", "create_top_cap", "cap_material_override", "cap_material", "create_bottom_cap", "bottom_cap_material_override", "bottom_cap_material",
        "wall_normal_mode", "surface_heights_json", "roof_normal", "surface_method", "cap_rotation", "cornice_profile",
        "surface_module_rotation", "surface_height_offset", "cornice_profile_rotation",
        "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
        "cornice_flip_side", "cornice_uv_mode", "cornice_corner_mode", "cornice_linear_step", "cornice_smooth_warning_angle",
    ):
        _copy_attr(settings, definition, name)
    legacy_mode = getattr(settings, "build_mode", "NONE")
    definition.generator_type = {"ROOF_ONLY": "SURFACE"}.get(legacy_mode, legacy_mode if legacy_mode in {"WALL", "CORNICE"} else "NONE")
    definition.virtual_material = getattr(settings, "reference_material", None)
    # Deprecated reference_image is deliberately not used.
    assign_virtual_asset_by_resolution(definition, definition.virtual_material)
    # Seamless migration from the old Plane-object workflow: capture its material/image
    # into the persistent Definition, then NEXTGEN can work without that Object.
    source_plane = getattr(settings, "source_plane", None)
    if source_plane is not None and getattr(source_plane, "type", None) == "MESH":
        try:
            poly = source_plane.data.polygons[0] if source_plane.data.polygons else None
            if poly is not None and poly.material_index < len(source_plane.data.materials):
                material = source_plane.data.materials[poly.material_index]
                if definition.virtual_material is None and material is not None:
                    definition.virtual_material = material
                if definition.virtual_image is None and material is not None and material.use_nodes and material.node_tree:
                    image_node = next((n for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image is not None), None)
                    if image_node is not None:
                        definition.virtual_image = image_node.image
                assign_virtual_asset_by_resolution(definition, definition.virtual_material, definition.virtual_image)
        except Exception:
            pass
    try:
        definition.corner_uid_json = json.dumps([int(item.vertex_uid) for item in settings.cornice_corners])
        definition.corner_methods_json = json.dumps({str(int(m.vertex_uid)):str(getattr(m,"corner_method","CLASSIC")) for m in settings.cornice_corners if int(m.vertex_uid)>0})
    except Exception:
        definition.corner_uid_json = "[]"
        definition.corner_methods_json = "{}"
    definition.initialized = True
    ensure_source_uid(path_obj, definition)
    definition.revision += 1
    return definition


def sync_scene_from_definition(path_obj, settings):
    """Compatibility adapter for the legacy production generators.

    Viewport tools mutate the Definition.  Existing tested generators still read
    Scene settings, so this adapter is the only bridge between both worlds.
    """
    d = definition_for(path_obj)
    if d is None:
        return
    settings.path_mesh = path_obj
    settings.build_mode = {"SURFACE": "ROOF_ONLY"}.get(d.generator_type, d.generator_type)
    for name in (
        "module_standard", "output_name", "stories", "custom_first_story", "first_story_height",
        "story_direction", "align_bottom", "bottom_extend", "create_inner", "inset_distance", "inner_wall_height_delta", "inner_wall_height_overrides_json", "secondary_points_json", "wall_offset", "wall_alignment",
        "wall_module_rotation", "wall_uv_origin_mode", "create_inner_surface", "inner_surface_offset_auto", "inner_surface_offset",
        "inner_surface_height_offset", "inner_surface_material",
        "inset_side", "close_ends", "close_start_end", "close_finish_end", "keep_inner_path", "create_top_cap", "cap_material_override", "cap_material", "create_bottom_cap", "bottom_cap_material_override", "bottom_cap_material",
        "wall_normal_mode", "surface_heights_json", "roof_normal", "surface_method", "cap_rotation", "cornice_profile",
        "surface_module_rotation", "surface_height_offset", "cornice_profile_rotation",
        "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
        "cornice_flip_side", "cornice_uv_mode", "cornice_corner_mode", "cornice_linear_step", "cornice_smooth_warning_angle",
    ):
        _copy_attr(d, settings, name)
    settings.tile_length = float(d.module_standard)
    active_material, _unused_image = virtual_asset_for_module(d)
    if active_material is not None:
        settings.reference_material = active_material
    # Legacy settings.reference_image is not synchronized into the new system.
    # Authored Sharp corners are Definition data, not transient UI state.
    try:
        desired = [int(value) for value in json.loads(d.corner_uid_json or "[]") if int(value) > 0]
    except Exception:
        desired = []
    try:
        current = [int(item.vertex_uid) for item in settings.cornice_corners]
        if current != desired:
            settings.cornice_corners.clear()
            for uid in desired:
                marker = settings.cornice_corners.add()
                marker.vertex_uid = uid
                marker.cached_index = -1
                marker.path_name = path_obj.name
                if hasattr(marker, "corner_type"):
                    marker.corner_type = "SHARP"
    except Exception:
        pass
    # Per-corner variant survives switching UI modes and Blender reopen.
    from ..production.corner_methods import MODE_IDS
    try:
        raw = json.loads(getattr(d, 'corner_methods_json', '{}') or '{}')
        methods = raw if isinstance(raw, dict) else {}
    except (ValueError, TypeError):
        methods = {}
    for marker in settings.cornice_corners:
        mode = methods.get(str(int(marker.vertex_uid)), 'CLASSIC')
        marker.corner_method = mode if mode in MODE_IDS else 'CLASSIC'


def sync_definition_from_scene(path_obj, settings):
    d = definition_for(path_obj)
    if d is None:
        return
    for name in (
        "module_standard", "output_name", "stories", "custom_first_story", "first_story_height",
        "story_direction", "align_bottom", "bottom_extend", "create_inner", "inset_distance", "inner_wall_height_delta", "inner_wall_height_overrides_json", "secondary_points_json", "wall_offset", "wall_alignment",
        "wall_module_rotation", "wall_uv_origin_mode", "create_inner_surface", "inner_surface_offset_auto", "inner_surface_offset",
        "inner_surface_height_offset", "inner_surface_material",
        "inset_side", "close_ends", "close_start_end", "close_finish_end", "keep_inner_path", "create_top_cap", "cap_material_override", "cap_material", "create_bottom_cap", "bottom_cap_material_override", "bottom_cap_material",
        "wall_normal_mode", "surface_heights_json", "roof_normal", "surface_method", "cap_rotation", "cornice_profile",
        "surface_module_rotation", "surface_height_offset", "cornice_profile_rotation",
        "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
        "cornice_flip_side", "cornice_uv_mode", "cornice_corner_mode", "cornice_linear_step", "cornice_smooth_warning_angle",
    ):
        _copy_attr(settings, d, name)
    d.generator_type = {"ROOF_ONLY": "SURFACE"}.get(getattr(settings, "build_mode", "NONE"), getattr(settings, "build_mode", "NONE"))
    d.virtual_material = getattr(settings, "reference_material", None)
    # Material determines the module; the old reference_image is ignored.
    assign_virtual_asset_by_resolution(d, d.virtual_material)
    try:
        d.corner_uid_json = json.dumps([int(item.vertex_uid) for item in settings.cornice_corners if int(item.vertex_uid) > 0])
        d.corner_methods_json = json.dumps({str(int(m.vertex_uid)):str(getattr(m,'corner_method','CLASSIC')) for m in settings.cornice_corners if int(m.vertex_uid)>0})
    except Exception:
        pass
    d.initialized = True
    ensure_source_uid(path_obj, d)
    d.revision += 1



_POINTER_COLLECTIONS = {
    "virtual_material": "materials",
    "virtual_image": "images",
    "virtual_material_2k": "materials",
    "virtual_image_2k": "images",
    "virtual_material_4k": "materials",
    "virtual_image_4k": "images",
    "cornice_profile": "objects",
    "inner_surface_material": "materials",
    "cap_material": "materials",
    "bottom_cap_material": "materials",
}


def serialize_definition(definition):
    """Serialize editable Definition state for an Undo-safe viewport commit."""
    if definition is None:
        return ""
    values = {}
    for name in SNAPSHOT_FIELDS:
        if name in {"last_result_name", "last_error", "last_committed_revision", "last_successful_settings_json"}:
            continue
        try:
            value = getattr(definition, name)
        except Exception:
            continue
        if name in _POINTER_COLLECTIONS:
            values[name] = value.name if value is not None else None
        else:
            values[name] = value
    return json.dumps({"schema": CURRENT_SCHEMA_VERSION, "values": values}, ensure_ascii=False, separators=(",", ":"))


def apply_serialized_definition(definition, payload):
    """Apply a serialized target state, resolving Blender datablock pointers by name."""
    if definition is None or not payload:
        return False
    try:
        data = json.loads(payload)
        values = data.get("values", {})
    except Exception:
        return False
    for name, value in values.items():
        if name not in SNAPSHOT_FIELDS:
            continue
        try:
            if name in _POINTER_COLLECTIONS:
                collection = getattr(bpy.data, _POINTER_COLLECTIONS[name])
                value = collection.get(value) if value else None
            setattr(definition, name, value)
        except Exception:
            pass
    definition.initialized = True
    definition.revision += 1
    return True

def total_wall_height(definition):
    module = float(definition.module_standard)
    stories = max(1, int(definition.stories))
    first = float(definition.first_story_height) if definition.custom_first_story and definition.first_story_height > 1.0e-9 else module
    return first + module * (stories - 1)


def set_total_wall_height(definition, total_height):
    """Represent arbitrary total height without violating the existing module stack model."""
    total_height = max(0.001, float(total_height))
    module = float(definition.module_standard)
    stories = max(1, int((total_height + module - 1.0e-9) // module))
    # ceil without importing math; exact multiples remain exact.
    if stories * module < total_height - 1.0e-9:
        stories += 1
    first = total_height - module * (stories - 1)
    if first <= 1.0e-9:
        first = module
    definition.stories = stories
    definition.custom_first_story = abs(first - module) > 1.0e-8
    definition.first_story_height = first if definition.custom_first_story else 0.0
    definition.revision += 1


def analyze_path(path_obj):
    """Fast non-destructive topology summary for the Viewport Inspector."""
    result = {
        "valid_mesh": False, "vertices": 0, "edges": 0, "components": 0,
        "branches": [], "isolated": [], "closed_components": 0, "open_components": 0,
        "simple_chains": [], "total_length": 0.0, "intersections": [], "xy_area": 0.0,
    }
    if path_obj is None or path_obj.type != "MESH":
        return result
    mesh = path_obj.data
    result["valid_mesh"] = True
    result["vertices"] = len(mesh.vertices)
    result["edges"] = len(mesh.edges)
    adjacency = {i: [] for i in range(len(mesh.vertices))}
    for edge in mesh.edges:
        a, b = map(int, edge.vertices)
        if a != b:
            adjacency[a].append(b); adjacency[b].append(a)
    used = {i for i, links in adjacency.items() if links}
    result["isolated"] = sorted(set(range(len(mesh.vertices))) - used)
    result["branches"] = sorted(i for i in used if len(adjacency[i]) > 2)
    remaining = set(used)
    world = path_obj.matrix_world
    while remaining:
        root = min(remaining); stack = [root]; comp = set()
        while stack:
            i = stack.pop()
            if i in comp: continue
            comp.add(i)
            stack.extend(n for n in adjacency[i] if n not in comp)
        remaining -= comp
        result["components"] += 1
        degrees = {i: len([n for n in adjacency[i] if n in comp]) for i in comp}
        if any(v > 2 for v in degrees.values()):
            continue
        endpoints = sorted(i for i, v in degrees.items() if v == 1)
        closed = all(v == 2 for v in degrees.values()) and len(comp) >= 3
        if not closed and len(endpoints) != 2:
            continue
        start = min(comp) if closed else endpoints[0]
        order = [start]; visited = {start}; previous = None; current = start
        for _ in range(len(comp) + 2):
            candidates = [n for n in adjacency[current] if n in comp and n != previous]
            if not candidates: break
            nxt = min(candidates)
            if closed and nxt == start:
                order.append(start); break
            if nxt in visited: break
            visited.add(nxt); order.append(nxt); previous, current = current, nxt
        if closed and order[-1] != start:
            continue
        if not closed and len(order) != len(comp):
            continue
        points = [world @ mesh.vertices[i].co for i in order]
        signed_xy_area = (sum(a.x*b.y-b.x*a.y for a,b in zip(points, points[1:]))*0.5) if closed else 0.0
        if closed:
            result["xy_area"] += abs(signed_xy_area)

        # Diagnostic-only XY segment intersections for BOTH open and closed
        # paths.  Adjacent segments share an authored vertex and are not a
        # problem; the first/last segment pair is also adjacent for closed
        # loops.  Keep the scan bounded so the interactive Problem Navigator
        # can never turn into an accidental O(n²) freeze on giant imports.
        nseg = len(points)-1
        if nseg <= 1000:
            from .path_proximity import xy_segment_candidate_pairs
            def _cross(a,b): return a[0]*b[1]-a[1]*b[0]
            for i,j in xy_segment_candidate_pairs(points,closed=closed):
                a,b=points[i],points[i+1]
                r=(b.x-a.x,b.y-a.y)
                c,d=points[j],points[j+1]
                v=(d.x-c.x,d.y-c.y)
                denom=_cross(r,v)
                if abs(denom)<1e-12:
                    continue
                ac=(c.x-a.x,c.y-a.y)
                t=_cross(ac,v)/denom
                u=_cross(ac,r)/denom
                if 1e-8<t<1-1e-8 and 1e-8<u<1-1e-8:
                    result["intersections"].append(
                        Vector((a.x+t*r[0], a.y+t*r[1], (a.z+b.z+c.z+d.z)*0.25))
                    )
        length = sum((b - a).length for a, b in zip(points, points[1:]))
        result["total_length"] += length
        result["simple_chains"].append({"indices": order, "points": points, "closed": closed, "length": length, "signed_xy_area": signed_xy_area})
        if closed: result["closed_components"] += 1
        else: result["open_components"] += 1
    return result


def register():
    """Primary Blender registration path.

    This path is intentionally boring: register the exact current class first,
    then attach the pointer. Self-heal is *not* used during addon_enable.
    """
    _remove_definition_pointer()

    # A stale same-name class should normally have been removed by the package
    # pre-reload cleanup. Keep one defensive attempt for legacy installs.
    stale = getattr(bpy.types, "UVWALL_PG_path_definition", None)
    if stale is not None and stale is not UVWALL_PG_path_definition:
        try:
            bpy.utils.unregister_class(stale)
        except Exception:
            pass

    if not _rna_class_is_registered(UVWALL_PG_path_definition):
        bpy.utils.register_class(UVWALL_PG_path_definition)

    bpy.types.Object.uvwall_definition = bpy.props.PointerProperty(type=UVWALL_PG_path_definition)

    ok, message = runtime_schema_status()
    if not ok:
        # Roll back our own partial registration before propagating the error.
        _remove_definition_pointer()
        _unregister_class_object(UVWALL_PG_path_definition)
        raise RuntimeError("Path Definition RNA registration failed: " + message)


def unregister():
    _remove_definition_pointer()
    _unregister_class_object(UVWALL_PG_path_definition)

