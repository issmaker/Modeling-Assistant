"""Pure presentation geometry for DWOS 3.7.8 Composer.

Nothing in this module imports Blender, changes a scene, or owns input state.
Screen-space drafting annotations are deliberately *nominal* for CORNER and
CURVED windows: a projected bbox is not a geometric measurement.
"""
from __future__ import annotations
import math


def rounded_outline(x, y, w, h, radius=7.0, steps=5):
    """Return a counterclockwise rounded rectangle ring with finite coordinates."""
    x, y, w, h = map(float, (x, y, w, h))
    if not all(math.isfinite(q) for q in (x, y, w, h)) or min(w, h) <= 0:
        return ()
    r = max(0.0, min(float(radius), w * 0.5, h * 0.5))
    n = max(1, min(16, int(steps)))
    if r < 0.1:
        return ((x, y), (x+w, y), (x+w, y+h), (x, y+h))
    corners = (
        (x+w-r, y+r, -math.pi/2, 0.0),
        (x+w-r, y+h-r, 0.0, math.pi/2),
        (x+r, y+h-r, math.pi/2, math.pi),
        (x+r, y+r, math.pi, 3*math.pi/2),
    )
    return tuple((cx+r*math.cos(a+(b-a)*i/n), cy+r*math.sin(a+(b-a)*i/n))
                 for cx, cy, a, b in corners for i in range(n+1))


def _intersects(a, b, pad=0.0):
    x, y, w, h = a
    X, Y, W, H = b
    return min(x+w+pad, X+W+pad) > max(x-pad, X-pad) and min(y+h+pad, Y+H+pad) > max(y-pad, Y-pad)


def constructor_guides(frame, viewport, avoid=(), *, margin=9.0, gap=17.0):
    """Select non-overlapping, on-screen annotation rails outside the model.

    Returns a dictionary with 'horizontal' and/or 'vertical' rail records.
    Rails include a *text centre* and screen-line segments; physical values
    must come from actual DWOS properties rather than projected pixel lengths.
    An unavailable safe rail is omitted instead of drawing through Composer.
    """
    if frame is None:
        return {}
    try:
        x, y, w, h = (float(v) for v in frame)
        vw, vh = (float(v) for v in viewport)
    except (TypeError, ValueError):
        return {}
    if not all(map(math.isfinite, (x, y, w, h, vw, vh))) or min(w, h, vw, vh) <= 0:
        return {}
    if w < 26 or h < 26:
        return {}
    margin = max(4.0, float(margin))
    gap = max(12.0, float(gap))
    # A full dimension label requires clearance.  Minimize label cover over
    # other DWOS windows, even if the rail itself is clear.
    rails = {}
    blockers = tuple(r for r in avoid if r and len(r) == 4)
    for direction, choices in (
        ('horizontal', ((x, y+h+gap), (x, y-gap))),
        ('vertical', ((x+w+gap, y), (x-gap, y))),
    ):
        for rx, ry in choices:
            if direction == 'horizontal':
                rect = (rx, ry-8, w, 16)
                label = (rx+w*.5-52, ry+9 if ry>y else ry-23, 104, 13)
                segments = (((x,ry),(x+w,ry)),((x,ry-4),(x,ry+4)),((x+w,ry-4),(x+w,ry+4)))
                orientation = 'top' if ry>y else 'bottom'
            else:
                rect = (rx-8, ry, 16, h)
                label = (rx+11 if rx>x else rx-109, ry+h*.5-7, 99, 14)
                segments = (((rx,y),(rx,y+h)),((rx-4,y),(rx+4,y)),((rx-4,y+h),(rx+4,y+h)))
                orientation = 'right' if rx>x else 'left'
            def on_screen(r):
                ax, ay, aw, ah = r
                return ax >= margin and ay >= margin and ax+aw <= vw-margin and ay+ah <= vh-margin
            if not on_screen(rect) or not on_screen(label):
                continue
            if any(_intersects(rect, blocker, 4) or _intersects(label, blocker, 4) for blocker in blockers):
                continue
            rails[direction] = {'segments': segments, 'label': label, 'side': orientation}
            break
    return rails


