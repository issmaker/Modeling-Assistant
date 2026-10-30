"""Blender-independent Cornice Sharp experimental frame methods."""
from math import hypot, isfinite
MODES = (
 ('CLASSIC', 'Classic · Miter', 'Точный исходный бисектор'),
 ('BISECTOR', 'Bisector · Compact', 'Биссектриса без растяжения профиля'),
 ('INCOMING', 'Previous · Safe Bias', 'Безопасный наклон сечения к входящему ребру'),
 ('OUTGOING', 'Next · Safe Bias', 'Безопасный наклон сечения к выходящему ребру'),
 ('LIMITED', 'Bounded · 1.35×', 'Ограниченный вынос на угле'),
)
MODE_IDS = frozenset(m[0] for m in MODES)
MODE_LABELS = {m[0]: m[1] for m in MODES}

def select_frame(previous, corner, following, side_sign, mode):
    """(normal XY, multiplier); never changes the authored center XYZ."""
    if mode not in MODE_IDS:
        raise ValueError('Unknown corner method: '+str(mode))
    if side_sign not in (1,-1):
        raise ValueError('Invalid side sign')
    def normal(a,b):
        dx,dy=b[0]-a[0],b[1]-a[1]
        r=hypot(dx,dy)
        if r <= 1.e-10 or not isfinite(r):
            raise ValueError('Degenerate corner segment')
        return (-dy/r*side_sign,dx/r*side_sign)
    a,b=normal(previous,corner),normal(corner,following)
    sx,sy=a[0]+b[0],a[1]+b[1]
    length=hypot(sx,sy)
    if length<1.e-8:
        raise ValueError('Ambiguous 180-degree reversal')
    mid=(sx/length,sy/length)
    denom=mid[0]*b[0]+mid[1]*b[1]
    if denom<=1.e-8:
        raise ValueError('Unsafe corner miter')
    exact=1./denom
    if mode=='CLASSIC':return mid,exact
    if mode=='BISECTOR':return mid,1.
    # A pure incoming/outgoing frame at a *single* authored corner creates
    # folded sweep triangles on the opposite side of the corner. These were
    # incorrectly presented as square trims; a square joint needs TWO
    # independently trimmed segment meshes and is not representable with one
    # sweep cross-section. Use a bounded, distinct directional bias instead.
    if mode in ('INCOMING','OUTGOING'):
        wa,wb=(0.65,0.35) if mode=='INCOMING' else (0.35,0.65)
        vx,vy=wa*a[0]+wb*b[0],wa*a[1]+wb*b[1]
        norm=hypot(vx,vy)
        if norm<=1.e-8:raise ValueError('Unsafe corner biased frame')
        return (vx/norm,vy/norm),1.
    if mode=='LIMITED':return mid,min(exact,1.35)
    raise AssertionError(mode)
