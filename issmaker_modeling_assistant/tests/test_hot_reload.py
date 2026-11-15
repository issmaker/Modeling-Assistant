"""No-restart reload regression tests; run under Blender background."""
import pathlib
import tempfile
import unittest
from unittest import mock

from issmaker_modeling_assistant.core import hot_reload


class HotReloadTest(unittest.TestCase):
    def test_loaded_package_syntax_preflight(self):
        result=hot_reload.preflight()
        self.assertGreater(result['modules_checked'],100)
        self.assertEqual(len(result['version']),3)

    def test_failed_syntax_fails_before_unregister(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)
            (root/'__init__.py').write_text("bl_info={'version':(2,0,46)}\n",encoding='utf-8')
            (root/'a.py').write_text('x = 1\n',encoding='utf-8')
            (root/'b.py').write_text('def failed(:\n pass',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'UPDATE_PREFLIGHT_FAILED'):
                hot_reload.preflight(root)

    def test_missing_package_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)
            (root/'__init__.py').write_text("bl_info={'version':(2,0,46)}\n",encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'UPDATE_PREFLIGHT_PACKAGE_INCOMPLETE'):
                hot_reload.preflight(root)

    def test_bad_version_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary)
            (root/'__init__.py').write_text("bl_info={'version':('bad',)}\n",encoding='utf-8')
            for name in ('a','b','c'):(root/(name+'.py')).write_text('pass\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'UPDATE_PREFLIGHT_VERSION_INVALID'):
                hot_reload.preflight(root)

    def test_passive_overlay_not_modal(self):
        busy=hot_reload.active_sessions()
        self.assertNotIn('COLLISION',busy)

    def test_request_rejects_busy_modal(self):
        with mock.patch.object(hot_reload,'active_sessions',return_value=['UV_WALL']):
            with self.assertRaisesRegex(RuntimeError,'UPDATE_MODAL_SESSION_ACTIVE'):
                hot_reload.request_reload()

    def test_status_schema(self):
        current=hot_reload.status()
        self.assertIn('state',current)
        self.assertIn('message',current)

    def test_missing_module_path_detection(self):
        with mock.patch.object(hot_reload,'installed_root',side_effect=RuntimeError('MISSING')):
            with self.assertRaisesRegex(RuntimeError,'MISSING'):
                hot_reload.preflight()


if __name__=='__main__':
    unittest.main()
