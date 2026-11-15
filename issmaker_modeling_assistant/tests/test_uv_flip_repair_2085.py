"""UV Fix v2: safe pure planning + strict refusal in ambiguity cases."""
import unittest
from issmaker_modeling_assistant.core.uv_flip_repair import (
    propose_single_face_fix,UnsafeUVFix)
from issmaker_modeling_assistant.core.uv_flip_v2 import analyze_uv_faces
from issmaker_modeling_assistant.tests.test_uv_flip_v2_2085 import setup,tri

class UVFlipRepairTests(unittest.TestCase):
 def test_single_triangle_outlier_reflects_only_free_corner(self):
  faces=setup()
  original=repr(faces)
  result=propose_single_face_fix(faces)
  self.assertEqual(result['face'],3)
  self.assertEqual(result['neighbor'],2)
  self.assertEqual(result['changed_corner'],2)
  self.assertEqual(result['old_uv'][:2],result['new_uv'][:2])
  self.assertNotEqual(result['old_uv'][2],result['new_uv'][2])
  self.assertEqual(result['issues_after'],())
  self.assertEqual(repr(faces),original)

 def test_can_reanalyze_proposed_island(self):
  faces=setup()
  proposal=propose_single_face_fix(faces)
  new=dict(faces[3])
  new['uv']=proposal['new_uv']
  new['triangles']=(proposal['new_uv'],)
  after=analyze_uv_faces(faces[:3]+[new])
  self.assertNotIn(3,after['issues'])
  self.assertEqual(after['overlap_pairs'],())

 def test_whole_mirror_is_not_autofixed(self):
  mirrored=[]
  for face in setup()[:3]:
   mirrored.append({**face,
    'uv':tuple((-u,v) for u,v in face['uv']),
    'triangles':tuple(tuple((-u,v) for u,v in t)
                      for t in face['triangles'])})
  with self.assertRaises(UnsafeUVFix):
   propose_single_face_fix(mirrored)

 def test_tied_winding_is_rejected(self):
  faces=[
    tri(0,1,2,(0,0),(1,0),(0,1)),
    tri(1,3,2,(1,0),(0,0),(0,1))]
  with self.assertRaises(UnsafeUVFix):
   propose_single_face_fix(faces)

 def test_invalid_target_is_rejected(self):
  with self.assertRaises(UnsafeUVFix):
   propose_single_face_fix(setup(),target=0)

 def test_tampered_two_anchor_edges_is_rejected(self):
  faces=setup()
  # Duplicate the opposite face to make the outlier's shared edge ambiguous.
  dup=dict(faces[2])
  faces.append(dup)
  with self.assertRaises(UnsafeUVFix):
   propose_single_face_fix(faces)

if __name__=="__main__":
 unittest.main()
