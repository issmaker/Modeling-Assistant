"""BH04B.2 native BMesh topology tests; run through AUTO TEST only.

Cases are small controlled fixtures with real Blender BMVert identity.  They do
not claim the user's real scene passed; a separate one-click scenario will
prove that later. Source Mesh and authored UV are never modified.
"""
import bmesh
import traceback
from .swept_collision_guard import validate_swept_edges


def run_native_source_tessellation_cases():
    records = []
    for name, drop_shared_id, corrupt_segment, remove_sibling, expectation in (
        ('WELDED_POINT_ONLY', False, False, False, 'ALLOW'),
        ('UNWELDED_POINT_ONLY', True, False, False, 'BLOCK'),
        ('SEGMENT_INSIDE_NEW_FACE', False, True, False, 'BLOCK'),
        ('NO_NONDEGENERATE_SIBLING', False, False, True, 'BLOCK'),
    ):
        bm = bmesh.new()
        try:
            # Original selected EDGE extends from A to B; new strip from y=0
            # to y=.3. The redundant source polygon corner lies off the strip.
            va=bm.verts.new((0,0,0))
            vb=bm.verts.new((1,0,0))
            vp=bm.verts.new((.7,-.2,0) if not corrupt_segment else (.7,.2,0))
            vq=bm.verts.new(vp.co)
            vr=bm.verts.new((.7,-.2,-1))
            coincident=bm.verts.new((1,0,0)) if drop_shared_id else vb
            poly=[vp,vq,coincident,vr]
            source_face=bm.faces.new(poly)
            face_edges=tuple((e.verts[0],e.verts[1]) for e in source_face.edges)
            deg=(vp,vq,coincident)
            good=(vp,coincident,vr)
            def obstacle(verts):
                return {'tri':tuple(tuple(float(c) for c in v.co) for v in verts),
                        'verts':tuple(verts),'face_index':77,'linked_edges':face_edges}
            obstacles=[obstacle(deg)]
            if not remove_sibling:
                obstacles.append(obstacle(good))
            strips=[{'edge':(va,vb), 'quad':((0.,0.,0.),(1.,0.,0.),(1.,.3,0.),(0.,.3,0.))}]
            stats={}; evidence=[]
            try:
                validate_swept_edges(strips,obstacles,witness=evidence.append,statistics=stats)
                observed='ALLOW'
            except ValueError:
                observed='BLOCK'
            assert observed==expectation, f'expected {expectation}, got {observed}'
            if expectation=='ALLOW':
                assert stats['source_zero_area_children_proven_nonblocking']==1, stats
            records.append({'case':'BH04B2_'+name,'status':'PASS','evidence':
                            {'expected':expectation,'observed':observed,'statistics':stats,'witnesses':evidence}})
        except Exception as exc:
            records.append({'case':'BH04B2_'+name,'status':'FAIL',
                            'evidence':{'error':str(exc),'traceback':traceback.format_exc()[-8000:]}})
        finally:
            bm.free()
    return records
