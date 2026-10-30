"""Read-only Blender Mesh adapter for Pass 15.49.3 role diagnostics.

Runs ONLY on explicit report or support-diagnostics command, not HUD redraw.
No BMesh or evaluated Mesh is created. Modifier/render output is NOT certified.
"""
from __future__ import annotations

import json
from .role_surface_validator import WALL_ROLES, scan_triangles
from .surface_orientation import mirrored_transform


def _roles(obj, mesh):
    count=len(mesh.polygons)
    attr=mesh.attributes.get('uvwall_face_role')
    if attr is not None or obj.get('uv_wall_mode') in {'WALL','CORNICE','SURFACE'}:
        if attr is None:
            return ('WALL', (), -1, False, 'ROLE_METADATA_MISSING')
        if len(attr.data)!=count or getattr(attr,'domain','')!='FACE' or getattr(attr,'data_type','')!='INT':
            raise ValueError('ROLE_WALL_METADATA_STALE')
        return ('WALL', attr.data, -1, False, None)
    raw=obj.get('DWOS_glass_roles') or obj.get('DWOS_face_roles')
    if raw is not None:
        try: roles=json.loads(raw)
        except (TypeError,ValueError) as exc:raise ValueError('ROLE_DWOS_METADATA_INVALID') from exc
        if not isinstance(roles,list) or len(roles)!=count or not all(isinstance(x,str) for x in roles):
            raise ValueError('ROLE_DWOS_METADATA_STALE')
        master = obj if obj.get('DWOS_interior_sign') is not None else obj.parent
        sign = master.get('DWOS_interior_sign') if master is not None else None
        if sign not in (-1,1):
            # No dependable anchor; do not pretend the default is factual.
            return ('DWOS', tuple('UNKNOWN' for _ in roles), -1, True,
                    'DWOS_INTERIOR_SIGN_MISSING')
        adaptive_type=str(master.get('DWOS_adaptive_classification','')).upper()
        return ('DWOS',tuple(roles),int(sign),adaptive_type in {'CORNER','CURVED'},None)
    return ('UNKNOWN',(),-1,False,'FACE_ROLES_NOT_PUBLISHED')


def inspect_object(obj, *, mode='FAST', budget=256, full_limit=350000):
    """Inspection of source Mesh in OBJECT mode, not modifier/evaluated render."""
    if getattr(obj,'type',None)!='MESH' or getattr(obj,'mode',None)!='OBJECT':
        raise ValueError('ROLE_REQUIRES_OBJECT_MODE_MESH')
    mesh=obj.data
    if mesh is None:
        raise ValueError('ROLE_MESH_MISSING')
    family,roles,sign,adaptive,warning=_roles(obj,mesh)
    matrix3=obj.matrix_world.to_3x3()
    mirror=mirrored_transform(tuple(tuple(matrix3[row][col] for col in range(3)) for row in range(3)))
    tris=mesh.loop_triangles
    if mesh.polygons and not tris:
        raise ValueError('ROLE_TRIANGULATION_UNAVAILABLE')
    uv=mesh.uv_layers.active
    uv_data=uv.data if uv is not None else None
    def triangle_at(index):
        tri=tris[index]
        return (int(tri.polygon_index), tuple(int(i) for i in tri.vertices),
                tuple(tuple(float(x) for x in uv_data[int(li)].uv) for li in tri.loops)
                if uv_data is not None else None)
    def vertex_at(index):return mesh.vertices[index].co
    def role_at(fi):
        if not roles:return 'UNKNOWN'
        if family=='WALL':return WALL_ROLES.get(int(roles[fi].value),'UNKNOWN')
        return roles[fi]
    result=scan_triangles(len(tris),triangle_at,vertex_at,role_at,
                          family=family,interior_sign=sign,adaptive=adaptive,
                          mode=mode,sample_budget=budget,full_limit=full_limit)
    # Local role validation is NOT sufficient proof about a mirrored world
    # object or an evaluated modifier stack. Keep them explicitly partial.
    if result['status']=='ROLE_ANCHORS_CHECKED' and mirror=='MIRRORED':
        result['status']='PARTIAL_WORLD_MIRROR'
    if result['status']=='ROLE_ANCHORS_CHECKED' and obj.modifiers:
        result['status']='PARTIAL_MODIFIERS'
    result.update(source_object=obj.name,face_role_family=family,
                  face_role_metadata_warning=warning, adaptive_role_axes_unverified=adaptive,
                  object_transform_orientation=mirror, evaluated_modifiers_unverified=bool(obj.modifiers),
                  uv_layer=uv.name if uv is not None else None,
                  inspection_source='BLENDER_SOURCE_MESH',
                  limitations='SOURCE_MESH_ONLY; SHADING/TANGENTS AND MODIFIERS UNVERIFIED')
    return result
