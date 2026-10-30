"""Dependent, parameterically derived planting/soil surface for a double wall.

The wall Path remains the single authority. Child-result Mesh data is replaced
only after geometry + UV + validation succeeded; a failed child cannot damage the
existing wall result. This intentionally does not create an independent Path.
"""
import bpy
from math import radians
from mathutils import Vector
from ..core.result_lifecycle import install_mesh_on_result
from ..core.semantic_geometry import set_face_roles


def generate_inner_surface(context, settings, definition, source_obj, log_event):
    from .path import ordered_path_vertices, is_path_closed, offset_polyline, source_quad_data, total_wall_height
    from .wall_surface import validate_simple_path
    from .surface import build_full_top_cap, topology_stitch_tolerance
    from .validation import smart_regulation_weld, validate_uv_safe_range, validate_uv_faces_not_folded, validate_surface_projected_area, validate_surface_boundary_coverage, merge_coincident_vertex_indices, stitch_t_junctions, sanitize_generated_topology
    from ..core.definitions import ensure_source_uid
    from .planar_integrity import interior_side, assert_nested_boundaries
    if not settings.create_inner or not settings.create_inner_surface:
        return None
    if settings.path_mesh is None or settings.path_mesh.type != 'MESH':
        raise ValueError('Inner Surface requires an authored Path Mesh')
    path = ordered_path_vertices(settings.path_mesh)
    if not is_path_closed(path):
        raise ValueError('Inner Surface requires a closed wall Path')
    from .wall_alignment import left_signed_shift, planter_extra_offset, automatic_inset_side, resolve_placement
    mode = resolve_placement(getattr(settings,'wall_alignment','LEGACY'),path,settings.inset_side)
    wall_side = automatic_inset_side(mode, settings.inset_side)
    alignment_shift = left_signed_shift(mode,
                                        settings.inset_distance,wall_side,True)
    effective_shift = float(settings.wall_offset) + alignment_shift
    if abs(effective_shift) > 1e-8:
        path = offset_polyline(path, abs(effective_shift),
                               'LEFT' if effective_shift > 0 else 'RIGHT')

    # The planting surface is a dependent *interior* result.  Which of the two
    # generated wall shells is geometrically inside depends on closed-loop
    # winding and LEFT/CENTER/RIGHT placement; it is NOT equivalent to
    # settings.inset_side.  1.9.0 forced that shared flag to interior_side(),
    # fighting WALL placement and making RIGHT appear impossible.
    other_shell = offset_polyline(path, float(settings.inset_distance), wall_side)
    validate_simple_path(other_shell, 'Вторая стенка')
    inner_wall_boundary = None
    try:
        assert_nested_boundaries(path, other_shell, 'Внутренняя стенка')
        inner_wall_boundary = other_shell
    except ValueError:
        try:
            assert_nested_boundaries(other_shell, path, 'Внутренняя стенка')
            inner_wall_boundary = path
        except ValueError as exc:
            raise ValueError('Клумба: не удалось определить внутреннюю границу двух стен: ' + str(exc))
    side = interior_side(inner_wall_boundary)
    extra_offset = planter_extra_offset(settings.inner_surface_offset_auto, settings.inner_surface_offset)
    boundary = offset_polyline(inner_wall_boundary, max(0.0,extra_offset), side)
    validate_simple_path(boundary, 'Граница внутренней поверхности')
    if extra_offset > 1e-8:
        assert_nested_boundaries(inner_wall_boundary,boundary,'Граница клумбы')
    target_offset = float(settings.inset_distance) + extra_offset
    # Path is the upper edge in DOWN mode; otherwise top = Path + total height.
    source = source_quad_data(source_obj, settings.tile_length)
    wall_height = total_wall_height(
        source['height'],settings.stories,
        settings.first_story_height if settings.custom_first_story else 0.0
    )
    z_offset = (0.0 if settings.story_direction=='DOWN' else wall_height) + float(settings.inner_surface_height_offset)
    verts, faces, face_uvs = build_full_top_cap(
        boundary,source,settings.tile_length,
        angle_rad=radians(settings.surface_module_rotation),
        z_offset=z_offset,preferred_normal=Vector((0,0,1)),
    )
    if not faces:
        raise ValueError('Inner Surface produced no polygons')
    child_name=(settings.output_name or 'UV_Wall')+'_Inner_Surface'
    mesh=bpy.data.meshes.new(child_name+'_Mesh')
    try:
        mesh.from_pydata(verts,[],faces)
        mesh.update()
        set_face_roles(mesh,['SURFACE']*len(mesh.polygons))
        material=settings.inner_surface_material
        if material is None and len(source_obj.data.materials):
            material=source_obj.data.materials[0]
        if material is not None:
            from ..core.material_intelligence import inspect_material_asset
            material_state = inspect_material_asset(material, None, settings.module_standard)
            if material_state.get('status') != 'VALID':
                raise ValueError('Материал клумбы/поверхности: ' + material_state.get('message','материал не прошёл проверку'))
            mesh.materials.append(material)
        else:
            raise ValueError('Материал клумбы/поверхности не назначен')
        uv_layer=mesh.uv_layers.new(name='UVMap')
        for poly,uvs in zip(mesh.polygons,face_uvs):
            for li,uv in zip(poly.loop_indices,uvs):
                uv_layer.data[li].uv=uv
        smart_regulation_weld(mesh,protected_points=boundary)
        # Recovered UV fragments can introduce vertices along a neighbouring
        # tile edge.  Merge and stitch before checking manifold boundaries.
        merge_coincident_vertex_indices(mesh)
        stitch_t_junctions(mesh, tolerance=topology_stitch_tolerance(settings.tile_length))
        sanitize_generated_topology(mesh)
        validate_surface_projected_area(mesh, boundary, settings.tile_length)
        validate_surface_boundary_coverage(mesh, boundary, settings.tile_length)
        validate_uv_faces_not_folded(mesh)
        validate_uv_safe_range(mesh,source)
        saved_child = getattr(definition, 'last_inner_surface_name', '') if definition is not None else ''
        parent_uid=ensure_source_uid(settings.path_mesh,definition)
        previous=bpy.data.objects.get(saved_child) if saved_child else None
        if previous is not None and (previous.type!='MESH' or previous.get('uvwall_child_parent')!=parent_uid or not previous.get('uvwall_child_surface')):
            previous=None
        if previous is None:
            # Repair migrated/renamed child link by ownership, never by name.
            matches=[o for o in bpy.data.objects if o.type=='MESH' and o.get('uvwall_child_surface') and
                     o.get('uvwall_child_parent')==parent_uid and not o.get('uvwall_detached') and not o.get('uvwall_baked')]
            previous=matches[0] if matches else None
        from .module_seam_flags import mark_mesh_uv_seams
        mark_mesh_uv_seams(mesh)
        obj,reused=install_mesh_on_result(context,mesh,child_name,previous)
        if 'uvwall_pending_delete' in obj:
            del obj['uvwall_pending_delete']
        obj.hide_set(False)
        obj.hide_render=False
        obj['uvwall_child_surface']=True
        obj['uvwall_child_parent']=ensure_source_uid(settings.path_mesh,definition)
        obj['uvwall_child_z_offset']=float(settings.inner_surface_height_offset)
        obj['uvwall_child_offset']=target_offset
        obj['uvwall_child_direction']=side
        if definition is not None:
            definition.last_inner_surface_name=obj.name
        # A migrated old child may have lost Definition.last_name. Keep only
        # this child for the source UID; no duplicate hierarchy entries.
        from ..core.result_lifecycle import schedule_duplicate_children
        duplicates=schedule_duplicate_children(parent_uid,keep=obj)
        log_event(f'INNER_SURFACE result={obj.name}; reused={reused}; duplicates_removed={duplicates}; offset={target_offset:.4f}; direction={side}; inner_wall={settings.inset_distance:.4f}; extra={extra_offset:.4f}; z_offset={z_offset:.4f}; faces={len(mesh.polygons)}','DEPENDENCY_UPDATE')
        return obj
    except Exception:
        if mesh.users==0:
            bpy.data.meshes.remove(mesh)
        raise
