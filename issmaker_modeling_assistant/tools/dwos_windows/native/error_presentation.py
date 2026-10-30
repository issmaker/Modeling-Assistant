"""User-facing DWOS error decision kernel; avoid false invalid-geometry claims.

Never discard raw exception or traceback from diagnostics. Classification
represents evidence about the *reason* for rejection, not a geometry verdict.
"""
from __future__ import annotations

CATALOG={
 'LP_FIRST_NON_ORTHOGONAL_COMPLEX_PATH_UNSUPPORTED':('UNSUPPORTED_TOPOLOGY','Этот проём содержит наклонные грани или неплоский контур.','Сохраните исходный путь; для него потребуется отдельный геометрический режим.'),
 'ZONE_ORTHOGONAL_EMPTY_LEAF':('INVALID_ZONE_LAYOUT','Одна из созданных зон не пересекает реальный проём.','Переместите разделитель или выберите другую зону, исходная геометрия не менялась.'),
 'ZONE_NEXT_RESOURCE_BUDGET':('RESOURCE_LIMIT','План зон превысил безопасный лимит.','Уменьшите число разделителей или делайте отдельные окна.'),
 'ZONE_NEXT_AREA_CONSERVATION':('INTERNAL_ERROR','Проверка площади зоны обнаружила расхождение.','Сохраните ZIP диагностики, не публикуйте текущий вариант.'),
 'ZONE_FILL_NOT_SUPPORTED_YET':('UNSUPPORTED_FEATURE','Выбранный тип заполнения пока не поддерживается.','Выберите GLASS, PANEL OPT или EMPTY.'),
 'ZONE_PANEL_FULL_NOT_SUPPORTED_YET':('UNSUPPORTED_FEATURE','Двухсторонняя панель FULL ещё не поддерживается.','Режим OPT доступен; FULL потребует безопасной двусторонней геометрии.'),

 'RECOVER_NEEDS_OBJECT_MODE':('UNSUPPORTED_CONTEXT','Repair LOD доступен только в Object Mode.','Перейдите в Object Mode.'),
 'RECOVER_ONLY_PRECUT_STAGES_2_3':('UNSUPPORTED_STAGE','Эта стадия не поддерживает Repair LOD.','Используйте Restore из Backup или диагностику ZIP.'),
 'RECOVER_SOURCE_TOPOLOGY_CHANGED':('STALE_STATE','Исходная стена изменилась.','Восстановление заблокировано, чтобы сохранить авторскую геометрию.'),
 'RECOVER_SOURCE_UV_CHANGED':('STALE_STATE','UV исходной стены изменены.','Восстановление заблокировано; проверьте UV.'),
 'RECOVER_BACKUP_INTEGRITY_MISMATCH':('INTEGRITY','Backup не совпадает с исходной стеной.','Сохраните ZIP диагностики, не продолжайте.'),
 'RECOVER_EXISTING_LOD_PROVENANCE_MISMATCH':('INTEGRITY','Объект HP/LP принадлежит другой сессии.','Не перезаписывайте; сохраните ZIP.'),
 'RECOVER_INVALID_LOD_REFERENCE':('INTEGRITY','Сессия содержит некорректную ссылку на LOD.','Восстановление остановлено.'),
 'RECOVER_NOTHING_MISSING':('STATE_SEQUENCE','Оба HP/LP находятся на месте.','Восстановление не требуется.'),
 'RECOVER_OBJECT_NAME_CONFLICT':('INTEGRITY','Имя создаваемого LOD занято.','Проверьте дублирование.'),
 'RECOVER_HOST_CHANGED_DURING_REBUILD':('INTEGRITY','Стена изменилась при Repair LOD.','Операция откатывается; сохраните ZIP.'),
 'BACKUP_MESH_NOT_FOUND_OR_PROVENANCE_MISMATCH':('INTEGRITY','Backup отсутствует или относится к другой стене.','Используйте проверенную копию проекта.'),
 'RECOVER_LEGACY_SESSION_BLOCKED':('UNSUPPORTED_STAGE','Старая сессия не поддерживается Repair LOD.','Используйте Legacy Clone.'),
 'STEP_ORDER':('STATE_SEQUENCE','Действия выполнены не по порядку.','Примените следующее разрешённое действие в панели DWOS NEXT.'),
 'PUBLISHED_LOD_MISSING_UNDO':('STALE_STATE','После отмены отсутствует часть построенной геометрии.','Не продолжайте операцию. Сохраните ZIP и восстановите Backup в Object Mode.'),
 'HOST_CHANGED_SINCE_STEP_1':('STALE_STATE','Стена изменилась после создания окна.','Повторно проверьте исходную стену и создайте новую сессию из Backup.'),
 'HOST_CHANGED_SINCE_CUT':('STALE_STATE','Стена изменилась после удаления заполнения.','Проверьте источник изменений; восстановите сессию из Backup при необходимости.'),
 'SOURCE_TOPOLOGY_CHANGED':('STALE_STATE','Контур исходной стены изменился.','Выполните повторный поиск проёмов вместо продолжения старой сессии.'),
 'SOURCE_UV_CHANGED':('STALE_STATE','Исходная UV-развёртка была изменена.','Проверьте UV-модули и перезапустите процесс на проверенной копии.'),
 'HOST_UV_CHANGED_SINCE_CUT':('STALE_STATE','UV исходной стены изменились после вырезания.','Проверьте изменения и восстановите резервную копию.'),
 'LP_FIRST_RECTANGULAR_PATCH_ONLY':('UNSUPPORTED_TOPOLOGY','Этот тип сложного контура пока не поддерживается LP-FIRST.','Оставьте геометрию без изменений; сложные пути будут обработаны отдельным ядром.'),
 'SURFACE_GRAPH_REQUIRES_RECTANGULAR_OUTER_AND_INNER':('UNSUPPORTED_TOPOLOGY','Форма окна пока не подходит для текущего LP-FIRST.','Не изменяйте контур ради обхода ошибки; используйте подходящий режим.'),
 'LP_FIRST_CELL_BUDGET_EXCEEDED':('RESOURCE_LIMIT','Сетка превышает безопасный лимит обработки.','Уменьшите число делений или используйте модульное построение.'),
 'MULLION_TOO_WIDE_FOR_SIX_GLASS_ZONES':('INVALID_PARAMETER','Толщина разделителей слишком велика для размера окна.','Уменьшите толщину импостов или измените количество зон.'),
 'NO_SELECTED_POLYGONS':('INVALID_SELECTION','Не выбраны грани фасада.','Выберите нужные грани в Edit Mode.'),
 'NON_UNIT_SCALE':('UNSUPPORTED_TRANSFORM','Для этой лабораторной операции не поддерживается неприменённый масштаб.','Работайте с копией и примените Scale, не изменяя исходник.'),
 'UNIT_SCALE_NOT_ONE':('UNSUPPORTED_UNITS','Тестовая LP-FIRST система ожидает метры.','Проверьте Unit Scale в настройках сцены.'),
 'UV_RESOLUTION_MUST_BE_2048_OR_4096':('INVALID_PARAMETER','Размер текстуры не подходит для выбранного режима.','Используйте 2048 для 2×2 м либо 4096 для 4×4 м.'),
 'HP_NONMANIFOLD':('GEOMETRY_DEFECT','Обнаружены некорректные соединения рёбер с несколькими гранями.','Откройте диагностику геометрии и проверьте указанные рёбра.'),
 'LP_NONMANIFOLD':('GEOMETRY_DEFECT','Обнаружены некорректные соединения рёбер LP.','Откройте диагностику геометрии и проверьте указанные рёбра.'),
 'ZONE_NEXT_CELL_NOT_UNIQUELY_ASSIGNED':('INTERNAL_ERROR','Не удалось однозначно связать ячейку с зоной.','Сохраните ZIP диагностики; не продолжайте публикацию этого окна.'),
 'ZONE_NEXT_AREA_CONSERVATION':('INTERNAL_ERROR','Проверка площади зон обнаружила расхождение.','Сохраните ZIP диагностики и повторите на отдельной тестовой копии.'),
}
def explain(exc):
    raw=str(exc)
    code=raw.split(':',1)[0].strip()
    if code in CATALOG:
        category,title,advice=CATALOG[code]
    elif isinstance(exc,MemoryError):
        code='MEMORY_EXHAUSTED';category='RESOURCE_LIMIT';title='Недостаточно памяти для построения.';advice='Сохраните проект и уменьшите размер операции.'
    elif isinstance(exc,(FloatingPointError,OverflowError)):
        code='NUMERIC_UNCERTAINTY';category='NUMERIC_UNCERTAINTY';title='Численный расчёт не завершился надёжно.';advice='Сохраните диагностику; исходная геометрия не признана ошибочной.'
    elif isinstance(exc,ValueError):
        code=code if len(code)<75 else 'INVALID_INPUT'
        category='INVALID_PARAMETER';title='Не удалось применить переданные параметры.';advice='Проверьте выделение и значения в текущей панели.'
    else:
        code=type(exc).__name__;category='INTERNAL_ERROR';title='Внутренняя ошибка инструмента.';advice='Сохраните ZIP диагностики и передайте журнал разработчику.'
    return {'code':code,'category':category,'title':title,'advice':advice,
            'raw':raw,'exception_type':type(exc).__name__}

def user_message(exc):
    d=explain(exc)
    return f"{d['title']} {d['advice']}"
