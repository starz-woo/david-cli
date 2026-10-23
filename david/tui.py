"""Full-screen terminal frontend. The controller owns all model/tool work."""
import asyncio
import json
import re

from bisect import bisect_right
from functools import lru_cache
from copy import deepcopy
from queue import Empty, SimpleQueue
from threading import Lock
from time import monotonic

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.scroll import scroll_page_down, scroll_page_up
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, Layout, Window)
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.widgets import Frame, TextArea

from .cli import safe_text
from .clipboard import ClipboardError, copy_text
from .file_refs import file_reference_prefix, quote_file_reference


_TRUNCATION_MARKER = '[앞부분 생략]\n'


THEMES = {
    'dark': {
        '': 'bg:#262624 #deded8', 'header': '#999991',
        'title': '#d99572 bold', 'working': '#d99572 bold',
        'changes': '#a6af98', 'hint': '#999991',
        'frame.border': '#676761', 'frame.label': '#d99572 bold',
        'role.user': 'bg:#353532 #eeeeea bold', 'body.user': 'bg:#353532 #eeeeea',
        'role.assistant': '#deded8', 'role.system': '#999991', 'role.tool': '#deded8 bold',
        'body.system': '#999991', 'body.tool': '#a9a9a1',
        'heading': '#eeeeea bold', 'code': '#c5c5bd', 'fence': '#999991',
        'diff.add': '#a6c58b', 'diff.remove': '#dc9490', 'diff.header': '#deded8 bold',
        'diff.context': '#b7b7ae', 'approval': '#d99572 bold', 'error': '#ff8b85 bold',
    },
    'light': {
        '': 'bg:#faf9f5 #343430', 'header': '#74746b',
        'title': '#a95432 bold', 'working': '#a95432 bold',
        'changes': '#506a3f', 'hint': '#74746b',
        'frame.border': '#b6b6ac', 'frame.label': '#a95432 bold',
        'role.user': 'bg:#eae9e2 #242420 bold', 'body.user': 'bg:#eae9e2 #242420',
        'role.assistant': '#343430', 'role.system': '#74746b', 'role.tool': '#343430 bold',
        'body.system': '#74746b', 'body.tool': '#67675d',
        'heading': '#242420 bold', 'code': '#505047', 'fence': '#74746b',
        'diff.add': '#3e6c2e', 'diff.remove': '#a23932', 'diff.header': '#343430 bold',
        'diff.context': '#626259', 'approval': '#a95432 bold', 'error': '#b42318 bold',
    },
    'mono': {
        'header': '', 'title': 'bold', 'working': 'bold', 'changes': '', 'hint': '',
        'frame.border': '', 'frame.label': 'bold', 'role.user': 'reverse bold',
        'body.user': 'reverse', 'role.assistant': '', 'role.system': 'italic', 'role.tool': 'bold',
        'body.system': '', 'body.tool': '', 'heading': 'bold', 'code': 'italic',
        'fence': '', 'diff.add': 'underline', 'diff.remove': 'italic',
        'diff.header': 'bold', 'approval': 'bold underline', 'error': 'bold underline',
    },
}


def diff_rows(lines):
    """Styles and real old/new positions, bounded by each unified hunk."""
    old = new = old_left = new_left = 0
    result = []
    for text in lines:
        left = right = ''
        match = re.match(r'^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@', text)
        if match:
            old, count_old, new, count_new = match.groups()
            old, new = int(old), int(new)
            old_left, new_left = int(count_old or 1), int(count_new or 1)
            style = 'class:diff.header'
        elif text.startswith('-') and old_left:
            style, left = 'class:diff.remove', str(old)
            old, old_left = old + 1, old_left - 1
        elif text.startswith('+') and new_left:
            style, right = 'class:diff.add', str(new)
            new, new_left = new + 1, new_left - 1
        elif text.startswith(' ') and old_left and new_left:
            style, left, right = 'class:diff.context', str(old), str(new)
            old, new = old + 1, new + 1
            old_left, new_left = old_left - 1, new_left - 1
        else:
            if not text.startswith('\\'):
                old_left = new_left = 0
            style = ('class:diff.header' if text.startswith(('+++', '---', '@@', 'diff --git ', 'index ')) else
                     'class:diff.add' if text.startswith('+') else
                     'class:diff.remove' if text.startswith('-') else 'class:diff.context')
        result.append((style, left, right))
    return result


class DiffLexer(Lexer):
    def lex_document(self, document):
        rows = diff_rows(document.lines)
        return lambda number: [(rows[number][0], document.lines[number])]



class TranscriptLexer(Lexer):
    """Styles come from trusted entry metadata, never role-looking body text."""

    def __init__(self, styles):
        self.styles = tuple(styles)

    def lex_document(self, document):
        def line(number):
            style = self.styles[number] if number < len(self.styles) else ''
            return [(style, document.lines[number])]
        return line


