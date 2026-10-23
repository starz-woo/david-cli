"""File safety caps remain independent of configurable context budgets."""
import json
import tempfile
import unittest
from pathlib import Path
from david.controller import Controller
from test_agent import FakeClient


class AttachmentBudgetTests(unittest.TestCase):
    def test_token_mode_attachment_bypasses_legacy_character_cap_but_keeps_file_output_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / 'project'
            workspace.mkdir()
            (workspace / 'large.txt').write_text('x' * 45000)
            settings = root / 'settings.json'
            raw = {'model': {'name': 'fixture', 'context_window_tokens': 1000000},
                   'agent': {'max_context_chars': 4096},
                   'security': {'max_output_chars': 65536}}
            settings.write_text(json.dumps(raw))
            controller = Controller(settings, workspace, session_dir=root / 'sessions')
            self.addCleanup(controller.close)
            controller.agent.client = FakeClient({'role': 'assistant', 'content': 'ok'})
            controller._run_prompt('read @large.txt')
            self.assertIn('x' * 45000, controller.agent.client.requests[0][-1]['content'])
            controller.agent.settings['model']['context_window_tokens'] = 0
            with self.assertRaisesRegex(ValueError, '컨텍스트'):
                controller._attach('read @large.txt')
            controller.agent.settings['model']['context_window_tokens'] = 1000000
            (workspace / 'large.txt').write_text('x' * 70000)
            with self.assertRaisesRegex(ValueError, '출력 제한'):
                controller._attach('read @large.txt')
