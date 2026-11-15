"""Pure geometry QA for return-to-WALL control, independent of fake GPU clicks."""
import unittest
from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.viewport.offscreen_recovery import (
 wall_path_visible, return_control_rect, _segment_hits_viewport)

class OffscreenWallTests(unittest.TestCase):
    def test_front_visible(self):
        for width,height in ((1920,1080),(2560,1440),(1366,768),(420,320)):
            path=({'points':[(100.,100.),(width-40.,height-70.)]},)
            self.assertTrue(wall_path_visible(path,lambda x:x,width,height))
    def test_back_and_rotated_missing(self):
        for width,height in ((1920,1080),(2560,1440),(420,320)):
            self.assertFalse(wall_path_visible(
                ({'points':[(-200.,-200.),(-400.,-230.)]},),
                lambda x:x,width,height))
            self.assertFalse(wall_path_visible(
                ({'points':[(2.,1.),(3.,1.)]},),lambda _p:None,width,height))
    def test_crossing_segment_is_visible_even_when_endpoints_outside(self):
        self.assertTrue(wall_path_visible(
            ({'points':[(-1000.,500.),(4000.,500.)]},),
            lambda p:p,1920,1080))
        self.assertTrue(_segment_hits_viewport((-100.,100.),(400.,100.),300,200,10))
    def test_outside_segment_not_visible(self):
        self.assertFalse(wall_path_visible(
            ({'points':[(-400.,-300.),(-200.,-100.)]},),
            lambda p:p,1920,1080))
    def test_no_geometry_must_not_hide_ui(self):
        self.assertTrue(wall_path_visible((),lambda p:p,1920,1080))
    def test_single_button_rect_is_within_viewport(self):
        for width,height in ((1920,1080),(2560,1440),(1366,768),(320,220)):
            for scale in (1.,1.25,1.5,2.):
                with self.subTest(width=width,height=height,scale=scale):
                    x,y,x1,y1=return_control_rect(width,height,scale)
                    self.assertTrue(0<=x<x1<=width)
                    self.assertTrue(0<=y<y1<=height)
                    self.assertGreaterEqual(x1-x,96)
                    self.assertGreaterEqual(y1-y,28)
    def test_very_small_viewport_disables_control(self):
        self.assertIsNone(return_control_rect(95,80))
    def test_no_mesh_modification(self):
        items=[{'points':[(-300.,500.),(-100.,500.)]}]
        frozen=repr(items)
        wall_path_visible(items,lambda x:x,1920,1080)
        self.assertEqual(repr(items),frozen)
if __name__=="__main__":
 unittest.main()
