"""Stateful regression for the contextual master switch and nested cap editor.

This test is a concrete implementation using the project-neutral principle:
compare a separately defined expected state machine against actual controller
behavior after every operation. Always run against disposable Blender data.
"""
import unittest
import bpy

from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.viewport.cap_state import (
    cap_master_enabled, toggled_master_caps,
)
from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.viewport.controller import (
    UVWALL_OT_viewport_inspector,
)


class _Engine:
    def invalidate(self, *args): pass
    def update(self): pass


class _ControllerHarness:
    def __init__(self,path):
        self.path=path
        self._engine=_Engine()
        self.commits=[]
    def _definition(self):
        return self.path.uvwall_definition
    def _path(self):
        return self.path
    def _commit_or_restore(self, context, snapshot, quiet=True):
        self.commits.append((
            bool(self._definition().create_top_cap),
            bool(self._definition().create_bottom_cap),
        ))
        return True


class MasterCapStatefulTests(unittest.TestCase):
    def setUp(self):
        mesh=bpy.data.meshes.new("AUDIT_CAP_PATH_MESH")
        mesh.from_pydata([(0,0,0),(2,0,0)],[(0,1)],[])
        mesh.update()
        self.path=bpy.data.objects.new("AUDIT_CAP_PATH",mesh)
        bpy.context.scene.collection.objects.link(self.path)
        definition=self.path.uvwall_definition
        definition.generator_type="WALL"
        definition.create_inner=True
        self.controller=_ControllerHarness(self.path)
        self.mesh=mesh

    def tearDown(self):
        bpy.data.objects.remove(self.path,do_unlink=True)
        bpy.data.meshes.remove(self.mesh)

    def click(self):
        return UVWALL_OT_viewport_inspector._apply_context_button(
            self.controller,"W_TOP",bpy.context)

    def test_known_bug_top_on_then_bottom_on_then_master_off(self):
        d=self.path.uvwall_definition
        d.create_top_cap=True
        d.create_bottom_cap=False
        # Simulates the independent detail editor's committed lower cap.
        d.create_bottom_cap=True
        self.assertTrue(cap_master_enabled(d.create_top_cap,d.create_bottom_cap))
        self.assertTrue(self.click())
        self.assertEqual((d.create_top_cap,d.create_bottom_cap),(False,False))
        self.assertEqual(self.controller.commits[-1],(False,False))

    def test_bottom_only_then_master_off(self):
        d=self.path.uvwall_definition
        d.create_top_cap=False;d.create_bottom_cap=True
        self.assertTrue(self.click())
        self.assertEqual((d.create_top_cap,d.create_bottom_cap),(False,False))

    def test_off_then_master_on_enables_top_only(self):
        d=self.path.uvwall_definition
        d.create_top_cap=False;d.create_bottom_cap=False
        self.assertTrue(self.click())
        self.assertEqual((d.create_top_cap,d.create_bottom_cap),(True,False))
        self.assertTrue(self.click())
        self.assertEqual((d.create_top_cap,d.create_bottom_cap),(False,False))

    def test_exhaust_all_4_initial_states_and_double_click(self):
        d=self.path.uvwall_definition
        for top in (False,True):
            for bottom in (False,True):
                d.create_top_cap=top;d.create_bottom_cap=bottom
                expected=toggled_master_caps(top,bottom)
                self.assertTrue(self.click())
                self.assertEqual((bool(d.create_top_cap),bool(d.create_bottom_cap)),expected)
                other=toggled_master_caps(*expected)
                self.assertTrue(self.click())
                self.assertEqual((bool(d.create_top_cap),bool(d.create_bottom_cap)),other)

    def test_original_authored_path_unchanged(self):
        before=[tuple(v.co) for v in self.path.data.vertices]
        d=self.path.uvwall_definition
        d.create_top_cap=True;d.create_bottom_cap=True
        self.click()
        self.assertEqual([tuple(v.co) for v in self.path.data.vertices],before)


if __name__=="__main__":
    unittest.main()
