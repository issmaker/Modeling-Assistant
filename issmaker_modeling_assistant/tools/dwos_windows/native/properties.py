from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatVectorProperty,
    IntProperty, PointerProperty, StringProperty, FloatProperty,
)
from bpy.types import PropertyGroup

from .interior_core import MAX_INTERIOR_DEPTH_M

def _cap_interior_depth(self, _context):
    """Cap depth in N-panel, Composer, batch copy and scripted RNA writes."""
    raw=str(self.interior_depth_text).strip().replace(',', '.')
    try:
        value=float(raw)
    except (ValueError,TypeError):
        return  # partial user typing is validated on commit
    if value>MAX_INTERIOR_DEPTH_M:
        self.interior_depth_text='1.5'



def _auto_smart_interior_settings(self, context):
    """Debounced contact-only rebuild after changing clearance/mode/enabled."""
    scene = getattr(self, 'id_data', None) or getattr(context, 'scene', None)
    if scene is not None:
        from .smart_interior_auto import settings_changed
        settings_changed(scene)


class DWOSOpeningCandidate(PropertyGroup):
    candidate_id: StringProperty()
    enabled: BoolProperty(name="Выбран", default=True)
    width_text: StringProperty()
    height_text: StringProperty()
    wall_depth_text: StringProperty()
    center: FloatVectorProperty(size=3)
    axis_x: FloatVectorProperty(size=3)
    axis_y: FloatVectorProperty(size=3)
    axis_z: FloatVectorProperty(size=3)
    payload_json: StringProperty(default="{}")


class DWOSSplit(PropertyGroup):
    split_id: StringProperty()
    axis: EnumProperty(items=[('V', 'Vertical', ''), ('H', 'Horizontal', '')], default='V')
    position_text: StringProperty(name="Позиция", default="0")
    thickness_text: StringProperty(name="Толщина", default="0,06")
    override_thickness: BoolProperty(name="Индивидуальная толщина", default=False)


