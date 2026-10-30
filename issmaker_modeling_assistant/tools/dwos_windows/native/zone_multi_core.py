"""Transactional, Blender-independent multi-zone split staging.

Selection consists of actual leaf IDs from one window.  Every leaf is validated
against the SAME starting state, and no mutation escapes on partial failure.
"""
from __future__ import annotations
import json
try:
    from .zone_core import decode, layout, add
    from .branch_core import layout_state, add_split, validate_post_clearance
except ImportError:
    from zone_core import decode, layout, add
    from branch_core import layout_state, add_split, validate_post_clearance


def normalize_zone_ids(raw):
    ids=json.loads(raw) if isinstance(raw,str) else raw
    # Blender operator JSON -> tuple; staging revalidates the already normalised IDs.
    # Do not reject a valid immutable tuple on this second validation pass.
    if not isinstance(ids,(list,tuple)) or not ids or len(ids)>128:
        raise ValueError('Выберите от 1 до 128 зон')
    if any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
        raise ValueError('Некорректный список зон')
    return tuple(ids)


def toggle_zone(ids,zone,extend=False):
    old=list(ids or ())
    if not extend:return [zone]
    return [i for i in old if i!=zone] if zone in old else old+[zone]


def stage_zone_add_many(raw, ids, axis, thickness, width, height, fw):
    wanted=normalize_zone_ids(ids)
    state=decode(raw)
    old_ids={zid for zid,_ in layout(state,width,height,fw)[0]}
    if any(z not in old_ids for z in wanted):
        raise ValueError('Одна или несколько зон больше не существуют')
    for number,zid in enumerate(wanted,1):
        try:
            state,_=add(state,zid,axis,thickness,width,height,fw)
        except ValueError as exc:
            raise ValueError(f'Зона {number}/{len(wanted)} ({zid}): {exc}') from exc
    layout(state,width,height,fw)
    return state


def stage_branch_add_many(state,topo,ids,axis,thickness,height,fw,posts=()):
    wanted=normalize_zone_ids(ids)
    old_ids={zid for zid,_rect,_bid in layout_state(state,topo,height,fw,posts)[0]}
    if any(z not in old_ids for z in wanted):
        raise ValueError('Одна или несколько CORNER-зон больше не существуют')
    next_state=state
    for number,zid in enumerate(wanted,1):
        try:
            next_state,_=add_split(next_state,topo,height,fw,zid,axis,thickness,posts)
        except ValueError as exc:
            raise ValueError(f'CORNER зона {number}/{len(wanted)} ({zid}): {exc}') from exc
    validate_post_clearance(next_state,topo,height,fw,posts)
    return next_state
