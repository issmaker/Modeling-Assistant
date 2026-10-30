"""BH04B.5 native BMesh fixtures for source-face content integrity.
These run ONLY inside Blender as part of Native Auto Lab.
"""
import bmesh
import traceback
from .source_face_ledger import capture_source_face_ledger, verify_source_face_ledger


def run_native_source_ledger_cases():
    cases=[]
    bm=None
    try:
        bm=bmesh.new()
        a=bm.verts.new((0,0,0));b=bm.verts.new((1,0,0))
        c=bm.verts.new((1,0,1));d=bm.verts.new((0,0,1))
        f=bm.faces.new((a,b,c,d))
        uv=bm.loops.layers.uv.new('UVMap')
        alt=bm.loops.layers.uv.new('OtherUV')
        for i,l in enumerate(f.loops):
            l[uv].uv=((i%2)*0.2,(i//2)*0.2)
            l[alt].uv=((i%2)*0.3,(i//2)*0.3)
        layers=(('OtherUV',alt),('UVMap',uv))
        ledger=capture_source_face_ledger(bm,[f],layers)
        # A genuine new adjacent face must not mutate the authored quad.
        e=bm.verts.new((1,1,0));g=bm.verts.new((1,1,1))
        bm.faces.new((b,e,g,c))
        checked=verify_source_face_ledger(bm,ledger)
        cases.append({'case':'BH04B5_SOURCE_PRESERVED', 'status':'PASS','evidence':checked})
        # Re-resolve the source face from the ACTIVE mesh, never trust a
        # Python BMFace handle retained across a topology operation.
        source_faces=[face for face in bm.faces if len(face.verts)==4
                      and all(abs(float(v.co.y))<1e-7 for v in face.verts)]
        if len(source_faces)!=1:raise AssertionError('Authored source quad missing')
        current_source=source_faces[0]
        current_source.loops[0][alt].uv=(0.77,0.88)
        try:
            verify_source_face_ledger(bm,ledger)
            raise AssertionError('Changed secondary authored UV went undetected')
        except ValueError as ex:
            cases.append({'case':'BH04B5_SECONDARY_UV_REJECT','status':'PASS',
                          'evidence':{'reason':str(ex)}})
        current_source.loops[0][alt].uv=(0.0,0.0)
        # Original face disappears: must NOT treat the new face as original.
        bm.faces.remove(current_source)
        try:
            verify_source_face_ledger(bm,ledger)
            raise AssertionError('Missing source face went undetected')
        except ValueError as ex:
            cases.append({'case':'BH04B5_SOURCE_REMOVED_REJECT','status':'PASS',
                          'evidence':{'reason':str(ex)}})
    except Exception as exc:
        cases.append({'case':'BH04B5_LEDGER_SUITE','status':'FAIL',
                      'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-8000:]}})
    finally:
        if bm is not None: bm.free()
    return cases
