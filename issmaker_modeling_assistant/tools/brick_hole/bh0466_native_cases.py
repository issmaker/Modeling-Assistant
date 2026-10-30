"""BH0466 real-Blender read-only BMesh checks. NOT GPU/UI acceptance."""

def run_native_collision_switch_cases(lab_scene):
    import bpy
    import bmesh
    from unittest.mock import patch
    from . import edge_local_frame, collision_guard_enabled, BH0466_BUILD_ID
    from .collision_policy_core import intersections_enabled
    from . import swept_collision_guard
    rows=[]
    def check(name, ok, info):
        rows.append({'case':name,'status':'PASS' if ok else 'FAIL','evidence':info})
    check('BH0466_NATIVE_BUILD_ID',
          BH0466_BUILD_ID=='ISSMAKER_BH0466_COLLISION_SWITCH_20261026',
          {'id':BH0466_BUILD_ID})
    check('BH0466_NATIVE_LEGACY_DEFAULT_ON',
          intersections_enabled(object()),{'default':intersections_enabled(object())})
    mesh=bpy.data.meshes.new('BH0466_SWITCH_FIXTURE')
    obj=bpy.data.objects.new('BH0466_SWITCH_OBJECT',mesh)
    lab_scene.collection.objects.link(obj)
    bm=bmesh.new()
    scene=lab_scene
    old=(scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal),
         scene.brick_hole_check_intersections)
    try:
        vertices=[bm.verts.new(v) for v in ((0.,0.,0.),(2.,0.,0.),(2.,2.,0.),(0.,2.,0.))]
        bm.faces.new(vertices)
        bm.normal_update()
        edge=next(e for e in bm.edges if set(e.verts)=={vertices[0],vertices[1]})
        scene.brick_hole_direction_source='MANUAL'
        scene.brick_hole_manual_normal=(0.,0.,1.)
        before=(len(bm.verts),len(bm.edges),len(bm.faces),tuple(tuple(v.co) for v in bm.verts))
        with patch.object(swept_collision_guard,'validate_swept_edges',
                          side_effect=ValueError('COLLISION: synthetic witness')) as blocker:
            scene.brick_hole_check_intersections=True
            blocked=False
            try:edge_local_frame(obj,[edge],scene,.3,validate_shape=True,bm=bm)
            except ValueError as exc:blocked='synthetic witness' in str(exc)
            check('BH0466_NATIVE_PROTECTION_ON_BLOCKS',blocked and blocker.call_count==1,
                  {'guard_calls':blocker.call_count,'blocked':blocked})
            blocker.reset_mock()
            scene.brick_hole_check_intersections=False
            normals,directions=edge_local_frame(obj,[edge],scene,.3,validate_shape=True,bm=bm)
            check('BH0466_NATIVE_PROTECTION_OFF_BYPASSES',
                  blocker.call_count==0 and len(normals)==1 and len(directions)==2,
                  {'guard_calls':blocker.call_count,'directions':len(directions)})
            check('BH0466_NATIVE_BMESH_READONLY',
                  before==(len(bm.verts),len(bm.edges),len(bm.faces),tuple(tuple(v.co) for v in bm.verts)),
                  {'unchanged':before==(len(bm.verts),len(bm.edges),len(bm.faces),tuple(tuple(v.co) for v in bm.verts))})
            check('BH0466_NATIVE_FLAG_OFF_REAL',not collision_guard_enabled(scene),
                  {'flag':bool(scene.brick_hole_check_intersections)})
        # OFF never disables independent degenerate-geometry rejection.
        degenerate_raises=False
        try:edge_local_frame(obj,[edge],scene,0.,validate_shape=True,bm=bm)
        except ValueError:degenerate_raises=True
        check('BH0466_NATIVE_DEPTH_ZERO_STILL_BLOCKED',degenerate_raises,{'blocked':degenerate_raises})
    finally:
        scene.brick_hole_direction_source=old[0]
        scene.brick_hole_manual_normal=old[1]
        scene.brick_hole_check_intersections=old[2]
        bm.free()
        bpy.data.objects.remove(obj,do_unlink=True)
        bpy.data.meshes.remove(mesh)
    return rows
