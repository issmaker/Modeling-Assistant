"""Shared Surface-oriented cap patch predicates (no bpy dependency).

The Blender Surface tiling routine owns XY polygon clipping. These predicates
only decide when a 3-D ribbon patch needs a genuine geometric diagonal and
interpolate the authored height at the resulting Surface boundary points.
"""
from math import sqrt, isfinite, hypot


class CapClipPoint:
    """Float64-only intermediate for Surface UV clipping (Blender Mesh is f32)."""
    __slots__ = ('x', 'y', 'z')
    def __init__(self, values):
        self.x, self.y, self.z = map(float, values)
    def lerp(self, other, t):
        return CapClipPoint((self.x + (other.x-self.x)*t,
                             self.y + (other.y-self.y)*t,
                             self.z + (other.z-self.z)*t))


def area2(points):
    if not points:
        return 0.0
    x0, y0 = points[0].x, points[0].y
    return sum((a.x-x0)*(b.y-y0)-(b.x-x0)*(a.y-y0)
               for a, b in zip(points, points[1:] + points[:1]))


def triangle_area2(a, b, c):
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)


def patch_diagonal(patch):
    """Return two area-preserving XY triangles, respecting a concave miter."""
    if len(patch) != 4:
        raise ValueError('Cap patch requires four ordered vertices')
    target = abs(area2(patch))
    if target <= 1e-12:
        raise ValueError('Cap patch has zero projected area')
    for first, second in (((0, 1, 2), (0, 2, 3)),
                          ((0, 1, 3), (1, 2, 3))):
        value = abs(triangle_area2(*(patch[i] for i in first))) + abs(
            triangle_area2(*(patch[i] for i in second)))
        if all(abs(triangle_area2(*(patch[i] for i in tri))) > 1e-12
               for tri in (first, second)) and abs(value - target) <= max(1e-9, target * 1e-9):
            return first, second
    raise ValueError('Cap patch is folded or self-intersecting in XY')


def nonplanar_deviation(patch):
    """Maximum distance of a four-point ribbon patch from the first triangle plane."""
    if len(patch) != 4:
        raise ValueError('Cap patch requires four ordered vertices')
    first, second = patch_diagonal(patch)
    a, b, c = (patch[i] for i in first)
    d = patch[next(i for i in second if i not in first)]
    ab = (b.x-a.x, b.y-a.y, b.z-a.z)
    ac = (c.x-a.x, c.y-a.y, c.z-a.z)
    normal = (ab[1]*ac[2]-ab[2]*ac[1],
              ab[2]*ac[0]-ab[0]*ac[2],
              ab[0]*ac[1]-ab[1]*ac[0])
    norm = sqrt(sum(n*n for n in normal))
    if norm <= 1e-12:
        raise ValueError('Cap patch has a degenerate support triangle')
    return abs(sum(normal[i] * (v) for i, v in enumerate((d.x-a.x,d.y-a.y,d.z-a.z)))) / norm


def triangle_height(x, y, a, b, c, epsilon=1e-9):
    """Height of barycentric XY interpolation; None if outside the triangle."""
    denom = triangle_area2(a, b, c)
    if abs(denom) <= epsilon * epsilon:
        return None
    wb = ((x-a.x)*(c.y-a.y)-(y-a.y)*(c.x-a.x)) / denom
    wc = ((b.x-a.x)*(y-a.y)-(b.y-a.y)*(x-a.x)) / denom
    wa = 1.0-wb-wc
    if min(wa,wb,wc) < -epsilon or max(wa,wb,wc) > 1+epsilon:
        return None
    z = wa*a.z + wb*b.z + wc*c.z
    return z if isfinite(z) else None




def _barycentric_xy(x, y, a, b, c, epsilon=1e-12):
    """Return barycentric weights in XY, or None for a degenerate triangle."""
    denom = triangle_area2(a, b, c)
    if abs(denom) <= epsilon:
        return None
    wb = ((x-a.x)*(c.y-a.y)-(y-a.y)*(c.x-a.x)) / denom
    wc = ((b.x-a.x)*(y-a.y)-(b.y-a.y)*(x-a.x)) / denom
    return 1.0-wb-wc, wb, wc


