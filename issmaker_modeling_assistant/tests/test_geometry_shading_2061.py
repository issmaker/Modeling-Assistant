"""Geometry shading for arbitrary Blender meshes: smooth, flat, rollback."""
import math
import unittest
import bpy
from issmaker_modeling_assistant.geometry_shading import (
    apply_angle_smoothing,apply_flat_shading,
)


class GeneralGeometryShadingTests(unittest.TestCase):
    def setUp(self):
        self.original_mode=bpy.context.mode
        self.original_active=bpy.context.view_layer.objects.active
        self.original_selection=list(bpy.context.selected_objects)
        if bpy.context.mode!="OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        self.mesh=bpy.data.meshes.new("__ISSMAKER_SHADE_QA_MESH")
        self.mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],[],[(0,1,2,3)])
        self.mesh.uv_layers.new(name="UVMap")
        self.ob=bpy.data.objects.new("__ISSMAKER_SHADE_QA",self.mesh)
        bpy.context.scene.collection.objects.link(self.ob)
        bpy.ops.object.select_all(action="DESELECT")
        self.ob.select_set(True)
        bpy.context.view_layer.objects.active=self.ob
        self.uv=[tuple(q.uv) for q in self.mesh.uv_layers.active.data]
        self.vertex=[tuple(v.co) for v in self.mesh.vertices]

    def tearDown(self):
        if bpy.context.mode!="OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        bpy.data.objects.remove(self.ob,do_unlink=True)
        if self.mesh.users==0:bpy.data.meshes.remove(self.mesh)
        for ob in self.original_selection:
            if ob.name in bpy.data.objects:ob.select_set(True)
        if self.original_active and self.original_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active=self.original_active

    def test_smooth_then_flat_preserves_authored_uv_and_vertices(self):
        apply_angle_smoothing(bpy.context,self.ob,math.radians(47.5))
        self.assertTrue(all(p.use_smooth for p in self.mesh.polygons))
        apply_flat_shading(bpy.context,self.ob)
        self.assertTrue(all(not p.use_smooth for p in self.mesh.polygons))
        self.assertEqual(self.uv,[tuple(q.uv) for q in self.mesh.uv_layers.active.data])
        self.assertEqual(self.vertex,[tuple(v.co) for v in self.mesh.vertices])

    def test_flat_refuses_multi_user_shared_mesh(self):
        clone=bpy.data.objects.new("__ISSMAKER_SHADE_QA_LINKED",self.mesh)
        bpy.context.scene.collection.objects.link(clone)
        try:
            with self.assertRaises(ValueError):
                apply_flat_shading(bpy.context,self.ob)
        finally:
            bpy.data.objects.remove(clone,do_unlink=True)
        self.assertEqual(self.uv,[tuple(q.uv) for q in self.mesh.uv_layers.active.data])


if __name__=="__main__":
    unittest.main()
