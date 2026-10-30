"""DWOS attribute policy for auxiliary Glass/Interior Weld baking.

Mesh selection/visibility state is maintained by Blender and is not a user
geometry attribute requiring loop/point transfer.  Do not silently discard
actual user-created colour, weight, float, vector, integer or boolean layers.
This module deliberately has no bpy dependency so it can be unit-tested.
"""
from __future__ import annotations


# Native attributes created/managed by Blender's Mesh itself. Geometry,
# material IDs, sharp flags are carried by the BMesh/mesh conversion pipeline.
NATIVE_MESH_ATTRIBUTES = frozenset({
    'position', 'material_index', 'sharp_face', 'sharp_edge',
})

# Blender-managed viewport/edit-mode state, not material geometry. In Blender
# 4.5 these can appear among Mesh.attributes after interacting with a mesh.
# They must NEVER prevent a production Weld on generated glass/interior.
NATIVE_EDITOR_ATTRIBUTES = frozenset({
    '.select_vert', '.select_edge', '.select_poly',
    '.hide_vert', '.hide_edge', '.hide_poly',
    '.sculpt_mask', '.sculpt_face_set', '.sculpt_face_sets',
})


def supported_without_custom_transfer(attr, uv_names=()):
    """True only for attributes whose semantics are handled without custom copy.

    Do NOT accept arbitrary leading-dot names: a project may have a private
    attribute that carries real data and must remain protected by preflight.
    A declared internal Blender attribute is safe to leave to Blender's own
    BMesh conversion. Unknown named user attributes still block the bake.
    """
    name = str(attr.name)
    return (
        name in NATIVE_MESH_ATTRIBUTES
        or name in NATIVE_EDITOR_ATTRIBUTES
        or name in uv_names
        or name.startswith('DWOS_')
        or bool(getattr(attr, 'is_internal', False))
    )
