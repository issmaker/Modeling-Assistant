"""Quick Favorites: persistent state, native hitboxes, radial heart geometry."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bpy
from issmaker_modeling_assistant.core import quick_favorites
from issmaker_modeling_assistant import radial_geometry,tool_hud_draw


class QuickFavoritesTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='issmaker_favorites_')
        self.path=Path(self.folder.name)/'quick_favorites_v1.json'
        self.saved=quick_favorites._config_path
        quick_favorites._config_path=lambda:self.path
        quick_favorites.reset_cache()

    def tearDown(self):
        quick_favorites.reset_cache()
        quick_favorites._config_path=self.saved
        self.folder.cleanup()

    def test_default_is_nonintrusive_and_empty(self):
        self.assertEqual(quick_favorites.favorites(),())
        self.assertIsNone(quick_favorites.last_shading())
        self.assertFalse(self.path.exists())

    def test_pinning_multiple_is_persistent_and_ordered(self):
        for key in ("COLLISION","SHADING_LAST","UV_WALL"):
            self.assertTrue(quick_favorites.toggle(key))
        self.assertEqual(quick_favorites.favorites(),
                         ("COLLISION","SHADING_LAST","UV_WALL"))
        quick_favorites.reset_cache()
        self.assertEqual(quick_favorites.favorites(),
                         ("COLLISION","SHADING_LAST","UV_WALL"))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))["schema"],
                         quick_favorites.SCHEMA)
        self.assertFalse(quick_favorites.toggle("COLLISION"))
        self.assertNotIn("COLLISION",quick_favorites.favorites())

    def test_corrupted_saved_file_is_backed_up_before_reset(self):
        self.path.write_text("{broken JSON",encoding="utf-8")
        self.assertEqual(quick_favorites.favorites(),())
        self.assertTrue(quick_favorites.toggle("COLLISION"))
        backups=list(self.path.parent.glob("quick_favorites_v1.invalid_*.json"))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"),"{broken JSON")
        self.assertEqual(quick_favorites.favorites(),("COLLISION",))

    def test_unknown_ids_fail_without_writing(self):
        with self.assertRaises(ValueError):
            quick_favorites.toggle("DESTRUCTIVE_DELETE_ALL")
        self.assertFalse(self.path.exists())
        self.assertRaises(ValueError,quick_favorites.validate,
                          {"schema":quick_favorites.SCHEMA,
                           "favorites":["COLLISION","COLLISION"],"last_shading":None})

    def test_shading_is_last_successfully_recorded_mode(self):
        quick_favorites.toggle("SHADING_LAST")
        quick_favorites.record_shading("SMOOTH",44.5)
        self.assertEqual(quick_favorites.last_shading()["angle_degrees"],44.5)
        self.assertIn("44.5",quick_favorites.tooltip("SHADING_LAST"))
        self.assertEqual(quick_favorites.icon("SHADING_LAST"),"FAV_SHADE")
        quick_favorites.record_shading("FLAT")
        quick_favorites.reset_cache()
        self.assertEqual(quick_favorites.last_shading()["mode"],"FLAT")
        self.assertEqual(quick_favorites.icon("SHADING_LAST"),"FAV_FLAT")
        with self.assertRaises(ValueError):
            quick_favorites.record_shading("SMOOTH",float("nan"))

    def test_heart_hitboxes_are_outside_normal_wedges(self):
        for scale in (.6,.8,1.,1.2,1.3):
            for i in range(len(radial_geometry.TOOL_MODES)):
                xy=radial_geometry.favorite_point(i,scale)
                self.assertEqual(radial_geometry.favorite_pick(*xy,scale),i)
                self.assertEqual(radial_geometry.favorite_pick(
                    xy[0]+8*scale,xy[1]+1*scale,scale),i)
                self.assertEqual(radial_geometry.polar_pick(*xy,scale,sectors=len(radial_geometry.TOOL_MODES)),-1)
            self.assertEqual(radial_geometry.favorite_pick(0,0,scale),-1)

    def test_no_heart_overlaps_geometry_or_appearance_chips(self):
        for scale in (.6,1.,1.3):
            for i in range(len(radial_geometry.TOOL_MODES)):
                x,y=radial_geometry.favorite_point(i,scale)
                self.assertFalse(radial_geometry.geometry_pick(x,y,scale))
                self.assertFalse(radial_geometry.appearance_pick(x,y,scale))

    def test_two_sector_favorites_never_overlap_chips_or_wedges(self):
        from math import pi,cos,sin
        self.assertEqual(len(radial_geometry.TOOL_MODES),2)
        for scale in (.6,1.,1.3):
            for index in range(2):
                x,y=radial_geometry.favorite_point(index,scale)
                # Visible outline radius exceeds the circular hitbox, so sample it too.
                for step in range(32):
                    angle=2*pi*step/32
                    cx=x+13.5*scale*cos(angle)
                    cy=y+13.5*scale*sin(angle)
                    self.assertFalse(radial_geometry.appearance_pick(cx,cy,scale))
                    self.assertFalse(radial_geometry.geometry_pick(cx,cy,scale))
                    self.assertEqual(radial_geometry.polar_pick(
                        cx,cy,scale,sectors=2),-1)

    def test_favorites_dock_fit_does_not_overlap_tools(self):
        anchor=(1050.,42.)
        for height in (260,350,720,1080):
            items=quick_favorites.quick_positions(anchor,5,height,1.)
            self.assertLessEqual(len(items),5)
            self.assertTrue(all(y>anchor[1]+22. for x,y in items))
            self.assertTrue(all(y+16.+10.<=height for x,y in items))
            self.assertEqual(len(set(items)),len(items))
            self.assertEqual(tuple(x for x,y in items),tuple(anchor[0] for _ in items))

    def test_unique_vector_icons_exist(self):
        for id in quick_favorites.ALLOWED:
            self.assertIn(quick_favorites.icon(id),tool_hud_draw._STROKES)
            self.assertGreater(len(tool_hud_draw._STROKES[quick_favorites.icon(id)]),3)
        self.assertIn("FAV_FLAT",tool_hud_draw._STROKES)

    def test_native_blender_operators_are_registered(self):
        self.assertIsNotNone(bpy.ops.ima.favorite_launch.get_rna_type())
        self.assertIsNotNone(bpy.ops.ima.favorite_repeat_shading.get_rna_type())
        self.assertIsNotNone(bpy.ops.ima.view_mode_pie.get_rna_type())


if __name__=='__main__':
    unittest.main()
