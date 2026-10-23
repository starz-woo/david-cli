"""Explicit client budgets, not assertions about any serving model's capacity."""
import json
import tempfile
import unittest
from pathlib import Path

from david.config import DEFAULTS, ConfigError, load_settings


class ModelProfileTests(unittest.TestCase):
    def load(self, raw):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'settings.json'
            path.write_text(json.dumps(raw), encoding='utf-8')
            return load_settings(path)

    def test_explicit_profile_precedes_defaults_but_not_explicit_model_settings(self):
        raw = {'model_profiles': {'my-serving-deployment': {
            'context_window_tokens': 1000000, 'max_tokens': 16000,
            'context_safety_margin_tokens': 2048}},
            'model': {'name': 'arbitrary-model', 'profile': 'my-serving-deployment', 'max_tokens': 2048}}
        settings = self.load(raw)
        self.assertEqual(settings['model']['context_window_tokens'], 1000000)
        self.assertEqual(settings['model']['max_tokens'], 2048)
        self.assertEqual(settings['agent']['context_safety_margin_tokens'], 2048)
        raw['model']['context_safety_margin_tokens'] = 4096
        self.assertEqual(self.load(raw)['agent']['context_safety_margin_tokens'], 4096)
        raw['model'].pop('profile')
        self.assertEqual(self.load(raw)['model']['context_window_tokens'], DEFAULTS['model']['context_window_tokens'])

    def test_new_defaults_use_token_budget_and_preserve_explicit_legacy_configuration(self):
        fresh = self.load({})
        self.assertGreater(fresh['model']['context_window_tokens'], fresh['model']['max_tokens'])
        self.assertGreater(fresh['model']['max_tokens'], 2048)
        old = self.load({'model': {'max_tokens': 2048, 'context_window_tokens': 0}})
        self.assertEqual(old['model']['max_tokens'], 2048)
        self.assertEqual(old['model']['context_window_tokens'], 0)

    def test_invalid_profiles_and_impossible_window_fail_without_clamping(self):
        invalid = [
            {'model': {'profile': 'missing'}},
            {'model_profiles': {'bad': {'base_url': 'http://other'}}},
            {'model_profiles': {'unused': {'max_tokens': True}}},
            {'model_profiles': {'unused': {'context_window_tokens': 3000000}}},
            {'model_profiles': {'bad': []}},
            {'model': {'context_window_tokens': 4096}},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                self.load(raw)
