"""BH-03.2H.1: actual BMesh selection and read-only direction alternatives.

Validates evidence from real Blender BMesh, not interactive GPU/HUD.
"""
import bmesh


def run_native_alternative_cases(scene,build_case,snapshot):
    from .direction_alternatives_blender import query_alternatives
    from .direction_alternatives_core import candidate_specs
    import bpy
    results=[]
    for name,depth in [('AXES_POSITIVE',.05),('AXES_NEGATIVE',-.05)]:
        obj,mesh,count=build_case(scene,'BH032H1_'+name,choice='top',density=1000.)
        original=snapshot(obj)
        settings=(scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal))
        report=query_alternatives(bpy.context,depth,max_probes=8,max_seconds=30.)
        keys=[x['key'] for x in report['items']]
        passed=(count==1 and keys==[c[0] for c in candidate_specs()]
                and all(x['status']=='GEOMETRY_BLOCKED' for x in report['items'] if x['axis']=='X')
                and report['probes']>=1 and snapshot(obj)==original
                and (scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal))==settings)
        results.append({'case':'BH032H1_'+name,'status':'PASS' if passed else 'FAIL',
                        'evidence':{'keys':keys,'probes':report['probes'],
                                    'statuses':{a['key']:a['status'] for a in report['items']},
                                    'scene_restored':(scene.brick_hole_direction_source,tuple(scene.brick_hole_manual_normal))==settings,
                                    'mesh_unchanged':snapshot(obj)==original}})
    return results
