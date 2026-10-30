"""BH-03.2H.2: compare indexed and reference collision preflight on Blender BMesh."""
import bmesh
import time
import traceback
import bpy


def run_native_broadphase_cases(scene, build_case, snapshot):
    from .swept_collision_guard import validate_swept_edges
    output = []
    for name, direction, with_barrier in (
        ('SAFE_POSITIVE', .20, False),
        ('SAFE_NEGATIVE', -.20, False),
        ('BLOCKED_POSITIVE', .20, True),
        ('BLOCKED_NEGATIVE', -.20, True),
    ):
        obj = None
        try:
            obj, _, count = build_case(scene, 'BH032H2_'+name,
                                       4., 1000., 'top', True, False)
            bm = bmesh.from_edit_mesh(obj.data)
            selected = [e for e in bm.edges if e.select and not e.hide]
            if len(selected) != count: raise AssertionError('Selected edges changed')
            if with_barrier:
                y = -.10 if direction > 0 else .10
                vv = [bm.verts.new(pt) for pt in ((2,y-.05,3.8),(2,y+.05,4.2),(2,y-.05,4.2))]
                bm.faces.new(vv).select_set(False)
                bm.normal_update()
            original = snapshot(obj)
            e = selected[0]
            a, b = (tuple(obj.matrix_world @ v.co) for v in e.verts)
            move = (0., -direction, 0.)
            qa = tuple(a[i]+move[i] for i in range(3))
            qb = tuple(b[i]+move[i] for i in range(3))
            strips = [{'edge':tuple(e.verts),'quad':(a,b,qb,qa)}]
            face_index = {f:i for i,f in enumerate(bm.faces)}
            obstacles = []
            for loops in bm.calc_loop_triangles():
                f = loops[0].face
                obstacles.append({'face_index':face_index[f],
                    'tri':tuple(tuple(obj.matrix_world @ loop.vert.co) for loop in loops),
                    'verts':tuple(loop.vert for loop in loops),
                    'linked_edges':tuple(tuple(ed.verts) for ed in f.edges)})
            results = []
            for mode in ('X_SWEEP','BVH'):
                stats = {}
                try:
                    pairs = validate_swept_edges(strips,obstacles,broadphase=mode,statistics=stats)
                    results.append(('ALLOW',pairs,stats))
                except ValueError as exc:
                    results.append(('BLOCK',str(exc),stats))
            if results[0][:2] != results[1][:2]:
                raise AssertionError('BVH changes guard outcome: '+repr(results))
            if with_barrier and results[0][0] != 'BLOCK':
                raise AssertionError('Intersection not blocked')
            if not with_barrier and results[0][0] != 'ALLOW':
                raise AssertionError('Safe extrusion blocked')
            if snapshot(obj) != original:
                raise AssertionError('Read-only collision changed BMesh/UV')
            output.append({'case':'BH032H2_'+name,'status':'PASS',
                           'evidence':{'reference':results[0], 'optimized':results[1],
                                       'mesh_unchanged':True}})
        except Exception as exc:
            output.append({'case':'BH032H2_'+name,'status':'FAIL',
                           'evidence':{'error':str(exc),'traceback':traceback.format_exc()[-10000:]}})
        finally:
            try:
                if bpy.context.object is not None and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                output.append({'case':'BH032H2_'+name+'_CLEANUP','status':'FAIL',
                               'evidence':{'error':str(exc)}})
    return output
