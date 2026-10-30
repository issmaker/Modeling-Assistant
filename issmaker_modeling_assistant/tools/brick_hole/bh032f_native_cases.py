"""Blender-native, geometry-only BH-03.2F snap checks.

Real BMesh, real BVHTree; controlled pixel locations are NOT live mouse/UI.
"""
import traceback
import bpy
import bmesh
from .snap_spatial_index import SnapScreenGrid
from .snap_occlusion_blender import build_visible_bvh, ray_to_target_is_clear


def run_native_occlusion_cases(scene):
    records=[]
    names=('BH032F_VISIBLE_OCCLUDER','BH032F_HIDE_FRONT_FACE',
           'BH032F_NEAREST_VISIBLE','BH032F_LARGE_BMESH_GRID')
    for name in names:
        obj=None
        mesh=None
        try:
            mesh=bpy.data.meshes.new('ISSMAKER_'+name+'_MESH')
            if name=='BH032F_LARGE_BMESH_GRID':
                coords=[(float(i%120)/10.,float(i//120)/10.,0.) for i in range(12000)]
                mesh.from_pydata(coords,[],[])
            else:
                # Two parallel surface quads.  Camera is at z+3, so a ray to
                # the back z=0 plane is obstructed by the front z=1 plane.
                mesh.from_pydata([(-1.,-1.,0.),(1.,-1.,0.),(1.,1.,0.),(-1.,1.,0.),
                                  (-1.,-1.,1.),(1.,-1.,1.),(1.,1.,1.),(-1.,1.,1.)],
                                 [],[(0,1,2,3),(4,5,6,7)])
            mesh.update()
            obj=bpy.data.objects.new('ISSMAKER_'+name,mesh)
            scene.collection.objects.link(obj)
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for other in bpy.context.selected_objects:
                other.select_set(False)
            bpy.context.view_layer.objects.active=obj
            obj.select_set(True)
            bpy.ops.object.mode_set(mode='EDIT')
            bm=bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            if name=='BH032F_HIDE_FRONT_FACE':
                face=next(f for f in bm.faces if all(v.co.z>.5 for v in f.verts))
                face.hide_set(True)
            bvh=build_visible_bvh(obj)
            if name=='BH032F_LARGE_BMESH_GRID':
                grid=SnapScreenGrid(1920,1080)
                for v in bm.verts:
                    grid.add(v.index, tuple(v.co), (float(v.index%120)*7.,float(v.index//120)*7.))
                nearby,overflow=grid.query((100.,100.))
                if overflow or not nearby:
                    raise RuntimeError('12k-vertex grid rejected ordinary query')
                data={'vertices':len(bm.verts),'indexed':grid.count,'nearby':len(nearby)}
            elif name=='BH032F_HIDE_FRONT_FACE':
                if not ray_to_target_is_clear(bvh,(0.,0.,3.),(0.,0.,0.)):
                    raise RuntimeError('Hidden front face blocks rear vertex')
                data={'hidden_faces_ignored':True}
            elif name=='BH032F_VISIBLE_OCCLUDER':
                if ray_to_target_is_clear(bvh,(0.,0.,3.),(0.,0.,0.)):
                    raise RuntimeError('Visible front face did not occlude rear vertex')
                if not ray_to_target_is_clear(bvh,(0.,0.,3.),(0.,0.,1.)):
                    raise RuntimeError('Surface at target incorrectly classed occluded')
                data={'hidden_by_front_face':True,'front_surface_visible':True}
            else:
                grid=SnapScreenGrid(1920,1080)
                grid.add(0,(0.,0.,0.),(100.,100.))
                grid.add(4,(0.,0.,1.),(101.,100.))
                nearby,overflow=grid.query((100.,100.))
                candidates=[v[0] for v in nearby if ray_to_target_is_clear(bvh,(0.,0.,3.),v[1])]
                if overflow or candidates!=[4]:
                    raise RuntimeError('Nearest occluded vertex was selected instead of visible one')
                data={'chosen_vertex':4,'occluded_rejected':0}
            records.append({'case':name,'status':'PASS','evidence':data|{'uses_real_blender_bmesh':True,'live_mouse':'NOT_TESTED'}})
        except Exception as exc:
            records.append({'case':name,'status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-5000:]}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:
                    bpy.data.objects.remove(obj,do_unlink=True)
                if mesh is not None and mesh.users==0:
                    bpy.data.meshes.remove(mesh)
            except Exception as exc:
                records.append({'case':name+'_CLEANUP','status':'FAIL','evidence':{'reason':str(exc)}})
    return records
