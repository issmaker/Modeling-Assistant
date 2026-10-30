from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-7


@dataclass
class SplitSpec:
    axis: str
    position: float
    thickness: float
    split_id: str = ""


def _uniq(values, eps=1e-8):
    out = []
    for value in sorted(values):
        if not out or abs(value - out[-1]) > eps:
            out.append(value)
    return out


def frame_grid(width, height, frame_width, splits, zone_tree=None):
    """Return the rectilinear 2D arrangement used by the whole frame.

    Outer frame and all mullions/transoms share this one grid.  No overlapping
    boxes are generated; intersections therefore reuse the same vertices/cells.
    """
    hx = width * 0.5
    hy = height * 0.5
    fw = max(EPS, min(frame_width, min(width, height) * 0.49))
    xs = [-hx, -hx + fw, hx - fw, hx]
    ys = [-hy, -hy + fw, hy - fw, hy]
    bands = []
    if zone_tree:
        if __package__:
            from .zone_core import layout
        else:  # Pure-core regression tests without importing Blender / bpy.
            from zone_core import layout
        _leaves, region_bands = layout(zone_tree,width,height,fw)
        bands = [row[-1] for row in region_bands]
        for x0,x1,y0,y1 in bands:
            xs.extend((x0,x1))
            ys.extend((y0,y1))
    for split in ([] if zone_tree else splits):
        t = max(EPS, split.thickness)
        if split.axis == 'V':
            xs += [split.position - t * 0.5, split.position + t * 0.5]
        elif split.axis == 'H':
            ys += [split.position - t * 0.5, split.position + t * 0.5]
    xs = _uniq([max(-hx, min(hx, x)) for x in xs])
    ys = _uniq([max(-hy, min(hy, y)) for y in ys])

    def active(cx, cy):
        if cx <= -hx + fw + EPS or cx >= hx - fw - EPS or cy <= -hy + fw + EPS or cy >= hy - fw - EPS:
            return True
        for x0,x1,y0,y1 in bands:
            if x0-EPS <= cx <= x1+EPS and y0-EPS <= cy <= y1+EPS:
                return True
        for split in ([] if zone_tree else splits):
            if split.axis == 'V' and abs(cx - split.position) <= split.thickness * 0.5 + EPS:
                return True
            if split.axis == 'H' and abs(cy - split.position) <= split.thickness * 0.5 + EPS:
                return True
        return False

    cells = {}
    for ix in range(len(xs) - 1):
        for iy in range(len(ys) - 1):
            cx = (xs[ix] + xs[ix + 1]) * 0.5
            cy = (ys[iy] + ys[iy + 1]) * 0.5
            cells[(ix, iy)] = active(cx, cy)
    return xs, ys, cells


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def build_frame_mesh(
    width,
    height,
    frame_width,
    depth,
    splits,
    optimize=False,
    interior_sign=-1,
    front_offset=0.0,
    zone_tree=None,
):
    """Build one connected frame mesh anchored to the wall facade plane.

    Local +Z is the detected exterior direction.  By default interior_sign=-1,
    therefore the exterior/front frame face is exactly at ``front_offset`` and
    the frame extends inward to ``front_offset - depth``.  This fixes the old
    centered-depth behaviour where the facade face was recessed by depth/2.
    """
    if interior_sign not in (-1, 1):
        raise ValueError('interior_sign must be -1 or +1')
    xs, ys, cells = frame_grid(width, height, frame_width, splits, zone_tree)
    exterior_sign = -interior_sign
    z_front = float(front_offset)
    z_back = z_front + interior_sign * float(depth)

    verts = []
    vid = {}
    faces = []
    roles = []

    def vertex(ix, iy, layer):
        key = (ix, iy, layer)
        if key not in vid:
            vid[key] = len(verts)
            verts.append((xs[ix], ys[iy], z_front if layer == 0 else z_back))
        return vid[key]

    def append_face(indices, role, desired_normal):
        pts = [verts[i] for i in indices]
        n = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
        if _dot(n, desired_normal) < 0:
            indices = tuple(reversed(indices))
        faces.append(tuple(indices))
        roles.append(role)

    nx = len(xs) - 1
    ny = len(ys) - 1

    # DWOS 2.1.6: frame/split grid and wall contact must be INDEPENDENT.
    # Splits subdivide the internal frame face, not the four-sided exterior
    # contact contour. Drawing the entire rectangular grid out to the outer
    # edge used to create dozens of unnecessary perimeter vertices (and a
    # T-junction against the host's four-vertex opening).
    #
    # Each mitered perimeter belt is triangulated against the existing inner
    # grid. Only the four opening corners exist at +/-width/2, +/-height/2.
    # The interior seam is precisely coincident with the original grid, so
    # connected mullions retain their topology without external subdivisions.
    def contact_strip(outer_a, outer_b, inner_points, layer, role, normal):
        if len(inner_points) < 2:
            raise ValueError('Нарушен внутренний контур рамы')
        a = vertex(*outer_a, layer)
        b = vertex(*outer_b, layer)
        inside = [vertex(ix, iy, layer) for ix, iy in inner_points]
        # Balanced fan. Unlike an n-gon with collinear vertices, every new
        # face has exactly three non-collinear points; no hidden degenerate
        # triangles are introduced by Blender's n-gon tessellator.
        midpoint = (len(inside) - 1) // 2
        for i in range(len(inside) - 1):
            tip = a if i < midpoint else b
            append_face((tip, inside[i], inside[i + 1]), role, normal)
        append_face((a, b, inside[midpoint]), role, normal)

    # Front belt exists in FULL and OPTIMIZED; rear belt only in FULL.
    # The four diagonal miter seams share exactly the same vertex indices.
    strips = (
        ((0, 0), (nx, 0), [(i, 1) for i in range(1, nx)]),
        ((0, ny), (nx, ny), [(i, ny - 1) for i in range(1, nx)]),
        ((0, 0), (0, ny), [(1, i) for i in range(1, ny)]),
        ((nx, 0), (nx, ny), [(nx - 1, i) for i in range(1, ny)]),
    )
    for layer, role, normal_sign in ((0, 'FRAME_EXTERIOR', exterior_sign),
                                     (1, 'FRAME_INTERIOR_BACK', interior_sign)):
        if layer == 1 and optimize:
            continue
        for outer_a, outer_b, inner_points in strips:
            contact_strip(outer_a, outer_b, inner_points, layer, role,
                          (0.0, 0.0, float(normal_sign)))

    for (ix, iy), enabled in cells.items():
        if not enabled:
            continue

        # Ring cells are represented by the four dedicated contact strips;
        # drawing their grid faces would recreate the redundant vertices.
        if 0 < ix < nx - 1 and 0 < iy < ny - 1:
            a = vertex(ix, iy, 0)
            b = vertex(ix + 1, iy, 0)
            c = vertex(ix + 1, iy + 1, 0)
            d = vertex(ix, iy + 1, 0)
            append_face((a, b, c, d), 'FRAME_EXTERIOR',
                        (0.0, 0.0, float(exterior_sign)))
            if not optimize:
                a1 = vertex(ix, iy, 1)
                b1 = vertex(ix + 1, iy, 1)
                c1 = vertex(ix + 1, iy + 1, 1)
                d1 = vertex(ix, iy + 1, 1)
                append_face((a1, b1, c1, d1), 'FRAME_INTERIOR_BACK',
                            (0.0, 0.0, float(interior_sign)))

        # Boundary sides of the 2D union. Adjacent active cells do not create an
        # internal face, which keeps the outer frame + splits as one clean topology.
        # Crucial for both FULL and OPTIMIZED: the four OUTER contact sides must
        # remain open. They are a joining interface with the wall, not part of
        # the manufactured frame surface. The front/back perimeter edges stay
        # in their respective sheets and can be welded to the wall boundary.
        neighbors = [((-1, 0), 'LEFT'), ((1, 0), 'RIGHT'), ((0, -1), 'BOTTOM'), ((0, 1), 'TOP')]
        for (dx, dy), side in neighbors:
            if cells.get((ix + dx, iy + dy), False):
                continue
            if ((side == 'LEFT' and ix == 0)
                    or (side == 'RIGHT' and ix == nx - 1)
                    or (side == 'BOTTOM' and iy == 0)
                    or (side == 'TOP' and iy == ny - 1)):
                # Skip before vertex() calls, otherwise OPTIMIZED creates
                # unused rear-ring vertices after opening the contact.
                continue
            if side == 'LEFT':
                quad = (vertex(ix, iy, 0), vertex(ix, iy, 1), vertex(ix, iy + 1, 1), vertex(ix, iy + 1, 0))
                outer = ix == 0
                # At a clear opening, LEFT is the left boundary of the
                # right-hand frame belt. Its outward normal points -X,
                # toward the glazing, not back into frame material.
                desired = (-1.0, 0.0, 0.0)
            elif side == 'RIGHT':
                quad = (vertex(ix + 1, iy, 0), vertex(ix + 1, iy + 1, 0), vertex(ix + 1, iy + 1, 1), vertex(ix + 1, iy, 1))
                outer = ix == nx - 1
                desired = (1.0, 0.0, 0.0)
            elif side == 'BOTTOM':
                quad = (vertex(ix, iy, 0), vertex(ix + 1, iy, 0), vertex(ix + 1, iy, 1), vertex(ix, iy, 1))
                outer = iy == 0
                desired = (0.0, -1.0, 0.0)
            else:
                quad = (vertex(ix, iy + 1, 0), vertex(ix, iy + 1, 1), vertex(ix + 1, iy + 1, 1), vertex(ix + 1, iy + 1, 0))
                outer = iy == ny - 1
                desired = (0.0, 1.0, 0.0)
            append_face(quad, 'FRAME_REVEAL', desired)

    return {
        'verts': verts,
        'faces': faces,
        'roles': roles,
        'xs': xs,
        'ys': ys,
        'cells': cells,
        'z_front': z_front,
        'z_back': z_back,
        'interior_sign': interior_sign,
        'exterior_sign': exterior_sign,
        'outer_contact_policy': 'OPEN_FOR_WELD',
        'outer_contact_corners': 4,
    }


