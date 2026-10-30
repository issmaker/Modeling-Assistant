"""ISSMAKER Viewport skin VIS-200-B — unified floating HUD presentation contract.

Do not import Blender, GPU, or nested tools here. Renderers own their positions,
original hitboxes, keyboard state, edit buffers, and geometry algorithms.
This module provides only paint/typography tokens derived from the existing
ThemeSnapshot; diagnostic red/orange/marker colours are intentionally separate.
"""
from __future__ import annotations
from functools import lru_cache
from .workspace_components import component_visual
from .hud_readability import readable_neutral

# Logical pixels BEFORE each tool's existing display/user scale transform.
TEXT_PX = {'caption': 10, 'control': 11, 'field': 11, 'body': 12, 'title': 13}
CORNER_PX = {'panel': 9., 'button': 6., 'field': 5., 'tab': 6.}
BORDER_PX = 1.


def text_px(role):
    """Semantic font size; painters keep their own established scaling."""
    if role not in TEXT_PX:
        raise ValueError('Unknown text role')
    return TEXT_PX[role]


@lru_cache(maxsize=128)
def _cached_ink(kind, source, panel):
    return readable_neutral(source, panel, 7. if kind == 'primary' else 4.5)


def ink(theme=None, *, kind='primary'):
    """Readable neutral HUD text; not used for geometry or warning colours.

    Cache resolves repeated control labels without frame-time contrast searches.
    """
    if kind not in {'primary', 'muted', 'accent'}:
        raise ValueError('Unknown ink role')
    panel=tuple(getattr(theme, 'panel', (.025, .032, .036)))
    if kind == 'accent':
        # Accent is a decoration AND sometimes title text. Only the text
        # version is corrected for contrast; marker colours remain untouched.
        accent=tuple(getattr(theme, 'accent', (.31, .79, .66)))+(1.,)
        return _cached_ink('muted',accent,panel)
    source=((.91, .95, .94, 1.) if kind == 'primary'
            else tuple(getattr(theme, 'subtitle', (.66, .76, .76))) + (1.,))
    return _cached_ink(kind, source, panel)


def component(part='button', state='normal', role='neutral', theme=None):
    """One visual contract for UI controls; input ownership remains local."""
    return component_visual(part, state, role, theme)


def control_ink(theme=None, *, part='button', state='normal', role='neutral'):
    """Accessible foreground for the *actual control fill*, not just panel.

    Does not affect control background, diagnostics, hitboxes or input buffers.
    The colour for disabled controls remains semantically muted but readable.
    """
    visual=component(part,state,role,theme)
    # Focus/hover emphasis should stay readable even for very dark custom
    # accent colours; never substitute this for geometry/warning markers.
    return _cached_ink('muted',visual.foreground,visual.background)


def control_radius(part='button', scale=1.):
    """Shared visual radius. Painters clamp it to the drawn rectangle."""
    from math import isfinite
    if part not in CORNER_PX:raise ValueError('Unknown control part')
    try: scale=float(scale)
    except (ValueError,TypeError,OverflowError): scale=1.
    if not isfinite(scale) or scale<=0:scale=1.
    return CORNER_PX[part]*scale


def shell(theme=None):
    card=component_visual('card',theme=theme)
    header=component_visual('header',theme=theme)
    return {'panel':card.background, 'border':card.border,
            'header':header.background,'title':ink(theme),
            'muted':ink(theme,kind='muted'), 'accent':ink(theme,kind='accent')}


def semantic_ink(theme=None, *, role='warning'):
    """Readable semantic *text* on a panel (never geometry/marker colours).

    Unlike the diagnostic palette, textual warning/error captions may be
    lightened to meet contrast without changing their warning/danger hue.
    """
    from .design_tokens import ORANGE, RED
    if role not in {'warning','danger','success'}:
        raise ValueError('Unknown semantic text role')
    color={'warning':ORANGE, 'danger':RED,
           'success':tuple(getattr(theme,'accent',(.31,.79,.66)))+(1.,)}[role]
    panel=tuple(getattr(theme,'panel',(.025,.032,.036)))
    return _cached_ink('muted',color,panel)
