"""Universal Surface Engine: topology/UV/source invariants and stress tests."""
import copy
import math
import unittest
from issmaker_modeling_assistant.core import surface_graph as g


def grid(nx,ny,omit=()):
    vertices=[(float(x),0.,float(y)) for y in range(ny+1) for x in range(nx+1)]
    missing=set(omit)
    faces=[]
    for y in range(ny):
        for x in range(nx):
            if (x,y) in missing:continue
            i=y*(nx+1)+x
            faces.append((i,i+1,i+nx+2,i+nx+1))
    return vertices,faces


class SurfaceGraphTests(unittest.TestCase):
    def test_one_quad_is_single_disk(self):
        v,f=grid(1,1);report=g.analyze(v,f)
        self.assertEqual(report['component_count'],1)
        self.assertEqual(report['boundary_count'],1)
        self.assertTrue(report['legacy_single_loop_compatible'])
        c=report['components'][0]
        self.assertEqual(c['hole_count'],0)
        self.assertEqual(c['genus'],0)
        self.assertEqual(c['euler_characteristic'],1)
        self.assertEqual(c['boundary_loops'][0]['edge_count'],4)
        self.assertAlmostEqual(c['boundary_loops'][0]['perimeter_m'],4.)

    def test_connected_grid_single_border(self):
        v,f=grid(6,5);report=g.analyze(v,f)
        self.assertEqual(report['component_count'],1)
        self.assertEqual(report['boundary_count'],1)
        self.assertEqual(report['components'][0]['boundary_loops'][0]['edge_count'],22)
        self.assertEqual(report['components'][0]['hole_count'],0)

    def test_hole_is_two_real_rings(self):
        v,f=grid(3,3,omit={(1,1)})
        d=g.analyze(v,f)
        self.assertEqual(d['component_count'],1)
        self.assertEqual(d['boundary_count'],2)
        self.assertFalse(d['legacy_single_loop_compatible'])
        comp=d['components'][0]
        self.assertEqual(comp['status'],'VALID_SURFACE_GRAPH')
        self.assertEqual(comp['hole_count'],1)
        self.assertEqual([x['edge_count'] for x in comp['boundary_loops']],[12,4])
        self.assertAlmostEqual(comp['boundary_loops'][0]['perimeter_m'],12.)
        self.assertAlmostEqual(comp['boundary_loops'][1]['perimeter_m'],4.)

    def test_two_separate_components(self):
        v,f=grid(1,1)
        other=[(x+5.,y,z) for x,y,z in v]
        d=g.analyze(v+other,f+[tuple(i+len(v) for i in f[0])])
        self.assertEqual(d['component_count'],2)
        self.assertEqual(d['boundary_count'],2)
        self.assertTrue(all(x['hole_count']==0 for x in d['components']))

    def test_concave_L_shape_preserved(self):
        v,f=grid(2,2,omit={(1,1)})
        d=g.analyze(v,f)
        self.assertEqual(d['boundary_count'],1)
        self.assertEqual(d['components'][0]['boundary_loops'][0]['edge_count'],8)
        self.assertAlmostEqual(d['total_surface_area_m2'],3.)

    def test_3d_nonplanar_patch_does_not_require_projection(self):
        v,f=grid(2,2)
        v=list(v);v[4]=(1.,0.27,1.)
        d=g.analyze(v,f)
        self.assertEqual(d['boundary_count'],1)
        self.assertEqual(d['components'][0]['status'],'VALID_SURFACE_GRAPH')
        self.assertGreater(d['total_surface_area_m2'],4.)

    def test_nonmanifold_shared_edge_reported(self):
        v=[(0,0,0),(1,0,0),(0,0,1),(1,0,1),(.5,1.,0)]
        f=[(0,1,2),(1,0,3),(0,1,4)]
        d=g.analyze(v,f)
        self.assertEqual(len(d['nonmanifold_edges']),1)
        self.assertEqual(d['components'][0]['status'],'NONMANIFOLD')
        self.assertFalse(d['legacy_single_loop_compatible'])

    def test_inconsistent_winding_diagnosed(self):
        v,f=grid(2,1)
        reversed_faces=[f[0],tuple(reversed(f[1]))]
        d=g.analyze(v,reversed_faces)
        self.assertEqual(len(d['winding_conflict_edges']),1)
        self.assertEqual(d['components'][0]['status'],'INCONSISTENT_WINDING')

    def test_preserve_authored_winding(self):
        v,f=grid(1,1)
        a=g.analyze(v,f)['components'][0]['boundary_loops'][0]['vertex_ids']
        b=g.analyze(v,[tuple(reversed(f[0]))])['components'][0]['boundary_loops'][0]['vertex_ids']
        self.assertEqual(a[0],b[0])
        self.assertEqual(a[1:],list(reversed(b[1:])))

    def test_source_face_ids_survive_mapping(self):
        v,f=grid(2,1)
        d=g.analyze(v,f,source_face_ids=[123,456])
        self.assertEqual(d['components'][0]['source_face_ids'],[123,456])

    def test_source_input_immutable(self):
        v,f=grid(3,3,omit={(1,1)})
        uv=[[(float(v[i][0]),float(v[i][2])) for i in face] for face in f]
        before=copy.deepcopy((v,f,uv))
        d=g.analyze(v,f,uv)
        self.assertEqual(before,(v,f,uv))
        self.assertTrue(d['source_vertices_unchanged'])
        self.assertTrue(d['source_uv_unchanged'])
        self.assertEqual(d['source_uv_seams'],0)

    def test_explicit_uv_seam(self):
        v,f=grid(2,1)
        uv=[[(v[i][0],v[i][2]) for i in face] for face in f]
        uv[1]=[(x+7.,y) for x,y in uv[1]]
        self.assertEqual(g.analyze(v,f,uv)['source_uv_seams'],1)

    def test_reject_invalid_uv(self):
        v,f=grid(1,1)
        with self.assertRaisesRegex(ValueError,'UV_COUNT_MISMATCH'):
            g.analyze(v,f,[])
        with self.assertRaisesRegex(ValueError,'UV_LOOP_MISMATCH'):
            g.analyze(v,f,[[(0,0)]])
        with self.assertRaisesRegex(ValueError,'NONFINITE_UV'):
            g.analyze(v,f,[[(math.nan,0)]*4])

    def test_reject_nonfinite_vertex(self):
        v,f=grid(1,1);v[0]=(0,float('inf'),0)
        with self.assertRaisesRegex(ValueError,'NONFINITE_VERTEX'):
            g.analyze(v,f)

    def test_reject_invalid_faces(self):
        v,f=grid(1,1)
        for candidate in [(0,1,1,3),(0,1,99),(0,1)]:
            with self.subTest(candidate=candidate),self.assertRaisesRegex(ValueError,'INVALID_FACE_RING'):
                g.analyze(v,[candidate])

    def test_reject_duplicate_source_ids(self):
        v,f=grid(2,1)
        with self.assertRaisesRegex(ValueError,'INVALID_SOURCE_FACE_IDS'):
            g.analyze(v,f,source_face_ids=[9,9])

    def test_no_vertex_deletion_on_micro_edges(self):
        v=[(0,0,0),(1,0,0),(1.002,0,0),(2,0,0),
           (0,0,1),(1,0,1),(1.002,0,1),(2,0,1)]
        f=[(0,1,5,4),(1,2,6,5),(2,3,7,6)]
        d=g.analyze(v,f)
        self.assertEqual(d['components'][0]['vertices'],8)
        self.assertEqual(d['components'][0]['boundary_loops'][0]['edge_count'],8)

    def test_1000_quad_grid_scalability(self):
        v,f=grid(40,25)
        d=g.analyze(v,f)
        self.assertEqual(d['components'][0]['status'],'VALID_SURFACE_GRAPH')
        self.assertEqual(d['components'][0]['hole_count'],0)
        self.assertEqual(d['components'][0]['boundary_loops'][0]['edge_count'],130)
        self.assertEqual(d['components'][0]['face_indices'][0],0)
        self.assertEqual(len(d['components'][0]['face_indices']),1000)

if __name__=='__main__':unittest.main()