def ribbon_coordinates_xy(patch, x, y, epsilon=1e-8):
    """Inverse bilinear coordinates for a convex cap ribbon patch.

    Patch order is OUTER_START, OUTER_END, INNER_END, INNER_START.
    ``s`` follows the wall segment and ``t`` goes across wall thickness.
    Unlike a support-triangle interpolation this mapping has no artificial
    full-strip diagonal, which is essential when authored path Z varies.
    """
    if len(patch) != 4:
        raise ValueError('Cap ribbon coordinates require four ordered vertices')
    a, b1, c1, d1 = patch[0], patch[1], patch[3], patch[2]
    bx, by = b1.x-a.x, b1.y-a.y
    cx, cy = c1.x-a.x, c1.y-a.y
    dx = d1.x-b1.x-c1.x+a.x
    dy = d1.y-b1.y-c1.y+a.y

    # A support triangle gives a very good deterministic Newton seed even for
    # strongly mitered quads.  The seed is not used as the final height field.
    s = t = 0.5
    triangles = patch_diagonal(patch)
    param = ((0.0,0.0),(1.0,0.0),(1.0,1.0),(0.0,1.0))
    for tri in triangles:
        weights = _barycentric_xy(x, y, *(patch[i] for i in tri))
        if weights is None or min(weights) < -1e-5:
            continue
        s = sum(w * param[i][0] for w, i in zip(weights, tri))
        t = sum(w * param[i][1] for w, i in zip(weights, tri))
        break

    # The input is already float64. A world-coordinate float32 envelope here
    # stopped Newton before it had found the boundary height (millimetres at
    # survey coordinates), letting the cap cross its straight wall rim.
    scale = max(1.0, *(hypot(p.x-a.x, p.y-a.y) for p in patch))
    target = max(1e-11, scale * 1e-12)
    for _ in range(32):
        fx = a.x + bx*s + cx*t + dx*s*t - x
        fy = a.y + by*s + cy*t + dy*s*t - y
        if hypot(fx, fy) <= target:
            break
        jsx, jsy = bx + dx*t, by + dy*t
        jtx, jty = cx + dx*s, cy + dy*s
        det = jsx*jty - jsy*jtx
        if abs(det) <= 1e-14:
            break
        ds = (-fx*jty + fy*jtx) / det
        dt = (-jsx*fy + jsy*fx) / det
        s += ds
        t += dt
        if not isfinite(s) or not isfinite(t):
            break

    px = a.x + bx*s + cx*t + dx*s*t
    py = a.y + by*s + cy*t + dy*s*t
    residual = hypot(px-x, py-y)
    envelope = max(target * 2.0, float(epsilon) * 4.0)
    if residual > envelope or s < -1e-5 or s > 1.0+1e-5 or t < -1e-5 or t > 1.0+1e-5:
        raise ValueError(
            'Cap Surface could not invert variable-height ribbon mapping; '
            f'xy=({x:.9f},{y:.9f}); st=({s:.9f},{t:.9f}); residual={residual:.9g}'
        )
    return max(0.0, min(1.0, s)), max(0.0, min(1.0, t))


def ribbon_height_at_xy(patch, x, y, epsilon=1e-8):
    """Smooth ruled/bilinear cap height without a full-strip diagonal crease."""
    s, t = ribbon_coordinates_xy(patch, x, y, epsilon=epsilon)
    z0 = patch[0].z*(1.0-t) + patch[3].z*t
    z1 = patch[1].z*(1.0-t) + patch[2].z*t
    z = z0*(1.0-s) + z1*s
    if not isfinite(z):
        raise ValueError('Cap Surface produced non-finite ribbon height')
    return z


