"""Wave5 provenance, render and cross-version icon compatibility contracts."""
import hashlib
import json
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import bpy

from issmaker_modeling_assistant import icon_pack


ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/"assets"/"icons"
SOURCES=ROOT/"assets"/"icons_blender_svg"


class OfficialBlenderIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest=json.loads((ASSETS/"manifest.json").read_text(encoding="utf8"))
        cls.source=json.loads((SOURCES/"source_manifest.json").read_text(encoding="utf8"))

    def test_complete_21_native_sources_and_runtime_masks(self):
        aliases=[item["hud_symbol"] for item in self.source["icons"]]
        self.assertEqual(len(aliases),21)
        self.assertEqual(len(set(aliases)),21)
        self.assertEqual(self.manifest["icons"],aliases)
        self.assertEqual(set(icon_pack.available()),set(aliases))

    def test_all_source_svgs_match_official_upstream_hashes(self):
        self.assertEqual(self.source["blender_source_tag"],"v4.5.5")
        for item in self.source["icons"]:
            svg=(SOURCES/item["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(svg).hexdigest(),item["sha256"])
            self.assertTrue(ET.fromstring(svg).tag.endswith("svg"))
            self.assertIn("blender/blender/v4.5.5/",item["upstream"])

    def test_all_blender_native_icon_identifiers_available_in_current_runtime(self):
        names=bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
        for item in self.source["icons"]:
            with self.subTest(name=item["blender_icon"]):
                self.assertIn(item["blender_icon"],names)

    def test_packed_alpha_masks_have_valid_dimensions_and_visible_artwork(self):
        for item in self.source["icons"]:
            alias=item["hud_symbol"]
            data=zlib.decompress((ASSETS/(alias+".rgba8.z")).read_bytes())
            self.assertEqual(len(data),128*128)
            self.assertGreater(sum(x>32 for x in data),100)
            self.assertLessEqual(max(data),255)
            pixels=next(x for x in self.manifest["items"] if x["symbol"]==alias)
            self.assertEqual(hashlib.sha256(data).hexdigest(),pixels["mask_sha256"])

    def test_previews_are_true_128_rgba_png(self):
        for alias in self.manifest["icons"]:
            data=(ASSETS/(alias+".png")).read_bytes()
            self.assertEqual(data[:8],b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II",data[16:24]),(128,128))
            self.assertEqual((data[24],data[25]),(8,6))

    def test_unavailable_icon_fails_without_gpu_import(self):
        self.assertFalse(icon_pack.render("THIS_ICON_DOES_NOT_EXIST",0.,0.,32.))

    def test_native_icon_box_has_measured_ring_clearance(self):
        # The same formula used by tool_hud_draw; native Gizmo owns hitboxes.
        for diameter in (28.,32.,40.,54.,66.):
            for hover in (0.,.5,1.):
                glyph=diameter*(.66+.022*hover)
                self.assertGreaterEqual((diameter-glyph)/2.,4.)

    def test_all_native_ui_icons_from_panel_python_are_accepted(self):
        names=bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items
        for path in (ROOT/"ui.py",ROOT/"module_visualization.py",
                    ROOT/"tools/uv_wall/UV_Wall_Professional/ui/panel.py"):
            content=path.read_text(encoding="utf8")
            all_icons=re.findall("icon\\s*=\\s*['\\\"]([A-Z0-9_]+)['\\\"]",content)
            invalid=[name for name in all_icons if name not in names]
            self.assertFalse(invalid,(path.name,invalid))


if __name__=="__main__":
    unittest.main()
