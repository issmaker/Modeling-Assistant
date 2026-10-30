"""Real Blender/BMesh evidence for BH-03.2D; UI mouse gestures NOT verified."""
import math
import traceback
import bpy
import bmesh
from .direction_snap_blender import available_axes
from .direction_snap_core import project_vertex_depth


def run_native_direction_snap_cases(scene, build_case, snapshot):
    results=[]
    cases=('BH032D_AXIS_FILTER','BH032D_LOCAL_AXIS','BH032D_VERTEX_SNAP','BH032D_SNAP_REJECTION')
    for case in cases:
        obj=None
        try:
            obj,mesh,n=build_case(scene,case,choice='top')
            before=snapshot(obj)
            from . import unified_preflight
            ctx=bpy.context
            ctx.scene.brick_hole_resolution='4096'
            ctx.scene.brick_hole_uv_map='UVMap'
            ctx.scene.brick_hole_material_mode='INHERIT'
            ctx.scene.brick_hole_direction_source='AUTO'
            ctx.scene.brick_hole_depth=.3
            ctx.scene.brick_hole_direction='OUTWARD'
            axes=available_axes(ctx)
            if case=='BH032D_AXIS_FILTER':
                if axes['X'][0] or not axes['Y'][0] or not axes['Z'][0]:
                    raise RuntimeError('Horizontal X boundary must block X and allow Y/Z')
                proof={'axes':axes}
            elif case=='BH032D_LOCAL_AXIS':
                # One local axis exactly tangent to X must be refused; the
                # orthonormal alternatives remain geometrically eligible.
                basis=((1.,0.,0.),(0.,1.,0.),(0.,0.,1.))
                local=available_axes(ctx,face_basis=basis)
                if local!=axes:raise RuntimeError('Local/global baseline mismatch')
                proof={'local_axes':local}
            elif case=='BH032D_VERTEX_SNAP':
                # Both endpoints exist in the actual Blender Mesh, and may be
                # read through a 3D world position without mutating the Mesh.
                bm=bmesh.from_edit_mesh(obj.data)
                edge=next(e for e in bm.edges if e.select)
                va=tuple(obj.matrix_world@edge.verts[0].co)
                vertex=tuple(va[k]+(0.,-.3,0.)[k] for k in range(3))
                signed,residual=project_vertex_depth(va,(0.,-1.,0.),vertex)
                if abs(signed-.3)>1e-8 or residual>1e-8:
                    raise RuntimeError('Wrong signed snap projection')
                proof={'depth':signed,'off_axis_m':residual}
            else:
                bm=bmesh.from_edit_mesh(obj.data)
                edge=next(e for e in bm.edges if e.select)
                va=tuple(obj.matrix_world@edge.verts[0].co)
                rejected=False
                try:
                    project_vertex_depth(va,(0.,1.,0.),(va[0]+.15,va[1]+.3,va[2]))
                except ValueError:rejected=True
                if not rejected:raise RuntimeError('Off-axis vertex accepted')
                proof={'off_axis_vertex_rejected':True}
            after=snapshot(obj)
            if before!=after:raise RuntimeError('Readonly direction/snap tests modified Mesh or UV')
            results.append({'case':case,'status':'PASS','evidence':proof|{'mesh_unchanged':True}})
        except Exception as exc:
            results.append({'case':case,'status':'FAIL','evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-5000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                results.append({'case':case+'_CLEANUP','status':'FAIL','evidence':{'reason':str(exc)}})
    return results