class DWOSWindowSettings(PropertyGroup):
    is_window: BoolProperty(default=False)
    finalized: BoolProperty(default=False)
    window_id: StringProperty()
    host_name: StringProperty()
    opening_payload_json: StringProperty(default="{}")
    width_text: StringProperty(default="0")
    height_text: StringProperty(default="0")
    wall_depth_text: StringProperty(default="0")

    frame_width_text: StringProperty(name="Толщина основной рамы", default="0,08")
    internal_width_text: StringProperty(name="Толщина внутренних рам", default="0,06")
    frame_depth_text: StringProperty(name="Глубина рамы", default="0,10")
    # V1.4: front/exterior face is anchored to the detected wall facade plane.
    # Positive offset moves the frame toward the exterior, negative toward interior.
    frame_offset_text: StringProperty(name="Смещение рамы", default="0")

    optimization: EnumProperty(
        name="Режим",
        items=[
            ('FULL', 'FULL', 'Полная закрытая геометрия рамы'),
            ('OPTIMIZED', 'OPTIMIZED', 'Удалить тыльные грани РАМ, направленные внутрь помещения; не зависит от видимости и Interior BOX'),
        ],
        default='FULL',
    )
    interior_mode: EnumProperty(
        name='Интерьер',
        items=[('OFF','Выключен','Нет интерьерной геометрии'),
               ('REVEALS','Legacy: converted to BOX on rebuild','Old files only'),
               ('BOX','Бокс','Откосы и задняя стенка')],
        default='BOX',
    )
    interior_depth_text: StringProperty(name='Глубина Interior (до 1,5 м)', default='1', update=_cap_interior_depth)
    interior_groups_count: IntProperty(name='Число физических Interior',description='Один Interior по умолчанию; дополнительные только по выбранным сквозным V',default=1,min=1,max=64)

    interior_partition_mode: EnumProperty(name='Разделение Interior',items=[('AUTO','Все V','Разделять по всем сквозным V (режим старых сцен)'),('COUNT','Количество','Сгенерировать выбранное число физических секций, включая 1'),('CUSTOM','По импостам','Один Interior, пока пользователь не назначил V концами секций')],default='CUSTOM')
    interior_dividers_json: StringProperty(name='Разделители Interior',default='[]')
    interior_joint_gap_text: StringProperty(name='Мин. зазор секций, м',default='0,01')
    interior_clearance_text: StringProperty(name='Смещение от средней линии рамы', default='0')
    interior_offset_text: StringProperty(name="Смещение Interior", default="0")
    glass_offset_text: StringProperty(name="Смещение стекла по глубине", default="0")
    glass_edge_offset_text: StringProperty(name="Отступ стекла от середины рамы", default="0")
    glass_gap_text: StringProperty(name="Расстояние между стеклами", default="0,075")
    # Local +Z is the detected exterior direction. Default interior is -Z.
    flip_interior: BoolProperty(name="Перевернуть Exterior / Interior", default=False)
    corner_post_mode: EnumProperty(
        name='Угловой импост',
        items=[
            ('AUTO','AUTO','Для CORNER создавать архитектурный угловой импост автоматически'),
            ('ON','ON','Всегда создавать угловой импост на hard-corner'),
            ('OFF','OFF','Не создавать угловой импост'),
        ],
        default='AUTO',
    )

    # RC23 controlled comparison. LEGACY is retained for A/B parity in the same scene.
    # Other modes produce one independent four-quad open shell INSIDE the frame mesh.
    corner_post_variant: EnumProperty(
        name='Геометрия центрального импоста',
        items=[('LEGACY','LEGACY','Старый сшитый центральный профиль'),
               ('BISECTOR','BISECTOR','Независимый прямоугольный открытый профиль'),
               ('ANGLE','ANGLE','Независимый углозависимый параллелограмм'),
               ('TAPER','TAPER','Независимый сужающийся профиль')],
        default='LEGACY',
    )
    corner_post_width_text: StringProperty(name='Ширина центрального импоста',default='0,06')

    # 3.5.1 Geometry Lock. Legacy A/B/C/D values are retained only so saved
    # 3.5.0 files deserialize safely; rebuild always migrates CORNER to LOCKED.
    corner_interior_variant: EnumProperty(
        name='Corner Interior',
        items=[
            ('LOCKED','LOCKED','User-approved two-return / one-corner geometry'),
            ('A','Legacy A','3.5.0 comparison value; migrated on rebuild'),
            ('B','Legacy B','3.5.0 comparison value; migrated on rebuild'),
            ('C','Legacy C','3.5.0 comparison value; migrated on rebuild'),
            ('D','Legacy D','3.5.0 comparison value; migrated on rebuild'),
        ],
        default='LOCKED',
    )

    # Native Blender material selection; None means DWOS_Frame_Default.
    frame_material: PointerProperty(name='Материал рамы', type=bpy.types.Material)

    # 1.6 recursive Zone Composition; empty means legacy global splits.
    zone_tree_json: StringProperty(name='Дерево зон', default='')
    # 3.4: CORNER assemblies own independent per-facade Branch trees.
    branch_state_json: StringProperty(name='Ветки CORNER', default='')
    active_branch_id: StringProperty(name='Активная ветка CORNER', default='')
    active_zone_id: StringProperty(name='Выбранная зона', default='')
    active_zone_split_id: StringProperty(name='Выбранный локальный Split', default='')

    splits: CollectionProperty(type=DWOSSplit)
    active_split_index: IntProperty(default=-1)
    # 2.3: stable logical mullion IDs, saved separately from generated Mesh.
    mullion_selection_json: StringProperty(name='Выбранные импосты', default='[]')
    mullion_groups_json: StringProperty(name='Связанные группы', default='{}')
    mullion_group_name: StringProperty(name='Имя группы', default='Группа 1')
    mullion_offset_text: StringProperty(name='Сдвиг группы, м', default='0,10')
    mullion_step_text: StringProperty(name='Шаг, м', default='0,50')


