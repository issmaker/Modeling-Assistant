"""BH0465 pure read-only UI recovery classification (no Blender imports)."""

COLLISION_CODE = 'COLLISION: новая поверхность пересекает исходную сетку'


def is_recoverable_collision(error):
    """Only the known swept source-contact family, never arbitrary exceptions."""
    return COLLISION_CODE in str(error)


def recovery_hint(message):
    if not is_recoverable_collision(message):
        raise ValueError('Ожидалась проверенная ошибка коллизии')
    return ('Коллизия: новая поверхность пересекает исходную геометрию. '
            'S — найти проверенную безопасную глубину; '
            'измените направление или Esc — выход. Меш не изменён.')


def probe_budget(edge_count, source_faces):
    if not (1 <= edge_count <= 512) or source_faces < 0:
        raise ValueError('Недопустимый размер тестового меша')
    heavy = edge_count > 100 or source_faces > 10000
    return (12, .005) if heavy else (26, .001)
