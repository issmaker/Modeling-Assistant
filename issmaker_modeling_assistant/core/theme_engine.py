"""ISSMAKER UI theme contract, Pass 11A.

Pure Python: no bpy, gpu, modal operators or imports from tools. Every tool
consumes the same immutable snapshot; diagnostics/mesh colours remain separate.
"""
from dataclasses import dataclass
from math import isfinite

SCHEMES = ('obsidian', 'lagoon', 'dusk', 'sand', 'cobalt', 'forest', 'plum', 'copper', 'frost')
SURFACES = ('architectural', 'graphite', 'mineral', 'smoke', 'ink')
from .pattern_engine import VISIBLE_IDS as PATTERNS

ACCENTS = {
    'MINT': (.06,.62,.49), 'BLUE': (.12,.43,.88), 'AMBER': (.82,.49,.14),
    'PURPLE': (.51,.33,.78), 'CYAN': (.04,.69,.78), 'CORAL': (.91,.34,.32),
    'LIME': (.56,.72,.17), 'ROSE': (.78,.36,.57),
    'SILVER': (.56,.65,.73), 'GOLD': (.85,.64,.23),
}
# Distinct, subdued base tints. Never used for error or geometry marker colours.
SCHEME_BASES = {
    'obsidian':(.018,.026,.031), 'lagoon':(.020,.044,.046),
    'dusk':(.034,.029,.053), 'sand':(.058,.052,.042),
    'cobalt':(.018,.031,.059), 'forest':(.022,.042,.033),
    'plum':(.043,.028,.046), 'copper':(.052,.036,.030),
    'frost':(.036,.049,.057),
}
SURFACE_STRENGTH = {
    'architectural':1.0, 'graphite':.70, 'mineral':1.18,
    'smoke':1.32, 'ink':.43,
}


def _num(x, default, lo, hi):
    try:
        f=float(x)
        if isfinite(f): return min(hi,max(lo,f))
    except (TypeError,ValueError,OverflowError): pass
    return default


def _rgb(seq, fallback):
    try:
        vals=tuple(float(v) for v in seq)
        if len(vals)==3 and all(isfinite(v) for v in vals):
            return tuple(max(0.,min(1.,v)) for v in vals)
    except (TypeError,ValueError,OverflowError): pass
    return fallback


def rgba(rgb, alpha=1.): return tuple(rgb)+(alpha,)


def mix(a,b,factor):
    t=max(0.,min(1.,float(factor)))
    return tuple(a[i]*(1.-t)+b[i]*t for i in range(3))


@dataclass(frozen=True)
class ThemeSnapshot:
    scheme: str
    surface: str
    pattern: str
    accent: tuple
    secondary: tuple
    panel: tuple
    header: tuple
    border: tuple
    subtitle: tuple
    pattern_strength: float
    pattern_scale: float
    pattern_amplitude: float
    pattern_parallax: float
    pattern_relief: float
    pattern_speed: float
    pattern_density: float
    pattern_opacity: float
    pattern_contrast: float
    pattern_glow: float
    pattern_softness: float
    motion: str
    font_scale: float
    window_scale: float
    render_quality: str='HIGH'

    def uv_palette(self):
        return {
            'panel':rgba(self.panel,.99),
            'header':rgba(self.header,.99),
            'border':rgba(self.border,.96),
            'accent':rgba(self.accent,.95),
            'subtitle':rgba(self.subtitle,1.),
        }


def from_values(values):
    """Resolve a validated snapshot from an untrusted mapping (not a bpy object)."""
    scheme=str(values.get('ui_scheme','obsidian')).lower()
    surface=str(values.get('ui_surface','architectural')).lower()
    pattern=str(values.get('ui_pattern','contours')).lower()
    if scheme not in SCHEMES:scheme='obsidian'
    if surface not in SURFACES:surface='architectural'
    if pattern not in PATTERNS:pattern='contours'
    base=SCHEME_BASES[scheme]
    material=SURFACE_STRENGTH[surface]
    accent=ACCENTS.get(str(values.get('accent','MINT')).upper(),ACCENTS['MINT'])
    if values.get('ui_custom_accent',False):
        accent=_rgb(values.get('ui_accent_color',accent),accent)
    secondary=_rgb(values.get('ui_secondary_color',(.26,.48,.50)),(.26,.48,.50))
    panel=mix(base,(.025,.031,.034),.26)
    panel=tuple(min(1.,v*material) for v in panel)
    border=mix(panel,accent,.19)
    header=mix(panel,secondary,.14)
    subtitle=mix((.46,.54,.55),accent,.26)
    motion=str(values.get('ui_motion_quality','BALANCED')).upper()
    if motion not in {'OFF','BALANCED','RICH'}:motion='BALANCED'
    if bool(values.get('ui_reduced_motion',False)):motion='OFF'
    return ThemeSnapshot(
        scheme,surface,pattern,accent,secondary,panel,header,border,subtitle,
        _num(values.get('ui_pattern_strength',.48),.48,0.,1.),
        _num(values.get('ui_pattern_scale',1.),1.,.25,4.),
        _num(values.get('ui_pattern_amplitude',.5),.5,0.,2.),
        _num(values.get('ui_pattern_parallax',.2),.2,0.,1.),
        _num(values.get('ui_pattern_relief',.3),.3,0.,1.),
        _num(values.get('ui_pattern_speed',1.),1.,0.,3.),
        _num(values.get('ui_pattern_density',1.),1.,.25,2.5),
        _num(values.get('ui_pattern_opacity',.48),.48,0.,1.),
        _num(values.get('ui_pattern_contrast',.65),.65,.1,2.),
        _num(values.get('ui_pattern_glow',.25),.25,0.,1.),
        _num(values.get('ui_pattern_softness',.5),.5,0.,1.),
        motion,
        _num(values.get('font_scale',1.),1.,.75,2.),
        _num(values.get('window_scale',1.),1.,.75,2.),
        str(values.get('ui_render_quality','HIGH')).upper() if str(values.get('ui_render_quality','HIGH')).upper() in ('ULTRA','HIGH','BALANCED','PERFORMANCE') else 'HIGH',
    )


def from_preferences(prefs):
    if prefs is None:return from_values({})
    fields=('ui_scheme','ui_surface','ui_pattern','accent','ui_custom_accent',
            'ui_accent_color','ui_secondary_color','ui_pattern_strength',
            'ui_pattern_scale','ui_pattern_amplitude','ui_pattern_parallax',
            'ui_pattern_relief','ui_pattern_speed',
            'ui_pattern_density','ui_pattern_opacity','ui_pattern_contrast',
            'ui_pattern_glow','ui_pattern_softness','ui_motion_quality',
            'ui_reduced_motion','font_scale','window_scale','ui_render_quality')
    values={name:getattr(prefs,name,None) for name in fields}
    # Keep the theme-driven HUD typography in sync with the same manual
    # accessibility preset used by geometry/HIT layout (not Blender OS DPI).
    from .ui_layout_scaling import from_preferences as _display_profile
    visual=_display_profile(prefs)
    values['font_scale']=visual.font
    values['window_scale']=visual.safe_window
    return from_values(values)
