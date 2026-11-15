import unittest
import math
from issmaker_modeling_assistant.core import universal_geometry as ua

class UniversalGeometryTests(unittest.TestCase):
    def test_single_quad(self):
        v=[(0,0,0),(2,0,0),(2,0,1),(0,0,1)]
        d=ua.analyze_patch(v,[(0,1,2,3)])
        self.assertEqual(d['face_count'],1)
        self.assertEqual(d['boundary_edge_count'],4)
        self.assertEqual(d['boundary_components'],1)
        self.assertEqual(d['nonmanifold_edges'],0)
    def test_connected_two_quads(self):
        v=[(0,0,0),(1,0,0),(2,0,0),(0,0,1),(1,0,1),(2,0,1)]
        d=ua.analyze_patch(v,[(0,1,4,3),(1,2,5,4)])
        self.assertEqual(d['connected_components'],1)
        self.assertEqual(d['boundary_edge_count'],6)
    def test_disconnected_faces_reported(self):
        v=[(0,0,0),(1,0,0),(1,1,0),(0,1,0),(5,0,0),(6,0,0),(6,1,0),(5,1,0)]
        d=ua.analyze_patch(v,[(0,1,2,3),(4,5,6,7)])
        self.assertEqual(d['connected_components'],2)
        self.assertEqual(d['boundary_components'],2)
    def test_four_quads_surround_hole(self):
        v=[(x,y,0) for y in (0,1,2,3) for x in (0,1,2,3)]
        f=[]
        for y in range(3):
            for x in range(3):
                if x==1 and y==1:continue
                a=y*4+x;f.append((a,a+1,a+5,a+4))
        d=ua.analyze_patch(v,f)
        self.assertEqual(d['face_count'],8)
        self.assertEqual(d['connected_components'],1)
        self.assertEqual(d['boundary_components'],2)
    def test_short_edges_on_curve(self):
        pts=[(0,0,0),(1,0,0),(1.02,.001,0),(2,.2,0),(2.03,.21,0),(3,.8,0)]
        r=ua.analyze_polyline(pts)
        self.assertGreaterEqual(r['short_edge_count'],2)
        self.assertEqual(r['vertices'],len(pts))
    def test_true_corner_protected(self):
        pts=[(0,0,0),(1,0,0),(1,.02,0),(1,1,0)]
        r=ua.analyze_polyline(pts)
        self.assertIn(1,r['hard_junction_indices'])
    def test_closed_ring(self):
        r=ua.analyze_polyline([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],closed=True)
        self.assertEqual(r['segments'],4)
        self.assertEqual(r['vertices'],4)
    def test_uv_seam_detected(self):
        v=[(0,0,0),(1,0,0),(2,0,0),(0,0,1),(1,0,1),(2,0,1)]
        f=[(0,1,4,3),(1,2,5,4)]
        u1=[(0,0),(.25,0),(.25,.25),(0,.25)]
        u2=[(.5,0),(.75,0),(.75,.25),(.5,.25)]
        self.assertEqual(ua.analyze_patch(v,f,[u1,u2])['internal_uv_seams'],1)
    def test_uv_seam_not_reported_when_matching(self):
        v=[(0,0,0),(1,0,0),(2,0,0),(0,0,1),(1,0,1),(2,0,1)]
        f=[(0,1,4,3),(1,2,5,4)]
        u1=[(0,0),(.25,0),(.25,.25),(0,.25)]
        u2=[(.25,0),(.5,0),(.5,.25),(.25,.25)]
        self.assertEqual(ua.analyze_patch(v,f,[u1,u2])['internal_uv_seams'],0)
    def test_invalid_face_rejected(self):
        with self.assertRaisesRegex(ValueError,'UGA_INVALID_FACE_RING'):
            ua.analyze_patch([(0,0,0),(1,0,0)],[(0,1,2)])
    def test_nonfinite_point_rejected(self):
        with self.assertRaisesRegex(ValueError,'UGA_NONFINITE_POINT'):
            ua.analyze_polyline([(math.inf,0,0)])
