"""ISSMAKER Pass 14 / universal, Blender-free contextual workspace policy.

Presentation-only: these helpers never write meshes, modal tool state, or model
properties. Clients may use the output to reserve a viewport *focus area* as a
soft preference. Failure of focus-aware placement MUST fall back to a strict
non-overlapping layout against other UI surfaces (not overlap the UI).
"""
from dataclasses import dataclass
from math import isfinite

TOOL_MODES={
    'UV_WALL': frozenset({'WALL','SURFACE','CORNICE'}),
    'DWOS': frozenset({'FRAME','BOX','GLASS','CAD'}),
    'BRICK_HOLE': frozenset({'PREVIEW','EDIT'}),
    'COLLISION': frozenset({'INSPECT','GROUPS'}),
}

@dataclass(frozen=True)
class ContextSnapshot:
    tool: str
    mode: str
    object_selected: bool
    active_editor: str = ''
    interacting: bool = False
    viewport_size: tuple = (0,0)

    @property
    def supported(self):
        return self.mode in TOOL_MODES.get(self.tool, frozenset())

    @property
    def show_editors(self):
        return self.supported and self.object_selected


def project_focus_rects(viewport_size, anchors, *, radius=96, max_regions=2):
    """Return valid viewport-local padded focus squares for selected markers.

    These are SOFT visual reservations only, never authoritative hitboxes.
    Skip invalid/offscreen points. Limit cardinality to bound placement cost.
    """
    try:
        w,h=map(float,viewport_size); r=float(radius)
    except (ValueError,TypeError,OverflowError): return ()
    if not all(isfinite(v) for v in (w,h,r)) or w<=0 or h<=0 or r<=0:return ()
    result=[]
    for point in anchors:
        try: x,y=map(float,point[:2])
        except (ValueError,TypeError,OverflowError):continue
        if not all(isfinite(v) for v in (x,y)) or x<0 or y<0 or x>w or y>h:continue
        rect=(max(0.,x-r),max(0.,y-r),min(w,x+r),min(h,y+r))
        if rect[2]>rect[0] and rect[3]>rect[1] and rect not in result:
            result.append(rect)
        if len(result)>=max_regions:break
    return tuple(result)


def resource_actions(kind, draft):
    """Show resource buttons only when their matching element is enabled."""
    if kind=='CAP':
        result=[]
        if draft.get('create_top_cap') and draft.get('cap_material_override'): result.append(('Верх','cap_material'))
        if draft.get('create_bottom_cap') and draft.get('bottom_cap_material_override'): result.append(('Низ','bottom_cap_material'))
        return tuple(result)
    if kind=='PLANT':
        return (('Материал','inner_surface_material'),) if draft.get('create_inner_surface') else ()
    if kind=='CORNICE':
        return (('Профиль','cornice_profile'),)
    return ()


def compact_input_rows(width, scale=1., *, min_width=318.):
    try: width=float(width);scale=float(scale)
    except (ValueError,TypeError,OverflowError):return False
    return isfinite(width) and isfinite(scale) and scale>0 and width>=min_width*scale
