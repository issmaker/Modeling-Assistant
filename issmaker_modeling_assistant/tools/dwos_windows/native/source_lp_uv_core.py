"""W58.3 Source LP UV: preserve author-selected UV, including intentional seams.

Captures only the selected BMesh face corners, before deleting the host faces.
The snapshot is transformed once into the window's local space, making later
LP rebuilds deterministic and independent of the edited host mesh.
"""
from __future__ import annotations

import hashlib
import json
import math
from mathutils import Vector


SCHEMA = "W58.3-LP-SOURCE-UV-v1"


def _placeholder_face_uv(face):
    vertices=[loop.vert.co for loop in face.loops]
    if len(vertices)<3:raise ValueError("SOURCE_FACE_TOO_SMALL")
    origin=vertices[0]
    along=max((v-origin for v in vertices[1:]),key=lambda q:q.length)
    if along.length<1e-8:raise ValueError("SOURCE_FACE_DEGENERATE")
    u=along.normalized()
    normal=face.normal
    if normal.length<1e-8:raise ValueError("SOURCE_FACE_ZERO_NORMAL")
    v=normal.cross(u).normalized()
    # Use physical coordinates as scratch UV: NOT a metric contract.
    return [[float((p-origin).dot(u)),float((p-origin).dot(v))] for p in vertices]


def capture_face_uv(group, uv_layer, placeholder=False):
    # MATERIAL PLACEHOLDER: source UV is not a precondition. Compute
    # nondegenerate temporary 2D coordinates IN MEMORY from source XYZ;
    # do not modify the author's wall UV or an existing UVMap.
    if uv_layer is None and not placeholder:
        raise ValueError("SOURCE_LP_UV_MISSING: selected wall faces need UVMap")
    result = []
    for face in sorted(group, key=lambda face: face.index):
        data = {
            "id": int(face.index),
            "position": [list(map(float, loop.vert.co)) for loop in face.loops],
            "uv": ([list(map(float, loop[uv_layer].uv)) for loop in face.loops]
                   if not placeholder else _placeholder_face_uv(face)),
            "material": int(face.material_index),
        }
        if len(data["position"]) < 3:
            raise ValueError("SOURCE_LP_FACE_HAS_FEWER_THAN_3_VERTICES")
        if not all(math.isfinite(c) for point in data["position"] for c in point):
            raise ValueError("SOURCE_LP_FACE_HAS_INVALID_XYZ")
        if not all(math.isfinite(c) for uv in data["uv"] for c in uv):
            raise ValueError("SOURCE_LP_FACE_HAS_INVALID_UV")
        result.append(data)
    return result


def prepare_local_payload(captured, frame_matrix, host_matrix):
    to_window = frame_matrix.inverted() @ host_matrix
    faces = []
    for face in captured:
        faces.append({
            "id": int(face["id"]),
            "position": [list(map(float, to_window @ Vector(p)))
                         for p in face["position"]],
            "uv": [[float(t) for t in pair] for pair in face["uv"]],
            "material": int(face["material"]),
        })
    return {"schema": SCHEMA, "faces": faces}


def checksum(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf8")
    ).hexdigest()


def _barycentric(point, a, b, c):
    e0 = b-a
    e1 = c-a
    ep = point-a
    d00 = e0.dot(e0)
    d01 = e0.dot(e1)
    d11 = e1.dot(e1)
    d20 = ep.dot(e0)
    d21 = ep.dot(e1)
    den = d00*d11-d01*d01
    if abs(den) < 1e-14:
        return None
    v = (d11*d20-d01*d21)/den
    w = (d00*d21-d01*d20)/den
    u = 1-v-w
    nearest = a*u+b*v+c*w
    return (u, v, w), float((nearest-point).length)


