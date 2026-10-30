"""Real Blender read-only Safe Depth checks for BH-03.2C Evidence Lab."""
import time
import traceback
import bpy
import bmesh


def run_native_safe_depth_cases(scene, build_case, snapshot):
    from . import readonly_edge_safe_depth
    records=[]
    # Mesh barrier is intentionally created ONLY in the private lab scene.
    cases=(
        ('BH032C_SAFE_AS_REQUESTED',False,.3,False),
        ('BH032C_CONSERVATIVE_POSITIVE',True,.5,False),
        ('BH032C_CONSERVATIVE_NEGATIVE',True,-.5,False),
        ('BH032C_BAD_UV_REJECT',False,.3,True),
    )
    for name,obstacle,depth,bad_uv in cases:
        obj=None
        start=time.perf_counter()
        try:
            obj,_,_=build_case(scene,name,4.,1000.,'top',not bad_uv,False)
            bm=bmesh.from_edit_mesh(obj.data)
            if obstacle:
                y=-.15 if depth>0 else .15
                vv=[bm.verts.new(p) for p in
                    ((2.,y-.1,3.8),(2.,y+.1,4.25),(2.,y-.1,4.25))]
                bm.faces.new(vv).select_set(False)
                bm.normal_update()
            before=snapshot(obj)
            outcome=None
            try:
                outcome=readonly_edge_safe_depth(bpy.context,depth)
            except ValueError as exc:
                if not bad_uv: raise
                outcome=str(exc)
            after=snapshot(obj)
            if before != after: raise RuntimeError('Safe Depth mutated user geometry or UV')
            if bad_uv:
                if not isinstance(outcome,str) or 'UV' not in outcome:
                    raise RuntimeError('Bad UV must reject without candidate: '+str(outcome))
            elif obstacle:
                if not (0.001<=abs(outcome.candidate)<.3 and
                        (outcome.candidate>0)==(depth>0)):
                    raise RuntimeError('Conservative safe bound not returned: '+str(outcome))
            elif outcome.candidate!=depth:
                raise RuntimeError('Safe requested depth changed: '+str(outcome))
            records.append({'case':name,'status':'PASS','evidence':{
                'requested':depth,'candidate':getattr(outcome,'candidate',None),
                'probes':getattr(outcome,'calls',None),'read_only':True,
                'elapsed_ms':round((time.perf_counter()-start)*1000,2)}})
        except Exception as exc:
            records.append({'case':name,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                records.append({'case':name+'_CLEANUP','status':'FAIL',
                                'evidence':{'reason':str(exc)}})
    return records
