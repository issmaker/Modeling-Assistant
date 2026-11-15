"""Blender-free material policy for animated HUD backgrounds.

The surface shader draws *behind* controls and has no event handlers.
Colours are derived only from the currently selected ThemeSnapshot.
Reduced motion, quality caps and zero intensity never generate animation.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from .hud_render_budget import decorative_time

SURFACES=("architectural","graphite","mineral","smoke","ink")
MODES={name:index for index,name in enumerate(SURFACES)}

@dataclass(frozen=True)
class MaterialSpec:
    mode: int
    primary: tuple
    secondary: tuple
    opacity: float
    phase: float
    quality: str

def _mix(a,b,t):
    return tuple(float(a[i])*(1.-t)+float(b[i])*t for i in range(3))

def resolve(theme, *, now=0.0, preview=False):
    """Return a small immutable GPU uniform set, or None to skip the material.

    'now' is caller elapsed monotonic seconds, never Blender scene time;
    no mesh, image datablock or material node is accessed or created.
    """
    if theme is None or str(getattr(theme,'pattern','none')).lower()=='none':
        return None
    try:
        strength=float(getattr(theme,'pattern_strength',0.))
        opacity=float(getattr(theme,'pattern_opacity',0.))
        if not all(isfinite(v) for v in (strength,opacity,now)):
            return None
        amount=max(0.,min(1.,strength))*max(0.,min(1.,opacity))
        if amount<=.001:return None
        panel=tuple(theme.panel)
        accent=tuple(theme.accent)
        secondary=tuple(theme.secondary)
        if any(len(x)!=3 or any(not isfinite(float(v)) or not 0.<=float(v)<=1. for v in x)
               for x in (panel,accent,secondary)):
            return None
        quality=str(getattr(theme,'render_quality','HIGH')).upper()
        if quality not in ('ULTRA','HIGH','BALANCED','PERFORMANCE'):quality='HIGH'
        motion=str(getattr(theme,'motion','OFF')).upper()
        speed=max(0.,min(3.,float(getattr(theme,'pattern_speed',1.))))
        animate=motion!='OFF' and quality!='PERFORMANCE' and speed>.001
        # Loop the uniform value to retain precision after days of uptime.
        phase=(decorative_time(max(0.,now),quality,animate)*speed)%(96.)
        foreground=_mix(panel,accent,.63)
        other=_mix(panel,secondary,.55)
        # Decorative opacity is deliberately below the surface fill/label.
        opacity_out=min(.42,amount*(.40 if preview else .27))
        return MaterialSpec(MODES.get(str(theme.surface).lower(),0),
                            foreground,other,opacity_out,phase,quality)
    except (ValueError,TypeError,OverflowError,AttributeError):
        return None

def contrast_luma(rgb):
    """Relative luminance for panel-background QA, not color correction."""
    def channel(x):
        x=max(0.,min(1.,float(x)))
        return x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4
    return sum(weight*channel(v) for weight,v in zip((.2126,.7152,.0722),rgb))

def contrast_ratio(a,b):
    aa=contrast_luma(a);bb=contrast_luma(b)
    return (max(aa,bb)+.05)/(min(aa,bb)+.05)
