"""Pure ISSMAKER HUD readability contract (Pass 12.2).

All UI painters share *physical pixel* alignment for text, bounded fitting,
and a minimum contrast for neutral captions. Semantic red/orange geometry
and statuses MUST NOT be recolored by this helper. No Blender imports.
"""
from __future__ import annotations
from math import floor,isfinite

def snap(value):
    """Align text baseline to physical pixels without moving hit rectangles."""
    value=float(value)
    if not isfinite(value):raise ValueError('Nonfinite pixel')
    return float(floor(value+.5))

def snap_stroke(value):
    """Align a 1-pixel hairline to a physical half pixel."""
    return snap(value)+.5

def _linear(c):
    c=max(0.,min(1.,float(c)))
    return c/12.92 if c<=.04045 else ((c+.055)/1.055)**2.4

def luminance(rgb):
    if len(rgb)<3 or not all(isfinite(float(v)) for v in rgb[:3]):
        raise ValueError('Invalid RGB')
    return sum(k*_linear(c) for k,c in zip((.2126,.7152,.0722),rgb))

def contrast_ratio(foreground,background):
    a,b=sorted((luminance(foreground),luminance(background)))
    return (b+.05)/(a+.05)

def readable_neutral(color,background,minimum=4.5):
    """Improve contrast of neutral UI captions against their opaque panel.

    RGB is adjusted toward the higher-contrast monochrome target. Alpha is
    retained. This deliberately does NOT replace diagnostic/semantic colors.
    """
    color=tuple(float(x) for x in color)
    if len(color) not in (3,4): raise ValueError('Invalid neutral color')
    minimum=float(minimum)
    if not isfinite(minimum) or not 1<=minimum<=21:raise ValueError('Invalid ratio')
    if contrast_ratio(color,background)>=minimum:return color
    white=(1.,1.,1.);black=(0.,0.,0.)
    target=white if contrast_ratio(white,background)>=contrast_ratio(black,background) else black
    left,right=0.,1.
    for _ in range(24):
        mid=(left+right)*.5
        proposal=tuple(color[i]*(1-mid)+target[i]*mid for i in range(3))
        if contrast_ratio(proposal,background)>=minimum:right=mid
        else:left=mid
    return tuple(color[i]*(1-right)+target[i]*right for i in range(3))+color[3:]

def fit_caption(text,available_px,measure,ellipsis='…'):
    """Fitted display-only caption using actual BLF measurements.

    Never use on numeric input text or editable buffers. If text cannot fit,
    return empty rather than draw into an adjacent clickable control.
    """
    text=str(text)
    available=float(available_px)
    if not isfinite(available) or available<=0:return ''
    if not text:return ''
    def width(s):
        w=float(measure(s))
        if not isfinite(w) or w<0:raise ValueError('Bad font metrics')
        return w
    try:
        if width(text)<=available:return text
        if width(ellipsis)>available:return ''
        lo,hi=0,len(text)
        while lo<hi:
            mid=(lo+hi+1)//2
            if width(text[:mid]+ellipsis)<=available:lo=mid
            else:hi=mid-1
        return text[:lo].rstrip()+ellipsis
    except (TypeError,ValueError,RuntimeError,OverflowError):
        return ''  # never render outside bounds when the font backend fails
