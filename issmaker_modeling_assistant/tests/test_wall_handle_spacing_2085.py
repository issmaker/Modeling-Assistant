"""Regression: real click radii of OFFSET and THICKNESS must not overlap."""
import math
import unittest
from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.viewport.handle_spacing import separate_handle_pair

class ThinDoubleWallHandleTests(unittest.TestCase):
    def test_identical_gizmos_have_disjoint_hit_areas(self):
        for width,height in ((1920,1080),(2560,1440),(320,240)):
            a=(width*.5,height*.5)
            b,changed=separate_handle_pair(a,a,width,height)
            self.assertTrue(changed)
            self.assertGreaterEqual(math.dist(a,b),39.999)
    def test_nearly_colliding_handles_move_only_thickness(self):
        offset=(400.,300.)
        target=(410.,303.)
        new,changed=separate_handle_pair(offset,target,1920,1080)
        self.assertTrue(changed)
        self.assertGreaterEqual(math.dist(offset,new),39.999)
        self.assertEqual(offset,(400.,300.))
    def test_far_controls_are_untouched(self):
        point=(300.,400.)
        new,changed=separate_handle_pair((100.,100.),point,1920,1080)
        self.assertFalse(changed)
        self.assertEqual(new,point)
    def test_near_edges_find_safe_candidate(self):
        for a in ((25.,25.),(1895.,25.),(25.,1055.),(1895.,1055.)):
            new,changed=separate_handle_pair(a,a,1920,1080)
            self.assertTrue(changed)
            self.assertGreaterEqual(math.dist(a,new),39.999)
            self.assertTrue(18.<=new[0]<=1920-18.)
            self.assertTrue(18.<=new[1]<=1080-18.)
    def test_missing_anchor_never_invents_a_hitbox(self):
        self.assertEqual(separate_handle_pair(None,(4.,5.),1920,1080),((4.,5.),False))

    def test_nearby_height_handle_is_never_overlapped(self):
        for width,height in ((1920,1080),(2560,1440),(320,240)):
            offset=(width*.5,height*.5)
            height_handle=(offset[0],offset[1]+42.)
            safe,moved=separate_handle_pair(
                offset,offset,width,height,min_gap=42.,pad=20.,
                obstacles=[(height_handle,16.)])
            self.assertTrue(moved)
            self.assertGreaterEqual(math.dist(safe,offset),42.-1.e-6)
            self.assertGreaterEqual(math.dist(safe,height_handle),39.-1.e-6)

    def test_tiny_region_refuses_overlap_instead_of_unpickable_handle(self):
        result,moved=separate_handle_pair((40.,40.),(40.,40.),80,80,
                                          min_gap=42.,pad=20.)
        self.assertIsNone(result)
        self.assertFalse(moved)

    def test_invalid_projection_never_adds_clickable_handle(self):
        result,moved=separate_handle_pair((100.,100.),(float('nan'),1.),
                                          1920,1080)
        self.assertIsNone(result)
        self.assertFalse(moved)

    def test_existing_correct_hit_target_does_not_jump(self):
        anchor=(800.,500.)
        target=(905.,505.)
        result,moved=separate_handle_pair(anchor,target,1920,1080,
                                         obstacles=[((400.,400.),16.)])
        self.assertFalse(moved)
        self.assertEqual(result,target)

    def test_density_scan_no_intersections_with_other_handles(self):
        # Deterministic stress: near-coincident wall handles across HD/QHD
        # and several neighboring numeric/height anchors.
        for width,height in ((1920,1080),(2560,1440),(320,240)):
            for i in range(120):
                x=24.+(width-48.)*((i*23)%121)/120.
                y=24.+(height-48.)*((i*41)%121)/120.
                offset=(x,y)
                near=(x+((i%7)-3)*2.,y+((i%5)-2)*3.)
                other=(x+48.*math.cos(i*.21),y+48.*math.sin(i*.21))
                obstacles=[(other,16.)]
                safe,moved=separate_handle_pair(offset,near,width,height,
                      min_gap=42.,pad=20.,obstacles=obstacles)
                if safe is None:continue  # controller keeps numeric card
                self.assertGreaterEqual(math.dist(safe,offset),42.-1.e-5)
                self.assertGreaterEqual(math.dist(safe,other),39.-1.e-5)
                self.assertTrue(20.<=safe[0]<=width-20.)
                self.assertTrue(20.<=safe[1]<=height-20.)

if __name__=='__main__':
 unittest.main()
