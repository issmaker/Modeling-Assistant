"""DWOS 3.5.9 classification invariants.

CORNER windows are not allowed to inherit topology-changing options from ordinary
windows.  This module is pure Python so the policy can be regression-tested without
Blender runtime.
"""
from __future__ import annotations

POLICY = 'CORNER_FULL_TWO_GLASS_OPTIONAL_JUNCTION_V2'
CURVED_POLICY = 'CURVED_FULL_DOUBLE_GLAZING_LOCK_V1'
EPS = 1e-9


def enforce_corner_settings(settings, classification):
    """Normalize mandatory CORNER settings and return a change dictionary."""
    if str(classification or '').upper() not in {'CORNER', 'CURVED'}:
        return {}
    changes = {}
    if getattr(settings, 'optimization', None) != 'FULL':
        changes['optimization'] = (getattr(settings,'optimization',None), 'FULL')
        settings.optimization = 'FULL'
    # A curved window is not a hard CORNER: enforce FULL but no Corner Post.
    if str(classification or '').upper() == 'CURVED':
        return changes
    # Preserve the explicit user choice. AUTO remains compatible with the
    # historical integrated corner; OFF is the postless mitred branch joint.
    mode = getattr(settings, 'corner_post_mode', 'AUTO')
    if mode not in {'AUTO', 'ON', 'OFF'}:
        raise ValueError('CORNER junction mode is invalid; rebuilding stopped')
    return changes


def corner_glass_ranges(rect, corner_posts, junction_positions=()):
    """Return S-ranges occupied by physical glass panels after post subtraction."""
    s0,s1 = float(rect[0]), float(rect[1])
    if s1 <= s0:
        return []
    ranges=[(s0,s1)]
    for post in corner_posts or ():
        ps=float(post['s']); half=max(0.0,float(post.get('physical_branch_extent',float(post['thickness'])*0.5)))
        cut0,cut1=ps-half,ps+half
        next_ranges=[]
        for a,b in ranges:
            if cut1<=a+EPS or cut0>=b-EPS:
                next_ranges.append((a,b));continue
            if cut0>a+EPS: next_ranges.append((a,min(b,cut0)))
            if cut1<b-EPS: next_ranges.append((max(a,cut1),b))
        ranges=next_ranges
    # Two independent façade panes must remain distinct without a visible
    # junction profile. This is a zero-width seam, NOT an artificial gap.
    for ss in sorted(set(float(x) for x in junction_positions)):
        new=[]
        for a,b in ranges:
            if a+EPS < ss < b-EPS:
                new.extend(((a,ss),(ss,b)))
            else:
                new.append((a,b))
        ranges=new
    return [(a,b) for a,b in ranges if b-a>EPS]


def validate_corner_glass_contract(classification, optimization, corner_posts, glass_rect, *, junction_positions=()):
    if str(classification or '').upper() != 'CORNER':
        return {'policy':'NOT_CORNER','panel_count':0,'ranges':[]}
    if str(optimization) != 'FULL':
        raise ValueError('CORNER invariant: geometry must always be FULL')
    if len(corner_posts or ()) not in (0,1):
        raise ValueError('CORNER invariant: unsupported number of junction posts')
    if not corner_posts and len(tuple(junction_positions))!=1:
        raise ValueError('CORNER postless mode requires exactly one hard junction')
    ranges=corner_glass_ranges(glass_rect,corner_posts,junction_positions)
    if len(ranges)!=2:
        raise ValueError(f'CORNER invariant: exactly 2 glass panels required, got {len(ranges)}')
    return {'policy':POLICY,'panel_count':2,'ranges':ranges}


def validate_curved_glass_contract(classification, optimization, roles):
    if str(classification or '').upper() != 'CURVED':
        return {'policy': 'NOT_CURVED', 'layer_count': 0}
    if str(optimization) != 'FULL':
        raise ValueError('CURVED invariant: frame must always be FULL')
    actual={r for r in roles if str(r).startswith('GLASS_')}
    expected={'GLASS_EXTERIOR','GLASS_INTERIOR'}
    if actual!=expected:
        raise ValueError(f'CURVED invariant: exactly two glass layers required: {actual}')
    return {'policy': CURVED_POLICY,'layer_count':2}
