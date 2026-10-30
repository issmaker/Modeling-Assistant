"""Pass 13: pure, strict, adaptive right-column geometry for UV Wall editors.

Does not mutate modeling data. Returns a whole column reservation plus one exact
rectangle per interactive editor, with optional *visual-only* auto-compact.
All rectangles are viewport-local xyxy. Layout has no Blender dependency.
"""
from dataclasses import dataclass
from math import isfinite
from issmaker_modeling_assistant.core.overlay_placement import StrictOverlayAllocator

HEADER_HEIGHT = 27.0

@dataclass(frozen=True)
class EditorPlan:
    rect: tuple
    editors: tuple   # (editor key, rect, auto_compact)
    compressed: int


def plan_right_column(region, entries, obstacles=(), *, scale=1.0, header_height=None,
                      focus_areas=(), anchor=None):
    """Place >=1 editor card in a single continuous right-hand workspace.

    ``entries`` contains (unique ID, natural height, manually collapsed).
    The newest entry is last and has first claim on vertical space. Older
    entries get visual-only collapsed presentation if space is tight; their
    unsaved drafts, edit buffers and scroll positions stay untouched.
    """
    try:
        rw,rh=(float(v) for v in region)
        scale=float(scale)
        items=[(str(k),float(h),bool(manual)) for k,h,manual in entries]
    except (TypeError,ValueError,OverflowError):
        return None
    if not all(isfinite(v) for v in (rw,rh,scale)) or scale<=0 or len(items)<1:
        return None
    if rw<430*scale or rh<190*scale or len(set(k for k,_,_ in items))!=len(items):
        return None
    gap=0.0  # touching surfaces become one visible uninterrupted stack
    hh=(HEADER_HEIGHT if header_height is None else float(header_height))*scale
    min_h=46*scale
    width=min(370*scale, max(280*scale, rw*.39))
    max_h=max(0.,rh-24*scale)
    heights=[max(min_h,h) for _,h,_ in items]
    auto=[False]*len(items)
    def total(): return hh+sum(heights)+gap*(len(heights)-1)
    # Preserve newest active editor. Compact clean/dirty older editors only
    # visually, never change their draft, user collapse flag or editing text.
    for idx in range(len(items)-2,-1,-1):
        if total()<=max_h:break
        if not items[idx][2] and heights[idx]>min_h:
            heights[idx]=min_h
            auto[idx]=True
    if total()>max_h and not items[-1][2]:
        # Scroll the newest expanded editor instead of rejecting the entire
        # group, provided its footer + at least one usable row still fit.
        excess=total()-max_h
        minimum_active=145*scale
        if heights[-1]-excess>=minimum_active:
            heights[-1]-=excess
    if total()>max_h:return None
    allocator=StrictOverlayAllocator(rw,rh,margin=8*scale,gap=6*scale)
    for i,r in enumerate(obstacles):
        allocator.reserve('OBSTACLE:'+str(i),r)
    h=total()
    # Prefer near right edge, below Blender's gizmo reserve if necessary.
    if anchor is None:
        preferred=(rw-width-12*scale,rh-h-80*scale)
    else:
        try:
            ax,ay=(float(v) for v in anchor)
            if not all(isfinite(v) for v in (ax,ay)) or not (0<=ax<=1 and 0<=ay<=1):
                return None
        except (TypeError,ValueError,OverflowError):return None
        preferred=(8*scale+ax*max(0.,rw-width-16*scale),
                   8*scale+ay*max(0.,rh-h-16*scale))
    found=allocator.place('UV_WALL_EDITOR_WORKSPACE',(width,h),preferred)
    # Prefer a free column that avoids currently edited/projected geometry.
    # These are *soft* reservations: never sacrifice tool operability just
    # because an object fills the viewport. UI-versus-UI rules stay strict.
    if focus_areas and anchor is None:
        soft=StrictOverlayAllocator(rw,rh,margin=8*scale,gap=6*scale)
        for i,r in enumerate(obstacles):soft.reserve('OBSTACLE:'+str(i),r)
        for i,r in enumerate(focus_areas):soft.reserve('FOCUS:'+str(i),r)
        focused=soft.place('UV_WALL_EDITOR_WORKSPACE',(width,h),preferred)
        if focused is not None:found=focused
    if found is None:return None
    cursor=found[3]-hh
    slots=[]
    for idx in range(len(items)-1,-1,-1):
        key=items[idx][0];height=heights[idx]
        r=(found[0],max(found[1],cursor-height),found[2],min(found[3],cursor))
        slots.append((key,r,auto[idx]))
        cursor-=height+gap
    return EditorPlan(found,tuple(slots),sum(auto))