def polygon_nonplanar_deviation(points):
    """Maximum point-to-plane distance for a polygon; triangles are planar."""
    if len(points) <= 3:
        return 0.0
    a = points[0]
    b = c = None
    for i in range(1, len(points)-1):
        ab = (points[i].x-a.x, points[i].y-a.y, points[i].z-a.z)
        for j in range(i+1, len(points)):
            ac = (points[j].x-a.x, points[j].y-a.y, points[j].z-a.z)
            normal = (ab[1]*ac[2]-ab[2]*ac[1],
                      ab[2]*ac[0]-ab[0]*ac[2],
                      ab[0]*ac[1]-ab[1]*ac[0])
            norm = sqrt(sum(v*v for v in normal))
            if norm > 1e-14:
                b, c = points[i], points[j]
                break
        if b is not None:
            break
    if b is None:
        return 0.0
    ab = (b.x-a.x, b.y-a.y, b.z-a.z)
    ac = (c.x-a.x, c.y-a.y, c.z-a.z)
    normal = (ab[1]*ac[2]-ab[2]*ac[1],
              ab[2]*ac[0]-ab[0]*ac[2],
              ab[0]*ac[1]-ab[1]*ac[0])
    norm = sqrt(sum(v*v for v in normal))
    return max(abs(normal[0]*(p.x-a.x)+normal[1]*(p.y-a.y)+normal[2]*(p.z-a.z))/norm
               for p in points)


def split_local_nonplanar_face(points, uvs, tolerance):
    """Triangulate only a non-planar UV-cell fragment, never the whole ribbon.

    Convex UV-cell fragments need no new vertices: a boundary fan is the
    smallest explicit topology Blender can use for a genuinely warped face.
    """
    if len(points) <= 3 or polygon_nonplanar_deviation(points) <= tolerance:
        return [(list(points), list(uvs))]
    return [([points[0], points[i], points[i+1]],
             [uvs[0], uvs[i], uvs[i+1]]) for i in range(1, len(points)-1)]

def height_at_xy(patch, x, y, triangles=None, epsilon=1e-7):
    """Reproduce authored boundary Z; do not create additional support geometry."""
    if triangles is None:
        triangles = patch_diagonal(patch)
    for tri in triangles:
        z = triangle_height(x, y, *(patch[i] for i in tri), epsilon=epsilon)
        if z is not None:
            return z
    # Surface clipping runs in a rotated, f32-backed Blender Vector frame.
    # Undoing that rotation can displace a legitimate boundary point by a few
    # ulps. Project ONLY points within a bounded f32 roundoff envelope back
    # onto an authored support triangle; a genuinely outside point still fails.
    extent = max(1.0, abs(x), abs(y), *(max(abs(p.x), abs(p.y)) for p in patch))
    snap_limit = max(float(epsilon), 4.0 * (2.0 ** -23) * extent)
    nearest = None
    for tri in triangles:
        a, b, c = (patch[i] for i in tri)
        for first, second in ((a,b),(b,c),(c,a)):
            dx, dy = second.x-first.x, second.y-first.y
            length2 = dx*dx + dy*dy
            if length2 <= 1e-24:
                continue
            t = max(0.0, min(1.0, ((x-first.x)*dx+(y-first.y)*dy)/length2))
            px, py = first.x+t*dx, first.y+t*dy
            distance = hypot(x-px, y-py)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, first.z+t*(second.z-first.z))
    if nearest is not None and nearest[0] <= snap_limit and isfinite(nearest[1]):
        return nearest[1]
    raise ValueError(
        'Cap Surface point lies outside its original ribbon patch; '
        f'xy=({x:.9f},{y:.9f}); boundary_distance={nearest[0] if nearest else None}; '
        f'roundoff_limit={snap_limit:.9f}; '
        f'patch={[(round(p.x, 8), round(p.y, 8), round(p.z, 8)) for p in patch]}'
    )


