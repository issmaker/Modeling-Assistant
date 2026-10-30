"""DWOS 3.7.11: transactional plans for changing a CORNER separator mode.

Pure, no Blender dependencies. Preserve user-authored profile sizes and absolute
positions, rather than silently rescaling branch-local ratio coordinates.
"""
from __future__ import annotations
from math import isfinite
try:
    from .corner_rules import corner_posts
    from .branch_core import decode_state, encode_state, rows_as_dicts, apply_positions
except ImportError:
    from corner_rules import corner_posts
    from branch_core import decode_state, encode_state, rows_as_dicts, apply_positions


def plan_corner_mode_change(topology, raw_state, old_mode, new_mode, *,
                            height, frame_width, internal_width, frame_depth,
                            legacy_zone_tree='', legacy_splits=(),old_published_post_count=None):
    """Return new branch JSON without mutating the current window.

    Cannot rescale positions silently. Reject impossible target clear spans and
    legacy representations that need a separate explicit migration.
    """
    if topology.get('classification')!='CORNER':
        raise ValueError('Junction switch is only available for CORNER')
    if old_mode not in {'AUTO','ON','OFF'} or new_mode not in {'AUTO','ON','OFF'}:
        raise ValueError('Unknown CORNER separator mode')
    # Reject only the requested new ON. An unsupported *old* ON must remain
    # readable so users can safely migrate it to OFF without losing layouts.
    try:
        from .corner_post_policy_core import require_post_allowed
    except ImportError:
        from corner_post_policy_core import require_post_allowed
    require_post_allowed(topology,new_mode)
    if old_mode==new_mode:return str(raw_state or '')
    if legacy_zone_tree or legacy_splits:
        raise ValueError('Старая система импостов: для изменения угла нужна безопасная миграция дерева')
    # Older scenes published a real post under AUTO even on acute angles.
    # Decode authored positions against the old physical post envelope.
    physical_old='ON' if old_mode=='AUTO' and old_published_post_count==1 else old_mode
    old=corner_posts(topology,physical_old,internal_width,frame_depth)
    new=corner_posts(topology,new_mode,internal_width,frame_depth)
    before=decode_state(raw_state,topology)
    before_rows=rows_as_dicts(before,topology,height,frame_width,old)
    if not before_rows:
        # No authored profiles; keep serialized initial representation unchanged.
        return str(raw_state or '')
    after=decode_state(raw_state,topology)
    after_rows=rows_as_dicts(after,topology,height,frame_width,new)
    baseline={r['id']:r for r in before_rows}
    proposed={r['id']:r for r in after_rows}
    if baseline.keys()!=proposed.keys():
        raise ValueError('После смены режима исчезла часть импостов')
    shifts={sid:r['position'] for sid,r in baseline.items()
            if abs(r['position']-proposed[sid]['position'])>1e-7}
    if shifts:
        after=apply_positions(after,topology,height,frame_width,shifts,new)
    final={r['id']:r for r in rows_as_dicts(after,topology,height,frame_width,new)}
    for sid,r in baseline.items():
        got=final[sid]
        if abs(got['position']-r['position'])>1e-6 or abs(got['thickness']-r['thickness'])>1e-8:
            raise ValueError('Размер или позиция импоста изменились: режим угла не применён')
    return encode_state(after)
