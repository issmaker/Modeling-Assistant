"""UV Flip Inspector v2: relative-only diagnosis, no accidental neighbor blame."""
import unittest

from issmaker_modeling_assistant.core.uv_flip_v2 import (
    analyze_uv_faces, _triangles_interior_overlap,
)

def tri(a,b,c,va,vb,vc):
    return {'vertices':(a,b,c),
            'uv':(va,vb,vc),
            'triangles':((va,vb,vc),)}

def setup():
    # Chain of 4 connected triangles, only final face inverted in UV.
    return [
        tri(0,1,2,(0,0),(1,0),(0,1)),
        tri(1,3,2,(1,0),(1,1),(0,1)),
        tri(1,4,3,(1,0),(2,0),(1,1)),
        tri(3,4,5,(1,1),(2,0),(1,0)),
    ]

class UVFlipV2Tests(unittest.TestCase):
    def test_majority_detects_only_truly_local_inverted_face(self):
        result=analyze_uv_faces(setup())
        self.assertTrue(result['complete'])
        self.assertEqual(result['local_flip_faces'],(3,))
        self.assertNotIn(0,result['issues'])
        self.assertNotIn(1,result['issues'])
        self.assertIn('LOCAL_WINDING_OUTLIER',result['issues'][3])
        self.assertEqual(result['mirrored_islands_advisory'],0)

    def test_whole_mirrored_island_is_advisory_not_face_error(self):
        raw=setup()[:3]
        mirrored=[{**f,'uv':tuple((-u,v) for u,v in f['uv']),
                   'triangles':tuple(tuple((-u,v) for u,v in t)
                                     for t in f['triangles'])} for f in raw]
        result=analyze_uv_faces(mirrored)
        self.assertEqual(result['issues'],{})
        self.assertEqual(result['local_flip_faces'],())
        self.assertEqual(result['mirrored_islands_advisory'],1)

    def test_normal_connected_neighbors_are_not_issues(self):
        result=analyze_uv_faces(setup()[:3])
        self.assertEqual(result['issues'],{})
        self.assertEqual(result['overlap_pairs'],())

    def test_separate_intentionally_overlapping_islands_are_allowed(self):
        item=tri(0,1,2,(0,0),(1,0),(0,1))
        other=tri(3,4,5,(0,0),(1,0),(0,1))
        result=analyze_uv_faces([item,other])
        self.assertEqual(len(result['islands']),2)
        self.assertFalse(result['issues'])

    def test_strict_self_crossing_is_distinct_from_winding(self):
        face={'vertices':(0,1,2,3),
              'uv':((0,0),(1,1),(0,1),(1,0)), 'triangles':()}
        result=analyze_uv_faces([face])
        self.assertIn('SELF_CROSSING_FACE',result['issues'][0])
        self.assertFalse(result['local_flip_faces'])

    def test_connected_face_overlap_detected_but_no_neighbor_winding_false_positive(self):
        result=analyze_uv_faces(setup())
        self.assertTrue(any(2 in pair and 3 in pair
                            for pair in result['overlap_pairs']))
        self.assertNotIn('LOCAL_WINDING_OUTLIER',
                         result['issues'].get(2,()))

    def test_shared_uv_edge_has_zero_area_overlap(self):
        a=((0.,0.),(1.,0.),(0.,1.))
        b=((1.,0.),(1.,1.),(0.,1.))
        self.assertFalse(_triangles_interior_overlap(a,b))

    def test_real_triangle_interior_intersection(self):
        a=((0.,0.),(1.,0.),(0.,1.))
        b=((.1,.1),(.3,.1),(.1,.3))
        self.assertTrue(_triangles_interior_overlap(a,b))

    def test_incomplete_overlap_budget_is_honest(self):
        raw=setup()
        result=analyze_uv_faces(raw,max_overlap_pairs=0)
        self.assertFalse(result['complete'])

    def test_equal_opposite_winding_not_auto_blame(self):
        a=tri(0,1,2,(0,0),(1,0),(0,1))
        # Same edge 1->2 shared, UV triangle lies on wrong side.
        b=tri(1,3,2,(1,0),(0,0),(0,1))
        result=analyze_uv_faces([a,b])
        self.assertEqual(result['local_flip_faces'],())
        self.assertEqual(result['unresolved_islands'],1)

    def test_no_mesh_modification_on_readonly_scan(self):
        faces=setup()
        initial=repr(faces)
        analyze_uv_faces(faces)
        self.assertEqual(repr(faces),initial)

    def test_nonfinite_uv_is_ambiguous_not_autofixed(self):
        a=tri(0,1,2,(0,0),(float('nan'),0),(0,1))
        result=analyze_uv_faces([a])
        self.assertIn('FOLDED_OR_AMBIGUOUS',result['issues'][0])

if __name__=='__main__':
 unittest.main()
