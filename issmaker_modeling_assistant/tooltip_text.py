"""ISSMAKER 1.3.32: concise, action-accurate operator tooltip copy.

Pure Python: intentionally independent of bpy to permit preflight verification.
Blender queries these descriptions via Operator.description(context, properties).
"""


def uv_analysis(kind, session_active=False):
    """Explain which UV problem is searched, or that a running preview closes."""
    if session_active:
        return "Закрыть текущий просмотр UV-проблем. Новая проверка запускается после закрытия."
    if kind == 'FLIPPED':
        return ("Найти перевёрнутые UV-полигоны (Flip UV). "
                "Показать проблемные области без изменения выделения или UV.")
    if kind == 'STRETCH':
        return ("Найти растяжения UV-развёртки (Stretch). "
                "Проверить угловые искажения, не изменяя выделение и UV.")
    return "Найти ошибки UV-развёртки без изменения выделения."


def radial(kind):
    if kind == 'TOOLS':
        return 'Открыть радиальное меню модулей ISSMAKER. Проёмы запускают настоящий Viewport Preview.'
    if kind == 'LINKED':
        return ("Открыть Quick Select: выбрать ограничение быстрого выделения "
                "(UV, UDIM, Seam, Sharp, Normal, Material, Texture Color). Esc выключает режим.")
    return "Открыть радиальное меню режимов отображения текущего 3D Viewport."


def issue_navigation(direction):
    target = 'предыдущей' if direction < 0 else 'следующей'
    return (f"Перейти к {target} найденной UV-проблеме и подсветить её. "
            "Выделение полигонов не изменяется.")


def weld_navigation(direction):
    target = 'предыдущей' if direction < 0 else 'следующей'
    return (f"Перейти к {target} группе вершин-кандидатов для сшивки. "
            "Геометрия и выделение не изменяются.")


def editor_switch(editor):
    if editor == 'SHADER':
        return "Открыть Shader Editor в этой области. Новое окно не создаётся."
    return "Открыть UV Editor в этой области. Новое окно не создаётся."


def editor_split(editor):
    if editor == 'SHADER':
        return "Разделить текущую область и открыть дополнительный Shader Editor."
    return "Разделить текущую область и открыть дополнительный UV Editor."
