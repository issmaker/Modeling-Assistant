"""Assembly Contact Graph v1: independent source, virtual seat and honest limits."""
import copy
import math
import unittest
from issmaker_modeling_assistant.core import assembly_contact_graph as acg


def fixture():
    # Four outer front, four inner front, back = 8+8; a closed frame
    front=[(0,0,0),(2,0,0),(2,2,0),(0,2,0),
           (.25,.25,0),(1.75,.25,0),(1.75,1.75,0),(.25,1.75,0)]
    back=[(x,y,-.1) for x,y,z in front]
    # Only role counts are inspected in the graph; 3D shell stage validates
    # face manifold and collisions separately.
    return front+back,[(0,1,5,4)],['OUTER_WALL_CONTACT']*4+['INNER_GLAZING_REVEAL']*4


def analyze(source=None,guide=None,**kwargs):
    verts,_,roles=fixture()
    if source is None:source=verts[:4]
    if guide is None:
        guide=[(verts[i][0],verts[i][1],-.05) for i in (4,5,6,7)]
    return acg.evaluate(source,verts,[[0,1,5,4]]*8,roles,4,
                        [4,5,6,7],guide,**kwargs)


class AssemblyContactGraphTests(unittest.TestCase):
    def test_exact_wall_contact_passes(self):
        result=analyze()
        self.assertEqual(result['state'],'PREVIEW_VALIDATED')
        self.assertEqual(result['links'][0]['relation'],'EXACT_SOURCE_RIM')
        self.assertEqual(result['links'][0]['measured_error_m'],0.)
        self.assertEqual(result['contact_links_passed'],2)

    def test_glass_line_is_a_virtual_witness_not_actual_glass(self):
        result=analyze()
        self.assertEqual(result['links'][1]['relation'],'INNER_REVEAL_SEAT')
        self.assertEqual(result['links'][1]['state'],'PASS')
        self.assertEqual(result['links'][2]['state'],'NOT_CERTIFIED')
        self.assertFalse(result['production_glazing_fit_certified'])
        self.assertFalse(result['assembly_fit_certified'])

    def test_perturbed_wall_source_is_contact_conflict(self):
        v,_,_=fixture();src=list(v[:4]);src[2]=(2.01,2,0)
        r=analyze(source=src)
        self.assertEqual(r['state'],'CONTACT_CONFLICT')
        self.assertEqual(r['links'][0]['state'],'FAIL')
        self.assertAlmostEqual(r['links'][0]['measured_error_m'],.01,places=8)

    def test_perturbed_seat_line_is_contact_conflict(self):
        v,_,_=fixture();guide=[(v[i][0],v[i][1],-.05) for i in (4,5,6,7)]
        guide[0]=(guide[0][0]+.02,guide[0][1],guide[0][2])
        r=analyze(guide=guide)
        self.assertEqual(r['state'],'CONTACT_CONFLICT')
        self.assertEqual(r['links'][1]['state'],'FAIL')
        self.assertAlmostEqual(r['links'][1]['measured_error_m'],.02,places=8)

    def test_interior_and_lp_are_explicitly_not_certified(self):
        r=analyze()
        self.assertEqual(r['links'][3]['relation'],'INTERIOR_REVEAL_CONTACT')
        self.assertEqual(r['links'][4]['relation'],'LOD_CONSISTENCY')
        self.assertTrue(all(link['state']=='NOT_CERTIFIED' for link in r['links'][2:]))

    def test_source_input_immutable(self):
        v,_,_=fixture();src=list(v[:4]);guide=[(v[i][0],v[i][1],-.05) for i in (4,5,6,7)]
        old=copy.deepcopy((src,v,guide));analyze(src,guide)
        self.assertEqual((src,v,guide),old)

    def test_seat_fraction_one_third(self):
        v,_,_=fixture();guide=[(v[i][0],v[i][1],-.1/3) for i in (4,5,6,7)]
        r=analyze(guide=guide,seat_fraction=1/3)
        self.assertEqual(r['state'],'PREVIEW_VALIDATED')

    def test_invalid_empty_guide(self):
        with self.assertRaisesRegex(ValueError,'INVALID_GUIDE_RING'):
            analyze(guide=[])

    def test_invalid_in_outer_rim_guide(self):
        v,_,roles=fixture()
        with self.assertRaisesRegex(ValueError,'INVALID_GUIDE_IDS'):
            acg.evaluate(v[:4],v,[[0,1,5,4]]*8,roles,4,[0,1,2,3],v[4:8])

    def test_missing_sidewall_roles(self):
        v,_,roles=fixture()
        with self.assertRaisesRegex(ValueError,'MISSING_WALL_CONTACT_FACES'):
            acg.evaluate(v[:4],v,[[0,1,5,4]]*8,
                ['FRONT']*8,4,[4,5,6,7],[(v[i][0],v[i][1],-.05) for i in (4,5,6,7)])

    def test_invalid_seat_fraction(self):
        with self.assertRaisesRegex(ValueError,'INVALID_TOLERANCE_OR_SEAT'):
            analyze(seat_fraction=0.)

    def test_nan_coordinate_rejected(self):
        v,_,_=fixture();src=list(v[:4]);src[1]=(math.nan,0.,0.)
        with self.assertRaisesRegex(ValueError,'INVALID_POINT'):
            analyze(source=src)

    def test_report_excludes_raw_geometry(self):
        r=analyze()
        self.assertNotIn('vertices',r)
        self.assertNotIn('coordinates',r)
        self.assertEqual(len(r['nodes']),6)

    def test_precision_threshold(self):
        v,_,_=fixture();src=list(v[:4]);src[0]=(.00002,0.,0.)
        r=analyze(source=src,contact_tolerance_m=.00001)
        self.assertEqual(r['state'],'CONTACT_CONFLICT')
        r=analyze(source=src,contact_tolerance_m=.00003)
        self.assertEqual(r['state'],'PREVIEW_VALIDATED')

if __name__=='__main__':unittest.main()
