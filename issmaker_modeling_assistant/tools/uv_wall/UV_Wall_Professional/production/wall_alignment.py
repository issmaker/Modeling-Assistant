"""Wall ribbon alignment relative to the immutable authored path (schema 5).

The existing production model has one or two zero-thickness wall shells; the
'inset_distance' is their separation/ribbon width. No fictional extra masonry
thickness is inserted by these alignment modes.
"""
from math import isfinite

MODES = ('LEGACY', 'LEFT', 'CENTER', 'RIGHT', 'INSIDE', 'OUTSIDE')


def resolve_placement(mode, points, open_inside_side='LEFT'):
    """Resolve INSIDE/OUTSIDE to exact physical LEFT/RIGHT of authored Path.

    For a closed path, winding defines inside; for an open path, the user
    chooses which side represents inside. Legacy LEFT/RIGHT/CENTER unchanged.
    This function does not edit any vertex or stored alignment preference.
    """
    mode=str(mode or 'LEGACY')
    if mode not in MODES:
        raise ValueError('Invalid placement mode')
    if mode not in {'INSIDE','OUTSIDE'}:
        return mode
    if open_inside_side not in {'LEFT','RIGHT'}:
        raise ValueError('Open Path inside side must be LEFT or RIGHT')
    points=list(points)
    if len(points)<2:
        raise ValueError('Path must contain at least two positions')
    # Keep XY-only closure; a Z mismatch on the repeated seam is invalidated
    # elsewhere and should not silently change the physical side here.
    def xy2(p):
        return ((float(p.x),float(p.y)) if hasattr(p,'x') else
                (float(p[0]),float(p[1])))
    first,last=xy2(points[0]),xy2(points[-1])
    closed=len(points)>3 and sum((a-b)**2 for a,b in zip(first,last))<1e-12
    inside=open_inside_side
    if closed:
        ring=[xy2(p) for p in points[:-1]]
        area=sum(a[0]*b[1]-b[0]*a[1]
                 for a,b in zip(ring,ring[1:]+ring[:1]))*.5
        if abs(area)<1e-9:
            raise ValueError('Closed Path with zero signed area cannot define INSIDE')
        inside='LEFT' if area>0 else 'RIGHT'
    return inside if mode=='INSIDE' else ('RIGHT' if inside=='LEFT' else 'LEFT')



def automatic_inset_side(mode, current='LEFT'):
    """Return the production side implied by wall placement.

    LEFT/RIGHT are user-facing placement choices for the whole ribbon; asking
    for a second independent side made the same physical state ambiguous and
    could move the thickness gizmo to the fixed edge. CENTER is geometrically
    symmetric, so LEFT is used as a deterministic semantic owner. LEGACY keeps
    the authored value for old projects.
    """
    mode = str(mode or 'LEGACY')
    current = current if current in ('LEFT','RIGHT') else 'LEFT'
    if mode == 'LEFT':
        return 'LEFT'
    if mode == 'RIGHT':
        return 'RIGHT'
    if mode == 'CENTER':
        return 'LEFT'
    return current


def left_signed_shift(mode, inset_distance, inset_side='LEFT', double=True):
    """Signed left offset for the *first/outer* wall shell.

    The second shell is always at shift +/- inset_distance according to
    inset_side. LEFT/RIGHT describe the whole ribbon in authored path space,
    regardless of which boundary was designated 'outer' for legacy topology.
    """
    # Backwards-compatible low-level shape contract. Source-dependent INSIDE/
    # OUTSIDE normally resolve before this function; an unbound call defaults
    # to the explicit open-path inside_side orientation only.
    if mode == 'INSIDE':
        mode = inset_side
    elif mode == 'OUTSIDE':
        mode = 'RIGHT' if inset_side == 'LEFT' else 'LEFT'
    if mode not in {'LEGACY','LEFT','CENTER','RIGHT'} or inset_side not in ('LEFT', 'RIGHT'):
        raise ValueError('Invalid wall alignment / inset side')
    width = float(inset_distance)
    if not isfinite(width) or width <= 0:
        raise ValueError('Wall thickness must be positive and finite')
    if mode == 'LEGACY':
        return 0.0
    if not double:
        return {'LEFT': width*0.5, 'CENTER': 0., 'RIGHT': -width*0.5}[mode]
    inner_sign = 1. if inset_side == 'LEFT' else -1.
    first = {'LEFT': (0., width), 'CENTER': (-width*.5, width*.5),
             'RIGHT': (-width, 0.)}[mode]
    return first[0] if inner_sign > 0 else first[1]


