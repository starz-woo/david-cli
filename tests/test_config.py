import json
import tempfile
import unittest
from pathlib import Path


class ConfigTests(unittest.TestCase):
    def test_explicit_context_window_is_separate_from_output_and_request_limits(self):
        from david.config import load_settings
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            p.write_text(json.dumps({'model': {'context_window_tokens': 1000000,
                'max_tokens': 4096, 'max_request_bytes': 16777216},
                'agent': {'context_safety_margin_tokens': 2048}}))
            settings = load_settings(p)
            self.assertEqual(settings['model']['context_window_tokens'], 1000000)
            self.assertEqual(settings['model']['max_tokens'], 4096)
            self.assertEqual(settings['model']['max_request_bytes'], 16777216)
            self.assertEqual(settings['agent']['context_safety_margin_tokens'], 2048)

    def test_context_limits_validate_types_bounds_and_combined_reservation(self):
        from david.config import load_settings, ConfigError
        cases = [
            {'model': {'context_window_tokens': value}}
            for value in (True, -1, 2000001, 1.5)
        ] + [
            {'model': {'max_request_bytes': value}}
            for value in (True, 1023, 67108865, 4096.5)
        ] + [
            {'agent': {'context_safety_margin_tokens': value}}
            for value in (True, -1, 2000001, 1.5)
        ] + [
            {'model': {'context_window_tokens': 3072}},
            {'model': {'context_window_tokens': 3071}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'setting.json'
            for value in cases:
                with self.subTest(value=value):
                    p.write_text(json.dumps(value))
                    with self.assertRaises(ConfigError):
                        load_settings(p)
            p.write_text('{}')
            self.assertEqual(load_settings(p)['model']['context_window_tokens'], 0)

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
