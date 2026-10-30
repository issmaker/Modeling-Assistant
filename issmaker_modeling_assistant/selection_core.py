"""ISSMAKER Selection Core 1.3.7.

Pure selection-domain decisions and compact, pointer-safe edit-session fingerprints.
No module-level Blender object/BMesh element references survive events.
"""

import hashlib
import struct


def selected_uv_faces(bm, uv_layer, select_mode):
    """Return wholly UV-selected, visible faces and partial-face count.

    Mesh selection is intentionally NOT tested here: unsynced UV selection is
    authoritative even when a different polygon set is selected in 3D.
    """
    faces = []
    partial = 0
    edge_mode = select_mode == 'EDGE'
    for face in bm.faces:
        if face.hide:
            continue
        flags = [bool(getattr(loop[uv_layer], 'select_edge', False))
                 if edge_mode else bool(loop[uv_layer].select)
                 for loop in face.loops]
        if flags and all(flags):
            faces.append(face)
        elif any(flags):
            partial += 1
    return faces, partial


def selection_fingerprint(bm, uv_layer, mesh_select_mode, uv_select_mode, sync):
    """Digest ALL mesh and UV selection flags, mode, active face.

    This is deliberately not a face-only fingerprint: vertex/edge/UV edits
    performed by the user during issue preview must never be overwritten when
    leaving the tool. Excludes mutable selection history (advisory to Blender).
    """
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
            digest.update(bytes(
                (int(bool(loop[uv_layer].select)) |
                 (int(bool(getattr(loop[uv_layer], 'select_edge', False))) << 1))
                for loop in face.loops))
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
