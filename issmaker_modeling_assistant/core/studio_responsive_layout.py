"""Measured, DPI-aware Appearance Studio bounds; no Blender dependency.

Existing UIProfile provides the AUTO/QHD/UHD readability multiplier.
All drawn controls and hit-tests continue using the *same* physical rect.
"""
from math import isfinite

def dimensions(region_width,region_height,display_scale=1.):
    try:
        rw,rh,scale=map(float,(region_width,region_height,display_scale))
        if not all(isfinite(v) for v in (rw,rh,scale)) or rw<430 or rh<395:
            return None
    except (TypeError,ValueError,OverflowError):
        return None
    scale=max(.75,min(1.36,scale))
    width=min(rw-32.,max(400.,794.*scale))
    height=min(rh-32.,max(350.,680.*scale))
    if width<400. or height<350.:return None
    return width,height

def text_scale(value):
    try:
        scale=float(value)
        return max(.75,min(1.36,scale)) if isfinite(scale) else 1.
    except (TypeError,ValueError,OverflowError):
        return 1.
