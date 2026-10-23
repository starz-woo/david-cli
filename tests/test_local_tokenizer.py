"""Tiny local fixture exercises accounting, NOT real model/template compatibility."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from david.config import ConfigError, load_settings
from david.context import context_usage

HAS_TOKENIZER = all(importlib.util.find_spec(n) for n in ('tokenizers', 'jinja2'))
TEMPLATE = ("{{ bos_token }}{% for m in messages %}{{ m['role'] }}:{{ m['content'] }}\n{% endfor %}"
            "{{ tools | tojson }}{% if add_generation_prompt %}assistant:{% endif %}")


@unittest.skipUnless(HAS_TOKENIZER, 'install the tokenizer extra to exercise local fixture')
class LocalTokenizerTests(unittest.TestCase):
    def setUp(self):
        from tokenizers import Tokenizer, models, pre_tokenizers
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'user': 2,
            'assistant': 3, ':': 4, 'null': 5, '<BOS>': 6}, unk_token='[UNK]'))
        self.tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        self.tokenizer.save(str(self.root / 'tokenizer.json'))
        (self.root / 'tokenizer_config.json').write_text(json.dumps({
            'chat_template': TEMPLATE, 'bos_token': '<BOS>'}), encoding='utf-8')
        self.path = self.root / 'settings.json'
        self.raw = {'model': {'name': 'local-fixture', 'context_window_tokens': 1000000,
            'tokenizer_file': 'tokenizer.json', 'tokenizer_config_file': 'tokenizer_config.json'}}

    def load(self):
        self.path.write_text(json.dumps(self.raw), encoding='utf-8')
        return load_settings(self.path)

    def test_counts_actual_rendered_template_tools_and_generation_without_double_special_tokens(self):
        settings = self.load()
        messages = [{'role': 'user', 'content': 'hello'}]
        for tools in (None, [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]):
            rendered = '<BOS>user:hello\n' + json.dumps(tools, ensure_ascii=False) + 'assistant:'
            expected = len(self.tokenizer.encode(rendered, add_special_tokens=False).ids)
            usage = context_usage(settings, messages, tools)
            self.assertEqual(usage['local_input_tokens'], expected)
            self.assertEqual(usage['estimated_input_tokens'], expected)
            self.assertIn('local tokenizer', usage['method'])
            self.assertIn('server', usage['method'])
            self.assertNotEqual(expected, len(self.tokenizer.encode(json.dumps(messages)).ids))
        self.assertEqual(settings['model']['tokenizer_file'], str(self.root / 'tokenizer.json'))

    def test_assets_are_protected_and_session_fingerprint_tracks_budget_and_template(self):
        from david.controller import Controller
        from david.workspace import ToolError
        self.load()
        controller = Controller(self.path, self.root, session_dir=self.root / 'sessions')
        self.addCleanup(controller.close)
        for asset in ('tokenizer.json', 'tokenizer_config.json'):
            with self.subTest(asset=asset), self.assertRaises(ToolError):
                controller.agent.tools.execute('read_file', {'path': asset})
        original = controller._model_key()
        controller.agent.settings['model']['max_tokens'] += 1
        self.assertNotEqual(original, controller._model_key())
        original = controller._model_key()
        (self.root / 'tokenizer_config.json').write_text(json.dumps({
            'chat_template': TEMPLATE + 'changed', 'bos_token': '<BOS>'}))
        self.assertNotEqual(original, controller._model_key())

    def test_controller_reports_runtime_asset_failure_without_losing_error_event(self):
        from david.controller import Controller
        from test_agent import FakeClient
        self.load()
        events = []
        controller = Controller(self.path, self.root, session_dir=self.root / 'sessions', on_event=events.append)
        self.addCleanup(controller.close)
        client = FakeClient()
        controller.agent.client = client
        (self.root / 'tokenizer.json').unlink()
        controller._work('hello')
        self.assertEqual(client.requests, [])
        errors = [e for e in events if e.get('status') == 'error']
        self.assertTrue(errors)
        self.assertEqual(errors[-1]['context']['unit'], 'unavailable')
        self.assertIn('unavailable', controller._usage_text())

    def test_runtime_template_error_stops_before_request_with_no_estimate_fallback(self):
        from david.agent import Agent
        from test_agent import FakeClient, FakeTools
        (self.root / 'template.jinja').write_text(TEMPLATE + '{{ unknown_value }}')
        self.raw['model']['chat_template_file'] = 'template.jinja'
        settings = self.load()
        client = FakeClient()
        agent = Agent(client, FakeTools(), settings)
        with self.assertRaisesRegex(ConfigError, 'no estimate fallback'):
            agent.run('hello')
        self.assertEqual(client.requests, [])

    def test_unsafe_or_unsupported_templates_fail_closed(self):
        for template in (TEMPLATE + '{{ 100 ** 100 }}', TEMPLATE + '{{ messages.__class__ }}',
                         TEMPLATE + '{{ range(100) }}', TEMPLATE + '{{ "a" * 1000000 }}',
                         '{% for m in messages %}{% for t in tools %}x{% endfor %}{% endfor %}',
                         '{% include "secret" %}', '{{ messages }}'):
            with self.subTest(template=template):
                (self.root / 'template.jinja').write_text(template)
                self.raw['model']['chat_template_file'] = 'template.jinja'
                with self.assertRaisesRegex(ConfigError, 'Unsupported local chat template'):
                    self.load()

    def test_rendered_output_limit_and_missing_dependencies_fail_closed(self):
        from unittest.mock import patch
        settings = self.load()
        with patch('david.tokenizer.MAX_RENDER_BYTES', 4), self.assertRaisesRegex(ConfigError, 'rendered prompt'):
            context_usage(settings, [{'role': 'user', 'content': 'hello'}], None)
        import builtins
        original_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'tokenizers':
                raise ImportError('fixture missing optional dependency')
            return original_import(name, *args, **kwargs)
        from david.tokenizer import _cached_counter
        _cached_counter.cache_clear()
        with patch('builtins.__import__', side_effect=missing), self.assertRaisesRegex(ConfigError, 'dependencies missing'):
            self.load()

    def test_paths_require_regular_local_files_and_explicit_template(self):
        cases = ({'tokenizer_config_file': ''}, {'tokenizer_file': 'https://host/tokenizer.json'},
                 {'context_window_tokens': 0}, {'tokenizer_file': '.'})
        for overrides in cases:
            before = dict(self.raw['model'])
            self.raw['model'].update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ConfigError):
                self.load()
            self.raw['model'] = before
        link = self.root / 'linked.json'
        try:
            link.symlink_to(self.root / 'tokenizer.json')
        except OSError:
            return
        self.raw['model']['tokenizer_file'] = str(link)
        with self.assertRaisesRegex(ConfigError, 'symlinks'):
            self.load()

    def test_local_count_controls_real_agent_preflight_including_tools_and_output_reserve(self):
        from david.agent import Agent, AgentError
        from test_agent import FakeClient, FakeTools
        settings = self.load()
        client, tools = FakeClient({'role': 'assistant', 'content': 'ok'}), FakeTools()
        agent = Agent(client, tools, settings)
        prompt = 'hello ' * 20000
        messages = [{'role': 'system', 'content': agent.system}, {'role': 'user', 'content': prompt}]
        count = context_usage(settings, messages, tools.schemas())['local_input_tokens']
        settings['model']['context_window_tokens'] = count + settings['model']['max_tokens'] + settings['agent']['context_safety_margin_tokens']
        self.assertEqual(agent.run(prompt).text, 'ok')
        agent.history.clear()
        settings['model']['max_tokens'] += 1
        with self.assertRaises(AgentError):
            agent.run(prompt)
        self.assertEqual(len(client.requests), 1)
