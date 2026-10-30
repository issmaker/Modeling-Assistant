"""Topology-first authoring plan for vertical wall panels and shared WALL/CAP vertices.

Blender-independent geometry *planning*. UVs remain Blender per-loop and are
not allowed to force unrelated mesh segments to inherit remote-Z divisions.
"""
from math import ceil, floor, hypot, isfinite


def path_module_phase(points, *, stories_down=True, flat_tolerance=1e-8):
    """Plan one vertical module phase for PRIMARY and its derived SECONDARY.

    Based on the exact authored 3D path, not on object/world Z=0 and not on
    Offset Edges vertices.  MAIN and SECONDARY can use independent horizontal
    module stations while inheriting this ONE vertical phase.

    Flat   -> the authored path height (both UP and DOWN).
    Varied -> lowest point for UP; highest point for DOWN.
    """
    if not points:
        raise ValueError("Path module phase: empty authored path")
    if not isfinite(float(flat_tolerance)) or not 0 <= flat_tolerance <= 1e-4:
        raise ValueError("Path module phase: invalid flat tolerance")
    heights = []
    for index, point in enumerate(points):
        try:
            if len(point) != 3:
                raise ValueError("expected 3 coordinates")
            xyz = tuple(float(component) for component in point)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Path module phase: malformed vertex {index}") from exc
        if not all(isfinite(v) for v in xyz):
            raise ValueError(f"Path module phase: nonfinite vertex {index}")
        heights.append(xyz[2])
    low, high = min(heights), max(heights)
    span = high - low
    if not isfinite(span):
        raise ValueError("Path module phase: nonfinite height span")
    flat = span <= flat_tolerance
    anchor = low if (flat or not stories_down) else high
    # 'flat' near a numerical tolerance gets one real authored Z value;
    # no implicit rounding or mutation of any point is performed.
    return {
        "anchor_z": anchor,
        "min_z": low,
        "max_z": high,
        "flat": flat,
        "direction": "DOWN" if stories_down else "UP",
        "rule": "AUTHORED_FLAT" if flat else ("PATH_MAX_DOWN" if stories_down else "PATH_MIN_UP"),
        "point_count": len(heights),
        "shared_secondary_phase": True,
    }


def stable_path_z_anchor(points, *, stories_down=True):
    """Compatibility entry point: immutable authored Path's extremal Z phase."""
    return path_module_phase(points, stories_down=stories_down)["anchor_z"]


