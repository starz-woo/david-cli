"""Viewport regressions: exercise the real renderer, not just buffer cursors."""
import asyncio
import unittest

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from test_tui import FakeController


class ChatViewportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from david.tui import DavidTUI
        self.context = create_pipe_input()
        self.pipe = self.context.__enter__()
        self.output = DummyOutput()
        self.output.get_size = lambda: Size(rows=24, columns=80)
        self.controller = FakeController()
        self.ui = DavidTUI(self.controller, input=self.pipe, output=self.output)
        self.task = asyncio.create_task(self.ui.application.run_async())
        await self.render()

    async def asyncTearDown(self):
        if not self.task.done():
            self.ui.application.exit(result=0)
        await asyncio.wait_for(self.task, 3)
        self.context.__exit__(None, None, None)

    async def render(self):
        self.ui.application.invalidate()
        await asyncio.sleep(.08)

    def screen(self):
        screen = self.ui.application.renderer.last_rendered_screen
        return '\n'.join(''.join(cell.char for _, cell in sorted(line.items()))
                         for y, line in sorted(screen.data_buffer.items())
                         if 0 <= y < self.output.get_size().rows)

    async def send(self, keys):
        self.pipe.send_text(keys)
        await self.render()

    async def test_expanded_state_details_share_the_transcript_render_budget(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'x' * 90000})
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'y' * 90000})
        self.controller.on_event({'type': 'status', 'status': 'error', 'detail': 'z' * 100000})
        await self.render()
        await self.send('\x0f')
        self.assertLessEqual(len(self.ui.transcript.text), 220000)
        self.assertIn('생략', self.ui.transcript.text)

    async def test_state_details_wrap_and_remain_inspectable_in_compact_layout(self):
        detail = 'context estimate ' + '한글 ' * 30 + 'DETAIL-END'
        self.controller.on_event({'type': 'status', 'status': 'reading', 'detail': detail})
        await self.render()
        self.assertIn('DETAIL-END', self.screen())
        self.output.get_size = lambda: Size(rows=12, columns=38)
        self.ui.application._on_resize()
        await self.render()
        self.assertIn('reading', self.screen())
        # Ctrl+O is the existing details toggle; long state text is scrollable
        # alongside tool details rather than silently clipped by a status bar.
        await self.send('\x0f')
        self.assertIn(detail, self.ui.transcript.text)
        self.assertIn('DETAIL-END', self.screen())
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))

    async def test_minimal_chrome_keeps_real_status_details_and_save_state_visible(self):
        await self.render()
        idle = self.screen()
        self.assertNotIn('idle |', idle)
        self.assertIn('/help', idle)
        self.assertIn('Ctrl+C exit', idle)
        self.assertIn('Ctrl+D exit', idle)
        self.assertIn('저장 OFF', idle)
        self.assertNotIn('Working', idle)
        self.assertNotIn('test-session', idle)
        self.controller.on_event({'type': 'status', 'status': 'running',
                                  'detail': 'context estimate 1234; not provider usage'})
        await self.render()
        self.assertIn('✳ running', self.screen())
        self.assertIn('context estimate 1234', self.screen())
        self.controller.on_event({'type': 'status', 'status': 'done',
                                  'detail': 'context estimate 1300'})
        self.controller.on_event({'type': 'session', 'id': 'hidden-uuid', 'saving': True})
        await self.render()
        self.assertNotIn('✳', self.screen())
        self.assertIn('context estimate 1300', self.screen())
        self.assertIn('saving ON (평문)', self.screen())
        self.assertNotIn('hidden-uuid', self.screen())
        from david.tui import THEMES
        for theme in ('dark', 'light'):
            # Roles remain legible without multicolored heading cards.
            self.assertIn('bg:', THEMES[theme]['body.user'])
            self.assertNotIn('bg:', THEMES[theme]['header'])
            self.assertNotIn('bg:', THEMES[theme]['role.assistant'])
            self.assertEqual(THEMES[theme]['title'], THEMES[theme]['working'])
        self.assertNotEqual(THEMES['dark']['body.user'], THEMES['dark'][''])

    async def test_composer_grows_from_one_row_with_rules_and_no_message_frame(self):
        await self.render()
        self.assertEqual(self.ui.composer.window.render_info.window_height, 1)
        self.assertNotIn('Message', self.screen())
        self.assertNotIn('YOU / 나', self.screen())
        self.assertNotIn('test-session', self.screen())
        self.assertIn('David', self.screen())
        info = self.ui.composer.window.render_info
        rows = self.screen().splitlines()
        self.assertTrue(rows[info._y_offset - 1].startswith('──'))
        self.assertTrue(rows[info._y_offset + 1].startswith('──'))
        self.assertTrue(rows[info._y_offset].startswith('❯ '))
        await self.send('one\ntwo\nthree')
        self.assertEqual(self.ui.composer.window.render_info.window_height, 3)
        self.ui.composer.text = '\n'.join(str(i) for i in range(12))
        await self.render()
        self.assertEqual(self.ui.composer.window.render_info.window_height, 5)
        self.ui.composer.text = ''
        await self.render()
        self.assertEqual(self.ui.composer.window.render_info.window_height, 1)
        # Soft wrapping (including wide glyphs) must grow the same real window.
        self.ui.composer.text = '한' * 60
        await self.render()
        self.assertEqual(self.ui.composer.window.render_info.window_height, 2)

    async def test_paused_viewport_stays_on_retained_messages_across_eviction(self):
        self.controller.on_event({'type': 'reset'})
        for i in range(300):
            self.controller.on_event({'type': 'message', 'role': 'assistant',
                                      'text': f'MESSAGE-{i:03}'})
        async with asyncio.timeout(3):
            while 'MESSAGE-299' not in self.screen():
                await asyncio.sleep(.02)
        await self.send('\x1b[5~')
        visible = self.transcript_rows()
        self.assertTrue(any('MESSAGE-' in line for line in visible))
        self.assertFalse(self.ui.follow_output)
        for i in range(300, 320):
            self.controller.on_event({'type': 'message', 'role': 'assistant',
                                      'text': f'MESSAGE-{i:03}'})
            await self.render()
            self.assertEqual(visible, self.transcript_rows(), f'after MESSAGE-{i:03}')
        self.assertTrue(self.ui._trimmed)
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.unseen_output)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        await self.send('\x1b[1;5F')
        self.assertIn('MESSAGE-319', self.screen())
        self.assertTrue(self.ui.follow_output)

    async def test_paused_viewport_survives_stream_prefix_truncation(self):
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'text', 'text': ''.join(
            f'LINE{i:04} ' + 'x' * 90 + '\n' for i in range(1000))})
        await self.render()
        await self.send('\x1b[5~')
        visible = self.transcript_rows()
        self.assertIn('LINE0987', ''.join(visible))
        self.assertFalse(self.ui.follow_output)
        for start in (1000, 1060):
            self.controller.on_event({'type': 'text', 'text': ''.join(
                f'LINE{i:04} ' + 'x' * 90 + '\n' for i in range(start, start + 60))})
            await self.render()
            self.assertIn('LINE0987', self.ui.transcript.text)
            self.assertEqual(visible, self.transcript_rows())
        self.assertTrue(self.ui._trimmed)
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.unseen_output)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))

    async def test_paused_cjk_span_survives_partial_line_prefix_truncation_and_resize(self):
        self.controller.on_event({'type': 'reset'})
        text = ' '.join(f'한글{i:05}' for i in range(12000))
        self.controller.on_event({'type': 'text', 'text': text})
        await self.render()
        await self.send('\x1b[5~')
        self.assertGreater(self.ui.transcript.window.vertical_scroll_2, 0)
        for start, columns in ((12000, 80), (13000, 53)):
            anchor_word = self.transcript_rows()[0].split()[2]
            chunk = ' ' + ' '.join(f'한글{i:05}' for i in range(start, start + 1000))
            text += chunk
            self.controller.on_event({'type': 'text', 'text': chunk})
            self.output.get_size = lambda c=columns: Size(rows=24, columns=c)
            self.ui.application._on_resize()
            await self.render()
            self.assertIn(anchor_word, ''.join(row[4:] for row in self.transcript_rows()[:2]))
            self.assertIn('[앞부분 생략]', self.ui.transcript.text)
            self.assertFalse(self.ui.follow_output)
        # Final messages replace the truncated streaming text with the original
        # full response; its prefix accounting must restart, not accumulate.
        visible = self.transcript_rows()
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': text})
        await self.render()
        self.assertEqual(visible, self.transcript_rows())
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))

    async def test_trimmed_paused_stream_prefix_clamps_to_oldest_retained_output(self):
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'text', 'text': ''.join(
            f'LINE{i:04} ' + 'x' * 90 + '\n' for i in range(1000))})
        await self.render()
        self.ui.transcript.window.scroll_rows(-10000)
        self.ui.transcript.window.scroll_rows(20)
        await self.render()
        self.assertIn('LINE0010', self.screen())
        self.controller.on_event({'type': 'text', 'text': ''.join(
            f'LINE{i:04} ' + 'x' * 90 + '\n' for i in range(1000, 1060))})
        await self.render()
        self.assertNotIn('LINE0010', self.ui.transcript.text)
        self.assertIn('화면 기록 한도', self.screen())
        self.assertIn('[앞부분 생략]', self.screen())
        self.assertIn('LINE0061', self.screen())
        self.assertEqual(self.ui.transcript.window.render_info.vertical_scroll, 0)
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.unseen_output)

    def transcript_rows(self):
        screen = self.ui.application.renderer.last_rendered_screen
        info = self.ui.transcript.window.render_info
        return [''.join(screen.data_buffer[y][x].char
                        for x in range(info._x_offset, info._x_offset + info.window_width))
                for y in range(info._y_offset, info._y_offset + info.window_height)]

    async def test_paused_wrapped_cjk_anchor_survives_size_retention_and_resize(self):
        self.controller.on_event({'type': 'reset'})
        for i in range(20):
            self.controller.on_event({'type': 'message', 'role': 'assistant',
                                      'text': f'OLD-{i}\n' + ('x' * 99 + '\n') * 90})
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': ' '.join(f'한글{i:03}' for i in range(400))})
        await self.render()
        await self.send('\x1b[5~')
        before = self.transcript_rows()
        self.assertIn('한글', ''.join(before))
        self.assertGreater(self.ui.transcript.window.vertical_scroll_2, 0)
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': 'NEW\n' + ('y' * 99 + '\n') * 300})
        await self.render()
        self.assertTrue(self.ui._trimmed)
        self.assertNotIn('OLD-0\n', self.ui.transcript.text)
        self.assertEqual(before, self.transcript_rows())
        # Retention and rewrapping in the same frame must use the old character
        # anchor, not the new entry's numeric buffer row or wrapped-row index.
        anchor_word = before[0].split()[2]
        self.output.get_size = lambda: Size(rows=24, columns=53)
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': 'MORE\n' + ('z' * 99 + '\n') * 300})
        self.ui.application._on_resize()
        await self.render()
        self.assertIn(anchor_word, ''.join(self.transcript_rows()[:2]))
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))

    async def test_evicted_paused_entry_clamps_to_oldest_retained_output(self):
        self.controller.on_event({'type': 'reset'})
        for i in range(300):
            self.controller.on_event({'type': 'message', 'role': 'assistant',
                                      'text': f'MESSAGE-{i:03}'})
        async with asyncio.timeout(3):
            while 'MESSAGE-299' not in self.screen():
                await asyncio.sleep(.02)
        self.ui.transcript.window.scroll_rows(-10000)
        self.ui.transcript.window.scroll_rows(20)
        await self.render()
        self.assertIn('MESSAGE-010', self.screen())
        for i in range(300, 330):
            self.controller.on_event({'type': 'message', 'role': 'assistant',
                                      'text': f'MESSAGE-{i:03}'})
        await self.render()
        self.assertNotIn('MESSAGE-010', self.ui.transcript.text)
        self.assertIn('MESSAGE-030', self.screen())
        self.assertIn('화면 기록 한도', self.screen())
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.unseen_output)
        self.assertEqual(self.ui.transcript.window.render_info.vertical_scroll, 0)

    async def test_tiny_width_uses_content_space_instead_of_wrapping_card_padding(self):
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'text', 'text': 'first\nZ'})
        await self.render()
        self.output.get_size = lambda: Size(6, 4)
        self.ui.application._on_resize()
        await self.render()
        self.assertNotIn('Window too small', self.screen())
        self.assertIn('Z', self.screen())

    async def test_event_backlog_yields_between_frames_without_losing_approval(self):
        for _ in range(1000):
            self.controller.on_event({'type': 'text', 'text': 'x'})
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'review this'})
        self.ui._drain_events(self.ui.application)
        text = self.ui._entries[-1]['text']
        self.assertGreater(len(text), 0)
        self.assertLess(len(text), 1000, 'one render must not drain an unlimited worker backlog')
        async with asyncio.timeout(3):
            while self.ui.approval is None:
                await asyncio.sleep(.02)
        self.assertEqual(self.ui._entries[-1]['text'], 'x' * 1000)
        self.assertIn('review this', self.screen())
        await self.send('\x19')
        self.assertEqual(self.controller.decisions, [True])

    async def test_mouse_wheel_pauses_without_stealing_composer_or_approval_focus(self):
        self.controller.on_event({'type': 'text', 'text': '\n'.join(f'line {i}' for i in range(80))})
        await self.render()
        await self.send('\x1b[<64;10;5M')
        self.assertFalse(self.ui.follow_output)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        position = (self.ui.transcript.window.vertical_scroll, self.ui.transcript.window.vertical_scroll_2)
        self.controller.on_event({'type': 'text', 'text': '\nWHEEL-NEW'})
        await self.render()
        self.assertEqual(position, (self.ui.transcript.window.vertical_scroll,
                                   self.ui.transcript.window.vertical_scroll_2))
        self.assertNotIn('WHEEL-NEW', self.screen())
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': '+safe\n-old'})
        await self.render()
        self.ui.application.layout.focus(self.ui.approval_panel)
        await self.send('\x1b[1;5F')
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.approval_panel))
        self.assertFalse(self.controller.decisions)

    async def test_follow_survives_resize_and_paused_reader_stays_paused(self):
        self.controller.on_event({'type': 'text', 'text': '한글 words ' * 200 + '\nRESIZE-END'})
        await self.render()
        await self.send('draft')
        for rows, columns in ((10, 32), (30, 100), (7, 18)):
            self.output.get_size = lambda r=rows, c=columns: Size(r, c)
            self.ui.application._on_resize()
            await self.render()
            self.assertIn('RESIZE-END', self.screen())
            self.assertIn('draft', self.screen())
            self.assertTrue(self.ui.follow_output)
        # Read far enough back that growing the viewport cannot naturally
        # reveal the tail; resize must preserve the text anchor, not row count.
        for _ in range(20):
            await self.send('\x1b[5~')
        self.output.get_size = lambda: Size(12, 38)
        self.ui.application._on_resize()
        await self.render()
        self.assertFalse(self.ui.follow_output)
        self.assertNotIn('RESIZE-END', self.screen())
        await self.send('\x1b[1;5F')
        self.assertIn('RESIZE-END', self.screen())

    async def test_reset_resumes_follow_for_restored_session(self):
        self.controller.on_event({'type': 'text', 'text': '\n'.join(f'old {i}' for i in range(80))})
        await self.render()
        await self.send('\x1b[5~')
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': '\n'.join(f'restored {i}' for i in range(100)) + '\nRESTORED-END'})
        await self.render()
        self.assertTrue(self.ui.follow_output)
        self.assertIn('RESTORED-END', self.screen())
        self.assertNotIn('Paused', self.screen())

    async def test_banner_renders_root_and_windows_workspace_identity(self):
        from pathlib import Path
        from prompt_toolkit.application.current import set_app

        original_workspace = self.controller.workspace
        original_model = self.controller.model_name
        cases = [
            (Path('/'), 'internal-model', 'internal-model · /'),
            (Path('C:/work/demo'), 'internal-model', 'internal-model · demo'),
            (Path(r'C:\work\demo'), 'internal-model', 'internal-model · demo'),
            (Path('/work/demo'), 'model  ·  extra\x1b', r'model · extra\u001b · demo'),
        ]
        try:
            for workspace, model, identity in cases:
                with self.subTest(workspace=workspace, model=model):
                    self.controller.workspace = workspace
                    self.controller.model_name = model
                    # Render synchronously in the running app so a banner exception
                    # fails here, rather than escaping into the redraw callback.
                    with set_app(self.ui.application):
                        self.ui.application._redraw()
                    lines = self.screen().splitlines()
                    self.assertEqual(lines[0].strip(), 'David')
                    self.assertEqual(lines[1].strip(), identity)
                    self.assertIn('❯', self.screen())
                    self.assertIn('/help', self.screen())
                    self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        finally:
            self.controller.workspace = original_workspace
            self.controller.model_name = original_model

    async def test_compact_header_keeps_identity_without_long_workspace_noise(self):
        from pathlib import Path
        self.controller.workspace = Path('/very/long/organization/project/directory/demo')
        self.controller.on_event({'type': 'session', 'id': 'session-1234', 'saving': False})
        await self.render()
        header = self.ui.header_text()
        self.assertIn('David', header)
        self.assertIn('demo', header)
        self.assertIn('internal-model', header)
        self.assertNotIn('session-1234', header)
        self.assertNotIn('/very/long', header)
        self.assertNotIn('|  |', self.ui.status_text())

    async def test_flowing_transcript_has_trusted_prefixes_and_safe_heading_code_styles(self):
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'user', 'text': '한글 request'})
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': '# Plan\nYOU / 나\n```python\nprint("<b>safe</b>")\n```'})
        self.controller.on_event({'type': 'tool', 'name': 'read_file', 'result': {'ok': True}})
        await self.render()
        self.assertIn('❯ 한글 request', self.screen())
        self.assertIn('● # Plan', self.screen())
        self.assertIn('● read_file', self.screen())
        self.assertIn('  ⎿ done', self.screen())
        self.assertNotIn('│', '\n'.join(self.transcript_rows()))
        lexer = self.ui.transcript.control.lexer.lex_document(self.ui.transcript.buffer.document)
        lines = self.ui.transcript.text.splitlines()
        self.assertNotIn('DAVID', lines)
        self.assertEqual(lines.count('YOU / 나'), 1, 'body text must not become a role label')
        self.assertIn('body.user', lexer(lines.index('한글 request'))[0][0])
        self.assertNotIn('role.user', lexer(lines.index('YOU / 나'))[0][0])
        self.assertIn('heading', lexer(lines.index('# Plan'))[0][0])
        self.assertIn('code', lexer(lines.index('print("<b>safe</b>")'))[0][0])
        self.assertIn('<b>safe</b>', self.screen())
        screen = self.ui.application.renderer.last_rendered_screen
        cell_styles = ' '.join(cell.style for line in screen.data_buffer.values() for cell in line.values())
        self.assertIn('class:role.user', cell_styles)
        self.assertIn('class:role.assistant', cell_styles)
        self.assertIn('class:heading', cell_styles)
        self.assertIn('class:code', cell_styles)
        for theme in ('dark', 'light', 'mono'):
            from david.tui import THEMES
            self.assertNotEqual(THEMES[theme]['role.user'], THEMES[theme]['role.assistant'])

    async def test_wrapped_paragraph_pages_in_visual_rows_and_resumes_at_bottom(self):
        self.output.get_size = lambda: Size(rows=12, columns=38)
        self.ui.application._on_resize()
        self.controller.on_event({'type': 'text', 'text': ' '.join(
            f'word{i:03}' for i in range(160)) + ' WRAPPED-TAIL'})
        await self.render()
        self.assertIn('TAIL', self.screen())
        await self.send('\x1b[5~')
        self.assertNotIn('TAIL', self.screen())
        paused = (self.ui.transcript.window.vertical_scroll,
                  self.ui.transcript.window.vertical_scroll_2)
        self.ui.application.layout.focus(self.ui.composer)
        self.controller.on_event({'type': 'text', 'text': ' MORE-TAIL'})
        await self.render()
        self.assertEqual(paused, (self.ui.transcript.window.vertical_scroll,
                                 self.ui.transcript.window.vertical_scroll_2))
        for _ in range(30):
            await self.send('\x1b[6~')
            if self.ui.follow_output:
                break
        self.assertTrue(self.ui.follow_output)
        self.assertIn('MORE-TAIL', ''.join(row[2:].rstrip() for row in self.transcript_rows()))
        self.controller.on_event({'type': 'text', 'text': '\nAFTER-PAGEDOWN'})
        await self.render()
        self.assertIn('AFTER-PAGEDOWN', self.screen())

    async def test_pause_survives_streaming_and_ctrl_end_returns_to_latest(self):
        self.controller.on_event({'type': 'text', 'text': '\n'.join(
            f'history {i}' for i in range(80))})
        await self.render()
        await self.send('\x1b[5~')
        before = self.ui.transcript.window.render_info.vertical_scroll
        self.ui.application.layout.focus(self.ui.composer)
        self.controller.on_event({'type': 'text', 'text': '\nNEW-OUTPUT'})
        await self.render()
        self.assertEqual(self.ui.transcript.window.render_info.vertical_scroll, before)
        self.assertNotIn('NEW-OUTPUT', self.screen())
        self.assertIn('Paused', self.screen())
        self.assertIn('new output', self.screen())
        await self.send('\x1b[1;5F')
        self.assertIn('NEW-OUTPUT', self.screen())
        self.assertTrue(self.ui.transcript.window.render_info.bottom_visible)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        self.assertNotIn('Paused', self.screen())

    async def test_new_prompt_returns_to_latest_after_reading_history(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant',
                                  'text': '\n'.join(f'old line {i}' for i in range(80))})
        await self.render()
        await self.send('\x1b[5~')
        self.ui.application.layout.focus(self.ui.composer)
        def submit(text):
            self.controller.submissions.append(text)
            self.controller.on_event({'type': 'message', 'role': 'user', 'text': text})
            return True
        self.controller.submit = submit
        await self.send('LATEST-QUESTION\r')
        await self.render()
        self.assertIn('LATEST-QUESTION', self.screen())
        self.assertTrue(self.ui.transcript.window.render_info.bottom_visible)

    async def test_stream_tracks_latest_on_screen_while_composer_has_focus(self):
        self.controller.on_event({'type': 'text', 'text': '\n'.join(
            f'reply line {i}' for i in range(80))})
        await self.render()
        await self.send('my next draft')
        self.controller.on_event({'type': 'text', 'text': '\nLATEST-STREAM'})
        await self.render()
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        self.assertIn('my next draft', self.screen())
        self.assertIn('LATEST-STREAM', self.screen())
        self.assertTrue(self.ui.transcript.window.render_info.bottom_visible)
