from __future__ import annotations


def _face_for_normal(base, normal_sign):
    # Vertex order 0,1,2,3 produces +Z. Reverse for -Z.
    return (base, base + 1, base + 2, base + 3) if normal_sign > 0 else (base, base + 3, base + 2, base + 1)


def build_glass_mesh(rect, depth_center=0.0, offset=0.0, full=False, gap=0.075, interior_sign=-1):
    """Build one full pane (OPTIMIZED) or two full panes (FULL).

    Splits do not physically chop the glazing.  This mirrors the intended DWOS
    production workflow: one common glass sheet behind the frame/mullion network.
    """
    if isinstance(rect, (list, tuple)) and len(rect) == 1 and isinstance(rect[0], (list, tuple)):
        rect = rect[0]
    x0, x1, y0, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return [], [], []
    center = float(depth_center) + float(offset)
    verts = []
    faces = []
    tags = []
    exterior_sign = -int(interior_sign)

    if full:
        # Positive direction along interior_sign means deeper toward interior.
        z_ext = center - interior_sign * float(gap) * 0.5
        z_int = center + interior_sign * float(gap) * 0.5
        planes = [
            (z_ext, exterior_sign, 'GLASS_EXTERIOR'),
            (z_int, interior_sign, 'GLASS_INTERIOR'),
        ]
    else:
        planes = [(center, exterior_sign, 'GLASS_SINGLE')]

    for z, normal_sign, tag in planes:
        base = len(verts)
        verts += [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
        faces.append(_face_for_normal(base, normal_sign))
        tags.append(tag)
    return verts, faces, tags