def local_vertical_slabs(start_bot, start_top, end_bot, end_top, *,
                         path_anchor_z, uv_anchor_z, tile_height,
                         story_heights=(), stories_down=True,
                         include_story_cuts=False, max_levels=20000,
                         grid_anchor_z=None):
    """Z cells intersecting ONE station pair, not the entire wall.

    Story/UV bounds that do not cross the local segment are excluded before
    geometry creation. All segments use the same UV phase, so compatible
    boundaries meet at identical Z. Never shift authored XYZ coordinates.
    """
    inputs = (start_bot, start_top, end_bot, end_top,
              path_anchor_z, uv_anchor_z, tile_height)
    if any(not isfinite(float(v)) for v in inputs):
        raise ValueError("Topology-first: nonfinite Z")
    if tile_height <= 0 or max_levels < 1:
        raise ValueError("Topology-first: invalid tile parameters")
    if start_bot > start_top or end_bot > end_top:
        raise ValueError("Topology-first: inverted wall span")
    zmin = min(float(start_bot), float(end_bot))
    zmax = max(float(start_top), float(end_top))
    if zmax-zmin <= 1e-10:
        return ()
    floors = []
    z = float(path_anchor_z)
    direction = -1.0 if stories_down else 1.0
    for h in story_heights:
        h = float(h)
        if not isfinite(h) or h <= 0:
            raise ValueError("Topology-first: non-positive story height")
        floors.append(z)
        z += direction*h
    floors.append(z)
    if not include_story_cuts:
        # Height/stories determine the WALL envelope; their internal labels
        # are not physical splits when role/material/UV map do not change.
        floors = []
    # Always retain the path-anchored physical module grid. An explicitly
    # selected legacy WORLD UV mode adds its own UV boundaries for valid
    # unwrapped [0,1] tiles; it must never replace physical module seams.
    physical_z = float(path_anchor_z if grid_anchor_z is None else grid_anchor_z)
    if not isfinite(physical_z):
        raise ValueError("Topology-first: nonfinite module grid anchor")
    grid_phases = (physical_z,) if abs(physical_z-float(uv_anchor_z)) <= 1e-9 else (physical_z, float(uv_anchor_z))
    ranges = [(floor((zmin-phase)/tile_height), ceil((zmax-phase)/tile_height))
              for phase in grid_phases]
    if sum(end-start+1 for start,end in ranges) + len(floors) > max_levels:
        raise ValueError("Topology-first: too many local vertical cells")
    eps = min(1e-7, float(tile_height)*1e-8)
    levels = [zmin,zmax]
    for boundary in floors:
        if zmin+eps < boundary < zmax-eps:
            levels.append(boundary)
    for phase, (kmin,kmax) in zip(grid_phases,ranges):
        for k in range(kmin,kmax+1):
            boundary = phase+k*float(tile_height)
            if zmin+eps < boundary < zmax-eps:
                levels.append(boundary)
    levels.sort()
    ordered = []
    for boundary in levels:
        if not ordered or boundary - ordered[-1] > eps:
            ordered.append(boundary)
    if ordered[0] != zmin:
        ordered[0] = zmin
    if ordered[-1] != zmax:
        ordered[-1] = zmax
    return tuple((lo,hi,float(uv_anchor_z))
                 for lo,hi in zip(ordered,ordered[1:]) if hi-lo > eps)


def preweld_shared_vertices(vertices, faces, *, structural_count,
                            cap_vertex_ids=(), tolerance=1e-8):
    """Share exact vertex indices *before* Mesh creation, preserving loop UV.

    Handles cap↔wall (and wall↔wall) coincident vertices. Does not stitch a
    vertex falling inside another edge; the local T-junction solver handles
    those. Reject rather than collapsing any polygon under the tolerance.
    """
    if tolerance <= 0 or not isfinite(tolerance):
        raise ValueError("Topology-first: invalid preweld tolerance")
    if not 0 <= structural_count <= len(vertices):
        raise ValueError("Topology-first: invalid structural index boundary")
    # Spatial bins + adjacent cells handle quantization boundaries correctly.
    bins = {}
    output = []
    remap = []
    tol2 = tolerance*tolerance
    for point in vertices:
        pos = tuple(float(c) for c in point)
        if len(pos)!=3 or any(not isfinite(c) for c in pos):
            raise ValueError("Topology-first: malformed vertex")
        cell = tuple(floor(c/tolerance) for c in pos)
        nearest = None
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    for idx in bins.get((cell[0]+dx,cell[1]+dy,cell[2]+dz),()):
                        other = output[idx]
                        if sum((a-b)**2 for a,b in zip(pos,other)) <= tol2:
                            nearest = idx
                            break
                    if nearest is not None: break
                if nearest is not None: break
            if nearest is not None: break
        if nearest is None:
            nearest=len(output)
            output.append(pos)
            bins.setdefault(cell,[]).append(nearest)
        remap.append(nearest)
    new_faces = [tuple(remap[int(v)] for v in face) for face in faces]
    for i, face in enumerate(new_faces):
        if len(face) < 3 or len(set(face)) != len(face):
            raise ValueError(f"Topology-first: preweld would degenerate face {i}")
    cap_ids = sorted(set(remap[int(i)] for i in cap_vertex_ids))
    return {
        "vertices":output, "faces":new_faces,
        "cap_vertex_ids":cap_ids,
        "structural_count":len(set(remap[:structural_count])),
        "saved_vertices":len(vertices)-len(output),
        "shared_cap_to_wall":sum(1 for i in cap_vertex_ids
                                 if remap[int(i)] < len(set(remap[:structural_count]))),
    }
