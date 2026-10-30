"""Blender-free geometry contract for the Unified Issue Navigator HUD.

Coordinates are editor WINDOW pixels; no scene/mesh access.  Both the
renderer and the hit-testing gizmos consume the SAME layout function.
"""
from math import cos, sin, pi

BUTTONS = 4


def layout(center_x, center_y, scale=1.0):
    if not 0.5 <= scale <= 2.0:
        raise ValueError('HUD scale outside valid interval')
    size = 36.0 * scale
    step = 46.0 * scale
    xs = tuple(center_x + (i-1.5)*step for i in range(BUTTONS))
    # panel contains buttons, status and padding; status BELOW buttons.
    panel = (center_x - 111.0*scale, center_y - 49.0*scale,
             center_x + 111.0*scale, center_y + 33.0*scale)
    label_y = center_y - 36.0*scale
    return {'buttons': tuple((x, center_y+5.0*scale, size) for x in xs),
            'panel': panel, 'label_anchor': (center_x, label_y),
            'scale': scale}


def rounded_rect(x0, y0, x1, y1, radius, segments=7):
    """Return boundary vertices counterclockwise; safe for tiny rectangles."""
    if x1 <= x0 or y1 <= y0:
        return ()
    r = max(0.0, min(radius, (x1-x0)/2, (y1-y0)/2))
    vertices = []
    corners = ((x1-r, y0+r, -pi/2), (x1-r, y1-r, 0),
               (x0+r, y1-r, pi/2), (x0+r, y0+r, pi))
    for cx, cy, start in corners:
        for k in range(segments+1):
            angle = start + (pi/2)*k/segments
            vertices.append((cx + r*cos(angle), cy+r*sin(angle)))
    return tuple(vertices)


def triangulate_fan(perimeter):
    """Filled convex polygon, flat list of vertices for shader TRIS."""
    if len(perimeter) < 3:
        return ()
    out = []
    a = perimeter[0]
    for i in range(1,len(perimeter)-1):
        out.extend((a, perimeter[i], perimeter[i+1]))
    return tuple(out)


def icon_segments(slot, cx, cy, s, weld=False):
    """Line segments in screen pixels. Red close icon is always slot 3."""
    half = s*0.26
    if slot == 0:
        points = ((cx+half*.45,cy+half), (cx-half*.55,cy), (cx+half*.45,cy-half))
    elif slot == 1:
        points = ((cx-half*.45,cy+half), (cx+half*.55,cy), (cx-half*.45,cy-half))
    elif slot == 2 and weld:
        points = ((cx-half*.9,cy), (cx-half*.17,cy-half*.65), (cx+half*.95,cy+half*.83))
    elif slot == 2:
        points = ((cx-half*.8,cy+half*.9),(cx+half*.9,cy+half*.08),
                  (cx+half*.12,cy-half*.12),(cx-half*.13,cy-half*.9),
                  (cx-half*.8,cy+half*.9))
    else:
        a=(cx-half*.65,cy-half*.65); b=(cx+half*.65,cy+half*.65)
        c=(cx-half*.65,cy+half*.65); d=(cx+half*.65,cy-half*.65)
        return ((a,b),(c,d))
    return tuple(zip(points, points[1:]))


def thick_segment(a,b,width):
    """Two triangles for a screen-space stroked segment; no GPU wide-lines."""
    dx=b[0]-a[0];dy=b[1]-a[1]
    length=(dx*dx+dy*dy)**0.5
    if length <= 1e-8:return ()
    nx=-dy/length*width*.5;ny=dx/length*width*.5
    p=(a[0]+nx,a[1]+ny);q=(a[0]-nx,a[1]-ny)
    r=(b[0]-nx,b[1]-ny);s=(b[0]+nx,b[1]+ny)
    return (p,q,r,p,r,s)
