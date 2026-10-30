"""DWOS 2.1.4: compose auxiliary glazing and Interior into ONE ordinary Host Mesh.

All operations modify only an unassigned *staged* Mesh. The original scene and
its Blender datablocks are untouched until a successful weld commit.
"""
from __future__ import annotations

import bpy
import bmesh

from .weld_attribute_policy import supported_without_custom_transfer


def _validate_auxiliary(obj):
    if obj.type != 'MESH' or obj.data is None or not obj.data.polygons:
        raise RuntimeError(f'{obj.name}: стекло/Interior должны быть непустыми Mesh')
    if obj.library or obj.data.library or obj.data.shape_keys:
        raise RuntimeError(f'{obj.name}: linked data / Shape Keys нельзя автоматически запечь')
    if obj.modifiers or obj.constraints:
        raise RuntimeError(f'{obj.name}: примените модификаторы/constraints перед Weld')
    if obj.vertex_groups:
        raise RuntimeError(f'{obj.name}: vertex groups нельзя переносить без проверки')
    uv_names = {layer.name for layer in obj.data.uv_layers}
    for attr in obj.data.attributes:
        # Blender may expose .select_vert / .hide_vert and other internal
        # edit-mode state here. These are NOT user data and are intentionally
        # not copied to the consolidated Host. Real custom attributes still
        # require an explicit transfer implementation before destructive Weld.
        if supported_without_custom_transfer(attr, uv_names):
            continue
        raise RuntimeError(f'{obj.name}: пользовательский атрибут {attr.name} '
                           'требует отдельного переноса (Weld не изменил Host)')


def collect_auxiliary(frame):
    """Reject unknown children: no third-party object may disappear during cleanup."""
    if frame.library or frame.data.library or frame.modifiers or frame.constraints:
        raise RuntimeError(f'{frame.name}: linked/controller modifiers не поддерживаются Weld Bake')
    found=[]
    for child in frame.children:
        if child.get('DWOS_role') not in {'GLASS','INTERIOR'} or child.children:
            raise RuntimeError(f'{frame.name}: неожиданный дочерний объект {child.name}, Weld остановлен')
        _validate_auxiliary(child)
        found.append(child)
    if not any(o.get('DWOS_role') == 'GLASS' for o in found):
        raise RuntimeError(f'{frame.name}: нет стекла; один Mesh не может быть собран полностью')
    return found


def _material_index(dest, material):
    if material is None:
        if not dest.materials:
            raise RuntimeError('Пустой материал стекла/Interior: сначала назначьте Material Slot')
        return 0
    for i, mat in enumerate(dest.materials):
        if mat == material:
            return i
    dest.materials.append(material)
    return len(dest.materials)-1


def bake_auxiliary_mesh(host, staged_mesh, parts):
    """Append glass/Interior triangles/quads, UV loops and Material Slots to host.

    Glass is intentionally a separate *surface shell inside the same Object*;
    welding the glass physically to the frame would destroy the glazing model.
    """
    for item in parts:
        _validate_auxiliary(item)
    bm = bmesh.new()
    try:
        bm.from_mesh(staged_mesh)
        for item in parts:
            src = item.data
            transform = host.matrix_world.inverted() @ item.matrix_world
            if abs(transform.determinant()) < 1e-10:
                raise RuntimeError(f'{item.name}: вырожденная матрица трансформации')
            negative = transform.determinant() < 0
            mapping = {vert.index: bm.verts.new(transform @ vert.co) for vert in src.vertices}
            face_materials = [_material_index(staged_mesh, mat) for mat in src.materials]
            uv_map = {layer.name: bm.loops.layers.uv.get(layer.name) or bm.loops.layers.uv.new(layer.name)
                      for layer in src.uv_layers}
            for poly in src.polygons:
                inds=list(poly.vertices)
                loops=list(poly.loop_indices)
                if negative:
                    inds.reverse();loops.reverse()
                try:
                    face=bm.faces.new([mapping[i] for i in inds])
                except ValueError as exc:
                    raise RuntimeError(f'{item.name}: дублирующаяся или неверная грань') from exc
                face.material_index=face_materials[poly.material_index] if face_materials else 0
                face.smooth=poly.use_smooth
                # Mesh polygon loops and BMesh face loops share winding order.
                for bmloop, loop_idx in zip(face.loops, loops):
                    for layer in src.uv_layers:
                        bmloop[uv_map[layer.name]].uv=layer.data[loop_idx].uv.copy()
        bm.to_mesh(staged_mesh)
        staged_mesh.update()
        for item in parts:
            for layer in item.data.uv_layers:
                if staged_mesh.uv_layers.get(layer.name) is None:
                    raise RuntimeError(f'{item.name}: не перенесён UV {layer.name}')
    finally:
        bm.free()
    return staged_mesh


def remove_dwos_mesh_metadata(mesh):
    """Strip only owned metadata, never UV, colors, materials or user attributes."""
    for attr in list(mesh.attributes):
        if attr.name.startswith('DWOS_'):
            mesh.attributes.remove(attr)
    for key in list(mesh.keys()):
        if isinstance(key,str) and key.startswith('DWOS_'):
            del mesh[key]


def preview_rules(scene, host, frames):
    """Pure decision: preserve DWOS attributes while any editable window remains."""
    included={o.as_pointer() for o in frames}
    return not any(o.dwos_window.is_window and not o.dwos_window.finalized
                   and o.dwos_window.host_name==host.name and o.as_pointer() not in included
                   and not o.get('DWOS_archived_source') for o in scene.objects)
