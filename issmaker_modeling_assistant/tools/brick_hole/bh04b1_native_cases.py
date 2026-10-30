"""BH-04B.1 Blender-native BMesh coordinate provenance checks.
Run only through AUTO TEST in Blender. No offline Blender PASS claimed.
"""
import bmesh
import traceback
from .swept_collision_guard import validate_swept_edges


def run_native_degenerate_guard_cases():
    results=[]
    for name,geom,expect in (
        ('REMOTE_SOURCE_DEGENERATE',((100,100,100),(101,101,101),(102,102,102)),'ALLOW'),
        ('NEAR_SOURCE_DEGENERATE',((.5,.1,0),(.6,.1,0),(.7,.1,0)),'BLOCK'),
        ('REAL_COLLISION',((.5,.08,-1),(.5,.08,1),(.5,.28,0)),'BLOCK'),
    ):
        bm=bmesh.new()
        try:
            edge_verts=[bm.verts.new(co) for co in ((0,0,0),(1,0,0))]
            src_verts=[bm.verts.new(co) for co in geom]
            witness=[]; stats={}
            strips=[{'edge':tuple(edge_verts),'quad':((0.,0.,0.),(1.,0.,0.),(1.,.3,0.),(0.,.3,0.))}]
            obstacles=[{'tri':tuple(tuple(float(x) for x in v.co) for v in src_verts),
                        'verts':tuple(src_verts),'face_index':64,'linked_edges':()}]
            try:
                validate_swept_edges(strips,obstacles,witness=witness.append,statistics=stats)
                actual='ALLOW'
            except ValueError:
                actual='BLOCK'
            if actual!=expect:raise AssertionError(f'{name}: expected {expect}, got {actual}')
            if name=='NEAR_SOURCE_DEGENERATE':
                if len(witness)!=1 or witness[0]['kind']!='source_degenerate_nearby':
                    raise AssertionError('Missing source degenerate witness')
                if witness[0]['source_face_index']!=64:
                    raise AssertionError('Incorrect face provenance')
            if name=='REMOTE_SOURCE_DEGENERATE' and stats.get('remote_source_degenerate_ignored')!=1:
                raise AssertionError('Remote source degenerate not localized')
            results.append({'case':'BH04B1_'+name,'status':'PASS',
                            'evidence':{'classification':actual,'witness':witness,'statistics':stats}})
        except Exception as exc:
            results.append({'case':'BH04B1_'+name,'status':'FAIL',
                            'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-8000:]}})
        finally:
            bm.free()
    return results
