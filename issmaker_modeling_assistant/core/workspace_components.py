"""ISSMAKER 1.8.8.27 / shared GPU-HUD component contract (Pass 14.1).

Blender-free: no bpy/gpu/tools imports.  This is a PRESENTATION policy,
not a new input/modal controller.  Painters keep exactly their old hitboxes
and edit buffers; the contract supplies coherent colours, roles and sizing.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from .design_tokens import mix_color

PARTS = frozenset({'button', 'tab', 'field', 'header', 'card'})
STATES = frozenset({'normal', 'hover', 'active', 'disabled', 'focused', 'open', 'quiet'})
ROLES = frozenset({'neutral', 'primary', 'warning', 'danger'})

@dataclass(frozen=True)
class ComponentVisual:
    background: tuple
    border: tuple
    foreground: tuple
    accent: tuple
    interactive: bool


def _finite_rgb(value, default):
    try:
        result = tuple(float(v) for v in value[:3])
        if len(result) != 3 or not all(isfinite(v) and 0 <= v <= 1 for v in result):
            raise ValueError('Invalid RGB')
        return result
    except (TypeError, ValueError, OverflowError, AttributeError):
        return default


@lru_cache(maxsize=384)
def _resolve(part, state, role, panel, header, border, accent, subtitle):
    if role == 'warning': ink = (.94, .66, .34)
    elif role == 'danger': ink = (.94, .42, .42)
    else: ink = accent
    # Neutral panels are deliberately matte: decorative patterns stay behind
    # opaque text fields, rather than washing out essential labels.
    surface = mix_color((*panel, 1.), (*header, 1.), .40 if part in {'field','header'} else .23)
    outline = (*border, .88)
    selected = state in {'active','focused'}
    hover = state == 'hover'
    if state == 'disabled':
        return ComponentVisual(mix_color(surface, (*panel,1.), .53),
                               (*border,.44),(*subtitle,.58),(*ink,.54),False)
    if selected:
        amount = .19 if part in {'button','tab'} else .095
        return ComponentVisual(mix_color(surface, (*ink,1.), amount),
                               mix_color(outline, (*ink,1.), .72),
                               (.94,.98,.97,1.),(*ink,1.),True)
    if hover:
        return ComponentVisual(mix_color(surface, (*ink,1.), .08),
                               mix_color(outline, (*ink,1.), .37),
                               (.92,.96,.96,1.),(*ink,1.),True)
    if state == 'open':
        return ComponentVisual(mix_color(surface, (*ink,1.), .038),
                               mix_color(outline, (*ink,1.), .23),
                               (.86,.93,.92,1.),(*ink,1.),True)
    if state == 'quiet':
        return ComponentVisual(mix_color(surface, (*panel,1.), .24),
                               (*border,.57),(*subtitle,1.),(*ink,.72),True)
    return ComponentVisual(surface,outline,
                           (.87,.93,.92,1.) if part=='field' else (*subtitle,1.),
                           (*ink,1.),True)


def component_visual(part='button', state='normal', role='neutral', theme=None):
    """Unified matte style. Any semantic danger/warning remains distinguishable.

    ``theme`` may be the ThemeSnapshot; never store bpy objects in this cache.
    Pure snapshots also avoid Pass08's circular import on add-on enable.
    """
    if part not in PARTS or state not in STATES or role not in ROLES:
        raise ValueError('Unknown HUD component, state or role')
    if theme is None:
        panel=(.025,.032,.036); header=(.034,.044,.048)
        border=(.17,.25,.26); accent=(.31,.79,.66); subtitle=(.72,.79,.80)
    else:
        panel=_finite_rgb(getattr(theme,'panel',()),(.025,.032,.036))
        header=_finite_rgb(getattr(theme,'header',()),(.034,.044,.048))
        border=_finite_rgb(getattr(theme,'border',()),(.17,.25,.26))
        accent=_finite_rgb(getattr(theme,'accent',()),(.31,.79,.66))
        subtitle=_finite_rgb(getattr(theme,'subtitle',()),(.72,.79,.80))
    return _resolve(part,state,role,panel,header,border,accent,subtitle)


def inline_field_rect(x, y, width, row_height, *, scale=1., label_fraction=.45,
                      gutter=8., horizontal=True):
    """Return (label_bounds,field_bounds) in xyxy without layout overlap.

    For tight widths, the caller should use the existing vertical row layout;
    this helper never creates a clickable region with a negative dimension.
    """
    vals=(x,y,width,row_height,scale,label_fraction,gutter)
    try:
        x,y,width,row_height,scale,label_fraction,gutter=map(float,vals)
    except (ValueError,TypeError,OverflowError):return None
    if not all(isfinite(v) for v in (x,y,width,row_height,scale,label_fraction,gutter)):
        return None
    if scale<=0 or width<=0 or row_height<20*scale or gutter<0:
        return None
    if horizontal:
        if width<260*scale:return None
        fraction=max(.30,min(.60,label_fraction))
        pivot=x+width*fraction
        a=(x,y,pivot-gutter*scale,y+row_height)
        b=(pivot,y,x+width,y+row_height)
    else:
        if row_height<52*scale:return None
        pivot=y+row_height*.48
        a=(x,pivot+gutter*scale,x+width,y+row_height)
        b=(x,y,x+width,pivot)
    if a[2]<=a[0] or b[2]<=b[0] or a[3]<=a[1] or b[3]<=b[1]:return None
    return a,b


def compatibility_metrics(font_scale=1., window_scale=1.):
    """One legal pair of multipliers, no second Blender-DPI multiplication."""
    def sane(v):
        try: v=float(v)
        except (ValueError,TypeError,OverflowError):return 1.
        return min(2.,max(.75,v)) if isfinite(v) else 1.
    font=sane(font_scale);window=sane(window_scale)
    return {'font':font,'window':max(font,window),
            'control_min':25*max(font,window),'gap':8*max(font,window)}


def diagnostics():
    """JSON-safe cache snapshot for one-click Support ZIP / UI Debug."""
    info=_resolve.cache_info()
    return {'contract':'UNIFIED_VISUAL_KIT_1510', 'cache_entries':info.currsize,
            'cache_hits':info.hits,'cache_misses':info.misses,
            'cache_capacity':info.maxsize,
            'parts':sorted(PARTS),'states':sorted(STATES),
            'visual_rhythm':'COMPACT/STANDARD/DETAILED',
            'semantic_renderer':'studio_gpu_components.draw_component'}


def release():
    """Drop pure presentation caches on add-on unregister / UI reload."""
    _resolve.cache_clear()


# Pass 15.10: optical rhythm for real native GPU panels. These values are
# presentation-only, and never resize a legacy hitbox behind its controller.
@dataclass(frozen=True)
class VisualRhythm:
    density: str
    row_px: float
    header_px: float
    gap_px: float
    padding_px: float
    radius_px: float
    font_px: float


def visual_rhythm(part='button', *, density='STANDARD', window_scale=1.,
                  font_scale=1.):
    """Read-only component spec used by adaptive preview and HUD audits.

    Never shrink editable rows below the measured text line. Controllers may
    use the result only after committing the matching draw/hit rectangle.
    """
    if part not in PARTS or density not in {'COMPACT','STANDARD','DETAILED'}:
        raise ValueError('Unknown component or density')
    def sane(v):
        try: v=float(v)
        except (ValueError,TypeError,OverflowError):return 1.
        return min(2.,max(.75,v)) if isfinite(v) else 1.
    font=sane(font_scale)
    scale=max(font,sane(window_scale))
    factor={'COMPACT':.88,'STANDARD':1.,'DETAILED':1.15}[density]
    height={'button':32.,'tab':31.,'field':32.,'header':35.,'card':36.}[part]
    row=max((height*factor),24.*font)*scale
    return VisualRhythm(density,row,35.*scale,8.*factor*scale,
                        12.*factor*scale,{'button':6.,'tab':6.,
                        'field':5.,'header':8.,'card':9.}[part]*scale,
                        {'button':11.,'tab':11.,'field':12.,'header':13.,'card':12.}[part]*font)
