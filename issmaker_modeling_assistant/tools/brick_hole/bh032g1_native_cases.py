"""BH-03.2G.1 Blender-native BMesh topology regression.

Native suite does not alter the user's scene. Read-only pure collision guard
is given genuine BMVert identities, including deliberately non-welded duplicates.
"""
import time
import traceback
import bmesh
from .swept_collision_guard import validate_swept_edges


def run_native_shared_vertex_cases():
    results = []
    # Relocated from the real field witness (Blender 4.5.5), translated to (0,0,0).
    q0=(0.,0.,0.)
    q1=(0.,0.,-2.000343322753906)
    qb=(-0.29891543384057,-0.025486751168434,-2.000343347060)
    qa=(-0.29891543384057,-0.025486751168434,-0.000000024306)
    su=(0.,0.,4.00000023841858)
    ss=(-0.11588287353516,-0.00997161865234,0.)
    setups = (
        ('BH032G1_TOPO_VERTEX_TOUCH', 'shared', True),
        ('BH032G1_UNWELDED_TOUCH', 'unwelded', False),
        ('BH032G1_INTERIOR_PENETRATION', 'crossing', False),
        ('BH032G1_AREA_OVERLAP', 'overlap', False),
    )
    for label,kind,expected_ok in setups:
        bm=bmesh.new()
        started=time.perf_counter()
        try:
            a=bm.verts.new(q0)
            b=bm.verts.new(q1)
            bm.edges.new((a,b))
            if kind=='shared':
                third=bm.verts.new(su)
                fourth=bm.verts.new(ss)
                face=bm.faces.new((third,a,fourth))
            elif kind=='unwelded':
                duplicate=bm.verts.new(q0)
                third=bm.verts.new(su)
                fourth=bm.verts.new(ss)
                face=bm.faces.new((third,duplicate,fourth))
            elif kind=='crossing':
                # Shares a genuine topological vertex, but intersects the
                # interior of the new strip along the x-axis at z=-0.5.
                third=bm.verts.new((-0.23,-0.20,-0.5))
                fourth=bm.verts.new((-0.23,0.20,-0.5))
                face=bm.faces.new((a,third,fourth))
            else:
                # Coplanar positive-area region despite source vertex ID.
                third=bm.verts.new(tuple((q0[k]+qb[k])*0.5 for k in range(3)))
                fourth=bm.verts.new(tuple((q0[k]+qa[k])*0.5 for k in range(3)))
                face=bm.faces.new((a,third,fourth))
            strip={'edge':(a,b),'quad':(q0,q1,qb,qa)}
            obstacle={'tri':tuple(tuple(v.co) for v in face.verts),
                      'verts':tuple(face.verts),
                      'linked_edges':tuple(tuple(e.verts) for e in face.edges)}
            before=(len(bm.verts),len(bm.edges),len(bm.faces),
                    tuple(tuple(float(x) for x in v.co) for v in bm.verts))
            error=None
            try: validate_swept_edges([strip],[obstacle])
            except ValueError as exc: error=str(exc)
            after=(len(bm.verts),len(bm.edges),len(bm.faces),
                   tuple(tuple(float(x) for x in v.co) for v in bm.verts))
            if before!=after: raise AssertionError('Collision check mutated BMesh')
            if (error is None)!=expected_ok:
                raise AssertionError('Wrong collision verdict: '+str(error))
            results.append({'case':label,'status':'PASS','evidence':{
                'expected':'allow' if expected_ok else 'reject',
                'outcome':'allow' if error is None else error,
                'read_only':True,'actual_bmesh':True,
                'elapsed_ms':round((time.perf_counter()-started)*1000,3)}})
        except Exception as exc:
            results.append({'case':label,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-10000:]}})
        finally: bm.free()
    return results
