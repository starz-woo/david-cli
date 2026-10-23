import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CliTests(unittest.TestCase):
    def test_interactive_terminal_selects_tui_and_explicit_plain_preserves_legacy(self):
        import json
        import types
        from david.cli import main
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / 'config.json'
            config.write_text(json.dumps({'model': {'name': 'fixture'}}))
            run_tui = MagicMock(return_value=0)
            with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                    patch.dict('sys.modules', {'david.tui': types.SimpleNamespace(run_tui=run_tui)}), \
                    patch('builtins.input', return_value='/quit'), contextlib.redirect_stdout(io.StringIO()):
                # --tui is explicit so redirect_stdout need not pretend to be a terminal.
                self.assertEqual(main(['--settings', str(config), '--workspace', str(root), '--tui']), 0)
                run_tui.assert_called_once()
                self.assertEqual(main(['--settings', str(config), '--workspace', str(root), '--plain']), 0)
                self.assertEqual(run_tui.call_count, 1)

    def test_build_session_loads_prompt_and_protects_settings(self):
        from david.cli import build_session
        from unittest.mock import MagicMock
        import json
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'custom.json'
            prompt = root / 'style.txt'
            prompt.write_text('Answer concisely.')
            config.write_text(json.dumps({'model': {'name': 'internal'}, 'agent': {'system_prompt_file': 'style.txt'}}))
            tools = MagicMock()
            tools.schemas.return_value = []
            factory = MagicMock(return_value=tools)
            modules = {'david.transport': types.SimpleNamespace(OpenAIClient=MagicMock()),
                       'david.workspace': types.SimpleNamespace(WorkspaceTools=factory)}
            with patch.dict('sys.modules', modules):
                agent = build_session(config, root, True)
            self.assertTrue(agent.settings['security']['read_only'])
            self.assertFalse(agent.settings['security']['enable_commands'])
            self.assertIn('Answer concisely.', agent.system)
            self.assertIn(config.resolve(), factory.call_args.kwargs['protected_paths'])
            self.assertIn(prompt.resolve(), factory.call_args.kwargs['protected_paths'])

    def test_one_shot_and_reload_rebuild_session(self):
        from david.cli import main
        from david.agent import TurnResult
        from david.config import DEFAULTS
        from unittest.mock import MagicMock
        import json
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'setting.json'
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            agent = MagicMock()
            agent.settings = DEFAULTS
            agent.run.return_value = TurnResult('Done', 1, 0.01)
            with patch('david.cli.build_session', return_value=agent, create=True) as build, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--settings', str(config), '--workspace', tmp, '--prompt', 'hello']), 0)
                agent.run.assert_called_once_with('hello')
                build.assert_called_once()
            with patch('david.cli.build_session', return_value=agent, create=True) as build, patch('builtins.input', side_effect=['/reload', '/quit']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--settings', str(config), '--workspace', tmp]), 0)
                self.assertEqual(build.call_count, 2)

    def test_workspace_is_mandatory(self):
        from david.cli import main
        import json
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'setting.json'
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(['--settings', str(config), '--prompt', 'hello']), 2)
            self.assertIn('--workspace', error.getvalue())

    def test_cli_check_is_offline_and_missing_file_is_error(self):
        from david.cli import main
        import json
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'setting.json'
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            with contextlib.redirect_stdout(io.StringIO()) as output, patch('socket.socket', side_effect=AssertionError('network forbidden')):
                self.assertEqual(main(['--settings', str(config), '--check']), 0)
            self.assertIn('OFFLINE', output.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--settings', str(config) + '.missing', '--check']), 2)

    def test_module_entrypoint_help(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-m', 'david', '--help'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--workspace', result.stdout)
        self.assertIn('--settings', result.stdout)

    def test_terminal_escape_and_bidi_are_rendered_inert(self):
        from david.cli import safe_text
        text = safe_text('한글\n\x1b]52;c;payload\x07\x9b2J\rX\u202eevil')
        self.assertIn('한글\n', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\x07', text)
        self.assertNotIn('\x9b', text)
        self.assertNotIn('\r', text)
        self.assertNotIn('\u202e', text)
        self.assertIn('\\u001b', text)

    def test_approval_interrupt_cancels_real_agent_turn_before_more_tools_or_requests(self):
        import json
        from copy import deepcopy
        from unittest.mock import MagicMock
        from david.agent import Agent
        from david.cli import approve, run_turn
        from david.config import DEFAULTS
        from david.workspace import WorkspaceTools
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            tools = WorkspaceTools(root, {}, {}, approve)
            calls = [{'id': name, 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': json.dumps({'path': name, 'content': 'changed'})}}
                for name in ('first.txt', 'second.txt')]
            client = MagicMock()
            client.complete.side_effect = [
                {'role': 'assistant', 'content': None, 'tool_calls': calls},
                {'role': 'assistant', 'content': 'Done.'}]
            agent = Agent(client, tools, deepcopy(DEFAULTS))
            agent.history = [[{'role': 'user', 'content': 'Earlier request'},
                              {'role': 'assistant', 'content': 'Earlier answer'}]]
            with contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', side_effect=[KeyboardInterrupt, 'yes']) as prompt:
                status = run_turn(agent, 'Write both files')
            self.assertEqual(status, 130)
            self.assertEqual(agent.history, [])
            self.assertEqual(client.complete.call_count, 1)
            self.assertEqual(prompt.call_count, 1)
            self.assertEqual(list(root.iterdir()), [])
            self.assertIn('Interrupted. Conversation cleared', output.getvalue())

    def test_approval_eof_is_still_denied(self):
        from david.cli import approve
        with contextlib.redirect_stdout(io.StringIO()), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=EOFError):
            self.assertFalse(approve('write', 'diff'))

    def test_approval_denies_noninteractive_and_requires_exact_yes(self):
        from david.cli import approve
        with contextlib.redirect_stdout(io.StringIO()), patch('sys.stdin.isatty', return_value=False):
            self.assertFalse(approve('write', 'diff'))
        with contextlib.redirect_stdout(io.StringIO()), patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y'):
            self.assertFalse(approve('write', 'diff'))
        with contextlib.redirect_stdout(io.StringIO()), patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='yes'):
            self.assertTrue(approve('write', 'diff'))
