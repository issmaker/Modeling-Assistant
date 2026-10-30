"""Read-only Geometry Truth fingerprints for authored Paths and WALL results.

Independent of Blender imports. Hashes are signatures of *exact* coordinates,
loop topology, UV and face semantics; different vertex orders produce different
hashes deliberately. Validation occurs before stable Result publication.
"""
from __future__ import annotations

from hashlib import blake2b
from math import isfinite
from struct import pack


class GeometryTruthError(ValueError):
    """Invalid geometry, nonfinite source, or incomplete UV contract."""


def _float(value):
    number = float(value)
    if not isfinite(number):
        raise GeometryTruthError('GEOMETRY_TRUTH: nonfinite coordinate/UV')
    return number


def _vec3(digest, v):
    digest.update(pack('<3d', _float(v[0]), _float(v[1]), _float(v[2])))


def _int(value):
    n = int(value)
    if n < 0 or n > 2**63 - 1:
        raise GeometryTruthError('GEOMETRY_TRUTH: invalid vertex/loop index')
    return n


def path_snapshot(path_obj):
    """Snapshot original authored local mesh and world transform (not offsets)."""
    data = path_obj.data
    h = blake2b(digest_size=32, person=b'iss-path-truth')
    for vertex in data.vertices:
        _vec3(h, vertex.co)
    for edge in data.edges:
        indices = edge.vertices
        h.update(pack('<2q', _int(indices[0]), _int(indices[1])))
    for row in path_obj.matrix_world:
        h.update(pack('<4d', *(_float(value) for value in row)))
    return {'path_digest': h.hexdigest(), 'authored_vertices':len(data.vertices),
            'authored_edges':len(data.edges)}


def assert_path_unchanged(path_obj, expected):
    actual = path_snapshot(path_obj)
    if actual != expected:
        raise GeometryTruthError(
            'GEOMETRY_TRUTH: authored Path changed during WALL build; '
            'stable result was not published')
    return actual


def mesh_snapshot(mesh, face_roles):
    """Whole-mesh contract; pre-publication only, never per viewport frame.

    Signatures include every physical point, edge, face loop order, material,
    polygon normal, UV for each loop and semantic role.  No payload is copied
    to the Result; the bounded digest is suitable for regression comparisons.
    """
    verts = mesh.vertices
    polygons = mesh.polygons
    uv = mesh.uv_layers.active
    if uv is None:
        raise GeometryTruthError('GEOMETRY_TRUTH: missing active UV layer')
    if len(face_roles) != len(polygons):
        raise GeometryTruthError('GEOMETRY_TRUTH: face-role coverage missing')
    coords=blake2b(digest_size=32,person=b'iss-xyz-truth')
    topology=blake2b(digest_size=32,person=b'iss-topo-truth')
    uv_hash=blake2b(digest_size=32,person=b'iss-uv-truth')
    material=blake2b(digest_size=32,person=b'iss-mat-truth')
    for vert in verts:
        _vec3(coords, vert.co)
    for edge in mesh.edges:
        a,b=edge.vertices
        topology.update(pack('<2q',_int(a),_int(b)))
    seen_loops=0
    # Slot identities matter: changing the material datablock at an unchanged
    # face material_index must not produce a false "identical material" report.
    slots = tuple(getattr(mesh, 'materials', ()) or ())
    material.update(pack('<q', len(slots)))
    for slot in slots:
        name = str(getattr(slot, 'name_full', getattr(slot, 'name', '')) if slot is not None else '<NONE>')
        encoded = name.encode('utf-8', errors='replace')
        material.update(pack('<q', len(encoded)))
        material.update(encoded)
    for fi,poly in enumerate(polygons):
        indices=list(poly.loop_indices)
        if len(indices)<3:
            raise GeometryTruthError(f'GEOMETRY_TRUTH: polygon {fi} has <3 loops')
        topology.update(pack('<q',len(indices)))
        role = _int(face_roles[fi])
        material.update(pack('<2q',_int(poly.material_index),role))
        _vec3(material,poly.normal)
        for loop_index in indices:
            idx=_int(loop_index)
            if idx>=len(mesh.loops) or idx>=len(uv.data):
                raise GeometryTruthError('GEOMETRY_TRUTH: bad UV/geometry loop index')
            v_index=_int(mesh.loops[idx].vertex_index)
            if v_index>=len(verts):
                raise GeometryTruthError('GEOMETRY_TRUTH: face refers to missing vertex')
            topology.update(pack('<q',v_index))
            u,v=uv.data[idx].uv
            uv_hash.update(pack('<2d',_float(u),_float(v)))
            seen_loops+=1
    if seen_loops != len(mesh.loops):
        raise GeometryTruthError('GEOMETRY_TRUTH: orphaned or repeated face loops')
    return {'schema':'ISSMAKER_WALL_TRUTH_V1',
            'vertices':len(verts),'edges':len(mesh.edges),
            'faces':len(polygons),'loops':len(mesh.loops),
            'xyz_digest':coords.hexdigest(),'topology_digest':topology.hexdigest(),
            'uv_digest':uv_hash.hexdigest(),'material_role_normal_digest':material.hexdigest()}
