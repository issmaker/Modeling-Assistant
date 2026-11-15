"""Closed S/V frame shell: manifold/winding/source-contact invariants."""
import copy
import math
import unittest
from collections import Counter
from issmaker_modeling_assistant.core import profile_join_solver,svd_volume_core,surface_graph

RECT=[(0.,0.,0.),(2.,0.,0.),(2.,1.5,0.),(0.,1.5,0.)]


def envelope():
    front=profile_join_solver.search(RECT,.25)
    back=[(x,y,z-.1) for x,y,z in front['vertices']]
    return front,back


class SVDVolumeCoreTests(unittest.TestCase):
    def test_solid_shell_has_zero_boundary_edges(self):
        front,back=envelope()
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        self.assertEqual(r['topology_status'],'CLOSED_ORIENTED_SHELL')
        self.assertEqual(r['open_boundary_edges'],0)
        self.assertEqual(r['outer_wall_contact_face_count'],4)
        self.assertEqual(r['inner_glazing_reveal_face_count'],4)
        self.assertEqual(r['front_face_count'],4)
        self.assertEqual(r['back_face_count'],4)
        self.assertEqual(len(r['faces']),16)
        self.assertEqual(len(r['vertices']),16)

    def test_closed_shell_is_torus_boundary_genus_one(self):
        front,back=envelope()
        m=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        g=surface_graph.analyze(m['vertices'],m['faces'])
        self.assertEqual(g['boundary_count'],0)
        self.assertEqual(g['component_count'],1)
        self.assertEqual(g['components'][0]['genus'],1)
        self.assertEqual(g['components'][0]['status'],'VALID_SURFACE_GRAPH')
        self.assertFalse(g['winding_conflict_edges'])

    def test_first_half_is_preserved_front_vertices(self):
        front,back=envelope()
        old=copy.deepcopy((front,back))
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        self.assertEqual(r['vertices'][:8],front['vertices'])
        self.assertEqual(r['vertices'][8:],back)
        self.assertEqual((front,back),old)
        self.assertFalse(r['source_geometry_modified'])
        self.assertFalse(r['production_geometry_replaced'])

    def test_side_roles_intentional_contact(self):
        front,back=envelope()
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        role_count=Counter(r['roles'])
        self.assertEqual(role_count['OUTER_WALL_CONTACT'],4)
        self.assertEqual(role_count['INNER_GLAZING_REVEAL'],4)
        self.assertEqual(role_count['FRONT_PROFILE_BAND'],4)
        self.assertEqual(role_count['BACK_PROFILE_BAND'],4)

    def test_quad_warp_is_split_not_falsely_accepted_as_planar(self):
        front,back=envelope()
        back[0]=(back[0][0]+.03,back[0][1],back[0][2])
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        self.assertGreater(r['triangulated_nonplanar_quads'],0)
        graph=surface_graph.analyze(r['vertices'],r['faces'])
        self.assertEqual(graph['boundary_count'],0)
        self.assertEqual(graph['components'][0]['genus'],1)

    def test_all_edges_have_two_opposite_directed_incidents(self):
        front,back=envelope()
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        counts=Counter((a,b) for face in r['faces']
               for a,b in zip(face,face[1:]+face[:1]))
        for a,b in counts:
            self.assertEqual(counts[(a,b)],1)
            self.assertEqual(counts[(b,a)],1)

    def test_no_missing_back_vertices(self):
        front,back=envelope()
        r=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        used=set(v for face in r['faces'] for v in face)
        self.assertEqual(used,set(range(len(r['vertices']))))

    def test_invalid_mismatched_back_vertices(self):
        front,back=envelope()
        with self.assertRaisesRegex(ValueError,'VERTEX_OR_FACE_BUDGET'):
            svd_volume_core.stage(front['vertices'],back[:-1],front['faces'],front['roles'],4)

    def test_invalid_source_contact_count(self):
        front,back=envelope()
        with self.assertRaisesRegex(ValueError,'INVALID_CONTACT_COUNT'):
            svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],999)

    def test_invalid_roles_count(self):
        front,back=envelope()
        with self.assertRaisesRegex(ValueError,'ROLE_OR_FACE_COUNT'):
            svd_volume_core.stage(front['vertices'],back,front['faces'],[],4)

    def test_invalid_duplicate_front_face(self):
        front,back=envelope()
        with self.assertRaisesRegex(ValueError,'DUPLICATE_FRONT_FACE'):
            svd_volume_core.stage(front['vertices'],back,front['faces']+[front['faces'][0]],
                              front['roles']+['PROFILE_BAND'],4)

    def test_nonfinite_vertex_rejected(self):
        front,back=envelope();back[3]=(float('nan'),0.,-.1)
        with self.assertRaisesRegex(ValueError,'NONFINITE_VERTEX'):
            svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)

    def test_invalid_face_index(self):
        front,back=envelope();faces=list(front['faces']);faces[0]=[0,1,99,4]
        with self.assertRaisesRegex(ValueError,'INVALID_FRONT_FACE'):
            svd_volume_core.stage(front['vertices'],back,faces,front['roles'],4)

    def test_reproducible(self):
        front,back=envelope()
        a=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        b=svd_volume_core.stage(front['vertices'],back,front['faces'],front['roles'],4)
        self.assertEqual(a,b)

if __name__=='__main__':unittest.main()
