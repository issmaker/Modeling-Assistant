"""Audit shared theme and responsive layout metrics without changing Blender."""
import unittest

from issmaker_modeling_assistant.core import theme_engine, viewport_skin, ui_layout_scaling
from issmaker_modeling_assistant.core.hud_readability import contrast_ratio


class ThemeConsistencyTests(unittest.TestCase):
    def test_every_scheme_and_user_accent_is_readable(self):
        for scheme in theme_engine.SCHEMES:
            for key in theme_engine.ACCENTS:
                with self.subTest(scheme=scheme,accent=key):
                    theme=theme_engine.from_values({'ui_scheme':scheme,'accent':key})
                    palette=viewport_skin.shell(theme)
                    self.assertGreaterEqual(
                        contrast_ratio(palette['title'],palette['panel']),6.9)
                    for state in ('normal','active','hover','open'):
                        visual=viewport_skin.component('button',state,theme=theme)
                        ink=viewport_skin.control_ink(theme,state=state)
                        self.assertGreaterEqual(
                            contrast_ratio(ink,visual.background),4.4)

    def test_change_accent_also_changes_wall_background_and_border(self):
        mint=theme_engine.from_values({'accent':'MINT'})
        purple=theme_engine.from_values({'accent':'PURPLE'})
        self.assertNotEqual(viewport_skin.shell(mint)['border'],
                            viewport_skin.shell(purple)['border'])
        self.assertNotEqual(viewport_skin.component(theme=mint).background,
                            viewport_skin.component(theme=purple).background)

    def test_custom_accent_is_not_overwritten_by_theme_preset(self):
        chosen=(.94,.24,.73)
        for preset in theme_engine.SCHEMES:
            t=theme_engine.from_values({'ui_scheme':preset,
               'ui_custom_accent':True,'ui_accent_color':chosen})
            self.assertEqual(t.accent,chosen)
            self.assertNotEqual(viewport_skin.shell(t)['border'],chosen)

    def test_hd_qhd_ui_floor_does_not_multiply_blender_scale_twice(self):
        f=ui_layout_scaling.auto_display_floor
        hd=f((1920,1080),1)
        qhd=f((2560,1440),1)
        self.assertEqual(hd,1)
        self.assertGreater(qhd,hd)
        self.assertLessEqual(qhd,1.36)
        self.assertEqual(f((2560,1440),2),1)
        self.assertEqual(f((1366,768),1),1)

    def test_real_layout_peer_regions_do_not_overlap(self):
        outer=(0,0,520,540)
        rects=[
            ('resources',(8,8,103,42)),
            ('checks',(110,8,205,42)),
            ('build',(212,8,307,42)),
            ('history',(314,8,409,42)),
            ('preview',(416,8,511,42)),
        ]
        self.assertEqual(ui_layout_scaling.inspect_layout(outer,rects,margin=4),[])


if __name__=='__main__':
    unittest.main()
