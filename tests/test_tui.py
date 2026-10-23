"""Keyboard/event integration tests; no model or real terminal required."""
import asyncio
import unittest
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


class FakeController:
    workspace = Path('/workspace/demo')
    model_name = 'internal-model'
    busy = False
    saving = False
    session_id = 'test-session'
    files = ['src/app.py', 'docs/my notes.md']
    commands = ['/help', '/clear', '/quit']

    def __init__(self):
        self.submissions = []
        self.decisions = []
        self.cancelled = 0
        self.closed = []
        self.on_event = None

    def submit(self, text):
        self.submissions.append(text)
        return True

    def cancel(self):
        self.cancelled += 1

    def respond_approval(self, decision):
        self.decisions.append(decision)

    def close(self, timeout=2):
        self.closed.append(timeout)
        return True


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from david.tui import DavidTUI
        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.controller = FakeController()
        self.ui = DavidTUI(self.controller, input=self.pipe, output=DummyOutput())
        self.task = asyncio.create_task(self.ui.application.run_async())
        await self.wait_for(lambda: self.ui.application.is_running)

    async def asyncTearDown(self):
        if hasattr(self, 'task'):
            if not self.task.done():
                self.ui.application.exit(result=0)
            await asyncio.wait_for(self.task, 3)
        if hasattr(self, 'pipe_context'):
            self.pipe_context.__exit__(None, None, None)

    async def wait_for(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.01)

    async def send(self, text):
        self.pipe.send_text(text)
        await asyncio.sleep(.06)

    async def test_startup_discloses_server_transmission_and_storage_off(self):
        await self.wait_for(lambda: '서버로 전송' in self.ui.transcript.text)
        self.assertIn('샌드박스', self.ui.transcript.text)
        self.assertIn('저장 OFF', self.ui.status_text())

    async def test_failed_command_is_visible_when_collapsed_and_transcript_is_bounded(self):
        self.controller.on_event({'type': 'tool', 'name': 'run_command',
                                  'result': {'exit_code': 1, 'output': 'FAILED test_one'}})
        await self.wait_for(lambda: 'run_command' in self.ui.transcript.text)
        self.assertIn('error', self.ui.transcript.text)
        for i in range(30):
            self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': str(i) + 'x' * 10000})
        await self.wait_for(lambda: '29xxx' in self.ui.transcript.text)
        self.assertLessEqual(len(self.ui.transcript.text), 220000)
        self.assertIn('생략', self.ui.transcript.text)

    async def test_approval_diff_has_distinct_add_and_remove_styles(self):
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': '--- a\n+++ b\n-old\n+new'})
        await self.wait_for(lambda: self.ui.approval is not None)
        tokens = self.ui.approval_panel.control.lexer.lex_document(self.ui.approval_panel.buffer.document)
        self.assertIn('diff.add', tokens(4)[0][0])
        self.assertIn('diff.remove', tokens(3)[0][0])

    async def test_enter_submits_unicode_in_full_screen_composer(self):
        self.assertTrue(self.ui.application.full_screen)
        await self.send('한글 日本語 café 😀\r')
        await self.wait_for(lambda: self.controller.submissions)
        self.assertEqual(self.controller.submissions, ['한글 日本語 café 😀'])
        self.assertEqual(self.ui.composer.text, '')
        self.assertIn('internal-model', self.ui.header_text())
        self.assertIn('/workspace/demo', self.ui.header_text())
        self.assertIsNotNone(self.controller.on_event)

    async def test_approval_requires_fresh_exact_user_decision(self):
        await self.send('yes')
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': '--- a\n+++ b\n-old\n+new\x1b'})
        await self.wait_for(lambda: self.ui.composer.text == '')
        self.assertIn('+new\\u001b', self.ui.approval_panel.text)
        self.controller.on_event({'type': 'text', 'text': 'yes'})
        self.controller.on_event({'type': 'input', 'text': 'yes'})
        await self.send('\r')
        self.assertFalse(self.controller.decisions)
        await self.send('YES\r')
        self.assertFalse(self.controller.decisions)
        self.ui.composer.text = ' yes '
        await self.send('\r')
        self.assertFalse(self.controller.decisions)
        self.ui.composer.text = 'yes\nno'
        await self.send('\r')
        self.assertFalse(self.controller.decisions)
        self.ui.composer.text = 'yes'
        await self.send('\r')
        self.assertEqual(self.controller.decisions, [True])
        await self.send('\x19')
        self.assertEqual(self.controller.decisions, [True])
        self.controller.on_event({'type': 'approval', 'action': 'run', 'detail': 'command'})
        await self.wait_for(lambda: self.ui.approval is not None)
        await self.send('\x0e')
        self.assertEqual(self.controller.decisions, [True, False])
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'next'})
        await self.wait_for(lambda: self.ui.approval is not None)
        await self.send('\x19')
        self.assertEqual(self.controller.decisions, [True, False, True])
        self.assertFalse(self.controller.submissions)
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'cancel me'})
        self.controller.on_event({'type': 'status', 'status': 'cancelled'})
        await self.wait_for(lambda: self.ui.status == 'cancelled')
        await self.send('\x19')
        self.assertEqual(self.controller.decisions, [True, False, True])

    async def test_cancel_and_quit_wait_for_cooperative_close_without_freezing(self):
        import threading
        await self.send('draft\x03')
        self.assertEqual(self.ui.composer.text, '')
        self.assertEqual(self.controller.cancelled, 0)
        self.controller.busy = True
        self.controller.on_event({'type': 'approval', 'action': 'run', 'detail': 'sleep'})
        await self.wait_for(lambda: self.ui.approval is not None)
        await self.send('\x03\x19')
        self.assertEqual(self.controller.cancelled, 1)
        self.assertFalse(self.controller.decisions)
        entered, release = threading.Event(), threading.Event()
        threads = []
        def close(timeout=2):
            self.controller.closed.append(timeout)
            threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return False
        self.controller.close = close
        try:
            await self.send('\x11')
            await self.wait_for(entered.is_set)
            await self.send('responsive')
            self.assertEqual(self.ui.composer.text, 'responsive')
            self.assertFalse(self.task.done())
            self.assertNotEqual(threads[0], threading.get_ident())
        finally:
            release.set()
        await self.wait_for(lambda: 'Ctrl+Q' in self.ui.detail)
        self.assertIn('stopping', self.ui.status_text())
        self.assertFalse(self.task.done())
        self.assertEqual(self.controller.closed, [0])
        self.controller.on_event({'type': 'status', 'status': 'done'})
        await asyncio.sleep(.03)
        self.assertIn('stopping', self.ui.status_text())
        self.controller.close = lambda timeout=0: True
        # A false close keeps the application alive and polls, without another key.
        self.assertEqual(await asyncio.wait_for(self.task, 3), 0)

    async def test_tab_completes_only_controller_files_and_slash_commands(self):
        await self.send('@src/\t')
        self.assertEqual(self.ui.composer.text, '@src/app.py')
        self.ui.composer.text = '@"docs/my'
        self.ui.composer.buffer.cursor_position = len(self.ui.composer.text)
        await self.send('\t')
        self.assertEqual(self.ui.composer.text, '@"docs/my notes.md"')
        self.ui.composer.text = '/he'
        self.ui.composer.buffer.cursor_position = len(self.ui.composer.text)
        await self.send('\t')
        self.assertEqual(self.ui.composer.text, '/help')
        self.ui.composer.text = '@.env'
        self.ui.composer.buffer.cursor_position = len(self.ui.composer.text)
        await self.send('\t')
        self.assertEqual(self.ui.composer.text, '@.env')
        self.assertFalse(self.ui.application.layout.has_focus(self.ui.composer))
        await self.send('\x1b[Z')
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        self.controller.files = ['bad\u202efile', 'bad\nfile']
        self.ui.composer.text = '@bad'
        self.ui.composer.buffer.cursor_position = len(self.ui.composer.text)
        await self.send('\t')
        self.assertEqual(self.ui.composer.text, '@bad')

    async def test_tool_details_changes_and_scrollback_remain_usable(self):
        result = {'ok': True, 'output': '\n'.join('line ' + str(i) for i in range(100)) + '\nTAIL\u202e'}
        self.controller.on_event({'type': 'tool', 'name': 'read_file', 'result': result})
        self.controller.on_event({'type': 'changes', 'changes': [
            {'path': 'src/app.py', 'added': 2, 'removed': 1, 'status': 'modified'}]})
        await self.wait_for(lambda: 'read_file' in self.ui.transcript.text)
        self.assertNotIn('TAIL', self.ui.transcript.text)
        self.assertIn('src/app.py', self.ui.changes_text())
        self.assertIn('+2', self.ui.changes_text())
        self.assertIn('-1', self.ui.changes_text())
        await self.send('\x0f')
        self.assertIn('TAIL', self.ui.transcript.text)
        self.assertIn('line 0\nline 1', self.ui.transcript.text)
        self.assertNotIn('\u202e', self.ui.transcript.text)
        await self.send('\x1b[5~')
        self.assertLess(self.ui.transcript.buffer.cursor_position, len(self.ui.transcript.text))
        cursor = self.ui.transcript.buffer.cursor_position
        self.controller.on_event({'type': 'text', 'text': 'stream while reading'})
        await self.wait_for(lambda: 'stream while reading' in self.ui.transcript.text)
        self.assertEqual(self.ui.transcript.buffer.cursor_position, cursor)
        await self.send('\x1b[6~')
        self.assertGreater(self.ui.transcript.buffer.cursor_position, cursor)
        await self.send('\x0f')
        self.assertNotIn('TAIL', self.ui.transcript.text)

    async def test_resize_keeps_unicode_composer_and_approval_visible(self):
        from prompt_toolkit.data_structures import Size
        await self.send('한글😀')
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': '+한글\n-old'})
        await self.wait_for(lambda: self.ui.approval is not None)
        await self.send('no')
        for rows, columns in [(8, 24), (3, 12), (30, 100), (6, 18)]:
            self.ui.application.output.get_size = lambda r=rows, c=columns: Size(r, c)
            self.ui.application._on_resize()
            await asyncio.sleep(.06)
            screen = self.ui.application.renderer.last_rendered_screen
            rendered = '\n'.join(''.join(cell.char for _, cell in sorted(line.items()))
                                 for _, line in sorted(screen.data_buffer.items()))
            self.assertNotIn('Window too small', rendered)
            self.assertIn('no', rendered)
            self.assertIn('write', rendered)
        await self.send('\r')
        self.assertEqual(self.controller.decisions, [False])

    async def test_reset_clears_prior_conversation_and_approval_before_replay(self):
        self.controller.on_event({'type': 'text', 'text': 'old turn'})
        self.controller.on_event({'type': 'tool', 'name': 'old_tool', 'result': {'ok': True}})
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'stale'})
        self.controller.on_event({'type': 'changes', 'changes': [{'path': 'old.py', 'action': 'create'}]})
        await self.wait_for(lambda: self.ui.approval is not None)
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'restored'})
        await self.wait_for(lambda: 'restored' in self.ui.transcript.text)
        await self.send('\x19')
        self.assertFalse(self.controller.decisions)
        self.assertNotIn('old turn', self.ui.transcript.text)
        self.assertNotIn('old_tool', self.ui.transcript.text)
        self.assertNotIn('old.py', self.ui.changes_text())
        self.assertEqual(self.ui.composer.text, '')
        self.assertIsNone(self.ui.approval)

    async def test_theme_and_input_height_follow_controller_preferences(self):
        from david.tui import DavidTUI
        for theme in ('dark', 'light', 'mono'):
            self.controller.ui = {'theme': theme, 'input_height': 6}
            ui = DavidTUI(self.controller, input=self.pipe, output=DummyOutput())
            self.assertEqual(ui.theme, theme)
            self.assertEqual(ui.composer.window.height.preferred, 6)
            rules = dict(ui.application.style.style_rules)
            if theme == 'mono':
                self.assertFalse(any('#' in value for value in rules.values()))
            elif theme == 'light':
                self.assertIn('bg:#edf2f7', rules['header'])
        self.controller.ui = {'theme': 'invalid', 'input_height': 'invalid'}
        ui = DavidTUI(self.controller, input=self.pipe, output=DummyOutput())
        self.assertEqual(ui.theme, 'dark')
        self.assertEqual(ui.composer.window.height.preferred, 5)

    async def test_slow_submit_does_not_block_composer_or_overwrite_new_draft(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        threads = []
        def submit(text):
            self.controller.submissions.append(text)
            threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return True
        self.controller.submit = submit
        try:
            await self.send('request\r')
            await self.wait_for(entered.is_set)
            self.assertNotEqual(threads[0], threading.get_ident())
            await self.send(' continued')
            self.assertIn('continued', self.ui.composer.text)
            await self.send('\r')
            self.assertEqual(self.controller.submissions, ['request'])
        finally:
            release.set()
        await asyncio.sleep(.05)
        self.assertIn('continued', self.ui.composer.text)

    async def test_change_actions_do_not_invent_missing_line_counts(self):
        self.controller.on_event({'type': 'changes', 'changes': [
            {'path': 'new.py', 'action': 'create'},
            {'path': 'old.py', 'action': 'delete'},
        ]})
        await self.wait_for(lambda: 'new.py' in self.ui.changes_text())
        self.assertIn('create', self.ui.changes_text())
        self.assertIn('delete', self.ui.changes_text())
        self.assertNotIn('+0', self.ui.changes_text())

    async def test_queued_cancellation_invalidates_decision_before_next_render(self):
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'stale'})
        await self.wait_for(lambda: self.ui.approval is not None)
        # Worker cancellation is received between redraw and the next user key.
        self.controller.on_event({'type': 'status', 'status': 'cancelled'})
        self.ui._decide(True)
        self.assertFalse(self.controller.decisions)

    async def test_submission_errors_preserve_draft_and_render_safely(self):
        self.ui.composer.text = 'keep my request'
        self.controller.submit = lambda text: False
        await self.send('\r')
        self.assertEqual(self.ui.composer.text, 'keep my request')
        def fail(text):
            raise ValueError('failed\x1b\u202e')
        self.controller.submit = fail
        await self.ui._submit(self.ui.composer.text, self.ui._composer_revision)
        self.assertEqual(self.ui.status, 'error')
        self.assertIn('failed\\u001b\\u202e', self.ui.status_text())
        self.assertEqual(self.ui.composer.text, 'keep my request')
        def cancel(text):
            raise KeyboardInterrupt('cancelled')
        self.controller.submit = cancel
        await self.ui._submit(self.ui.composer.text, self.ui._composer_revision)
        self.assertEqual(self.ui.status, 'cancelled')
        self.assertFalse(self.task.done())

    async def test_quit_command_uses_cooperative_shutdown(self):
        await self.send('/quit\r')
        await self.wait_for(self.task.done)
        self.assertEqual(self.task.result(), 0)
        self.assertTrue(self.controller.closed)
        self.assertFalse(self.controller.submissions)

    async def test_quit_waits_for_pending_submit_before_final_close(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def submit(text):
            entered.set()
            release.wait(2)
            self.controller.busy = True
            return True
        def cancel():
            self.controller.cancelled += 1
            self.controller.busy = False
        self.controller.submit = submit
        self.controller.cancel = cancel
        self.controller.close = lambda timeout=0: not self.controller.busy
        try:
            await self.send('start\r')
            await self.wait_for(entered.is_set)
            await self.send('\x11')
            self.assertFalse(self.task.done())
        finally:
            release.set()
        self.assertEqual(await asyncio.wait_for(self.task, 3), 0)
        self.assertFalse(self.controller.busy)

    async def test_final_message_after_summary_does_not_duplicate_stream(self):
        for event in [
            {'type': 'text', 'text': 'answer'},
            {'type': 'summary', 'text': '1 call'},
            {'type': 'status', 'status': 'done'},
            {'type': 'message', 'role': 'assistant', 'text': 'answer'},
        ]:
            self.controller.on_event(event)
        await self.wait_for(lambda: '1 call' in self.ui.transcript.text)
        self.assertEqual(self.ui.transcript.text.count('answer'), 1)

    async def test_cancel_during_pending_submit_cancels_late_started_work(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def submit(text):
            entered.set()
            release.wait(2)
            self.controller.busy = True
            return True
        def cancel():
            self.controller.cancelled += 1
            self.controller.busy = False
        self.controller.submit = submit
        self.controller.cancel = cancel
        try:
            await self.send('start\r')
            await self.wait_for(entered.is_set)
            await self.send('\x03')
        finally:
            release.set()
        await self.wait_for(lambda: not self.ui._submitting)
        self.assertFalse(self.controller.busy)
        self.assertGreaterEqual(self.controller.cancelled, 1)

    async def test_worker_events_stream_once_and_update_status(self):
        import threading
        def emit():
            for event in [
                {'type': 'message', 'role': 'user', 'text': '<b>hello</b>'},
                {'type': 'status', 'status': 'reading', 'detail': 'src/app.py'},
                {'type': 'text', 'text': '안녕 '},
                {'type': 'text', 'text': '<b>world</b>\x1b\u202e'},
                {'type': 'message', 'role': 'assistant', 'text': '안녕 <b>world</b>\x1b\u202e'},
                {'type': 'summary', 'text': '1 model call'},
                {'type': 'session', 'id': 'new-session', 'saving': True},
            ]:
                self.controller.on_event(event)
        thread = threading.Thread(target=emit)
        thread.start()
        thread.join()
        await self.wait_for(lambda: '1 model call' in self.ui.transcript.text)
        self.assertEqual(self.ui.transcript.text.count('안녕'), 1)
        self.assertIn('<b>world</b>\\u001b\\u202e', self.ui.transcript.text)
        self.assertIn('<b>hello</b>', self.ui.transcript.text)
        self.assertIn('reading', self.ui.status_text())
        self.assertIn('src/app.py', self.ui.status_text())
        self.assertIn('new-session', self.ui.header_text())
        self.assertIn('saving', self.ui.status_text())
        self.controller.on_event({'type': 'input', 'text': 'edit 한글\u202e'})
        await self.wait_for(lambda: self.ui.composer.text == 'edit 한글\\u202e')

    async def test_multiline_paste_newline_keys_and_submit_fallback(self):
        await self.send('first\x1b\rsecond\nthird')
        self.assertEqual(self.ui.composer.text, 'first\nsecond\nthird')
        await self.send('\x1b[200~\nfourth\r\nfifth\x1b]52;attack\x07\u202e\x1b[201~')
        self.assertFalse(self.controller.submissions)
        self.assertEqual(self.ui.composer.text,
                         'first\nsecond\nthird\nfourth\nfifth\\u001b]52;attack\\u0007\\u202e')
        await self.send('\x13')
        self.assertEqual(self.controller.submissions, [
            'first\nsecond\nthird\nfourth\nfifth\\u001b]52;attack\\u0007\\u202e'])


class TuiRunnerTests(unittest.TestCase):
    def test_run_tui_returns_after_cooperative_shutdown(self):
        from david.tui import run_tui
        controller = FakeController()
        with create_pipe_input() as pipe:
            pipe.send_text('\x11')
            self.assertEqual(run_tui(controller, input=pipe, output=DummyOutput()), 0)
        self.assertTrue(controller.closed)
        self.assertGreaterEqual(controller.cancelled, 1)
