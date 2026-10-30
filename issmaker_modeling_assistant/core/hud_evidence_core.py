"""ISSMAKER HUD Evidence Lab — pure audit primitives (no Blender imports).

Never treat a simulated rectangle as proof of an actual Blender click.
All unobserved manual checks stay NOT_TESTED.
"""
from __future__ import annotations
from math import isfinite

PROFILES = {
    '1K': (1920, 1080),
    '2K': (2560, 1440),
    '4K': (3840, 2160),
}
STATES = {'NOT_TESTED', 'PASS', 'FAIL', 'BLOCKED'}


def profile_fit(width, height, target, tolerance=.10):
    """Compare actual Blender window dimensions, not physical monitor pixels."""
    if target not in PROFILES:
        return False
    try:
        w, h = float(width), float(height)
        if not isfinite(w) or not isfinite(h) or w <= 0 or h <= 0:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    x, y = PROFILES[target]
    return abs(w - x) <= x * tolerance and abs(h - y) <= y * tolerance


def intersect(a, b):
    """Rectangles are xyxy. Touching edges are not collisions."""
    try:
        ax1, ay1, ax2, ay2 = map(float, a)
        bx1, by1, bx2, by2 = map(float, b)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(isfinite(v) for v in (ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)):
        return False
    if ax2 <= ax1 or ay2 <= ay1 or bx2 <= bx1 or by2 <= by1:
        return False
    return ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2


def potential_collisions(snapshots, fixed=None):
    """Return candidate overlaps; these are NOT automatic FAIL or proof of occlusion.

    Receives window_visibility.snapshot_all()/fixed_snapshot(). Ranges use
    the same viewport region-local coordinates. Ignore hidden/expired entries.
    """
    fixed = fixed or {}
    collisions = []
    for scope, rows in sorted((snapshots or {}).items()):
        visible = []
        for row in rows:
            if row.get('hidden') or row.get('status') != 'VISIBLE' or row.get('rect') is None:
                continue
            try:
                seconds = float(row.get('seconds_ago', 999))
            except (TypeError, ValueError):
                continue
            if seconds > 2.5:
                continue
            visible.append((str(row.get('key', '')), row['rect']))
        for row in fixed.get(scope, ()):
            if row.get('rect') is not None and float(row.get('seconds_ago', 999)) <= 2.5:
                visible.append((str(row.get('key', '')), row['rect']))
        for i, (ka, ra) in enumerate(visible):
            for kb, rb in visible[i+1:]:
                if ka == kb:
                    continue
                if intersect(ra, rb):
                    collisions.append({'scope': scope, 'first': ka, 'second': kb, 'rects': [list(ra), list(rb)]})
    return collisions


def evidence_summary(steps, results):
    counts = {s: 0 for s in sorted(STATES)}
    for step in steps:
        code = step['id']
        state = results.get(code, {}).get('status', 'NOT_TESTED')
        if state not in STATES:
            state = 'NOT_TESTED'
        counts[state] += 1
    # Never release based solely on static checks, manual PASS, or missing 1K/2K/4K.
    return {'counts': counts, 'total': len(steps), 'complete': counts['NOT_TESTED'] == 0 and counts['BLOCKED'] == 0,
            'blender_lab': True, 'release_verified': False,
            'reason': 'Manual observations and screenshot evidence require independent review; no automatic release approval.'}


def validate_step_id(steps, step_id):
    return str(step_id) in {s['id'] for s in steps}