class TranscriptWindow(Window):
    """Scroll in rendered rows, independently of focus and the text cursor.

    A logical line can occupy many screens. Window's default cursor-based
    scrolling cannot preserve a reader's position inside that line.
    """

    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self._content = None
        self._document = Document('')
        self._entry_starts = []
        self._width = 1
        self._height = 1
        self._heights = {}

    @staticmethod
    @lru_cache(maxsize=4096)
    def _cell_width(character):
        # Match the renderer, including visible tab/control representations.
        return Char(character, '').width

    def _line_starts(self, row):
        if row not in self._heights:
            # Summed widths undercount rows when a wide glyph wraps with one
            # cell left. Match the renderer and retain character anchors.
            assert self._content is not None
            prefix = fragment_list_to_text(to_formatted_text(self.get_line_prefix(row, 0))) if self.get_line_prefix else ''
            prefix_width = sum(self._cell_width(c) for c in prefix)
            column, index, starts = prefix_width, 0, [0]
            for _, text, *_ in self._content.get_line(row):
                for character in text:
                    width = self._cell_width(character)
                    if column + width > self._width:
                        starts.append(index)
                        column = prefix_width
                    column += width
                    index += 1
            self._heights[row] = starts
        return self._heights[row]

    def _line_height(self, row):
        return len(self._line_starts(row))

    def _move(self, position, delta):
        row, offset = position
        offset += delta
        while offset < 0 and row > 0:
            row -= 1
            offset += self._line_height(row)
        while row < self._content.line_count - 1 and offset >= self._line_height(row):
            offset -= self._line_height(row)
            row += 1
        return row, max(0, min(offset, self._line_height(row) - 1))

    def _bottom(self):
        last = self._content.line_count - 1
        return self._move((last, self._line_height(last) - 1), -(self._height - 1))

    def _scroll(self, ui_content, width, height):
        anchor = None
        row = self.vertical_scroll
        if not self.owner.follow_output and self._content is not None:
            starts = self._line_starts(min(row, self._content.line_count - 1))
            anchor = starts[min(self.vertical_scroll_2, len(starts) - 1)]
            # The old rendered row belongs to an entry, not a permanent buffer
            # index. Eviction (and its notice) shifts all surviving entries.
            for entry, start, removed, body_start in reversed(self._entry_starts):
                if start <= row:
                    new_start = next((line for item, line, _, _body in self.owner._entry_starts
                                      if item is entry), None)
                    if new_start is None:
                        # The reader's entry expired: stay paused at the oldest
                        # retained output instead of jumping towards the tail.
                        row, anchor = 0, 0
                    else:
                        delta = entry.get('_text_offset', 0) - removed
                        if delta and row >= body_start:
                            # Offsets count characters, including newlines and the
                            # inserted marker, not logical lines or display cells.
                            offset = (self._document.translate_row_col_to_index(row, anchor)
                                      - self._document.translate_row_col_to_index(body_start, 0)
                                      - delta)
                            if offset < len(_TRUNCATION_MARKER):
                                row, anchor = 0, 0
                            else:
                                document = self.owner.transcript.buffer.document
                                new_body = next(body for item, _, _, body in self.owner._entry_starts if item is entry)
                                index = document.translate_row_col_to_index(new_body, 0) + offset
                                row, anchor = document.translate_index_to_position(index)
                        else:
                            row += new_start - start
                    break
        self._content = ui_content
        self._document = self.owner.transcript.buffer.document
        self._entry_starts = self.owner._entry_starts
        self._heights.clear()
        self._width, self._height = max(1, width), max(1, height)
        self.vertical_scroll = min(row, ui_content.line_count - 1)
        if anchor is not None:
            starts = self._line_starts(self.vertical_scroll)
            self.vertical_scroll_2 = max(0, bisect_right(starts, anchor) - 1)
        self.horizontal_scroll = 0
        bottom = self._bottom()
        if self.owner.follow_output:
            position = bottom
        else:
            row = min(self.vertical_scroll, ui_content.line_count - 1)
            position = min((row, min(self.vertical_scroll_2, self._line_height(row) - 1)), bottom)
        self.vertical_scroll, self.vertical_scroll_2 = position

    def _copy_body(self, ui_content, new_screen, write_position, move_x, width, *args, **kwargs):
        mapping, positions = super()._copy_body(
            ui_content, new_screen, write_position, move_x, width, *args, **kwargs)
        # Lexers style glyphs only. Paint the entire physical row, including
        # empty lines, wide-character filler cells and soft-wrap remainders.
        for y, (line, _) in mapping.items():
            if not 0 <= y < write_position.height:
                continue
            prefixes = self.owner._line_prefixes
            if line < len(prefixes) and prefixes[line][0] == 'class:role.user':
                row = new_screen.data_buffer[write_position.ypos + y]
                for x in range(write_position.xpos + move_x, write_position.xpos + move_x + width):
                    cell = row[x]
                    row[x] = Char(cell.char, 'class:body.user ' + cell.style)
        return mapping, positions

    def scroll_rows(self, amount):
        if self._content is None:
            return
        position = self._move((self.vertical_scroll, self.vertical_scroll_2), amount)
        bottom = self._bottom()
        if amount > 0 and position >= bottom:
            self.owner.return_to_latest()
        else:
            self.owner.follow_output = False
            self.vertical_scroll, self.vertical_scroll_2 = min(position, bottom)
            # Cursor is only a selection/navigation aid, never the follow state.
            document = self.owner.transcript.buffer.document
            self.owner.transcript.buffer.cursor_position = document.translate_row_col_to_index(
                self.vertical_scroll, min(len(document.lines[self.vertical_scroll]),
                                          self._line_starts(self.vertical_scroll)[self.vertical_scroll_2]))
        self.owner.application.invalidate()

    def _scroll_up(self):
        self.scroll_rows(-3)

    def _scroll_down(self):
        self.scroll_rows(3)


