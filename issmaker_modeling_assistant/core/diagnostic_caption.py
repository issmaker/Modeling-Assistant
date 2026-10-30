"""Read-only diagnostic badges shared by UV Editor, 3D UV and Brick Hole.

Pure layout logic, independent of Blender and GPU. These badges are neither
hit targets nor geometry markers. Semantic line/fill colors remain owned by
individual diagnostics; only the text/card receives ISSMAKER styling.
"""
from __future__ import annotations

from math import isfinite
from .hud_readability import fit_caption


def _valid_rect(rect):
    try:
        x0, y0, x1, y1 = (float(v) for v in rect)
        return all(isfinite(v) for v in (x0, y0, x1, y1)) and x1 > x0 and y1 > y0
    except (TypeError, ValueError, OverflowError):
        return False


def overlap(a, b, gap=4.0):
    """True for peer labels/window bounds within a small legibility gap."""
    if not _valid_rect(a) or not _valid_rect(b):
        return False
    return (a[0] < b[2] + gap and a[2] > b[0] - gap
            and a[1] < b[3] + gap and a[3] > b[1] - gap)


def layout_caption(text, viewport, anchor, measure, *, scale=1.0,
                   location='near', obstacles=(), placed=(), margin=8.0,
                   max_width_ratio=.62):
    """Return (xywh, readable_text) or None; never overflow/hide a peer control.

    `measure` measures actual rendered BLF text in pixels at the caller's
    current font size; no character-count guesses. `obstacles` and `placed`
    are WINDOW-region xyxy bounds. No reservation/event state is changed.
    """
    try:
        width, height = (float(v) for v in viewport)
        ax, ay = (float(v) for v in anchor)
        s = float(scale)
        pad = float(margin)
        fraction = float(max_width_ratio)
        if (not all(isfinite(v) for v in (width,height,ax,ay,s,pad,fraction))
                or min(width,height) < 100 or s <= 0 or pad < 0
                or not .15 <= fraction <= 1):
            return None
        # The badge's geometry matches rendered text size but does not exceed
        # the actual WINDOW region even if Blender has a very high DPI scale.
        padding_x = max(9., 12.*s)
        badge_height = max(24., 27.*s)
        max_badge_width = min(420.*s, width*fraction, width-2*pad)
        text_space = max_badge_width - 2*padding_x - 5.*s
        if text_space < 24:
            return None
        visible = fit_caption(str(text), text_space, measure)
        if not visible:
            return None
        tw = float(measure(visible))
        if not isfinite(tw) or tw < 0:
            return None
        bw = min(max_badge_width, max(50., tw+2*padding_x+5.*s))
        bh = min(badge_height, height-2*pad)
        if bh < 21:
            return None
        if location == 'top_left':
            choices = ((pad+8.,height-bh-pad-8.),
                       (width-bw-pad-8.,height-bh-pad-8.))
        elif location == 'near':
            offset = 12.*s
            choices = ((ax+offset, ay+offset),
                       (ax-bw-offset, ay+offset),
                       (ax+offset, ay-bh-offset),
                       (ax-bw-offset, ay-bh-offset))
        else:
            raise ValueError('Unknown caption location')
        occupied = tuple(rect for rect in (*obstacles, *placed) if _valid_rect(rect))
        for x,y in choices:
            candidate=(x,y,x+bw,y+bh)
            if x < pad or y < pad or x+bw > width-pad or y+bh > height-pad:
                continue
            if any(overlap(candidate, peer) for peer in occupied):
                continue
            return ((x,y,bw,bh), visible)
    except (TypeError,ValueError,OverflowError,RuntimeError):
        return None
    return None


def draw_caption(painter, rect, value, theme, *, size=11, semantic=(1.,.45,.25,1.)):
    """Style only; caller owns a live Painter/GPU context and its cleanup."""
    from . import viewport_skin
    x,y,w,h=(float(v) for v in rect)
    card=viewport_skin.component('card',theme=theme)
    painter.box(x,y,w,h,card.background,card.border,rad=viewport_skin.control_radius('panel'))
    # Compact indicator carries the *original* diagnostic meaning (error,
    # UV flip, stretch, preview). Never reinterpret it as the user's UI accent.
    painter.box(x+5,y+5,3,max(1.,h-10),tuple(semantic),rad=1.)
    label_color=viewport_skin.ink(theme,kind='primary')
    painter.text(value,x+15,y+(h-max(9,int(size)))*.5+2,
                 size=max(9,int(size)),color=label_color,width=max(0.,w-23))