# 3.7.9: a purely visual context card for the selected or hovered mullion.
# There are intentionally no mouse targets; geometry/model ownership stays in
# the existing Composer modal and parameter core.
def focus_card_rect(anchor, viewport, avoid=(), *, selected=False, margin=10.0):
    """Place a read-only context card outside protected UI/model rectangles.

    The card is omitted rather than overlapping another window or the Composer.
    Malformed or nonfinite screen geometry is treated as unavailable.
    """
    try:
        ax, ay = map(float, anchor)
        vw, vh = map(float, viewport)
        margin = float(margin)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (ax, ay, vw, vh, margin))):
        return None
    w, h = (194.0, 46.0) if selected else (164.0, 33.0)
    margin = max(8.0, margin)
    if vw < w + margin * 2 or vh < h + margin * 2:
        return None
    protected = []
    for item in avoid:
        if item is None:
            continue
        try:
            r = tuple(map(float, item))
        except (TypeError, ValueError):
            continue
        if len(r) == 4 and all(map(math.isfinite, r)) and r[2] > 0 and r[3] > 0:
            protected.append(r)
    offsets = (
        (19, 15), (-w-19, 15), (19, -h-15), (-w-19, -h-15),
        (-w/2, 26), (-w/2, -h-26), (25, -h/2), (-w-25, -h/2),
    )
    for dx, dy in offsets:
        x, y = ax+dx, ay+dy
        r = (x, y, w, h)
        if x < margin or y < margin or x+w > vw-margin or y+h > vh-margin:
            continue
        if any(_intersects(r, blocker, 5.0) for blocker in protected):
            continue
        return r
    return None


def composer_hover_help(key):
    """Non-invasive help text for the Composer status rail; no side effects."""
    key = str(key or '')
    help_by_action = {
        'HUD_DRAG': 'ПЕРЕМЕЩЕНИЕ · тяните заголовок мышью; Esc отменяет перенос',
        'HUD_PIN': 'P / F · закрепить здесь или разрешить перемещение',
        'HUD_RESET': 'RST · вернуть исходное автоматическое размещение',
        'HEALTH_TOGGLE': 'HEALTH · сведения о состоянии и связях окна',
        'HEALTH_LOCATE': 'ПОКАЗАТЬ · выделить связанные элементы',
        'HEALTH_RETRY': 'ПОВТОРИТЬ · восстановление Interior по запросу',
        'CLOSE': 'ЗАКРЫТЬ · завершить Composer, сохранив модель',
        'EXPAND': 'ПАРАМЕТРЫ · развернуть настройки окна',
        'COLLAPSE': 'КОМПАКТНО · свернуть настройки',
        'PAGE_FRAME': 'FRAME · рама и импосты',
        'PAGE_INTERIOR': 'BOX · параметры внутреннего контура',
        'PAGE_GLASS': 'GLASS · стеклянная система',
        'ADD_V': '+ V · добавить вертикальное разделение',
        'ADD_H': '+ H · добавить горизонтальное разделение',
        'ZONE_ADD_V': '+ V · разделить выбранную зону вертикально',
        'ZONE_ADD_H': '+ H · разделить выбранную зону горизонтально',
        'MULLION_EDIT': 'ИМПОСТ · точное положение и толщина',
        'ALIGN_SEGMENTS': 'ВЫРОВНЯТЬ · выровнять совместимые импосты',
        'SPLIT_DONE': 'ГОТОВО · закрыть параметры импоста',
        'SPLIT_GLOBAL': 'ОБЩАЯ · применить текущую общую толщину к выбранному импосту',
        'MORE_FRAME': 'ДОПОЛНИТЕЛЬНО · показать параметры рамы',
        'CORNER_AUTO': 'AUTO · автоматическое правило углового стыка',
        'CORNER_ON': 'ON · центральный импост доступен только с внутреннего угла 70°',
        'CORNER_OFF': 'OFF · отключить угловой профиль',
    }
    if key == 'FIELD:INTERNAL_WIDTH':
        return 'РАМЫ / НОВЫЕ · общая толщина / угловой профиль; созданные импосты меняются отдельно'
    if key.startswith('FIELD:'):
        return 'ЗНАЧЕНИЕ · клик для точного ввода, Enter — подтвердить'
    return help_by_action.get(key, '')
