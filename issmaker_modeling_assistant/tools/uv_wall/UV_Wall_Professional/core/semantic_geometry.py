"""Semantic metadata helpers for generated UV Wall geometry.

The generated mesh is not the source of truth, but preserving semantic roles on
its faces makes validation, diagnostics and future direct-manipulation reliable.
"""
from __future__ import annotations

import bpy
import bmesh
from mathutils import Vector
from .nearest_segments import NearestSegments

FACE_ROLE_ATTRIBUTE = "uvwall_face_role"

FACE_ROLE_CODES = {
    "UNKNOWN": 0,
    "WALL_OUTER": 10,
    "WALL_INNER": 11,
    "END_CAP": 12,
    "TOP_CAP": 20,
    "BOTTOM_CAP": 21,
    "SURFACE": 30,
    "CORNICE": 40,
}
FACE_ROLE_NAMES = {value: key for key, value in FACE_ROLE_CODES.items()}


def _face_attribute(mesh, create=False):
    attr = mesh.attributes.get(FACE_ROLE_ATTRIBUTE)
    if attr is None and create:
        attr = mesh.attributes.new(FACE_ROLE_ATTRIBUTE, type="INT", domain="FACE")
    return attr


def set_face_roles(mesh, roles):
    """Assign a semantic role to every face.

    ``roles`` can contain symbolic names or integer codes. The list must match
    the mesh face count so a stale pre-cleanup role map can never be applied to
    a changed topology silently.
    """
    if len(roles) != len(mesh.polygons):
        raise ValueError(
            f"Semantic face roles mismatch: roles={len(roles)}, faces={len(mesh.polygons)}"
        )
    attr = _face_attribute(mesh, create=True)
    for index, role in enumerate(roles):
        code = FACE_ROLE_CODES.get(role, role) if isinstance(role, str) else int(role)
        attr.data[index].value = int(code)


def face_role_codes(mesh):
    attr = _face_attribute(mesh, create=False)
    if attr is None or len(attr.data) != len(mesh.polygons):
        return [FACE_ROLE_CODES["UNKNOWN"]] * len(mesh.polygons)
    return [int(item.value) for item in attr.data]


def face_role_name(code):
    return FACE_ROLE_NAMES.get(int(code), "UNKNOWN")


def role_counts(mesh):
    counts = {}
    for code in face_role_codes(mesh):
        name = face_role_name(code)
        counts[name] = counts.get(name, 0) + 1
    return counts


def validate_and_fix_role_normals(mesh, epsilon=1.0e-6):
    """Orient cap faces by their semantic role without guessing from XY shape.

    Wall/end/cornice faces keep their construction winding. TOP_CAP and SURFACE
    are expected +Z; BOTTOM_CAP is expected -Z. If a cap face was reversed by a
    cleanup/weld operation, flip only that face in BMesh. Returns a diagnostic
    dictionary.
    """
    if len(mesh.polygons) == 0:
        return {"flipped": 0, "violations": 0, "counts": {}}

    attr = _face_attribute(mesh, create=False)
    if attr is None:
        return {"flipped": 0, "violations": 0, "counts": {"UNKNOWN": len(mesh.polygons)}}

    role_values = face_role_codes(mesh)
    bm = bmesh.new()
    flipped = 0
    violations = 0
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        for face in bm.faces:
            code = role_values[face.index] if face.index < len(role_values) else FACE_ROLE_CODES["UNKNOWN"]
            if code in {FACE_ROLE_CODES["TOP_CAP"], FACE_ROLE_CODES["SURFACE"]}:
                if face.normal.z < -epsilon:
                    face.normal_flip()
                    flipped += 1
            elif code == FACE_ROLE_CODES["BOTTOM_CAP"]:
                if face.normal.z > epsilon:
                    face.normal_flip()
                    flipped += 1
        bm.normal_update()
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()

    # Validate again on the final Mesh normals.
    final_roles = face_role_codes(mesh)
    for polygon in mesh.polygons:
        code = final_roles[polygon.index]
        if code in {FACE_ROLE_CODES["TOP_CAP"], FACE_ROLE_CODES["SURFACE"]} and polygon.normal.z < -epsilon:
            violations += 1
        elif code == FACE_ROLE_CODES["BOTTOM_CAP"] and polygon.normal.z > epsilon:
            violations += 1

    return {"flipped": flipped, "violations": violations, "counts": role_counts(mesh)}



