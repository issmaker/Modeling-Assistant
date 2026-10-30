"""Safe, always-FULL viewport layout: no compact launcher.

Small regions show as many tools as geometrically fit, without overlap.
"""
import math


def _round_points(points):
    return [(round(x, 3), round(y, 3)) for x, y in points]


def _line(left, right, count, step, y):
    if count <= 0:
        return []
    center = (left + right) / 2
    return _round_points([(center + (i - (count - 1) / 2) * step, y)
                          for i in range(count)])


def _dimensions(left, right, scale, density, height):
    scale = min(1.3, max(.8, float(scale)))
    size = 34 * scale
    step = max(size + 6, (46 if density == 'COMFORT' else 40) * scale)
    border = size / 2 + 8
    h = max(0, float(height)) if height is not None else 100000.
    y = min(42., h - border)
    return size, step, border, h, y


def _wrapped_safe(left, right, count, step, y, height, border):
    """Fill rows from bottom up, and intentionally suppress unavailable slots."""
    if count <= 0 or right < left or y < border:
        return []
    cols = max(1, int(math.floor((right - left) / step)) + 1)
    available = max(0, int(math.floor((height - border - y) / (step + 6))) + 1)
    if available == 0:
        return []
    result = []
    for index in range(min(count, cols * available)):
        row, col = divmod(index, cols)
        row_len = min(cols, count - row * cols)
        center = (left + right) / 2
        result.append((center + (col - (row_len - 1) / 2) * step,
                       y + row * (step + 6)))
    return _round_points(result)


def viewport_layout(left, right, edit_count, *, density='COMFORT', scale=1.0,
                    editor_dock=True, view_dock=True, height=None,
                    mode='FULL'):
    """Return visual positions (editor, view, edit) without collisions.

    Positions may be fewer than requested if there is not enough space.
    No menu-launcher fallback or overlapping invisible buttons are created.
    """
    result = {'editor': [], 'view': [], 'edit': [], 'wrapped': False,
              'limited': False}
    if right <= left:
        return result
    size, step, border, h, y = _dimensions(left, right, scale, density, height)
    width = right - left
    if width < 2 * border or y < border:
        return result
    if width < 280:
        # No hidden launcher and no overlapping click-targets: use the available
        # strip for tools, not an unrelated fallback menu.
        result['edit'] = _wrapped_safe(left + border, right - border,
                                        edit_count, step, y, h, border)
        result['wrapped'] = bool(result['edit'])
        result['limited'] = len(result['edit']) < edit_count
        return result

    # Three right-side buttons: UCX, View, Tools.
    # One pure coordinate source for Blender Gizmos and passive artwork.
    n_view = 3 if view_dock and width >= 650 else 0
    n_editor = 2 if editor_dock and width >= 800 else 0
    result['editor'] = _round_points([
        (left + border + i * step, y) for i in range(n_editor)])
    result['view'] = _round_points([
        (right - border - (n_view - i - 1) * step, y)
        for i in range(n_view)])

    center_left = (result['editor'][-1][0] + step) if n_editor else left + border
    center_right = (result['view'][0][0] - step) if n_view else right - border
    fits_one = center_right >= center_left and (
        (edit_count - 1) * step <= center_right - center_left)
    if edit_count and fits_one:
        result['edit'] = _line(center_left, center_right, edit_count, step, y)
    elif edit_count:
        # Next row is deliberately above every side button and never overlaps
        # the click regions around bottom-left and bottom-right groups.
        rise_y = y + step + 6
        if rise_y + border <= h:
            result['edit'] = _wrapped_safe(left + border, right - border,
                                           edit_count, step, rise_y, h, border)
        result['wrapped'] = bool(result['edit'])
    result['limited'] = len(result['edit']) < edit_count
    return result


def simple_strip(left, right, count, *, density='COMFORT', scale=1.0,
                 y=42, height=None):
    if right <= left or count <= 0:
        return []
    _, step, border, h, baseline = _dimensions(left, right, scale, density, height)
    baseline = min(float(y), baseline)
    if baseline < border or right - left < 2 * border:
        return []
    l, r = left + border, right - border
    if (count - 1) * step <= r - l:
        return _line(l, r, count, step, baseline)
    return _wrapped_safe(l, r, count, step, baseline, h, border)
