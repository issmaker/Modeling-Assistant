import bpy
from mathutils import Matrix, Vector
from .material_intelligence import inspect_material_asset

SAFE_MARGIN = 0.01171875


class VirtualSourceProxy:
    """Object-shaped adapter around an unlinked Mesh datablock.

    No Object is linked to a Collection or Scene, so the user's Outliner stays
    clean. Existing production generators can keep reading source_obj.data.
    """
    type = "MESH"

    def __init__(self, name, mesh, size):
        self.name = name
        self.data = mesh
        self.matrix_world = Matrix.Identity(4)
        self.dimensions = Vector((size, 0.0, size))

    def __hash__(self):
        return id(self)



def _module_material(definition, size):
    """The selected module holds only a Material; Image fields are obsolete."""
    prop = "virtual_material_2k" if abs(float(size)-2.0) < 1.e-6 else "virtual_material_4k"
    return getattr(definition, prop, None)

def resolve_virtual_asset(definition, settings, size):
    """A virtual source is determined by the Material's own color texture.

    Deprecated explicit Image pointers are ignored even in old .blend files.
    Never mutate the user's Material or insert replacement Image Texture nodes.
    """
    material = _module_material(definition, size)
    if material is None:
        material = (getattr(definition, "virtual_material", None)
                    or getattr(settings, "reference_material", None))
    module = "2" if abs(float(size) - 2.0) < 1.0e-6 else "4"
    state = inspect_material_asset(material, None, module)
    if state["status"] != "VALID":
        raise ValueError(state["message"])
    return material, state["image"]


def _virtual_mesh_name(material, size):
    safe = (material.name if material is not None else "Material").replace("/", "_").replace("\\", "_")
    return f"__UVWall_VirtualReference_{int(round(size))}m_{safe}"


def _cached_virtual_mesh(material, size):
    """Return a persistent unlinked reference Mesh.

    The previous implementation created and removed a material-bearing Mesh on
    every production commit.  That is unnecessary depsgraph/material datablock
    churn.  These meshes have no Object and never appear in the scene/Outliner.
    """
    name = _virtual_mesh_name(material, size)
    mesh = bpy.data.meshes.get(name)
    if mesh is not None:
        valid = (
            bool(mesh.get("uvwall_virtual_reference"))
            and abs(float(mesh.get("uvwall_module_size", 0.0)) - float(size)) < 1.0e-9
            and len(mesh.vertices) == 4
            and len(mesh.polygons) == 1
            and len(mesh.materials) == 1
            and mesh.materials[0] == material
            and mesh.uv_layers.active is not None
        )
        if valid:
            return mesh
        if mesh.users == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                mesh = None

    mesh = bpy.data.meshes.new(name)
    half = size * 0.5
    mesh.from_pydata(
        [(-half, 0.0, -half), (half, 0.0, -half), (half, 0.0, half), (-half, 0.0, half)],
        [], [(0, 1, 2, 3)],
    )
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    values = (
        (SAFE_MARGIN, SAFE_MARGIN),
        (1.0 - SAFE_MARGIN, SAFE_MARGIN),
        (1.0 - SAFE_MARGIN, 1.0 - SAFE_MARGIN),
        (SAFE_MARGIN, 1.0 - SAFE_MARGIN),
    )
    for loop_index, value in zip(mesh.polygons[0].loop_indices, values):
        uv.data[loop_index].uv = value
    mesh.materials.append(material)
    mesh["uvwall_virtual_reference"] = True
    mesh["uvwall_module_size"] = float(size)
    return mesh


def resolve_virtual_source(definition, settings):
    """Return a cached unlinked source without creating a scene Object."""
    size = float(getattr(definition, "module_standard", getattr(settings, "module_standard", "4")))
    material, _image = resolve_virtual_asset(definition, settings, size)

    mesh = _cached_virtual_mesh(material, size)
    return VirtualSourceProxy(f"Virtual_{int(size)}x{int(size)}m", mesh, size)



def purge_virtual_cache():
    """Remove unlinked UV Wall virtual meshes and private derived materials."""
    for mesh in list(bpy.data.meshes):
        try:
            owned = bool(mesh.get("uvwall_virtual_reference"))
        except Exception:
            owned = False
        if owned and mesh.users == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                pass
    for material in list(bpy.data.materials):
        try:
            owned = bool(material.get("uvwall_virtual_material"))
        except Exception:
            owned = False
        if owned and material.users == 0:
            try:
                bpy.data.materials.remove(material)
            except Exception:
                pass

def release_virtual_source(source):
    """Release a proxy handle, not its cached datablock.

    Cached virtual reference meshes intentionally survive between builds to
    avoid repeated Mesh/Material creation-removal cycles in Blender's depsgraph.
    They remain unlinked and invisible in the scene.
    """
    return None
