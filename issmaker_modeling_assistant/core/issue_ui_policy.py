"""PPA Wave 10: pure accessible presentation/filtering atop common UI tokens.

This has no Blender dependency, geometry access, timers, GPU resources or state.
"""
from __future__ import annotations

LABELS = {
    'PROVEN_INVALID': ('ERROR', 'Доказанная ошибка', 'Ошибка подтверждена геометрическим свидетельством'),
    'UNCERTAIN': ('QUESTION', 'Неопределённо', 'Алгоритм не доказал ошибку; проверьте результат'),
    'WARNING': ('ERROR', 'Предупреждение', 'Не является математическим доказательством'),
    'REVIEW': ('INFO', 'Нужна проверка', 'Сведения источника требуют ручной проверки'),
    'RESOURCE_LIMIT': ('TIME', 'Ограничение ресурсов', 'Лимит проверки не доказывает дефект геометрии'),
    'SETUP_REQUIRED': ('TOOL_SETTINGS', 'Нужна настройка', 'Проверьте входные данные или режим'),
    'INTERNAL_ERROR': ('CANCEL', 'Ошибка ISSMAKER', 'Ошибка инструмента, не пользовательской геометрии'),
}
SOURCE_IDS = frozenset(('ALL', 'RELIABILITY', 'BRICK_HOLE', 'UV_SEAMS', 'COLLISION'))
KIND_IDS = frozenset(('ALL', 'PROVEN_INVALID', 'WARNING', 'REVIEW', 'UNCERTAIN', 'OTHER'))


def presentation(kind, stale=False):
    if stale:
        return ('FILE_REFRESH', 'Снимок устарел', 'Координаты могли измениться. Обновите список')
    return LABELS.get(str(kind or ''), ('INFO', 'Статус неизвестен', 'Проверьте подробности'))


def visible_issues(issues, kind='ALL', source='ALL'):
    """Filter only existing snapshots; preserves incoming order and references."""
    kind = kind if kind in KIND_IDS else 'ALL'
    source = source if source in SOURCE_IDS else 'ALL'
    def accepted(row):
        if not isinstance(row, dict):
            return False
        severity = row.get('kind', '')
        if kind == 'OTHER':
            if severity in {'PROVEN_INVALID', 'WARNING', 'REVIEW', 'UNCERTAIN'}:
                return False
        elif kind != 'ALL' and severity != kind:
            return False
        return source == 'ALL' or row.get('source') == source
    return tuple(item for item in issues if accepted(item))


def select_index(visible, previous_id='', direction=0):
    """Preserve stable issue ID across filter toggles; wrap otherwise."""
    if not visible:
        return -1
    positions = [i for i, issue in enumerate(visible) if issue.get('id') == previous_id]
    current = positions[0] if positions else (len(visible) - 1 if direction < 0 else 0)
    if direction:
        return (current + (1 if direction > 0 else -1)) % len(visible)
    return current


def list_state(total, visible, stale, omitted=0, failures=0):
    if stale:
        return 'STALE'
    if failures:
        return 'SOURCE_FAILED'
    if omitted:
        return 'PARTIAL'
    if total == 0:
        return 'EMPTY'
    return 'FILTERED_EMPTY' if not visible else 'LIVE'
