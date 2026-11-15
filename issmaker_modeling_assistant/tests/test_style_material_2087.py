"""Pure STYLE material and palette safety tests, Blender 4.5/5.2."""
import math
import unittest
from dataclasses import replace

from issmaker_modeling_assistant.core import (
    theme_engine,viewport_skin,workspace_components,
    ui_layout_scaling,studio_responsive_layout)
from issmaker_modeling_assistant.core.material_policy import (
    SURFACES,resolve,contrast_ratio)
from issmaker_modeling_assistant.appearance_gpu import Painter


class STYLEMaterialPolicyTests(unittest.TestCase):
    def theme(self,**values):
        return theme_engine.from_values({
            'ui_custom_accent':True,'ui_accent_color':(.58,.33,.83),
            'ui_secondary_color':(.24,.42,.67),
            'ui_pattern_strength':.9,'ui_pattern_opacity':.8,
            **values})

    def test_all_five_materials_have_distinct_modes(self):
        self.assertEqual(len(SURFACES),5)
        self.assertEqual(
            [resolve(self.theme(ui_surface=name.upper()),now=2).mode for name in SURFACES],
            list(range(5)))

    def test_time_changes_when_motion_is_enabled(self):
        t=self.theme(ui_motion_quality='RICH')
        self.assertNotEqual(resolve(t,now=1.25).phase,
                            resolve(t,now=2.52).phase)

    def test_reduced_motion_is_static(self):
        t=self.theme(ui_motion_quality='OFF')
        self.assertEqual(resolve(t,now=1).phase,0.)
        self.assertEqual(resolve(t,now=2000).phase,0.)
        t=self.theme(ui_reduced_motion=True)
        self.assertEqual(resolve(t,now=100).phase,0.)

    def test_performance_mode_has_stable_gpu_background(self):
        t=self.theme(ui_render_quality='PERFORMANCE',ui_motion_quality='RICH')
        self.assertEqual(resolve(t,now=10).phase,
                         resolve(t,now=87).phase)

    def test_no_background_preset_does_not_draw_any_material(self):
        t=self.theme(ui_pattern='NONE')
        self.assertIsNone(resolve(t,now=12.))
        for surface in SURFACES:
            self.assertIsNone(resolve(self.theme(ui_pattern='NONE',
                                                  ui_surface=surface.upper()),now=1))

    def test_zero_opacity_or_strength_is_free_skip(self):
        self.assertIsNone(resolve(self.theme(ui_pattern_strength=0),now=5))
        self.assertIsNone(resolve(self.theme(ui_pattern_opacity=0),now=5))

    def test_nonfinite_values_do_not_leak_gpu_uniforms(self):
        self.assertIsNone(resolve(self.theme(),now=float('nan')))
        self.assertIsNone(resolve(self.theme(),now=float('inf')))
        self.assertIsNone(resolve(replace(self.theme(),accent=(float('inf'),.2,.4)),now=3))

    def test_five_surfaces_preserve_one_palette_source_with_distinct_body(self):
        # Surface material properties intentionally change panel shading.
        # Common *accent and secondary preferences* must drive every tint;
        # requiring identical final pixels would erase the material presets.
        palette=[]
        for name in SURFACES:
            theme=self.theme(ui_surface=name.upper())
            sp=resolve(theme,now=3)
            self.assertTrue(0.<=sp.opacity<=.42)
            self.assertTrue(all(0<=v<=1 for v in sp.primary+sp.secondary))
            for i in range(3):
                self.assertAlmostEqual(sp.primary[i],
                    theme.panel[i]*.37+theme.accent[i]*.63,places=10)
                self.assertAlmostEqual(sp.secondary[i],
                    theme.panel[i]*.45+theme.secondary[i]*.55,places=10)
            palette.append((theme.accent,theme.secondary,theme.panel))
        self.assertEqual(len({x[:2] for x in palette}),1)
        self.assertGreater(len({x[2] for x in palette}),1)

    def test_user_custom_accent_controls_all_material_tints(self):
        for name in SURFACES:
            dark=resolve(self.theme(ui_surface=name.upper(),
                         ui_accent_color=(.03,.07,.12)),now=3)
            bright=resolve(self.theme(ui_surface=name.upper(),
                         ui_accent_color=(.8,.26,.12)),now=3)
            self.assertNotEqual(dark.primary,bright.primary)
            self.assertGreater(bright.primary[0],dark.primary[0])

    def test_user_secondary_controls_second_tint(self):
        a=resolve(self.theme(ui_secondary_color=(.09,.2,.3)))
        b=resolve(self.theme(ui_secondary_color=(.75,.1,.4)))
        self.assertNotEqual(a.secondary,b.secondary)

    def test_old_fixed_teal_base_no_longer_used(self):
        t=self.theme(ui_accent_color=(.88,.08,.18),
                     ui_secondary_color=(.2,.12,.93))
        primary,secondary=Painter._pattern_tint(t,False)
        expected_primary=tuple(t.panel[i]*.38+t.accent[i]*.62 for i in range(3))
        expected_secondary=tuple(t.panel[i]*.46+t.secondary[i]*.54 for i in range(3))
        self.assertEqual(primary,expected_primary)
        self.assertEqual(secondary,expected_secondary)

    def test_palette_matrix_legible_for_all_schemes_and_surfaces(self):
        for scheme in theme_engine.SCHEMES:
            for surface in theme_engine.SURFACES:
                for accent in tuple(theme_engine.ACCENTS)+('CUSTOM_BLACK','CUSTOM_WHITE'):
                    with self.subTest(scheme=scheme,surface=surface,accent=accent):
                        opt={'ui_scheme':scheme,'ui_surface':surface}
                        if accent=='CUSTOM_BLACK':
                            opt.update(ui_custom_accent=True,ui_accent_color=(0.,0.,0.))
                        elif accent=='CUSTOM_WHITE':
                            opt.update(ui_custom_accent=True,ui_accent_color=(1.,1.,1.))
                        else:
                            opt.update(accent=accent,ui_custom_accent=False)
                        t=theme_engine.from_values(opt)
                        ink=viewport_skin.ink(t)
                        self.assertGreaterEqual(
                            contrast_ratio(t.panel,ink[:3]),4.5)
                        self.assertTrue(all(.05<=c<=.40 for c in t.panel))
                        button=workspace_components.component_visual('button',theme=t)
                        self.assertTrue(button.interactive)
                        self.assertNotEqual(button.background[:3],t.panel)

    def test_opacity_bounded_and_preview_moderated(self):
        t=self.theme(ui_pattern_strength=1.,ui_pattern_opacity=1.)
        normal=resolve(t,preview=False)
        preview=resolve(t,preview=True)
        self.assertLessEqual(normal.opacity,.42)
        self.assertLessEqual(preview.opacity,.42)
        self.assertGreater(preview.opacity,normal.opacity)

    def test_quality_quantizes_phase_and_preserves_normalized_limits(self):
        for quality in ('ULTRA','HIGH','BALANCED','PERFORMANCE'):
            t=self.theme(ui_render_quality=quality)
            for now in (0.,.25,14.5,100000.):
                s=resolve(t,now=now)
                self.assertGreaterEqual(s.phase,0.)
                self.assertLess(s.phase,96.)

    def test_zero_speed_remains_static(self):
        t=self.theme(ui_pattern_speed=0.)
        self.assertEqual(resolve(t,now=1).phase,resolve(t,now=200).phase)

    def test_invalid_theme_does_not_throw(self):
        self.assertIsNone(resolve(None))
        self.assertIsNone(resolve(replace(self.theme(),panel=(1.,2.))))

