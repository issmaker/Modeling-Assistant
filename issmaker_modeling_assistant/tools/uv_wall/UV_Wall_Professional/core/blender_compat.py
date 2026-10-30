"""Blender 4.x / 5.x runtime feature gate.

Feature checks are intentionally non-destructive and execute before registering
RNA or importing production modules. Successful probes do not certify visuals.
"""
from __future__ import annotations

MIN_BLENDER = (4, 0, 0)
MAX_EXCLUSIVE = (6, 0, 0)


def version_family(version):
    try:
        v = tuple(int(x) for x in version[:3]); v += (0,) * (3 - len(v))
    except (TypeError, ValueError) as exc:
        raise RuntimeError('UV Wall: invalid Blender version') from exc
    if v < MIN_BLENDER or v >= MAX_EXCLUSIVE:
        raise RuntimeError(
            f'UV Wall requires Blender 4.x or 5.x; found {v[0]}.{v[1]}.{v[2]}. '
            'No scene or addon data has been changed.'
        )
    return f'{v[0]}.{v[1]}'



def _mesh_rna_property(mesh_type, property_name):
    """Read-only RNA property detection. Do not mutate bpy.data at startup.

    Blender's bpy.types.Mesh exposes Mesh.attributes and Mesh.uv_layers as
    instance/RNA properties; hasattr(bpy.types.Mesh, name) is unreliable.
    A missing introspection surface is *unknown*, not a missing Mesh API.
    Returns (available: bool|None, diagnostic_source: str).
    """
    if mesh_type is None:
        return False, 'mesh_type_missing'
    try:
        rna = getattr(mesh_type, 'bl_rna', None)
        properties = getattr(rna, 'properties', None) if rna is not None else None
    except (AttributeError, RuntimeError, TypeError):
        properties = None
    if properties is not None:
        getter = getattr(properties, 'get', None)
        if callable(getter):
            try:
                return getter(property_name) is not None, 'bl_rna.properties.get'
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass
        try:
            return property_name in properties, 'bl_rna.properties.contains'
        except (TypeError, RuntimeError, ValueError):
            pass
        try:
            properties[property_name]
            return True, 'bl_rna.properties.index'
        except (KeyError, IndexError, RuntimeError, TypeError, ValueError):
            # RNA exists, but the property was not found. This is strong
            # evidence that the required feature is absent.
            return False, 'bl_rna.properties.missing'
    try:
        if hasattr(mesh_type, property_name):
            return True, 'python_class'
    except (RuntimeError, TypeError):
        pass
    # Do not reject Blender just because its type wrapper does not expose
    # these instance properties and RNA metadata could not be inspected.
    return None, 'rna_metadata_unavailable'


def runtime_features(bpy, bmesh, gpu):
    version = getattr(bpy.app, 'version', ())
    family = version_family(version)
    mesh_type = getattr(getattr(bpy, 'types', None), 'Mesh', None)
    mesh_attrs, attrs_source = _mesh_rna_property(mesh_type, 'attributes')
    mesh_uv, uv_source = _mesh_rna_property(mesh_type, 'uv_layers')
    checks = {
        'Mesh.attributes': mesh_attrs,
        'Mesh.uv_layers': mesh_uv,
        'BMesh.dissolve_edges': hasattr(bmesh.ops, 'dissolve_edges'),
        'BMesh.remove_doubles': hasattr(bmesh.ops, 'remove_doubles'),
        'SpaceView3D.draw_handler_add': hasattr(bpy.types.SpaceView3D, 'draw_handler_add'),
        'gpu.shader.from_builtin': hasattr(gpu.shader, 'from_builtin'),
    }
    return {
        'version': '.'.join(str(x) for x in version[:3]),
        'family': family,
        'checks': checks,
        'probe_sources': {'Mesh.attributes': attrs_source, 'Mesh.uv_layers': uv_source},
        'unverified': sorted(name for name, available in checks.items() if available is None),
        'missing': sorted(name for name, available in checks.items() if available is False),
    }


def assert_supported_environment(bpy, bmesh, gpu):
    result = runtime_features(bpy, bmesh, gpu)
    if result['missing']:
        raise RuntimeError(
            'UV Wall Blender API incompatibility on Blender ' + result['version']
            + ': ' + ', '.join(result['missing'])
            + '. Confirm the actual missing API in support diagnostics.'
        )
    return result