def clear_rectangles(width, height, frame_width, splits):
    """Connected clear cells, retained for viewport/split diagnostics."""
    xs, ys, cells = frame_grid(width, height, frame_width, splits)
    seen = set()
    rects = []
    for key, enabled in cells.items():
        if enabled or key in seen:
            continue
        stack = [key]
        comp = []
        seen.add(key)
        while stack:
            current = stack.pop()
            comp.append(current)
            x, y = current
            for neigh in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neigh in seen or cells.get(neigh, True):
                    continue
                seen.add(neigh)
                stack.append(neigh)
        minx = min(c[0] for c in comp)
        maxx = max(c[0] for c in comp) + 1
        miny = min(c[1] for c in comp)
        maxy = max(c[1] for c in comp) + 1
        rects.append((xs[minx], xs[maxx], ys[miny], ys[maxy]))
    return rects


def main_clear_rectangle(width, height, frame_width):
    """One full glass/interior aperture inside the outer frame.

    Splits sit in front of the glass and therefore do not physically chop the
    glass into disconnected islands.
    """
    hx = width * 0.5
    hy = height * 0.5
    fw = max(EPS, min(frame_width, min(width, height) * 0.49))
    return (-hx + fw, hx - fw, -hy + fw, hy - fw)


def perimeter_parameters(width, height, frame_width, splits):
    """Four-sided wall contact (not the independent interior split grid).

    This is intentionally only two endpoints on each side, even for a heavily
    subdivided window. Call frame_grid() for the INTERNAL split arrangement.
    """
    hx = width * 0.5
    hy = height * 0.5
    return {
        'bottom': [(-hx, -hy), (hx, -hy)],
        'top': [(-hx, hy), (hx, hy)],
        'left': [(-hx, -hy), (-hx, hy)],
        'right': [(hx, -hy), (hx, hy)],
    }


def interior_centerline_rectangle(width,height,frame_width):
    """Centerline of outer frame belt, NOT the inner clear aperture.

    Position = halfway between outer boundary and inner aperture edge.
    A positive user clearance further insets this contour.
    """
    # Standalone geometry module (also imported directly by pure-core QA).
    import math
    if not all(math.isfinite(v) for v in (width,height,frame_width)):
        raise ValueError('Frame centerline: NaN/Inf')
    if frame_width <= EPS or 2*frame_width >= min(width,height)-EPS:
        raise ValueError('Frame centerline: нет свободного пространства')
    inset=frame_width*.5
    return (-width*.5+inset,width*.5-inset,-height*.5+inset,height*.5-inset)
