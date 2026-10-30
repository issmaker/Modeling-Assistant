"""DWOS 3.5.2 pure retained-layout QA helpers.

Every HUD control is validated against one panel rect before it is drawn/hit-tested.
Text fitting is deterministic in pure tests; Blender runtime may pass exact BLF widths.
"""
from __future__ import annotations
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class Rect:
    x: float; y: float; w: float; h: float
    @property
    def right(self): return self.x+self.w
    @property
    def top(self): return self.y+self.h
    def tuple(self): return (self.x,self.y,self.w,self.h)

def as_rect(r):
    return r if isinstance(r,Rect) else Rect(*(float(v) for v in r))

def inset(r,pad=8.0):
    r=as_rect(r); p=max(0.0,float(pad))
    return Rect(r.x+p,r.y+p,max(0.0,r.w-2*p),max(0.0,r.h-2*p))

def contains(container,rect,eps=1e-6):
    c=as_rect(container); r=as_rect(rect)
    return r.x>=c.x-eps and r.y>=c.y-eps and r.right<=c.right+eps and r.top<=c.top+eps

def fit_inside(container,rect,pad=8.0,min_w=18.0,min_h=18.0):
    """Clamp a control into the panel content box without changing draw/hit identity."""
    c=inset(container,pad); r=as_rect(rect)
    w=min(max(float(min_w),r.w),c.w); h=min(max(float(min_h),r.h),c.h)
    x=min(max(r.x,c.x),max(c.x,c.right-w)); y=min(max(r.y,c.y),max(c.y,c.top-h))
    return Rect(x,y,w,h)

def estimated_text_width(text,size=10.0):
    # Conservative Cyrillic/Latin UI approximation for static QA only.
    return len(str(text))*float(size)*0.62

def fit_text(text,max_width,size=10.0,width_fn=None,ellipsis='…'):
    text=str(text); mw=max(0.0,float(max_width)); fn=width_fn or (lambda s:estimated_text_width(s,size))
    if fn(text)<=mw:return text
    if fn(ellipsis)>mw:return ''
    lo,hi=0,len(text)
    while lo<hi:
        mid=(lo+hi+1)//2
        if fn(text[:mid]+ellipsis)<=mw:lo=mid
        else:hi=mid-1
    return text[:lo]+ellipsis

def validate_layout(panel,controls,pad=8.0):
    """Return human-readable layout errors; empty list is a release gate PASS."""
    content=inset(panel,pad); errors=[]; rows=[]
    for key,rect in controls:
        r=as_rect(rect)
        if not contains(content,r):errors.append(f'{key}: OUT_OF_BOUNDS')
        if r.w<=0 or r.h<=0 or not all(math.isfinite(v) for v in r.tuple()):errors.append(f'{key}: INVALID_RECT')
        rows.append((key,r))
    # Only flag real area overlaps. Touching edges are OK.
    for i,(ka,a) in enumerate(rows):
        for kb,b in rows[i+1:]:
            ix=min(a.right,b.right)-max(a.x,b.x); iy=min(a.top,b.top)-max(a.y,b.y)
            if ix>1.0 and iy>1.0:errors.append(f'{ka}/{kb}: OVERLAP')
    return errors

def panel_height_for_state(page,is_corner=False,frame_more=False,error=False):
    """3.5.2 measured heights; enough room means no controls can leak below HUD."""
    page=str(page).upper()
    if page=='FRAME': base=(548 if frame_more else (411 if is_corner else 376))+(56 if is_corner else 0)
    elif page=='INTERIOR': base=364 if is_corner else 332
    elif page=='GLASS': base=284
    else: base=376
    # 3.6.1: reserved 60 px for the restored scope row and its explanatory hint.
    return base+60+(28 if error else 0)
