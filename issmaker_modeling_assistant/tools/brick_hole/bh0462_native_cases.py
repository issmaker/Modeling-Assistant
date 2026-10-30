"""BH0462 *actual* Blender BMesh tests; never label offline tests as native."""
import bpy
import bmesh
import traceback
import math
from .seam_inspector import scan_bmesh


def _make_seam_object(scene):
    mesh=bpy.data.meshes.new('BH0462_UV_SEAM_NATIVE_MESH')
    mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0),
                      (2,0,0),(2,1,0)],[],[(0,1,2,3),(1,4,5,2)])
    mesh.uv_layers.new(name='UVMap')
    mesh.update()
    obj=bpy.data.objects.new('BH0462_UV_SEAM_NATIVE',mesh)
    scene.collection.objects.link(obj)
    for old in tuple(bpy.context.selected_objects):old.select_set(False)
    bpy.context.view_layer.objects.active=obj
    obj.select_set(True)
    bpy.context.tool_settings.mesh_select_mode=(False,True,False)
    bpy.ops.object.mode_set(mode='EDIT')
    bm=bmesh.from_edit_mesh(obj.data);layer=bm.loops.layers.uv.active
    for face in bm.faces:
        for loop in face.loops:
            x,y=loop.vert.co.xy
            loop[layer].uv=(float(x)*.2,float(y)*.2)
            if float(face.calc_center_median().x)>1.:
                loop[layer].uv.x+=.25
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    return obj


def run_native_single_tile_cases(scene,build_case,snapshot):
    result=[]
    # New long-depth regression evidence. These are true bpy operator executions.
    specs=(('BH0462_CONTINUE_5M','CONTINUE',2.,1000.,5.1345,4096,True),
           ('BH0462_ADAPTIVE_6M','ADAPTIVE',2.,900.,6.,4096,True),
           ('BH0462_CONTINUE_8M','CONTINUE',2.,750.,8.,4096,True),
           ('BH0462_CONTINUE_2K_5M','CONTINUE',1.5,2000.,5.,2048,True),
           ('BH0462_SOURCE_OUTSIDE_BLOCK','CONTINUE',5.,1000.,5.,4096,False))
    for label,mode,length,density,depth,resolution,should_succeed in specs:
        obj=None
        try:
            obj,mesh,_=build_case(scene,label,length,density,'top',True,False)
            scene.brick_hole_uv_policy=mode
            scene['brick_hole_uv_policy_set']=True
            scene.brick_hole_uv_transition_m=.3
            scene.brick_hole_resolution=str(resolution)
            scene.brick_hole_uv_map='UVMap'
            scene.brick_hole_direction='OUTWARD'
            scene.brick_hole_direction_source='AUTO'
            scene.brick_hole_depth=depth
            scene.brick_hole_material_mode='INHERIT'
            before=snapshot(obj)
            error=''
            try:status=bpy.ops.mesh.brick_hole_uv_policy_apply()
            except RuntimeError as exc:
                error=str(exc);status={'CANCELLED'}
            after=snapshot(obj)
            if should_succeed:
                if 'FINISHED' not in status:raise AssertionError('Apply cancelled: '+error)
                if len(after['faces'])<=len(before['faces'])+1:
                    raise AssertionError('No actual long-strip cuts generated')
                for source in before['uvfaces']:
                    if source not in after['uvfaces']:
                        raise AssertionError('Source geometry UV changed')
                bm=bmesh.from_edit_mesh(obj.data)
                layer=bm.loops.layers.uv.active
                faces=[face for face in bm.faces if
                       any(tuple(round(float(v),6) for v in l.vert.co)
                           not in before['vertices'] for l in face.loops)]
                if not faces:raise AssertionError('Missing new faces')
                low= (48 if resolution==4096 else 24)/resolution-3e-6
                hi=1-low
                for face in faces:
                    if face.calc_area()<=1e-11:raise AssertionError('Zero-area new polygon')
                    for loop in face.loops:
                        if any(not low<=float(t)<=hi for t in loop[layer].uv):
                            raise AssertionError('UV outside original single tile')
                # Source face must retain original loops and topology, not be
                # cut into T junctions by UV-only strip clipping.
                old_face_uv=before['uvfaces'][0]
                if old_face_uv not in after['uvfaces']:
                    raise AssertionError('Source face subdivided')
            else:
                if 'CANCELLED' not in status or after!=before:
                    raise AssertionError('Expected refusal with atomic rollback: '+str(status))
                if not error or 'SINGLE_TILE' not in error:
                    raise AssertionError('Wrong error reason: '+error)
            result.append({'case':label,'status':'PASS',
                           'evidence':{'operator':sorted(status),
                                       'before_faces':len(before['faces']),
                                       'after_faces':len(after['faces']),
                                       'source_uv_preserved':True,
                                       'single_tile_bounds':True,
                                       'long_depth_m':depth,
                                       'texture_periodicity_proven':False}})
        except Exception as exc:
            result.append({'case':label,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                result.append({'case':label+'_CLEANUP','status':'FAIL',
                               'evidence':{'reason':str(exc)}})
    # Independent BMesh seam analysis, read-only and exact intentional marker.
    obj=None
    try:
        obj=_make_seam_object(scene)
        bm=bmesh.from_edit_mesh(obj.data)
        bm.faces.index_update()
        layer=bm.loops.layers.uv.active
        def uv_snapshot():
            return tuple(tuple(tuple(l[layer].uv) for l in f.loops) for f in bm.faces)
        before=uv_snapshot()
        issues,stats=scan_bmesh(bm,obj)
        if stats['broken']!=1 or stats['review']!=0:
            raise AssertionError('Failed unexpected UV seam detection: '+repr(stats))
        if uv_snapshot()!=before:raise AssertionError('Analyzer modified authored UV')
        result.append({'case':'BH0462_UV_SEAM_UNMARKED','status':'PASS','evidence':stats})
        edge=next(e for e in bm.edges if len(e.link_faces)==2)
        edge.seam=True
        issues,stats=scan_bmesh(bm,obj)
        if stats['broken']!=0 or stats['review']!=1:
            raise AssertionError('Authored seam classified as error: '+repr(stats))
        if uv_snapshot()!=before:raise AssertionError('Marker analyzer changed UV')
        result.append({'case':'BH0462_UV_SEAM_INTENTIONAL','status':'PASS','evidence':stats})
    except Exception as exc:
        result.append({'case':'BH0462_UV_SEAM_NATIVE','status':'FAIL',
                       'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
        except Exception as exc:
            result.append({'case':'BH0462_SEAM_CLEANUP','status':'FAIL',
                           'evidence':{'reason':str(exc)}})
    return result
