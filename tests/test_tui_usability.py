"""Usability contracts against a running application and its rendered cells."""
import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.styles import Style

import test_tui_scroll


class UsabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_tui_scroll.ChatViewportTests.asyncSetUp
    asyncTearDown = test_tui_scroll.ChatViewportTests.asyncTearDown
    render = test_tui_scroll.ChatViewportTests.render
    send = test_tui_scroll.ChatViewportTests.send
    screen = test_tui_scroll.ChatViewportTests.screen

    async def test_arrow_history_restores_draft_without_recording_commands_or_rejections(self):
        await self.send('first 한글\r')
        await self.send('/help\r')
        self.controller.submit = lambda text: False
        await self.send('rejected\r')
        self.controller.submit = lambda text: True
        self.ui.composer.text = ''
        await self.send('second\nline\r')
        await self.send('unsent draft')
        await self.send('\x1b[A')
        self.assertEqual(self.ui.composer.text, 'second\nline')
        # Recall starts at the first row so another Up reaches older history.
        await self.send('\x1b[A')
        self.assertEqual(self.ui.composer.text, 'first 한글')
        await self.send('\x1b[B')
        self.assertEqual(self.ui.composer.text, 'second\nline')
        # Down within a multiline entry is ordinary cursor movement first.
        self.ui.composer.buffer.cursor_position = len(self.ui.composer.text)
        await self.send('\x1b[B')
        self.assertEqual(self.ui.composer.text, 'unsent draft')
        self.assertEqual(self.ui.input_history, ['first 한글', 'second\nline'])

    async def test_history_arrows_respect_soft_wrapped_cjk_visual_rows(self):
        await self.send('previous\r')
        draft = '한' * 100
        self.ui.composer.text = draft
        self.ui.composer.buffer.cursor_position = len(draft)
        await self.render()
        await self.send('\x1b[A')
        self.assertEqual(self.ui.composer.text, draft)
        self.ui.composer.buffer.cursor_position = 0
        await self.send('\x1b[A')
        self.assertEqual(self.ui.composer.text, 'previous')
        await self.send('\x1b[B')
        self.assertEqual(self.ui.composer.text, draft)

    async def test_history_is_bounded_in_memory_without_persisting_or_partial_prompts(self):
        for n in range(110):
            await self.ui._submit(str(n), self.ui._composer_revision)
        self.assertEqual(len(self.ui.input_history), 100)
        self.assertEqual(self.ui.input_history[0], '10')
        await self.ui._submit('x' * 100001, self.ui._composer_revision)
        self.assertNotIn('x' * 100001, self.ui.input_history)
        self.assertLessEqual(sum(map(len, self.ui.input_history)), 100000)

    async def test_copy_is_user_triggered_final_reply_only_and_never_submitted(self):
        with patch('david.tui.copy_text') as copy:
            self.controller.on_event({'type': 'text', 'text': 'provisional'})
            await self.render()
            await self.send('/copy\r')
            copy.assert_not_called()
            self.assertIn('No finalized', self.ui.detail)
            self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': '```py\n한글\n```'})
            self.controller.on_event({'type': 'text', 'text': 'unfinished next reply'})
            await self.render()
            copy.assert_not_called()
            await self.send('/copy\r')
            copy.assert_called_once_with('```py\n한글\n```')
            self.assertIn('Copied', self.ui.detail)
            self.assertFalse(self.controller.submissions)
            self.assertFalse(self.ui.input_history)
            self.controller.on_event({'type': 'reset'})
            await self.render()
            await self.send('/copy\r')
            self.assertEqual(copy.call_count, 1)

    async def test_copy_failure_is_honest_and_does_not_approve(self):
        from david.clipboard import ClipboardError
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'reply'})
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'review'})
        await self.render()
        with patch('david.tui.copy_text', side_effect=ClipboardError('unavailable')):
            await self.send('/copy\r')
        self.assertIn('Copy failed', self.ui.detail)
        self.assertIn('Shift', self.ui.detail)
        self.assertFalse(self.controller.decisions)
        self.assertFalse(self.controller.submissions)
        self.assertIsNotNone(self.ui.approval)

    async def test_approval_menu_requires_fresh_navigation_defaults_deny_and_esc_denies(self):
        await self.send('yes')
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'review'})
        await self.render()
        self.assertIn('Allow once', self.screen())
        self.assertIn('Deny', self.screen())
        self.assertFalse(self.ui.approval_choice)
        await self.send('\r')
        self.assertFalse(self.controller.decisions, 'unscoped Enter must never approve or consume approval')
        await self.send('\x1b[A\r')
        self.assertEqual(self.controller.decisions, [True])
        self.controller.on_event({'type': 'approval', 'action': 'run', 'detail': 'next'})
        await self.render()
        self.assertFalse(self.ui.approval_choice)
        await self.send('\x1b[B\r')
        self.assertEqual(self.controller.decisions, [True, False])
        self.controller.on_event({'type': 'approval', 'action': 'run', 'detail': 'third'})
        await self.render()
        await self.send('\x1b')
        await asyncio.sleep(.6)
        self.assertEqual(self.controller.decisions, [True, False, False])
        self.assertFalse(self.ui.input_history)

    async def test_menu_cancellation_and_replacement_invalidate_armed_allow(self):
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'first'})
        await self.render()
        await self.send('\x1b[A')
        self.assertTrue(self.ui.approval_choice)
        self.controller.on_event({'type': 'approval', 'action': 'run', 'detail': 'replacement'})
        self.ui._decide(True)
        self.assertFalse(self.controller.decisions)
        await self.render()
        self.assertFalse(self.ui.approval_choice)
        await self.send('\r')
        self.assertFalse(self.controller.decisions)
        await self.send('\x1b[A\x03\r')
        self.assertFalse(self.controller.decisions)

    async def test_menu_focus_keeps_exit_commands_unconditional(self):
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'first'})
        await self.render()
        await self.send('\x1b[A/quit\r')
        self.assertFalse(self.controller.decisions)
        self.assertFalse(self.controller.submissions)
        self.assertEqual(await asyncio.wait_for(self.task, 3), 0)

    async def test_approval_diff_scrolls_from_composer_and_shows_real_hunk_line_numbers(self):
        detail = 'diff --git a/test.py b/test.py\n--- a/test.py\n+++ b/test.py\n@@ -10,2 +10,31 @@\n-old\n keep\n' + '\n'.join('+new' + str(n) for n in range(30))
        self.controller.on_event({'type': 'approval', 'action': 'write test.py', 'detail': detail})
        await self.render()
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        lex = self.ui.approval_panel.lexer.lex_document(self.ui.approval_panel.buffer.document)
        self.assertIn('diff.header', lex(1)[0][0])
        self.assertIn('diff.context', lex(6)[0][0])
        self.assertEqual(self.ui.approval_panel.text, 'write test.py\n' + detail)
        self.assertIn('10', ''.join(text for _, text in self.ui._diff_prefix(5, 0)))
        self.assertIn('11', ''.join(text for _, text in self.ui._diff_prefix(6, 0)))
        self.assertNotIn('10', ''.join(text for _, text in self.ui._diff_prefix(1, 0)))
        for _ in range(8):
            await self.send('\x1b[6~')
        self.assertIn('+new29', self.screen())
        self.assertFalse(self.controller.decisions)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))

    async def test_emitted_phase_animates_on_time_without_inventing_thinking(self):
        from prompt_toolkit.formatted_text import fragment_list_to_text
        self.controller.on_event({'type': 'status', 'status': 'waiting', 'detail': 'Awaiting server'})
        await self.render()
        first = self.ui.application.render_counter
        initial = fragment_list_to_text(self.ui.activity_text())
        await asyncio.sleep(.35)
        later = fragment_list_to_text(self.ui.activity_text())
        self.assertGreater(self.ui.application.render_counter, first)
        self.assertNotEqual(initial, later)
        self.assertIn('waiting', later)
        self.assertNotIn('thinking', later)
        self.assertIn('s', later)
        self.controller.on_event({'type': 'status', 'status': 'thinking', 'detail': 'Server reasoning delta'})
        await self.render()
        self.assertIn('thinking', fragment_list_to_text(self.ui.activity_text()))
        self.controller.on_event({'type': 'status', 'status': 'done'})
        await self.render()
        done = fragment_list_to_text(self.ui.activity_text())
        await asyncio.sleep(.25)
        self.assertEqual(done, fragment_list_to_text(self.ui.activity_text()))

    async def test_tool_phases_animate_in_actual_renderer_and_restart_the_phase_clock(self):
        clock = [100.0]
        with patch('david.tui.monotonic', side_effect=lambda: clock[0]):
            for phase in ('reading', 'editing', 'running'):
                self.controller.on_event({'type': 'status', 'status': phase})
                await self.render()
                initial = self.screen()
                self.assertIn(phase + ' · 0.0s', initial)
                clock[0] += .3
                self.ui.application._redraw()
                later = self.screen()
                self.assertIn(phase + ' · 0.3s', later)
                self.assertNotEqual(initial, later)
                self.assertNotIn('thinking', later)
            for phase in ('done', 'error', 'cancelled'):
                self.controller.on_event({'type': 'status', 'status': phase})
                await self.render()
                initial = self.screen()
                clock[0] += 2
                self.ui.application._redraw()
                self.assertEqual(initial, self.screen())

    async def test_server_usage_is_visible_separate_from_context_and_long_status_details(self):
        from david.context import UsageLedger
        turn, session = UsageLedger(), UsageLedger()
        turn.record({'prompt_tokens': 120, 'completion_tokens': 34, 'total_tokens': 154}, complete=True)
        session.record({'prompt_tokens': 700, 'completion_tokens': 80, 'total_tokens': 780}, complete=True)
        snapshot = {'turn': turn.snapshot(), 'session': session.snapshot(), 'context': {
            'unit': 'estimated_tokens', 'estimated_input_tokens': 321,
            'input_budget_tokens': 1000, 'reserved_output_tokens': 50}}
        self.controller.on_event({'type': 'status', 'status': 'done',
                                  'detail': 'Long status ' * 100, 'usage': snapshot})
        await self.render()
        screen = self.screen()
        self.assertIn('Q in:120 out:34', screen)
        self.assertIn('S total:780', screen)
        self.assertIn('tokens(server/live)', screen)
        self.assertIn('/usage', screen)
        self.assertIn('context ESTIMATE 321/1000', screen)
        self.assertIn('reserve 50', screen)
        self.assertNotIn('context ESTIMATE 780', screen)
        self.assertIn('❯', screen)

    async def test_partial_and_unknown_server_usage_survive_error_and_cancelled_status(self):
        from david.context import UsageLedger
        self.assertIn('Q in:? out:?', self.screen())
        self.assertIn('S in:? out:?', self.screen())
        ledger = UsageLedger()
        ledger.record({'prompt_tokens': 12, 'completion_tokens': 0}, complete=True)
        ledger.record(None, complete=False)
        snapshot = {'turn': ledger.snapshot(), 'session': ledger.snapshot()}
        for phase in ('error', 'cancelled'):
            self.controller.on_event({'type': 'status', 'status': phase, 'usage': snapshot,
                                      'detail': 'request interrupted'})
            await self.render()
            screen = self.screen()
            self.assertIn('Q in:12 out:0 partial', screen)
            self.assertIn('S in:12 out:0 partial', screen)
            self.assertNotIn('S total:', screen)
            self.assertIn('/usage', screen)
            self.assertIn('ERROR' if phase == 'error' else phase, screen)
        ledger.record({'total_tokens': 15}, complete=True)
        snapshot = {'turn': ledger.snapshot(), 'session': ledger.snapshot()}
        self.controller.on_event({'type': 'status', 'status': 'done', 'usage': snapshot})
        await self.render()
        self.assertIn('S total:15 partial', self.screen())

    async def test_usage_lifecycle_does_not_relabel_previous_question_as_current(self):
        from david.context import UsageLedger
        ledger = UsageLedger()
        ledger.record({'prompt_tokens': 81, 'completion_tokens': 9, 'total_tokens': 90}, complete=True)
        snapshot = {'turn': ledger.snapshot(), 'session': ledger.snapshot(), 'context': {
            'unit': 'characters', 'input_characters': 111, 'input_budget_characters': 1000}}
        self.controller.on_event({'type': 'status', 'status': 'done', 'usage': snapshot})
        await self.render()
        self.assertIn('Q in:81 out:9', self.screen())
        self.controller.on_event({'type': 'message', 'role': 'user', 'text': 'next question'})
        self.controller.on_event({'type': 'status', 'status': 'waiting', 'progress': {
            'usage': {'prompt_tokens': 999}, 'reasoning_chunks': 20}})
        await self.render()
        self.assertIn('Q in:? out:?', self.screen())
        self.assertIn('S total:90', self.screen())
        self.assertNotIn('Q in:81', self.screen())
        self.assertNotIn('999', self.screen())
        # /clear resets the viewport, not the actual live ledger. The controller's
        # following terminal event is authoritative and restores session accounting.
        self.controller.on_event({'type': 'reset'})
        await self.render()
        self.assertIn('S in:? out:?', self.screen())
        self.controller.on_event({'type': 'status', 'status': 'done', 'usage': snapshot})
        await self.render()
        self.assertIn('S total:90', self.screen())
        self.assertIn('context 111/1000 chars (not tokens)', self.screen())
        # New/resumed sessions replay messages but never restore historical usage.
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'user', 'text': 'replayed question'})
        self.controller.on_event({'type': 'session', 'id': 'resumed', 'saving': False})
        empty = UsageLedger().snapshot()
        self.controller.on_event({'type': 'status', 'status': 'done',
                                  'usage': {'turn': empty, 'session': empty}})
        await self.render()
        self.assertIn('Q in:? out:?', self.screen())
        self.assertIn('S in:? out:?', self.screen())
        self.assertNotIn('total:90', self.screen())

    async def test_context_method_is_reported_verbatim_not_inferred_from_token_counts(self):
        self.controller.on_event({'type': 'status', 'status': 'waiting', 'context': {
            'unit': 'estimated_tokens', 'estimated_input_tokens': 123,
            'input_budget_tokens': 1000, 'reserved_output_tokens': 50,
            'method': 'local tokenizer; server template unverified'}})
        await self.render()
        self.assertIn('context ESTIMATE 123/1000', self.screen())
        self.assertIn('method: local tokenizer; server template unverified', self.screen())
        self.assertIn('Q in:? out:?', self.screen())
        self.assertNotIn('Q in:123', self.screen())

    async def test_usage_chrome_keeps_composer_visible_in_tiny_viewports(self):
        from david.context import UsageLedger
        ledger = UsageLedger()
        ledger.record({'prompt_tokens': 120, 'completion_tokens': 34, 'total_tokens': 154}, complete=False)
        self.controller.on_event({'type': 'status', 'status': 'error',
                                  'detail': 'long error ' * 100,
                                  'usage': {'turn': ledger.snapshot(), 'session': ledger.snapshot()},
                                  'context': {'unit': 'estimated_tokens', 'estimated_input_tokens': 999,
                                              'input_budget_tokens': 1000, 'method': 'unverified ' * 50}})
        self.ui.composer.text = 'draft'
        try:
            for rows, columns in ((12, 38), (8, 38), (6, 20), (4, 20), (2, 20), (1, 20)):
                self.output.get_size = lambda r=rows, c=columns: Size(rows=r, columns=c)
                self.ui.application._on_resize()
                await self.render()
                self.assertIn('❯ draft', self.screen(), (rows, columns))
                self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
                if rows >= 8:
                    self.assertIn('Q in:120 out:34 partial', self.screen())
                if rows >= 2:
                    self.assertIn('Ctrl+D exit', self.screen())
        finally:
            self.output.get_size = lambda: Size(rows=24, columns=80)
            self.ui.application._on_resize()

    async def test_usage_details_command_remains_controller_owned_without_clearing_metrics(self):
        from david.context import UsageLedger
        ledger = UsageLedger()
        ledger.record({'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}, complete=True)
        self.controller.on_event({'type': 'status', 'status': 'done',
                                  'usage': {'turn': ledger.snapshot(), 'session': ledger.snapshot()}})
        await self.render()
        await self.send('/usage\r')
        self.assertEqual(self.controller.submissions, ['/usage'])
        self.assertIn('Q in:7 out:3', self.screen())
        self.assertIn('S total:10', self.screen())
        self.assertFalse(self.ui.input_history)

    async def test_context_occupancy_is_labeled_estimate_not_provider_usage(self):
        self.controller.on_event({'type': 'status', 'status': 'waiting', 'context': {
            'unit': 'estimated_tokens', 'estimated_input_tokens': 123,
            'input_budget_tokens': 1000, 'reserved_output_tokens': 50}})
        await self.render()
        text = self.screen()
        self.assertIn('ESTIMATE 123/1000', text)
        self.assertIn('reserve 50', text)
        self.assertNotIn('0 tokens used', text)

    async def test_keyboard_selection_copies_plain_subset_with_separate_key_and_ctrl_c_still_exits(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': '한글 reply\nsecond line'})
        await self.render()
        with patch('david.tui.copy_text') as copy:
            await self.send('/select\r')
            self.assertTrue(self.ui.selecting)
            copy.assert_not_called()
            await self.send('\x00\x1b[C\x1b[C\x02')
            copy.assert_called_once_with('한글')
            self.assertIn('Ctrl+B copy', self.ui.footer_text())
            self.assertFalse(self.controller.submissions)
            await self.send('\x03')
            self.assertEqual(await asyncio.wait_for(self.task, 3), 0)

    async def test_selection_escape_preserves_draft_and_approval_replaces_copy_view(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'reply'})
        await self.render()
        await self.send('/select\r')
        await self.send('\x1b')
        await asyncio.sleep(.6)
        self.assertFalse(self.ui.selecting)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        await self.send('/select\r')
        self.controller.on_event({'type': 'approval', 'action': 'write', 'detail': 'review'})
        await self.render()
        self.assertFalse(self.ui.selecting)
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.composer))
        await self.send('\r')
        self.assertFalse(self.controller.decisions)

    async def test_diff_numbers_follow_hunk_counts_even_for_header_looking_content(self):
        detail = '@@ -2,1 +3,1 @@\n--- old text\n+++ new text\n+outside hunk'
        self.controller.on_event({'type': 'approval', 'action': 'patch', 'detail': detail})
        await self.render()
        self.assertIn('2', ''.join(text for _, text in self.ui._diff_prefix(2, 0)))
        self.assertIn('3', ''.join(text for _, text in self.ui._diff_prefix(3, 0)))
        self.assertFalse(any(c.isdigit() for _, text in self.ui._diff_prefix(4, 0) for c in text))
        lex = self.ui.approval_panel.lexer.lex_document(self.ui.approval_panel.buffer.document)
        self.assertIn('diff.remove', lex(2)[0][0])
        self.assertIn('diff.add', lex(3)[0][0])

    async def test_selection_view_allows_slash_exit_without_touching_clipboard(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'reply'})
        await self.render()
        with patch('david.tui.copy_text') as copy:
            await self.send('/select\r')
            await self.send('/exit\r')
            self.assertTrue(self.task.done())
            copy.assert_not_called()
        self.assertFalse(self.controller.submissions)

    async def test_background_done_does_not_steal_keyboard_selection_focus(self):
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'last reply'})
        await self.render()
        await self.send('/select\r')
        self.controller.on_event({'type': 'status', 'status': 'done'})
        await self.render()
        self.assertTrue(self.ui.application.layout.has_focus(self.ui.selection_panel))

    async def test_error_styles_come_from_metadata_not_body_prefixes(self):
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'assistant', 'text': 'ERROR harmless quoted text'})
        self.controller.on_event({'type': 'tool', 'name': 'run_command', 'result': {'exit_code': 2}})
        self.controller.on_event({'type': 'status', 'status': 'error', 'detail': 'connection failed'})
        await self.render()
        self.assertIn('ERROR run_command', self.ui.transcript.text)
        self.assertIn('ERROR', self.screen())
        lex = self.ui.transcript.lexer.lex_document(self.ui.transcript.buffer.document)
        for row, text in enumerate(self.ui.transcript.buffer.document.lines):
            if text.startswith('ERROR harmless'):
                self.assertNotIn('error', lex(row)[0][0])
            if text.startswith('ERROR run_command'):
                self.assertIn('error', lex(row)[0][0])
        self.assertIn('error', self.ui.activity_text()[0][0])
        for theme in ('dark', 'light', 'mono'):
            from david.tui import THEMES
            style = Style.from_dict(THEMES[theme])
            error = style.get_attrs_for_style_str('class:error')
            self.assertTrue(error.bold)
            if theme != 'mono':
                self.assertNotEqual(error.color, style.get_attrs_for_style_str('').color)

    async def test_theme_covers_every_cell_and_full_wrapped_user_rows(self):
        from david.tui import THEMES
        self.controller.on_event({'type': 'reset'})
        self.controller.on_event({'type': 'message', 'role': 'user', 'text': '한글 ' * 30 + '\n\nshort'})
        for theme in ('dark', 'light', 'mono'):
            self.ui.theme = theme
            self.ui.application.style = Style.from_dict(THEMES[theme])
            await self.render()
            screen = self.ui.application.renderer.last_rendered_screen
            style = self.ui.application._merged_style
            base = style.get_attrs_for_style_str('')
            user = style.get_attrs_for_style_str('class:body.user')
            for y in range(24):
                for x in range(80):
                    attrs = style.get_attrs_for_style_str(screen.data_buffer[y][x].style)
                    if theme != 'mono':
                        self.assertIn(attrs.bgcolor, (base.bgcolor, user.bgcolor), (theme, x, y))
            info = self.ui.transcript.window.render_info
            for y, (line, _) in info.visible_line_to_row_col.items():
                if line < len(self.ui._line_prefixes) and 'role.user' in self.ui._line_prefixes[line][0]:
                    for x in range(80):
                        attrs = style.get_attrs_for_style_str(screen.data_buffer[info._y_offset + y][x].style)
                        self.assertEqual(attrs.bgcolor, user.bgcolor, (theme, line, x))
                        self.assertEqual(attrs.reverse, user.reverse, (theme, line, x))
