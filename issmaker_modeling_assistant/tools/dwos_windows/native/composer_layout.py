"""Pure geometry for compact DWOS viewport control placement (no Blender imports)."""
from __future__ import annotations

def intersects(a,b,pad=8):
    ax,ay,aw,ah=a;bx,by,bw,bh=b
    return not (ax+aw+pad <= bx or bx+bw+pad <= ax or ay+ah+pad <= by or by+bh+pad <= ay)

def clamp(rect,viewport,margin=10):
    x,y,w,h=rect;vw,vh=viewport
    return (max(margin,min(x,max(margin,vw-w-margin))),
            max(margin,min(y,max(margin,vh-h-margin))),w,h)

def floating_rect(anchor,size,viewport,avoid=(),margin=12):
    """Place a small contextual card without covering the main panel or the cursor."""
    ax,ay=anchor;w,h=size
    choices=[(ax+20,ay+16),(ax-w-20,ay+16),(ax+20,ay-h-16),
             (ax-w-20,ay-h-16),(ax-w*.5,ay+h*.15)]
    for fx,fy,fw,fh in avoid:
        choices += [(fx-w-24,ay-h*.5),(fx+fw+24,ay-h*.5),
                    (ax-w*.5,fy-h-24),(ax-w*.5,fy+fh+24)]
    preferred=[]
    for cx,cy in choices:
        r=clamp((cx,cy,w,h),viewport,margin)
        overlaps=sum(1 for forbidden in avoid if intersects(r,forbidden))
        cursor= int(r[0]-7<=ax<=r[0]+w+7 and r[1]-7<=ay<=r[1]+h+7)
        shift=abs(r[0]-cx)+abs(r[1]-cy)
        preferred.append(((overlaps,cursor,shift),r))
    return min(preferred,key=lambda item:item[0])[1]

