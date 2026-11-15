"""ISSMAKER Selection Core 1.3.7.

Pure selection-domain decisions and compact, pointer-safe edit-session fingerprints.
No module-level Blender object/BMesh element references survive events.
"""

import hashlib
import struct


def uv_selection_supported(loop, uv_layer):
    """Blender 5.0+ uses BMLoop/BMFace UV attributes; 4.x uses BMLoopUV."""
    return (hasattr(loop, 'uv_select_vert') or
            hasattr(loop[uv_layer], 'select'))


def uv_flags(loop, uv_layer):
    """Read actual UV selection, never fall back to broader mesh selection."""
    if hasattr(loop, 'uv_select_vert'):
        return bool(loop.uv_select_vert), bool(loop.uv_select_edge)
    uv = loop[uv_layer]
    if not hasattr(uv, 'select'):
        raise RuntimeError('UV selection attributes unavailable in this Blender build')
    return bool(uv.select), bool(getattr(uv, 'select_edge', False))


def set_uv_flags(loop, uv_layer, selected, edge_selected):
    """Set UV flags across 4.x/5.x without changing underlying 3D selection."""
    if hasattr(loop, 'uv_select_vert_set'):
        loop.uv_select_vert_set(bool(selected))
        loop.uv_select_edge_set(bool(edge_selected))
        return
    uv = loop[uv_layer]
    uv.select = bool(selected)
    if hasattr(uv, 'select_edge'):
        uv.select_edge = bool(edge_selected)


def uv_face_flag(face, uv_layer):
    if hasattr(face, 'uv_select'):
        return bool(face.uv_select)
    return all(uv_flags(loop, uv_layer)[0] for loop in face.loops)


def set_uv_face_flag(face, uv_layer, value):
    if hasattr(face, 'uv_select_set'):
        face.uv_select_set(bool(value))
    else:
        for loop in face.loops:
            set_uv_flags(loop, uv_layer, bool(value), bool(value))


def selected_uv_faces(bm, uv_layer, select_mode):
    """Use actual UV editor face/corner selection, not selected Mesh faces.

    Blender 5.0 removed BMLoopUV.select and moved UV selection to BMLoop
    and BMFace. A fallback to loop.vert.select could modify every island when
    the full mesh is selected in 3D; it is deliberately forbidden here.
    """
    faces = []
    partial = 0
    mode = str(select_mode).upper()
    for face in bm.faces:
        if face.hide:
            continue
        if mode == 'FACE':
            selected = uv_face_flag(face, uv_layer)
            if selected:
                faces.append(face)
            continue
        channel = 1 if mode == 'EDGE' else 0
        flags = [uv_flags(loop, uv_layer)[channel] for loop in face.loops]
        if flags and all(flags):
            faces.append(face)
        elif any(flags):
            partial += 1
    return faces, partial


def selection_fingerprint(bm, uv_layer, mesh_select_mode, uv_select_mode, sync):
    """Digest actual mesh/UV selection and modes, independent of UV map."""
    digest = hashlib.blake2b(digest_size=32)
    digest.update(bytes((int(bool(x)) for x in mesh_select_mode)))
    digest.update(str(uv_select_mode).encode('ascii', errors='replace'))
    digest.update(bytes((int(bool(sync)),)))
    digest.update(struct.pack('<4Q', len(bm.verts), len(bm.edges),
                              len(bm.faces),
                              bm.faces.active.index + 1 if bm.faces.active else 0))
    for domain in (bm.verts, bm.edges, bm.faces):
        digest.update(bytes(int(element.select) for element in domain))
    if uv_layer is not None:
        for face in bm.faces:
            digest.update(bytes([int(uv_face_flag(face, uv_layer))]))
            for loop in face.loops:
                v,e=uv_flags(loop, uv_layer)
                digest.update(bytes((int(v) | (int(e)<<1),)))
    return digest.digest()

def uv_and_mesh_fingerprint(bm, uv_layer, settings):
    return selection_fingerprint(
        bm, uv_layer, settings.mesh_select_mode,
        getattr(settings, 'uv_select_mode', 'VERTEX'),
        getattr(settings, 'use_uv_select_sync', False),
    )


