"""BH0475 native acceptance: connected multi-plane FILLED extrusion.

Runs the actual registered operators in a temporary Blender scene. User
objects, selection mode, scene, logs and .blend are restored after the suite.
"""
import bpy
import bmesh
import math
from . import RESULT_LAYER_NAME, face_uv_density_gate


def run_native_multiface_cases():
    arc = ((0., 0.), (1., 0.), (1.8, .55), (2.2, 1.45))
    cases = (
        ('PLANAR_TWO', ((0., 0.), (1.4, 0.), (2.8, 0.)), 'AUTO', (0., -1., 0.), .2, True),
        ('CORNER_45', ((0., 0.), (1.4, 0.), (2.39, .99)), 'AUTO', (0., -1., 0.), .2, True),
        ('CORNER_90', ((0., 0.), (1.4, 0.), (1.4, 1.4)), 'AUTO', (0., -1., 0.), .2, True),
        ('CORNER_120', ((0., 0.), (1.4, 0.), (.7, 1.212435)), 'AUTO', (0., -1., 0.), .2, True),
        ('CURVED_THREE', arc, 'AUTO', (0., -1., 0.), .05, True),
        ('CURVED_THREE_NEGATIVE', arc, 'AUTO', (0., -1., 0.), -.05, True),
        ('CURVED_THREE_NEGATIVE_DEEP_REJECT', arc, 'AUTO', (0., -1., 0.), -.2, False),
        ('CURVED_MANUAL_DIAGONAL', arc, 'MANUAL', (.4, -1., .2), .2, True),
        ('CURVED_MANUAL_HORIZONTAL', arc, 'MANUAL', (0., -1., 0.), .2, True),
        ('MANUAL_TANGENT_REJECT', arc, 'MANUAL', (0., 0., 1.), .2, False),
    )
    win = bpy.context.window
    if win is None or bpy.context.mode != 'OBJECT':
        raise RuntimeError('BH0475 requires Object Mode; scene is untouched')
    previous_scene = win.scene
    previous_selection = tuple(bpy.context.tool_settings.mesh_select_mode)
    previous_objects = tuple(bpy.data.objects)
    previous_meshes = set(bpy.data.meshes)
    previous_path = bpy.data.filepath
    log = bpy.data.texts.get('BrickHole_Log')
    previous_log = log.as_string() if log else None
    lab = bpy.data.scenes.new('BH0475_MULTIFACE_TEMP')
    rows = []
    try:
        win.scene = lab
        for name, path, direction, vector, depth, expect_success in cases:
            n = len(path)
            coords = [(x, y, z) for z in (0., 2.) for x, y in path]
            polygons = [(i, i+1, n+i+1, n+i) for i in range(n-1)]
            mesh = bpy.data.meshes.new('BH0475_' + name + '_MESH')
            mesh.from_pydata(coords, [], polygons)
            mesh.uv_layers.new(name='UVMap')
            ob = bpy.data.objects.new('BH0475_' + name, mesh)
            lab.collection.objects.link(ob)
            bpy.context.view_layer.objects.active = ob
            ob.select_set(True)
            lab.brick_hole_depth = abs(depth)
            lab.brick_hole_direction = 'OUTWARD' if depth > 0 else 'INWARD'
            lab.brick_hole_uv_map = 'UVMap'
            lab.brick_hole_resolution = '4096'
            lab.brick_hole_direction_source = direction
            lab.brick_hole_manual_normal = vector
            lab.brick_hole_material_mode = 'INHERIT'
            try:
                bpy.context.tool_settings.mesh_select_mode = (False, False, True)
                bpy.ops.object.mode_set(mode='EDIT')
                bm = bmesh.from_edit_mesh(ob.data)
                uv = bm.loops.layers.uv.active
                dist = [0.]
                for i in range(n-1):
                    dist.append(dist[-1] + math.dist(path[i], path[i+1]))
                for f in bm.faces:
                    f.select_set(True)
                    for loop in f.loops:
                        v = loop.vert.co
                        k = min(range(n), key=lambda j:
                                (v.x-path[j][0])**2 + (v.y-path[j][1])**2)
                        loop[uv].uv = (.04+dist[k]/4.096, .04+v.z/4.096)
                bm.select_flush(True)
                bmesh.update_edit_mesh(ob.data, loop_triangles=True, destructive=False)
                original_uv = sorted(tuple(sorted(tuple(float(x) for x in l[uv].uv)
                                                  for l in f.loops))
                                     for f in bm.faces)
                original_xyz = sorted(tuple(round(float(x), 6) for x in v.co)
                                      for v in bm.verts)
                op = (bpy.ops.mesh.brick_hole_extrude_unified
                      if name.startswith('CURVED_THREE')
                      else bpy.ops.mesh.brick_hole_extrude_faces)
                problem = None
                try:
                    status = op()
                except RuntimeError as exc:
                    status = {'CANCELLED'}
                    problem = str(exc)
                succeeded = 'FINISHED' in status
                bm = bmesh.from_edit_mesh(ob.data)
                uv = bm.loops.layers.uv.active
                count = 0
                if succeeded:
                    caps = [f for f in bm.faces if f.select and not f.hide]
                    after_uv = sorted(tuple(sorted(tuple(float(x) for x in l[uv].uv)
                                                   for l in f.loops))
                                      for f in caps)
                    if after_uv != original_uv or len(caps) != n-1:
                        raise AssertionError('Cap count or UV changed')
                    if any(f.calc_area() <= 1e-10 for f in bm.faces):
                        raise AssertionError('Degenerate face after extrusion')
                    tag = bm.faces.layers.int.get(RESULT_LAYER_NAME)
                    if tag is None:
                        raise AssertionError('Reveal tags missing')
                    for f in bm.faces:
                        if f[tag] > 0:
                            face_uv_density_gate(f, uv, ob.matrix_world, 4096)
                            count += 1
                    if count < 2:
                        raise AssertionError('Reveal faces missing')
                else:
                    current_xyz = sorted(tuple(round(float(x), 6) for x in v.co)
                                         for v in bm.verts)
                    if current_xyz != original_xyz or len(bm.faces) != n-1:
                        raise AssertionError('Transaction rollback failed')
                rows.append({'case':'BH0475_'+name,
                             'status':'PASS' if succeeded == expect_success else 'FAIL',
                             'evidence':{'finished':succeeded,'reveal_faces':count,
                                         'error':problem}})
            except Exception as exc:
                rows.append({'case':'BH0475_'+name, 'status':'FAIL',
                             'evidence':{'error':type(exc).__name__+': '+str(exc)}})
            finally:
                if bpy.context.mode == 'EDIT_MESH':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.data.objects.remove(ob, do_unlink=True)
    finally:
        if bpy.context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
        win.scene = previous_scene
        for ob in tuple(lab.objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.scenes.remove(lab)
        bpy.context.tool_settings.mesh_select_mode = previous_selection
        for mesh in tuple(bpy.data.meshes):
            if mesh not in previous_meshes and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        current_log = bpy.data.texts.get('BrickHole_Log')
        if previous_log is None:
            if current_log is not None:
                bpy.data.texts.remove(current_log)
        elif current_log is not None:
            current_log.clear()
            current_log.write(previous_log)
        if win.scene is not previous_scene or tuple(bpy.data.objects) != previous_objects or bpy.data.filepath != previous_path:
            raise RuntimeError('BH0475 cleanup did not restore state')
    return rows
