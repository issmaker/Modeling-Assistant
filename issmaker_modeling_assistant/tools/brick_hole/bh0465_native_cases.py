"""Native smoke checks for BH0465 core, *not* interactive HUD proof."""

def run_native_recovery_cases():
    import bpy
    import bmesh
    from . import BH0465_BUILD_ID
    from .collision_recovery_core import is_recoverable_collision, recovery_hint, probe_budget
    rows=[]
    def check(name, cond, evidence):
        rows.append({'case':name,'status':'PASS' if cond else 'FAIL', 'evidence':evidence})
    check('BH0465_NATIVE_BUILD_ID',
          BH0465_BUILD_ID=='ISSMAKER_BH0465_COLLISION_RECOVERY_20261026',
          {'active_build_id':BH0465_BUILD_ID})
    recognized=is_recoverable_collision(ValueError('COLLISION: новая поверхность пересекает исходную сетку'))
    rejected=not is_recoverable_collision(ValueError('UV source invalid'))
    hint=recovery_hint('COLLISION: новая поверхность пересекает исходную сетку')
    check('BH0465_NATIVE_RECOVERABLE_CLASSIFICATION',
          recognized and rejected and 'S —' in hint and 'Esc' in hint,
          {'real_collision':recognized,'unrelated_value_error_rejected':rejected})
    mesh=bpy.data.meshes.new('BH0465_QA_READONLY_MESH')
    try:
        mesh.from_pydata([(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)],[],[(0,1,2)])
        before=(len(mesh.vertices),len(mesh.edges),len(mesh.polygons),
                tuple(tuple(v.co) for v in mesh.vertices))
        count=probe_budget(46,22187)
        after=(len(mesh.vertices),len(mesh.edges),len(mesh.polygons),
               tuple(tuple(v.co) for v in mesh.vertices))
        check('BH0465_NATIVE_HEAVY_MESH_BUDGET_READONLY',
              count==(12,.005) and before==after,
              {'budget':count,'source_faces_sample':22187,'mesh_unchanged':before==after})
    finally:
        bpy.data.meshes.remove(mesh)
    return rows
