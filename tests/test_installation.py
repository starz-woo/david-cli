"""Global-command contract without modifying the real user's HOME/PATH."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from david.config import DEFAULTS


class InstallationTests(unittest.TestCase):
    def test_active_command_launcher_is_protected(self):
        from david.cli import build_session
        import sys
        from david.workspace import ToolError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            launcher = root / 'david-command'
            launcher.write_text('trusted launcher')
            config = root / 'config.json'
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            with patch.object(sys, 'argv', [str(launcher)]):
                agent = build_session(config, root)
            with self.assertRaises(ToolError):
                agent.tools.execute('read_file', {'path': launcher.name})

    def test_package_version_matches_runtime(self):
        import tomllib
        import david
        meta = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
        self.assertEqual(meta['project']['version'], david.__version__)

    def test_console_uses_user_settings_and_current_directory(self):
        from david.cli import console_main
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            config = home / '.david' / 'setting.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            workspace = home / 'work'
            workspace.mkdir()
            # The workspace's hostile settings are never auto-loaded.
            (workspace / 'setting.json').write_text('not valid JSON')
            agent = MagicMock()
            agent.settings = deepcopy(DEFAULTS)
            with patch('pathlib.Path.home', return_value=home), patch('pathlib.Path.cwd', return_value=workspace), \
                    patch('david.cli.build_session', return_value=agent) as build, \
                    patch('builtins.input', return_value='/quit'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(console_main([]), 0)
            build.assert_called_once_with(config, workspace, False)

    def test_console_init_is_offline_and_never_overwrites(self):
        from david.cli import console_main
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            config = home / '.david' / 'setting.json'
            with patch('pathlib.Path.home', return_value=home), \
                    patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(console_main(['--init']), 0)
                value = json.loads(config.read_text())
                self.assertEqual(value['model']['name'], 'CHANGE_ME')
                config.write_text('keep this exactly')
                self.assertEqual(console_main(['--init']), 0)
                self.assertEqual(config.read_text(), 'keep this exactly')
                self.assertEqual(console_main(['--config-path']), 0)
            self.assertIn(str(config), output.getvalue())

    def test_console_explicit_settings_and_workspace_take_precedence(self):
        from david.cli import console_main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / 'explicit.json'
            config.write_text(json.dumps({'model': {'name': 'internal'}}))
            agent = MagicMock()
            agent.settings = deepcopy(DEFAULTS)
            with patch('david.cli.build_session', return_value=agent) as build, \
                    patch('builtins.input', return_value='/quit'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(console_main(['--settings', str(config), '--workspace', str(root)]), 0)
            build.assert_called_once_with(config, root, False)
