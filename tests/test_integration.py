"""Offline integration: deterministic loopback HTTP fixture, NOT a real LLM."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from david.config import DEFAULTS


@contextmanager
def fixture_server(responses, streaming=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            message = responses[len(requests) - 1]
            if streaming:
                chunk = {'choices': [{'index': 0, 'delta': message, 'finish_reason': None}]}
                finish = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}]}
                body = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(finish) + '\n\ndata: [DONE]\n\n').encode()
            else:
                body = json.dumps({'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def tool_message(name, arguments, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class IntegrationTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'POSIX PTY integration; Windows console needs native verification')
    def test_real_terminal_yes_approval_creates_file(self):
        import os
        import pty
        import select
        import time
        entry = Path(__file__).resolve().parents[1] / 'david.py'
        responses = [tool_message('write_file', {'path': 'approved.txt', 'content': 'approved'}, 'w1'),
                     {'role': 'assistant', 'content': 'APPROVED_FIXTURE_DONE'}]
        with tempfile.TemporaryDirectory() as tmp, fixture_server(responses) as (url, requests):
            root = Path(tmp).resolve()
            config = root / 'setting.json'
            config.write_text(json.dumps({'model': {'base_url': url, 'name': 'fixture-not-an-llm', 'stream': False}}))
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, str(entry), '--settings', str(config), '--workspace', str(root), '--prompt', 'create approved.txt'],
                                       stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output, sent = bytearray(), False
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                        if b'yes ' in output and not sent:
                            os.write(master, b'yes\n')
                            sent = True
                    elif process.poll() is not None:
                        break
                self.assertEqual(process.wait(timeout=2), 0, output.decode(errors='replace'))
                self.assertTrue(sent)
                self.assertEqual((root / 'approved.txt').read_text(), 'approved')
                self.assertIn(b'APPROVED_FIXTURE_DONE', output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                os.close(master)

    def test_real_http_agent_read_edit_and_execute_approved_test(self):
        from david.agent import Agent
        from david.transport import OpenAIClient
        from david.workspace import WorkspaceTools
        original = 'value = 1\n'
        digest = hashlib.sha256(original.encode()).hexdigest()
        responses = [tool_message('read_file', {'path': 'sample.py'}, 'r1'),
                     tool_message('replace_text', {'path': 'sample.py', 'old': 'value = 1', 'new': 'value = 2', 'expected_sha256': digest}, 'r2'),
                     tool_message('run_command', {'name': 'test'}, 'r3'),
                     {'role': 'assistant', 'content': 'Fixture completed.'}]
        with tempfile.TemporaryDirectory() as tmp, fixture_server(responses) as (url, requests):
            root = Path(tmp).resolve()
            (root / 'sample.py').write_text(original)
            settings = deepcopy(DEFAULTS)
            settings['model'].update(base_url=url, name='fixture-not-an-llm', stream=False)
            settings['security']['enable_commands'] = True
            settings['commands'] = {'test': {'argv': ['{python}', '-c', "from pathlib import Path; assert Path('sample.py').read_text() == 'value = 2\\n'; print('REAL_COMMAND_OK')"], 'timeout_seconds': 5}}
            approvals = []
            def approve(action, detail):
                approvals.append((action, detail))
                return True
            tools = WorkspaceTools(root, settings['security'], settings['commands'], approve)
            result = Agent(OpenAIClient(settings['model']), tools, settings).run('Edit and test the sample.')
            self.assertEqual(result.text, 'Fixture completed.')
            self.assertEqual((root / 'sample.py').read_text(), 'value = 2\n')
            self.assertEqual(len(approvals), 2)
            self.assertEqual(len(requests), 4)
            self.assertIn('REAL_COMMAND_OK', requests[-1]['messages'][-1]['content'])

    def test_real_cli_streaming_and_noninteractive_write_denial(self):
        entry = Path(__file__).resolve().parents[1] / 'david.py'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            responses = [tool_message('write_file', {'path': 'unauthorized.txt', 'content': 'NO'}, 'w1'),
                         {'role': 'assistant', 'content': '승인 거부 확인\u001b[2J'}]
            # Streaming tool chunks include an index, as required by OpenAI SSE.
            responses[0]['tool_calls'][0]['index'] = 0
            with fixture_server(responses, streaming=True) as (url, requests):
                config = root / 'setting.json'
                config.write_text(json.dumps({'model': {'base_url': url, 'name': 'fixture-not-an-llm'}}))
                result = subprocess.run([sys.executable, str(entry), '--settings', str(config), '--workspace', str(root), '--prompt', 'write a file'],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((root / 'unauthorized.txt').exists())
                self.assertIn('DENIED', result.stdout)
                self.assertIn('승인 거부 확인', result.stdout)
                self.assertNotIn('\x1b', result.stdout)
                self.assertEqual(len(requests), 2)
