"""Actual Blender/BMesh cases for BH-03.2A; invoked by the existing one-click lab.

Only isolated in-memory bmeshes are created; real scene never changes.
"""

import math
import traceback

import bmesh
from mathutils import Matrix, Vector
from mathutils.geometry import tessellate_polygon


def run_native_coverage_cases():
    from . import face_uv_density_gate
    cases = []
    # 6.6 cm first segment, long almost-collinear edge, as seen in submitted logs.
    points = [(0.,0.,0.),(.066120,0.,0.),(2.461208,0.,0.),
              (2.981753,0.,0.),(2.981753,.330,0.),(0.,.330,0.)]
    fixtures = [
        ('BH032A_REAL_UV_1000',1000.,False,True),
        ('BH032A_REAL_UV_981_43',981.43,False,False),
        ('BH032A_REAL_UV_AUTHORED_BEND',1000.,True,False),
    ]
    for name, ppm, uv_bend, expected_pass in fixtures:
        bm = bmesh.new()
        try:
            vertices=[bm.verts.new(x) for x in points]
            face=bm.faces.new(vertices)
            uv_layer=bm.loops.layers.uv.new('UVMap')
            for i,loop in enumerate(face.loops):
                p=loop.vert.co
                # Non-border source coordinates to avoid unrelated tile errors.
                u=.10+p.x*ppm/4096.
                v=.24+p.y*1000/4096.
                if uv_bend and i==1:v+=2.0/4096
                loop[uv_layer].uv=(u,v)
            coords=[tuple(loop.vert.co) for loop in face.loops]
            source_uv=[tuple(loop[uv_layer].uv) for loop in face.loops]
            triangles=[list(map(int,tri)) for tri in tessellate_polygon([[Vector(p) for p in coords]])]
            before={'world':coords,'uv':source_uv}
            error=None
            try:face_uv_density_gate(face,uv_layer,Matrix.Identity(4),4096)
            except (ValueError,RuntimeError) as exc:error=f'{type(exc).__name__}: {exc}'
            after={'world':[tuple(l.vert.co) for l in face.loops],
                   'uv':[tuple(l[uv_layer].uv) for l in face.loops]}
            # Explicitly verify read-only validation does not alter loops.
            unchanged=before==after
            passed = (error is None)==expected_pass and unchanged
            cases.append({
                'case':name,'status':'PASS' if passed else 'FAIL',
                'evidence':{'test_type':'actual Blender tessellate_polygon + BMesh',
                            'source_ppm':ppm,'uv_kink':uv_bend,
                            'source_uv_unchanged':unchanged,
                            'loops':len(coords),'blender_triangles':triangles,
                            'complete_n_minus_2':len(triangles)==len(coords)-2,
                            'expected_accept':expected_pass,'actual_accept':error is None,
                            'validator_error':error,
                            'coordinate_snapshot':before if not passed else None}
            })
        except Exception as exc:
            cases.append({'case':name,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-6000:]}})
        finally:
            bm.free()
    return cases