class StudioResponsiveLayoutTests(unittest.TestCase):
    def test_hd_window_uses_readable_legacy_dimensions(self):
        self.assertEqual(studio_responsive_layout.dimensions(1574,954,1.),
                         (794.,680.))

    def test_qhd_window_expands_from_existing_safe_auto_scale(self):
        a=ui_layout_scaling.auto_display_floor((1920,1080),1.)
        b=ui_layout_scaling.auto_display_floor((2560,1361),1.)
        hd=studio_responsive_layout.dimensions(1574,954,a)
        qhd=studio_responsive_layout.dimensions(2100,1214,b)
        self.assertGreater(qhd[0],hd[0])
        self.assertGreater(qhd[1],hd[1])
        self.assertLessEqual(qhd[0],2100-32)
        self.assertLessEqual(qhd[1],1214-32)
        self.assertLessEqual(b,1.36)

    def test_narrow_viewport_never_exceeds_measured_bounds(self):
        for w,h in ((450,450),(600,650),(900,550),(1100,620)):
            bounds=studio_responsive_layout.dimensions(w,h,1.36)
            self.assertTrue(bounds is None or
                (400<=bounds[0]<=w-32 and 350<=bounds[1]<=h-32))

    def test_too_small_viewport_refuses_unreadable_layout(self):
        for w,h in ((350,800),(500,340),(0,0),(-600,1000)):
            self.assertIsNone(studio_responsive_layout.dimensions(w,h,1.))

    def test_responsive_scale_clamped_not_multiplied_by_blender_ui_again(self):
        self.assertEqual(studio_responsive_layout.text_scale(100.),1.36)
        self.assertEqual(studio_responsive_layout.text_scale(-1.),.75)
        self.assertEqual(studio_responsive_layout.text_scale(float('nan')),1.)
        self.assertAlmostEqual(studio_responsive_layout.text_scale(1.18),1.18)

    def test_nonfinite_scale_refuses_unsafe_geometry(self):
        self.assertIsNone(
            studio_responsive_layout.dimensions(1920,1080,float('nan')))

    def test_uhd_is_capped_by_region_and_valid_window_scale(self):
        bounds=studio_responsive_layout.dimensions(3800,2100,1.42)
        self.assertEqual(bounds,(794.*1.36,680.*1.36))
        self.assertLess(bounds[0],3800)

    def test_color_matrix_preserves_glyph_ink_contrast(self):
        for surface in theme_engine.SURFACES:
            for scheme in theme_engine.SCHEMES:
                theme=theme_engine.from_values(
                    {'ui_surface':surface,'ui_scheme':scheme})
                label=viewport_skin.ink(theme)[:3]
                self.assertGreaterEqual(
                    contrast_ratio(theme.panel,label),4.5)


if __name__=='__main__':
    unittest.main()
