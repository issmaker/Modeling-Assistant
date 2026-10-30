"""Blender-independent secondary-wall control point settings (Pass 50).

The field is a delta from the *derived* offset shell, never from the authored
Path object. JSON keys are authored stable corner UIDs.  Missing = AUTO.
Every present entry is MANUAL and survives changes to the primary wall.
"""
import json
from math import isfinite

_FIELDS = ('dx', 'dy', 'dz', 'dh')
_LIMIT = 10000.0


def parse_secondary_points(payload):
    try:
        data = json.loads(payload or '{}')
    except (TypeError, ValueError) as exc:
        raise ValueError('Дополнительная стена: повреждены точки JSON') from exc
    if not isinstance(data, dict) or len(data) > 100000:
        raise ValueError('Дополнительная стена: неверная таблица UID точек')
    result = {}
    for key, spec in data.items():
        try:
            uid = int(key)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('Дополнительная стена: неверный UID') from exc
        if uid <= 0 or str(uid) != key or not isinstance(spec, dict):
            raise ValueError('Дополнительная стена: UID или структура точки неверны')
        if set(spec) - set(_FIELDS):
            raise ValueError('Дополнительная стена: неизвестная настройка точки')
        point = {}
        for name in _FIELDS:
            try:
                raw = spec.get(name, 0.0)
                if isinstance(raw, (bool, list, dict)):
                    raise TypeError('wrong type')
                number = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('Дополнительная стена: координата не число') from exc
            if not isfinite(number) or abs(number) > _LIMIT:
                raise ValueError('Дополнительная стена: координата вне допустимого диапазона')
            point[name] = number
        result[str(uid)] = point
    return result


def assign_secondary_points(points, uid_positions, overrides, *, tolerance=0.0002):
    """Return an exact per-station control field. Closed seam repeats first.

    Coordinates are those of the original authored path, BEFORE offsetting or
    selecting a safe closed seam. Matching may not silently choose two UIDs.
    """
    tolerance2 = tolerance*tolerance
    if len(uid_positions) > 100000:
        raise ValueError('Слишком много UID точек')
    matched = []
    for p in points:
        uids = [uid for uid, q in uid_positions.items()
                if (float(p.x)-float(q.x))**2+(float(p.y)-float(q.y))**2+
                   (float(p.z)-float(q.z))**2 <= tolerance2]
        if len(uids) != 1:
            raise ValueError('Дополнительная стена: неоднозначное соответствие UID')
        matched.append(dict(overrides.get(str(uids[0]), dict.fromkeys(_FIELDS, 0.0))))
    return matched


def resolve_secondary_points(path_obj, authored_points, payload):
    from ..ui.settings import sync_corner_uids
    mapping=sync_corner_uids(path_obj)
    positions={uid: data[1] for uid,data in mapping.items()}
    overrides=parse_secondary_points(payload)
    if set(overrides)-{str(uid) for uid in mapping}:
        raise ValueError('Дополнительная стена: точки с устаревшими UID, восстановите соответствие')
    return assign_secondary_points(authored_points, positions, overrides)


def shifted_points(points, settings, vector_type):
    """Pure construction of a secondary shell; preserve primary points."""
    if len(points)!=len(settings):
        raise ValueError('Дополнительная стена: число управляющих точек не совпадает')
    # Offsets are always derived in XY. Historical dz/dh are read only for
    # backwards-compatible JSON validation; neither changes actual geometry.
    return [vector_type((p.x+v['dx'], p.y+v['dy'], p.z))
            for p,v in zip(points,settings)]


def selected_endcaps(create_inner, is_closed, enabled, start_enabled, finish_enabled):
    """Actual open-end cap flags; never produce caps on closed or single paths."""
    active = bool(create_inner) and not bool(is_closed) and bool(enabled)
    return (active and bool(start_enabled), active and bool(finish_enabled))
