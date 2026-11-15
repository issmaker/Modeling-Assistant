"""Wave8: Studio Dock fully removed; Floating Pro is the only WALL UX.

Behavior and migration regression across Blender 4.5/5.2. Tests never
create production geometry, import deleted modules or change global prefs.
"""
import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from issmaker_modeling_assistant.core import appearance_store
from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.ui import preferences,viewport_actions

PACKAGE=Path(__file__).resolve().parents[1]
WALL=PACKAGE/"tools/uv_wall/UV_Wall_Professional"


class FloatingOnlyTests(unittest.TestCase):
    def test_only_floating_mode_can_be_resolved(self):
        self.assertEqual(preferences.active_ui_mode(),"FLOATING_PRO")
        context=SimpleNamespace(preferences=SimpleNamespace(
          addons={"issmaker_modeling_assistant":SimpleNamespace(
            preferences=SimpleNamespace(ui_mode="STUDIO_DOCK"))}))
        self.assertEqual(preferences.active_ui_mode(context),"FLOATING_PRO")

    def test_root_and_nested_preferences_no_longer_expose_mode_toggle(self):
        from issmaker_modeling_assistant import ui
        self.assertNotIn("ui_mode",ui.IMA_AddonPreferences.__annotations__)
        self.assertNotIn("ui_mode",preferences.UVWALL_AddonPreferences.__annotations__)
        self.assertTrue(hasattr(preferences.UVWALL_AddonPreferences,"draw"))
        for key in ("dock_sizing","dock_manual_width","dock_manual_height",
                    "dock_presets_json","dock_sections_json"):
            self.assertNotIn(key,ui.IMA_AddonPreferences.__annotations__)

    def test_old_appearance_profile_loaded_without_legacy_mode(self):
        old={"schema":appearance_store.SCHEMA,"values":{
             "ui_mode":"STUDIO_DOCK","dock_sizing":"MANUAL",
             "dock_manual_width":330,"dock_manual_height":720,
             "dock_presets_json":'{"WALL":{"x":{}}}',
             "ui_scheme":"SAND","ui_pattern":"NONE",
             "ui_motion_quality":"OFF","ui_pattern_strength":.6}}
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/appearance_store.FILE_NAME
            target.write_text(json.dumps(old),encoding="utf8")
            profile,source=appearance_store.read(folder)
        self.assertEqual(source,"primary")
        self.assertEqual(profile["ui_scheme"],"SAND")
        self.assertEqual(profile["ui_motion_quality"],"OFF")
        self.assertEqual(profile["ui_pattern_strength"],.6)
        self.assertNotIn("ui_mode",profile)
        self.assertNotIn("dock_sizing",profile)
        self.assertNotIn("dock_presets_json",profile)

    def test_removed_mode_cannot_be_written_back_to_profile(self):
        clean=appearance_store._validate({
             "ui_mode":"STUDIO_DOCK","dock_manual_width":9999,
             "ui_pattern":"NONE","ui_pattern_opacity":.5})
        self.assertNotIn("ui_mode",clean)
        self.assertNotIn("dock_manual_width",clean)
        self.assertEqual(clean["ui_pattern_opacity"],.5)

    def test_legacy_profile_restore_does_not_reintroduce_mode(self):
        prefs=SimpleNamespace()
        values=appearance_store.restore_into_preferences(prefs,{
            "ui_mode":"STUDIO_DOCK","dock_sizing":"MANUAL",
            "ui_pattern":"NONE","ui_pattern_opacity":.35})
        self.assertNotIn("ui_mode",values)
        self.assertFalse(hasattr(prefs,"ui_mode"))
        self.assertEqual(prefs.ui_pattern_opacity,.35)

    def test_legacy_theme_preserves_current_motion_instead_of_dock_setting(self):
        prefs=SimpleNamespace(ui_theme="EMBER",dock_animation="OFF",
                              ui_motion_quality="RICH")
        result=appearance_store.migrate_legacy(prefs)
        self.assertEqual(result["ui_motion_quality"],"RICH")

    def test_all_studio_dock_module_files_removed(self):
        for name in ("ui/studio_dock.py","ui/dock_workspace_state.py",
                     "viewport/studio_dock_layout.py"):
            self.assertFalse((WALL/name).exists(),name)

    def test_shared_palette_preserved_and_dock_ops_removed(self):
        self.assertEqual(viewport_actions.UVWALL_OT_hover_palette.bl_idname,
                         "uvwall.hover_palette")
        self.assertEqual(viewport_actions.UVWALL_OT_inner_height_point.bl_idname,
                         "uvwall.inner_height_point")
        module=(WALL/"__init__.py").read_text(encoding="utf8")
        for forbidden in ("UVWALL_OT_dock_field","UVWALL_OT_dock_switch",
                          "UVWALL_OT_dock_presets","UVWALL_OT_studio_build"):
            self.assertNotIn(forbidden,module)

    def test_controller_has_no_dock_modal_routes(self):
        source=(WALL/"viewport/controller.py").read_text(encoding="utf8")
        functions={node.name for node in ast.walk(ast.parse(source))
                   if isinstance(node,ast.FunctionDef)}
        self.assertFalse(functions & {"_draw_studio_dock",
             "_dock_field_row","_handle_studio_dock_event",
             "_execute_studio_dock_button"})
        self.assertNotIn("STUDIO_DOCK",source)
        self.assertIn("def _draw_wall_return_only",source)

    def test_signed_wall_manifest_covers_remaining_files(self):
        data=json.loads((WALL/"release_integrity_200.json").read_text(encoding="utf8"))
        self.assertEqual(len(data["sha256"]),109)
        for rel,digest in data["sha256"].items():
            with self.subTest(file=rel):
                p=WALL/rel
                self.assertTrue(p.is_file())
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)

    def test_runtime_manifest_has_no_deleted_modules(self):
        m=json.loads((WALL/"release_manifest_200.json").read_text(encoding="utf8"))
        self.assertEqual(len(m["sha256"]),61)
        self.assertIn("UV_Wall_Professional.ui.viewport_actions",m["sha256"])
        self.assertFalse(any("studio_dock" in x or "dock_workspace_state" in x
                             for x in m["sha256"]))


if __name__=="__main__":
    unittest.main()