def reconcile_patch_boundary_xy(patch, x, y, epsilon=1e-7):
    """Reconcile a rounded UV-clipping boundary point with its authored patch.

    The XY coordinates of Blender ``mathutils.Vector`` are float32. A clipped
    vertex may land a few micrometres outside a legitimate patch, particularly
    around tile corners at world coordinates above ~100m. Only snap when the
    projected point is within a *bounded* float32 roundoff envelope of the
    actual patch boundary (never the artificial support triangle diagonal).
    The original clipping coverage and footprint validations remain in force.
    """
    x, y = float(x), float(y)
    if not isfinite(x) or not isfinite(y):
        raise ValueError('Cap Surface: non-finite clipped XY point')
    extent = max(1.0, abs(x), abs(y),
                 *(max(abs(p.x), abs(p.y)) for p in patch))
    # One float32 ULP is 2**-23 relative. Four ULPs allow Vector roundtrips
    # but are far below the 2mm semantic Weld distance.
    limit = max(float(epsilon), 4.0 * (2.0 ** -23) * extent)
    nearest = None
    for a, b in zip(patch, patch[1:] + patch[:1]):
        dx, dy = b.x-a.x, b.y-a.y
        length2 = dx*dx + dy*dy
        if length2 <= 1e-24:
            continue
        t = max(0.0, min(1.0, ((x-a.x)*dx+(y-a.y)*dy)/length2))
        px, py = a.x+t*dx, a.y+t*dy
        distance = hypot(x-px, y-py)
        if nearest is None or distance < nearest[0]:
            nearest = (distance, px, py)
    # Exact interior points must stay exactly where Surface clipping placed
    # them; this is only a precision guard at the authored outer boundary.
    inside = False
    for a, b in zip(patch, patch[1:] + patch[:1]):
        if (a.y > y) != (b.y > y):
            if x < a.x + (y-a.y)*(b.x-a.x)/(b.y-a.y):
                inside = not inside
    if inside:
        return x, y
    if nearest is not None and nearest[0] <= limit:
        return nearest[1], nearest[2]
    raise ValueError(
        'Cap Surface clipping point genuinely outside its ribbon patch; '
        f'xy=({x:.9f},{y:.9f}); boundary_distance={nearest[0] if nearest else None}; '
        f'limit={limit:.9f}; patch={[(tuple(round(v,8) for v in (p.x,p.y,p.z))) for p in patch]}'
    )


def is_convex_ccw(polygon, tolerance):
    """Convexity in rotated XY; input ring is counter-clockwise."""
    for index in range(len(polygon)):
        a,b,c = (polygon[j % len(polygon)] for j in (index,index+1,index+2))
        cross = (b.x-a.x)*(c.y-b.y) - (b.y-a.y)*(c.x-b.x)
        if cross < -tolerance*tolerance:
            return False
    return True


def cap_tile_fragments(rotated, support_triangles, needs_triangles,
                       x0, y0, tile, tolerance, convex_clip, triangle_clip):
    """Surface's verified convex tile clipping; no BMesh contextual_create."""
    if needs_triangles:
        return triangle_clip(support_triangles, x0, y0, tile, epsilon=tolerance)
    clipped = convex_clip(rotated, x0, x0+tile, y0, y0+tile, epsilon=tolerance)
    return [clipped] if len(clipped) >= 3 else []


def rotated_support(patch, triangle_indices, angle, vector_type, rotate, signed_area):
    """Shared XY basis for convex clipping and optional support triangles."""
    rotated=[vector_type((*rotate(p.x,p.y,angle),0.0)) for p in patch]
    if signed_area(rotated)<0.0:
        rotated.reverse()
    triangles=[[vector_type((*rotate(patch[i].x,patch[i].y,angle),0.0))
                for i in indices] for indices in triangle_indices]
    return rotated,triangles


def cap_cell_uv(clipped, u_min, v_min, tile, angle, rotate, source, source_uv):
    """Preserve existing UV tile mapping; independent of cap polygonization."""
    uvs=[]
    for point in clipped:
        ru,rv=rotate(point.x,point.y,angle)
        u=max(0.0,min(1.0,(ru-u_min)/tile))
        v=max(0.0,min(1.0,(rv-v_min)/tile))
        uvs.append(source_uv(source,u,v))
    return uvs


def cap_tile_ranges(rotated, tile, tolerance):
    """Integer tile range inclusive, robust at cell boundaries."""
    from math import floor
    min_u,max_u=min(p.x for p in rotated),max(p.x for p in rotated)
    min_v,max_v=min(p.y for p in rotated),max(p.y for p in rotated)
    a,b=floor(min_u/tile),floor((max_u-tolerance)/tile)
    c,d=floor(min_v/tile),floor((max_v-tolerance)/tile)
    return a,max(a,b),c,max(c,d)
