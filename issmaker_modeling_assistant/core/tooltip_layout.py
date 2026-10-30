"""Pure measured multiline tooltip compositor for 3D Viewport hover hints.

Presentation only: returns a painted box and text rows. Never creates hitboxes,
new Blender handlers, or modifies the neighboring spatial markers.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite

@dataclass(frozen=True)
class TooltipLayout:
    rect: tuple
    lines: tuple
    font_px: int
    line_height: float
    pad_x: float
    pad_y: float


def _fit_lines(text, max_width, measure, limit):
    """Greedy wrap; split long unbroken tokens with bounded measurement.

    A hidden remainder is always disclosed by ellipsis, never silently lost.
    """
    result=[]
    words=str(text).replace('\r','').replace('\t',' ').replace('\n',' \n ').split(' ')
    current=''
    def valid(candidate):
        width=float(measure(candidate))
        if not isfinite(width) or width<0:raise ValueError('Bad glyph metrics')
        return width<=max_width+1.e-5
    for token in words:
        if token=='\n':
            if current:result.append(current);current=''
            elif result:result.append('')
            continue
        if not token:continue
        if valid((current+' '+token) if current else token):
            current=(current+' '+token) if current else token
            continue
        if current:
            result.append(current);current=''
        if valid(token):current=token;continue
        for char in token:
            if valid(current+char):current+=char
            else:
                if current:result.append(current);current=''
                if valid(char):current=char
                else:raise ValueError('Font/viewport cannot contain one glyph')
        # Preserve word separation even when the next token will fit.
    if current:result.append(current)
    if len(result)>limit:
        result=result[:limit]
        base=result[-1]
        if not valid('…'):return ()
        while base and not valid(base.rstrip()+'…'):
            base=base[:-1]
        result[-1]=base.rstrip()+'…'
    return tuple(result)


def tooltip_layout(text, viewport, pointer, measure, *, font_px=12,
                   max_width=420., max_lines=5, margin=8., pad_x=12., pad_y=10.):
    """Return None if no safe card. Coordinates are Viewport *physical pixels*.

    `measure` must evaluate with the same `blf.size` used for actual painting.
    The resulting rectangle is never used as a mouse target.
    """
    try:
        vw,vh=(float(x) for x in viewport)
        mx,my=(float(x) for x in pointer)
        font_px=int(font_px)
        bounds=(vw,vh,mx,my,float(max_width),float(margin),float(pad_x),float(pad_y))
        if not all(isfinite(x) for x in bounds) or font_px<8 or max_lines<1 or \
           min(vw,vh,max_width)<=0 or margin<0 or min(pad_x,pad_y)<0:
            return None
        line_height=max(font_px*1.42,font_px+3.)
        available_w=min(float(max_width),vw-2*margin)
        text_w=available_w-2*pad_x
        available_h=vh-2*margin-2*pad_y
        count=min(int(max_lines),int(available_h//line_height))
        if count<1 or text_w<float(measure('…')):return None
        # Huge unexpected diagnostic strings never hold the GPU callback hostage.
        raw=str(text)
        clipped=raw[:1600]+('…' if len(raw)>1600 else '')
        lines=_fit_lines(clipped,text_w,measure,count)
        if not lines:return None
        width=min(available_w,max(118.,max(float(measure(line)) for line in lines)+2*pad_x))
        width=min(width,available_w)
        height=2*pad_y+line_height*len(lines)
        if height>vh-2*margin+1.e-6:return None
        x=min(max(margin,mx+16),vw-margin-width)
        # Prefer above cursor, but fit below if the cursor is near bottom.
        above=my-14-height
        below=my+18
        preferred=above if above>=margin and above+height<=vh-margin else below
        y=min(max(margin,preferred),vh-margin-height)
        return TooltipLayout((x,y,width,height),lines,font_px,line_height,pad_x,pad_y)
    except (TypeError,ValueError,OverflowError,RuntimeError):
        return None
