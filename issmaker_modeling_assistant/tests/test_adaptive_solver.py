"""Regression tests for adaptive candidate search; Blender geometry integration is separately exercised."""
import math
import unittest
from issmaker_modeling_assistant.core import adaptive_solver as solver

P=[(0.,0.,0.),(.7,0.,0.),(.72,.002,0.),(1.5,.12,0.),
   (1.52,.124,0.),(2.2,.4,0.),(2.22,.405,0.),(2.9,.85,0.)]
D=[[0.,1.,0.],[.1,.995,0.],[.2,.98,0.],[.25,.97,0.],
   [.28,.96,0.],[.35,.94,0.],[.38,.92,0.],[.45,.9,0.]]

class SolverTest(unittest.TestCase):
    def test_diagnostics_recognize_short_edges(self):
        r=solver.analyze(P,D,1.)
        self.assertGreater(r['short_count'],0)
        self.assertEqual(r['difficulty'],'SHORT_EDGES')

    def test_original_is_first(self):
        c=list(solver.generate(P,D,1.))
        self.assertGreater(len(c),1)
        self.assertEqual(c[0]['name'],'ORIGINAL')
        self.assertEqual(c[0]['vectors'],D)

    def test_original_accepted_without_simplification(self):
        out,report=solver.resolve(P,D,1.,lambda _:True)
        self.assertEqual(out,D)
        self.assertEqual(report['attempts'],1)
        self.assertEqual(report['selected_strategy'],'ORIGINAL')

    def test_failed_original_triggers_search(self):
        source=[list(v) for v in D]
        points=list(P)
        def reject_original(candidate):
            if candidate==source:raise ValueError('SIMULATED_SOURCE_COLLISION')
            return True
        output,receipt=solver.resolve(P,D,1.,reject_original)
        self.assertNotEqual(output,source)
        self.assertGreater(receipt['attempts'],1)
        self.assertEqual(receipt['history'][0]['status'],'REJECTED')
        self.assertEqual(receipt['history'][-1]['status'],'ACCEPTED')
        self.assertEqual(P,points)
        self.assertEqual(D,source)

    def test_multiple_rejections_still_find_later_solution(self):
        count=[0]
        def verifier(_):
            count[0]+=1
            if count[0]<3:raise ValueError('SYNTHETIC_GEOMETRY_CONFLICT')
            return True
        _,receipt=solver.resolve(P,D,1.,verifier)
        self.assertEqual(receipt['attempts'],3)

    def test_exhaustion_does_not_mutate_input(self):
        before=list(P)
        with self.assertRaisesRegex(ValueError,'SOLVER_EXHAUSTED'):
            solver.resolve(P,D,1.,lambda _:False)
        self.assertEqual(P,before)

    def test_depth_2m_bounded(self):
        for c in solver.generate(P,D,2.):
            for a,b in zip(c['vectors'],D):
                self.assertLessEqual(math.dist(a,b)*2.,.0050001)

    def test_deterministic_order(self):
        c1=[x['name'] for x in solver.generate(P,D,1.)]
        c2=[x['name'] for x in solver.generate(P,D,1.)]
        self.assertEqual(c1,c2)

    def test_candidate_count_bounded(self):
        self.assertLessEqual(len(list(solver.generate(P,D,1.,max_trials=3))),3)

    def test_invalid_inputs_rejected(self):
        with self.assertRaisesRegex(ValueError,'SOLVER_VECTOR_COUNT_MISMATCH'):
            solver.analyze(P,D[:1],1.)
        with self.assertRaises(ValueError):
            solver.analyze(P,D,0.)

    def test_protected_anchor_never_removed(self):
        for c in solver.generate(P,D,1.,protect=(2,4)):
            self.assertIn(2,c['plan']['kept_indices'])
            self.assertIn(4,c['plan']['kept_indices'])

if __name__=='__main__':unittest.main()