def project_lp_uv(payload, vertices, faces, *, distance_tolerance=0.0005,
                  barycentric_tolerance=0.0005):
    if payload.get("schema") != SCHEMA:
        raise ValueError("SOURCE_LP_UV_UNSUPPORTED_SCHEMA")
    source = {}
    for face in payload["faces"]:
        pts = [Vector(p) for p in face["position"]]
        uv = [Vector(pair) for pair in face["uv"]]
        if len(pts) != len(uv) or len(pts) < 3:
            raise ValueError("SOURCE_LP_UV_BAD_FACE_INPUT")
        triangles = []
        for index in range(1, len(pts)-1):
            triangles.append((pts[0], pts[index], pts[index+1],
                              uv[0], uv[index], uv[index+1]))
        source[int(face["id"])] = triangles
    if not source:
        raise ValueError("SOURCE_LP_UV_EMPTY_INPUT")

    def closest(point, candidates):
        options = []
        for tri in candidates:
            q = _barycentric(point, *tri[:3])
            if q is None:
                continue
            weights, distance = q
            outside = max(0., -min(weights))
            options.append((distance + outside*10., distance, outside,
                            weights, tri))
        if not options:
            raise ValueError("SOURCE_LP_UV_NO_VALID_TRIANGLE")
        return min(options, key=lambda x: x[0])

    output = []
    max_distance = 0.
    max_outside = 0.
    for face_index, polygon in enumerate(faces):
        points = [Vector(vertices[i]) for i in polygon]
        midpoint = sum(points, Vector((0., 0., 0.)))/len(points)
        candidate_face = min(
            source,
            key=lambda fid: closest(midpoint, source[fid])[0]
        )
        tri_list = source[candidate_face]
        mapped = []
        for point in points:
            _score, distance, outside, weights, tri = closest(point, tri_list)
            max_distance = max(max_distance, distance)
            max_outside = max(max_outside, outside)
            if distance > distance_tolerance or outside > barycentric_tolerance:
                raise ValueError(
                    "SOURCE_LP_UV_OUTSIDE_SELECTED_POLYGON: "
                    f"LP face={face_index}, source={candidate_face}, "
                    f"distance={distance:.7g}m, outside={outside:.7g}"
                )
            u, v, w = weights
            val = tri[3]*u + tri[4]*v + tri[5]*w
            mapped.append((float(val.x), float(val.y)))
        area = .5*abs(sum(
            mapped[i][0]*mapped[(i+1)%len(mapped)][1]
            - mapped[(i+1)%len(mapped)][0]*mapped[i][1]
            for i in range(len(mapped))
        ))
        if area < 1e-12:
            raise ValueError(f"SOURCE_LP_UV_DEGENERATE_FACE: {face_index} source={candidate_face} uv={mapped} xyz={[tuple(v) for v in points]}")
        output.append(mapped)
    return output, {"max_position_error_m": max_distance,
                    "max_barycentric_overrun": max_outside,
                    "source_face_count": len(source),
                    "lp_face_count": len(output)}


def transfer_hp_front_uv(frame_obj):
    """Transfer the captured LP/source UV onto the HP contact/front facade.

    Depth extrusions keep their already-metric generated UV and remain
    independent UV islands. This deliberately avoids projecting 3D sides
    onto the source plane, which would degenerate their UV.
    """
    payload_text=str(frame_obj.get("DWOS_W58_source_lp_uv_json",""))
    if not payload_text:
        return {"mapped_front_faces": 0, "depth_faces_kept": 0}
    payload=json.loads(payload_text)
    mesh=frame_obj.data
    layer=mesh.uv_layers.active
    if layer is None:
        raise ValueError("HP_SOURCE_UV_MISSING_GENERATED_UVMAP")
    coords=[tuple(v.co) for v in mesh.vertices]
    mapped=0
    depth=0
    errors=[]
    for polygon in mesh.polygons:
        face=[mesh.loops[i].vertex_index for i in polygon.loop_indices]
        try:
            projected,stats=project_lp_uv(payload,coords,[face])
        except ValueError as exc:
            if str(exc).startswith("SOURCE_LP_UV_OUTSIDE_SELECTED_POLYGON"):
                # This polygon has profile depth (not part of the LP facade).
                depth+=1
                continue
            errors.append(f"{polygon.index}:{exc}")
            continue
        for li, uv in zip(polygon.loop_indices,projected[0]):
            layer.data[li].uv=uv
        mapped+=1
    if errors:
        raise ValueError("HP_SOURCE_UV_TRANSFER_FAILED: "+str(errors[:3]))
    if mapped==0:
        raise ValueError("HP_SOURCE_UV_NO_FRONT_FACES")
    mesh["DWOS_W58_hp_source_uv_front_faces"]=mapped
    mesh["DWOS_W58_hp_source_uv_depth_faces"]=depth
    mesh["DWOS_W58_hp_source_uv_schema"]=SCHEMA
    return {"mapped_front_faces":mapped,"depth_faces_kept":depth}
