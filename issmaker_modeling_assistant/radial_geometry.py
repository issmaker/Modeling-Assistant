"""Blender-free math for a centered, bounded view-mode radial HUD."""
from math import atan2, cos, sin, pi, hypot

MODES = (
    ('usual', 'Solid', 'SOLID'),
    ('normals', 'Normals', 'NORMAL'),
    ('zfight', 'Z-fight', 'DEPTH'),
    ('night', 'Night', 'MOON'),
    ('ucx', 'UCX', 'CUBE'),
    ('ucx2', 'UCX 2', 'CUBES'),
    ('textured', 'Textures', 'GRID'),
    ('materials', 'Materials', 'MATERIAL'),
)

# Tool slots come from the shared registry; adding a module does not change the HUD.
from .core.registry import MENU_TOOLS
TOOL_MODES = tuple((spec.tool_id, {'BRICK_HOLE':'Openings','DWOS':'Windows\n& Doors','COLLISION':'Collisions','UV_WALL':'Walls'}[spec.tool_id], spec.icon) for spec in MENU_TOOLS)
DISABLED_TOOL_IDS = frozenset(spec.tool_id for spec in MENU_TOOLS if not spec.enabled)

# Quick Select retains eight wedges. Tools use four generous 90-degree sectors.
QUICK_MODES = (
    ('UV', 'UV', 'UV'),
    ('UDIM', 'UDIM', 'UDIM'),
    ('SEAM', 'Seam', 'SEAM'),
    ('SHARP', 'Sharp', 'SHARP'),
    ('NORMAL', 'Normal', 'NORMAL'),
    ('MATERIAL', 'Material', 'MATERIAL'),
    ('TEXTURE_COLOR', 'Texture\nColor', 'TEXTURE_COLOR'),
    ('OFF', 'Off', 'OFF'),
)

def radial_scale(width, height, button_scale=1.0):
    """Scale down instead of placing unclickable sectors outside an editor."""
    if width < 260 or height < 260:
        return None
    fit = min((width - 32.0)/340., (height - 32.0)/340., 1.0)
    return min(max(.55, fit), max(.8, min(1.3, button_scale)))


def polar_pick(dx, dy, scale=1.0, sectors=8):
    """Return 0..sectors-1, center -2 or outside -1; 4 tools, 8 view/quick."""
    if sectors not in (4, 5, 8):
        raise ValueError("Supported wheel sectors: 4, 5 or 8")
    r = hypot(dx, dy)
    if r <= 42.0*scale:
        return -2
    if not 68.0*scale <= r <= 157.0*scale:
        return -1
    # Segment zero at top; progress clockwise.
    angle = (pi/2 - atan2(dy, dx)) % (2*pi)
    step = 2*pi/sectors
    if sectors in (4,5) and abs(((angle + step/2) % step) - step/2) > step/2 - .042:
        return -1
    return int((angle + step/2) // step) % sectors


def point_for(index, radius, scale=1.0, sectors=8):
    angle = pi/2 - (index % sectors) * 2*pi/sectors
    return (cos(angle)*radius*scale, sin(angle)*radius*scale)


def sector_polygon(index, inner, outer, scale=1.0, slices=18, sectors=8):
    """Annular sector, same geometry as the matching hit area."""
    center_angle = pi/2 - index*2*pi/sectors
    spread = pi/sectors - .042
    angles = [center_angle-spread+2*spread*i/slices for i in range(slices+1)]
    points = [(cos(a)*outer*scale,sin(a)*outer*scale) for a in angles]
    points += [(cos(a)*inner*scale,sin(a)*inner*scale) for a in reversed(angles)]
    return tuple(points)


def ease_out(t):
    t=max(0.,min(1.,float(t)))
    return 1.-(1.-t)**3


def appearance_pick(dx,dy,scale=1.):
    """Separate lower settings chip: does not replace any of four tool wedges."""
    return (-161.*scale <= dx <= -7.*scale and -220.*scale <= dy <= -173.*scale)


def geometry_pick(dx,dy,scale=1.):
    return (7.*scale <= dx <= 161.*scale and -220.*scale <= dy <= -173.*scale)