def declutter_handles(handles,minimum=23):
    """Deterministic label/hit positions when projected handles overlap."""
    placed={}
    for name,point in handles.items():
        x,y=point
        if not isinstance(point,(tuple,list)) or len(point)!=2:continue
        for n in range(12):
            if all((x-p[0])**2+(y-p[1])**2 >= minimum**2 for p in placed.values()):break
            x=point[0]+(n%4+1)*minimum*.56
            y=point[1]+(n//4-1)*minimum*.67
        placed[name]=(x,y)
    return placed

def screen_safe_panel(window_rect,size,viewport,avoid=(),previous_side=None,margin=12,gap=18):
    """DWOS 3.2 stable Viewport HUD placement.

    Chooses an outside position when possible and falls back to a viewport edge
    when the projected window is larger than the visible region.  ``previous_side``
    gives a small hysteresis bonus so zoom/orbit does not make the card jump every
    redraw.  Returns ``(x,y,w,h,side)``.
    """
    import math
    w,h=size;vw,vh=viewport
    if min(w,h,vw,vh)<=0:return (margin,margin,w,h,'EDGE')
    if window_rect is None:
        return (max(margin,vw-w-margin),max(margin,vh-h-margin),w,h,'EDGE')
    x,y,ww,wh=window_rect
    vals=(x,y,ww,wh,w,h,vw,vh)
    if not all(math.isfinite(float(v)) for v in vals):
        return (max(margin,vw-w-margin),max(margin,vh-h-margin),w,h,'EDGE')
    candidates={
        'RIGHT':(x+ww+gap,y+wh*.5-h*.5),
        'LEFT':(x-w-gap,y+wh*.5-h*.5),
        'TOP':(x+ww*.5-w*.5,y+wh+gap),
        'BOTTOM':(x+ww*.5-w*.5,y-h-gap),
    }
    scored=[]
    for side,(cx,cy) in candidates.items():
        raw=(cx,cy,w,h)
        cl=clamp(raw,viewport,margin)
        shift=abs(cl[0]-cx)+abs(cl[1]-cy)
        # Prefer genuinely outside positions; heavy clamp means the anchor left
        # the viewport and this candidate is only a fallback.
        overlap_window=1 if intersects(cl,(x,y,ww,wh),pad=4) else 0
        overlap_ui=sum(1 for r in avoid if intersects(cl,r,pad=6))
        hysteresis=-18 if side==previous_side else 0
        scored.append(((overlap_ui*10000+overlap_window*3000+shift+hysteresis),cl,side))
    score,rect,side=min(scored,key=lambda q:q[0])
    if score>=3000:
        # 3.5 CLOSE-ZOOM POLICY: when the projected window consumes the viewport,
        # move the HUD DOWN instead of pinning it to the upper-left/upper-right.
        # This keeps the architectural corner and top frame visible while editing.
        edge_choices=[
            ('BOTTOM_RIGHT',(vw-w-margin,margin,w,h)),
            ('BOTTOM_LEFT',(margin,margin,w,h)),
            ('BOTTOM_CENTER',(max(margin,(vw-w)*.5),margin,w,h)),
        ]
        best=None
        for es,er in edge_choices:
            ov=sum(1 for r in avoid if intersects(er,r,pad=6))
            # Strong hysteresis: close zoom/orbit must not bounce left/right.
            sticky=-14 if previous_side==es else 0
            center_penalty=4 if es=='BOTTOM_CENTER' else 0
            item=(ov*100+center_penalty+sticky,er,es)
            if best is None or item[0]<best[0]:best=item
        _,rect,side=best
    return (*rect,side)


def overlap_area(a, b, pad=0):
    """Actual 2D covered area, independent of inclusive edge-touch semantics."""
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    if min(aw,ah,bw,bh)<=0:
        return 0.0
    return max(0.0, min(ax+aw, bx+bw+pad)-max(ax,bx-pad))*max(0.0, min(ay+ah,by+bh+pad)-max(ay,by-pad))


def screen_safe_multi_panel(window_rect, size, viewport, selected_rects, avoid=(),
                            previous_side=None, margin=12, gap=18):
    """Fit the HUD without covering ANY selected window's projected geometry.

    Return None (instead of drawing across mesh) when there is no safe screen
    space. Caller must preserve Composer selection and allow normal sidebar use.
    All calculations are pure and therefore fuzzable without Blender.
    """
    import math
    vw,vh=(float(v) for v in viewport)
    w,h=(float(v) for v in size)
    if not all(math.isfinite(v) for v in (vw,vh,w,h)) or min(vw,vh,w,h)<=0:
        return None
    if w+2*margin>vw or h+2*margin>vh:
        return None
    blocked=[]
    for rect in (*selected_rects,*avoid):
        try:
            values=tuple(float(v) for v in rect)
        except (TypeError,ValueError):continue
        if len(values)==4 and all(math.isfinite(v) for v in values) and min(values[2:])>0:
            blocked.append(values)
    if window_rect is not None and window_rect not in selected_rects:
        blocked.append(window_rect)
    # Geometry defines critical candidate edges; this finds gaps between
    # projected window silhouettes rather than only testing HUD near active.
    xs=[margin,vw-w-margin,(vw-w)/2]
    ys=[margin,vh-h-margin,(vh-h)/2]
    for x,y,rw,rh in blocked:
        xs.extend((x-w-gap,x+rw+gap))
        ys.extend((y-h-gap,y+rh+gap))
    if window_rect is not None:
        x,y,rw,rh=window_rect
        xs.extend((x-w-gap,x+rw+gap,x+rw/2-w/2))
        ys.extend((y-h-gap,y+rh+gap,y+rh/2-h/2))
    candidates=[]
    seen=set()
    for xx in xs:
        for yy in ys:
            x=max(margin,min(xx,vw-w-margin))
            y=max(margin,min(yy,vh-h-margin))
            key=(round(x,3),round(y,3))
            if key in seen:continue
            seen.add(key)
            rect=(x,y,w,h)
            if any(overlap_area(rect,r,pad=5)>1e-7 for r in blocked):
                continue
            if window_rect is None:
                ax,ay=vw/2,vh/2
            else:
                ax=window_rect[0]+window_rect[2]/2
                ay=window_rect[1]+window_rect[3]/2
            distance=abs((x+w/2)-ax)+abs((y+h/2)-ay)
            side=('RIGHT' if x>ax else 'LEFT') if abs(x+w/2-ax)>abs(y+h/2-ay) else ('TOP' if y>ay else 'BOTTOM')
            hysteresis=20 if side==previous_side else 0
            candidates.append((distance-hysteresis,x,y,side))
    if not candidates:
        return None
    _,x,y,side=min(candidates,key=lambda item:(item[0],item[1],item[2]))
    return x,y,w,h,side


# 3.6.1: deterministic toolbar geometry, shared by COMPACT and EXPANDED HUD.
# Source semantics remain in batch_edit; do not implement a second batch engine.
SCOPE_BUTTONS = (("ACTIVE", "ONE"), ("SELECTED", "SEL"),
                 ("SAME_SIZE", "SAME"), ("ALL_HOST", "ALL"))
SCOPE_HINTS = {
    "ACTIVE": "ONE — только активное окно",
    "SELECTED": "SEL — выделенные окна на этой стене",
    "SAME_SIZE": "SAME — такой же размер на этой стене",
    "ALL_HOST": "ALL — все окна на этой стене",
    "COPY": "COPY — копировать раскладку импостов",
}


def scope_toolbar_rects(x, y, panel_width=292):
    """Return physical (x,y,w,h) for four scope buttons + COPY.

    No layout clamping: smaller Composer panes are not drawn, so neither
    paint rectangles nor hit targets can silently pile up at a panel edge.
    """
    panel_width=float(panel_width)
    if panel_width<292:
        raise ValueError("Scope toolbar requires 292 px Composer")
    widths=(44,44,51,43,64)
    labels=("ACTIVE","SELECTED","SAME_SIZE","ALL_HOST","COPY")
    px=float(x)+12.0
    result={}
    for key,width in zip(labels,widths):
        result[key]=(px,float(y),float(width),28.0)
        px+=width+5.0
    if px-5 > float(x)+panel_width-10:
        raise ValueError("Scope toolbar outside panel")
    return result


def compact_panel_height(is_corner=False,error=False):
    """One height contract for compact Composer + its always-visible toolbar."""
    return (172 if is_corner else 152)+(27 if error else 0)


# 3.6.4: independent centered bottom action, not a panel child.
def bottom_center_action_rect(viewport, selected_rects=(), *, width=256.0,
                              height=44.0, margin=14.0, safe_gap=7.0):
    """A fixed bottom-center CTA only if it fits and does not occlude selected meshes."""
    import math
    vw,vh=(float(v) for v in viewport)
    values=(vw,vh,width,height,margin)
    if not all(math.isfinite(q) for q in values) or vw<width+margin*2 or vh<height+margin*2:
        return None
    rect=((vw-width)*0.5, margin, width, height)
    if any(overlap_area(rect,r,pad=safe_gap)>1e-7 for r in selected_rects):
        return None
    return rect


# Pass04: Pure mapping between Composer logical points and physical viewport pixels.
# The same mapping MUST be used by drawing, hit testing and caret placement.
from dataclasses import dataclass

@dataclass(frozen=True)
class ComposerPixelMap:
    anchor_x: float
    anchor_y: float
    scale: float

    def __post_init__(self):
        import math
        if not all(math.isfinite(float(v)) for v in (self.anchor_x,self.anchor_y,self.scale)) or self.scale<=0:
            raise ValueError('Invalid DWOS Composer pixel map')

    def point(self, x, y):
        return (self.anchor_x+(float(x)-self.anchor_x)*self.scale,
                self.anchor_y+(float(y)-self.anchor_y)*self.scale)

    def xyxy(self, x0, y0, x1, y1):
        ax,ay=self.point(x0,y0)
        bx,by=self.point(x1,y1)
        return (ax,ay,bx,by)

    def xywh(self, x, y, w, h):
        px,py=self.point(x,y)
        return (px,py,float(w)*self.scale,float(h)*self.scale)

    def inverse_point(self, physical_x, physical_y):
        return (self.anchor_x+(float(physical_x)-self.anchor_x)/self.scale,
                self.anchor_y+(float(physical_y)-self.anchor_y)/self.scale)

    def baseline(self, x, logical_padding):
        px,_=self.point(x,self.anchor_y)
        return px+float(logical_padding)*self.scale


def popup_scale_for_viewport(size, viewport, desired, margin=14.0):
    """Keep contextual editors clickable and fully on-screen at high DPI.

    Prefer the user scaling factor; restrict to physical viewport dimensions.
    Return None when a legible 75% popup will not fit, never draw off-screen.
    """
    import math
    try:
        w,h=(float(v) for v in size)
        vw,vh=(float(v) for v in viewport)
        ds=float(desired)
        m=float(margin)
    except (ValueError,TypeError,OverflowError):return None
    if not all(math.isfinite(v) for v in (w,h,vw,vh,ds,m)):
        return None
    if min(w,h,vw,vh,ds)<=0 or m<0:return None
    scale=min(ds,(vw-2*m)/w,(vh-2*m)/h)
    if scale<.75:return None
    return scale