def validate_and_fix_wall_normals(mesh, path_points, inset_side='LEFT', create_inner=True,
                                  outer_normal_side=None, epsilon=1.0e-6):
    """Guarantee deterministic WALL_OUTER/WALL_INNER winding.

    The generated wall mesh is authored in world coordinates.  For a double
    wall the ribbon itself defines the unambiguous outside: the first boundary
    faces away from the second boundary, while the second boundary faces in the
    opposite direction.  For a single wall we retain the resolved production
    normal side.  Cleanup/weld may change face winding; this validator repairs
    only semantic wall faces and then verifies the result.
    """
    if not mesh.polygons:
        return {"flipped": 0, "violations": 0, "checked": 0}
    points=list(path_points or [])
    segments=[]
    for a,b in zip(points,points[1:]):
        dx=float(b.x-a.x); dy=float(b.y-a.y)
        length2=dx*dx+dy*dy
        if length2 <= epsilon*epsilon:
            continue
        inv=length2**-0.5
        left=( -dy*inv, dx*inv )
        segments.append((float(a.x),float(a.y),dx,dy,length2,left))
    if not segments:
        return {"flipped": 0, "violations": 0, "checked": 0}
    roles=face_role_codes(mesh)
    outer_code=FACE_ROLE_CODES["WALL_OUTER"]
    inner_code=FACE_ROLE_CODES["WALL_INNER"]

    # Exact segment AABB index replaces an O(faces * Path) full scan.
    # Equal-distance ties always select the first authored segment, as before.
    segment_index = NearestSegments(segments)
    # Default bounded reference parity; exhaustive only when explicitly
    # requested on a diagnostic copy of the Blender scene.
    if bool(bpy.context.scene.get('uvwall_geometry_truth_full', False)):
        segment_index.set_truth_mode('FULL')

    def nearest_left(cx, cy):
        index = segment_index.nearest_index(cx, cy)
        return segments[index][5] if index is not None else None

    def expected_sign(code):
        if create_inner:
            # Inner boundary lies on inset_side; its outward normal is that side.
            side=inset_side if code==inner_code else ('RIGHT' if inset_side=='LEFT' else 'LEFT')
        else:
            side=outer_normal_side or ('RIGHT' if inset_side=='LEFT' else 'LEFT')
        return 1.0 if side=='LEFT' else -1.0

    bm=bmesh.new(); flipped=0; checked=0
    try:
        bm.from_mesh(mesh); bm.faces.ensure_lookup_table()
        for face in bm.faces:
            code=roles[face.index] if face.index<len(roles) else FACE_ROLE_CODES['UNKNOWN']
            if code not in {outer_code,inner_code}:
                continue
            center=face.calc_center_median()
            left=nearest_left(float(center.x),float(center.y))
            if left is None:
                continue
            ex=left[0]*expected_sign(code); ey=left[1]*expected_sign(code)
            dot=float(face.normal.x)*ex+float(face.normal.y)*ey
            checked+=1
            if dot < -epsilon:
                face.normal_flip(); flipped+=1
        bm.normal_update(); bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    violations=0
    roles=face_role_codes(mesh)
    for poly in mesh.polygons:
        code=roles[poly.index] if poly.index<len(roles) else FACE_ROLE_CODES['UNKNOWN']
        if code not in {outer_code,inner_code}:
            continue
        center=poly.center
        left=nearest_left(float(center.x),float(center.y))
        if left is None:
            continue
        ex=left[0]*expected_sign(code); ey=left[1]*expected_sign(code)
        if float(poly.normal.x)*ex+float(poly.normal.y)*ey < -epsilon:
            violations+=1
    return {"flipped": flipped, "violations": violations, "checked": checked,
            "geometry_truth": segment_index.truth_report()}



def validate_face_role_coverage(mesh, allowed_roles=None):
    codes = face_role_codes(mesh)
    unknown = sum(1 for code in codes if code == FACE_ROLE_CODES["UNKNOWN"])
    invalid = []
    if allowed_roles is not None:
        allowed_codes = {FACE_ROLE_CODES[name] for name in allowed_roles}
        invalid = [index for index, code in enumerate(codes) if code not in allowed_codes]
    if unknown or invalid:
        raise ValueError(
            f"Semantic face-role coverage failed: unknown={unknown}; invalid={len(invalid)}; "
            f"sample={invalid[:20]}"
        )
    return role_counts(mesh)

def expected_component_count(mesh):
    """Return a semantic expectation when it can be known safely.

    A wall containing both outer and inner shells but no cap/end faces is
    intentionally two disconnected surface components. Most other generated
    modes are expected to be one component per generated object.
    """
    counts = role_counts(mesh)
    outer = counts.get("WALL_OUTER", 0)
    inner = counts.get("WALL_INNER", 0)
    connectors = (
        counts.get("TOP_CAP", 0)
        + counts.get("BOTTOM_CAP", 0)
        + counts.get("END_CAP", 0)
    )
    if outer and inner and not connectors:
        return 2, "OUTER_INNER_OPEN_SHELLS"
    if any(counts.get(name, 0) for name in ("SURFACE", "CORNICE", "TOP_CAP", "WALL_OUTER")):
        return 1, "SEMANTIC_SINGLE_RESULT"
    return None, "UNKNOWN"
