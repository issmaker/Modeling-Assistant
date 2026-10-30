"""ISSMAKER Design System 0.1 — mirrors DWOS native viewport theme.

Only drawing/style values live here. No Blender operators, handlers or mesh logic.
Per-module geometry and window-specific controls remain independent.
"""
# Pass 08: the design system is the dependency root.  This module must never
# import Blender, `tools.*`, or a nested package.  Importing tools here forces
# DWOS native/__init__.py to execute while the design system is initializing,
# which caused Blender's circular import (component_style not yet defined).
# Values match the accepted DWOS viewport_theme palette byte-for-byte.
PANEL_BG = (0.010, 0.014, 0.017, 0.915)
PANEL_SURFACE = (0.030, 0.040, 0.045, 0.94)
PANEL_HOVER = (0.052, 0.067, 0.072, 0.97)
PANEL_BORDER = (0.095, 0.125, 0.135, 0.92)
TEXT = (0.88, 0.91, 0.92, 1.0)
MUTED = (0.47, 0.53, 0.55, 1.0)
MINT = (0.31, 0.79, 0.66, 1.0)
ORANGE = (0.92, 0.60, 0.24, 1.0)
RED = (0.88, 0.32, 0.29, 1.0)
BLUE = (0.39, 0.65, 0.86, 1.0)
SHADOW = (0.0, 0.0, 0.0, 0.20)
SPACING=(4,8,12,16,24)
CORNER_RADIUS=7.0
HUD_ALPHA=0.94

MODULE_LABELS={
    'BRICK_HOLE':'Проёмы', 'COLLISION':'Коллизии',
    'UV_WALL':'Стены', 'DWOS':'Окна и двери',
}


# UI 2.0 / Pass05: shared semantic design rules for every viewport tool.
# DWOS is the color reference, but geometry, hitboxes and modal logic are NOT
# shared here. These helpers are intentionally pure, deterministic and testable.
from math import isfinite

FONT_BASE_PX = {'caption': 10, 'label': 12, 'section': 13, 'title': 14}
COMPONENT_HEIGHT_PX = {'compact': 25, 'normal': 32, 'touch': 38}
SPACING_PX = {'xs': 4, 'sm': 8, 'md': 12, 'lg': 16, 'xl': 24}
CORNER_PX = {'input': 5.0, 'button': 6.0, 'panel': 9.0}
TEXT_PRIMARY = TEXT
TEXT_MUTED = MUTED
ACTION_PRIMARY = MINT
ACTION_WARNING = ORANGE
ACTION_DANGER = RED


def mix_color(a, b, factor):
    """Blend compatible RGBA sequences without silently changing alpha."""
    try:
        a, b = tuple(float(v) for v in a), tuple(float(v) for v in b)
        t = float(factor)
        if len(a) != 4 or len(b) != 4 or not isfinite(t):
            raise ValueError('Invalid RGBA or fraction')
        if not all(isfinite(x) for x in (*a, *b)):
            raise ValueError('Nonfinite RGBA')
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Invalid style color') from exc
    t = max(0.0, min(1.0, t))
    return tuple(a[i]*(1.0-t)+b[i]*t for i in range(4))


def component_style(state='normal', *, role='neutral', theme=None):
    """Semantic visual states; disabled controls must also be non-interactive."""
    if state not in {'normal', 'hover', 'active', 'disabled'}:
        raise ValueError('Unknown UI state')
    if role not in {'neutral', 'primary', 'warning', 'danger'}:
        raise ValueError('Unknown UI role')
    # Each renderer passes a resolved snapshot for live theme updates;
    # semantic warning/error colors remain independent of the appearance.
    accent={'neutral': MINT, 'primary': MINT,
            'warning': ORANGE, 'danger': RED}[role]
    surface=PANEL_SURFACE
    border=PANEL_BORDER
    hover=PANEL_HOVER
    panel=PANEL_BG
    text_color=TEXT
    if theme is not None:
        surface=tuple(theme.header)+(PANEL_SURFACE[3],)
        border=tuple(theme.border)+(PANEL_BORDER[3],)
        panel=tuple(theme.panel)+(PANEL_BG[3],)
        hover=mix_color(surface,tuple(theme.accent)+(1.,),.11)
        if role in {'neutral','primary'}:
            accent=tuple(theme.accent)+(1.,)

    if state == 'disabled':
        return {'bg': mix_color(surface, panel, .55),
                'border': border,
                'text': mix_color(MUTED, panel, .28), 'interactive': False}
    if state == 'hover':
        return {'bg': mix_color(hover, accent, .08),
                'border': mix_color(border, accent, .32),
                'text': text_color, 'interactive': True}
    if state == 'active':
        return {'bg': mix_color(surface, accent, .26),
                'border': mix_color(border, accent, .76),
                'text': text_color, 'interactive': True}
    return {'bg': surface,
            'border': mix_color(border, accent, .20 if role!='neutral' else 0.),
            'text': text_color, 'interactive': True}


def fit_label(value, available_px, measure, *, ellipsis='…'):
    """Return (visible, clipped). For readonly captions only, never edit buffers.

    Backend supplies actual BLF pixel measurements at the rendered font size.
    Binary search bounds work for monotone font metrics; no character-count
    approximation, and return no partial glyph outside bounds.
    """
    text=str(value)
    try:
        cap=float(available_px)
        if not isfinite(cap) or cap<=0: return '', bool(text)
        def width(s):
            w=float(measure(s))
            if not isfinite(w) or w<0: raise ValueError('Invalid width')
            return w
        if width(text)<=cap:return text,False
        if width(ellipsis)>cap:return '',bool(text)
        left,right=0,len(text)
        while left<right:
            mid=(left+right+1)//2
            if width(text[:mid]+ellipsis)<=cap:left=mid
            else:right=mid-1
        return text[:left].rstrip()+ellipsis,True
    except (TypeError, ValueError, OverflowError):
        return '', bool(text)


def ease_out_cubic(progress):
    """Deterministic interruptible tween, no implicit Blender timers."""
    try: x=float(progress)
    except (TypeError,ValueError,OverflowError):x=0.0
    if not isfinite(x): x=0.0
    x=max(0.,min(1.,x))
    return 1.0-(1.0-x)**3
