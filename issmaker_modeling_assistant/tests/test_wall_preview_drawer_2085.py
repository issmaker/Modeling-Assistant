"""Real geometry/interaction contracts for compact read-only preview menu."""
import unittest
from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.viewport.preview_drawer_layout import (
 preview_drawer_rect, preview_drawer_rows)

def overlap(a,b):
 return min(a[2],b[2])-max(a[0],b[0])>0 and min(a[3],b[3])-max(a[1],b[1])>0

class WallPreviewDrawerTests(unittest.TestCase):
 def test_hd_qhd_and_split_viewports_fit(self):
  for width,height,scale in ((1920,1080,1),(2560,1440,1.3),
                            (1366,768,1),(520,400,1),(980,580,1.4)):
   rail=((width-520)*.5,74,(width+520)*.5,123)
   if width<520:
    rail=(0.,74.,width,123.)
   drawer=preview_drawer_rect(rail,width,height,scale)
   with self.subTest(width=width,height=height,scale=scale):
    self.assertIsNotNone(drawer)
    self.assertTrue(0<=drawer[0]<drawer[2]<=width)
    self.assertTrue(0<=drawer[1]<drawer[3]<=height)
    self.assertFalse(overlap(rail,drawer))
    rows=preview_drawer_rows(drawer,scale)
    self.assertEqual(len(rows),3)
    for row in rows:
     self.assertTrue(drawer[0]<=row[0]<row[2]<=drawer[2])
     self.assertTrue(drawer[1]<=row[1]<row[3]<=drawer[3])
    for a,b in zip(rows,rows[1:]):
     self.assertFalse(overlap(a,b))
 def test_tiny_editor_does_not_render_over_invalid_area(self):
  self.assertIsNone(preview_drawer_rect((0,0,180,40),180,190,1))
if __name__=='__main__':
 unittest.main()
