"""BH-04B.6.4 actual Blender tests for a >100-edge SAFE DEPTH selection.

These run ONLY within the user's Blender via BH031 AUTO TEST. Offline AST is
not considered evidence of their success. Mesh and authored UV are immutable.
"""
import bpy
import bmesh
import math
import traceback


def run_native_large_contour_cases(scene):
    records=[]
    obj=None
    try:
        from . import readonly_edge_safe_depth, _capture_collision_witness, _ACTION_TRACE
        # Convex 138-gon, one authored source face, all 138 boundary edges
        # selected. No dependency on the user's production blend file.
        n=138
        coords=[(1.2*math.cos(2*math.pi*i/n),
                 1.2*math.sin(2*math.pi*i/n), 0.0) for i in range(n)]
        mesh=bpy.data.meshes.new('BH0464_138_EDGES_MESH')
        mesh.from_pydata(coords, [], [tuple(range(n))])
        mesh.update()
        uv=mesh.uv_layers.new(name='UVMap')
        for li, vid in zip(mesh.polygons[0].loop_indices, range(n)):
            x,y,_=coords[vid]
            uv.data[li].uv=(.5+x/8.,.5+y/8.)
        obj=bpy.data.objects.new('BH0464_138_EDGES_NATIVE',mesh)
        scene.collection.objects.link(obj)
        if bpy.context.object and bpy.context.object.mode=='EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for other in tuple(bpy.context.selected_objects): other.select_set(False)
        bpy.context.view_layer.objects.active=obj
        obj.select_set(True)
        bpy.context.tool_settings.mesh_select_mode=(False,True,False)
        bpy.ops.object.mode_set(mode='EDIT')
        bm=bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        for face in bm.faces: face.select_set(False)
        for vert in bm.verts: vert.select_set(False)
        for edge in bm.edges: edge.select_set(True)
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
        layer=bm.loops.layers.uv.active
        def snapshot():
            return (tuple(tuple(float(x) for x in v.co) for v in bm.verts),
                    tuple(tuple(tuple(float(u) for u in l[layer].uv)
                                for l in f.loops) for f in bm.faces),
                    len(bm.edges),len(bm.faces))
        before=snapshot()
        scene.brick_hole_uv_map='UVMap'
        scene.brick_hole_direction_source='AUTO'
        scene.brick_hole_resolution='4096'
        scene.brick_hole_material_mode='INHERIT'
        suggestion=readonly_edge_safe_depth(bpy.context,.06)
        if suggestion.calls<1 or suggestion.calls>12:
            raise RuntimeError('large budget not enforced: '+repr(suggestion))
        if abs(suggestion.candidate-.06)>1.e-9:
            raise RuntimeError('safe candidate unexpectedly changed: '+repr(suggestion))
        if snapshot()!=before:
            raise RuntimeError('SAFE DEPTH modified authored geometry or UV')
        records.append({'case':'BH0464_NATIVE_138_EDGE_SAFE_DEPTH','status':'PASS',
                        'evidence':{'selected_edges':n,'probes':suggestion.calls,
                                    'candidate_m':suggestion.candidate,
                                    'mesh_uv_preserved':True}})
    except Exception as exc:
        records.append({'case':'BH0464_NATIVE_138_EDGE_SAFE_DEPTH','status':'FAIL',
                        'evidence':{'reason':str(exc),
                                    'traceback':traceback.format_exc()[-12000:]}})
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
        except Exception as exc:
            records.append({'case':'BH0464_NATIVE_CLEANUP','status':'FAIL',
                            'evidence':{'reason':str(exc)}})
    try:
        from . import _capture_collision_witness, _ACTION_TRACE
        _capture_collision_witness({'kind':'source_contact',
            'primary_strip':0,'source_face_index':3292,
            'source_face_incident':False,'source_shared_vertex_count':0,
            'contact_xyz':[[1.125,.25,.5],[1.25,.25,.5]]})
        relevant=[entry for entry in _ACTION_TRACE
                  if entry.get('action')=='collision_witness']
        if not relevant or '3292' not in relevant[-1]['detail'] or not (
                'contact_xyz=' in relevant[-1]['detail']):
            raise RuntimeError('collision witness missing from support trace')
        records.append({'case':'BH0464_NATIVE_COLLISION_WITNESS','status':'PASS',
                        'evidence':{'source_face':3292,'trace_has_xyz':True}})
    except Exception as exc:
        records.append({'case':'BH0464_NATIVE_COLLISION_WITNESS','status':'FAIL',
                        'evidence':{'reason':str(exc),
                                    'traceback':traceback.format_exc()[-12000:]}})
    return records
