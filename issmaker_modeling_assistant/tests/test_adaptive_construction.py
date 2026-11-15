"""Regression suite: pure geometry, no scene mutation.

Run in Blender background or a configured addon test environment:
    python -m unittest discover -s tests -p test_adaptive_construction.py -v
"""
import math
import unittest

from issmaker_modeling_assistant.core import adaptive_construction as ac


class AdaptiveConstructionTest(unittest.TestCase):
    def test_micro_edges_are_removed_but_contacts_remain(self):
        p=[(0.,0.,0.),(.7,0.,0.),(.72,.002,0.),(1.5,.12,0.),
           (1.52,.124,0.),(2.2,.4,0.)]
        result=ac.plan(p)
        self.assertEqual(result['source_count'],len(p))
        self.assertGreater(len(result['removed_indices']),0)
        self.assertLessEqual(result['max_error_m'],.00400001)
        self.assertEqual(result['kept_indices'][0],0)
        self.assertEqual(result['kept_indices'][-1],len(p)-1)

    def test_hard_corner_protected(self):
        p=[(0.,0.,0.),(1.,0.,0.),(1.01,0.,0.),
           (1.01,.01,0.),(1.01,1.,0.)]
        result=ac.plan(p)
        # Index 2 is the actual 90-degree turn; index 3 is collinear with
        # the outgoing branch and may safely be removed.
        self.assertIn(2,result['kept_indices'])

    def test_forced_protected_uv_anchor(self):
        p=[(0.,0.,0.),(.7,0.,0.),(.72,.0001,0.),(1.3,.05,0.)]
        r=ac.plan(p,protect=(2,))
        self.assertIn(2,r['kept_indices'])

    def test_long_segment_preserved(self):
        p=[(0.,0.,0.),(.8,0.,0.),(1.6,0.,0.),(2.4,0.,0.)]
        r=ac.plan(p)
        self.assertEqual(r['removed_indices'],[])

    def test_out_of_tolerance_point_rejected(self):
        p=[(0.,0.,0.),(.8,0.,0.),(.81,.02,0.),(1.6,.01,0.)]
        r=ac.plan(p,max_error_m=.004)
        self.assertEqual(r['removed_indices'],[])

    def test_uv_source_points_remain_unchanged(self):
        p=[(0.,0.,0.),(.7,0.,0.),(.72,.002,0.),(1.5,.12,0.)]
        before=list(p)
        r=ac.plan(p)
        vectors=[[0.,1.,0.],[0.,1.,0.],[.2,.98,0.],[.25,.97,0.]]
        old=[list(v) for v in vectors]
        new=ac.interpolate_vectors(p,vectors,r)
        self.assertEqual(p,before)
        self.assertEqual(vectors,old)
        self.assertEqual(new[0],old[0])
        self.assertEqual(new[-1],old[-1])

    def test_offset_guarantee_20cm_and_2m(self):
        src=[[0.,1.,0.],[0.,1.,0.]]
        dst=[[.2,.98,0.],[.4,.9,0.]]
        for depth in (.2,1.,2.,-2.):
            with self.subTest(depth=depth):
                out,worst=ac.bounded_offset_vectors(src,dst,depth,.005)
                self.assertLessEqual(worst,.00500001)
                self.assertLessEqual(math.dist(out[1],src[1])*abs(depth),.00500001)

    def test_nonfinite_fails_closed(self):
        with self.assertRaises(ValueError):
            ac.plan([(0,0,0),(math.nan,0,0),(1,0,0)])
        with self.assertRaises(ValueError):
            ac.bounded_offset_vectors([[0,0,0]],[[math.inf,0,0]],1.)

    def test_degenerate_fails_closed(self):
        result=ac.plan([(0.,0.,0.),(0.,0.,0.),(1.,0.,0.)])
        self.assertFalse(result['removed_indices'])

    def test_randomized_error_budget(self):
        import random
        rng=random.Random(41842)
        for _ in range(120):
            points=[(0.,0.,0.)]
            for i in range(1,28):
                step=rng.choice((.012,.018,.025,.35,.62))
                x=points[-1][0]+step
                y=.15*math.sin(x/2.)
                points.append((x,y,0.))
            result=ac.plan(points,max_error_m=.004)
            self.assertLessEqual(result['max_error_m'],.00400001)
            self.assertEqual(result['kept_indices'][0],0)
            self.assertEqual(result['kept_indices'][-1],len(points)-1)

if __name__=='__main__':unittest.main()
