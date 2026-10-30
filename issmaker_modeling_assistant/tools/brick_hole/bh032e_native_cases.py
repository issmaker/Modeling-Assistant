"""BH-03.2E Blender BMesh-backed snapping tests (no GUI mouse automation).

Screen positions below are controlled test fixture inputs; only vertex world
positions, vertex indexes and topology come from REAL Blender BMesh.
"""
import math
import traceback
import bpy
import bmesh
from .direction_snap_core import choose_vertex_candidate,project_vertex_depth


def run_native_vertex_snap_cases(scene,build_case,snapshot):
    results=[]
    cases=('BH032E_PROJECTED_EXISTING_VERTEX','BH032E_HIDDEN_INDEX_STABILITY',
           'BH032E_OFFSCREEN_REJECT','BH032E_STRICT_REJECTION')
    for case in cases:
        obj=None
        try:
            obj,_,_=build_case(scene,case,choice='top')
            bm=bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            # Source handle at top-edge midpoint (world); target vertex at
            # the *bottom* of the real wall. The midpoint handle axis is
            # 2 metres from all facade corner vertices on a 4 m module.
            edge=next(e for e in bm.edges if e.select)
            world_verts=tuple(obj.matrix_world@v.co for v in edge.verts)
            origin=(world_verts[0]+world_verts[1])*.5
            origin=tuple(origin)
            target=min(bm.verts,key=lambda v:(float(v.co.z),float(v.co.x)))
            bm.verts.index_update()
            target_index=int(target.index)
            target_world=tuple(obj.matrix_world@target.co)
            if case=='BH032E_HIDDEN_INDEX_STABILITY':
                # Hide a different vertex; the target's BMesh ID must remain
                # the true ID instead of enumerate(visible) rank.
                other=next(v for v in bm.verts if v is not target and not v.select)
                other.hide_set(True)
                bm.verts.index_update()
                if target.hide:raise RuntimeError('Target unexpectedly hidden')
            before=snapshot(obj)
            candidates=[(int(v.index),tuple(obj.matrix_world@v.co),
                        (80.,80.) if v is target else (350.+v.index*3.,310.))
                        for v in bm.verts if not v.hide]
            if case=='BH032E_OFFSCREEN_REJECT':
                found=choose_vertex_candidate(candidates,(0.,0.),origin,(0.,0.,1.))
                if found is not None:raise RuntimeError('Off-screen target accepted')
                detail={'rejected_away_from_cursor':True}
            elif case=='BH032E_STRICT_REJECTION':
                projected=choose_vertex_candidate(candidates,(80.,80.),origin,(0.,0.,1.),
                                                   axis_tolerance_m=.025)
                if projected is not None:raise RuntimeError('Off-axis vertex accepted in strict mode')
                detail={'strict_mode_refused_2m_lateral_offset':True}
            else:
                found=choose_vertex_candidate(candidates,(80.,80.),origin,(0.,0.,1.))
                if found is None:raise RuntimeError('Failed to select existing BMesh vertex')
                if found['vertex_index']!=target_index:
                    raise RuntimeError('Vertex index differs from BMesh ID')
                if abs(found['signed_depth']+4.)>1e-7:
                    raise RuntimeError('World projected depth differs from -4m')
                if abs(found['axis_residual_m']-2.)>1e-6:
                    raise RuntimeError('Off-axis projection incorrectly calculated')
                if not found['is_projected']:
                    raise RuntimeError('Off-axis target not labelled as projected')
                if math.dist(found['projected_xyz'],(2.,0.,0.))>1e-7:
                    raise RuntimeError('Projected landing point not on handle axis')
                detail={'vertex_index':target_index,'depth_m':found['signed_depth'],
                        'lateral_offset_m':found['axis_residual_m'],
                        'projected_xyz':found['projected_xyz'],
                        'mouse_automation':'NOT_TESTED'}
            after=snapshot(obj)
            if before!=after:raise RuntimeError('Readonly snap changed Mesh/UV')
            results.append({'case':case,'status':'PASS',
                            'evidence':{'mesh_uv_unchanged':True}|detail})
        except Exception as exc:
            results.append({'case':case,'status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-5000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                results.append({'case':case+'_CLEANUP','status':'FAIL',
                                'evidence':{'reason':str(exc)}})
    return results