def left_signed_pair(mode, inset_distance, inset_side='LEFT', double=True, extra=0.):
    shift = left_signed_shift(mode, inset_distance, inset_side, double) + float(extra)
    if not isfinite(shift):
        raise ValueError('Wall offset must be finite')
    return (shift, shift + (float(inset_distance) if inset_side == 'LEFT' else -float(inset_distance))) if double else (shift, None)


def planter_extra_offset(auto, manual):
    """Auto means flush to the *actual* inner-wall boundary, never half width."""
    offset = 0. if auto else float(manual)
    if not isfinite(offset) or offset < 0:
        raise ValueError('Клумба: дополнительный отступ должен быть неотрицательным')
    return offset


def thickness_gizmo(mode, width, inset_side='LEFT', double=True):
    """(left-offset, drag-normal sign, displacement per unit thickness).

    A right-aligned double wall has its inner edge fixed at Path: anchoring
    the gizmo there makes drag appear broken. Attach it to the *moving* edge.
    Center uses half-speed physical motion on either side, so the drag handler
    compensates exactly by that 0.5 sensitivity.
    """
    width = float(width)
    if not double:
        if mode == 'CENTER':
            side = 1. if inset_side=='LEFT' else -1.
            return side*width*.5, side, .5  # virtual thickness edge
        if mode == 'LEGACY':
            side = 1. if inset_side=='LEFT' else -1.
            return side*width, side, 1. # legacy virtual thickness edge
        shift = left_signed_shift(mode,width,inset_side,False)
        direction = 1. if mode=='LEFT' else -1.
        return shift,direction,.5
    first, second = left_signed_pair(mode,width,inset_side,True)
    da, db = left_signed_pair(mode,width+1.,inset_side,True)
    da-=first;db-=second
    if abs(db)>=abs(da):
        return second, (1. if db>=0 else -1.),abs(db)
    return first,(1. if da>=0 else -1.),abs(da)


def resolve_wall_normal_side(signed_area, mode):
    """Physical side of a directed wall path, not a reversed UI label.

    A positive shoelace area (CCW contour) has its *interior* on the LEFT,
    hence outward on the RIGHT. The explicit PATH_ modes select their own
    physical sides even for open lines. 'OUTWARD' / 'INWARD' are defined only
    for nondegenerate, closed planar contours by the production caller.
    """
    from math import isfinite
    if not isfinite(float(signed_area)):
        raise ValueError('Nonfinite contour signed area')
    if mode == 'PATH_LEFT':
        return 'LEFT'
    if mode == 'PATH_RIGHT':
        return 'RIGHT'
    if mode not in {'OUTWARD','INWARD'}:
        raise ValueError('Unknown WALL normal mode')
    outside = 'RIGHT' if float(signed_area) >= 0.0 else 'LEFT'
    return outside if mode == 'OUTWARD' else ('LEFT' if outside == 'RIGHT' else 'RIGHT')


def preview_normal_side(points, *, closed, normal_mode):
    """Resolve the preview's physical facing from production's normal policy.

    OUTWARD/INWARD on open paths follow the build operator's documented
    PATH_RIGHT/PATH_LEFT normalization. No geometric offset is inferred.
    """
    mode = str(normal_mode or 'OUTWARD')
    if not closed:
        if mode == 'OUTWARD':
            mode = 'PATH_RIGHT'
        elif mode == 'INWARD':
            mode = 'PATH_LEFT'
        return resolve_wall_normal_side(0.0, mode)
    ring = list(points)
    if len(ring) > 2:
        def xy(v):
            return (float(v.x), float(v.y)) if hasattr(v, 'x') else (float(v[0]), float(v[1]))
        if sum((a-b)**2 for a,b in zip(xy(ring[0]),xy(ring[-1]))) < 1e-12:
            ring.pop()
    if len(ring) < 3:
        raise ValueError('Closed WALL preview requires at least three vertices')
    xy_points=[(float(v.x),float(v.y)) if hasattr(v,'x') else (float(v[0]),float(v[1])) for v in ring]
    area=.5*sum(a[0]*b[1]-b[0]*a[1]
                  for a,b in zip(xy_points,xy_points[1:]+xy_points[:1]))
    if abs(area)<1e-9:
        raise ValueError('Cannot infer normal side of zero-area closed path')
    return resolve_wall_normal_side(area,mode)
