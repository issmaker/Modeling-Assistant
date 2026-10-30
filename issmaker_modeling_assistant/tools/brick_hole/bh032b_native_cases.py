"""Blender-native read-only evidence fixtures for EDGE Swept Collision Guard.

Run only from the existing BH031 isolated scene Evidence Lab. The original
working scene is never used for collision fixtures.
"""
import time
import traceback

import bpy
import bmesh


def run_native_swept_cases(scene, build_case, snapshot):
    from . import edge_local_frame
    results = []
    cases = [
        ('BH032B_SAFE_TOP', 'top', .3, False, True),
        ('BH032B_SAFE_TWO', 'top_bottom', .3, False, True),
        ('BH032B_COLLIDE_IN_SWEEP', 'top', .3, True, False),
        ('BH032B_COLLIDE_NEGATIVE', 'top', -.3, True, False),
    ]
    for name,selection,depth,obstacle,should_pass in cases:
        obj = None
        started = time.perf_counter()
        try:
            obj, _, nselected = build_case(scene,name,4.,1000.,selection,True,False)
            bm = bmesh.from_edit_mesh(obj.data)
            selected = [e for e in bm.edges if e.select and not e.hide]
            if len(selected)!=nselected: raise RuntimeError('Selection count changed')
            if obstacle:
                # A real BMesh triangle crossing the swept top side near
                # x=2, z=4, halfway from source edge to extruded edge.
                # The barrier is on the same Mesh but is not selected.
                y = -.15 if depth>0 else .15
                world_points=((2.,y-.10,3.80),(2.,y+.10,4.25),(2.,y-.10,4.25))
                verts=[bm.verts.new(p) for p in world_points]
                barrier=bm.faces.new(verts)
                barrier.select_set(False)
                bm.normal_update()
            before=snapshot(obj)
            caught=None
            try:
                normals,offsets=edge_local_frame(obj,selected,scene,depth,validate_shape=True,bm=bm)
            except ValueError as err:
                caught=str(err)
            after=snapshot(obj)
            if before!=after:
                raise RuntimeError('Read-only collision preflight modified scene geometry or UV')
            if should_pass:
                if caught is not None:raise RuntimeError('Valid EDGE refused: '+caught)
                if len(normals)!=len(selected):raise RuntimeError('Missing local normals')
            elif caught is None or 'COLLISION:' not in caught:
                raise RuntimeError('Crossing not blocked by swept guard: '+str(caught))
            results.append({'case':name,'status':'PASS','evidence':{
                'expected': 'allow' if should_pass else 'reject',
                'outcome': 'allow' if caught is None else caught,
                'read_only':True,'depth':depth,'selected_edges':nselected,
                'elapsed_ms':round((time.perf_counter()-started)*1000.,2)}})
        except Exception as exc:
            results.append({'case':name,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        finally:
            try:
                if bpy.context.object is not None and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:
                    bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                results.append({'case':name+'_CLEANUP','status':'FAIL',
                                'evidence':{'reason':str(exc)}})
    return results
