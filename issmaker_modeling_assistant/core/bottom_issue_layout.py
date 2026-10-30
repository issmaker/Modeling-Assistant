"""PPA Waves 11/12: pixel-space placement for issue HUD.

No Blender imports. Caller supplies the legacy HUD layout (which already avoids
main tool docks); drawing and native gizmos consume the identical returned data.
"""
from __future__ import annotations
import math

POSITIONS = frozenset(('CENTER', 'LEFT', 'RIGHT', 'CUSTOM'))


def shift_layout(base, width, height, position='CENTER', margin=12.0, *, custom_x=.5, custom_y=.18):
    """Translate immutable legacy layout; return None if unsafe/no room.

    Does not multiply Blender UI scale twice: base already uses tool HUD scale.
    The translation is uniform for both visual GPU and Blender hit proxies.
    """
    if not base or position not in POSITIONS:
        return None
    try:
        width, height, margin = float(width), float(height), float(margin)
        scale = float(base['scale'])
        panel = tuple(float(v) for v in base['panel'])
        buttons = tuple(tuple(float(v) for v in item) for item in base['buttons'])
        label = tuple(float(v) for v in base['label_anchor'])
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    if (not all(math.isfinite(v) for v in (width, height, margin, scale, *panel, *label,
                                           *(v for item in buttons for v in item)))
        or not (width > 0 and height > 0 and margin >= 0 and scale > 0)
        or len(buttons) != 4 or len(panel) != 4 or len(label) != 2):
        return None
    x0, y0, x1, y1 = panel
    if x1 <= x0 or y1 <= y0:
        return None
    # Exclusive to the unified issue HUD: a true, disjoint native grip above
    # the existing four action disks. Do not alter shared legacy HUDs.
    old_top = y1
    y1 += 24.0 * scale
    grip = ((x0+x1)*.5, old_top + 12.0*scale, 106.0*scale, 18.0*scale)
    panel_width = x1 - x0
    if position == 'CUSTOM':
        try:
            ux, uy = float(custom_x), float(custom_y)
        except (TypeError, ValueError, OverflowError):
            return None
        if not (math.isfinite(ux) and math.isfinite(uy)):
            return None
        # Reserve the upper Viewport for modeling; custom placements stay low.
        max_top = min(height-margin, height*.48)
        if max_top - margin < y1-y0 or width-2*margin < panel_width:
            return None
        desired_x = min(width-margin-panel_width*.5, max(margin+panel_width*.5, width*ux))
        desired_y = min(max_top-(y1-y0)*.5, max(margin+(y1-y0)*.5, height*uy))
        dx = desired_x - (x0+x1)*.5
        dy = desired_y - (y0+y1)*.5
    elif position == 'LEFT':
        dx = (margin + panel_width / 2.0) - (x0 + x1) / 2.0
    elif position == 'RIGHT':
        dx = (width - margin - panel_width / 2.0) - (x0 + x1) / 2.0
    else:
        dx = 0.0
    dy = dy if position == 'CUSTOM' else 0.0
    bounds = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
    if (bounds[0] < margin - 1e-6 or bounds[1] < margin - 1e-6 or
        bounds[2] > width - margin + 1e-6 or bounds[3] > height - margin + 1e-6):
        return None
    shifted = tuple((x + dx, y + dy, size) for x, y, size in buttons)
    shifted_grip = (grip[0]+dx, grip[1]+dy, grip[2], grip[3])
    gx, gy, gw, gh = shifted_grip
    if (gx-gw*.5 < bounds[0]-1e-6 or gx+gw*.5 > bounds[2]+1e-6
            or gy-gh*.5 < bounds[1]-1e-6 or gy+gh*.5 > bounds[3]+1e-6):
        return None
    for bx, by, size in shifted:
        # Strong separation invariant: native grip rectangle cannot cover
        # the bounding box of a navigation button (not merely its circle).
        if (abs(bx-gx)*2 < gw+size and abs(by-gy)*2 < gh+size):
            return None
    for x, y, size in shifted:
        if size <= 0 or x - size*.5 < bounds[0] - 1e-6 or x + size*.5 > bounds[2] + 1e-6:
            return None
        if y - size*.5 < bounds[1] - 1e-6 or y + size*.5 > bounds[3] + 1e-6:
            return None
    return {'panel': bounds, 'buttons': shifted, 'grip': shifted_grip, 'label_anchor': (label[0]+dx, label[1]+dy),
            'scale': scale, 'position': position}


def hit_button(layout, x, y):
    """Pure Oracle matching IMA_GT_NavButton circular native pick contract."""
    if not layout:
        return -1
    try:
        x, y = float(x), float(y)
        if not (math.isfinite(x) and math.isfinite(y)):
            return -1
        for i, (bx, by, size) in enumerate(layout['buttons']):
            if (bx-x)**2 + (by-y)**2 <= (size*0.5)**2:
                return i
    except (TypeError, ValueError, OverflowError, KeyError):
        return -1
    return -1


def hit_grip(layout, x, y):
    """Match the native rectangular 2D grip selection shape exactly."""
    if not layout:
        return False
    try:
        x, y = float(x), float(y)
        gx, gy, w, h = (float(t) for t in layout['grip'])
        return (all(math.isfinite(t) for t in (x,y,gx,gy,w,h))
                and w > 0 and h > 0
                and abs(x-gx) <= w*.5 and abs(y-gy) <= h*.5
                and hit_button(layout, x, y) < 0)
    except (TypeError, ValueError, OverflowError, KeyError):
        return False


def drag_position(layout, mouse_x, mouse_y, region_x, region_y, width, height, offset_x, offset_y):
    """Absolute window mouse -> normalized TARGET panel center without a jump."""
    cursor = normalized_cursor(mouse_x, mouse_y, region_x, region_y, width, height)
    if cursor is None or not layout:
        return None
    try:
        delta_x, delta_y = float(offset_x), float(offset_y)
        if not (math.isfinite(delta_x) and math.isfinite(delta_y)):
            return None
        w, h = float(width), float(height)
        x = cursor[0]*w+delta_x
        y = cursor[1]*h+delta_y
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return (max(0., min(1., x/w)), max(0., min(1., y/h)))
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def snapshot(layout):
    if not layout:
        return {'status': 'NO_ROOM', 'buttons': []}
    return {'status': 'READY', 'position': layout['position'],
            'panel': [round(v, 2) for v in layout['panel']],
            'grip': [round(v, 2) for v in layout['grip']],
            'buttons': [[round(v, 2) for v in row] for row in layout['buttons']]}


def normalized_cursor(mouse_x, mouse_y, region_x, region_y, width, height):
    """Convert absolute Blender window mouse pixels into clamped WINDOW region UV.

    Used only during a user-initiated modal drag; no scene ray casts.
    """
    try:
        x, y, rx, ry, w, h = map(float, (mouse_x,mouse_y,region_x,region_y,width,height))
        if not all(map(math.isfinite, (x,y,rx,ry,w,h))) or w <= 0 or h <= 0:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return (max(0.,min(1.,(x-rx)/w)), max(0.,min(1.,(y-ry)/h)))


def issue_status_label(kind, *, stale=False, dragging=False, locked=False):
    """Priority is truthful issue status before cosmetic or interaction state."""
    if stale:
        return 'УСТАРЕЛО'
    if dragging:
        return 'ПЕРЕМЕЩЕНИЕ'
    if locked:
        return 'ЗАКРЕПЛЕНО'
    return str(kind or 'ПРОБЛЕМЫ')[:32]
