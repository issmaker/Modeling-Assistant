"""Unified ISSMAKER GPU window shell / layout policy, Pass 14.2.

Blender-free geometry: no dependency on modal operators, Blender APIs or GPU.
It provides the same ratios and visual roles for all HUD renderers, while
leaving existing hitboxes/interaction controllers as the sole authority.
"""
from dataclasses import dataclass
from math import isfinite, sin, cos, pi
from .workspace_components import component_visual, compatibility_metrics

@dataclass(frozen=True)
class ShellSpec:
    header: float
    footer: float
    padding: float
    gap: float
    control: float
    radius: float
    compact: bool

@dataclass(frozen=True)
class ShellZones:
    outer: tuple
    header: tuple
    body: tuple
    footer: tuple
    title: tuple
    actions: tuple


def spec(kind='editor', *, scale=1., compact=False):
    """Visual metrics across DWOS/UV Wall/Collision/Brick Hole.

    Header defaults remain compatible with already painted modal widgets;
    the shell never independently defines or changes click targets.
    """
    try: scale=float(scale)
    except (TypeError,ValueError,OverflowError): scale=1.
    if not isfinite(scale): scale=1.
    scale=max(.75,min(2.,scale))
    if kind not in ('editor','composer','toolbar','compact','group'):
        raise ValueError('Unknown shell kind')
    hh={'editor':35.,'composer':36.,'toolbar':24.,'compact':30.,'group':27.}[kind]
    pad=12. if kind=='editor' else (14. if kind=='composer' else 8.)
    return ShellSpec(hh*scale, (66. if kind=='editor' else 0.)*scale,
                     pad*scale,7.*scale,27.*scale,8.*scale,bool(compact))


def zones(rect, *, kind='editor', scale=1., footer=None, reserved_right=0.):
    """One measurable, non-overlapping header/body/footer convention (xyxy)."""
    try:
        x0,y0,x1,y1=[float(v) for v in rect]
        reserve=float(reserved_right)
    except (TypeError,ValueError,OverflowError):return None
    if not all(isfinite(v) for v in (x0,y0,x1,y1,reserve)) or x1<=x0 or y1<=y0:return None
    s=spec(kind,scale=scale)
    ft=s.footer if footer is None else float(footer)
    if not isfinite(ft) or ft<0 or (y1-y0)<=s.header+ft:return None
    header=(x0,y1-s.header,x1,y1)
    foot=(x0,y0,x1,y0+ft)
    body=(x0+min(s.padding,(x1-x0)/4),y0+ft,
          x1-min(s.padding,(x1-x0)/4),y1-s.header)
    title=(x0+s.padding,header[1],max(x0+s.padding,x1-s.padding-max(0.,reserve)),y1)
    actions=(x0+s.padding,y0+s.padding,x1-s.padding,y0+ft-s.padding)
    if title[2]<=title[0] or body[3]<=body[1]:return None
    return ShellZones((x0,y0,x1,y1),header,body,foot,title,actions)


def action_bar(rect, count=3, *, scale=1., min_width=42., weights=None):
    """Return disjoint action rectangles or fail closed; no squeezed invisible click zones."""
    try:
        x0,y0,x1,y1=map(float,rect)
        scale=float(scale)
        count=int(count)
    except (ValueError,TypeError,OverflowError):return ()
    if not all(isfinite(v) for v in (x0,y0,x1,y1,scale)) or scale<=0 or count<=0 or count>8:return ()
    gap=7*scale
    available=x1-x0-gap*(count-1)
    if y1<=y0 or available<count*min_width*scale:return ()
    if weights is None: weights=(1.,)*count
    try: ww=tuple(float(v) for v in weights)
    except (TypeError,ValueError,OverflowError):return ()
    if len(ww)!=count or not all(isfinite(v) and v>0 for v in ww):return ()
    total=sum(ww);cur=x0;out=[]
    for index,weight in enumerate(ww):
        end=x1 if index==count-1 else cur+available*weight/total
        out.append((cur,y0,end,y1));cur=end+gap
    if any(r[2]-r[0]<min_width*scale-1e-5 for r in out):return ()
    return tuple(out)


def surface(theme=None, *, role='neutral'):
    """Same matte shell palette for floating window and embedded HUD."""
    card=component_visual('card','normal',role,theme)
    head=component_visual('header','normal',role,theme)
    return {'panel':card.background,'border':card.border,
            'header':head.background,'title':head.foreground,
            'accent':head.accent}


def truncate_title(text, width, measure, *, ellipsis='…'):
    """Pixel-measured title fitting; never modifies editable input text."""
    value=str(text)
    try:limit=float(width)
    except (ValueError,TypeError,OverflowError):return ''
    if not isfinite(limit) or limit<=0:return ''
    if measure(value)<=limit:return value
    if measure(ellipsis)>limit:return ''
    lo,hi=0,len(value)
    while lo<hi:
        mid=(lo+hi+1)//2
        if measure(value[:mid]+ellipsis)<=limit:lo=mid
        else:hi=mid-1
    return value[:lo]+ellipsis


def diagnostics():
    return {'contract':'UNIFIED_WINDOW_SHELL_142','kinds':['editor','composer','toolbar','compact','group'],
            'authority':'existing draw/hitbox controller','overlap_policy':'fail-closed'}


def rounded_vertices(rect, radius=7., *, segments=6):
    """Blender-free rounded polygon for the shared native GPU shell.

    Drawable geometry only; caller uses its unchanged original rectangle for
    hit-testing. Radius is inward-clamped to prevent painted pixel overflow.
    """
    try:
        x0,y0,x1,y1=(float(v) for v in rect)
        radius=float(radius); segments=int(segments)
    except (TypeError,ValueError,OverflowError):return ()
    if not all(isfinite(v) for v in (x0,y0,x1,y1,radius)):
        return ()
    if x1<=x0 or y1<=y0 or segments<2 or segments>32:
        return ()
    r=max(0.,min(radius,(x1-x0)*.5,(y1-y0)*.5))
    if r<.3:
        return ((x0,y0),(x1,y0),(x1,y1),(x0,y1))
    corners=((x1-r,y0+r,-pi*.5,0.),(x1-r,y1-r,0.,pi*.5),
             (x0+r,y1-r,pi*.5,pi),(x0+r,y0+r,pi,pi*1.5))
    points=[]
    for cx,cy,a,b in corners:
        for i in range(segments+1):
            theta=a+(b-a)*i/segments
            points.append((cx+cos(theta)*r,cy+sin(theta)*r))
    return tuple(points)
