"""Real POSIX terminal + HTTP + file/command/session workflow, not an LLM benchmark."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from test_integration import fixture_server, tool_message


class TerminalProcess:
    def __init__(self, config, workspace, home):
        import fcntl
        import pty
        import struct
        import termios
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), TERM='xterm-256color')
        self.process = subprocess.Popen([sys.executable, '-m', 'david', '--settings', str(config),
                                         '--workspace', str(workspace), '--tui'],
                                        stdin=slave, stdout=slave, stderr=slave, env=env,
                                        cwd=Path(__file__).resolve().parents[1])
        os.close(slave)
        self.pending = bytearray()
        self.output = bytearray()

    def read(self, timeout=.1):
        import select
        if select.select([self.master], [], [], timeout)[0]:
            try:
                value = os.read(self.master, 65536)
            except OSError:
                value = b''
            self.pending.extend(value)
            self.output.extend(value)
            if b'\x1b[6n' in value:
                os.write(self.master, b'\x1b[1;1R')
            return bool(value)
        return self.process.poll() is None

    def expect(self, text, timeout=6):
        needle = text.encode('utf-8')
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            if time.monotonic() >= deadline or not self.read():
                raise AssertionError('Terminal did not show ' + repr(text) + '\n' + self.output.decode(errors='replace')[-5000:])
        end = self.pending.index(needle) + len(needle)
        del self.pending[:end]

    def send(self, text):
        os.write(self.master, text.encode('utf-8'))

    def quit(self):
        # A worker event may render before the async submit clears its draft.
        # Clear that draft explicitly so this command cannot append to /save on.
        self.send('\x15/quit\r')
        deadline = time.monotonic() + 5
        while self.process.poll() is None and time.monotonic() < deadline:
            self.read()
        if self.process.wait(timeout=2) != 0:
            raise AssertionError(self.output.decode(errors='replace')[-5000:])

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=3)
        os.close(self.master)


@unittest.skipIf(sys.platform == 'win32', 'POSIX PTY; Windows IME/console must be checked natively')
class TuiWorkflowTests(unittest.TestCase):
    def test_real_tui_patch_test_save_resume_and_conflict_safe_undo(self):
        original = 'value = 1\n'
        digest = hashlib.sha256(original.encode()).hexdigest()
        responses = [tool_message('read_file', {'path': 'sample.py'}, 'read'),
                     tool_message('apply_patch', {'changes': [
                         {'path': 'sample.py', 'content': 'value = 2\n', 'expected_sha256': digest},
                         {'path': 'added.txt', 'content': 'added\n'}]}, 'patch'),
                     tool_message('run_command', {'name': 'test'}, 'command'),
                     {'role': 'assistant', 'content': 'TUI_WORKFLOW_DONE'}]
        with tempfile.TemporaryDirectory() as tmp, fixture_server(responses) as (url, requests):
            root = Path(tmp).resolve()
            workspace, home = root / 'project', root / 'home'
            workspace.mkdir()
            home.mkdir()
            (workspace / 'sample.py').write_text(original)
            config = root / 'config.json'
            config.write_text(json.dumps({'model': {'name': 'fixture-not-an-llm', 'base_url': url, 'stream': False},
                'security': {'enable_commands': True}, 'commands': {'test': {'argv': ['{python}', '-c',
                "from pathlib import Path; assert Path('sample.py').read_text() == 'value = 2\\n'; print('REAL_TEST_OK')"],
                'timeout_seconds': 5}}}))
            first = TerminalProcess(config, workspace, home)
            try:
                first.expect('Message')
                first.send('sample.py 수정하고 테스트해줘\r')
                first.expect('Approval')
                first.send('\x19')
                first.expect('"argv":')
                first.send('\x19')
                first.expect('TUI_WORKFLOW_DONE')
                first.expect('done |')
                first.send('/save on\r')
                first.expect('saving')
                first.quit()
            finally:
                first.close()
            self.assertEqual((workspace / 'sample.py').read_text(), 'value = 2\n')
            self.assertEqual((workspace / 'added.txt').read_text(), 'added\n')
            self.assertEqual(len(requests), 4)
            self.assertIn('REAL_TEST_OK', requests[-1]['messages'][-1]['content'])
            records = list((home / '.david' / 'sessions').glob('*.json'))
            self.assertEqual(len(records), 1)
            saved = records[0].read_bytes()
            ident = records[0].stem
            second = TerminalProcess(config, workspace, home)
            try:
                second.expect('Message')
                second.send('/resume ' + ident + '\r')
                second.expect('복원 완료')
                second.send('/undo\r')
                second.expect('undo_last:')
                second.send('\x19')
                second.expect('되돌림 완료')
                second.quit()
            finally:
                second.close()
            self.assertEqual((workspace / 'sample.py').read_text(), original)
            self.assertFalse((workspace / 'added.txt').exists())
            self.assertEqual(records[0].read_bytes(), saved, 'resume must not silently enable autosave')
            self.assertEqual(len(requests), 4, 'session and undo commands never contact the model')
