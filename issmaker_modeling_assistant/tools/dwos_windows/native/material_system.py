from __future__ import annotations

from pathlib import Path
import bpy

ASSET_DIR = Path(__file__).resolve().parent / 'textures'
INTERIOR_DIFFUSE = 'DWOS_Interior_Diffuse_White_256.png'
DEFAULT_ERM = 'DWOS_Default_ERM_256.png'
FLAT_NORMAL = 'DWOS_Flat_Normal_256.png'
ERM_PACKING = 'R=Emissive;G=Roughness;B=Metallic'

def _principled(mat):
    mat.use_nodes = True
    nt = mat.node_tree
    if nt is None:
        return None
    bsdf = nt.nodes.get('Principled BSDF')
    if bsdf is None:
        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    return bsdf


def _set_input(bsdf, names, value):
    if bsdf is None:
        return
    for name in names:
        inp = bsdf.inputs.get(name)
        if inp is not None:
            inp.default_value = value
            return


def _asset_image(filename: str, non_color: bool):
    path = ASSET_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f'DWOS system texture missing: {path}')
    name = f'DWOS_SYS_{filename}'
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.load(str(path), check_existing=False)
        img.name = name
    try:
        img.colorspace_settings.name = 'Non-Color' if non_color else 'sRGB'
    except Exception:
        pass
    img['DWOS_system_texture'] = True
    return img


def ensure_frame_material(settings=None):
    """Use an existing material exactly as supplied, without modifying nodes."""
    assigned = getattr(settings, 'frame_material', None) if settings else None
    if assigned is not None:
        return assigned
    name = 'DWOS_Frame_Default'
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        bsdf = _principled(mat)
        _set_input(bsdf, ('Base Color',), (0.19, 0.20, 0.22, 1.0))
        _set_input(bsdf, ('Metallic',), 0.0)
        _set_input(bsdf, ('Roughness',), 0.65)
        mat.diffuse_color = (0.19, 0.20, 0.22, 1.0)
        mat['DWOS_material_role'] = 'FRAME'
        mat['DWOS_material_source'] = 'DEFAULT'
    return mat


def assign_frame_material(frame_obj):
    """Change material assignment without rebuilding geometry or touching nodes."""
    settings = getattr(frame_obj, 'dwos_window', None)
    _assign_single(frame_obj, ensure_frame_material(settings))


def ensure_glass_material():
    name = 'DWOS_Glass_Clear'
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    bsdf = _principled(mat)
    # Restrained viewport glass: readable but not a mirror-like blue slab.
    _set_input(bsdf, ('Base Color',), (0.62, 0.74, 0.78, 1.0))
    _set_input(bsdf, ('Roughness',), 0.18)
    _set_input(bsdf, ('IOR',), 1.45)
    _set_input(bsdf, ('Transmission Weight', 'Transmission'), 0.82)
    _set_input(bsdf, ('Alpha',), 0.24)
    mat.diffuse_color = (0.62, 0.74, 0.78, 0.24)
    try:
        mat.surface_render_method = 'DITHERED'
    except Exception:
        pass
    mat['DWOS_material_role'] = 'GLASS'
    mat['DWOS_glass_preset'] = 'CLEAR'
    return mat


def ensure_interior_material():
    name = 'DWOS_Interior_White_System'
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    if mat.get('DWOS_material_schema') != 'window-core-1.4':
        nodes.clear()
        out = nodes.new('ShaderNodeOutputMaterial'); out.location = (620, 0)
        bsdf = nodes.new('ShaderNodeBsdfPrincipled'); bsdf.location = (340, 0)
        links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
        diffuse = _asset_image(INTERIOR_DIFFUSE, False)
        erm = _asset_image(DEFAULT_ERM, True)
        normal = _asset_image(FLAT_NORMAL, True)
        d = nodes.new('ShaderNodeTexImage'); d.name = 'DWOS_Diffuse'; d.image = diffuse; d.location = (-620, 150)
        e = nodes.new('ShaderNodeTexImage'); e.name = 'DWOS_ERM'; e.image = erm; e.location = (-620, -70)
        n = nodes.new('ShaderNodeTexImage'); n.name = 'DWOS_Normal'; n.image = normal; n.location = (-620, -300)
        sep = nodes.new('ShaderNodeSeparateColor'); sep.location = (-350, -60)
        nm = nodes.new('ShaderNodeNormalMap'); nm.location = (-330, -285)
        links.new(d.outputs['Color'], bsdf.inputs['Base Color'])
        links.new(e.outputs['Color'], sep.inputs['Color'])
        if sep.outputs.get('Green') and bsdf.inputs.get('Roughness'):
            links.new(sep.outputs['Green'], bsdf.inputs['Roughness'])
        if sep.outputs.get('Blue') and bsdf.inputs.get('Metallic'):
            links.new(sep.outputs['Blue'], bsdf.inputs['Metallic'])
        if sep.outputs.get('Red') and bsdf.inputs.get('Emission Strength'):
            links.new(sep.outputs['Red'], bsdf.inputs['Emission Strength'])
        links.new(n.outputs['Color'], nm.inputs['Color'])
        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])
        mat['DWOS_material_schema'] = 'window-core-1.4'
        mat['DWOS_erm_packing'] = ERM_PACKING
        mat['DWOS_system_spec'] = 'Diffuse=White256;ERM=(0,1,0);Normal=Flat'
    mat.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    mat['DWOS_material_role'] = 'INTERIOR'
    return mat


def _assign_single(obj, material):
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return
    mats = obj.data.materials
    mats.clear()
    mats.append(material)
    for poly in obj.data.polygons:
        poly.material_index = 0


def apply_window_materials(frame_obj, glass_obj=None, interior_obj=None):
    settings = getattr(frame_obj, 'dwos_window', None)
    _assign_single(frame_obj, ensure_frame_material(settings))
    if glass_obj is not None:
        _assign_single(glass_obj, ensure_glass_material())
    if interior_obj is not None:
        _assign_single(interior_obj, ensure_interior_material())


def material_summary(obj):
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return []
    counts = [0 for _ in obj.data.materials]
    invalid = 0
    for p in obj.data.polygons:
        if 0 <= p.material_index < len(counts):
            counts[p.material_index] += 1
        else:
            invalid += 1
    out = []
    for i, slot in enumerate(obj.material_slots):
        mat = slot.material
        out.append({
            'slot': i,
            'name': mat.name if mat else '<empty>',
            'role': mat.get('DWOS_material_role', '') if mat else '',
            'source': mat.get('DWOS_material_source', '') if mat else '',
            'faces': counts[i] if i < len(counts) else 0,
        })
    if invalid:
        out.append({'slot': -1, 'name': '<invalid-index>', 'role': '', 'source': '', 'faces': invalid})
    return out
