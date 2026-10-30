"""DWOS visual contract for every ISSMAKER viewport HUD (Pass 15.43).

No bpy, GPU or tools imports; UI-only, deterministic, fail-closed.  A component
has precisely one paint rectangle, and controllers MUST use it as their hitbox.
"""
from math import isfinite

HUD_FAMILIES = ('DWOS','BRICK_HOLE','COLLISION','UV_WALL','TOOL_RADIAL','ISSUE_NAV')
CONTROL_KEYS = frozenset(('apply','cancel','collapse','expand','view',
    'mode_auto','mode_face','mode_global','axis_x','axis_y','axis_z',
    'depth_minus','depth_plus'))

def paint_spec(key, *, selected=False, focused=False, disabled=False):
    """Return DWOS `draw_component` semantics, independent from hit geometry."""
    key=str(key)
    if key=='accent_strip':
        return {'part':'card','state':'quiet','role':'primary','interactive':False}
    part=('card' if key in {'panel','view_menu','mode_back','axis_back','numeric_back'}
          else 'field' if key=='value_hit' else 'button')
    role='primary' if key in {'apply','expand','mode_face','mode_auto','mode_global',
                                'axis_x','axis_y','axis_z'} else 'neutral'
    if key=='cancel':role='danger'
    state='disabled' if disabled else 'focused' if focused else 'active' if selected else 'normal'
    return {'part':part,'state':state,'role':role,
            'interactive':not disabled and part not in {'card'}}


def rectangle_report(layout, viewport, *, scale=1., interactive=()):
    """Audit a complete GPU/hit rectangle map with one independent oracle.

    Only rectangles (4-tuples) are hit targets; point positions are labels and
    never interpreted as clickables. Invalid/overflowing clickables are hard
    errors; popup surfaces can overlap their own children on purpose.
    """
    try:
        width,height=(float(x) for x in viewport)
        k=float(scale)
        if not all(isfinite(x) for x in (width,height,k)) or min(width,height,k)<=0:
            raise ValueError('invalid viewport')
    except (ValueError,TypeError,OverflowError):
        return {'ok':False,'errors':('invalid viewport',),'interactive_count':0}
    errors=[]
    interactive=tuple(interactive)
    for key in interactive:
        rect=layout.get(key)
        if rect is None:
            errors.append(f'{key}: missing hitbox')
            continue
        if len(rect)!=4:
            errors.append(f'{key}: not rectangle')
            continue
        try:
            x0,y0,x1,y1=map(float,rect)
            if not all(isfinite(v) for v in (x0,y0,x1,y1)):
                raise ValueError('nonfinite')
            if x1<=x0 or y1<=y0:
                raise ValueError('empty')
            if x0<-1e-5 or y0<-1e-5 or x1>width+1e-5 or y1>height+1e-5:
                raise ValueError('outside viewport')
        except (ValueError,TypeError,OverflowError) as exc:
            errors.append(f'{key}: {exc}')
    return {'ok':not errors,'errors':tuple(errors),
            'interactive_count':len(interactive)}


def hit_popup_first(layout, point, *, popup='view_menu', items=(), under=()):
    """Pure hit priority. Popup gutters block buttons below while open."""
    try:x,y=map(float,point)
    except (ValueError,TypeError,OverflowError):return None
    if not all(isfinite(z) for z in (x,y)):return None
    def inside(key):
        r=layout.get(key)
        return bool(r and len(r)==4 and r[0]<=x<=r[2] and r[1]<=y<=r[3])
    if popup in layout:
        for key in items:
            if inside(key):return key
        if inside(popup):return popup
    for key in under:
        if inside(key):return key
    return None
