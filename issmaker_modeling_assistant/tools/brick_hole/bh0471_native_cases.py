"""BH0471 real Blender precision test cases. Invoked only by AUTO TEST BRICK HOLE.

No UI/GPU proof is claimed. Tests are read-only except temporary BMesh data.
"""
import math
import traceback

from mathutils import Matrix, Vector
from .tessellation_reliability import verify_collinear_tessellation, TessellationCoverageError


def run_native_precision_cases():
    rows = []

    def run(case, test):
        try:
            details = test()
            rows.append({'case': 'BH0471_' + case, 'status': 'PASS', 'evidence': details})
        except Exception as exc:
            rows.append({'case': 'BH0471_' + case, 'status': 'FAIL',
                         'evidence': {'reason': str(exc), 'traceback': traceback.format_exc()[-7000:]}})

    base = [(0., 0., 0.), (2., 0., 0.), (4., 0., 0.), (4., 4., 0.), (0., 4., 0.)]
    uv = [(x/4, y/4) for x, y, _ in base]
    tris = [(0, 2, 3), (0, 3, 4)]

    def translated():
        verts = [(p[0]+1e9, p[1]+1e9, p[2]) for p in base]
        assert verify_collinear_tessellation(verts, uv, tris, 4096) == tris
        return {'translation_m': 1e9, 'proof': 'FULL_BOUNDARY_AND_UV'}
    run('LARGE_WORLD_COORDINATES', translated)

    def tiny():
        verts = [(p[0]*1e-4, p[1]*1e-4, p[2]) for p in base]
        assert verify_collinear_tessellation(verts, uv, tris, 4096) == tris
        return {'scale': 1e-4, 'proof': 'NO_ONE_METER_TOLERANCE_FLOOR'}
    run('SMALL_GEOMETRY', tiny)

    def linear_frame():
        from mathutils import Matrix
        rotation = Matrix.Rotation(math.radians(37), 4, 'Z')
        scale = Matrix.Diagonal((1.3, 2.1, 0.75, 1.))
        world = Matrix.Translation(Vector((1.e8, 1.e8, 1.e8))) @ rotation @ scale
        metric = [world.to_3x3() @ Vector(p) for p in base]
        expected_04 = (metric[2] - metric[0]).length
        assert abs(expected_04 - 5.2) < 1e-5, expected_04
        assert verify_collinear_tessellation([tuple(p) for p in metric], uv, tris, 4096) == tris
        return {'translation_m': 1e8, 'scaled_edge_m': float(expected_04),
                'frame': 'WORLD_LINEAR_WITHOUT_TRANSLATION'}
    run('AFFINE_FRAME_PRESERVES_SCALE', linear_frame)

    def uv_authored_kink():
        modified = list(uv)
        modified[1] = (0.5, 0.125)
        try:
            verify_collinear_tessellation(base, modified, tris, 4096)
        except TessellationCoverageError as exc:
            assert 'UV_NONLINEAR' in str(exc), str(exc)
            return {'result': 'REJECTED', 'reason': str(exc)}
        raise AssertionError('Invalid authored UV kink was accepted')
    run('UV_KINK_REJECT', uv_authored_kink)

    def warped():
        vertices = list(base)
        vertices[3] = (4., 4., 0.02)
        try:
            verify_collinear_tessellation(vertices, uv, tris, 4096)
        except TessellationCoverageError as exc:
            assert 'NONPLANAR' in str(exc), str(exc)
            return {'result': 'REJECTED', 'reason': str(exc)}
        raise AssertionError('Warped n-gon was accepted')
    run('NONPLANAR_REJECT', warped)

    def mesh_float_precision():
        # Blender mesh vectors are usually float32; record representability
        # rather than claiming geometry below source precision can be restored.
        import bmesh
        bm = bmesh.new()
        try:
            a = bm.verts.new((0, 0, 0))
            b = bm.verts.new((0.001, 0, 0))
            observed = (b.co - a.co).length
            assert abs(observed-0.001) < 1e-8
            return {'stored_length_m': float(observed), 'source': 'REAL_BMESH'}
        finally:
            bm.free()
    run('BMESH_LOCAL_PRECISION', mesh_float_precision)
    return rows
