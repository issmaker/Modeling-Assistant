"""BH0480 native end-to-end regressions for production micro-corner via usual FILLED Apply.

No test changes or saves the user's source scene, selection, or meshes.
Run in OBJECT mode. Tests create and dispose private, temporary cloned objects.
"""
import bpy
import bmesh

def run_native_micro_corner_cases():
    from . import MESH_OT_brick_hole_extrude_faces
    source=bpy.data.objects.get('UV_Wall')
    if source is None or source.type!='MESH':
        raise RuntimeError('BH0480 QA requires source UV_Wall')
    if bpy.context.mode!='OBJECT':
        raise RuntimeError('BH0480 QA runs from OBJECT mode')
    win=bpy.context.window
    original_scene=win.scene
    original_mode=tuple(bpy.context.tool_settings.mesh_select_mode)
    all_objs=tuple(bpy.data.objects)
    all_scenes=tuple(bpy.data.scenes)
    log=bpy.data.texts.get('BrickHole_Log')
    original_log=log.as_string() if log else None
    data_before=(tuple(tuple(v.co) for v in source.data.vertices),
                 tuple(tuple(p.vertices) for p in source.data.polygons),
                 tuple(tuple(uv.uv) for layer in source.data.uv_layers
                       for uv in layer.data))
    lab=bpy.data.scenes.new('BH0480_NATIVE_QA_TEMP')
    rows=[]
    try:
        win.scene=lab
        for label,depth,ppm,permit,expect in (
            ('MICRO_5CM',.05,1000,False,True),
            ('MICRO_491MM',.491817831993103,1000,False,True),
            ('MICRO_800_DENIED',.491817831993103,800,False,False),
            ('MICRO_800_APPROVED',.491817831993103,800,True,True),
        ):
            obj=source.copy()
            obj.data=source.data.copy()
            lab.collection.objects.link(obj)
            old_mesh_name=obj.data.name
            entry={'case':'BH0480_'+label,'status':'FAIL'}
            try:
                bpy.context.view_layer.objects.active=obj
                obj.select_set(True)
                bpy.context.tool_settings.mesh_select_mode=(False,False,True)
                lab.brick_hole_depth=depth
                lab.brick_hole_direction='INWARD'
                lab.brick_hole_direction_source='AUTO'
                lab.brick_hole_uv_map='UVMap'
                lab.brick_hole_resolution='4096'
                lab.brick_hole_material_mode='INHERIT'
                lab.brick_hole_check_intersections=True
                lab.brick_hole_corner_uv_allow_deviation=permit
                bpy.ops.object.mode_set(mode='EDIT')
                bm=bmesh.from_edit_mesh(obj.data)
                bm.faces.ensure_lookup_table()
                uv=bm.loops.layers.uv.get('UVMap')
                if ppm==800:
                    for loop in bm.faces[32].loops:
                        val=loop[uv].uv.copy()
                        loop[uv].uv=(.01171875+(val.x-.01171875)*.8,
                                     .01171875+(val.y-.01171875)*.8)
                for face in bm.faces:
                    face.select_set(False)
                for ix in (31,36,41):
                    bm.faces[ix].select_set(True)
                bm.select_flush(True)
                bmesh.update_edit_mesh(obj.data)
                try:
                    outcome=bpy.ops.mesh.brick_hole_extrude_unified()
                    passed='FINISHED' in outcome
                    exception=''
                except Exception as ex:
                    passed=False
                    exception=str(ex)
                result=bmesh.from_edit_mesh(obj.data)
                face_count=len(result.faces)
                wire_count=sum(e.is_wire for e in result.edges)
                selected_covers=sum(f.select for f in result.faces)
                data=bpy.data.texts.get('BrickHole_Log')
                text=data.as_string() if data else ''
                report_found=('MICRO_CORNER_BOUNDARY_LOCK' in text)
                blocked_by_permission=('требуется ваше разрешение' in text)
                ok=(
                    passed==expect and wire_count==0
                    and ((face_count==201 and selected_covers==2 and report_found)
                         if expect else (face_count==196 and blocked_by_permission))
                )
                entry.update({'status':'PASS' if ok else 'FAIL',
                              'evidence':{'build':passed,'expected':expect,
                                          'faces':face_count,'wires':wire_count,
                                          'caps':selected_covers,
                                          'new_solver_logged':report_found,
                                          'approval_required':blocked_by_permission,
                                          'error':exception[:160]}})
            except Exception as ex:
                entry['evidence']={'test_error':type(ex).__name__+': '+str(ex)}
            finally:
                if bpy.context.mode=='EDIT_MESH':
                    bpy.ops.object.mode_set(mode='OBJECT')
                names={old_mesh_name,obj.data.name}
                bpy.data.objects.remove(obj,do_unlink=True)
                for name in names:
                    mesh=bpy.data.meshes.get(name)
                    if mesh is not None and mesh.users==0:
                        bpy.data.meshes.remove(mesh)
            rows.append(entry)
    finally:
        if bpy.context.mode=='EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
        win.scene=original_scene
        bpy.data.scenes.remove(lab)
        bpy.context.tool_settings.mesh_select_mode=original_mode
        current=bpy.data.texts.get('BrickHole_Log')
        if original_log is None:
            if current:bpy.data.texts.remove(current)
        elif current:
            current.clear()
            current.write(original_log)
    after=(tuple(tuple(v.co) for v in source.data.vertices),
           tuple(tuple(p.vertices) for p in source.data.polygons),
           tuple(tuple(uv.uv) for layer in source.data.uv_layers
                 for uv in layer.data))
    if data_before!=after or all_objs!=tuple(bpy.data.objects) or all_scenes!=tuple(bpy.data.scenes):
        rows.append({'case':'BH0480_SOURCE_PRESERVED','status':'FAIL'})
    else:
        rows.append({'case':'BH0480_SOURCE_PRESERVED','status':'PASS'})
    return rows