class WorkspaceCompleter(Completer):
    """Never walk the filesystem: the controller supplies already-allowed paths."""

    def __init__(self, controller):
        self.controller = controller

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        reference = file_reference_prefix(before)
        if reference is not None:
            length, prefix = reference
            for path in self.controller.files:
                if safe_text(path) != path or any(c in path for c in '\n\t'):
                    continue
                if path.startswith(prefix):
                    yield Completion(quote_file_reference(path), start_position=-length, display=path)
        elif before.startswith('/') and not any(c.isspace() for c in before):
            for command in dict.fromkeys([*self.controller.commands, '/copy', '/select']):
                if command.startswith(before) and safe_text(command) == command:
                    yield Completion(command, start_position=-len(before))


class DavidTUI:
    """An injectable prompt_toolkit application for the controller protocol."""

    def __init__(self, controller, input=None, output=None):
        self.controller = controller
        preferences = getattr(controller, 'ui', {})
        if not isinstance(preferences, dict):
            preferences = {}
        theme = preferences.get('theme', 'dark')
        self.theme = theme if isinstance(theme, str) and theme in THEMES else 'dark'
        height = preferences.get('input_height', 5)
        if type(height) is not int or not 1 <= height <= 20:
            height = 5
        self._events = SimpleQueue()
        self._event_lock = Lock()
        self._approval_epoch = 0
        self._shown_approval_epoch = None
        self._entries = []
        self._status_entry = {'role': 'system', 'text': ''}
        self._entry_starts = []
        self._line_prefixes = []
        self._trimmed = False
        self.changes = []
        self.show_tool_details = False
        self._stream_index = None
        self.follow_output = True
        self.unseen_output = False
        self.approval = None
        self.approval_choice = False
        self.approval_menu = Window(FormattedTextControl(self.approval_menu_text, focusable=True), height=1)
        self._diff_numbers = []
        self.approval_panel = TextArea(read_only=True, scrollbar=True, wrap_lines=True, lexer=DiffLexer(),
                                       get_line_prefix=self._diff_prefix,
                                       height=Dimension(min=1, preferred=7, max=12))
        self.status = 'idle'
        self._phase_started = monotonic()
        self.context_usage = {}
        self.server_usage = {}
        self.detail = ''
        self._closing = False
        self._stopping = False
        self._submitting = False
        self._composer_revision = 0
        self.input_history = []
        self._history_index = None
        self._history_draft = Document('')
        self._last_reply = None
        self._copying = False
        self._cancel_requested = False
        self.session_id = controller.session_id
        self.saving = controller.saving
        self.selecting = False
        self.selection_panel = TextArea(read_only=True, wrap_lines=True, scrollbar=True)
        self.transcript = TextArea(read_only=True, wrap_lines=True)
        self.transcript.window = TranscriptWindow(
            self, content=self.transcript.control, wrap_lines=True,
            right_margins=self.transcript.window.right_margins,
            get_line_prefix=self._transcript_prefix, height=Dimension(min=1))
        self.completer = WorkspaceCompleter(controller)
        self.composer = TextArea(multiline=True, wrap_lines=True,
                                 completer=self.completer, complete_while_typing=False,
                                 height=Dimension(min=1, max=height), dont_extend_height=True,
                                 get_line_prefix=lambda line, wrap: [('class:hint', '❯ ' if line == 0 and wrap == 0 else '  ')])
        bindings = KeyBindings()
        editing = has_focus(self.composer)
        self.composer.buffer.on_text_changed += self._sanitize_composer

        @bindings.add('enter', filter=editing)
        @bindings.add('c-s', filter=editing)
        def submit(event):
            self.submit()

        history = editing & Condition(lambda: self.approval is None and
                                      self.composer.buffer.complete_state is None)

        @bindings.add('up', filter=history & Condition(lambda: self._composer_boundary(-1)))
        def history_previous(event):
            self._recall_history(-1)

        @bindings.add('down', filter=history & Condition(lambda: self._composer_boundary(1)))
        def history_next(event):
            self._recall_history(1)

        @bindings.add('escape', 'enter', filter=editing)
        @bindings.add('c-j', filter=editing)
        def newline(event):
            self.composer.buffer.insert_text('\n')

        @bindings.add(Keys.BracketedPaste, filter=editing)
        def paste(event):
            text = event.data.replace('\r\n', '\n').replace('\r', '\n')
            self.composer.buffer.insert_text(safe_text(text))

        @bindings.add('tab')
        def tab(event):
            buffer = self.composer.buffer
            if editing() and self.approval is None:
                if buffer.complete_state:
                    buffer.complete_next()
                    return
                completions = list(self.completer.get_completions(buffer.document, CompleteEvent()))
                if len(completions) == 1:
                    buffer.apply_completion(completions[0])
                    return
                if completions:
                    buffer.start_completion(select_first=True)
                    return
            self.application.layout.focus_next()

        @bindings.add('s-tab')
        def previous_focus(event):
            self.application.layout.focus_previous()

        @bindings.add('c-o')
        def toggle_tools(event):
            self.show_tool_details = not self.show_tool_details
            self._render_transcript()

        @bindings.add('escape', filter=Condition(lambda: self.selecting), eager=True)
        def leave_selection(event):
            self.selecting = False
            self.application.layout.focus(self.composer)

        @bindings.add('/', filter=has_focus(self.selection_panel))
        def selection_command(event):
            self.selecting = False
            self.application.layout.focus(self.composer)
            self.composer.buffer.insert_text('/')

        @bindings.add('c-b', filter=has_focus(self.selection_panel))
        def copy_selection(event):
            if not self._copying:
                buffer = self.selection_panel.buffer
                if buffer.selection_state:
                    text = buffer.copy_selection().text
                    if text:
                        self._copying = True
                        self.application.create_background_task(self._copy(text))
                else:
                    self.detail = 'Ctrl+Space starts a selection; arrows extend it; Ctrl+B copies'

        @bindings.add('pageup')
        def page_up(event):
            if self.selecting and self.application.layout.has_focus(self.selection_panel):
                scroll_page_up(event)
            elif self.approval is not None:
                self._scroll_approval(event, scroll_page_up)
            else:
                window = self.transcript.window
                window.scroll_rows(-max(1, window._height - 1))

        @bindings.add('pagedown')
        def page_down(event):
            if self.selecting and self.application.layout.has_focus(self.selection_panel):
                scroll_page_down(event)
            elif self.approval is not None:
                self._scroll_approval(event, scroll_page_down)
            else:
                window = self.transcript.window
                window.scroll_rows(max(1, window._height - 1))

        @bindings.add('c-end')
        def latest(event):
            if self.selecting:
                self.selection_panel.buffer.cursor_position = len(self.selection_panel.text)
            else:
                self.return_to_latest()

        @bindings.add('c-c', eager=True)
        def cancel(event):
            self.cancel()

        @bindings.add('c-d')
        @bindings.add('c-q')
        def quit(event):
            self.request_close()

        menu_focus = has_focus(self.approval_menu)
        menu_navigation = Condition(lambda: self.approval is not None) & (
            menu_focus | (editing & Condition(lambda: not self.composer.text)))

        @bindings.add('up', filter=menu_navigation)
        @bindings.add('left', filter=menu_navigation)
        def choose_allow(event):
            self.approval_choice = True
            self.application.layout.focus(self.approval_menu)

        @bindings.add('down', filter=menu_navigation)
        @bindings.add('right', filter=menu_navigation)
        def choose_deny(event):
            self.approval_choice = False
            self.application.layout.focus(self.approval_menu)

        @bindings.add('enter', filter=menu_focus)
        def decide_selected(event):
            self._decide(self.approval_choice)

        @bindings.add(Keys.Any, filter=menu_focus)
        def menu_typing(event):
            # Slash exits remain available even after navigating the menu.
            self.application.layout.focus(self.composer)
            self.composer.buffer.insert_text(safe_text(event.data))

        @bindings.add('escape', filter=Condition(lambda: self.approval is not None), eager=True)
        def escape_deny(event):
            self._decide(False)

        @bindings.add('c-y', filter=Condition(lambda: self.approval is not None))
        def approve(event):
            self._decide(True)

        @bindings.add('c-n', filter=Condition(lambda: self.approval is not None))
        def deny(event):
            self._decide(False)

        transcript_view = DynamicContainer(lambda: self.selection_panel if self.selecting else self.transcript)
        normal = HSplit([
                Window(FormattedTextControl(self.banner_text), height=3),
                transcript_view,
                ConditionalContainer(
                    Frame(HSplit([self.approval_panel, self.approval_menu]),
                          title='Approval · ↑↓ Enter · Esc deny · Ctrl+Y / Ctrl+N',
                          style='class:approval'),
                    filter=Condition(lambda: self.approval is not None)),
                ConditionalContainer(
                    Window(FormattedTextControl(self.changes_text), height=1, style='class:changes'),
                    Condition(lambda: bool(self.changes))),
                ConditionalContainer(
                    Window(FormattedTextControl(self.activity_text), wrap_lines=True,
                           height=Dimension(min=1, max=3), dont_extend_height=True),
                    Condition(lambda: self.status != 'idle' or bool(self.detail))),
                Window(FormattedTextControl(self.usage_text), wrap_lines=True,
                       height=Dimension(min=1, max=4), dont_extend_height=True),
                Window(height=1, char='─', style='class:frame.border'),
                self.composer,
                Window(height=1, char='─', style='class:frame.border'),
                Window(FormattedTextControl(self.footer_text), height=1, style='class:hint'),
            ])
        compact = HSplit([
            ConditionalContainer(Window(FormattedTextControl(self.header_text), height=1,
                                        style='class:header'),
                                 Condition(lambda: self._size().rows >= 6)),
            ConditionalContainer(transcript_view,
                                 Condition(lambda: self._size().rows >= 4 and self.approval is None)),
            ConditionalContainer(self.approval_panel, Condition(lambda: self.approval is not None
                                                                and self._size().rows >= 3)),
            ConditionalContainer(self.approval_menu, Condition(lambda: self.approval is not None
                                                               and self._size().rows >= 4)),
            ConditionalContainer(Window(FormattedTextControl(self.activity_text), height=1),
                                 Condition(lambda: self._size().rows >= 6 and
                                           (self.status != 'idle' or bool(self.detail)))),
            ConditionalContainer(
                Window(FormattedTextControl(self.usage_text), wrap_lines=True,
                       height=Dimension(min=1, max=3), dont_extend_height=True),
                Condition(lambda: self._size().rows >= 8)),
            self.composer,
            ConditionalContainer(Window(FormattedTextControl(self.footer_text), height=1),
                                 Condition(lambda: self._size().rows >= 2)),
        ])
        root = FloatContainer(
            DynamicContainer(lambda: compact if self._size().rows < 16 or self._size().columns < 48 else normal),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=7))],
        )
        self.application = Application(
            layout=Layout(root, focused_element=self.composer),
            key_bindings=bindings, full_screen=True, mouse_support=True,
            input=input, output=output, before_render=self._drain_events,
            style=Style.from_dict(THEMES[self.theme]),
            refresh_interval=.1,
        )
        controller.on_event = self.handle_event
        self.handle_event({'type': 'message', 'role': 'system', 'text':
            '코드·프롬프트·도구 출력 → 설정된 서버로 전송. 명령 실행은 OS 샌드박스가 아닙니다.\n'
            '저장 OFF (기본). /save on: 대화·코드·undo 원본을 평문 저장.'})

    def _size(self):
        if not hasattr(self, 'application'):
            # Layout walks dynamic containers before Application has been assigned.
            from prompt_toolkit.data_structures import Size
            return Size(rows=24, columns=80)
        return self.application.output.get_size()

    def _identity_components(self):
        # Show identity, not a potentially screen-wide absolute Windows path.
        workspace = str(self.controller.workspace).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
        workspace = ' '.join(safe_text(workspace).split()) or '/'
        model = ' '.join(safe_text(self.controller.model_name).split())
        return model, workspace

    def header_text(self):
        model, workspace = self._identity_components()
        return f'  David  ·  {model}  ·  {workspace}'

    def banner_text(self):
        model, workspace = self._identity_components()
        return [('class:title', '  David\n'),
                ('class:hint', f'  {model} · {workspace}\n')]

    def activity_text(self):
        # Only real controller state, never invented thinking time/token counts.
        active = self.status not in ('idle', 'done', 'cancelled', 'error', 'approval')
        if self.status == 'error':
            return [('class:error', '  ERROR'), ('class:error', ' · ' + safe_text(self.detail))]
        elapsed = max(0, monotonic() - self._phase_started)
        # The clock animates the emitted phase; it never infers reasoning.
        animated = self.status in ('connecting', 'waiting', 'thinking', 'generating', 'tool',
                                   'reading', 'editing', 'running')
        prefix = '✳ ' if active else '  '
        timer = f' · {elapsed:.1f}s ' + '✳✶✻✽'[int(elapsed * 8) % 4] if animated else ''
        return [('class:working' if active else 'class:hint', prefix + self.status + timer),
                ('class:hint', safe_text(' · ' + self.detail) if self.detail else '')]

    def usage_text(self):
        # Server accounting spans requests; context occupancy is a separate measure.
        turn = self.server_usage.get('turn', {}).get('tokens', {})
        session = self.server_usage.get('session', {}).get('tokens', {})
        def count(tokens, key):
            value = tokens.get(key)
            return '?' if value is None else str(value)
        question = f"Q in:{count(turn, 'prompt_tokens')} out:{count(turn, 'completion_tokens')}"
        cumulative = (f"S total:{count(session, 'total_tokens')}" if session.get('total_tokens') is not None
                      else f"S in:{count(session, 'prompt_tokens')} out:{count(session, 'completion_tokens')}")
        if self.server_usage.get('turn', {}).get('partial'):
            question += ' partial'
        if self.server_usage.get('session', {}).get('partial'):
            cumulative += ' partial'
        text = question + ' · ' + cumulative + ' · tokens(server/live) · /usage'
        context = self.context_usage
        occupancy = ''
        if context.get('unit') == 'estimated_tokens':
            occupancy = (f"\ncontext ESTIMATE {context.get('estimated_input_tokens', '?')}/"
                         f"{context.get('input_budget_tokens', '?')} tokens"
                         f" · output reserve {context.get('reserved_output_tokens', '?')}")
        elif context.get('unit') == 'characters':
            occupancy = (f"\ncontext {context.get('input_characters', '?')}/"
                         f"{context.get('input_budget_characters', '?')} chars (not tokens)")
        if context.get('method'):
            occupancy += '\nmethod: ' + ' '.join(safe_text(context['method']).split())
        return [('class:hint', safe_text(text + occupancy))]

    def footer_text(self):
        storage = 'saving ON (평문)' if self.saving else '저장 OFF'
        # Keep an exit route visible even when the rest is clipped on narrow terminals.
        exit_hint = 'Ctrl+D exit'
        if self.selecting:
            return exit_hint + ' · Ctrl+Space mark · arrows select · Ctrl+B copy · Esc back'
        if not self.follow_output:
            return exit_hint + ' · Paused · ' + ('new output · ' if self.unseen_output else '') + 'Ctrl+End latest · ' + storage
        if self._cancel_requested:
            cancel_hint = 'Ctrl+C again to exit'
        elif self.controller.busy or self._submitting or self.approval is not None:
            cancel_hint = 'Ctrl+C stop'
        else:
            cancel_hint = 'Ctrl+C exit'
        return exit_hint + ' · ' + cancel_hint + ' · /help · ' + storage

    def _scroll_approval(self, event, scroll):
        focused = self.application.layout.current_control
        self.application.layout.focus(self.approval_panel)
        try:
            scroll(event)
        finally:
            self.application.layout.focus(focused)

    def _diff_prefix(self, line, wrap):
        if self._size().columns < 48 or line >= len(self._diff_numbers):
            return []
        text = self._diff_numbers[line]
        return [('class:hint', ' ' * len(text) if wrap else text)]

    def _number_diff(self, text):
        self._diff_numbers = [f'{left:>4} {right:>4} │ '
                              for _, left, right in diff_rows(text.split('\n'))]

    def approval_menu_text(self):
        return [('class:approval' + (' reverse' if self.approval_choice else ''),
                 ('❯ ' if self.approval_choice else '  ') + 'Allow once'),
                ('', '    '),
                ('class:approval' + (' reverse' if not self.approval_choice else ''),
                 ('❯ ' if not self.approval_choice else '  ') + 'Deny')]

    def changes_text(self):
        if not self.changes:
            return 'Changes: none'
        items = []
        for change in self.changes:
            counts = ''
            if 'added' in change:
                counts += f" +{change['added']}"
            if 'removed' in change:
                counts += f" -{change['removed']}"
            action = change.get('action', change.get('status', ''))
            items.append(f"{change.get('path', '?')} {action}{counts}")
        return safe_text('Changes: ' + ' | '.join(items))

    def status_text(self):
        paused = ('Paused · ' + ('new output · ' if self.unseen_output else '') + 'Ctrl+End latest | ') if not self.follow_output else ''
        detail = (self.detail + ' | ') if self.detail else ''
        storage = 'saving ON (평문)' if self.saving else '저장 OFF'
        return safe_text('  ' + paused + f'{self.status} | ' + detail + storage)

    def return_to_latest(self):
        self.follow_output = True
        self.unseen_output = False
        self.transcript.buffer.cursor_position = len(self.transcript.text)
        self.application.invalidate()

    def cancel(self):
        if self._cancel_requested:
            self.request_close()
        elif self.controller.busy or self._submitting or self.approval is not None:
            self._cancel_requested = True
            self.approval = None
            self.application.layout.focus(self.composer)
            self.status = 'stopping'
            self.detail = 'Cancellation requested; Ctrl+C again or Ctrl+D to exit safely'
            self.controller.cancel()
        else:
            self.request_close()

    def request_close(self):
        if self._closing:
            return
        self._closing = True
        self._stopping = True
        self.approval = None
        self.status = 'stopping'
        self.detail = 'Cancelling and waiting for active work; no tools are killed'
        self.controller.cancel()
        self.application.create_background_task(self._close())

    async def _close(self):
        try:
            while self._submitting:
                await asyncio.sleep(.02)
            # A submit in flight may have launched work after the first cancel.
            self.controller.cancel()
            while True:
                stopped = await asyncio.to_thread(self.controller.close, timeout=0)
                if stopped:
                    self.application.exit(result=0)
                    return
                self.status = 'stopping'
                self.detail = 'Still stopping; waiting safely for active work to finish'
                self.application.invalidate()
                await asyncio.sleep(.2)
        except Exception as exc:
            self.status = 'error'
            self.detail = safe_text(exc) + '; Ctrl+D or Ctrl+Q retries shutdown'
        finally:
            self._closing = False
            self.application.invalidate()

    def submit(self):
        text = safe_text(self.composer.text)
        if text in ('/quit', '/exit'):
            self.request_close()
            return
        if text == '/select':
            self.composer.text = ''
            if self.approval is not None:
                self.detail = 'Finish the approval before opening selection view'
            elif self._last_reply is None:
                self.detail = 'No finalized assistant reply available to select'
            else:
                self.selecting = True
                self.selection_panel.buffer.set_document(Document(self._last_reply, 0), bypass_readonly=True)
                self.application.layout.focus(self.selection_panel)
            return
        if text == '/copy':
            self.composer.text = ''
            self.copy_reply()
            return
        if self.approval is not None:
            if text in ('yes', 'no'):
                self._decide(text == 'yes')
            else:
                self.detail = '↑↓ to select Allow once / Deny, Enter to confirm; Esc denies. Or type yes / no.'
            return
        if not self._stopping and not self._submitting and text.strip():
            self.return_to_latest()
            self._cancel_requested = False
            self._submitting = True
            self.application.create_background_task(self._submit(text, self._composer_revision))

    async def _submit(self, text, revision):
        try:
            accepted = await asyncio.to_thread(self.controller.submit, text)
            if accepted and not text.lstrip().startswith('/') and len(text) <= 100000:
                self.input_history.append(text)
                while len(self.input_history) > 100 or sum(map(len, self.input_history)) > 100000:
                    self.input_history.pop(0)
                self._history_index = None
            if accepted and self._cancel_requested:
                self.controller.cancel()
            if accepted and revision == self._composer_revision and self.approval is None:
                self.composer.text = ''
        except (Exception, KeyboardInterrupt) as exc:
            self.status = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'
            self.detail = safe_text(exc)
        finally:
            self._submitting = False
            self.application.invalidate()

    def copy_reply(self):
        if self._copying:
            return
        if self._last_reply is None:
            self.detail = 'No finalized assistant reply available to copy'
            return
        self._copying = True
        self.application.create_background_task(self._copy(self._last_reply))

    async def _copy(self, text):
        try:
            await asyncio.to_thread(copy_text, text)
            self.detail = 'Copied plain text to the native clipboard'
        except ClipboardError as exc:
            self.detail = ('Copy failed: ' + safe_text(exc) +
                           '. Terminal Shift+mouse selection may work (terminal-dependent).')
        finally:
            self._copying = False
            self.application.invalidate()

    def _composer_boundary(self, direction):
        # Match physical wrapping, including a two-cell prompt and wide glyphs.
        # Do not consume Up/Down as history in the middle of a visual document.
        document = self.composer.buffer.document
        width = max(3, self._size().columns)
        starts, column = [0], 2
        for index, character in enumerate(document.text + ' '):
            if character == '\n':
                starts.append(index + 1)
                column = 2
                continue
            cells = TranscriptWindow._cell_width(character)
            if column + cells > width:
                starts.append(index)
                column = 2
            column += cells
        visual_row = bisect_right(starts, document.cursor_position) - 1
        return visual_row == (0 if direction < 0 else len(starts) - 1)

    def _recall_history(self, direction):
        if not self.input_history:
            return
        if self._history_index is None:
            if direction > 0:
                return
            self._history_draft = self.composer.buffer.document
            self._history_index = len(self.input_history)
        self._history_index = max(0, min(len(self.input_history), self._history_index + direction))
        if self._history_index == len(self.input_history):
            self.composer.buffer.document = self._history_draft
            self._history_index = None
        else:
            text = self.input_history[self._history_index]
            self.composer.buffer.document = Document(text, 0 if direction < 0 else len(text))

    def _decide(self, decision):
        with self._event_lock:
            current = self._shown_approval_epoch == self._approval_epoch
        if self.approval is None or not current:
            return
        self.approval = None
        self.composer.text = ''
        self.status = 'running'
        self.detail = 'Approved' if decision else 'Denied'
        self.controller.respond_approval(decision)
        self.application.layout.focus(self.composer)

    def _sanitize_composer(self, buffer):
        self._composer_revision += 1
        text = safe_text(buffer.text)
        if text != buffer.text:
            cursor = len(safe_text(buffer.document.text_before_cursor))
            buffer.document = Document(text, cursor)

    def handle_event(self, event):
        """Safe for controller worker threads; widgets mutate only during render."""
        event = deepcopy(event)
        with self._event_lock:
            if event.get('type') in ('approval', 'reset') or (
                event.get('type') == 'status' and event.get('status') in ('cancelled', 'error', 'done', 'idle')
            ):
                self._approval_epoch += 1
            event['_approval_epoch'] = self._approval_epoch
            self._events.put(event)
        self.application.invalidate()

    def _drain_events(self, application):
        changed = False
        # Keep keyboard/cancellation responsive during a burst. Preserve FIFO,
        # including approvals and resets; never discard control events.
        for _ in range(128):
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            changed = True
            kind = event.get('type')
            if kind == 'reset':
                self.follow_output = True
                self.unseen_output = False
                self._entries.clear()
                self._last_reply = None
                self.selecting = False
                self.selection_panel.buffer.set_document(Document(''), bypass_readonly=True)
                self.context_usage = {}
                self.server_usage = {}
                self._history_index = None
                self._trimmed = False
                self.changes = []
                self._stream_index = None
                self.approval = None
                self.approval_panel.buffer.set_document(Document(''), bypass_readonly=True)
                self.composer.text = ''
                self.show_tool_details = False
                self.status = 'idle'
                self.detail = ''
                self.application.layout.focus(self.composer)
            elif kind == 'text':
                if self._stream_index is None:
                    self._stream_index = len(self._entries)
                    self._entries.append({'role': 'assistant', 'text': ''})
                self._entries[self._stream_index]['text'] += safe_text(event.get('text', ''))
            elif kind == 'message':
                role = safe_text(event.get('role', 'message'))
                text = safe_text(event.get('text', ''))
                if role == 'user':
                    # The controller emits this before each accepted prompt/retry.
                    # Do not let the previous question masquerade as the new one.
                    self.server_usage = {'session': self.server_usage.get('session', {})}
                    self.context_usage = {}
                if role == 'assistant':
                    # Keep a bounded full reply, never silently copy a truncated one.
                    self._last_reply = text if len(text) <= 1000000 else None
                if role == 'assistant' and self._stream_index is not None:
                    self._entries[self._stream_index]['text'] = text
                    self._entries[self._stream_index]['_text_offset'] = 0
                else:
                    self._entries.append({'role': role, 'text': text})
                self._stream_index = None
            elif kind == 'tool':
                name = safe_text(event.get('name', 'tool'))
                result = event.get('result', {})
                failed = result.get('error') or result.get('ok') is False or result.get('timed_out') or result.get('exit_code', 0) != 0
                outcome = 'error' if failed else 'done'
                details = '\n'.join(
                    f'{key}:\n{value}' if isinstance(value, str) and '\n' in value
                    else f'{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)}'
                    for key, value in result.items())
                self._entries.append({'role': 'tool ' + name, 'text': outcome,
                                      'details': safe_text(details), 'error': bool(failed)})
                self._stream_index = None
            elif kind == 'changes':
                self.changes = event.get('changes', [])
            elif kind == 'summary':
                self._entries.append({'role': 'summary', 'text': safe_text(event.get('text', ''))})
                # Some controllers deliver the final assistant message after metrics.
                # Keep its streaming slot until that message or the next turn arrives.
            elif kind == 'approval' and not self._stopping and not self._cancel_requested:
                self.selecting = False
                self.approval = event
                self.approval_choice = False
                self._shown_approval_epoch = event['_approval_epoch']
                self.status = 'approval'
                self.detail = 'Review the action before deciding'
                text = safe_text(str(event.get('action', '')) + '\n' + str(event.get('detail', '')))
                self._number_diff(text)
                self.approval_panel.buffer.set_document(Document(text, 0), bypass_readonly=True)
                self.composer.text = ''
                self.application.layout.focus(self.composer)
            elif kind == 'status' and not self._stopping:
                status = safe_text(event.get('status', 'idle'))
                if status != self.status:
                    self._phase_started = monotonic()
                self.status = status
                if isinstance(event.get('usage'), dict):
                    self.server_usage = event['usage']
                    if isinstance(self.server_usage.get('context'), dict):
                        self.context_usage = self.server_usage['context']
                if isinstance(event.get('context'), dict):
                    self.context_usage = event['context']
                self.detail = safe_text(event.get('detail', ''))
                if self.status in ('cancelled', 'error', 'done', 'idle'):
                    if self.approval is not None:
                        self.application.layout.focus(self.composer)
                    self.approval = None
            elif kind == 'session':
                self.session_id = safe_text(event.get('id', ''))
                self.saving = bool(event.get('saving', False))
            elif kind == 'input' and self.approval is None:
                text = safe_text(event.get('text', ''))
                self.composer.buffer.document = Document(text, len(text))
        if changed:
            self._render_transcript()
        if not self._events.empty():
            self.application.invalidate()

    def _transcript_prefix(self, line, wrap_count):
        if line < len(self._line_prefixes):
            style, text = self._line_prefixes[line]
            return [(style, ' ' * len(text) if wrap_count else text)]
        return [('', '  ')]

    def _render_transcript(self):
        # Bound both retained data and rendering cost on long coding sessions.
        for entry in self._entries:
            for key in ('text', 'details'):
                if len(entry.get(key, '')) > 100000:
                    if key == 'text':
                        entry['_text_offset'] = (entry.get('_text_offset', 0)
                                                 + len(entry[key]) - 99980 - len(_TRUNCATION_MARKER))
                    entry[key] = _TRUNCATION_MARKER + entry[key][-99980:]
                    self._trimmed = True
        size = sum(len(e.get('text', '')) + len(e.get('details', '')) + 100 for e in self._entries)
        while self._entries and (size > 200000 or len(self._entries) > 300):
            removed = self._entries.pop(0)
            size -= len(removed.get('text', '')) + len(removed.get('details', '')) + 100
            if self._stream_index is not None:
                self._stream_index = self._stream_index - 1 if self._stream_index else None
            self._trimmed = True
        lines, styles, prefixes, entry_starts = [], [], [], []

        def add(text, style='', prefix=('', '  ')):
            lines.append(text)
            styles.append(style)
            prefixes.append(prefix)

        if self._trimmed:
            add('[화면 기록 한도로 오래된 출력 생략 · 저장 세션과 모델 컨텍스트 한도는 별도]',
                'class:body.system')
        entries = self._entries
        if self.show_tool_details and self.detail:
            detail = safe_text(f'Status: {self.status}\n{self.detail}')
            budget = max(0, min(99980, 200000 - size - 100))
            if len(detail) > budget:
                detail = '[상태 상세: 화면 기록 한도로 뒷부분 생략]\n' + detail[:budget]
            self._status_entry['text'] = detail
            self._status_entry['error'] = self.status == 'error'
            entries = [*entries, self._status_entry]
        for entry in entries:
            if lines:
                add('')
            start = len(lines)
            role = entry['role']
            category = role if role in ('user', 'assistant') else 'tool' if role.startswith('tool ') else 'system'
            if category == 'tool':
                tool_style = 'class:error' if entry.get('error') else 'class:role.tool'
                add(('ERROR ' if entry.get('error') else '') + role[5:], tool_style, (tool_style, '● '))
            entry_starts.append((entry, start, entry.get('_text_offset', 0), len(lines)))
            text = entry['text']
            if 'details' in entry:
                text += ('\n' + entry['details']) if self.show_tool_details else ' [Ctrl+O details]'
            in_code = False
            for number, body_line in enumerate(text.split('\n')):
                style = 'class:error' if entry.get('error') else 'class:body.' + category
                if category == 'assistant':
                    if body_line.lstrip().startswith(('```', '~~~')):
                        style = 'class:fence'
                        in_code = not in_code
                    elif in_code:
                        style = 'class:code'
                    elif body_line.startswith(('# ', '## ', '### ', '#### ')):
                        style = 'class:heading'
                prefix = ('  ⎿ ' if number == 0 else '    ') if category == 'tool' else (
                    {'user': '❯ ', 'assistant': '● '}.get(category, '  ') if number == 0 else '  ')
                add(body_line, style, ('class:role.' + category, prefix))
        text = '\n'.join(lines)
        self._line_prefixes = prefixes
        self._entry_starts = entry_starts
        self.transcript.lexer = TranscriptLexer(styles)
        buffer = self.transcript.buffer
        if not self.follow_output and text != buffer.text:
            self.unseen_output = True
        cursor = len(text) if self.follow_output else min(buffer.cursor_position, len(text))
        buffer.set_document(Document(text, cursor), bypass_readonly=True)


def run_tui(controller, input=None, output=None) -> int:
    """Run on the calling thread; model/tool operations belong to the controller."""
    ui = DavidTUI(controller, input=input, output=output)
    return ui.application.run()
