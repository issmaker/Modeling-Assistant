"""Independent inner wall height field, no changes to authored Path geometry.

Overrides are keyed by stable ``uvwall_corner_uid`` (shared by the cornice
markers); they survive path vertex reordering and Blender scene reloads.
Zero field keeps the legacy exact wall builder path.
"""
import json
from bisect import bisect_right
from math import isfinite


def parse_overrides(payload):
    try:
        value = json.loads(payload or '{}')
    except (ValueError, TypeError) as exc:
        raise ValueError('Неправильный JSON индивидуальных высот внутренней стены') from exc
    if not isinstance(value, dict):
        raise ValueError('Высоты внутренней стены должны быть объектом UID→смещение')
    result = {}
    if len(value) > 100000:
        raise ValueError('Слишком много высотных поправок')
    for key, number in value.items():
        try:
            uid = int(key)
            delta = float(number)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Плохой UID или смещение внутренней стены') from exc
        if uid <= 0 or str(uid) != str(key) or not isfinite(delta) or abs(delta) > 10000.0:
            raise ValueError('Недопустимая высотная поправка внутренней стены')
        result[str(uid)] = delta
    return result


def assign_deltas(points, uid_positions, global_delta, overrides, *, tolerance=0.0002):
    """Map world authored XYZ to stable UID; close ring repeats first value."""
    global_delta = float(global_delta)
    if not isfinite(global_delta) or abs(global_delta) > 10000:
        raise ValueError('Недопустимая общая высота внутренней стены')
    result = []
    tolerance2 = tolerance*tolerance
    for p in points:
        matched = [uid for uid, q in uid_positions.items()
                   if (float(p.x)-float(q.x))**2 + (float(p.y)-float(q.y))**2 +
                      (float(p.z)-float(q.z))**2 <= tolerance2]
        if len(matched) != 1:
            raise ValueError('Неоднозначное соответствие UID точек пути; построение остановлено')
        result.append(overrides.get(str(matched[0]), global_delta))
    return result


def resolve_path_deltas(path_obj, path_points, global_delta, raw_overrides):
    from ..ui.settings import sync_corner_uids
    mapping = sync_corner_uids(path_obj)
    uid_positions = {uid: data[1] for uid, data in mapping.items()}
    return assign_deltas(path_points, uid_positions, global_delta,
                         parse_overrides(raw_overrides))


def validate_vertex_heights(heights, num_points):
    if len(heights) != num_points:
        raise ValueError('Высоты внутренней стены не совпадают с количеством вершин')
    result = [float(h) for h in heights]
    if any(not isfinite(h) or h <= 0.002 for h in result):
        raise ValueError('Высота внутренней стены должна быть больше 2 мм в каждой точке')
    return result


def height_at_station(distances, heights, distance):
    """Linear interpolation along each derived path edge, including closed seam."""
    if len(distances) != len(heights) or len(distances) < 2:
        raise ValueError('Несогласованные станции независимой высоты')
    if distance <= distances[0]:
        return heights[0]
    if distance >= distances[-1]:
        return heights[-1]
    i = max(0, min(len(distances)-2, bisect_right(distances, distance)-1))
    span = distances[i+1]-distances[i]
    if span <= 1e-9:
        raise ValueError('Вырожденный участок исходного пути')
    t = (distance-distances[i])/span
    return heights[i]*(1-t) + heights[i+1]*t
