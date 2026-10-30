"""BH04B.6 actual Blender/BMesh policy routing tests, NOT live GPU/mouse tests."""
import bpy,bmesh,traceback
from .uv_policy_contract import support_decision

def run_native_uv_policy_cases(scene, build_case, snapshot):
    cases=(('BH046_CONTINUE_APPLY','CONTINUE','top',4.,750.,True),
           ('BH046_ADAPTIVE_APPLY','ADAPTIVE','bottom',2.,1200.,True),
           ('BH046_CONNECTED_REFUSE','CONTINUE','connected',4.,750.,False),
           ('BH046_FACE_REFUSE','ADAPTIVE','top',4.,750.,False))
    out=[]
    for name,mode,selection,length,ppm,expected_success in cases:
        obj=None
        try:
            obj,mesh,count=build_case(scene,name,length,ppm,selection,True,False)
            scene.brick_hole_uv_policy=mode
            scene.brick_hole_uv_transition_m=.25
            scene.brick_hole_depth=.30
            scene.brick_hole_direction='OUTWARD'
            scene.brick_hole_direction_source='AUTO'
            scene.brick_hole_uv_map='UVMap'
            scene.brick_hole_resolution='4096'
            scene.brick_hole_material_mode='INHERIT'
            if name=='BH046_FACE_REFUSE':
                bpy.context.tool_settings.mesh_select_mode=(False,False,True)
                bm=bmesh.from_edit_mesh(obj.data)
                for f in bm.faces:f.select_set(False)
                # Blender edit BMesh index tables are invalid after construction.
                # Refresh before indexed access: otherwise the *test fixture*
                # fails before invoking the production operator.
                bm.faces.ensure_lookup_table()
                bm.faces[0].select_set(True)
                bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
            before=snapshot(obj)
            error=''
            try:response=bpy.ops.mesh.brick_hole_uv_policy_apply()
            except RuntimeError as exc:
                error=str(exc);response={'CANCELLED'}
            after=snapshot(obj)
            if expected_success:
                if 'FINISHED' not in response or len(after['faces'])<=len(before['faces']):
                    raise AssertionError('Valid mode did not create geometry')
                # Independent authored-loop preservation: before original UV data
                # must still exist verbatim in the new snapshot.
                for source in before['uvfaces']:
                    if source not in after['uvfaces']:
                        raise AssertionError('Source authored UV changed')
            else:
                if 'CANCELLED' not in response or after!=before:
                    raise AssertionError('Expected refusal with untouched BMesh/UV')
                # An unexpected refusal (or a Python exception) is NOT PASS.
                # Negative fixtures must be refused for the precise contract.
                expected_hint = ('связанные рёбра' if name=='BH046_CONNECTED_REFUSE'
                                 else 'Face Extrude' if name=='BH046_FACE_REFUSE'
                                 else None)
                if expected_hint and expected_hint not in error:
                    raise AssertionError('Wrong rejection reason: expected '+expected_hint+
                                         '; received '+repr(error[:350]))
            out.append({'case':name,'status':'PASS','evidence':{
                'mode':mode,'operator':sorted(response),'before_faces':len(before['faces']),
                'after_faces':len(after['faces']),'source_preserved':True,
                'expected_refusal':not expected_success,'bpy_error':error[:300]}})
        except Exception as exc:
            out.append({'case':name,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                out.append({'case':name+'_CLEANUP','status':'FAIL','evidence':{'error':str(exc)}})
    # Native geometry test of layout boxes (NOT actual GPU/click verification).
    try:
        from . import interactive_hud_layout
        from ...core.dwos_hud_contract import rectangle_report
        matrix=[]
        for width,height in ((360,240),(568,360),(800,600),(1280,720)):
            boxes=interactive_hud_layout(width,height)
            report=rectangle_report(boxes,(width,height),interactive=(
                'uv_strict','uv_adaptive','uv_continue','apply','cancel'))
            if not report['ok']:raise AssertionError(repr(report['errors']))
            matrix.append((width,height,report['interactive_count']))
        out.append({'case':'BH046_HUD_NATIVE_LAYOUT_ONLY','status':'PASS',
                    'evidence':{'bounds':matrix,'gpu_rendered':False,'mouse_tested':False}})
    except Exception as exc:
        out.append({'case':'BH046_HUD_NATIVE_LAYOUT_ONLY','status':'FAIL',
                    'evidence':{'error':str(exc),'traceback':traceback.format_exc()[-10000:]}})
    return out
