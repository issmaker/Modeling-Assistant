"""Pure non-mutating orientation rules for DWOS COPY layouts.

Positive horizontal axis = existing authored local order.  A receiving frame
whose horizontal axis points backwards in world space needs a horizontal copy
reflection to retain the same *world-facing* mullion order.
"""
from __future__ import annotations

import copy
import math


def _direction(vector):
    try:
        v = tuple(float(x) for x in vector)
        if len(v) != 3 or not all(math.isfinite(x) for x in v):
            return None
        d = math.sqrt(sum(x*x for x in v))
        return tuple(x/d for x in v) if d > 1e-12 else None
    except (ValueError, TypeError):
        return None


def needs_horizontal_reflection(source_x, target_x, source_up, target_up, *, axis_threshold=0.96):
    """Only reverse near-antiparallel horizontal axes with aligned up vectors.

    Perpendicular and ambiguous orientations retain legacy local semantics.
    The caller can reject unsupported topology before mutating Blender state.
    """
    sx, tx, su, tu = map(_direction, (source_x, target_x, source_up, target_up))
    if any(v is None for v in (sx, tx, su, tu)):
        return False
    return (sum(a*b for a,b in zip(sx, tx)) <= -float(axis_threshold)
            and sum(a*b for a,b in zip(su, tu)) >= float(axis_threshold))


def mirror_zone_tree(tree):
    """Mirror zone layout around its vertical centre, retaining authored IDs.

    For V nodes mirror the split ratio and exchange left/right children. For H
    nodes preserve vertical order but recursively reflect both subtrees.
    Source tree is never modified.
    """
    clone = copy.deepcopy(tree)
    def visit(node):
        if not isinstance(node, dict):
            raise ValueError('Повреждено дерево импостов для COPY')
        axis = node.get('axis')
        if axis is None:
            return
        children = node.get('children')
        if axis not in ('V','H') or not isinstance(children,list) or len(children)!=2:
            raise ValueError('Неподдерживаемое дерево импостов для отражения')
        if axis == 'V':
            ratio = float(node['ratio'])
            if not math.isfinite(ratio) or not 0.0 < ratio < 1.0:
                raise ValueError('Неверная доля импоста при отражении COPY')
            node['ratio'] = 1.0 - ratio
            node['children'] = [children[1], children[0]]
        for child in node['children']:
            visit(child)
    visit(clone)
    return clone


def mirror_split_rows(rows, width, parse_meter, format_meter, *, source_width=None):
    """Reflect legacy V stations by relative span in independent target copy."""
    width = float(width)
    source_width = width if source_width is None else float(source_width)
    if not all(math.isfinite(w) and w > 0 for w in (width,source_width)):
        raise ValueError('Неверная ширина исходного окна или получателя для COPY')
    output=copy.deepcopy(list(rows))
    for row in output:
        if row['axis']=='V':
            pos=float(parse_meter(row['position_text']))
            if not math.isfinite(pos) or pos < 0 or pos > source_width:
                raise ValueError('Неизвестное положение импоста в раскладке')
            row['position_text']=format_meter(width*(1.0-pos/source_width))
    return output
