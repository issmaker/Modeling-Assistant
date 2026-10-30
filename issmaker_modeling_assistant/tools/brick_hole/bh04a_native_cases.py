"""BH-04A Blender-native read-only BMesh corner oracle tests."""
import bmesh, traceback, bpy
from .corner_trim_oracle import analyze_corner_pair


def run_native_corner_oracle_cases():
    records=[]
    for label,kind in [('SHARED_SEAM','safe'),('NONWELDED','unrelated'),
                       ('COPLANAR_OVERLAP','overlap'),('TRIANGLE_WARP','warp')]:
        bm=bmesh.new()
        try:
            v=[bm.verts.new(p) for p in ((0,0,0),(1,0,0),(0,1,0))]
            a={'edge':(v[0],v[1]), 'quad':((0,0,0),(1,0,0),(1,0,.3),(0,0,.3))}
            b={'edge':(v[0],v[2]), 'quad':((0,0,0),(0,1,0),(0,1,.3),(0,0,.3))}
            if kind=='unrelated':
                detached=bm.verts.new((0,0,0))
                b['edge']=(detached,v[2])
            if kind=='overlap':
                b['quad']=a['quad']
            if kind=='warp':
                b['quad']=((0,0,0),(0,1,0),(0,1,.3),(0.5,0,.3))
            before=(len(bm.verts),len(bm.edges),len(bm.faces))
            result=analyze_corner_pair(a,b)
            after=(len(bm.verts),len(bm.edges),len(bm.faces))
            expected={'safe':'SAFE_SHARED_SEAM','unrelated':'BLOCK_UNRELATED',
                      'overlap':'BLOCK_COPLANAR_OVERLAP','warp':'BLOCK_OFFSET_MISMATCH'}[kind]
            if result['status']!=expected:raise AssertionError((result['status'],expected))
            if before!=after:raise AssertionError('Read-only oracle changed BMesh')
            records.append({'case':'BH04A_'+label,'status':'PASS','evidence':result})
        except Exception as exc:
            records.append({'case':'BH04A_'+label,'status':'FAIL',
                            'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-5000:]}})
        finally:bm.free()
    return records


def run_native_corner_operator_cases(scene, snapshot):
    """Invoke the actual registered operator, not only the pure oracle.

    Scene belongs to the existing temporary native laboratory, never the user's
    authoring scene. Both outcomes are read-only, including rejected inputs.
    """
    records=[]
    for case,edge_count in (('REAL_OPERATOR_TWO_EDGES',2),
                            ('REAL_OPERATOR_REJECT_ONE',1)):
        obj=None
        try:
            mesh=bpy.data.meshes.new('ISSMAKER_BH04A_OPERATOR_'+case)
            mesh.from_pydata([(0,0,0),(1,0,0),(1,0,1),(0,0,1),
                              (0,1,0),(0,1,1)],[],[(0,1,2,3),(0,3,5,4)])
            mesh.update()
            uv=mesh.uv_layers.new(name='UVMap')
            for polygon in mesh.polygons:
                for loop_idx in polygon.loop_indices:
                    co=mesh.vertices[mesh.loops[loop_idx].vertex_index].co
                    uv.data[loop_idx].uv=(.2+.1*co.x+.1*co.y,.2+.1*co.z)
            obj=bpy.data.objects.new('ISSMAKER_BH04A_OPERATOR_'+case,mesh)
            scene.collection.objects.link(obj)
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for other in tuple(bpy.context.selected_objects):other.select_set(False)
            bpy.context.view_layer.objects.active=obj
            obj.select_set(True)
            bpy.context.tool_settings.mesh_select_mode=(False,True,False)
            bpy.ops.object.mode_set(mode='EDIT')
            bm=bmesh.from_edit_mesh(obj.data)
            for f in bm.faces:f.select_set(False)
            for e in bm.edges:e.select_set(False)
            for v in bm.verts:v.select_set(False)
            choose=[e for e in bm.edges if all(abs(v.co.z-1.)<1e-7 for v in e.verts)]
            if len(choose)!=2:raise AssertionError('fixture: selected top edges incorrect')
            for e in choose[:edge_count]:e.select_set(True)
            bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
            scene.brick_hole_direction_source='AUTO'
            scene.brick_hole_depth=.3
            scene.brick_hole_direction='OUTWARD'
            before=snapshot(obj)
            outcome=bpy.ops.mesh.brick_hole_corner_analyze()
            after=snapshot(obj)
            status=scene.brick_hole_corner_analysis_status
            if 'FINISHED' not in outcome:raise AssertionError('Operator did not return FINISHED')
            if before!=after:raise AssertionError('Operator mutated Mesh or UV')
            if edge_count==1 and 'BLOCK_INPUT' not in status:
                raise AssertionError('One-edge selection was not explicitly rejected')
            if edge_count==2 and not (status.startswith('SAFE_') or status.startswith('BLOCK_')
                                      or status.startswith('CUT_CANDIDATE')):
                raise AssertionError('Unexpected real operator status '+status)
            records.append({'case':'BH04A_'+case,'status':'PASS',
                            'evidence':{'operator':list(outcome),'status':status,
                                        'mesh_uv_unchanged':True}})
        except Exception as exc:
            records.append({'case':'BH04A_'+case,'status':'FAIL',
                            'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-7000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                records.append({'case':'BH04A_'+case+'_CLEANUP','status':'FAIL',
                                'evidence':{'reason':str(exc)}})
    return records