class DWOSSceneSettings(PropertyGroup):
    lod_view_mode: EnumProperty(name='DWOS LOD',items=[('HP','HP','Полная геометрия'),('LP','LP','Плоская геометрия'),('BOTH','Оба','Обе версии'),('NONE','Скрыть','Скрыть обе версии')],default='HP')
    openings: CollectionProperty(type=DWOSOpeningCandidate)
    wall_name: StringProperty()
    opening_source_fingerprint: StringProperty(name='Контроль исходного Mesh проёмов',default='')
    status: StringProperty(default="")
    show_overlay: BoolProperty(default=True)
    openings_index: IntProperty(default=-1)
    show_opening_details: BoolProperty(name='Показать по отдельности', default=False)
    show_support_tools: BoolProperty(name='Сервис и диагностика (совместимость)', default=False)
    show_studio_section: BoolProperty(name='РАБОЧАЯ СТУДИЯ', default=True)
    show_diagnostics_section: BoolProperty(name='ДИАГНОСТИКА', default=False)
    show_service_section: BoolProperty(name='СЕРВИС', default=False)
    smart_interior_conflict_behavior: EnumProperty(
        name="Контакты Interior",
        description="ASK FIRST сохраняет оба исходных Interior, не подрезая соседей; решения только по подтверждению",
        items=[
          ("ASK_FIRST","ASK FIRST","Отметить проблемные зоны и дождаться выбора пользователя"),
          ("AUTO_LEGACY","AUTO LEGACY","Старый автоматический решатель для совместимости")],
        default="ASK_FIRST",update=_auto_smart_interior_settings)
    smart_interior_enabled: BoolProperty(name='Smart Interior',description='Согласовывать пересекающиеся Interior в одной связанной операции',default=True,update=_auto_smart_interior_settings)
    smart_interior_gap: FloatProperty(name='Зазор, м',description='Расстояние между адаптированными оболочками (метры)',default=0.03,min=0,max=0.1,precision=3,update=_auto_smart_interior_settings)
    smart_interior_mode: EnumProperty(name='Согласование',items=[
        ('BALANCED','Оба','Согласование с геометрическим зазором: 3D-плоскость или митор углов PLANAR'),
        ('PRIORITY','Приоритет активного','Сохранить активный Interior; подрезать соседние'),
        ('CONTOUR_FOLLOW','По контуру PLANAR','Контурная адаптация вертикальных PLANAR BOX, сложное — PENDING'),
    ],default='BALANCED',update=_auto_smart_interior_settings)
    manual_interior_strategy: EnumProperty(name='Ручной метод',items=[('BALANCED','Равномерно','Подрезать обе оболочки'),('PRIORITY','Сохранить выбранный','Сохранить активный Interior, подрезать соседний'),('CONTOUR_FOLLOW','По контуру','Контурное согласование доступных PLANAR BOX')],default='PRIORITY')
    show_window_advanced: BoolProperty(name='Дополнительные операции', default=False)

    mullion_presets_json: StringProperty(name='Шаблоны раскладки',default='{}')
    mullion_preset_name: StringProperty(name='Шаблон',default='Мой шаблон')
    batch_scope: EnumProperty(
        name='Область применения',
        items=[
            ('ACTIVE', 'ONE', 'Только активное окно'),
            ('SELECTED', 'SELECTED', 'Все выделенные окна DWOS'),
            ('SAME_SIZE', 'SAME SIZE', 'Окна того же размера на этой стене'),
            ('ALL_HOST', 'ALL', 'Все окна DWOS на этой стене'),
        ],
        default='ACTIVE',
    )
    same_size_tolerance_text: StringProperty(name='Допуск размера', default='0,01')


CLASSES = (DWOSOpeningCandidate, DWOSSplit, DWOSWindowSettings, DWOSSceneSettings)


def register_props():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.dwos_window = PointerProperty(type=DWOSWindowSettings)
    bpy.types.Scene.dwos = PointerProperty(type=DWOSSceneSettings)


def unregister_props():
    if hasattr(bpy.types.Scene, 'dwos'):
        del bpy.types.Scene.dwos
    if hasattr(bpy.types.Object, 'dwos_window'):
        del bpy.types.Object.dwos_window
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
