"""Blender-free authoritative mullion field consistency contracts.

No reads from UI caches and no mutations.  A CORNER branch row, a zone row,
and a legacy split are not interchangeable data sources.
"""
from __future__ import annotations
import math
try:
    from .numeric import format_meter
except ImportError:
    from numeric import format_meter


def split_field_value(row, field, width, height):
    """Read a canonical layout row; refuse nonexistent, stale, or invalid ids."""
    if not isinstance(row, dict) or not row.get('id'):
        raise ValueError('Импост не найден; обновите выбор и попробуйте ещё раз')
    if field not in {'TREE_POSITION', 'TREE_THICKNESS'}:
        raise ValueError('Неизвестное поле импоста')
    axis = row.get('axis')
    if axis not in {'V', 'H'}:
        raise ValueError('Неверная ось импоста')
    try:
        position = float(row['position'])
        thickness = float(row['thickness'])
        w, h = float(width), float(height)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Параметры импоста недоступны') from exc
    if not all(map(math.isfinite, (position, thickness, w, h))) or min(w, h, thickness) <= 0:
        raise ValueError('Недопустимый размер импоста')
    extent = w if axis == 'V' else h
    if not (-extent/2 < position < extent/2):
        raise ValueError('Позиция импоста за пределами проёма')
    if thickness >= extent:
        raise ValueError('Толщина импоста превышает размер проёма')
    if field == 'TREE_THICKNESS':
        return format_meter(thickness)
    return format_meter(position + extent/2)


def verify_unique_rows(rows, *, branch_mode=False):
    """Report structural inconsistencies, without assuming physical mesh validity."""
    errors=[]; seen=set()
    for row in rows:
        sid = str(row.get('id','')) if isinstance(row,dict) else ''
        if not sid or sid in seen:
            errors.append(('DUPLICATE_OR_EMPTY_ID',sid))
            continue
        seen.add(sid)
        if branch_mode and (not row.get('branch_id') or not sid.startswith(str(row['branch_id'])+'|')):
            errors.append(('BRANCH_ID_MISMATCH',sid))
        try:
            axis=row['axis'];pos=float(row['position']);thick=float(row['thickness'])
            valid=(axis in {'H','V'} and math.isfinite(pos) and math.isfinite(thick) and thick>0)
        except (KeyError,TypeError,ValueError):
            valid=False
        if not valid:
            errors.append(('INVALID_SPLIT_DIMENSIONS',sid))
    return tuple(errors)
