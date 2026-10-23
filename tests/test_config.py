import json
import tempfile
import unittest
from pathlib import Path


class ConfigTests(unittest.TestCase):
    def test_ui_theme_and_input_height_are_customizable_and_validated(self):
        from david.config import load_settings, ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            p.write_text(json.dumps({'ui': {'theme': 'mono', 'input_height': 7}}))
            self.assertEqual(load_settings(p)['ui'], {'theme': 'mono', 'input_height': 7})
            for ui in ({'theme': 'untrusted markup'}, {'input_height': True}, {'input_height': 0}):
                p.write_text(json.dumps({'ui': ui}))
                with self.assertRaises(ConfigError):
                    load_settings(p)

    def test_reject_invalid_or_unsafe_settings(self):
        from david.config import load_settings, ConfigError
        cases = [[], {'typo': {}}, {'security': {'enable_commands': 'false'}},
                 {'model': {'timeout_seconds': -1}}, {'model': {'name': ''}},
                 {'model': {'api_key': 'do-not-store-secrets'}},
                 {'agent': {'tool_mode': 'automatic'}},
                 {'commands': {'test': {'argv': 'python test.py'}}},
                 {'commands': {'test': {'argv': ['python'], 'shell': True}}},
                 {'security': {'denied_names': ['']}},
                 {'model': {'temperature': float('nan')}},
                 {'security': {'max_output_chars': True}}]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            for value in cases:
                with self.subTest(value=value):
                    p.write_text(json.dumps(value))
                    with self.assertRaises(ConfigError):
                        load_settings(p)

    def test_duplicate_keys_are_rejected(self):
        from david.config import load_settings, ConfigError
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            p.write_text('{"security":{"read_only":true,"read_only":false}}')
            with self.assertRaises(ConfigError):
                load_settings(p)

    def test_load_settings_and_defaults_without_mutating_defaults(self):
        from david.config import load_settings
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            p.write_text(json.dumps({'model': {'name': 'internal-model', 'base_url': 'https://inference.example/v1'}}))
            settings = load_settings(p)
            self.assertEqual(settings['model']['name'], 'internal-model')
            self.assertTrue(settings['model']['stream'])
            self.assertFalse(settings['security']['enable_commands'])
            settings['security']['denied_names'].clear()
            self.assertIn('.env', load_settings(p)['security']['denied_names'])