def content_fingerprint(bm, uv_layer):
    """Hash source coordinates/topology and active UV values once per scan.

    Called at scan start/end rather than on every TIMER; no BMesh element
    references are stored, so edits and redo cannot invalidate Python handles.
    """
    digest = hashlib.blake2b(digest_size=32)
    digest.update(struct.pack('<3Q', len(bm.verts), len(bm.edges), len(bm.faces)))
    for vert in bm.verts:
        digest.update(struct.pack('<3d', float(vert.co.x), float(vert.co.y), float(vert.co.z)))
    for edge in bm.edges:
        a, b = sorted((edge.verts[0].index, edge.verts[1].index))
        digest.update(struct.pack('<2Q', a, b))
    for face in bm.faces:
        digest.update(struct.pack('<Q', len(face.loops)))
        for loop in face.loops:
            digest.update(struct.pack('<Q', loop.vert.index))
            uv = loop[uv_layer].uv
            digest.update(struct.pack('<2d', float(uv.x), float(uv.y)))
    return digest.digest()




def capture_mesh_flags(bm):
    """Selection-only rollback for fast modal operations; bounded byte copies."""
    return (
        bytes(int(v.select) for v in bm.verts),
        bytes(int(e.select) for e in bm.edges),
        bytes(int(f.select) for f in bm.faces),
    )


def restore_mesh_flags(bm, state):
    vertices, edges, faces = state
    if (len(vertices), len(edges), len(faces)) != (
            len(bm.verts), len(bm.edges), len(bm.faces)):
        raise RuntimeError('Невозможно восстановить выделение: топология изменилась')
    for v, selected in zip(bm.verts, vertices):
        v.select = bool(selected)
    for e, selected in zip(bm.edges, edges):
        e.select = bool(selected)
    for f, selected in zip(bm.faces, faces):
        f.select = bool(selected)


def topology_fingerprint(bm):
    """Stable mesh connectivity proof even if counts are unchanged."""
    digest = hashlib.blake2b(digest_size=32)
    digest.update(struct.pack('<3Q', len(bm.verts), len(bm.edges), len(bm.faces)))
    for edge in bm.edges:
        a, b = sorted((edge.verts[0].index, edge.verts[1].index))
        digest.update(struct.pack('<2Q', a, b))
    for face in bm.faces:
        digest.update(struct.pack('<Q', len(face.loops)))
        for loop in face.loops:
            digest.update(struct.pack('<Q', loop.vert.index))
    return digest.digest()

def uv_layer_key(mesh):
    """Stable logical UV identity across Edit Mesh/RNA wrapper refreshes.

    Pointer addresses of individual MeshUVLoopLayer wrappers are intentionally
    excluded. Index, active name and ordered UV-layer names distinguish real
    map switches, renames, additions and removals without treating an RNA
    reallocation as a semantic user change.
    """
    maps = mesh.uv_layers
    active = maps.active
    if active is None:
        return None
    index = getattr(maps, 'active_index', None)
    try:
        names = tuple(item.name for item in maps)
    except TypeError:
        # Lightweight tests or unusual embedding shims may expose only active.
        names = (active.name,)
    return (index, active.name, names)


def uv_layer_matches(mesh, expected):
    """Return False for any meaningful active UV layer / layer-list change."""
    return expected is not None and uv_layer_key(mesh) == expected


def snapshot_edit_session(context, obj, require_uv=False):
    """Immutable identifiers only; no cached BMesh RNA references."""
    mesh = obj.data
    layer = mesh.uv_layers.active
    if require_uv and layer is None:
        raise RuntimeError('Отсутствует активная UV-карта')
    return {
        'object_ptr': obj.as_pointer(),
        'mesh_ptr': mesh.as_pointer(),
        'uv_key': uv_layer_key(mesh),
        'mode': context.mode,
    }


def edit_session_matches(context, snapshot, require_uv=False):
    """Fail closed on object/mesh/layer switches or mode change."""
    try:
        obj = context.active_object
        if context.mode != 'EDIT_MESH' or obj is None or obj.type != 'MESH':
            return False
        if obj.as_pointer() != snapshot['object_ptr']:
            return False
        mesh = obj.data
        if mesh.as_pointer() != snapshot['mesh_ptr']:
            return False
        if require_uv:
            if not uv_layer_matches(mesh, snapshot['uv_key']):
                return False
        return True
    except (ReferenceError, RuntimeError, AttributeError, KeyError):
        return False
